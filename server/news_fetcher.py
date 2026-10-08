"""Headless парсер Telegram-канала через /s/ web-preview.

t.me/s/<channel> отдаёт публичную HTML-страницу с последними сообщениями.
Парсим BeautifulSoup без Playwright.

На российских VPS домен t.me блокирован так же, как api.telegram.org,
поэтому фетчер умеет ходить через выделенный прокси (тот же SOCKS5,
который используется ботом). MOEX/FINAM-трафик при этом не страдает —
он гоняется через общий ProxyPool (или напрямую).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from server.http_client import HttpClient

log = logging.getLogger(__name__)


class TelegramNewsFetcher:
    MAX_GAP_PAGES = 20  # догрузка пропуска: до ~400 постов за раз

    def __init__(
        self,
        http: HttpClient,
        channel: str,
        news_file: Path,
        proxy_url: Optional[str] = None,
        max_attempts: int = 3,
        timeout: float = 25.0,
    ):
        self.http = http
        self.channel = channel
        self.news_file = Path(news_file)
        self.url = f"https://t.me/s/{channel}"
        self.proxy_url = proxy_url
        self.max_attempts = max_attempts
        self.timeout = timeout

    def load(self) -> List[Dict[str, Any]]:
        if not self.news_file.exists():
            return []
        try:
            return json.loads(self.news_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, items: List[Dict[str, Any]]) -> None:
        self.news_file.parent.mkdir(parents=True, exist_ok=True)
        self.news_file.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_html(self, url: str) -> Optional[str]:
        if self.proxy_url:
            return self._fetch_via_dedicated_proxy(url)
        resp = self.http.get(url)
        return resp.text if resp is not None and resp.status_code == 200 else None

    @staticmethod
    def _post_id(link: str) -> Optional[int]:
        m = re.search(r"/(\d+)$", link or "")
        return int(m.group(1)) if m else None

    def fetch(self) -> int:
        """Скачивает новые посты канала в JSON. Возвращает кол-во новых.

        Первая страница веб-версии — только ~20 последних постов. Если с прошлой
        загрузки вышло больше (всплеск новостей, простой сервера), догружаем
        страницы ?before=<номер>, пока не дойдём до уже сохранённого поста.
        """
        existing = self.load()
        existing_links = {n.get("link") for n in existing if n.get("link")}
        known_ids = [i for i in (self._post_id(link) for link in existing_links) if i]
        last_known = max(known_ids) if known_ids else None

        messages = []
        url = self.url
        for page in range(self.MAX_GAP_PAGES):
            html = self._get_html(url)
            if not html:
                log.warning("Не удалось загрузить %s", url)
                break
            page_msgs = BeautifulSoup(html, "html.parser").select(".tgme_widget_message[data-post]")
            if not page_msgs:
                break
            messages.extend(page_msgs)
            oldest = min(int(m["data-post"].rsplit("/", 1)[-1]) for m in page_msgs)
            if last_known is None or oldest <= last_known + 1:
                break
            url = f"{self.url}?before={oldest}"
            time.sleep(1.0 + random.random())
        if not messages:
            log.info("Сообщений не найдено на %s", self.url)
            return 0

        new_items: List[Dict[str, Any]] = []
        for msg in messages:
            text_el = msg.select_one(".tgme_widget_message_text")
            if text_el is None:
                continue
            text = text_el.get_text("\n").strip()
            if not text:
                continue

            link_el = msg.select_one(".tgme_widget_message_date")
            link = link_el.get("href") if link_el else ""
            if link in existing_links:
                continue

            time_el = msg.select_one(".tgme_widget_message_date time")
            raw_dt = time_el.get("datetime") if time_el else None
            formatted = self._format_dt(raw_dt)

            new_items.append({
                "id": self._post_id(link),
                "text": re.sub(r"\s+\n", "\n", text),
                "date": formatted,
                "link": link,
                "parsed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            })
            existing_links.add(link)

        if new_items:
            merged = sorted(existing + new_items, key=lambda n: self._post_id(n.get("link")) or 0)
            # Храним последние 20 000 постов (~2 года канала), чтобы файл не разрастался
            merged = merged[-20000:]
            self.save(merged)

        # Показываем диапазон дат сообщений, попавших в выдачу.
        # Это полезно, чтобы понять: парсер реально видит свежие новости
        # или Telegram отдаёт ему старую кешированную страницу.
        dates = [
            (m.select_one(".tgme_widget_message_date time") or {}).get("datetime", "")
            if m.select_one(".tgme_widget_message_date time") else ""
            for m in messages
        ]
        dates = [d for d in dates if d]
        if dates:
            log.info(
                "Новости: загрузка %s — найдено %d сообщений (от %s до %s), новых %d",
                self.channel, len(messages), min(dates), max(dates), len(new_items),
            )
        else:
            log.info(
                "Новости: загрузка %s — найдено %d сообщений, новых %d",
                self.channel, len(messages), len(new_items),
            )
        return len(new_items)

    # ── Выделенный прокси для t.me ───────────────────────────────────────────
    def _fetch_via_dedicated_proxy(self, url: str) -> Optional[str]:
        """Запрос через тот же SOCKS5, что использует Telegram-бот.

        UA ротатируется через общий ProxyPool, чтобы запросы выглядели
        как живой посетитель веб-превью.

        Важно: для SOCKS5 принудительно используем схему ``socks5h://`` —
        DNS должен резолвиться на стороне прокси (в Нидерландах), иначе
        московский DNS блокирует t.me ещё до соединения.
        """
        proxy_url = self.proxy_url or ""
        if proxy_url.startswith("socks5://"):
            proxy_url = "socks5h://" + proxy_url[len("socks5://"):]
        proxies = {"http": proxy_url, "https": proxy_url}
        for attempt in range(1, self.max_attempts + 1):
            identity = self.http.pool.acquire()
            headers = {
                "User-Agent": identity.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    timeout=self.timeout,
                )
                if resp.status_code == 200 and "tgme_widget_message" in resp.text:
                    return resp.text
                log.warning(
                    "t.me попытка %d/%d вернула %s",
                    attempt, self.max_attempts, resp.status_code,
                )
            except (requests.RequestException, OSError) as exc:
                log.warning(
                    "t.me попытка %d/%d через прокси упала: %s",
                    attempt, self.max_attempts, exc,
                )
            time.sleep(min(3.0 * attempt, 8.0) + random.random())
        return None

    @staticmethod
    def _format_dt(value: Any) -> str:
        if not value:
            return "Неизвестно"
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)
