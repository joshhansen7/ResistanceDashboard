#!/bin/bash
# Wyoming Pulse — Cron Setup
# Run this once to install scheduled jobs

PULSE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="python3"  # Adjust if needed

echo "Wyoming Pulse — Installing cron jobs"
echo "Project directory: $PULSE_DIR"
echo ""

# Check RSS feeds every 12 hours (6am and 6pm)
(crontab -l 2>/dev/null; echo "0 6,18 * * * cd $PULSE_DIR && $PYTHON wyoming_pulse.py ingest >> logs/cron.log 2>&1") | crontab -

# Run analysis daily at 7am (after morning feed check)
(crontab -l 2>/dev/null; echo "0 7 * * * cd $PULSE_DIR && $PYTHON wyoming_pulse.py analyze >> logs/cron.log 2>&1") | crontab -

# Generate digest every other Monday at 8am
(crontab -l 2>/dev/null; echo "0 8 * * 1 cd $PULSE_DIR && $PYTHON wyoming_pulse.py digest >> logs/cron.log 2>&1") | crontab -

echo "Cron jobs installed. Current crontab:"
echo ""
crontab -l
echo ""
echo "Done! Feeds will be checked at 6am/6pm, analysis runs at 7am,"
echo "and digests are generated every Monday at 8am."
