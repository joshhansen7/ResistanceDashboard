"""
Prometheus — Digest Generation
Generates biweekly intelligence reports from analyzed articles.
"""

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import sentiment_index
from shared import load_config, get_api_key

logger = logging.getLogger("resistance_dashboard.digest")

OUTPUT_DIR = Path(__file__).parent / "output" / "digests"

SYNTHESIS_PROMPT = """You are an intelligence analyst producing a biweekly sentiment report about data center development across the United States for Prometheus Hyperscale leadership.

{states_summary}

Given the following classified articles and aggregate data from the past {days} days, produce a concise intelligence digest. Be analytical and objective. Distinguish between signals and noise. Flag anything that represents a meaningful shift from baseline.

The digest should include:
1. NATIONAL SENTIMENT SNAPSHOT — Overall WSI score, trend direction, period comparison
2. STATE BREAKDOWN — Brief notes on each tracked state with their WSI scores
3. BY LOCATION — Notable developments in specific cities/counties
4. TOP THEMES — The 2-3 most significant narratives or developments
5. ENTITY TRACKER — Which companies were mentioned and in what context
6. LEGISLATIVE/REGULATORY UPDATE — Any policy developments (by state)
7. WATCH LIST — Emerging issues that bear monitoring
8. KEY ARTICLES — The 3-5 most significant pieces with source, date, and state

Keep it under 1000 words. Write for busy executives who want the bottom line."""

def _get_tracked_states(conn):
    """Derive tracked states from the database (states with analyzed articles)."""
    rows = conn.execute(
        "SELECT DISTINCT state FROM articles WHERE analyzed = 1 AND state IS NOT NULL "
        "AND state NOT IN ('nationwide', 'other') ORDER BY state"
    ).fetchall()
    return [r["state"] for r in rows]


def compute_stats(articles, conn=None, tracked_states=None):
    """Compute aggregate statistics from a list of analyzed articles."""
    if not articles:
        return {}

    scores = [a["sentiment_score"] for a in articles if a["sentiment_score"] is not None]

    # Per-state breakdown
    state_articles = defaultdict(list)
    for a in articles:
        state = a["state"] if a["state"] else "other"
        state_articles[state].append(a)

    # Location breakdown (grouped by state)
    location_scores = defaultdict(lambda: defaultdict(list))
    for a in articles:
        state = a["state"] if a["state"] else "other"
        loc = a["location_relevance"] or "statewide"
        if a["sentiment_score"] is not None:
            location_scores[state][loc].append(a["sentiment_score"])

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

    stats = {
        "article_count": len(articles),
        "avg_sentiment": safe_avg(scores),
        "state_counts": {s: len(arts) for s, arts in state_articles.items()},
        "location_avgs": {
            state: {
                loc: {"avg": safe_avg(s), "count": len(s)}
                for loc, s in locs.items()
            }
            for state, locs in location_scores.items()
        },
        "top_topics": topic_counter.most_common(10),
        "top_entities": entity_counter.most_common(10),
        "sentiment_distribution": dict(label_counter),
    }

    # WSI scores (if connection available)
    if conn is not None:
        overall_bundle = sentiment_index.compute_wsi_bundle(conn)
        stats["wsi_overall"] = overall_bundle.get("current_wsi")
        stats["wsi_by_state"] = {}
        for state in tracked_states:
            state_bundle = sentiment_index.compute_wsi_bundle(conn, state=state)
            stats["wsi_by_state"][state] = state_bundle.get("current_wsi")

        stats["period_comparison"] = overall_bundle.get("period_comparison")

    return stats


def build_synthesis_input(articles, stats, days, tracked_states=None):
    """Build the input for Claude Sonnet to synthesize the digest."""
    lines = [f"PERIOD: Last {days} days", f"ARTICLES ANALYZED: {stats['article_count']}", ""]

    # Aggregate stats
    lines.append("AGGREGATE STATISTICS:")
    if stats.get("avg_sentiment") is not None:
        lines.append(f"  Overall average sentiment: {stats['avg_sentiment']:.2f}/5.0")
    if stats.get("wsi_overall") is not None:
        lines.append(f"  Weighted Sentiment Index (WSI): {stats['wsi_overall']:.2f}/5.0")

    # WSI by state
    wsi_by_state = stats.get("wsi_by_state", {})
    if wsi_by_state:
        lines.append("\n  WSI by state:")
        for state, wsi in wsi_by_state.items():
            count = stats.get("state_counts", {}).get(state, 0)
            if wsi is not None:
                lines.append(f"    {state.title()}: {wsi:.2f} (n={count})")

    # Period comparison
    pc = stats.get("period_comparison", {})
    if pc and pc.get("change") is not None:
        lines.append(f"\n  Period comparison: current 4wk={pc['current_4wk']:.2f}, "
                     f"prior 4wk={pc['prior_4wk']:.2f}, change={pc['change']:+.2f} ({pc['direction']})")

    # Location breakdown by state
    lines.append("\n  By location:")
    for state in tracked_states:
        state_locs = stats.get("location_avgs", {}).get(state, {})
        if state_locs:
            for loc, data in state_locs.items():
                if data["avg"] is not None:
                    lines.append(f"    {state.title()} / {loc}: {data['avg']:.2f} (n={data['count']})")

    lines.append("\n  Top topics:")
    for topic, count in stats.get("top_topics", []):
        lines.append(f"    {topic}: {count}")

    lines.append("\n  Top entities mentioned:")
    for entity, count in stats.get("top_entities", []):
        lines.append(f"    {entity}: {count}")

    lines.append("\n  Sentiment distribution:")
    for label, count in stats.get("sentiment_distribution", {}).items():
        lines.append(f"    {label}: {count}")

    # Individual article summaries (grouped by state)
    lines.append("\n\nARTICLE SUMMARIES:")
    lines.append("-" * 60)
    for state in list(tracked_states) + ["nationwide", "other"]:
        state_arts = [a for a in articles if (a["state"] if a["state"] else "other") == state]
        if not state_arts:
            continue
        lines.append(f"\n--- {state.upper()} ({len(state_arts)} articles) ---")
        for a in state_arts:
            lines.append(f"\nSource: {a['source']}")
            lines.append(f"Title: {a['title']}")
            lines.append(f"Date: {a['published_date'] or 'Unknown'}")
            lines.append(f"Sentiment: {a['sentiment_label']} ({a['sentiment_score']})")
            lines.append(f"Location: {a['location_relevance']}")
            if a["key_claims"]:
                lines.append(f"Key claims: {a['key_claims']}")
            lines.append("")

    return "\n".join(lines)


