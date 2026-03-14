#!/usr/bin/env python3
"""
Wyoming Pulse — Main CLI Entry Point
Sentiment tracking for data center development in Wyoming.

Usage:
  python wyoming_pulse.py ingest          Fetch RSS feeds and filter articles
  python wyoming_pulse.py analyze         Run sentiment analysis on new articles
  python wyoming_pulse.py digest          Generate digest for current period
  python wyoming_pulse.py digest --from YYYY-MM-DD --to YYYY-MM-DD
  python wyoming_pulse.py status          Show database stats
  python wyoming_pulse.py run             Full pipeline: ingest → analyze → digest (if due)
  python wyoming_pulse.py manual          Launch manual input tool
  python wyoming_pulse.py backfill        Ingest + analyze all, generate baseline digest
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
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


def setup_logging():
    """Configure logging with both console and rotating file handler."""
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "wyoming_pulse.log"

    root_logger = logging.getLogger("wyoming_pulse")
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
    print("\n📡 Wyoming Pulse — Ingesting feeds...")
    result = ingest.ingest_feeds()
    print(f"\nResults:")
    print(f"  Feeds checked:    {result['feeds_checked']}")
    print(f"  Total entries:    {result['total_entries']}")
    print(f"  Keyword matches:  {result['keyword_matches']}")
    print(f"  New articles:     {result['new_articles']}")


def cmd_analyze(args):
    """Run sentiment analysis on unanalyzed articles."""
    print("\n🔬 Wyoming Pulse — Analyzing articles...")
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
    print("\n📊 Wyoming Pulse — Generating digest...")
    result = digest.generate_digest(start_date=start_date, end_date=end_date)
    if not result:
        print("No analyzed articles found for the specified period.")


def cmd_status(args):
    """Show database statistics."""
    db.init_db()
    conn = db.get_connection()
    stats = db.get_status(conn)
    conn.close()

    print("\nWyoming Pulse — Status")
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
    print("\n🚀 Wyoming Pulse — Full Pipeline Run")
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
        days_since = (datetime.utcnow() - last_end).days
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


def cmd_backfill(args):
    """Backfill: ingest + analyze all + generate baseline digest."""
    print("\n📦 Wyoming Pulse — Backfill Mode")
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
    today = datetime.utcnow().strftime("%Y-%m-%d")
    digest.generate_digest(start_date="2026-01-01", end_date=today)

    print("\n✅ Backfill complete!")


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Wyoming Pulse — Data Center Sentiment Tracker",
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

    # backfill
    subparsers.add_parser("backfill", help="Ingest + analyze all + baseline digest")

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
        "backfill": cmd_backfill,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
