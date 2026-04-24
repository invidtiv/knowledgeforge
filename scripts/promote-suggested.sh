#!/usr/bin/env bash
# promote-suggested.sh — Interactively promote suggested discoveries to semantic memory
# Usage: bash promote-suggested.sh [--project NAME] [--limit N]
#
# Shows each suggestion and prompts for action:
#   [y] promote with suggested type and title
#   [e] promote with edits (change type or title)
#   [s] skip

set -euo pipefail

export KNOWLEDGEFORGE_CONFIG="${KNOWLEDGEFORGE_CONFIG:-/home/bsdev/knowledgeforge/config.yaml}"
KF="/home/bsdev/knowledgeforge/.venv/bin/knowledgeforge"

PROJECT=""
LIMIT=20

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

PROJECT_OPT=""
if [[ -n "$PROJECT" ]]; then
    PROJECT_OPT="--project $PROJECT"
fi

echo "=== Promotion Suggestions ==="
echo ""

# Show suggestions with commands
$KF semantic suggest-promotions $PROJECT_OPT --limit "$LIMIT" --commands

echo ""
echo "Starting interactive promotion..."
echo "(Copy discovery IDs from the table above)"
echo ""

PROMOTED=0
SKIPPED=0

while true; do
    read -p "Enter discovery_id to promote (or 'q' to quit): " DISC_ID
    if [[ "$DISC_ID" == "q" || "$DISC_ID" == "quit" ]]; then
        break
    fi

    if [[ -z "$DISC_ID" ]]; then
        continue
    fi

    read -p "Record type [fact/runbook/project_overview] (default: fact): " RTYPE
    RTYPE="${RTYPE:-fact}"

    read -p "Title (press Enter for auto-generated): " TITLE

    TITLE_OPT=""
    if [[ -n "$TITLE" ]]; then
        TITLE_OPT="--title \"$TITLE\""
    fi

    echo "Promoting..."
    eval $KF semantic promote-discovery "\"$DISC_ID\"" "$RTYPE" $TITLE_OPT
    PROMOTED=$((PROMOTED + 1))
    echo ""
done

echo ""
echo "=== Summary ==="
echo "Promoted: $PROMOTED"
echo "Done."
