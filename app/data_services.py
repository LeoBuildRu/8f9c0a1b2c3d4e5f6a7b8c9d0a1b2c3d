"""Сервисы загрузки рыночных данных."""

from datetime import datetime
import requests
import pandas as pd

FINAM_INTRADAY_INTERVALS = {1, 2, 3, 4, 5, 6, 7, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720}


def parse_finam_csv(response_text, interval):
    """Parse FINAM CSV response into a dataframe."""
    lines = response_text.strip().splitlines()
    if len(lines) <= 1:
        return pd.DataFrame()

    data = []
    for line in lines[1:]:
        if not line.strip():
            continue

        parts = line.split(',')
        if len(parts) < 8:
            continue

        try:
            candle_time = datetime.strptime(parts[2], '%Y%m%d')
            if interval in FINAM_INTRADAY_INTERVALS and len(parts) > 3:
                time_str = parts[3].strip()
                if len(time_str) == 6 and time_str.isdigit():
                    candle_time = datetime.strptime(parts[2] + time_str, '%Y%m%d%H%M%S')

            data.append({
                'DATE': candle_time,
                'OPEN': float(parts[4]),
                'HIGH': float(parts[5]),
                'LOW': float(parts[6]),
                'CLOSE': float(parts[7]),
                'VOL': float(parts[8]) if len(parts) > 8 else 0
            })
        except (ValueError, IndexError):
            continue

    return pd.DataFrame(data)


def get_moex_interval(interval):
    """Map app interval code to MOEX ISS interval code and optional resample rule."""
    higher_tf_map = {
        8: (24, None),   # day
        9: (7, None),    # week
        10: (31, None),  # month
        11: (4, None),   # quarter
    }
    if interval in higher_tf_map:
        return higher_tf_map[interval]

    if interval <= 1:
        return 1, None

    if interval < 60:
        return 1, f"{interval}min"

    if interval == 60:
        return 60, None

    if interval % 60 == 0:
        return 60, f"{interval // 60}h"

    return 10, None


def fetch_moex_data(stock_code, start_date, end_date, interval, timeout=15, http_get=None, board=None):
    """Fetch candles from MOEX ISS.

    http_get — опциональный callable вида ``http_get(url, params=..., timeout=...) -> Response``.
    Используется на сервере для ротации прокси. По умолчанию — обычный requests.get.
    board — режим торгов, если бумага не на TQBR (например, TQTF для фондов вроде LQDT).
    """
    if stock_code == "IMOEX":
        engine, market, board = "stock", "index", "SNDX"
    else:
        engine, market, board = "stock", "shares", board or "TQBR"

    moex_interval, resample_rule = get_moex_interval(interval)
    url = f"https://iss.moex.com/iss/engines/{engine}/markets/{market}/boards/{board}/securities/{stock_code}/candles.json"

    _get = http_get or (lambda u, params=None, timeout=timeout: requests.get(u, params=params, timeout=timeout))

    all_rows = []
    columns = None
    start = 0
    while True:
        params = {
            "from": start_date.strftime("%Y-%m-%d"),
            "till": end_date.strftime("%Y-%m-%d"),
            "interval": moex_interval,
            "start": start,
            "iss.meta": "off",
            "iss.only": "candles",
        }

        response = _get(url, params=params, timeout=timeout)
        if response is None:
            break
        response.raise_for_status()
        payload = response.json().get("candles", {})
        chunk = payload.get("data", [])
        columns = payload.get("columns", columns)

        if not chunk:
            break

        all_rows.extend(chunk)
        if len(chunk) < 500:
            break
        start += 500

    if not all_rows or not columns:
        return pd.DataFrame()

    frame = pd.DataFrame(all_rows, columns=columns)
    if frame.empty:
        return frame

    frame["DATE"] = pd.to_datetime(frame["begin"], errors="coerce")
    frame = frame.rename(columns={
        "open": "OPEN",
        "high": "HIGH",
        "low": "LOW",
        "close": "CLOSE",
        "volume": "VOL",
    })
    frame = frame[["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOL"]].dropna(subset=["DATE"])
    frame = frame.sort_values("DATE").reset_index(drop=True)

    if resample_rule and len(frame) > 1:
        frame = (frame
            .set_index("DATE")
            .resample(resample_rule)
            .agg({
                "OPEN": "first",
                "HIGH": "max",
                "LOW": "min",
                "CLOSE": "last",
                "VOL": "sum",
            })
            .dropna(subset=["OPEN", "HIGH", "LOW", "CLOSE"])
            .reset_index()
        )

    return frame


def fetch_stock_data_with_fallback(stock_code, stock_id, start_date, end_date, interval, timeout=15, http_get=None):
    """Try MOEX ISS first, then fallback to FINAM.

    MOEX ищет бумагу по тикеру, а FINAM — по числовому ID из STOCKS, где есть
    ошибки (у MGNT и GMKN один ID). К тому же export.finam.ru сейчас отвечает
    HTTP 400 на любой запрос, так что FINAM остаётся только запасным вариантом.

    http_get — опциональный callable для прокси-ротации. См. fetch_moex_data.
    """
    try:
        frame = fetch_moex_data(stock_code, start_date, end_date, interval, timeout=timeout, http_get=http_get)
        if not frame.empty:
            return frame
    except requests.RequestException:
        pass

    finam_url = 'https://export.finam.ru/export9.out'
    finam_params = {
        'market': 1,
        'em': stock_id,
        'code': stock_code,
        'apply': 0,
        'df': start_date.day,
        'mf': start_date.month - 1,
        'yf': start_date.year,
        'dt': end_date.day,
        'mt': end_date.month - 1,
        'yt': end_date.year,
        'p': interval,
        'f': f'{stock_code}_export',
        'e': '.csv',
        'cn': stock_code,
        'dtf': 1,
        'tmf': 1,
        'MSOR': 1,
        'mstime': 'on',
        'mstimever': 1,
        'sep': 1,
        'sep2': 1,
        'datf': 1,
        'at': 1
    }

    _get = http_get or (lambda u, params=None, timeout=timeout: requests.get(u, params=params, timeout=timeout))

    try:
        response = _get(finam_url, params=finam_params, timeout=timeout)
        if response is not None:
            response.raise_for_status()
            frame = parse_finam_csv(response.text, interval)
            if not frame.empty:
                return frame
    except requests.RequestException:
        pass

    return pd.DataFrame()
