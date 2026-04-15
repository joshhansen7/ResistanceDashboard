"""
Prometheus — Web Search
Searches Google News RSS for articles across tracked states, scores relevance
with Claude API, and stores results in the unified pending review queue.

Supports two modes:
  - Default (per_state=False): runs nationwide + priority-state queries
  - Sweep  (per_state=True):  runs sweep_queries templates × all 50 states
"""

import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests

import db
import geo
from geo import infer_state_from_text
from ingest import check_keyword_match, detect_location, _score_relevance
from shared import load_config, get_anthropic_client
from utils import clean_html, normalize_for_comparison

logger = logging.getLogger("wyoming_pulse.websearch")

USER_AGENT = "PrometheusDashboard/1.0 (News Research; +https://github.com/prometheus-hyperscale)"

DEFAULT_QUERY = '"data center"'

SWEEP_PROGRESS_PATH = Path(__file__).parent / "data" / "sweep_progress.json"


# ── Progress tracking (for per-state sweep mode) ────────────────────

def _load_sweep_progress():
    """Load the set of completed (state, query, date_key) tuples."""
    if SWEEP_PROGRESS_PATH.exists():
        try:
            data = json.loads(SWEEP_PROGRESS_PATH.read_text())
            return set(tuple(item) for item in data.get("completed", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def _save_sweep_progress(completed):
    """Persist completed tuples so a sweep can be resumed."""
    SWEEP_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"completed": [list(item) for item in sorted(completed)]}
    tmp_path = SWEEP_PROGRESS_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2))
    tmp_path.replace(SWEEP_PROGRESS_PATH)


def _clear_sweep_progress():
    """Remove progress file (e.g. after a completed sweep)."""
    if SWEEP_PROGRESS_PATH.exists():
        SWEEP_PROGRESS_PATH.unlink()


# ── Utilities ────────────────────────────────────────────────────────

def _extract_source_from_title(title):
    """Google News RSS titles often end with ' - Source Name'. Extract it."""
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        return parts[1].strip(), parts[0].strip()
    return "Google News", title


def _title_signature(title):
    """Return a normalized title signature for fuzzy duplicate checks."""
    norm = normalize_for_comparison(title)
    words = set(norm.split())
    if len(words) < 3:
        return None
    return {"title": title, "words": words}


def _check_title_similarity(title, existing_signatures):
    """
    Check if a title is similar to any existing title.
    Returns (is_duplicate, matched_title) using word overlap.
    """
    signature = _title_signature(title)
    if signature is None:
        return False, None
    norm_words = signature["words"]

    for existing in existing_signatures:
        exist_words = existing["words"]
        intersection = norm_words & exist_words
        union = norm_words | exist_words
        similarity = len(intersection) / len(union) if union else 0
        if similarity >= 0.6:
            return True, existing["title"]

    return False, None


def _resolve_article_url(article, resolved_cache, existing_urls):
    """
    Resolve Google News redirect URLs only for articles we intend to persist.
    Returns the final URL to store or None if the resolved URL already exists.
    """
    url = article.get("url") or ""
    if not url or "news.google.com" not in url:
        return url or None

    cached = resolved_cache.get(url)
    if cached is None:
        from scraper import resolve_google_news_url

        cached = resolve_google_news_url(
            url,
            interval=0.3,
            title=article.get("title"),
            source=article.get("source"),
        )
        resolved_cache[url] = cached

    final_url = cached or url
    if final_url != url and final_url in existing_urls:
        logger.debug("Resolved URL duplicate skipped: %s", final_url[:80])
        return None
    return final_url


# ── Main search function ─────────────────────────────────────────────

