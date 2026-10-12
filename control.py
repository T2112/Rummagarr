from __future__ import annotations

import asyncio
import threading

STOPPED = "stopped"
RUNNING = "running"
PAUSED = "paused"


class WorkerControl:
    """Start, pause, resume, stop, and skip for the download worker.

    Pause blocks the worker at its next wait without clearing job info, the
    wanted selection, or the on-disk queue. Skip abandons one current item
    and lets the caller move on. Stop ends the run and leaves saved state in
    place.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = STOPPED
        self._skip_keys: set[str] = set()
        self._skip_next = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def is_stopped(self) -> bool:
        return self.state == STOPPED

    def is_paused(self) -> bool:
        return self.state == PAUSED

    def is_running(self) -> bool:
        return self.state == RUNNING

    def start(self) -> None:
        with self._lock:
            self._state = RUNNING

    def pause(self) -> None:
        with self._lock:
            if self._state == RUNNING:
                self._state = PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._state == PAUSED:
                self._state = RUNNING

    def stop(self) -> None:
        with self._lock:
            self._state = STOPPED
            self._skip_keys.clear()
            self._skip_next = False

    def arm_skip(self, key: str | None) -> None:
        """Skip `key` if the worker is on it, or the next item when `key` is empty."""
        with self._lock:
            if self._state == STOPPED:
                return
            if key:
                self._skip_keys.add(key)
            else:
                self._skip_next = True

    def _poll(self, key: str | None) -> tuple[str, bool]:
        with self._lock:
            if self._state == STOPPED:
                return "stop", False
            if key:
                if key in self._skip_keys:
                    self._skip_keys.discard(key)
                    return "skip", False
                if self._skip_next:
                    self._skip_next = False
                    return "skip", False
            elif self._skip_next:
                # End an idle wait, but let the next real item consume the skip.
                return "yield", False
            return "wait", self._state == PAUSED

    async def gate(
        self,
        key: str | None = None,
        sleep: float = 0.0,
        deadline: list[float] | None = None,
    ) -> str:
        """Wait out a pause, then optionally sleep for active seconds.

        Returns ``ok``, ``skip``, or ``stop``. Time spent paused is added to
        ``deadline[0]`` so a long pause does not expire the download.
        """
        loop = asyncio.get_running_loop()
        remaining = max(0.0, float(sleep))
        while True:
            action, paused = self._poll(key)
            if action == "stop":
                return "stop"
            if action == "skip":
                return "skip"
            if action == "yield":
                return "ok"
            if paused:
                started = loop.time()
                await asyncio.sleep(0.05)
                if deadline is not None:
                    deadline[0] += loop.time() - started
                continue
            if remaining <= 0:
                # A zero wait must still yield. Otherwise a pass that sends no
                # search runs the worker in a tight loop and the server reader
                # never gets to send a FileSearch.
                await asyncio.sleep(0)
                return "ok"
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            remaining -= step
