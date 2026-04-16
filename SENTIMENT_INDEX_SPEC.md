# Sentiment Intelligence Redesign — Implementation Spec

## Context

The Resistance Dashboard tracks public sentiment toward data center development across Wyoming, Texas, and Michigan (with more states to follow). The current system assigns each article a raw sentiment score (1.0-5.0) and displays simple averages. This spec redesigns the analytics layer to produce a more meaningful **Weighted Sentiment Index (WSI)** and modernizes the Sentiment page and report generation.

### Problems with the current approach
1. **Equal weighting**: 20 articles about the same event dominate the average, drowning out other signals
2. **No time decay**: A 6-month-old article has the same influence as one from yesterday
3. **Sparse time series**: Daily data points with irregular gaps create misleading charts
4. **Elite/public distinction is broken**: 91 of 98 articles classify as "elite" — the split is too lopsided to be useful
5. **Sentiment page is underbaked**: Just heatmaps and a distribution chart; doesn't surface the narratives and entities that the digest reports compute
6. **Reports are Wyoming-hardcoded**: Digest generation only references Wyoming locations

---

## Part 1: Weighted Sentiment Index (WSI)

### 1.1 Weekly Bucketing

Aggregate articles into **ISO week buckets** (Monday-Sunday). Each week produces one data point per state and per location.

- Use `DATE(published_date, 'weekday 0', '-6 days')` in SQLite to compute week-start dates
- Weeks with no articles: **carry forward** the last known value and flag as `"carried": true` in the API response
- Display format: "Mar 17" (week-start date)

### 1.2 Event Clustering

Group articles about the same underlying event to prevent one story from dominating.

**Clustering algorithm:**
1. Within each weekly bucket, for each state:
2. Compare every pair of articles using their `entities_mentioned` arrays
3. Two articles belong to the same cluster if:
   - They share **2 or more entities**, AND
   - They were published within **3 days** of each other
4. Use union-find (disjoint set) to build transitive clusters
5. Each cluster gets a single representative sentiment score = mean of its articles

**Cluster weighting (log-scale diminishing returns):**
- A cluster of `n` articles gets weight `log(n + 1)` (natural log)
- This means:
  - 1 article = weight 0.69
  - 3 articles = weight 1.39 (~2x)
  - 10 articles = weight 2.40 (~3.5x)
  - 20 articles = weight 3.04 (~4.4x)
- The cluster's sentiment contribution = `cluster_avg_sentiment * log(n + 1)`

**Fallback for articles with no/few entities:**
- Articles sharing 0-1 entities are each treated as their own cluster (weight = log(2) = 0.69)
- This is the conservative default — no clustering when we can't confidently group

### 1.3 Time-Decay Weighting (Exponential)

Apply exponential decay so recent weeks matter more than old ones.

- **Half-life: 4 weeks** (28 days)
- Formula: `decay_weight = 0.5 ^ (weeks_ago / 4)`
- A week from 4 weeks ago has 50% weight
- A week from 8 weeks ago has 25% weight
- A week from 12 weeks ago has 12.5% weight

### 1.4 Computing the WSI

For a given state (or "all states") at a given point in time:

```
WSI = sum(week_score * decay_weight for each week) / sum(decay_weight for each week)

where:
  week_score = sum(cluster_sentiment * cluster_log_weight) / sum(cluster_log_weight)
    for all clusters in that week

  decay_weight = 0.5 ^ (weeks_ago / 4)
```

The WSI produces a single number on the 1.0-5.0 scale, just like raw sentiment, but weighted for recency and event significance.

### 1.5 Implementation Location

Create a new module: **`resistance-dashboard/sentiment_index.py`**

Functions:
- `compute_weekly_buckets(conn, state=None, weeks_back=26)` — returns raw weekly aggregates
- `cluster_articles(articles)` — groups articles by entity overlap + date proximity, returns clusters with log-weights
- `compute_wsi(conn, state=None, weeks_back=26)` — returns the WSI value and the weekly time series
- `compute_wsi_trend(conn, state=None, weeks_back=26)` — returns list of `{week_start, wsi, raw_avg, article_count, cluster_count, carried}` for charting

New API endpoint: **`GET /api/sentiment-index?state=<optional>`**

Returns:
```json
{
  "current_wsi": 3.45,
  "raw_avg": 3.21,
  "trend": [
    {"week": "2026-03-17", "wsi": 3.45, "raw": 3.50, "articles": 12, "clusters": 5, "carried": false},
    {"week": "2026-03-10", "wsi": 3.30, "raw": 3.10, "articles": 8, "clusters": 4, "carried": false},
    {"week": "2026-03-03", "wsi": 3.25, "raw": 3.25, "articles": 0, "clusters": 0, "carried": true}
  ],
  "period_comparison": {
    "current_4wk": 3.45,
    "prior_4wk": 3.20,
    "change": 0.25,
    "direction": "improving"
  }
}
```

