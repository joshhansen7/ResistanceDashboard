"""
Prometheus — Geographic Normalization
Maps freeform place names to county FIPS codes using a lookup table,
with embedded-county extraction, regional name resolution, and
Nominatim + FCC geocoding fallback with auto-caching.
"""

import json
import logging
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import requests as _requests

logger = logging.getLogger("resistance_dashboard.geo")

DATA_DIR = Path(__file__).parent / "data"
PLACE_TO_COUNTY_PATH = DATA_DIR / "place_to_county.json"
US_STATES_PATH = DATA_DIR / "us_states.json"

_place_cache = None
_states_cache = None
_cache_dirty = False
_defer_flush = False  # When True, suppress per-geocode flushes (batch backfill)

# Rate-limit tracking for Nominatim (max 1 req/sec per their policy)
_last_nominatim_call = 0.0

_SPECIAL_SCOPE_KEYS = ("nationwide", "other", "statewide", "international")
_US_NATIONAL_PATTERNS = (
    r"\bu\.?s\.?\b",
    r"\bunited states\b",
    r"\bamerican\b",
    r"\bfederal\b",
    r"\bnationwide\b",
    r"\bacross the country\b",
    r"\bacross the united states\b",
    r"\bdomestic\b",
)
_INTERNATIONAL_PLACE_PATTERNS = (
    ("Canada", (r"\bcanada\b", r"\bcanadian\b")),
    ("Mexico", (r"\bmexico\b", r"\bmexican\b")),
    ("Brazil", (r"\bbrazil\b", r"\bbrazilian\b")),
    ("Argentina", (r"\bargentina\b", r"\bargentinian\b", r"\bargentine\b")),
    ("Chile", (r"\bchile\b", r"\bchilean\b")),
    ("Peru", (r"\bperu\b", r"\bperuvian\b")),
    ("Colombia", (r"\bcolombia\b", r"\bcolombian\b")),
    ("United Kingdom", (r"\bunited kingdom\b", r"\buk\b", r"\bbritain\b", r"\bbritish\b")),
    ("England", (r"\bengland\b", r"\benglish\b")),
    ("Scotland", (r"\bscotland\b", r"\bscottish\b")),
    ("Ireland", (r"\bireland\b", r"\birish\b")),
    ("France", (r"\bfrance\b", r"\bfrench\b")),
    ("Germany", (r"\bgermany\b", r"\bgerman\b")),
    ("Netherlands", (r"\bnetherlands\b", r"\bdutch\b")),
    ("Belgium", (r"\bbelgium\b", r"\bbelgian\b")),
    ("Switzerland", (r"\bswitzerland\b", r"\bswiss\b")),
    ("Austria", (r"\baustria\b", r"\baustrian\b")),
    ("Italy", (r"\bitaly\b", r"\bitalian\b")),
    ("Spain", (r"\bspain\b", r"\bspanish\b")),
    ("Portugal", (r"\bportugal\b", r"\bportuguese\b")),
    ("Poland", (r"\bpoland\b", r"\bpolish\b")),
    ("Czech Republic", (r"\bczech republic\b", r"\bczechia\b", r"\bczech\b")),
    ("Sweden", (r"\bsweden\b", r"\bswedish\b")),
    ("Norway", (r"\bnorway\b", r"\bnorwegian\b")),
    ("Denmark", (r"\bdenmark\b", r"\bdanish\b")),
    ("Finland", (r"\bfinland\b", r"\bfinnish\b")),
    ("Ukraine", (r"\bukraine\b", r"\bukrainian\b")),
    ("Russia", (r"\brussia\b", r"\brussian\b")),
    ("Turkey", (r"\bturkey\b", r"\bturkish\b")),
    ("Israel", (r"\bisrael\b", r"\bisraeli\b")),
    ("Saudi Arabia", (r"\bsaudi arabia\b", r"\bsaudi\b")),
    ("United Arab Emirates", (r"\bunited arab emirates\b", r"\buae\b", r"\bemirati\b")),
    ("Qatar", (r"\bqatar\b", r"\bqatari\b")),
    ("India", (r"\bindia\b", r"\bindian\b")),
    ("China", (r"\bchina\b", r"\bchinese\b")),
    ("Taiwan", (r"\btaiwan\b", r"\btaiwanese\b")),
    ("Japan", (r"\bjapan\b", r"\bjapanese\b")),
    ("South Korea", (r"\bsouth korea\b", r"\bkorean\b")),
    ("Singapore", (r"\bsingapore\b", r"\bsingaporean\b")),
    ("Malaysia", (r"\bmalaysia\b", r"\bmalaysian\b")),
    ("Indonesia", (r"\bindonesia\b", r"\bindonesian\b")),
    ("Vietnam", (r"\bvietnam\b", r"\bvietnamese\b")),
    ("Thailand", (r"\bthailand\b", r"\bthai\b")),
    ("Philippines", (r"\bphilippines\b", r"\bphilippine\b", r"\bfilipino\b")),
    ("Australia", (r"\baustralia\b", r"\baustralian\b")),
    ("New Zealand", (r"\bnew zealand\b", r"\bnew zealander\b")),
    ("South Africa", (r"\bsouth africa\b", r"\bsouth african\b")),
    ("Nigeria", (r"\bnigeria\b", r"\bnigerian\b")),
    ("Kenya", (r"\bkenya\b", r"\bkenyan\b")),
    ("European Union", (r"\beuropean union\b",)),
)