def _build_states_summary(config, tracked_states):
    """Build a dynamic 'States currently tracked' line from config + database."""
    parts = []
    states_cfg = config.get("priority_states", {})
    for sk in tracked_states:
        locs = states_cfg.get(sk, {}).get("locations", {})
        name = sk.title()
        if locs:
            loc_names = [k.replace("_", " ").title() for k in locs]
            parts.append(f"{name} ({', '.join(loc_names)})")
        else:
            parts.append(name)
    if parts:
        return "States currently tracked: " + ", ".join(parts) + "."
    return "No state-specific tracking configured; analyzing articles from all US states."


def generate_digest_with_api(synthesis_input, days, config, tracked_states=None):
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

    states_summary = _build_states_summary(config, tracked_states or [])
    client = anthropic.Anthropic(api_key=api_key)
    prompt = SYNTHESIS_PROMPT.format(days=days, states_summary=states_summary)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=prompt,
            messages=[{"role": "user", "content": synthesis_input}],
        )
        return response.content[0].text, None
    except Exception as e:
        logger.error("Digest synthesis API error: %s", e)
        return None, str(e)


def format_fallback_digest(stats, articles, period_start, period_end, tracked_states=None):
    """Generate a basic digest without the API (template-based)."""
    lines = [
        f"# Prometheus — Intelligence Digest",
        f"**Period:** {period_start} to {period_end}",
        f"**Articles analyzed:** {stats['article_count']}",
        "",
    ]

    if stats.get("avg_sentiment") is not None:
        lines.append("## Sentiment Snapshot")
        lines.append(f"- **Overall average:** {stats['avg_sentiment']:.2f}/5.0")
        if stats.get("wsi_overall") is not None:
            lines.append(f"- **Weighted Sentiment Index:** {stats['wsi_overall']:.2f}/5.0")
        pc = stats.get("period_comparison", {})
        if pc and pc.get("change") is not None:
            lines.append(f"- **4wk trend:** {pc['change']:+.2f} ({pc['direction']})")
        lines.append("")

    # Per-state WSI
    wsi_by_state = stats.get("wsi_by_state", {})
    if wsi_by_state:
        lines.append("## State Breakdown")
        for state, wsi in wsi_by_state.items():
            if wsi is not None:
                lines.append(f"- **{state.title()}:** WSI {wsi:.2f}")
        lines.append("")

    lines.append("## By Location")
    for state in tracked_states:
        state_locs = stats.get("location_avgs", {}).get(state, {})
        for loc, data in state_locs.items():
            if data["avg"] is not None:
                lines.append(f"- **{state.title()} / {loc.replace('_', ' ').title()}:** {data['avg']:.2f} (n={data['count']})")
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
    sorted_articles = sorted(
        [a for a in articles if a["sentiment_score"] is not None],
        key=lambda a: abs(a["sentiment_score"] - 3.0),
        reverse=True,
    )
    for a in sorted_articles[:5]:
        state_label = (a["state"] or "").upper()[:2] if a["state"] else ""
        lines.append(
            f"- [{a['sentiment_label']}, {a['sentiment_score']:.1f}] "
            f"**{a['title']}** — {a['source']} ({a['published_date'] or 'N/A'}) [{state_label}]"
        )
    lines.append("")

    lines.append("---")
    lines.append("*Generated by Prometheus Intelligence Dashboard*")
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
        end_date = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
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

    tracked_states = _get_tracked_states(conn)
    stats = compute_stats(article_list, conn=conn, tracked_states=tracked_states)

    # Try API synthesis first, fall back to template
    days = (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days
    synthesis_input = build_synthesis_input(article_list, stats, days, tracked_states=tracked_states)
    narrative, api_error = generate_digest_with_api(synthesis_input, days, config, tracked_states=tracked_states)

    if narrative:
        wsi_str = f"**WSI:** {stats.get('wsi_overall', 0):.2f}/5.0\n" if stats.get("wsi_overall") else ""
        content = (
            f"# Prometheus — Intelligence Digest\n"
            f"**Period:** {start_date} to {end_date}\n"
            f"**Articles analyzed:** {stats['article_count']}\n"
            f"**Overall sentiment:** {stats.get('avg_sentiment', 0):.2f}/5.0\n"
            f"{wsi_str}\n"
            f"---\n\n{narrative}\n\n"
            f"---\n*Generated by Prometheus Intelligence Dashboard on {datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M UTC')}*"
        )
    else:
        if api_error:
            logger.warning("API synthesis failed (%s), using template fallback", api_error)
        content = format_fallback_digest(stats, article_list, start_date, end_date, tracked_states=tracked_states)

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
    if stats.get("wsi_overall") is not None:
        print(f"WSI:              {stats['wsi_overall']:.2f}/5.0")

    return filename


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_digest()
