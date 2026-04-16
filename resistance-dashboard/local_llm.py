"""
Prometheus — Local LLM Integration
Uses Ollama (Qwen3 8B) for relevance scoring, topic classification,
location inference, and summarization. Falls back to Claude API if unavailable.
"""

import json
import logging
import subprocess
import time

import requests

from shared import load_config

logger = logging.getLogger("resistance_dashboard.local_llm")

_ollama_checked = False
_ollama_available = False
_ollama_checked_at = 0.0
_ollama_models = {}

OLLAMA_STATUS_TTL_SECONDS = 30


def _get_config():
    """Get local LLM config from config.yaml."""
    config = load_config()
    return config.get("local_llm", {})


def is_available():
    """Check if Ollama is reachable."""
    global _ollama_checked, _ollama_available, _ollama_checked_at
    llm_cfg = _get_config()
    if not llm_cfg.get("enabled", True):
        return False

    base_url = llm_cfg.get("base_url", "http://localhost:11434")
    now = time.time()
    if _ollama_checked and (now - _ollama_checked_at) < OLLAMA_STATUS_TTL_SECONDS:
        return _ollama_available

    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        _ollama_available = resp.status_code == 200
        _ollama_models[base_url] = resp.json().get("models", []) if _ollama_available else []
        _ollama_checked = True
        _ollama_checked_at = now
        return _ollama_available
    except Exception:
        _ollama_available = False
        _ollama_models[base_url] = []
        _ollama_checked = True
        _ollama_checked_at = now
        return False


