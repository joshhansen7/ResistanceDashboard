#!/bin/bash
# Prometheus Resistance Dashboard — Cron Setup
# Run this once to install scheduled jobs

DASHBOARD_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="python3"  # Adjust if needed

echo "Prometheus Resistance Dashboard — Installing cron jobs"
echo "Project directory: $DASHBOARD_DIR"
echo ""

# Check RSS feeds every 12 hours (6am and 6pm)
(crontab -l 2>/dev/null; echo "0 6,18 * * * cd $DASHBOARD_DIR && $PYTHON resistance_dashboard.py ingest >> logs/cron.log 2>&1") | crontab -

# Run analysis daily at 7am (after morning feed check)
(crontab -l 2>/dev/null; echo "0 7 * * * cd $DASHBOARD_DIR && $PYTHON resistance_dashboard.py analyze >> logs/cron.log 2>&1") | crontab -

# Generate digest every other Monday at 8am
(crontab -l 2>/dev/null; echo "0 8 * * 1 cd $DASHBOARD_DIR && $PYTHON resistance_dashboard.py digest >> logs/cron.log 2>&1") | crontab -

echo "Cron jobs installed. Current crontab:"
echo ""
crontab -l
echo ""
echo "Done! Feeds will be checked at 6am/6pm, analysis runs at 7am,"
echo "and digests are generated every Monday at 8am."
