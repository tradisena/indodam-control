# control.py — THB Indodam (REAL MT5, no dummy)
# - Flask routes kompatibel (avoid 404): serve index.html di "/"
# - Fitur: Auto M1 per-menit, Break-Event basket, SR Gate, Session target, Cooldown
# - Data dari MetaTrader5: tick/quotes, candles, positions, equity/freemargin, order_send

from flask import Flask, request, jsonify, send_from_directory
from threading import Thread, Event
import os, time, datetime

import MetaTrader5 as mt5  # pip install MetaTrader5

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# ===================== SETUP (last-writer-wins) =====================
SETUP = {
    "symbol": os.environ.get("MT5_SYMBOL", "XAUUSDC"),  # sesuaikan nama simbol broker
    "auto_mode": False,
    "tpsm_auto": False,
    "tpsb_auto": False,
    "abe_auto": False,
    "sr": { "auto_entry_enabled": True, "near_pct": 0.10, "baseline": "ADR14" },
    "auto_m1": { "enabled": True, "min_wait_sec": 60 },
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

# ===================== STATE =====================
STATE = {
    "online": False, "locked": False,
    "price": 0.0, "tick_dir": 0,
    "equity": 0.0, "margin_used": 0.0,
    "timer": "00:00",
    "cooldown": False, "cooldown_until": 0.0,
    "history_today": [],           # summary dari closed deals (hari ini)
    "last_entry_ts": 0.0,
    "last_m1_minute": None,
    "session_active": False, "session_start_ts": 0.0,
    "session_be_hit": False, "session_peak_pl": 0.0
}

stop_flag = Event()

# ===================== MT5 helpers =====================
def mt5_init():
    ok = mt5.initialize()
    if not ok:
        return False
    return maybe_login()

def maybe_login():
    # Jika akun sudah login di terminal, ini akan True. Kalau perlu kredensial, gunakan ENV.
    acc = mt5.account_info()
    if acc is not None:
        return True
    login = os.environ.get("263084911")
    password = os.environ.get("Lunas2025$$$")
    server = os.environ.get("Exness-MT5Real37")
    if login and password and server:
        return mt5.login(int(login), password=password, server=server)
    return False

def symbol_ensure(symbol):
    si = mt5.symbol_info(symbol)
    if si is None:
        return False
    if not si.visible:
        mt5.symbol_select(symbol, True)
    return True

def tick(symbol):
    t = mt5.symbol_info_tick(symbol)
    return t

def account_snapshot():
    ai = mt5.account_info()
    if not ai: return 0.0, 0.0, 0.0
    return ai.equity, ai.margin, ai.margin_free

def positions(symbol=None):
    if symbol:
        return mt5.positions_get(symbol=symbol) or []
    return mt5.positions_get() or []

def deals_today(symbol=None):
    # Closed deals hari ini (UTC)
    utc_from = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    utc_to = datetime.datetime.utcnow()
    deals = mt5.history_deals_get(utc_from, utc_to)
    out = []
    if deals:
        for d in deals:
            if symbol and d.symbol != symbol: continue
            if d.entry == mt5.DEAL_ENTRY_OUT:  # closing
                out.append({
                    "symbol": d.symbol, "side": "BUY" if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL) and d.type==mt5.DEAL_TYPE_BUY else "SELL",
                    "lot": d.volume, "start": 0.0, "close": d.price, "profit": d.profit,
                    "time_utc": datetime.datetime.utcfromtimestamp(d.time).isoformat(timespec="seconds")+"Z"
                })
    return out

def candles(symbol, tf, count):
    tf_map = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1
    }
    timeframe = tf_map.get(tf, mt5.TIMEFRAME_M1)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    out = []
    if rates is not None:
        for r in rates:
            out.append({
                "time": int(r['time']),
                "open": float(r['open']), "high": float(r['high']),
                "low": float(r['low']), "close": float(r['close'])
            })
    return out

def float_pl(symbol):
    # floating P/L agregat dari posisi open symbol
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
    # disederhanakan (nilai broker bisa berbeda karena swaps/commission)
    return float(pl)

def open_count(symbol):
    return len(positions(symbol))

def total_lot(symbol):
    return sum(p.volume for p in positions(symbol))

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
    baseline = (r - s) if SETUP["sr"]["baseline"]=="DAY_RANGE" else max(1e-9, (r - s))
    return dist / baseline if baseline>0 else 1.0

def sr_gate_ok(symbol):
    if not SETUP["sr"]["auto_entry_enabled"]:
        return True
    pct = nearest_sr_pct(symbol)
    ok = (pct <= SETUP["sr"]["near_pct"])
    print(f"[SR-GATE] pct={pct:.3f} thr={SETUP['sr']['near_pct']:.3f} -> {'PASS' if ok else 'DENY'}", flush=True)
    return ok