---

## Part 2: Remove Elite/Public Distinction

### 2.1 What to remove

**Database:** Keep the `voice_type` column in the schema (no migration needed), but stop using it in any display or computation.

**Analysis prompt (`analyze.py`):** Remove `voice_type` from the JSON output schema and the voice type classification instructions. This simplifies the prompt and saves tokens.

**Dashboard JS (`dashboard.js`):**
- Remove the voice comparison bar chart (`renderVoiceChart`)
- Remove the `metricElite` and `metricPublic` metric cards from the dashboard
- Remove the `voiceChart` canvas from `index.html`

**API (`api.py`):**
- Remove `/api/voice-comparison` endpoint entirely
- Remove elite/public averages from `/api/overview` response

**Digest (`digest.py`):**
- Remove elite vs public from `compute_stats()`
- Remove "elite vs public" from the SYNTHESIS_PROMPT sections
- Update to reference all states, not just Wyoming

### 2.2 What replaces it on the Dashboard

The voice comparison chart slot becomes a **Period Comparison** card showing:
- Current 4-week WSI vs prior 4-week WSI
- Direction arrow (up/down/flat)
- Percentage or absolute change
- Sourced from the `/api/sentiment-index` endpoint's `period_comparison` field

---

## Part 3: Sentiment Page Redesign

The Sentiment page should be the analytical deep-dive — everything from the digest reports plus interactive exploration.

### 3.1 New Layout (top to bottom)

#### Section A: Sentiment Index Trend (replaces sparse daily chart)
- **Weekly WSI line chart** with dual lines: WSI (bold, primary) and Raw Average (thin, secondary)
- X-axis: weekly buckets with proper spacing
- Carried-forward weeks shown as dotted line segments
- State filter dropdown (same as Dashboard)
- Tooltip shows: week range, WSI, raw avg, article count, cluster count

#### Section B: Period Comparison Cards
- Row of 3 cards (one per state): current 4-week WSI, trend arrow, change value
- Plus an "All States" aggregate card
- Color-coded by sentiment (using the smooth gradient)

#### Section C: Location x Time Heatmap (existing, already updated)
- Keep the current multi-state heatmap
- Switch from daily to **weekly buckets** to match the new time series
- Use WSI-weighted values instead of raw averages

#### Section D: Topic Sentiment Breakdown
- Horizontal bar chart showing avg sentiment per topic tag
- Sorted by deviation from neutral (3.0) — most polarizing topics first
- Article count shown next to each bar
- Color-coded by sentiment

#### Section E: Entity Tracker
- Table showing top entities mentioned across all articles
- Columns: Entity name, mention count, avg sentiment of articles mentioning them, trend (up/down vs prior period)
- Sortable by any column
- This surfaces which companies/organizations are driving the narrative

#### Section F: Key Articles
- The 5-10 most significant articles (highest deviation from neutral, or from highly-clustered events)
- Show: title, source, date, sentiment score, location, key claims
- Clickable to expand full detail

#### Section G: Sentiment Distribution (existing)
- Keep the doughnut chart
- Add raw count labels

#### Section H: Raw Data (collapsible)
- Full article-level data table (similar to Articles page but read-only)
- Shows both raw scores and cluster membership
- Export to CSV button

### 3.2 Remove from Sentiment Page
- The Topic x Time heatmap can stay but should use weekly buckets
- Consider whether it adds enough value vs the Topic Sentiment Breakdown bars

---

## Part 4: Report/Digest Modernization

### 4.1 Update SYNTHESIS_PROMPT

Replace the current Wyoming-only prompt with a multi-state version:

```
You are an intelligence analyst producing a biweekly sentiment report about data center
development across the United States for Prometheus Hyperscale leadership.

States currently tracked: Wyoming (Evanston, Casper, Cheyenne), Texas (Dallas),
Michigan (Ann Arbor, Van Buren/Wayne County, Benton Harbor/Berrien County).

Given the following classified articles and aggregate data from the past {days} days,
produce a concise intelligence digest. Be analytical and objective. Distinguish between
signals and noise. Flag anything that represents a meaningful shift from baseline.

The digest should include:
1. NATIONAL SENTIMENT SNAPSHOT — Overall WSI score, trend direction, period comparison
2. STATE BREAKDOWN — Brief notes on each tracked state with their WSI scores
3. BY LOCATION — Notable developments in specific cities/counties
4. TOP THEMES — The 2-3 most significant narratives or developments
5. ENTITY TRACKER — Which companies were mentioned and in what context
6. LEGISLATIVE/REGULATORY UPDATE — Any policy developments (by state)
7. WATCH LIST — Emerging issues that bear monitoring
8. KEY ARTICLES — The 3-5 most significant pieces with source, date, and state

Keep it under 1000 words. Write for busy executives who want the bottom line.
```

