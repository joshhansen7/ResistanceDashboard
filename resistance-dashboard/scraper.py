"""
Prometheus — Article Scraper
Fetches full article text from URLs using trafilatura.
Falls back gracefully when content can't be retrieved.
"""

import base64
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger("resistance_dashboard.scraper")

# Minimum content length to consider "full" — below this, we try scraping
THIN_CONTENT_THRESHOLD = db.FULL_CONTENT_THRESHOLD


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _record_scrape_attempt(
    conn,
    article_id,
    status,
    *,
    resolved_url=None,
    full_text=None,
):
    """Persist scrape attempt metadata and optional upgraded text."""
    sets = [
        "scrape_status = ?",
        "scrape_attempts = COALESCE(scrape_attempts, 0) + 1",
        "last_scrape_at = ?",
    ]
    params = [status, _now_iso()]

    if resolved_url:
        sets.append("resolved_url = ?")
        params.append(resolved_url)
    if full_text is not None:
        sets.append("full_text = ?")
        params.append(full_text)
        sets.append("content_quality = ?")
        params.append(db.classify_content_quality(full_text))

    params.append(article_id)
    conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", params)


def _try_base64_decode(url):
    """
    Fast offline decode for older CBMi-format Google News URLs where the
    real URL is embedded as plaintext in the base64-encoded protobuf.
    Returns the real URL or None.
    """
    match = re.search(r"/(?:rss/)?articles/([^?]+)", url)
    if not match:
        return None

    article_id = match.group(1)
    for pad in range(4):
        try:
            decoded = base64.urlsafe_b64decode(article_id + "=" * pad)
            found = re.findall(rb"https?://[^\x00-\x1f\x7f-\x9f\"<>\\]+", decoded)
            if found:
                real_url = found[0].decode("utf-8", errors="ignore")
                real_url = re.split(r"[^\x20-\x7e]", real_url)[0]
                if len(real_url) > 20 and "." in real_url:
                    return real_url
        except Exception:
            continue
    return None


def _gnewsdecode_with_status(url, interval=0.5):
    """
    Use the googlenewsdecoder package to resolve newer-format Google News URLs.
    This makes 2 HTTP requests to Google's batchexecute endpoint.
    Returns a (decoded_url_or_None, rate_limited) tuple so paced callers can
    back off when Google returns HTTP 429.
    """
    try:
        from googlenewsdecoder import gnewsdecoder

        result = gnewsdecoder(url, interval=interval)
        if result.get("status"):
            return result["decoded_url"], False
        msg = result.get("message", "") or ""
        if "429" in msg or "rate" in msg.lower():
            logger.debug("gnewsdecoder: Google rate limit (429)")
            return None, True
        logger.debug("gnewsdecoder failed: %s", msg)
    except ImportError:
        logger.warning("googlenewsdecoder not installed — run: pip install googlenewsdecoder")
    except Exception as e:
        if "429" in str(e):
            return None, True
        logger.debug("gnewsdecoder error: %s", e)
    return None, False


def _try_gnewsdecoder(url, interval=0.5):
    """Backward-compatible wrapper returning just the decoded URL (or None)."""
    decoded, _ = _gnewsdecode_with_status(url, interval=interval)
    return decoded


def _try_title_search(title, source=None):
    """
    Fallback: search for the article by title using DuckDuckGo lite.
    Returns the first matching URL or None.
    """
    if not title:
        return None
    try:
        import requests as req
        from urllib.parse import quote_plus, unquote

        # Clean title: strip source suffix that Google News appends
        clean_title = title.rsplit(" - ", 1)[0] if " - " in title else title

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = req.get(
            f"https://lite.duckduckgo.com/lite/?q={quote_plus(clean_title)}",
            headers=headers, timeout=10,
        )
        if resp.status_code != 200:
            return None

        # DDG lite encodes result URLs in uddg= parameters
        uddg_links = re.findall(r"uddg=([^&\"]+)", resp.text)
        if uddg_links:
            decoded = [unquote(u) for u in uddg_links]
            # Return first non-aggregator result
            for u in decoded:
                if any(skip in u for skip in ["duckduckgo", "google.com", "bing.com"]):
                    continue
                logger.debug("Title search found: %s", u[:80])
                return u
    except Exception as e:
        logger.debug("Title search failed: %s", e)
    return None


