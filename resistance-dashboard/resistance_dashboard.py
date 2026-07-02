#!/usr/bin/env python3
"""
Prometheus Resistance Dashboard — Main CLI Entry Point
Sentiment tracking for data center development across the United States.

Articles are sourced via Google News search (50-state sweep + nationwide
thematic pass). RSS feeds have been retired.

Usage:
  python resistance_dashboard.py run             Daily pipeline: search sweep → resolve/scrape → analyze → digest (if due)
  python resistance_dashboard.py run --days 2 --dry-run
  python resistance_dashboard.py backfill --from 2025-09-01 --to 2026-01-01 [--state ohio]
  python resistance_dashboard.py analyze         Run sentiment analysis on new articles
  python resistance_dashboard.py digest          Generate digest for current period
  python resistance_dashboard.py digest --from YYYY-MM-DD --to YYYY-MM-DD
  python resistance_dashboard.py status          Show database stats
  python resistance_dashboard.py infill          Resolve Google News URLs (gradual) + scrape thin content
  python resistance_dashboard.py infill --upgrade  Also re-scrape + reanalyze resolved thin articles
  python resistance_dashboard.py infill --drain  Loop until the whole resolve/upgrade backlog is empty
  python resistance_dashboard.py manual          Launch manual input tool
  python resistance_dashboard.py dashboard       Launch the web dashboard
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Ensure project directory is on the path
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

import db
import analyze
import digest
import manual_input
import websearch
from shared import APP_NAME, APP_SLUG, prepare_log_path


def setup_logging():
    """Configure logging with both console and rotating file handler."""
    log_file = prepare_log_path()

    root_logger = logging.getLogger(APP_SLUG)
    root_logger.setLevel(logging.INFO)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    # File handler with rotation (5 MB, keep 3 backups)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )

    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)

    return root_logger


def cmd_analyze(args):
    """Run sentiment analysis on unanalyzed articles."""
    print(f"\n🔬 {APP_NAME} — Analyzing articles...")
    result = analyze.analyze_articles()
    if result.get("error"):
        print(f"\nError: {result['error']}")
        print("Set your API key: export ANTHROPIC_API_KEY='sk-ant-...'")
        return
    print(f"\nResults:")
    print(f"  Analyzed:       {result.get('analyzed', 0)}")
    print(f"  Errors:         {result.get('errors', 0)}")
    if result.get("total_input_tokens"):
        print(f"  Input tokens:   {result['total_input_tokens']:,}")
        print(f"  Output tokens:  {result['total_output_tokens']:,}")


def cmd_digest(args):
    """Generate a digest report."""
    start_date = getattr(args, "from_date", None)
    end_date = getattr(args, "to_date", None)
    print(f"\n📊 {APP_NAME} — Generating digest...")
    result = digest.generate_digest(start_date=start_date, end_date=end_date)
    if not result:
        print("No analyzed articles found for the specified period.")


def cmd_status(args):
    """Show database statistics."""
    db.init_db()
    conn = db.get_connection()
    stats = db.get_status(conn)
    conn.close()

    print(f"\n{APP_NAME} — Status")
    print("=" * 25)
    print(f"Database: {db.DB_PATH}")
    print(f"Total articles: {stats['total_articles']}")
    print(f"  Analyzed: {stats['analyzed']}")
    print(f"  Pending analysis: {stats['pending']}")

    if stats["by_source"]:
        print(f"\nBy source:")
        for source, count in stats["by_source"].items():
            print(f"  {source}: {count}")

    print(f"\nSentiment distribution:")
    label_display = {
        "strongly_positive": "Strongly positive",
        "slightly_positive": "Slightly positive",
        "neutral": "Neutral",
        "slightly_negative": "Slightly negative",
        "strongly_negative": "Strongly negative",
    }
    for label, display in label_display.items():
        count = stats["sentiment_distribution"].get(label, 0)
        if count > 0:
            print(f"  {display}: {count}")

    print(f"\nLast feed check:  {stats['last_feed_check']}")
    print(f"Last analysis:    {stats['last_analysis']}")
    print(f"Last digest:      {stats['last_digest']}")
    print(f"Next digest due:  {stats['next_digest_due']}")
    print(f"\nAPI usage estimate this month: {stats['api_usage_estimate']}")

    infill = stats.get("infill", {})
    if infill:
        print("\nInfill backlog:")
        print(f"  Unresolved Google News URLs:  {infill['unresolved_wrappers']}"
              f"  ({infill['resolve_exhausted']} gave up after {db.RESOLVE_ATTEMPTS_CAP} attempts)")
        print(f"  Ready to upgrade (real URL):  {infill['ready_to_upgrade']}")
        print(f"  Analyzed on thin content:     {infill['thin_analyzed']}"
              f"  ({infill['upgrade_exhausted']} hit the rescrape cap)")


def _run_infill():
    """Resolve a bounded slice of Google News URLs + scrape thin content.

    Shared by the daily pipeline and backfill. Reads pacing/limits from the
    `infill` config block. Returns the combined infill stats dict.
    """
    import scraper
    from shared import load_config
    cfg = load_config() or {}
    infill_cfg = cfg.get("infill", {}) if isinstance(cfg, dict) else {}
    return scraper.infill_articles(
        resolve_limit=infill_cfg.get("resolve_limit_per_run", 250),
        interval=infill_cfg.get("interval_seconds", 2.0),
        backoff=infill_cfg.get("rate_limit_backoff_seconds", 60.0),
        max_rate_limit_hits=infill_cfg.get("max_rate_limit_hits", 3),
        max_resolve_seconds=infill_cfg.get("max_resolve_seconds", 1800),
        workers=infill_cfg.get("scrape_workers", 4),
    )


def _run_upgrade(limit=None, days_back=None, oldest_first=False):
    """Rescrape + re-analyze analyzed thin articles that now carry a real URL.

    Never touches Google (resolution happens in the infill pass), so it drains
    the analyzed-on-a-snippet backlog regardless of rate limits. Reads its
    default batch size from the `infill` config block. Returns the stats dict.
    """
    import scraper
    from shared import load_config
    cfg = load_config() or {}
    infill_cfg = cfg.get("infill", {}) if isinstance(cfg, dict) else {}
    if limit is None:
        limit = infill_cfg.get("upgrade_limit_per_run", 150)
    conn = db.get_connection()
    try:
        return scraper.upgrade_resolved_thin_articles(
            conn,
            days_back=days_back,
            limit=limit,
            oldest_first=oldest_first,
        )
    finally:
        conn.close()


def _maybe_generate_digest(args, interval_days=14):
    """Generate a digest if `interval_days` have elapsed since the last one."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT period_end FROM digests ORDER BY generated_date DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if row:
        last_end = datetime.fromisoformat(row["period_end"])
        days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - last_end).days
        if days_since >= interval_days:
            print(f"  Last digest ended {days_since} days ago. Generating new digest...")
            cmd_digest(args)
        else:
            print(f"  Last digest ended {days_since} days ago. Next due in {interval_days - days_since} days.")
    else:
        print("  No previous digest found. Generating first digest...")
        cmd_digest(args)