def ensure_running():
    """
    Ensure Ollama is running and the configured model is available.
    Auto-launches Ollama on macOS if not running.
    Returns True if ready, False if unavailable.
    """
    global _ollama_checked

    llm_cfg = _get_config()
    if not llm_cfg.get("enabled", True):
        logger.info("Local LLM disabled in config")
        return False

    base_url = llm_cfg.get("base_url", "http://localhost:11434")
    model = llm_cfg.get("model", "qwen3:8b")

    # Check if already running
    if is_available():
        # Check if model is pulled
        if _model_available(base_url, model):
            return True
        # Pull the model
        logger.info("Pulling model %s...", model)
        try:
            subprocess.run(["ollama", "pull", model], check=True, timeout=600)
            _ollama_checked = False
            return True
        except Exception as e:
            logger.warning("Failed to pull model %s: %s", model, e)
            return False

    # Try to launch Ollama (macOS)
    logger.info("Ollama not running — attempting to launch...")
    try:
        subprocess.Popen(
            ["open", "-a", "Ollama"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning("Failed to launch Ollama: %s", e)
        return False

    # Poll until available (max ~15 seconds)
    for _ in range(15):
        time.sleep(1)
        if is_available():
            logger.info("Ollama is now running")
            if _model_available(base_url, model):
                return True
            # Pull model
            logger.info("Pulling model %s...", model)
            try:
                subprocess.run(["ollama", "pull", model], check=True, timeout=600)
                _ollama_checked = False
                return True
            except Exception as e:
                logger.warning("Failed to pull model %s: %s", model, e)
                return False

    logger.warning("Ollama did not start within 15 seconds")
    return False


def _model_available(base_url, model):
    """Check if a specific model is available in Ollama."""
    try:
        if not is_available():
            return False
        models = _ollama_models.get(base_url, [])
        model_base = model.split(":")[0]
        for m in models:
            name = m.get("name", "")
            if name == model or name.startswith(model_base + ":") or name == model_base:
                return True
        return False
    except Exception:
        pass
    return False


def _call_ollama(prompt, system=None):
    """Make a request to Ollama's chat API with thinking disabled."""
    llm_cfg = _get_config()
    base_url = llm_cfg.get("base_url", "http://localhost:11434")
    model = llm_cfg.get("model", "qwen3:8b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 256,
        },
    }

    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")
    except Exception as e:
        logger.error("Ollama API error: %s", e)
        return None


def _extract_json_object(text):
    """Extract first balanced {...} JSON object from text using brace counting."""
    start = text.find('{')
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def _parse_json(text):
    """Parse JSON from LLM response, handling markdown fences and thinking tags."""
    if not text:
        return None

    # Strip thinking tags (Qwen3 sometimes produces these)
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Strip markdown fences
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from the text (brace-counted, handles nesting)
        obj_text = _extract_json_object(text)
        if obj_text:
            try:
                return json.loads(obj_text)
            except json.JSONDecodeError:
                pass
        # Try array
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def score_relevance(title, summary, source=""):
    """
    Score an article's relevance to data center development (1-10).
    Returns (score, reason) tuple.
    """
    prompt = f"""Score this article's relevance to data center development in the United States on a 1-10 scale.

Scoring guide:
10: Directly about Prometheus Hyperscale, its leadership, or its projects
9: Data center project news naming a specific company and location
8: Explicitly about data center development, siting, bans, moratoriums, or community opposition
7: About data center infrastructure, permitting, tax incentives, or utility/grid deals
6: Data center industry trends, market analysis, or company expansions with specific locations
5: General AI infrastructure or hyperscaler capital spending with data center implications
4: General AI industry news or tech company strategy tangentially related to data centers
3: Energy/grid policy or land use that could affect data center feasibility
1-2: No relevance to data centers

Return ONLY a JSON object: {{"score": <int 1-10>, "reason": "<one sentence>"}}

Article:
Title: {title}
Source: {source}
Summary: {(summary or '')[:500]}"""

    response = _call_ollama(prompt)
    if response is None:
        return None, "Ollama unavailable"

    result = _parse_json(response)
    if result and "score" in result:
        score = max(1, min(10, int(float(result["score"]))))
        return score, result.get("reason", "")

    # Fallback: try to extract score from text
    import re
    match = re.search(r'"?score"?\s*[:=]\s*(\d+)', response)
    if match:
        return max(1, min(10, int(match.group(1)))), ""

    logger.warning("Failed to parse relevance score from local LLM")
    return None, "Scoring failed"


def classify_topics(title, content, topic_list):
    """
    Classify an article into topic categories from the provided list.
    Returns list of matching topic keys.
    """
    topics_str = "\n".join(
        f"- {t['key']}: {t['description']}"
        for t in topic_list
    )

    prompt = f"""Classify this article into the relevant topic categories. An article can match multiple topics.

Available topics:
{topics_str}

Return ONLY a JSON array of matching topic keys, e.g. ["energy_ratepayer", "water"]

Article:
Title: {title}
Content: {(content or '')[:1000]}"""

    response = _call_ollama(prompt)
    result = _parse_json(response)
    if isinstance(result, list):
        valid_keys = {t["key"] for t in topic_list}
        return [k for k in result if k in valid_keys]

    return []


def infer_locations(title, content):
    """
    Infer geographic locations from article text.
    Returns list of location dicts.
    """
    prompt = f"""Identify the US geographic locations mentioned in this article.

Return ONLY a JSON array of location objects:
[{{"state": "<lowercase state name>", "place": "<city or county if specific>", "relevance": "primary|mentioned"}}]

Rules:
- "primary": the article is mainly about this place
- "mentioned": the place is referenced but isn't the main focus
- Use lowercase state names (e.g., "wyoming", "california")
- If the article is about federal/national policy with no specific state, use "nationwide"
- Be careful with ambiguous city names: Evanston could be WY or IL, Portland could be OR or ME — use context clues

Article:
Title: {title}
Content: {(content or '')[:1500]}"""

    response = _call_ollama(prompt)
    result = _parse_json(response)
    if isinstance(result, list):
        # Validate entries
        valid = []
        for loc in result:
            if isinstance(loc, dict) and "state" in loc:
                valid.append({
                    "state": loc["state"].lower().strip(),
                    "place": loc.get("place"),
                    "relevance": loc.get("relevance", "primary"),
                })
        return valid if valid else [{"state": "nationwide", "relevance": "primary"}]

    return [{"state": "nationwide", "relevance": "primary"}]


def summarize_article(content, max_words=500):
    """
    Summarize article content.
    Returns summary string.
    """
    if not content or len(content) < 200:
        return content or ""

    prompt = f"""Summarize this article in {max_words} words or fewer. Focus on the key facts, claims, and any positions taken by stakeholders.

Article:
{content[:3000]}"""

    response = _call_ollama(prompt)
    if response:
        # Strip thinking tags
        import re
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL).strip()
        return response

    return content[:1000]
