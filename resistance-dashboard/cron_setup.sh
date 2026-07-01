#!/bin/bash
# Prometheus Resistance Dashboard — Cron Setup
# Run this once to install scheduled jobs on a persistent host.
#
# Schedule (daily):
#   06:00  run  — unified pipeline: search sweep (2-day window) → resolve Google
#                 News URLs + scrape content → analyze → digest (when due)
#
# The pipeline is self-contained and correctly ordered: the 50-state search
# sweep runs first, then URL resolution + scraping enrich the new articles, then
# sentiment analysis runs on the enriched text, then a digest is generated when
# due. The resolve step is rate-limit-safe (bounded slice per run with HTTP 429
# backoff). RSS feeds have been retired — search is the sole article source.

DASHBOARD_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer the project virtualenv if present, else fall back to system python3.
if [ -x "$DASHBOARD_DIR/.venv/bin/python" ]; then
  PYTHON="$DASHBOARD_DIR/.venv/bin/python"
else
  PYTHON="python3"
fi

echo "Prometheus Resistance Dashboard — Installing cron jobs"
echo "Project directory: $DASHBOARD_DIR"
echo "Python:            $PYTHON"
echo ""

mkdir -p "$DASHBOARD_DIR/logs"

# Remove any previously-installed dashboard cron lines so re-running is idempotent.
EXISTING="$(crontab -l 2>/dev/null | grep -v "resistance_dashboard.py")"

NEW_JOBS="$(cat <<CRON
# ── Prometheus Resistance Dashboard ──
0 6 * * * cd $DASHBOARD_DIR && $PYTHON resistance_dashboard.py run >> logs/cron.log 2>&1
CRON
)"

printf '%s\n%s\n' "$EXISTING" "$NEW_JOBS" | grep -v '^$' | crontab -

echo "Cron jobs installed. Current crontab:"
echo ""
crontab -l
echo ""
echo "Done! Daily unified pipeline (sweep→resolve/scrape→analyze→digest) at 6am."
echo "Logs: $DASHBOARD_DIR/logs/cron.log"
echo ""
echo "NOTE: On macOS, the terminal app running this may need 'Full Disk Access'"
echo "      (System Settings → Privacy & Security) for cron to read/write project files."
