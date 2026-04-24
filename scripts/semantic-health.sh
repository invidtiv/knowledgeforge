#!/usr/bin/env bash
# semantic-health.sh — Quick semantic memory health check with actionable output
# Usage: bash semantic-health.sh [--fix]
#
# Without --fix: shows audit summary and issues
# With --fix: shows audit + copy-paste commands to resolve issues

set -euo pipefail

export KNOWLEDGEFORGE_CONFIG="${KNOWLEDGEFORGE_CONFIG:-/home/bsdev/knowledgeforge/config.yaml}"
KF="/home/bsdev/knowledgeforge/.venv/bin/knowledgeforge"

FIX_MODE=false
if [[ "${1:-}" == "--fix" ]]; then
    FIX_MODE=true
fi

echo "=== Semantic Memory Health Check ==="
echo ""

if $FIX_MODE; then
    $KF semantic audit --commands
else
    $KF semantic audit
fi

echo ""
echo "=== Stale Records ==="
echo ""

if $FIX_MODE; then
    $KF semantic review-stale --days 30 --commands
else
    $KF semantic review-stale --days 30
fi

echo ""
echo "=== Promotion Suggestions ==="
echo ""

if $FIX_MODE; then
    $KF semantic suggest-promotions --commands
else
    $KF semantic suggest-promotions
fi

echo ""
echo "--- Health check complete ---"
if ! $FIX_MODE; then
    echo "Tip: Run with --fix to see actionable commands for each issue"
fi
