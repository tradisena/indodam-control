# control.py
# Backend THT Indodam (Flask + MT5 attach tanpa kredensial)
# Menyajikan UI (index.html) dan API real-time untuk UI mobile.
#
# Endpoint:
#   GET  /                    -> index.html
#   GET  /index.html          -> index.html
#   GET  /<static files>      -> file statik jika ada; non-API fallback ke index.html
#   GET  /api/status
#   GET  /api/candles?symbol=...&tf=M1&count=120
#   POST /api/strategy/toggle
#   POST /api/strategy/tpsm  {on: bool}
#   POST /api/strategy/tpsb  {on: bool}
#   POST /api/symbol/select  {symbol: "XAUUSDC"...}
#   POST /api/action/buy|sell|add|breakeven|close  ({lot: 0.01})

from __future__ import annotations
import os, threading, time, math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from flask import Flask, request, jsonify, send_from_directory, abort

# ===== Try import MetaTrader5 =====
MT5_OK = False
try:
    import MetaTrader5 as mt5
    MT5_OK = True
except Exception:
    MT5_OK = False

import pandas as pd
pd.options.mode.chained_assignment = None

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ========= Static routes (serve UI) =========
@app.route("/")
def serve_root():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/index.html")
def serve_index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/<path:path>")
def serve_static(path):
    # Jangan ganggu API
    if path.startswith("api/"):
        abort(404)
    fp = os.path.join(BASE_DIR, path)
    if os.path.isfile(fp):
        return send_from_directory(BASE_DIR, path)
    # Fallback SPA: rute non-API diarahkan ke index.html
    return send_from_directory(BASE_DIR, "index.html")

@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "404 Not Found", "path": request.path}), 404
    return send_from_directory(BASE_DIR, "index.html")

# ========= Config =========
@dataclass
class Config:
    # default watchlist (akan di-resolve ke varian broker, mis. XAUUSDc)
    symbols: List[str] = field(default_factory=lambda: ["XAUUSDC", "BTCUSDC"])
    symbol: str = "XAUUSDC"

    # timeframe bars
    bars_m1: int = 180
    bars_m5: int = 240

    # MA params
    p_ema9: int = 9
    p_ma20: int = 20
    p_ema50: int = 50

    # SR window (menit M1)
    sr_window: int = 15

    # Guard
    spread_max_points: float = 250.0  # ~points
    loop_sec: float = 1.0

    # Daily guard
    daily_target: float = 10.0
    daily_min: float = -10.0

    # TPSM (MAX) longgar
    tpsm_be_mult: float = 0.5
    tpsm_step_profit: float = 0.5
    tpsm_step_vsl: float = 0.25

    # TPSB (THIN) ketat
    tpsb_be_mult: float = 0.25
    tpsb_step_profit: float = 0.25
    tpsb_step_vsl: float = 0.25

    # Opsional: path terminal MT5 (tanpa kredensial)
    terminal_path: Optional[str] = None  # contoh: r"C:\Program Files\MetaTrader 5\terminal64.exe"

cfg = Config()

# ========= State =========
@dataclass
class State:
    online: bool = False
    auto_mode: bool = True
    mode: str = "SIDE"      # UP / DOWN / SIDE
    tpsm_auto: bool = False
    tpsb_auto: bool = False

    symbol: str = cfg.symbol
    price: float = 0.0
    equity: float = 0.0
    free: float = 0.0
    spread: float = 0.0
    ping: int = 0

    support_m1: Optional[float] = None
    resistance_m1: Optional[float] = None

    open_positions: List[Dict] = field(default_factory=list)
    open_count: int = 0
    total_lot: float = 0.0
    float_pl: float = 0.0

    vsl: float = 0.0            # virtual SL agregat
    best_pl: float = 0.0
    timer_start: Optional[datetime] = None
    timer: str = "00:00"
    adds_done: int = 0

    cooldown: bool = False
    cooldown_until: Optional[datetime] = None

    daily_pl: float = 0.0
    last_reset_day: datetime.date = field(default_factory=lambda: datetime.now(timezone.utc).date())

    history_today: List[Dict] = field(default_factory=list)
    quotes: List[Dict] = field(default_factory=list)

