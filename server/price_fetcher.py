"""Воркер обновления текущих котировок MOEX.

Тянет последние цены по всем тикерам корзины с ротацией прокси/UA.
Кешируется в JSON-файл — Telegram-бот использует его, чтобы показывать
актуальные P&L по открытым позициям без ожидания следующего tick().
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

from server.http_client import HttpClient

log = logging.getLogger(__name__)

MOEX_MARKDATA_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"
)
MOEX_INDEX_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities.json"
)


class PriceFetcher:
    def __init__(self, http: HttpClient, cache_file: Path):
        self.http = http
        self.cache_file = Path(cache_file)
        self._last: Dict[str, float] = self._load()

    # ── Кеш ──────────────────────────────────────────────────────────────────
    def _load(self) -> Dict[str, float]:
        if not self.cache_file.exists():
            return {}
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return data.get("prices", {})
        except Exception:
            return {}

    def _save(self) -> None:
        payload = {"updated_at": datetime.utcnow().isoformat(timespec="seconds"), "prices": self._last}
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── API ──────────────────────────────────────────────────────────────────
    @property
    def prices(self) -> Dict[str, float]:
        return dict(self._last)

    def price(self, ticker: str) -> Optional[float]:
        return self._last.get(ticker)

    # ── Загрузка ─────────────────────────────────────────────────────────────
    def refresh(self, tickers: Iterable[str]) -> Dict[str, float]:
        """Обновляет цены для указанных тикеров. Возвращает свежий словарь."""
        wanted = set(tickers)

        updated: Dict[str, float] = {}
        updated.update(self._fetch(MOEX_MARKDATA_URL, wanted))
        if "IMOEX" in wanted and "IMOEX" not in updated:
            updated.update(self._fetch(MOEX_INDEX_URL, {"IMOEX"}))

        if updated:
            self._last.update(updated)
            self._save()
            log.info("Обновлено цен: %d (из %d запрошенных)", len(updated), len(wanted))
        else:
            log.warning("Цены не обновлены: ответ MOEX пустой")
        return self.prices

    # ── Внутренние ──────────────────────────────────────────────────────────
    def _fetch(self, url: str, wanted: Iterable[str]) -> Dict[str, float]:
        params = {
            "iss.meta": "off",
            "iss.only": "marketdata",
            "marketdata.columns": "SECID,LAST,LCURRENTPRICE",
        }
        resp = self.http.get(url, params=params)
        if resp is None or resp.status_code != 200:
            return {}
        try:
            payload = resp.json().get("marketdata", {})
            columns = payload.get("columns", [])
            data = payload.get("data", [])
        except Exception as exc:
            log.warning("Ошибка разбора %s: %s", url, exc)
            return {}

        if not data:
            return {}

        wanted = set(wanted)
        out: Dict[str, float] = {}
        idx_secid = columns.index("SECID") if "SECID" in columns else 0
        try:
            idx_last = columns.index("LAST")
        except ValueError:
            idx_last = 1
        try:
            idx_lcp = columns.index("LCURRENTPRICE")
        except ValueError:
            idx_lcp = -1

        for row in data:
            try:
                secid = row[idx_secid]
                if secid not in wanted:
                    continue
                last = row[idx_last] if idx_last >= 0 and idx_last < len(row) else None
                lcp = row[idx_lcp] if idx_lcp >= 0 and idx_lcp < len(row) else None
                price = last if last not in (None, 0) else lcp
                if price not in (None, 0):
                    out[secid] = float(price)
            except (IndexError, ValueError, TypeError):
                continue
        return out
