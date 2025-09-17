# control.py — THB Indodam (REAL MT5) — final
# - Serve index.html at "/"
# - MT5 init+login real (default credential dari parametermu; bisa override ENV)
# - Whitelist simbol: XAUUSDc, BTCUSDc (pilih via UI /api/symbol/select)
# - Quotes multi-simbol, candles M1, order BUY/SELL (IOC → FOK), Close All, Breakeven
# - Auto M1 tick + session/breakeven/cooldown (ringkas, aman)
# - Endpoint diag /api/diag untuk cek cepat
# - Port 5000

from flask import Flask, request, jsonify, send_from_directory
from threading import Thread, Event
import os, time

import MetaTrader5 as mt5  # pip install MetaTrader5

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# ====== PARAMETER DEFAULT (bisa override via ENV) ======
DEFAULTS = {
    "MT5_PATH":   r"C:\Program Files\MetaTrader 5\terminal64.exe",
    "MT5_LOGIN":  "263084911",
    "MT5_PASSWORD":"Lunas2025$$$",
    "MT5_SERVER": "Exness-MT5Real37",
    "MT5_SYMBOL": "XAUUSDc",          # default aktif
}
def CFG(key):
    return os.environ.get(key) or DEFAULTS.get(key) or ""

# ====== SETUP & STATE ======
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
STATE = {
    "online": False, "locked": False,
    "timer": "00:00",
    "cooldown": False, "cooldown_until": 0.0,
    "last_entry_ts": 0.0,
    "last_m1_minute": None,
    "session_active": False, "session_start_ts": 0.0,
    "session_be_hit": False, "session_peak_pl": 0.0,
}
stop_flag = Event()

# ====== MT5 HELPERS ======
def mt5_init():
    path = CFG("MT5_PATH")
    try:
        ok = mt5.initialize(path) if path else mt5.initialize()
    except Exception as e:
        print("[MT5] initialize EXC:", e, flush=True)
        return False
    if not ok:
        print("[MT5] initialize FAIL:", mt5.last_error(), flush=True)
        return False
    return maybe_login()

def maybe_login():
    ai = mt5.account_info()
    if ai is not None:
        print(f"[MT5] already logged-in: {ai.login} / {ai.server}", flush=True)
        return True
    login = CFG("MT5_LOGIN")
    password = CFG("MT5_PASSWORD")
    server = CFG("MT5_SERVER")
    if not (login and password and server):
        print("[MT5] creds missing", flush=True)
        return False
    try:
        ok = mt5.login(int(login), password=password, server=server)
    except Exception as e:
        print("[MT5] login EXC:", e, flush=True)
        ok = False
    print("[MT5] login:", "OK" if ok else f"FAIL {mt5.last_error()}", flush=True)
    return ok

def symbol_ensure(symbol):
    si = mt5.symbol_info(symbol)
    if si and si.visible:
        return True
    if si and not si.visible:
        mt5.symbol_select(symbol, True)
        si = mt5.symbol_info(symbol)
        if si and si.visible:
            return True
    base = symbol.rstrip(".")
    try:
        cands = mt5.symbols_get(f"{base}*") or []
    except Exception:
        cands = []
    for c in cands:
        if c.visible or mt5.symbol_select(c.name, True):
            print(f"[SYMBOL] fallback -> {c.name}", flush=True)
            SETUP["symbol"] = c.name
            return True
    print(f"[SYMBOL] not visible: {symbol}", flush=True)
    return False

def tick(symbol):
    try:
        return mt5.symbol_info_tick(symbol)
    except: return None

def positions(symbol=None):
    try:
        if symbol: return mt5.positions_get(symbol=symbol) or []
        return mt5.positions_get() or []
    except: return []

def candles(symbol, tf, count):
    tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
              "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1}
    timeframe = tf_map.get(tf, mt5.TIMEFRAME_M1)
    try:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    except: rates = None
    out = []
    if rates is not None:
        for r in rates:
            out.append({
                "time": int(r['time']),
                "open": float(r['open']),
                "high": float(r['high']),
                "low":  float(r['low']),
                "close":float(r['close'])
            })
    return out

