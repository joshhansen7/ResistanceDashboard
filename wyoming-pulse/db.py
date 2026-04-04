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
    state TEXT,
    location_relevance TEXT,
    topic_tags TEXT,
    entities_mentioned TEXT,
    key_claims TEXT,
    sentiment_justification TEXT,
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

CREATE TABLE IF NOT EXISTS pending_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id TEXT NOT NULL,
    source TEXT,
    title TEXT NOT NULL,
    url TEXT,
    published_date TEXT,
    summary TEXT,
    matched_keywords TEXT,
    keyword_score REAL,
    location_relevance TEXT,
    relevance_score REAL,
    relevance_reason TEXT,
    created_date TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS article_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    place TEXT,
    relevance TEXT DEFAULT 'primary',
    county_fips TEXT,
    county_name TEXT,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_articles_analyzed ON articles(analyzed);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_date);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
CREATE INDEX IF NOT EXISTS idx_articles_sentiment ON articles(sentiment_label);
CREATE INDEX IF NOT EXISTS idx_pending_search ON pending_articles(search_id);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_articles(status);
CREATE INDEX IF NOT EXISTS idx_article_states_state ON article_states(state);
CREATE INDEX IF NOT EXISTS idx_article_states_article ON article_states(article_id);
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
        # Migrate: add sentiment_justification column if missing
        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
        if "sentiment_justification" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN sentiment_justification TEXT")
            logger.info("Migrated: added sentiment_justification column")
        if "state" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN state TEXT")
            # Leave state NULL for pre-existing articles rather than assuming a state;
            # they will be categorized when re-analyzed or can be manually assigned.
            logger.info("Migrated: added state column (existing records have state=NULL)")
        # Migrate: add source_type and state columns to pending_articles
        pending_cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_articles)").fetchall()]
        if "source_type" not in pending_cols:
            conn.execute("ALTER TABLE pending_articles ADD COLUMN source_type TEXT DEFAULT 'websearch'")
            logger.info("Migrated: added source_type column to pending_articles")
        if "state" not in pending_cols:
            conn.execute("ALTER TABLE pending_articles ADD COLUMN state TEXT")
            logger.info("Migrated: added state column to pending_articles")
        # Migrate: add locations_json column to articles
        if "locations_json" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN locations_json TEXT")
            logger.info("Migrated: added locations_json column to articles")
            # Backfill from existing state/location_relevance
            rows = conn.execute(
                "SELECT id, state, location_relevance FROM articles WHERE locations_json IS NULL"
            ).fetchall()
            for r in rows:
                state = r["state"] or "other"
                loc = r["location_relevance"]
                entry = {"state": state, "relevance": "primary"}
                if loc and loc not in ("statewide", "nationwide"):
                    entry["place"] = loc
                conn.execute(
                    "UPDATE articles SET locations_json = ? WHERE id = ?",
                    (json.dumps([entry]), r["id"]),
                )
            logger.info("Backfilled locations_json for %d articles", len(rows))
        # Migrate: backfill article_states junction table if empty
        has_states = conn.execute(
            "SELECT COUNT(*) as c FROM article_states"
        ).fetchone()["c"]
        if has_states == 0:
            analyzed_with_locs = conn.execute(
                "SELECT id, locations_json FROM articles "
                "WHERE analyzed = 1 AND locations_json IS NOT NULL"
            ).fetchall()
            for r in analyzed_with_locs:
                try:
                    locs = json.loads(r["locations_json"])
                    _sync_article_states(conn, r["id"], locs)
                except (json.JSONDecodeError, TypeError):
                    continue
            if analyzed_with_locs:
                logger.info("Backfilled article_states for %d articles", len(analyzed_with_locs))

        # Migrate: add content_quality column to track scrape completeness
        if "content_quality" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN content_quality TEXT")
            logger.info("Migrated: added content_quality column")
            # Backfill based on current full_text length
            conn.execute("""
                UPDATE articles SET content_quality = CASE
                    WHEN full_text IS NULL OR LENGTH(full_text) < 200 THEN 'thin'
                    ELSE 'full'
                END
                WHERE content_quality IS NULL
            """)
            logger.info("Backfilled content_quality for existing articles")

        conn.commit()
        logger.info("Database initialized at %s", db_path or DB_PATH)
    finally:
        conn.close()