def normalize_fips(fips):
    """Normalize a FIPS code to canonical 5-digit zero-padded form."""
    if fips is None:
        return None
    s = str(fips).strip()
    if not s:
        return None
    return s.zfill(5)

# ═══════════════════════════════════════════════════════════════
#  STATIC DATA LOADING
# ═══════════════════════════════════════════════════════════════

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


def reload_place_table():
    """Force reload the place table from disk (clears cache)."""
    global _place_cache, _cache_dirty
    _place_cache = None
    _cache_dirty = False
    return _load_place_table()


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


# ═══════════════════════════════════════════════════════════════
#  STATE REFERENCE HELPERS
# ═══════════════════════════════════════════════════════════════

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


def get_state_counties(state_key):
    """Return sorted county choices for a state from the place lookup table."""
    normalized_state = normalize_state_key(state_key)
    if not normalized_state or normalized_state in ("nationwide", "other", "statewide", "international"):
        return []

    counties = {}
    for key, value in _load_place_table().items():
        if ", " not in key:
            continue
        _, key_state = key.rsplit(", ", 1)
        if key_state != normalized_state:
            continue
        county_name = value.get("county")
        county_fips = normalize_fips(value.get("fips"))
        if county_name and county_fips:
            counties[county_fips] = county_name

    return [
        {"fips": fips, "county": county}
        for fips, county in sorted(counties.items(), key=lambda item: item[1])
    ]


def resolve_county_fips(state_key, county_fips):
    """Resolve a county FIPS to county metadata within a state."""
    target = normalize_fips(county_fips)
    if not target:
        return None
    for county in get_state_counties(state_key):
        if county["fips"] == target:
            return county
    return None


def normalize_state_key(state_key):
    """Normalize a state key: underscores to spaces, lowercase, validate.
    Returns the canonical state key if valid US state, or None.
    Also accepts special dashboard scope keys such as 'nationwide' and 'international'.
    """
    if not state_key:
        return None
    key = state_key.lower().strip().replace("_", " ")
    if key in _SPECIAL_SCOPE_KEYS:
        return key
    if get_state_info(key):
        return key
    return None


def is_valid_us_state(state_key):
    """Check if a key maps to a real US state (excluding special non-state scopes)."""
    key = normalize_state_key(state_key)
    return bool(key and get_state_info(key))


def infer_international_place(title="", summary="", content=""):
    """Return a canonical non-US place/country hint when one is clearly present."""
    combined = f"{title} {summary} {content}"
    for place, patterns in _INTERNATIONAL_PLACE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                return place
    return None


def build_international_location(place=None, relevance="primary"):
    """Build a normalized international location entry for articles outside the US."""
    entry = {"state": "international", "relevance": relevance}
    if place and str(place).strip().lower() != "international":
        clean_place = str(place).strip()
        entry["place"] = clean_place
        entry["town"] = clean_place
        entry["source_place"] = clean_place
    return entry


