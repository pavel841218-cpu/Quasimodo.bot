import os
import datetime
import logging
import asyncio
import aiohttp
import numpy as np
from aiohttp import web

# =============================================================
# НАСТРОЙКИ
# =============================================================

BINGX_BASE_URL = "https://open-api.bingx.com"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# -------------------------------------------------------------
# ОБЩИЕ ФИЛЬТРЫ (Market Cap НЕ используется, только 24h Volume)
# -------------------------------------------------------------

MIN_24H_VOLUME_USDT = 300_000

EXCLUDED_SYMBOLS = {
    "USDC",
    "FDUSD",
}

MAX_CONCURRENT = 10

# -------------------------------------------------------------
# ПОЛКА / НАКОПЛЕНИЕ
# -------------------------------------------------------------

BASE_MIN_HOURS = 4
BASE_MAX_HOURS = 12

# Максимальная ширина основной полки
BASE_MAX_WIDTH_PCT = 4.5

# Расширенный поиск полки
EXTENDED_BASE_MAX_HOURS = 48
EXTENDED_BASE_MAX_WIDTH_PCT = 6.0

# -------------------------------------------------------------
# 🐭 ТИХИЙ СИГНАЛ
# -------------------------------------------------------------

QUIET_MAX_RANGE_PCT = 2.2
QUIET_MAX_BODY_PCT = 1.2

# Денежный поток должен быть примерно стабильным: 0.5x - 1.5x от среднего
QUIET_MONEY_MIN_MULT = 0.5
QUIET_MONEY_MAX_MULT = 1.5

QUIET_MIN_POSITION_PCT = 50

QUIET_MAX_CURRENT_RANGE_PCT = 1.8
QUIET_MAX_CURRENT_BODY_PCT = 1.0

QUIET_MIN_ACTUAL_RVOL = 0.8

# -------------------------------------------------------------
# ⚡ СРЕДНИЙ СИГНАЛ — ПРОБОЙ
# -------------------------------------------------------------

BREAKOUT_MIN_CHANGE_PCT = 1.5
BREAKOUT_MIN_ACTUAL_RVOL = 1.4
BREAKOUT_MIN_ABOVE_SHELF_PCT = 0.05

# -------------------------------------------------------------
# 🔥 АГРЕССИВНЫЙ СИГНАЛ
# -------------------------------------------------------------

AGGRESSIVE_MIN_ACTUAL_RVOL = 2.2
AGGRESSIVE_MIN_CHANGE_PCT = 0.8

AGGRESSIVE_WINDOW_START = 49
AGGRESSIVE_WINDOW_END = 56
AGGRESSIVE_WINDOW_RVOL = 1.8

AGGRESSIVE_EXTREME_RVOL = 3.5
AGGRESSIVE_EXTREME_CHANGE_PCT = 0.5

# -------------------------------------------------------------
# EMA 20 / EMA 40 НАСТРОЙКИ
# -------------------------------------------------------------

EMA_FAST = 20
EMA_SLOW = 40

# Максимальное расстояние от EMA до границ полки (%)
EMA_SHELF_TOLERANCE_PCT = 1.5

# -------------------------------------------------------------
# COOLDOWN
# -------------------------------------------------------------

COOLDOWN_QUIET = 1800          # 30 минут
COOLDOWN_BREAKOUT = 3600       # 60 минут
COOLDOWN_AGGRESSIVE = 900      # 15 минут

VOLUME_CACHE_SECONDS = 300
KLINE_CACHE_SECONDS = 15

# =============================================================
# LOGGING
# =============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("ConsolidationHunter")


# =============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================

def parse_kline(kline):
    return (
        float(kline[1]),  # open
        float(kline[2]),  # high
        float(kline[3]),  # low
        float(kline[4]),  # close
        float(kline[5])   # volume
    )


def calculate_ema_exact(prices, period):
    """
    Точный расчет EMA, где первое значение вычисляется как SMA.
    """
    if not prices or len(prices) < period:
        return 0.0

    # Старт с SMA за первые period элементов
    sma_init = sum(prices[:period]) / period
    alpha = 2 / (period + 1)

    ema_value = sma_init
    for price in prices[period:]:
        ema_value = (alpha * float(price)) + ((1 - alpha) * ema_value)

    return ema_value


