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
# ОБЩИЕ ФИЛЬТРЫ
# -------------------------------------------------------------

MIN_24H_VOLUME_USDT = 2_000_000

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

# Минимум стабильных свечей из последних 7
QUIET_STABLE_COUNT = 5

# Денежный поток должен быть примерно стабильным:
# 0.5x - 1.5x от среднего
QUIET_MONEY_MIN_MULT = 0.5
QUIET_MONEY_MAX_MULT = 1.5

# Цена должна находиться ближе к верхней части полки
QUIET_MIN_POSITION_PCT = 55

# Текущая свеча для тихого сигнала
QUIET_MAX_CURRENT_RANGE_PCT = 1.5
QUIET_MAX_CURRENT_BODY_PCT = 0.8

# Тихий сигнал не требует огромного объёма,
# иначе он превращается в агрессивный.
QUIET_MIN_ACTUAL_RVOL = 0.8

# -------------------------------------------------------------
# ⚡ СРЕДНИЙ СИГНАЛ — ПРОБОЙ
# -------------------------------------------------------------

BREAKOUT_MIN_CHANGE_PCT = 1.6

# Реальный объём текущей свечи относительно средней свечи
BREAKOUT_MIN_ACTUAL_RVOL = 1.5

# Минимальный выход выше верхней границы полки
BREAKOUT_MIN_ABOVE_SHELF_PCT = 0.05

# -------------------------------------------------------------
# 🔥 АГРЕССИВНЫЙ СИГНАЛ
# -------------------------------------------------------------

# Агрессивный должен работать именно от большого объёма.
AGGRESSIVE_MIN_ACTUAL_RVOL = 2.5

# Минимальное движение цены
AGGRESSIVE_MIN_CHANGE_PCT = 0.8

# Окно последних минут часа
AGGRESSIVE_WINDOW_START = 49
AGGRESSIVE_WINDOW_END = 56

# В окне 49-56 можно разрешить чуть меньший RVOL,
# потому что сама позиция внутри часа уже является подтверждением.
AGGRESSIVE_WINDOW_RVOL = 2.0

# Если объём экстремальный — разрешаем сигнал ещё раньше.
AGGRESSIVE_EXTREME_RVOL = 4.0

# Экстремальный объём может дать агрессивный сигнал
# даже при меньшем изменении цены.
AGGRESSIVE_EXTREME_CHANGE_PCT = 0.5

# -------------------------------------------------------------
# EMA
# -------------------------------------------------------------

EMA_FAST = 20
EMA_SLOW = 40

# EMA НЕ является жёстким фильтром.
# Она только добавляет подтверждение к score.

# -------------------------------------------------------------
# COOLDOWN
# -------------------------------------------------------------

COOLDOWN_QUIET = 1800          # 30 минут
COOLDOWN_BREAKOUT = 3600       # 60 минут
COOLDOWN_AGGRESSIVE = 900      # 15 минут

# -------------------------------------------------------------
# КЭШ
# -------------------------------------------------------------

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
    """
    BingX kline:

    [0] timestamp
    [1] open
    [2] high
    [3] low
    [4] close
    [5] volume

    Возвращает:
    open, high, low, close, volume
    """

    return (
        float(kline[1]),
        float(kline[2]),
        float(kline[3]),
        float(kline[4]),
        float(kline[5])
    )


def calculate_ema(prices, period):
    if not prices:
        return 0.0

    if len(prices) == 1:
        return float(prices[0])

    alpha = 2 / (period + 1)

    ema_value = float(prices[0])

    for price in prices[1:]:
        ema_value = (
            alpha * float(price)
            + (1 - alpha) * ema_value
        )

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
# EMA
# =============================================================

def check_ema_support(klines_1h, shelf_high, shelf_low):
    """
    EMA используется только как подтверждение.

    НЕ блокирует сигнал.
    """

    if len(klines_1h) < EMA_SLOW + 2:
        return {
            "score": 0,
            "detail": "",
            "ema20": 0.0,
            "ema40": 0.0
        }

    # Для EMA берём только закрытые свечи.
    # Текущая незакрытая свеча не должна двигать EMA.
    closed = klines_1h[:-1]

    closes = [
        parse_kline(k)[3]
        for k in closed
    ]

    if len(closes) < EMA_SLOW:
        return {
            "score": 0,
            "detail": "",
            "ema20": 0.0,
            "ema40": 0.0
        }

    ema20 = calculate_ema(closes, EMA_FAST)
    ema40 = calculate_ema(closes, EMA_SLOW)

    current_price = parse_kline(klines_1h[-1])[3]

    score = 0
    detail = ""

    # Лучший вариант:
    # EMA20 находится внутри полки.
    if shelf_low <= ema20 <= shelf_high:
        score = 15
        detail = "EMA20 находится внутри полки накопления"

    elif shelf_low <= ema40 <= shelf_high:
        score = 12
        detail = "EMA40 находится внутри полки накопления"

    elif ema20 > ema40 and current_price >= ema20:
        score = 10
        detail = "EMA20 > EMA40 — бычья структура"

    elif current_price >= ema20:
        score = 5
        detail = "Цена выше EMA20"

    else:
        score = 0
        detail = "EMA без дополнительного подтверждения"

    return {
        "score": score,
        "detail": detail,
        "ema20": ema20,
        "ema40": ema40
    }


