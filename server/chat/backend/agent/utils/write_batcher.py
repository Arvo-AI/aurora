"""Best-effort batched SQL writer for high-volume audit rows."""

import logging
import os
import threading
import time
import weakref
from typing import Callable, List, Optional, Tuple

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

BATCH_FLUSH_INTERVAL_SECONDS: float = 1.5
_BATCHING_ENABLED: bool = os.getenv("MULTI_AGENT_BATCH_WRITES", "true").lower() == "true"
_MAX_QUEUE_SIZE: int = int(os.getenv("MULTI_AGENT_BATCH_MAX_QUEUE", "5000"))

QueueItem = Tuple[str, tuple, Optional[Callable[[object, object], None]]]


def batching_enabled() -> bool:
    return _BATCHING_ENABLED


class WriteBatcher:
    _instances: "weakref.WeakSet[WriteBatcher]" = weakref.WeakSet()
    _shutdown_thread_started: bool = False

    def __init__(self, name: str = "write_batcher") -> None:
        self.name = name
        self._buffer: List[QueueItem] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if _BATCHING_ENABLED:
            self._start_flusher()
        WriteBatcher._instances.add(self)

    def _start_flusher(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_flusher,
            name=f"{self.name}-flusher",
            daemon=True,
        )
        self._thread.start()

    def _run_flusher(self) -> None:
        while not self._stop_event.wait(BATCH_FLUSH_INTERVAL_SECONDS):
            try:
                self.flush()
            except Exception as e:
                logger.warning("%s flusher iteration failed: %s", self.name, e)

    def enqueue(
        self,
        sql: str,
        params: tuple,
        pre_commit_hook: Optional[Callable[[object, object], None]] = None,
    ) -> None:
        if not _BATCHING_ENABLED:
            self._execute_immediate([(sql, params, pre_commit_hook)])
            return
        with self._lock:
            if len(self._buffer) >= _MAX_QUEUE_SIZE:
                dropped = self._buffer.pop(0)
                logger.warning(
                    "%s buffer full (%d); dropping oldest write to keep memory bounded",
                    self.name, _MAX_QUEUE_SIZE,
                )
                del dropped
            self._buffer.append((sql, params, pre_commit_hook))

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            queued = self._buffer
            self._buffer = []
        self._execute_batch(queued)

    def _execute_immediate(self, items: List[QueueItem]) -> None:
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    for sql, params, hook in items:
                        if hook is not None:
                            try:
                                hook(cur, conn)
                            except Exception as e:
                                logger.warning("%s pre-commit hook failed: %s", self.name, e)
                        cur.execute(sql, params)
                conn.commit()
        except Exception as e:
            logger.warning("%s immediate write failed: %s", self.name, e)

    def _execute_batch(self, items: List[QueueItem]) -> None:
        if not items:
            return
        for attempt in (1, 2):
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cur:
                        for sql, params, hook in items:
                            if hook is not None:
                                try:
                                    hook(cur, conn)
                                except Exception as e:
                                    logger.warning("%s pre-commit hook failed: %s", self.name, e)
                            cur.execute(sql, params)
                    conn.commit()
                return
            except Exception as e:
                logger.warning("%s batch flush attempt %d failed: %s", self.name, attempt, e)
                time.sleep(0.05)
        logger.warning("%s dropping %d queued audit rows after retry failure", self.name, len(items))

    def shutdown(self) -> None:
        self._stop_event.set()
        try:
            self.flush()
        except Exception as e:
            logger.warning("%s shutdown flush failed: %s", self.name, e)

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass


_default_batcher: Optional[WriteBatcher] = None
_default_batcher_lock = threading.Lock()


def get_default_batcher() -> WriteBatcher:
    global _default_batcher
    with _default_batcher_lock:
        if _default_batcher is None:
            _default_batcher = WriteBatcher(name="default_write_batcher")
        return _default_batcher
