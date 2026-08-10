import os
import time
import asyncio
import logging
from datetime import datetime
import aiohttp
from flask import Flask
import threading

# ==================== CONFIGURATION ====================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

PORT = int(os.environ.get("PORT", 10000))

# Parameters for shelf finding
BASE_MIN_HOURS = 4
BASE_MAX_HOURS = 24
EXTENDED_BASE_MAX_HOURS = 48

BASE_MAX_WIDTH_PCT = 5.0
EXTENDED_BASE_MAX_WIDTH_PCT = 7.0

# Whale Window parameters
WHALE_WINDOW_START_MIN = 49
WHALE_WINDOW_END_MIN = 56
MIN_RVOL_WHALE = 2.5

# Memory store for active shelves
ACTIVE_SHELVES = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ==================== FLASK KEEP-ALIVE SERVER ====================

app = Flask(__name__)

@app.route('/')
def home():
    return f"ConsolidationHunter is Running. Active Shelves: {len(ACTIVE_SHELVES)}", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

# ==================== HELPER FUNCTIONS ====================

def parse_kline(k):
    """Универсальный разбор свечи BingX (dict или list)."""
    try:
        if isinstance(k, dict):
            o = float(k.get("open", 0))
            h = float(k.get("high", 0))
            l = float(k.get("low", 0))
            c = float(k.get("close", 0))
            v = float(k.get("volume", 0))
            return o, h, l, c, v

        if isinstance(k, (list, tuple)):
            o = float(k[1])
            h = float(k[2])
            l = float(k[3])
            c = float(k[4])
            v = float(k[5])
            return o, h, l, c, v
    except (IndexError, KeyError, TypeError, ValueError):
        pass

    return 0.0, 0.0, 0.0, 0.0, 0.0


def get_effective_candle_bounds(o, h, l, c):
    """Cuts extreme wicks slightly to prevent single spike from breaking tight shelf."""
    body_high = max(o, c)
    body_low = min(o, c)
    eff_high = h - (h - body_high) * 0.3
    eff_low = l + (body_low - l) * 0.3
    return eff_high, eff_low


def detect_u_w_dip(candles):
    """Detects if there was a shakeout/dip in the middle of consolidation."""
    if len(candles) < 6:
        return False, 0.0

    lows = []
    for k in candles:
        _, _, low, _, _ = parse_kline(k)
        if low > 0:
            lows.append(low)

    if not lows:
        return False, 0.0

    avg_low = sum(lows) / len(lows)
    min_low = min(lows)

    if min_low < avg_low * 0.96:
        dip_pct = ((avg_low - min_low) / avg_low) * 100
        return True, round(dip_pct, 2)

    return False, 0.0


def find_shelf_before_breakout(klines_1h):
    """Searches for tight horizontal consolidation (shelf) in recent closed candles."""
    if len(klines_1h) < BASE_MIN_HOURS + 1:
        return None

    closed_candles = klines_1h[:-1]
    if len(closed_candles) < BASE_MIN_HOURS:
        return None

    for hours in range(min(len(closed_candles), EXTENDED_BASE_MAX_HOURS), BASE_MIN_HOURS - 1, -1):
        sub_candles = closed_candles[-hours:]
        sub_highs = []
        sub_lows = []

        for k in sub_candles:
            o, h, l, c, _ = parse_kline(k)
            if l <= 0 or c <= 0:
                continue
            eff_h, eff_l = get_effective_candle_bounds(o, h, l, c)
            sub_highs.append(eff_h)
            sub_lows.append(eff_l)

        if not sub_highs or not sub_lows:
            continue

        shelf_high = max(sub_highs)
        shelf_low = min(sub_lows)

        if shelf_low <= 0:
            continue

        width = ((shelf_high - shelf_low) / shelf_low) * 100
        has_dip, dip_depth = detect_u_w_dip(sub_candles)

        if has_dip:
            max_allowed_width = EXTENDED_BASE_MAX_WIDTH_PCT
        elif hours <= BASE_MAX_HOURS:
            max_allowed_width = BASE_MAX_WIDTH_PCT
        else:
            max_allowed_width = EXTENDED_BASE_MAX_WIDTH_PCT

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


# ==================== API & TG COMMUNICATION ====================

async def send_telegram_alert(session, text):
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or CHAT_ID == "YOUR_TELEGRAM_CHAT_ID":
        logging.info(f"[TG MOCK ALERT]:\n{text}")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status != 200:
                logging.error(f"Failed to send TG message: {await resp.text()}")
    except Exception as e:
        logging.error(f"Error sending Telegram alert: {e}")


async def get_active_usdt_pairs(session):
    """
    Получает ТОЛЬКО реально торгуемые USDT perpetual пары через ticker.
    Отсекает фьючерсные индексы (NCS*) на этапе формирования списка.
    """
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"

    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                logging.warning(f"❌ BingX ticker HTTP {resp.status}")
                return []

            data = await resp.json()

            if data.get("code") != 0:
                logging.warning(f"❌ BingX ticker code={data.get('code')} msg={data.get('msg')}")
                return []

            tickers = data.get("data", [])
            active_pairs = []

            for item in tickers:
                symbol = str(item.get("symbol", "")).strip().upper()

                # Отсекаем нефьючерсные индексы и не-USDT пары
                if not symbol.endswith("-USDT") or symbol.startswith("NCS"):
                    continue

                try:
                    last_price = float(item.get("lastPrice", 0))
                except (TypeError, ValueError):
                    last_price = 0

                try:
                    quote_volume = float(item.get("quoteVolume", 0))
                except (TypeError, ValueError):
                    quote_volume = 0

                if last_price <= 0:
                    continue

                if quote_volume <= 0:
                    continue

                active_pairs.append(symbol)

            active_pairs = sorted(set(active_pairs))

            logging.info(f"✅ BingX: найдено активных USDT-Futures пар: {len(active_pairs)}")

            return active_pairs

    except Exception as e:
        logging.error(f"❌ Ошибка получения активных пар BingX: {e}")

    return []


