"""
Prometheus Hyperscale — Data Center Sentiment Report generator.

Produces a self-contained, externally-presentable HTML report from the
Resistance Dashboard database. The report is intended for sharing outside
the company (e.g. attached to an email to a partner), so it must match the
live dashboard's default data-integrity filter (high confidence only),
filter out non-US and junk state buckets, and lead with the headline
finding (Wyoming's position among high-volume states).
"""

import json
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import db
import geo
import sentiment_index
from shared import load_config

WYOMING_KEY = "wyoming"
HIGH_VOLUME_THRESHOLD = 50
CURATED_ARTICLES_LIMIT = 5
KEY_CLAIMS_EXCERPT_CHARS = 200


def generate_export_html(db_path):
    """Generate a self-contained HTML report file. Returns the path."""
    conn = db.get_connection(db_path)
    try:
        report_data = _build_report_data(conn)
    finally:
        conn.close()

    template_path = Path(__file__).parent / "templates" / "export_template.html"
    template = template_path.read_text(encoding="utf-8")

    # Escape </ to prevent the embedded JSON from breaking out of <script>
    report_json = json.dumps(report_data, indent=2, default=str).replace("</", "<\\/")
    html = template.replace("{{REPORT_DATA}}", report_json)
    html = html.replace(
        "{{GENERATED_DATE}}",
        datetime.now(timezone.utc).replace(tzinfo=None).strftime("%B %d, %Y"),
    )

    out = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
    out.write(html)
    out.close()
    return out.name


def _build_report_data(conn):
    """Aggregate all sections of the report into one JSON-serializable dict."""
    overview = _get_overview(conn)
    ranking = _get_state_ranking(conn)
    trend_overall = _get_overall_trend(conn)
    wyoming = _get_wyoming_bundle(conn)
    methodology = _get_methodology(conn, overview)

    return {
        "generated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "overview": overview,
        "ranking": ranking,
        "trend_overall": trend_overall,
        "wyoming": wyoming,
        "methodology": methodology,
    }


# ────────────────────────────────────────────────────────────
# Overview
# ────────────────────────────────────────────────────────────

def _get_overview(conn):
    """Corpus-wide counts and averages, matching the live dashboard default."""
    pred = db.high_confidence_predicate("articles")
    params = db.low_confidence_params()

    total_unfiltered = conn.execute(
        "SELECT COUNT(*) AS c FROM articles"
    ).fetchone()["c"]

    analyzed = conn.execute(
        f"SELECT COUNT(*) AS c FROM articles WHERE analyzed = 1 AND {pred}",
        params,
    ).fetchone()["c"]

    excluded_low_conf = conn.execute(
        f"SELECT COUNT(*) AS c FROM articles WHERE analyzed = 1 AND NOT ({pred})",
        params,
    ).fetchone()["c"]

    avg_row = conn.execute(
        f"SELECT AVG(sentiment_score) AS avg FROM articles WHERE analyzed = 1 AND {pred}",
        params,
    ).fetchone()
    avg_sentiment = round(avg_row["avg"], 2) if avg_row and avg_row["avg"] is not None else None

    date_range = conn.execute(
        f"SELECT MIN(published_date) AS dmin, MAX(published_date) AS dmax "
        f"FROM articles WHERE analyzed = 1 AND {pred} AND published_date IS NOT NULL",
        params,
    ).fetchone()
    date_start = (date_range["dmin"] or "")[:10] if date_range else None
    date_end = (date_range["dmax"] or "")[:10] if date_range else None

    overall_bundle = sentiment_index.compute_wsi_bundle(conn, include_low_confidence=False)

    # Count the states represented in the high-confidence corpus (US states only)
    us_states = set(geo.get_all_states().keys())
    state_rows = conn.execute(
        f"SELECT DISTINCT state FROM article_states "
        f"WHERE article_id IN (SELECT id FROM articles WHERE analyzed = 1 AND {pred})",
        params,
    ).fetchall()
    states_covered = sum(1 for r in state_rows if r["state"] in us_states)

    # County count within US articles
    county_rows = conn.execute(
        f"SELECT COUNT(DISTINCT county_fips) AS c FROM article_states "
        f"WHERE county_fips IS NOT NULL "
        f"AND article_id IN (SELECT id FROM articles WHERE analyzed = 1 AND {pred})",
        params,
    ).fetchone()
    counties_covered = county_rows["c"] if county_rows else 0

    return {
        "total_articles": total_unfiltered,
        "analyzed_articles": analyzed,
        "excluded_low_confidence": excluded_low_conf,
        "avg_sentiment": avg_sentiment,
        "overall_wsi": overall_bundle.get("current_wsi"),
        "overall_period_comparison": overall_bundle.get("period_comparison"),
        "date_range_start": date_start,
        "date_range_end": date_end,
        "states_covered": states_covered,
        "counties_covered": counties_covered,
    }


