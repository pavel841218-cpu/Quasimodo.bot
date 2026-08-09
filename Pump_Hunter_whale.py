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

EXCLUDED_SYMBOLS = {"USDC", "FDUSD"}

MAX_CONCURRENT = 30  # Оптимально для быстрого параллельного скана

# -------------------------------------------------------------
# ПОЛКА / НАКОПЛЕНИЕ
# -------------------------------------------------------------

BASE_MIN_HOURS = 4
BASE_MAX_HOURS = 12
BASE_MAX_WIDTH_PCT = 4.5

EXTENDED_BASE_MAX_HOURS = 48
EXTENDED_BASE_MAX_WIDTH_PCT = 6.0

# ⚙️ АДАПТАЦИЯ ПОЛКИ И ФИЛЬТРАЦИЯ ШУМА (ТЕНИ / СКВИЗЫ)
MAX_REBUILT_SHELF_WIDTH_PCT = 5.5  # Максимальная ширина полки после расширения тенью
MAX_WICK_TO_BODY_RATIO = 3.0       # Порог аномальной тени для среза сквизов

# -------------------------------------------------------------
# ⚡ СРЕДНИЙ СИГНАЛ — ПРОБОЙ
# -------------------------------------------------------------

BREAKOUT_MIN_CHANGE_PCT = 1.6
BREAKOUT_MIN_ACTUAL_RVOL = 1.5
BREAKOUT_MIN_ABOVE_SHELF_PCT = 0.05

# -------------------------------------------------------------
# EMA & COOLDOWN
# -------------------------------------------------------------

EMA_FAST = 20
EMA_SLOW = 40

COOLDOWN_BREAKOUT = 3600       # 60 минут
COOLDOWN_AGGRESSIVE = 900      # 15 минут

KLINE_CACHE_SECONDS = 15

# =============================================================
# LOGGING & GLOBAL STATE
# =============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("ConsolidationHunter")

# Память бота для активных полок накопления
ACTIVE_SHELVES = {}  
LAST_FULL_SCAN = 0


# =============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ДЕТЕКТОР U/W-ЯМЫ
# =============================================================

def parse_kline(kline):
    return (
        float(kline[1]),  # open
        float(kline[2]),  # high
        float(kline[3]),  # low
        float(kline[4]),  # close
        float(kline[5])   # volume
    )


def calculate_ema(prices, period):
    if not prices:
        return 0.0
    if len(prices) == 1:
        return float(prices[0])
    alpha = 2 / (period + 1)
    ema_value = float(prices[0])
    for price in prices[1:]:
        ema_value = alpha * float(price) + (1 - alpha) * ema_value
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


def get_effective_candle_bounds(open_p, high_p, low_p, close_p):
    """Срезает аномальные тени (сквизы), чтобы не размазывать полку."""
    body_top = max(open_p, close_p)
    body_bottom = min(open_p, close_p)
    body_size = abs(close_p - open_p)

    if body_size == 0:
        return high_p, low_p

    upper_wick = high_p - body_top
    lower_wick = body_bottom - low_p

    eff_high = high_p if (upper_wick / body_size) < MAX_WICK_TO_BODY_RATIO else body_top + (body_size * 1.5)
    eff_low = low_p if (lower_wick / body_size) < MAX_WICK_TO_BODY_RATIO else body_bottom - (body_size * 1.5)

    return eff_high, eff_low


def detect_u_w_dip(sub_candles):
    """
    Определяет 'Яму перед взлётом' (Shakeout / U-W паттерн):
    Провал цены в середине полки на 3-6 часов с быстрым возвратом к хаю.
    """
    if len(sub_candles) < 8:
        return False, 0.0

    closes = [parse_kline(k)[3] for k in sub_candles]
    
    start_level = np.mean(closes[:3])
    end_level = np.mean(closes[-2:])
    mid_level = np.min(closes[3:-2])
    
    if start_level <= 0 or mid_level <= 0:
        return False, 0.0

    dip_depth_pct = ((start_level - mid_level) / start_level) * 100
    edge_diff_pct = abs(start_level - end_level) / start_level * 100

    if 1.2 <= dip_depth_pct <= 5.0 and edge_diff_pct <= 2.0:
        return True, round(dip_depth_pct, 2)

    return False, 0.0


# =============================================================
# УПРАВЛЕНИЕ ПАМЯТЬЮ ПОЛОК
# =============================================================