def infer_state_from_text(title, summary="", content=""):
    """
    Scan article title and summary for US state names and abbreviations.
    Returns the best-matching US state key, or one of:
      - "nationwide" for broad US scope with no single-state focus
      - "international" for clearly non-US coverage

    Full state names are matched case-insensitively.
    Two-letter abbreviations are matched only as standalone uppercase words
    to avoid false positives (e.g. "IN", "OR", "ME" in normal prose).
    """
    states = _load_us_states()
    if not states:
        if infer_international_place(title, summary, content):
            return "international"
        return "nationwide"

    # Build lookup structures
    name_to_key = {}       # "maine" -> "maine", "new york" -> "new york"
    abbr_to_key = {}       # "ME" -> "maine", "NY" -> "new york"
    for key, info in states.items():
        name_to_key[info["name"].lower()] = key
        abbr_to_key[info["abbr"]] = key

    combined = f"{title} {summary} {content}"
    counts = {}

    # Match full state names (case-insensitive, word-boundary)
    for name_lower, key in name_to_key.items():
        pattern = r"\b" + re.escape(name_lower) + r"\b"
        hits = len(re.findall(pattern, combined, re.IGNORECASE))
        if hits:
            counts[key] = counts.get(key, 0) + hits

    # Match abbreviations — only uppercase standalone words in original text
    for abbr, key in abbr_to_key.items():
        if abbr in ("IN", "OR", "OH", "OK", "HI", "ME"):
            continue
        pattern = r"(?<![A-Za-z])" + re.escape(abbr) + r"(?![A-Za-z])"
        hits = len(re.findall(pattern, combined))
        if hits:
            counts[key] = counts.get(key, 0) + hits

    if not counts:
        for pattern in _US_NATIONAL_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                return "nationwide"
        if infer_international_place(title, summary, content):
            return "international"
        return "nationwide"

    best = max(counts, key=counts.get)
    return best


# ═══════════════════════════════════════════════════════════════
#  REGIONAL / INFORMAL PLACE NAMES
# ═══════════════════════════════════════════════════════════════

# Well-known informal regions mapped to a representative county.
_REGIONAL_PLACES = {
    "northern virginia, virginia": {"fips": "51059", "county": "Fairfax County"},
    "nova, virginia": {"fips": "51059", "county": "Fairfax County"},
    "silicon valley, california": {"fips": "6085", "county": "Santa Clara County"},
    "san francisco bay area, california": {"fips": "6075", "county": "San Francisco County"},
    "bay area, california": {"fips": "6075", "county": "San Francisco County"},
    "inland empire, california": {"fips": "6071", "county": "San Bernardino County"},
    "imperial valley, california": {"fips": "6025", "county": "Imperial County"},
    "permian basin, texas": {"fips": "48329", "county": "Midland County"},
    "dallas-fort worth, texas": {"fips": "48113", "county": "Dallas County"},
    "dfw, texas": {"fips": "48113", "county": "Dallas County"},
    "research triangle, north carolina": {"fips": "37183", "county": "Wake County"},
    "hampton roads, virginia": {"fips": "51810", "county": "Virginia Beach city"},
    "puget sound, washington": {"fips": "53033", "county": "King County"},
    "twin cities, minnesota": {"fips": "27053", "county": "Hennepin County"},
    "tri-cities, washington": {"fips": "53005", "county": "Benton County"},
    "lehigh valley, pennsylvania": {"fips": "42077", "county": "Lehigh County"},
    "pittsburgh region, pennsylvania": {"fips": "42003", "county": "Allegheny County"},
    "thumb, michigan": {"fips": "26157", "county": "Tuscola County"},
}

# Directional prefix patterns that are too vague when combined with a state name.
_DIRECTIONAL_PREFIXES = re.compile(
    r"^(North|South|East|West|Central|Northern|Southern|Eastern|Western|"
    r"Southwest|Northwest|Southeast|Northeast|Upstate|Downstate)\s+",
    re.IGNORECASE,
)


