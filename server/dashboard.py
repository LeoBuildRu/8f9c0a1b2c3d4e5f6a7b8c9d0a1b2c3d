"""Дашборд LeoAgent: веб-интерфейс только для чтения.

Слушает 127.0.0.1 — из интернета недоступен. С ПК открывается через
SSH-туннель: dashboard.bat в корне проекта поднимает туннель и окно.

Данные: БД агента (портфель, сделки, журнал), архив новостей и котировки
MOEX ISS (бумаги пула, IMOEX, курсы ЦБ, юань). Раз в минуту пишет точку
капитала в data/equity_history.jsonl — из неё строится кривая капитала.

Запуск:
    python -m server.dashboard        # порт LEOAGENT_DASHBOARD_PORT, по умолчанию 8765
"""

from __future__ import annotations

import bisect
import calendar
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from app.agent.news_analyzer import NewsAnalyzer
from app.agent.risk import RiskParams
from app.agent.trader_agent import PRIORITY_TICKERS, TraderAgent
from server import config

log = logging.getLogger("leoagent.dashboard")

HOST = "127.0.0.1"
PORT = int(os.environ.get("LEOAGENT_DASHBOARD_PORT", "8765"))
UI_FILE = Path(__file__).resolve().parent / "dashboard_ui" / "index.html"
EQUITY_FILE = config.DATA_DIR / "equity_history.jsonl"
EQUITY_SAMPLE_SEC = 60
ISS = "https://iss.moex.com/iss"
MSK = timedelta(hours=3)
HEARTBEAT_STALE_SEC = 15 * 60  # тик раз в 5–10 мин; дольше 15 — что-то не так
# Таймфрейм графика → (интервал ISS, глубина истории в днях)
TIMEFRAMES = {"10m": (10, 4), "1h": (60, 30), "1d": (24, 400)}
CHART_TICKERS = set(PRIORITY_TICKERS) | {"IMOEX"}


# ── Время ───────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    """Наивное UTC, как в БД агента (datetime.utcnow() устарел)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_utc(value: Any) -> Optional[datetime]:
    """Время из БД и новостей: наивное UTC в ISO-формате."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")[:19])
    except ValueError:
        return None


def _ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC → миллисекунды эпохи (для JS Date)."""
    return calendar.timegm(dt.timetuple()) * 1000 if dt else None


def _chart_time(msk: datetime) -> int:
    """Время по Москве как будто в UTC: график подписывает ось в UTC,
    так подписи совпадают с московским временем биржи."""
    return calendar.timegm(msk.timetuple())


