"""
Prometheus — Web Search
Searches Google News RSS for articles across tracked states, scores relevance
with Claude API, and stores results in the unified pending review queue.
"""

import logging
import re
import time
import uuid
from datetime import datetime
from urllib.parse import quote_plus

import feedparser
import requests

import db
from ingest import check_keyword_match, detect_location, _score_relevance
from shared import load_config, get_anthropic_client
from utils import clean_html, normalize_for_comparison

logger = logging.getLogger("wyoming_pulse.websearch")

USER_AGENT = "PrometheusDashboard/1.0 (News Research; +https://github.com/prometheus-hyperscale)"

DEFAULT_QUERY = '"data center" Wyoming OR Texas OR Michigan'


def _extract_source_from_title(title):
    """Google News RSS titles often end with ' - Source Name'. Extract it."""
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        return parts[1].strip(), parts[0].strip()
    return "Google News", title


def _check_title_similarity(title, existing_titles):
    """
    Check if a title is similar to any existing title.
    Returns (is_duplicate, matched_title) using word overlap.
    """
    norm = normalize_for_comparison(title)
    norm_words = set(norm.split())
    if len(norm_words) < 3:
        return False, None

    for existing in existing_titles:
        exist_words = set(normalize_for_comparison(existing).split())
        if len(exist_words) < 3:
            continue
        intersection = norm_words & exist_words
        union = norm_words | exist_words
        similarity = len(intersection) / len(union) if union else 0
        if similarity >= 0.6:
            return True, existing

    return False, None


def run_websearch(query=None, days_back=30, state=None):
    """
    Search Google News RSS, score results with Claude, store in pending queue.
    If state is given, only runs queries for that state.
    If query is given, uses that instead of per-state queries.
    Returns dict with summary statistics.
    """
    config = load_config()
    pipeline = config.get("pipeline", {})
    auto_threshold = pipeline.get("auto_approve_threshold")
    relevance_model = pipeline.get("relevance_model",
                                    config.get("anthropic", {}).get("classification_model",
                                                                     "claude-haiku-4-5-20251001"))

    search_id = str(uuid.uuid4())[:8]

    # Build list of queries to run
    queries_to_run = []
    if query:
        # Explicit query — run it without state association
        queries_to_run.append({"query": query.strip(), "state": state})
    else:
        # Use per-state web_search_queries from config
        states = config.get("states", {})
        for state_key, state_cfg in states.items():
            if state and state_key != state:
                continue
            for q in state_cfg.get("web_search_queries", []):
                queries_to_run.append({"query": q, "state": state_key})

        # Fallback: if no per-state queries, use default
        if not queries_to_run:
            queries_to_run.append({"query": DEFAULT_QUERY, "state": None})

    logger.info("Running %d search queries", len(queries_to_run))

    db.init_db()
    conn = db.get_connection()

    # Load existing titles for similarity checking
    existing_rows = conn.execute("SELECT title FROM articles").fetchall()
    pending_rows = conn.execute(
        "SELECT title FROM pending_articles WHERE status = 'pending'"
    ).fetchall()
    existing_titles = [r["title"] for r in existing_rows] + [r["title"] for r in pending_rows]

    # Track URLs we've already seen in this search to avoid cross-query duplicates
    seen_urls = set()

    total_results = 0
    skipped_url = 0
    skipped_title = 0
    matched_articles = []

    for query_info in queries_to_run:
        search_query = query_info["query"]
        query_state = query_info["state"]

        encoded_q = quote_plus(f"{search_query} when:{days_back}d")
        rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=US&ceid=US:en"

        logger.info("Searching: %s [%s]", search_query, query_state or "all")

        try:
            resp = requests.get(rss_url, headers={"User-Agent": USER_AGENT}, timeout=15)
            feed = feedparser.parse(resp.content)
        except Exception as e:
            logger.error("Failed to fetch Google News RSS: %s", e)
            continue

        total_results += len(feed.entries)

        for entry in feed.entries:
            raw_title = getattr(entry, "title", "") or ""
            source, title = _extract_source_from_title(raw_title)
            link = getattr(entry, "link", "") or ""
            summary = clean_html(getattr(entry, "summary", "") or "")
            pub_date = None
            for attr in ("published_parsed", "updated_parsed"):
                parsed = getattr(entry, attr, None)
                if parsed:
                    try:
                        pub_date = datetime(*parsed[:6]).strftime("%Y-%m-%d")
                    except (ValueError, TypeError):
                        pass
                    break

            # Cross-query dedup
            if link in seen_urls:
                continue
            seen_urls.add(link)

            # URL dedup against articles + pending
            if link:
                existing = conn.execute(
                    "SELECT id FROM articles WHERE url = ?", (link,)
                ).fetchone()
                if existing:
                    skipped_url += 1
                    continue
                existing_pending = conn.execute(
                    "SELECT id FROM pending_articles WHERE url = ? AND status = 'pending'",
                    (link,),
                ).fetchone()
                if existing_pending:
                    skipped_url += 1
                    continue

            # Title similarity check
            is_dup, matched_title = _check_title_similarity(title, existing_titles)
            if is_dup:
                skipped_title += 1
                logger.debug("Title duplicate: '%s' ~ '%s'", title, matched_title)
                continue

            existing_titles.append(title)

            # Keyword filtering — try query's state first, then all states
            keywords, score, matched_state = check_keyword_match(
                title, summary, config, state_key=query_state
            )
            location = detect_location(title, summary, config, state_key=matched_state or query_state)

            matched_articles.append({
                "source": source,
                "title": title,
                "url": link,
                "published_date": pub_date,
                "summary": summary,
                "matched_keywords": keywords or [],
                "keyword_score": score,
                "location_relevance": location,
                "state": matched_state or query_state,
            })

        time.sleep(1)  # Rate limit between queries

    keyword_matches = len(matched_articles)
    logger.info("After dedup: %d new articles (%d URL dupes, %d title dupes)",
                keyword_matches, skipped_url, skipped_title)

    # Relevance scoring
    client = get_anthropic_client()
    api_key_set = client is not None
    scored = 0
    auto_approved = 0

    for article in matched_articles:
        if client:
            rel_score, rel_reason = _score_relevance(client, relevance_model, article)
            article["relevance_score"] = rel_score
            article["relevance_reason"] = rel_reason
            scored += 1
            time.sleep(0.3)

            # Auto-approve if above threshold
            if auto_threshold and rel_score >= auto_threshold:
                article_data = {
                    "source": article["source"],
                    "source_type": "websearch",
                    "title": article["title"],
                    "url": article["url"],
                    "published_date": article["published_date"],
                    "full_text": article["summary"],
                    "summary": article["summary"],
                    "matched_keywords": article["matched_keywords"],
                    "keyword_score": article["keyword_score"],
                }
                result = db.insert_article(conn, article_data)
                if result is not None:
                    auto_approved += 1
                    logger.info("Auto-approved: %s (rel=%d)", article["title"][:60], rel_score)
                continue
        else:
            article["relevance_score"] = None
            article["relevance_reason"] = None

        # Store in pending queue
        article["search_id"] = search_id
        article["source_type"] = "websearch"
        db.insert_pending_article(conn, article)

    db.insert_feed_run(conn, "Web Search", total_results, keyword_matches, "success")
    conn.close()

    return {
        "search_id": search_id,
        "query": query or "(per-state queries)",
        "total_results": total_results,
        "new_articles": keyword_matches,
        "skipped_url": skipped_url,
        "skipped_title": skipped_title,
        "scored": scored,
        "auto_approved": auto_approved,
        "api_key_set": api_key_set,
        "days_back": days_back,
    }