def _is_vague_regional(place, state):
    """Return True if the place is a vague directional+state region like 'West Texas'."""
    m = _DIRECTIONAL_PREFIXES.match(place)
    if not m:
        return False
    remainder = place[m.end():].strip().lower()
    # "Northern Michigan" → remainder = "michigan", same as state → vague
    if remainder == state.lower():
        return True
    # Also check state full name: "West Virginia" is a real state, not vague
    info = get_state_info(state)
    if info and remainder == info["name"].lower():
        # But "West Virginia" as place in state "virginia" IS vague
        # "West Virginia" as place in state "west virginia" is the state itself
        if normalize_state_key(place):
            return False  # It's a real state name
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  DIACRITIC / UNICODE NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def _strip_diacritics(text):
    """Remove diacritical marks: 'Doña Ana' → 'Dona Ana'."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ═══════════════════════════════════════════════════════════════
#  COUNTY EXTRACTION & RESOLUTION
# ═══════════════════════════════════════════════════════════════

def _extract_county_from_place(place, state):
    """
    Extract an embedded county name from a compound place string.
    E.g. "Saline Township, Washtenaw County" → look up "washtenaw county, michigan".
    Returns county dict or None.
    """
    # Match "Something, X County" pattern
    m = re.match(r"^(.+),\s+([\w\s\-']+County)$", place, re.IGNORECASE)
    if not m:
        return None
    county_name = m.group(2).strip()
    key = f"{county_name.lower()}, {state.lower().strip()}"
    table = _load_place_table()
    result = table.get(key)
    if result:
        return result
    # Try with diacritics stripped
    stripped_key = f"{_strip_diacritics(county_name).lower()}, {state.lower().strip()}"
    if stripped_key != key:
        result = table.get(stripped_key)
        if result:
            return result
    return None


def _resolve_county_name(county_name, state):
    """
    Resolve a county name (like "Washtenaw County" or "Doña Ana County")
    to its FIPS code. Handles diacritics and missing "County" suffix.
    Returns county dict or None.
    """
    if not county_name or not state:
        return None
    table = _load_place_table()
    state_lower = state.lower().strip()

    # Ensure "County" suffix is present for lookup
    cn = county_name.strip()
    if not cn.lower().endswith(" county"):
        cn = cn + " County"

    # Direct lookup
    key = f"{cn.lower()}, {state_lower}"
    result = table.get(key)
    if result:
        return result

    # Diacritic-stripped lookup
    stripped_key = f"{_strip_diacritics(cn).lower()}, {state_lower}"
    if stripped_key != key:
        result = table.get(stripped_key)
        if result:
            return result

    return None


# ═══════════════════════════════════════════════════════════════
#  GEOCODING FALLBACK (Nominatim + FCC)
# ═══════════════════════════════════════════════════════════════

def _geocode_place(place, state):
    """
    Geocode a place name via Nominatim (→ lat/lon) then FCC Area API (→ county FIPS).
    Returns {"fips": "...", "county": "..."} or None.
    Rate-limited to 1 req/sec for Nominatim.
    """
    global _last_nominatim_call
    info = get_state_info(state)
    if not info:
        return None
    state_name = info["name"]
    expected_state_fips = info["fips"]

    # ── Tier 1: Nominatim → lat/lon ──
    try:
        # Rate limit
        elapsed = time.time() - _last_nominatim_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        query = f"{place}, {state_name}, USA"
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": "1"},
            headers={"User-Agent": "PrometheusResearch/1.0"},
            timeout=5,
        )
        _last_nominatim_call = time.time()

        if resp.status_code != 200 or not resp.json():
            logger.debug("Nominatim: no result for %s", query)
            return None

        data = resp.json()[0]
        lat, lon = float(data["lat"]), float(data["lon"])
    except Exception as e:
        logger.debug("Nominatim error for %s, %s: %s", place, state, e)
        return None

    # ── Tier 2: FCC Area API → county FIPS ──
    try:
        resp = _requests.get(
            "https://geo.fcc.gov/api/census/area",
            params={"lat": lat, "lon": lon, "format": "json"},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.debug("FCC API: HTTP %d for %s,%s", resp.status_code, lat, lon)
            return None

        results = resp.json().get("results", [])
        if not results:
            logger.debug("FCC API: no results for %s,%s", lat, lon)
            return None

        fcc = results[0]
        county_fips = fcc.get("county_fips", "")
        county_name = fcc.get("county_name", "")
        state_fips = fcc.get("state_fips", "")

        # Validate: geocoded result must be in the expected state
        if state_fips != expected_state_fips:
            logger.debug(
                "Geocode state mismatch for %s, %s: got state FIPS %s, expected %s",
                place, state, state_fips, expected_state_fips,
            )
            return None

        if county_fips and county_name:
            # Normalize county name to include "County" if it doesn't
            if not county_name.endswith(" County") and "city" not in county_name.lower():
                county_name = county_name + " County"
            return {"fips": normalize_fips(county_fips), "county": county_name}

    except Exception as e:
        logger.debug("FCC API error for %s,%s: %s", lat, lon, e)

    return None


# ═══════════════════════════════════════════════════════════════
#  AUTO-CACHING
# ═══════════════════════════════════════════════════════════════

def _cache_geocoded(key, result):
    """Add a geocoded result to the in-memory cache and mark dirty."""
    global _cache_dirty
    table = _load_place_table()
    normalized = dict(result)
    if "fips" in normalized:
        normalized["fips"] = normalize_fips(normalized["fips"])
    table[key] = normalized
    _cache_dirty = True


def flush_place_cache():
    """Write the in-memory place cache to disk (atomic)."""
    global _cache_dirty
    if not _cache_dirty or _place_cache is None:
        return
    try:
        tmp_path = PLACE_TO_COUNTY_PATH.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(_place_cache, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(PLACE_TO_COUNTY_PATH))
        _cache_dirty = False
        logger.info("Flushed place cache (%d entries) to %s", len(_place_cache), PLACE_TO_COUNTY_PATH)
    except Exception as e:
        logger.error("Failed to flush place cache: %s", e)


@contextmanager
def deferred_place_cache_flush():
    """Batch cache writes so repeated geocodes only flush once at the end."""
    global _defer_flush
    prev = _defer_flush
    _defer_flush = True
    try:
        yield
    finally:
        _defer_flush = prev
        if not _defer_flush:
            flush_place_cache()


# ═══════════════════════════════════════════════════════════════
#  MAIN NORMALIZATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def normalize_location(place, state, county_hint=None):
    """
    Look up a place name and return its county FIPS mapping.
    Returns {"fips": "56041", "county": "Uinta County"} or None.

    Resolution cascade:
    1. If county_hint provided (new prompt format), resolve directly
    2. Direct table lookup of place
    3. Extract embedded county from compound place ("X, Y County")
    4. Regional names dict
    5. Skip vague directional regions
    6. Geocode via Nominatim + FCC (auto-cached)
    """
    if not state:
        return None

    # Try static (non-network) resolution first
    result = _resolve_static(place, state, county_hint=county_hint)
    if result:
        return _normalize_result_fips(result)

    if not place:
        return None

    place_stripped = place.strip()
    state_lower = state.lower().strip()

    # Skip vague directional regions before hitting the network
    if _is_vague_regional(place_stripped, state):
        logger.debug("Vague regional place, skipping: %s, %s", place, state)
        return None

    # Geocode via Nominatim + FCC
    key = f"{place_stripped.lower()}, {state_lower}"
    result = _geocode_place(place_stripped, state)
    if result:
        _cache_geocoded(key, result)
        # Flush immediately for single-article flow; backfill defers to end
        if not _defer_flush:
            flush_place_cache()
        return _normalize_result_fips(result)

    logger.debug("Unresolvable place: %s, %s", place, state)
    return None


def _normalize_result_fips(result):
    """Return a copy of a county-result dict with its fips zero-padded."""
    if not result:
        return result
    out = dict(result)
    if "fips" in out:
        out["fips"] = normalize_fips(out["fips"])
    return out


def _location_place(loc, include_raw=False):
    """Return the best available place-like label from a location dict."""
    if not loc:
        return None
    return (
        loc.get("town")
        or loc.get("place")
        or (loc.get("raw_place") if include_raw else None)
    )


def _source_place(loc):
    """Return the original extracted location phrase when available."""
    if not loc:
        return None
    return loc.get("source_place") or loc.get("raw_place") or _location_place(loc, include_raw=False) or loc.get("county")


def _ensure_source_place(loc):
    """Populate source_place once from the extracted phrase."""
    source = _source_place(loc)
    if source and loc.get("source_place") != source:
        loc["source_place"] = source
        return True
    return False


def get_primary_location(locations):
    """Return the first primary location (or first location) from a list."""
    if not locations or not isinstance(locations, list):
        return None
    return next((loc for loc in locations if loc.get("relevance") == "primary"), locations[0])


def derive_article_scope(locations):
    """Return backward-compatible (state, location_relevance) fields from normalized locations."""
    primary = get_primary_location(locations)
    if not primary:
        return "other", "statewide"

    state = normalize_state_key(primary.get("state")) or primary.get("state") or "other"
    if state == "nationwide":
        return state, "nationwide"
    if state == "international":
        place = _location_place(primary, include_raw=True) or primary.get("county_name")
        return state, place or "international"

    place = _location_place(primary)
    if place:
        return state, place
    if primary.get("county_name"):
        return state, primary["county_name"]
    return state, "statewide"


def derive_location_display(locations, fallback_state=None, fallback_location=None):
    """Return the user-facing primary geography label: county, General, or Nationwide."""
    primary = get_primary_location(locations)
    if primary:
        state = normalize_state_key(primary.get("state")) or fallback_state
        if state == "nationwide":
            return "Nationwide"
        if state == "international":
            return _location_place(primary, include_raw=True) or primary.get("county_name") or "International"
        if primary.get("county_name"):
            return primary["county_name"]
        return "General"

    if normalize_state_key(fallback_state) == "nationwide" or str(fallback_location or "").lower() == "nationwide":
        return "Nationwide"
    if normalize_state_key(fallback_state) == "international" or str(fallback_location or "").lower() == "international":
        return "International"
    return "General"


def _collapse_primary_location_to_statewide(loc):
    """Downgrade an unresolved primary place to statewide while preserving the raw label."""
    raw_place = _source_place(loc) or loc.get("raw_county")
    changed = _ensure_source_place(loc)
    if raw_place and loc.get("raw_place") != raw_place:
        loc["raw_place"] = raw_place
        changed = True
    if loc.get("place") is not None:
        loc["place"] = None
        changed = True
    if loc.get("town") is not None:
        loc["town"] = None
        changed = True
    if loc.get("county_fips") is not None:
        loc["county_fips"] = None
        changed = True
    if loc.get("county_name") is not None:
        loc["county_name"] = None
        changed = True
    if loc.get("normalization_status") != "statewide_fallback":
        loc["normalization_status"] = "statewide_fallback"
        changed = True
    return changed


def set_primary_geography(locations, state, scope, county_fips=None):
    """Apply a normalized geography override to the primary location."""
    if not isinstance(locations, list):
        locations = []

    normalized_state = normalize_state_key(state) or state or "other"
    primary = get_primary_location(locations)
    if primary is None:
        primary = {"relevance": "primary"}
        locations.append(primary)

    primary["state"] = normalized_state

    if normalized_state == "nationwide":
        primary["place"] = "nationwide"
        primary["town"] = "nationwide"
        primary["county_fips"] = None
        primary["county_name"] = None
        primary.pop("raw_place", None)
        primary.pop("source_place", None)
        primary.pop("normalization_status", None)
        return locations

    if normalized_state == "international":
        source_place = _source_place(primary)
        if source_place and source_place.lower() != "international":
            primary["place"] = source_place
            primary["town"] = source_place
            primary["source_place"] = source_place
        else:
            primary["place"] = None
            primary["town"] = None
        primary["county_fips"] = None
        primary["county_name"] = None
        primary.pop("normalization_status", None)
        return locations

    if scope == "county":
        county = resolve_county_fips(normalized_state, county_fips)
        if not county:
            raise ValueError("Unknown county for selected state")
        _ensure_source_place(primary)
        raw_place = _source_place(primary)
        if raw_place and raw_place != county["county"]:
            primary["raw_place"] = raw_place
        if not primary.get("place") and raw_place and raw_place != county["county"]:
            primary["place"] = raw_place
        if "town" in primary and not primary.get("town") and raw_place and raw_place != county["county"]:
            primary["town"] = raw_place
        primary["county"] = county["county"]
        primary["county_fips"] = county["fips"]
        primary["county_name"] = county["county"]
        primary.pop("normalization_status", None)
        return locations

    _collapse_primary_location_to_statewide(primary)
    return locations


def normalize_location_entry(loc, geocode=True):
    """Normalize a single location dict in-place. Returns True when fields changed."""
    if not isinstance(loc, dict):
        return False

    changed = _ensure_source_place(loc)
    place = _location_place(loc, include_raw=True)
    county_hint = loc.get("county")
    state = loc.get("state")

    if place or county_hint:
        if geocode:
            county = normalize_location(place, state, county_hint=county_hint)
        else:
            county = _resolve_static(place, state, county_hint)
        if county:
            if place:
                if loc.get("place") != place:
                    loc["place"] = place
                    changed = True
                if "town" in loc and loc.get("town") != place:
                    loc["town"] = place
                    changed = True
            if loc.get("county_fips") != county["fips"]:
                loc["county_fips"] = county["fips"]
                changed = True
            if loc.get("county_name") != county["county"]:
                loc["county_name"] = county["county"]
                changed = True
            if loc.pop("normalization_status", None) is not None:
                changed = True
        else:
            if loc.get("county_fips") is not None:
                loc["county_fips"] = None
                changed = True
            if loc.get("county_name") is not None:
                loc["county_name"] = None
                changed = True
    else:
        if loc.get("county_fips") is not None:
            loc["county_fips"] = None
            changed = True
        if loc.get("county_name") is not None:
            loc["county_name"] = None
            changed = True

    normalized_state = normalize_state_key(state)
    if normalized_state == "international":
        if loc.get("county_fips") is not None:
            loc["county_fips"] = None
            changed = True
        if loc.get("county_name") is not None:
            loc["county_name"] = None
            changed = True
        if loc.pop("normalization_status", None) is not None:
            changed = True
        return changed

    if (
        loc.get("relevance", "primary") == "primary"
        and normalized_state not in (None, "nationwide", "other", "statewide")
        and (_location_place(loc, include_raw=True) or county_hint)
        and not loc.get("county_fips")
    ):
        changed = _collapse_primary_location_to_statewide(loc) or changed

    return changed


def normalize_locations(locations):
    """
    Take a list of location dicts from Claude and add county_fips/county_name.
    Modifies entries in-place and returns the list.

    Handles both old format (place only) and new format (town + county).

    Input:  [{"state": "michigan", "town": "Saline Township", "county": "Washtenaw County"}]
    Output: [{"state": "michigan", "town": "Saline Township", "county": "Washtenaw County",
              "place": "Saline Township", "county_fips": "26161", "county_name": "Washtenaw County"}]
    """
    for loc in locations:
        normalize_location_entry(loc, geocode=True)
        loc.setdefault("county_fips", None)
        loc.setdefault("county_name", None)
    return locations


# ═══════════════════════════════════════════════════════════════
#  DIAGNOSTICS & BACKFILL
# ═══════════════════════════════════════════════════════════════

def list_unmapped_places(conn):
    """
    Scan all articles for places in locations_json that don't have FIPS mappings.
    Returns list of {"place_state": ..., "count": ...}.
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
            place = _location_place(loc, include_raw=True)
            state = loc.get("state")
            if place and state and not loc.get("county_fips"):
                key = f"{place}, {state}"
                unmapped[key] = unmapped.get(key, 0) + 1

    return sorted(
        [{"place_state": k, "count": v} for k, v in unmapped.items()],
        key=lambda x: x["count"],
        reverse=True,
    )


