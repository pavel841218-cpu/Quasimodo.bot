import os
import datetime
import logging
import asyncio
import aiohttp
import numpy as np
from aiohttp import web

# -------------------------------------------------------------
# НАСТРОЙКИ
# -------------------------------------------------------------
BINGX_BASE_URL = "https://open-api.bingx.com"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_24H_VOLUME_USDT = 2_000_000
EXCLUDED_SYMBOLS = {"USDC", "FDUSD"}

BASE_MIN_HOURS = 6
BASE_MAX_HOURS = 24
BASE_MAX_WIDTH_PCT = 4.5
BASE_MAX_BODY_PCT = 1.5
BASE_MAX_RANGE_PCT = 2.5

EMA_FAST = 20
EMA_SLOW = 40

COOLDOWN_EARLY = 1800
COOLDOWN_STRONG = 2400
COOLDOWN_CONFIRMED = 3600

MAX_CONCURRENT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# -------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -------------------------------------------------------------
def parse_kline(kline):
    return float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4]), float(kline[5])


def calculate_ema(prices, period):
    if not prices or len(prices) == 0:
        return 0.0
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


def find_last_accumulation_before_breakout(klines_1h):
    if len(klines_1h) < BASE_MIN_HOURS + 3:
        return False, 0.0, 0.0, 0.0, 0, [], -1
    
    highs = [parse_kline(k)[1] for k in klines_1h]
    lows = [parse_kline(k)[2] for k in klines_1h]
    opens = [parse_kline(k)[0] for k in klines_1h]
    closes = [parse_kline(k)[3] for k in klines_1h]
    
    search_start = len(klines_1h) - 2
    
    for end in range(search_start, BASE_MIN_HOURS, -1):
        for h in range(BASE_MAX_HOURS, BASE_MIN_HOURS - 1, -1):
            start = end - h
            if start < 0:
                continue
            
            sub_highs = highs[start:end]
            sub_lows = lows[start:end]
            sub_opens = opens[start:end]
            sub_closes = closes[start:end]
            
            max_h = max(sub_highs)
            min_l = min(sub_lows)
            
            if min_l <= 0:
                continue
            
            width = ((max_h - min_l) / min_l) * 100
            
            if width <= BASE_MAX_WIDTH_PCT:
                bodies = [abs(c - o) / o * 100 for o, c in zip(sub_opens, sub_closes) if o > 0]
                ranges_list = [(h - l) / l * 100 for h, l in zip(sub_highs, sub_lows) if l > 0]
                
                if not bodies or not ranges_list:
                    continue
                
                avg_body = np.mean(bodies)
                avg_range = np.mean(ranges_list)
                
                if avg_body <= BASE_MAX_BODY_PCT and avg_range <= BASE_MAX_RANGE_PCT:
                    post_base_closes = closes[end:]
                    
                    if len(post_base_closes) >= 1:
                        last_close = post_base_closes[-1]
                        
                        breakout_now = last_close > max_h * 1.003
                        sustained = all(c > max_h * 0.99 for c in post_base_closes[-3:]) if len(post_base_closes) >= 3 else breakout_now
                        
                        if breakout_now and sustained:
                            return True, round(width, 2), max_h, min_l, h, klines_1h[start:end], start
    
    return False, 0.0, 0.0, 0.0, 0, [], -1


def check_ema_setup(closes, range_high, range_low):
    if len(closes) < EMA_SLOW:
        return None, 0, ""
    
    ema20 = calculate_ema(closes, EMA_FAST)
    ema40 = calculate_ema(closes, EMA_SLOW)
    last_close = closes[-1]
    
    if range_low <= ema20 <= range_high:
        return "EMA20_IN_BASE", 20, "EMA20 внутри базы накопления"
    if range_low <= ema40 <= range_high:
        return "EMA40_IN_BASE", 18, "EMA40 внутри базы накопления"
    if last_close > ema20 and ema20 > ema40:
        return "EMA_BULLISH", 15, "EMA20 > EMA40 бычья структура"
    if last_close > ema20:
        return "ABOVE_EMA20", 10, "Цена выше EMA20"
    
    return None, 0, ""


def check_price_compression(base_candles, base_high, base_low):
    if len(base_candles) < 4:
        return False, 0.0
    
    closes = [parse_kline(k)[3] for k in base_candles]
    
    if base_high <= base_low:
        return False, 0.0
    
    last_closes = closes[-4:]
    positions = [(c - base_low) / (base_high - base_low) * 100 for c in last_closes]
    avg_position = np.mean(positions)
    trend_up = positions[-1] > positions[0]
    is_compressed = avg_position > 55 and trend_up
    
    return is_compressed, round(avg_position, 1)