# ── Кэш и запросы к MOEX ────────────────────────────────────────────────────
class TTLCache:
    """Кэш с временем жизни. Если обновить не удалось — отдаёт прошлое значение."""

    def __init__(self) -> None:
        self._data: Dict[Any, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, ttl: float, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        try:
            value = fn()
        except Exception:
            if hit:
                log.warning("Не удалось обновить %s, отдаю прошлое значение", key, exc_info=True)
                return hit[1]
            raise
        with self._lock:
            self._data[key] = (now, value)
        return value


cache = TTLCache()


def _iss(path: str, **params: Any) -> Dict[str, Any]:
    r = requests.get(f"{ISS}/{path}", params={"iss.meta": "off", **params}, timeout=15)
    r.raise_for_status()
    return r.json()


def _rows(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(zip(block["columns"], row)) for row in block.get("data", [])]


def _fetch_quotes() -> Dict[str, Any]:
    j = _iss(
        "engines/stock/markets/shares/boards/TQBR/securities.json",
        securities=",".join(PRIORITY_TICKERS),
        **{
            "iss.only": "securities,marketdata",
            "securities.columns": "SECID,SHORTNAME,PREVPRICE,LOTSIZE,DECIMALS",
            "marketdata.columns": "SECID,LAST,LCURRENTPRICE,LASTTOPREVPRICE,VALTODAY,UPDATETIME",
        },
    )
    info = {r["SECID"]: r for r in _rows(j["securities"])}
    stocks = {}
    for r in _rows(j["marketdata"]):
        s = info.get(r["SECID"], {})
        prev = s.get("PREVPRICE")
        price = r["LAST"] or r["LCURRENTPRICE"] or prev
        change = r["LASTTOPREVPRICE"]
        if change is None and price and prev:
            change = (price / prev - 1) * 100
        stocks[r["SECID"]] = {
            "ticker": r["SECID"],
            "name": s.get("SHORTNAME") or r["SECID"],
            "price": price,
            "change_pct": change,
            "decimals": s.get("DECIMALS"),
            "lot": s.get("LOTSIZE"),
            "value_today": r["VALTODAY"],
            "updated": r["UPDATETIME"],
        }

    imoex = _rows(_iss(
        "engines/stock/markets/index/boards/SNDX/securities/IMOEX.json",
        **{"iss.only": "marketdata", "marketdata.columns": "CURRENTVALUE,LASTCHANGEPRC,UPDATETIME"},
    )["marketdata"])[0]
    cbr = _rows(_iss("statistics/engines/currency/markets/selt/rates.json")["cbrf"])[0]
    cny = _rows(_iss(
        "engines/currency/markets/selt/boards/CETS/securities/CNYRUB_TOM.json",
        **{"iss.only": "marketdata", "marketdata.columns": "LAST,WAPRICE,LASTTOPREVPRICE"},
    )["marketdata"])[0]

    return {
        "stocks": [stocks[t] for t in PRIORITY_TICKERS if t in stocks],
        "imoex": {
            "value": imoex["CURRENTVALUE"],
            "change_pct": imoex["LASTCHANGEPRC"],
            "updated": imoex["UPDATETIME"],
        },
        "fx": [
            {"code": "USD", "label": "USD · ЦБ", "value": cbr["CBRF_USD_LAST"], "change_pct": cbr["CBRF_USD_LASTCHANGEPRCNT"]},
            {"code": "EUR", "label": "EUR · ЦБ", "value": cbr["CBRF_EUR_LAST"], "change_pct": cbr["CBRF_EUR_LASTCHANGEPRCNT"]},
            {"code": "CNY", "label": "CNY · биржа", "value": cny["LAST"] or cny["WAPRICE"], "change_pct": cny["LASTTOPREVPRICE"]},
        ],
    }


def quotes() -> Dict[str, Any]:
    return cache.get("quotes", 15, _fetch_quotes)


def _fallback_prices() -> Dict[str, float]:
    """Цены, которые агент сам кэширует, — если MOEX сейчас недоступен."""
    try:
        return json.loads(config.PRICE_CACHE_FILE.read_text(encoding="utf-8")).get("prices", {})
    except Exception:
        return {}


def current_prices() -> Dict[str, float]:
    try:
        return {s["ticker"]: s["price"] for s in quotes()["stocks"] if s["price"]}
    except Exception:
        return _fallback_prices()


# ── БД агента ───────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _position_view(t: Dict[str, Any], price: float, now: datetime) -> Dict[str, Any]:
    is_long = t["side"] == "LONG"
    sign = 1 if is_long else -1
    entry, qty = t["entry_price"], t["qty"]
    sl, tp = t["stop_loss"], t["take_profit"]
    span = (tp - sl) if is_long else (sl - tp)

    def progress(p: float) -> float:
        # 0 — цена у стопа, 1 — у тейка
        raw = ((p - sl) if is_long else (sl - p)) / span if span else 0.5
        return max(0.0, min(1.0, raw))

    deadline = _parse_utc(t["deadline_at"])
    return {
        "id": t["id"],
        "ticker": t["ticker"],
        "side": t["side"],
        "qty": qty,
        "entry": entry,
        "price": price,
        "stop_loss": sl,
        "take_profit": tp,
        "pnl_abs": (price - entry) * qty * sign,
        "pnl_pct": (price / entry - 1) * 100 * sign if entry else 0.0,
        "value": qty * price if is_long else qty * (2 * entry - price),
        "progress": progress(price),
        "entry_progress": progress(entry),
        "opened_ms": _ms(_parse_utc(t["opened_at"])),
        "deadline_ms": _ms(deadline),
        "hours_left": (deadline - now).total_seconds() / 3600 if deadline else None,
    }


def _read_equity_file() -> List[Dict[str, Any]]:
    if not EQUITY_FILE.exists():
        return []
    points = []
    for line in EQUITY_FILE.read_text(encoding="utf-8").splitlines():
        try:
            points.append(json.loads(line))
        except ValueError:
            continue
    return points


def _equity_history() -> List[Dict[str, Any]]:
    # Файл растёт на 1440 строк в сутки — не перечитываем на каждый запрос
    return cache.get("equity_history", 30, _read_equity_file)


def build_state() -> Dict[str, Any]:
    now = _utcnow()
    prices = current_prices()
    with closing(_db()) as c:
        pf = dict(c.execute("SELECT initial_capital, cash FROM portfolio WHERE id = 1").fetchone())
        open_rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY opened_at")]
        closed = [r["pnl_abs"] or 0.0 for r in c.execute("SELECT pnl_abs FROM trades WHERE status = 'CLOSED'")]
        last_decision = c.execute("SELECT MAX(created_at) FROM decisions").fetchone()[0]

    positions = [_position_view(t, prices.get(t["ticker"], t["entry_price"]), now) for t in open_rows]
    initial, cash = pf["initial_capital"], pf["cash"]
    equity = cash + sum(p["value"] for p in positions)
    gross_win = sum(x for x in closed if x > 0)
    gross_loss = -sum(x for x in closed if x <= 0)

    # P&L за сегодняшний день по Москве: от первой точки кривой капитала за день
    today_msk = (now + MSK).date()
    day_start = next(
        (p["equity"] for p in _equity_history()
         if (_parse_utc(p["t"]) or now) + MSK >= datetime.combine(today_msk, datetime.min.time())),
        None,
    )

    tick_at = _parse_utc(last_decision)
    return {
        "now_ms": _ms(now),
        "session_open": TraderAgent._session_open(now),
        "agent": {
            "last_tick_ms": _ms(tick_at),
            "stale": tick_at is None or (now - tick_at).total_seconds() > HEARTBEAT_STALE_SEC,
        },
        "portfolio": {
            "initial": initial,
            "cash": cash,
            "equity": equity,
            "invested": sum(p["value"] for p in positions),
            "unrealized": sum(p["pnl_abs"] for p in positions),
            "realized": sum(closed),
            "total_return_pct": (equity / initial - 1) * 100 if initial else 0.0,
            "today_pnl": equity - day_start if day_start is not None else None,
            "open_count": len(positions),
            "max_open": RiskParams().max_open_positions,
            "closed_count": len(closed),
            "wins": sum(1 for x in closed if x > 0),
            "profit_factor": gross_win / gross_loss if gross_loss else None,
        },
        "positions": positions,
    }


def closed_trades(limit: int) -> List[Dict[str, Any]]:
    with closing(_db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?", (limit,))]
    out = []
    for t in rows:
        opened, closed_at = _parse_utc(t["opened_at"]), _parse_utc(t["closed_at"])
        out.append({
            "id": t["id"],
            "ticker": t["ticker"],
            "side": t["side"],
            "qty": t["qty"],
            "entry": t["entry_price"],
            "exit": t["close_price"],
            "reason": t["close_reason"],
            "pnl_abs": t["pnl_abs"],
            "pnl_pct": t["pnl_pct"],
            "opened_ms": _ms(opened),
            "closed_ms": _ms(closed_at),
            "hours": (closed_at - opened).total_seconds() / 3600 if opened and closed_at else None,
        })
    return out


def decisions(limit: int, include_hold: bool) -> List[Dict[str, Any]]:
    where = "" if include_hold else "WHERE action != 'HOLD'"
    with closing(_db()) as c:
        rows = c.execute(
            f"SELECT id, created_at, ticker, action, confidence, rationale FROM decisions {where} "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{
        "id": r["id"],
        "ms": _ms(_parse_utc(r["created_at"])),
        "ticker": r["ticker"],
        "action": r["action"],
        "confidence": r["confidence"],
        "rationale": r["rationale"],
    } for r in rows]


def news(limit: int) -> List[Dict[str, Any]]:
    try:
        items = json.loads(config.NEWS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    items.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    out = []
    for item in items[:limit]:
        text = (item.get("text") or "").strip()
        sentiment, political = NewsAnalyzer._score_text(text)
        out.append({
            "ms": _ms(_parse_utc(item.get("date"))),
            "text": text[:600],
            "link": item.get("link"),
            "sentiment": sentiment,
            "political": political,
            "tickers": NewsAnalyzer._mentions(text),
        })
    return out


# ── Графики ─────────────────────────────────────────────────────────────────
def _fetch_candles(ticker: str, tf: str) -> List[Dict[str, Any]]:
    interval, days = TIMEFRAMES[tf]
    board = "index/boards/SNDX" if ticker == "IMOEX" else "shares/boards/TQBR"
    since = (_utcnow() + MSK - timedelta(days=days)).strftime("%Y-%m-%d")
    raw, start = [], 0
    while True:
        chunk = _iss(
            f"engines/stock/markets/{board}/securities/{ticker}/candles.json",
            interval=interval, start=start,
            **{"from": since, "iss.only": "candles", "candles.columns": "begin,open,high,low,close"},
        )["candles"]["data"]
        raw.extend(chunk)
        if len(chunk) < 500:
            break
        start += len(chunk)
    return [
        {"time": _chart_time(datetime.fromisoformat(b)), "open": o, "high": h, "low": lo, "close": c}
        for b, o, h, lo, c in raw
    ]


def candles(ticker: str, tf: str) -> Dict[str, Any]:
    if ticker not in CHART_TICKERS or tf not in TIMEFRAMES:
        raise ValueError("неизвестный тикер или таймфрейм")
    bars = cache.get(("candles", ticker, tf), 30 if tf != "1d" else 300, lambda: _fetch_candles(ticker, tf))
    times = [b["time"] for b in bars]

    def snap(utc: Optional[datetime]) -> Optional[int]:
        """Время сделки → время свечи, в которую она попала."""
        if utc is None or not times:
            return None
        i = bisect.bisect_right(times, _chart_time(utc + MSK)) - 1
        return times[i] if i >= 0 else None

    markers, lines = [], []
    if ticker != "IMOEX":
        with closing(_db()) as c:
            trades = [dict(r) for r in c.execute("SELECT * FROM trades WHERE ticker = ? ORDER BY opened_at", (ticker,))]
        for t in trades:
            entry_time = snap(_parse_utc(t["opened_at"]))
            if entry_time is not None:
                markers.append({"time": entry_time, "kind": "entry", "side": t["side"]})
            if t["status"] == "CLOSED":
                exit_time = snap(_parse_utc(t["closed_at"]))
                if exit_time is not None:
                    markers.append({"time": exit_time, "kind": "exit", "side": t["side"],
                                    "reason": t["close_reason"], "pnl_pct": t["pnl_pct"]})
            else:
                lines = [
                    {"kind": "entry", "price": t["entry_price"]},
                    {"kind": "sl", "price": t["stop_loss"]},
                    {"kind": "tp", "price": t["take_profit"]},
                ]
    markers.sort(key=lambda m: m["time"])
    return {"ticker": ticker, "tf": tf, "bars": bars, "markers": markers, "lines": lines}


def equity_curve(max_points: int = 1500) -> Dict[str, Any]:
    points = _equity_history()
    step = max(1, len(points) // max_points)
    sampled = points[::step]
    if points and sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    series = []
    for p in sampled:
        utc = _parse_utc(p.get("t"))
        if utc is not None:
            series.append({"time": _chart_time(utc + MSK), "value": p["equity"]})
    with closing(_db()) as c:
        initial = c.execute("SELECT initial_capital FROM portfolio WHERE id = 1").fetchone()[0]
    return {"initial": initial, "points": series}


def equity_sampler() -> None:
    """Раз в минуту записывает капитал — из этих точек строится кривая."""
    while True:
        try:
            s = build_state()
            point = {"t": _utcnow().isoformat(timespec="seconds"), "equity": round(s["portfolio"]["equity"], 2)}
            with EQUITY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(point) + "\n")
        except Exception:
            log.warning("Не удалось записать точку капитала", exc_info=True)
        time.sleep(EQUITY_SAMPLE_SEC)


# ── HTTP ────────────────────────────────────────────────────────────────────
def _int(q: Dict[str, List[str]], name: str, default: int, cap: int) -> int:
    try:
        return max(1, min(cap, int(q.get(name, [default])[0])))
    except ValueError:
        return default


ROUTES: Dict[str, Callable[[Dict[str, List[str]]], Any]] = {
    "/api/ping": lambda q: {"ok": True},
    "/api/state": lambda q: build_state(),
    "/api/quotes": lambda q: quotes(),
    "/api/trades": lambda q: closed_trades(_int(q, "limit", 50, 500)),
    "/api/decisions": lambda q: decisions(_int(q, "limit", 40, 500), q.get("hold", ["0"])[0] == "1"),
    "/api/news": lambda q: news(_int(q, "limit", 40, 200)),
    "/api/candles": lambda q: candles(q.get("ticker", ["SBER"])[0].upper(), q.get("tf", ["1h"])[0]),
    "/api/equity": lambda q: equity_curve(),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        route = ROUTES.get(url.path)
        if route is None:
            self._send(404, b'{"error": "not found"}', "application/json")
            return
        try:
            body = json.dumps(route(parse_qs(url.query)), ensure_ascii=False, default=str)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json")
        except Exception as exc:
            log.exception("Ошибка %s", url.path)
            self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # запросы каждые несколько секунд — в журнал не пишем


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    threading.Thread(target=equity_sampler, name="equity-sampler", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("Дашборд: http://%s:%d", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
