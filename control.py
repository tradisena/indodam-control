# control.py â€” THB Indodam (REAL MT5 + Persist JSON + Thread-Safe)
# Port 5000, UI: index.html di folder yang sama
# Fitur: TPSM/TPSB/ABE, Auto M1 (SR-Gate 10%), Session target/timeout, Cooldown
# Endpoints: /, /api/status, /api/candles, /api/diag, /api/symbol/select,
#            /api/strategy/(toggle|tpsm|tpsb|abe), /api/action/(buy|sell|add|close|breakeven)

import os, json, time, threading, ctypes, traceback
from datetime import datetime, timezone, timedelta
from threading import Thread, Event, RLock
from flask import Flask, request, jsonify, send_from_directory

# MetaTrader5 can be unavailable on some hosts (e.g., server without MT5).
# Make import safe so the web app still runs and returns offline status
try:
    import MetaTrader5 as mt5  # type: ignore
except Exception as e:  # ImportError or other runtime loading errors
    print("[MT5] import failed:", e, flush=True)
    mt5 = None  # sentinel; all MT5 calls must guard against this

BASE_DIR    = os.path.abspath(os.path.dirname(__file__))
PERSIST_FILE= os.path.join(BASE_DIR, "setup.json")

app = Flask(__name__)
stop_flag = Event()
MTX = RLock()                 # <â€” Kunci semua akses MT5
STATUS_FAILS = {"count": 0}   # <â€” Menahan OFFLINE jika 1x error sejenak

# ========== DEFAULT ENV ========== 
DEFAULTS = {
    "MT5_PATH":     r"C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "MT5_LOGIN":    "263084911", # ID Akun Master
    "MT5_PASSWORD": "500Juta25$$$",
    "MT5_SERVER":   "Exness-MT5Real20",
    "MT5_SYMBOL":   "XAUUSDc",
}
def CFG(k): return os.environ.get(k) or DEFAULTS.get(k) or ""


