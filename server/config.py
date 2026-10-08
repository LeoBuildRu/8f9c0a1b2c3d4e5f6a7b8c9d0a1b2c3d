"""Конфигурация сервера. Всё, что зависит от окружения, — через env-переменные.

На VPS значения задаются в /etc/leoagent.env (загружается systemd unit-ом).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, sep: str = ",") -> List[str]:
    raw = _env(name, "")
    return [item.strip() for item in raw.split(sep) if item.strip()]


# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN: str = _env("LEOAGENT_TG_TOKEN", "")
# Отдельный прокси для api.telegram.org. На российских VPS он чаще всего
# заблокирован — без прокси python-telegram-bot падает с ConnectTimeout.
# Форматы: "http://host:port", "http://user:pass@host:port",
#          "socks5://host:port", "socks5://user:pass@host:port".
TELEGRAM_PROXY: Optional[str] = _env("LEOAGENT_TG_PROXY")
# Список chat_id, которым разрешено получать данные. Если пусто — после первого
# /start от пользователя его id запоминается в БД и считается доверенным.
ADMIN_CHAT_IDS: List[int] = []
for raw in _env_list("LEOAGENT_TG_ADMINS"):
    try:
        ADMIN_CHAT_IDS.append(int(raw))
    except ValueError:
        pass

# ── Капитал и стратегия ─────────────────────────────────────────────────────
CAPITAL_LIMIT: float = _env_float("LEOAGENT_CAPITAL", 1_000_000.0)
# "ml" — входы по прогнозу LightGBM (модель в data/models/, см. research/train_ml.py);
# "indicators" — прежний скоринг RSI/MACD/средних. Без модели агент сам вернётся к индикаторам.
STRATEGY: str = (_env("LEOAGENT_STRATEGY", "ml") or "ml").lower()
# Свободные деньги считаются лежащими в фонде денежного рынка LQDT
CASH_YIELD: bool = (_env("LEOAGENT_CASH_YIELD", "true") or "true").lower() in ("1", "true", "yes")

# ── Пути ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path(_env("LEOAGENT_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(_env("LEOAGENT_DB", str(DATA_DIR / "agent_state.db")))
NEWS_FILE = Path(_env("LEOAGENT_NEWS_FILE", str(DATA_DIR / "cbr_news.json")))
PRICE_CACHE_FILE = Path(_env("LEOAGENT_PRICE_CACHE", str(DATA_DIR / "price_cache.json")))

# ── Прокси / User-Agent ─────────────────────────────────────────────────────
# Формат строки прокси: "http://user:pass@host:port" или "http://host:port"
# Список можно задать переменной LEOAGENT_PROXIES (через запятую)
# или положить файлом proxies.txt (по одной строке) в DATA_DIR.
PROXIES: List[str] = _env_list("LEOAGENT_PROXIES")
PROXY_FILE = DATA_DIR / "proxies.txt"
if not PROXIES and PROXY_FILE.exists():
    PROXIES = [
        line.strip()
        for line in PROXY_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

# Если задать "true" — прокси обязательны, без них агент не делает HTTP.
PROXY_REQUIRED: bool = _env("LEOAGENT_PROXY_REQUIRED", "false").lower() in ("1", "true", "yes")

# Список User-Agent. Если пусто — используется fake_useragent (если доступен).
USER_AGENTS: List[str] = _env_list("LEOAGENT_USER_AGENTS", sep="|")

# ── Интервалы (секунды) ──────────────────────────────────────────────────────
PRICE_REFRESH_MIN_SEC = _env_int("LEOAGENT_PRICE_MIN_SEC", 5 * 60)   # 5 мин
PRICE_REFRESH_MAX_SEC = _env_int("LEOAGENT_PRICE_MAX_SEC", 15 * 60)  # 15 мин
NEWS_REFRESH_MIN_SEC = _env_int("LEOAGENT_NEWS_MIN_SEC", 10 * 60)    # 10 мин
NEWS_REFRESH_MAX_SEC = _env_int("LEOAGENT_NEWS_MAX_SEC", 30 * 60)    # 30 мин
# Сделки до суток со стопами ~1%: проверять SL/TP реже раза в 10 минут нельзя
TICK_MIN_SEC = _env_int("LEOAGENT_TICK_MIN_SEC", 5 * 60)             # 5 мин
TICK_MAX_SEC = _env_int("LEOAGENT_TICK_MAX_SEC", 10 * 60)            # 10 мин

# ── Telegram-канал с новостями (можно подменить) ────────────────────────────
NEWS_CHANNEL = _env("LEOAGENT_NEWS_CHANNEL", "cbrstocks")

# ── Лог-уровень ──────────────────────────────────────────────────────────────
LOG_LEVEL = _env("LEOAGENT_LOG_LEVEL", "INFO").upper()


def validate() -> List[str]:
    """Возвращает список проблем конфигурации. Пустой список = всё ок."""
    issues: List[str] = []
    if not TELEGRAM_TOKEN:
        issues.append("LEOAGENT_TG_TOKEN не задан")
    if PROXY_REQUIRED and not PROXIES:
        issues.append("PROXY_REQUIRED=true, но прокси не заданы")
    if PRICE_REFRESH_MIN_SEC > PRICE_REFRESH_MAX_SEC:
        issues.append("PRICE_REFRESH_MIN_SEC > PRICE_REFRESH_MAX_SEC")
    if NEWS_REFRESH_MIN_SEC > NEWS_REFRESH_MAX_SEC:
        issues.append("NEWS_REFRESH_MIN_SEC > NEWS_REFRESH_MAX_SEC")
    if TICK_MIN_SEC > TICK_MAX_SEC:
        issues.append("TICK_MIN_SEC > TICK_MAX_SEC")
    return issues
