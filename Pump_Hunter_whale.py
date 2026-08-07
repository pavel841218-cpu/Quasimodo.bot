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
    try:
        return float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4]), float(kline[5])
    except (IndexError, ValueError, TypeError) as e:
        raise ValueError(f"Ошибка парсинга свечи: {e}")


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
    """
    Ищет ПОСЛЕДНЮЮ валидную базу.
    С ЗАЩИТОЙ от ошибок.
    """
    try:
        if len(klines_1h) < BASE_MIN_HOURS + 3:
            return False, 0.0, 0.0, 0.0, 0, [], -1
        
        highs = []
        lows = []
        opens = []
        closes = []
        
        for k in klines_1h:
            try:
                o, h, l, c, v = parse_kline(k)
                highs.append(h)
                lows.append(l)
                opens.append(o)
                closes.append(c)
            except:
                continue
        
        if len(highs) < BASE_MIN_HOURS + 3:
            return False, 0.0, 0.0, 0.0, 0, [], -1
        
        search_start = len(highs) - 2
        
        for end in range(search_start, BASE_MIN_HOURS, -1):
            for h in range(min(BASE_MAX_HOURS, end), BASE_MIN_HOURS - 1, -1):
                start = end - h
                if start < 0:
                    continue
                
                sub_highs = highs[start:end]
                sub_lows = lows[start:end]
                sub_opens = opens[start:end]
                sub_closes = closes[start:end]
                
                if not sub_highs or not sub_lows:
                    continue
                
                max_h = max(sub_highs)
                min_l = min(sub_lows)
                
                if min_l <= 0:
                    continue
                
                width = ((max_h - min_l) / min_l) * 100
                
                if width <= BASE_MAX_WIDTH_PCT:
                    bodies = []
                    ranges_list = []
                    
                    for o, c, h_val, l_val in zip(sub_opens, sub_closes, sub_highs, sub_lows):
                        if o > 0:
                            bodies.append(abs(c - o) / o * 100)
                        if l_val > 0:
                            ranges_list.append((h_val - l_val) / l_val * 100)
                    
                    if not bodies or not ranges_list:
                        continue
                    
                    avg_body = np.mean(bodies)
                    avg_range = np.mean(ranges_list)
                    
                    if avg_body <= BASE_MAX_BODY_PCT and avg_range <= BASE_MAX_RANGE_PCT:
                        # Проверяем пробой
                        if end < len(closes):
                            post_base_closes = closes[end:]
                            
                            if len(post_base_closes) >= 1:
                                last_close = post_base_closes[-1]
                                
                                if last_close > max_h * 1.003:
                                    # База найдена
                                    return True, round(width, 2), max_h, min_l, h, klines_1h[start:end], start
        
        return False, 0.0, 0.0, 0.0, 0, [], -1
        
    except Exception as e:
        logging.debug(f"Ошибка в find_base: {e}")
        return False, 0.0, 0.0, 0.0, 0, [], -1


def check_ema_setup(closes, range_high, range_low):
    try:
        if len(closes) < EMA_SLOW:
            return None, 0, ""
        
        ema20 = calculate_ema(closes, EMA_FAST)
        ema40 = calculate_ema(closes, EMA_SLOW)
        last_close = closes[-1]
        
        if range_low <= ema20 <= range_high:
            return "EMA20_IN_BASE", 20, "🧲 EMA20 внутри базы накопления"
        if range_low <= ema40 <= range_high:
            return "EMA40_IN_BASE", 18, "🧲 EMA40 внутри базы накопления"
        if last_close > ema20 and ema20 > ema40:
            return "EMA_BULLISH", 15, "📈 EMA20 > EMA40 — бычья структура"
        if last_close > ema20:
            return "ABOVE_EMA20", 10, "📈 Цена выше EMA20"
        
        return None, 0, ""
    except:
        return None, 0, ""


def check_price_compression(base_candles, base_high, base_low):
    try:
        if len(base_candles) < 4:
            return False, 0.0
        
        closes = []
        for k in base_candles:
            try:
                closes.append(parse_kline(k)[3])
            except:
                continue
        
        if len(closes) < 4:
            return False, 0.0
        
        if base_high <= base_low:
            return False, 0.0
        
        last_closes = closes[-4:]
        positions = [(c - base_low) / (base_high - base_low) * 100 for c in last_closes]
        avg_position = np.mean(positions)
        trend_up = positions[-1] > positions[0]
        is_compressed = avg_position > 55 and trend_up
        
        return is_compressed, round(avg_position, 1)
    except:
        return False, 0.0


def check_holding_above_base(klines_1h, base_high, base_end_idx):
    try:
        if base_end_idx >= len(klines_1h) - 1:
            return False, 0, 0
        
        post_base = klines_1h[base_end_idx:]
        closes_after = []
        
        for k in post_base:
            try:
                closes_after.append(parse_kline(k)[3])
            except:
                continue
        
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
    except:
        return False, 0, 0


