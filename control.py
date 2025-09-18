# control.py â€” THB Indodam (REAL MT5 + Persist JSON + Thread-Safe)
# Port 5000, UI: index.html di folder yang sama
# Fitur: TPSM/TPSB/ABE, Auto M1 (SR-Gate 10%), Session target/timeout, Cooldown
# Endpoints: /, /api/status, /api/candles, /api/diag, /api/symbol/select,
#            /api/strategy/(toggle|tpsm|tpsb|abe), /api/action/(buy|sell|add|close|breakeven)

import os, json, time, threading
from datetime import datetime, timezone, timedelta
from threading import Thread, Event, RLock
from flask import Flask, request, jsonify, send_from_directory
import MetaTrader5 as mt5

BASE_DIR    = os.path.abspath(os.path.dirname(__file__))
PERSIST_FILE= os.path.join(BASE_DIR, "setup.json")

app = Flask(__name__)
stop_flag = Event()
MTX = RLock()                 # <â€” Kunci semua akses MT5
STATUS_FAILS = {"count": 0}   # <â€” Menahan OFFLINE jika 1x error sejenak

# ========== DEFAULT ENV ==========
DEFAULTS = {
    "MT5_PATH":     r"C:\Program Files\MetaTrader 5\terminal64.exe",
    "MT5_LOGIN":    "263084911",
    "MT5_PASSWORD": "Lunas2025$$$",
    "MT5_SERVER":   "Exness-MT5Real37",
    "MT5_SYMBOL":   "XAUUSDc",
}
def CFG(k): return os.environ.get(k) or DEFAULTS.get(k) or ""

# ========== SETUP (persist) ==========
SETUP = {
    "symbols": ["XAUUSDc", "BTCUSDc"],
    "symbol": CFG("MT5_SYMBOL"),
    "auto_mode": False,
    "tpsm_auto": False,
    "tpsb_auto": False,
    "abe_auto": False,
    "sr": {"auto_entry_enabled": True, "near_pct": 0.10, "baseline": "ADR14"},
    "auto_m1": {"enabled": True, "min_wait_sec": 60},
    "daily_target": 10.0, "daily_min": -10.0,
    "session": {
        "profit_target": 3.0,
        "loss_limit": -3.0,
        "max_duration_sec": 9*60,
        "be_required": False,
        "min_positions_for_be": 2,
        "be_min_profit": 0.10
    }
}
def persist_load():
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k,v in data.items():
                if k in SETUP:
                    if isinstance(SETUP[k], dict) and isinstance(v, dict):
                        SETUP[k].update(v)
                    else:
                        SETUP[k] = v
    except Exception as e:
        print("[PERSIST] load EXC:", e, flush=True)

def persist_save():
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(SETUP, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[PERSIST] save EXC:", e, flush=True)

persist_load()

# ========== RUNTIME ==========
STATE = {
    "locked": False,
    "timer": "00:00",
    "cooldown": False, "cooldown_until": 0.0,
    "last_entry_ts": 0.0,
    "last_m1_minute": None,
    "session_active": False, "session_start_ts": 0.0,
    "session_be_hit": False, "session_peak_pl": 0.0,
}

SR_TRIGGER = {
    "buy": {"armed": True, "last_ts": 0.0, "pending": False},
    "sell": {"armed": True, "last_ts": 0.0, "pending": False}
}
SR_STATE = {
    "support": 0.0, "resistance": 0.0, "mid": 0.0,
    "top": 0.0, "bottom": 0.0
}
SR_MIN_GAP = 5.0
SR_PRICE_BUFFER_PCT = 0.0015

CLOSE_REASON = {}

# ========== MT5 helper (semua dibungkus MTX) ==========
def mt5_init():
    term = CFG("MT5_PATH")
    with MTX:
        try:
            ok = mt5.initialize(term) if term and os.path.exists(term) else mt5.initialize()
        except Exception as e:
            print("[MT5] init EXC:", e, flush=True); return False
        if not ok:
            print("[MT5] init FAIL:", mt5.last_error(), flush=True); return False
    return maybe_login()

def maybe_login():
    with MTX:
        ai = mt5.account_info()
    if ai is not None:
        print(f"[MT5] already logged-in: {ai.login}/{ai.server}", flush=True)
        return True
    login, pwd, server = CFG("MT5_LOGIN"), CFG("MT5_PASSWORD"), CFG("MT5_SERVER")
    if not (login and pwd and server):
        print("[MT5] no credentials", flush=True); return False
    with MTX:
        try:
            ok = mt5.login(int(login), password=pwd, server=server)
        except Exception as e:
            print("[MT5] login EXC:", e, flush=True); ok = False
    print("[MT5] login:", "OK" if ok else f"FAIL {mt5.last_error()}", flush=True)
    return ok
def mt5_restart():
    with MTX:
        try:
            mt5.shutdown()
        except Exception as e:
            print("[MT5] shutdown EXC:", e, flush=True)
    time.sleep(0.5)
    print("[MT5] restart triggered", flush=True)
    return mt5_init()

def symbol_ensure(symbol):
    with MTX:
        try:
            si = mt5.symbol_info(symbol)
        except: si = None
    if si and si.visible: return True
    if si and not si.visible:
        with MTX:
            try:
                mt5.symbol_select(symbol, True)
                si = mt5.symbol_info(symbol)
            except: si = None
        if si and si.visible: return True
    # fallback wildcard
    base = symbol.rstrip(".")
    with MTX:
        try:
            cands = mt5.symbols_get(f"{base}*") or []
        except:
            cands = []
    for c in cands:
        with MTX:
            ok = (c.visible or mt5.symbol_select(c.name, True))
        if ok:
            print(f"[SYMBOL] fallback -> {c.name}", flush=True)
            SETUP["symbol"] = c.name
            persist_save()
            return True
    print(f"[SYMBOL] not visible: {symbol}", flush=True); return False

def tick(sym):
    with MTX:
        try: return mt5.symbol_info_tick(sym)
        except: return None

def positions(sym=None):
    with MTX:
        try: return mt5.positions_get(symbol=sym) or [] if sym else (mt5.positions_get() or [])
        except: return []

def candles(sym, tf, count):
    tf_map = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1
    }
    timeframe = tf_map.get(tf, mt5.TIMEFRAME_M1)
    with MTX:
        try:
            rates = mt5.copy_rates_from_pos(sym, timeframe, 0, count)
        except:
            rates = None
    out = []
    if rates is not None:
        for r in rates:
            out.append({"time": int(r['time']), "open": float(r['open']),
                        "high": float(r['high']), "low": float(r['low']),
                        "close": float(r['close'])})
    return out

