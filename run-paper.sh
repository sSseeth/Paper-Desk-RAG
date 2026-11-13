#!/usr/bin/env bash
# Scheduler entry (guide p17): one AUTO_PAPER cycle on the main paper account.
# Refuses until the mechanics lifecycle gate has passed. Log exit status, return it.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
./.venv/bin/python desk.py cycle --mode AUTO_PAPER
rc=$?
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-paper exit=$rc" >> logs/launcher.log
exit $rc
