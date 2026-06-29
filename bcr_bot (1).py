#!/usr/bin/env python3
"""
BCR Signal Bot — Break, Close, Retest (Precise Strategy)
Instruments: Jump 50 Index, Jump 75 Index
Timeframes: M30 (structure + break/close) → M15 (retest developing) → M5 (confirmation candle + entry)
Signals sent to Telegram with Entry, SL, TP1, TP2, R:R
"""

import json
import time
import asyncio
import requests
import websockets
from datetime import datetime
from collections import defaultdict

# ── CONFIG ─────────────────────────────────────────────
DERIV_APP_ID     = "1089"
TELEGRAM_TOKEN   = "8932997378:AAFpoh1u65zGRCIZHqp1BqFrjltc5b-X6Co"
TELEGRAM_CHAT_ID = "6292588974"
WS_URL           = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

PAIRS = [
    {"id": "j50", "name": "Jump 50 Index", "symbol": "1HZ50V", "buffer": 100},
    {"id": "j75", "name": "Jump 75 Index", "symbol": "1HZ75V", "buffer": 150},
]

SYMBOL_MAP = {p["symbol"]: p for p in PAIRS}

# ── STRATEGY CONSTANTS ─────────────────────────────────
MIN_RR_RATIO       = 2.0
MAX_ENTRY_DISTANCE = 300
MIN_CANDLES_RANGE  = 5
BODY_CLOSE_PCT     = 0.6
SIGNAL_COOLDOWN    = 4 * 60 * 60
SCAN_INTERVAL      = 60

# ── STATE ──────────────────────────────────────────────
ticks       = defaultdict(list)
last_signal = defaultdict(float)
pair_stage  = defaultdict(lambda: {
    "key_level":    None,
    "direction":    None,
    "stage":        "WATCHING",
    "break_candle": None,
})

# ── HELPERS ────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.json().get("ok"):
            log("✅ Telegram alert sent")
        else:
            log(f"⚠️ Telegram error: {r.json().get('description')}")
    except Exception as e:
        log(f"❌ Telegram failed: {e}")

# ── CANDLE BUILDER ─────────────────────────────────────
def build_candles(symbol, minutes, count=20):
    now_ms = time.time() * 1000
    ms     = minutes * 60 * 1000
    candles = []
    for i in range(count, 0, -1):
        start  = now_ms - ms * i
        end    = start + ms
        slice_ = [p for (t, p) in ticks[symbol] if start <= t < end]
        if len(slice_) >= 3:
            candles.append({
                "open":  slice_[0],
                "close": slice_[-1],
                "high":  max(slice_),
                "low":   min(slice_),
                "size":  max(slice_) - min(slice_),
            })
    return candles

# ── KEY LEVEL DETECTION ────────────────────────────────
def find_key_levels(candles):
    if len(candles) < MIN_CANDLES_RANGE:
        return []
    levels    = []
    avg_range = sum(c["size"] for c in candles) / len(candles)
    tolerance = avg_range * 0.3
    for c in candles:
        for price, ltype in [(c["high"], "resistance"), (c["low"], "support")]:
            touches = sum(
                1 for x in candles
                if abs(x["high"] - price) <= tolerance or abs(x["low"] - price) <= tolerance
            )
            if touches >= 2:
                if not any(abs(price - lv["price"]) <= tolerance for lv in levels):
                    levels.append({"price": price, "type": ltype, "touches": touches})
    return sorted(levels, key=lambda x: x["touches"], reverse=True)

# ── BREAK AND CLOSE CHECK ──────────────────────────────
def check_break_and_close(candle, level_price, direction):
    body_top    = max(candle["open"], candle["close"])
    body_bottom = min(candle["open"], candle["close"])
    body_size   = abs(candle["close"] - candle["open"])
    candle_size = candle["size"]
    if candle_size == 0:
        return False
    if body_size / candle_size < BODY_CLOSE_PCT:
        return False
    if direction == "BUY":
        return body_bottom > level_price and candle["close"] > level_price
    elif direction == "SELL":
        return body_top < level_price and candle["close"] < level_price
    return False

