"""Filesystem watcher for live vault and project sync."""
import os
import time
import logging
import threading
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileMovedEvent

from knowledgeforge.config import KnowledgeForgeConfig

logger = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    """Handles file events with debouncing."""

    def __init__(self, engine, config: KnowledgeForgeConfig):
        self.engine = engine
        self.config = config
        self._pending = {}  # path -> timestamp
        self._lock = threading.Lock()
        self._timer = None

    def _should_ignore(self, path: str) -> bool:
        """Check if path matches ignore patterns."""
        for pattern in self.config.ignore_patterns:
            if pattern in path:
                return True
        return False

    def _is_supported(self, path: str) -> bool:
        """Check if file extension is supported."""
        ext = Path(path).suffix.lower()
        return ext in self.config.obsidian_extensions or ext in self.config.code_extensions

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
        if self._should_ignore(path) or not self._is_supported(path):
            return
        logger.info(f"File deleted: {path}")
        # Delete chunks for this file from store
        try:
            source_file = os.path.basename(path)
            self.engine.store.delete_by_source_file(self.engine.config.docs_collection, source_file)
            self.engine.store.delete_by_source_file(self.engine.config.code_collection, source_file)
        except Exception as e:
            logger.error(f"Failed to handle deletion of {path}: {e}")

    def on_moved(self, event):
        if event.is_directory:
            return
        # Treat as delete + create
        self.on_deleted(type('Event', (), {'is_directory': False, 'src_path': event.src_path})())
        self._schedule(event.dest_path)

    def _schedule(self, path: str):
        """Schedule a debounced re-ingestion."""
        if self._should_ignore(path) or not self._is_supported(path):
            return

        with self._lock:
            self._pending[path] = time.time()

        # Reset debounce timer
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.config.watch_debounce_seconds, self._process_pending)
        self._timer.daemon = True
        self._timer.start()

    def _process_pending(self):
        """Process all pending file changes."""
        with self._lock:
            paths = list(self._pending.keys())
            self._pending.clear()

        for path in paths:
            if os.path.exists(path):
                logger.info(f"Re-ingesting changed file: {path}")
                try:
                    result = self.engine.ingest_file(path)
                    logger.info(f"  -> {result.chunks_created} chunks created")
                except Exception as e:
                    logger.error(f"Failed to re-ingest {path}: {e}")


class VaultWatcher:
    """Watches filesystem for changes and triggers re-ingestion."""

    def __init__(self, engine, config: KnowledgeForgeConfig):
        self.engine = engine
        self.config = config
        self._observer = None
        self._running = False

    def start(self):
        """Start watching configured directories."""
        self._observer = Observer()
        handler = _DebouncedHandler(self.engine, self.config)

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
        logger.info("Filesystem watcher started")

    def stop(self):
        """Stop watching."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
        self._running = False
        logger.info("Filesystem watcher stopped")

    def is_running(self) -> bool:
        return self._running