def account_snapshot():
    ai = mt5.account_info()
    if not ai: return 0.0, 0.0, 0.0
    return float(ai.equity), float(ai.margin), float(ai.margin_free)

def float_pl(symbol):
    pos = positions(symbol)
    t = tick(symbol)
    if t is None: return 0.0
    price_now = t.bid if any(p.type==mt5.POSITION_TYPE_SELL for p in pos) else t.ask
    pl = 0.0
    for p in pos:
        if p.type == mt5.POSITION_TYPE_BUY:
            pl += (price_now - p.price_open) * p.volume * p.contract_size
        else:
            pl += (p.price_open - price_now) * p.volume * p.contract_size
    return float(pl)

def open_count(symbol): return len(positions(symbol))
def total_lot(symbol):   return sum(p.volume for p in positions(symbol))

def nearest_sr_pct(symbol):
    data = candles(symbol, "M1", 30)
    if len(data) < 5: return 1.0
    lows = [d["low"] for d in data[-15:]]
    highs= [d["high"] for d in data[-15:]]
    s = min(lows); r = max(highs)
    t = tick(symbol)
    if not t: return 1.0
    price = t.last if t.last>0 else (t.bid or t.ask or 0.0)
    dist = min(abs(price - s), abs(r - price))
    baseline = max(1e-9, (r - s))
    return dist / baseline if baseline>0 else 1.0

def sr_gate_ok(symbol):
    if not SETUP["sr"]["auto_entry_enabled"]:
        return True
    pct = nearest_sr_pct(symbol)
    ok = (pct <= SETUP["sr"]["near_pct"])
    return ok

def order_send_with_fallback(symbol, side, lot):
    if not symbol_ensure(symbol): return (False, "symbol-not-visible")
    t = tick(symbol)
    if not t: return (False, "no-tick")
    price = t.ask if side=="BUY" else t.bid
    otype = mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL
    # IOC
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": otype,
        "price": price,
        "deviation": 50,
        "magic": 556677,
        "comment": "THB Indodam",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        return (True, f"OK:{res.retcode}")
    # FOK fallback
    req["type_filling"] = mt5.ORDER_FILLING_FOK
    res2 = mt5.order_send(req)
    ok = res2 and res2.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
    return (bool(ok), (res2.comment if res2 else "send-failed"))

def close_all(symbol, reason="manual"):
    pos = positions(symbol)
    n = 0
    for p in pos:
        side = "SELL" if p.type==mt5.POSITION_TYPE_BUY else "BUY"
        t = tick(symbol)
        if not t: continue
        price = (t.bid if side=="SELL" else t.ask)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": p.ticket,
            "symbol": symbol,
            "volume": p.volume,
            "type": (mt5.ORDER_TYPE_SELL if side=="SELL" else mt5.ORDER_TYPE_BUY),
            "price": price,
            "deviation": 50,
            "magic": 556677,
            "comment": f"close-all:{reason}",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            n += 1
    return n

# ====== COOLDOWN/SESSION/AUTO (ringkas) ======
def set_cooldown(sec):
    STATE["cooldown"] = True
    STATE["cooldown_until"] = time.time() + sec

def cooldown_tick():
    if STATE["cooldown"]:
        rem = max(0, STATE["cooldown_until"] - time.time())
        m,s = int(rem)//60, int(rem)%60
        STATE["timer"] = f"{m:02d}:{s:02d}"
        if rem<=0:
            STATE["cooldown"] = False
            STATE["cooldown_until"] = 0
    else:
        STATE["timer"] = "00:00"

def begin_session_if_needed(symbol):
    if not STATE["session_active"] and open_count(symbol)>0:
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

def try_break_event(symbol):
    if not SETUP["abe_auto"]: return
    if open_count(symbol) < SETUP["session"]["min_positions_for_be"]: return
    pl = float_pl(symbol)
    if pl >= SETUP["session"]["be_min_profit"]:
        close_all(symbol, reason="break-event")
        STATE["session_be_hit"] = True
        end_session()
        set_cooldown(10)