# ────────────────────────────────────────────────────────────
# State ranking (split into high-volume / low-volume)
# ────────────────────────────────────────────────────────────

def _state_article_counts(conn):
    """Article count per US state under the default high-confidence filter."""
    pred = db.high_confidence_predicate("a")
    params = db.low_confidence_params()
    rows = conn.execute(
        f"SELECT s.state, COUNT(DISTINCT a.id) AS c "
        f"FROM articles a JOIN article_states s ON s.article_id = a.id "
        f"WHERE a.analyzed = 1 AND {pred} "
        f"GROUP BY s.state",
        params,
    ).fetchall()
    return {r["state"]: r["c"] for r in rows}


def _get_state_ranking(conn):
    """Return ranked lists of US states, split by article-volume threshold."""
    us_states = geo.get_all_states()
    counts = _state_article_counts(conn)

    rows = []
    for state_key, info in us_states.items():
        count = counts.get(state_key, 0)
        if count == 0:
            continue
        bundle = sentiment_index.compute_wsi_bundle(
            conn, state=state_key, include_low_confidence=False
        )
        rows.append({
            "state_key": state_key,
            "state_name": info["name"],
            "state_abbr": info["abbr"],
            "articles": count,
            "wsi": bundle.get("current_wsi"),
            "period_comparison": bundle.get("period_comparison") or {},
        })

    def _sort_key(r):
        # Sort WSI desc; None values sort to the bottom
        return (-(r["wsi"] if r["wsi"] is not None else -1), -r["articles"])

    rows.sort(key=_sort_key)
    high_volume = [r for r in rows if r["articles"] >= HIGH_VOLUME_THRESHOLD]
    low_volume = [r for r in rows if r["articles"] < HIGH_VOLUME_THRESHOLD]
    return {
        "high_volume": high_volume,
        "low_volume": low_volume,
        "threshold": HIGH_VOLUME_THRESHOLD,
    }


# ────────────────────────────────────────────────────────────
# Overall trend
# ────────────────────────────────────────────────────────────

def _get_overall_trend(conn):
    """Weekly WSI trend for the full (high-confidence, US-only) corpus."""
    trend = sentiment_index.compute_wsi_bundle(conn, include_low_confidence=False).get("trend", [])
    return [
        {
            "week": t["week"],
            "wsi": t["wsi"],
            "raw": t["raw"],
            "articles": t["articles"],
            "clusters": t["clusters"],
            "carried": t["carried"],
        }
        for t in trend
    ]


# ────────────────────────────────────────────────────────────
# Wyoming deep-dive
# ────────────────────────────────────────────────────────────

def _wy_base_predicate():
    """SQL predicate and params for 'high-confidence + in Wyoming via article_states'."""
    pred = db.high_confidence_predicate("articles")
    params = db.low_confidence_params()
    where = (
        f"analyzed = 1 AND {pred} "
        f"AND id IN (SELECT article_id FROM article_states WHERE state = ?)"
    )
    params = list(params) + [WYOMING_KEY]
    return where, params


def _get_wyoming_trend(conn):
    bundle = sentiment_index.compute_wsi_bundle(
        conn, state=WYOMING_KEY, include_low_confidence=False
    )
    return [
        {
            "week": t["week"],
            "wsi": t["wsi"],
            "raw": t["raw"],
            "articles": t["articles"],
            "clusters": t["clusters"],
            "carried": t["carried"],
        }
        for t in bundle.get("trend", [])
    ]