def update_or_invalidate_shelf(symbol, current_price, current_high, current_low):
    if symbol not in ACTIVE_SHELVES:
        return "INVALIDATED"

    shelf = ACTIVE_SHELVES[symbol]
    
    if current_price < (shelf["low"] * 0.985):
        logger.info(f"❌ [ПОЛКА СЛОМАНА] {symbol} | Пролив цены ({current_price} < {shelf['low']})")
        return "INVALIDATED"

    new_high = max(shelf["high"], current_high)
    new_low = min(shelf["low"], current_low)
    
    if new_low <= 0:
        return "INVALIDATED"

    new_width = ((new_high - new_low) / new_low) * 100

    if new_width <= MAX_REBUILT_SHELF_WIDTH_PCT:
        if new_width > shelf["width"]:
            shelf["high"] = new_high
            shelf["low"] = new_low
            shelf["width"] = round(new_width, 2)
            logger.info(f"🔄 [ПОЛКА АДАПТИРОВАНА] {symbol} | Новая ширина: {new_width:.2f}%")
            return "UPDATED"
        return "KEEP"

    logger.info(f"🗑️ [ПОЛКА РАЗМАЗАНА] {symbol} | Ширина {new_width:.2f}% > {MAX_REBUILT_SHELF_WIDTH_PCT}%")
    return "INVALIDATED"


def remove_shelf(symbol):
    if symbol in ACTIVE_SHELVES:
        del ACTIVE_SHELVES[symbol]


# =============================================================
# АНАЛИЗ И ПОИСК ПОЛОК
# =============================================================

def check_ema_support(klines_1h, shelf_high, shelf_low):
    if len(klines_1h) < EMA_SLOW + 2:
        return {"score": 0, "detail": "", "ema20": 0.0, "ema40": 0.0}

    closed = klines_1h[:-1]
    closes = [parse_kline(k)[3] for k in closed]

    if len(closes) < EMA_SLOW:
        return {"score": 0, "detail": "", "ema20": 0.0, "ema40": 0.0}

    ema20 = calculate_ema(closes, EMA_FAST)
    ema40 = calculate_ema(closes, EMA_SLOW)
    current_price = parse_kline(klines_1h[-1])[3]

    if shelf_low <= ema20 <= shelf_high:
        score, detail = 15, "EMA20 находится внутри полки накопления"
    elif shelf_low <= ema40 <= shelf_high:
        score, detail = 12, "EMA40 находится внутри полки накопления"
    elif ema20 > ema40 and current_price >= ema20:
        score, detail = 10, "EMA20 > EMA40 — бычья структура"
    elif current_price >= ema20:
        score, detail = 5, "Цена выше EMA20"
    else:
        score, detail = 0, "EMA без дополнительного подтверждения"

    return {"score": score, "detail": detail, "ema20": ema20, "ema40": ema40}


def find_shelf_before_breakout(klines_1h):
    if len(klines_1h) < BASE_MIN_HOURS + 1:
        return None

    closed_candles = klines_1h[:-1]
    if len(closed_candles) < BASE_MIN_HOURS:
        return None

    highs, lows, money = [], [], []

    for k in closed_candles:
        o, h, l, c, v = parse_kline(k)
        if l <= 0 or c <= 0:
            continue
        
        eff_h, eff_l = get_effective_candle_bounds(o, h, l, c)
        highs.append(eff_h)
        lows.append(eff_l)
        money.append(v * c)

    if len(highs) < BASE_MIN_HOURS:
        return None

    # Поиск основной и расширенной полки
    for hours in range(EXTENDED_BASE_MAX_HOURS, BASE_MIN_HOURS - 1, -1):
        if len(closed_candles) < hours:
            continue
            
        sub_candles = closed_candles[-hours:]
        sub_highs = [get_effective_candle_bounds(*parse_kline(k)[:4])[0] for k in sub_candles]
        sub_lows = [get_effective_candle_bounds(*parse_kline(k)[:4])[1] for k in sub_candles]
        
        shelf_high, shelf_low = max(sub_highs), min(sub_lows)
        if shelf_low <= 0:
            continue

        width = ((shelf_high - shelf_low) / shelf_low) * 100
        
        # Детекция U/W ямы
        has_dip, dip_depth = detect_u_w_dip(sub_candles)
        max_allowed_width = 6.0 if has_dip else (BASE_MAX_WIDTH_PCT if hours <= BASE_MAX_HOURS else EXTENDED_BASE_MAX_WIDTH_PCT)

        if width <= max_allowed_width:
            return {
                "hours": hours,
                "width": round(width, 2),
                "high": shelf_high,
                "low": shelf_low,
                "has_dip": has_dip,
                "dip_depth": dip_depth
            }

    return None