def resolve_google_news_url(url, interval=0.5, title=None, source=None):
    """
    Resolve a Google News redirect URL to the actual article URL.
    Uses a layered strategy:
      1. Fast base64 offline decode (works for older CBMi format with embedded URLs)
      2. googlenewsdecoder package (handles newer AU_yqL format via Google's batchexecute)
      3. DuckDuckGo title search (fallback when Google rate-limits us)
      4. Fallback: return original URL

    Can be called from websearch.py (at ingest time) or scraper.py (at scrape time).
    """
    if "news.google.com" not in url:
        return url

    # Layer 1: fast offline base64 decode
    decoded = _try_base64_decode(url)
    if decoded:
        logger.debug("Resolved via base64: %s -> %s", url[:60], decoded[:80])
        return decoded

    # Layer 2: googlenewsdecoder (2 HTTP requests, ~1-3s)
    decoded = _try_gnewsdecoder(url, interval=interval)
    if decoded:
        logger.debug("Resolved via gnewsdecoder: %s -> %s", url[:60], decoded[:80])
        return decoded

    # Layer 3: title search via DuckDuckGo (fallback for rate-limited scenarios)
    if title:
        decoded = _try_title_search(title, source)
        if decoded:
            logger.debug("Resolved via title search: %s -> %s", url[:60], decoded[:80])
            return decoded

    logger.warning("Could not resolve Google News URL: %s", url[:80])
    return url


def scrape_url(url, timeout=8):
    """
    Fetch and extract article text from a URL.
    Resolves Google News redirect URLs first.
    Returns extracted text or None on failure.
    """
    try:
        import trafilatura
        from trafilatura.settings import use_config

        config = use_config()
        config.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(timeout))
        config.set("DEFAULT", "MAX_REDIRECTS", "1")
        downloaded = trafilatura.fetch_url(url, config=config)
        if not downloaded:
            logger.debug("No content downloaded from %s", url)
            return None

        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )

        if text and len(text) > THIN_CONTENT_THRESHOLD:
            return text
        else:
            logger.debug("Extracted text too short from %s (%d chars)",
                         url, len(text) if text else 0)
            return None

    except ImportError:
        logger.warning("trafilatura not installed — run: pip install trafilatura")
        return None
    except Exception as e:
        logger.debug("Scrape failed for %s: %s", url, e)
        return None


def scrape_article_metadata(url):
    """
    Fetch a URL and extract article metadata + full text.
    Returns a dict with title, full_text, published_date, source, or
    a minimal dict with just source if scraping fails.
    """
    from urllib.parse import urlparse

    # Derive source from domain
    domain = urlparse(url).netloc.lower()
    source = domain.removeprefix("www.")
    # Title-case the domain for display (e.g. "reuters.com" -> "Reuters.Com")
    source_display = source.split(".")[0].title() if source else "Unknown"

    resolved_url = resolve_google_news_url(url)

    try:
        import json as _json
        import trafilatura

        downloaded = trafilatura.fetch_url(resolved_url)
        if not downloaded:
            return {"source": source_display, "url": resolved_url}

        raw = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            with_metadata=True,
            output_format="json",
        )
        if not raw:
            return {"source": source_display, "url": resolved_url}

        metadata = _json.loads(raw)
        result = {
            "title": metadata.get("title") or "",
            "full_text": metadata.get("text") or "",
            "published_date": metadata.get("date") or "",
            "source": metadata.get("sitename") or metadata.get("hostname") or source_display,
            "url": resolved_url,
        }
        return result

    except ImportError:
        logger.warning("trafilatura not installed — run: pip install trafilatura")
        return {"source": source_display, "url": resolved_url}
    except Exception as e:
        logger.debug("Metadata scrape failed for %s: %s", url, e)
        return {"source": source_display, "url": resolved_url}


