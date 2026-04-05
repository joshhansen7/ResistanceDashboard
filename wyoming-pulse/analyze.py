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

SYSTEM_PROMPT_TEMPLATE = """You are a sentiment analyst tracking public perception of data center development across the United States.

Analyze the following article and return a JSON object (no markdown fences, no preamble) with these fields:

{{
  "sentiment_score": <float 1.0-5.0>,
  "sentiment_label": "<strongly_negative|slightly_negative|neutral|slightly_positive|strongly_positive>",
  "locations": [
    {{"state": "<lowercase state name>", "place": "<city, township, or county if applicable>", "relevance": "<primary|mentioned>"}}
  ],
  "topic_tags": ["<from: {topic_tags}>"],
  "entities_mentioned": ["<company or organization names>"],
  "key_claims": "<1-2 sentence summary of the most notable claims or narratives>",
  "sentiment_justification": "<1-2 sentences explaining why this specific score was assigned — cite the specific tone, framing, or quotes that drove the rating>"
}}

{topic_descriptions}

Geographic tagging rules for the "locations" array:
- Each entry needs "state" (lowercase US state name), optional "place" (specific city/township/county), and "relevance" ("primary" or "mentioned")
- "primary": the article is primarily about this place. "mentioned": the place is referenced but isn't the main focus
- An article can have multiple primary locations (e.g., comparing developments in two cities)
- An article about state-level policy should include a statewide entry with no "place" field
- If the article discusses both statewide policy AND specific locations, include entries for both
- If the article is about multiple states, include entries for each state
- Use "nationwide" as state for federal/national scope with no single-state focus
- Be specific with place names: prefer "Saline Township" over "Ann Arbor area" if the article names the township
- DISAMBIGUATION: When a place name exists in multiple states (e.g., Evanston in WY vs IL, Springfield in many states, Portland in OR vs ME), use context clues to determine the correct state: nearby cities mentioned, state agencies referenced, regional references, news source geography.

Sentiment scale — rate the TONE and FRAMING of the article, not the subject matter:
1.0 = Strongly negative — article frames data centers as harmful, uses alarming language, amplifies opposition voices without counterpoint
1.5 = Negative leaning strongly — predominantly critical framing with minimal balance
2.0 = Slightly negative — cautionary tone, leads with concerns, skeptical framing
2.5 = Negative leaning mildly — mostly balanced but tilts toward concerns
3.0 = Neutral — factual reporting, balanced quotes from both sides, no editorial lean
3.5 = Positive leaning mildly — mostly balanced but tilts toward benefits
4.0 = Slightly positive — optimistic framing, leads with benefits, emphasizes opportunity
4.5 = Positive leaning strongly — predominantly supportive with minimal caveats
5.0 = Strongly positive — enthusiastic boosterism, promotional tone, uncritical support

IMPORTANT: Score based on how the article is WRITTEN, not what it is ABOUT.
- A neutral Reuters article reporting on a moratorium vote = 3.0 (neutral reporting)
- An editorial arguing moratoriums are needed = 1.5 (advocacy against data centers)
- An editorial arguing moratoriums are harmful = 4.5 (advocacy for data centers)
- A press release announcing a new data center with only positive framing = 5.0
- An investigative piece examining both economic benefits and environmental costs = 3.0
- A community newspaper quoting angry residents with no industry response = 1.5"""


def build_user_message(article):
    """Build the user message content for an article."""
    content = article["full_text"] or article["summary"] or ""
    # Truncate to 8000 chars — enough for location/entity detection
    # deeper in the article while keeping token costs reasonable
    if len(content) > 8000:
        content = content[:8000] + "..."

    return (
        f"Source: {article['source']}\n"
        f"Title: {article['title']}\n"
        f"Date: {article['published_date'] or 'Unknown'}\n"
        f"Content: {content}"
    )