def get_base_symbol(symbol):
    symbol_upper = symbol.upper()
    if symbol_upper.endswith("-USDT"):
        return symbol_upper[:-5]
    return symbol_upper


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


# =============================================================
# EMA И ПЕРЕСЕЧЕНИЯ В ПОЛКЕ
# =============================================================

def check_ema_in_shelf(klines_1h, shelf_high, shelf_low):
    """
    Проверяет, находятся ли EMA20 и EMA40 в полке accumulation
    и формируют ли они переплетение / бычью структуру.
    """
    if len(klines_1h) < EMA_SLOW + 5:
        return {"valid": False, "score": 0, "detail": "Мало свечей для EMA"}

    closed = klines_1h[:-1]
    closes = [parse_kline(k)[3] for k in closed]

    ema20 = calculate_ema_exact(closes, EMA_FAST)
    ema40 = calculate_ema_exact(closes, EMA_SLOW)

    if ema20 == 0.0 or ema40 == 0.0:
        return {"valid": False, "score": 0, "detail": "Ошибка расчета EMA"}

    # Проверка нахождения EMA в районе полки (с допустимым погрешностным диапазоном)
    margin = (shelf_high - shelf_low) * (EMA_SHELF_TOLERANCE_PCT / 100.0)
    effective_low = shelf_low - margin
    effective_high = shelf_high + margin

    ema20_in = effective_low <= ema20 <= effective_high
    ema40_in = effective_low <= ema40 <= effective_high

    if not (ema20_in or ema40_in):
        return {"valid": False, "score": 0, "detail": "EMA вне полки накопления"}

    # Сжатие скользящих (расстояние между EMA20 и EMA40 не более 1.2%)
    ema_diff_pct = abs(ema20 - ema40) / ema40 * 100

    score = 10
    detail = []

    if ema20_in and ema40_in:
        score += 10
        detail.append("EMA20 и EMA40 внутри полки")

    if ema_diff_pct <= 1.2:
        score += 10
        detail.append(f"Переплетение/сжатие EMA ({ema_diff_pct:.2f}%)")

    if ema20 >= ema40:
        score += 5
        detail.append("EMA20 >= EMA40 (Бычья структура)")

    return {
        "valid": True,
        "score": score,
        "detail": ", ".join(detail),
        "ema20": ema20,
        "ema40": ema40
    }


# =============================================================
# ПОИСК ПОЛКИ
# =============================================================

def find_shelf_before_breakout(klines_1h):
    if len(klines_1h) < BASE_MIN_HOURS + 1:
        return None

    closed_candles = klines_1h[:-1]
    highs, lows, money = [], [], []

    for k in closed_candles:
        o, h, l, c, v = parse_kline(k)
        if l <= 0 or c <= 0:
            continue
        highs.append(h)
        lows.append(l)
        money.append(v * c)

    if len(highs) < BASE_MIN_HOURS:
        return None

    # Поиск стандартной полки (4-12 часов)
    for hours in range(BASE_MAX_HOURS, BASE_MIN_HOURS - 1, -1):
        if len(highs) < hours:
            continue

        sub_highs = highs[-hours:]
        sub_lows = lows[-hours:]
        sub_money = money[-hours:]

        shelf_high = max(sub_highs)
        shelf_low = min(sub_lows)

        if shelf_low <= 0:
            continue

        width = ((shelf_high - shelf_low) / shelf_low) * 100
        if width > BASE_MAX_WIDTH_PCT:
            continue

        avg_money = np.mean(sub_money)
        if avg_money <= 0:
            continue

        stable_count = sum(
            1 for v in sub_money
            if (QUIET_MONEY_MIN_MULT * avg_money <= v <= QUIET_MONEY_MAX_MULT * avg_money)
        )

        if (stable_count / len(sub_money)) >= 0.65:
            return {
                "hours": hours,
                "width": round(width, 2),
                "high": shelf_high,
                "low": shelf_low,
                "avg_money": float(avg_money),
                "stable_count": stable_count,
                "candles": closed_candles[-hours:]
            }

    # Расширенная полка (до 48 часов)
    for hours in range(EXTENDED_BASE_MAX_HOURS, BASE_MAX_HOURS, -1):
        if len(highs) < hours:
            continue

        sub_highs = highs[-hours:]
        sub_lows = lows[-hours:]
        sub_money = money[-hours:]

        shelf_high = max(sub_highs)
        shelf_low = min(sub_lows)

        if shelf_low <= 0:
            continue

        width = ((shelf_high - shelf_low) / shelf_low) * 100
        if width > EXTENDED_BASE_MAX_WIDTH_PCT:
            continue

        avg_money = np.mean(sub_money)
        if avg_money <= 0:
            continue

        return {
            "hours": hours,
            "width": round(width, 2),
            "high": shelf_high,
            "low": shelf_low,
            "avg_money": float(avg_money),
            "stable_count": 0,
            "candles": closed_candles[-hours:]
        }

    return None