### 4.2 Update `compute_stats()`

- Remove elite/public breakdown
- Add per-state WSI scores
- Add period comparison (current vs prior)
- Add cluster summary (how many events detected, largest cluster)
- Add entity frequency data
- Pull data for all states, not just Wyoming locations

### 4.3 Update `build_synthesis_input()`

- Include WSI alongside raw averages
- Group articles by state
- Include cluster information (which articles are about the same event)
- Include entity mention counts

### 4.4 Update HTML Export (`export.py` / `export_template.html`)

- Replace legacy branding with "Prometheus Resistance Dashboard" (or appropriate name)
- Add state tabs or sections
- Replace daily trend chart with weekly WSI chart
- Remove elite/public chart
- Add entity table
- Add period comparison visualization

---

## Part 5: Implementation Order

Recommended sequence (each step is independently shippable):

### Phase 1: Foundation (sentiment_index.py + API)
1. Create `sentiment_index.py` with clustering, decay, and WSI computation
2. Add `/api/sentiment-index` endpoint
3. Write tests to verify WSI computation with known data
4. **No UI changes yet** — just the backend

### Phase 2: Remove Elite/Public
1. Remove voice comparison chart from Dashboard
2. Remove elite/public metrics cards
3. Remove `/api/voice-comparison` endpoint
4. Remove voice_type from analysis prompt
5. Replace the chart slot with Period Comparison card (sourced from `/api/sentiment-index`)

### Phase 3: Dashboard Updates
1. Replace daily trend chart with weekly WSI trend chart
2. Wire state filter to use WSI data
3. Update map tooltips to show WSI instead of raw avg (optional)

### Phase 4: Sentiment Page Redesign
1. Replace Location heatmap with weekly-bucketed version
2. Add Entity Tracker table
3. Add Topic Sentiment Breakdown bars
4. Add Key Articles section
5. Add Period Comparison cards per state
6. Add raw data export

### Phase 5: Report Modernization
1. Update SYNTHESIS_PROMPT for multi-state
2. Update compute_stats() with WSI and entities
3. Update HTML export template
4. Remove Wyoming-only references throughout

---

## Part 6: Technical Notes

### Database
- No schema changes needed. WSI is computed at query time, not stored.
- The `entities_mentioned` column (JSON array) is already populated by analysis and is the foundation for clustering.
- Consider adding an index on `(state, published_date)` for performance as article count grows.

### Clustering Performance
- With ~100 articles, brute-force pairwise comparison is fine (O(n^2) where n is articles per week per state, usually < 20)
- If scaling to thousands of articles per week, switch to entity-indexed lookup (group by shared entities first, then check date proximity)

### Existing Functions to Reuse
- `sentimentColor()` in dashboard.js — already updated to smooth gradient
- `fetchJSON()` — standard API fetcher
- `STATE_CONFIG` / `STATE_REGIONS` / `STATE_ABBR` — all state config objects
- `_state_filter()` in api.py — helper for adding state WHERE clause
- `positionTooltip()` — tooltip positioning helper

### Key Files to Modify
| File | Changes |
|------|---------|
| `resistance-dashboard/sentiment_index.py` | **NEW** — WSI computation engine |
| `resistance-dashboard/dashboard/api.py` | New endpoint, remove voice-comparison |
| `resistance-dashboard/dashboard/static/dashboard.js` | Chart updates, remove voice, add period comparison |
| `resistance-dashboard/dashboard/templates/index.html` | Remove voice elements, add new sections |
| `resistance-dashboard/dashboard/static/style.css` | Styling for new components |
| `resistance-dashboard/analyze.py` | Remove voice_type from prompt |
| `resistance-dashboard/digest.py` | Multi-state, WSI, remove elite/public |
| `resistance-dashboard/dashboard/export.py` | Multi-state export |
| `resistance-dashboard/dashboard/templates/export_template.html` | Updated export template |

### Configuration Constants (for tuning)
```python
WSI_HALF_LIFE_WEEKS = 4          # Decay half-life
WSI_CLUSTER_ENTITY_THRESHOLD = 2  # Min shared entities to cluster
WSI_CLUSTER_DATE_WINDOW_DAYS = 3  # Max days apart to cluster
WSI_WEEKS_BACK = 26               # Default lookback (6 months)
```