def insert_article(conn, article_data):
    """
    Insert a new article into the database.
    Returns the row id on success, None if the article already exists (duplicate URL).
    """
    full_text = article_data.get("full_text")
    content_quality = "full" if full_text and len(full_text) >= 200 else "thin"

    sql = """
    INSERT OR IGNORE INTO articles
        (source, source_type, title, url, published_date, ingested_date,
         full_text, summary, matched_keywords, keyword_score,
         state, location_relevance, content_quality)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    now = datetime.utcnow().isoformat()
    params = (
        article_data.get("source", "Unknown"),
        article_data.get("source_type", "news"),
        article_data["title"],
        article_data.get("url"),
        article_data.get("published_date"),
        now,
        full_text,
        article_data.get("summary"),
        json.dumps(article_data.get("matched_keywords", [])),
        article_data.get("keyword_score", 0.0),
        article_data.get("state"),
        article_data.get("location_relevance"),
        content_quality,
    )
    cursor = conn.execute(sql, params)
    if cursor.rowcount > 0:
        logger.debug("Inserted article: %s", article_data["title"])
        return cursor.lastrowid
    else:
        logger.debug("Duplicate skipped: %s", article_data.get("url"))
        return None


def get_unanalyzed_articles(conn, limit=20):
    """Get articles that haven't been analyzed yet.
    If limit is None, return all unanalyzed articles.
    """
    if limit is None:
        sql = """
        SELECT * FROM articles
        WHERE analyzed = 0
        ORDER BY ingested_date ASC
        """
        return conn.execute(sql).fetchall()
    sql = """
    SELECT * FROM articles
    WHERE analyzed = 0
    ORDER BY ingested_date ASC
    LIMIT ?
    """
    return conn.execute(sql, (limit,)).fetchall()


def update_article_analysis(conn, article_id, analysis):
    """Update an article with sentiment analysis results."""
    # Serialize locations_json
    locations_json = analysis.get("locations_json")
    if locations_json and isinstance(locations_json, list):
        locations_str = json.dumps(locations_json)
    else:
        locations_str = None

    sql = """
    UPDATE articles SET
        analyzed = 1,
        analyzed_date = ?,
        sentiment_score = ?,
        sentiment_label = ?,
        voice_type = ?,
        state = ?,
        location_relevance = ?,
        topic_tags = ?,
        entities_mentioned = ?,
        key_claims = ?,
        sentiment_justification = ?,
        analysis_raw = ?,
        locations_json = ?
    WHERE id = ?
    """
    now = datetime.utcnow().isoformat()
    params = (
        now,
        analysis.get("sentiment_score"),
        analysis.get("sentiment_label"),
        analysis.get("voice_type"),
        analysis.get("state"),
        analysis.get("location_relevance"),
        json.dumps(analysis.get("topic_tags", [])),
        json.dumps(analysis.get("entities_mentioned", [])),
        analysis.get("key_claims"),
        analysis.get("sentiment_justification"),
        json.dumps(analysis),
        locations_str,
        article_id,
    )
    conn.execute(sql, params)

    # Populate article_states junction table for multi-state queries
    _sync_article_states(conn, article_id, locations_json)

    conn.commit()


def _sync_article_states(conn, article_id, locations):
    """Sync the article_states junction table from a locations list."""
    conn.execute("DELETE FROM article_states WHERE article_id = ?", (article_id,))
    if not locations or not isinstance(locations, list):
        return
    for loc in locations:
        state = loc.get("state")
        if not state:
            continue
        conn.execute(
            """INSERT INTO article_states (article_id, state, place, relevance, county_fips, county_name)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (article_id, state, loc.get("place"), loc.get("relevance", "primary"),
             loc.get("county_fips"), loc.get("county_name")),
        )