def account_snapshot():
    with MTX:
        try:
            return mt5.account_info()
        except:
            return None


def float_pl(sym):
    total = 0.0
    for p in positions(sym):
        total += float(getattr(p, 'profit', 0.0) or 0.0)
    return total


def open_count(sym): return len(positions(sym))
def total_lot(sym):  return sum(p.volume for p in positions(sym))

# ========== History helper ==========

def get_history_today():
    now = datetime.now()
    start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with MTX:
        try:
            deals = mt5.history_deals_get(start_day, now) or []
        except Exception:
            deals = []
    trades = {}
    total_pl = 0.0
    for d in deals:
        ticket = int(getattr(d, 'position_id', getattr(d, 'ticket', 0)) or 0)
        if ticket == 0:
            continue
        deal_time = datetime.fromtimestamp(getattr(d, 'time', 0), tz=timezone.utc)
        if deal_time.date() != datetime.utcnow().date():
            continue
        rec = trades.setdefault(ticket, {
            'symbol': getattr(d, 'symbol', ''),
            'side': 'UNKNOWN',
            'lot': 0.0,
            'start': 0.0,
            'close': 0.0,
            'profit': 0.0,
            'time_utc': deal_time.strftime('%H:%M:%S'),
            'exec': 'UNKNOWN',
        })
        entry = getattr(d, 'entry', None)
        volume = float(getattr(d, 'volume', 0.0) or 0.0)
        if entry == 0:
            rec['side'] = 'BUY' if getattr(d, 'type', 0) == 0 else 'SELL'
            rec['start'] = getattr(d, 'price', 0.0)
            rec['lot'] = max(rec['lot'], volume)
        elif entry == 1:
            trade_side = 'BUY' if getattr(d, 'type', 0) == 1 else 'SELL'
            if rec['side'] == 'UNKNOWN':
                rec['side'] = trade_side
            rec['close'] = getattr(d, 'price', 0.0)
            rec['lot'] = max(rec['lot'], volume)
            profit = float(getattr(d, 'profit', 0.0) or 0.0)
            rec['profit'] += profit
            rec['time_utc'] = deal_time.strftime('%H:%M:%S')
            comment = (getattr(d, 'comment', '') or '').upper()
            label = comment.split('CLOSE-ALL:')[-1] if 'CLOSE-ALL:' in comment else comment
            label = (label or '').strip().upper()
            if label.startswith('SESSION-P'):
                rec['exec'] = 'SESSION-PROFIT'
            elif label.startswith('SESSION-L'):
                rec['exec'] = 'SESSION-LOSS'
            elif label.startswith('SESSION-T'):
                rec['exec'] = 'SESSION-TIMEOUT'
            elif label.startswith('SESS'):
                rec['exec'] = 'SESSION'
            elif label.startswith('BREAKEV'):
                rec['exec'] = 'BREAKEVEN'
            elif label.startswith('TPSM'):
                rec['exec'] = 'TPSM'
            elif label.startswith('TPSB'):
                rec['exec'] = 'TPSB'
            elif label.startswith('CROSSR'):
                rec['exec'] = 'CROSSR'
            elif label and rec['exec'] == 'UNKNOWN':
                rec['exec'] = label
            elif not label and rec['exec'] == 'UNKNOWN':
                rec['exec'] = 'MANUAL'
            total_pl += profit
            if rec['start'] == 0.0:
                rec['start'] = rec['close']
    history = []
    for rec in trades.values():
        if rec['profit'] == 0.0 and rec['close'] == 0.0:
            continue
        history.append({
            'symbol': rec['symbol'],
            'side': rec['side'],
            'lot': round(rec['lot'], 2),
            'start': rec['start'],
            'close': rec['close'],
            'profit': rec['profit'],
            'time_utc': rec['time_utc'],
            'exec': rec['exec'] or 'UNKNOWN',
        })
    history.sort(key=lambda r: r['time_utc'], reverse=True)
    return history[:10], total_pl