# =============================================================
# ПОИСК ПОЛКИ
# =============================================================

def find_shelf_before_breakout(klines_1h):
    """
    ВАЖНО:

    Текущая H1 свеча НЕ участвует в формировании полки.

    Иначе получится ошибка:
    памп текущей свечи расширяет полку,
    а затем бот думает, что памп является частью накопления.
    """

    if len(klines_1h) < BASE_MIN_HOURS + 1:
        return None

    closed_candles = klines_1h[:-1]

    if len(closed_candles) < BASE_MIN_HOURS:
        return None

    highs = []
    lows = []
    money = []

    for k in closed_candles:
        o, h, l, c, v = parse_kline(k)

        if l <= 0 or c <= 0:
            continue

        highs.append(h)
        lows.append(l)

        # Объём свечи в USDT
        money.append(v * c)

    if len(highs) < BASE_MIN_HOURS:
        return None

    # ---------------------------------------------------------
    # ОСНОВНОЙ ПОИСК
    # ---------------------------------------------------------

    for hours in range(
        BASE_MAX_HOURS,
        BASE_MIN_HOURS - 1,
        -1
    ):

        if len(highs) < hours:
            continue

        sub_highs = highs[-hours:]
        sub_lows = lows[-hours:]
        sub_money = money[-hours:]

        shelf_high = max(sub_highs)
        shelf_low = min(sub_lows)

        if shelf_low <= 0:
            continue

        width = (
            (shelf_high - shelf_low)
            / shelf_low
            * 100
        )

        if width > BASE_MAX_WIDTH_PCT:
            continue

        avg_money = np.mean(sub_money)

        if avg_money <= 0:
            continue

        stable_count = sum(
            1
            for v in sub_money
            if (
                QUIET_MONEY_MIN_MULT * avg_money
                <= v
                <= QUIET_MONEY_MAX_MULT * avg_money
            )
        )

        stability_ratio = (
            stable_count / len(sub_money)
        )

        # Основная полка должна быть достаточно спокойной.
        if stability_ratio >= 0.70:

            return {
                "hours": hours,
                "width": round(width, 2),
                "high": shelf_high,
                "low": shelf_low,
                "avg_money": float(avg_money),
                "stable_count": stable_count,
                "candles": closed_candles[-hours:]
            }

    # ---------------------------------------------------------
    # РАСШИРЕННЫЙ ПОИСК
    # ---------------------------------------------------------

    for hours in range(
        EXTENDED_BASE_MAX_HOURS,
        BASE_MAX_HOURS,
        -1
    ):

        if len(highs) < hours:
            continue

        sub_highs = highs[-hours:]
        sub_lows = lows[-hours:]
        sub_money = money[-hours:]

        shelf_high = max(sub_highs)
        shelf_low = min(sub_lows)

        if shelf_low <= 0:
            continue

        width = (
            (shelf_high - shelf_low)
            / shelf_low
            * 100
        )

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

def check_quiet_accumulation(
    klines_1h,
    shelf_high,
    shelf_low
):

    if len(klines_1h) < 9:
        return None

    # 7 закрытых свечей непосредственно перед текущей.
    candles = klines_1h[-8:-1]

    ranges = []
    bodies = []
    money = []
    closes = []

    for k in candles:

        o, h, l, c, v = parse_kline(k)

        if o <= 0 or l <= 0:
            continue

        ranges.append(
            (h - l) / l * 100
        )

        bodies.append(
            abs(c - o) / o * 100
        )

        money.append(v * c)
        closes.append(c)

    if len(money) < 5:
        return None

    avg_money = np.mean(money)

    if avg_money <= 0:
        return None

    stable_count = sum(
        1
        for value in money
        if (
            QUIET_MONEY_MIN_MULT * avg_money
            <= value
            <= QUIET_MONEY_MAX_MULT * avg_money
        )
    )

    avg_range = np.mean(ranges)
    avg_body = np.mean(bodies)

    close_average = np.mean(closes)

    if close_average <= 0:
        return None

    close_deviations = [
        abs(c - close_average)
        / close_average
        * 100
        for c in closes
    ]

    max_close_deviation = max(close_deviations)

    is_quiet = (
        avg_range <= QUIET_MAX_RANGE_PCT
        and
        avg_body <= QUIET_MAX_BODY_PCT
        and
        stable_count >= QUIET_STABLE_COUNT
    )

    if not is_quiet:
        return None

    current_close = parse_kline(klines_1h[-1])[3]

    if shelf_high <= shelf_low:
        position = 50.0
    else:
        position = (
            (current_close - shelf_low)
            /
            (shelf_high - shelf_low)
            * 100
        )

    return {
        "stable_count": stable_count,
        "avg_range": avg_range,
        "avg_body": avg_body,
        "max_close_deviation": max_close_deviation,
        "position": position,
        "avg_money": avg_money
    }