def check_holding_above_base(klines_1h, base_high, base_end_idx):
    if base_end_idx >= len(klines_1h) - 1:
        return False, 0, 0
    
    post_base = klines_1h[base_end_idx:]
    closes_after = [parse_kline(k)[3] for k in post_base]
    
    if not closes_after:
        return False, 0, 0
    
    held_closed = 0
    held_all = 0
    
    for i, c in enumerate(closes_after):
        if c > base_high * 0.995:
            held_all += 1
            if i < len(closes_after) - 1:
                held_closed += 1
        else:
            break
    
    is_holding = held_all >= 1
    return is_holding, held_closed, held_all


# -------------------------------------------------------------
# ЗАЩИТА ОТ СПУФИНГА (ORDERBOOK SPOOFING CHECK)
# -------------------------------------------------------------
async def check_orderbook_spoofing(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/depth"
    params = {"symbol": symbol, "limit": 20}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("code") == 0 and "data" in res:
                    bids = res["data"].get("bids", [])
                    asks = res["data"].get("asks", [])
                    
                    if not bids or not asks:
                        return True
                    
                    total_bid_usdt = sum(float(b[0]) * float(b[1]) for b in bids)
                    total_ask_usdt = sum(float(a[0]) * float(a[1]) for a in asks)
                    
                    if total_ask_usdt <= 0:
                        return True

                    ratio = total_bid_usdt / total_ask_usdt
                    if ratio > 6.0:
                        logging.warning(f"Спуфинг [{symbol}]: перекос Bids/Asks (Ratio: {ratio:.1f})")
                        return False

                    max_single_bid = max(float(b[0]) * float(b[1]) for b in bids)
                    if max_single_bid > total_bid_usdt * 0.60 and total_bid_usdt > 100_000:
                        logging.warning(f"Спуфинг [{symbol}]: стенка ({max_single_bid:.0f} USDT)")
                        return False

    except Exception as e:
        logging.debug(f"Ошибка проверки стакана {symbol}: {e}")
        return True
        
    return True


# -------------------------------------------------------------
# ОСНОВНОЙ АЛГОРИТМ v2.1
# -------------------------------------------------------------
def evaluate_accumulation_expansion(symbol, klines_1h, volume_24h_usdt=0.0):
    base_symbol = get_base_symbol(symbol)
    if base_symbol in EXCLUDED_SYMBOLS:
        return None
    
    if volume_24h_usdt < MIN_24H_VOLUME_USDT:
        return None
    
    if len(klines_1h) < BASE_MIN_HOURS + 3:
        return None
    
    curr = klines_1h[-1]
    curr_open, curr_high, curr_low, curr_close, curr_vol = parse_kline(curr)
    
    if curr_open <= 0 or curr_low <= 0:
        return None
    
    curr_change = ((curr_close - curr_open) / curr_open) * 100
    candle_range = curr_high - curr_low
    
    close_position = (curr_close - curr_low) / candle_range if candle_range > 0 else 0.5
    upper_wick = curr_high - max(curr_close, curr_open)
    upper_wick_ratio = upper_wick / candle_range if candle_range > 0 else 0
    
    wick_penalty = 0
    if upper_wick_ratio > 0.50:
        wick_penalty = 3
    elif upper_wick_ratio > 0.35:
        wick_penalty = 2
    elif upper_wick_ratio > 0.25:
        wick_penalty = 1
    
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    elapsed_minutes = now_dt.minute + now_dt.second / 60
    if elapsed_minutes < 1:
        elapsed_minutes = 1
    
    curr_volume_usdt = curr_vol * curr_close
    projected_volume = curr_volume_usdt * (60 / elapsed_minutes)
    
    past_volumes = [parse_kline(k)[4] * parse_kline(k)[3] for k in klines_1h[-13:-1]]
    avg_past_volume = np.mean(past_volumes) if past_volumes else 1.0
    raw_rvol = projected_volume / avg_past_volume if avg_past_volume > 0 else 1.0
    
    if elapsed_minutes < 3:
        projected_rvol = min(raw_rvol, 5.0)
    elif elapsed_minutes < 5:
        projected_rvol = min(raw_rvol, 7.0)
    else:
        projected_rvol = raw_rvol
    
    is_flat, base_width, base_high, base_low, base_hours, base_candles, base_start = \
        find_last_accumulation_before_breakout(klines_1h)
    
    if not is_flat:
        return None
    
    all_closes = [parse_kline(k)[3] for k in klines_1h]
    ema_type, ema_score, ema_detail = check_ema_setup(all_closes, base_high, base_low)
    
    is_compressed, compression_pct = check_price_compression(base_candles, base_high, base_low)
    
    base_end_idx = base_start + base_hours
    is_holding, held_closed, held_all = check_holding_above_base(klines_1h, base_high, base_end_idx)
    
    breakout_above_base = curr_close > base_high
    breakout_pct = ((curr_close - base_high) / base_high) * 100 if base_high > 0 else 0
    distance_from_base = breakout_pct
    returning_to_base = curr_close < base_high * 0.998 and curr_change < 0.5
    
    strong_close = close_position >= 0.55
    very_strong_close = close_position >= 0.70
    powerful_candle = (
        curr_change >= 3.0 and breakout_pct >= 1.0 and 
        projected_rvol >= 1.8 and close_position >= 0.55
    )
    
    if very_strong_close and powerful_candle:
        wick_penalty = 0
    elif strong_close and curr_change >= 2.0:
        wick_penalty = max(0, wick_penalty - 2)
    elif strong_close:
        wick_penalty = max(0, wick_penalty - 1)
    
    if not breakout_above_base or returning_to_base:
        return None
    
    # 1. CONFIRMED
    confirmed_conditions = [
        curr_change >= 1.5,
        breakout_pct >= 0.5,
        projected_rvol >= 1.8,
        base_hours >= BASE_MIN_HOURS,
        close_position >= 0.55,
    ]
    confirmed_score = sum(confirmed_conditions)
    
    bonus = 0
    if ema_type:
        bonus += 1
    if is_compressed:
        bonus += 2
    if held_closed >= 1:
        bonus += 2
    elif is_holding:
        bonus += 1
    if curr_change >= 3.0:
        bonus += 2
    if projected_rvol >= 2.5:
        bonus += 1
    if distance_from_base >= 5.0:
        bonus += 2
    
    total_confirmed = confirmed_score + bonus - wick_penalty
    
    if total_confirmed >= 7:
        if curr_change >= 15.0 and distance_from_base >= 10.0:
            final_score = 98
            signal_name = "ВЗРЫВНОЙ ПРОБОЙ БАЗЫ"
        elif curr_change >= 8.0:
            final_score = 93
            signal_name = "МОЩНЫЙ ПРОБОЙ БАЗЫ"
        elif curr_change >= 3.0:
            final_score = 87
            signal_name = "ПРОБОЙ БАЗЫ"
        else:
            final_score = 80
            signal_name = "ПОДТВЕРЖДЕНИЕ ИМПУЛЬСА"
        
        return _build_signal("CONFIRMED", signal_name, final_score, curr_close,
                           curr_change, projected_rvol, base_hours, base_width,
                           base_high, base_low, breakout_pct, distance_from_base,
                           int(elapsed_minutes), ema_type, is_compressed,
                           compression_pct, is_holding, held_closed, held_all,
                           close_position, upper_wick_ratio, ema_detail)
    
    # 2. STRONG EARLY
    strong_conditions = [
        curr_change >= 0.8,
        breakout_pct >= 0.3,
        projected_rvol >= 2.0,
        base_hours >= BASE_MIN_HOURS,
        close_position >= 0.60,
    ]
    strong_score = sum(strong_conditions)
    
    bonus = 0
    if ema_type:
        bonus += 1
    if is_compressed:
        bonus += 2
    if is_holding:
        bonus += 1
    
    total_strong = strong_score + bonus - wick_penalty
    
    if total_strong >= 6:
        return _build_signal("STRONG_EARLY", "УСИЛЕННЫЙ СИГНАЛ",
                           min(92, 60 + total_strong * 6), curr_close,
                           curr_change, projected_rvol, base_hours, base_width,
                           base_high, base_low, breakout_pct, distance_from_base,
                           int(elapsed_minutes), ema_type, is_compressed,
                           compression_pct, is_holding, held_closed, held_all,
                           close_position, upper_wick_ratio, ema_detail)
    
    # 3. EARLY
    early_conditions = [
        curr_change >= 0.5,
        breakout_pct >= 0.15,
        base_hours >= BASE_MIN_HOURS,
        base_width <= BASE_MAX_WIDTH_PCT,
        close_position >= 0.50,
        projected_rvol >= 1.5,
    ]
    early_score = sum(early_conditions)
    
    bonus = 0
    if ema_type:
        bonus += 1
    if is_compressed:
        bonus += 1
    if is_holding:
        bonus += 1
    
    total_early = early_score + bonus - wick_penalty
    
    if total_early >= 5:
        return _build_signal("EARLY", "НАЧАЛО ВЫХОДА ИЗ НАКОПЛЕНИЯ",
                           min(88, 50 + total_early * 7), curr_close,
                           curr_change, projected_rvol, base_hours, base_width,
                           base_high, base_low, breakout_pct, distance_from_base,
                           int(elapsed_minutes), ema_type, is_compressed,
                           compression_pct, is_holding, held_closed, held_all,
                           close_position, upper_wick_ratio, ema_detail)
    
    return None


def _build_signal(signal_type, signal_name, score, price, change_pct, rvol,
                  base_hours, base_width, base_high, base_low, breakout_pct,
                  distance_from_base, elapsed, ema_type, is_compressed,
                  compression_pct, is_holding, held_closed, held_all,
                  close_position, wick_ratio, ema_detail):
    reasons = [
        f"База: {base_hours}ч (ширина {base_width}%)",
        f"Верх базы: {base_high:.6f}",
        f"Выход: +{breakout_pct:.2f}% над базой",
        f"Proj. RVOL: x{rvol:.1f}",
    ]
    if change_pct >= 1.5:
        reasons.append(f"Рост H1: +{change_pct:.1f}%")
    if ema_detail:
        reasons.append(ema_detail)
    if is_compressed:
        reasons.append(f"Поджатие: {compression_pct:.0f}%")
    if held_closed >= 1:
        reasons.append(f"Удержание: {held_closed} закр. свеч над базой")
    elif is_holding:
        reasons.append(f"Удержание: {held_all} свеч над базой")
    reasons.append(f"Закрытие: {close_position*100:.0f}%")
    
    return {
        "type": signal_type,
        "signal_name": signal_name,
        "score": score,
        "price": price,
        "change_pct": round(change_pct, 2),
        "rvol": round(rvol, 1),
        "base_hours": base_hours,
        "base_width": base_width,
        "base_high": base_high,
        "base_low": base_low,
        "breakout_pct": round(breakout_pct, 2),
        "distance_from_base": round(distance_from_base, 2),
        "elapsed": elapsed,
        "ema_type": ema_type,
        "is_compressed": is_compressed,
        "compression_pct": compression_pct,
        "is_holding": is_holding,
        "held_closed": held_closed,
        "held_all": held_all,
        "close_position": round(close_position * 100, 0),
        "wick_ratio": round(wick_ratio * 100, 0),
        "reasons": reasons,
    }


# -------------------------------------------------------------
# ФОРМАТИРОВАНИЕ И ОТПРАВКА
# -------------------------------------------------------------
def format_signal_message(symbol, signal):
    clean = get_base_symbol(symbol)
    price_str = f"{signal['price']:.6f}".rstrip('0').rstrip('.')
    
    msg = f"<b>{signal['signal_name']}</b>\n\n"
    msg += f"📊 <b>{clean} / USDT</b>\n\n"
    msg += f"💰 Цена: <code>{price_str}</code>\n"
    msg += f"📈 Изменение H1: <b>+{signal['change_pct']}%</b>\n"
    msg += f"📊 Proj. RVOL: <b>x{signal['rvol']}</b>\n"
    msg += f"⏰ {signal['elapsed']}-я минута часа\n\n"
    
    msg += "📦 <b>База накопления:</b>\n"
    msg += f"• Длительность: <b>{signal['base_hours']}ч</b>\n"
    msg += f"• Ширина: <b>{signal['base_width']}%</b>\n"
    msg += f"• Верх: <code>{signal['base_high']:.6f}</code>\n"
    msg += f"• Низ: <code>{signal['base_low']:.6f}</code>\n\n"
    
    msg += "🚀 <b>Выход из базы:</b>\n"
    msg += f"• Над базой: <b>+{signal['breakout_pct']}%</b>\n"
    
    if signal.get('distance_from_base', 0) > 5:
        msg += f"• Дистанция: <b>+{signal['distance_from_base']}%</b>\n"
    
    cp = signal.get('close_position', 0)
    wr = signal.get('wick_ratio', 0)
    
    if cp >= 70:
        msg += f"• Свеча: <b>отличная</b> (закрытие {cp:.0f}%)\n"
    elif cp >= 55:
        msg += f"• Свеча: <b>хорошая</b> (закрытие {cp:.0f}%)\n"
    else:
        msg += f"• Свеча: закрытие {cp:.0f}% (тень {wr:.0f}%)\n"
    
    if signal.get('is_compressed'):
        msg += f"• Поджатие: <b>{signal['compression_pct']}%</b>\n"
    
    hc = signal.get('held_closed', 0)
    ha = signal.get('held_all', 0)
    if hc >= 1:
        msg += f"• Удержание: <b>{hc} закр. свеч</b> над базой\n"
    elif signal.get('is_holding'):
        msg += f"• Удержание: <b>{ha} свеч</b> над базой\n"
    
    msg += f"\n⭐ Сила сигнала: <b>{signal['score']}/100</b>\n\n"
    
    msg += "<b>Детали:</b>\n"
    for reason in signal['reasons']:
        if reason:
            msg += f"• {reason}\n"
    
    return msg


async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception:
        return False


# -------------------------------------------------------------
# ЗАГРУЗКА ДАННЫХ
# -------------------------------------------------------------
async def fetch_24h_volume(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
    params = {"symbol": symbol}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0 and "data" in data:
                    return float(data["data"].get("quoteVolume", 0))
    except Exception:
        pass
    return 0.0


async def fetch_futures_symbols(session):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0 and "data" in data:
                    return [item["symbol"] for item in data["data"]
                            if item.get("symbol", "").endswith("-USDT") and item.get("status") == 1]
    except Exception:
        pass
    return []


async def fetch_klines(session, symbol, interval="1h", limit=60):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("code") == 0 and "data" in res:
                    return res["data"]
    except Exception:
        pass
    return None


# -------------------------------------------------------------
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# -------------------------------------------------------------
async def process_symbol(session, symbol, semaphore, volume_cache, sent_signals):
    async with semaphore:
        try:
            current_time = asyncio.get_event_loop().time()
            
            if symbol in sent_signals:
                last_time, last_type = sent_signals[symbol]
                if last_type == "EARLY":
                    cooldown = COOLDOWN_EARLY
                elif last_type == "STRONG_EARLY":
                    cooldown = COOLDOWN_STRONG
                else:
                    cooldown = COOLDOWN_CONFIRMED
                if current_time - last_time < cooldown:
                    return None
            
            volume_24h = volume_cache.get(symbol, {}).get("volume", 0.0)
            cache_time = volume_cache.get(symbol, {}).get("time", 0)
            if current_time - cache_time > 300:
                volume_24h = await fetch_24h_volume(session, symbol)
                volume_cache[symbol] = {"volume": volume_24h, "time": current_time}
            
            klines_1h = await fetch_klines(session, symbol, "1h", limit=60)
            if not klines_1h or len(klines_1h) < BASE_MIN_HOURS + 3:
                return None
            
            signal = evaluate_accumulation_expansion(symbol, klines_1h, volume_24h)
            
            if signal:
                is_real = await check_orderbook_spoofing(session, symbol)
                if not is_real:
                    logging.info(f"Спуфинг отклонен по {symbol}")
                    return None
                
                message = format_signal_message(symbol, signal)
                success = await send_telegram(session, message)
                if success:
                    sent_signals[symbol] = (current_time, signal["type"])
                    return signal
            return None
        except Exception as e:
            logging.debug(f"Ошибка {symbol}: {e}")
            return None


# -------------------------------------------------------------
# ОСНОВНОЙ ЦИКЛ
# -------------------------------------------------------------
async def scanner_loop():
    logging.info("Сканер запущен")
    
    sent_signals = {}
    volume_cache = {}
    MAX_STORED = 500
    
    connector = aiohttp.TCPConnector(limit=20)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                start_time = asyncio.get_event_loop().time()
                current_time = start_time
                
                symbols = await fetch_futures_symbols(session)
                if not symbols:
                    await asyncio.sleep(30)
                    continue
                
                logging.info(f"Сканирую {len(symbols)} пар...")
                
                tasks = [process_symbol(session, sym, semaphore, volume_cache, sent_signals) for sym in symbols]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                signals_found = sum(1 for r in results if r is not None and not isinstance(r, Exception))
                
                elapsed = asyncio.get_event_loop().time() - start_time
                logging.info(f"Скан за {elapsed:.1f}с | Сигналов: {signals_found}")
                
                if len(sent_signals) > MAX_STORED:
                    old = [s for s, (t, _) in sent_signals.items() if current_time - t > 7200]
                    for s in old:
                        del sent_signals[s]
                if len(volume_cache) > 1000:
                    old_v = [s for s, d in volume_cache.items() if current_time - d.get("time", 0) > 600]
                    for s in old_v:
                        del volume_cache[s]
                
                await asyncio.sleep(20)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Критическая ошибка: {e}")
                await asyncio.sleep(30)


# -------------------------------------------------------------
# ВЕБ-СЕРВЕР
# -------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Active", status=200)


async def main():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Веб-сервер слушает порт {port}")
    
    asyncio.create_task(scanner_loop())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Остановлено")
