"""Анализ новостей.

Берёт cbr_news.json (его уже заполняет существующий NewsParser) и оценивает:
- общий sentiment по последним N новостям
- per-ticker sentiment по упоминаниям
- наличие политических/санкционных триггеров

Лёгкий лексический анализ — без зависимости от внешних NLP-моделей.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import STOCKS

NEWS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "cbr_news.json")


POSITIVE_TERMS = {
    "рост", "выросли", "выручка", "прибыль", "рекорд", "дивиденд", "buyback", "выкуп",
    "позитив", "поддержка", "восстановление", "рост спроса", "снижение ставки", "smb",
    "рост экспорта", "контракт", "сделка", "слияние", "ipo", "размещени",
}

NEGATIVE_TERMS = {
    "санкци", "падени", "обвал", "штраф", "убытк", "снижение прибыл",
    "понижение", "downgrade", "снижение рейтинг", "блокиров", "арест", "запрет",
    "повышение ставки", "инфляция растёт", "дефолт", "приостанов", "delisting",
    "делистинг", "обыск", "уголовн", "налог увеличен",
}

# «США» убраны: в биржевом канале это почти всегда рыночные новости
# («индексы США», «ставка ФРС»), а не политический риск.
POLITICAL_TERMS = {
    "санкци", "ес ", "евросоюз", "us treasury", "sdn", "ofac",
    "украин", "минфин сша", "новые ограничения", "указ президент",
    "посольств", "g7", "g-7", "блокирующ",
}

# Псевдонимы для основных эмитентов. Сюда добавляются только тикеры с активным
# новостным потоком — для остальных агент решает по чисто техническим сигналам.
# Псевдоним ищется с начала слова (см. _term_regex), пробел в конце — целое слово.
TICKER_ALIASES: Dict[str, List[str]] = {
    # Нефть и газ
    "GAZP": ["газпром", "gazprom", "gazp"],
    "LKOH": ["лукойл", "lukoil", "lkoh"],
    "ROSN": ["роснефть", "rosneft", "rosn"],
    "NVTK": ["новатэк", "novatek"],
    "TATN": ["татнефть", "tatneft"],
    "SNGS": ["сургутнефтегаз", "surgut"],
    "BANE": ["башнефть", "bashneft"],
    "TRNFP": ["транснефть", "transneft"],
    # Банки и финансы
    "SBER": ["сбер", "сбербанк", "sber"],
    "VTBR": ["втб", "vtb"],
    "T": ["тинькоф", "tinkoff", "т-банк", "т-технолог"],
    # «Мосбиржа» в канале — почти всегда площадка или индекс, а не эмитент.
    "MOEX": ["moex ", "акции мосбирж", "акций мосбирж"],
    "BSPB": ["банк санкт-петербург", "bspb"],
    "CBOM": ["мкб ", "московский кредитный"],
    "SVCB": ["совкомбанк", "svcb"],
    "RENI": ["ренессанс страх", "renaissance"],
    "AFKS": ["афк систем", "афк «систем", "афк \"систем"],
    # Металлы и горнодобыча
    "GMKN": ["норникель", "норильск", "nornickel", "gmkn"],
    "PLZL": ["полюс", "polyus"],
    "CHMF": ["северсталь", "severstal"],
    "NLMK": ["нлмк", "nlmk"],
    "MAGN": ["ммк", "magnitogorsk"],
    "RUAL": ["русал", "rusal"],
    "ALRS": ["алроса", "alrosa"],
    "MTLR": ["мечел", "mechel"],
    "RASP": ["распадск", "raspadskaya"],
    "SELG": ["селигдар", "seligdar"],
    "UGLD": ["ugld", "южуралзолот", "югк"],
    # Химия
    "PHOR": ["фосагро", "phosagro"],
    "AKRN": ["акрон", "akron"],
    "KZOS": ["куйбышевазот", "kzos"],
    # Электроэнергетика
    "IRAO": ["интер рао", "интеррао", "inter rao"],
    "HYDR": ["русгидро", "rushydro"],
    "FEES": ["фск", "fsk", "россети"],
    "UPRO": ["юнипро", "unipro"],
    # Розница
    "MGNT": ["магнит", "magnit"],
    "X5": ["x5", "икс 5", "пятёрочк", "пятерочк", "перекрёсток", "перекресток"],
    "FIXR": ["fix price", "фикс прайс"],
    "OZON": ["озон", "ozon"],
    "MVID": ["м.видео", "мвидео", "m.video"],
    "BELU": ["белуга", "beluga", "новабев"],
    # IT, медиа, связь
    "YDEX": ["яндекс", "yandex", "ydex"],
    "MTSS": ["мтс ", "mts"],
    "RTKM": ["ростелеком", "rostelecom"],
    "VKCO": ["вк ", "vk"],
    "POSI": ["positive tech", "позитив текнолоджиз", "позитив технолоджиз"],
    "ASTR": ["астра", "astra"],
    "SOFL": ["софтлайн", "softline"],
    "DELI": ["делимобиль", "delimobil"],
    "WUSH": ["whoosh", "вуш"],
    "HEAD": ["headhunter", "хэдхантер", "хедхантер"],
    "MDMG": ["мать и дитя", "mdmg"],
    # Транспорт
    "AFLT": ["аэрофлот", "aeroflot"],
    "FLOT": ["совкомфлот", "sovcomflot"],
    "NMTP": ["нмтп", "новороссийский морской"],
    "FESH": ["fesco", "феско", "дальневосточное"],
    "KMAZ": ["камаз", "kamaz"],
    # Стройка
    "LSRG": ["лср ", "lsr group"],
    "PIKK": ["пик ", "pik group"],
    "ETLN": ["эталон", "etalon"],
    "SMLT": ["самолёт", "самолет", "samolet"],
    # Прочее
    "RAGR": ["русагро", "rusagro"],
    "SGZH": ["сегежа", "segezha"],
}


# Канал сам размечает посты хэштегами: #SBER — компания, #Нефтянка — отрасль,
# #Макро/#Политика — весь рынок. Хэштег надёжнее поиска названий в тексте.
SECTOR_TAGS: Dict[str, List[str]] = {
    "нефтянка": ["LKOH", "ROSN", "TATN", "TATNP", "SNGS", "SNGSP", "BANE", "BANEP"],
    "нефть": ["LKOH", "ROSN", "TATN", "TATNP", "SNGS", "SNGSP", "BANE", "BANEP"],
    "газ": ["GAZP", "NVTK"],
    "банки": ["SBER", "SBERP", "VTBR", "T", "BSPB", "CBOM", "SVCB"],
    "металлурги": ["CHMF", "NLMK", "MAGN", "GMKN", "RUAL"],
    "металлы": ["CHMF", "NLMK", "MAGN", "GMKN", "RUAL"],
    "золото": ["PLZL", "UGLD", "SELG"],
    "уголь": ["MTLR", "RASP"],
    "алмазы": ["ALRS"],
    "удобрения": ["PHOR", "AKRN"],
    "ритейл": ["MGNT", "X5", "FIXR"],
    "маркетплейсы": ["OZON"],
    "телеком": ["MTSS", "RTKM"],
    "энергетика": ["IRAO", "HYDR", "FEES", "UPRO"],
    "электроэнергетика": ["IRAO", "HYDR", "FEES", "UPRO"],
    "авиа": ["AFLT"],
    "авиация": ["AFLT"],
    "недвижимость": ["SMLT", "PIKK", "ETLN", "LSRG"],
    "девелоперы": ["SMLT", "PIKK", "ETLN", "LSRG"],
}
MARKET_TAGS = {"макро", "цбрф", "политика", "санкции", "геополитика", "imoex", "прогноз", "бонды", "статистика"}
POLITICAL_TAGS = {"политика", "санкции", "геополитика"}
_HASHTAG_RX = re.compile(r"#([A-Za-zА-Яа-яЁё0-9_]+)")
_TICKER_WORD_RX = re.compile(r"(?<![\w#$])([A-Z]{3,5})(?!\w)")  # «Данные CBOM за сегодня»


def hashtags(text: str) -> set:
    return {t.lower() for t in _HASHTAG_RX.findall(text)}


def _term_regex(term: str) -> "re.Pattern[str]":
    """Термин ищется с начала слова: «газпром» находит «Газпрома», но «рост»
    больше не находит «простой», а «moex» — «IMOEX». Пробел в конце термина
    означает «только целое слово»: «ес » не совпадёт с «интерес »."""
    tail = r"(?!\w)" if term.endswith(" ") else ""
    return re.compile(r"(?<!\w)" + re.escape(term.strip()) + tail)


_POSITIVE_RX = [_term_regex(t) for t in POSITIVE_TERMS]
_NEGATIVE_RX = [_term_regex(t) for t in NEGATIVE_TERMS]
_POLITICAL_RX = [_term_regex(t) for t in POLITICAL_TERMS]
_ALIAS_RX = {
    ticker: [_term_regex(a) for a in aliases]
    for ticker, aliases in TICKER_ALIASES.items()
}


@dataclass
class NewsAssessment:
    total: int
    window_hours: float
    overall_sentiment: float          # [-1, 1]
    political_risk: float             # [0, 1]
    per_ticker: Dict[str, float] = field(default_factory=dict)
    triggers: List[str] = field(default_factory=list)  # короткие заголовки-триггеры

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "window_hours": self.window_hours,
            "overall_sentiment": self.overall_sentiment,
            "political_risk": self.political_risk,
            "per_ticker": self.per_ticker,
            "triggers": self.triggers,
        }


class NewsAnalyzer:
    def __init__(self, path: str = NEWS_FILE, window_hours: float = 72.0):
        self.path = os.path.abspath(path)
        self.window_hours = window_hours

    # ── Загрузка ──────────────────────────────────────────────────────────────
    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _recent(self) -> List[Dict[str, Any]]:
        all_news = self._load()
        cutoff = datetime.now() - timedelta(hours=self.window_hours)
        out: List[Dict[str, Any]] = []
        for item in all_news:
            ts = self._parse_dt(item.get("date") or item.get("parsed_at"))
            if ts is None or ts >= cutoff:
                out.append(item)
        if not out:
            # fallback: берём 50 последних, если в окно ничего не попало
            out = sorted(all_news, key=lambda x: x.get("date", ""), reverse=True)[:50]
        return out

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(value)[:19], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    # ── Скоринг текста ────────────────────────────────────────────────────────
    @staticmethod
    def _score_text(text: str) -> tuple[float, bool]:
        text_low = text.lower()
        pos = sum(1 for rx in _POSITIVE_RX if rx.search(text_low))
        neg = sum(1 for rx in _NEGATIVE_RX if rx.search(text_low))
        political = bool(hashtags(text) & POLITICAL_TAGS) or any(rx.search(text_low) for rx in _POLITICAL_RX)
        if pos == 0 and neg == 0:
            return 0.0, political
        score = (pos - neg) / (pos + neg)
        return float(score), political

    @staticmethod
    def _alias_mentions(text: str) -> List[str]:
        """Компании, найденные по названиям в тексте (запасной способ без хэштегов)."""
        low = text.lower()
        return [
            ticker for ticker, patterns in _ALIAS_RX.items()
            if any(rx.search(low) for rx in patterns)
        ]

    @staticmethod
    def targets(text: str) -> List[Tuple[str, str]]:
        """К чему относится пост: [(тикер, "company" | "sector")] или [("IMOEX", "market")].

        Сначала хэштеги тикеров, если их нет — названия компаний в тексте; затем
        отраслевые хэштеги; новость без компаний и отраслей с макро- или
        политическим хэштегом относится ко всему рынку."""
        tags = hashtags(text)
        found = [(t.upper(), "company") for t in sorted(tags) if t.upper() in STOCKS and t.upper() != "IMOEX"]
        if not found:
            words = dict.fromkeys(w for w in _TICKER_WORD_RX.findall(text) if w in STOCKS and w != "IMOEX")
            found = [(t, "company") for t in words]
        if not found:
            found = [(t, "company") for t in NewsAnalyzer._alias_mentions(text)]
        companies = {t for t, _ in found}
        for tag in sorted(tags):
            found += [(t, "sector") for t in SECTOR_TAGS.get(tag, []) if t not in companies]
        if not found and tags & MARKET_TAGS:
            found = [("IMOEX", "market")]
        return list(dict.fromkeys(found))

    @staticmethod
    def _mentions(text: str) -> List[str]:
        """Бумаги, к которым относится пост: компании и их отрасли."""
        return [t for t, scope in NewsAnalyzer.targets(text) if scope != "market"]

    # ── Главный вход ──────────────────────────────────────────────────────────
    def assess(self, tickers: Optional[Iterable[str]] = None) -> NewsAssessment:
        items = self._recent()
        if not items:
            return NewsAssessment(0, self.window_hours, 0.0, 0.0)

        wanted = set(tickers) if tickers else None
        scores: List[float] = []
        political_hits = 0
        per_ticker: Dict[str, List[float]] = {}
        triggers: List[str] = []

        for item in items:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            sc, political = self._score_text(text)
            scores.append(sc)
            if political:
                political_hits += 1
                short = re.sub(r"\s+", " ", text)[:140]
                if len(triggers) < 5:
                    triggers.append(short)

            for ticker in self._mentions(text):
                if wanted is not None and ticker not in wanted:
                    continue
                per_ticker.setdefault(ticker, []).append(sc)

        overall = sum(scores) / len(scores) if scores else 0.0
        political_risk = political_hits / len(items)
        agg_per_ticker = {t: sum(v) / len(v) for t, v in per_ticker.items() if v}

        return NewsAssessment(
            total=len(items),
            window_hours=self.window_hours,
            overall_sentiment=round(overall, 3),
            political_risk=round(political_risk, 3),
            per_ticker={k: round(v, 3) for k, v in agg_per_ticker.items()},
            triggers=triggers,
        )