# =============================================================
# СТАКАН
# =============================================================

async def check_orderbook_pump(session, symbol):

    url = (
        f"{BINGX_BASE_URL}"
        "/openApi/swap/v2/quote/depth"
    )

    params = {
        "symbol": symbol,
        "limit": 10
    }

    try:

        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=3)
        ) as resp:

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

            total_bid = sum(
                safe_float(b[0]) * safe_float(b[1])
                for b in bids
            )

            total_ask = sum(
                safe_float(a[0]) * safe_float(a[1])
                for a in asks
            )

            if total_bid <= 0 or total_ask <= 0:
                return None

            bid_ask_ratio = (
                total_bid / total_ask
            )

            max_bid_wall = max(
                safe_float(b[0]) * safe_float(b[1])
                for b in bids
            )

            wall_pct = (
                max_bid_wall / total_bid * 100
                if total_bid > 0
                else 0
            )

            # Только действительно заметная стенка.
            if (
                max_bid_wall >= 50_000
                and wall_pct >= 50
            ):

                return {
                    "wall_usdt": int(max_bid_wall),
                    "wall_pct": round(wall_pct),
                    "bid_ask_ratio": round(
                        bid_ask_ratio,
                        2
                    )
                }

    except Exception:
        return None

    return None


# =============================================================
# ОЦЕНКА ТРЁХ СИГНАЛОВ
# =============================================================

