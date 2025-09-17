# control.py — THB Indodam (REAL MT5 + persist JSON)
# - Port 5000, serve index.html
# - Koneksi MetaTrader5 real (ENV override; default spt setup kamu)
# - Persistensi setup ke setup.json (tanpa DB)
# - Fitur: TPSM/TPSB/ABE, Auto M1 + SR-Gate 10%, Session target/loss/timeout, Cooldown
# - Endpoints: /, /api/status, /api/candles, /api/diag, /api/symbol/select,
#              /api/strategy/(toggle|tpsm|tpsb|abe), /api/action/(buy|sell|add|close|breakeven)

import os, json, time
from threading import Thread, Event
from flask import Flask, request, jsonify, send_from_directory
import MetaTrader5 as mt5  # pip install MetaTrader5

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PERSIST_FILE = os.path.join(BASE_DIR, "setup.json")

app = Flask(__name__)

# --------- DEFAULT ENV / PARAM ---------
DEFAULTS = {
    "MT5_PATH":    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    "MT5_LOGIN":   "263084911",
    "MT5_PASSWORD":"Lunas2025$$$",
    "MT5_SERVER":  "Exness-MT5Real37",
    "MT5_SYMBOL":  "XAUUSDc",
}
def CFG(k): return os.environ.get(k) or DEFAULTS.get(k) or ""

# --------- SETUP (persistable) ---------
SETUP = {
    "symbols": ["XAUUSDc", "BTCUSDc"],
    "symbol": CFG("MT5_SYMBOL"),
    "auto_mode": False,
    "tpsm_auto": False,
    "tpsb_auto": False,
    "abe_auto": False,
    "sr": {"auto_entry_enabled": True, "near_pct": 0.10, "baseline": "ADR14"},  # 10% gate
    "auto_m1": {"enabled": True, "min_wait_sec": 60},  # autotrade jika tak ada posisi
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
            with open(PERSIST_FILE,"r",encoding="utf-8") as f:
                data = json.load(f)
            # hanya kunci yang dikenal
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
        # jangan simpan kredensial ENV
        data = {
            k:(v if k!="symbols" else list(v))
            for k,v in SETUP.items()
        }
        with open(PERSIST_FILE,"w",encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[PERSIST] save EXC:", e, flush=True)

persist_load()

# --------- RUNTIME STATE ---------
STATE = {
    "locked": False,
    "timer": "00:00",
    "cooldown": False, "cooldown_until": 0.0,
    "last_entry_ts": 0.0,
    "last_m1_minute": None,
    "session_active": False, "session_start_ts": 0.0,
    "session_be_hit": False, "session_peak_pl": 0.0,
}
stop_flag = Event()

# --------- MT5 helpers ---------
def mt5_init():
    term = CFG("MT5_PATH")
    try:
        ok = mt5.initialize(term) if term and os.path.exists(term) else mt5.initialize()
    except Exception as e:
        print("[MT5] initialize EXC:", e, flush=True); return False
    if not ok:
        print("[MT5] init FAIL:", mt5.last_error(), flush=True); return False
    return maybe_login()

def maybe_login():
    ai = mt5.account_info()
    if ai is not None:
        print(f"[MT5] already logged-in: {ai.login}/{ai.server}", flush=True)
        return True
    login, pwd, server = CFG("MT5_LOGIN"), CFG("MT5_PASSWORD"), CFG("MT5_SERVER")
    if login and pwd and server:
        try:
            ok = mt5.login(int(login), password=pwd, server=server)
        except Exception as e:
            print("[MT5] login EXC:", e, flush=True); ok=False
        print("[MT5] login:", "OK" if ok else f"FAIL {mt5.last_error()}", flush=True)
        return ok
    print("[MT5] no credentials", flush=True)
    return False

def symbol_ensure(symbol):
    si = mt5.symbol_info(symbol)
    if si and si.visible: return True
    if si and not si.visible:
        mt5.symbol_select(symbol, True)
        si = mt5.symbol_info(symbol)
        if si and si.visible: return True
    # fallback wildcard
    base = symbol.rstrip(".")
    try: cands = mt5.symbols_get(f"{base}*") or []
    except: cands = []
    for c in cands:
        if c.visible or mt5.symbol_select(c.name, True):
            print(f"[SYMBOL] fallback -> {c.name}", flush=True)
            SETUP["symbol"] = c.name
            persist_save()
            return True
    print(f"[SYMBOL] not visible: {symbol}", flush=True); return False

def tick(sym):
    try: return mt5.symbol_info_tick(sym)
    except: return None

def positions(sym=None):
    try: return mt5.positions_get(symbol=sym) or [] if sym else (mt5.positions_get() or [])
    except: return []

def candles(sym, tf, count):
    tf_map = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1
    }
    timeframe = tf_map.get(tf, mt5.TIMEFRAME_M1)
    try: rates = mt5.copy_rates_from_pos(sym, timeframe, 0, count)
    except: rates = None
    out = []
    if rates is not None:
        for r in rates:
            out.append({"time": int(r['time']), "open": float(r['open']), "high": float(r['high']),
                        "low": float(r['low']), "close": float(r['close'])})
    return out

def account_snapshot():
    ai = mt5.account_info()
    if not ai: return 0.0, 0.0, 0.0
    return float(ai.equity), float(ai.margin), float(ai.margin_free)

def float_pl(sym):
    pos = positions(sym)
    t = tick(sym)
    if t is None: return 0.0
    # gunakan mid sesuai sisi
    last = (t.last if t.last>0 else 0.0)
    price_buy = t.ask or last
    price_sell = t.bid or last
    pl = 0.0
    for p in pos:
        if p.type == mt5.POSITION_TYPE_BUY:
            pl += (price_sell - p.price_open) * p.volume * p.contract_size
        else:
            pl += (p.price_open - price_buy) * p.volume * p.contract_size
    return float(pl)

def open_count(sym): return len(positions(sym))
def total_lot(sym):  return sum(p.volume for p in positions(sym))

# --------- SR gate / auto-entry ---------
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
    res = mt5.order_send(req)
    if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        return (True, f"OK:{res.retcode}")
    req["type_filling"] = mt5.ORDER_FILLING_FOK
    res2 = mt5.order_send(req)
    ok = res2 and res2.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
    return (bool(ok), (res2.comment if res2 else "send-failed"))

def close_all(sym, reason="manual"):
    pos = positions(sym); n = 0
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
        res = mt5.order_send(req)
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED): n += 1
    return n

