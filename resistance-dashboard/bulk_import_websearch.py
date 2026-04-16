"""
Prometheus Resistance Dashboard — Seed Data Loader
Loads pre-analyzed articles from data/fixtures/seed_articles.json into
the database so the dashboard has data to visualize immediately.
"""

import json
from pathlib import Path

import db

FIXTURE_PATH = Path(__file__).parent / "data" / "fixtures" / "seed_articles.json"


def load_fixture_articles():
    """Load articles from the JSON fixture file."""
    with open(FIXTURE_PATH, "r") as f:
        return json.load(f)


def run_websearch_import():
    """Import all seed articles into the database with pre-populated analysis.
    Returns dict with summary statistics.
    """
    articles = load_fixture_articles()

    db.init_db()
    conn = db.get_connection()

    inserted = 0
    skipped = 0

    for article in articles:
        article_data = {
            "source": article["source"],
            "source_type": "news",
            "title": article["title"],
            "url": article["url"],
            "published_date": article["published_date"],
            "full_text": article["summary"],
            "summary": article["summary"],
            "matched_keywords": ["web_search_import"],
            "keyword_score": 1.0,
        }

        row_id = db.insert_article(conn, article_data)

        if row_id:
            analysis = {
                "sentiment_score": article["sentiment_score"],
                "sentiment_label": article["sentiment_label"],
                "voice_type": article["voice_type"],
                "location_relevance": article["location_relevance"],
                "topic_tags": article["topic_tags"],
                "entities_mentioned": article["entities_mentioned"],
                "key_claims": article["key_claims"],
            }
            db.update_article_analysis(conn, row_id, analysis)
            inserted += 1
        else:
            skipped += 1

    db.insert_feed_run(conn, "Seed Data Import", len(articles), inserted, "success")
    conn.close()

    return {
        "total_articles": len(articles),
        "inserted": inserted,
        "skipped": skipped,
        "pre_analyzed": True,
    }


def run_import():
    """CLI entry point — prints results to stdout."""
    result = run_websearch_import()

    print(f"\nSeed Data Import Complete")
    print(f"{'='*40}")
    print(f"  Total articles:  {result['total_articles']}")
    print(f"  Inserted:        {result['inserted']}")
    print(f"  Skipped (dupes): {result['skipped']}")
    print(f"  All pre-analyzed: Yes")

    articles = load_fixture_articles()
    loc_counts = {}
    for a in articles:
        loc = a["location_relevance"]
        loc_counts[loc] = loc_counts.get(loc, 0) + 1
    print(f"\n  By location:")
    for loc, count in sorted(loc_counts.items()):
        print(f"    {loc:12s}: {count}")

    sent_counts = {}
    for a in articles:
        lbl = a["sentiment_label"]
        sent_counts[lbl] = sent_counts.get(lbl, 0) + 1
    print(f"\n  By sentiment:")
    for lbl, count in sorted(sent_counts.items()):
        print(f"    {lbl:20s}: {count}")


if __name__ == "__main__":
    run_import()
