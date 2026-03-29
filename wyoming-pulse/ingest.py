"""
Prometheus — RSS Feed Ingestion
Fetches per-state RSS feeds, filters articles by keyword relevance,
scores with Claude API, and stores in the pending review queue.
"""

import logging
import re
import time
import uuid
from datetime import datetime

import feedparser
import requests

import db
from shared import load_config, get_anthropic_client

logger = logging.getLogger("wyoming_pulse.ingest")

USER_AGENT = "PrometheusDashboard/1.0 (RSS Feed Reader; +https://github.com/prometheus-hyperscale)"

RELEVANCE_PROMPT = """Score this article's relevance to data center development on a 1-10 scale.

Context: We track public sentiment toward data center projects across Wyoming, Texas, and Michigan for Prometheus Hyperscale.

Scoring guide:
10: Directly about Prometheus Hyperscale or its projects
8-9: About data center development/infrastructure in a tracked state (Wyoming, Texas, Michigan)
6-7: About data center industry trends, energy policy, or grid impacts relevant to these states
4-5: Tangentially related (general energy, tech industry, land use)
1-3: Not relevant to data center development

Return ONLY a JSON object (no markdown fences, no preamble):
{{"score": <int 1-10>, "reason": "<one sentence explanation>"}}

Article:
Title: {title}
Source: {source}
Date: {date}
Summary: {summary}"""