def run_websearch(query=None, days_back=30, state=None,
                  per_state=False, start_date=None, end_date=None,
                  skip_analysis=False, progress_callback=None):
    """
    Search Google News RSS, score results with Claude, store in pending queue.

    Default mode (per_state=False):
        Runs nationwide + priority-state queries. Same as before.

    Sweep mode (per_state=True):
        Runs sweep_queries templates × all 50 US states.
        Supports date-range (start_date/end_date) or days_back.
        Includes progress tracking for resumability.
        Optionally chains sentiment analysis after insert.

    Returns dict with summary statistics.
    """
    config = load_config()
    pipeline = config.get("pipeline", {})
    auto_threshold = pipeline.get("auto_approve_threshold")
    reject_threshold = pipeline.get("auto_reject_threshold")
    relevance_model = pipeline.get("relevance_model",
                                    config.get("anthropic", {}).get("classification_model",
                                                                     "claude-haiku-4-5-20251001"))

    search_id = str(uuid.uuid4())[:8]

    # ── Build query list ──────────────────────────────────────────
    queries_to_run = []

    if per_state:
        # Sweep mode: template queries × all states
        nationwide = config.get("nationwide", {})
        templates = nationwide.get("sweep_queries", [])
        if not templates:
            # Fallback if sweep_queries not configured
            templates = [DEFAULT_QUERY]

        # Get all 50 states (or filter to one)
        all_states = geo.get_all_states()
        if state:
            state_keys = [state] if state in all_states else []
            if not state_keys:
                logger.warning("Unknown state key: %s", state)
                return {"error": f"Unknown state: {state}"}
        else:
            state_keys = sorted(all_states.keys())

        for state_key in state_keys:
            state_name = all_states[state_key]["name"]
            for template in templates:
                # Append quoted state name to template
                full_query = f'{template} "{state_name}"'
                queries_to_run.append({
                    "query": full_query,
                    "state": state_key,
                    "template": template,  # For progress tracking
                })

        # Default to 7 days for sweep if no explicit date params
        if not start_date and days_back == 30:
            days_back = 7

        logger.info("Sweep mode: %d templates × %d states = %d queries",
                    len(templates), len(state_keys), len(queries_to_run))
    elif query:
        # Explicit query — run it without state association
        queries_to_run.append({"query": query.strip(), "state": state})
    else:
        # Default: nationwide discovery + priority state queries
        if not state:
            nationwide = config.get("nationwide", {})
            for q in nationwide.get("web_search_queries", []):
                queries_to_run.append({"query": q, "state": None})

        priority_states = config.get("priority_states", {})
        for state_key, state_cfg in priority_states.items():
            if state and state_key != state:
                continue
            for q in state_cfg.get("web_search_queries", []):
                queries_to_run.append({"query": q, "state": state_key})

        if not queries_to_run:
            queries_to_run.append({"query": DEFAULT_QUERY, "state": None})

    logger.info("Running %d search queries (per_state=%s)", len(queries_to_run), per_state)

    # ── Progress tracking (sweep mode only) ───────────────────────
    completed_progress = set()
    if per_state:
        completed_progress = _load_sweep_progress()
        # Date key for progress: identifies this sweep's date window
        if start_date:
            date_key = f"{start_date}:{end_date or 'now'}"
        else:
            date_key = f"days:{days_back}"

    # ── Database setup ────────────────────────────────────────────
    db.init_db()
    conn = db.get_connection()
    try:
        # Load existing titles and URLs up front so we do constant-time dedup
        existing_rows = conn.execute(
            "SELECT url, title FROM articles WHERE (url IS NOT NULL AND url != '') OR title IS NOT NULL"
        ).fetchall()
        pending_rows = conn.execute(
            "SELECT url, title FROM pending_articles WHERE status = 'pending' "
            "AND ((url IS NOT NULL AND url != '') OR title IS NOT NULL)"
        ).fetchall()
        existing_urls = {
            r["url"] for r in existing_rows if r["url"]
        }
        existing_urls.update(
            r["url"] for r in pending_rows if r["url"]
        )
        existing_title_signatures = [
            sig for sig in (
                _title_signature(r["title"]) for r in list(existing_rows) + list(pending_rows)
            ) if sig is not None
        ]

        seen_urls = set()

        total_results = 0
        skipped_url = 0
        skipped_title = 0
        skipped_progress = 0
        matched_articles = []
        states_searched = set()
        resolved_url_cache = {}

        for i, query_info in enumerate(queries_to_run):
            search_query = query_info["query"]
            query_state = query_info["state"]
            if progress_callback is not None:
                progress_callback({
                    "phase": "querying",
                    "current": i + 1,
                    "total": len(queries_to_run),
                    "state": query_state,
                    "query": search_query,
                })

            # Progress check (sweep mode)
            if per_state and "template" in query_info:
                progress_key = (query_state or "", query_info["template"], date_key)
                if progress_key in completed_progress:
                    skipped_progress += 1
                    continue

            # Build RSS URL with date filtering
            if start_date:
                # Date-range mode: after:/before: operators
                date_filter = f" after:{start_date}"
                if end_date:
                    date_filter += f" before:{end_date}"
                encoded_q = quote_plus(search_query + date_filter)
            else:
                # Days-back mode
                encoded_q = quote_plus(search_query) + f"+when:{days_back}d"

            rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=US&ceid=US:en"

            if per_state:
                # Log progress periodically (every 10 queries)
                if i % 10 == 0:
                    logger.info("Sweep progress: %d/%d queries (state: %s)",
                               i + 1, len(queries_to_run), query_state or "all")
            else:
                logger.info("Searching: %s [%s]", search_query, query_state or "all")

            try:
                resp = requests.get(rss_url, headers={"User-Agent": USER_AGENT}, timeout=15)
                feed = feedparser.parse(resp.content)
            except Exception as e:
                logger.error("Failed to fetch Google News RSS: %s", e)
                # Still mark as completed so we don't retry on resume
                if per_state and "template" in query_info:
                    completed_progress.add(progress_key)
                continue

            total_results += len(feed.entries)
            if query_state:
                states_searched.add(query_state)

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
                            pub_date = datetime(*parsed[:6]).isoformat()
                        except (ValueError, TypeError):
                            pass
                        break

                if not link:
                    logger.debug("Skipping websearch result with empty URL: %s", title[:60])
                    continue

                # Cross-query dedup
                if link in seen_urls:
                    continue
                seen_urls.add(link)

                # URL dedup against articles + pending
                if link in existing_urls:
                    skipped_url += 1
                    continue

                # Title similarity check
                is_dup, matched_title = _check_title_similarity(title, existing_title_signatures)
                if is_dup:
                    skipped_title += 1
                    logger.debug("Title duplicate: '%s' ~ '%s'", title, matched_title)
                    continue

                signature = _title_signature(title)
                if signature is not None:
                    existing_title_signatures.append(signature)
                existing_urls.add(link)

                # Keyword matching — metadata only (LLM is the quality gate)
                keywords, score, matched_state = check_keyword_match(
                    title, summary, config, state_key=query_state
                )
                if not keywords:
                    keywords, score = [], 0

                # Determine final state: keyword match > query state > text inference
                final_state = matched_state or query_state
                if not final_state:
                    final_state = infer_state_from_text(title, summary)

                location = detect_location(title, summary, config, state_key=final_state)

                matched_articles.append({
                    "source": source,
                    "title": title,
                    "url": link,
                    "published_date": pub_date,
                    "summary": summary,
                    "matched_keywords": keywords or [],
                    "keyword_score": score,
                    "location_relevance": location,
                    "state": final_state,
                })

            # Mark progress (sweep mode)
            if per_state and "template" in query_info:
                completed_progress.add(progress_key)
                _save_sweep_progress(completed_progress)

            time.sleep(1)  # Rate limit between queries

        # Final progress save
        if per_state:
            _save_sweep_progress(completed_progress)

        keyword_matches = len(matched_articles)
        logger.info("After dedup: %d new articles (%d URL dupes, %d title dupes, %d skipped-progress)",
                    keyword_matches, skipped_url, skipped_title, skipped_progress)

        # ── Relevance scoring ─────────────────────────────────────
        from ingest import score_relevance_hybrid
        client = get_anthropic_client()
        api_key_set = client is not None
        scored = 0
        auto_approved = 0
        try:
            import local_llm
            local_ready = local_llm.ensure_running()
        except Exception as e:
            logger.debug("Local LLM unavailable: %s", e)
            local_ready = False

        for article in matched_articles:
            if progress_callback is not None:
                progress_callback({
                    "phase": "scoring",
                    "current": scored + 1,
                    "total": len(matched_articles),
                    "state": article.get("state"),
                    "title": article.get("title"),
                })
            rel_score, rel_reason = score_relevance_hybrid(
                article, client, relevance_model, local_ready=local_ready,
            )
            if rel_score is not None:
                article["relevance_score"] = rel_score
                article["relevance_reason"] = rel_reason
                scored += 1
                if not local_ready:
                    time.sleep(0.1)

                # Auto-approve if above threshold
                if auto_threshold and rel_score >= auto_threshold:
                    final_url = _resolve_article_url(article, resolved_url_cache, existing_urls)
                    if not final_url:
                        continue
                    article["url"] = final_url
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
                        "state": article["state"],
                        "location_relevance": article["location_relevance"],
                    }
                    result = db.insert_article(conn, article_data)
                    if result is not None:
                        auto_approved += 1
                        existing_urls.add(final_url)
                        logger.info("Auto-approved: %s (rel=%d)", article["title"][:60], rel_score)
                    continue

                # Auto-reject if below threshold
                if reject_threshold and rel_score <= reject_threshold:
                    logger.debug("Auto-rejected: %s (rel=%d)", article["title"][:60], rel_score)
                    continue
            else:
                article["relevance_score"] = None
                article["relevance_reason"] = None

            # Store pending items with the original URL and resolve lazily later.
            # This keeps the review queue functional without paying the network
            # cost of decoding every Google News redirect during the sweep.
            article["search_id"] = search_id
            article["source_type"] = "websearch"
            db.insert_pending_article(conn, article)

        db.insert_feed_run(conn, "Web Search" + (" (sweep)" if per_state else ""),
                          total_results, keyword_matches, "success")
    finally:
        conn.close()

    # ── Post-sweep analysis ───────────────────────────────────────
    analyzed = 0
    if per_state and not skip_analysis and auto_approved > 0:
        try:
            import analyze
            logger.info("Running analysis on %d new auto-approved articles...", auto_approved)
            if progress_callback is not None:
                progress_callback({
                    "phase": "analyzing",
                    "current": 0,
                    "total": auto_approved,
                })
            result = analyze.analyze_articles(limit=max(auto_approved, 50))
            analyzed = result.get("analyzed", 0)
            if result.get("error"):
                logger.warning("Analysis error: %s", result["error"])
        except Exception as e:
            logger.error("Post-sweep analysis failed: %s", e)

    # Clear progress on successful complete sweep (not single-state)
    if per_state and not state:
        _clear_sweep_progress()

    return {
        "search_id": search_id,
        "query": query or ("(per-state sweep)" if per_state else "(per-state queries)"),
        "total_results": total_results,
        "new_articles": keyword_matches,
        "skipped_url": skipped_url,
        "skipped_title": skipped_title,
        "skipped_progress": skipped_progress if per_state else 0,
        "scored": scored,
        "auto_approved": auto_approved,
        "analyzed": analyzed,
        "api_key_set": api_key_set,
        "days_back": days_back,
        "per_state": per_state,
        "states_searched": len(states_searched) if per_state else 0,
    }