# ========== SR auto-trade ==========

def compute_sr_thresholds(sym):
    data = candles(sym, "M1", 30)
    if len(data) < 5:
        return None
    window = data[-15:]
    support = min(d['low'] for d in window)
    resistance = max(d['high'] for d in window)
    rng = max(1e-6, resistance - support)
    mid = support + rng * 0.5
    top = resistance - rng * 0.10
    bottom = support + rng * 0.10
    buffer_buy = max(1e-6, support * SR_PRICE_BUFFER_PCT)
    buffer_sell = max(1e-6, resistance * SR_PRICE_BUFFER_PCT)
    return {
        'support': support,
        'resistance': resistance,
        'mid': mid,
        'top': top,
        'bottom': bottom,
        'buffer_buy': buffer_buy,
        'buffer_sell': buffer_sell,
    }

def sr_auto_trade(sym):
    global SR_STATE
    thresholds = compute_sr_thresholds(sym)
    if thresholds is None:
        SR_STATE = {'support': 0.0, 'resistance': 0.0, 'mid': 0.0, 'top': 0.0, 'bottom': 0.0}
        for trig in SR_TRIGGER.values():
            trig['armed'] = True
            trig['pending'] = False
        return
    SR_STATE = {
        'support': thresholds['support'],
        'resistance': thresholds['resistance'],
        'mid': thresholds['mid'],
        'top': thresholds['top'],
        'bottom': thresholds['bottom'],
    }
    t = tick(sym)
    price = None
    if t:
        price = t.last if t.last > 0 else (t.bid or t.ask or None)
    now = time.time()
    open_cnt = open_count(sym)
    cooldown = STATE['cooldown']

    if open_cnt > 0:
        for trig in SR_TRIGGER.values():
            if trig['pending']:
                trig['pending'] = False

    def rearm(side):
        trig = SR_TRIGGER[side]
        if trig['armed'] or trig['pending']:
            return
        if now - trig['last_ts'] < SR_MIN_GAP:
            return
        if cooldown or open_cnt != 0:
            return
        if price is None:
            trig['armed'] = True
            return
        if side == 'buy':
            if price >= thresholds['bottom'] + thresholds['buffer_buy']:
                trig['armed'] = True
        else:
            if price <= thresholds['top'] - thresholds['buffer_sell']:
                trig['armed'] = True

    rearm('buy')
    rearm('sell')

    if price is None or open_cnt != 0 or cooldown:
        return

    lot_default = float(SETUP.get('sr_default_lot', 0.01) or 0.01)

    buy_trig = SR_TRIGGER['buy']
    if buy_trig['armed'] and not buy_trig['pending'] and price <= thresholds['bottom'] and now - buy_trig['last_ts'] >= SR_MIN_GAP:
        STATE['locked'] = True
        ok, _ = order_send_with_fallback(sym, 'BUY', lot_default)
        STATE['locked'] = False
        if ok:
            buy_trig['armed'] = False
            buy_trig['pending'] = True
            buy_trig['last_ts'] = now
            begin_session_if_needed(sym)
        else:
            buy_trig['last_ts'] = now

    sell_trig = SR_TRIGGER['sell']
    if sell_trig['armed'] and not sell_trig['pending'] and price >= thresholds['top'] and now - sell_trig['last_ts'] >= SR_MIN_GAP:
        STATE['locked'] = True
        ok, _ = order_send_with_fallback(sym, 'SELL', lot_default)
        STATE['locked'] = False
        if ok:
            sell_trig['armed'] = False
            sell_trig['pending'] = True
            sell_trig['last_ts'] = now
            begin_session_if_needed(sym)
        else:
            sell_trig['last_ts'] = now

# ========== SR-gate / Auto-entry ==========# ========== SR auto-trade ==========

def compute_sr_thresholds(sym):
    data = candles(sym, "M1", 30)
    if len(data) < 5:
        return None
    window = data[-15:]
    support = min(d["low"] for d in window)
    resistance = max(d["high"] for d in window)
    rng = max(1e-6, resistance - support)
    mid = support + rng * 0.5
    top = resistance - rng * 0.10
    bottom = support + rng * 0.10
    buffer_buy = max(1e-6, support * SR_PRICE_BUFFER_PCT)
    buffer_sell = max(1e-6, resistance * SR_PRICE_BUFFER_PCT)
    return {
        "support": support,
        "resistance": resistance,
        "mid": mid,
        "top": top,
        "bottom": bottom,
        "buffer_buy": buffer_buy,
        "buffer_sell": buffer_sell,
    }