# ========== SETUP (persist) ========== 
SETUP = {
    "symbols": ["XAUUSDc", "BTCUSDc", "BTCUSDm", "XAUUSDm"],
    "symbol": CFG("MT5_SYMBOL"),
    "mt5_accounts": [],
    "active_mt5_login": None,
    "auto_mode": False,
    "sr_buy_enabled": True,
    "sr_sell_enabled": False,
    "cross_buy_enabled": False,
    "cross_sell_enabled": False,
    "tpsm_auto": True, # Default ON on restart
    "abe_auto": False,
    "auto_tpsb_enabled": False, # Default OFF on restart
    "trailing_stop_enabled": False,
    "trailing_stop_value": 2000.0,
    # XY coordinates for desktop auto-click (10 slots)
    "click_xy": [
        {"title": f"Close Trade {i+1:02d}", "func": "close_row", "x": 0, "y": 0, "target_neg_pct": 0.0, "target_pos_pct": 0.0}
        for i in range(10)
    ],
    "sr_config": {
        "active_mode": "mode_01",
        "modes": {
            "mode_01": { "name": "Auto SR 01", "candle_lookback": 15, "near_pct": 10.0 },
            "mode_02": { "name": "Auto SR 02", "candle_lookback": 30, "near_pct": 20.0 }
        }
    },
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

            # --- MIGRASI SR ke SR_CONFIG ---
            if 'sr' in data and 'sr_config' not in data:
                print("[PERSIST] Migrating legacy 'sr' settings to 'sr_config'...", flush=True)
                old_sr = data.get('sr', {})
                lookback = old_sr.get('candle_lookback', 15)
                near_pct = old_sr.get('near_pct', 10.0)
                data['sr_config'] = {
                    "active_mode": "mode_01",
                    "modes": {
                        "mode_01": { "name": "Auto SR 01", "candle_lookback": lookback, "near_pct": near_pct },
                        "mode_02": { "name": "Auto SR 02", "candle_lookback": 30, "near_pct": 20.0 }
                    }
                }
                del data['sr']

            # Lakukan migrasi data click_xy
            if 'click_xy' in data and isinstance(data['click_xy'], list):
                migrated_xy = []
                for i, item in enumerate(data['click_xy']):
                    if isinstance(item, dict) and ('title' not in item or 'func' not in item):
                        new_item = {
                            "title": f"Close Trade {i+1:02d}",
                            "func": "close_row",
                            "x": item.get("x", 0),
                            "y": item.get("y", 0)
                        }
                        item.update(new_item)
                        migrated_xy.append(item)
                    elif isinstance(item, dict):
                        migrated_xy.append(item)
                data['click_xy'] = migrated_xy
            
            # Update SETUP
            for k,v in data.items():
                if k in SETUP:
                    if isinstance(SETUP[k], dict) and isinstance(v, dict):
                        SETUP[k].update(v)
                    else:
                        SETUP[k] = v
        
        if not SETUP.get("mt5_accounts"):
            default_login = CFG("MT5_LOGIN")
            default_pass = CFG("MT5_PASSWORD")
            default_server = CFG("MT5_SERVER")
            if default_login and default_pass and default_server:
                SETUP["mt5_accounts"].append({
                    "alias": "Fahmi 500Juta",
                    "login": default_login,
                    "password": default_pass,
                    "server": default_server
                })
                if not SETUP.get("active_mt5_login"):
                    SETUP["active_mt5_login"] = default_login
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
RETRY_DELAY_SEC = 4  # Waktu (detik) sebelum mencoba klik ulang
MAX_RETRIES = 3      # Jumlah maksimal percobaan ulang

STATE = {
    "locked": False,
    "timer": "00:00",
    "cooldown": False, "cooldown_until": 0.0,
    "last_entry_ts": 0.0,
    "last_m1_minute": None,
    "cross_last_check_minute": None,
    "last_m5_toggle_ts": None, # Ditambahkan untuk logika auto-toggle M5
    "session_active": False, "session_start_ts": 0.0,
    "session_be_hit": False, "session_peak_pl": 0.0,
    "session_close_triggered": False,
    "last_system_message": None,
    "pending_open": None, # {side, ts, retries, x, y, reason}
    "failed_open": False,
    # --- Retry System State ---
    "pending_close": {},  # Format: { ticket: {ts, retries, x, y, reason} }
    "failed_close": set(),  # Format: { ticket1, ticket2 }
    "pl_trailing_peaks": {}, # { ticket: peak_pl_in_account_currency },
    "arah_posisi_terkunci": None, # None | "BUY" | "SELL",
    "last_cross_direction": None, # None | "UP" | "DOWN",
    "m5_locked_direction": None, # None | "BUY" | "SELL"
    "sr_original_near_pct": None # Untuk menyimpan nilai near_pct asli saat mode Jarak Lebar aktif
}

APP_VERSION = "1.4.2" # Version check for debugging

SR_TRIGGER = {
    "buy":  {"armed": True, "last_ts": 0.0, "pending": False, "last_trigger_candle_ts": 0.0, "last_trigger_level": 0.0},
    "sell": {"armed": True, "last_ts": 0.0, "pending": False, "last_trigger_candle_ts": 0.0, "last_trigger_level": 0.0}
}
SR_STATE = {
    "support": 0.0, "resistance": 0.0, "mid": 0.0,
    "top": 0.0, "bottom": 0.0
}
SR_MIN_GAP = 5.0
SR_PRICE_BUFFER_PCT = 0.0015

# State untuk mencegah trigger berulang pada posisi yang sama
TRIGGERED_TICKETS = set()

CLOSE_REASON = {}

# Determine supported filling modes for a symbol and return a preferred order list
def _filling_sequence_for_symbol(sym):
    """Return preferred filling modes for this broker/symbol. 
    For Exness XAUUSDc (from spec), allowed are IOC and FOK. We lock to these two only.
    We will prefer the symbol's declared filling_mode if it is IOC/FOK, then try the other.
    """
    modes = []
    if mt5 is None:
        return modes
    try:
        with MTX:
            si = mt5.symbol_info(sym)
        IOC = getattr(mt5, 'ORDER_FILLING_IOC', None)
        FOK = getattr(mt5, 'ORDER_FILLING_FOK', None)
        fm = getattr(si, 'filling_mode', None) if si else None
        # prefer broker's declared mode when it's IOC/FOK
        if fm in (IOC, FOK):
            modes.append(fm)
        # add the other allowed mode
        for m in (IOC, FOK):
            if m is not None and m not in modes:
                modes.append(m)
    except Exception:
        pass
    return [m for m in modes if m is not None]

def _get_active_account_creds():
    """Mendapatkan kredensial dari akun yang aktif di SETUP."""
    active_login = SETUP.get("active_mt5_login")
    accounts = SETUP.get("mt5_accounts", [])
    if active_login and accounts:
        for acc in accounts:
            if str(acc.get("login")) == str(active_login):
                # Mengembalikan salinan untuk menghindari modifikasi yang tidak disengaja
                return acc.copy()
    # Fallback ke ENV/DEFAULTS jika tidak ada akun aktif yang diset
    return {
        "login": CFG("MT5_LOGIN"),
        "password": CFG("MT5_PASSWORD"),
        "server": CFG("MT5_SERVER")
    }

# Helper to classify position open comment into a friendly label
def classify_open_exec(comment: str) -> str:
    c = str(comment or '').upper()
    if 'AUTO SR BUY' in c:
        return 'Auto SR Buy'
    if 'AUTO SR SELL' in c:
        return 'Auto SR Sell'
    if 'SR10' in c:
        return 'Auto SR'
    if 'AUTO60' in c:
        return 'Auto60s'
    if 'TPSM' in c:
        return 'TPSM'
    if 'TPSB' in c:
        return 'TPSB'
    if 'MANUAL' in c:
        return 'Manual'
    return 'Unknown'

# ========== MT5 helper (semua dibungkus MTX) ========== 
def mt5_init():
    if mt5 is None:
        print("[MT5] unavailable (import failed)", flush=True)
        return False
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
    if mt5 is None:
        return False
    with MTX:
        ai = mt5.account_info()
    if ai is not None:
        print(f"[MT5] already logged-in: {ai.login}/{ai.server}", flush=True)
        return True
    creds = _get_active_account_creds()
    login, pwd, server = creds.get("login"), creds.get("password"), creds.get("server")
    if not (login and pwd and server):
        print("[MT5] no credentials for active account", flush=True); return False
    with MTX:
        try:
            ok = mt5.login(int(login), password=str(pwd), server=str(server))
        except Exception as e:
            print("[MT5] login EXC:", e, flush=True); ok = False
    print("[MT5] login:", "OK" if ok else f"FAIL {mt5.last_error()}", flush=True)
    return ok
def mt5_restart():
    if mt5 is None:
        return False
    with MTX:
        try:
            mt5.shutdown()
        except Exception as e:
            print("[MT5] shutdown EXC:", e, flush=True)
    time.sleep(0.5)
    print("[MT5] restart triggered", flush=True)
    return mt5_init()

def symbol_ensure(symbol):
    if mt5 is None:
        return False
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
    if mt5 is None:
        return None
    with MTX:
        try:
            return mt5.symbol_info_tick(sym)
        except:
            return None

def positions(sym=None):
    if mt5 is None:
        return []
    with MTX:
        try:
            pos_tuple = mt5.positions_get(symbol=sym) if sym else mt5.positions_get()
            if pos_tuple is None:
                return [] # No positions found is a valid success case
            return list(pos_tuple)
        except Exception as e:
            print(f"[positions] Gagal mengambil data posisi: {e}", flush=True)
            return [] # Return empty list on error for consistency

def candles(sym, tf, count):
    if mt5 is None:
        return []
    tf_map = {
        "M1": getattr(mt5, "TIMEFRAME_M1", 1),
        "M5": getattr(mt5, "TIMEFRAME_M5", 5),
        "M15": getattr(mt5, "TIMEFRAME_M15", 15),
        "M30": getattr(mt5, "TIMEFRAME_M30", 30),
        "H1": getattr(mt5, "TIMEFRAME_H1", 60),
    }
    timeframe = tf_map.get(tf, tf_map["M1"])
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
    if mt5 is None:
        return None
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
    if mt5 is None:
        return [], 0.0
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
            # Tag exec_reason from entry comment (SR10/AUTO60/TPSM/TPSB) if present
            cmt = (getattr(d, 'comment', '') or '').upper()
            if 'SR10' in cmt:
                rec['exec'] = 'SR10'
            elif 'AUTO60' in cmt:
                rec['exec'] = 'AUTO60'
            elif 'TPSM' in cmt:
                rec['exec'] = 'TPSM'
            elif 'TPSB' in cmt:
                rec['exec'] = 'TPSB'
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
    return history[:300], total_pl

def price_in_trigger_zone(price, thresholds):
    """Checks if the price is in the upper or lower trigger zones."""
    if price is None or thresholds is None:
        return False
    # True if price is at or above the top line, or at or below the bottom line.
    return price >= thresholds['top'] or price <= thresholds['bottom']

# ========== SR-gate / Auto-entry ==========# ========== SR auto-trade ========== 
def compute_sr_thresholds(sym):
    # --- Gunakan struktur sr_config yang baru ---
    sr_conf = SETUP.get("sr_config", {})
    active_mode_key = sr_conf.get("active_mode", "mode_01")
    active_settings = sr_conf.get("modes", {}).get(active_mode_key, {})

    lookback = int(active_settings.get("candle_lookback", 15))
    near_pct_val = float(active_settings.get("near_pct", 10.0))
    # --- Akhir perubahan ---

    # Ambil lebih banyak data untuk memastikan lookback terpenuhi
    data = candles(sym, "M1", lookback + 15)
    if len(data) < lookback:
        return None, None
    
    last_candle_ts = data[-1]["time"]
    window = data[-lookback:]
    support = min(d["low"] for d in window)
    resistance = max(d["high"] for d in window)
    rng = max(1e-6, resistance - support)
    mid = support + rng * 0.5
    
    # Ambil nilai persentase dari setup dan konversi ke desimal
    trigger_pct = near_pct_val / 100.0
    top = resistance - rng * trigger_pct
    bottom = support + rng * trigger_pct
    buffer_buy = max(1e-6, support * SR_PRICE_BUFFER_PCT)
    buffer_sell = max(1e-6, resistance * SR_PRICE_BUFFER_PCT)
    
    thresholds = {
        "support": support,
        "resistance": resistance,
        "mid": mid,
        "top": top,
        "bottom": bottom,
        "buffer_buy": buffer_buy,
        "buffer_sell": buffer_sell,
    }
    return thresholds, last_candle_ts

def sr_auto_trade(sym):
    global SR_STATE

    # Cek jumlah total posisi terbuka untuk simbol ini
    open_pos = positions(sym) or []
    if len(open_pos) >= 4:
        return

    thresholds, current_candle_ts = compute_sr_thresholds(sym)
    if thresholds is None:
        SR_STATE = {"support": 0.0, "resistance": 0.0, "mid": 0.0, "top": 0.0, "bottom": 0.0}
        # reset arming so manual can still work safely
        for trig in SR_TRIGGER.values():
            trig["armed"] = True
            trig["pending"] = False
            trig["was_outside"] = False # Reset new state flag
        return
    SR_STATE = {
        "support": thresholds["support"],
        "resistance": thresholds["resistance"],
        "mid": thresholds["mid"],
        "top": thresholds["top"],
        "bottom": thresholds["bottom"],
    }
    # Only proceed with trading if auto mode is ON
    if not SETUP.get("auto_mode"):
        return
    t = tick(sym)
    # Find coordinates from setup for auto-click
    sr_buy_setup = next((item for item in SETUP.get("click_xy", []) if item.get("func") == "auto_sr_buy"), None)
    sr_sell_setup = next((item for item in SETUP.get("click_xy", []) if item.get("func") == "auto_sr_sell"), None)

    price = None
    if t:
        price = t.last if t.last > 0 else (t.bid or t.ask or None)
    now = time.time()
    cooldown = STATE["cooldown"]

    in_trigger_zone = price_in_trigger_zone(price, thresholds)

    # 'rearm' hanya jika harga keluar dari zona trigger
    for side in ("buy", "sell"):
        trig = SR_TRIGGER[side]
        # Hapus status 'pending' jika posisi berhasil terbuka
        if trig.get("pending") and open_count(sym) > 0:
            trig["pending"] = False
            print(f"[Auto SR {side.upper()}] Status 'pending' dihapus (posisi terdeteksi).", flush=True)

        # Re-arm jika harga ada di zona aman, tidak cooldown, dan jeda waktu terpenuhi
        if (not trig["armed"]) and (not trig.get("pending")):
            if (not in_trigger_zone) and (not cooldown) and (now - trig["last_ts"]) >= SR_MIN_GAP:
                trig["armed"] = True

    # Fire only if: armed AND not cooling
    if price is None or cooldown:
        return

    # Jangan proses jika sudah ada permintaan buka posisi yang sedang berjalan
    if STATE.get("pending_open"):
        return

    # --- NEW BOUNCE LOGIC ---
    buy_trig = SR_TRIGGER["buy"]
    sell_trig = SR_TRIGGER["sell"]

    # Initialize flags if they don't exist
    if "was_outside" not in buy_trig: buy_trig["was_outside"] = False
    if "was_outside" not in sell_trig: sell_trig["was_outside"] = False

    # --- Candle Lock: Cek jika sudah ada trade di candle M1 ini ---
    if current_candle_ts and (SR_TRIGGER["buy"]["last_trigger_candle_ts"] == current_candle_ts or SR_TRIGGER["sell"]["last_trigger_candle_ts"] == current_candle_ts):
        return

    # Determine current price position
    is_below_bottom = price <= thresholds["bottom"]
    is_above_top = price >= thresholds["top"]

    # --- BUY TRIGGER (Bounce Up from Support) ---
    # Condition: was outside (below) and now is inside (not below)
    if buy_trig.get("was_outside") and not is_below_bottom:
        if (sr_buy_setup and SETUP.get("sr_buy_enabled") and buy_trig["armed"] and 
            (not buy_trig.get("pending")) and (now - buy_trig["last_ts"]) >= SR_MIN_GAP):

            if STATE.get("arah_posisi_terkunci") and STATE.get("arah_posisi_terkunci") != "BUY":
                buy_trig["last_ts"] = now # Update timestamp to prevent rapid checks
            else:
                x, y = sr_buy_setup.get("x"), sr_buy_setup.get("y")
                if x and y and x > 0 and y > 0:
                    if STATE.get("arah_posisi_terkunci") is None:
                        STATE["arah_posisi_terkunci"] = "BUY"
                        print(f"[ARAH_POSISI] Posisi pertama. Arah trading dikunci ke 'BUY'.", flush=True)

                    reason = "Auto SR BUY"
                    print(f"[{reason}] Terpicu (Bounce). Menyiapkan untuk membuka posisi. Harga {price:.2f} > Bottom {thresholds['bottom']:.2f}", flush=True)
                    initial_pos_count = _get_open_count(sym)
                    STATE["pending_open"] = {"side": "BUY", "ts": 0, "retries": 0, "x": x, "y": y, "reason": reason, "initial_pos_count": initial_pos_count}
                    buy_trig.update({ "armed": False, "pending": True, "last_ts": now, "last_trigger_candle_ts": current_candle_ts })
                else:
                    message = f"Auto SR BUY diabaikan. Koordinat tidak valid untuk 'auto_sr_buy'."
                    print(f"[Auto SR BUY] {message}", flush=True)
                    STATE["last_system_message"] = {"text": message, "type": "warn"}
                    buy_trig["last_ts"] = now

    # --- SELL TRIGGER (Bounce Down from Resistance) ---
    # Condition: was outside (above) and now is inside (not above)
    elif sell_trig.get("was_outside") and not is_above_top:
        if (sr_sell_setup and SETUP.get("sr_sell_enabled") and sell_trig["armed"] and 
            (not sell_trig.get("pending")) and (now - sell_trig["last_ts"]) >= SR_MIN_GAP):
            
            if STATE.get("arah_posisi_terkunci") and STATE.get("arah_posisi_terkunci") != "SELL":
                sell_trig["last_ts"] = now # Update timestamp to prevent rapid checks
            else:
                x, y = sr_sell_setup.get("x"), sr_sell_setup.get("y")
                if x and y and x > 0 and y > 0:
                    if STATE.get("arah_posisi_terkunci") is None:
                        STATE["arah_posisi_terkunci"] = "SELL"
                        print(f"[ARAH_POSISI] Posisi pertama. Arah trading dikunci ke 'SELL'.", flush=True)
                    
                    reason = "Auto SR SELL"
                    print(f"[{reason}] Terpicu (Bounce). Menyiapkan untuk membuka posisi. Harga {price:.2f} < Top {thresholds['top']:.2f}", flush=True)
                    initial_pos_count = _get_open_count(sym)
                    STATE["pending_open"] = {"side": "SELL", "ts": 0, "retries": 0, "x": x, "y": y, "reason": reason, "initial_pos_count": initial_pos_count}
                    sell_trig.update({ "armed": False, "pending": True, "last_ts": now, "last_trigger_candle_ts": current_candle_ts })
                else:
                    message = f"Auto SR SELL diabaikan. Koordinat tidak valid untuk 'auto_sr_sell'."
                    print(f"[Auto SR SELL] {message}", flush=True)
                    STATE["last_system_message"] = {"text": message, "type": "warn"}
                    sell_trig["last_ts"] = now

    # --- Update state for the next tick ---
    buy_trig["was_outside"] = is_below_bottom
    sell_trig["was_outside"] = is_above_top



def order_send_with_fallback(sym, side, lot, reason=None):
    if mt5 is None:
        return (False, "mt5-unavailable")
    if not symbol_ensure(sym):
        return (False, "symbol-not-visible")
    t = tick(sym)
    if not t:
        return (False, "no-tick")
    price = t.ask if side == "BUY" else t.bid
    otype = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    comment = (reason or "THB Indodam")[:30]
    # Use magic=0 for true manual orders so brokers that disable EA/autotrading still allow manual trades
    is_manual = isinstance(reason, str) and reason.upper().startswith("MANUAL")
    magic_val = 0 if is_manual else 556677
    # Lock to IOC -> FOK only (per symbol spec)
    fill_seq = _filling_sequence_for_symbol(sym) or [getattr(mt5,'ORDER_FILLING_IOC',None), getattr(mt5,'ORDER_FILLING_FOK',None)]
    last_msg = 'send-failed'
    for fm in fill_seq:
        if fm is None: continue
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(lot),
            "type": otype,
            "price": price,
            "deviation": 200,
            "magic": magic_val,
            "comment": comment,
            "type_filling": fm,
        }
        with MTX:
            res = mt5.order_send(req)
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            return (True, f"OK:{res.retcode}")
        last_msg = getattr(res, 'comment', None) or last_msg
        # try next mode
    return (False, last_msg)

