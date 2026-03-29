# Article Geography Pipeline — Design Spec

## Problem

The current system assigns each article a single `state` and single `location_relevance` from a hardcoded list. This breaks in several ways:

1. **Articles can be about multiple places** — "Wyoming Legislature debates regulations, citing concerns from Evanston and Casper" is about statewide policy AND two specific cities
2. **Articles can span multiple states** — "Prometheus announces expansion from Wyoming to Michigan"
3. **Locations are hardcoded** — adding a new city or state requires code changes in analyze.py, config.yaml, ingest.py, and dashboard.js
4. **No geographic hierarchy** — "Saline Township" can't be automatically rolled up to "Washtenaw County" or "Michigan"
5. **Inconsistent granularity** — some articles are tagged to a city, others to "statewide," with no way to express that an article is primarily statewide but mentions a specific place

## Design

### Core Concept: Multi-location tagging with county normalization

Each article gets a **list of location tags**, each with:
- A **state**
- An optional **place name** (freeform, as detected by Claude)
- An optional **county FIPS code** (normalized post-Claude)
- A **relevance level** (primary vs mentioned)

```json
{
  "locations": [
    {"state": "wyoming", "place": "Evanston", "county_fips": "56041", "county_name": "Uinta County", "relevance": "primary"},
    {"state": "wyoming", "place": null, "county_fips": null, "county_name": null, "relevance": "mentioned"}
  ]
}
```

The second entry (no place, no county) represents "Wyoming generally" — a statewide reference.

### Relevance levels

- **primary** — the article is primarily about this place. An article can have multiple primary locations (e.g., comparing data center developments in two cities).
- **mentioned** — the place is referenced but isn't the main geographic focus. A national policy article that name-drops Wyoming as an example.

---

## Schema Changes

### New column on `articles` table

```sql
ALTER TABLE articles ADD COLUMN locations_json TEXT;
```

Contains a JSON array of location objects. Example:
```json
[
  {"state": "wyoming", "place": "Evanston", "county_fips": "56041", "county_name": "Uinta County", "relevance": "primary"},
  {"state": "wyoming", "relevance": "mentioned"}
]
```

### Keep existing columns for backward compatibility

`state` and `location_relevance` remain populated with the **primary** location (first primary entry, or first entry if none marked primary). This means:
- Existing queries and views continue to work unchanged
- The old columns act as a denormalized index for simple filtering
- New features can use the richer `locations_json` data

---

## Analysis Prompt Changes

### Current prompt output (single location)
```json
{
  "state": "wyoming",
  "location_relevance": "evanston"
}
```

### New prompt output (multi-location)
```json
{
  "locations": [
    {"state": "wyoming", "place": "Evanston", "relevance": "primary"},
    {"state": "wyoming", "relevance": "mentioned"}
  ]
}
```

### Updated prompt instructions

Replace the current state/location instructions with:

```
Geographic tagging:
Return a "locations" array identifying every geographic area this article is relevant to.

Each location entry should have:
- "state": the US state (lowercase). Use "nationwide" for federal/national scope with no single-state focus.
- "place": the specific city, township, county, or region name (if applicable). Omit this field for statewide articles.
- "relevance": "primary" if the article is primarily about this place, "mentioned" if the place is referenced but not the main focus.

Rules:
- An article can have multiple primary locations (e.g., comparing developments in two cities)
- An article about state-level policy should include a statewide entry (no "place" field)
- If the article discusses both statewide policy AND specific locations, include both
- If the article is about multiple states, include entries for each state
- Be specific: prefer "Saline Township" over "Ann Arbor area" if the article names the township
- For tracked states, common places include:
  Wyoming: Evanston, Casper, Cheyenne, Converse County, Uinta County, Natrona County
  Texas: Dallas, Fort Worth, Lancaster, Ellis County
  Michigan: Ann Arbor, Saline Township, Van Buren Township, Benton Harbor, Benton Township
- Don't limit to this list — return whatever places the article actually references
```

