"""
Wyoming Pulse — RSS Feed Ingestion
Fetches RSS feeds, filters articles by keyword relevance, and stores in database.
"""

import json
import logging
import re
import time
from datetime import datetime

import feedparser
import requests

import db
from shared import load_config  # noqa: F401 — re-exported for backward compat

logger = logging.getLogger("wyoming_pulse.ingest")

# User-agent to avoid being blocked by news sites
USER_AGENT = "WyomingPulse/1.0 (RSS Feed Reader; +https://github.com/prometheus-hyperscale)"


def normalize_text(text):
    """Lowercase and normalize whitespace for keyword matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.lower().strip())


def check_keyword_match(title, summary, config):
    """
    Check if an article matches keyword filters.
    Returns (matched_keywords, score) or (None, 0) if no match.
    """
    keywords = config.get("keywords", {})
    locations = config.get("locations", {})

    text = normalize_text(f"{title} {summary}")
    matched = []

    # Collect all location keywords for context checking
    all_location_keywords = []
    for loc_list in locations.values():
        all_location_keywords.extend(loc_list)
    location_pattern = [kw.lower() for kw in all_location_keywords]
    wyoming_context = ["wyoming"] + location_pattern

    has_wyoming_context = any(ctx in text for ctx in wyoming_context)

    # Check primary keywords — any single match = relevant (score 1.0)
    for kw in keywords.get("primary", []):
        if kw.lower() in text:
            matched.append(kw)
    if matched:
        return matched, 1.0

    # Check company keywords — require Wyoming context (score 0.8)
    for kw in keywords.get("companies", []):
        if kw.lower() in text and has_wyoming_context:
            matched.append(kw)
    if matched:
        return matched, 0.8

    # Check secondary keywords — require co-occurrence with location or primary (score 0.6)
    primary_present = any(kw.lower() in text for kw in keywords.get("primary", []))
    for kw in keywords.get("secondary", []):
        if kw.lower() in text and (has_wyoming_context or primary_present):
            matched.append(kw)
    if matched:
        return matched, 0.6

    return None, 0.0


def detect_location(title, summary, config):
    """Determine the geographic relevance of an article."""
    text = normalize_text(f"{title} {summary}")
    locations = config.get("locations", {})

    for loc_name, loc_keywords in locations.items():
        if loc_name == "statewide":
            continue
        for kw in loc_keywords:
            if kw.lower() in text:
                return loc_name

    return "statewide"


def parse_published_date(entry):
    """Extract and normalize the published date from a feed entry."""
    # feedparser provides published_parsed or updated_parsed as time.struct_time
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                dt = datetime(*parsed[:6])
                return dt.isoformat()
            except (ValueError, TypeError):
                pass

    # Try raw string fields
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            return raw

    return None


def extract_content(entry):
    """Extract the best available content from a feed entry."""
    # Try content field (some feeds provide full text)
    if hasattr(entry, "content") and entry.content:
        # content is a list of dicts with 'value' key
        parts = []
        for c in entry.content:
            val = c.get("value", "")
            if val:
                # Strip HTML tags for clean text
                clean = re.sub(r"<[^>]+>", " ", val)
                clean = re.sub(r"\s+", " ", clean).strip()
                parts.append(clean)
        if parts:
            return " ".join(parts)

    # Fall back to summary
    summary = getattr(entry, "summary", "") or ""
    if summary:
        clean = re.sub(r"<[^>]+>", " ", summary)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    return ""


def ingest_feeds(config=None):
    """
    Fetch all enabled RSS feeds, filter articles, and store matches.
    Returns dict with summary statistics.
    """
    if config is None:
        config = load_config()

    conn = db.get_connection()
    db.init_db()

    feeds = config.get("feeds", [])
    total_found = 0
    total_matched = 0
    total_new = 0

    for feed_cfg in feeds:
        name = feed_cfg.get("name", "Unknown")
        url = feed_cfg.get("url", "")
        enabled = feed_cfg.get("enabled", True)

        if not enabled or not url:
            logger.info("Skipping disabled/empty feed: %s", name)
            continue

        logger.info("Fetching feed: %s", name)
        try:
            # Use requests with a proper user-agent to avoid being blocked,
            # then hand the content to feedparser for parsing.
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except requests.RequestException as req_err:
                logger.warning("HTTP error for %s: %s — falling back to feedparser direct", name, req_err)
                parsed = feedparser.parse(url)

            if parsed.bozo and not parsed.entries:
                error_msg = str(getattr(parsed, "bozo_exception", "Unknown error"))
                logger.warning("Feed error for %s: %s", name, error_msg)
                db.insert_feed_run(conn, name, 0, 0, status=f"error: {error_msg[:100]}")
                continue

            feed_found = len(parsed.entries)
            feed_matched = 0

            for entry in parsed.entries:
                title = getattr(entry, "title", "") or ""
                summary = getattr(entry, "summary", "") or ""
                link = getattr(entry, "link", "") or ""

                matched_kw, score = check_keyword_match(title, summary, config)

                if matched_kw:
                    feed_matched += 1
                    full_text = extract_content(entry)
                    pub_date = parse_published_date(entry)

                    article_data = {
                        "source": name,
                        "source_type": "news",
                        "title": title.strip(),
                        "url": link.strip(),
                        "published_date": pub_date,
                        "full_text": full_text,
                        "summary": summary.strip()[:500] if summary else "",
                        "matched_keywords": matched_kw,
                        "keyword_score": score,
                    }

                    result = db.insert_article(conn, article_data)
                    if result is not None:
                        total_new += 1
                        logger.info("New article: [%s] %s (score=%.1f)", name, title[:60], score)

            total_found += feed_found
            total_matched += feed_matched
            db.insert_feed_run(conn, name, feed_found, feed_matched)
            logger.info("Feed %s: %d entries, %d matched keywords", name, feed_found, feed_matched)

        except Exception as e:
            logger.error("Failed to process feed %s: %s", name, e)
            db.insert_feed_run(conn, name, 0, 0, status=f"error: {str(e)[:100]}")

        # Rate limit between feeds
        time.sleep(2)

    conn.close()

    summary = {
        "feeds_checked": sum(1 for f in feeds if f.get("enabled") and f.get("url")),
        "total_entries": total_found,
        "keyword_matches": total_matched,
        "new_articles": total_new,
    }
    logger.info(
        "Ingestion complete: %d feeds, %d entries, %d matches, %d new",
        summary["feeds_checked"], total_found, total_matched, total_new,
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = ingest_feeds()
    print(f"\nIngestion Results:")
    print(f"  Feeds checked:    {result['feeds_checked']}")
    print(f"  Total entries:    {result['total_entries']}")
    print(f"  Keyword matches:  {result['keyword_matches']}")
    print(f"  New articles:     {result['new_articles']}")
