"""
Prometheus Resistance Dashboard — Shared Utilities
Common functions used across multiple modules: config loading,
path resolution, API key resolution, and Anthropic client initialization.
"""

import logging
import os
from pathlib import Path

import yaml

APP_NAME = "Prometheus Resistance Dashboard"
APP_SLUG = "resistance_dashboard"

logger = logging.getLogger(APP_SLUG)

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
APIKEY_PATH = PROJECT_ROOT.parent / "apikey.txt"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_DB_PATH = DATA_DIR / f"{APP_SLUG}.db"
DEFAULT_LOG_PATH = LOG_DIR / f"{APP_SLUG}.log"
REPORT_DOWNLOAD_NAME = f"{APP_SLUG}_report.html"


def resolve_db_path(db_path=None):
    """Resolve the canonical database path."""
    if db_path is not None:
        return Path(db_path)
    return DEFAULT_DB_PATH


def prepare_log_path():
    """Resolve the canonical log path."""
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DEFAULT_LOG_PATH


def normalize_config(config):
    """
    Handle backward compatibility between old and new config formats.
    Old format: states + global_keywords
    New format: priority_states + nationwide
    """
    if "nationwide" in config and "priority_states" in config:
        return config
    # Auto-migrate old format in memory
    if "states" in config and "priority_states" not in config:
        config["priority_states"] = config.pop("states")
    if "global_keywords" in config and "nationwide" not in config:
        config["nationwide"] = {
            "keywords": config.pop("global_keywords"),
            "sweep_queries": [],
            "thematic_queries": [],
        }
    return config


def load_config():
    """Load the YAML configuration file and normalize to current format."""
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
    return normalize_config(config)


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