The prompt no longer constrains locations to a hardcoded list. Claude returns whatever geography it detects.

### Backward-compatible fields

The analysis response parser will:
1. Read the `locations` array from Claude
2. Set `state` to the state of the first primary location
3. Set `location_relevance` to the place of the first primary location (or "statewide" if no place)
4. Store the full array in `locations_json`

If Claude still returns old-style `state`/`location_relevance` (e.g., from cached prompts), the parser falls back to wrapping them into a single-entry array.

---

## FIPS Normalization Layer

### Overview

After Claude returns location tags with freeform place names, a normalization step maps each place to its county FIPS code. This enables:
- Automatic geographic hierarchy (county → state)
- Map coloring by county
- Consistent rollup regardless of how the place was named

### Lookup table: `data/place_to_county.json`

A JSON file mapping `"place, state"` keys to county information:

```json
{
  "evanston, wyoming": {"fips": "56041", "county": "Uinta County"},
  "casper, wyoming": {"fips": "56025", "county": "Natrona County"},
  "cheyenne, wyoming": {"fips": "56021", "county": "Laramie County"},
  "converse county, wyoming": {"fips": "56009", "county": "Converse County"},
  "dallas, texas": {"fips": "48113", "county": "Dallas County"},
  "lancaster, texas": {"fips": "48113", "county": "Dallas County"},
  "ann arbor, michigan": {"fips": "26161", "county": "Washtenaw County"},
  "saline township, michigan": {"fips": "26161", "county": "Washtenaw County"},
  "van buren township, michigan": {"fips": "26163", "county": "Wayne County"},
  "benton harbor, michigan": {"fips": "26021", "county": "Berrien County"},
  "benton township, michigan": {"fips": "26021", "county": "Berrien County"}
}
```

### Normalization process

For each location tag with a `place`:
1. Normalize: `f"{place.lower().strip()}, {state.lower().strip()}"`
2. Look up in `place_to_county.json`
3. If found: add `county_fips` and `county_name` to the location tag
4. If not found: leave `county_fips` as null, store raw place name
   - Log a warning: "Unknown place: {place}, {state} — add to place_to_county.json"
   - The article still works, just without county-level map coloring

### Growing the table

The lookup table grows in three ways:
1. **Manual** — when you notice an unmapped place in the logs, add it to the JSON file
2. **Batch discovery** — a utility function scans all articles for unmapped places and lists them
3. **Future: Census geocoder** — optional enhancement to auto-resolve unknown places via the Census Bureau's free geocoding API (https://geocoding.geo.census.gov/). Not needed initially but could be added as a fallback.

### Multiple places → same county

Multiple places can map to the same FIPS code. This is correct — "Ann Arbor" and "Saline Township" both map to Washtenaw County (26161). When aggregating at the county level, articles tagged to either place contribute to the Washtenaw County score.

### Places without county mapping

Some locations are inherently county-level or regional:
- "Uinta County" → FIPS 56041 (direct county reference)
- "Dallas-Fort Worth" → could map to multiple counties; pick the primary one (Dallas County 48113) or allow multi-county mapping

The lookup table handles these as entries like any other place name.

---

## Aggregation Changes

### State-level WSI

When computing WSI for a state:
1. Select all articles where `locations_json` contains at least one entry with that state
2. **Deduplicate by article ID** — an article about "Evanston and Casper" counts once for Wyoming
3. Apply existing WSI pipeline: weekly bucketing → clustering → log-weights → time decay
4. Apply **specificity weighting** (new):
   - Articles with a specific place (county-level) get weight **1.0**
   - Articles with statewide relevance (no place) get weight **0.6**
   - The relevance level also matters: "primary" = full specificity weight, "mentioned" = 0.5x

### County-level aggregation

For heatmap and map coloring:
1. Group articles by county FIPS
2. Each county gets its own average, computed only from articles tagged to that county
3. Articles without a county FIPS (statewide) don't appear in county-level views