# =============================================================
# ТИХАЯ АККУМУЛЯЦИЯ
# =============================================================

def check_quiet_accumulation(klines_1h, shelf):
    hours = shelf["hours"]
    closed_candles = klines_1h[:-1]

    if len(closed_candles) < hours:
        return None

    shelf_candles = closed_candles[-hours:]
    ranges, bodies, money, closes = [], [], [], []

    for k in shelf_candles:
        o, h, l, c, v = parse_kline(k)
        if o <= 0 or l <= 0:
            continue
        ranges.append((h - l) / l * 100)
        bodies.append(abs(c - o) / o * 100)
        money.append(v * c)
        closes.append(c)

    if not money:
        return None

    avg_money = np.mean(money)
    if avg_money <= 0:
        return None

    stable_count = sum(
        1 for value in money
        if (QUIET_MONEY_MIN_MULT * avg_money <= value <= QUIET_MONEY_MAX_MULT * avg_money)
    )

    avg_range = np.mean(ranges)
    avg_body = np.mean(bodies)

    is_quiet = (
        avg_range <= QUIET_MAX_RANGE_PCT and
        avg_body <= QUIET_MAX_BODY_PCT and
        (stable_count / len(money)) >= 0.60
    )

    if not is_quiet:
        return None

    current_close = parse_kline(klines_1h[-1])[3]
    shelf_high, shelf_low = shelf["high"], shelf["low"]

    position = (
        50.0 if shelf_high <= shelf_low
        else ((current_close - shelf_low) / (shelf_high - shelf_low) * 100)
    )

    return {
        "stable_count": stable_count,
        "avg_range": avg_range,
        "avg_body": avg_body,
        "position": position,
        "avg_money": avg_money
    }


# =============================================================
# СТАКАН
# =============================================================