def close_all(sym, reason="manual"):
    if mt5 is None:
        return 0, []
    # Fetch all then filter to be robust across suffix/name variants
    pos_all = positions()
    pos = [p for p in pos_all if str(getattr(p, 'symbol', '')).upper() == str(sym).upper()]
    n = 0; fails = []
    label = reason.upper() if isinstance(reason, str) else str(reason)
    if not pos:
        return 0, [{"ticket": None, "ret": None, "msg": "no-position-for-symbol"}]
    # Ensure symbol visible/selected
    symbol_ensure(sym)
    for p in pos:
        close_side = "SELL" if p.type==mt5.POSITION_TYPE_BUY else "BUY"
        t = tick(sym)
        if not t:
            fails.append({"ticket": int(p.ticket), "ret": None, "msg": "no-tick"});
            continue
        price = (t.bid if close_side=="SELL" else t.ask)
        # Use magic=0 for manual-initiated close so it passes when EA trading is disabled
        magic_val = 0 if str(reason).lower() == "manual" else 556677
        ok = False
        for fm in _filling_sequence_for_symbol(sym):
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": sym,
                "volume": p.volume, "type": (mt5.ORDER_TYPE_SELL if close_side=="SELL" else mt5.ORDER_TYPE_BUY),
                "price": price, "deviation": 200, "magic": magic_val, "comment": f"close-all:{reason}",
                "type_filling": fm,
            }
            with MTX:
                res = mt5.order_send(req)
            ok = bool(res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED))
            if ok:
                break
        if not ok:
            fails.append({
                "ticket": int(p.ticket),
                "ret": (res.retcode if res else None),
                "msg": (getattr(res,'comment',None) or 'send-failed')
            })
        if ok:
            CLOSE_REASON[int(p.ticket)] = label
            n += 1
    return n, fails

# ========== Auto-Click Helpers (ctypes) ========== 
def _win_leftclick(x: int, y: int):
    """Send a left-click event to screen coordinates using ctypes."""
    try:
        # Constants for mouse_event
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        
        # Ensure coordinates are integers
        x = int(x)
        y = int(y)

        # Move cursor and click
        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.05) # short delay after moving
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05) # short delay between down and up
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception as e:
        print(f"[_win_leftclick] EXC: {e}", flush=True)

def _click_by_func(func_name: str, reason: str):
    """Finds a setup by function name and performs a click."""
    setup = next((item for item in SETUP.get("click_xy", []) if item.get("func") == func_name), None)
    if setup and setup.get("x") > 0 and setup.get("y") > 0:
        x, y = setup["x"], setup["y"]
        print(f"[{reason}] Triggered. Clicking '{func_name}' at ({x},{y})", flush=True)
        STATE["last_system_message"] = {"text": f"Aksi otomatis: {reason}", "type": "info"}
        _win_leftclick(x, y)
        return True
    else:
        print(f"[{reason}] Gagal. Tidak ditemukan setup valid untuk '{func_name}' di Kordinat XY.", flush=True)
        STATE["last_system_message"] = {"text": f"Aksi {reason} gagal: setup '{func_name}' tidak ditemukan.", "type": "warn"}
        return False

def _get_open_count(sym=None):
    """Get the number of open positions, optionally filtered by symbol."""
    try:
        # positions() is already thread-safe with MTX
        return len(positions(sym=sym) or [])
    except Exception:
        return 0

def _get_click_xy():
    """Get valid click coordinates from SETUP."""
    coords = SETUP.get("click_xy", [])
    if not isinstance(coords, list):
        return []
    
    valid_coords = []
    for item in coords:
        if isinstance(item, dict):
            # Hanya ambil koordinat yang fungsinya untuk 'close_row'
            if item.get("func") == "close_row":
                x, y = item.get("x"), item.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)) and x > 0 and y > 0:
                    valid_coords.append({"x": int(x), "y": int(y)})
    return valid_coords

def close_all_via_clicker(sym: str, delay_ms=300):
    """
    Klik tombol [X] Close sesuai JUMLAH posisi terbuka utk 'sym',
    urutan bottom->top (nilai Y terbesar lebih dulu).
    """
    try:
        # Pakai hitungan posisi real; jika ingin patokan UI nanti bisa kirim n via body
        open_n = _get_open_count(sym=sym)
        coords = _get_click_xy()
        if open_n <= 0:
            return True, 0, "Tidak ada posisi terbuka."
        if not coords:
            return False, 0, "Koordinat click_xy belum diset."

        # **Kunci perbaikan**: urutkan berdasar Y, terbesar (paling bawah) ke terkecil (paling atas)
        coords_sorted = sorted(coords, key=lambda c: int(c["y"]))
        n = min(open_n, len(coords_sorted))
        pick = list(reversed(coords_sorted[-n:]))  # bottom -> top

        print(f"[AUTOCLICK] CloseAll bottom->top, count={n}, sym={sym}", flush=True)
        for i, c in enumerate(pick, 1):
            x, y = int(c["x"])
            print(f"[AUTOCLICK] #{i}/{n} click at ({x},{y})", flush=True)
            _win_leftclick(x, y)
            time.sleep(max(0.05, delay_ms / 1000.0))

        try:
            end_session_no_cooldown()
            persist_save()
        except Exception:
            pass

        return True, n, f"Clicked {n} rows (bottom->top)."
    except Exception as e:
        print(f"[AUTOCLICK] EXC: {e}", flush=True)
        traceback.print_exc()
        return False, str(e)

# ========== Cooldown / Session / Auto ========== 
def set_cooldown(sec):
    STATE["cooldown"] = True
    STATE["cooldown_until"] = time.time() + sec

def cooldown_tick():
    """Updates the cooldown timer state. Called by its own dedicated worker."""
    if STATE["cooldown"]:
        rem = max(0, STATE["cooldown_until"] - time.time())
        m,s = int(rem)//60, int(rem)%60
        STATE["timer"] = f"{m:02d}:{s:02d}"
        if rem<=0:
            STATE["cooldown"] = False; STATE["cooldown_until"] = 0
    else:
        STATE["timer"] = "00:00"

