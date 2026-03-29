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
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request, send_file

import db

logger = logging.getLogger("wyoming_pulse.api")

bp = Blueprint("api", __name__, url_prefix="/api")

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
            "started": datetime.utcnow().isoformat(),
            "finished": None,
            "result": None,
            "error": None,
        }

    def _worker():
        try:
            result = target_fn(**kwargs)
            with _tasks_lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["finished"] = datetime.utcnow().isoformat()
                _tasks[task_id]["result"] = result
        except Exception as e:
            logger.error("Task %s (%s) failed: %s", task_id, task_type, e)
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["finished"] = datetime.utcnow().isoformat()
                _tasks[task_id]["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return task_id


def get_conn():
    """Get a database connection using the configured path."""
    return db.get_connection(current_app.config["DB_PATH"])


# ──────────────────────────────────────────────
# /api/overview
# ──────────────────────────────────────────────
@bp.route("/overview")
def overview():
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) as c FROM articles").fetchone()["c"]
        analyzed = conn.execute(
            "SELECT COUNT(*) as c FROM articles WHERE analyzed = 1"
        ).fetchone()["c"]
        pending = total - analyzed

        avg_row = conn.execute(
            "SELECT AVG(sentiment_score) as avg FROM articles WHERE analyzed = 1"
        ).fetchone()
        avg_sentiment = round(avg_row["avg"], 2) if avg_row["avg"] else None

        # Period comparison: last 14 days vs previous 14 days
        now = datetime.utcnow()
        current_start = (now - timedelta(days=14)).isoformat()
        prev_start = (now - timedelta(days=28)).isoformat()
        prev_end = current_start

        cur_avg_row = conn.execute(
            "SELECT AVG(sentiment_score) as avg FROM articles "
            "WHERE analyzed = 1 AND published_date >= ?",
            (current_start,),
        ).fetchone()
        prev_avg_row = conn.execute(
            "SELECT AVG(sentiment_score) as avg FROM articles "
            "WHERE analyzed = 1 AND published_date >= ? AND published_date < ?",
            (prev_start, prev_end),
        ).fetchone()

        cur_avg = round(cur_avg_row["avg"], 2) if cur_avg_row["avg"] else None
        prev_avg = round(prev_avg_row["avg"], 2) if prev_avg_row["avg"] else None
        sentiment_change = None
        if cur_avg is not None and prev_avg is not None:
            sentiment_change = round(cur_avg - prev_avg, 2)

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
            "last_ingestion": last_ingest["run_date"] if last_ingest else None,
            "last_analysis": last_analysis["analyzed_date"] if last_analysis else None,
        })
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/sentiment-trend
# ──────────────────────────────────────────────
@bp.route("/sentiment-trend")
def sentiment_trend():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DATE(published_date) as date, "
            "AVG(sentiment_score) as avg, COUNT(*) as count "
            "FROM articles WHERE analyzed = 1 AND published_date IS NOT NULL "
            "GROUP BY DATE(published_date) ORDER BY date ASC"
        ).fetchall()

        data = [
            {"date": r["date"], "avg_sentiment": round(r["avg"], 2), "count": r["count"]}
            for r in rows
        ]
        return jsonify({"data": data})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/voice-comparison
