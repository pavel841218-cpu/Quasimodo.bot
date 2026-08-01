import asyncio
import aiohttp
import logging
import os
import time
import threading
from bottle import Bottle, run
import pandas as pd
import numpy as np

# ==================== МАСКИРОВОЧНЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
web_app = Bottle()

@web_app.route('/')
def home():
    return "Bot is running 24/7!"

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    run(web_app, host='0.0.0.0', port=port, quiet=True)

# Запускаем веб-сервер в фоновом потоке
threading.Thread(target=run_web_server, daemon=True).start()

# ==================== НАСТРОЙКИ (ENV RENDER) ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAMES = ['5m', '15m', '30m']
LIMIT_CANDLES = 80
SWING_WINDOW = 3
SLIPPAGE_PCT = 0.003
MIN_RR_RATIO = 2.0
MIN_PROFIT_PCT = 1.2  # Минимальный профит движения в % (отсеиваем микро-сетапы)

SIGNAL_COOLDOWN_HOURS = 4  # Защита от спама на Х часов
MAX_SIGNALS_PER_SCAN = 5

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class SignalTracker:
    def __init__(self, cooldown_hours=4):
        self.signals = {}
        self.cooldown = cooldown_hours * 3600
    
    def can_send(self, key):
        current_time = time.time()
        if key in self.signals:
            if current_time - self.signals[key] < self.cooldown:
                return False
        return True
    
    def mark_sent(self, key):
        self.signals[key] = time.time()
    
    def cleanup(self):
        current_time = time.time()
        expired = [k for k, t in self.signals.items() if current_time - t > self.cooldown * 2]
        for k in expired:
            del self.signals[k]

async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")
        return False

async def get_futures_symbols(session):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0 and "data" in data:
                    return [item['symbol'] for item in data['data'] if item.get('symbol', '').endswith('-USDT') and item.get('status') == 1]
    except Exception as e:
        logger.error(f"Ошибка получения списка монет: {e}")
    return []

async def get_klines(session, symbol, interval):
    url = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": LIMIT_CANDLES}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("code") == 0 and "data" in res and len(res["data"]) >= 40:
                    df = pd.DataFrame(res['data'])
                    df['high'] = df['high'].astype(float)
                    df['low'] = df['low'].astype(float)
                    df['close'] = df['close'].astype(float)
                    df['open'] = df['open'].astype(float)
                    return df
    except Exception:
        pass
    return None

def find_swings(df, window=SWING_WINDOW):
    highs, lows = [], []
    for i in range(window, len(df) - window):
        current_high, current_low = df['high'].iloc[i], df['low'].iloc[i]
        if all(current_high > df['high'].iloc[i - j] for j in range(1, window + 1)) and \
           all(current_high > df['high'].iloc[i + j] for j in range(1, window + 1)):
            if not highs or (i - highs[-1][0] > 3):
                highs.append((i, current_high))
        if all(current_low < df['low'].iloc[i - j] for j in range(1, window + 1)) and \
           all(current_low < df['low'].iloc[i + j] for j in range(1, window + 1)):
            if not lows or (i - lows[-1][0] > 3):
                lows.append((i, current_low))
    return highs, lows

def detect_bullish_qm(df):
    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2: return None
    h1_idx, h1_val = highs[-2]
    h2_idx, h2_val = highs[-1]
    l1_idx, l1_val = lows[-2]
    l2_idx, l2_val = lows[-1]

    if not (h1_idx < l2_idx < h2_idx) or not (l2_val < l1_val) or not (h2_val > h1_val): return None
    if h2_idx - l2_idx > 20: return None

    current_price = df['close'].iloc[-1]
    target_zone_low, target_zone_high = h1_val * (1 - SLIPPAGE_PCT), h1_val * (1 + SLIPPAGE_PCT)
    if not ((target_zone_low <= current_price <= target_zone_high) or (l2_val < current_price <= h1_val)): return None

    stop_loss = l2_val * 0.998
    take_profit = h2_val * 1.002
    risk, reward = current_price - stop_loss, take_profit - current_price
    if risk <= 0: return None

    rr_ratio = round(reward / risk, 2)
    if rr_ratio < MIN_RR_RATIO: return None

    profit_pct = round((reward / current_price) * 100, 2)
    if profit_pct < MIN_PROFIT_PCT: return None  # Отсекаем мелкие профиты

    return {
        "type": "LONG 🟢", "pattern": "Bullish Quasimodo", "entry": current_price,
        "shoulder": h1_val, "stop": stop_loss, "take": take_profit, "rr": rr_ratio,
        "risk_pct": round((risk / current_price) * 100, 2), "profit_pct": profit_pct
    }

