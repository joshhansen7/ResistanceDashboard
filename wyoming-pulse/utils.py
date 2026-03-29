"""
Wyoming Pulse — Utility Functions
Shared text processing helpers used across multiple modules.
"""

import json
import logging
import re

logger = logging.getLogger("wyoming_pulse.utils")


def strip_markdown_fences(text):
    """Remove markdown code fences (```json ... ```) from text."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text


def parse_json_response(text):
    """
    Parse a JSON object from a Claude response.
    Handles markdown fences and returns the parsed dict, or None on failure.
    """
    text = strip_markdown_fences(text)
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        logger.warning("Expected JSON object, got %s", type(result).__name__)
        return None
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse JSON response: %s", e)
        logger.debug("Raw response: %s", text[:500])
        return None


def clean_html(text):
    """Strip HTML tags from text and normalize whitespace."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean).strip()


def normalize_for_comparison(text):
    """Normalize text for fuzzy title comparison (lowercase, alphanumeric only)."""
    t = re.sub(r"[^a-z0-9\s]", "", text.lower())
    return re.sub(r"\s+", " ", t).strip()
