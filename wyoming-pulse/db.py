"""
Wyoming Pulse — Database Layer
SQLite database for storing articles, sentiment analysis, and digest history.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("wyoming_pulse.db")

# Default database path
DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "wyoming_pulse.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'news',
    title TEXT NOT NULL,
    url TEXT UNIQUE,
    published_date TEXT,
    ingested_date TEXT NOT NULL,
    full_text TEXT,
    summary TEXT,
    matched_keywords TEXT,
    keyword_score REAL,
    analyzed INTEGER DEFAULT 0,
    analyzed_date TEXT,
    sentiment_score REAL,
    sentiment_label TEXT,
    voice_type TEXT,
    location_relevance TEXT,
    topic_tags TEXT,
    entities_mentioned TEXT,
    key_claims TEXT,
    analysis_raw TEXT,
    included_in_digest TEXT
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    generated_date TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    article_count INTEGER,
    avg_sentiment REAL,
    content TEXT
);

CREATE TABLE IF NOT EXISTS feed_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_name TEXT NOT NULL,
    run_date TEXT NOT NULL,
    articles_found INTEGER DEFAULT 0,
    articles_matched INTEGER DEFAULT 0,
    status TEXT DEFAULT 'success'
);

CREATE INDEX IF NOT EXISTS idx_articles_analyzed ON articles(analyzed);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_date);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
CREATE INDEX IF NOT EXISTS idx_articles_sentiment ON articles(sentiment_label);
"""


def get_connection(db_path=None):
    """Get a SQLite connection, creating the database if needed."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    """Initialize the database schema."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        logger.info("Database initialized at %s", db_path or DB_PATH)
    finally:
        conn.close()


def insert_article(conn, article_data):
    """
    Insert a new article into the database.
    Returns the row id on success, None if the article already exists (duplicate URL).
    """
    sql = """
    INSERT OR IGNORE INTO articles
        (source, source_type, title, url, published_date, ingested_date,
         full_text, summary, matched_keywords, keyword_score)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    now = datetime.utcnow().isoformat()
    params = (
        article_data.get("source", "Unknown"),
        article_data.get("source_type", "news"),
        article_data["title"],
        article_data.get("url"),
        article_data.get("published_date"),
        now,
        article_data.get("full_text"),
        article_data.get("summary"),
        json.dumps(article_data.get("matched_keywords", [])),
        article_data.get("keyword_score", 0.0),
    )
    cursor = conn.execute(sql, params)
    if cursor.rowcount > 0:
        logger.debug("Inserted article: %s", article_data["title"])
        return cursor.lastrowid
    else:
        logger.debug("Duplicate skipped: %s", article_data.get("url"))
        return None


def get_unanalyzed_articles(conn, limit=20):
    """Get articles that haven't been analyzed yet."""
    sql = """
    SELECT * FROM articles
    WHERE analyzed = 0
    ORDER BY ingested_date ASC
    LIMIT ?
    """
    return conn.execute(sql, (limit,)).fetchall()


def update_article_analysis(conn, article_id, analysis):
    """Update an article with sentiment analysis results."""
    sql = """
    UPDATE articles SET
        analyzed = 1,
        analyzed_date = ?,
        sentiment_score = ?,
        sentiment_label = ?,
        voice_type = ?,
        location_relevance = ?,
        topic_tags = ?,
        entities_mentioned = ?,
        key_claims = ?,
        analysis_raw = ?
    WHERE id = ?
    """
    now = datetime.utcnow().isoformat()
    params = (
        now,
        analysis.get("sentiment_score"),
        analysis.get("sentiment_label"),
        analysis.get("voice_type"),
        analysis.get("location_relevance"),
        json.dumps(analysis.get("topic_tags", [])),
        json.dumps(analysis.get("entities_mentioned", [])),
        analysis.get("key_claims"),
        json.dumps(analysis),
        article_id,
    )
    conn.execute(sql, params)
    conn.commit()


def get_articles_for_digest(conn, start_date, end_date):
    """Get analyzed articles within a date range for digest generation."""
    sql = """
    SELECT * FROM articles
    WHERE analyzed = 1
      AND published_date >= ?
      AND published_date <= ?
    ORDER BY published_date ASC
    """
    return conn.execute(sql, (start_date, end_date)).fetchall()