### The "statewide" bucket

Statewide articles (no place, no county) remain their own bucket in the heatmap. They represent the political/regulatory climate that affects all locations in the state. They contribute to the state-level WSI at reduced weight (0.6x).

### Multi-state articles

An article about "Wyoming and Michigan" contributes to both states' WSI. When computing the overall "All States" WSI, it's deduplicated — counts once globally, even though it has location tags in two states.

---

## Migration Plan

### Phase 1: Schema + backfill
1. Add `locations_json` column to articles table
2. Backfill from existing `state`/`location_relevance`:
   ```python
   for article in all_articles:
       locations = [{"state": article.state, "relevance": "primary"}]
       if article.location_relevance and article.location_relevance != "statewide":
           locations[0]["place"] = article.location_relevance
       article.locations_json = json.dumps(locations)
   ```
3. Run FIPS normalization on backfilled data (add county_fips where place matches lookup table)

### Phase 2: Analysis prompt update
1. Update SYSTEM_PROMPT in analyze.py with new location instructions
2. Update `parse_analysis_response()` to handle both old and new formats
3. Add normalization step after parsing
4. New articles get multi-location tags; old articles retain their backfilled single-location tags

### Phase 3: Aggregation update
1. Update `sentiment_index.py` to read from `locations_json`
2. Add specificity weighting
3. Add deduplication logic for multi-location articles
4. Update heatmap API to use county-level data from `locations_json`

### Phase 4: Display updates
1. Heatmap shows counties (already works via FIPS)
2. Article detail shows all location tags (not just primary)
3. Entity tracker and topic bars can be filtered by location

### Phase 5: Dynamic states (eliminate all hardcoded state lists)
1. Create `data/us_states.json` with all 50 states (name, abbr, FIPS)
2. Add `GET /api/states` endpoint — returns states with analyzed articles
3. Replace all hardcoded `<option>` tags in `index.html` with empty `<select>` elements
4. Add `getStates()` / `populateStateDropdown()` JS helpers; call on page init and after data changes
5. Rewrite `renderPeriodCards()` to loop over data-driven state list
6. Replace `STATE_ABBR`, `STATE_REGIONS`, `REGION_OPTIONS` with data from `/api/states`
7. Update map renderer to color states from `/api/state-sentiment` data (no hardcoded `STATE_NAME_TO_KEY`)
8. Update `analyze.py` prompt to accept any US state (not hardcoded list)
9. Update `digest.py` to derive `TRACKED_STATES` from database
10. Verify: adding a row with `state='idaho'` to the DB makes Idaho appear in all dropdowns, cards, and map coloring without any code changes

---

## Dynamic States & Scalability

### Problem: Hardcoded state lists

Currently, states are hardcoded in **at least 10 places** across the codebase:

| Location | What's hardcoded |
|----------|-----------------|
| `index.html` — dashboard state filter | `<option>` tags for WY, TX, MI |
| `index.html` — sentiment state filter | Same |
| `index.html` — articles state filter | Same + "Nationwide" |
| `index.html` — web search state filter | Same |
| `index.html` — article detail state dropdown | Same + "Other" |
| `dashboard.js` — `STATE_CONFIG` | FIPS codes, city coordinates, county mappings |
| `dashboard.js` — `STATE_ABBR` | wyoming→WY, texas→TX, michigan→MI |
| `dashboard.js` — `STATE_REGIONS` | Regions per state |
| `dashboard.js` — `REGION_OPTIONS` | Dropdown options per state |
| `dashboard.js` — `renderPeriodCards()` | Fetches WSI for hardcoded state list |
| `analyze.py` — `SYSTEM_PROMPT` | `wyoming\|texas\|michigan\|nationwide\|other` |
| `digest.py` — `TRACKED_STATES` | `["wyoming", "texas", "michigan"]` |
| `api.py` — `/api/locations` | Hardcoded location lists per state |
| `config.yaml` — `states:` | Feeds/keywords per state |