def cooldown_worker():
    """A separate, lightweight thread to manage cooldown timer state. 
    This ensures the UI remains responsive even if the main engine loop is busy or blocked.
    """
    while not stop_flag.is_set():
        cooldown_tick()
        time.sleep(0.25) # Check 4 times a second, doesn't need to be faster.

def begin_session_if_needed(sym):
    if not STATE["session_active"] and open_count(sym)>0:
        STATE["session_active"] = True
        STATE["session_start_ts"] = time.time()
        STATE["session_be_hit"] = False
        STATE["session_peak_pl"] = 0.0
        STATE["session_close_triggered"] = False

def end_session():
    STATE["session_active"] = False
    STATE["session_start_ts"] = 0.0
    STATE["session_be_hit"] = False
    STATE["session_peak_pl"] = 0.0
    STATE["session_close_triggered"] = False
    set_cooldown(15)

def end_session_no_cooldown():
    STATE["session_active"] = False
    STATE["session_start_ts"] = 0.0
    STATE["session_be_hit"] = False
    STATE["session_peak_pl"] = 0.0
    STATE["session_close_triggered"] = False

def try_break_event(sym):
    if not SETUP["abe_auto"]: return
    # Check if we already hit BE in this session to prevent re-triggering
    if STATE.get("session_be_hit"): return
    if open_count(sym) < SETUP["session"]["min_positions_for_be"]: return
    pl = float_pl(sym)
    if pl >= SETUP["session"]["be_min_profit"]:
        # Use clicker instead of API
        if _click_by_func("auto_bep", "Auto BEP"):
            STATE["session_be_hit"] = True
            end_session(); set_cooldown(10)

def session_tick(sym):
    if not STATE["session_active"] or STATE.get("session_close_triggered"): return
    elapsed = time.time() - STATE["session_start_ts"]
    pl = float_pl(sym)
    if pl > STATE["session_peak_pl"]: STATE["session_peak_pl"] = pl

    reason = None
    if pl >= SETUP["session"]["profit_target"]:
        reason = "Session Profit"
    elif pl <= SETUP["session"]["loss_limit"]:
        reason = "Session Loss"
    elif elapsed >= SETUP["session"]["max_duration_sec"]:
        reason = "Session Timeout"

    if reason:
        STATE["session_close_triggered"] = True # Tandai bahwa penutupan sesi telah dipicu
        # Use clicker instead of API
        if _click_by_func("auto_close_all", reason):
            end_session()

def retry_and_verify_close_tick(sym):
    """
    Verify if positions queued for closing were successful.
    If not, retry clicking up to MAX_RETRIES.
    """
    if not STATE["pending_close"]:
        return

    open_pos_tickets = {p.ticket for p in positions(sym)}
    now = time.time()
    
    # Iterate over a copy as we might modify the dict
    for ticket, details in list(STATE["pending_close"].items()):
        # SUCCESS: Ticket is no longer in open positions
        if ticket not in open_pos_tickets:
            print(f"[RETRY_SYS] Close confirmed for ticket {ticket}", flush=True)
            STATE["last_system_message"] = {"text": f"Posisi #{ticket} berhasil ditutup.", "type": "success"}
            TRIGGERED_TICKETS.add(ticket) # Mark as permanently handled
            del STATE["pending_close"][ticket]
            if ticket in STATE["failed_close"]:
                STATE["failed_close"].remove(ticket)
            continue

        # FAILURE: Ticket is still open, check for retry
        # First click is done here, retries > 0 are subsequent retries
        time_since_last_try = now - details["ts"]
        
        # We check for retries > 0 because the first attempt (retries=0) should happen immediately
        if details["retries"] == 0 or time_since_last_try >= RETRY_DELAY_SEC:
            if details["retries"] < MAX_RETRIES:
                # Perform click
                _win_leftclick(details["x"], details["y"])
                
                # Update state
                details["retries"] += 1
                details["ts"] = now
                
                msg = ""
                if details["retries"] == 1: # First attempt
                    msg = f"Mencoba menutup via {details.get('reason', '')} (tiket: {ticket})..."
                    print(f"[RETRY_SYS] Attempting close for ticket {ticket} at ({details['x']},{details['y']})...", flush=True)
                else: # Retry attempts
                    msg = f"Retry ({details['retries']-1}/{MAX_RETRIES}) menutup tiket {ticket}..."
                    print(f"[RETRY_SYS] Retrying ({details['retries']-1}/{MAX_RETRIES}) close for ticket {ticket}...", flush=True)

                STATE["last_system_message"] = {"text": msg, "type": "info"}
            else:
                # Max retries reached
                print(f"[RETRY_SYS] MAX RETRIES reached for ticket {ticket}. Marking as FAILED.", flush=True)
                STATE["last_system_message"] = {"text": f"Gagal menutup tiket {ticket} setelah {MAX_RETRIES} percobaan.", "type": "error"}
                del STATE["pending_close"][ticket]
                STATE["failed_close"].add(ticket)

def retry_and_verify_open_tick(sym):
    """
    Verify if a position was opened after an auto-click trigger.
    If not, retry clicking up to MAX_RETRIES.
    """
    if not STATE.get("pending_open"):
        STATE["failed_open"] = False # Clear failure status when not pending
        return

    details = STATE["pending_open"]
    now = time.time()

    # SUCCESS: A new position has been opened
    if _get_open_count(sym) > details.get("initial_pos_count", -1):
        print(f"[{details['reason']}] Open confirmed.", flush=True)
        STATE["last_system_message"] = {"text": f"Posisi {details['side']} berhasil dibuka via {details['reason']}.", "type": "ok"}
        STATE["pending_open"] = None
        begin_session_if_needed(sym) # Start session after successful open
        return

    # FAILURE/RETRY LOGIC
    time_since_last_try = now - details["ts"]

    # First attempt is immediate (ts=0), subsequent retries have a delay
    if details["retries"] == 0 or time_since_last_try >= RETRY_DELAY_SEC:
        if details["retries"] < MAX_RETRIES:
            # Perform click
            _win_leftclick(details["x"], details["y"])
            
            # Update state
            details["retries"] += 1
            details["ts"] = now
            
            if details["retries"] == 1: # First attempt
                msg = f"Mencoba membuka {details['side']} via {details['reason']}..."
            else: # Retry attempts
                msg = f"Retry ({details['retries']-1}/{MAX_RETRIES-1}) membuka posisi {details['side']}..."
            
            print(f"[RETRY_SYS] {msg}", flush=True)
            STATE["last_system_message"] = {"text": msg, "type": "info"}
        else:
            # Max retries reached
            print(f"[RETRY_SYS] MAX RETRIES reached for opening {details['side']}. Marking as FAILED.", flush=True)
            STATE["last_system_message"] = {"text": f"Gagal membuka posisi {details['side']} setelah {MAX_RETRIES-1} percobaan.", "type": "error"}
            STATE["pending_open"] = None
            STATE["failed_open"] = True
            # Manually reset the SR trigger since the open failed
            side_key = details["side"].lower()
            if side_key in SR_TRIGGER:
                SR_TRIGGER[side_key]["pending"] = False
                # Saat gagal, jangan re-arm, biarkan logika re-arm standar yang berjalan
            set_cooldown(10) # Cooldown to prevent immediate re-triggering

def auto_tpsb_tick(sym):
    """Monitor open positions and queue for closing if P/L percentage target is met."""
    if not SETUP.get("auto_tpsb_enabled"):
        return

    current_tick = tick(sym)
    if not current_tick:
        return
    current_price = current_tick.last if current_tick.last > 0 else (current_tick.bid + current_tick.ask) / 2.0
    if current_price <= 0:
        return

    open_pos = positions(sym)
    open_pos.sort(key=lambda p: (p.profit or 0.0))

    if not open_pos:
        TRIGGERED_TICKETS.clear()
        STATE["pending_close"].clear()
        STATE["failed_close"].clear()
        return

    current_tickets = {p.ticket for p in open_pos}
    TRIGGERED_TICKETS.intersection_update(current_tickets)
    STATE["failed_close"].intersection_update(current_tickets)

    tpsb_setups = [s for s in SETUP.get("click_xy", []) if s.get("func") == "auto_tpsb"]

    for i, pos in enumerate(open_pos):
        if i >= len(tpsb_setups):
            break
        
        if pos.ticket in TRIGGERED_TICKETS or pos.ticket in STATE["pending_close"] or pos.ticket in STATE["failed_close"]:
            continue

        setup = tpsb_setups[i]
        entry_price = float(getattr(pos, 'price_open', 0.0))
        pos_type = getattr(pos, 'type', -1)

        if entry_price == 0 or pos_type == -1: continue

        pl_pct = 0.0
        if pos_type == 0: pl_pct = ((current_price - entry_price) / entry_price) * 100.0
        elif pos_type == 1: pl_pct = ((entry_price - current_price) / entry_price) * 100.0
        pl_pct = round(pl_pct, 3)

        neg_target_pct = float(setup.get("target_neg_pct", 0.0))
        pos_target_pct = float(setup.get("target_pos_pct", 0.0))

        should_trigger = (pos_target_pct > 0 and pl_pct >= pos_target_pct) or \
                         (neg_target_pct < 0 and pl_pct <= neg_target_pct)

        if should_trigger:
            x, y = int(setup.get("x", 0)), int(setup.get("y", 0))
            if x > 0 and y > 0:
                print(f"[AUTO_TPSB] Queued for close ticket {pos.ticket} at ({x},{y}). Price Move: {pl_pct:.4f}%", flush=True)
                STATE["pending_close"][pos.ticket] = {
                    "ts": time.time(), "retries": 0, "x": x, "y": y,
                    "reason": f"Auto TPSB baris #{i+1}"
                }
            else:
                msg = f"Auto TPSB baris #{i+1} target tercapai, tapi koordinat tidak valid (X:{x}, Y:{y})."
                print(f"[AUTO_TPSB] {msg}", flush=True)
                STATE["last_system_message"] = {"text": msg, "type": "warn"}

