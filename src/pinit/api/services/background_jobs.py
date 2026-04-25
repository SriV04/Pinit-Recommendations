from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


JobCallable = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class BackgroundJob:
    name: str
    handler: JobCallable


class BackgroundJobRunner:
    """Small in-process async worker pool for bounded background jobs."""

    def __init__(self, worker_count: int = 3):
        self.worker_count = max(1, worker_count)
        self._queue: asyncio.Queue[Optional[BackgroundJob]] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._workers:
                return

            for index in range(self.worker_count):
                task = asyncio.create_task(
                    self._worker_loop(index + 1),
                    name=f"background-job-worker-{index + 1}",
                )
                self._workers.append(task)

            logger.info("Started %d background job workers", self.worker_count)

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._workers:
                return

            for _ in self._workers:
                await self._queue.put(None)

            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            logger.info("Stopped background job workers")

    async def enqueue(self, name: str, handler: JobCallable) -> None:
        await self.start()
        await self._queue.put(BackgroundJob(name=name, handler=handler))
        logger.info("Queued background job '%s'", name)

    async def _worker_loop(self, worker_id: int) -> None:
        logger.info("Background worker %d ready", worker_id)
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                logger.info("Background worker %d shutting down", worker_id)
                return

            try:
                logger.info("Worker %d starting job '%s'", worker_id, job.name)
                await job.handler()
                logger.info("Worker %d completed job '%s'", worker_id, job.name)
            except Exception as exc:
                logger.error(
                    "Worker %d failed job '%s': %s",
                    worker_id,
                    job.name,
                    exc,
                    exc_info=True,
                )
            finally:
                self._queue.task_done()


_DEFAULT_WORKER_COUNT = int(os.getenv("LOCATION_ADD_WORKER_COUNT", "3"))
_runner = BackgroundJobRunner(worker_count=_DEFAULT_WORKER_COUNT)


def get_background_job_runner() -> BackgroundJobRunner:
    return _runner
