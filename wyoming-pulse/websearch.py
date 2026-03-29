"""
Wyoming Pulse — Web Search
Searches Google News RSS for articles, scores relevance with Claude API,
and stores results in a pending review queue.
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
from ingest import check_keyword_match, detect_location
from shared import load_config, get_anthropic_client
from utils import parse_json_response, clean_html, normalize_for_comparison

logger = logging.getLogger("wyoming_pulse.websearch")

USER_AGENT = "WyomingPulse/1.0 (News Research; +https://github.com/prometheus-hyperscale)"

DEFAULT_QUERY = '"Wyoming data center" OR "Prometheus Hyperscale" OR "hyperscale Wyoming"'

RELEVANCE_PROMPT_TEMPLATE = """You are evaluating whether a news article is relevant to tracking data center development in Wyoming, particularly for Prometheus Hyperscale (which has projects in Evanston and Casper, Wyoming).

Rate this article's relevance on a scale of 1-10:
- 10: Directly about Prometheus Hyperscale or Wyoming data center projects
- 7-9: About data centers, energy, or infrastructure in Wyoming
- 4-6: Tangentially related (e.g., national data center trends mentioning Wyoming)
- 1-3: Not relevant to Wyoming data center development

Return ONLY a JSON object (no markdown fences, no preamble):
{{"score": <int 1-10>, "reason": "<one sentence explanation>"}}

Article:
Title: $TITLE
Source: $SOURCE
Date: $DATE
Summary: $SUMMARY"""


def _parse_relevance_response(text):
    """Parse the JSON relevance score from Claude."""
    result = parse_json_response(text)
    if result is not None:
        score = int(result.get("score", 5))
        score = max(1, min(10, score))
        reason = result.get("reason", "")
        return score, reason

    # Regex fallback for malformed JSON
    score_match = re.search(r'"?score"?\s*[:=]\s*(\d+)', text)
    reason_match = re.search(r'"?reason"?\s*[:=]\s*"([^"]*)"', text)
    if score_match:
        score = max(1, min(10, int(score_match.group(1))))
        reason = reason_match.group(1) if reason_match else ""
        return score, reason
    return 5, "Could not parse relevance score"


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
        # Jaccard similarity
        intersection = norm_words & exist_words
        union = norm_words | exist_words
        similarity = len(intersection) / len(union) if union else 0
        if similarity >= 0.6:
            return True, existing

    return False, None


def run_websearch(query=None, days_back=30):
    """
    Search Google News RSS, score results with Claude, store in pending queue.
    Returns dict with summary statistics.
    """
    config = load_config()
    search_query = query.strip() if query else DEFAULT_QUERY
    search_id = str(uuid.uuid4())[:8]

    # Phase A: Fetch Google News RSS
    encoded_q = quote_plus(f"{search_query} when:{days_back}d")
    rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=US&ceid=US:en"

    logger.info("Searching Google News: %s", search_query)

    try:
        resp = requests.get(rss_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        feed = feedparser.parse(resp.content)
    except Exception as e:
        logger.error("Failed to fetch Google News RSS: %s", e)
        return {"error": str(e), "search_id": search_id, "total_results": 0}

    total_results = len(feed.entries)
    logger.info("Found %d results from Google News", total_results)

    # Phase A continued: Filter, dedup by URL and title similarity
    db.init_db()
    conn = db.get_connection()

    # Load existing titles for similarity checking (articles + pending)
    existing_rows = conn.execute("SELECT title FROM articles").fetchall()
    pending_rows = conn.execute(
        "SELECT title FROM pending_articles WHERE status = 'pending'"
    ).fetchall()
    existing_titles = [r["title"] for r in existing_rows] + [r["title"] for r in pending_rows]

    matched_articles = []
    skipped_url = 0
    skipped_title = 0
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

        # Dedup: skip if URL already in articles table
        existing = conn.execute(
            "SELECT id FROM articles WHERE url = ?", (link,)
        ).fetchone()
        if existing:
            skipped_url += 1
            continue

        # Also skip if already in pending
        existing_pending = conn.execute(
            "SELECT id FROM pending_articles WHERE url = ? AND status = 'pending'",
            (link,),
        ).fetchone()
        if existing_pending:
            skipped_url += 1
            continue

        # Title similarity check against existing articles
        is_dup, matched_title = _check_title_similarity(title, existing_titles)
        if is_dup:
            skipped_title += 1
            logger.debug("Title duplicate: '%s' ~ '%s'", title, matched_title)
            continue

        # Add this title to the list so within-batch duplicates are caught
        existing_titles.append(title)

        # Keyword filtering
        keywords, score = check_keyword_match(title, summary, config)
        location = detect_location(title, summary, config)

        matched_articles.append({
            "source": source,
            "title": title,
            "url": link,
            "published_date": pub_date,
            "summary": summary,
            "matched_keywords": keywords or [],
            "keyword_score": score,
            "location_relevance": location,
        })

    keyword_matches = len(matched_articles)
    logger.info("After dedup: %d new articles (%d URL dupes, %d title dupes)",
                keyword_matches, skipped_url, skipped_title)

    # Check API key availability early (used for status reporting)
    client = get_anthropic_client()
    api_key_set = client is not None

    if not matched_articles:
        conn.close()
        db.insert_feed_run(db.get_connection(), "Web Search", total_results, 0, "success")
        return {
            "search_id": search_id,
            "query": search_query,
            "total_results": total_results,
            "new_articles": 0,
            "skipped_url": skipped_url,
            "skipped_title": skipped_title,
            "scored": 0,
            "api_key_set": api_key_set,
            "days_back": days_back,
        }

    # Phase B: Claude relevance scoring
    model = config.get("anthropic", {}).get(
        "classification_model", "claude-haiku-4-5-20251001"
    )
    scored = 0

    for article in matched_articles:
        if client:
            try:
                prompt = (RELEVANCE_PROMPT_TEMPLATE
                    .replace("$TITLE", article["title"])
                    .replace("$SOURCE", article["source"])
                    .replace("$DATE", article["published_date"] or "Unknown")
                    .replace("$SUMMARY", article["summary"][:500])
                )
                response = client.messages.create(
                    model=model,
                    max_tokens=150,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.content[0].text
                score, reason = _parse_relevance_response(text)
                article["relevance_score"] = score
                article["relevance_reason"] = reason
                scored += 1
                time.sleep(0.3)  # Rate limiting
            except Exception as e:
                logger.warning("Relevance scoring failed for '%s': [%s] %s",
                               article["title"], type(e).__name__, e)
                article["relevance_score"] = 5
                article["relevance_reason"] = "Scoring error"
        else:
            # No API key — leave score/reason as None so the UI can show "unscored"
            article["relevance_score"] = None
            article["relevance_reason"] = None

    # Phase C: Store in pending queue
    for article in matched_articles:
        article["search_id"] = search_id
        db.insert_pending_article(conn, article)

    # Log the search run
    db.insert_feed_run(conn, "Web Search", total_results, keyword_matches, "success")
    conn.close()

    return {
        "search_id": search_id,
        "query": search_query,
        "total_results": total_results,
        "new_articles": keyword_matches,
        "skipped_url": skipped_url,
        "skipped_title": skipped_title,
        "scored": scored,
        "api_key_set": api_key_set,
        "days_back": days_back,
    }
