"""Серверная точка входа LeoAgent.

Запуск:
    python -m server.main

Обычно поднимается через systemd. См. server/deploy/leoagent.service.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Dict, List

from app.agent.news_analyzer import NewsAnalyzer
from app.agent.trader_agent import PRIORITY_TICKERS, TraderAgent
from app.config import STOCKS
from server import config
from server.http_client import HttpClient
from server.news_fetcher import TelegramNewsFetcher
from server.price_fetcher import PriceFetcher
from server.proxy_pool import ProxyPool
from server.scheduler import RandomIntervalJob
from server.telegram_bot import TelegramBot

log = logging.getLogger("leoagent")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    # httpx на INFO пишет полный URL каждого запроса, а в URL Telegram Bot API
    # зашит токен бота — он попадал бы в journalctl.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _candidate_tickers() -> List[str]:
    """25 ликвидных бумаг агента — на них стратегия проверена в backtest.py.

    Если хочется временно изменить список — задайте `LEOAGENT_TICKERS`
    через запятую (например, для отладки на 5 бумагах).
    """
    custom = config._env_list("LEOAGENT_TICKERS")
    if custom:
        return [t for t in custom if t in STOCKS]
    return [t for t in PRIORITY_TICKERS if t in STOCKS]


async def _serve() -> None:
    _setup_logging()

    issues = config.validate()
    if issues:
        for i in issues:
            log.error("Конфиг: %s", i)
        sys.exit(2)

    log.info("Запуск LeoAgent. Капитал: %.0f ₽", config.CAPITAL_LIMIT)
    log.info("DB: %s", config.DB_PATH)
    log.info("Прокси: %d шт.", len(config.PROXIES))

    proxy_pool = ProxyPool(config.PROXIES, user_agents=config.USER_AGENTS)
    http = HttpClient(proxy_pool, proxy_required=config.PROXY_REQUIRED)

    # Цены, новости
    price_fetcher = PriceFetcher(http, config.PRICE_CACHE_FILE)
    news_fetcher = TelegramNewsFetcher(
        http,
        config.NEWS_CHANNEL,
        config.NEWS_FILE,
        proxy_url=config.TELEGRAM_PROXY,  # тот же SOCKS5, что у бота
    )
    news_analyzer = NewsAnalyzer(path=str(config.NEWS_FILE))

    # ML-прогноз выключен: MarketAnalyzer отдаёт в PricePredictor свечи вместе
    # со своими индикаторами, из-за SMA50 модели не хватает строк для оценки
    # точности, и агент прогноз игнорирует. Пока это не исправлено и польза ML
    # не проверена в backtest.py, обучение моделей на каждом тике — пустая
    # трата CPU маленького VPS.
    agent = TraderAgent(
        stocks_map=STOCKS,
        predictor=None,
        capital_limit=config.CAPITAL_LIMIT,
        db_path=str(config.DB_PATH),
        news_analyzer=news_analyzer,
    )

    bot = TelegramBot(
        token=config.TELEGRAM_TOKEN,
        agent=agent,
        get_prices=lambda: price_fetcher.prices,
        proxy_url=config.TELEGRAM_PROXY,
    )

    tickers = _candidate_tickers()

    # ── Джобы ────────────────────────────────────────────────────────────────
    async def refresh_prices_job():
        await asyncio.to_thread(price_fetcher.refresh, tickers)

    async def refresh_news_job():
        await asyncio.to_thread(news_fetcher.fetch)

    async def tick_job():
        report = await asyncio.to_thread(agent.tick, tickers)
        if report.error:
            log.error("Тик упал: %s", report.error)
        else:
            log.info(report.summary())

    jobs = [
        RandomIntervalJob(
            "prices", refresh_prices_job,
            config.PRICE_REFRESH_MIN_SEC, config.PRICE_REFRESH_MAX_SEC,
        ),
        RandomIntervalJob(
            "news", refresh_news_job,
            config.NEWS_REFRESH_MIN_SEC, config.NEWS_REFRESH_MAX_SEC,
        ),
        RandomIntervalJob(
            "tick", tick_job,
            config.TICK_MIN_SEC, config.TICK_MAX_SEC,
        ),
    ]

    # ── Запуск ───────────────────────────────────────────────────────────────
    await bot.start()
    for job in jobs:
        job.start()
    log.info("Все воркеры запущены")

    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Получен сигнал, останавливаемся…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _signal_handler)
        except (NotImplementedError, AttributeError):
            # Windows / некоторые окружения — пропускаем
            pass

    await stop_event.wait()

    for job in jobs:
        job.stop()
    await bot.shutdown()
    log.info("Остановлены.")


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
