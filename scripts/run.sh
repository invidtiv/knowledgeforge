#!/bin/bash
# KnowledgeForge Service Runner
set -e

echo "=== Starting KnowledgeForge Services ==="

# Start REST API in background
echo "Starting REST API on port 8742..."
python -m knowledgeforge.interfaces.rest_api &
REST_PID=$!

# Start file watcher in background
echo "Starting filesystem watcher..."
knowledgeforge watch &
WATCH_PID=$!

echo ""
echo "Services running:"
echo "  REST API: http://127.0.0.1:8742 (PID: $REST_PID)"
echo "  Watcher:  Active (PID: $WATCH_PID)"
echo ""
echo "Press Ctrl+C to stop all services"

# Handle shutdown
trap "kill $REST_PID $WATCH_PID 2>/dev/null; echo 'Services stopped.'" EXIT

# Wait for any background process
wait