def evaluate_signals(
    symbol,
    klines_1h,
    volume_24h_usdt=0.0
):

    base_symbol = get_base_symbol(symbol)

    if base_symbol in EXCLUDED_SYMBOLS:
        return []

    if volume_24h_usdt < MIN_24H_VOLUME_USDT:
        return []

    if len(klines_1h) < 50:
        return []

    # ---------------------------------------------------------
    # ТЕКУЩАЯ СВЕЧА
    # ---------------------------------------------------------

    current = klines_1h[-1]

    open_h, high_h, low_h, close_h, vol_raw = parse_kline(
        current
    )

    if (
        open_h <= 0
        or low_h <= 0
        or close_h <= 0
    ):
        return []

    now = datetime.datetime.now(
        datetime.timezone.utc
    )

    elapsed = (
        now.minute
        + now.second / 60
    )

    if elapsed < 1:
        elapsed = 1.0

    # ---------------------------------------------------------
    # ДВИЖЕНИЕ
    # ---------------------------------------------------------

    change_pct = (
        (close_h - open_h)
        / open_h
        * 100
    )

    candle_range_pct = (
        (high_h - low_h)
        / low_h
        * 100
    )

    body_pct = (
        abs(close_h - open_h)
        / open_h
        * 100
    )

    # ---------------------------------------------------------
    # ОБЪЁМ
    # ---------------------------------------------------------

    current_money = (
        vol_raw * close_h
    )

    # Только закрытые свечи.
    past_money = []

    for k in klines_1h[-21:-1]:

        o, h, l, c, v = parse_kline(k)

        if c > 0:
            past_money.append(v * c)

    if not past_money:
        return []

    avg_money = np.mean(past_money)

    if avg_money <= 0:
        return []

    # ---------------------------------------------------------
    # РЕАЛЬНЫЙ RVOL
    #
    # Это важно для агрессивного режима.
    #
    # Мы НЕ прогнозируем весь час.
    # Смотрим сколько денег уже прошло через рынок
    # относительно обычной H1 свечи.
    # ---------------------------------------------------------

    actual_rvol = (
        current_money / avg_money
    )

    # ---------------------------------------------------------
    # ПРОЕКЦИЯ RVOL
    #
    # Дополнительная информация.
    # НЕ используется как главный триггер агрессивного.
    # ---------------------------------------------------------

    projected_money = (
        current_money
        * 60
        / max(elapsed, 1)
    )

    projected_rvol = (
        projected_money / avg_money
    )

    # Ограничиваем безумные значения в первые минуты.
    if elapsed < 3:
        projected_rvol = min(
            projected_rvol,
            5.0
        )

    elif elapsed < 5:
        projected_rvol = min(
            projected_rvol,
            7.0
        )

    # ---------------------------------------------------------
    # ПОЛКА
    # ---------------------------------------------------------

    shelf = find_shelf_before_breakout(
        klines_1h
    )

    if not shelf:
        return []

    shelf_high = shelf["high"]
    shelf_low = shelf["low"]

    if shelf_high <= shelf_low:
        return []

    # ---------------------------------------------------------
    # ПОЗИЦИЯ В ПОЛКЕ
    # ---------------------------------------------------------

    position_in_shelf = (
        (close_h - shelf_low)
        /
        (shelf_high - shelf_low)
        * 100
    )

    # ---------------------------------------------------------
    # ПРОБОЙ
    # ---------------------------------------------------------

    above_shelf = (
        close_h > shelf_high
    )

    breakout_pct = 0.0

    if above_shelf:

        breakout_pct = (
            (close_h - shelf_high)
            / shelf_high
            * 100
        )

    # ---------------------------------------------------------
    # EMA
    # ---------------------------------------------------------

    ema = check_ema_support(
        klines_1h,
        shelf_high,
        shelf_low
    )

    ema_score = ema["score"]
    ema_detail = ema["detail"]

    signals = []

    # =========================================================
    # 🐭 1. ТИХИЙ СИГНАЛ
    # =========================================================

    quiet_data = check_quiet_accumulation(
        klines_1h,
        shelf_high,
        shelf_low
    )

    if quiet_data:

        quiet_valid = (
            position_in_shelf
            >= QUIET_MIN_POSITION_PCT
            and
            candle_range_pct
            <= QUIET_MAX_CURRENT_RANGE_PCT
            and
            body_pct
            <= QUIET_MAX_CURRENT_BODY_PCT
            and
            change_pct
            >= -0.05
            and
            actual_rvol
            >= QUIET_MIN_ACTUAL_RVOL
            and
            not above_shelf
        )

        if quiet_valid:

            score = 80

            score += (
                ema_score // 3
            )

            if position_in_shelf >= 75:
                score += 5

            if quiet_data["stable_count"] >= 6:
                score += 5

            signals.append({

                "mode": "QUIET",

                "type":
                    "QUIET_ACCUMULATION",

                "signal_name":
                    "🐭 ТИХАЯ АККУМУЛЯЦИЯ",

                "score":
                    min(score, 95),

                "price":
                    close_h,

                "change_pct":
                    round(change_pct, 2),

                "actual_rvol":
                    round(actual_rvol, 2),

                "projected_rvol":
                    round(projected_rvol, 1),

                "elapsed":
                    int(elapsed),

                "shelf_hours":
                    shelf["hours"],

                "shelf_width":
                    shelf["width"],

                "shelf_high":
                    shelf_high,

                "shelf_low":
                    shelf_low,

                "position_pct":
                    round(position_in_shelf),

                "avg_shelf_money":
                    shelf["avg_money"],

                "ema_detail":
                    ema_detail,

                "reasons": [

                    f"Тихая аккумуляция: "
                    f"{shelf['hours']}ч",

                    f"Стабильный денежный поток: "
                    f"{quiet_data['stable_count']}/7",

                    f"Позиция в полке: "
                    f"{position_in_shelf:.0f}%",

                    f"Текущий RVOL: "
                    f"x{actual_rvol:.2f}",

                    f"Диапазон H1: "
                    f"{candle_range_pct:.2f}%",

                    ema_detail
                ]
            })

    # =========================================================
    # ⚡ 2. СРЕДНИЙ СИГНАЛ
    # =========================================================

    normal_valid = (

        above_shelf

        and

        breakout_pct
        >= BREAKOUT_MIN_ABOVE_SHELF_PCT

        and

        change_pct
        >= BREAKOUT_MIN_CHANGE_PCT

        and

        actual_rvol
        >= BREAKOUT_MIN_ACTUAL_RVOL
    )

    if normal_valid:

        score = 78

        score += (
            ema_score // 2
        )

        if actual_rvol >= 2.0:
            score += 5

        if breakout_pct >= 0.5:
            score += 4

        signals.append({

            "mode": "NORMAL",

            "type":
                "BREAKOUT",

            "signal_name":
                "⚡ ПРОБОЙ ПОЛКИ",

            "score":
                min(score, 95),

            "price":
                close_h,

            "change_pct":
                round(change_pct, 2),

            "actual_rvol":
                round(actual_rvol, 2),

            "projected_rvol":
                round(projected_rvol, 1),

            "elapsed":
                int(elapsed),

            "shelf_hours":
                shelf["hours"],

            "shelf_width":
                shelf["width"],

            "shelf_high":
                shelf_high,

            "shelf_low":
                shelf_low,

            "breakout_pct":
                round(breakout_pct, 2),

            "avg_shelf_money":
                shelf["avg_money"],

            "ema_detail":
                ema_detail,

            "reasons": [

                f"Пробой полки: "
                f"+{change_pct:.2f}%",

                f"Выход выше полки: "
                f"+{breakout_pct:.2f}%",

                f"Реальный RVOL: "
                f"x{actual_rvol:.2f}",

                f"Проекция RVOL: "
                f"x{projected_rvol:.1f}",

                f"Полка: "
                f"{shelf['hours']}ч / "
                f"{shelf['width']}%",

                ema_detail
            ]
        })

    # =========================================================
    # 🔥 3. АГРЕССИВНЫЙ СИГНАЛ
    # =========================================================

    in_aggressive_window = (
        AGGRESSIVE_WINDOW_START
        <= int(elapsed)
        <= AGGRESSIVE_WINDOW_END
    )

    # Основной принцип:
    #
    # Агрессивный = большой РЕАЛЬНЫЙ объём.
    #
    # Не ждём обязательного пробоя.
    # Не ждём закрытия свечи.
    #
    # Это и есть ранний вход.
    # ---------------------------------------------------------

    aggressive_by_volume = (
        actual_rvol
        >= AGGRESSIVE_MIN_ACTUAL_RVOL
        and
        change_pct
        >= AGGRESSIVE_MIN_CHANGE_PCT
    )

    aggressive_by_extreme_volume = (
        actual_rvol
        >= AGGRESSIVE_EXTREME_RVOL
        and
        change_pct
        >= AGGRESSIVE_EXTREME_CHANGE_PCT
    )

    aggressive_by_window = (
        in_aggressive_window
        and
        actual_rvol
        >= AGGRESSIVE_WINDOW_RVOL
        and
        change_pct
        >= AGGRESSIVE_MIN_CHANGE_PCT
    )

    aggressive_valid = (
        aggressive_by_volume
        or
        aggressive_by_extreme_volume
        or
        aggressive_by_window
    )

    if aggressive_valid:

        score = 84

        # Большой объём — главное.
        if actual_rvol >= 3.0:
            score += 4

        if actual_rvol >= 4.0:
            score += 4

        # EMA только усиливает.
        score += (
            ema_score // 3
        )

        # Выход из полки — дополнительный бонус,
        # но НЕ обязательное условие.
        if above_shelf:
            score += 5

        # Последние минуты часа дают дополнительный бонус.
        if in_aggressive_window:
            score += 4

        signals.append({

            "mode":
                "AGGRESSIVE",

            "type":
                "AGGRESSIVE_FLOOD",

            "signal_name":
                "🔥 АГРЕССИВНЫЙ ВХОД",

            "score":
                min(score, 99),

            "price":
                close_h,

            "change_pct":
                round(change_pct, 2),

            "actual_rvol":
                round(actual_rvol, 2),

            "projected_rvol":
                round(projected_rvol, 1),

            "elapsed":
                int(elapsed),

            "shelf_hours":
                shelf["hours"],

            "shelf_width":
                shelf["width"],

            "shelf_high":
                shelf_high,

            "shelf_low":
                shelf_low,

            "position_pct":
                round(position_in_shelf),

            "breakout_pct":
                round(breakout_pct, 2),

            "above_shelf":
                above_shelf,

            "avg_shelf_money":
                shelf["avg_money"],

            "ema_detail":
                ema_detail,

            "window":
                in_aggressive_window,

            "reasons": [

                f"🔥 Большой реальный объём: "
                f"RVOL x{actual_rvol:.2f}",

                f"Движение H1: "
                f"+{change_pct:.2f}%",

                (
                    f"⏰ Усиленное окно "
                    f"{AGGRESSIVE_WINDOW_START}-"
                    f"{AGGRESSIVE_WINDOW_END} мин"
                    if in_aggressive_window
                    else
                    "Ранний объёмный вход"
                ),

                (
                    "🚀 Цена уже выше полки"
                    if above_shelf
                    else
                    "⚡ Цена ещё может быть в полке — "
                    "вход ловим ДО полного пробоя"
                ),

                f"Позиция: "
                f"{position_in_shelf:.0f}% полки",

                f"Полка: "
                f"{shelf['hours']}ч / "
                f"{shelf['width']}%",

                ema_detail
            ]
        })

    return signals


