"""
Prometheus — Relevance & Keyword Scoring Helpers

Shared article-scoring utilities used by the search pipeline (websearch.py):
keyword/tier matching, state/location detection, and Claude-based relevance
scoring. (This module formerly hosted RSS feed ingestion, now retired — the
dashboard sources all articles via Google News search.)
"""

import logging
import re

from geo import infer_state_from_text

logger = logging.getLogger("resistance_dashboard.ingest")

RELEVANCE_PROMPT = """Score this article's relevance to data center development in the United States on a 1-10 scale.

Context: We track public sentiment toward data center projects nationwide. Prometheus Hyperscale is a data center developer we track closely.

Scoring guide:
10: Directly about Prometheus Hyperscale, its leadership, or its projects
9: Data center project news naming a specific company and location
8: Explicitly about data center development, siting, bans, moratoriums, or community opposition in any US state
7: About data center infrastructure, permitting, tax incentives, or utility/grid deals for data centers
6: Data center industry trends, market analysis, or company expansions with specific locations
5: General AI infrastructure or hyperscaler capital spending with data center implications
4: General AI industry news or tech company strategy tangentially related to data centers
3: Energy/grid policy or land use without specific data center connection
1-2: No relevance to data centers (junk, unrelated local news, sports, etc.)

Return ONLY a JSON object (no markdown fences, no preamble):
{{"score": <int 1-10>, "reason": "<one sentence explanation>"}}

Article:
Title: {title}
Source: {source}
Date: {date}
Summary: {summary}"""