Adding a new state (e.g., Idaho) requires touching most of these files. The goal is to reduce this to **one place**: `config.yaml` (for ingestion) — everything else derives from the data.

### Solution: Data-driven state discovery

#### New API endpoint: `GET /api/states`

Returns all states that have at least one analyzed article, derived from the database:

```json
{
  "states": [
    {"key": "wyoming", "name": "Wyoming", "abbr": "WY", "article_count": 93, "fips": "56"},
    {"key": "texas", "name": "Texas", "abbr": "TX", "article_count": 5, "fips": "48"},
    {"key": "michigan", "name": "Michigan", "abbr": "MI", "article_count": 7, "fips": "26"},
    {"key": "idaho", "name": "Idaho", "abbr": "ID", "article_count": 2, "fips": "16"}
  ]
}
```

This endpoint uses a static mapping of state names → abbreviations/FIPS (all 50 US states — this is reference data, not configuration). If a new state appears in the article data, it automatically shows up.

#### State reference data: `data/us_states.json`

A static file with all 50 US states (this is fixed reference data, not user configuration):

```json
{
  "wyoming": {"name": "Wyoming", "abbr": "WY", "fips": "56"},
  "texas": {"name": "Texas", "abbr": "TX", "fips": "48"},
  "michigan": {"name": "Michigan", "abbr": "MI", "fips": "26"},
  "idaho": {"name": "Idaho", "abbr": "ID", "fips": "16"},
  ...
}
```

#### Frontend: dynamic dropdowns and cards

All state dropdowns are populated dynamically on page load:

```javascript
// On app init, fetch once and cache
let _statesCache = null;
async function getStates() {
    if (!_statesCache) _statesCache = await fetchJSON('/api/states');
    return _statesCache.states;
}

// Populate any state dropdown
async function populateStateDropdown(selectId, includeAll = true) {
    const states = await getStates();
    const sel = document.getElementById(selectId);
    sel.innerHTML = includeAll ? '<option value="">All States</option>' : '';
    states.forEach(s => {
        sel.innerHTML += `<option value="${s.key}">${s.name}</option>`;
    });
}
```

This replaces hardcoded `<option>` tags. When Idaho gets its first article, it appears in every dropdown automatically.

#### Period comparison cards: dynamic

`renderPeriodCards()` currently hardcodes `['', 'wyoming', 'texas', 'michigan']`. Replace with:

```javascript
async function renderPeriodCards() {
    const states = await getStates();
    const stateKeys = ['', ...states.map(s => s.key)]; // '' = All States
    // fetch WSI for each...
}
```

#### Analysis prompt: open-ended states

Replace `"state": "<wyoming|texas|michigan|nationwide|other>"` with:

```
"state": the US state name in lowercase (e.g., "wyoming", "texas", "idaho").
Use "nationwide" for federal/national scope. Use "other" only for non-US locations.
```

Claude already knows US states. No need to enumerate them.

#### Map: data-driven coloring

The D3 map already renders all 50 states from TopoJSON. Currently only WY/TX/MI get colored because `STATE_NAME_TO_KEY` only maps those three. Change to:

```javascript
// Instead of hardcoded STATE_NAME_TO_KEY, derive from /api/states
const stateSentiment = await fetchJSON('/api/state-sentiment');
// stateSentiment already returns all states with data
// The map renderer just checks if the state has data, no hardcoded list needed
```

For county drill-down, `STATE_CONFIG` currently provides city coordinates for markers. With the new system, city markers come from the `place_to_county.json` lookup table (which can include lat/lon) or from the project config (future). States without configured cities still get county-level coloring from FIPS — they just don't have city dot markers.

#### What still needs manual config

Only these things require manual configuration for a new state:

1. **Ingestion config** (`config.yaml` → `states:`) — RSS feeds, keywords, and search queries. Without this, you won't find articles about the new state. But if articles flow in through other means (manual entry, broad web search), they still work.