S = State()

# ========= MT5 attach / adapters =========
def mt5_init() -> bool:
    """Attach ke terminal MT5 aktif tanpa kredensial."""
    if not MT5_OK:
        return False
    try:
        ok = mt5.initialize(path=cfg.terminal_path) if cfg.terminal_path else mt5.initialize()
        if not ok:
            return False
        return mt5.terminal_info() is not None
    except Exception:
        return False

def resolve_symbol_like(name: str) -> str:
    """Resolve symbol ke varian broker (XAUUSDC → XAUUSDc, dst)."""
    if not S.online:
        return name
    try:
        info = mt5.symbol_info(name)
        if info:
            mt5.symbol_select(name, True)
            return name
        for mask in (name+"*", name.replace("USDC","USD")+"*", name.replace("USD","USDC")+"*"):
            candidates = mt5.symbols_get(mask)
            if candidates:
                sym = candidates[0].name
                mt5.symbol_select(sym, True)
                return sym
    except Exception:
        pass
    return name

def br_tick(symbol: str) -> Optional[Dict]:
    if not S.online:
        return None
    try:
        t = mt5.symbol_info_tick(symbol)
        return {"bid": t.bid, "ask": t.ask} if t else None
    except Exception:
        return None

def br_spread_points(symbol: str) -> float:
    t = br_tick(symbol)
    if not t: return 0.0
    return abs(t["ask"] - t["bid"]) * 100.0

def br_account() -> Dict:
    if not S.online:
        return {"equity": 0.0, "margin_free": 0.0}
    a = mt5.account_info()
    return {"equity": float(a.equity), "margin_free": float(a.margin_free)} if a else {"equity":0.0,"margin_free":0.0}

TF_MAP = {}
if MT5_OK:
    TF_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
    }

def br_rates(symbol: str, tf: str, count: int) -> Optional[pd.DataFrame]:
    if not S.online:
        return None
    try:
        rates = mt5.copy_rates_from_pos(symbol, TF_MAP.get(tf, mt5.TIMEFRAME_M1), 0, count)
        if rates is None:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.rename(columns={"open":"open","high":"high","low":"low","close":"close"}, inplace=True)
        return df[["time","open","high","low","close"]]
    except Exception:
        return None

def br_positions(symbol: str):
    if not S.online:
        return []
    try:
        poss = mt5.positions_get(symbol=symbol)
        return poss or []
    except Exception:
        return []

def br_market_buy(symbol: str, lot: float) -> bool:
    if not S.online:
        return False
    try:
        t = mt5.symbol_info_tick(symbol);  price = t.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 80,
            "magic": 8824,
            "comment": "THTIndodam",
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        r = mt5.order_send(req)
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
    except Exception:
        return False

def br_market_sell(symbol: str, lot: float) -> bool:
    if not S.online:
        return False
    try:
        t = mt5.symbol_info_tick(symbol);  price = t.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 80,
            "magic": 8824,
            "comment": "THTIndodam",
            "type_filling": mt5.Order_FILLING_FOK if hasattr(mt5, "Order_FILLING_FOK") else mt5.ORDER_FILLING_FOK,
        }
        r = mt5.order_send(req)
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
    except Exception:
        return False

def br_close_all(symbol: str) -> bool:
    poss = br_positions(symbol)
    ok_all = True
    for p in poss:
        try:
            is_buy = (p.type == 0)  # 0 buy, 1 sell
            t = mt5.symbol_info_tick(symbol)
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": t.bid if is_buy else t.ask,
                "deviation": 80,
                "magic": 8824,
                "comment": "THTIndodam close",
            }
            r = mt5.order_send(req)
            ok_all = ok_all and (r is not None and r.retcode == mt5.TRADE_RETCODE_DONE)
        except Exception:
            ok_all = False
    return ok_all

# ========= Indicators / helpers =========
def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def sma(s: pd.Series, p: int) -> pd.Series:
    return s.rolling(p).mean()

def last_closed_idx(df: pd.DataFrame) -> int:
    return -2 if len(df) >= 2 else -1