# =============================================================
# ФОРМАТ СООБЩЕНИЯ
# =============================================================

def format_signal_message(
    symbol,
    signal,
    orderbook=None
):

    clean = get_base_symbol(symbol)

    price = signal["price"]

    if price >= 1:
        price_str = f"{price:.4f}".rstrip("0").rstrip(".")
    elif price >= 0.01:
        price_str = f"{price:.6f}".rstrip("0").rstrip(".")
    else:
        price_str = f"{price:.10f}".rstrip("0").rstrip(".")

    mode = signal["mode"]

    if mode == "QUIET":
        mode_text = "🐭 ТИХИЙ"

    elif mode == "NORMAL":
        mode_text = "⚡ СРЕДНИЙ"

    else:
        mode_text = "🔥 АГРЕССИВНЫЙ"

    msg = (
        f"<b>{signal['signal_name']}</b>\n"
        f"<b>Режим: {mode_text}</b>\n\n"
    )

    msg += (
        f"📌 Монета: "
        f"<b>{clean} / USDT</b>\n"
    )

    msg += (
        f"💵 Цена: "
        f"<code>{price_str}</code>\n"
    )

    msg += (
        f"📊 Изменение H1: "
        f"<b>{signal['change_pct']:+.2f}%</b>\n"
    )

    msg += (
        f"📈 Реальный RVOL: "
        f"<b>x{signal['actual_rvol']:.2f}</b>\n"
    )

    msg += (
        f"📈 Проекция RVOL: "
        f"<b>x{signal['projected_rvol']:.1f}</b>\n"
    )

    msg += (
        f"⏰ Минута часа: "
        f"<b>{signal['elapsed']} / 60</b>\n"
    )

    msg += (
        f"📦 Полка: "
        f"<b>{signal['shelf_hours']}ч</b> | "
        f"ширина {signal['shelf_width']}%\n"
    )

    msg += (
        f"📍 Позиция в полке: "
        f"<b>{signal.get('position_pct', 0)}%</b>\n"
    )

    if signal.get("breakout_pct", 0) > 0:

        msg += (
            f"🚀 Пробой: "
            f"<b>+{signal['breakout_pct']:.2f}%</b>\n"
        )

    msg += (
        f"⭐ Score: "
        f"<b>{signal['score']}/100</b>\n"
    )

    # Особая надпись для агрессивного.
    if mode == "AGGRESSIVE":

        if signal.get("window"):

            msg += (
                "\n🔥 <b>49–56 МИНУТА — "
                "УСИЛЕННОЕ ОКНО</b>\n"
            )

        msg += (
            "⚡ <b>Главный триггер — большой объём.</b>\n"
            "Бот не ждёт обязательного закрытия H1 "
            "или полного пробоя полки.\n"
        )

    msg += (
        "\n<b>🔍 Анализ:</b>\n"
    )

    for reason in signal.get("reasons", []):

        if reason:

            msg += f"• {reason}\n"

    if orderbook:

        msg += (
            "\n📖 <b>Стакан:</b>\n"
            f"Стенка: "
            f"${orderbook['wall_usdt']:,}\n"
            f"Доля стенки: "
            f"{orderbook['wall_pct']}%\n"
            f"Bid/Ask: "
            f"x{orderbook['bid_ask_ratio']}\n"
        )

    return msg


