"""
Prometheus — RSS Feed Ingestion
Fetches per-state RSS feeds, filters articles by keyword relevance,
scores with Claude API, and stores in the pending review queue.
"""

import logging
import re
import time
import uuid
from datetime import datetime, timezone

import feedparser
import requests

import db
from geo import infer_state_from_text
from shared import load_config, get_anthropic_client
from utils import clean_html

logger = logging.getLogger("resistance_dashboard.ingest")

USER_AGENT = "PrometheusDashboard/1.0 (RSS Feed Reader; +https://github.com/prometheus-hyperscale)"

RELEVANCE_PROMPT = """Score this article's relevance to data center development in the United States on a 1-10 scale.

Context: We track public sentiment toward data center projects nationwide. Prometheus Hyperscale is a data center developer we track closely.

Scoring guide:
10: Directly about Prometheus Hyperscale, its leadership, or its projects
9: Data center project news naming a specific company and location
8: Explicitly about data center development, siting, bans, moratoriums, or community opposition in any US state
7: About data center infrastructure, permitting, tax incentives, or utility/grid deals for data centers
6: Data center industry trends, market analysis, or company expansions with specific locations
5: General AI infrastructure or hyperscaler capital spending with data center implications
4: General AI industry news or tech company strategy tangentially related to data centers
3: Energy/grid policy or land use without specific data center connection
1-2: No relevance to data centers (junk, unrelated local news, sports, etc.)

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


# Short, precise list for company keyword co-occurrence filtering.
# A company name must appear alongside one of these to match.
TOPIC_SIGNALS = [
    "data center", "data centre", "datacenter", "hyperscale",
    "server farm", "colocation", "megawatt", "gigawatt",
    "data hall", "compute", "ai infrastructure", "cooling",
    "power capacity", "rack", "campus",
]


def _build_keyword_set(config, state_key=None):
    """
    Build merged keyword lists from nationwide + optional priority_state.
    Returns dict with primary, companies, secondary, and state_locations.
    """
    nationwide = config.get("nationwide", {}).get("keywords", {})
    result = {
        "primary": [kw.lower() for kw in nationwide.get("primary", [])],
        "companies": [kw.lower() for kw in nationwide.get("companies", [])],
        "secondary": [kw.lower() for kw in nationwide.get("secondary", [])],
        "state_locations": [],
    }
    if state_key:
        state_cfg = config.get("priority_states", {}).get(state_key, {})
        state_kw = state_cfg.get("keywords", {})
        result["primary"].extend(kw.lower() for kw in state_kw.get("primary", []))
        result["companies"].extend(kw.lower() for kw in state_kw.get("companies", []))
        result["secondary"].extend(kw.lower() for kw in state_kw.get("secondary", []))
        # Collect location keywords for state context checking
        locs = state_cfg.get("locations", {})
        for loc_list in locs.values():
            result["state_locations"].extend(kw.lower() for kw in loc_list)
        result["state_locations"].append(state_key.lower())
    return result


def _match_keywords(text, keywords):
    """
    Match keywords against text using tiered logic.
    Returns (matched_keywords_list, score) or (None, 0.0).
    """
    matched = []

    # Tier 1: Primary keywords — any single match (score 1.0)
    # Note: keywords are pre-lowercased by _build_keyword_set.
    for kw in keywords["primary"]:
        if kw in text:
            matched.append(kw)
    if matched:
        return matched, 1.0

    # Context checks for lower tiers
    has_state_context = any(loc in text for loc in keywords["state_locations"])
    has_topic_signal = any(sig in text for sig in TOPIC_SIGNALS)

    # Tier 2: Company keywords — require topic signal OR state context
    for kw in keywords["companies"]:
        if kw in text and (has_topic_signal or has_state_context):
            matched.append(kw)
    if matched:
        return matched, 0.9 if has_topic_signal else 0.8

    # Tier 3: Secondary — require topic signal, company presence, or state context
    company_present = any(kw in text for kw in keywords["companies"])
    for kw in keywords["secondary"]:
        if kw in text and (has_topic_signal or company_present or has_state_context):
            matched.append(kw)
    if matched:
        return matched, 0.6

    return None, 0.0


def check_keyword_match(title, summary, config, state_key=None):
    """
    Check if an article is relevant and determine which state it belongs to.

    Two separate concerns:
      1. RELEVANCE — does the article match topic keywords? (nationwide + state-specific)
      2. CATEGORIZATION — which state is it about? (priority state locations > text inference)

    Returns (matched_keywords, score, matched_state) or (None, 0, None).
    """
    text = normalize_text(f"{title} {summary}")
    keywords = _build_keyword_set(config, state_key)
    matched, score = _match_keywords(text, keywords)

    if not matched:
        return None, 0.0, None

    # Determine state
    if state_key:
        # Check if article actually mentions this state's locations
        if any(loc in text for loc in keywords["state_locations"]):
            return matched, score, state_key
        # Infer from text, but default to the query state
        inferred = infer_state_from_text(title, summary)
        return matched, score, inferred or state_key

    # No state hint — check priority state locations first
    priority_states = config.get("priority_states", {})
    for sk, st_cfg in priority_states.items():
        locs = st_cfg.get("locations", {})
        all_loc_kw = [kw.lower() for loc_list in locs.values() for kw in loc_list] + [sk]
        if any(kw in text for kw in all_loc_kw):
            return matched, score, sk

    # Infer state from article text (handles all 50 states via geo.py)
    inferred = infer_state_from_text(title, summary)
    return matched, score, inferred


def detect_location(title, summary, config, state_key=None):
    """Determine the geographic relevance of an article."""
    text = normalize_text(f"{title} {summary}")

    if state_key:
        state_cfg = config.get("priority_states", {}).get(state_key, {})
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
                parts.append(clean_html(val))
        if parts:
            return " ".join(parts)
    summary = getattr(entry, "summary", "") or ""
    if summary:
        return clean_html(summary)
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


def score_relevance_hybrid(article, client=None, model=None, local_ready=None):
    """
    Score relevance using local LLM first, falling back to Claude API.
    Returns (score, reason) tuple.
    """
    try:
        import local_llm
        if local_ready is None:
            local_ready = local_llm.ensure_running()
        if local_ready:
            score, reason = local_llm.score_relevance(
                article.get("title", ""),
                article.get("summary", ""),
                article.get("source", ""),
            )
            return score, reason
    except Exception as e:
        logger.debug("Local LLM unavailable: %s", e)

    # Fallback to Claude API
    if client and model:
        return _score_relevance(client, model, article)

    return None, None


def ingest_feeds(config=None, state=None):
    """
    Fetch all enabled RSS feeds, filter articles, score relevance,
    and store in the pending review queue.
    Returns dict with summary statistics.
    """
    if config is None:
        config = load_config()

    conn = db.get_connection()
    try:
        db.init_db()

        pipeline = config.get("pipeline", {})
        auto_threshold = pipeline.get("auto_approve_threshold")
        reject_threshold = pipeline.get("auto_reject_threshold")
        relevance_model = pipeline.get("relevance_model",
                                        config.get("anthropic", {}).get("classification_model",
                                                                         "claude-haiku-4-5-20251001"))
        client = get_anthropic_client()
        try:
            import local_llm
            local_ready = local_llm.ensure_running()
        except Exception as e:
            logger.debug("Local LLM unavailable: %s", e)
            local_ready = False

        existing_urls = {
            r["url"] for r in conn.execute(
                "SELECT url FROM articles WHERE url IS NOT NULL AND url != ''"
            ).fetchall()
        }
        existing_urls.update(
            r["url"] for r in conn.execute(
                "SELECT url FROM pending_articles WHERE status = 'pending' AND url IS NOT NULL AND url != ''"
            ).fetchall()
        )

        # Collect feeds from per-state configs
        all_feeds = []
        states = config.get("priority_states", {})
        for state_key, state_cfg in states.items():
            if state and state_key != state:
                continue
            for feed_cfg in state_cfg.get("feeds", []):
                feed_cfg = dict(feed_cfg)  # copy
                feed_cfg["_state"] = state_key
                all_feeds.append(feed_cfg)

        # Legacy fallback: top-level feeds (no state association — will be inferred from text)
        if not all_feeds and "feeds" in config:
            for feed_cfg in config["feeds"]:
                feed_cfg = dict(feed_cfg)
                feed_cfg["_state"] = None
                all_feeds.append(feed_cfg)

        total_found = 0
        total_matched = 0
        total_new = 0
        total_auto_approved = 0
        total_auto_rejected = 0

        for feed_cfg in all_feeds:
            name = feed_cfg.get("name", "Unknown")
            url = feed_cfg.get("url", "")
            enabled = feed_cfg.get("enabled", True)
            feed_state = feed_cfg.get("_state")

            if not enabled or not url:
                logger.info("Skipping disabled/empty feed: %s", name)
                continue

            search_id = f"rss_{name.lower().replace(' ', '_')}_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d_%H%M')}"
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

                        # Determine final state: keyword match > feed state > text inference
                        final_state = matched_state or feed_state
                        if not final_state:
                            final_state = infer_state_from_text(title, summary)
                        location = "international" if final_state == "international" else detect_location(
                            title, summary, config, state_key=final_state
                        )

                        # Skip articles with no URL entirely
                        if not link:
                            logger.debug("Skipping article with empty URL: %s", title[:60])
                            continue

                        # Dedup: skip if URL already in articles or pending
                        if link in existing_urls:
                            continue

                        # Score relevance (local LLM first, Claude fallback)
                        article_for_scoring = {
                            "title": title, "source": name,
                            "published_date": pub_date, "summary": summary,
                        }
                        rel_score, rel_reason = score_relevance_hybrid(
                            article_for_scoring, client, relevance_model, local_ready=local_ready,
                        )
                        if rel_score is not None and not local_ready:
                            time.sleep(0.1)  # Brief pause (local LLM is fast)

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
                                "state": final_state,
                                "location_relevance": location,
                            }
                            result = db.insert_article(conn, article_data)
                            if result is not None:
                                existing_urls.add(link.strip())
                                total_auto_approved += 1
                                total_new += 1
                                logger.info("Auto-approved: [%s] %s (rel=%d)", name, title[:60], rel_score)
                            continue

                        # Auto-reject if below threshold
                        if reject_threshold and rel_score and rel_score <= reject_threshold:
                            total_auto_rejected += 1
                            logger.debug("Auto-rejected: [%s] %s (rel=%d)", name, title[:60], rel_score)
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
                            "state": final_state,
                        }
                        db.insert_pending_article(conn, pending_data)
                        existing_urls.add(link.strip())
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
    finally:
        conn.close()

    summary = {
        "feeds_checked": sum(1 for f in all_feeds if f.get("enabled") and f.get("url")),
        "total_entries": total_found,
        "keyword_matches": total_matched,
        "new_articles": total_new,
        "auto_approved": total_auto_approved,
        "auto_rejected": total_auto_rejected,
    }
    logger.info(
        "Ingestion complete: %d feeds, %d entries, %d matches, %d new (%d auto-approved, %d auto-rejected)",
        summary["feeds_checked"], total_found, total_matched, total_new, total_auto_approved, total_auto_rejected,
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