def fmt_timer(dt: Optional[datetime]) -> str:
    if not dt: return "00:00"
    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    m, s = divmod(max(0, sec), 60)
    return f"{m:02d}:{s:02d}"

def add_mas(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty: return None
    c = df["close"]
    df["EMA9"]  = ema(c, cfg.p_ema9)
    df["MA20"]  = sma(c, cfg.p_ma20)
    df["EMA50"] = ema(c, cfg.p_ema50)
    return df

def trend_from_m5(df5: Optional[pd.DataFrame]) -> str:
    if df5 is None or len(df5) < 60: return "SIDE"
    i = last_closed_idx(df5); j = i-1
    m9, m20, m50 = df5.loc[df5.index[i], ["EMA9","MA20","EMA50"]]
    s9  = df5["EMA9"].iloc[i] - df5["EMA9"].iloc[j]
    s20 = df5["MA20"].iloc[i] - df5["MA20"].iloc[j]
    if (m9 > m20 > m50) and (s9>0 and s20>0):   return "UP"
    if (m9 < m20 < m50) and (s9<0 and s20<0):   return "DOWN"
    return "SIDE"

def sr_from_m1(df1: Optional[pd.DataFrame]) -> tuple[Optional[float], Optional[float]]:
    if df1 is None or len(df1) < 20: return (None, None)
    i = last_closed_idx(df1)
    w = cfg.sr_window
    lows  = df1["low"].iloc[i-w+1: i+1]
    highs = df1["high"].iloc[i-w+1: i+1]
    return (float(lows.min()), float(highs.max()))

def atr_proxy_from_m1(df1: Optional[pd.DataFrame]) -> float:
    if df1 is None or len(df1) < 2: return 1e-6
    tr = (df1["high"] - df1["low"]).rolling(14).mean()
    val = float(tr.iloc[last_closed_idx(df1)]) if len(tr)>14 else float((df1["high"]-df1["low"]).tail(14).mean())
    return max(1e-6, val)

# ========= Worker loop =========
lock = threading.Lock()

def reset_if_new_day():
    d = datetime.now(timezone.utc).date()
    if d != S.last_reset_day:
        S.last_reset_day = d
        S.daily_pl = 0.0
        S.history_today.clear()

def stop_open_locked() -> bool:
    return (S.daily_pl >= cfg.daily_target) or (S.daily_pl <= cfg.daily_min)

def update_status():
    # quotes (real from MT5)
    q = []
    if S.online:
        for sym in cfg.symbols:
            real = resolve_symbol_like(sym)
            t = br_tick(real)
            if t:
                q.append({"symbol": real, "bid": round(t["bid"], 2), "ask": round(t["ask"], 2)})
    S.quotes = q

    # account & price & spread
    acc = br_account()
    S.equity = acc["equity"]
    S.free   = acc["margin_free"]

    t = br_tick(S.symbol)
    if t:
        S.price = (t["bid"] + t["ask"]) / 2.0
        S.spread = abs(t["ask"] - t["bid"]) * 100.0

    # dataframes
    df1 = br_rates(S.symbol, "M1", cfg.bars_m1)
    df5 = add_mas(br_rates(S.symbol, "M5", cfg.bars_m5))
    S.mode = trend_from_m5(df5)
    S.support_m1, S.resistance_m1 = sr_from_m1(df1)

    # positions
    poss = br_positions(S.symbol)
    opens = []
    total_lot = 0.0
    float_pl  = 0.0
    for p in poss:
        side = "BUY" if p.type == 0 else "SELL"
        opens.append({
            "ticket": int(getattr(p, "ticket", 0)),
            "side": side,
            "lot": float(p.volume),
            "entry": float(p.price_open),
            "pl": float(p.profit),
        })
        total_lot += float(p.volume)
        float_pl  += float(p.profit)
    S.open_positions = opens
    S.open_count = len(opens)
    S.total_lot = total_lot
    S.float_pl  = float_pl

    # timer & reset vSL when flat
    if S.open_count > 0 and not S.timer_start:
        S.timer_start = datetime.now(timezone.utc)
    if S.open_count == 0:
        S.timer_start = None
        S.vsl = 0.0
        S.best_pl = 0.0
    S.timer = fmt_timer(S.timer_start)

    # cooldown status
    if S.cooldown_until and datetime.now(timezone.utc) < S.cooldown_until:
        S.cooldown = True
    else:
        S.cooldown = False
        S.cooldown_until = None

def manage_tps():
    """Auto TPSM/TPSB: kelola vSL virtual (agregat total P/L) + auto close saat jatuh ke vSL."""
    if not S.online or S.open_count == 0:
        return
    df1 = br_rates(S.symbol, "M1", cfg.bars_m1)
    rng = atr_proxy_from_m1(df1)

    # pilih profil
    if S.tpsb_auto:
        be_mult = cfg.tpsb_be_mult; step_p = cfg.tpsb_step_profit; step_v = cfg.tpsb_step_vsl
    elif S.tpsm_auto:
        be_mult = cfg.tpsm_be_mult; step_p = cfg.tpsm_step_profit; step_v = cfg.tpsm_step_vsl
    else:
        return

    pl = S.float_pl
    S.best_pl = max(S.best_pl, pl)

    be_trig = be_mult * rng
    if pl >= be_trig:
        steps = int((pl - be_trig) // (step_p * rng)) + 1
        target_vsl = steps * (step_v * rng)
        S.vsl = max(S.vsl, target_vsl)

    # auto-close saat P/L total jatuh ke vSL
    if S.vsl > 0 and pl <= S.vsl:
        before = pl
        ok = br_close_all(S.symbol)
        if ok:
            S.history_today.insert(0, {
                "symbol": S.symbol, "side": "MIX", "lot": round(S.total_lot,2),
                "start": 0, "close": 0, "profit": round(before,2),
                "time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S")
            })
            S.daily_pl += before
            S.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=60)