# ──────────────────────────────────────────────
@bp.route("/voice-comparison")
def voice_comparison():
    conn = get_conn()
    try:
        result = {}
        for voice in ("elite", "public"):
            row = conn.execute(
                "SELECT AVG(sentiment_score) as avg, COUNT(*) as count "
                "FROM articles WHERE analyzed = 1 AND voice_type = ?",
                (voice,),
            ).fetchone()
            result[voice] = {
                "avg": round(row["avg"], 2) if row["avg"] else None,
                "count": row["count"],
            }
        return jsonify(result)
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/state-sentiment
# ──────────────────────────────────────────────
@bp.route("/state-sentiment")
def state_sentiment():
    """Per-state average sentiment for the US map."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT state, AVG(sentiment_score) as avg, COUNT(*) as count "
            "FROM articles WHERE analyzed = 1 AND state IS NOT NULL "
            "GROUP BY state"
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
    conn = get_conn()
    try:
        state = request.args.get("state", "wyoming")
        state_locations = {
            "wyoming": ["evanston", "casper", "cheyenne", "statewide"],
            "texas": ["dallas", "statewide"],
        }
        locs = state_locations.get(state, ["statewide"])
        result = {}
        for loc in locs:
            row = conn.execute(
                "SELECT AVG(sentiment_score) as avg, COUNT(*) as count "
                "FROM articles WHERE analyzed = 1 AND state = ? AND location_relevance = ?",
                (state, loc),
            ).fetchone()
            result[loc] = {
                "avg": round(row["avg"], 2) if row["avg"] else None,
                "count": row["count"],
            }
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
        rows = conn.execute(
            "SELECT entities_mentioned FROM articles WHERE analyzed = 1"
        ).fetchall()

        entity_counts = defaultdict(int)
        for row in rows:
            ents = json.loads(row["entities_mentioned"]) if row["entities_mentioned"] else []
            for e in ents:
                entity_counts[e] += 1

        entities_list = [
            {"name": name, "count": count}
            for name, count in sorted(entity_counts.items(), key=lambda x: x[1], reverse=True)
        ]
        return jsonify({"entities": entities_list})
    finally:
        conn.close()


# ──────────────────────────────────────────────
# /api/articles
# ──────────────────────────────────────────────
@bp.route("/articles")
def articles():
    conn = get_conn()
    try:
        limit = request.args.get("limit", 25, type=int)
        offset = request.args.get("offset", 0, type=int)
        location = request.args.get("location", "")
        state = request.args.get("state", "")
        sentiment = request.args.get("sentiment_label", "")

        where_clauses = ["analyzed = 1"]
        params = []

        if state:
            where_clauses.append("state = ?")
            params.append(state)
        if location:
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

        articles_list = []
        for r in rows:
            articles_list.append({
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
                "topic_tags": json.loads(r["topic_tags"]) if r["topic_tags"] else [],
                "entities_mentioned": json.loads(r["entities_mentioned"]) if r["entities_mentioned"] else [],
                "summary": r["summary"],
            })

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
            if key in ("topic_tags", "entities_mentioned"):
                val = json.dumps(val) if isinstance(val, list) else val
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
# /api/control/* — Control Panel Endpoints
# ──────────────────────────────────────────────

@bp.route("/control/add-article", methods=["POST"])
def control_add_article():
    """Manually add a single article."""
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"success": False, "error": "Title is required"}), 400

    article_data = {
        "source": data.get("source", "Manual Entry").strip(),
        "source_type": data.get("source_type", "news"),
        "title": title,
        "url": data.get("url", "").strip() or None,
        "published_date": data.get("published_date", datetime.utcnow().strftime("%Y-%m-%d")),
        "full_text": data.get("full_text", ""),
        "summary": (data.get("full_text") or "")[:500],
        "matched_keywords": ["manual_entry"],
        "keyword_score": 1.0,
    }

    conn = get_conn()
    try:
        row_id = db.insert_article(conn, article_data)
        if row_id:
            return jsonify({"success": True, "article_id": row_id})
        return jsonify({"success": False, "error": "Duplicate article (URL already exists)"}), 409
    finally:
        conn.close()


@bp.route("/control/run-ingest", methods=["POST"])
def control_run_ingest():
    """Trigger RSS feed ingestion in background."""
    import ingest
    task_id = _run_in_background("ingest", ingest.ingest_feeds)
    return jsonify({"task_id": task_id})


@bp.route("/control/run-analysis", methods=["POST"])
def control_run_analysis():
    """Trigger sentiment analysis in background."""
    import analyze
    task_id = _run_in_background("analysis", analyze.analyze_articles)
    return jsonify({"task_id": task_id})


@bp.route("/control/run-digest", methods=["POST"])
def control_run_digest():
    """Trigger digest generation in background."""
    import digest
    task_id = _run_in_background("digest", digest.generate_digest)
    return jsonify({"task_id": task_id})


@bp.route("/control/run-websearch", methods=["POST"])
def control_run_websearch():
    """Trigger web search with optional query and date range."""
    import websearch
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip() or None
    days_back = data.get("days_back", 30)
    try:
        days_back = int(days_back)
    except (ValueError, TypeError):
        days_back = 30
    task_id = _run_in_background("websearch", websearch.run_websearch,
                                  query=query, days_back=days_back)
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
    """Get pending articles for review."""
    search_id = request.args.get("search_id", "")
    conn = get_conn()
    try:
        rows = db.get_pending_articles(conn, search_id or None)
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
                "matched_keywords": json.loads(r["matched_keywords"]) if r["matched_keywords"] else [],
                "keyword_score": r["keyword_score"],
                "location_relevance": r["location_relevance"],
                "relevance_score": r["relevance_score"],
                "relevance_reason": r["relevance_reason"],
                "created_date": r["created_date"],
            })
        return jsonify({"articles": articles, "total": len(articles)})
    finally:
        conn.close()


@bp.route("/control/approve", methods=["POST"])
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


@bp.route("/control/reject", methods=["POST"])
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
