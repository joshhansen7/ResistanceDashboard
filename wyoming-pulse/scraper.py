"""
Prometheus — Article Scraper
Fetches full article text from URLs using trafilatura.
Falls back gracefully when content can't be retrieved.
"""

import base64
import logging
import re
import time

import db

logger = logging.getLogger("wyoming_pulse.scraper")

# Minimum content length to consider "full" — below this, we try scraping
THIN_CONTENT_THRESHOLD = 200


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


def _try_gnewsdecoder(url, interval=0.5):
    """
    Use the googlenewsdecoder package to resolve newer-format Google News URLs.
    This makes 2 HTTP requests to Google's batchexecute endpoint.
    Returns the real URL or None.
    """
    try:
        from googlenewsdecoder import gnewsdecoder

        result = gnewsdecoder(url, interval=interval)
        if result.get("status"):
            return result["decoded_url"]
        else:
            msg = result.get("message", "")
            if "429" in msg:
                logger.debug("gnewsdecoder: Google rate limit (429)")
            else:
                logger.debug("gnewsdecoder failed: %s", msg)
    except ImportError:
        logger.warning("googlenewsdecoder not installed — run: pip install googlenewsdecoder")
    except Exception as e:
        logger.debug("gnewsdecoder error: %s", e)
    return None


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


def scrape_url(url, timeout=15):
    """
    Fetch and extract article text from a URL.
    Resolves Google News redirect URLs first.
    Returns extracted text or None on failure.
    """
    try:
        import trafilatura

        # Resolve Google News redirects to actual article URL
        resolved_url = resolve_google_news_url(url)

        downloaded = trafilatura.fetch_url(resolved_url)
        if not downloaded:
            logger.debug("No content downloaded from %s", resolved_url)
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


def scrape_thin_articles(conn, limit=None, progress_callback=None):
    """
    Find articles with thin full_text and attempt to scrape full content.
    Returns dict with summary stats.
    """
    where = "analyzed = 0 AND url IS NOT NULL AND url != ''"
    sql = f"""
    SELECT id, url, full_text, title FROM articles
    WHERE {where}
    ORDER BY ingested_date ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql).fetchall()

    # Filter to thin content
    thin = [r for r in rows if not r["full_text"] or len(r["full_text"]) < THIN_CONTENT_THRESHOLD]

    if not thin:
        logger.info("No thin articles to scrape")
        return {"scraped": 0, "failed": 0, "skipped": 0}

    logger.info("Found %d articles with thin content to scrape", len(thin))

    scraped = 0
    failed = 0

    for i, row in enumerate(thin):
        url = row["url"]
        title = row["title"]

        if progress_callback:
            progress_callback(i + 1, len(thin))

        logger.info("Scraping [%d/%d]: %s", i + 1, len(thin), title[:60])

        # Resolve Google News URL first, and persist the real URL
        resolved_url = resolve_google_news_url(url)
        if resolved_url != url:
            try:
                conn.execute("UPDATE articles SET url = ? WHERE id = ?", (resolved_url, row["id"]))
                conn.commit()
                logger.info("  -> Resolved URL: %s", resolved_url[:80])
            except Exception:
                logger.warning("  -> URL already exists in DB, keeping original")
                resolved_url = url

        text = scrape_url(resolved_url)
        if text:
            conn.execute(
                "UPDATE articles SET full_text = ?, content_quality = 'full' WHERE id = ?",
                (text, row["id"]),
            )
            conn.commit()
            scraped += 1
            logger.info("  -> Got %d chars", len(text))
        else:
            conn.execute(
                "UPDATE articles SET content_quality = 'thin' WHERE id = ? AND (content_quality IS NULL OR content_quality != 'full')",
                (row["id"],),
            )
            conn.commit()
            failed += 1
            logger.info("  -> Failed to scrape")

        # Brief pause between requests
        time.sleep(0.5)

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
                conn.execute("UPDATE articles SET url = ? WHERE id = ?", (real_url, row["id"]))
                conn.commit()
                resolved += 1
                logger.info("  -> %s", real_url[:80])
            except Exception as e:
                # UNIQUE constraint: another article already has this URL (cross-query duplicate)
                logger.warning("  -> Duplicate URL, keeping original: %s", e)
                failed += 1
        else:
            failed += 1
            logger.info("  -> Could not resolve")

        time.sleep(0.3)

    return {"resolved": resolved, "failed": failed, "total": len(rows)}
