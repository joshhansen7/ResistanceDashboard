"""
Wyoming Pulse — Sentiment Analysis
Sends unanalyzed articles to Claude API for sentiment classification.
"""

import logging
import time

import db
from shared import load_config, get_anthropic_client
from utils import parse_json_response

logger = logging.getLogger("wyoming_pulse.analyze")

SYSTEM_PROMPT = """You are a sentiment analyst tracking public perception of data center development across the United States. You work for Prometheus Hyperscale, which has projects in Evanston and Casper, Wyoming and is expanding nationally.

Analyze the following article and return a JSON object (no markdown fences, no preamble) with these fields:

{
  "sentiment_score": <float 1.0-5.0>,
  "sentiment_label": "<strongly_negative|slightly_negative|neutral|slightly_positive|strongly_positive>",
  "voice_type": "<elite|public>",
  "state": "<wyoming|texas|nationwide|other>",
  "location_relevance": "<statewide|evanston|casper|cheyenne|dallas|nationwide|other>",
  "topic_tags": ["<from: energy_ratepayer, water, jobs_economic, land_use_wildlife, regulation_transparency, tax_incentives, national_security_ai, community_impact>"],
  "entities_mentioned": ["<company or organization names>"],
  "key_claims": "<1-2 sentence summary of the most notable claims or narratives>",
  "sentiment_justification": "<1-2 sentences explaining why this specific score was assigned — cite the specific tone, framing, or quotes that drove the rating>"
}

State and location rules:
- "state": the US state the article primarily concerns. Use "nationwide" for federal/national policy with no single state focus. Use "other" for states not listed.
- "location_relevance": the specific city/region within the state. Use "statewide" when the article covers a whole state without a specific city focus. Use "nationwide" only when state is also "nationwide". Wyoming cities: evanston, casper, cheyenne. Texas cities: dallas.

Sentiment scale:
1.0 = Strongly negative (active opposition, calls for moratorium, fear-based)
2.0 = Slightly negative (concern, skepticism, cautionary tone)
3.0 = Neutral (factual reporting, balanced, no clear lean)
4.0 = Slightly positive (cautious optimism, emphasis on benefits with caveats)
5.0 = Strongly positive (enthusiastic support, boosterism, economic development framing)

Voice types:
- "elite": legislators, officials, editorial boards, organization spokespeople, academics
- "public": comments, social media posts, letters to editor, community members at public meetings

Be precise. A factual news article that quotes both supporters and opponents is neutral (3.0). An editorial urging caution is slightly negative (2.0). A county commissioner's enthusiastic endorsement is strongly positive (5.0)."""


def build_user_message(article):
    """Build the user message content for an article."""
    content = article["full_text"] or article["summary"] or ""
    # Truncate to 3000 chars to control token usage
    if len(content) > 3000:
        content = content[:3000] + "..."

    return (
        f"Source: {article['source']}\n"
        f"Title: {article['title']}\n"
        f"Date: {article['published_date'] or 'Unknown'}\n"
        f"Content: {content}"
    )


def parse_analysis_response(response_text):
    """Parse the JSON response from Claude, handling edge cases."""
    result = parse_json_response(response_text)
    if result is None:
        return None

    # Validate required fields
    required = ["sentiment_score", "sentiment_label", "voice_type", "state", "location_relevance"]
    for field in required:
        if field not in result:
            logger.warning("Missing required field in response: %s", field)
            return None

    # Clamp sentiment score to valid range
    score = result.get("sentiment_score", 3.0)
    result["sentiment_score"] = max(1.0, min(5.0, float(score)))

    # Ensure list fields are lists
    for list_field in ("topic_tags", "entities_mentioned"):
        if list_field not in result or not isinstance(result[list_field], list):
            result[list_field] = []

    return result


def analyze_articles(config=None, limit=20):
    """
    Analyze unanalyzed articles using Claude API.
    Returns dict with summary statistics.
    """
    if config is None:
        config = load_config()

    client = get_anthropic_client()
    if client is None:
        return {"error": "API key not configured", "analyzed": 0}

    api_config = config.get("anthropic", {})
    model = api_config.get("classification_model", "claude-haiku-4-5-20251001")
    max_retries = api_config.get("max_retries", 3)
    timeout = api_config.get("timeout_seconds", 30)

    conn = db.get_connection()
    articles = db.get_unanalyzed_articles(conn, limit=limit)

    if not articles:
        logger.info("No unanalyzed articles found.")
        conn.close()
        return {"analyzed": 0, "skipped": 0, "errors": 0}

    logger.info("Found %d articles to analyze", len(articles))
    analyzed_count = 0
    error_count = 0
    total_input_tokens = 0
    total_output_tokens = 0

    for article in articles:
        article_id = article["id"]
        title = article["title"]
        logger.info("Analyzing: %s", title[:60])

        user_msg = build_user_message(article)

        for attempt in range(max_retries):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=600,
                    timeout=timeout,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                )

                # Track token usage
                usage = response.usage
                total_input_tokens += usage.input_tokens
                total_output_tokens += usage.output_tokens

                response_text = response.content[0].text
                analysis = parse_analysis_response(response_text)

                if analysis:
                    db.update_article_analysis(conn, article_id, analysis)
                    analyzed_count += 1
                    logger.info(
                        "  -> %s (%.1f) [%s]",
                        analysis["sentiment_label"],
                        analysis["sentiment_score"],
                        analysis["location_relevance"],
                    )
                    break
                else:
                    logger.warning("  -> Invalid response on attempt %d", attempt + 1)
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)

            except Exception as e:
                logger.error("  -> API error on attempt %d: %s", attempt + 1, e)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    error_count += 1

        # Brief pause between API calls
        time.sleep(0.5)

    conn.close()

    summary = {
        "analyzed": analyzed_count,
        "errors": error_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }
    logger.info(
        "Analysis complete: %d analyzed, %d errors, %d input tokens, %d output tokens",
        analyzed_count, error_count, total_input_tokens, total_output_tokens,
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = analyze_articles()
    print(f"\nAnalysis Results:")
    print(f"  Analyzed:       {result.get('analyzed', 0)}")
    print(f"  Errors:         {result.get('errors', 0)}")
    if "total_input_tokens" in result:
        print(f"  Input tokens:   {result['total_input_tokens']:,}")
        print(f"  Output tokens:  {result['total_output_tokens']:,}")