# =============================================================
# СТАКАН I/O
# =============================================================

async def check_orderbook_pump(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/depth"
    params = {"symbol": symbol, "limit": 10}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("code") != 0:
                return None

            book = data.get("data", {})
            bids, asks = book.get("bids", []), book.get("asks", [])
            if not bids or not asks:
                return None

            total_bid = sum(safe_float(b[0]) * safe_float(b[1]) for b in bids)
            total_ask = sum(safe_float(a[0]) * safe_float(a[1]) for a in asks)
            if total_bid <= 0 or total_ask <= 0:
                return None

            max_bid_wall = max(safe_float(b[0]) * safe_float(b[1]) for b in bids)
            wall_pct = (max_bid_wall / total_bid * 100) if total_bid > 0 else 0

            if max_bid_wall >= 50_000 and wall_pct >= 50:
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

def evaluate_signals(symbol, klines_1h, shelf):
    base_symbol = get_base_symbol(symbol)
    if base_symbol in EXCLUDED_SYMBOLS or len(klines_1h) < 50 or not shelf:
        return []

    current = klines_1h[-1]
    open_h, high_h, low_h, close_h, vol_raw = parse_kline(current)
    if open_h <= 0 or low_h <= 0 or close_h <= 0:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    current_minute = now.minute
    elapsed = max(current_minute + now.second / 60, 1.0)

    IS_WHALE_WINDOW = 49 <= current_minute <= 56

    change_pct = ((close_h - open_h) / open_h) * 100

    current_money = vol_raw * close_h
    past_money = [parse_kline(k)[4] * parse_kline(k)[3] for k in klines_1h[-21:-1] if parse_kline(k)[3] > 0]
    
    if not past_money:
        return []

    avg_money = np.mean(past_money)
    if avg_money <= 0:
        return []

    actual_rvol = current_money / avg_money
    shelf_high, shelf_low = shelf["high"], shelf["low"]
    
    if shelf_high <= shelf_low:
        return []

    position_in_shelf = ((close_h - shelf_low) / (shelf_high - shelf_low)) * 100
    above_shelf = close_h > shelf_high
    breakout_pct = ((close_h - shelf_high) / shelf_high * 100) if above_shelf else 0.0

    ema = check_ema_support(klines_1h, shelf_high, shelf_low)
    ema_score, ema_detail = ema["score"], ema["detail"]

    signals = []

    # 1. 🔥 АГРЕССИВНЫЙ ВХОД (ОКНО КИТА 49-56 МИН)
    if IS_WHALE_WINDOW:
        if actual_rvol >= 1.8 and (change_pct >= 0.6 or position_in_shelf >= 80):
            score = 88 + (ema_score // 3)
            if above_shelf: score += 7
            if actual_rvol >= 3.0: score += 5

            reasons = [
                f"🎯 Окно кита: {current_minute}-я минута часа!",
                f"Налитие ликвидности: RVOL x{actual_rvol:.2f}",
                f"Поджим к хаю полки: {position_in_shelf:.0f}%",
                ema_detail
            ]

            if shelf.get("has_dip"):
                score += 8
                reasons.append(f"🕳️ Вытряхивание U/W-яма (-{shelf['dip_depth']}%)")

            signals.append({
                "mode": "AGGRESSIVE",
                "type": "AGGRESSIVE_FLOOD",
                "signal_name": "🚨 WHALE WINDOW (49-56 мин)",
                "score": min(score, 99),
                "price": close_h,
                "change_pct": round(change_pct, 2),
                "actual_rvol": round(actual_rvol, 2),
                "elapsed": int(elapsed),
                "shelf_hours": shelf["hours"],
                "shelf_width": shelf["width"],
                "position_pct": round(position_in_shelf),
                "reasons": reasons
            })

    # 2. ⚡ ОБЫЧНЫЙ ПРОБОЙ ПОЛКИ (В другое время)
    elif above_shelf and breakout_pct >= BREAKOUT_MIN_ABOVE_SHELF_PCT and actual_rvol >= BREAKOUT_MIN_ACTUAL_RVOL:
        score = 80 + (ema_score // 2)
        reasons = [
            f"Выход выше полки: +{breakout_pct:.2f}%",
            f"Реальный RVOL: x{actual_rvol:.2f}",
            ema_detail
        ]

        if shelf.get("has_dip"):
            score += 8
            reasons.append(f"🕳️ Вытряхивание U/W-яма (-{shelf['dip_depth']}%)")

        signals.append({
            "mode": "NORMAL",
            "type": "BREAKOUT",
            "signal_name": "⚡ ПРОБОЙ ПОЛКИ",
            "score": min(score, 95),
            "price": close_h,
            "change_pct": round(change_pct, 2),
            "actual_rvol": round(actual_rvol, 2),
            "elapsed": int(elapsed),
            "shelf_hours": shelf["hours"],
            "shelf_width": shelf["width"],
            "reasons": reasons
        })

    return signals


# =============================================================
# TELEGRAM & API
# =============================================================

def format_signal_message(symbol, signal, orderbook=None):
    clean = get_base_symbol(symbol)
    price = signal["price"]
    price_str = f"{price:.4f}".rstrip("0").rstrip(".") if price >= 1 else f"{price:.8f}".rstrip("0").rstrip(".")

    mode_map = {"NORMAL": "⚡ СРЕДНИЙ", "AGGRESSIVE": "🔥 АГРЕССИВНЫЙ"}

    msg = f"<b>{signal['signal_name']}</b>\n<b>Режим: {mode_map.get(signal['mode'], '')}</b>\n\n"
    msg += f"📌 Монета: <b>{clean} / USDT</b>\n💵 Цена: <code>{price_str}</code>\n"
    msg += f"📊 Изменение H1: <b>{signal['change_pct']:+.2f}%</b>\n"
    msg += f"📈 Реальный RVOL: <b>x{signal['actual_rvol']:.2f}</b>\n"
    msg += f"⏰ Минута часа: <b>{signal['elapsed']} / 60</b>\n"
    msg += f"📦 Полка: <b>{signal['shelf_hours']}ч</b> | ширина {signal['shelf_width']}%\n"
    msg += f"⭐ Score: <b>{signal['score']}/100</b>\n\n<b>🔍 Анализ:</b>\n"

    for reason in signal.get("reasons", []):
        msg += f"• {reason}\n"

    if orderbook:
        msg += f"\n📖 <b>Стакан:</b>\nСтенка: ${orderbook['wall_usdt']:,}\nДоля: {orderbook['wall_pct']}%\n"

    return msg


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


async def fetch_futures_symbols(session):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200: return []
            data = await resp.json()
            if data.get("code") != 0: return []
            return [i["symbol"] for i in data.get("data", []) if i.get("symbol", "").endswith("-USDT") and i.get("status") == 1 and get_base_symbol(i["symbol"]) not in EXCLUDED_SYMBOLS]
    except Exception:
        return []


async def fetch_klines(session, symbol, interval="1h", limit=80):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return data.get("data") if data.get("code") == 0 else None
    except Exception:
        return None


def get_cooldown(signal_type):
    if signal_type == "BREAKOUT": return COOLDOWN_BREAKOUT
    if signal_type == "AGGRESSIVE_FLOOD": return COOLDOWN_AGGRESSIVE
    return 1800


# =============================================================
# ОБРАБОТКА ОДНОЙ МОНЕТЫ
# =============================================================

async def process_symbol(session, symbol, semaphore, kline_cache, sent_signals):
    async with semaphore:
        try:
            loop_time = asyncio.get_event_loop().time()

            cached_kline = kline_cache.get(symbol)
            klines = None
            if cached_kline and (loop_time - cached_kline["time"] <= KLINE_CACHE_SECONDS):
                klines = cached_kline["data"]

            if klines is None:
                klines = await fetch_klines(session, symbol, "1h", 80)
                if not klines or len(klines) < 50:
                    return []
                kline_cache[symbol] = {"data": klines, "time": loop_time}

            curr_o, curr_h, curr_l, curr_c, _ = parse_kline(klines[-1])

            shelf_status = update_or_invalidate_shelf(symbol, curr_c, curr_h, curr_l)
            if shelf_status == "INVALIDATED":
                remove_shelf(symbol)
                return []

            shelf = ACTIVE_SHELVES.get(symbol)
            if not shelf:
                return []

            signals = evaluate_signals(symbol, klines, shelf)
            if not signals:
                return []

            sent = []
            for signal in signals:
                signal_type = signal["type"]
                key = (symbol, signal_type)
                last_sent = sent_signals.get(key, 0)
                
                if loop_time - last_sent < get_cooldown(signal_type):
                    continue

                orderbook = await check_orderbook_pump(session, symbol)
                if orderbook:
                    signal["score"] = min(99, signal["score"] + 5)

                message = format_signal_message(symbol, signal, orderbook)
                if await send_telegram(session, message):
                    sent_signals[key] = loop_time
                    sent.append(signal)
                    logger.info(f"🚨 Сигнал отправлен: {symbol} | {signal_type}")

                    if signal_type in ["BREAKOUT", "AGGRESSIVE_FLOOD"]:
                        logger.info(f"🚀 [ПОЛКА ОТРАБОТАЛА] {symbol} удален из памяти.")
                        remove_shelf(symbol)

            return sent

        except Exception as e:
            logger.debug(f"Ошибка {symbol}: {e}")
            return []


# =============================================================
# ГЛАВНЫЙ СКАНЕР
# =============================================================

async def scanner_loop():
    global LAST_FULL_SCAN
    logger.info("🚀 ConsolidationHunter запущен")

    kline_cache = {}
    sent_signals = {}

    connector = aiohttp.TCPConnector(limit=50, limit_per_host=30, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": "ConsolidationHunter/1.0"}) as session:
        
        # 1. ПРИНУДИТЕЛЬНЫЙ ПЕРВИЧНЫЙ СКАН ПРИ СТАРТЕ
        logger.info("🔎 [ПЕРВИЧНЫЙ СКАН] Запуск... Формируем память полок.")
        all_symbols = await fetch_futures_symbols(session)
        
        if all_symbols:
            async def init_shelves(sym):
                async with semaphore:
                    klines = await fetch_klines(session, sym, "1h", 80)
                    if klines and len(klines) >= 50:
                        shelf = find_shelf_before_breakout(klines)
                        if shelf:
                            ACTIVE_SHELVES[sym] = shelf

            tasks = [init_shelves(s) for s in all_symbols]
            await asyncio.gather(*tasks, return_exceptions=True)
            LAST_FULL_SCAN = asyncio.get_event_loop().time()
            logger.info(f"💾 [ПЕРВИЧНЫЙ СКАН ЗАВЕРШЕН] Найдено полок: {len(ACTIVE_SHELVES)}")

        # 2. ОСНОВНОЙ ЦИКЛ
        while True:
            scan_start = asyncio.get_event_loop().time()
            now_dt = datetime.datetime.now(datetime.timezone.utc)

            try:
                if now_dt.minute == 0 and (scan_start - LAST_FULL_SCAN >= 300):
                    logger.info("🔎 [ГЛОБАЛЬНЫЙ СКАН] Новый час — обновляем полки...")
                    all_symbols = await fetch_futures_symbols(session)
                    
                    async def scan_for_shelves(sym):
                        async with semaphore:
                            klines = await fetch_klines(session, sym, "1h", 80)
                            if klines and len(klines) >= 50:
                                shelf = find_shelf_before_breakout(klines)
                                if shelf:
                                    ACTIVE_SHELVES[sym] = shelf

                    tasks = [scan_for_shelves(s) for s in all_symbols]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    LAST_FULL_SCAN = scan_start
                    logger.info(f"💾 [ПАМЯТЬ ОБНОВЛЕНА] В памяти полок: {len(ACTIVE_SHELVES)}")

                target_symbols = list(ACTIVE_SHELVES.keys())

                if target_symbols:
                    tasks = [
                        process_symbol(session, symbol, semaphore, kline_cache, sent_signals)
                        for symbol in target_symbols
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)

                if len(kline_cache) > 500:
                    kline_cache.clear()

                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка scanner_loop: {e}")
                await asyncio.sleep(10)


# =============================================================
# WEB SERVER & MAIN
# =============================================================

async def handle_ping(request):
    return web.Response(text="ConsolidationHunter Active", status=200)

async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "active_shelves_count": len(ACTIVE_SHELVES),
        "shelves": list(ACTIVE_SHELVES.keys())
    })

async def main():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"🌐 Web server: 0.0.0.0:{port}")
    await scanner_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Остановлено пользователем")