def _get_sentiment_distribution(conn, state=None):
    """Counts per sentiment_label, high-confidence only, optionally state-scoped."""
    pred = db.high_confidence_predicate("articles")
    params = list(db.low_confidence_params())
    where = f"analyzed = 1 AND {pred} AND sentiment_label IS NOT NULL"
    if state:
        where += " AND id IN (SELECT article_id FROM article_states WHERE state = ?)"
        params.append(state)
    rows = conn.execute(
        f"SELECT sentiment_label, COUNT(*) AS c FROM articles WHERE {where} GROUP BY sentiment_label",
        params,
    ).fetchall()
    label_order = [
        "strongly_positive", "slightly_positive", "neutral",
        "slightly_negative", "strongly_negative",
    ]
    dist = {r["sentiment_label"]: r["c"] for r in rows}
    return {lbl: dist.get(lbl, 0) for lbl in label_order}


def _topic_labels_map():
    """Map from config topic key → display label."""
    config = load_config()
    return {t["key"]: t.get("label", t["key"]) for t in config.get("topics", [])}


def _get_topics(conn, state=None, limit=8):
    """Topic distribution for the high-confidence corpus, optionally state-scoped."""
    pred = db.high_confidence_predicate("articles")
    params = list(db.low_confidence_params())
    where = f"analyzed = 1 AND {pred}"
    if state:
        where += " AND id IN (SELECT article_id FROM article_states WHERE state = ?)"
        params.append(state)
    rows = conn.execute(
        f"SELECT topic_tags, sentiment_score FROM articles WHERE {where}",
        params,
    ).fetchall()

    label_map = _topic_labels_map()
    data = defaultdict(lambda: {"count": 0, "scores": []})
    for r in rows:
        tags = json.loads(r["topic_tags"]) if r["topic_tags"] else []
        for tag in tags:
            data[tag]["count"] += 1
            if r["sentiment_score"] is not None:
                data[tag]["scores"].append(r["sentiment_score"])

    topics = []
    for key, d in data.items():
        avg = round(sum(d["scores"]) / len(d["scores"]), 2) if d["scores"] else None
        topics.append({
            "key": key,
            "label": label_map.get(key, key.replace("_", " ").title()),
            "count": d["count"],
            "avg": avg,
        })
    topics.sort(key=lambda x: x["count"], reverse=True)
    return topics[:limit]


def _get_entities(conn, state=None, limit=10):
    """Top entities by mention count, optionally state-scoped."""
    pred = db.high_confidence_predicate("articles")
    params = list(db.low_confidence_params())
    where = f"analyzed = 1 AND {pred}"
    if state:
        where += " AND id IN (SELECT article_id FROM article_states WHERE state = ?)"
        params.append(state)
    rows = conn.execute(
        f"SELECT entities_mentioned FROM articles WHERE {where}",
        params,
    ).fetchall()
    counts = defaultdict(int)
    for r in rows:
        ents = json.loads(r["entities_mentioned"]) if r["entities_mentioned"] else []
        for e in ents:
            counts[e] += 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [{"name": n, "count": c} for n, c in ranked[:limit]]