def _scrape_thin_article(row):
    url = row["url"]
    title = row["title"]
    source = row["source"]

    resolved_url = row["resolved_url"] or url
    if db.is_google_news_wrapper(url):
        if not row["resolved_url"]:
            return {
                "id": row["id"],
                "title": title,
                "url": url,
                "resolved_url": url,
                "text": None,
            }
        resolved_url = resolve_google_news_url(url, title=title, source=source)
        if resolved_url == url:
            return {
                "id": row["id"],
                "title": title,
                "url": url,
                "resolved_url": resolved_url,
                "text": None,
            }

    text = scrape_url(resolved_url)
    return {
        "id": row["id"],
        "title": title,
        "url": url,
        "resolved_url": resolved_url,
        "text": text,
    }


def scrape_thin_articles(conn, limit=None, progress_callback=None, article_ids=None, workers=8):
    """
    Find articles with thin full_text and attempt to scrape full content.
    Returns dict with summary stats.
    """
    where = "analyzed = 0 AND url IS NOT NULL AND url != '' AND COALESCE(scrape_status, 'pending') = 'pending'"
    params = []
    if article_ids:
        placeholders = ",".join("?" * len(article_ids))
        where += f" AND id IN ({placeholders})"
        params.extend(article_ids)
    sql = f"""
    SELECT id, url, resolved_url, full_text, title, content_quality, source FROM articles
    WHERE {where}
    ORDER BY ingested_date ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()

    # Filter to thin content
    thin = [
        dict(r)
        for r in rows
        if (r["content_quality"] or db.classify_content_quality(r["full_text"])) == "thin"
    ]

    if not thin:
        logger.info("No thin articles to scrape")
        return {"scraped": 0, "failed": 0, "skipped": 0}

    logger.info("Found %d articles with thin content to scrape", len(thin))

    scraped = 0
    failed = 0
    if progress_callback:
        progress_callback(0, len(thin))

    workers = max(1, int(workers or 1))
    logger.info("Scraping thin content with %d workers", workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_scrape_thin_article, row) for row in thin]
        for i, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            url = result["url"]
            resolved_url = result["resolved_url"]
            text = result["text"]
            title = result["title"]

            if progress_callback:
                progress_callback(i, len(thin))

            logger.info("Scraped [%d/%d]: %s", i, len(thin), title[:60])
            if resolved_url != url:
                logger.info("  -> Resolved URL: %s", resolved_url[:80])

            if text:
                _record_scrape_attempt(
                    conn,
                    result["id"],
                    "scraped",
                    resolved_url=resolved_url if resolved_url != url else None,
                    full_text=text,
                )
                scraped += 1
                logger.info("  -> Got %d chars", len(text))
            else:
                _record_scrape_attempt(
                    conn,
                    result["id"],
                    "resolved_only" if resolved_url != url else "failed",
                    resolved_url=resolved_url if resolved_url != url else None,
                )
                failed += 1
                logger.info("  -> Failed to scrape")

            conn.commit()

    return {
        "scraped": scraped,
        "failed": failed,
        "skipped": len(rows) - len(thin),
    }


def resolve_google_news_urls_batch(conn, limit=None, progress_callback=None):
    """
    Resolve all Google News wrapper URLs in the articles table to real URLs.
    Useful for backfilling existing articles that were stored with Google News URLs.
    Returns dict with summary stats.
    """
    sql = """
    SELECT id, url, title FROM articles
    WHERE url LIKE '%news.google.com%'
    ORDER BY ingested_date ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql).fetchall()

    if not rows:
        logger.info("No Google News URLs to resolve")
        return {"resolved": 0, "failed": 0, "total": 0}

    logger.info("Resolving %d Google News URLs", len(rows))

    resolved = 0
    failed = 0

    for i, row in enumerate(rows):
        if progress_callback:
            progress_callback(i + 1, len(rows))

        url = row["url"]
        title = row["title"]
        logger.info("Resolving [%d/%d]: %s", i + 1, len(rows), title[:60])

        real_url = resolve_google_news_url(url, interval=0.5)
        if real_url != url:
            try:
                conn.execute(
                    "UPDATE articles SET resolved_url = ?, scrape_status = 'resolved_only' WHERE id = ?",
                    (real_url, row["id"]),
                )
                conn.commit()
                resolved += 1
                logger.info("  -> %s", real_url[:80])
            except Exception as e:
                logger.warning("  -> Failed to store resolved URL: %s", e)
                failed += 1
        else:
            failed += 1
            logger.info("  -> Could not resolve")

        time.sleep(0.3)

    return {"resolved": resolved, "failed": failed, "total": len(rows)}