def worker():
    # init attach
    S.online = mt5_init()
    if S.online:
        S.symbol = resolve_symbol_like(S.symbol)
    while True:
        try:
            with lock:
                reset_if_new_day()
                if S.online:
                    update_status()
                    if S.auto_mode and not stop_open_locked():
                        manage_tps()
                else:
                    # coba reattach tiap loop jika offline
                    S.online = mt5_init()
                    if S.online:
                        S.symbol = resolve_symbol_like(S.symbol)
        except Exception:
            pass
        time.sleep(cfg.loop_sec)

# ========= API =========
@app.route("/api/status")
def api_status():
    with lock:
        data = {
            "online": S.online,
            "auto_mode": S.auto_mode,
            "mode": S.mode,
            "symbol": S.symbol,
            "price": S.price,
            "equity": S.equity,
            "free": S.free,
            "spread": round(S.spread,2),
            "ping": S.ping,

            "daily_pl": round(S.daily_pl,2),
            "daily_target": cfg.daily_target,
            "daily_min": cfg.daily_min,
            "locked": stop_open_locked(),

            "tpsm_auto": S.tpsm_auto,
            "tpsb_auto": S.tpsb_auto,
            "vsl": round(S.vsl,2),
            "best_pl": round(S.best_pl,2),
            "timer": S.timer,
            "adds_done": S.adds_done,
            "cooldown": S.cooldown,
            "cooldown_remain": int((S.cooldown_until - datetime.now(timezone.utc)).total_seconds()) if S.cooldown_until else 0,

            "support_m1": S.support_m1,
            "resistance_m1": S.resistance_m1,

            "open_positions": S.open_positions,
            "open_count": S.open_count,
            "total_lot": round(S.total_lot,2),
            "float_pl": round(S.float_pl,2),

            "history_today": S.history_today,
            "quotes": S.quotes,
        }
        return jsonify(data)

@app.route("/api/candles")
def api_candles():
    sym = request.args.get("symbol", S.symbol)
    tf  = request.args.get("tf", "M1")
    cnt = int(request.args.get("count", "120"))
    with lock:
        if not S.online:
            return jsonify([])
        sym = resolve_symbol_like(sym)
        df = br_rates(sym, tf, cnt)
        if df is None or df.empty:
            return jsonify([])
        out = [{"time": int(t.timestamp()), "open": float(o), "high": float(h),
                "low": float(l), "close": float(c)}
               for t,o,h,l,c in df[["time","open","high","low","close"]].itertuples(index=False, name=None)]
        return jsonify(out)

