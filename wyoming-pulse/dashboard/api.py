"""
Wyoming Pulse — Dashboard API Endpoints
All /api/* routes returning JSON for the frontend.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request, send_file

import db

bp = Blueprint("api", __name__, url_prefix="/api")


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
# /api/locations
# ──────────────────────────────────────────────
@bp.route("/locations")
def locations():
    conn = get_conn()
    try:
        result = {}
        for loc in ("evanston", "casper", "cheyenne", "statewide"):
            row = conn.execute(
                "SELECT AVG(sentiment_score) as avg, COUNT(*) as count "
                "FROM articles WHERE analyzed = 1 AND location_relevance = ?",
                (loc,),
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
        sentiment = request.args.get("sentiment_label", "")

        where_clauses = ["analyzed = 1"]
        params = []

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
            f"sentiment_score, sentiment_label, voice_type, location_relevance, "
            f"url, key_claims "
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
                "location_relevance": r["location_relevance"],
                "url": r["url"],
                "key_claims": r["key_claims"],
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