async def check_orderbook_pump(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/depth"
    params = {"symbol": symbol, "limit": 10}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("code") != 0:
                return None

            book = data.get("data", {})
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            if not bids or not asks:
                return None

            total_bid = sum(safe_float(b[0]) * safe_float(b[1]) for b in bids)
            total_ask = sum(safe_float(a[0]) * safe_float(a[1]) for a in asks)

            if total_bid <= 0 or total_ask <= 0:
                return None

            max_bid_wall = max(safe_float(b[0]) * safe_float(b[1]) for b in bids)
            wall_pct = (max_bid_wall / total_bid * 100) if total_bid > 0 else 0

            if max_bid_wall >= 40_000 and wall_pct >= 45:
                return {
                    "wall_usdt": int(max_bid_wall),
                    "wall_pct": round(wall_pct),
                    "bid_ask_ratio": round(total_bid / total_ask, 2)
                }
    except Exception:
        return None
    return None


# =============================================================
# ОЦЕНКА СИГНАЛОВ
# =============================================================

def evaluate_signals(symbol, klines_1h, volume_24h_usdt=0.0):
    base_symbol = get_base_symbol(symbol)
    if base_symbol in EXCLUDED_SYMBOLS or volume_24h_usdt < MIN_24H_VOLUME_USDT:
        return []

    if len(klines_1h) < 50:
        return []

    current = klines_1h[-1]
    open_h, high_h, low_h, close_h, vol_raw = parse_kline(current)

    if open_h <= 0 or low_h <= 0 or close_h <= 0:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed = max(1.0, now.minute + now.second / 60)

    change_pct = ((close_h - open_h) / open_h) * 100
    candle_range_pct = ((high_h - low_h) / low_h) * 100
    body_pct = (abs(close_h - open_h) / open_h) * 100

    current_money = vol_raw * close_h
    past_money = [parse_kline(k)[4] * parse_kline(k)[3] for k in klines_1h[-21:-1] if parse_kline(k)[3] > 0]

    if not past_money:
        return []

    avg_money = np.mean(past_money)
    if avg_money <= 0:
        return []

    actual_rvol = current_money / avg_money
    projected_rvol = (current_money * 60 / elapsed) / avg_money

    # Поиск полки
    shelf = find_shelf_before_breakout(klines_1h)
    if not shelf:
        return []

    shelf_high, shelf_low = shelf["high"], shelf["low"]
    if shelf_high <= shelf_low:
        return []

    position_in_shelf = ((close_h - shelf_low) / (shelf_high - shelf_low)) * 100
    above_shelf = close_h > shelf_high
    breakout_pct = ((close_h - shelf_high) / shelf_high * 100) if above_shelf else 0.0

    # Обязательная проверка взаимодействия EMA с полкой
    ema_res = check_ema_in_shelf(klines_1h, shelf_high, shelf_low)
    if not ema_res["valid"]:
        return []  # Отбрасываем, если EMA сильно оторваны от полки накопления

    signals = []

    # 1. ТИХИЙ СИГНАЛ
    quiet_data = check_quiet_accumulation(klines_1h, shelf)
    if quiet_data:
        quiet_valid = (
            position_in_shelf >= QUIET_MIN_POSITION_PCT and
            candle_range_pct <= QUIET_MAX_CURRENT_RANGE_PCT and
            body_pct <= QUIET_MAX_CURRENT_BODY_PCT and
            change_pct >= -0.1 and
            actual_rvol >= QUIET_MIN_ACTUAL_RVOL and
            not above_shelf
        )
        if quiet_valid:
            score = 75 + ema_res["score"]
            signals.append({
                "mode": "QUIET",
                "type": "QUIET_ACCUMULATION",
                "signal_name": "🐭 ТИХАЯ АККУМУЛЯЦИЯ",
                "score": min(score, 95),
                "price": close_h,
                "change_pct": round(change_pct, 2),
                "actual_rvol": round(actual_rvol, 2),
                "projected_rvol": round(projected_rvol, 1),
                "elapsed": int(elapsed),
                "shelf_hours": shelf["hours"],
                "shelf_width": shelf["width"],
                "shelf_high": shelf_high,
                "shelf_low": shelf_low,
                "position_pct": round(position_in_shelf),
                "reasons": [
                    f"Накопление: {shelf['hours']}ч / ширина {shelf['width']}%",
                    f"Позиция у верха полки: {position_in_shelf:.0f}%",
                    f"RVOL: x{actual_rvol:.2f}",
                    ema_res["detail"]
                ]
            })

    # 2. СРЕДНИЙ СИГНАЛ — ПРОБОЙ
    normal_valid = (
        above_shelf and
        breakout_pct >= BREAKOUT_MIN_ABOVE_SHELF_PCT and
        change_pct >= BREAKOUT_MIN_CHANGE_PCT and
        actual_rvol >= BREAKOUT_MIN_ACTUAL_RVOL
    )
    if normal_valid:
        score = 75 + ema_res["score"]
        signals.append({
            "mode": "NORMAL",
            "type": "BREAKOUT",
            "signal_name": "⚡ ПРОБОЙ ПОЛКИ",
            "score": min(score, 95),
            "price": close_h,
            "change_pct": round(change_pct, 2),
            "actual_rvol": round(actual_rvol, 2),
            "projected_rvol": round(projected_rvol, 1),
            "elapsed": int(elapsed),
            "shelf_hours": shelf["hours"],
            "shelf_width": shelf["width"],
            "shelf_high": shelf_high,
            "shelf_low": shelf_low,
            "breakout_pct": round(breakout_pct, 2),
            "reasons": [
                f"Пробой полки: +{change_pct:.2f}%",
                f"Выход выше сопротивления: +{breakout_pct:.2f}%",
                f"Реальный RVOL: x{actual_rvol:.2f}",
                ema_res["detail"]
            ]
        })

    # 3. АГРЕССИВНЫЙ СИГНАЛ
    in_window = AGGRESSIVE_WINDOW_START <= int(elapsed) <= AGGRESSIVE_WINDOW_END
    agg_valid = (
        (actual_rvol >= AGGRESSIVE_MIN_ACTUAL_RVOL and change_pct >= AGGRESSIVE_MIN_CHANGE_PCT) or
        (actual_rvol >= AGGRESSIVE_EXTREME_RVOL and change_pct >= AGGRESSIVE_EXTREME_CHANGE_PCT) or
        (in_window and actual_rvol >= AGGRESSIVE_WINDOW_RVOL and change_pct >= AGGRESSIVE_MIN_CHANGE_PCT)
    )
    if agg_valid:
        score = 80 + ema_res["score"]
        signals.append({
            "mode": "AGGRESSIVE",
            "type": "AGGRESSIVE_FLOOD",
            "signal_name": "🔥 АГРЕССИВНЫЙ ВХОД",
            "score": min(score, 99),
            "price": close_h,
            "change_pct": round(change_pct, 2),
            "actual_rvol": round(actual_rvol, 2),
            "projected_rvol": round(projected_rvol, 1),
            "elapsed": int(elapsed),
            "shelf_hours": shelf["hours"],
            "shelf_width": shelf["width"],
            "shelf_high": shelf_high,
            "shelf_low": shelf_low,
            "position_pct": round(position_in_shelf),
            "breakout_pct": round(breakout_pct, 2),
            "window": in_window,
            "reasons": [
                f"Всплеск объема: RVOL x{actual_rvol:.2f}",
                f"Рост H1: +{change_pct:.2f}%",
                f"Полка: {shelf['hours']}ч / {shelf['width']}%",
                ema_res["detail"]
            ]
        })

    return signals


# =============================================================
# ФОРМАТ СООБЩЕНИЯ ДЛЯ TELEGRAM
# =============================================================

def format_signal_message(symbol, signal, orderbook=None):
    clean = get_base_symbol(symbol)
    price = signal["price"]

    if price >= 1:
        price_str = f"{price:.4f}".rstrip("0").rstrip(".")
    elif price >= 0.01:
        price_str = f"{price:.6f}".rstrip("0").rstrip(".")
    else:
        price_str = f"{price:.10f}".rstrip("0").rstrip(".")

    mode_text = {"QUIET": "🐭 ТИХИЙ", "NORMAL": "⚡ СРЕДНИЙ", "AGGRESSIVE": "🔥 АГРЕССИВНЫЙ"}[signal["mode"]]

    msg = f"<b>{signal['signal_name']}</b>\n<b>Режим: {mode_text}</b>\n\n"
    msg += f"📌 Монета: <b>{clean} / USDT</b>\n"
    msg += f"💵 Цена: <code>{price_str}</code>\n"
    msg += f"📊 Изменение H1: <b>{signal['change_pct']:+.2f}%</b>\n"
    msg += f"📈 Реальный RVOL: <b>x{signal['actual_rvol']:.2f}</b>\n"
    msg += f"⏰ Минута часа: <b>{signal['elapsed']} / 60</b>\n"
    msg += f"📦 Полка: <b>{signal['shelf_hours']}ч</b> | ширина {signal['shelf_width']}%\n"
    msg += f"⭐ Score: <b>{signal['score']}/100</b>\n\n"

    msg += "<b>🔍 Анализ:</b>\n"
    for reason in signal.get("reasons", []):
        msg += f"• {reason}\n"

    if orderbook:
        msg += (
            f"\n📖 <b>Стакан:</b>\n"
            f"• Стенка: ${orderbook['wall_usdt']:,}\n"
            f"• Bid/Ask: x{orderbook['bid_ask_ratio']}\n"
        )

    return msg


# =============================================================
# TELEGRAM И API BINGX
# =============================================================

async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception:
        return False


async def fetch_24h_volume(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
    try:
        async with session.get(url, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return safe_float(data.get("data", {}).get("quoteVolume", 0))
    except Exception:
        pass
    return 0.0


async def fetch_futures_symbols(session):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return [
                        item["symbol"] for item in data.get("data", [])
                        if item.get("symbol", "").endswith("-USDT") and item.get("status") == 1
                    ]
    except Exception:
        pass
    return []


async def fetch_klines(session, symbol, interval="1h", limit=80):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines"
    try:
        async with session.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return data.get("data")
    except Exception:
        pass
    return None


# =============================================================
# ОБРАБОТКА И СКАНЕР
# =============================================================

async def process_symbol(session, symbol, semaphore, volume_cache, kline_cache, sent_signals):
    async with semaphore:
        loop_time = asyncio.get_event_loop().time()

        # Volume Cache
        cached_vol = volume_cache.get(symbol)
        if cached_vol and (loop_time - cached_vol["time"] <= VOLUME_CACHE_SECONDS):
            volume_24h = cached_vol["volume"]
        else:
            volume_24h = await fetch_24h_volume(session, symbol)
            volume_cache[symbol] = {"volume": volume_24h, "time": loop_time}

        if volume_24h < MIN_24H_VOLUME_USDT:
            return []

        # Kline Cache
        cached_kline = kline_cache.get(symbol)
        if cached_kline and (loop_time - cached_kline["time"] <= KLINE_CACHE_SECONDS):
            klines = cached_kline["data"]
        else:
            klines = await fetch_klines(session, symbol, "1h", 80)
            if klines:
                kline_cache[symbol] = {"data": klines, "time": loop_time}

        if not klines or len(klines) < 50:
            return []

        signals = evaluate_signals(symbol, klines, volume_24h)
        if not signals:
            return []

        sent = []
        for signal in signals:
            sig_type = signal["type"]
            key = (symbol, sig_type)
            cooldown = COOLDOWN_QUIET if sig_type == "QUIET_ACCUMULATION" else (COOLDOWN_BREAKOUT if sig_type == "BREAKOUT" else COOLDOWN_AGGRESSIVE)

            if loop_time - sent_signals.get(key, 0) < cooldown:
                continue

            orderbook = await check_orderbook_pump(session, symbol)
            if orderbook:
                signal["score"] = min(99, signal["score"] + 5)

            message = format_signal_message(symbol, signal, orderbook)
            if await send_telegram(session, message):
                sent_signals[key] = loop_time
                sent.append(signal)

        return sent


async def scanner_loop():
    logger.info("🚀 ConsolidationHunter v2 запущен. Фильтр EMA + Сжатие включены.")

    volume_cache, kline_cache, sent_signals = {}, {}, {}
    connector = aiohttp.TCPConnector(limit=30, limit_per_host=20, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": "ConsolidationHunter/2.0"}) as session:
        while True:
            scan_start = asyncio.get_event_loop().time()
            try:
                symbols = await fetch_futures_symbols(session)
                logger.info(f"🔍 Начат скан. Получено пар: {len(symbols)}")
                if symbols:
                    tasks = [process_symbol(session, s, semaphore, volume_cache, kline_cache, sent_signals) for s in symbols]
                    await asyncio.gather(*tasks, return_exceptions=True)

                elapsed = asyncio.get_event_loop().time() - scan_start
                logger.info(f"✅ Скан завершен за {elapsed:.2f} сек.")
                sleep_time = max(1.0, 20.0 - elapsed)
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                logger.info("Сканер остановлен.")
                break
            except Exception as e:
                logger.error(f"Ошибка в сканере: {e}")
                await asyncio.sleep(10)


# =============================================================
# WEB SERVER & MAIN
# =============================================================

async def handle_ping(request):
    return web.Response(text="ConsolidationHunter v2 Active", status=200)

async def start_background_tasks(app):
    """
    Запускает сканер как фоновую задачу при старте веб-сервера.
    """
    app['scanner_task'] = asyncio.create_task(scanner_loop())

async def cleanup_background_tasks(app):
    """
    Корректно отменяет задачу сканера при остановке сервера.
    """
    app['scanner_task'].cancel()
    try:
        await app['scanner_task']
    except asyncio.CancelledError:
        pass

def main():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Остановлено")
