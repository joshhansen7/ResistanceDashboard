"""
Wyoming Pulse — Export Report Generator
Generates a self-contained HTML report for sharing with leadership.
"""

import json
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import db


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
            "trend": _get_trend(conn),
            "voice": _get_voice(conn),
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


def _get_trend(conn):
    rows = conn.execute(
        "SELECT DATE(published_date) as date, AVG(sentiment_score) as avg, COUNT(*) as count "
        "FROM articles WHERE analyzed = 1 AND published_date IS NOT NULL "
        "GROUP BY DATE(published_date) ORDER BY date ASC"
    ).fetchall()
    return [{"date": r["date"], "avg": round(r["avg"], 2), "count": r["count"]} for r in rows]


def _get_voice(conn):
    result = {}
    for voice in ("elite", "public"):
        row = conn.execute(
            "SELECT AVG(sentiment_score) as avg, COUNT(*) as count "
            "FROM articles WHERE analyzed = 1 AND voice_type = ?", (voice,)
        ).fetchone()
        result[voice] = {"avg": round(row["avg"], 2) if row["avg"] else None, "count": row["count"]}
    return result


def _get_locations(conn):
    result = {}
    for loc in ("evanston", "casper", "cheyenne", "statewide"):
        row = conn.execute(
            "SELECT AVG(sentiment_score) as avg, COUNT(*) as count "
            "FROM articles WHERE analyzed = 1 AND location_relevance = ?", (loc,)
        ).fetchone()
        result[loc] = {"avg": round(row["avg"], 2) if row["avg"] else None, "count": row["count"]}
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
        "voice_type, location_relevance, key_claims "
        "FROM articles WHERE analyzed = 1 ORDER BY published_date DESC LIMIT 50"
    ).fetchall()
    return [
        {
            "title": r["title"], "source": r["source"],
            "date": r["published_date"],
            "score": r["sentiment_score"], "label": r["sentiment_label"],
            "voice": r["voice_type"], "location": r["location_relevance"],
            "claims": r["key_claims"],
        }
        for r in rows
    ]