# ── RETEST CHECK ───────────────────────────────────────
def check_retest(candles_m15, level_price, direction):
    if not candles_m15:
        return False, "NO_DATA"
    last        = candles_m15[-1]
    body_top    = max(last["open"], last["close"])
    body_bottom = min(last["open"], last["close"])
    avg_range   = sum(c["size"] for c in candles_m15) / len(candles_m15)
    tolerance   = avg_range * 0.5
    if direction == "BUY":
        touching = abs(last["low"] - level_price) <= tolerance or last["low"] <= level_price
        body_ok  = body_bottom >= level_price
        if touching and body_ok:
            return True, "VALID"
        elif touching:
            return False, "DEVELOPING"
    elif direction == "SELL":
        touching = abs(last["high"] - level_price) <= tolerance or last["high"] >= level_price
        body_ok  = body_top <= level_price
        if touching and body_ok:
            return True, "VALID"
        elif touching:
            return False, "DEVELOPING"
    return False, "INVALID"

# ── CONFIRMATION CANDLE CHECK ──────────────────────────
def check_confirmation_candle(candles_m5, level_price, direction):
    if len(candles_m5) < 2:
        return False, None
    last        = candles_m5[-1]
    prev        = candles_m5[-2]
    body_size   = abs(last["close"] - last["open"])
    candle_size = last["size"]
    if candle_size == 0:
        return False, None
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    # Pin Bar
    if direction == "BUY":
        if lower_wick > body_size * 2 and last["close"] > level_price and last["low"] <= level_price * 1.001:
            return True, "PIN BAR"
    if direction == "SELL":
        if upper_wick > body_size * 2 and last["close"] < level_price and last["high"] >= level_price * 0.999:
            return True, "PIN BAR"

    # Engulfing
    prev_body_top    = max(prev["open"], prev["close"])
    prev_body_bottom = min(prev["open"], prev["close"])
    if direction == "BUY":
        if last["close"] > prev_body_top and last["open"] < prev_body_bottom and last["close"] > level_price:
            return True, "BULLISH ENGULFING"
    if direction == "SELL":
        if last["close"] < prev_body_bottom and last["open"] > prev_body_top and last["close"] < level_price:
            return True, "BEARISH ENGULFING"

    # Inside Bar Breakout
    if direction == "BUY":
        if (prev["high"] > last["high"] and prev["low"] < last["low"] and
                last["close"] > prev["high"] and last["close"] > level_price):
            return True, "INSIDE BAR BREAKOUT"
    if direction == "SELL":
        if (prev["high"] > last["high"] and prev["low"] < last["low"] and
                last["close"] < prev["low"] and last["close"] < level_price):
            return True, "INSIDE BAR BREAKOUT"

    return False, None

# ── RISK/REWARD CALCULATOR ─────────────────────────────
def calculate_rr(entry, level_price, direction, buffer, candles_m30):
    if direction == "BUY":
        sl   = level_price - buffer
        risk = entry - sl
    else:
        sl   = level_price + buffer
        risk = sl - entry
    if risk <= 0:
        return None
    levels    = find_key_levels(candles_m30)
    tp_levels = []
    for lv in levels:
        if direction == "BUY" and lv["price"] > entry:
            tp_levels.append(lv["price"])
        elif direction == "SELL" and lv["price"] < entry:
            tp_levels.append(lv["price"])
    tp_levels.sort(reverse=(direction == "SELL"))
    tp1    = tp_levels[0] if tp_levels else (entry + risk * 2 if direction == "BUY" else entry - risk * 2)
    tp2    = tp_levels[1] if len(tp_levels) > 1 else (entry + risk * 3 if direction == "BUY" else entry - risk * 3)
    reward = abs(tp1 - entry)
    rr     = reward / risk if risk > 0 else 0
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "risk": risk, "reward": reward, "rr": rr}