def _month_windows(start_date, end_date):
    """Yield (window_start, window_end) date strings for each calendar month.

    Splits a long date range into monthly windows so each Google News query
    stays under the ~100-result cap (preserves historical-backfill coverage).
    """
    current = datetime.strptime(start_date, "%Y-%m-%d").replace(day=1)
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while current <= end:
        window_start = max(current, datetime.strptime(start_date, "%Y-%m-%d"))
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        window_end = min(next_month - timedelta(days=1), end)
        yield window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
        current = next_month


def cmd_run(args):
    """Daily pipeline: search sweep → resolve/scrape → analyze → digest (if due)."""
    days = getattr(args, "days_back", 2)
    dry_run = getattr(args, "dry_run", False)

    print(f"\n🚀 {APP_NAME} — Daily Pipeline Run")
    print("=" * 40)
    if dry_run:
        print("  Mode: DRY RUN (search preview only — no writes/scrape/analyze/digest)")

    # Step 1: Search sweep (all 50 states + nationwide thematic pass)
    print(f"\n--- Step 1: Search sweep (last {days} days) ---")
    sweep = websearch.run_websearch(
        per_state=True, days_back=days, skip_analysis=True, dry_run=dry_run,
    )
    print(f"  States searched: {sweep.get('states_searched', 0)}")
    print(f"  Total results:   {sweep.get('total_results', 0)}")
    print(f"  New articles:    {sweep.get('new_articles', 0)}")
    print(f"  Auto-approved:   {sweep.get('auto_approved', 0)}")

    if dry_run:
        print("\n✅ Pipeline dry-run complete!")
        return

    # Step 2: Infill — resolve Google News URLs + scrape thin content
    print("\n--- Step 2: Resolving URLs + scraping content ---")
    infill = _run_infill()
    print(f"  URLs resolved:    {infill.get('resolve', {}).get('resolved', 0)}")
    print(f"  Articles scraped: {infill.get('scrape', {}).get('scraped', 0)}")

    # Step 3: Analyze (after infill, so it sees freshly scraped full_text)
    print("\n--- Step 3: Analyzing articles ---")
    cmd_analyze(args)

    # Step 4: Upgrade — rescrape + re-analyze thin articles whose URL has been
    # resolved (drains the analyzed-on-a-snippet backlog a slice per day)
    print("\n--- Step 4: Upgrading resolved thin articles ---")
    up = _run_upgrade()
    print(f"  Upgraded:   {up.get('upgraded', 0)} of {up.get('candidates', 0)} candidates")
    print(f"  Reanalyzed: {up.get('reanalyzed', 0)}")

    # Step 5: Digest (if due)
    print("\n--- Step 5: Checking digest schedule ---")
    _maybe_generate_digest(args)

    print("\n✅ Pipeline complete!")