# -------------------------------------------------------------
# ОСНОВНОЙ АЛГОРИТМ
# -------------------------------------------------------------
def evaluate_accumulation_expansion(symbol, klines_1h, volume_24h_usdt=0.0):
    """Защищённая версия"""
    try:
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
        
        if candle_range <= 0:
            close_position = 0.5
        else:
            close_position = (curr_close - curr_low) / candle_range
        
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
        
        past_volumes = []
        for k in klines_1h[-13:-1]:
            try:
                _, _, _, c, v = parse_kline(k)
                past_volumes.append(v * c)
            except:
                continue
        
        avg_past_volume = np.mean(past_volumes) if past_volumes else 1.0
        raw_rvol = projected_volume / avg_past_volume if avg_past_volume > 0 else 1.0
        
        if elapsed_minutes < 3:
            projected_rvol = min(raw_rvol, 5.0)
        elif elapsed_minutes < 5:
            projected_rvol = min(raw_rvol, 7.0)
        else:
            projected_rvol = raw_rvol
        
        # Поиск базы
        is_flat, base_width, base_high, base_low, base_hours, base_candles, base_start = \
            find_last_accumulation_before_breakout(klines_1h)
        
        if not is_flat:
            return None
        
        # EMA
        all_closes = []
        for k in klines_1h:
            try:
                all_closes.append(parse_kline(k)[3])
            except:
                continue
        
        ema_type, ema_score, ema_detail = check_ema_setup(all_closes, base_high, base_low)
        
        # Поджатие
        is_compressed, compression_pct = check_price_compression(base_candles, base_high, base_low)
        
        # Удержание
        base_end_idx = base_start + base_hours
        is_holding, held_closed, held_all = check_holding_above_base(klines_1h, base_high, base_end_idx)
        
        # Выход
        breakout_above_base = curr_close > base_high
        breakout_pct = ((curr_close - base_high) / base_high) * 100 if base_high > 0 else 0
        distance_from_base = breakout_pct
        returning_to_base = curr_close < base_high * 0.998 and curr_change < 0.5
        
        # Компенсация тени
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
        
        # CONFIRMED
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
                signal_name = "🔥 ВЗРЫВНОЙ ПРОБОЙ БАЗЫ"
            elif curr_change >= 8.0:
                final_score = 93
                signal_name = "🔥 МОЩНЫЙ ПРОБОЙ БАЗЫ"
            elif curr_change >= 3.0:
                final_score = 87
                signal_name = "🚀 ПРОБОЙ БАЗЫ"
            else:
                final_score = 80
                signal_name = "📈 ПОДТВЕРЖДЕНИЕ ИМПУЛЬСА"
            
            return _build_signal("CONFIRMED", signal_name, final_score, curr_close,
                               curr_change, projected_rvol, base_hours, base_width,
                               base_high, base_low, breakout_pct, distance_from_base,
                               int(elapsed_minutes), ema_type, is_compressed,
                               compression_pct, is_holding, held_closed, held_all,
                               close_position, upper_wick_ratio, ema_detail)
        
        # STRONG EARLY
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
            return _build_signal("STRONG_EARLY", "🟢 УСИЛЕННЫЙ СИГНАЛ",
                               min(92, 60 + total_strong * 6), curr_close,
                               curr_change, projected_rvol, base_hours, base_width,
                               base_high, base_low, breakout_pct, distance_from_base,
                               int(elapsed_minutes), ema_type, is_compressed,
                               compression_pct, is_holding, held_closed, held_all,
                               close_position, upper_wick_ratio, ema_detail)
        
        # EARLY
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
            return _build_signal("EARLY", "🟡 НАЧАЛО ВЫХОДА ИЗ НАКОПЛЕНИЯ",
                               min(88, 50 + total_early * 7), curr_close,
                               curr_change, projected_rvol, base_hours, base_width,
                               base_high, base_low, breakout_pct, distance_from_base,
                               int(elapsed_minutes), ema_type, is_compressed,
                               compression_pct, is_holding, held_closed, held_all,
                               close_position, upper_wick_ratio, ema_detail)
        
        return None
        
    except Exception as e:
        logging.debug(f"Ошибка в evaluate для {symbol}: {e}")
        return None


def _build_signal(signal_type, signal_name, score, price, change_pct, rvol,
                  base_hours, base_width, base_high, base_low, breakout_pct,
                  distance_from_base, elapsed, ema_type, is_compressed,
                  compression_pct, is_holding, held_closed, held_all,
                  close_position, wick_ratio, ema_detail):
    reasons = [
        f"📦 База: {base_hours}ч (ширина {base_width}%)",
        f"🎯 Верх базы: {base_high:.6f}",
        f"🚀 Выход: +{breakout_pct:.2f}% над базой",
        f"📊 Proj. RVOL: x{rvol:.1f}",
    ]
    if change_pct >= 1.5:
        reasons.append(f"📈 Рост H1: +{change_pct:.1f}%")
    if ema_detail:
        reasons.append(ema_detail)
    if is_compressed:
        reasons.append(f"📈 Поджатие: {compression_pct:.0f}%")
    if held_closed >= 1:
        reasons.append(f"📌 Удержание: {held_closed} закр. свеч над базой")
    elif is_holding:
        reasons.append(f"📌 Удержание: {held_all} свеч над базой")
    reasons.append(f"🕯 Закрытие: {close_position*100:.0f}%")
    
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
// ... (format_signal_message, send_telegram — без изменений)
// ... (fetch_24h_volume, fetch_futures_symbols, fetch_klines — без изменений)
// ... (process_symbol, scanner_loop — без изменений)
// ... (handle_ping, main — без изменений)
