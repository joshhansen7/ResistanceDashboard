"""
Prometheus — Export Report Generator
Generates a self-contained HTML report for sharing with leadership.
"""

import json
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import db
import sentiment_index


STATE_LOCATIONS = {
    "wyoming": ["evanston", "casper", "cheyenne", "statewide"],
    "texas": ["dallas", "statewide"],
    "michigan": ["ann_arbor", "van_buren", "benton_harbor", "statewide"],
}


def generate_export_html(db_path):
    """
    Generate a self-contained HTML report file.
    Returns the path to the generated file.
    """
    conn = db.get_connection(db_path)

    try:
        # Gather all data
        report_data = {
            "generated": datetime.utcnow().isoformat(),
            "overview": _get_overview(conn),
            "wsi": _get_wsi(conn),
            "trend": _get_trend(conn),
            "locations": _get_locations(conn),
            "topics": _get_topics(conn),
            "entities": _get_entities(conn),
            "articles": _get_articles(conn),
        }
    finally:
        conn.close()

    # Render the template
    template_path = Path(__file__).parent / "templates" / "export_template.html"
    template = template_path.read_text(encoding="utf-8")

    html = template.replace("{{REPORT_DATA}}", json.dumps(report_data, indent=2))
    html = html.replace("{{GENERATED_DATE}}", datetime.utcnow().strftime("%B %d, %Y"))

    # Write to temp file
    out = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
    out.write(html)
    out.close()
    return out.name


def _get_overview(conn):
    total = conn.execute("SELECT COUNT(*) as c FROM articles").fetchone()["c"]
    analyzed = conn.execute("SELECT COUNT(*) as c FROM articles WHERE analyzed = 1").fetchone()["c"]
    avg_row = conn.execute("SELECT AVG(sentiment_score) as avg FROM articles WHERE analyzed = 1").fetchone()
    return {
        "total_articles": total,
        "analyzed_articles": analyzed,
        "avg_sentiment": round(avg_row["avg"], 2) if avg_row["avg"] else None,
    }


def _get_wsi(conn):
    """Get WSI data for the report."""
    wsi = sentiment_index.compute_wsi(conn)
    comparison = sentiment_index.compute_period_comparison(conn)
    state_wsi = {}
    for state in STATE_LOCATIONS:
        sw = sentiment_index.compute_wsi(conn, state=state)
        state_wsi[state] = sw.get("current_wsi")
    return {
        "current_wsi": wsi.get("current_wsi"),
        "raw_avg": wsi.get("raw_avg"),
        "period_comparison": comparison,
        "by_state": state_wsi,
    }


def _get_trend(conn):
    """Get weekly WSI trend for the report."""
    trend = sentiment_index.compute_wsi_trend(conn)
    return [
        {"week": t["week"], "wsi": t["wsi"], "raw": t["raw"],
         "articles": t["articles"], "clusters": t["clusters"], "carried": t["carried"]}
        for t in trend
    ]


def _get_locations(conn):
    """Get location sentiment for all tracked states."""
    result = {}
    for state, locs in STATE_LOCATIONS.items():
        for loc in locs:
            row = conn.execute(
                "SELECT AVG(sentiment_score) as avg, COUNT(*) as count "
                "FROM articles WHERE analyzed = 1 AND state = ? AND location_relevance = ?",
                (state, loc),
            ).fetchone()
            key = f"{state}:{loc}"
            result[key] = {
                "avg": round(row["avg"], 2) if row["avg"] else None,
                "count": row["count"],
                "state": state,
                "location": loc,
            }
    return result


def _get_topics(conn):
    rows = conn.execute(
        "SELECT topic_tags, sentiment_score FROM articles WHERE analyzed = 1"
    ).fetchall()
    topic_data = defaultdict(lambda: {"scores": [], "count": 0})
    for row in rows:
        tags = json.loads(row["topic_tags"]) if row["topic_tags"] else []
        for tag in tags:
            topic_data[tag]["count"] += 1
            if row["sentiment_score"] is not None:
                topic_data[tag]["scores"].append(row["sentiment_score"])
    return [
        {"name": name, "count": d["count"],
         "avg": round(sum(d["scores"]) / len(d["scores"]), 2) if d["scores"] else None}
        for name, d in sorted(topic_data.items(), key=lambda x: x[1]["count"], reverse=True)
    ]


def _get_entities(conn):
    rows = conn.execute("SELECT entities_mentioned FROM articles WHERE analyzed = 1").fetchall()
    counts = defaultdict(int)
    for row in rows:
        ents = json.loads(row["entities_mentioned"]) if row["entities_mentioned"] else []
        for e in ents:
            counts[e] += 1
    return [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda x: x[1], reverse=True)]


def _get_articles(conn):
    rows = conn.execute(
        "SELECT title, source, published_date, sentiment_score, sentiment_label, "
        "state, location_relevance, key_claims "
        "FROM articles WHERE analyzed = 1 ORDER BY published_date DESC LIMIT 50"
    ).fetchall()
    return [
        {
            "title": r["title"], "source": r["source"],
            "date": r["published_date"],
            "score": r["sentiment_score"], "label": r["sentiment_label"],
            "state": r["state"], "location": r["location_relevance"],
            "claims": r["key_claims"],
        }
        for r in rows
    ]