def upgrade_recent_low_confidence_articles(
    conn,
    days_back=30,
    limit=250,
    progress_callback=None,
    oldest_first=False,
):
    """
    Reprocess recent analyzed low-confidence rows and only overwrite when scrape quality
    materially improves the article text.
    """
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_back)).isoformat()
    order_direction = "ASC" if oldest_first else "DESC"
    sql = f"""
    SELECT id, title, source, url, resolved_url, full_text, summary, content_quality
    FROM articles
    WHERE analyzed = 1
      AND {db.low_confidence_predicate('articles')}
      AND COALESCE(published_date, analyzed_date, ingested_date) >= ?
    ORDER BY COALESCE(published_date, analyzed_date, ingested_date) {order_direction}
    LIMIT ?
    """
    rows = conn.execute(sql, db.low_confidence_params() + [cutoff, int(limit)]).fetchall()

    if not rows:
        logger.info("No recent low-confidence articles found for upgrade")
        return {
            "candidates": 0,
            "attempted": 0,
            "resolved": 0,
            "upgraded": 0,
            "failed": 0,
            "not_improved": 0,
            "reanalyzed": 0,
        }

    resolved = 0
    upgraded = 0
    failed = 0
    not_improved = 0
    upgraded_ids = []

    if progress_callback:
        progress_callback({"phase": "rescraping", "current": 0, "total": len(rows)})

    for idx, row in enumerate(rows):
        url = row["url"]
        resolved_url = row["resolved_url"] or url
        if db.is_google_news_wrapper(url):
            candidate_url = resolve_google_news_url(
                url,
                interval=0.5,
                title=row["title"],
                source=row["source"],
            )
            if candidate_url != url:
                resolved_url = candidate_url
                resolved += 1

        text = scrape_url(resolved_url)
        if not text:
            _record_scrape_attempt(
                conn,
                row["id"],
                "resolved_only" if resolved_url != url else "failed",
                resolved_url=resolved_url if resolved_url != url else None,
            )
            failed += 1
        else:
            old_text = row["full_text"] or row["summary"] or ""
            if db.scrape_upgrade_is_material(old_text, text):
                _record_scrape_attempt(
                    conn,
                    row["id"],
                    "upgraded",
                    resolved_url=resolved_url if resolved_url != url else None,
                    full_text=text,
                )
                upgraded += 1
                upgraded_ids.append(row["id"])
                logger.info("Upgraded article %d with %d chars", row["id"], len(text))
            else:
                _record_scrape_attempt(
                    conn,
                    row["id"],
                    "not_improved",
                    resolved_url=resolved_url if resolved_url != url else None,
                )
                not_improved += 1

        conn.commit()
        if progress_callback:
            progress_callback({"phase": "rescraping", "current": idx + 1, "total": len(rows)})
        time.sleep(0.5)

    reanalyzed = 0
    if upgraded_ids:
        import analyze

        if progress_callback:
            progress_callback({"phase": "reanalyzing", "current": 0, "total": len(upgraded_ids)})
        result = analyze.analyze_articles(
            article_ids=upgraded_ids,
            include_analyzed=True,
            skip_scrape=True,
            progress_callback=lambda payload: progress_callback({
                "phase": "reanalyzing",
                "current": payload.get("current", 0),
                "total": payload.get("total", len(upgraded_ids)),
            }) if progress_callback and payload.get("phase") == "analyzing" else None,
        )
        reanalyzed = result.get("analyzed", 0)

    return {
        "candidates": len(rows),
        "attempted": len(rows),
        "resolved": resolved,
        "upgraded": upgraded,
        "failed": failed,
        "not_improved": not_improved,
        "reanalyzed": reanalyzed,
    }