def detect_bearish_qm(df):
    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2: return None
    l1_idx, l1_val = lows[-2]
    l2_idx, l2_val = lows[-1]
    h1_idx, h1_val = highs[-2]
    h2_idx, h2_val = highs[-1]

    if not (l1_idx < h2_idx < l2_idx) or not (h2_val > h1_val) or not (l2_val < l1_val): return None
    if l2_idx - h2_idx > 20: return None

    current_price = df['close'].iloc[-1]
    target_zone_low, target_zone_high = l1_val * (1 - SLIPPAGE_PCT), l1_val * (1 + SLIPPAGE_PCT)
    if not ((target_zone_low <= current_price <= target_zone_high) or (l1_val <= current_price < h2_val)): return None

    stop_loss = h2_val * 1.002
    take_profit = l2_val * 0.998
    risk, reward = stop_loss - current_price, current_price - take_profit
    if risk <= 0: return None

    rr_ratio = round(reward / risk, 2)
    if rr_ratio < MIN_RR_RATIO: return None

    profit_pct = round((reward / current_price) * 100, 2)
    if profit_pct < MIN_PROFIT_PCT: return None  # Отсекаем мелкие профиты

    return {
        "type": "SHORT 🔴", "pattern": "Bearish Quasimodo", "entry": current_price,
        "shoulder": l1_val, "stop": stop_loss, "take": take_profit, "rr": rr_ratio,
        "risk_pct": round((risk / current_price) * 100, 2), "profit_pct": profit_pct
    }

def format_price(price):
    if price >= 100: return f"{price:.2f}"
    elif price >= 1: return f"{price:.4f}"
    elif price >= 0.001: return f"{price:.6f}"
    else: return f"{price:.8f}"

async def scan_market():
    logger.info("🚀 Сканер Квазимодо запущен")
    connector = aiohttp.TCPConnector(limit=10)
    tracker = SignalTracker(cooldown_hours=SIGNAL_COOLDOWN_HOURS)

    async with aiohttp.ClientSession(connector=connector) as session:
        await send_telegram(
            session,
            "🤖 <b>Бот Quasimodo запущен на Render!</b>\n"
            f"📊 TF: {', '.join(TIMEFRAMES)} | RR >= 1:{MIN_RR_RATIO} | Min Profit >= {MIN_PROFIT_PCT}%"
        )

        scan_counter = 0
        while True:
            try:
                scan_counter += 1
                if scan_counter % 20 == 0: tracker.cleanup()

                symbols = await get_futures_symbols(session)
                if not symbols:
                    await asyncio.sleep(30)
                    continue

                signals_found = 0
                for symbol in symbols:
                    clean_symbol = symbol.replace('-USDT', 'USDT')

                    for tf in TIMEFRAMES:
                        df = await get_klines(session, symbol, tf)
                        if df is None: continue

                        signal = detect_bullish_qm(df) or detect_bearish_qm(df)
                        if signal:
                            # Уникальный ключ для блокировки спама (Монета + TF + Направление)
                            signal_key = f"{clean_symbol}_{tf}_{signal['type']}"
                            if not tracker.can_send(signal_key): continue

                            signals_found += 1
                            if signals_found > MAX_SIGNALS_PER_SCAN: break

                            msg = (
                                f"🎯 <b>ПАТТЕРН КВАЗИМОДО!</b>\n\n"
                                f"📌 <b>Монета:</b> <code>{clean_symbol}</code>\n"
                                f"⏱ <b>Таймфрейм:</b> {tf}\n"
                                f"📊 <b>Тип:</b> {signal['type']}\n\n"
                                f"📥 <b>Вход:</b> <code>{format_price(signal['entry'])}</code>\n"
                                f"🎯 <b>Тейк:</b> <code>{format_price(signal['take'])}</code> ({signal['profit_pct']}%)\n"
                                f"🛑 <b>Стоп:</b> <code>{format_price(signal['stop'])}</code> ({signal['risk_pct']}%)\n"
                                f"⚖️ <b>R:R:</b> 1:{signal['rr']}"
                            )

                            if await send_telegram(session, msg):
                                tracker.mark_sent(signal_key)

                    await asyncio.sleep(0.05)
                    if signals_found > MAX_SIGNALS_PER_SCAN: break

                await asyncio.sleep(30)

            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"Ошибка цикла: {e}")
                await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(scan_market())