def session_tick(symbol):
    if not STATE["session_active"]: return
    elapsed = time.time() - STATE["session_start_ts"]
    pl = float_pl(symbol)
    if pl > STATE["session_peak_pl"]: STATE["session_peak_pl"] = pl
    if pl >= SETUP["session"]["profit_target"]:
        close_all(symbol, "session-profit"); end_session(); return
    if pl <= SETUP["session"]["loss_limit"]:
        close_all(symbol, "session-loss"); end_session(); return
    if elapsed >= SETUP["session"]["max_duration_sec"]:
        close_all(symbol, "session-timeout"); end_session(); return

def auto_m1_tick(symbol):
    if not (SETUP["auto_mode"] and SETUP["auto_m1"]["enabled"]): return
    if STATE["locked"] or STATE["cooldown"] or open_count(symbol)>0: return
    minute = int(time.time() // 60)
    if STATE["last_m1_minute"] == minute: return
    if time.time() - STATE["last_entry_ts"] < SETUP["auto_m1"]["min_wait_sec"]: return
    if not sr_gate_ok(symbol):
        STATE["last_m1_minute"] = minute; return
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else "BUY")
    ok, _ = order_send_with_fallback(symbol, side, lot=0.01)
    if ok:
        STATE["last_m1_minute"] = minute
        STATE["last_entry_ts"] = time.time()
        begin_session_if_needed(symbol)

# ====== ENGINE LOOP ======
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
                nxt = time.time() + 1.0
        except Exception as e:
            print("[ENGINE]", e, flush=True)
        time.sleep(0.01)

# ====== UI / STATIC ======
@app.route("/", methods=["GET"])
def root():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/static/<path:path>", methods=["GET"])
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, "static"), path)

# ====== API ======
@app.route("/api/status", methods=["GET"])
def api_status():
    # MT5 siap?
    online = False
    try: online = (mt5.terminal_info() is not None)
    except: online = False

    symbol = SETUP["symbol"]
    if online and symbol_ensure(symbol):
        t  = tick(symbol)
        eq, margin, free = account_snapshot()
        oc = open_count(symbol)
        tl = total_lot(symbol)
        pl = float_pl(symbol)
        price = 0.0; tick_dir = 0
        if t:
            price = (t.last if t.last>0 else (t.bid or t.ask or 0.0))
            tick_dir = 1 if (t.ask - t.bid) > 0 else (-1 if (t.ask - t.bid) < 0 else 0)

        quotes = []
        for symq in SETUP["symbols"]:
            if not symbol_ensure(symq): continue
            tq = tick(symq)
            if tq:
                quotes.append({"symbol": symq, "bid": round(tq.bid or 0.0, 2), "ask": round(tq.ask or 0.0, 2)})

        return jsonify({
            "online": True, "locked": STATE["locked"], "mode": "SIDE",
            "auto_mode": SETUP["auto_mode"], "symbol": symbol,
            "price": round(price,2), "tick_dir": tick_dir,
            "equity": round(eq,2), "daily_pl": 0.00,
            "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
            "free_margin": round(free,2),
            "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
            "vsl": 0.0, "best_pl": STATE["session_peak_pl"], "adds_done": 0, "timer": STATE["timer"],
            "total_lot": round(tl,2), "open_count": oc, "float_pl": round(pl,2),
            "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
            "open_positions": [
                {
                    "side": ("BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL"),
                    "lot": p.volume, "entry": p.price_open,
                    "pl": ((price - p.price_open) if p.type==mt5.POSITION_TYPE_BUY else (p.price_open - price)) * p.volume * p.contract_size
                } for p in positions(symbol)
            ],
            "history_today": [],
            "quotes": quotes
        })

    # OFFLINE
    return jsonify({
        "online": False, "locked": STATE["locked"], "mode": "SIDE",
        "auto_mode": SETUP["auto_mode"], "symbol": SETUP["symbol"],
        "price": 0.0, "tick_dir": 0, "equity": 0.0, "daily_pl": 0.0,
        "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
        "free_margin": 0.0,
        "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
        "vsl": 0.0, "best_pl": 0.0, "adds_done": 0, "timer": STATE["timer"],
        "total_lot": 0.0, "open_count": 0, "float_pl": 0.0,
        "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
        "open_positions": [], "history_today": [], "quotes": []
    })