def auto_tpsm_tick(sym):
    """Monitor open positions and queue for closing if P/L percentage target is met for TPSM."""
    if not SETUP.get("tpsm_auto"):
        return

    current_tick = tick(sym)
    if not current_tick: return
    current_price = current_tick.last if current_tick.last > 0 else (current_tick.bid + current_tick.ask) / 2.0
    if current_price <= 0: return

    open_pos = positions(sym)
    open_pos.sort(key=lambda p: (p.profit or 0.0))

    if not open_pos:
        TRIGGERED_TICKETS.clear()
        STATE["pending_close"].clear()
        STATE["failed_close"].clear()
        return

    current_tickets = {p.ticket for p in open_pos}
    TRIGGERED_TICKETS.intersection_update(current_tickets)
    STATE["failed_close"].intersection_update(current_tickets)

    tpsm_setups = [s for s in SETUP.get("click_xy", []) if s.get("func") == "auto_tpsm"]

    for i, pos in enumerate(open_pos):
        if i >= len(tpsm_setups): break
        
        if pos.ticket in TRIGGERED_TICKETS or pos.ticket in STATE["pending_close"] or pos.ticket in STATE["failed_close"]:
            continue

        setup = tpsm_setups[i]
        entry_price = float(getattr(pos, 'price_open', 0.0))
        pos_type = getattr(pos, 'type', -1)

        if entry_price == 0 or pos_type == -1: continue

        pl_pct = 0.0
        if pos_type == 0: pl_pct = ((current_price - entry_price) / entry_price) * 100.0
        elif pos_type == 1: pl_pct = ((entry_price - current_price) / entry_price) * 100.0
        pl_pct = round(pl_pct, 3)

        neg_target_pct = float(setup.get("target_neg_pct", 0.0))
        pos_target_pct = float(setup.get("target_pos_pct", 0.0))

        should_trigger = (pos_target_pct > 0 and pl_pct >= pos_target_pct) or \
                         (neg_target_pct < 0 and pl_pct <= neg_target_pct)

        if should_trigger:
            x, y = int(setup.get("x", 0)), int(setup.get("y", 0))
            if x > 0 and y > 0:
                print(f"[AUTO_TPSM] Queued for close ticket {pos.ticket} at ({x},{y}). Price Move: {pl_pct:.4f}%", flush=True)
                STATE["pending_close"].update({pos.ticket: {
                    "ts": time.time(), "retries": 0, "x": x, "y": y,
                    "reason": f"Auto TPSM baris #{i+1}"
                }})
            else:
                msg = f"Auto TPSM baris #{i+1} target tercapai, tapi koordinat tidak valid (X:{x}, Y:{y})."
                print(f"[AUTO_TPSM] {msg}", flush=True)
                STATE["last_system_message"] = {"text": msg, "type": "warn"}

def close_single_position(ticket, reason=""):
    if mt5 is None:
        return False, "mt5-unavailable"
    
    pos_info = mt5.positions_get(ticket=ticket)
    if not pos_info or len(pos_info) == 0:
        return False, f"position-not-found:{ticket}"
    
    pos = pos_info[0]
    sym = pos.symbol
    
    if not symbol_ensure(sym):
        return False, f"symbol-not-visible:{sym}"

    close_side = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    t = tick(sym)
    if not t:
        return False, "no-tick"
    
    price = t.bid if close_side == mt5.ORDER_TYPE_SELL else t.ask
    magic_val = 556677
    
    ok = False
    last_res = None
    for fm in _filling_sequence_for_symbol(sym):
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": sym,
            "volume": pos.volume,
            "type": close_side,
            "price": price,
            "deviation": 200,
            "magic": magic_val,
            "comment": reason,
            "type_filling": fm,
        }
        with MTX:
            res = mt5.order_send(req)
        
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            ok = True
            break
        last_res = res

    if ok:
        CLOSE_REASON[int(ticket)] = reason.upper()
        return True, "OK"
    else:
        msg = getattr(last_res, 'comment', 'send-failed') if last_res else 'send-failed'
        return False, msg

def trailing_stop_tick(sym):
    """
    Monitors open positions and closes them if their P/L drops by a
    specified amount in the account's currency from their peak P/L.
    """
    if not SETUP.get("trailing_stop_enabled"):
        return

    trailing_value = float(SETUP.get("trailing_stop_value", 0))
    if trailing_value <= 0:
        return

    open_pos = positions(sym)
    
    # Cleanup peak tracker for closed positions
    current_tickets = {p.ticket for p in open_pos}
    if 'pl_trailing_peaks' in STATE:
        for ticket in list(STATE['pl_trailing_peaks'].keys()):
            if ticket not in current_tickets:
                del STATE['pl_trailing_peaks'][ticket]

    if not open_pos:
        return

    for p in open_pos:
        ticket = p.ticket
        profit = p.profit

        # Profit must be positive to start trailing
        if profit <= 0:
            if ticket in STATE['pl_trailing_peaks']:
                del STATE['pl_trailing_peaks'][ticket]
            continue

        current_peak = STATE['pl_trailing_peaks'].get(ticket, 0.0)
        new_peak = max(current_peak, profit)
        STATE['pl_trailing_peaks'][ticket] = new_peak

        trigger_profit_level = new_peak - trailing_value
        
        if new_peak > trailing_value and profit < trigger_profit_level:
            reason = f"PL-TRAIL-{trailing_value}"
            print(f"[{reason}] Closing ticket {ticket}. Profit {profit:.2f} < Trigger {trigger_profit_level:.2f} (Peak: {new_peak:.2f})", flush=True)
            
            ok, msg = close_single_position(ticket, reason)
            if not ok:
                print(f"[{reason}] FAILED to close ticket {ticket}: {msg}", flush=True)
            else:
                # Cleanup after successful close
                if ticket in STATE['pl_trailing_peaks']:
                    del STATE['pl_trailing_peaks'][ticket]

def manage_tpsm_tpsb_mode(sym):
    """Secara dinamis mengelola mode Auto TPSM/TPSB berdasarkan jumlah posisi live."""
    open_positions = _get_open_count(sym)
    
    # Kondisi untuk mengaktifkan Auto TPSB: 3 atau lebih posisi terbuka
    if open_positions >= 3 and not SETUP.get("auto_tpsb_enabled"):
        print(f"[MODE_SWITCH] {open_positions} posisi terbuka. Mengaktifkan Auto TPSB.", flush=True)
        SETUP["auto_tpsb_enabled"] = True
        SETUP["tpsm_auto"] = False
        persist_save()
        
    # Kondisi untuk kembali ke Auto TPSM: kurang dari 3 posisi terbuka
    elif open_positions < 3 and not SETUP.get("tpsm_auto"):
        print(f"[MODE_SWITCH] {open_positions} posisi terbuka. Mengembalikan ke Auto TPSM.", flush=True)
        SETUP["tpsm_auto"] = True
        SETUP["auto_tpsb_enabled"] = False
        persist_save()

def auto_manage_trailing_stop(sym):
    """Secara otomatis mengaktifkan/menonaktifkan trailing stop berdasarkan jumlah posisi."""
    open_positions = _get_open_count(sym)
    should_be_enabled = (open_positions >= 2)
    is_enabled = SETUP.get("trailing_stop_enabled", False)

    if should_be_enabled != is_enabled:
        SETUP["trailing_stop_enabled"] = should_be_enabled
        status_text = "ON" if should_be_enabled else "OFF"
        print(f"[AUTO_TS] {open_positions} posisi terbuka. Trailing Stop otomatis diatur ke {status_text}.", flush=True)
        persist_save()

def check_and_reset_trade_direction_lock(sym):
    """Reset lock dan state pemicu S/R jika tidak ada posisi terbuka."""
    if _get_open_count(sym) == 0:
        if STATE.get("arah_posisi_terkunci") is not None:
            print("[CYCLE_RESET] Semua posisi ditutup. Mereset siklus trading.", flush=True)
            STATE["arah_posisi_terkunci"] = None
            # Reset state pemicu untuk siklus baru
            SR_TRIGGER["buy"]["last_trigger_candle_ts"] = 0.0
            SR_TRIGGER["sell"]["last_trigger_candle_ts"] = 0.0
            SR_TRIGGER["buy"]["last_trigger_level"] = 0.0
            SR_TRIGGER["sell"]["last_trigger_level"] = 0.0

        if STATE.get("m5_locked_direction") is not None:
            print("[M5 Auto-Set] Siklus selesai. Kunci arah M5 dilepas.", flush=True)
            STATE["m5_locked_direction"] = None
            
            # Kembalikan near_pct ke nilai asli jika sebelumnya diubah
            if STATE.get("sr_original_near_pct") is not None:
                sr_conf = SETUP.get("sr_config", {})
                active_mode_key = sr_conf.get("active_mode", "mode_01")
                active_settings = sr_conf.get("modes", {}).get(active_mode_key)
                if active_settings:
                    print(f"[M5 Auto-Set] Mengembalikan 'near_pct' ke nilai asli: {STATE['sr_original_near_pct']} untuk mode {active_mode_key}", flush=True)
                    active_settings['near_pct'] = STATE['sr_original_near_pct']
                    STATE['sr_original_near_pct'] = None
                    persist_save()

