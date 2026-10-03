"""Простой asyncio-планировщик с рандомным интервалом."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


class RandomIntervalJob:
    def __init__(
        self,
        name: str,
        func: Callable[[], Awaitable[None]],
        min_sec: int,
        max_sec: int,
        run_immediately: bool = True,
    ):
        self.name = name
        self.func = func
        self.min_sec = min_sec
        self.max_sec = max_sec
        self.run_immediately = run_immediately
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.next_at: Optional[float] = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name=f"job-{self.name}")

    def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            if self.run_immediately:
                await self._run_once()
            while not self._stop.is_set():
                delay = random.randint(self.min_sec, self.max_sec)
                self.next_at = asyncio.get_event_loop().time() + delay
                log.info("[%s] следующий запуск через %d с", self.name, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break  # _stop был установлен — выходим
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
                await self._run_once()
        except asyncio.CancelledError:
            log.info("[%s] остановлен", self.name)
            raise

    async def _run_once(self) -> None:
        try:
            log.info("[%s] запуск", self.name)
            await self.func()
        except Exception as exc:
            log.exception("[%s] ошибка: %s", self.name, exc)