def normalize_text(text):
    """Lowercase and normalize whitespace for keyword matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.lower().strip())


# Short, precise list for company keyword co-occurrence filtering.
# A company name must appear alongside one of these to match.
TOPIC_SIGNALS = [
    "data center", "data centre", "datacenter", "hyperscale",
    "server farm", "colocation", "megawatt", "gigawatt",
    "data hall", "compute", "ai infrastructure", "cooling",
    "power capacity", "rack", "campus",
]


def _build_keyword_set(config, state_key=None):
    """
    Build merged keyword lists from nationwide + optional priority_state.
    Returns dict with primary, companies, secondary, and state_locations.
    """
    nationwide = config.get("nationwide", {}).get("keywords", {})
    result = {
        "primary": [kw.lower() for kw in nationwide.get("primary", [])],
        "companies": [kw.lower() for kw in nationwide.get("companies", [])],
        "secondary": [kw.lower() for kw in nationwide.get("secondary", [])],
        "state_locations": [],
    }
    if state_key:
        state_cfg = config.get("priority_states", {}).get(state_key, {})
        state_kw = state_cfg.get("keywords", {})
        result["primary"].extend(kw.lower() for kw in state_kw.get("primary", []))
        result["companies"].extend(kw.lower() for kw in state_kw.get("companies", []))
        result["secondary"].extend(kw.lower() for kw in state_kw.get("secondary", []))
        # Collect location keywords for state context checking
        locs = state_cfg.get("locations", {})
        for loc_list in locs.values():
            result["state_locations"].extend(kw.lower() for kw in loc_list)
        result["state_locations"].append(state_key.lower())
    return result


def _match_keywords(text, keywords):
    """
    Match keywords against text using tiered logic.
    Returns (matched_keywords_list, score) or (None, 0.0).
    """
    matched = []

    # Tier 1: Primary keywords — any single match (score 1.0)
    # Note: keywords are pre-lowercased by _build_keyword_set.
    for kw in keywords["primary"]:
        if kw in text:
            matched.append(kw)
    if matched:
        return matched, 1.0

    # Context checks for lower tiers
    has_state_context = any(loc in text for loc in keywords["state_locations"])
    has_topic_signal = any(sig in text for sig in TOPIC_SIGNALS)

    # Tier 2: Company keywords — require topic signal OR state context
    for kw in keywords["companies"]:
        if kw in text and (has_topic_signal or has_state_context):
            matched.append(kw)
    if matched:
        return matched, 0.9 if has_topic_signal else 0.8

    # Tier 3: Secondary — require topic signal, company presence, or state context
    company_present = any(kw in text for kw in keywords["companies"])
    for kw in keywords["secondary"]:
        if kw in text and (has_topic_signal or company_present or has_state_context):
            matched.append(kw)
    if matched:
        return matched, 0.6

    return None, 0.0


def check_keyword_match(title, summary, config, state_key=None):
    """
    Check if an article is relevant and determine which state it belongs to.

    Two separate concerns:
      1. RELEVANCE — does the article match topic keywords? (nationwide + state-specific)
      2. CATEGORIZATION — which state is it about? (priority state locations > text inference)

    Returns (matched_keywords, score, matched_state) or (None, 0, None).
    """
    text = normalize_text(f"{title} {summary}")
    keywords = _build_keyword_set(config, state_key)
    matched, score = _match_keywords(text, keywords)

    if not matched:
        return None, 0.0, None

    # Determine state
    if state_key:
        # Check if article actually mentions this state's locations
        if any(loc in text for loc in keywords["state_locations"]):
            return matched, score, state_key
        # Infer from text, but default to the query state
        inferred = infer_state_from_text(title, summary)
        return matched, score, inferred or state_key

    # No state hint — check priority state locations first
    priority_states = config.get("priority_states", {})
    for sk, st_cfg in priority_states.items():
        locs = st_cfg.get("locations", {})
        all_loc_kw = [kw.lower() for loc_list in locs.values() for kw in loc_list] + [sk]
        if any(kw in text for kw in all_loc_kw):
            return matched, score, sk

    # Infer state from article text (handles all 50 states via geo.py)
    inferred = infer_state_from_text(title, summary)
    return matched, score, inferred


def detect_location(title, summary, config, state_key=None):
    """Determine the geographic relevance of an article."""
    text = normalize_text(f"{title} {summary}")

    if state_key:
        state_cfg = config.get("priority_states", {}).get(state_key, {})
        locations = state_cfg.get("locations", {})
    else:
        locations = config.get("locations", {})

    for loc_name, loc_keywords in locations.items():
        if loc_name == "statewide":
            continue
        for kw in loc_keywords:
            if kw.lower() in text:
                return loc_name

    return "statewide"


def _score_relevance(client, model, article):
    """Score an article's relevance using Claude API. Returns (score, reason)."""
    try:
        prompt = RELEVANCE_PROMPT.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            date=article.get("published_date") or "Unknown",
            summary=(article.get("summary") or "")[:500],
        )
        response = client.messages.create(
            model=model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        from utils import parse_json_response
        result = parse_json_response(text)
        if result:
            score = max(1, min(10, int(result.get("score", 5))))
            return score, result.get("reason", "")

        # Regex fallback
        score_match = re.search(r'"?score"?\s*[:=]\s*(\d+)', text)
        reason_match = re.search(r'"?reason"?\s*[:=]\s*"([^"]*)"', text)
        if score_match:
            score = max(1, min(10, int(score_match.group(1))))
            reason = reason_match.group(1) if reason_match else ""
            return score, reason
    except Exception as e:
        logger.warning("Relevance scoring failed: %s", e)
    return 5, "Scoring error"


def score_relevance_hybrid(article, client=None, model=None, local_ready=None):
    """
    Score relevance using local LLM first, falling back to Claude API.
    Returns (score, reason) tuple.
    """
    try:
        import local_llm
        if local_ready is None:
            local_ready = local_llm.ensure_running()
        if local_ready:
            score, reason = local_llm.score_relevance(
                article.get("title", ""),
                article.get("summary", ""),
                article.get("source", ""),
            )
            return score, reason
    except Exception as e:
        logger.debug("Local LLM unavailable: %s", e)

    # Fallback to Claude API
    if client and model:
        return _score_relevance(client, model, article)

    return None, None
