"""
Weighted Sentiment Index (WSI) — computation engine.

Aggregates raw article sentiment into a meaningful index using:
  - ISO-week bucketing (Monday-Sunday)
  - Event clustering via entity overlap + date proximity
  - Log-scale cluster weighting (diminishing returns for duplicate coverage)
  - Exponential time-decay (4-week half-life)
"""

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger("wyoming_pulse.sentiment_index")

# ── Tuning constants ──────────────────────────────────────
WSI_HALF_LIFE_WEEKS = 4            # Decay half-life
WSI_CLUSTER_ENTITY_THRESHOLD = 2   # Min shared entities to cluster
WSI_CLUSTER_DATE_WINDOW_DAYS = 3   # Max days apart to cluster
WSI_WEEKS_BACK = 26                # Default lookback (6 months)


# ── Union-Find for clustering ─────────────────────────────

class UnionFind:
    """Disjoint-set / union-find data structure."""

    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ── Core functions ─────────────────────────────────────────

def _week_start(date_str):
    """Return the ISO week-start (Monday) for a given date string."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Try parsing just the date portion
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    # Monday of that ISO week
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def _parse_date(date_str):
    """Parse a date string into a datetime, tolerating various formats."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None


def compute_weekly_buckets(
    conn,
    state=None,
    county_fips=None,
    weeks_back=WSI_WEEKS_BACK,
    include_low_confidence=False,
):
    """
    Fetch analyzed articles and group them into ISO-week buckets.

    Returns dict keyed by week-start date string, each value being a list of
    article dicts with: id, title, published_date, sentiment_score,
    entities_mentioned (as list), state, location_relevance.
    """
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(weeks=weeks_back)).strftime("%Y-%m-%d")

    where = "analyzed = 1 AND published_date IS NOT NULL AND published_date >= ?"
    params = [cutoff]
    if not include_low_confidence:
        where += f" AND {db.high_confidence_predicate('articles')}"
        params.extend(db.low_confidence_params())
    if county_fips:
        fips_unpadded = county_fips.lstrip("0") or "0"
        fips_padded = county_fips.zfill(5)
        where += (" AND articles.id IN "
                  "(SELECT article_id FROM article_states WHERE county_fips IN (?, ?))")
        params.extend([fips_unpadded, fips_padded])
    elif state:
        where += (" AND articles.id IN "
                  "(SELECT article_id FROM article_states WHERE state = ?)")
        params.append(state)

    rows = conn.execute(
        f"SELECT id, title, published_date, sentiment_score, "
        f"entities_mentioned, state, location_relevance "
        f"FROM articles WHERE {where} "
        f"ORDER BY published_date ASC",
        params,
    ).fetchall()

    buckets = defaultdict(list)
    for r in rows:
        ws = _week_start(r["published_date"])
        if ws is None:
            continue
        entities_raw = r["entities_mentioned"]
        entities = json.loads(entities_raw) if entities_raw else []
        buckets[ws].append({
            "id": r["id"],
            "title": r["title"],
            "published_date": r["published_date"],
            "sentiment_score": r["sentiment_score"],
            "entities": [e.lower().strip() for e in entities],
            "state": r["state"],
            "location_relevance": r["location_relevance"],
        })

    return dict(buckets)