def parse_analysis_response(response_text):
    """Parse the JSON response from Claude, handling edge cases."""
    from geo import normalize_locations, normalize_state_key

    result = parse_json_response(response_text)
    if result is None:
        return None

    # Validate required fields — accept either new format (locations) or old (state + location_relevance)
    if "sentiment_score" not in result or "sentiment_label" not in result:
        logger.warning("Missing sentiment_score or sentiment_label in response")
        return None

    # Handle location formats
    if "locations" in result and isinstance(result["locations"], list) and result["locations"]:
        # New multi-location format
        locations = result["locations"]
        # Normalize state keys and reject non-US locations
        for loc in locations:
            if "state" in loc:
                loc["state"] = normalize_state_key(loc["state"]) or loc["state"]
        # Filter to only valid US states
        valid_locations = [l for l in locations if normalize_state_key(l.get("state"))]
        locations = valid_locations if valid_locations else [{"state": "nationwide", "relevance": "primary", "place": "nationwide"}]
        # Normalize FIPS
        normalize_locations(locations)
        result["locations_json"] = locations
        # Set backward-compat fields from first primary (or first) location
        primary = next((l for l in locations if l.get("relevance") == "primary"), locations[0])
        result["state"] = primary.get("state", "other")
        place = primary.get("place")
        result["location_relevance"] = place if place else "statewide"
    elif "state" in result:
        # Old single-location format — wrap into locations array
        state = result.get("state", "other")
        loc = result.get("location_relevance", "statewide")
        entry = {"state": state, "relevance": "primary"}
        if loc and loc not in ("statewide", "nationwide"):
            entry["place"] = loc
        locations = [entry]
        normalize_locations(locations)
        result["locations_json"] = locations
    else:
        # No location data — default to nationwide (factors into national
        # averages but not any specific state).
        logger.info("No location data in response — defaulting to nationwide")
        locations = [{"state": "nationwide", "relevance": "primary", "place": "nationwide"}]
        result["locations_json"] = locations
        result["state"] = "nationwide"
        result["location_relevance"] = "nationwide"

    # Clamp sentiment score to valid range
    score = result.get("sentiment_score", 3.0)
    result["sentiment_score"] = max(1.0, min(5.0, float(score)))

    # Normalize sentiment label to standard 5 categories based on score.
    # The prompt allows half-point scores for granularity, but labels
    # must stay in the standard set for dashboard compatibility.
    s = result["sentiment_score"]
    if s <= 1.5:
        result["sentiment_label"] = "strongly_negative"
    elif s <= 2.5:
        result["sentiment_label"] = "slightly_negative"
    elif s <= 3.5:
        result["sentiment_label"] = "neutral"
    elif s <= 4.5:
        result["sentiment_label"] = "slightly_positive"
    else:
        result["sentiment_label"] = "strongly_positive"

    # Ensure list fields are lists
    for list_field in ("topic_tags", "entities_mentioned"):
        if list_field not in result or not isinstance(result[list_field], list):
            result[list_field] = []

    return result


def analyze_articles(config=None, limit=None, progress_callback=None):
    """
    Analyze unanalyzed articles using Claude API.
    Returns dict with summary statistics.
    If limit is None, all unanalyzed articles are processed.
    If progress_callback is provided, it is called as progress_callback(current, total)
    after each article is processed.
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

    # Build topic list and descriptions from config
    topics = config.get("topics", [])
    if topics:
        topic_keys = [t["key"] for t in topics]
        topic_tags_str = ", ".join(topic_keys)
        desc_lines = ["Topic descriptions:"] + [
            f"- {t['key']}: {t['description']}" for t in topics
        ]
        topic_descriptions_str = "\n".join(desc_lines)
    else:
        # Fallback if no topics in config
        topic_tags_str = "energy_ratepayer, water, jobs_economic, land_use_wildlife, regulation_transparency, tax_incentives, national_security_ai, community_impact"
        topic_descriptions_str = ""

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        topic_tags=topic_tags_str,
        topic_descriptions=topic_descriptions_str,
    )

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

    total = len(articles)
    for idx, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]
        logger.info("Analyzing: %s", title[:60])

        user_msg = build_user_message(article)

        for attempt in range(max_retries):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    timeout=timeout,
                    system=system_prompt,
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

        # Report progress after each article
        if progress_callback is not None:
            progress_callback(idx + 1, total)

        # Brief pause between API calls
        time.sleep(0.25)

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