def auto_cross_trade(sym):
    """
    Triggers a trade based on the crossover of EMA(9) and SMA(20) on M1 timeframe.
    """
    # 1. Check if the feature is enabled and not in cooldown or pending another open
    if not (SETUP.get("cross_buy_enabled") or SETUP.get("cross_sell_enabled")):
        return
    if STATE.get("cooldown") or STATE.get("pending_open"):
        return

    # 2. Fetch candle data
    # We need at least 21 data points for a 20-period SMA, plus one previous point for comparison.
    # Fetching 50 to be safe and allow for stable EMA calculation.
    m1_candles = candles(sym, "M1", 50)
    if len(m1_candles) < 21:
        return # Not enough data

    # 3. Helper functions for MAs
    def _sma(data, period):
        if len(data) < period:
            return None
        return sum(c['close'] for c in data[-period:]) / period

    def _ema(data, period):
        if len(data) < period:
            return None
        prices = [c['close'] for c in data]
        multiplier = 2 / (period + 1)
        # Start with SMA for the first EMA value
        ema = sum(prices[:period]) / period
        # Apply EMA formula for the rest of the data
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    # 4. Calculate MAs for the last two candles
    # Previous candle's MAs
    ema9_prev = _ema(m1_candles[:-1], 9)
    sma20_prev = _sma(m1_candles[:-1], 20)

    # Current candle's MAs
    ema9_curr = _ema(m1_candles, 9)
    sma20_curr = _sma(m1_candles, 20)

    if not all([ema9_prev, sma20_prev, ema9_curr, sma20_curr]):
        return # Could not calculate all MAs

    # 5. Crossover detection logic
    last_cross = STATE.get("last_cross_direction")

    # Golden Cross (Buy signal)
    if SETUP.get("cross_buy_enabled") and ema9_prev < sma20_prev and ema9_curr > sma20_curr:
        if last_cross != "UP":
            print("[Auto Cross] Golden Cross terdeteksi (EMA9 > SMA20).", flush=True)
            if _click_by_func("auto_53_buy", "Auto Cross Buy"):
                STATE["last_cross_direction"] = "UP"
                set_cooldown(10) # Add a small cooldown to prevent immediate re-triggering
    
    # Death Cross (Sell signal)
    elif SETUP.get("cross_sell_enabled") and ema9_prev > sma20_prev and ema9_curr < sma20_curr:
        if last_cross != "DOWN":
            print("[Auto Cross] Death Cross terdeteksi (EMA9 < SMA20).", flush=True)
            if _click_by_func("auto_53_sell", "Auto Cross Sell"):
                STATE["last_cross_direction"] = "DOWN"
                set_cooldown(10) # Add a small cooldown to prevent immediate re-triggering

# ========== M5 Candle Auto-Toggle Logic ==========
def enforce_m5_direction_lock():
    """
    Enforces the SR Buy/Sell state based on the M5 candle lock.
    This function will override any manual toggles if a lock is active.
    """
    locked_direction = STATE.get("m5_locked_direction")
    if locked_direction is None:
        return

    should_be_buy = (locked_direction == "BUY")
    should_be_sell = (locked_direction == "SELL")

    needs_update = False
    if SETUP.get("sr_buy_enabled") != should_be_buy:
        SETUP["sr_buy_enabled"] = should_be_buy
        needs_update = True
    
    if SETUP.get("sr_sell_enabled") != should_be_sell:
        SETUP["sr_sell_enabled"] = should_be_sell
        needs_update = True

    if needs_update:
        print(f"[M5 Lock] Menegakkan kunci arah: Buy: {should_be_buy}, Sell: {should_be_sell}", flush=True)
        persist_save()

def auto_toggle_sr_on_m5(sym):
    """
    Sets the M5 lock direction at the start of a new M5 candle if no positions are open.
    It has two modes:
    - Jarak Lebar: If 2 previous M5 candles are the same color, set near_pct to 90.
    - Normal: Otherwise, based on 1 previous candle, and near_pct is normal.
    The enforcement is handled by enforce_m5_direction_lock().
    """
    # 1. Master condition: only run if no trades are open AND no lock is active.
    if open_count(sym) > 0 or STATE.get("m5_locked_direction") is not None:
        return

    # 2. Get the last 3 M5 candles for 2-candle lookback.
    m5_candles = candles(sym, "M5", 3)
    if len(m5_candles) < 3:
        return  # Not enough data

    # 3. Identify current and previous candles.
    current_candle = m5_candles[-1]
    prev_candle_1 = m5_candles[-2]
    prev_candle_2 = m5_candles[-3]

    # 4. Only run this logic ONCE per new candle.
    if STATE.get('last_m5_toggle_ts') == current_candle['time']:
        return
    
    # 5. Determine colors of the two previous candles
    is_prev1_red = prev_candle_1['close'] < prev_candle_1['open']
    is_prev1_green = prev_candle_1['close'] > prev_candle_1['open']
    is_prev2_red = prev_candle_2['close'] < prev_candle_2['open']
    is_prev2_green = prev_candle_2['close'] > prev_candle_2['open']

    new_direction = None
    is_wide_mode = False
    config_changed = False

    # 6. Check for 2-candle special "Jarak Lebar" condition
    if is_prev1_red and is_prev2_red:
        new_direction = "SELL"  # 2 red -> SELL
        is_wide_mode = True
        print("[M5 Auto-Set] 2 candle MERAH terdeteksi. Mode Jarak Lebar -> SELL.", flush=True)
    elif is_prev1_green and is_prev2_green:
        new_direction = "BUY"   # 2 green -> BUY
        is_wide_mode = True
        print("[M5 Auto-Set] 2 candle HIJAU terdeteksi. Mode Jarak Lebar -> BUY.", flush=True)
    
    # 7. If not in special mode, apply normal logic
    else:
        if is_prev1_red:
            new_direction = "BUY"   # 1 red -> BUY
        elif is_prev1_green:
            new_direction = "SELL"  # 1 green -> SELL

    # 8. Manage near_pct based on mode
    if is_wide_mode:
        if STATE.get("sr_original_near_pct") is None:
            sr_conf = SETUP.get("sr_config", {})
            active_mode_key = sr_conf.get("active_mode", "mode_01")
            active_settings = sr_conf.get("modes", {}).get(active_mode_key)
            if active_settings:
                original_pct = active_settings.get('near_pct')
                STATE['sr_original_near_pct'] = original_pct
                active_settings['near_pct'] = 90.0
                print(f"[M5 Auto-Set] Mode Jarak Lebar aktif. Menyimpan near_pct asli ({original_pct}) dan set ke 90.0 untuk mode {active_mode_key}", flush=True)
                config_changed = True
    # The restoration is handled by check_and_reset_trade_direction_lock

    # 9. Apply changes if a direction was determined
    if new_direction:
        STATE["m5_locked_direction"] = new_direction
        print(f"[M5 Auto-Set] Arah trading dikunci ke '{new_direction}' berdasarkan candle M5.", flush=True)
        # Enforce will save if buy/sell enabled status changes
        enforce_m5_direction_lock()
        # But if only near_pct changed, we need to save explicitly
        if config_changed:
            persist_save()
    
    # 10. Mark this candle as processed
    STATE['last_m5_toggle_ts'] = current_candle['time']

# ========== Engine loop ========== 
def engine_loop():
    next_tick = time.time()
    last_connect_check = 0
    while not stop_flag.is_set():
        now = time.time()

        # --- Connection Management (every 15s if offline) ---
        is_connected = False
        if mt5 and now - last_connect_check > 15:
            with MTX:
                try: is_connected = bool(mt5.terminal_info() and mt5.terminal_info().connected)
                except Exception: is_connected = False
            if not is_connected:
                print("[ENGINE] Offline, attempting to reconnect...", flush=True)
                mt5_restart()
            last_connect_check = now

        # --- Core Logic ---
        try:
            if now >= next_tick:
                sym = SETUP["symbol"]

                # Set M5 lock direction if applicable
                auto_toggle_sr_on_m5(sym)
                
                # Enforce M5 direction lock if active
                enforce_m5_direction_lock()
                
                # Reset cycle states if applicable (including M5 lock)
                check_and_reset_trade_direction_lock(sym)
                
                # Manage TPSM/TPSB mode based on live position count
                manage_tpsm_tpsb_mode(sym)
                auto_manage_trailing_stop(sym)

                # 1. Run all logics that can create 'tasks' (pending_open/pending_close)
                sr_auto_trade(sym)
                auto_cross_trade(sym)
                auto_tpsb_tick(sym)
                auto_tpsm_tick(sym)
                trailing_stop_tick(sym)
                # 2. Run executors that process those 'tasks'
                retry_and_verify_open_tick(sym)
                retry_and_verify_close_tick(sym)
                # 3. Run other session logic
                try_break_event(sym)
                session_tick(sym)
                next_tick = now + 1.0
        except Exception as e:
            print("[ENGINE]", e, flush=True)
            traceback.print_exc()
        time.sleep(0.1) # Sleep longer to reduce CPU usage


# ========== UI/Static ========== 
@app.route("/", methods=["GET"])
def root():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/static/<path:path>", methods=["GET"])
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, "static"), path)

# Simple liveness probe for batch/monitoring
@app.route("/health", methods=["GET"])
def health():
    return (f"OK v{APP_VERSION}", 200, {"Content-Type": "text/plain; charset=utf-8"})