def _get_wyoming_counties(conn):
    """County-level breakdown of Wyoming coverage."""
    pred_a = db.high_confidence_predicate("a")
    params = list(db.low_confidence_params()) + [WYOMING_KEY]
    rows = conn.execute(
        f"""
        SELECT s.county_fips, s.county_name,
               COUNT(DISTINCT a.id) AS count,
               AVG(a.sentiment_score) AS avg
        FROM articles a
        JOIN article_states s ON s.article_id = a.id
        WHERE a.analyzed = 1 AND {pred_a} AND s.state = ?
        GROUP BY s.county_fips, s.county_name
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
        county_name = r["county_name"]
        if not county_name and not r["county_fips"]:
            county_name = "Statewide / Unspecified"
        out.append({
            "fips": r["county_fips"],
            "county_name": county_name or "—",
            "count": r["count"],
            "avg": round(r["avg"], 2) if r["avg"] is not None else None,
        })
    return out


def _excerpt_key_claims(raw, limit=KEY_CLAIMS_EXCERPT_CHARS):
    """Render key_claims (which may be JSON list or a plain string) as a short excerpt."""
    if not raw:
        return ""
    text = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            text = " • ".join(str(x) for x in parsed if x)
        elif isinstance(parsed, str):
            text = parsed
        else:
            text = str(parsed)
    except (json.JSONDecodeError, TypeError):
        text = raw
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:—-") + "…"
    return text


def _fetch_curated_wyoming(conn, order):
    """Return the top/bottom 5 Wyoming articles by sentiment_score."""
    if order not in ("DESC", "ASC"):
        raise ValueError("order must be 'DESC' or 'ASC'")
    where, params = _wy_base_predicate()
    rows = conn.execute(
        f"""
        SELECT id, title, url, source, published_date, sentiment_score, sentiment_label,
               state, location_relevance, key_claims
        FROM articles
        WHERE {where} AND sentiment_score IS NOT NULL
        ORDER BY sentiment_score {order}, published_date DESC
        LIMIT ?
        """,
        params + [CURATED_ARTICLES_LIMIT],
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "source": r["source"],
            "published_date": r["published_date"],
            "sentiment_score": r["sentiment_score"],
            "sentiment_label": r["sentiment_label"],
            "state": r["state"],
            "location_relevance": r["location_relevance"],
            "key_claims_excerpt": _excerpt_key_claims(r["key_claims"]),
        }
        for r in rows
    ]


def _get_wyoming_bundle(conn):
    """All Wyoming-specific data for the deep-dive section."""
    bundle = sentiment_index.compute_wsi_bundle(
        conn, state=WYOMING_KEY, include_low_confidence=False
    )
    counts = _state_article_counts(conn)
    info = geo.get_state_info(WYOMING_KEY) or {}
    return {
        "state_name": info.get("name", "Wyoming"),
        "state_abbr": info.get("abbr", "WY"),
        "articles": counts.get(WYOMING_KEY, 0),
        "wsi": bundle.get("current_wsi"),
        "period_comparison": bundle.get("period_comparison") or {},
        "trend": _get_wyoming_trend(conn),
        "label_distribution": _get_sentiment_distribution(conn, state=WYOMING_KEY),
        "topics": _get_topics(conn, state=WYOMING_KEY, limit=8),
        "entities": _get_entities(conn, state=WYOMING_KEY, limit=10),
        "counties": _get_wyoming_counties(conn),
        "top_positive": _fetch_curated_wyoming(conn, "DESC"),
        "top_negative": _fetch_curated_wyoming(conn, "ASC"),
    }


# ────────────────────────────────────────────────────────────
# Methodology appendix
# ────────────────────────────────────────────────────────────

def _get_sources(conn, limit=15):
    """Top news sources by article count (high-confidence analyzed)."""
    pred = db.high_confidence_predicate("articles")
    params = db.low_confidence_params()
    rows = conn.execute(
        f"SELECT source, COUNT(*) AS c FROM articles "
        f"WHERE analyzed = 1 AND {pred} AND source IS NOT NULL "
        f"GROUP BY source ORDER BY c DESC LIMIT ?",
        list(params) + [limit],
    ).fetchall()
    return [{"source": r["source"], "count": r["c"]} for r in rows]


def _get_methodology(conn, overview):
    """Metadata for the methodology appendix."""
    config = load_config()
    anthropic_cfg = config.get("anthropic", {}) or {}
    return {
        "sources": _get_sources(conn, limit=15),
        "date_range": {
            "start": overview.get("date_range_start"),
            "end": overview.get("date_range_end"),
        },
        "classification_model": anthropic_cfg.get("classification_model", "claude-haiku-4-5"),
        "synthesis_model": anthropic_cfg.get("synthesis_model", "claude-sonnet-4-5"),
        "total_articles": overview.get("total_articles"),
        "analyzed_articles": overview.get("analyzed_articles"),
        "excluded_low_confidence": overview.get("excluded_low_confidence"),
        "wsi_half_life_weeks": sentiment_index.WSI_HALF_LIFE_WEEKS,
        "wsi_cluster_entity_threshold": sentiment_index.WSI_CLUSTER_ENTITY_THRESHOLD,
        "wsi_cluster_date_window_days": sentiment_index.WSI_CLUSTER_DATE_WINDOW_DAYS,
        "wsi_weeks_back": sentiment_index.WSI_WEEKS_BACK,
    }
