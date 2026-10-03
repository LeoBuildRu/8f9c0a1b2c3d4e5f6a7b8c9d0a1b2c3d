"""Пул прокси и User-Agent с ротацией.

Принцип: возвращаем «свежий» прокси и UA для каждого запроса. Если запрос
падает — помечаем прокси как «битый» на N минут, и больше не используем,
пока кулдаун не истечёт.
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


# Лёгкий статический фолбэк, если fake_useragent не установлен / не работает.
_DEFAULT_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 "
    "Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]


def _load_dynamic_uas() -> List[str]:
    try:
        from fake_useragent import UserAgent
        ua = UserAgent()
        return [ua.random for _ in range(20)]
    except Exception:
        return []


@dataclass
class _ProxyState:
    url: str
    cooldown_until: float = 0.0
    fail_count: int = 0


@dataclass
class Identity:
    proxy: Optional[str]
    user_agent: str

    def as_requests_kwargs(self) -> Dict[str, object]:
        kw: Dict[str, object] = {"headers": {"User-Agent": self.user_agent}}
        if self.proxy:
            kw["proxies"] = {"http": self.proxy, "https": self.proxy}
        return kw


class ProxyPool:
    """Потокобезопасный пул прокси + UA."""

    def __init__(
        self,
        proxies: Iterable[str],
        user_agents: Iterable[str] = (),
        cooldown_seconds: float = 300.0,
        max_fails_before_drop: int = 5,
    ):
        self._lock = threading.Lock()
        self._states: List[_ProxyState] = [_ProxyState(url=p) for p in proxies if p]
        self._cooldown = cooldown_seconds
        self._max_fails = max_fails_before_drop

        uas = list(user_agents) or _load_dynamic_uas() or list(_DEFAULT_UAS)
        random.shuffle(uas)
        self._uas = uas
        self._ua_cycle = itertools.cycle(self._uas)

    @property
    def proxy_count(self) -> int:
        return len(self._states)

    def acquire(self) -> Identity:
        """Берёт случайный живой прокси (или None если их нет) + следующий UA."""
        ua = next(self._ua_cycle)
        with self._lock:
            now = time.time()
            available = [s for s in self._states if s.cooldown_until <= now]
            if not available:
                return Identity(proxy=None, user_agent=ua)
            state = random.choice(available)
            return Identity(proxy=state.url, user_agent=ua)

    def mark_success(self, identity: Identity) -> None:
        if not identity.proxy:
            return
        with self._lock:
            for s in self._states:
                if s.url == identity.proxy:
                    s.fail_count = 0
                    s.cooldown_until = 0.0
                    return

    def mark_failure(self, identity: Identity) -> None:
        if not identity.proxy:
            return
        with self._lock:
            for s in self._states:
                if s.url == identity.proxy:
                    s.fail_count += 1
                    s.cooldown_until = time.time() + self._cooldown
                    if s.fail_count >= self._max_fails:
                        # «карантин» подольше
                        s.cooldown_until = time.time() + self._cooldown * 4
                    return

    def stats(self) -> Dict[str, int]:
        now = time.time()
        with self._lock:
            alive = sum(1 for s in self._states if s.cooldown_until <= now)
            sleeping = len(self._states) - alive
        return {"total": len(self._states), "alive": alive, "cooldown": sleeping}