# ========== API ========== 
def _status_payload_offline():
    return {
        "online": False, "locked": STATE["locked"], "mode": "SIDE",
        "auto_mode": SETUP["auto_mode"], "symbol": SETUP["symbol"],
        "mt5_accounts": SETUP.get("mt5_accounts", []),
        "active_mt5_login": SETUP.get("active_mt5_login"),
        "symbols": SETUP.get("symbols", []),
        "price": 0.0, "tick_dir": 0, "equity": 0.0, "daily_pl": 0.0,
        "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
        "free_margin": 0.0,
        "tpsm_auto": SETUP["tpsm_auto"], "abe_auto": SETUP["abe_auto"],
        "sr_buy_enabled": SETUP.get("sr_buy_enabled", True),
        "sr_sell_enabled": SETUP.get("sr_sell_enabled", False),
        "auto_tpsb_enabled": SETUP.get("auto_tpsb_enabled", False),
        "trailing_stop_enabled": SETUP.get("trailing_stop_enabled", False),
        "trailing_stop_value": SETUP.get("trailing_stop_value", 2000.0),
        "cross_buy_enabled": SETUP.get("cross_buy_enabled", False),
        "cross_sell_enabled": SETUP.get("cross_sell_enabled", False),
        "click_xy": SETUP.get("click_xy", []),
        "vSL": 0.0, "best_pl": 0.0, "adds_done": 0, "timer": STATE["timer"],
        "total_lot": 0.0, "open_count": 0, "float_pl": 0.0,
        "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
        "last_system_message": None,
        "pending_open_status": None,
        "open_positions": [], "history_today": [], "quotes": [],
        "sr_buy_armed": SR_TRIGGER["buy"]["armed"], "sr_sell_armed": SR_TRIGGER["sell"]["armed"],
        "sr_buy_last_ts": SR_TRIGGER["buy"]["last_ts"], "sr_sell_last_ts": SR_TRIGGER["sell"]["last_ts"],
        "sr_last_ts": max(SR_TRIGGER["buy"]["last_ts"], SR_TRIGGER["sell"]["last_ts"]),
        "sr_support": SR_STATE["support"], "sr_resistance": SR_STATE["resistance"],
        "sr_config": SETUP.get("sr_config", {}),
        "sr_top": SR_STATE["top"], "sr_bottom": SR_STATE["bottom"], "sr_mid": SR_STATE["mid"],
        "currency": "USD" # Default currency
    }

