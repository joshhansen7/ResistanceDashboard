"""
Wyoming Pulse — Shared Utilities
Common functions used across multiple modules: config loading,
API key resolution, and Anthropic client initialization.
"""

import logging
import os

import yaml
from pathlib import Path

logger = logging.getLogger("wyoming_pulse")

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
APIKEY_PATH = PROJECT_ROOT.parent / "apikey.txt"


def load_config():
    """Load the YAML configuration file."""
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def get_api_key():
    """
    Load the Anthropic API key.
    Checks ANTHROPIC_API_KEY env var first, then falls back to apikey.txt.
    Returns the key string or None.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and APIKEY_PATH.exists():
        api_key = APIKEY_PATH.read_text().strip()
    return api_key or None


def get_anthropic_client():
    """
    Initialize and return an Anthropic client.
    Returns None if the API key is not available or the package is missing.
    """
    api_key = get_api_key()
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set and apikey.txt not found")
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.error("anthropic package not installed. Run: pip install anthropic")
        return None
