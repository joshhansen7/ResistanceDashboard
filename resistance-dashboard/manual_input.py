"""
Prometheus Resistance Dashboard — Manual Input Tool
CLI for manually adding articles, social media items, and legislative items.
Also supports bulk CSV import.
"""

import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import db

logger = logging.getLogger("resistance_dashboard.manual")


def prompt_input(prompt_text, default=None):
    """Get user input with an optional default value."""
    if default:
        display = f"{prompt_text} [{default}]: "
    else:
        display = f"{prompt_text}: "

    value = input(display).strip()
    return value if value else default


def prompt_url_required(prompt_text="URL (required)"):
    """Prompt for a URL, re-prompting until a valid http(s) URL is given. Returns None if user gives up (empty after retry)."""
    for _ in range(3):
        value = input(f"{prompt_text}: ").strip()
        if value and (value.startswith("http://") or value.startswith("https://")):
            return value
        print("  URL is required and must start with http:// or https://")
    return None


def prompt_multiline(prompt_text):
    """Get multi-line input from the user. Empty line to finish."""
    print(f"{prompt_text} (enter a blank line to finish):")
    lines = []
    while True:
        line = input("> ")
        if not line.strip() and lines:
            break
        lines.append(line)
    return "\n".join(lines)


def add_news_article(conn):
    """Add a news article manually (e.g., Cowboy State Daily)."""
    print("\n--- Add News Article ---")
    source = prompt_input("Source name", "Cowboy State Daily")
    title = prompt_input("Article title")
    if not title:
        print("Title is required. Aborting.")
        return

    url = prompt_url_required("URL (required)")
    if not url:
        print("Valid URL is required. Aborting.")
        return
    date = prompt_input("Date (YYYY-MM-DD)", datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d"))
    text = prompt_multiline("Brief summary or paste article text")

    article_data = {
        "source": source,
        "source_type": "news",
        "title": title,
        "url": url,
        "published_date": date,
        "full_text": text,
        "summary": text[:500] if text else "",
        "matched_keywords": ["manual_entry"],
        "keyword_score": 1.0,
    }

    result = db.insert_article(conn, article_data)
    if result:
        print(f"\nSaved! Article will be analyzed on next analysis run.")
    else:
        print(f"\nArticle already exists (duplicate URL).")


def add_social_media(conn):
    """Add a social media post/comment."""
    print("\n--- Add Social Media Item ---")
    platforms = {"1": "Facebook", "2": "X (Twitter)", "3": "Reddit", "4": "Other"}
    print("  1. Facebook")
    print("  2. X (Twitter)")
    print("  3. Reddit")
    print("  4. Other")
    choice = prompt_input("Platform", "1")
    platform = platforms.get(choice, "Other")

    source = prompt_input("Source/Page name (e.g., 'Cowboy State Daily FB')")
    title = prompt_input("Brief title/description")
    if not title:
        print("Title is required. Aborting.")
        return

    url = prompt_url_required("URL (required)")
    if not url:
        print("Valid URL is required. Aborting.")
        return
    date = prompt_input("Date (YYYY-MM-DD)", datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d"))
    text = prompt_multiline("Post/comment text")

    article_data = {
        "source": f"{source} ({platform})" if source else platform,
        "source_type": "social_media",
        "title": title,
        "url": url,
        "published_date": date,
        "full_text": text,
        "summary": text[:500] if text else "",
        "matched_keywords": ["manual_entry"],
        "keyword_score": 1.0,
    }

    result = db.insert_article(conn, article_data)
    if result:
        print(f"\nSaved! Item will be analyzed on next analysis run.")
    else:
        print(f"\nItem already exists (duplicate URL).")


def add_legislative(conn):
    """Add a legislative or regulatory item."""
    print("\n--- Add Legislative/Regulatory Item ---")
    source = prompt_input("Source (e.g., 'Wyoming Legislature', 'PSC')")
    title = prompt_input("Title/Bill number")
    if not title:
        print("Title is required. Aborting.")
        return

    url = prompt_url_required("URL (required)")
    if not url:
        print("Valid URL is required. Aborting.")
        return
    date = prompt_input("Date (YYYY-MM-DD)", datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d"))
    text = prompt_multiline("Description or key provisions")

    article_data = {
        "source": source or "Wyoming Legislature",
        "source_type": "legislative",
        "title": title,
        "url": url,
        "published_date": date,
        "full_text": text,
        "summary": text[:500] if text else "",
        "matched_keywords": ["manual_entry"],
        "keyword_score": 1.0,
    }

    result = db.insert_article(conn, article_data)
    if result:
        print(f"\nSaved! Item will be analyzed on next analysis run.")
    else:
        print(f"\nItem already exists (duplicate URL).")


def bulk_csv_import(conn):
    """Import articles from a CSV file."""
    print("\n--- Bulk CSV Import ---")
    print("Expected columns: source, title, url, date, text, source_type")
    print("  source_type options: news, social_media, legislative, opinion")

    filepath = prompt_input("CSV file path")
    if not filepath:
        print("No file specified. Aborting.")
        return

    path = Path(filepath).expanduser()
    if not path.exists():
        print(f"File not found: {path}")
        return

    imported = 0
    skipped = 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            # Validate columns
            required_cols = {"source", "title"}
            if not required_cols.issubset(set(reader.fieldnames or [])):
                print(f"CSV must have at least these columns: {required_cols}")
                print(f"Found columns: {reader.fieldnames}")
                return

            for row in reader:
                title = row.get("title", "").strip()
                if not title:
                    skipped += 1
                    continue

                row_url = row.get("url", "").strip()
                if not row_url:
                    skipped += 1
                    continue

                article_data = {
                    "source": row.get("source", "CSV Import").strip(),
                    "source_type": row.get("source_type", "news").strip(),
                    "title": title,
                    "url": row_url,
                    "published_date": row.get("date", "").strip() or None,
                    "full_text": row.get("text", "").strip(),
                    "summary": (row.get("text", "").strip() or "")[:500],
                    "matched_keywords": ["csv_import"],
                    "keyword_score": 1.0,
                }

                result = db.insert_article(conn, article_data)
                if result:
                    imported += 1
                else:
                    skipped += 1

    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    print(f"\nImport complete: {imported} imported, {skipped} skipped (duplicates or empty)")


def run_manual_input():
    """Main interactive loop for manual input."""
    db.init_db()
    conn = db.get_connection()

    print("\nPrometheus Resistance Dashboard — Manual Entry")
    print("=" * 30)

    try:
        while True:
            print("\n1. Add a news article (e.g., Cowboy State Daily)")
            print("2. Add a social media item (Facebook, X, Reddit)")
            print("3. Add a legislative item")
            print("4. Bulk import from CSV")
            print("5. Exit")

            choice = prompt_input("\nChoice", "5")

            if choice == "1":
                add_news_article(conn)
                conn.commit()
            elif choice == "2":
                add_social_media(conn)
                conn.commit()
            elif choice == "3":
                add_legislative(conn)
                conn.commit()
            elif choice == "4":
                bulk_csv_import(conn)
                conn.commit()
            elif choice == "5":
                break
            else:
                print("Invalid choice. Try again.")

    except (KeyboardInterrupt, EOFError):
        print("\n\nExiting.")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_manual_input()