async def get_klines_1h(session, symbol, limit=60):
    """
    Получение 1H свечей BingX.
    При неактивной/приостановленной монете просто пропускаем её.
    """
    url = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"

    params = {
        "symbol": symbol,
        "interval": "1h",
        "limit": limit
    }

    try:
        async with session.get(url, params=params, timeout=8) as resp:
            if resp.status != 200:
                return []

            data = await resp.json()

            code = data.get("code")

            # 104915 = символ приостановлен/недоступен
            if code == 104915:
                return []

            if code != 0:
                return []

            candles = data.get("data", [])

            if not isinstance(candles, list):
                return []

            if len(candles) < 5:
                return []

            try:
                candles = sorted(candles, key=lambda x: int(x[0]))
            except (IndexError, TypeError, ValueError):
                return []

            return candles

    except asyncio.TimeoutError:
        return []

    except Exception:
        return []


# ==================== CORE BOT LOGIC ====================

async def refresh_shelves_memory(session):
    """Scans market to rebuild active shelves database"""
    logging.info("🔎 [ГЛОБАЛЬНЫЙ СКАН] Обновление базы полок...")
    pairs = await get_active_usdt_pairs(session)
    logging.info(f"Получено пар для скана: {len(pairs)}")

    new_shelves = {}
    for symbol in pairs:
        klines = await get_klines_1h(session, symbol, limit=50)
        if not klines:
            continue

        shelf = find_shelf_before_breakout(klines)
        if shelf:
            new_shelves[symbol] = shelf

        await asyncio.sleep(0.05)

    global ACTIVE_SHELVES
    ACTIVE_SHELVES = new_shelves
    logging.info(f"💾 [ПЕРВИЧНЫЙ СКАН ЗАВЕРШЕН] Найдено полок: {len(ACTIVE_SHELVES)}")


async def check_whale_window_breakouts(session):
    """Мониторинг пробоя уже найденных полок."""
    if not ACTIVE_SHELVES:
        return

    logging.info(f"🐳 [WHALE WINDOW CHECK] Мониторинг {len(ACTIVE_SHELVES)} полок...")

    for symbol, shelf in list(ACTIVE_SHELVES.items()):
        try:
            klines = await get_klines_1h(session, symbol, limit=25)

            if not klines or len(klines) < 20:
                continue

            curr_candle = klines[-1]
            curr_o, curr_h, curr_l, curr_c, curr_v = parse_kline(curr_candle)

            if curr_c <= 0 or curr_v <= 0:
                continue

            hist_candles = klines[-21:-1]
            hist_vols = []

            for k in hist_candles:
                _, _, _, _, volume = parse_kline(k)
                if volume > 0:
                    hist_vols.append(volume)

            if len(hist_vols) < 5:
                continue

            avg_v = sum(hist_vols) / len(hist_vols)
            if avg_v <= 0:
                continue

            rvol = curr_v / avg_v

            shelf_high = float(shelf["high"])
            shelf_low = float(shelf["low"])

            if shelf_high <= 0 or shelf_low <= 0:
                continue

            is_pressing_high = curr_h >= shelf_high * 0.995
            is_volume_spike = rvol >= MIN_RVOL_WHALE

            logging.info(
                f"🔍 {symbol} | "
                f"H={curr_h:.8g} | "
                f"C={curr_c:.8g} | "
                f"RVOL={rvol:.2f}x | "
                f"ShelfHigh={shelf_high:.8g}"
            )

            if is_pressing_high and is_volume_spike:
                clean_symbol = symbol.replace("-", "")

                dip_text = (
                    f"ДА ({shelf['dip_depth']}%)"
                    if shelf["has_dip"]
                    else "НЕТ"
                )

                msg = (
                    f"🚀 <b>[WHALE BREAKOUT ALERT] {clean_symbol}</b>\n\n"
                    f"🔹 <b>Ширина полки:</b> {shelf['width']}%\n"
                    f"🔹 <b>Длительность полки:</b> {shelf['hours']}ч\n"
                    f"🔹 <b>RVOL:</b> {round(rvol, 2)}x\n"
                    f"🔹 <b>Цена:</b> {curr_c}\n"
                    f"🔹 <b>HIGH свечи:</b> {curr_h}\n"
                    f"🔹 <b>Верх полки:</b> {shelf_high}\n"
                    f"🔹 <b>U/W яма:</b> {dip_text}\n\n"
                    f"🐳 <i>Цена поджимает верх полки + всплеск объёма!</i>"
                )

                await send_telegram_alert(session, msg)
                logging.info(f"🚀 СИГНАЛ ОТПРАВЛЕН: {clean_symbol} | RVOL={rvol:.2f}x")

                ACTIVE_SHELVES.pop(symbol, None)

            await asyncio.sleep(0.05)

        except Exception as e:
            logging.error(f"Ошибка проверки {symbol}: {e}")


async def main_loop():
    async with aiohttp.ClientSession() as session:
        await refresh_shelves_memory(session)

        last_scanned_hour = -1

        while True:
            now = datetime.utcnow()
            curr_hour = now.hour
            curr_min = now.minute

            if curr_min == 0 and curr_hour != last_scanned_hour:
                await refresh_shelves_memory(session)
                last_scanned_hour = curr_hour

            if WHALE_WINDOW_START_MIN <= curr_min <= WHALE_WINDOW_END_MIN:
                await check_whale_window_breakouts(session)
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(30)


if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    asyncio.run(main_loop())
