#!/usr/bin/env bash
# review-stale.sh — Review stale semantic records interactively
# Usage: bash review-stale.sh [--days N] [--project NAME]
#
# For each stale record, shows content and prompts for action:
#   [r] mark as reviewed (still valid)
#   [a] archive (no longer relevant)
#   [s] skip

set -euo pipefail

export KNOWLEDGEFORGE_CONFIG="${KNOWLEDGEFORGE_CONFIG:-/home/bsdev/knowledgeforge/config.yaml}"
KF="/home/bsdev/knowledgeforge/.venv/bin/knowledgeforge"

DAYS=30
PROJECT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --days) DAYS="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

PROJECT_OPT=""
if [[ -n "$PROJECT" ]]; then
    PROJECT_OPT="--project $PROJECT"
fi

echo "=== Stale Semantic Record Review (>${DAYS} days) ==="
echo ""

# Get stale records as list output first
$KF semantic review-stale --days "$DAYS" $PROJECT_OPT

echo ""
echo "Starting interactive review..."
echo "(Use the record IDs and types shown above)"
echo ""

while true; do
    read -p "Enter record_type record_id (or 'q' to quit): " INPUT
    if [[ "$INPUT" == "q" || "$INPUT" == "quit" ]]; then
        echo "Review complete."
        break
    fi

    RECORD_TYPE=$(echo "$INPUT" | awk '{print $1}')
    RECORD_ID=$(echo "$INPUT" | awk '{print $2}')

    if [[ -z "$RECORD_TYPE" || -z "$RECORD_ID" ]]; then
        echo "Usage: <record_type> <record_id>  (e.g., 'fact abc12345-...')"
        continue
    fi

    echo ""
    echo "Searching for record content..."
    $KF semantic search "$RECORD_ID" --type "$RECORD_TYPE" --limit 1 2>/dev/null || true

    echo ""
    read -p "Action — [r]eview (still valid), [a]rchive, [s]kip: " ACTION

    case "$ACTION" in
        r|review)
            $KF semantic mark-reviewed "$RECORD_TYPE" "$RECORD_ID"
            echo "  -> Marked as reviewed"
            ;;
        a|archive)
            $KF semantic archive "$RECORD_TYPE" "$RECORD_ID"
            echo "  -> Archived"
            ;;
        s|skip)
            echo "  -> Skipped"
            ;;
        *)
            echo "  -> Unknown action, skipped"
            ;;
    esac
    echo ""
done