def _current_monday():
    """Return the current week's Monday in naive UTC."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now - timedelta(days=now.weekday())


def _build_week_metrics(buckets):
    """Build reusable per-week metrics from a bucket dict."""
    week_data = {}
    all_scores = []

    for week_start, articles in buckets.items():
        clusters = cluster_articles(articles)
        week_score = _compute_week_score(clusters)
        raw_scores = [a["sentiment_score"] for a in articles if a["sentiment_score"] is not None]
        all_scores.extend(raw_scores)

        week_data[week_start] = {
            "week": week_start,
            "wsi": round(week_score, 2) if week_score is not None else None,
            "raw": round(sum(raw_scores) / len(raw_scores), 2) if raw_scores else None,
            "articles": len(articles),
            "clusters": len(clusters),
            "carried": False,
        }

    return week_data, all_scores


def _build_trend(week_data, weeks_back):
    """Generate a continuous weekly trend with carry-forward values."""
    current_monday = _current_monday()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(weeks=weeks_back)
    cutoff_monday = cutoff - timedelta(days=cutoff.weekday())

    trend = []
    monday = cutoff_monday
    last_known = None

    while monday <= current_monday:
        week_start = monday.strftime("%Y-%m-%d")
        if week_start in week_data:
            last_known = week_data[week_start]
            trend.append(dict(week_data[week_start]))
        else:
            trend.append({
                "week": week_start,
                "wsi": last_known["wsi"] if last_known is not None else None,
                "raw": last_known["raw"] if last_known is not None else None,
                "articles": 0,
                "clusters": 0,
                "carried": True,
            })
        monday += timedelta(weeks=1)

    return trend


def _build_period_comparison(week_data):
    """Compare the most recent 4 weeks against the prior 4 weeks."""
    current_monday = _current_monday()
    current_start = (current_monday - timedelta(weeks=4)).strftime("%Y-%m-%d")
    prior_start = (current_monday - timedelta(weeks=8)).strftime("%Y-%m-%d")

    current_scores = []
    prior_scores = []

    for week_start, entry in week_data.items():
        week_score = entry["wsi"]
        if week_score is None:
            continue
        if week_start >= current_start:
            current_scores.append(week_score)
        elif week_start >= prior_start:
            prior_scores.append(week_score)

    current_4wk = round(sum(current_scores) / len(current_scores), 2) if current_scores else None
    prior_4wk = round(sum(prior_scores) / len(prior_scores), 2) if prior_scores else None

    change = None
    direction = "flat"
    if current_4wk is not None and prior_4wk is not None:
        change = round(current_4wk - prior_4wk, 2)
        if change > 0.05:
            direction = "improving"
        elif change < -0.05:
            direction = "declining"

    return {
        "current_4wk": current_4wk,
        "prior_4wk": prior_4wk,
        "change": change,
        "direction": direction,
    }


def compute_wsi_bundle(
    conn,
    state=None,
    county_fips=None,
    weeks_back=WSI_WEEKS_BACK,
    include_low_confidence=False,
):
    """
    Compute the current WSI, trend, and period comparison from a single bucket pass.
    """
    buckets = compute_weekly_buckets(
        conn,
        state=state,
        county_fips=county_fips,
        weeks_back=weeks_back,
        include_low_confidence=include_low_confidence,
    )
    if not buckets:
        return {
            "current_wsi": None,
            "raw_avg": None,
            "week_count": 0,
            "trend": _build_trend({}, weeks_back),
            "period_comparison": {
                "current_4wk": None,
                "prior_4wk": None,
                "change": None,
                "direction": "flat",
            },
        }

    week_data, all_scores = _build_week_metrics(buckets)
    current_monday = _current_monday()
    numerator = 0.0
    denominator = 0.0

    for week_start, entry in week_data.items():
        week_score = entry["wsi"]
        if week_score is None:
            continue

        week_date = datetime.strptime(week_start, "%Y-%m-%d")
        weeks_ago = max(0.0, (current_monday - week_date).days / 7.0)
        decay = 0.5 ** (weeks_ago / WSI_HALF_LIFE_WEEKS)
        numerator += week_score * decay
        denominator += decay

    return {
        "current_wsi": round(numerator / denominator, 2) if denominator > 0 else None,
        "raw_avg": round(sum(all_scores) / len(all_scores), 2) if all_scores else None,
        "week_count": len(buckets),
        "trend": _build_trend(week_data, weeks_back),
        "period_comparison": _build_period_comparison(week_data),
    }


def cluster_articles(articles):
    """
    Group articles by entity overlap + date proximity using union-find.

    Two articles cluster if they share >= WSI_CLUSTER_ENTITY_THRESHOLD entities
    AND were published within WSI_CLUSTER_DATE_WINDOW_DAYS of each other.

    Returns list of clusters, each a dict with:
      - articles: list of article dicts
      - avg_sentiment: mean sentiment of the cluster
      - log_weight: log(n + 1) where n = cluster size
    """
    n = len(articles)
    if n == 0:
        return []

    uf = UnionFind(n)

    # Pairwise comparison
    for i in range(n):
        ents_i = set(articles[i]["entities"])
        date_i = _parse_date(articles[i]["published_date"])
        if not ents_i or len(ents_i) < WSI_CLUSTER_ENTITY_THRESHOLD:
            # Article has too few entities to meaningfully cluster
            continue
        for j in range(i + 1, n):
            ents_j = set(articles[j]["entities"])
            if len(ents_j) < WSI_CLUSTER_ENTITY_THRESHOLD:
                continue
            shared = len(ents_i & ents_j)
            if shared < WSI_CLUSTER_ENTITY_THRESHOLD:
                continue
            # Check date proximity
            date_j = _parse_date(articles[j]["published_date"])
            if date_i and date_j:
                delta = abs((date_i - date_j).days)
                if delta > WSI_CLUSTER_DATE_WINDOW_DAYS:
                    continue
            uf.union(i, j)

    # Build clusters from union-find
    groups = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(articles[i])

    clusters = []
    for members in groups.values():
        scores = [a["sentiment_score"] for a in members if a["sentiment_score"] is not None]
        avg = sum(scores) / len(scores) if scores else None
        clusters.append({
            "articles": members,
            "avg_sentiment": avg,
            "log_weight": math.log(len(members) + 1),
            "size": len(members),
        })

    return clusters


def _compute_week_score(clusters):
    """
    Compute the weighted sentiment for a single week from its clusters.

    week_score = sum(cluster_sentiment * cluster_log_weight) / sum(cluster_log_weight)
    """
    numerator = 0.0
    denominator = 0.0
    for c in clusters:
        if c["avg_sentiment"] is None:
            continue
        numerator += c["avg_sentiment"] * c["log_weight"]
        denominator += c["log_weight"]
    if denominator == 0:
        return None
    return numerator / denominator


def compute_wsi(conn, state=None, county_fips=None, weeks_back=WSI_WEEKS_BACK):
    """
    Compute the current Weighted Sentiment Index.

    Returns dict with:
      - current_wsi: the time-decay-weighted index value (1.0 - 5.0)
      - raw_avg: simple average of all articles in the period
      - week_count: number of weeks with data
    """
    bundle = compute_wsi_bundle(conn, state=state, county_fips=county_fips, weeks_back=weeks_back)
    return {
        "current_wsi": bundle["current_wsi"],
        "raw_avg": bundle["raw_avg"],
        "week_count": bundle["week_count"],
    }


def compute_wsi_trend(conn, state=None, county_fips=None, weeks_back=WSI_WEEKS_BACK):
    """
    Compute the WSI time series for charting.

    Returns list of dicts sorted chronologically:
      {week_start, wsi, raw_avg, article_count, cluster_count, carried}

    Gaps (weeks with no articles) are filled with carried-forward values.
    """
    return compute_wsi_bundle(conn, state=state, county_fips=county_fips, weeks_back=weeks_back)["trend"]


def compute_period_comparison(conn, state=None, county_fips=None):
    """
    Compare current 4-week WSI vs prior 4-week WSI.

    Returns dict with current_4wk, prior_4wk, change, direction.
    """
    return compute_wsi_bundle(conn, state=state, county_fips=county_fips, weeks_back=8)["period_comparison"]