def sr_auto_trade(sym):
    global SR_STATE
    thresholds = compute_sr_thresholds(sym)
    if thresholds is None:
        SR_STATE = {"support": 0.0, "resistance": 0.0, "mid": 0.0, "top": 0.0, "bottom": 0.0}
        return
    SR_STATE = {
        "support": thresholds["support"],
        "resistance": thresholds["resistance"],
        "mid": thresholds["mid"],
        "top": thresholds["top"],
        "bottom": thresholds["bottom"],
    }
    t = tick(sym)
    price = None
    if t:
        price = t.last if t.last > 0 else (t.bid or t.ask or None)
    now = time.time()
    open_cnt = open_count(sym)
    cooldown = STATE["cooldown"]

    def rearm(side):
        trig = SR_TRIGGER[side]
        if trig["armed"]:
            return
        if now - trig["last_ts"] < SR_MIN_GAP:
            return
        if cooldown or open_cnt != 0:
            return
        if price is None:
            trig["armed"] = True
            return
        if side == "buy":
            if price >= thresholds["bottom"] + thresholds["buffer_buy"]:
                trig["armed"] = True
        else:
            if price <= thresholds["top"] - thresholds["buffer_sell"]:
                trig["armed"] = True

    rearm("buy")
    rearm("sell")

    if price is None or open_cnt != 0 or cooldown:
        return

    lot_default = float(SETUP.get("sr_default_lot", 0.01) or 0.01)

    if SR_TRIGGER["buy"]["armed"] and price <= thresholds["bottom"] and now - SR_TRIGGER["buy"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "BUY", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["buy"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["buy"]["last_ts"] = now

    if SR_TRIGGER["sell"]["armed"] and price >= thresholds["top"] and now - SR_TRIGGER["sell"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "SELL", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["sell"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["sell"]["last_ts"] = now

# ========== SR-gate / Auto-entry ==========# ========== SR auto-trade ==========

def compute_sr_thresholds(sym):
    data = candles(sym, "M1", 30)
    if len(data) < 5:
        return None
    window = data[-15:]
    support = min(d["low"] for d in window)
    resistance = max(d["high"] for d in window)
    rng = max(1e-6, resistance - support)
    mid = support + rng * 0.5
    top = resistance - rng * 0.10
    bottom = support + rng * 0.10
    buffer_buy = max(1e-6, support * SR_PRICE_BUFFER_PCT)
    buffer_sell = max(1e-6, resistance * SR_PRICE_BUFFER_PCT)
    return {
        "support": support,
        "resistance": resistance,
        "mid": mid,
        "top": top,
        "bottom": bottom,
        "buffer_buy": buffer_buy,
        "buffer_sell": buffer_sell,
    }

def sr_auto_trade(sym):
    global SR_STATE
    thresholds = compute_sr_thresholds(sym)
    if thresholds is None:
        SR_STATE = {"support": 0.0, "resistance": 0.0, "mid": 0.0, "top": 0.0, "bottom": 0.0}
        return
    SR_STATE = {
        "support": thresholds["support"],
        "resistance": thresholds["resistance"],
        "mid": thresholds["mid"],
        "top": thresholds["top"],
        "bottom": thresholds["bottom"],
    }
    t = tick(sym)
    price = None
    if t:
        price = t.last if t.last > 0 else (t.bid or t.ask or None)
    now = time.time()
    open_cnt = open_count(sym)
    cooldown = STATE["cooldown"]

    def rearm(side):
        trig = SR_TRIGGER[side]
        if trig["armed"]:
            return
        if now - trig["last_ts"] < SR_MIN_GAP:
            return
        if cooldown or open_cnt != 0:
            return
        if price is None:
            trig["armed"] = True
            return
        if side == "buy":
            if price >= thresholds["bottom"] + thresholds["buffer_buy"]:
                trig["armed"] = True
        else:
            if price <= thresholds["top"] - thresholds["buffer_sell"]:
                trig["armed"] = True

    rearm("buy")
    rearm("sell")

    if price is None or open_cnt != 0 or cooldown:
        return

    lot_default = float(SETUP.get("sr_default_lot", 0.01) or 0.01)

    if SR_TRIGGER["buy"]["armed"] and price <= thresholds["bottom"] and now - SR_TRIGGER["buy"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "BUY", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["buy"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["buy"]["last_ts"] = now

    if SR_TRIGGER["sell"]["armed"] and price >= thresholds["top"] and now - SR_TRIGGER["sell"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "SELL", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["sell"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["sell"]["last_ts"] = now

# ========== SR-gate / Auto-entry ==========# ========== SR auto-trade ==========

def compute_sr_thresholds(sym):
    data = candles(sym, "M1", 30)
    if len(data) < 5:
        return None
    window = data[-15:]
    support = min(d["low"] for d in window)
    resistance = max(d["high"] for d in window)
    rng = max(1e-6, resistance - support)
    mid = support + rng * 0.5
    top = resistance - rng * 0.10
    bottom = support + rng * 0.10
    buffer_buy = max(1e-6, support * SR_PRICE_BUFFER_PCT)
    buffer_sell = max(1e-6, resistance * SR_PRICE_BUFFER_PCT)
    return {
        "support": support,
        "resistance": resistance,
        "mid": mid,
        "top": top,
        "bottom": bottom,
        "buffer_buy": buffer_buy,
        "buffer_sell": buffer_sell,
    }

def sr_auto_trade(sym):
    global SR_STATE
    thresholds = compute_sr_thresholds(sym)
    if thresholds is None:
        SR_STATE = {"support": 0.0, "resistance": 0.0, "mid": 0.0, "top": 0.0, "bottom": 0.0}
        return
    SR_STATE = {
        "support": thresholds["support"],
        "resistance": thresholds["resistance"],
        "mid": thresholds["mid"],
        "top": thresholds["top"],
        "bottom": thresholds["bottom"],
    }
    t = tick(sym)
    price = None
    if t:
        price = t.last if t.last > 0 else (t.bid or t.ask or None)
    now = time.time()
    open_cnt = open_count(sym)
    cooldown = STATE["cooldown"]

    def rearm(side):
        trig = SR_TRIGGER[side]
        if trig["armed"]:
            return
        if now - trig["last_ts"] < SR_MIN_GAP:
            return
        if cooldown or open_cnt != 0:
            return
        if price is None:
            trig["armed"] = True
            return
        if side == "buy":
            if price >= thresholds["bottom"] + thresholds["buffer_buy"]:
                trig["armed"] = True
        else:
            if price <= thresholds["top"] - thresholds["buffer_sell"]:
                trig["armed"] = True

    rearm("buy")
    rearm("sell")

    if price is None or open_cnt != 0 or cooldown:
        return

    lot_default = float(SETUP.get("sr_default_lot", 0.01) or 0.01)

    if SR_TRIGGER["buy"]["armed"] and price <= thresholds["bottom"] and now - SR_TRIGGER["buy"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "BUY", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["buy"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["buy"]["last_ts"] = now

    if SR_TRIGGER["sell"]["armed"] and price >= thresholds["top"] and now - SR_TRIGGER["sell"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "SELL", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["sell"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["sell"]["last_ts"] = now

# ========== SR-gate / Auto-entry ==========
# ========== SR auto-trade ==========

def compute_sr_thresholds(sym):
    data = candles(sym, "M1", 30)
    if len(data) < 5:
        return None
    window = data[-15:]
    support = min(d["low"] for d in window)
    resistance = max(d["high"] for d in window)
    rng = max(1e-6, resistance - support)
    mid = support + rng * 0.5
    top = resistance - rng * 0.10
    bottom = support + rng * 0.10
    buffer_buy = max(1e-6, support * SR_PRICE_BUFFER_PCT)
    buffer_sell = max(1e-6, resistance * SR_PRICE_BUFFER_PCT)
    return {
        "support": support,
        "resistance": resistance,
        "mid": mid,
        "top": top,
        "bottom": bottom,
        "buffer_buy": buffer_buy,
        "buffer_sell": buffer_sell,
    }

def sr_auto_trade(sym):
    global SR_STATE
    thresholds = compute_sr_thresholds(sym)
    if thresholds is None:
        SR_STATE = {"support": 0.0, "resistance": 0.0, "mid": 0.0, "top": 0.0, "bottom": 0.0}
        return
    SR_STATE = {
        "support": thresholds["support"],
        "resistance": thresholds["resistance"],
        "mid": thresholds["mid"],
        "top": thresholds["top"],
        "bottom": thresholds["bottom"],
    }
    t = tick(sym)
    price = None
    if t:
        price = t.last if t.last > 0 else (t.bid or t.ask or None)
    now = time.time()
    open_cnt = open_count(sym)
    cooldown = STATE["cooldown"]

    def rearm(side):
        trig = SR_TRIGGER[side]
        if trig["armed"]:
            return
        if now - trig["last_ts"] < SR_MIN_GAP:
            return
        if cooldown or open_cnt != 0:
            return
        if price is None:
            trig["armed"] = True
            return
        if side == "buy":
            if price >= thresholds["bottom"] + thresholds["buffer_buy"]:
                trig["armed"] = True
        else:
            if price <= thresholds["top"] - thresholds["buffer_sell"]:
                trig["armed"] = True

    rearm("buy")
    rearm("sell")

    if price is None or open_cnt != 0 or cooldown:
        return

    lot_default = float(SETUP.get("sr_default_lot", 0.01) or 0.01)

    if SR_TRIGGER["buy"]["armed"] and price <= thresholds["bottom"] and now - SR_TRIGGER["buy"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "BUY", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["buy"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["buy"]["last_ts"] = now

    if SR_TRIGGER["sell"]["armed"] and price >= thresholds["top"] and now - SR_TRIGGER["sell"]["last_ts"] >= SR_MIN_GAP:
        STATE["locked"] = True
        ok, _ = order_send_with_fallback(sym, "SELL", lot_default)
        STATE["locked"] = False
        if ok:
            SR_TRIGGER["sell"] = {"armed": False, "last_ts": now}
            begin_session_if_needed(sym)
        else:
            SR_TRIGGER["sell"]["last_ts"] = now

# ========== SR-gate / Auto-entry ==========
def nearest_sr_pct(sym):
    data = candles(sym, "M1", 30)
    if len(data) < 5: return 1.0
    window = data[-15:]
    s = min(d["low"] for d in window)
    r = max(d["high"] for d in window)
    t = tick(sym)
    if not t: return 1.0
    price = t.last if t.last>0 else (t.bid or t.ask or 0.0)
    dist = min(abs(price - s), abs(r - price))
    baseline = max(1e-9, r - s)
    return dist / baseline if baseline>0 else 1.0

def sr_gate_ok(sym):
    if not SETUP["sr"]["auto_entry_enabled"]: return True
    return nearest_sr_pct(sym) <= SETUP["sr"]["near_pct"]

def order_send_with_fallback(sym, side, lot):
    if not symbol_ensure(sym): return (False, "symbol-not-visible")
    t = tick(sym)
    if not t: return (False, "no-tick")
    price = t.ask if side=="BUY" else t.bid
    otype = mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(lot),
        "type": otype, "price": price, "deviation": 50,
        "magic": 556677, "comment": "THB Indodam",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    with MTX:
        res = mt5.order_send(req)
    if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        return (True, f"OK:{res.retcode}")
    req["type_filling"] = mt5.ORDER_FILLING_FOK
    with MTX:
        res2 = mt5.order_send(req)
    ok = res2 and res2.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
    return (bool(ok), (res2.comment if res2 else "send-failed"))

def close_all(sym, reason="manual"):
    pos = positions(sym); n = 0
    label = reason.upper() if isinstance(reason, str) else str(reason)
    for p in pos:
        close_side = "SELL" if p.type==mt5.POSITION_TYPE_BUY else "BUY"
        t = tick(sym)
        if not t: continue
        price = (t.bid if close_side=="SELL" else t.ask)
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": sym,
            "volume": p.volume, "type": (mt5.ORDER_TYPE_SELL if close_side=="SELL" else mt5.ORDER_TYPE_BUY),
            "price": price, "deviation": 50, "magic": 556677, "comment": f"close-all:{reason}",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        with MTX:
            res = mt5.order_send(req)
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            CLOSE_REASON[int(p.ticket)] = label
            n += 1
    return n

# ========== Cooldown / Session / Auto ==========
def set_cooldown(sec):
    STATE["cooldown"] = True
    STATE["cooldown_until"] = time.time() + sec

def cooldown_tick():
    if STATE["cooldown"]:
        rem = max(0, STATE["cooldown_until"] - time.time())
        m,s = int(rem)//60, int(rem)%60
        STATE["timer"] = f"{m:02d}:{s:02d}"
        if rem<=0:
            STATE["cooldown"] = False; STATE["cooldown_until"] = 0
    else:
        STATE["timer"] = "00:00"

def begin_session_if_needed(sym):
    if not STATE["session_active"] and open_count(sym)>0:
        STATE["session_active"] = True
        STATE["session_start_ts"] = time.time()
        STATE["session_be_hit"] = False
        STATE["session_peak_pl"] = 0.0

def end_session():
    STATE["session_active"] = False
    STATE["session_start_ts"] = 0.0
    STATE["session_be_hit"] = False
    STATE["session_peak_pl"] = 0.0
    set_cooldown(15)

def try_break_event(sym):
    if not SETUP["abe_auto"]: return
    if open_count(sym) < SETUP["session"]["min_positions_for_be"]: return
    pl = float_pl(sym)
    if pl >= SETUP["session"]["be_min_profit"]:
        close_all(sym, "BREAKEVEN"); STATE["session_be_hit"] = True
        end_session(); set_cooldown(10)

def session_tick(sym):
    if not STATE["session_active"]: return
    elapsed = time.time() - STATE["session_start_ts"]
    pl = float_pl(sym)
    if pl > STATE["session_peak_pl"]: STATE["session_peak_pl"] = pl
    if pl >= SETUP["session"]["profit_target"]:
        close_all(sym, "SESSION-PROFIT"); end_session(); return
    if pl <= SETUP["session"]["loss_limit"]:
        close_all(sym, "SESSION-LOSS"); end_session(); return
    if elapsed >= SETUP["session"]["max_duration_sec"]:
        close_all(sym, "SESSION-TIMEOUT"); end_session(); return

def auto_m1_tick(sym):
    if not (SETUP["auto_mode"] and SETUP["auto_m1"]["enabled"]): return
    if STATE["locked"] or STATE["cooldown"] or open_count(sym)>0: return
    minute = int(time.time() // 60)
    if STATE["last_m1_minute"] == minute: return
    if time.time() - STATE["last_entry_ts"] < SETUP["auto_m1"]["min_wait_sec"]: return
    if not sr_gate_ok(sym):
        STATE["last_m1_minute"] = minute; return
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else "BUY")
    # kunci ringan agar engine tidak tabrakan dgn status
    STATE["locked"] = True
    ok, _ = order_send_with_fallback(sym, side, lot=0.01)
    STATE["locked"] = False
    if ok:
        STATE["last_m1_minute"] = minute
        STATE["last_entry_ts"] = time.time()
        begin_session_if_needed(sym)

# ========== Engine loop ==========
def engine_loop():
    nxt = time.time()
    while not stop_flag.is_set():
        cooldown_tick()
        try:
            if time.time() >= nxt:
                sym = SETUP["symbol"]
                auto_m1_tick(sym)
                try_break_event(sym)
                session_tick(sym)
                sr_auto_trade(sym)
                nxt = time.time() + 1.0
        except Exception as e:
            print("[ENGINE]", e, flush=True)
        time.sleep(0.01)

# ========== UI/Static ==========
@app.route("/", methods=["GET"])
def root():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/static/<path:path>", methods=["GET"])
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, "static"), path)

# ========== API ==========
def _status_payload_offline():
    return {
        "online": False, "locked": STATE["locked"], "mode": "SIDE",
        "auto_mode": SETUP["auto_mode"], "symbol": SETUP["symbol"],
        "price": 0.0, "tick_dir": 0, "equity": 0.0, "daily_pl": 0.0,
        "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
        "free_margin": 0.0,
        "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
        "vSL": 0.0, "best_pl": 0.0, "adds_done": 0, "timer": STATE["timer"],
        "total_lot": 0.0, "open_count": 0, "float_pl": 0.0,
        "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
        "open_positions": [], "history_today": [], "quotes": [],
        "sr_buy_armed": SR_TRIGGER["buy"]["armed"], "sr_sell_armed": SR_TRIGGER["sell"]["armed"],
        "sr_buy_last_ts": SR_TRIGGER["buy"]["last_ts"], "sr_sell_last_ts": SR_TRIGGER["sell"]["last_ts"],
        "sr_support": SR_STATE["support"], "sr_resistance": SR_STATE["resistance"],
        "sr_top": SR_STATE["top"], "sr_bottom": SR_STATE["bottom"], "sr_mid": SR_STATE["mid"]
    }

@app.route("/api/status", methods=["GET"])
def api_status():
    try:
        with MTX:
            ti = mt5.terminal_info()
        # Perbaikan: cek connected harus eksplisit True, bukan default True
        online = bool(ti) and bool(getattr(ti, "connected", False))
        if not online:
            STATUS_FAILS["count"] += 1
            if STATUS_FAILS["count"] <= 1:
                print("[/api/status] minor offline tolerance:", STATUS_FAILS["count"], flush=True)
            else:
                print("[/api/status] offline detected - attempting MT5 restart", flush=True)
                mt5_restart()
                STATUS_FAILS["count"] = 0
            return jsonify(_status_payload_offline())

        STATUS_FAILS["count"] = 0
        sym = SETUP["symbol"]
        symbol_ensure(sym)

        t  = tick(sym)
        ai = account_snapshot()
        digits = getattr(ai, 'currency_digits', 2) if ai else 2
        eq = float(getattr(ai, 'equity', 0.0) or 0.0)
        free = float(getattr(ai, 'margin_free', 0.0) or 0.0)
        oc = open_count(sym)
        tl = total_lot(sym)
        history, daily_pl_total = get_history_today()
        pl = float_pl(sym)

        price = 0.0; tick_dir = 0
        if t:
            price = (t.last if t.last>0 else (t.bid or t.ask or 0.0))
            diff  = (t.ask or 0.0) - (t.bid or 0.0)
            tick_dir = 1 if diff>0 else (-1 if diff<0 else 0)

        quotes = []
        for symq in SETUP["symbols"]:
            if not symbol_ensure(symq): continue
            tq = tick(symq)
            if tq:
                quotes.append({"symbol": symq, "bid": round(tq.bid or 0.0, 2), "ask": round(tq.ask or 0.0, 2)})

        return jsonify({
            "online": True, "locked": STATE["locked"], "mode": "SIDE",
            "auto_mode": SETUP["auto_mode"], "symbol": sym,
            "price": round(price,2), "tick_dir": tick_dir,
            "equity": round(eq, digits), "daily_pl": round(daily_pl_total, digits),
            "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
            "free_margin": round(free, digits),
            "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
            "vSL": 0.0, "best_pl": STATE["session_peak_pl"], "adds_done": 0, "timer": STATE["timer"],
            "total_lot": round(tl,2), "open_count": oc, "float_pl": round(pl, digits),
            "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
            "open_positions": [
                {
            "side": ("BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL"),
            "lot": p.volume, "entry": p.price_open,
            "pl": round(float(getattr(p, 'profit', 0.0) or 0.0), digits)
                } for p in positions(sym)
            ],
            "history_today": history,
            "quotes": quotes,
            "sr_buy_armed": SR_TRIGGER["buy"]["armed"], "sr_sell_armed": SR_TRIGGER["sell"]["armed"],
            "sr_buy_last_ts": SR_TRIGGER["buy"]["last_ts"], "sr_sell_last_ts": SR_TRIGGER["sell"]["last_ts"],
            "sr_support": SR_STATE["support"], "sr_resistance": SR_STATE["resistance"],
            "sr_top": SR_STATE["top"], "sr_bottom": SR_STATE["bottom"], "sr_mid": SR_STATE["mid"]
        })
    except Exception as e:
        # jangan 500 â€” selalu balas JSON aman
        print("[/api/status] EXC:", e, flush=True)
        STATUS_FAILS["count"] += 1
        return jsonify(_status_payload_offline())

@app.route("/api/candles", methods=["GET"])
def api_candles():
    sym = request.args.get("symbol", SETUP["symbol"])
    tf  = request.args.get("tf", "M1")
    cnt = int(request.args.get("count", 120))
    try:
        data = candles(sym, tf, cnt)
        return jsonify(data)
    except Exception as e:
        print("[/api/candles] EXC:", e, flush=True)
        return jsonify([])

@app.route("/api/symbol/select", methods=["POST"])
def api_symbol_select():
    sym = (request.get_json(force=True) or {}).get("symbol") or SETUP["symbol"]
    if sym not in SETUP["symbols"]:
        return jsonify({"ok": False, "msg": "symbol not allowed"}), 400
    SETUP["symbol"] = sym
    persist_save()
    symbol_ensure(sym)
    return jsonify({"ok": True})

@app.route("/api/strategy/toggle", methods=["POST"])
def api_toggle():
    SETUP["auto_mode"] = not SETUP["auto_mode"]
    persist_save()
    return jsonify({"ok": True, "auto_mode": SETUP["auto_mode"]})

@app.route("/api/strategy/tpsm", methods=["POST"])
def api_tpsm():
    SETUP["tpsm_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/tpsb", methods=["POST"])
def api_tpsb():
    SETUP["tpsb_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/abe", methods=["POST"])
def api_abe():
    SETUP["abe_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/action/buy", methods=["POST"])
def api_buy():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    trig = SR_TRIGGER["buy"]
    if (not trig["armed"] or trig.get("pending")) and (time.time() - trig["last_ts"] < SR_MIN_GAP or trig.get("pending")) and open_count(SETUP["symbol"]) == 0:
        return jsonify({"ok": False, "msg": "sr-buy-guard"})
    STATE["locked"] = True
    ok, msg = order_send_with_fallback(SETUP["symbol"], "BUY", lot)
    STATE["locked"] = False
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/sell", methods=["POST"])
def api_sell():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    trig = SR_TRIGGER["sell"]
    if (not trig["armed"] or trig.get("pending")) and (time.time() - trig["last_ts"] < SR_MIN_GAP or trig.get("pending")) and open_count(SETUP["symbol"]) == 0:
        return jsonify({"ok": False, "msg": "sr-sell-guard"})
    STATE["locked"] = True
    ok, msg = order_send_with_fallback(SETUP["symbol"], "SELL", lot)
    STATE["locked"] = False
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/add", methods=["POST"])
def api_add():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else "BUY")
    STATE["locked"] = True
    ok, msg = order_send_with_fallback(SETUP["symbol"], side, lot)
    STATE["locked"] = False
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/close", methods=["POST"])
def api_close():
    STATE["locked"] = True
    n = close_all(SETUP["symbol"], reason="manual")
    STATE["locked"] = False
    end_session(); persist_save()
    return jsonify({"ok": True, "closed": n})

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    sym = SETUP["symbol"]
    need = float(SETUP["session"]["be_min_profit"])
    if float_pl(sym) >= need and open_count(sym) >= SETUP["session"]["min_positions_for_be"]:
        STATE["locked"] = True
        close_all(sym, "BREAKEVEN")
        STATE["locked"] = False
        STATE["session_be_hit"] = True
        end_session(); set_cooldown(10)
        return jsonify({"ok": True, "action": "close-all"})
    return jsonify({"ok": False, "msg": "Belum memenuhi BE"})

@app.route("/api/diag", methods=["GET"])
def api_diag():
    with MTX:
        try: ti = mt5.terminal_info()
        except Exception as e: ti = f"EXC {e}"
        try: ai = mt5.account_info()
        except Exception as e: ai = f"EXC {e}"
        le = mt5.last_error()
    return jsonify({
        "terminal_info": str(ti),
        "account_info": str(ai),
        "last_error": str(le),
        "symbol": SETUP.get("symbol"),
        "env": {
            "MT5_PATH": CFG("MT5_PATH"),
            "MT5_LOGIN": CFG("MT5_LOGIN"),
            "MT5_PASSWORD": CFG("MT5_PASSWORD"),
            "MT5_SERVER": CFG("MT5_SERVER"),
            "MT5_SYMBOL": CFG("MT5_SYMBOL"),
        }
    })

# ========== Boot ==========
def boot():
    ok = mt5_init()
    print("[MT5] initialized =", ok, "| symbol:", SETUP["symbol"], flush=True)
    symbol_ensure(SETUP["symbol"])
    Thread(target=engine_loop, daemon=True).start()

if __name__ == "__main__":
    boot()
    app.run(host="0.0.0.0", port=5000, debug=False)





