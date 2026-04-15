"""
Wyoming Pulse — Dashboard API Endpoints
All /api/* routes returning JSON for the frontend.
"""

import json
import logging
import threading
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, send_file

import db
import geo
import sentiment_index
from shared import load_config

logger = logging.getLogger("wyoming_pulse.api")

bp = Blueprint("api", __name__, url_prefix="/api")


def local_only(f):
    """Reject requests not originating from localhost."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        remote = request.remote_addr or ""
        if remote not in ("127.0.0.1", "::1", "localhost"):
            return jsonify({"error": "forbidden: local access only"}), 403
        return f(*args, **kwargs)
    return wrapper

# ──────────────────────────────────────────────
# Background task runner
# ──────────────────────────────────────────────
_tasks = {}
_tasks_lock = threading.Lock()


def _run_in_background(task_type, target_fn, **kwargs):
    """Spawn a daemon thread to run target_fn. Returns task_id immediately."""
    task_id = str(uuid.uuid4())[:8]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "type": task_type,
            "status": "running",
            "started": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "finished": None,
            "result": None,
            "error": None,
            "progress": None,
        }
        # Prune old tasks to prevent unbounded memory growth
        if len(_tasks) > 50:
            # Remove oldest entries by start time, keep most recent 50
            sorted_ids = sorted(_tasks, key=lambda k: _tasks[k].get("started", ""))
            for old_id in sorted_ids[:-50]:
                del _tasks[old_id]

    def _worker():
        try:
            result = target_fn(**kwargs)
            with _tasks_lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["result"] = result
        except Exception as e:
            logger.error("Task %s (%s) failed: %s", task_id, task_type, e)
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return task_id


def _create_tracked_task(task_type):
    """Create a task record with progress support and return its id."""
    task_id = str(uuid.uuid4())[:8]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "type": task_type,
            "status": "running",
            "started": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "finished": None,
            "result": None,
            "error": None,
            "progress": None,
        }
    return task_id


def _update_task_progress(task_id, progress):
    """Update progress payload for a tracked task."""
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id]["progress"] = progress


def get_conn():
    """Get a database connection using the configured path."""
    return db.get_connection(current_app.config["DB_PATH"])


def _safe_json_loads(value, default):
    """Safely parse a JSON column value; return default on failure or empty."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _active_states(conn):
    """Return tracked states with counts and reference data."""
    rows = conn.execute(
        "SELECT ast.state, COUNT(DISTINCT ast.article_id) as count "
        "FROM article_states ast "
        "JOIN articles a ON a.id = ast.article_id "
        "WHERE a.analyzed = 1 AND ast.state IS NOT NULL "
        "AND ast.state NOT IN ('nationwide', 'other') "
        "GROUP BY ast.state ORDER BY ast.state ASC"
    ).fetchall()

    states_list = []
    for r in rows:
        key = r["state"]
        info = geo.get_state_info(key)
        states_list.append({
            "key": key,
            "name": info["name"] if info else key.title(),
            "abbr": info["abbr"] if info else key.upper()[:2],
            "fips": info["fips"] if info else None,
            "article_count": r["count"],
        })
    return states_list


def fips_pair(fips):
    """Return (unpadded, padded) tuple for FIPS code matching across both formats in DB."""
    s = str(fips).strip()
    return (s.lstrip("0") or "0", s.zfill(5))


# ──────────────────────────────────────────────
# /api/overview
# ──────────────────────────────────────────────
def _state_filter(include_analyzed=True):
    """Build WHERE clauses and params with optional ?state= and ?county_fips= filters.

    Returns (where_sql, params) where where_sql is a composed WHERE clause string.
    When include_analyzed is True, includes the `analyzed = 1` predicate; when
    False, omits it (used by callers that need to count both analyzed and
    pending rows). Uses the article_states junction table so multi-state
    articles count for all their states. county_fips takes precedence over state.
    """
    clauses = []
    params = []
    if include_analyzed:
        clauses.append("analyzed = 1")

    county_fips = request.args.get("county_fips")
    if county_fips:
        fips_unpadded, fips_padded = fips_pair(county_fips)
        clauses.append(
            "articles.id IN (SELECT article_id FROM article_states WHERE county_fips IN (?, ?))"
        )
        params.extend([fips_unpadded, fips_padded])
    else:
        state = request.args.get("state")
        if state:
            clauses.append(
                "articles.id IN (SELECT article_id FROM article_states WHERE state = ?)"
            )
            params.append(state)

    where_sql = " AND ".join(clauses) if clauses else "1=1"
    return where_sql, params


