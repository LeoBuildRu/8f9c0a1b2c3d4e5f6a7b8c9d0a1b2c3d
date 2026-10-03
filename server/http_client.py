"""HTTP-клиент с ротацией прокси и UA.

`get(url, ...)` — синхронный requests.get с ретраями.
`get_async(...)` — то же через asyncio.to_thread, чтобы не блокировать loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict, Optional

import requests

from server.proxy_pool import Identity, ProxyPool

log = logging.getLogger(__name__)


class HttpClient:
    def __init__(
        self,
        pool: ProxyPool,
        max_attempts: int = 4,
        timeout: float = 20.0,
        proxy_required: bool = False,
    ):
        self.pool = pool
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.proxy_required = proxy_required

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Возвращает Response или None после исчерпания попыток."""
        timeout = kwargs.pop("timeout", self.timeout)
        params = kwargs.pop("params", None)
        for attempt in range(1, self.max_attempts + 1):
            identity: Identity = self.pool.acquire()
            if self.proxy_required and not identity.proxy:
                log.warning("Прокси обязательны, но пул пуст — пропускаем запрос %s", url)
                return None
            kw = identity.as_requests_kwargs()
            try:
                resp = requests.get(url, params=params, timeout=timeout, **kw)
                if resp.status_code >= 500:
                    raise requests.RequestException(f"server status {resp.status_code}")
                self.pool.mark_success(identity)
                return resp
            except (requests.RequestException, OSError) as exc:
                self.pool.mark_failure(identity)
                log.warning(
                    "HTTP попытка %d/%d через %s упала: %s",
                    attempt, self.max_attempts, identity.proxy or "direct", exc,
                )
                time.sleep(min(2.0 * attempt, 6.0) + random.random())
        return None

    async def get_async(self, url: str, **kwargs) -> Optional[requests.Response]:
        return await asyncio.to_thread(self.get, url, **kwargs)