2. **City marker coordinates** (future project config) — where to put dots on the state drill-down map. Without this, the county map still works (counties color by sentiment), you just don't get labeled city dots.

3. **Place-to-county mappings** (`place_to_county.json`) — grows organically. Unknown places still store and display, they just don't get county-level rollup until mapped.

Everything else — dropdowns, cards, heatmaps, charts, aggregation, WSI — works automatically from whatever data exists.

---

## File Changes

| File | Phase | Changes |
|------|-------|---------|
| `db.py` | 1 | Add `locations_json` column, migration, backfill |
| `data/place_to_county.json` | 1 | **NEW** — place name → county FIPS lookup table |
| `data/us_states.json` | 1 | **NEW** — all 50 US states reference data (name, abbr, FIPS) |
| `analyze.py` | 2 | Updated prompt (open-ended states, multi-location output), response parser, normalization step |
| `sentiment_index.py` | 3 | Multi-location WSI, specificity weights, dedup, read from `locations_json` |
| `dashboard/api.py` | 3, 4, 5 | New `/api/states` endpoint, updated location-weekly, article queries, state-sentiment |
| `dashboard/static/dashboard.js` | 4, 5 | Dynamic dropdowns, dynamic period cards, dynamic map coloring, data-driven `STATE_ABBR`, heatmap, article detail |
| `dashboard/templates/index.html` | 5 | Remove hardcoded `<option>` tags from all state dropdowns (replaced with empty `<select>` populated by JS) |
| `digest.py` | 5 | Replace `TRACKED_STATES` with data-driven state list from DB |
| `ingest.py` | 2 | Remove hardcoded location detection, use `locations_json`-aware flow |

---

## Example: Full lifecycle

**Article:** "Wyoming Legislature approves data center tax incentives, a move praised by Evanston officials but questioned by Cheyenne environmental groups"

**Claude returns:**
```json
{
  "locations": [
    {"state": "wyoming", "relevance": "primary"},
    {"state": "wyoming", "place": "Evanston", "relevance": "mentioned"},
    {"state": "wyoming", "place": "Cheyenne", "relevance": "mentioned"}
  ]
}
```

**After FIPS normalization:**
```json
{
  "locations": [
    {"state": "wyoming", "county_fips": null, "county_name": null, "relevance": "primary"},
    {"state": "wyoming", "place": "Evanston", "county_fips": "56041", "county_name": "Uinta County", "relevance": "mentioned"},
    {"state": "wyoming", "place": "Cheyenne", "county_fips": "56021", "county_name": "Laramie County", "relevance": "mentioned"}
  ]
}
```

**Backward-compat columns:** `state = "wyoming"`, `location_relevance = "statewide"`

**Aggregation impact:**
- Wyoming WSI: included once, with statewide specificity weight (0.6)
- Uinta County (Evanston) heatmap: included with "mentioned" modifier (0.5x of county weight)
- Laramie County (Cheyenne) heatmap: included with "mentioned" modifier
- All States WSI: included once (deduped)

---

## Decisions (resolved)

1. **"Mentioned" locations and the heatmap** — Only "primary" locations contribute at full weight to county-level scores. "Mentioned" locations contribute at 0.25x weight. This prevents a statewide policy article that name-drops Evanston from dominating Evanston's sentiment score.

2. **"Nationwide" articles** — Nationwide articles contribute only to the "All States" WSI, not to individual states. They're important context but adding them to every state adds noise. They appear in the heatmap/cards only under an "All States" view.

3. **Re-analysis of existing articles** — Don't re-analyze. Backfill existing `state`/`location_relevance` into single-entry `locations_json` arrays. This gives a reasonable approximation. Data quality improves naturally as new articles get the full multi-location treatment. Re-analysis can be done selectively later if needed.

4. **Lookup table seeding** — Start minimal with known locations only (~20 entries). Grow organically as new places appear. Unknown places still store and display — they just don't get county-level map coloring until mapped. A utility function lists all unmapped places for periodic cleanup.