@bp.route("/overview")
def overview():
    conn = get_conn()
    try:
        where, params = _state_filter()
        base_where, _ = _state_filter(include_analyzed=False)  # for total (includes unanalyzed)

        total = conn.execute(
            f"SELECT COUNT(*) as c FROM articles WHERE {base_where}", params
        ).fetchone()["c"]
        analyzed = conn.execute(
            f"SELECT COUNT(*) as c FROM articles WHERE {where}", params
        ).fetchone()["c"]
        pending = total - analyzed

        avg_row = conn.execute(
            f"SELECT AVG(sentiment_score) as avg FROM articles WHERE {where}", params
        ).fetchone()
        avg_sentiment = round(avg_row["avg"], 2) if avg_row["avg"] else None

        # Period comparison: last 14 days vs previous 14 days
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        current_start = (now - timedelta(days=14)).isoformat()
        prev_start = (now - timedelta(days=28)).isoformat()
        prev_end = current_start

        cur_avg_row = conn.execute(
            f"SELECT AVG(sentiment_score) as avg FROM articles "
            f"WHERE {where} AND published_date >= ?",
            params + [current_start],
        ).fetchone()
        prev_avg_row = conn.execute(
            f"SELECT AVG(sentiment_score) as avg FROM articles "
            f"WHERE {where} AND published_date >= ? AND published_date < ?",
            params + [prev_start, prev_end],
        ).fetchone()

        cur_avg = round(cur_avg_row["avg"], 2) if cur_avg_row["avg"] else None
        prev_avg = round(prev_avg_row["avg"], 2) if prev_avg_row["avg"] else None
        sentiment_change = None
        if cur_avg is not None and prev_avg is not None:
            sentiment_change = round(cur_avg - prev_avg, 2)

        # Sentiment distribution by label
        dist_rows = conn.execute(
            f"SELECT sentiment_label, COUNT(*) as c FROM articles "
            f"WHERE {where} AND sentiment_label IS NOT NULL "
            f"GROUP BY sentiment_label", params
        ).fetchall()
        sentiment_distribution = {
            "strongly_negative": 0, "slightly_negative": 0,
            "neutral": 0, "slightly_positive": 0, "strongly_positive": 0,
        }
        for r in dist_rows:
            if r["sentiment_label"] in sentiment_distribution:
                sentiment_distribution[r["sentiment_label"]] = r["c"]

        last_ingest = conn.execute(
            "SELECT run_date FROM feed_runs ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
        last_analysis = conn.execute(
            "SELECT analyzed_date FROM articles WHERE analyzed = 1 "
            "ORDER BY analyzed_date DESC LIMIT 1"
        ).fetchone()

        return jsonify({
            "total_articles": total,
            "analyzed_articles": analyzed,
            "pending_articles": pending,
            "avg_sentiment": avg_sentiment,
            "current_period_avg": cur_avg,
            "previous_period_avg": prev_avg,
            "sentiment_change": sentiment_change,
            "sentiment_distribution": sentiment_distribution,
            "last_ingestion": last_ingest["run_date"] if last_ingest else None,
            "last_analysis": last_analysis["analyzed_date"] if last_analysis else None,
        })
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/states
# ──────────────────────────────────────────────
@bp.route("/states")
def states():
    """Return all states with analyzed articles, with reference data."""
    conn = get_conn()
    try:
        return jsonify({"states": _active_states(conn)})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/state-locations
# ──────────────────────────────────────────────
@bp.route("/state-locations")
def state_locations():
    """Return per-state location options derived from config."""
    config = load_config()
    locations = {}
    for state_key, state_cfg in config.get("priority_states", {}).items():
        locs = state_cfg.get("locations", {})
        entries = []
        for loc_key, _keywords in locs.items():
            label = loc_key.replace("_", " ").title()
            entries.append([loc_key, label])
        if entries:
            locations[state_key] = entries
    return jsonify({"locations": locations})


# ──────────────────────────────────────────────
# /api/sentiment-trend
# ──────────────────────────────────────────────
@bp.route("/sentiment-trend")
def sentiment_trend():
    conn = get_conn()
    try:
        where, params = _state_filter()
        rows = conn.execute(
            f"SELECT DATE(published_date) as date, "
            f"AVG(sentiment_score) as avg, COUNT(*) as count "
            f"FROM articles WHERE {where} AND published_date IS NOT NULL "
            f"GROUP BY DATE(published_date) ORDER BY date ASC",
            params,
        ).fetchall()

        data = [
            {"date": r["date"], "avg_sentiment": round(r["avg"], 2), "count": r["count"]}
            for r in rows
        ]
        return jsonify({"data": data})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/sentiment-index
# ──────────────────────────────────────────────
@bp.route("/sentiment-index")
def sentiment_index_endpoint():
    """Weighted Sentiment Index: current value, trend, and period comparison."""
    conn = get_conn()
    try:
        state = request.args.get("state") or None
        county_fips = request.args.get("county_fips") or None
        weeks_back = request.args.get("weeks_back", 26, type=int)
        bundle = sentiment_index.compute_wsi_bundle(
            conn,
            state=state,
            county_fips=county_fips,
            weeks_back=weeks_back,
        )

        return jsonify({
            "current_wsi": bundle["current_wsi"],
            "raw_avg": bundle["raw_avg"],
            "trend": bundle["trend"],
            "period_comparison": bundle["period_comparison"],
        })
    finally:
        conn.close()


@bp.route("/sentiment-index-cards")
def sentiment_index_cards():
    """Batch sentiment-index summaries for the period cards view."""
    conn = get_conn()
    try:
        weeks_back = request.args.get("weeks_back", 26, type=int)
        cards = []

        overall = sentiment_index.compute_wsi_bundle(conn, weeks_back=weeks_back)
        cards.append({
            "key": "",
            "name": "All States",
            "current_wsi": overall["current_wsi"],
            "period_comparison": overall["period_comparison"],
        })

        for state_info in _active_states(conn):
            bundle = sentiment_index.compute_wsi_bundle(
                conn,
                state=state_info["key"],
                weeks_back=weeks_back,
            )
            cards.append({
                "key": state_info["key"],
                "name": state_info["name"],
                "current_wsi": bundle["current_wsi"],
                "period_comparison": bundle["period_comparison"],
            })

        return jsonify({"cards": cards})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/state-sentiment
# ──────────────────────────────────────────────
@bp.route("/state-fips")
def state_fips():
    """Return {state_key: fips_code} for all US states."""
    states = geo.get_all_states()
    result = {}
    for key, info in states.items():
        result[key] = info.get("fips")
    return jsonify(result)


@bp.route("/state-sentiment")
def state_sentiment():
    """Per-state average sentiment for the US map."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ast.state, AVG(a.sentiment_score) as avg, "
            "COUNT(DISTINCT ast.article_id) as count "
            "FROM article_states ast "
            "JOIN articles a ON a.id = ast.article_id "
            "WHERE a.analyzed = 1 AND ast.state IS NOT NULL "
            "GROUP BY ast.state"
        ).fetchall()
        result = {}
        for r in rows:
            if r["state"] and r["state"] not in ("nationwide", "other"):
                result[r["state"]] = {
                    "avg": round(r["avg"], 2) if r["avg"] else None,
                    "count": r["count"],
                }
        return jsonify(result)
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/locations
# ──────────────────────────────────────────────
@bp.route("/locations")
def locations():
    """Per-location sentiment for a state, normalized to county level.
    Uses article_states junction table for consistent FIPS-based grouping.
    """
    conn = get_conn()
    try:
        state = request.args.get("state")
        if not state:
            return jsonify({"error": "state parameter is required"}), 400

        # Query via article_states for FIPS-based county grouping
        rows = conn.execute(
            "SELECT ast.county_fips, ast.county_name, a.sentiment_score "
            "FROM article_states ast "
            "JOIN articles a ON a.id = ast.article_id "
            "WHERE a.analyzed = 1 AND ast.state = ? AND a.sentiment_score IS NOT NULL",
            (state,),
        ).fetchall()

        county_scores = defaultdict(list)  # county_name -> [scores]
        county_fips_map = {}  # county_name -> fips
        for r in rows:
            fips = r["county_fips"]
            name = r["county_name"]
            if fips and name:
                county_scores[name].append(r["sentiment_score"])
                county_fips_map[name] = fips
            else:
                county_scores["statewide"].append(r["sentiment_score"])

        result = {}
        for name, scores in county_scores.items():
            entry = {
                "avg": round(sum(scores) / len(scores), 2) if scores else None,
                "count": len(scores),
            }
            if name in county_fips_map:
                entry["fips"] = county_fips_map[name].zfill(5)
            result[name] = entry
        return jsonify(result)
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/topics
# ──────────────────────────────────────────────
@bp.route("/topics")
def topics():
    conn = get_conn()
    try:
        where, params = _state_filter()
        rows = conn.execute(
            f"SELECT topic_tags, sentiment_score FROM articles WHERE {where}",
            params,
        ).fetchall()

        topic_data = defaultdict(lambda: {"scores": [], "count": 0})
        for row in rows:
            tags = _safe_json_loads(row["topic_tags"], [])
            for tag in tags:
                topic_data[tag]["count"] += 1
                if row["sentiment_score"] is not None:
                    topic_data[tag]["scores"].append(row["sentiment_score"])

        topics_list = []
        for name, data in sorted(topic_data.items(), key=lambda x: x[1]["count"], reverse=True):
            avg = round(sum(data["scores"]) / len(data["scores"]), 2) if data["scores"] else None
            topics_list.append({"name": name, "count": data["count"], "avg_sentiment": avg})

        return jsonify({"topics": topics_list})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/entities
# ──────────────────────────────────────────────
@bp.route("/entities")
def entities():
    conn = get_conn()
    try:
        where, params = _state_filter()
        rows = conn.execute(
            f"SELECT entities_mentioned, sentiment_score, published_date FROM articles WHERE {where}",
            params,
        ).fetchall()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recent_cutoff = (now - timedelta(days=14)).isoformat()
        prior_cutoff = (now - timedelta(days=28)).isoformat()
        entity_data = defaultdict(lambda: {"count": 0, "scores": [], "recent_scores": [], "prior_scores": []})
        for row in rows:
            ents = _safe_json_loads(row["entities_mentioned"], [])
            pub = row["published_date"] or ""
            for e in ents:
                entity_data[e]["count"] += 1
                if row["sentiment_score"] is not None:
                    entity_data[e]["scores"].append(row["sentiment_score"])
                    if pub >= recent_cutoff:
                        entity_data[e]["recent_scores"].append(row["sentiment_score"])
                    elif pub >= prior_cutoff:
                        entity_data[e]["prior_scores"].append(row["sentiment_score"])

        entities_list = []
        for name, data in sorted(entity_data.items(), key=lambda x: x[1]["count"], reverse=True):
            avg = round(sum(data["scores"]) / len(data["scores"]), 2) if data["scores"] else None
            recent_avg = round(sum(data["recent_scores"]) / len(data["recent_scores"]), 2) if data["recent_scores"] else None
            prior_avg = round(sum(data["prior_scores"]) / len(data["prior_scores"]), 2) if data["prior_scores"] else None
            trend = None
            if recent_avg is not None and prior_avg is not None:
                trend = round(recent_avg - prior_avg, 2)
            entities_list.append({
                "name": name, "count": data["count"],
                "avg_sentiment": avg, "recent_avg": recent_avg,
                "trend": trend, "recent_count": len(data["recent_scores"]),
            })

        return jsonify({"entities": entities_list})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/location-weekly
# ──────────────────────────────────────────────
@bp.route("/location-weekly")
def location_weekly():
    """Per-location weekly sentiment for the heatmap."""
    conn = get_conn()
    try:
        weeks_back = request.args.get("weeks_back", 12, type=int)
        state = request.args.get("state") or None
        sql = (
            "SELECT ast.state as state, ast.county_name as county_name, "
            "a.published_date as published_date, a.sentiment_score as sentiment_score "
            "FROM article_states ast "
            "JOIN articles a ON a.id = ast.article_id "
            "WHERE a.analyzed = 1 AND a.published_date IS NOT NULL "
            "AND ast.state IS NOT NULL "
        )
        params = []
        if state:
            sql += "AND ast.state = ? "
            params.append(state)
        sql += "ORDER BY a.published_date ASC"
        rows = conn.execute(sql, params).fetchall()

        # Group by state > county > week. Use county_name from the junction
        # table directly (already normalized at ingest time) — do NOT call
        # geo.normalize_location here, as it can trigger HTTP geocoding.
        from sentiment_index import _week_start
        from datetime import datetime, timedelta, timezone
        data = {}  # {state: {county: {week: [scores]}}}
        for r in rows:
            state = r["state"]
            loc = r["county_name"] or "statewide"
            ws = _week_start(r["published_date"])
            if not ws or not state:
                continue
            data.setdefault(state, {}).setdefault(loc, {}).setdefault(ws, [])
            if r["sentiment_score"] is not None:
                data[state][loc][ws].append(r["sentiment_score"])

        # Build week range
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        current_monday = now - timedelta(days=now.weekday())
        cutoff_monday = current_monday - timedelta(weeks=weeks_back)
        weeks = []
        m = cutoff_monday
        while m <= current_monday:
            weeks.append(m.strftime("%Y-%m-%d"))
            m += timedelta(weeks=1)

        # Build result with carry-forward
        result = {}
        for state, locations in sorted(data.items()):
            result[state] = {}
            for loc, week_scores in sorted(locations.items()):
                cells = []
                last_val = None
                for w in weeks:
                    scores = week_scores.get(w, [])
                    if scores:
                        val = round(sum(scores) / len(scores), 2)
                        last_val = val
                        cells.append({"week": w, "avg": val, "count": len(scores), "carried": False})
                    else:
                        cells.append({"week": w, "avg": last_val, "count": 0, "carried": last_val is not None})
                result[state][loc] = cells

        return jsonify({"data": result, "weeks": weeks})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/articles
# ──────────────────────────────────────────────
@bp.route("/articles")
def articles():
    conn = get_conn()
    try:
        limit = max(1, min(int(request.args.get("limit", 25)), 500))
        offset = max(0, int(request.args.get("offset", 0)))
        location = request.args.get("location", "")
        county_fips = request.args.get("county_fips", "")
        state = request.args.get("state", "")
        sentiment = request.args.get("sentiment_label", "")
        relevance = request.args.get("relevance", "")  # "primary" | "mentioned" | ""

        where_clauses = ["analyzed = 1"]
        params = []

        rel_filter_sql = ""
        rel_filter_params = []
        if relevance in ("primary", "mentioned"):
            rel_filter_sql = " AND relevance = ?"
            rel_filter_params = [relevance]

        if county_fips:
            fips_unpadded, fips_padded = fips_pair(county_fips)
            where_clauses.append(
                "articles.id IN (SELECT article_id FROM article_states WHERE county_fips IN (?, ?)" + rel_filter_sql + ")"
            )
            params.extend([fips_unpadded, fips_padded] + rel_filter_params)
        elif state:
            where_clauses.append(
                "articles.id IN (SELECT article_id FROM article_states WHERE state = ?" + rel_filter_sql + ")"
            )
            params.append(state)
            params.extend(rel_filter_params)
        if location and not county_fips:
            where_clauses.append("location_relevance = ?")
            params.append(location)
        if sentiment:
            where_clauses.append("sentiment_label = ?")
            params.append(sentiment)

        where_sql = " AND ".join(where_clauses)

        total_row = conn.execute(
            f"SELECT COUNT(*) as c FROM articles WHERE {where_sql}", params
        ).fetchone()

        rows = conn.execute(
            f"SELECT id, title, source, source_type, published_date, "
            f"sentiment_score, sentiment_label, voice_type, state, location_relevance, "
            f"url, key_claims, sentiment_justification, topic_tags, entities_mentioned, summary "
            f"FROM articles WHERE {where_sql} "
            f"ORDER BY published_date DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        # If filtering by state/county, look up each article's relevance to
        # that state so the frontend can show "Primary" vs "Mentioned" badges.
        filter_relevance = {}  # article_id -> "primary" | "mentioned"
        if state or county_fips:
            article_ids = [r["id"] for r in rows]
            if article_ids:
                placeholders = ",".join("?" * len(article_ids))
                if county_fips:
                    fips_unpadded, fips_padded = fips_pair(county_fips)
                    rel_rows = conn.execute(
                        f"SELECT article_id, relevance FROM article_states "
                        f"WHERE article_id IN ({placeholders}) AND county_fips IN (?, ?)",
                        article_ids + [fips_unpadded, fips_padded],
                    ).fetchall()
                else:
                    rel_rows = conn.execute(
                        f"SELECT article_id, relevance FROM article_states "
                        f"WHERE article_id IN ({placeholders}) AND state = ?",
                        article_ids + [state],
                    ).fetchall()
                for rr in rel_rows:
                    aid = rr["article_id"]
                    rel = rr["relevance"]
                    # "primary" wins over "mentioned" if multiple entries
                    if filter_relevance.get(aid) != "primary":
                        filter_relevance[aid] = rel

        articles_list = []
        for r in rows:
            article = {
                "id": r["id"],
                "title": r["title"],
                "source": r["source"],
                "source_type": r["source_type"],
                "published_date": r["published_date"],
                "sentiment_score": r["sentiment_score"],
                "sentiment_label": r["sentiment_label"],
                "voice_type": r["voice_type"],
                "state": r["state"],
                "location_relevance": r["location_relevance"],
                "url": r["url"],
                "key_claims": r["key_claims"],
                "sentiment_justification": r["sentiment_justification"],
                "topic_tags": _safe_json_loads(r["topic_tags"], []),
                "entities_mentioned": _safe_json_loads(r["entities_mentioned"], []),
                "summary": r["summary"],
            }
            if filter_relevance:
                article["state_relevance"] = filter_relevance.get(r["id"], "primary")
            articles_list.append(article)

        return jsonify({
            "articles": articles_list,
            "total": total_row["c"],
            "limit": limit,
            "offset": offset,
        })
    finally:
        conn.close()


# ──────────────────────────────────────────────
# PUT /api/articles/<id>  — edit an article's categorisations
# ──────────────────────────────────────────────
@bp.route("/articles/<int:article_id>", methods=["PUT"])
@local_only
def update_article(article_id):
    """Update editable fields on an article."""
    data = request.get_json(force=True)
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "Article not found"}), 404

        allowed = {
            "sentiment_label", "sentiment_score", "voice_type",
            "state", "location_relevance", "topic_tags", "entities_mentioned",
            "key_claims", "sentiment_justification",
        }
        sets = []
        params = []
        for key, val in data.items():
            if key not in allowed:
                continue
            if key == "sentiment_score":
                if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
                    return jsonify({"error": "sentiment_score must be a number between 1.0 and 5.0"}), 400
                if not (1.0 <= float(val) <= 5.0):
                    return jsonify({"error": "sentiment_score must be between 1.0 and 5.0"}), 400
                val = float(val)
            elif key in ("topic_tags", "entities_mentioned"):
                if val is not None and not isinstance(val, (list, str)):
                    return jsonify({"error": f"{key} must be a list"}), 400
                val = json.dumps(val) if isinstance(val, list) else val
            elif key in ("sentiment_label", "voice_type", "state", "location_relevance",
                         "key_claims", "sentiment_justification"):
                if val is not None and not isinstance(val, str):
                    return jsonify({"error": f"{key} must be a string"}), 400
            sets.append(f"{key} = ?")
            params.append(val)

        if not sets:
            return jsonify({"error": "No valid fields to update"}), 400

        params.append(article_id)
        conn.execute(
            f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
        return jsonify({"success": True, "updated": list(data.keys())})
    finally:
        conn.close()


@bp.route("/articles/<int:article_id>", methods=["DELETE"])
@local_only
def delete_article(article_id):
    """Permanently delete an article from the database."""
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id, title FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "Article not found"}), 404

        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        conn.commit()
        logger.info("Deleted article %d: %s", article_id, existing["title"][:60])
        return jsonify({"success": True, "deleted_id": article_id})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/feed-health
# ──────────────────────────────────────────────
@bp.route("/feed-health")
def feed_health():
    conn = get_conn()
    try:
        # Get distinct feed names and their most recent run
        rows = conn.execute(
            "SELECT feed_name, run_date, articles_found, articles_matched, status "
            "FROM feed_runs WHERE id IN ("
            "  SELECT MAX(id) FROM feed_runs GROUP BY feed_name"
            ") ORDER BY feed_name"
        ).fetchall()

        # Count total runs per feed
        run_counts = {}
        count_rows = conn.execute(
            "SELECT feed_name, COUNT(*) as c FROM feed_runs GROUP BY feed_name"
        ).fetchall()
        for r in count_rows:
            run_counts[r["feed_name"]] = r["c"]

        feeds = []
        for r in rows:
            feeds.append({
                "name": r["feed_name"],
                "last_run": r["run_date"],
                "articles_found": r["articles_found"],
                "articles_matched": r["articles_matched"],
                "status": r["status"],
                "total_runs": run_counts.get(r["feed_name"], 0),
            })

        return jsonify({"feeds": feeds})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/digests
# ──────────────────────────────────────────────
@bp.route("/digests")
def digests():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, filename, generated_date, period_start, period_end, "
            "article_count, avg_sentiment FROM digests ORDER BY generated_date DESC"
        ).fetchall()

        digests_list = [
            {
                "id": r["id"],
                "filename": r["filename"],
                "generated_date": r["generated_date"],
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "article_count": r["article_count"],
                "avg_sentiment": r["avg_sentiment"],
            }
            for r in rows
        ]
        return jsonify({"digests": digests_list})
    finally:
        conn.close()


@bp.route("/digest/<int:digest_id>")
def digest_detail(digest_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Digest not found"}), 404
        return jsonify({
            "id": row["id"],
            "filename": row["filename"],
            "generated_date": row["generated_date"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "article_count": row["article_count"],
            "avg_sentiment": row["avg_sentiment"],
            "content": row["content"],
        })
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/export
# ──────────────────────────────────────────────
@bp.route("/export")
def export_report():
    from .export import generate_export_html
    html_path = generate_export_html(current_app.config["DB_PATH"])
    return send_file(html_path, as_attachment=True, download_name="wyoming_pulse_report.html")


# ──────────────────────────────────────────────
# /api/config/* — Configuration Endpoints
# ──────────────────────────────────────────────

@bp.route("/config/topics")
def config_topics():
    """Return the configured topic categories."""
    config = load_config()
    topics = config.get("topics", [])
    return jsonify({"topics": topics})


@bp.route("/config/keywords")
def config_keywords():
    """Return the full keyword configuration for visibility."""
    config = load_config()
    nationwide = config.get("nationwide", {})
    result = {
        "nationwide_keywords": nationwide.get("keywords", {}),
        "nationwide_queries": nationwide.get("web_search_queries", []),
        "priority_states": {},
    }
    for state_key, state_cfg in config.get("priority_states", {}).items():
        result["priority_states"][state_key] = {
            "keywords": state_cfg.get("keywords", {}),
            "web_search_queries": state_cfg.get("web_search_queries", []),
        }
    return jsonify(result)


@bp.route("/resolve-url")
def resolve_url():
    """
    Lazily resolve a Google News URL and redirect to the real article.
    Caches the result back to the DB so subsequent clicks are instant.
    Falls back to the original URL if resolution fails.
    """
    from urllib.parse import urlparse
    from flask import redirect, request as flask_request
    url = flask_request.args.get("url", "")
    table = flask_request.args.get("table", "articles")  # articles or pending_articles

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": "Invalid URL scheme"}), 400

    def _url_in_db(u):
        c = get_conn()
        try:
            r1 = c.execute("SELECT 1 FROM articles WHERE url = ? LIMIT 1", (u,)).fetchone()
            if r1:
                return True
            r2 = c.execute("SELECT 1 FROM pending_articles WHERE url = ? LIMIT 1", (u,)).fetchone()
            return bool(r2)
        finally:
            c.close()

    is_google_news = parsed.hostname == "news.google.com"

    # If not Google News, only redirect if we know about this URL in our DB
    if not is_google_news:
        if not _url_in_db(url):
            return jsonify({"error": "URL not found in database"}), 400
        return redirect(url)

    # Look up the title for DDG fallback
    title = None
    source = None
    conn = get_conn()
    try:
        if table == "pending_articles":
            row = conn.execute(
                "SELECT title, source FROM pending_articles WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT title, source FROM articles WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
        if row:
            title = row["title"]
            source = row["source"]
    finally:
        conn.close()

    from scraper import resolve_google_news_url
    resolved = resolve_google_news_url(url, interval=1, title=title, source=source)

    # Validate resolved URL scheme to ensure safe redirect
    resolved_parsed = urlparse(resolved)
    if resolved_parsed.scheme not in ("http", "https"):
        return jsonify({"error": "Resolver returned invalid URL"}), 400

    # Cache back to DB if resolved
    if resolved != url:
        conn = get_conn()
        try:
            if table == "pending_articles":
                conn.execute("UPDATE pending_articles SET url = ? WHERE url = ?", (resolved, url))
            else:
                conn.execute("UPDATE articles SET url = ? WHERE url = ?", (resolved, url))
            conn.commit()
        finally:
            conn.close()

    return redirect(resolved)


@bp.route("/article/<int:article_id>")
def article_detail(article_id):
    """Return full article details for the detail page."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Article not found"}), 404

        # Parse JSON fields
        topic_tags = _safe_json_loads(row["topic_tags"], [])
        entities = _safe_json_loads(row["entities_mentioned"], [])
        locations = _safe_json_loads(row["locations_json"], [])

        # Get location details from article_states
        state_rows = conn.execute(
            "SELECT * FROM article_states WHERE article_id = ?", (article_id,)
        ).fetchall()
        state_details = [
            {
                "state": sr["state"],
                "place": sr["place"],
                "relevance": sr["relevance"],
                "county_fips": sr["county_fips"],
                "county_name": sr["county_name"],
            }
            for sr in state_rows
        ]

        return jsonify({
            "id": row["id"],
            "title": row["title"],
            "source": row["source"],
            "source_type": row["source_type"],
            "url": row["url"],
            "published_date": row["published_date"],
            "ingested_date": row["ingested_date"],
            "analyzed": bool(row["analyzed"]),
            "analyzed_date": row["analyzed_date"],
            "sentiment_score": row["sentiment_score"],
            "sentiment_label": row["sentiment_label"],
            "state": row["state"],
            "location_relevance": row["location_relevance"],
            "topic_tags": topic_tags,
            "entities_mentioned": entities,
            "key_claims": row["key_claims"],
            "sentiment_justification": row["sentiment_justification"],
            "summary": row["summary"],
            "full_text": row["full_text"],
            "locations": locations,
            "state_details": state_details,
        })
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/control/* — Control Panel Endpoints
# ──────────────────────────────────────────────

@bp.route("/control/add-article", methods=["POST"])
@local_only
def control_add_article():
    """Manually add an article by URL. Scrapes metadata automatically."""
    from scraper import scrape_article_metadata
    from ingest import check_keyword_match
    from geo import infer_state_from_text

    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "URL is required"}), 400

    # Scrape metadata from the URL
    meta = scrape_article_metadata(url)
    title = meta.get("title") or url
    full_text = meta.get("full_text") or ""
    summary = full_text[:500] if full_text else ""

    # Detect state from content
    state = infer_state_from_text(title, summary)
    if state == "nationwide":
        state = None

    # Run keyword matching for score
    config = load_config()
    matched_kw, kw_score, kw_state = check_keyword_match(title, summary, config)
    if kw_state and not state:
        state = kw_state

    article_data = {
        "source": meta.get("source", "Manual Entry"),
        "source_type": "news",
        "title": title,
        "url": meta.get("url", url),
        "published_date": meta.get("published_date") or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d"),
        "full_text": full_text,
        "summary": summary,
        "matched_keywords": matched_kw or ["manual_entry"],
        "keyword_score": kw_score if matched_kw else 1.0,
        "state": state,
    }

    conn = get_conn()
    try:
        row_id = db.insert_article(conn, article_data)
        if row_id:
            conn.commit()
            return jsonify({
                "success": True,
                "article_id": row_id,
                "title": title,
                "source": article_data["source"],
                "state": state,
            })
        return jsonify({"success": False, "error": "Duplicate article (URL already exists)"}), 409
    finally:
        conn.close()


@bp.route("/control/unanalyzed")
def control_unanalyzed():
    """Get articles approved but not yet analyzed."""
    conn = get_conn()
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 1000))
        offset = max(0, int(request.args.get("offset", 0)))
        total = conn.execute(
            "SELECT COUNT(*) as c FROM articles WHERE analyzed = 0"
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT id, title, source, published_date, state, url "
            "FROM articles WHERE analyzed = 0 "
            "ORDER BY ingested_date DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        articles = [
            {
                "id": r["id"],
                "title": r["title"],
                "source": r["source"],
                "published_date": r["published_date"],
                "state": r["state"],
                "url": r["url"],
            }
            for r in rows
        ]
        return jsonify({
            "articles": articles,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(articles) < total,
        })
    finally:
        conn.close()


@bp.route("/control/run-ingest", methods=["POST"])
@local_only
def control_run_ingest():
    """Trigger RSS feed ingestion in background."""
    import ingest
    task_id = _run_in_background("ingest", ingest.ingest_feeds)
    return jsonify({"task_id": task_id})


@bp.route("/control/run-analysis", methods=["POST"])
@local_only
def control_run_analysis():
    """Trigger sentiment analysis in background with progress tracking."""
    import analyze

    data = request.get_json(silent=True) or {}
    limit = data.get("limit") or None  # None = all

    task_id = str(uuid.uuid4())[:8]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "type": "analysis",
            "status": "running",
            "started": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "finished": None,
            "result": None,
            "error": None,
            "progress": {"phase": "starting"},
        }

    def _set_progress(progress):
        with _tasks_lock:
            _tasks[task_id]["progress"] = progress

    def _progress(progress):
        _set_progress(progress)

    def _worker():
        try:
            result = analyze.analyze_articles(limit=limit, progress_callback=_progress)
            with _tasks_lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["result"] = result
        except Exception as e:
            logger.error("Task %s (analysis) failed: %s", task_id, e)
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"task_id": task_id})


@bp.route("/control/run-digest", methods=["POST"])
@local_only
def control_run_digest():
    """Trigger digest generation in background."""
    import digest
    task_id = _run_in_background("digest", digest.generate_digest)
    return jsonify({"task_id": task_id})


@bp.route("/control/run-websearch", methods=["POST"])
@local_only
def control_run_websearch():
    """Trigger web search with optional query, date range, and state."""
    import websearch
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip() or None
    state = (data.get("state") or "").strip() or None
    days_back = data.get("days_back", 30)
    try:
        days_back = int(days_back)
    except (ValueError, TypeError):
        days_back = 30
    task_id = _create_tracked_task("websearch")

    def _progress(progress):
        _update_task_progress(task_id, progress)

    def _worker():
        try:
            result = websearch.run_websearch(
                query=query,
                days_back=days_back,
                state=state,
                progress_callback=_progress,
            )
            with _tasks_lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["result"] = result
        except Exception as e:
            logger.error("Task %s (websearch) failed: %s", task_id, e)
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["error"] = str(e)

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"task_id": task_id})


@bp.route("/control/run-sweep", methods=["POST"])
@local_only
def control_run_sweep():
    """Trigger per-state sweep: template queries × all 50 states."""
    import websearch
    data = request.get_json(silent=True) or {}
    state = (data.get("state") or "").strip() or None
    days_back = data.get("days_back", 7)
    start_date = (data.get("start_date") or "").strip() or None
    end_date = (data.get("end_date") or "").strip() or None
    skip_analysis = bool(data.get("skip_analysis", False))
    try:
        days_back = int(days_back)
    except (ValueError, TypeError):
        days_back = 7
    task_id = _create_tracked_task("sweep")

    def _progress(progress):
        _update_task_progress(task_id, progress)

    def _worker():
        try:
            result = websearch.run_websearch(
                per_state=True,
                days_back=days_back,
                start_date=start_date,
                end_date=end_date,
                state=state,
                skip_analysis=skip_analysis,
                progress_callback=_progress,
            )
            with _tasks_lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["result"] = result
        except Exception as e:
            logger.error("Task %s (sweep) failed: %s", task_id, e)
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["finished"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                _tasks[task_id]["error"] = str(e)

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"task_id": task_id})


@bp.route("/control/pending-batches")
def control_pending_batches():
    """Return all pending batches grouped by search_id with metadata."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT search_id, COUNT(*) as count, "
            "MIN(created_date) as created_date, "
            "AVG(relevance_score) as avg_relevance "
            "FROM pending_articles WHERE status = 'pending' "
            "GROUP BY search_id ORDER BY created_date DESC"
        ).fetchall()
        batches = [
            {
                "search_id": r["search_id"],
                "count": r["count"],
                "created_date": r["created_date"],
                "avg_relevance": round(r["avg_relevance"], 1) if r["avg_relevance"] is not None else None,
            }
            for r in rows
        ]
        return jsonify({"batches": batches})
    finally:
        conn.close()


@bp.route("/control/discard-batch", methods=["POST"])
@local_only
def control_discard_batch():
    """Reject all pending articles in a batch by search_id."""
    data = request.get_json(force=True)
    search_id = data.get("search_id", "").strip()
    if not search_id:
        return jsonify({"success": False, "error": "search_id required"}), 400
    conn = get_conn()
    try:
        result = conn.execute(
            "UPDATE pending_articles SET status = 'rejected' "
            "WHERE search_id = ? AND status = 'pending'",
            (search_id,),
        )
        conn.commit()
        return jsonify({"success": True, "discarded": result.rowcount})
    finally:
        conn.close()


@bp.route("/control/pending")
def control_pending():
    """Get pending articles for review (unified queue)."""
    search_id = request.args.get("search_id", "")
    state_filter = request.args.get("state", "")
    source_type_filter = request.args.get("source_type", "")
    limit = max(1, min(int(request.args.get("limit", 250)), 1000))
    offset = max(0, int(request.args.get("offset", 0)))
    conn = get_conn()
    try:
        where = "status = 'pending'"
        params = []
        if search_id:
            where += " AND search_id = ?"
            params.append(search_id)
        if state_filter:
            where += " AND state = ?"
            params.append(state_filter)
        if source_type_filter:
            where += " AND source_type = ?"
            params.append(source_type_filter)

        total = conn.execute(
            f"SELECT COUNT(*) as c FROM pending_articles WHERE {where}",
            params,
        ).fetchone()["c"]

        rows = conn.execute(
            f"SELECT id, search_id, source, title, url, published_date, summary, "
            f"relevance_score, relevance_reason, created_date, source_type, state "
            f"FROM pending_articles WHERE {where} "
            f"ORDER BY COALESCE(relevance_score, 0) DESC, created_date DESC "
            f"LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        articles = []
        for r in rows:
            articles.append({
                "id": r["id"],
                "search_id": r["search_id"],
                "source": r["source"],
                "title": r["title"],
                "url": r["url"],
                "published_date": r["published_date"],
                "summary": r["summary"],
                "relevance_score": r["relevance_score"],
                "relevance_reason": r["relevance_reason"],
                "created_date": r["created_date"],
                "source_type": r["source_type"] if "source_type" in r.keys() else "websearch",
                "state": r["state"] if "state" in r.keys() else None,
            })

        # Queue stats
        stats_rows = conn.execute(
            "SELECT source_type, COUNT(*) as c FROM pending_articles "
            "WHERE status = 'pending' GROUP BY source_type"
        ).fetchall()
        stats = {r["source_type"] or "websearch": r["c"] for r in stats_rows}

        return jsonify({
            "articles": articles,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(articles) < total,
            "stats": stats,
        })
    finally:
        conn.close()


@bp.route("/control/approve", methods=["POST"])
@local_only
def control_approve():
    """Approve selected pending articles — move to main articles table."""
    data = request.get_json(force=True)
    article_ids = data.get("article_ids", [])
    if not article_ids:
        return jsonify({"success": False, "error": "No articles selected"}), 400
    conn = get_conn()
    try:
        approved = db.approve_pending_articles(conn, article_ids)
        return jsonify({"success": True, "approved": approved})
    finally:
        conn.close()


@bp.route("/control/approve-above", methods=["POST"])
@local_only
def control_approve_above():
    """Approve all pending articles with relevance score >= threshold."""
    data = request.get_json(force=True)
    threshold = data.get("threshold", 7)
    try:
        threshold = float(threshold)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid threshold"}), 400
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM pending_articles "
            "WHERE status = 'pending' AND relevance_score >= ?",
            (threshold,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return jsonify({"success": True, "approved": 0})
        approved = db.approve_pending_articles(conn, ids)
        return jsonify({"success": True, "approved": approved})
    finally:
        conn.close()


@bp.route("/control/reject", methods=["POST"])
@local_only
def control_reject():
    """Reject selected pending articles."""
    data = request.get_json(force=True)
    article_ids = data.get("article_ids", [])
    if not article_ids:
        return jsonify({"success": False, "error": "No articles selected"}), 400
    conn = get_conn()
    try:
        db.reject_pending_articles(conn, article_ids)
        return jsonify({"success": True, "rejected": len(article_ids)})
    finally:
        conn.close()


@bp.route("/control/clear-pending", methods=["POST"])
@local_only
def control_clear_pending():
    """Clear resolved pending articles."""
    conn = get_conn()
    try:
        db.clear_pending_articles(conn)
        return jsonify({"success": True})
    finally:
        conn.close()


@bp.route("/control/task/<task_id>")
def control_task_status(task_id):
    """Poll status of a background task."""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


@bp.route("/control/tasks")
def control_task_list():
    """List recent background tasks."""
    with _tasks_lock:
        tasks = sorted(_tasks.values(), key=lambda t: t["started"], reverse=True)[:20]
    return jsonify({"tasks": tasks})