def backfill_article_states(conn):
    """Backfill article_states from existing locations_json for all analyzed articles."""
    rows = conn.execute(
        "SELECT id, locations_json FROM articles WHERE analyzed = 1 AND locations_json IS NOT NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        try:
            locs = json.loads(row["locations_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        _sync_article_states(conn, row["id"], locs)
        updated += 1
    conn.commit()
    logger.info("Backfilled article_states for %d articles", updated)
    return updated


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


# ──────────────────────────────────────────────
# Pending Articles (Web Search Review Queue)
# ──────────────────────────────────────────────

def insert_pending_article(conn, article_data):
    """Insert an article into the pending review queue."""
    sql = """
    INSERT INTO pending_articles
        (search_id, source, title, url, published_date, summary,
         matched_keywords, keyword_score, location_relevance,
         relevance_score, relevance_reason, created_date, status,
         source_type, state)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """
    now = datetime.utcnow().isoformat()
    params = (
        article_data["search_id"],
        article_data.get("source", "Web Search"),
        article_data["title"],
        article_data.get("url"),
        article_data.get("published_date"),
        article_data.get("summary", ""),
        json.dumps(article_data.get("matched_keywords", [])),
        article_data.get("keyword_score", 0.0),
        article_data.get("location_relevance", "statewide"),
        article_data.get("relevance_score"),
        article_data.get("relevance_reason"),
        now,
        article_data.get("source_type", "websearch"),
        article_data.get("state"),
    )
    cursor = conn.execute(sql, params)
    conn.commit()
    return cursor.lastrowid


def get_pending_articles(conn, search_id=None):
    """Fetch pending articles, optionally filtered by search_id."""
    if search_id:
        rows = conn.execute(
            "SELECT * FROM pending_articles WHERE search_id = ? AND status = 'pending' "
            "ORDER BY relevance_score DESC",
            (search_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pending_articles WHERE status = 'pending' "
            "ORDER BY relevance_score DESC"
        ).fetchall()
    return rows


def approve_pending_articles(conn, article_ids):
    """Move approved pending articles into the main articles table."""
    approved = 0
    for aid in article_ids:
        row = conn.execute(
            "SELECT * FROM pending_articles WHERE id = ? AND status = 'pending'",
            (aid,),
        ).fetchone()
        if not row:
            continue

        article_data = {
            "source": row["source"],
            "source_type": row["source_type"] if "source_type" in row.keys() else "websearch",
            "title": row["title"],
            "url": row["url"],
            "published_date": row["published_date"],
            "full_text": row["summary"],
            "summary": row["summary"],
            "matched_keywords": json.loads(row["matched_keywords"]) if row["matched_keywords"] else [],
            "keyword_score": row["keyword_score"],
            "state": row["state"] if "state" in row.keys() else None,
            "location_relevance": row["location_relevance"] if "location_relevance" in row.keys() else None,
        }
        result = insert_article(conn, article_data)
        if result:
            approved += 1

        conn.execute(
            "UPDATE pending_articles SET status = 'approved' WHERE id = ?",
            (aid,),
        )
    conn.commit()
    return approved


def reject_pending_articles(conn, article_ids):
    """Mark pending articles as rejected."""
    for aid in article_ids:
        conn.execute(
            "UPDATE pending_articles SET status = 'rejected' WHERE id = ?",
            (aid,),
        )
    conn.commit()


def clear_pending_articles(conn, search_id=None):
    """Remove pending articles (optionally by search_id)."""
    if search_id:
        conn.execute("DELETE FROM pending_articles WHERE search_id = ?", (search_id,))
    else:
        conn.execute("DELETE FROM pending_articles WHERE status != 'pending'")
    conn.commit()