# =============================================================
# TELEGRAM
# =============================================================

async def send_telegram(
    session,
    message
):

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        logger.error(
            "TELEGRAM_BOT_TOKEN или "
            "TELEGRAM_CHAT_ID не задан"
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:

        async with session.post(
            url,
            data=data,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:

            if resp.status == 200:
                return True

            text = await resp.text()

            logger.warning(
                f"Telegram HTTP {resp.status}: "
                f"{text[:300]}"
            )

    except Exception as e:

        logger.warning(
            f"Ошибка Telegram: {e}"
        )

    return False


# =============================================================
# 24H VOLUME
# =============================================================

async def fetch_24h_volume(
    session,
    symbol
):

    url = (
        f"{BINGX_BASE_URL}"
        "/openApi/swap/v2/quote/ticker"
    )

    params = {
        "symbol": symbol
    }

    try:

        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:

            if resp.status != 200:
                return 0.0

            data = await resp.json()

            if data.get("code") != 0:
                return 0.0

            ticker = data.get(
                "data",
                {}
            )

            return safe_float(
                ticker.get(
                    "quoteVolume",
                    0
                )
            )

    except Exception:
        return 0.0


# =============================================================
# СПИСОК ФЬЮЧЕРСОВ
# =============================================================

async def fetch_futures_symbols(
    session
):

    url = (
        f"{BINGX_BASE_URL}"
        "/openApi/swap/v2/quote/contracts"
    )

    try:

        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:

            if resp.status != 200:
                return []

            data = await resp.json()

            if data.get("code") != 0:
                return []

            result = []

            for item in data.get(
                "data",
                []
            ):

                symbol = item.get(
                    "symbol",
                    ""
                )

                status = item.get(
                    "status"
                )

                if (
                    symbol.endswith("-USDT")
                    and status == 1
                ):

                    base = get_base_symbol(
                        symbol
                    )

                    if base not in EXCLUDED_SYMBOLS:
                        result.append(symbol)

            return result

    except Exception as e:

        logger.warning(
            f"Ошибка получения символов: {e}"
        )

        return []


# =============================================================
# KLINES
# =============================================================

async def fetch_klines(
    session,
    symbol,
    interval="1h",
    limit=80
):

    url = (
        f"{BINGX_BASE_URL}"
        "/openApi/swap/v3/quote/klines"
    )

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:

        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:

            if resp.status != 200:
                return None

            data = await resp.json()

            if data.get("code") != 0:
                return None

            klines = data.get(
                "data"
            )

            if not klines:
                return None

            return klines

    except Exception:
        return None


# =============================================================
# COOLDOWN
# =============================================================

def get_cooldown(signal_type):

    if signal_type == "QUIET_ACCUMULATION":
        return COOLDOWN_QUIET

    if signal_type == "BREAKOUT":
        return COOLDOWN_BREAKOUT

    if signal_type == "AGGRESSIVE_FLOOD":
        return COOLDOWN_AGGRESSIVE

    return 1800


# =============================================================
# ОБРАБОТКА ОДНОЙ МОНЕТЫ
# =============================================================

async def process_symbol(
    session,
    symbol,
    semaphore,
    volume_cache,
    kline_cache,
    sent_signals
):

    async with semaphore:

        try:

            loop_time = (
                asyncio.get_event_loop().time()
            )

            # -------------------------------------------------
            # 24H VOLUME
            # -------------------------------------------------

            cached_volume = (
                volume_cache.get(symbol)
            )

            if cached_volume:

                volume_24h = cached_volume["volume"]

                age = (
                    loop_time
                    - cached_volume["time"]
                )

                if age > VOLUME_CACHE_SECONDS:

                    volume_24h = (
                        await fetch_24h_volume(
                            session,
                            symbol
                        )
                    )

                    volume_cache[symbol] = {
                        "volume": volume_24h,
                        "time": loop_time
                    }

            else:

                volume_24h = (
                    await fetch_24h_volume(
                        session,
                        symbol
                    )
                )

                volume_cache[symbol] = {
                    "volume": volume_24h,
                    "time": loop_time
                }

            if volume_24h < MIN_24H_VOLUME_USDT:
                return []

            # -------------------------------------------------
            # KLINES CACHE
            # -------------------------------------------------

            cached_kline = (
                kline_cache.get(symbol)
            )

            klines = None

            if cached_kline:

                age = (
                    loop_time
                    - cached_kline["time"]
                )

                if age <= KLINE_CACHE_SECONDS:
                    klines = cached_kline["data"]

            if klines is None:

                klines = await fetch_klines(
                    session,
                    symbol,
                    "1h",
                    80
                )

                if not klines:
                    return []

                kline_cache[symbol] = {
                    "data": klines,
                    "time": loop_time
                }

            if len(klines) < 50:
                return []

            # -------------------------------------------------
            # ТРИ СИГНАЛА
            # -------------------------------------------------

            signals = evaluate_signals(
                symbol,
                klines,
                volume_24h
            )

            if not signals:
                return []

            sent = []

            # -------------------------------------------------
            # КАЖДЫЙ ТИП СИГНАЛА ИМЕЕТ СВОЙ COOLDOWN
            # -------------------------------------------------

            for signal in signals:

                signal_type = signal["type"]

                key = (
                    symbol,
                    signal_type
                )

                last_sent = sent_signals.get(
                    key,
                    0
                )

                cooldown = get_cooldown(
                    signal_type
                )

                if (
                    loop_time - last_sent
                    < cooldown
                ):
                    continue

                # -------------------------------------------------
                # СТАКАН
                # -------------------------------------------------

                orderbook = (
                    await check_orderbook_pump(
                        session,
                        symbol
                    )
                )

                if orderbook:

                    signal["score"] = min(
                        99,
                        signal["score"] + 5
                    )

                message = format_signal_message(
                    symbol,
                    signal,
                    orderbook
                )

                success = await send_telegram(
                    session,
                    message
                )

                if success:

                    sent_signals[key] = (
                        loop_time
                    )

                    sent.append(signal)

                    logger.info(
                        f"🚨 {symbol} | "
                        f"{signal_type} | "
                        f"RVOL x"
                        f"{signal['actual_rvol']:.2f} | "
                        f"{signal['elapsed']} мин"
                    )

            return sent

        except Exception as e:

            logger.debug(
                f"Ошибка {symbol}: {e}"
            )

            return []


# =============================================================
# ГЛАВНЫЙ СКАНЕР
# =============================================================

async def scanner_loop():

    logger.info(
        "🚀 ConsolidationHunter запущен"
    )

    logger.info(
        "🐭 Тихий + ⚡ Средний + "
        "🔥 Агрессивный работают ОДНОВРЕМЕННО"
    )

    logger.info(
        f"🔥 Агрессивный RVOL: "
        f"x{AGGRESSIVE_MIN_ACTUAL_RVOL}"
    )

    logger.info(
        f"⏰ Агрессивное окно: "
        f"{AGGRESSIVE_WINDOW_START}-"
        f"{AGGRESSIVE_WINDOW_END} мин"
    )

    volume_cache = {}
    kline_cache = {}

    sent_signals = {}

    connector = aiohttp.TCPConnector(
        limit=30,
        limit_per_host=20,
        ttl_dns_cache=300
    )

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT
    )

    async with aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent":
                "ConsolidationHunter/1.0"
        }
    ) as session:

        while True:

            scan_start = (
                asyncio.get_event_loop().time()
            )

            try:

                symbols = (
                    await fetch_futures_symbols(
                        session
                    )
                )

                if not symbols:

                    logger.warning(
                        "Не удалось получить "
                        "список фьючерсов"
                    )

                    await asyncio.sleep(30)

                    continue

                logger.info(
                    f"🔍 Сканирую "
                    f"{len(symbols)} пар..."
                )

                tasks = [

                    process_symbol(
                        session,
                        symbol,
                        semaphore,
                        volume_cache,
                        kline_cache,
                        sent_signals
                    )

                    for symbol in symbols
                ]

                results = await asyncio.gather(
                    *tasks,
                    return_exceptions=True
                )

                quiet_count = 0
                normal_count = 0
                aggressive_count = 0

                for result in results:

                    if (
                        isinstance(
                            result,
                            Exception
                        )
                    ):
                        continue

                    if not result:
                        continue

                    for signal in result:

                        if signal["type"] == \
                                "QUIET_ACCUMULATION":

                            quiet_count += 1

                        elif signal["type"] == \
                                "BREAKOUT":

                            normal_count += 1

                        elif signal["type"] == \
                                "AGGRESSIVE_FLOOD":

                            aggressive_count += 1

                elapsed = (
                    asyncio.get_event_loop().time()
                    - scan_start
                )

                logger.info(
                    f"✅ Скан за {elapsed:.1f}с | "
                    f"🐭 {quiet_count} | "
                    f"⚡ {normal_count} | "
                    f"🔥 {aggressive_count}"
                )

                # -------------------------------------------------
                # ОЧИСТКА COOLDOWN
                # -------------------------------------------------

                if len(sent_signals) > 2000:

                    old_keys = [

                        key

                        for key, timestamp
                        in sent_signals.items()

                        if (
                            scan_start - timestamp
                            > 7200
                        )
                    ]

                    for key in old_keys:
                        del sent_signals[key]

                # -------------------------------------------------
                # ОЧИСТКА VOLUME CACHE
                # -------------------------------------------------

                if len(volume_cache) > 1500:

                    old_symbols = [

                        symbol

                        for symbol, data
                        in volume_cache.items()

                        if (
                            scan_start
                            - data["time"]
                            > 900
                        )
                    ]

                    for symbol in old_symbols:
                        del volume_cache[symbol]

                # -------------------------------------------------
                # ОЧИСТКА KLINE CACHE
                # -------------------------------------------------

                if len(kline_cache) > 1500:

                    old_symbols = [

                        symbol

                        for symbol, data
                        in kline_cache.items()

                        if (
                            scan_start
                            - data["time"]
                            > 120
                        )
                    ]

                    for symbol in old_symbols:
                        del kline_cache[symbol]

                await asyncio.sleep(20)

            except asyncio.CancelledError:

                logger.info(
                    "Scanner остановлен"
                )

                break

            except Exception as e:

                logger.error(
                    f"Ошибка scanner_loop: {e}"
                )

                await asyncio.sleep(30)


# =============================================================
# WEB SERVER
# =============================================================

async def handle_ping(request):

    return web.Response(
        text="ConsolidationHunter Active",
        status=200
    )


async def handle_health(request):

    return web.json_response({

        "status": "ok",

        "scanner":
            "running",

        "modes": [
            "QUIET",
            "NORMAL",
            "AGGRESSIVE"
        ],

        "aggressive_window": [
            AGGRESSIVE_WINDOW_START,
            AGGRESSIVE_WINDOW_END
        ],

        "aggressive_actual_rvol":
            AGGRESSIVE_MIN_ACTUAL_RVOL
    })


# =============================================================
# MAIN
# =============================================================

async def main():

    app = web.Application()

    app.router.add_get(
        "/",
        handle_ping
    )

    app.router.add_get(
        "/health",
        handle_health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    logger.info(
        f"🌐 Web server: "
        f"0.0.0.0:{port}"
    )

    await scanner_loop()


# =============================================================
# START
# =============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "🛑 Остановлено пользователем"
        )