def market_order(symbol, side, lot):
    if not symbol_ensure(symbol): return False, "symbol-not-visible"
    t = tick(symbol)
    if not t: return False, "no-tick"
    price = t.ask if side=="BUY" else t.bid
    order_type = mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 556677,
        "comment": "THB Indodam",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    ok = res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
    print(f"[ORDER] {side} {lot} @{price} -> {res.retcode if res else 'ERR'}", flush=True)
    return bool(ok), (res.comment if res else "send-failed")

def close_all(symbol, reason="manual"):
    pos = positions(symbol)
    n = 0
    for p in pos:
        side = "SELL" if p.type==mt5.POSITION_TYPE_BUY else "BUY"
        price = (tick(symbol).bid if side=="SELL" else tick(symbol).ask)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": p.ticket,
            "symbol": symbol,
            "volume": p.volume,
            "type": (mt5.ORDER_TYPE_SELL if side=="SELL" else mt5.ORDER_TYPE_BUY),
            "price": price,
            "deviation": 20,
            "magic": 556677,
            "comment": f"close-all:{reason}",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            n += 1
    print(f"[CLOSE-ALL] {n} positions | reason={reason}", flush=True)
    return n

# ===================== Cooldown & Session =====================
def set_cooldown(sec, reason="general"):
    STATE["cooldown"] = True
    STATE["cooldown_until"] = time.time() + sec
    print(f"[COOLDOWN] {sec}s by {reason}", flush=True)

def cooldown_tick():
    if STATE["cooldown"]:
        rem = STATE["cooldown_until"] - time.time()
        if rem <= 0:
            STATE["cooldown"] = False
            STATE["cooldown_until"] = 0
            rem = 0
        STATE["timer"] = fmt_mmss(rem=max(0,int(rem)))
    else:
        STATE["timer"] = "00:00"

def fmt_mmss(rem): m=rem//60; s=rem%60; return f"{int(m):02d}:{int(s):02d}"

def begin_session_if_needed(symbol):
    if not STATE["session_active"] and open_count(symbol)>0:
        STATE["session_active"] = True
        STATE["session_start_ts"] = time.time()
        STATE["session_be_hit"] = False
        STATE["session_peak_pl"] = 0.0

def end_session(reason="ended"):
    STATE["session_active"] = False
    STATE["session_start_ts"] = 0.0
    STATE["session_be_hit"] = False
    STATE["session_peak_pl"] = 0.0
    set_cooldown(15, reason="session")
    print(f"[SESSION] Ended: {reason}", flush=True)

# ===================== Auto / BE =====================
def try_break_event(symbol):
    if not SETUP["abe_auto"]: return
    if open_count(symbol) < SETUP["session"]["min_positions_for_be"]: return
    pl = float_pl(symbol)
    if pl >= SETUP["session"]["be_min_profit"]:
        close_all(symbol, reason="break-event")
        STATE["session_be_hit"] = True
        end_session("BE-hit")
        set_cooldown(10, "BE")

def session_tick(symbol):
    if not STATE["session_active"]: return
    elapsed = time.time() - STATE["session_start_ts"]
    pl = float_pl(symbol)
    if pl > STATE["session_peak_pl"]: STATE["session_peak_pl"] = pl

    if pl >= SETUP["session"]["profit_target"]:
        close_all(symbol, "session-profit"); end_session("profit-target"); return
    if pl <= SETUP["session"]["loss_limit"]:
        close_all(symbol, "session-loss"); end_session("loss-limit"); return
    if elapsed >= SETUP["session"]["max_duration_sec"]:
        close_all(symbol, "session-timeout"); end_session("timeout"); return

def auto_m1_tick(symbol):
    if not (SETUP["auto_mode"] and SETUP["auto_m1"]["enabled"]): return
    if STATE["locked"] or STATE["cooldown"] or open_count(symbol)>0: return
    minute = int(time.time() // 60)
    if STATE["last_m1_minute"] == minute: return
    if time.time() - STATE["last_entry_ts"] < SETUP["auto_m1"]["min_wait_sec"]: return
    if not sr_gate_ok(symbol):
        STATE["last_m1_minute"] = minute; return
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else ("BUY"))
    ok, msg = market_order(symbol, side, lot=0.01)
    if ok:
        STATE["last_m1_minute"] = minute
        STATE["last_entry_ts"] = time.time()
        begin_session_if_needed(symbol)

