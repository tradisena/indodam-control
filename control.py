# control.py — THB Indodam (NIC-N + memory, no DB)
# Compat fix: gunakan @app.route(..., methods=[...]) + static serving + scheduler stabil

from flask import Flask, request, jsonify, send_from_directory
from threading import Thread, Event
import time, random, math, datetime, os

app = Flask(__name__)

# ===================== SETUP (last-writer-wins) =====================
SETUP = {
    "symbol": "XAUUSDC",
    "auto_mode": False,
    "tpsm_auto": False,
    "tpsb_auto": False,
    "abe_auto": False,              # Auto Break-Event basket close
    "sr": {
        "auto_entry_enabled": True, # [SR-GATE] master switch
        "near_pct": 0.10,           # [SR-GATE] ganti batas persentase di sini
        "baseline": "ADR14"         # "ADR14" | "DAY_RANGE"
    },
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

# ===================== STATE =====================
STATE = {
    "online": True, "locked": False,
    "price": 0.0, "tick_dir": 0,
    "equity": 1000.00, "margin_used": 0.0,
    "vsl": 0.0, "best_pl": 0.0, "adds_done": 0,
    "timer": "00:00",
    "cooldown": False, "cooldown_until": 0.0,
    "history_today": [],
    "open_positions": [],
    "last_entry_ts": 0.0,
    "last_m1_minute": None,
    # session
    "session_active": False, "session_start_ts": 0.0,
    "session_be_hit": False, "session_peak_pl": 0.0
}

# ===================== Helpers =====================
def now_ts(): return time.time()
def fmt_mmss(rem):
    rem = max(0, int(rem)); m=rem//60; s=rem%60
    return f"{m:02d}:{s:02d}"
def free_margin(): return max(0.0, STATE["equity"] - STATE["margin_used"])
def open_count(): return len(STATE["open_positions"])
def total_lot(): return sum(p["lot"] for p in STATE["open_positions"])

def float_pl_agg():
    pl = 0.0
    for p in STATE["open_positions"]:
        diff = (STATE["price"] - p["entry"])
        pl += diff * (1 if p["side"] == "BUY" else -1) * 100  # skala simulasi
    return round(pl, 2)

def begin_session_if_needed():
    if not STATE["session_active"] and open_count() > 0:
        STATE["session_active"] = True
        STATE["session_start_ts"] = now_ts()
        STATE["session_be_hit"] = False
        STATE["session_peak_pl"] = 0.0

def end_session(reason="ended"):
    STATE["session_active"] = False
    STATE["session_start_ts"] = 0.0
    STATE["session_be_hit"] = False
    STATE["session_peak_pl"] = 0.0
    set_cooldown(15, reason="session")  # jeda pendek setelah sesi
    print(f"[SESSION] Ended by: {reason}", flush=True)

def set_cooldown(sec:int, reason="general"):
    STATE["cooldown"] = True
    STATE["cooldown_until"] = now_ts() + sec
    print(f"[COOLDOWN] {sec}s by {reason}", flush=True)

def cooldown_tick():
    if STATE["cooldown"]:
        rem = STATE["cooldown_until"] - now_ts()
        if rem <= 0:
            STATE["cooldown"] = False
            STATE["cooldown_until"] = 0
            rem = 0
        STATE["timer"] = fmt_mmss(rem)
    else:
        STATE["timer"] = "00:00"

# ===================== Market sim (gantikan dgn MT5) =====================
def simulate_price():
    last = STATE["price"] or 2000.0
    step = random.uniform(-0.3, 0.3)
    newp = max(10.0, last + step)
    STATE["tick_dir"] = 1 if newp > last else (-1 if newp < last else 0)
    STATE["price"] = round(newp, 2)

def get_candles(symbol:str, tf:str, count:int):
    base = STATE["price"] or 2000.0
    out, nowi = [], int(time.time())
    for i in range(count):
        t = nowi - (count - i)*60
        o = base + math.sin((i%24)/24*2*math.pi)*0.8 + random.uniform(-0.3,0.3)
        c = o + random.uniform(-0.6,0.6)
        h = max(o,c) + random.uniform(0,0.4)
        l = min(o,c) - random.uniform(0,0.4)
        out.append({"time": t, "open": round(o,2), "high": round(h,2),
                    "low": round(l,2), "close": round(c,2)})
        base = c
    return out

# ===================== Orders (gantikan dgn MT5) =====================
def place_order(side:str, lot:float):
    if STATE["locked"] or STATE["cooldown"]:
        return False, "locked/cooldown"
    entry = STATE["price"]
    STATE["open_positions"].append({"side": side, "lot": lot, "entry": entry, "time_open": now_ts()})
    STATE["margin_used"] += lot * 50.0
    STATE["last_entry_ts"] = now_ts()
    begin_session_if_needed()
    print(f"[ORDER] {side} {lot} @ {entry}", flush=True)
    return True, "ok"

def close_all_positions(reason="manual"):
    n = open_count()
    if n == 0: return 0
    pl_total = float_pl_agg()
    for p in STATE["open_positions"]:
        STATE["history_today"].append({
            "symbol": SETUP["symbol"], "side": p["side"], "lot": p["lot"],
            "start": round(p["entry"],2), "close": round(STATE["price"],2),
            "profit": round((STATE["price"]-p["entry"])*(1 if p["side"]=="BUY" else -1)*100,2),
            "time_utc": datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z"
        })
    STATE["equity"] += max(-1000, min(1000, pl_total))*0.01
    STATE["margin_used"] = 0.0
    STATE["open_positions"].clear()
    print(f"[CLOSE-ALL] {n} pos | reason={reason} | pl={pl_total}", flush=True)
    return n

# ===================== Signals & Gates =====================
def m1_signal():
    if SETUP["tpsm_auto"]: return "BUY"
    if SETUP["tpsb_auto"]: return "SELL"
    return "BUY" if random.random() > 0.5 else "SELL"

def nearest_sr_distance(price:float):
    win = get_candles(SETUP["symbol"], "M1", 15)
    s = min(d["low"] for d in win); r = max(d["high"] for d in win)
    dist = min(abs(price - s), abs(r - price))
    baseline = (r - s) if SETUP["sr"]["baseline"] == "DAY_RANGE" else max(0.01, (r - s))
    pct = dist / baseline if baseline > 0 else 1.0
    return dist, pct

def sr_gate_ok():
    if not SETUP["sr"]["auto_entry_enabled"]:
        return True, "disabled"
    dist, pct = nearest_sr_distance(STATE["price"])
    ok = (pct <= SETUP["sr"]["near_pct"])
    if not ok:
        print(f"[SR-GATE] deny (dist={dist:.2f}, pct={pct:.2f}, thr={SETUP['sr']['near_pct']:.2f})", flush=True)
    else:
        print(f"[SR-GATE] pass (pct={pct:.2f})", flush=True)
    return ok, "pass" if ok else "deny"

# ===================== Break-Event (basket close-all) =====================
def try_break_event():
    if not SETUP["abe_auto"]: return
    if open_count() < SETUP["session"]["min_positions_for_be"]: return
    pl = float_pl_agg()
    if pl >= SETUP["session"]["be_min_profit"]:
        close_all_positions(reason="break-event")
        STATE["session_be_hit"] = True
        end_session("BE-hit")
        set_cooldown(10, reason="BE")

# ===================== Session watcher =====================
def session_tick():
    if not STATE["session_active"]: return
    elapsed = now_ts() - STATE["session_start_ts"]
    pl = float_pl_agg()
    STATE["session_peak_pl"] = max(STATE["session_peak_pl"], pl)

    if pl >= SETUP["session"]["profit_target"]:
        close_all_positions(reason="session-profit"); end_session("profit-target"); return
    if pl <= SETUP["session"]["loss_limit"]:
        close_all_positions(reason="session-loss"); end_session("loss-limit"); return
    if elapsed >= SETUP["session"]["max_duration_sec"]:
        close_all_positions(reason="session-timeout"); end_session("timeout"); return

# ===================== Auto-entry M1 tiap menit =====================
def auto_m1_tick():
    if not (SETUP["auto_mode"] and SETUP["auto_m1"]["enabled"]): return
    if STATE["locked"] or STATE["cooldown"] or open_count()>0: return

    minute = int(time.time() // 60)
    if STATE["last_m1_minute"] == minute: return
    if now_ts() - STATE["last_entry_ts"] < SETUP["auto_m1"]["min_wait_sec"]: return

    ok, _ = sr_gate_ok()
    if not ok:
        STATE["last_m1_minute"] = minute  # tandai supaya tidak spam di menit ini
        return

    side = m1_signal()
    place_order(side, lot=0.01)
    STATE["last_m1_minute"] = minute

# ===================== Engine loop (scheduler stabil) =====================
stop_flag = Event()

def engine_loop():
    price_next = logic_next = time.time()
    while not stop_flag.is_set():
        now = time.time()

        # update harga ~2 Hz
        if now >= price_next:
            simulate_price()
            price_next = now + 0.5

        # housekeeping 10–20 Hz
        cooldown_tick()
        try_break_event()
        session_tick()

        # logic (auto M1) ~1 Hz — guard menit di dalam
        if now >= logic_next:
            auto_m1_tick()
            logic_next = now + 1.0

        time.sleep(0.01)

# ===================== STATIC / UI ROUTES =====================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

@app.route("/", methods=["GET"])
def root():
    # Pastikan index.html berada di direktori yang sama dengan control.py
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/static/<path:path>", methods=["GET"])
def static_files(path):
    # Jika kamu punya folder static/ untuk aset CSS/JS tambahan
    static_dir = os.path.join(BASE_DIR, "static")
    return send_from_directory(static_dir, path)

# ===================== API =====================
@app.route("/api/status", methods=["GET"])
def api_status():
    daily_pl = float(sum(h.get("profit", 0.0) for h in STATE.get("history_today", [])))
    resp = {
        "online": bool(STATE.get("online", False)),
        "locked": bool(STATE.get("locked", False)),
        "mode": "SIDE",
        "auto_mode": bool(SETUP.get("auto_mode", False)),

        "symbol": SETUP.get("symbol", "XAUUSDC"),
        "price": float(STATE.get("price", 0.0)),
        "tick_dir": int(STATE.get("tick_dir", 0)),

        "equity": round(float(STATE.get("equity", 0.0)),2),
        "daily_pl": round(daily_pl,2),
        "daily_target": float(SETUP.get("daily_target", 10.0)),
        "daily_min": float(SETUP.get("daily_min", -10.0)),

        "free_margin": round(free_margin(),2),

        "tpsm_auto": bool(SETUP.get("tpsm_auto", False)),
        "tpsb_auto": bool(SETUP.get("tpsb_auto", False)),
        "abe_auto":  bool(SETUP.get("abe_auto", False)),

        "vsl": float(STATE.get("vsl", 0.0)),
        "best_pl": float(STATE.get("session_peak_pl", 0.0)),
        "adds_done": int(STATE.get("adds_done", 0)),
        "timer": STATE.get("timer", "00:00"),

        "total_lot": round(total_lot(),2),
        "open_count": open_count(),
        "float_pl": float_pl_agg(),
        "cooldown": bool(STATE.get("cooldown", False)),
        "cooldown_remain": fmt_mmss(STATE.get("cooldown_until",0)-now_ts()) if STATE.get("cooldown") else "00:00",

        "open_positions": STATE.get("open_positions", []),
        "history_today": STATE.get("history_today", []),

        # quotes dummy
        "quotes": [
            {"symbol": SETUP.get("symbol","XAUUSDC"),
             "bid": round(float(STATE.get("price",0))-0.02,2),
             "ask": round(float(STATE.get("price",0))+0.02,2)}
        ]
    }
    return jsonify(resp)

@app.route("/api/candles", methods=["GET"])
def api_candles():
    sym = request.args.get("symbol", SETUP.get("symbol","XAUUSDC"))
    tf  = request.args.get("tf", "M1")
    cnt = int(request.args.get("count", 120))
    return jsonify(get_candles(sym, tf, cnt))

@app.route("/api/symbol/select", methods=["POST"])
def api_symbol_select():
    sym = (request.get_json(force=True) or {}).get("symbol") or SETUP["symbol"]
    SETUP["symbol"] = sym
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
    ok, msg = place_order("BUY", lot)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/sell", methods=["POST"])
def api_sell():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    ok, msg = place_order("SELL", lot)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/add", methods=["POST"])
def api_add():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    side = "BUY" if STATE.get("tick_dir",0) >= 0 else "SELL"
    ok, msg = place_order(side, lot)
    if ok: STATE["adds_done"] = int(STATE.get("adds_done",0)) + 1
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/close", methods=["POST"])
def api_close():
    n = close_all_positions(reason="manual")
    end_session("manual-close")
    return jsonify({"ok": True, "closed": n})

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    need = float(SETUP["session"]["be_min_profit"])
    pl = float_pl_agg()
    if pl >= need and open_count() >= SETUP["session"]["min_positions_for_be"]:
        close_all_positions(reason="breakeven-button")
        STATE["session_be_hit"] = True
        end_session("BE-button")
        set_cooldown(10, reason="BE")
        return jsonify({"ok": True, "action": "close-all", "pl": pl})
    return jsonify({"ok": False, "msg": f"Belum memenuhi BE (pl={pl:.2f} < {need:.2f} atau posisi kurang)"}), 200

# ===================== Boot =====================
stop_flag = Event()

def boot():
    STATE["price"] = 2000.0
    th = Thread(target=engine_loop, daemon=True); th.start()
    return th

if __name__ == "__main__":
    boot()
    # Bind ke semua interface; sesuaikan port dengan servermu
    app.run(host="0.0.0.0", port=5000, debug=False)