# ── MAIN BCR SCANNER ───────────────────────────────────
def scan_bcr(symbol, pair):
    state  = pair_stage[symbol]
    buffer = pair["buffer"]
    c30    = build_candles(symbol, 30, 20)
    c15    = build_candles(symbol, 15, 10)
    c5     = build_candles(symbol, 5,  10)
    if len(c30) < 5:
        return
    current_price = ticks[symbol][-1][1] if ticks[symbol] else None
    if not current_price:
        return

    # STAGE 1: WATCHING — find key level and break/close
    if state["stage"] == "WATCHING":
        levels = find_key_levels(c30)
        if not levels:
            return
        last_c30 = c30[-1]
        for lv in levels[:3]:
            if lv["type"] == "resistance":
                if check_break_and_close(last_c30, lv["price"], "BUY"):
                    log(f"🔨 {pair['name']} — BREAK & CLOSE above {lv['price']:.2f} (BUY)")
                    state.update({"key_level": lv["price"], "direction": "BUY",
                                  "stage": "BREAK_CLOSE", "break_candle": last_c30})
                    break
            if lv["type"] == "support":
                if check_break_and_close(last_c30, lv["price"], "SELL"):
                    log(f"🔨 {pair['name']} — BREAK & CLOSE below {lv['price']:.2f} (SELL)")
                    state.update({"key_level": lv["price"], "direction": "SELL",
                                  "stage": "BREAK_CLOSE", "break_candle": last_c30})
                    break

    # STAGE 2: BREAK_CLOSE — watch for retest on M15
    elif state["stage"] == "BREAK_CLOSE":
        level     = state["key_level"]
        direction = state["direction"]
        distance  = abs(current_price - level)
        if distance > MAX_ENTRY_DISTANCE:
            log(f"⚠️ {pair['name']} — Price too far ({distance:.0f} pts) — resetting")
            state["stage"] = "WATCHING"
            return
        retest_valid, retest_status = check_retest(c15, level, direction)
        if retest_status == "DEVELOPING":
            log(f"👀 {pair['name']} — Retest DEVELOPING at {level:.2f}")
            return
        if retest_valid:
            log(f"✅ {pair['name']} — Retest CONFIRMED at {level:.2f}")
            state["stage"] = "RETEST"
        if direction == "BUY" and current_price < level - buffer:
            state["stage"] = "WATCHING"
        if direction == "SELL" and current_price > level + buffer:
            state["stage"] = "WATCHING"

    # STAGE 3: RETEST — wait for M5 confirmation candle
    elif state["stage"] == "RETEST":
        level     = state["key_level"]
        direction = state["direction"]
        confirmed, candle_type = check_confirmation_candle(c5, level, direction)
        if not confirmed:
            log(f"⏳ {pair['name']} — Waiting for M5 confirmation at {level:.2f}")
            return
        now_ts = time.time()
        if now_ts - last_signal[symbol] < SIGNAL_COOLDOWN:
            log(f"⏸️ {pair['name']} — Cooldown active")
            state["stage"] = "WATCHING"
            return
        entry   = current_price
        rr_data = calculate_rr(entry, level, direction, buffer, c30)
        if not rr_data:
            state["stage"] = "WATCHING"
            return
        if rr_data["rr"] < MIN_RR_RATIO:
            send_telegram(
                f"⚠️ *BCR Setup — Insufficient R:R*\n\n"
                f"📊 *Pair:* {pair['name']}\n"
                f"{'🟢' if direction == 'BUY' else '🔴'} *Direction:* {direction}\n"
                f"📍 *Key Level:* {level:.2f}\n"
                f"📐 *R:R:* 1:{rr_data['rr']:.1f} (min 1:2 required)\n\n"
                f"❌ *SKIP THIS TRADE — insufficient risk/reward*"
            )
            state["stage"] = "WATCHING"
            last_signal[symbol] = now_ts
            return
        last_signal[symbol] = now_ts
        state["stage"] = "WATCHING"
        fire_signal(pair, direction, level, entry, rr_data, candle_type)