# ===================== Engine loop =====================
def engine_loop():
    price_next = logic_next = time.time()
    while not stop_flag.is_set():
        cooldown_tick()
        try:
            if time.time() >= logic_next:
                auto_m1_tick(SETUP["symbol"])
                try_break_event(SETUP["symbol"])
                session_tick(SETUP["symbol"])
                logic_next = time.time() + 1.0
        except Exception as e:
            print("[ENGINE]", e, flush=True)
        time.sleep(0.01)

# ===================== UI/Static =====================
@app.route("/", methods=["GET"])
def root():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/static/<path:path>", methods=["GET"])
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, "static"), path)

# ===================== API =====================
@app.route("/api/status", methods=["GET"])
def api_status():
    symbol = SETUP["symbol"]
    online = False
    try:
        online = mt5.terminal_info() is not None
    except: online = False

    if online and symbol_ensure(symbol):
        t = tick(symbol)
        price = (t.last if t and t.last>0 else (t.bid if t else 0.0)) or 0.0
        tick_dir = 0
        if t:
            # perkiraan arah simple (tidak stateful tick sebelumnya)
            tick_dir = 1 if (t.ask - t.bid) > 0 else (-1 if (t.ask - t.bid) < 0 else 0)
        eq, margin, free = account_snapshot()
        oc = open_count(symbol)
        tl = total_lot(symbol)
        pl = float_pl(symbol)
        hist = deals_today(symbol)
        quotes = []
        if t:
            quotes.append({"symbol": symbol, "bid": round(t.bid or 0.0, 2), "ask": round(t.ask or 0.0, 2)})

        STATE["online"] = True
        STATE["price"] = round(price, 2)
        STATE["equity"] = eq
        STATE["margin_used"] = margin

        resp = {
            "online": True, "locked": STATE["locked"], "mode": "SIDE",
            "auto_mode": SETUP["auto_mode"], "symbol": symbol,
            "price": round(price,2), "tick_dir": tick_dir,
            "equity": round(eq,2), "daily_pl": 0.00,
            "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
            "free_margin": round(free,2),
            "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
            "vsl": 0.0, "best_pl": STATE["session_peak_pl"], "adds_done": 0, "timer": STATE["timer"],
            "total_lot": round(tl,2), "open_count": oc, "float_pl": round(pl,2),
            "cooldown": STATE["cooldown"],
            "cooldown_remain": STATE["timer"],
            "open_positions": [
                {
                    "side": ("BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL"),
                    "lot": p.volume, "entry": p.price_open,
                    "pl": ((price - p.price_open) if p.type==mt5.POSITION_TYPE_BUY else (p.price_open - price)) * p.volume * p.contract_size
                } for p in positions(symbol)
            ],
            "history_today": hist,
            "quotes": quotes
        }
        return jsonify(resp)

    # offline / gagal init
    STATE["online"] = False
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

@app.route("/api/strategy/sr", methods=["POST"])
def api_sr():
    body = request.get_json(force=True) or {}
    if "enabled" in body: SETUP["sr"]["auto_entry_enabled"] = bool(body["enabled"])
    if "near_pct" in body: SETUP["sr"]["near_pct"] = max(0.01, min(0.5, float(body["near_pct"])))
    if "baseline" in body: SETUP["sr"]["baseline"] = str(body["baseline"])
    return jsonify({"ok": True, "sr": SETUP["sr"]})

@app.route("/api/action/buy", methods=["POST"])
def api_buy():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    ok, msg = market_order(SETUP["symbol"], "BUY", lot)
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/sell", methods=["POST"])
def api_sell():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    ok, msg = market_order(SETUP["symbol"], "SELL", lot)
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/add", methods=["POST"])
def api_add():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    # Default: tambah sesuai mode; kalau tidak ada, ikut tick bias
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else "BUY")
    ok, msg = market_order(SETUP["symbol"], side, lot)
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/close", methods=["POST"])
def api_close():
    n = close_all(SETUP["symbol"], reason="manual")
    end_session("manual-close")
    return jsonify({"ok": True, "closed": n})

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    sym = SETUP["symbol"]
    need = float(SETUP["session"]["be_min_profit"])
    if float_pl(sym) >= need and open_count(sym) >= SETUP["session"]["min_positions_for_be"]:
        close_all(sym, "breakeven-button"); STATE["session_be_hit"] = True
        end_session("BE-button"); set_cooldown(10, "BE")
        return jsonify({"ok": True, "action": "close-all"})
    return jsonify({"ok": False, "msg": "Belum memenuhi BE"}), 200

# ===================== Boot =====================
def boot():
    ok = mt5_init()
    print("[MT5] initialized=", ok, flush=True)
    symbol_ensure(SETUP["symbol"])
    th = Thread(target=engine_loop, daemon=True); th.start()
    return th

if __name__ == "__main__":
    boot()
    app.run(host="0.0.0.0", port=5000, debug=False)