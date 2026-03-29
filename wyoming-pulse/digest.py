"""
Wyoming Pulse — Digest Generation
Generates biweekly intelligence reports from analyzed articles.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import db
from shared import load_config, get_api_key

logger = logging.getLogger("wyoming_pulse.digest")

OUTPUT_DIR = Path(__file__).parent / "output" / "digests"

SYNTHESIS_PROMPT = """You are an intelligence analyst producing a biweekly sentiment report about data center development in Wyoming for Prometheus Hyperscale leadership.

Given the following classified articles and aggregate data from the past {days} days, produce a concise intelligence digest. Be analytical and objective. Distinguish between signals and noise. Flag anything that represents a meaningful shift from baseline.

The digest should include:
1. SENTIMENT SNAPSHOT — Overall score, elite vs public, trend direction
2. BY LOCATION — Brief notes on Evanston, Casper, and Cheyenne areas
3. TOP THEMES — The 2-3 most significant narratives or developments
4. HYPERSCALER TRACKER — Which companies were mentioned and in what context
5. LEGISLATIVE/REGULATORY UPDATE — Any policy developments
6. WATCH LIST — Emerging issues that bear monitoring
7. KEY ARTICLES — The 3-5 most significant pieces with source and date

Keep it under 800 words. Write for busy executives who want the bottom line."""


def compute_stats(articles):
    """Compute aggregate statistics from a list of analyzed articles."""
    if not articles:
        return {}

    scores = [a["sentiment_score"] for a in articles if a["sentiment_score"] is not None]
    elite_scores = [
        a["sentiment_score"] for a in articles
        if a["voice_type"] == "elite" and a["sentiment_score"] is not None
    ]
    public_scores = [
        a["sentiment_score"] for a in articles
        if a["voice_type"] == "public" and a["sentiment_score"] is not None
    ]

    # Location breakdown
    location_scores = {}
    for a in articles:
        loc = a["location_relevance"] or "statewide"
        if loc not in location_scores:
            location_scores[loc] = []
        if a["sentiment_score"] is not None:
            location_scores[loc].append(a["sentiment_score"])

    # Topic frequency
    topic_counter = Counter()
    for a in articles:
        tags = json.loads(a["topic_tags"]) if a["topic_tags"] else []
        topic_counter.update(tags)

    # Entity frequency
    entity_counter = Counter()
    for a in articles:
        entities = json.loads(a["entities_mentioned"]) if a["entities_mentioned"] else []
        entity_counter.update(entities)

    # Sentiment label distribution
    label_counter = Counter(a["sentiment_label"] for a in articles if a["sentiment_label"])

    def safe_avg(lst):
        return sum(lst) / len(lst) if lst else None

    return {
        "article_count": len(articles),
        "avg_sentiment": safe_avg(scores),
        "elite_avg": safe_avg(elite_scores),
        "elite_count": len(elite_scores),
        "public_avg": safe_avg(public_scores),
        "public_count": len(public_scores),
        "location_avgs": {
            loc: {"avg": safe_avg(s), "count": len(s)}
            for loc, s in location_scores.items()
        },
        "top_topics": topic_counter.most_common(10),
        "top_entities": entity_counter.most_common(10),
        "sentiment_distribution": dict(label_counter),
    }


def build_synthesis_input(articles, stats, days):
    """Build the input for Claude Sonnet to synthesize the digest."""
    lines = [f"PERIOD: Last {days} days", f"ARTICLES ANALYZED: {stats['article_count']}", ""]

    # Aggregate stats
    lines.append("AGGREGATE STATISTICS:")
    if stats.get("avg_sentiment") is not None:
        lines.append(f"  Overall average sentiment: {stats['avg_sentiment']:.2f}/5.0")
    if stats.get("elite_avg") is not None:
        lines.append(f"  Elite voice avg: {stats['elite_avg']:.2f} (n={stats['elite_count']})")
    if stats.get("public_avg") is not None:
        lines.append(f"  Public voice avg: {stats['public_avg']:.2f} (n={stats['public_count']})")

    lines.append("\n  By location:")
    for loc, data in stats.get("location_avgs", {}).items():
        if data["avg"] is not None:
            lines.append(f"    {loc}: {data['avg']:.2f} (n={data['count']})")

    lines.append("\n  Top topics:")
    for topic, count in stats.get("top_topics", []):
        lines.append(f"    {topic}: {count}")

    lines.append("\n  Top entities mentioned:")
    for entity, count in stats.get("top_entities", []):
        lines.append(f"    {entity}: {count}")

    lines.append("\n  Sentiment distribution:")
    for label, count in stats.get("sentiment_distribution", {}).items():
        lines.append(f"    {label}: {count}")

    # Individual article summaries
    lines.append("\n\nARTICLE SUMMARIES:")
    lines.append("-" * 60)
    for a in articles:
        lines.append(f"\nSource: {a['source']}")
        lines.append(f"Title: {a['title']}")
        lines.append(f"Date: {a['published_date'] or 'Unknown'}")
        lines.append(f"Sentiment: {a['sentiment_label']} ({a['sentiment_score']})")
        lines.append(f"Voice: {a['voice_type']}")
        lines.append(f"Location: {a['location_relevance']}")
        if a["key_claims"]:
            lines.append(f"Key claims: {a['key_claims']}")
        lines.append("")

    return "\n".join(lines)


def generate_digest_with_api(synthesis_input, days, config):
    """Use Claude Sonnet to generate the digest narrative."""
    api_key = get_api_key()
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set and apikey.txt not found"

    try:
        import anthropic
    except ImportError:
        return None, "anthropic package not installed"

    api_config = config.get("anthropic", {})
    model = api_config.get("synthesis_model", "claude-sonnet-4-5-20241022")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = SYNTHESIS_PROMPT.format(days=days)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=prompt,
            messages=[{"role": "user", "content": synthesis_input}],
        )
        return response.content[0].text, None
    except Exception as e:
        logger.error("Digest synthesis API error: %s", e)
        return None, str(e)


def format_fallback_digest(stats, articles, period_start, period_end):
    """Generate a basic digest without the API (template-based)."""
    lines = [
        f"# Wyoming Pulse — Sentiment Digest",
        f"**Period:** {period_start} to {period_end}",
        f"**Articles analyzed:** {stats['article_count']}",
        "",
    ]

    if stats.get("avg_sentiment") is not None:
        lines.append(f"## Sentiment Snapshot")
        lines.append(f"- **Overall average:** {stats['avg_sentiment']:.2f}/5.0")
        if stats.get("elite_avg") is not None:
            lines.append(f"- **Elite voices:** {stats['elite_avg']:.2f} (n={stats['elite_count']})")
        if stats.get("public_avg") is not None:
            lines.append(f"- **Public voices:** {stats['public_avg']:.2f} (n={stats['public_count']})")
        lines.append("")

    lines.append("## By Location")
    for loc, data in stats.get("location_avgs", {}).items():
        if data["avg"] is not None:
            lines.append(f"- **{loc.title()}:** {data['avg']:.2f} (n={data['count']})")
    lines.append("")

    if stats.get("top_topics"):
        lines.append("## Top Topics")
        for topic, count in stats["top_topics"][:5]:
            lines.append(f"- {topic}: {count} mentions")
        lines.append("")

    if stats.get("top_entities"):
        lines.append("## Entities Mentioned")
        for entity, count in stats["top_entities"][:5]:
            lines.append(f"- {entity}: {count} mentions")
        lines.append("")

    lines.append("## Sentiment Distribution")
    for label, count in stats.get("sentiment_distribution", {}).items():
        lines.append(f"- {label}: {count}")
    lines.append("")

    lines.append("## Key Articles")
    # Show the 5 most notable (highest/lowest sentiment deviation from neutral)
    sorted_articles = sorted(
        [a for a in articles if a["sentiment_score"] is not None],
        key=lambda a: abs(a["sentiment_score"] - 3.0),
        reverse=True,
    )
    for a in sorted_articles[:5]:
        lines.append(
            f"- [{a['sentiment_label']}, {a['sentiment_score']:.1f}] "
            f"**{a['title']}** — {a['source']} ({a['published_date'] or 'N/A'})"
        )
    lines.append("")

    lines.append("---")
    lines.append("*Generated by Wyoming Pulse*")
    return "\n".join(lines)


def generate_digest(start_date=None, end_date=None, config=None):
    """
    Generate a digest for the given period.
    If no dates provided, uses the last 14 days.
    Returns the digest filename on success.
    """
    if config is None:
        config = load_config()

    interval = config.get("digest", {}).get("interval_days", 14)

    if end_date is None:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")
    if start_date is None:
        start_dt = datetime.fromisoformat(end_date) - timedelta(days=interval)
        start_date = start_dt.strftime("%Y-%m-%d")

    # Extend date range to include full days
    end_date_full = end_date + "T23:59:59"
    start_date_full = start_date + "T00:00:00"

    conn = db.get_connection()
    articles = db.get_articles_for_digest(conn, start_date_full, end_date_full)

    if not articles:
        logger.warning("No analyzed articles found for period %s to %s", start_date, end_date)
        conn.close()
        return None

    logger.info("Generating digest for %s to %s (%d articles)", start_date, end_date, len(articles))

    # Limit articles
    max_articles = config.get("digest", {}).get("max_articles_per_digest", 50)
    article_list = list(articles)[:max_articles]

    stats = compute_stats(article_list)

    # Try API synthesis first, fall back to template
    days = (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days
    synthesis_input = build_synthesis_input(article_list, stats, days)
    narrative, api_error = generate_digest_with_api(synthesis_input, days, config)

    if narrative:
        # Wrap in markdown header
        content = (
            f"# Wyoming Pulse — Intelligence Digest\n"
            f"**Period:** {start_date} to {end_date}\n"
            f"**Articles analyzed:** {stats['article_count']}\n"
            f"**Overall sentiment:** {stats.get('avg_sentiment', 0):.2f}/5.0\n\n"
            f"---\n\n{narrative}\n\n"
            f"---\n*Generated by Wyoming Pulse on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*"
        )
    else:
        if api_error:
            logger.warning("API synthesis failed (%s), using template fallback", api_error)
        content = format_fallback_digest(stats, article_list, start_date, end_date)

    # Save to file
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"digest_{end_date}.md"
    filepath = OUTPUT_DIR / filename
    filepath.write_text(content, encoding="utf-8")
    logger.info("Digest saved to %s", filepath)

    # Also save a plaintext version for Slack
    plaintext = content.replace("# ", "").replace("## ", "").replace("**", "").replace("*", "")
    slack_path = OUTPUT_DIR / f"digest_{end_date}_slack.txt"
    slack_path.write_text(plaintext, encoding="utf-8")

    # Record in database
    article_ids = [a["id"] for a in article_list]
    db.mark_articles_in_digest(conn, article_ids, filename)
    db.insert_digest(conn, {
        "filename": filename,
        "period_start": start_date,
        "period_end": end_date,
        "article_count": len(article_list),
        "avg_sentiment": stats.get("avg_sentiment"),
        "content": content,
    })

    conn.close()

    print(f"\nDigest generated: {filepath}")
    print(f"Slack version:    {slack_path}")
    print(f"Articles:         {len(article_list)}")
    if stats.get("avg_sentiment") is not None:
        print(f"Avg sentiment:    {stats['avg_sentiment']:.2f}/5.0")

    return filename


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_digest()
