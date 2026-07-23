import asyncio
import requests
import pandas as pd
import numpy as np

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН"
TELEGRAM_CHAT_ID = "ТВОЙ_CHAT_ID"

TIMEFRAMES = ['5m', '15m', '30m'] # Таймфреймы для отслеживания
LIMIT_CANDLES = 100               # Сколько свечей загружать
SWING_WINDOW = 3                  # Чувствительность фракталов (3 свечи слева и справа)
SLIPPAGE_PCT = 0.003             # Допуск на подход к зоне (0.3%)
# =====================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки в ТГ: {e}")

def get_futures_symbols():
    """Получаем список всех бессрочных фьючерсов USDT с BingX"""
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
        res = requests.get(url, timeout=10).json()
        symbols = [item['symbol'] for item in res['data'] if item['symbol'].endswith('-USDT')]
        return symbols
    except Exception as e:
        print(f"Ошибка получения списка монет: {e}")
        return []

def get_klines(symbol, interval):
    """Загрузка свечей"""
    try:
        url = f"https://open-api.bingx.com/openApi/swap/v3/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": LIMIT_CANDLES}
        res = requests.get(url, params=params, timeout=5).json()
        
        if 'data' not in res or not res['data']:
            return None
            
        df = pd.DataFrame(res['data'])
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['open'] = df['open'].astype(float)
        return df
    except Exception:
        return None

def find_swings(df, window=SWING_WINDOW):
    """Поиск локальных пиков (Highs) и доньев (Lows)"""
    highs = []
    lows = []
    
    for i in range(window, len(df) - window):
        # Swing High
        if all(df['high'].iloc[i] > df['high'].iloc[i-j] for j in range(1, window+1)) and \
           all(df['high'].iloc[i] > df['high'].iloc[i+j] for j in range(1, window+1)):
            highs.append((i, df['high'].iloc[i]))
            
        # Swing Low
        if all(df['low'].iloc[i] < df['low'].iloc[i-j] for j in range(1, window+1)) and \
           all(df['low'].iloc[i] < df['low'].iloc[i+j] for j in range(1, window+1)):
            lows.append((i, df['low'].iloc[i]))
            
    return highs, lows

def detect_bullish_qm(df):
    """Поиск Бычьего Квазимодо (Лонг)"""
    highs, lows = find_swings(df)
    
    if len(highs) < 2 or len(lows) < 2:
        return None

    # Берем последние экстремумы
    h1_idx, h1_val = highs[-2]  # Левое плечо (High 1)
    h2_idx, h2_val = highs[-1]  # Слом структуры / MSB (High 2)
    
    l1_idx, l1_val = lows[-2]   # Low 1
    l2_idx, l2_val = lows[-1]   # Голова / Head (Low 2)

    # Правильная последовательность во времени: H1 -> L2 (Head) -> H2 (MSB)
    if not (h1_idx < l2_idx < h2_idx):
        return None

    # Условия Бычьего QM:
    # 1. Голова ниже предыдущего лоу (L2 < L1)
    # 2. Пробой левого плеча вверх (H2 > H1) - MSB / ChoCh
    if l2_val < l1_val and h2_val > h1_val:
        current_price = df['close'].iloc[-1]
        
        # Проверяем, что цена откатилась обратно в зону Левого Плеча (H1)
        target_zone_low = h1_val * (1 - SLIPPAGE_PCT)
        target_zone_high = h1_val * (1 + SLIPPAGE_PCT)
        
        if target_zone_low <= current_price <= target_zone_high or (current_price > l2_val and current_price <= h1_val):
            stop_loss = l2_val
            take_profit = h2_val
            
            risk = current_price - stop_loss
            reward = take_profit - current_price
            
            if risk > 0 and reward / risk >= 2.0: # Потенциал RR не менее 1:2
                rr_ratio = round(reward / risk, 2)
                return {
                    "type": "LONG 🟢 (Bullish QM)",
                    "entry": current_price,
                    "shoulder": h1_val,
                    "stop": stop_loss,
                    "take": take_profit,
                    "rr": rr_ratio
                }
    return None

