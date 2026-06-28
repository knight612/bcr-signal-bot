#!/usr/bin/env python3
"""
BCR Signal Bot — Break, Close, Retest
Pairs: FX Vol 20/40/60/80/99 + SFX Vol 20/40/60/80
Timeframes: M30 (structure) → M15 (retest) → M5 (entry)
Signals sent to Telegram
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
    {"id": "fxv20", "name": "FX Vol 20",  "symbol": "1HZ20V"},
    {"id": "fxv40", "name": "FX Vol 40",  "symbol": "1HZ40V"},
    {"id": "fxv60", "name": "FX Vol 60",  "symbol": "1HZ60V"},
    {"id": "fxv80", "name": "FX Vol 80",  "symbol": "1HZ80V"},
    {"id": "fxv99", "name": "FX Vol 99",  "symbol": "1HZ100V"},
    {"id": "sfx20", "name": "SFX Vol 20", "symbol": "stpRNG1"},
    {"id": "sfx40", "name": "SFX Vol 40", "symbol": "stpRNG2"},
    {"id": "sfx60", "name": "SFX Vol 60", "symbol": "stpRNG3"},
    {"id": "sfx80", "name": "SFX Vol 80", "symbol": "stpRNG4"},
]

SYMBOL_MAP = {p["symbol"]: p for p in PAIRS}

# Cooldown between signals per pair (seconds)
SIGNAL_COOLDOWN = 5 * 60

# ── STATE ──────────────────────────────────────────────
ticks       = defaultdict(list)   # symbol -> list of (timestamp, price)
last_signal = defaultdict(float)  # symbol -> last signal epoch
pair_state  = defaultdict(lambda: {"m30": False, "m15": False, "m5": False, "signal": None})

# ── HELPERS ────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.json().get("ok"):
            log("✅ Telegram alert sent")
        else:
            log(f"⚠️  Telegram error: {r.json().get('description')}")
    except Exception as e:
        log(f"❌ Telegram failed: {e}")

def get_candles(symbol, minutes):
    """Aggregate raw ticks into OHLC candles."""
    now_ms  = time.time() * 1000
    ms      = minutes * 60 * 1000
    lookback = 10  # number of candles to build
    candles = []
    for i in range(lookback, 0, -1):
        start = now_ms - ms * i
        end   = start + ms
        slice_ = [p for (t, p) in ticks[symbol] if start <= t < end]
        if slice_:
            candles.append({
                "open":  slice_[0],
                "close": slice_[-1],
                "high":  max(slice_),
                "low":   min(slice_),
            })
    return candles

def detect_bcr(symbol):
    """Run BCR logic and return (direction, level) or (None, None)."""
    state = pair_state[symbol]

    c30 = get_candles(symbol, 30)
    c15 = get_candles(symbol, 15)
    c5  = get_candles(symbol, 5)

    if len(c30) < 2:
        state["m30"] = state["m15"] = state["m5"] = False
        state["signal"] = None
        return None, None

    prev30 = c30[-2]
    last30 = c30[-1]
    rng    = prev30["high"] - prev30["low"]
    if rng == 0:
        return None, None

    # M30 — did price break AND close beyond structure?
    broke, direction, level = False, None, None
    if last30["close"] > prev30["high"] + rng * 0.05:
        broke, direction, level = True, "BUY", prev30["high"]
    elif last30["close"] < prev30["low"] - rng * 0.05:
        broke, direction, level = True, "SELL", prev30["low"]

    if not broke:
        state["m30"] = state["m15"] = state["m5"] = False
        state["signal"] = None
        return None, None
    state["m30"] = True

    # M15 — did price retest the broken level?
    if not c15:
        state["m15"] = state["m5"] = False
        state["signal"] = None
        return None, None
    last15 = c15[-1]
    tol    = rng * 0.15
    if direction == "BUY":
        m15_ok = last15["low"] <= level + tol and last15["close"] > level
    else:
        m15_ok = last15["high"] >= level - tol and last15["close"] < level
    if not m15_ok:
        state["m15"] = state["m5"] = False
        state["signal"] = None
        return None, None
    state["m15"] = True

    # M5 — entry confirmation
    if not c5:
        state["m5"] = False
        state["signal"] = None
        return None, None
    last5 = c5[-1]
    m5_ok = (last5["close"] > level) if direction == "BUY" else (last5["close"] < level)
    if not m5_ok:
        state["m5"] = False
        state["signal"] = None
        return None, None
    state["m5"] = True

    # Cooldown check
    now_ts = time.time()
    if state["signal"] == direction and now_ts - last_signal[symbol] < SIGNAL_COOLDOWN:
        return None, None

    state["signal"] = direction
    last_signal[symbol] = now_ts
    return direction, level

def fire_signal(pair, direction, level):
    arrow = "🟢" if direction == "BUY" else "🔴"
    msg = (
        f"🤖 *BCR Live Signal*\n\n"
        f"📊 *Pair:* {pair['name']}\n"
        f"{arrow} *Direction:* {direction}\n"
        f"📍 *Key Level:* {level:.3f}\n\n"
        f"✅ M30 — Structure broken and closed\n"
        f"✅ M15 — Retest confirmed\n"
        f"✅ M5 — Entry confirmed\n\n"
        f"⚡ Strategy: Break · Close · Retest\n"
        f"🕐 Time: {now_str()}\n\n"
        f"⚠️ Always manage your risk!"
    )
    log(f"🚨 SIGNAL: {pair['name']} {direction} @ {level:.3f}")
    send_telegram(msg)

def run_scan():
    """Scan all pairs for BCR setups."""
    for pair in PAIRS:
        sym = pair["symbol"]
        direction, level = detect_bcr(sym)
        state = pair_state[sym]
        status = "---"
        if state["signal"]:
            status = state["signal"]
        elif state["m30"] and state["m15"]:
            status = "RETEST..."
        elif state["m30"]:
            status = "BREAK"
        log(f"  {pair['name']:12} | M30={'✅' if state['m30'] else '❌'} M15={'✅' if state['m15'] else '❌'} M5={'✅' if state['m5'] else '❌'} | {status}")
        if direction:
            fire_signal(pair, direction, level)

# ── WEBSOCKET ──────────────────────────────────────────
async def on_message(ws, message):
    data = json.loads(message)

    if "error" in data:
        log(f"❌ Deriv API error: {data['error']['message']}")
        return

    if data.get("msg_type") == "tick":
        tick = data["tick"]
        sym  = tick["symbol"]
        price = tick["quote"]
        ts    = tick["epoch"] * 1000

        ticks[sym].append((ts, price))
        # Keep only last 90 minutes of ticks
        cutoff = (time.time() - 90 * 60) * 1000
        ticks[sym] = [(t, p) for (t, p) in ticks[sym] if t > cutoff]

async def main():
    log("🚀 BCR Bot starting...")
    send_telegram(
        "🤖 *BCR Signal Bot Online*\n\n"
        "Monitoring pairs:\n"
        "• FX Vol 20 / 40 / 60 / 80 / 99\n"
        "• SFX Vol 20 / 40 / 60 / 80\n\n"
        "⚡ Strategy: M30 Structure → M15 Retest → M5 Entry\n"
        "✅ Bot is live and scanning!"
    )

    reconnect_delay = 5
    while True:
        try:
            log(f"🔌 Connecting to Deriv WebSocket...")
            async with websockets.connect(WS_URL, ping_interval=30) as ws:
                log("✅ Connected!")
                reconnect_delay = 5

                # Subscribe to all pairs
                for pair in PAIRS:
                    await ws.send(json.dumps({"ticks": pair["symbol"], "subscribe": 1}))
                    log(f"   Subscribed to {pair['name']} ({pair['symbol']})")

                last_scan = time.time()

                async for message in ws:
                    await on_message(ws, message)

                    # Run BCR scan every 5 seconds
                    if time.time() - last_scan >= 5:
                        log("--- BCR Scan ---")
                        run_scan()
                        last_scan = time.time()

        except Exception as e:
            log(f"❌ Connection error: {e}")
            log(f"🔄 Reconnecting in {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

if __name__ == "__main__":
    asyncio.run(main())