# ── FIRE SIGNAL ────────────────────────────────────────
def fire_signal(pair, direction, level, entry, rr, candle_type):
    arrow = "🟢" if direction == "BUY" else "🔴"
    msg = (
        f"🤖 *BCR SIGNAL — {pair['name']}*\n\n"
        f"{'━' * 28}\n"
        f"📊 *INSTRUMENT:* {pair['name']}\n"
        f"⏱ *TIMEFRAME:* M30 → M15 → M5\n"
        f"📍 *KEY LEVEL:* {level:.2f}\n"
        f"{'━' * 28}\n\n"
        f"✅ *BREAK & CLOSE:* Body closed beyond level\n"
        f"✅ *RETEST:* Rejection confirmed on M15\n"
        f"✅ *CONFIRMATION:* {candle_type} on M5\n"
        f"✅ *SETUP VALID:* YES\n\n"
        f"{'━' * 28}\n"
        f"{arrow} *DIRECTION:* {direction}\n"
        f"🎯 *ENTRY:* {entry:.2f}\n"
        f"🛑 *STOP LOSS:* {rr['sl']:.2f}\n"
        f"💰 *TP1:* {rr['tp1']:.2f}\n"
        f"💰 *TP2:* {rr['tp2']:.2f}\n"
        f"📐 *RISK/REWARD:* 1:{rr['rr']:.1f}\n"
        f"{'━' * 28}\n\n"
        f"⚡ *ACTION:* ENTER NOW\n"
        f"⚠️ Always manage your risk!"
    )
    log(f"🚨 SIGNAL: {pair['name']} {direction} @ {entry:.2f} | SL:{rr['sl']:.2f} | TP1:{rr['tp1']:.2f} | R:R 1:{rr['rr']:.1f}")
    send_telegram(msg)

# ── WEBSOCKET ──────────────────────────────────────────
async def on_message(ws, message):
    data = json.loads(message)
    if "error" in data:
        log(f"❌ Deriv API error: {data['error']['message']}")
        return
    if data.get("msg_type") == "tick":
        tick  = data["tick"]
        sym   = tick["symbol"]
        price = tick["quote"]
        ts    = tick["epoch"] * 1000
        ticks[sym].append((ts, price))
        cutoff     = (time.time() - 90 * 60) * 1000
        ticks[sym] = [(t, p) for (t, p) in ticks[sym] if t > cutoff]

async def main():
    log("🚀 BCR Precision Bot starting...")
    send_telegram(
        "🤖 *BCR Precision Signal Bot Online*\n\n"
        "📊 *Instruments:*\n"
        "• Jump 50 Index\n"
        "• Jump 75 Index\n\n"
        "⚡ *Strategy:* Break · Close · Retest\n"
        "⏱ *Timeframes:* M30 → M15 → M5\n"
        "✅ *Filters:*\n"
        "  • Body close only (no wicks)\n"
        "  • Valid key level (2+ reactions)\n"
        "  • Retest with rejection\n"
        "  • Pin Bar / Engulfing / Inside Bar\n"
        "  • Minimum 1:2 Risk/Reward\n"
        "  • 4hr cooldown per pair\n\n"
        "🎯 High quality signals only!"
    )
    reconnect_delay = 5
    while True:
        try:
            log("🔌 Connecting to Deriv WebSocket...")
            async with websockets.connect(WS_URL, ping_interval=30) as ws:
                log("✅ Connected!")
                reconnect_delay = 5
                for pair in PAIRS:
                    await ws.send(json.dumps({"ticks": pair["symbol"], "subscribe": 1}))
                    log(f"   Subscribed to {pair['name']}")
                last_scan = time.time()
                async for message in ws:
                    await on_message(ws, message)
                    if time.time() - last_scan >= SCAN_INTERVAL:
                        log("--- BCR Precision Scan ---")
                        for pair in PAIRS:
                            try:
                                scan_bcr(pair["symbol"], pair)
                            except Exception as e:
                                log(f"⚠️ Error scanning {pair['name']}: {e}")
                        last_scan = time.time()
        except Exception as e:
            log(f"❌ Connection error: {e}")
            log(f"🔄 Reconnecting in {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

if __name__ == "__main__":
    asyncio.run(main())
