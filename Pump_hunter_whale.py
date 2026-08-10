        async with session.get(url, timeout=10) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                pairs = [item["symbol"] for item in data.get("data", []) if item["symbol"].endswith("-USDT")]
                return pairs
    except Exception as e:
        logging.error(f"Error fetching pairs: {e}")
    
    # Fallback list if API fails
    return ["BTC-USDT", "ETH-USDT", "SOL-USDT", "ESP-USDT", "NIL-USDT", "CAP-USDT"]

async def get_klines_1h(session, symbol, limit=60):
    url = f"https://open-api.bingx.com/openApi/swap/v3/quote/klines?symbol={symbol}&interval=1h&limit={limit}"
    try:
        async with session.get(url, timeout=8) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                return data.get("data", [])
    except Exception:
        pass
    return []

# ==================== CORE BOT LOGIC ====================
async def refresh_shelves_memory(session):
    """
    Scans market to rebuild active shelves database
    """
    logging.info("рџ”Ћ [Р“Р›РћР‘РђР›Р¬РќР«Р™ РЎРљРђРќ] РћР±РЅРѕРІР»РµРЅРёРµ Р±Р°Р·С‹ РїРѕР»РѕРє...")
    pairs = await get_active_usdt_pairs(session)
    logging.info(f"РџРѕР»СѓС‡РµРЅРѕ РїР°СЂ РґР»СЏ СЃРєР°РЅР°: {len(pairs)}")
    
    new_shelves = {}
    
    for symbol in pairs:
        klines = await get_klines_1h(session, symbol, limit=50)
        if not klines:
            continue
            
        shelf = find_shelf_before_breakout(klines)
        if shelf:
            new_shelves[symbol] = shelf
            
        await asyncio.sleep(0.05) # Rate limit friendly

    global ACTIVE_SHELVES
    ACTIVE_SHELVES = new_shelves
    logging.info(f"рџ’ѕ [РџР•Р Р’РР§РќР«Р™ РЎРљРђРќ Р—РђР’Р•Р РЁР•Рќ] РќР°Р№РґРµРЅРѕ РїРѕР»РѕРє: {len(ACTIVE_SHELVES)}")

async def check_whale_window_breakouts(session):
    """
    Targeted check during Whale Window (49-56 min) for volume break out
    """
    if not ACTIVE_SHELVES:
        return

    logging.info(f"рџђі [WHALE WINDOW CHECK] РњРѕРЅРёС‚РѕСЂРёРЅРі {len(ACTIVE_SHELVES)} РїРѕР»РѕРє...")
    
    for symbol, shelf in list(ACTIVE_SHELVES.items()):
        klines = await get_klines_1h(session, symbol, limit=25)
        if not klines or len(klines) < 20:
            continue

        curr_candle = klines[-1]
        hist_candles = klines[-21:-1]
        
        _, _, curr_h, _, curr_v = parse_kline(curr_candle)
        hist_vols = [parse_kline(k)[4] for k in hist_candles if parse_kline(k)[4] > 0]
        
        if not hist_vols:
            continue
            
        avg_v = sum(hist_vols) / len(hist_vols)
        rvol = (curr_v / avg_v) if avg_v > 0 else 0.0

        # Condition 1: Price pushing/breaking top of shelf
        shelf_high = shelf["high"]
        is_pressing_high = curr_h >= (shelf_high * 0.995)
        
        # Condition 2: Volume explosion
        is_volume_spike = rvol >= MIN_RVOL_WHALE

        if is_pressing_high and is_volume_spike:
            clean_symbol = symbol.replace("-", "")
            msg = (
                f"рџљЂ <b>[WHALE BREAKOUT ALERT] {clean_symbol}</b>\n\n"
                f"рџ”№ <b>РЁРёСЂРёРЅР° РїРѕР»РєРё:</b> {shelf['width']}% ({shelf['hours']}С‡)\n"
                f"рџ”№ <b>RVOL (Р’СЃРїР»РµСЃРє РѕР±СЉРµРјР°):</b> {round(rvol, 2)}x\n"
                f"рџ”№ <b>РЈСЂРѕРІРµРЅСЊ СЃРѕРїСЂРѕС‚РёРІР»РµРЅРёСЏ:</b> {shelf_high}\n"
                f"рџ”№ <b>U/W РЇРјР° (Р’С‹С‚СЂСЏС…РёРІР°РЅРёРµ):</b> {'Р”Рђ (' + str(shelf['dip_depth']) + '%)' if shelf['has_dip'] else 'РќР•Рў'}\n\n"
                f"рџ“Ќ <i>РљРёС‚С‹ РїРѕРґР¶РёРјР°СЋС‚ С†РµРЅСѓ Рє РїСЂРѕР±РѕСЋ!</i>"
            )
            await send_telegram_alert(session, msg)
            logging.info(f"вљЎ РЎРР“РќРђР› РћРўРџР РђР’Р›Р•Рќ: {clean_symbol}")
            
            # Remove to prevent spamming
            ACTIVE_SHELVES.pop(symbol, None)

        await asyncio.sleep(0.05)

async def main_loop():
    async with aiohttp.ClientSession() as session:
        # Initial scan on start
        await refresh_shelves_memory(session)
        
        last_scanned_hour = -1

        while True:
            now = datetime.utcnow()
            curr_hour = now.hour
            curr_min = now.minute

            # Hourly full rescan at 00 minutes
            if curr_min == 0 and curr_hour != last_scanned_hour:
                await refresh_shelves_memory(session)
                last_scanned_hour = curr_hour

            # Whale Window monitoring (49 to 56 minutes)
            if WHALE_WINDOW_START_MIN <= curr_min <= WHALE_WINDOW_END_MIN:
                await check_whale_window_breakouts(session)
                await asyncio.sleep(60)  # Check once every minute during window
            else:
                await asyncio.sleep(30)  # Regular sleep outside window

if __name__ == "__main__":
    # Start Keep-Alive Server in background thread
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    # Start main bot loop
    asyncio.run(main_loop())