def cmd_dashboard(args):
    """Launch the web dashboard."""
    host = args.host
    port = args.port
    from dashboard.app import start_dashboard
    start_dashboard(host=host, port=port)


def cmd_manual(args):
    """Launch the manual input tool."""
    manual_input.run_manual_input()


def cmd_infill(args):
    """Resolve Google News URLs gradually and scrape thin article content.

    Designed for a daily cron on a persistent host: it chips away at the Google
    News URL backlog at a rate-limit-safe pace, then scrapes any thin articles to
    fill in full text. Optionally also upgrades recent low-confidence articles
    (this re-runs analysis and uses the Anthropic API).
    """
    import scraper
    from shared import load_config

    print(f"\n🧩 {APP_NAME} — Article Infill")
    print("=" * 35)

    config = load_config() or {}
    infill_cfg = config.get("infill", {}) if isinstance(config, dict) else {}

    resolve_limit = args.resolve_limit if args.resolve_limit is not None \
        else infill_cfg.get("resolve_limit_per_run", 250)
    interval = args.interval if args.interval is not None \
        else infill_cfg.get("interval_seconds", 2.0)
    backoff = infill_cfg.get("rate_limit_backoff_seconds", 60.0)
    max_rate_limit_hits = infill_cfg.get("max_rate_limit_hits", 3)
    max_resolve_seconds = infill_cfg.get("max_resolve_seconds", 1800)
    workers = infill_cfg.get("scrape_workers", 4)

    def _one_pass(max_seconds=max_resolve_seconds):
        return scraper.infill_articles(
            resolve_limit=resolve_limit,
            scrape_limit=args.scrape_limit,
            interval=interval,
            backoff=backoff,
            max_rate_limit_hits=max_rate_limit_hits,
            max_resolve_seconds=max_seconds,
            workers=workers,
            do_scrape=not args.no_scrape,
        )

    if getattr(args, "drain", False):
        # One-time catch-up: loop resolve → scrape → upgrade until the backlog
        # is empty. Terminates because definitive resolve/scrape attempts are
        # capped per row (both pools shrink), sustained 429s break out below,
        # and the 100-batch cap backstops everything else. Interrupting is
        # fine: every attempt is committed per row, and whatever is left is
        # picked up by the next drain or daily cron run. The per-batch time
        # budget is cron safety, so drain mode runs without one.
        print("  Drain mode: looping resolve → scrape → upgrade until the backlog is empty.")
        batch = 0
        while batch < 100:
            batch += 1
            print(f"\n--- Drain batch {batch} ---")
            result = _one_pass(None)
            r = result.get("resolve", {})
            up = _run_upgrade(limit=args.upgrade_limit, days_back=args.upgrade_days)
            print(
                f"  Resolved {r.get('resolved', 0)}/{r.get('candidates', 0)} URLs; "
                f"upgraded {up.get('upgraded', 0)}/{up.get('candidates', 0)} articles "
                f"(reanalyzed {up.get('reanalyzed', 0)})"
            )
            if r.get("stopped_early"):
                print("\n⚠️  Sustained Google rate limiting — stopping the drain here.")
                print("   Progress is saved; re-run later or let the daily cron finish the rest.")
                break
            if r.get("candidates", 0) == 0 and up.get("candidates", 0) == 0:
                print("\n✅ Backlog drained!")
                break
        else:
            print("\n⚠️  Hit the 100-batch safety cap — re-run `infill --drain` to continue.")
        print("  Tip: run `python resistance_dashboard.py analyze` to score any remaining unanalyzed articles.")
        return

    print(f"  Resolving up to {resolve_limit} Google News URLs (interval {interval}s)...")
    result = _one_pass()

    r = result.get("resolve", {})
    s = result.get("scrape", {})
    print("\nResolve:")
    print(f"  Candidates:   {r.get('candidates', 0)}")
    print(f"  Resolved:     {r.get('resolved', 0)}")
    print(f"  Unresolved:   {r.get('unresolved', 0)}")
    if r.get("stopped_early"):
        print("  Note: stopped early on sustained rate limiting — resumes next run.")
    if r.get("time_budget_exhausted"):
        print("  Note: resolve time budget spent — resumes next run.")
    print("Scrape:")
    print(f"  Scraped:      {s.get('scraped', 0)}")
    print(f"  Failed:       {s.get('failed', 0)}")

    if getattr(args, "upgrade", False):
        print("\n--- Upgrading resolved thin articles (uses API) ---")
        up = _run_upgrade(limit=args.upgrade_limit, days_back=args.upgrade_days)
        print(f"  Candidates:   {up.get('candidates', 0)}")
        print(f"  Upgraded:     {up.get('upgraded', 0)}")
        print(f"  Reanalyzed:   {up.get('reanalyzed', 0)}")

    print("\n✅ Infill complete!")