# --------- cooldown / session / auto ---------
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
        close_all(sym, reason="break-event"); STATE["session_be_hit"] = True
        end_session(); set_cooldown(10)

def session_tick(sym):
    if not STATE["session_active"]: return
    elapsed = time.time() - STATE["session_start_ts"]
    pl = float_pl(sym)
    if pl > STATE["session_peak_pl"]: STATE["session_peak_pl"] = pl
    if pl >= SETUP["session"]["profit_target"]:
        close_all(sym, "session-profit"); end_session(); return
    if pl <= SETUP["session"]["loss_limit"]:
        close_all(sym, "session-loss"); end_session(); return
    if elapsed >= SETUP["session"]["max_duration_sec"]:
        close_all(sym, "session-timeout"); end_session(); return

def auto_m1_tick(sym):
    if not (SETUP["auto_mode"] and SETUP["auto_m1"]["enabled"]): return
    if STATE["locked"] or STATE["cooldown"] or open_count(sym)>0: return
    minute = int(time.time() // 60)
    if STATE["last_m1_minute"] == minute: return
    if time.time() - STATE["last_entry_ts"] < SETUP["auto_m1"]["min_wait_sec"]: return
    if not sr_gate_ok(sym):
        STATE["last_m1_minute"] = minute; return
    side = "BUY" if SETUP["tpsm_auto"] else ("SELL" if SETUP["tpsb_auto"] else "BUY")
    ok, _ = order_send_with_fallback(sym, side, lot=0.01)
    if ok:
        STATE["last_m1_minute"] = minute
        STATE["last_entry_ts"] = time.time()
        begin_session_if_needed(sym)

# --------- engine loop ---------
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

# --------- UI / static ---------
@app.route("/", methods=["GET"])
def root():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/static/<path:path>", methods=["GET"])
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, "static"), path)

# --------- API ---------
@app.route("/api/status", methods=["GET"])
def api_status():
    try: online = (mt5.terminal_info() is not None)
    except: online = False
    sym = SETUP["symbol"]
    if online and symbol_ensure(sym):
        t  = tick(sym)
        eq, margin, free = account_snapshot()
        oc = open_count(sym)
        tl = total_lot(sym)
        pl = float_pl(sym)
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
            "auto_mode": SETUP["auto_mode"], "symbol": sym,
            "price": round(price,2), "tick_dir": tick_dir,
            "equity": round(eq,2), "daily_pl": 0.00,
            "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
            "free_margin": round(free,2),
            "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
            "vSL": 0.0, "best_pl": STATE["session_peak_pl"], "adds_done": 0, "timer": STATE["timer"],
            "total_lot": round(tl,2), "open_count": oc, "float_pl": round(pl,2),
            "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
            "open_positions": [
                {
                    "side": ("BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL"),
                    "lot": p.volume, "entry": p.price_open,
                    "pl": ((price - p.price_open) if p.type==mt5.POSITION_TYPE_BUY else (p.price_open - price)) * p.volume * p.contract_size
                } for p in positions(sym)
            ],
            "history_today": [],
            "quotes": quotes
        })
    # offline
    return jsonify({
        "online": False, "locked": STATE["locked"], "mode": "SIDE",
        "auto_mode": SETUP["auto_mode"], "symbol": SETUP["symbol"],
        "price": 0.0, "tick_dir": 0, "equity": 0.0, "daily_pl": 0.0,
        "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
        "free_margin": 0.0,
        "tpsm_auto": SETUP["tpsm_auto"], "tpsb_auto": SETUP["tpsb_auto"], "abe_auto": SETUP["abe_auto"],
        "vSL": 0.0, "best_pl": 0.0, "adds_done": 0, "timer": STATE["timer"],
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
    end_session(); persist_save()
    return jsonify({"ok": True, "closed": n})

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    sym = SETUP["symbol"]
    need = float(SETUP["session"]["be_min_profit"])
    if float_pl(sym) >= need and open_count(sym) >= SETUP["session"]["min_positions_for_be"]:
        close_all(sym, "breakeven-button"); STATE["session_be_hit"] = True
        end_session(); set_cooldown(10)
        return jsonify({"ok": True, "action": "close-all"})
    return jsonify({"ok": False, "msg": "Belum memenuhi BE"})

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

# --------- boot ---------
def boot():
    ok = mt5_init()
    print("[MT5] initialized =", ok, "| symbol:", SETUP["symbol"], flush=True)
    symbol_ensure(SETUP["symbol"])
    Thread(target=engine_loop, daemon=True).start()

if __name__ == "__main__":
    boot()
    app.run(host="0.0.0.0", port=5000, debug=False)
