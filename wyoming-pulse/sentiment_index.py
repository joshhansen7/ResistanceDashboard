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
from datetime import datetime, timedelta

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


def compute_weekly_buckets(conn, state=None, weeks_back=WSI_WEEKS_BACK):
    """
    Fetch analyzed articles and group them into ISO-week buckets.

    Returns dict keyed by week-start date string, each value being a list of
    article dicts with: id, title, published_date, sentiment_score,
    entities_mentioned (as list), state, location_relevance.
    """
    cutoff = (datetime.utcnow() - timedelta(weeks=weeks_back)).strftime("%Y-%m-%d")

    where = "analyzed = 1 AND published_date IS NOT NULL AND published_date >= ?"
    params = [cutoff]
    if state:
        where += " AND state = ?"
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


def compute_wsi(conn, state=None, weeks_back=WSI_WEEKS_BACK):
    """
    Compute the current Weighted Sentiment Index.

    Returns dict with:
      - current_wsi: the time-decay-weighted index value (1.0 - 5.0)
      - raw_avg: simple average of all articles in the period
      - week_count: number of weeks with data
    """
    buckets = compute_weekly_buckets(conn, state=state, weeks_back=weeks_back)
    if not buckets:
        return {"current_wsi": None, "raw_avg": None, "week_count": 0}

    now = datetime.utcnow()
    current_monday = now - timedelta(days=now.weekday())
    current_monday_str = current_monday.strftime("%Y-%m-%d")

    numerator = 0.0
    denominator = 0.0
    all_scores = []

    for week_start, articles in buckets.items():
        clusters = cluster_articles(articles)
        week_score = _compute_week_score(clusters)
        if week_score is None:
            continue

        # Time decay
        ws_date = datetime.strptime(week_start, "%Y-%m-%d")
        weeks_ago = (current_monday - ws_date).days / 7.0
        if weeks_ago < 0:
            weeks_ago = 0
        decay = 0.5 ** (weeks_ago / WSI_HALF_LIFE_WEEKS)

        numerator += week_score * decay
        denominator += decay

        for a in articles:
            if a["sentiment_score"] is not None:
                all_scores.append(a["sentiment_score"])

    current_wsi = round(numerator / denominator, 2) if denominator > 0 else None
    raw_avg = round(sum(all_scores) / len(all_scores), 2) if all_scores else None

    return {
        "current_wsi": current_wsi,
        "raw_avg": raw_avg,
        "week_count": len(buckets),
    }


def compute_wsi_trend(conn, state=None, weeks_back=WSI_WEEKS_BACK):
    """
    Compute the WSI time series for charting.

    Returns list of dicts sorted chronologically:
      {week_start, wsi, raw_avg, article_count, cluster_count, carried}

    Gaps (weeks with no articles) are filled with carried-forward values.
    """
    buckets = compute_weekly_buckets(conn, state=state, weeks_back=weeks_back)

    now = datetime.utcnow()
    current_monday = now - timedelta(days=now.weekday())

    # Build the full week range
    cutoff = datetime.utcnow() - timedelta(weeks=weeks_back)
    cutoff_monday = cutoff - timedelta(days=cutoff.weekday())

    # Compute per-week scores
    week_data = {}
    for week_start, articles in buckets.items():
        clusters = cluster_articles(articles)
        week_score = _compute_week_score(clusters)
        raw_scores = [a["sentiment_score"] for a in articles if a["sentiment_score"] is not None]
        raw_avg = round(sum(raw_scores) / len(raw_scores), 2) if raw_scores else None

        week_data[week_start] = {
            "week": week_start,
            "wsi": round(week_score, 2) if week_score is not None else None,
            "raw": raw_avg,
            "articles": len(articles),
            "clusters": len(clusters),
            "carried": False,
        }

    # Generate continuous weekly sequence and fill gaps
    trend = []
    monday = cutoff_monday
    last_known = None

    while monday <= current_monday:
        ws = monday.strftime("%Y-%m-%d")
        if ws in week_data:
            last_known = week_data[ws]
            trend.append(week_data[ws])
        else:
            # Carry forward
            if last_known is not None:
                trend.append({
                    "week": ws,
                    "wsi": last_known["wsi"],
                    "raw": last_known["raw"],
                    "articles": 0,
                    "clusters": 0,
                    "carried": True,
                })
            else:
                trend.append({
                    "week": ws,
                    "wsi": None,
                    "raw": None,
                    "articles": 0,
                    "clusters": 0,
                    "carried": True,
                })
        monday += timedelta(weeks=1)

    return trend


def compute_period_comparison(conn, state=None):
    """
    Compare current 4-week WSI vs prior 4-week WSI.

    Returns dict with current_4wk, prior_4wk, change, direction.
    """
    now = datetime.utcnow()
    current_monday = now - timedelta(days=now.weekday())

    # Current 4 weeks: most recent 4 complete weeks
    current_start = (current_monday - timedelta(weeks=4)).strftime("%Y-%m-%d")
    # Prior 4 weeks: the 4 weeks before that
    prior_start = (current_monday - timedelta(weeks=8)).strftime("%Y-%m-%d")

    buckets = compute_weekly_buckets(conn, state=state, weeks_back=8)

    current_scores = []
    prior_scores = []

    for week_start, articles in buckets.items():
        clusters = cluster_articles(articles)
        week_score = _compute_week_score(clusters)
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
        else:
            direction = "flat"

    return {
        "current_4wk": current_4wk,
        "prior_4wk": prior_4wk,
        "change": change,
        "direction": direction,
    }