def cmd_backfill(args):
    """Historical backfill: date-range search sweep → resolve/scrape → analyze → baseline digest.

    Replaces the old RSS backfill and the separate historical-backfill command.
    A date range is split into monthly windows so each Google News query stays
    under the ~100-result cap. Without --from, falls back to a days-back sweep.
    """
    start_date = getattr(args, "from_date", None)
    end_date = getattr(args, "to_date", None) or \
        datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    state = getattr(args, "state", None)
    days_back = getattr(args, "days_back", 30)
    dry_run = getattr(args, "dry_run", False)
    skip_analysis = getattr(args, "skip_analysis", False)

    print(f"\n📦 {APP_NAME} — Historical Backfill")
    print("=" * 40)
    if start_date:
        print(f"  Date range: {start_date} → {end_date}  (monthly windows)")
    else:
        print(f"  Looking back: {days_back} days")
    print(f"  Scope: {state or 'all 50 states'} + nationwide thematic pass")
    if dry_run:
        print("  Mode: DRY RUN (no DB writes or API calls)")
    print()

    # Build the list of date windows to sweep.
    if start_date:
        windows = list(_month_windows(start_date, end_date))
    else:
        windows = [(None, None)]  # days-back mode: single window

    totals = {"total_results": 0, "new_articles": 0, "auto_approved": 0, "scored": 0}
    for i, (win_start, win_end) in enumerate(windows, start=1):
        label = f"{win_start} → {win_end}" if win_start else f"last {days_back}d"
        print(f"  [{i}/{len(windows)}] sweeping {label} ...")
        result = websearch.run_websearch(
            per_state=True,
            days_back=days_back,
            start_date=win_start,
            end_date=win_end,
            state=state,
            skip_analysis=True,   # analyze once at the end, after infill
            dry_run=dry_run,
        )
        if result.get("error"):
            print(f"    Error: {result['error']}")
            continue
        for k in totals:
            totals[k] += result.get(k, 0) or 0

    print(f"\nSweep totals:")
    print(f"  Total results:   {totals['total_results']}")
    print(f"  New articles:    {totals['new_articles']}")
    print(f"  Auto-approved:   {totals['auto_approved']}")

    if dry_run:
        print("\n  (Dry run — no changes were made)")
        print("\n✅ Backfill dry-run complete!")
        return

    # Resolve URLs + scrape, then analyze (so analysis sees full_text).
    if not skip_analysis:
        print("\n--- Resolving URLs + scraping content ---")
        infill = _run_infill()
        print(f"  URLs resolved:    {infill.get('resolve', {}).get('resolved', 0)}")
        print(f"  Articles scraped: {infill.get('scrape', {}).get('scraped', 0)}")

        print("\n--- Analyzing articles ---")
        ana = analyze.analyze_articles(limit=200)
        if ana.get("error"):
            print(f"  Analysis error: {ana['error']}")
            print("  Articles saved — run 'analyze' later after setting the API key.")
        else:
            print(f"  Analyzed: {ana.get('analyzed', 0)}")

    # Baseline digest covering the backfilled period.
    print("\n--- Generating baseline digest ---")
    digest.generate_digest(
        start_date=start_date or "2026-01-01",
        end_date=end_date,
    )

    print("\n✅ Backfill complete!")


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Prometheus Resistance Dashboard — Data Center Sentiment Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # analyze
    subparsers.add_parser("analyze", help="Run sentiment analysis on new articles")

    # digest
    digest_parser = subparsers.add_parser("digest", help="Generate digest report")
    digest_parser.add_argument(
        "--from", dest="from_date", help="Start date (YYYY-MM-DD)"
    )
    digest_parser.add_argument(
        "--to", dest="to_date", help="End date (YYYY-MM-DD)"
    )

    # status
    subparsers.add_parser("status", help="Show database statistics")

    # run — daily search pipeline
    run_parser = subparsers.add_parser(
        "run",
        help="Daily pipeline: search sweep → resolve/scrape → analyze → digest (if due)",
    )
    run_parser.add_argument(
        "--days", dest="days_back", default=2, type=int,
        help="Days to look back in the sweep (default: 2)",
    )
    run_parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Preview the search sweep only — no DB writes, scrape, analyze, or digest",
    )

    # manual
    subparsers.add_parser("manual", help="Launch manual input tool")

    # dashboard
    dash_parser = subparsers.add_parser("dashboard", help="Launch web dashboard")
    dash_parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    dash_parser.add_argument("--port", default=5000, type=int, help="Port (default: 5000)")

    # infill
    infill_parser = subparsers.add_parser(
        "infill",
        help="Gradually resolve Google News URLs + scrape thin content (cron-friendly)",
    )
    infill_parser.add_argument(
        "--resolve-limit", dest="resolve_limit", type=int, default=None,
        help="Max Google News URLs to resolve this run (default: config or 250)",
    )
    infill_parser.add_argument(
        "--scrape-limit", dest="scrape_limit", type=int, default=None,
        help="Max thin articles to scrape this run (default: all)",
    )
    infill_parser.add_argument(
        "--interval", dest="interval", type=float, default=None,
        help="Seconds to wait between Google News resolutions (default: config or 2.0)",
    )
    infill_parser.add_argument(
        "--no-scrape", dest="no_scrape", action="store_true",
        help="Only resolve URLs; skip the thin-content scrape pass",
    )
    infill_parser.add_argument(
        "--upgrade", dest="upgrade", action="store_true",
        help="Also re-scrape + reanalyze resolved thin articles (uses API)",
    )
    infill_parser.add_argument(
        "--upgrade-days", dest="upgrade_days", type=int, default=None,
        help="Look-back window in days for --upgrade (default: no window)",
    )
    infill_parser.add_argument(
        "--upgrade-limit", dest="upgrade_limit", type=int, default=None,
        help="Max articles to upgrade per batch (default: config or 150)",
    )
    infill_parser.add_argument(
        "--drain", dest="drain", action="store_true",
        help="Loop resolve → scrape → upgrade batches until the backlog is empty "
             "(one-time catch-up; resumable, uses API for re-analysis)",
    )

    # backfill — historical date-range search (subsumes the old historical-backfill)
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Historical date-range search → resolve/scrape → analyze → baseline digest",
    )
    backfill_parser.add_argument(
        "--from", dest="from_date", default=None,
        help="Start date (YYYY-MM-DD). Split into monthly windows. Omit for a days-back sweep.",
    )
    backfill_parser.add_argument(
        "--to", dest="to_date", default=None,
        help="End date (YYYY-MM-DD, default: today)",
    )
    backfill_parser.add_argument(
        "--days", dest="days_back", default=30, type=int,
        help="Days to look back when --from is not given (default: 30)",
    )
    backfill_parser.add_argument(
        "--state", default=None,
        help="Limit to one state (e.g. ohio, 'north dakota')",
    )
    backfill_parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Fetch and match but don't write to DB or call the API",
    )
    backfill_parser.add_argument(
        "--skip-analysis", dest="skip_analysis", action="store_true",
        help="Sweep + store only; skip resolve/scrape/analyze (still writes baseline digest)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Initialize logging and database
    setup_logging()
    db.init_db()

    commands = {
        "analyze": cmd_analyze,
        "digest": cmd_digest,
        "status": cmd_status,
        "run": cmd_run,
        "dashboard": cmd_dashboard,
        "manual": cmd_manual,
        "infill": cmd_infill,
        "backfill": cmd_backfill,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