def backfill_fips(conn, geocode=True):
    """
    Re-run FIPS normalization on all articles with locations_json.
    Updates county_fips/county_name where mappings exist (including newly
    added table entries and geocoding).

    Also re-syncs the article_states junction table for each updated article.

    Args:
        conn: Database connection
        geocode: If True (default), use Nominatim+FCC for unresolved places.
                 If False, only use static table + extraction (fast, no network).

    Returns count of articles updated.
    """
    import db as _db
    global _defer_flush

    rows = conn.execute(
        "SELECT id, state, location_relevance, locations_json FROM articles WHERE locations_json IS NOT NULL"
    ).fetchall()

    updated = 0
    total = len(rows)
    _defer_flush = True  # Suppress per-geocode disk writes; flush once at end

    try:
        for i, row in enumerate(rows):
            try:
                locs = json.loads(row["locations_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            changed = False
            for loc in locs:
                changed = normalize_location_entry(loc, geocode=geocode) or changed

            state, location_relevance = derive_article_scope(locs)
            scope_changed = (
                (row["state"] or "other") != state
                or (row["location_relevance"] or "statewide") != location_relevance
            )

            if changed or scope_changed:
                conn.execute(
                    "UPDATE articles SET locations_json = ?, state = ?, location_relevance = ? WHERE id = ?",
                    (json.dumps(locs), state, location_relevance, row["id"]),
                )
                _db._sync_article_states(conn, row["id"], locs)
                updated += 1

            # Progress logging every 100 articles
            if (i + 1) % 100 == 0:
                logger.info("Backfill progress: %d/%d articles processed, %d updated", i + 1, total, updated)

        conn.commit()
    finally:
        _defer_flush = False

    # Flush any geocoded results to disk
    flush_place_cache()

    logger.info("Backfill complete: %d/%d articles updated", updated, total)
    return updated


def backfill_international_articles(conn, limit=None):
    """
    Reassign analyzed rows that look clearly non-US from nationwide/other into
    the explicit international bucket.
    """
    import db as _db

    sql = """
        SELECT id, title, summary, full_text, state, location_relevance
        FROM articles
        WHERE analyzed = 1
          AND COALESCE(state, 'other') IN ('nationwide', 'other')
        ORDER BY COALESCE(published_date, analyzed_date, ingested_date) DESC, id DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql).fetchall()
    updated = 0
    for row in rows:
        inferred_state = infer_state_from_text(
            row["title"] or "",
            row["summary"] or "",
            row["full_text"] or "",
        )
        if inferred_state != "international":
            continue

        place = infer_international_place(
            row["title"] or "",
            row["summary"] or "",
            row["full_text"] or "",
        )
        locations = [build_international_location(place=place)]
        state, location_relevance = derive_article_scope(locations)

        if row["state"] == state and (row["location_relevance"] or "") == location_relevance:
            continue

        conn.execute(
            "UPDATE articles SET state = ?, location_relevance = ?, locations_json = ? WHERE id = ?",
            (state, location_relevance, json.dumps(locations), row["id"]),
        )
        _db._sync_article_states(conn, row["id"], locations)
        updated += 1

    if updated:
        conn.commit()
    logger.info("Backfilled %d articles into international scope", updated)
    return updated


def _resolve_static(place, state, county_hint=None):
    """
    Static-only resolution (no geocoding). Used by backfill with geocode=False.
    Tries: county_hint → direct lookup → diacritics → embedded county → county name → regional.
    """
    if not state:
        return None

    state_lower = state.lower().strip()
    table = _load_place_table()

    if county_hint:
        result = _resolve_county_name(county_hint, state)
        if result:
            return _normalize_result_fips(result)

    if not place:
        return None

    place_stripped = place.strip()
    place_lower = place_stripped.lower()

    # Direct lookup
    key = f"{place_lower}, {state_lower}"
    result = table.get(key)
    if result:
        return _normalize_result_fips(result)

    # Diacritics
    stripped_key = f"{_strip_diacritics(place_lower)}, {state_lower}"
    if stripped_key != key:
        result = table.get(stripped_key)
        if result:
            return _normalize_result_fips(result)

    # Embedded county
    result = _extract_county_from_place(place_stripped, state)
    if result:
        return _normalize_result_fips(result)

    # County name resolution
    if place_lower.endswith(" county"):
        result = _resolve_county_name(place_stripped, state)
        if result:
            return _normalize_result_fips(result)

    # Regional
    result = _REGIONAL_PLACES.get(key)
    if result:
        return _normalize_result_fips(result)

    return None