def mark_articles_in_digest(conn, article_ids, digest_filename):
    """Mark articles as included in a specific digest."""
    sql = "UPDATE articles SET included_in_digest = ? WHERE id = ?"
    for aid in article_ids:
        conn.execute(sql, (digest_filename, aid))
    conn.commit()


def insert_digest(conn, digest_data):
    """Insert a digest record."""
    sql = """
    INSERT INTO digests
        (filename, generated_date, period_start, period_end, article_count, avg_sentiment, content)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    now = datetime.utcnow().isoformat()
    params = (
        digest_data["filename"],
        now,
        digest_data["period_start"],
        digest_data["period_end"],
        digest_data.get("article_count", 0),
        digest_data.get("avg_sentiment"),
        digest_data.get("content"),
    )
    conn.execute(sql, params)
    conn.commit()


def insert_feed_run(conn, feed_name, articles_found, articles_matched, status="success"):
    """Record a feed ingestion run."""
    sql = """
    INSERT INTO feed_runs (feed_name, run_date, articles_found, articles_matched, status)
    VALUES (?, ?, ?, ?, ?)
    """
    now = datetime.utcnow().isoformat()
    conn.execute(sql, (feed_name, now, articles_found, articles_matched, status))
    conn.commit()


def get_status(conn):
    """Get database statistics for the status command."""
    stats = {}

    # Total and analyzed counts
    row = conn.execute("SELECT COUNT(*) as total FROM articles").fetchone()
    stats["total_articles"] = row["total"]

    row = conn.execute("SELECT COUNT(*) as c FROM articles WHERE analyzed = 1").fetchone()
    stats["analyzed"] = row["c"]
    stats["pending"] = stats["total_articles"] - stats["analyzed"]

    # By source
    rows = conn.execute(
        "SELECT source, COUNT(*) as c FROM articles GROUP BY source ORDER BY c DESC"
    ).fetchall()
    stats["by_source"] = {r["source"]: r["c"] for r in rows}

    # Sentiment distribution
    rows = conn.execute(
        """SELECT sentiment_label, COUNT(*) as c FROM articles
           WHERE analyzed = 1 AND sentiment_label IS NOT NULL
           GROUP BY sentiment_label"""
    ).fetchall()
    label_order = [
        "strongly_positive", "slightly_positive", "neutral",
        "slightly_negative", "strongly_negative",
    ]
    dist = {r["sentiment_label"]: r["c"] for r in rows}
    stats["sentiment_distribution"] = {lbl: dist.get(lbl, 0) for lbl in label_order}

    # Last feed check
    row = conn.execute(
        "SELECT run_date FROM feed_runs ORDER BY run_date DESC LIMIT 1"
    ).fetchone()
    stats["last_feed_check"] = row["run_date"] if row else "Never"

    # Last analysis
    row = conn.execute(
        "SELECT analyzed_date FROM articles WHERE analyzed = 1 ORDER BY analyzed_date DESC LIMIT 1"
    ).fetchone()
    stats["last_analysis"] = row["analyzed_date"] if row else "Never"

    # Last digest
    row = conn.execute(
        "SELECT filename, article_count FROM digests ORDER BY generated_date DESC LIMIT 1"
    ).fetchone()
    if row:
        stats["last_digest"] = f"{row['filename']} ({row['article_count']} articles)"
    else:
        stats["last_digest"] = "None"

    # Next digest (last digest date + 14 days, or now if none)
    row = conn.execute(
        "SELECT period_end FROM digests ORDER BY generated_date DESC LIMIT 1"
    ).fetchone()
    if row:
        from datetime import timedelta
        last_end = datetime.fromisoformat(row["period_end"])
        next_due = last_end + timedelta(days=14)
        stats["next_digest_due"] = next_due.strftime("%Y-%m-%d")
    else:
        stats["next_digest_due"] = "Run 'digest' to generate first report"

    # Rough API token estimate (analyzed articles * ~500 tokens avg)
    estimated_tokens = stats["analyzed"] * 500
    estimated_cost = estimated_tokens * 0.000001  # Rough Haiku pricing
    stats["api_usage_estimate"] = f"~{estimated_tokens:,} tokens (~${estimated_cost:.2f})"

    return stats