def gradual_resolve_google_news(
    conn,
    limit=50,
    interval=2.0,
    backoff=60.0,
    max_rate_limit_hits=3,
    progress_callback=None,
):
    """
    Resolve pending Google News wrapper URLs to their real article URLs, one at a
    time and *paced* to avoid Google's rate limiter (HTTP 429).

    Designed to run unattended once per day on a persistent host: it chews through
    a bounded slice of the backlog each run, backs off exponentially when Google
    starts rate-limiting, and stops early after `max_rate_limit_hits` sustained
    429s so the remaining URLs are simply picked up on the next daily run.

    Only `resolved_url` is written here (never `scrape_status`), so the article
    stays eligible for the scrape pass that follows.
    """
    sql = """
    SELECT id, url, title, source FROM articles
    WHERE url LIKE ?
      AND (resolved_url IS NULL OR resolved_url = '' OR resolved_url = url)
    ORDER BY COALESCE(published_date, ingested_date) DESC
    """
    params = [db.LOW_CONFIDENCE_URL_PATTERN]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()

    stats = {
        "candidates": len(rows),
        "resolved": 0,
        "unresolved": 0,
        "rate_limited": False,
        "stopped_early": False,
    }
    if not rows:
        logger.info("No pending Google News URLs to resolve")
        return stats

    logger.info(
        "Gradually resolving up to %d Google News URLs (interval=%.1fs, backoff=%.0fs)",
        len(rows), interval, backoff,
    )

    consecutive_rate_limits = 0
    current_backoff = backoff
    for i, row in enumerate(rows):
        if progress_callback:
            progress_callback({"phase": "resolving", "current": i + 1, "total": len(rows)})

        url = row["url"]

        # Layer 1: free offline base64 decode (no network, never rate-limited)
        decoded = _try_base64_decode(url)
        rate_limited = False
        if not decoded:
            decoded, rate_limited = _gnewsdecode_with_status(url, interval=0.5)
        if not decoded and row["title"]:
            decoded = _try_title_search(row["title"], row["source"])

        if decoded and decoded != url:
            conn.execute(
                "UPDATE articles SET resolved_url = ? WHERE id = ?",
                (decoded, row["id"]),
            )
            conn.commit()
            stats["resolved"] += 1
            consecutive_rate_limits = 0
            current_backoff = backoff
            logger.info("Resolved [%d/%d]: %s", i + 1, len(rows), decoded[:80])
        else:
            stats["unresolved"] += 1
            if rate_limited:
                stats["rate_limited"] = True
                consecutive_rate_limits += 1
                logger.warning(
                    "Google rate limit (%d/%d consecutive) — backing off %.0fs",
                    consecutive_rate_limits, max_rate_limit_hits, current_backoff,
                )
                if consecutive_rate_limits >= max_rate_limit_hits:
                    logger.warning(
                        "Sustained rate limiting — stopping; remaining URLs resume next run"
                    )
                    stats["stopped_early"] = True
                    break
                time.sleep(current_backoff)
                current_backoff = min(current_backoff * 2, 600)
                continue

        time.sleep(interval)

    logger.info(
        "Gradual resolve done: %d resolved, %d unresolved of %d candidates%s",
        stats["resolved"], stats["unresolved"], stats["candidates"],
        " (stopped early on rate limit)" if stats["stopped_early"] else "",
    )
    return stats


def infill_articles(
    db_path=None,
    resolve_limit=50,
    scrape_limit=None,
    interval=2.0,
    backoff=60.0,
    max_rate_limit_hits=3,
    workers=4,
    do_scrape=True,
    progress_callback=None,
):
    """
    Daily 'infill' pass for a persistent host. Two phases:

      1. Gradually resolve a bounded slice of the Google News URL backlog to real
         URLs (paced + 429 backoff). Chips away at the backlog a little each day.
      2. Scrape thin-content articles to fill in full_text (uses freshly resolved
         URLs from phase 1).

    Safe to run unattended once per day via cron. Returns combined stats.
    """
    conn = db.get_connection(db_path)
    try:
        resolve_stats = gradual_resolve_google_news(
            conn,
            limit=resolve_limit,
            interval=interval,
            backoff=backoff,
            max_rate_limit_hits=max_rate_limit_hits,
            progress_callback=progress_callback,
        )

        scrape_stats = {"scraped": 0, "failed": 0, "skipped": 0}
        if do_scrape:
            scrape_stats = scrape_thin_articles(
                conn,
                limit=scrape_limit,
                workers=workers,
                progress_callback=(
                    lambda current, total: progress_callback(
                        {"phase": "scraping", "current": current, "total": total}
                    )
                ) if progress_callback else None,
            )
    finally:
        conn.close()

    return {"resolve": resolve_stats, "scrape": scrape_stats}