@app.route("/api/candles", methods=["GET"])
def api_candles():
    sym = request.args.get("symbol", SETUP["symbol"])
    tf  = request.args.get("tf", "M1")
    cnt = int(request.args.get("count", 120))
    return jsonify(candles(sym, tf, cnt))

@app.route("/api/symbol/select", methods=["POST"])
def api_symbol_select():
    sym = (request.get_json(force=True) or {}).get("symbol") or SETUP["symbol"]
    if sym not in SETUP["symbols"]:
        return jsonify({"ok": False, "msg": "symbol not allowed"}), 400
    SETUP["symbol"] = sym
    symbol_ensure(sym)
    return jsonify({"ok": True})

@app.route("/api/strategy/toggle", methods=["POST"])
def api_toggle():
    SETUP["auto_mode"] = not SETUP["auto_mode"]
    return jsonify({"ok": True, "auto_mode": SETUP["auto_mode"]})

@app.route("/api/strategy/tpsm", methods=["POST"])
def api_tpsm():
    SETUP["tpsm_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    return jsonify({"ok": True})

@app.route("/api/strategy/tpsb", methods=["POST"])
def api_tpsb():
    SETUP["tpsb_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    return jsonify({"ok": True})

@app.route("/api/strategy/abe", methods=["POST"])
def api_abe():
    SETUP["abe_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    return jsonify({"ok": True})

@app.route("/api/action/buy", methods=["POST"])
def api_buy():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    ok, msg = order_send_with_fallback(SETUP["symbol"], "BUY", lot)
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/sell", methods=["POST"])
def api_sell():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    ok, msg = order_send_with_fallback(SETUP["symbol"], "SELL", lot)
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/add", methods=["POST"])
def api_add():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else "BUY")
    ok, msg = order_send_with_fallback(SETUP["symbol"], side, lot)
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/close", methods=["POST"])
def api_close():
    n = close_all(SETUP["symbol"], reason="manual")
    end_session()
    return jsonify({"ok": True, "closed": n})

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    sym = SETUP["symbol"]
    need = float(SETUP["session"]["be_min_profit"])
    if float_pl(sym) >= need and open_count(sym) >= SETUP["session"]["min_positions_for_be"]:
        close_all(sym, "breakeven-button"); STATE["session_be_hit"] = True
        end_session(); set_cooldown(10)
        return jsonify({"ok": True, "action": "close-all"})
    return jsonify({"ok": False, "msg": "Belum memenuhi BE"}), 200

@app.route("/api/diag", methods=["GET"])
def api_diag():
    try: ti = mt5.terminal_info()
    except Exception as e: ti = f"EXC {e}"
    try: ai = mt5.account_info()
    except Exception as e: ai = f"EXC {e}"
    return jsonify({
        "terminal_info": str(ti),
        "account_info": str(ai),
        "last_error": str(mt5.last_error()),
        "symbol": SETUP.get("symbol"),
        "env": {
            "MT5_PATH": CFG("MT5_PATH"),
            "MT5_LOGIN": CFG("MT5_LOGIN"),
            "MT5_SERVER": CFG("MT5_SERVER"),
            "MT5_SYMBOL": CFG("MT5_SYMBOL"),
        }
    })

# ====== BOOT ======
def boot():
    ok = mt5_init()
    print("[MT5] initialized =", ok, "| symbol target:", SETUP["symbol"], flush=True)
    symbol_ensure(SETUP["symbol"])
    th = Thread(target=engine_loop, daemon=True); th.start()
    return th

if __name__ == "__main__":
    boot()
    app.run(host="0.0.0.0", port=5000, debug=False)