def detect_bearish_qm(df):
    """Поиск Медвежьего Квазимодо (Шорт)"""
    highs, lows = find_swings(df)
    
    if len(highs) < 2 or len(lows) < 2:
        return None

    l1_idx, l1_val = lows[-2]   # Левое плечо (Low 1)
    l2_idx, l2_val = lows[-1]   # Слом структуры (Low 2)
    
    h1_idx, h1_val = highs[-2]  # High 1
    h2_idx, h2_val = highs[-1]  # Голова (High 2)

    if not (l1_idx < h2_idx < l2_idx):
        return None

    # Условия Медвежьего QM:
    # 1. Голова выше предыдущего хая (H2 > H1)
    # 2. Пробой левого плеча вниз (L2 < L1)
    if h2_val > h1_val and l2_val < l1_val:
        current_price = df['close'].iloc[-1]
        
        target_zone_low = l1_val * (1 - SLIPPAGE_PCT)
        target_zone_high = l1_val * (1 + SLIPPAGE_PCT)
        
        if target_zone_low <= current_price <= target_zone_high or (current_price < h2_val and current_price >= l1_val):
            stop_loss = h2_val
            take_profit = l2_val
            
            risk = stop_loss - current_price
            reward = current_price - take_profit
            
            if risk > 0 and reward / risk >= 2.0:
                rr_ratio = round(reward / risk, 2)
                return {
                    "type": "SHORT 🔴 (Bearish QM)",
                    "entry": current_price,
                    "shoulder": l1_val,
                    "stop": stop_loss,
                    "take": take_profit,
                    "rr": rr_ratio
                }
    return None

async def scan_market():
    print("🚀 Скрипт Quasimodo запущен и сканирует рынок...")
    send_telegram("🤖 <b>Бот Quasimodo (QM) запущен!</b>\nОтслеживаю таймфреймы: 5m, 15m, 30m.")
    
    # Кэш, чтобы не спамить об одной и той же монете
    sent_signals = set()

    while True:
        symbols = get_futures_symbols()
        print(f"Сканирую {len(symbols)} монет...")

        for symbol in symbols:
            clean_symbol = symbol.replace('-USDT', 'USDT')

            for tf in TIMEFRAMES:
                df = get_klines(symbol, tf)
                if df is None or len(df) < 50:
                    continue

                signal = detect_bullish_qm(df) or detect_bearish_qm(df)
                
                if signal:
                    signal_key = f"{clean_symbol}_{tf}_{signal['type']}"
                    
                    if signal_key not in sent_signals:
                        msg = (
                            f"🎯 <b>ПАТТЕРН КВАЗИМОДО FOUND!</b>\n\n"
                            f"📌 <b>Монета:</b> #{clean_symbol}\n"
                            f"⏱ <b>Таймфрейм:</b> {tf}\n"
                            f"Сигнал: <b>{signal['type']}</b>\n\n"
                            f"📥 <b>Вход (Тест плеча):</b> <code>{signal['entry']:.6f}</code>\n"
                            f"🛡 <b>Стоп-лосс (Голова):</b> <code>{signal['stop']:.6f}</code>\n"
                            f"🎯 <b>Тейк-профит (Перехай):</b> <code>{signal['take']:.6f}</code>\n"
                            f"⚖️ <b>Соотношение R:R:</b> 1:{signal['rr']}"
                        )
                        send_telegram(msg)
                        sent_signals.add(signal_key)

            await asyncio.sleep(0.1) # Задержка от бана API
            
        # Очищаем кэш сигналов каждый час
        if len(sent_signals) > 100:
            sent_signals.clear()

        await asyncio.sleep(30) # Пауза между кругами

if __name__ == "__main__":
    asyncio.run(scan_market())