@app.route("/api/status", methods=["GET"])
def api_status():
    try:
        # --- Consume one-shot system message ---
        system_message = STATE.get("last_system_message")
        if system_message:
            STATE["last_system_message"] = None # Consume the message

        if mt5 is None:
            return jsonify(_status_payload_offline())
        with MTX:
            ti = mt5.terminal_info()
        # Perbaikan: cek connected harus eksplisit True, bukan default True
        online = bool(ti) and bool(getattr(ti, "connected", False))
        if not online:
            # Jangan restart dari sini, biarkan background worker yang menangani.
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
        
        open_positions_data = []
        for p in positions(sym):
            status = None
            if p.ticket in STATE["failed_close"]:
                status = "Gagal Close!"
            elif p.ticket in STATE["pending_close"]:
                details = STATE["pending_close"].get(p.ticket)
                retries = details["retries"]
                # retries=0 is pre-first-attempt, retries=1 is first attempt
                if retries <= 1: 
                    status = "Mencoba menutup..."
                else:
                    status = f"Retry ({retries - 1}/{MAX_RETRIES})"

            open_positions_data.append({
                "ticket": p.ticket,
                "side": ("BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"),
                "lot": p.volume,
                "entry": p.price_open,
                "pl": round(float(getattr(p, 'profit', 0.0) or 0.0), digits),
                "open_exec": classify_open_exec(getattr(p, 'comment', '')),
                "close_status": status
            })

        pending_open_status = None
        if STATE.get("pending_open"):
            details = STATE["pending_open"]
            retries = details["retries"]
            if retries == 1:
                pending_open_status = f"Membuka {details['side']}..."
            elif retries > 1:
                pending_open_status = f"Retry ({retries-1}/{MAX_RETRIES-1}) {details['side']}..."

        return jsonify({
            "online": True, "locked": STATE["locked"], "mode": "SIDE",
            "auto_mode": SETUP["auto_mode"], "symbol": sym,
            "mt5_accounts": SETUP.get("mt5_accounts", []),
            "active_mt5_login": SETUP.get("active_mt5_login"),
            "symbols": SETUP.get("symbols", []),
            "price": round(price,2), "tick_dir": tick_dir,
            "equity": round(eq, digits), "daily_pl": round(daily_pl_total, digits),
            "daily_target": SETUP["daily_target"], "daily_min": SETUP["daily_min"],
            "free_margin": round(free, digits),
            "currency": getattr(ai, 'currency', 'USD'),
            "tpsm_auto": SETUP["tpsm_auto"], "abe_auto": SETUP["abe_auto"],
            "sr_buy_enabled": SETUP.get("sr_buy_enabled", False),
            "sr_sell_enabled": SETUP.get("sr_sell_enabled", False),
            "auto_tpsb_enabled": SETUP.get("auto_tpsb_enabled", False),
            "trailing_stop_enabled": SETUP.get("trailing_stop_enabled", False),
            "trailing_stop_value": SETUP.get("trailing_stop_value", 2000.0),
            "cross_buy_enabled": SETUP.get("cross_buy_enabled", False),
            "cross_sell_enabled": SETUP.get("cross_sell_enabled", False),
            "click_xy": SETUP.get("click_xy", []),
            "vSL": 0.0, "best_pl": STATE["session_peak_pl"], "adds_done": 0, "timer": STATE["timer"],
            "total_lot": round(tl,2), "open_count": oc, "float_pl": round(pl, digits),
            "cooldown": STATE["cooldown"], "cooldown_remain": STATE["timer"],
            "last_system_message": system_message, # Kirim pesan yang sudah di-consume
            "pending_open_status": pending_open_status,
            "open_positions": open_positions_data,
            "history_today": history,
            "quotes": quotes,
            "sr_buy_armed": SR_TRIGGER["buy"]["armed"], "sr_sell_armed": SR_TRIGGER["sell"]["armed"],
            "sr_buy_last_ts": SR_TRIGGER["buy"]["last_ts"], "sr_sell_last_ts": SR_TRIGGER["sell"]["last_ts"],
            "sr_last_ts": max(SR_TRIGGER["buy"]["last_ts"], SR_TRIGGER["sell"]["last_ts"]),
            "sr_support": SR_STATE["support"], "sr_resistance": SR_STATE["resistance"],
            "sr_config": SETUP.get("sr_config", {}),
            "sr_top": SR_STATE["top"], "sr_bottom": SR_STATE["bottom"], "sr_mid": SR_STATE["mid"],
        })
    except Exception as e:
        # jangan 500 â€” selalu balas JSON aman
        print("[/api/status] EXC:", e, flush=True)
        traceback.print_exc()
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
    on = bool((request.get_json(force=True) or {}).get("on"))
    SETUP["tpsm_auto"] = on
    if on:
        SETUP["auto_tpsb_enabled"] = False # Maintain mutual exclusivity
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/abe", methods=["POST"])
def api_abe():
    SETUP["abe_auto"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/sr", methods=["POST"])
def api_sr_toggle():
    on = bool((request.get_json(force=True) or {}).get("on"))
    SETUP["sr_buy_enabled"] = on
    if on:
        SETUP["sr_sell_enabled"] = False
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/srsell", methods=["POST"])
def api_srsell_toggle():
    SETUP["sr_sell_enabled"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/autotpsb", methods=["POST"])
def api_autotpsb_toggle():
    on = bool((request.get_json(force=True) or {}).get("on"))
    SETUP["auto_tpsb_enabled"] = on
    if on:
        SETUP["tpsm_auto"] = False # Maintain mutual exclusivity
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/trailingstop", methods=["POST"])
def api_trailingstop_toggle():
    data = request.get_json(force=True) or {}
    if "on" in data:
        SETUP["trailing_stop_enabled"] = bool(data.get("on"))
    if "value" in data:
        try:
            value = float(data.get("value"))
            if value >= 0:
                SETUP["trailing_stop_value"] = value
        except (ValueError, TypeError):
            pass # ignore invalid value
    persist_save()
    return jsonify({"ok": True, "trailing_stop_enabled": SETUP.get("trailing_stop_enabled"), "trailing_stop_value": SETUP.get("trailing_stop_value")})

@app.route("/api/strategy/crossbuy", methods=["POST"])
def api_crossbuy_toggle():
    SETUP["cross_buy_enabled"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/strategy/crosssell", methods=["POST"])
def api_crosssell_toggle():
    SETUP["cross_sell_enabled"] = bool((request.get_json(force=True) or {}).get("on"))
    persist_save()
    return jsonify({"ok": True})

@app.route("/api/action/buy", methods=["POST"])
def api_buy():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    if SETUP.get("auto_mode"):
        return jsonify({"ok": False, "msg": "manual-disabled-in-auto"})
    STATE["locked"] = True
    ok, msg = order_send_with_fallback(SETUP["symbol"], "BUY", lot, reason="MANUAL BUY")
    STATE["locked"] = False
    begin_session_if_needed(SETUP["symbol"])
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/action/sell", methods=["POST"])
def api_sell():
    lot = float((request.get_json(force=True) or {}).get("lot", 0.01))
    if SETUP.get("auto_mode"):
        return jsonify({"ok": False, "msg": "manual-disabled-in-auto"})
    STATE["locked"] = True
    ok, msg = order_send_with_fallback(SETUP["symbol"], "SELL", lot, reason="MANUAL SELL")
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
    # Fitur dinonaktifkan sementara untuk perbaikan.
    return jsonify({"ok": False, "msg": "Fitur Close All dinonaktifkan sementara."}, 403)

@app.route("/api/action/auto_click_close_all", methods=["POST"])
def api_auto_click_close_all():
    # Fitur dinonaktifkan sementara untuk perbaikan.
    return jsonify({"ok": False, "msg": "Fitur Close All dinonaktifkan sementara."}, 403)

@app.route("/api/action/click_xy", methods=["POST"])
def api_click_xy():
    data = request.get_json(force=True) or {}
    try:
        x = int(data.get("x")),
        y = int(data.get("y")),
        if not (x > 0 and y > 0):
            return jsonify({"ok": False, "msg": "Invalid coordinates"}), 400
    except (ValueError, TypeError):
        return jsonify({"ok": False, "msg": "Coordinates must be integers"}), 400

    try:
        print(f"[MANUAL_CLICK] Clicking at ({x},{y})", flush=True)
        _win_leftclick(x, y)
        return jsonify({"ok": True, "msg": f"Clicked at ({x},{y})"})
    except Exception as e:
        print(f"[MANUAL_CLICK] EXC: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/action/breakeven", methods=["POST"])
def api_be():
    sym = SETUP["symbol"]
    need = float(SETUP["session"]["be_min_profit"])
    if float_pl(sym) >= need and open_count(sym) >= SETUP["session"]["min_positions_for_be"]:
        # Use clicker instead of API
        if _click_by_func("auto_bep", "Manual BEP"):
            STATE["session_be_hit"] = True
            end_session(); set_cooldown(10)
            return jsonify({"ok": True, "action": "click-bep"})
        else:
            return jsonify({"ok": False, "msg": "Gagal: Setup 'auto_bep' tidak ditemukan."})
    return jsonify({"ok": False, "msg": "Belum memenuhi syarat BE."})

@app.route("/api/diag", methods=["GET"])
def api_diag():
    if mt5 is None:
        return jsonify({
            "terminal_info": "MT5 unavailable",
            "account_info": "MT5 unavailable",
            "last_error": "MT5 unavailable",
            "terminal": {"connected": False, "path": "", "data_path": "", "build": 0},
            "account": {"trade_allowed": False, "login": 0, "server": "", "currency": ""},
            "symbol": SETUP.get("symbol"),
            "env": {
                "MT5_PATH": CFG("MT5_PATH"),
                "MT5_LOGIN": CFG("MT5_LOGIN"),
                "MT5_PASSWORD": CFG("MT5_PASSWORD"),
                "MT5_SERVER": CFG("MT5_SERVER"),
                "MT5_SYMBOL": CFG("MT5_SYMBOL"),
            }
        })
    with MTX:
        try: ti = mt5.terminal_info()
        except Exception as e: ti = f"EXC {e}"
        try: ai = mt5.account_info()
        except Exception as e: ai = f"EXC {e}"
        try: le = mt5.last_error()
        except Exception as e: le = f"EXC {e}"
    # Structured fields (best-effort)
    term = {
        "connected": bool(getattr(ti, 'connected', False)) if not isinstance(ti, str) else False,
        "path": str(getattr(ti, 'path', '') if not isinstance(ti, str) else ''),
        "data_path": str((getattr(ti, 'data_path', '') if not isinstance(ti, str) else '') or (getattr(ti, 'data_folder', '') if not isinstance(ti, str) else '')) ,
        "build": int(getattr(ti, 'build', 0) or 0) if not isinstance(ti, str) else 0,
    }
    acct = {
        "trade_allowed": bool(getattr(ai, 'trade_allowed', False)) if not isinstance(ai, str) else False,
        "login": int(getattr(ai, 'login', 0) or 0) if not isinstance(ai, str) else 0,
        "server": str(getattr(ai, 'server', '') if not isinstance(ai, str) else ''),
        "currency": str(getattr(ai, 'currency', '') if not isinstance(ai, str) else ''),
    }
    return jsonify({
        "terminal_info": str(ti),
        "account_info": str(ai),
        "last_error": str(le),
        "terminal": term,
        "account": acct,
        "symbol": SETUP.get("symbol"),
        "env": {
            "MT5_PATH": CFG("MT5_PATH"),
            "MT5_LOGIN": CFG("MT5_LOGIN"),
            "MT5_PASSWORD": CFG("MT5_PASSWORD"),
            "MT5_SERVER": CFG("MT5_SERVER"),
            "MT5_SYMBOL": CFG("MT5_SYMBOL"),
        }
    })

# ----------- Setup XY API ----------- 
@app.route("/api/setup/xy", methods=["POST"])
def api_setup_xy_save():
    data = request.get_json(force=True) or {}
    slots = data.get("slots")
    if isinstance(slots, list):
        out = []
        for item in slots:
            if isinstance(item, dict):
                try:
                    title = str(item.get("title", "Untitled"))
                    func = str(item.get("func", "close_row"))
                    # Simpan target negatif sebagai angka negatif
                    neg_pct = -abs(float(item.get("target_neg_pct", 0.0)))
                    pos_pct = abs(float(item.get("target_pos_pct", 0.0)))
                    x = int(float(item.get("x", 0)))
                    y = int(float(item.get("y", 0)))
                    out.append({"title": title, "func": func, "x": x, "y": y, 
                                "target_neg_pct": neg_pct, "target_pos_pct": pos_pct})
                except Exception:
                    # Lewati item yang formatnya salah
                    continue
        SETUP["click_xy"] = out
        persist_save()
        return jsonify({"ok": True, "saved": out})
    return jsonify({"ok": False, "msg": "payload-must-be-a-list-of-slots"}), 400

@app.route("/api/setup/sr", methods=["POST"])
def api_setup_sr():
    data = request.get_json(force=True) or {}
    if isinstance(data, dict):
        # Dapatkan konfigurasi SR saat ini
        sr_config = SETUP.get("sr_config", {})

        # Update mode yang aktif jika ada di data
        if "active_mode" in data and data["active_mode"] in ["mode_01", "mode_02"]:
            sr_config["active_mode"] = data["active_mode"]
            print(f"[SETUP] SR Active Mode changed to: {sr_config['active_mode']}", flush=True)

        # Update settings untuk masing-masing mode jika ada di data
        if "modes" in data and isinstance(data["modes"], dict):
            for mode_key, new_settings in data["modes"].items():
                if mode_key in sr_config.get("modes", {}):
                    # Ambil pengaturan mode saat ini
                    current_mode_settings = sr_config["modes"][mode_key]
                    # Update hanya field yang ada di request
                    if "candle_lookback" in new_settings:
                        current_mode_settings["candle_lookback"] = int(new_settings["candle_lookback"])
                    if "near_pct" in new_settings:
                        current_mode_settings["near_pct"] = float(new_settings["near_pct"])
                    print(f"[SETUP] SR {mode_key} settings updated.", flush=True)

        SETUP["sr_config"] = sr_config
        persist_save()
        return jsonify({"ok": True, "saved": SETUP["sr_config"]})
    return jsonify({"ok": False, "msg": "Invalid payload"}), 400

@app.route("/api/setup/accounts", methods=["POST"])
def api_setup_accounts_save():
    data = request.get_json(force=True) or {}
    incoming_accounts = data.get("accounts")
    if isinstance(incoming_accounts, list):
        # Validasi untuk mencegah ID Login duplikat
        logins = [str(acc.get("login")) for acc in incoming_accounts if acc.get("login")]
        if len(logins) != len(set(logins)):
            return jsonify({"ok": False, "msg": "Ditemukan ID Login duplikat. Setiap akun harus memiliki ID Login yang unik."}, 400)

        # Buat lookup untuk password yang ada agar tidak hilang saat update
        existing_passwords = {str(acc.get("login")): acc.get("password") for acc in SETUP.get("mt5_accounts", [])}

        valid_accounts = []
        for acc in incoming_accounts:
            if isinstance(acc, dict) and all(k in acc for k in ["alias", "login", "server"]):
                login_str = str(acc.get("login")),
                # Jika password tidak ada di request, pakai yang lama.
                if not acc.get("password"):
                    acc["password"] = existing_passwords.get(login_str)
                
                # Hanya tambahkan jika semua data lengkap
                if all(acc.get(k) for k in ["alias", "login", "password", "server"]):
                    valid_accounts.append(acc)
        SETUP["mt5_accounts"] = valid_accounts
        persist_save()
        return jsonify({"ok": True, "saved": len(valid_accounts)})
    return jsonify({"ok": False, "msg": "Invalid payload"}), 400

@app.route("/api/setup/accounts/select", methods=["POST"])
def api_setup_accounts_select():
    data = request.get_json(force=True) or {}
    login = data.get("login")
    if not login:
        return jsonify({"ok": False, "msg": "Login ID required"}), 400
    
    SETUP["active_mt5_login"] = str(login)
    persist_save()
    
    # Memicu koneksi ulang dengan akun baru di thread terpisah
    Thread(target=mt5_restart, daemon=True).start()
    
    return jsonify({"ok": True, "msg": f"Beralih ke akun {login}. Menyambungkan ulang..."})

@app.route("/api/setup/symbols", methods=["POST"])
def api_setup_symbols():
    print("[DEBUG] /api/setup/symbols endpoint was hit!", flush=True)
    data = request.get_json(force=True) or {}
    symbols = data.get("symbols")
    if isinstance(symbols, list):
        # Sanitize and remove duplicates
        unique_symbols = sorted(list(set(str(s).strip() for s in symbols if str(s).strip())))
        SETUP["symbols"] = unique_symbols
        persist_save()
        # Ensure the newly saved symbols are selected in MT5 for availability
        for sym in unique_symbols:
            symbol_ensure(sym)
        return jsonify({"ok": True, "saved": unique_symbols})
    return jsonify({"ok": False, "msg": "Invalid payload, expected a list of symbols."}, 400)

# ========== Boot ========== 
def boot():
    ok = mt5_init()
    print(f"[MT5] initialized = {ok} | symbol: {SETUP['symbol']}", flush=True)
    symbol_ensure(SETUP["symbol"])
    Thread(target=cooldown_worker, daemon=True).start()
    Thread(target=engine_loop, daemon=True).start()

if __name__ == "__main__":
    boot()
    app.run(host="0.0.0.0", port=5000, debug=False)