def normalize_text(text):
    """Lowercase and normalize whitespace for keyword matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.lower().strip())


def _build_state_config(config, state_key):
    """
    Build a merged config for a specific state, combining state-specific
    and global keywords/locations.
    """
    states = config.get("states", {})
    state_cfg = states.get(state_key, {})
    global_kw = config.get("global_keywords", {})

    # Merge keywords: state-specific + global
    state_kw = state_cfg.get("keywords", {})
    merged_keywords = {
        "primary": state_kw.get("primary", []) + global_kw.get("primary", []),
        "companies": state_kw.get("companies", []) + global_kw.get("companies", []),
        "secondary": state_kw.get("secondary", []),
    }

    return {
        "keywords": merged_keywords,
        "locations": state_cfg.get("locations", {}),
        "state_key": state_key,
    }


def check_keyword_match(title, summary, config, state_key=None):
    """
    Check if an article matches keyword filters.
    If state_key is provided, uses that state's config.
    Otherwise falls back to legacy config structure.
    Returns (matched_keywords, score, matched_state) or (None, 0, None).
    """
    states = config.get("states", {})

    # If a specific state is given, only check that state
    if state_key:
        state_cfg = _build_state_config(config, state_key)
        matched, score = _check_keywords_for_state(title, summary, state_cfg, state_key)
        if matched:
            return matched, score, state_key
        return None, 0.0, None

    # Otherwise, check all states — prefer states with location context
    text = normalize_text(f"{title} {summary}")
    best_match = None
    for sk in states:
        state_cfg = _build_state_config(config, sk)
        matched, score = _check_keywords_for_state(title, summary, state_cfg, sk)
        if matched:
            # Check if this state has location context in the text
            locs = state_cfg.get("locations", {})
            all_loc_kw = [kw.lower() for loc_list in locs.values() for kw in loc_list] + [sk]
            has_context = any(kw in text for kw in all_loc_kw)
            if has_context:
                return matched, score, sk
            if best_match is None:
                best_match = (matched, score, sk)
    if best_match:
        return best_match

    # Legacy fallback: use top-level keywords/locations if present
    if "keywords" in config:
        legacy_cfg = {"keywords": config["keywords"], "locations": config.get("locations", {})}
        matched, score = _check_keywords_for_state(title, summary, legacy_cfg, "wyoming")
        if matched:
            return matched, score, "wyoming"

    return None, 0.0, None


def _check_keywords_for_state(title, summary, state_cfg, state_key):
    """Check keywords for a single state config. Returns (matched, score)."""
    keywords = state_cfg.get("keywords", {})
    locations = state_cfg.get("locations", {})

    text = normalize_text(f"{title} {summary}")
    matched = []

    # Build state context from location keywords + state name
    all_location_keywords = []
    for loc_list in locations.values():
        all_location_keywords.extend(loc_list)
    location_pattern = [kw.lower() for kw in all_location_keywords]
    state_context = [state_key] + location_pattern

    has_state_context = any(ctx in text for ctx in state_context)

    # Primary keywords — any single match = relevant (score 1.0)
    for kw in keywords.get("primary", []):
        if kw.lower() in text:
            matched.append(kw)
    if matched:
        return matched, 1.0

    # Company keywords — require state context (score 0.8)
    for kw in keywords.get("companies", []):
        if kw.lower() in text and has_state_context:
            matched.append(kw)
    if matched:
        return matched, 0.8

    # Secondary keywords — require co-occurrence with location or primary (score 0.6)
    primary_present = any(kw.lower() in text for kw in keywords.get("primary", []))
    for kw in keywords.get("secondary", []):
        if kw.lower() in text and (has_state_context or primary_present):
            matched.append(kw)
    if matched:
        return matched, 0.6

    return None, 0.0


def detect_location(title, summary, config, state_key=None):
    """Determine the geographic relevance of an article."""
    text = normalize_text(f"{title} {summary}")

    if state_key:
        state_cfg = config.get("states", {}).get(state_key, {})
        locations = state_cfg.get("locations", {})
    else:
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
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                dt = datetime(*parsed[:6])
                return dt.isoformat()
            except (ValueError, TypeError):
                pass
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            return raw
    return None


def extract_content(entry):
    """Extract the best available content from a feed entry."""
    if hasattr(entry, "content") and entry.content:
        parts = []
        for c in entry.content:
            val = c.get("value", "")
            if val:
                clean = re.sub(r"<[^>]+>", " ", val)
                clean = re.sub(r"\s+", " ", clean).strip()
                parts.append(clean)
        if parts:
            return " ".join(parts)
    summary = getattr(entry, "summary", "") or ""
    if summary:
        clean = re.sub(r"<[^>]+>", " ", summary)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean
    return ""


def _score_relevance(client, model, article):
    """Score an article's relevance using Claude API. Returns (score, reason)."""
    try:
        prompt = RELEVANCE_PROMPT.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            date=article.get("published_date") or "Unknown",
            summary=(article.get("summary") or "")[:500],
        )
        response = client.messages.create(
            model=model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        from utils import parse_json_response
        result = parse_json_response(text)
        if result:
            score = max(1, min(10, int(result.get("score", 5))))
            return score, result.get("reason", "")

        # Regex fallback
        score_match = re.search(r'"?score"?\s*[:=]\s*(\d+)', text)
        reason_match = re.search(r'"?reason"?\s*[:=]\s*"([^"]*)"', text)
        if score_match:
            score = max(1, min(10, int(score_match.group(1))))
            reason = reason_match.group(1) if reason_match else ""
            return score, reason
    except Exception as e:
        logger.warning("Relevance scoring failed: %s", e)
    return 5, "Scoring error"


def ingest_feeds(config=None, state=None):
    """
    Fetch all enabled RSS feeds, filter articles, score relevance,
    and store in the pending review queue.
    Returns dict with summary statistics.
    """
    if config is None:
        config = load_config()

    conn = db.get_connection()
    db.init_db()

    pipeline = config.get("pipeline", {})
    auto_threshold = pipeline.get("auto_approve_threshold")
    relevance_model = pipeline.get("relevance_model",
                                    config.get("anthropic", {}).get("classification_model",
                                                                     "claude-haiku-4-5-20251001"))
    client = get_anthropic_client()

    # Collect feeds from per-state configs
    all_feeds = []
    states = config.get("states", {})
    for state_key, state_cfg in states.items():
        if state and state_key != state:
            continue
        for feed_cfg in state_cfg.get("feeds", []):
            feed_cfg = dict(feed_cfg)  # copy
            feed_cfg["_state"] = state_key
            all_feeds.append(feed_cfg)

    # Legacy fallback: top-level feeds
    if not all_feeds and "feeds" in config:
        for feed_cfg in config["feeds"]:
            feed_cfg = dict(feed_cfg)
            feed_cfg["_state"] = "wyoming"
            all_feeds.append(feed_cfg)

    total_found = 0
    total_matched = 0
    total_new = 0
    total_auto_approved = 0

    for feed_cfg in all_feeds:
        name = feed_cfg.get("name", "Unknown")
        url = feed_cfg.get("url", "")
        enabled = feed_cfg.get("enabled", True)
        feed_state = feed_cfg.get("_state", "wyoming")

        if not enabled or not url:
            logger.info("Skipping disabled/empty feed: %s", name)
            continue

        search_id = f"rss_{name.lower().replace(' ', '_')}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}"
        logger.info("Fetching feed: %s [%s]", name, feed_state)

        try:
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

                matched_kw, score, matched_state = check_keyword_match(
                    title, summary, config, state_key=feed_state
                )

                if matched_kw:
                    feed_matched += 1
                    full_text = extract_content(entry)
                    pub_date = parse_published_date(entry)
                    location = detect_location(title, summary, config, state_key=feed_state)

                    # Dedup: skip if URL already in articles or pending
                    if link:
                        existing = conn.execute(
                            "SELECT id FROM articles WHERE url = ?", (link,)
                        ).fetchone()
                        if existing:
                            continue
                        existing_pending = conn.execute(
                            "SELECT id FROM pending_articles WHERE url = ? AND status = 'pending'",
                            (link,),
                        ).fetchone()
                        if existing_pending:
                            continue

                    # Score relevance
                    rel_score = None
                    rel_reason = None
                    if client:
                        rel_score, rel_reason = _score_relevance(client, relevance_model, {
                            "title": title, "source": name,
                            "published_date": pub_date, "summary": summary,
                        })
                        time.sleep(0.3)

                    # Auto-approve if above threshold
                    if auto_threshold and rel_score and rel_score >= auto_threshold:
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
                            total_auto_approved += 1
                            total_new += 1
                            logger.info("Auto-approved: [%s] %s (rel=%d)", name, title[:60], rel_score)
                        continue

                    # Otherwise, store in pending queue
                    pending_data = {
                        "search_id": search_id,
                        "source": name,
                        "title": title.strip(),
                        "url": link.strip(),
                        "published_date": pub_date,
                        "summary": summary.strip()[:500] if summary else "",
                        "matched_keywords": matched_kw,
                        "keyword_score": score,
                        "location_relevance": location,
                        "relevance_score": rel_score,
                        "relevance_reason": rel_reason,
                        "source_type": "rss",
                        "state": matched_state or feed_state,
                    }
                    db.insert_pending_article(conn, pending_data)
                    total_new += 1
                    logger.info("Queued: [%s] %s (rel=%s)", name, title[:60], rel_score)

            total_found += feed_found
            total_matched += feed_matched
            db.insert_feed_run(conn, name, feed_found, feed_matched)
            logger.info("Feed %s: %d entries, %d matched keywords", name, feed_found, feed_matched)

        except Exception as e:
            logger.error("Failed to process feed %s: %s", name, e)
            db.insert_feed_run(conn, name, 0, 0, status=f"error: {str(e)[:100]}")

        time.sleep(2)

    conn.close()

    summary = {
        "feeds_checked": sum(1 for f in all_feeds if f.get("enabled") and f.get("url")),
        "total_entries": total_found,
        "keyword_matches": total_matched,
        "new_articles": total_new,
        "auto_approved": total_auto_approved,
    }
    logger.info(
        "Ingestion complete: %d feeds, %d entries, %d matches, %d new (%d auto-approved)",
        summary["feeds_checked"], total_found, total_matched, total_new, total_auto_approved,
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
    print(f"  Auto-approved:    {result.get('auto_approved', 0)}")
