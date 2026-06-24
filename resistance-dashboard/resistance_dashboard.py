#!/usr/bin/env python3
"""
Prometheus Resistance Dashboard — Main CLI Entry Point
Sentiment tracking for data center development across the United States.

Usage:
  python resistance_dashboard.py ingest          Fetch RSS feeds and filter articles
  python resistance_dashboard.py analyze         Run sentiment analysis on new articles
  python resistance_dashboard.py digest          Generate digest for current period
  python resistance_dashboard.py digest --from YYYY-MM-DD --to YYYY-MM-DD
  python resistance_dashboard.py status          Show database stats
  python resistance_dashboard.py run             Full pipeline: ingest → analyze → digest (if due)
  python resistance_dashboard.py manual          Launch manual input tool
  python resistance_dashboard.py infill          Resolve Google News URLs (gradual) + scrape thin content
  python resistance_dashboard.py infill --upgrade  Also re-scrape + reanalyze recent low-confidence articles
  python resistance_dashboard.py backfill        Ingest + analyze all, generate baseline digest
  python resistance_dashboard.py sweep           Per-state sweep: search all 50 states systematically
  python resistance_dashboard.py sweep --days 30 --state ohio
  python resistance_dashboard.py sweep --start 2025-06-01 --end 2025-12-31
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
import ingest
import analyze
import digest
import manual_input
import websearch
import historical_backfill
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


def cmd_ingest(args):
    """Fetch RSS feeds and filter articles."""
    print(f"\n📡 {APP_NAME} — Ingesting feeds...")
    result = ingest.ingest_feeds()
    print(f"\nResults:")
    print(f"  Feeds checked:    {result['feeds_checked']}")
    print(f"  Total entries:    {result['total_entries']}")
    print(f"  Keyword matches:  {result['keyword_matches']}")
    print(f"  New articles:     {result['new_articles']}")


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


def cmd_run(args):
    """Full pipeline: ingest → analyze → digest (if due)."""
    print(f"\n🚀 {APP_NAME} — Full Pipeline Run")
    print("=" * 40)

    # Step 1: Ingest
    print("\n--- Step 1: Ingesting feeds ---")
    cmd_ingest(args)

    # Step 2: Analyze
    print("\n--- Step 2: Analyzing articles ---")
    cmd_analyze(args)

    # Step 3: Digest (check if one is due)
    print("\n--- Step 3: Checking digest schedule ---")
    conn = db.get_connection()
    row = conn.execute(
        "SELECT period_end FROM digests ORDER BY generated_date DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if row:
        last_end = datetime.fromisoformat(row["period_end"])
        days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - last_end).days
        if days_since >= 14:
            print(f"  Last digest ended {days_since} days ago. Generating new digest...")
            cmd_digest(args)
        else:
            print(f"  Last digest ended {days_since} days ago. Next due in {14 - days_since} days.")
    else:
        print("  No previous digest found. Generating first digest...")
        cmd_digest(args)

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
        else infill_cfg.get("resolve_limit_per_run", 50)
    interval = args.interval if args.interval is not None \
        else infill_cfg.get("interval_seconds", 2.0)
    backoff = infill_cfg.get("rate_limit_backoff_seconds", 60.0)
    max_rate_limit_hits = infill_cfg.get("max_rate_limit_hits", 3)
    workers = infill_cfg.get("scrape_workers", 4)

    print(f"  Resolving up to {resolve_limit} Google News URLs (interval {interval}s)...")
    result = scraper.infill_articles(
        resolve_limit=resolve_limit,
        scrape_limit=args.scrape_limit,
        interval=interval,
        backoff=backoff,
        max_rate_limit_hits=max_rate_limit_hits,
        workers=workers,
        do_scrape=not args.no_scrape,
    )

    r = result.get("resolve", {})
    s = result.get("scrape", {})
    print("\nResolve:")
    print(f"  Candidates:   {r.get('candidates', 0)}")
    print(f"  Resolved:     {r.get('resolved', 0)}")
    print(f"  Unresolved:   {r.get('unresolved', 0)}")
    if r.get("stopped_early"):
        print("  Note: stopped early on sustained rate limiting — resumes next run.")
    print("Scrape:")
    print(f"  Scraped:      {s.get('scraped', 0)}")
    print(f"  Failed:       {s.get('failed', 0)}")

    if getattr(args, "upgrade", False):
        print("\n--- Upgrading recent low-confidence articles (uses API) ---")
        conn = db.get_connection()
        try:
            up = scraper.upgrade_recent_low_confidence_articles(
                conn,
                days_back=args.upgrade_days,
                limit=args.upgrade_limit,
            )
        finally:
            conn.close()
        print(f"  Candidates:   {up.get('candidates', 0)}")
        print(f"  Upgraded:     {up.get('upgraded', 0)}")
        print(f"  Reanalyzed:   {up.get('reanalyzed', 0)}")

    print("\n✅ Infill complete!")


def cmd_backfill(args):
    """Backfill: ingest + analyze all + generate baseline digest."""
    print(f"\n📦 {APP_NAME} — Backfill Mode")
    print("=" * 35)
    print("This will ingest all available RSS content, analyze everything,")
    print("and generate a baseline digest.\n")

    # Ingest
    print("--- Ingesting all feeds ---")
    cmd_ingest(args)

    # Analyze all (higher limit)
    print("\n--- Analyzing all articles ---")
    result = analyze.analyze_articles(limit=200)
    if result.get("error"):
        print(f"\nError: {result['error']}")
        print("Set your API key: export ANTHROPIC_API_KEY='sk-ant-...'")
        print("\nArticles have been ingested and saved. Run 'analyze' after setting the key.")
        return
    print(f"  Analyzed: {result.get('analyzed', 0)}")

    # Generate baseline digest covering Jan 1, 2026 to today
    print("\n--- Generating baseline digest ---")
    today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    digest.generate_digest(start_date="2026-01-01", end_date=today)

    print("\n✅ Backfill complete!")


def cmd_historical_backfill(args):
    """Historical backfill: sweep Google News with date-range queries."""
    print(f"\n📜 {APP_NAME} — Historical Backfill")
    print("=" * 40)

    result = historical_backfill.run_historical_backfill(
        start_date=args.from_date,
        end_date=args.to_date,
        state=args.state,
        dry_run=args.dry_run,
        skip_analysis=args.skip_analysis,
    )

    if result.get("error"):
        print(f"\nError: {result['error']}")
        return

    print(f"\nResults:")
    print(f"  Total fetched:     {result['total_fetched']}")
    print(f"  New articles:      {result['total_new']}")
    print(f"  Skipped (URL):     {result['skipped_url']}")
    print(f"  Skipped (title):   {result['skipped_title']}")
    print(f"  Auto-inserted:     {result['auto_inserted']}")
    print(f"  Sent to pending:   {result['sent_to_pending']}")
    if result.get("analyzed"):
        print(f"  Analyzed:          {result['analyzed']}")
    if result["dry_run"]:
        print("\n  (Dry run — no changes were made)")

    print("\n✅ Historical backfill complete!")


def cmd_sweep(args):
    """Per-state sweep: search all 50 states with template queries."""
    print(f"\n🔍 {APP_NAME} — Per-State Sweep")
    print("=" * 40)

    days_back = getattr(args, "days_back", 7)
    start_date = getattr(args, "start_date", None)
    end_date = getattr(args, "end_date", None)
    state_filter = getattr(args, "state", None)
    dry_run = getattr(args, "dry_run", False)
    skip_analysis = getattr(args, "skip_analysis", False)

    if start_date:
        print(f"  Date range: {start_date} to {end_date or 'now'}")
    else:
        print(f"  Looking back: {days_back} days")
    if state_filter:
        print(f"  State: {state_filter}")
    else:
        print(f"  Scope: all 50 states")
    if dry_run:
        print(f"  Mode: DRY RUN (no DB writes or API calls)")
    print()

    result = websearch.run_websearch(
        per_state=True,
        days_back=days_back,
        start_date=start_date,
        end_date=end_date,
        state=state_filter,
        skip_analysis=skip_analysis,
    )

    if result.get("error"):
        print(f"\nError: {result['error']}")
        return

    print(f"\nResults:")
    print(f"  States searched:   {result.get('states_searched', 0)}")
    print(f"  Total results:     {result['total_results']}")
    print(f"  New articles:      {result['new_articles']}")
    print(f"  Skipped (URL):     {result['skipped_url']}")
    print(f"  Skipped (title):   {result['skipped_title']}")
    if result.get('skipped_progress'):
        print(f"  Skipped (resumed): {result['skipped_progress']}")
    print(f"  Scored:            {result['scored']}")
    print(f"  Auto-approved:     {result['auto_approved']}")
    if result.get('analyzed'):
        print(f"  Analyzed:          {result['analyzed']}")

    print("\n✅ Sweep complete!")


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Prometheus Resistance Dashboard — Data Center Sentiment Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ingest
    subparsers.add_parser("ingest", help="Fetch RSS feeds and filter articles")

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

    # run
    subparsers.add_parser("run", help="Full pipeline: ingest → analyze → digest")

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
        help="Max Google News URLs to resolve this run (default: config or 50)",
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
        help="Also re-scrape + reanalyze recent low-confidence articles (uses API)",
    )
    infill_parser.add_argument(
        "--upgrade-days", dest="upgrade_days", type=int, default=30,
        help="Look-back window in days for --upgrade (default: 30)",
    )
    infill_parser.add_argument(
        "--upgrade-limit", dest="upgrade_limit", type=int, default=100,
        help="Max articles to upgrade with --upgrade (default: 100)",
    )

    # backfill
    subparsers.add_parser("backfill", help="Ingest + analyze all + baseline digest")

    # sweep
    sweep_parser = subparsers.add_parser(
        "sweep",
        help="Per-state sweep: search all 50 states with template queries",
    )
    sweep_parser.add_argument(
        "--days", dest="days_back", default=7, type=int,
        help="Days to look back (default: 7)",
    )
    sweep_parser.add_argument(
        "--start", dest="start_date", default=None,
        help="Start date (YYYY-MM-DD, overrides --days)",
    )
    sweep_parser.add_argument(
        "--end", dest="end_date", default=None,
        help="End date (YYYY-MM-DD, used with --start)",
    )
    sweep_parser.add_argument(
        "--state", default=None,
        help="Limit to one state (e.g. ohio, 'north dakota')",
    )
    sweep_parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and match but don't write to DB or call API",
    )
    sweep_parser.add_argument(
        "--skip-analysis", action="store_true",
        help="Don't run sentiment analysis after inserting articles",
    )

    # historical-backfill
    hb_parser = subparsers.add_parser(
        "historical-backfill",
        help="Backfill articles from Google News over a historical date range",
    )
    hb_parser.add_argument(
        "--from", dest="from_date", default="2025-09-01",
        help="Start date (YYYY-MM-DD, default: 2025-09-01)",
    )
    hb_parser.add_argument(
        "--to", dest="to_date", default=None,
        help="End date (YYYY-MM-DD, default: today)",
    )
    hb_parser.add_argument(
        "--state", default=None,
        help="Limit to one state (e.g. wyoming, texas, michigan)",
    )
    hb_parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and match but don't write to DB or call API",
    )
    hb_parser.add_argument(
        "--skip-analysis", action="store_true",
        help="Ingest only; skip sentiment analysis",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Initialize logging and database
    setup_logging()
    db.init_db()

    commands = {
        "ingest": cmd_ingest,
        "analyze": cmd_analyze,
        "digest": cmd_digest,
        "status": cmd_status,
        "run": cmd_run,
        "dashboard": cmd_dashboard,
        "manual": cmd_manual,
        "infill": cmd_infill,
        "backfill": cmd_backfill,
        "sweep": cmd_sweep,
        "historical-backfill": cmd_historical_backfill,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
