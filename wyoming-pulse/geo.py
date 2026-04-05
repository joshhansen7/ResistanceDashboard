"""
Prometheus — Geographic Normalization
Maps freeform place names to county FIPS codes using a lookup table.
Provides state reference data (name, abbreviation, FIPS) for all 50 US states.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("wyoming_pulse.geo")

DATA_DIR = Path(__file__).parent / "data"
PLACE_TO_COUNTY_PATH = DATA_DIR / "place_to_county.json"
US_STATES_PATH = DATA_DIR / "us_states.json"

_place_cache = None
_states_cache = None


def _load_place_table():
    """Load the place-to-county lookup table."""
    global _place_cache
    if _place_cache is None:
        if PLACE_TO_COUNTY_PATH.exists():
            _place_cache = json.loads(PLACE_TO_COUNTY_PATH.read_text(encoding="utf-8"))
        else:
            _place_cache = {}
            logger.warning("place_to_county.json not found at %s", PLACE_TO_COUNTY_PATH)
    return _place_cache


def _load_us_states():
    """Load the US states reference data."""
    global _states_cache
    if _states_cache is None:
        if US_STATES_PATH.exists():
            _states_cache = json.loads(US_STATES_PATH.read_text(encoding="utf-8"))
        else:
            _states_cache = {}
            logger.warning("us_states.json not found at %s", US_STATES_PATH)
    return _states_cache


def get_state_info(state_key):
    """
    Get reference info for a US state.
    Returns {"name": "Wyoming", "abbr": "WY", "fips": "56"} or None.
    """
    states = _load_us_states()
    return states.get(state_key.lower().strip()) if state_key else None


def get_all_states():
    """Return the full US states reference dict."""
    return _load_us_states()


def get_state_abbr(state_key):
    """Get the abbreviation for a state key, or uppercase first 2 chars as fallback."""
    info = get_state_info(state_key)
    if info:
        return info["abbr"]
    if state_key:
        return state_key.upper()[:2]
    return "??"


def get_state_fips(state_key):
    """Get the FIPS code for a state key."""
    info = get_state_info(state_key)
    return info["fips"] if info else None


def normalize_state_key(state_key):
    """Normalize a state key: underscores to spaces, lowercase, validate.
    Returns the canonical state key if valid US state, or None.
    Also accepts 'nationwide' and 'district of columbia'.
    """
    if not state_key:
        return None
    key = state_key.lower().strip().replace("_", " ")
    if key in ("nationwide", "other", "statewide"):
        return key
    if get_state_info(key):
        return key
    return None


def is_valid_us_state(state_key):
    """Check if a state key is a valid US state (or nationwide)."""
    return normalize_state_key(state_key) is not None


def infer_state_from_text(title, summary=""):
    """
    Scan article title and summary for US state names and abbreviations.
    Returns the best-matching state key (e.g. "maine") or "nationwide".

    Full state names are matched case-insensitively.
    Two-letter abbreviations are matched only as standalone uppercase words
    to avoid false positives (e.g. "IN", "OR", "ME" in normal prose).
    """
    states = _load_us_states()
    if not states:
        return "nationwide"

    # Build lookup structures
    name_to_key = {}       # "maine" -> "maine", "new york" -> "new york"
    abbr_to_key = {}       # "ME" -> "maine", "NY" -> "new york"
    for key, info in states.items():
        name_to_key[info["name"].lower()] = key
        abbr_to_key[info["abbr"]] = key

    combined = f"{title} {summary}"
    counts = {}

    # Match full state names (case-insensitive, word-boundary)
    for name_lower, key in name_to_key.items():
        pattern = r"\b" + re.escape(name_lower) + r"\b"
        hits = len(re.findall(pattern, combined, re.IGNORECASE))
        if hits:
            counts[key] = counts.get(key, 0) + hits

    # Match abbreviations — only uppercase standalone words in original text
    # Use a stricter pattern to avoid matching common English words
    for abbr, key in abbr_to_key.items():
        # Skip very ambiguous 2-letter abbreviations that are common words
        # even in uppercase contexts (headlines often capitalize everything)
        if abbr in ("IN", "OR", "OH", "OK", "HI", "ME"):
            # For these, only match if preceded/followed by state-like context
            # e.g. "in ME" won't match, but "Portland, ME" or "ME legislature" may
            # We rely on the full-name match for these states instead
            continue
        pattern = r"(?<![A-Za-z])" + re.escape(abbr) + r"(?![A-Za-z])"
        hits = len(re.findall(pattern, combined))
        if hits:
            counts[key] = counts.get(key, 0) + hits

    if not counts:
        return "nationwide"

    # Return the state with the most mentions
    best = max(counts, key=counts.get)
    return best


def normalize_location(place, state):
    """
    Look up a place name in the county table.
    Returns {"fips": "56041", "county": "Uinta County"} or None.
    """
    if not place or not state:
        return None
    key = f"{place.lower().strip()}, {state.lower().strip()}"
    table = _load_place_table()
    result = table.get(key)
    if not result:
        logger.debug("Unknown place: %s — add to place_to_county.json", key)
    return result


def normalize_locations(locations):
    """
    Take a list of location dicts from Claude and add county_fips/county_name.
    Modifies entries in-place and returns the list.

    Input:  [{"state": "wyoming", "place": "Evanston", "relevance": "primary"}]
    Output: [{"state": "wyoming", "place": "Evanston", "county_fips": "56041",
              "county_name": "Uinta County", "relevance": "primary"}]
    """
    for loc in locations:
        place = loc.get("place")
        state = loc.get("state")
        if place and state:
            county = normalize_location(place, state)
            if county:
                loc["county_fips"] = county["fips"]
                loc["county_name"] = county["county"]
            else:
                loc.setdefault("county_fips", None)
                loc.setdefault("county_name", None)
        else:
            loc.setdefault("county_fips", None)
            loc.setdefault("county_name", None)
    return locations


def list_unmapped_places(conn):
    """
    Scan all articles for places in locations_json that don't have FIPS mappings.
    Returns list of {"place": ..., "state": ..., "count": ...}.
    """
    rows = conn.execute(
        "SELECT locations_json FROM articles WHERE locations_json IS NOT NULL"
    ).fetchall()

    unmapped = {}
    for row in rows:
        try:
            locs = json.loads(row["locations_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        for loc in locs:
            place = loc.get("place")
            state = loc.get("state")
            if place and state and not loc.get("county_fips"):
                key = f"{place}, {state}"
                unmapped[key] = unmapped.get(key, 0) + 1

    return sorted(
        [{"place_state": k, "count": v} for k, v in unmapped.items()],
        key=lambda x: x["count"],
        reverse=True,
    )


def backfill_fips(conn):
    """
    Re-run FIPS normalization on all articles with locations_json.
    Updates county_fips/county_name where mappings exist.
    Returns count of articles updated.
    """
    rows = conn.execute(
        "SELECT id, locations_json FROM articles WHERE locations_json IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        try:
            locs = json.loads(row["locations_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        changed = False
        for loc in locs:
            place = loc.get("place")
            state = loc.get("state")
            if place and state and not loc.get("county_fips"):
                county = normalize_location(place, state)
                if county:
                    loc["county_fips"] = county["fips"]
                    loc["county_name"] = county["county"]
                    changed = True

        if changed:
            conn.execute(
                "UPDATE articles SET locations_json = ? WHERE id = ?",
                (json.dumps(locs), row["id"]),
            )
            updated += 1

    conn.commit()
    return updated
