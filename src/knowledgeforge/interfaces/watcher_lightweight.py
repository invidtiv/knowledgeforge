"""Lightweight filesystem watcher for KnowledgeForge.

This module provides a minimal watcher that uses the REST API instead of
loading the full KnowledgeForgeEngine. Reduces memory usage from ~7GB to ~100MB.
"""
import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional
import requests

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileMovedEvent

from knowledgeforge.config import KnowledgeForgeConfig

logger = logging.getLogger(__name__)


class LightweightWatcher:
    """Minimal filesystem watcher that triggers indexing via REST API.

    Uses ~100MB memory vs ~7GB for the full engine-based watcher.
    Also keeps a tiny in-process queue so many file changes don't flood the API.
    """

    def __init__(self, config: KnowledgeForgeConfig, api_url: str = "http://127.0.0.1:8742"):
        """Initialize lightweight watcher.
        
        Args:
            config: KnowledgeForge configuration
            api_url: Base URL for KnowledgeForge REST API
        """
        self.config = config
        self.api_url = api_url.rstrip("/")
        self._observer: Optional[Observer] = None
        self._running = False
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._queue: list[str] = []
        self._queue_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None

    def _is_supported(self, path: str) -> bool:
        """Check if file extension is supported."""
        ext = Path(path).suffix.lower()
        return ext in self.config.obsidian_extensions or ext in self.config.code_extensions

    def _should_ignore(self, path: str) -> bool:
        """Check if path matches ignore patterns."""
        for pattern in self.config.ignore_patterns:
            if pattern in path:
                return True
        return False

    def _index_file(self, path: str) -> bool:
        """Trigger file indexing via REST API."""
        try:
            url = f"{self.api_url}/api/v1/ingest"
            payload = {
                "path": path,
                "full_reindex": False
            }

            response = self._session.post(url, json=payload, timeout=120)

            if response.status_code == 200:
                result = response.json()
                logger.info(f"Indexed {path}: {result.get('chunks_created', 0)} chunks")
                return True
            else:
                logger.error(f"Failed to index {path}: HTTP {response.status_code}")
                return False

        except requests.exceptions.ConnectionError:
            logger.error(f"REST API unavailable at {self.api_url}")
            return False
        except Exception as e:
            logger.error(f"Error indexing {path}: {e}")
            return False

    def _enqueue_file(self, path: str) -> None:
        """Queue a file for serialized ingestion to avoid API flooding."""
        with self._queue_lock:
            if path not in self._queue:
                self._queue.append(path)

    def _worker_loop(self) -> None:
        """Process queued file ingests one at a time."""
        while self._running:
            path = None
            with self._queue_lock:
                if self._queue:
                    path = self._queue.pop(0)
            if not path:
                time.sleep(0.5)
                continue
            if os.path.exists(path):
                logger.info(f"Processing queued file: {path}")
                self._index_file(path)

    def _delete_file(self, path: str) -> bool:
        """Trigger file deletion via REST API.
        
        Args:
            path: Absolute path to deleted file
            
        Returns:
            True if deletion succeeded, False otherwise
        """
        try:
            source_file = os.path.basename(path)
            
            # Delete from both collections (file could be in either)
            for collection in [self.config.docs_collection, self.config.code_collection]:
                url = f"{self.api_url}/api/v1/collections/{collection}/documents"
                params = {"source_file": source_file}
                
                response = self._session.delete(url, params=params, timeout=10)
                
                if response.status_code == 200:
                    result = response.json()
                    deleted = result.get('deleted', 0)
                    if deleted > 0:
                        logger.info(f"Deleted {deleted} chunks for {source_file} from {collection}")
                        
            return True
            
        except requests.exceptions.ConnectionError:
            logger.error(f"REST API unavailable at {self.api_url}")
            return False
        except Exception as e:
            logger.error(f"Error deleting {path}: {e}")
            return False

    def start(self) -> None:
        """Start watching configured directories."""
        self._observer = Observer()
        handler = _LightweightHandler(self)
        
        # Watch Obsidian vault
        if self.config.obsidian_vault_path and os.path.isdir(self.config.obsidian_vault_path):
            self._observer.schedule(handler, self.config.obsidian_vault_path, recursive=True)
            logger.info(f"Watching vault: {self.config.obsidian_vault_path}")

        # Watch code projects
        for proj in self.config.project_paths:
            proj_path = proj.get("path", "")
            if proj_path and os.path.isdir(proj_path):
                self._observer.schedule(handler, proj_path, recursive=True)
                logger.info(f"Watching project: {proj_path}")

        self._observer.start()
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Lightweight watcher started (REST API mode)")

    def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2)
        self._session.close()
        logger.info("Lightweight watcher stopped")

    def is_running(self) -> bool:
        """Check if watcher is running."""
        return self._running


class _LightweightHandler(FileSystemEventHandler):
    """Handles file events with debouncing - REST API version."""

    def __init__(self, watcher: LightweightWatcher):
        self.watcher = watcher
        self._pending: dict = {}  # path -> timestamp
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def on_modified(self, event):
        if event.is_directory:
            return
        self._schedule(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        self._schedule(event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        path = event.src_path
        if self.watcher._should_ignore(path) or not self.watcher._is_supported(path):
            return
        logger.info(f"File deleted: {path}")
        self.watcher._delete_file(path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # Treat as delete + create
        self.on_deleted(type('Event', (), {'is_directory': False, 'src_path': event.src_path})())
        self._schedule(event.dest_path)

    def _schedule(self, path: str):
        """Schedule a debounced re-ingestion."""
        if self.watcher._should_ignore(path) or not self.watcher._is_supported(path):
            return

        with self._lock:
            self._pending[path] = time.time()

        # Reset debounce timer
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(
            self.watcher.config.watch_debounce_seconds, 
            self._process_pending
        )
        self._timer.daemon = True
        self._timer.start()

    def _process_pending(self):
        """Process all pending file changes."""
        with self._lock:
            paths = list(self._pending.keys())
            self._pending.clear()

        for path in paths:
            if os.path.exists(path):
                logger.info(f"Queueing changed file: {path}")
                self.watcher._enqueue_file(path)
