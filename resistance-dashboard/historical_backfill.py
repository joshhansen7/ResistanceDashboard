"""
Prometheus — Historical Backfill
Sweeps Google News RSS with date-range queries (after:/before:) to backfill
articles from a historical period. Reuses the existing ingestion and analysis
pipeline — no new dependencies required.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests

import db
import analyze
from ingest import check_keyword_match, detect_location, _score_relevance
from shared import load_config, get_anthropic_client
from utils import clean_html, normalize_for_comparison
from websearch import _extract_source_from_title, _check_title_similarity

logger = logging.getLogger("resistance_dashboard.historical_backfill")

USER_AGENT = "PrometheusDashboard/1.0 (News Research; +https://github.com/prometheus-hyperscale)"

PROGRESS_PATH = Path(__file__).parent / "data" / "backfill_progress.json"

# Articles scoring at or above this relevance threshold are auto-inserted
BACKFILL_AUTO_THRESHOLD = 6


def _load_progress():
    """Load the set of completed (state, query, month) tuples."""
    if PROGRESS_PATH.exists():
        try:
            data = json.loads(PROGRESS_PATH.read_text())
            return set(tuple(item) for item in data.get("completed", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def _save_progress(completed):
    """Persist completed tuples so a run can be resumed."""
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"completed": [list(item) for item in sorted(completed)]}
    PROGRESS_PATH.write_text(json.dumps(data, indent=2))


def _monthly_windows(start_date, end_date):
    """Yield (window_start, window_end) date strings for each calendar month."""
    current = datetime.strptime(start_date, "%Y-%m-%d").replace(day=1)
    end = datetime.strptime(end_date, "%Y-%m-%d")

    while current <= end:
        window_start = max(current, datetime.strptime(start_date, "%Y-%m-%d"))
        # Last day of this month
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        window_end = min(next_month - timedelta(days=1), end)

        yield window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
        current = next_month


def run_historical_backfill(start_date="2025-09-01", end_date=None,
                            state=None, dry_run=False, skip_analysis=False):
    """
    Sweep Google News RSS with after:/before: date operators for each
    state × query × month combination.

    Args:
        start_date:     Earliest date to search (YYYY-MM-DD).
        end_date:       Latest date to search (YYYY-MM-DD, default today).
        state:          Limit to a single state key (e.g. "wyoming").
        dry_run:        Fetch and match but don't insert or call Claude API.
        skip_analysis:  Ingest only; don't run sentiment analysis afterward.

    Returns:
        Summary dict with counts.
    """
    if end_date is None:
        end_date = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")

    config = load_config()
    pipeline = config.get("pipeline", {})
    relevance_model = pipeline.get(
        "relevance_model",
        config.get("anthropic", {}).get("classification_model",
                                        "claude-haiku-4-5-20251001"),
    )

    db.init_db()
    conn = db.get_connection()

    # Load existing titles for dedup
    existing_titles = [
        r["title"] for r in conn.execute("SELECT title FROM articles").fetchall()
    ] + [
        r["title"]
        for r in conn.execute(
            "SELECT title FROM pending_articles WHERE status = 'pending'"
        ).fetchall()
    ]

    # Build query plan: (state_key, query_string) pairs
    queries = []
    states_cfg = config.get("priority_states", {})
    for state_key, state_cfg in states_cfg.items():
        if state and state_key != state:
            continue
        for q in state_cfg.get("web_search_queries", []):
            queries.append((state_key, q))

    if not queries:
        print("  No web_search_queries found in config.")
        conn.close()
        return {"error": "no queries configured"}

    windows = list(_monthly_windows(start_date, end_date))

    total_combos = len(queries) * len(windows)
    completed_progress = _load_progress()

    print(f"  Date range:  {start_date} → {end_date}")
    print(f"  States:      {state or 'all'}")
    print(f"  Queries:     {len(queries)}")
    print(f"  Windows:     {len(windows)} months")
    print(f"  Total jobs:  {total_combos}")
    print(f"  Resuming:    {len(completed_progress)} already done")
    if dry_run:
        print("  Mode:        DRY RUN (no DB writes, no API calls)")
    print()

    client = None if dry_run else get_anthropic_client()

    # Counters
    total_fetched = 0
    total_new = 0
    total_skipped_url = 0
    total_skipped_title = 0
    total_inserted = 0
    total_pending = 0
    jobs_done = len(completed_progress)

    seen_urls = set()

    for state_key, search_query in queries:
        for win_start, win_end in windows:
            month_label = win_start[:7]
            progress_key = (state_key, search_query, month_label)

            if progress_key in completed_progress:
                continue

            # Google News RSS with date range operators
            q_with_dates = f"{search_query} after:{win_start} before:{win_end}"
            encoded_q = quote_plus(q_with_dates)
            rss_url = (
                f"https://news.google.com/rss/search?q={encoded_q}"
                f"&hl=en-US&gl=US&ceid=US:en"
            )

            jobs_done += 1
            print(
                f"  [{jobs_done}/{total_combos}] {state_key} | {month_label} | "
                f"{search_query[:50]}",
                end="",
                flush=True,
            )

            try:
                resp = requests.get(
                    rss_url, headers={"User-Agent": USER_AGENT}, timeout=15
                )
                feed = feedparser.parse(resp.content)
            except Exception as e:
                logger.error("RSS fetch failed: %s", e)
                print(f" — ERROR: {e}")
                continue

            entries = feed.entries
            total_fetched += len(entries)
            batch_new = 0

            for entry in entries:
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

                # URL dedup against DB
                if link:
                    if conn.execute(
                        "SELECT 1 FROM articles WHERE url = ?", (link,)
                    ).fetchone():
                        total_skipped_url += 1
                        continue
                    if conn.execute(
                        "SELECT 1 FROM pending_articles WHERE url = ? AND status = 'pending'",
                        (link,),
                    ).fetchone():
                        total_skipped_url += 1
                        continue

                # Title similarity dedup
                is_dup, _ = _check_title_similarity(title, existing_titles)
                if is_dup:
                    total_skipped_title += 1
                    continue
                existing_titles.append(title)

                # Keyword matching
                keywords, score, matched_state = check_keyword_match(
                    title, summary, config, state_key=state_key
                )
                location = detect_location(
                    title, summary, config,
                    state_key=matched_state or state_key,
                )

                if dry_run:
                    batch_new += 1
                    total_new += 1
                    continue

                # Relevance scoring
                rel_score = None
                rel_reason = None
                if client:
                    rel_score, rel_reason = _score_relevance(
                        client, relevance_model,
                        {"title": title, "summary": summary, "source": source},
                    )
                    time.sleep(0.3)

                article_state = matched_state or state_key

                # Auto-insert high-relevance articles
                if rel_score is not None and rel_score >= BACKFILL_AUTO_THRESHOLD:
                    article_data = {
                        "source": source,
                        "source_type": "websearch",
                        "title": title,
                        "url": link,
                        "published_date": pub_date,
                        "full_text": summary,
                        "summary": summary,
                        "matched_keywords": keywords or [],
                        "keyword_score": score,
                    }
                    result = db.insert_article(conn, article_data)
                    if result is not None:
                        # Set state on the newly inserted row
                        conn.execute(
                            "UPDATE articles SET state = ? WHERE id = ?",
                            (article_state, result),
                        )
                        conn.commit()
                        total_inserted += 1
                        batch_new += 1
                        total_new += 1
                else:
                    # Lower-scoring → pending queue
                    pending_data = {
                        "search_id": f"backfill-{month_label}",
                        "source": source,
                        "title": title,
                        "url": link,
                        "published_date": pub_date,
                        "summary": summary,
                        "matched_keywords": keywords or [],
                        "keyword_score": score,
                        "location_relevance": location,
                        "relevance_score": rel_score,
                        "relevance_reason": rel_reason,
                        "source_type": "websearch",
                        "state": article_state,
                    }
                    db.insert_pending_article(conn, pending_data)
                    total_pending += 1
                    batch_new += 1
                    total_new += 1

            print(f" — {len(entries)} results, {batch_new} new")

            # Save progress after each (state, query, month) combo
            if not dry_run:
                completed_progress.add(progress_key)
                _save_progress(completed_progress)

            time.sleep(1)  # Rate limit between RSS fetches

    # Record feed run
    if not dry_run:
        db.insert_feed_run(
            conn, "Historical Backfill", total_fetched, total_new, "success"
        )

    conn.close()

    # Run sentiment analysis on new unanalyzed articles
    analyzed_count = 0
    if not dry_run and not skip_analysis and total_inserted > 0:
        print(f"\n  Running sentiment analysis on up to {total_inserted} new articles...")
        result = analyze.analyze_articles(limit=max(total_inserted, 50))
        analyzed_count = result.get("analyzed", 0)
        if result.get("error"):
            print(f"  Analysis error: {result['error']}")
            print("  Articles saved — run 'analyze' later after setting API key.")

    summary = {
        "start_date": start_date,
        "end_date": end_date,
        "total_fetched": total_fetched,
        "total_new": total_new,
        "skipped_url": total_skipped_url,
        "skipped_title": total_skipped_title,
        "auto_inserted": total_inserted,
        "sent_to_pending": total_pending,
        "analyzed": analyzed_count,
        "dry_run": dry_run,
    }
    return summary