# ---- Strategy toggles ----
@app.route("/api/strategy/toggle", methods=["POST"])
def api_toggle():
    with lock:
        S.auto_mode = not S.auto_mode
    return jsonify({"ok": True, "auto_mode": S.auto_mode})

@app.route("/api/strategy/tpsm", methods=["POST"])
def api_tpsm():
    j = request.get_json(force=True)
    on = bool(j.get("on", True))
    with lock:
        S.tpsm_auto = on
        if on: S.tpsb_auto = False
    return jsonify({"ok": True, "tpsm_auto": S.tpsm_auto})

@app.route("/api/strategy/tpsb", methods=["POST"])
def api_tpsb():
    j = request.get_json(force=True)
    on = bool(j.get("on", False))
    with lock:
        S.tpsb_auto = on
        if on: S.tpsm_auto = False
    return jsonify({"ok": True, "tpsb_auto": S.tpsb_auto})

# ---- Symbol select ----
@app.route("/api/symbol/select", methods=["POST"])
def api_symbol():
    j = request.get_json(force=True)
    sym = j.get("symbol") or cfg.symbol
    with lock:
        if S.online:
            sym = resolve_symbol_like(sym)
        S.symbol = sym
    return jsonify({"ok": True, "symbol": S.symbol})

# ---- Trade actions ----
def _can_trade() -> (bool, str):
    if not S.online: return (False, "offline")
    if stop_open_locked(): return (False, "locked")
    if S.cooldown: return (False, "cooldown")
    if S.spread > cfg.spread_max_points: return (False, "spread")
    return (True, "")

@app.route("/api/action/buy", methods=["POST"])
def api_buy():
    j = request.get_json(force=True)
    lot = float(j.get("lot", 0.01))
    with lock:
        ok, reason = _can_trade()
        if not ok: return jsonify({"ok": False, "reason": reason})
        done = br_market_buy(S.symbol, lot)
        if done: S.timer_start = datetime.now(timezone.utc)
        return jsonify({"ok": bool(done)})

@app.route("/api/action/sell", methods=["POST"])
def api_sell():
    j = request.get_json(force=True)
    lot = float(j.get("lot", 0.01))
    with lock:
        ok, reason = _can_trade()
        if not ok: return jsonify({"ok": False, "reason": reason})
        done = br_market_sell(S.symbol, lot)
        if done: S.timer_start = datetime.now(timezone.utc)
        return jsonify({"ok": bool(done)})

@app.route("/api/action/add", methods=["POST"])
def api_add():
    j = request.get_json(force=True)
    lot = float(j.get("lot", 0.01))
    with lock:
        ok, reason = _can_trade()
        if not ok: return jsonify({"ok": False, "reason": reason})
        buys = sum(1 for p in S.open_positions if p["side"]=="BUY")
        sells= S.open_count - buys
        side_buy = (buys >= sells)
        done = br_market_buy(S.symbol, lot) if side_buy else br_market_sell(S.symbol, lot)
        if done:
            S.adds_done += 1
            S.timer_start = datetime.now(timezone.utc)
        return jsonify({"ok": bool(done)})

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    with lock:
        S.vsl = max(S.vsl, 0.01)  # dorong vSL minimal BE+
    return jsonify({"ok": True, "vsl": S.vsl})

@app.route("/api/action/close", methods=["POST"])
def api_close():
    with lock:
        before = S.float_pl
        done = br_close_all(S.symbol)
        if done and abs(before) > 0:
            S.history_today.insert(0, {
                "symbol": S.symbol, "side": "MIX", "lot": round(S.total_lot,2),
                "start": 0, "close": 0, "profit": round(before,2),
                "time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S")
            })
            S.daily_pl += before
            S.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=60)
        return jsonify({"ok": bool(done)})

# ========= main =========
if __name__ == "__main__":
    # start worker
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)