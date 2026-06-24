#!/bin/bash
# Prometheus Resistance Dashboard — Cron Setup
# Run this once to install scheduled jobs on a persistent host.
#
# Schedule (all daily):
#   05:30  infill   — gradually resolve Google News URLs + scrape thin content
#   06:00  run      — ingest feeds → analyze new articles → digest (when due)
#
# The 'infill' job runs first so freshly-resolved URLs are available to the
# analyze/scrape steps in the 'run' that follows. It is rate-limit-safe: it only
# resolves a bounded slice of the Google News backlog each day and backs off when
# Google returns HTTP 429.

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
30 5 * * * cd $DASHBOARD_DIR && $PYTHON resistance_dashboard.py infill >> logs/cron.log 2>&1
0 6 * * * cd $DASHBOARD_DIR && $PYTHON resistance_dashboard.py run >> logs/cron.log 2>&1
CRON
)"

printf '%s\n%s\n' "$EXISTING" "$NEW_JOBS" | grep -v '^$' | crontab -

echo "Cron jobs installed. Current crontab:"
echo ""
crontab -l
echo ""
echo "Done! Daily: infill at 5:30am, full pipeline (ingest→analyze→digest) at 6am."
echo "Logs: $DASHBOARD_DIR/logs/cron.log"
echo ""
echo "NOTE: On macOS, the terminal app running this may need 'Full Disk Access'"
echo "      (System Settings → Privacy & Security) for cron to read/write project files."
