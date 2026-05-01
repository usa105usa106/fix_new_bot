import os
import re
import time
import tempfile
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from statistics import mean
from typing import Dict, List, Optional, Tuple

import ccxt
import telebot
from telebot import types

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle


VERSION = "0.1"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

# Только токен и админ берутся из Railway Variables.
# Остальные настройки меняются в самом Telegram-боте.
ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

STATE_DIR = Path(os.getenv("STATE_DIR", "/data"))
if not STATE_DIR.exists():
    STATE_DIR = Path("/tmp")
STATE_FILE = STATE_DIR / "trading_signal_bot_state_v01.json"

EXCHANGE_ID = "mexc"
EXCHANGE_DEFAULT_TYPE = "swap"
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d", "1w", "1M"]

DEFAULT_STATE = {
    "coins": ["BTC", "ETH", "SOL", "ADA", "XAU", "XAG", "TSLA", "NVDA", "XRP", "BCH", "TON"],
    "main_timeframe": "1h",
    "leverage": 6,
    "position_size_pct": 1.2,
    "asia_enabled": False,
    "america_enabled": False,
    "exchange_type": "swap",
    "chart_enabled": True,
}

CANDLE_LIMIT = 260

SYMBOL_ALIASES = {
    "XAU": ["XAU/USDT:USDT", "XAU/USDT", "XAUT/USDT", "PAXG/USDT"],
    "XAG": ["XAG/USDT:USDT", "XAG/USDT"],
    "TSLA": ["TSLA/USDT:USDT", "TSLA/USDT"],
    "NVDA": ["NVDA/USDT:USDT", "NVDA/USDT"],
    "TON": ["TON/USDT:USDT", "TON/USDT"],
}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
START_TS = time.time()

_exchange = None
_markets = None


def json_loads(s: str):
    import json
    return json.loads(s)


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


def html_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def normalize_coin(text: str) -> str:
    s = str(text).upper().strip()
    s = s.replace("📈", "").replace("📊", "").replace("$", "").strip()
    s = s.replace("-", "/").replace("_", "/")
    s = re.sub(r"\s+", "", s)
    if "/" in s:
        return s.split("/", 1)[0]
    for q in ["USDT", "USDC", "USD"]:
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    return re.sub(r"[^A-Z0-9]", "", s)


def default_state() -> Dict:
    return dict(DEFAULT_STATE)


def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            data = json_loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            data = default_state()
    else:
        data = default_state()

    merged = default_state()
    merged.update(data)

    coins = []
    for c in merged.get("coins", []):
        c = normalize_coin(c)
        if c and c not in coins:
            coins.append(c)
    merged["coins"] = coins or DEFAULT_STATE["coins"][:]

    if merged.get("main_timeframe") not in TIMEFRAMES:
        merged["main_timeframe"] = DEFAULT_STATE["main_timeframe"]

    merged["leverage"] = max(1, min(125, int(merged.get("leverage", DEFAULT_STATE["leverage"]))))
    merged["position_size_pct"] = max(0.1, min(100.0, float(merged.get("position_size_pct", DEFAULT_STATE["position_size_pct"]))))
    merged["exchange_type"] = merged.get("exchange_type") if merged.get("exchange_type") in ("swap", "spot") else "swap"
    merged["chart_enabled"] = bool(merged.get("chart_enabled", True))

    return merged


def save_state(data: Dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json_dumps(data), "utf-8")


def reset_state() -> Dict:
    data = default_state()
    save_state(data)
    return data


def is_allowed(message) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return bool(message.from_user and message.from_user.id in ALLOWED_USER_IDS)


def deny(message):
    uid = message.from_user.id if message.from_user else "unknown"
    bot.reply_to(
        message,
        "⛔️ Доступ запрещён.\n\n"
        f"Твой Telegram ID:\n<code>{uid}</code>\n\n"
        "Добавь его в Railway Variables:\n"
        "<code>ALLOWED_USER_IDS=123456789</code>",
    )


def keyboard() -> types.ReplyKeyboardMarkup:
    st = load_state()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)

    kb.row("/signal")
    kb.row("new", "exit", "⚙️ Настройки")

    asia = "🟢 Азия" if st["asia_enabled"] else "⚪️ Азия"
    america = "🟢 Америка" if st["america_enabled"] else "⚪️ Америка"
    kb.row(asia, america)

    kb.row("🏓 Ping", "♻️ Сброс")
    return kb


def settings_markup() -> types.InlineKeyboardMarkup:
    st = load_state()
    mk = types.InlineKeyboardMarkup(row_width=5)

    tf_buttons = [
        types.InlineKeyboardButton(("✅ " if st["main_timeframe"] == tf else "") + tf, callback_data=f"tf:{tf}")
        for tf in TIMEFRAMES
    ]
    mk.add(*tf_buttons)

    mk.row(
        types.InlineKeyboardButton("➖ Плечо", callback_data="lev:-"),
        types.InlineKeyboardButton(f"{st['leverage']}x", callback_data="noop"),
        types.InlineKeyboardButton("➕ Плечо", callback_data="lev:+"),
    )
    mk.row(
        types.InlineKeyboardButton("➖ Размер", callback_data="pos:-"),
        types.InlineKeyboardButton(f"{st['position_size_pct']:g}%", callback_data="noop"),
        types.InlineKeyboardButton("➕ Размер", callback_data="pos:+"),
    )
    mk.row(
        types.InlineKeyboardButton(("🟢" if st["asia_enabled"] else "⚪️") + " Азия", callback_data="session:asia"),
        types.InlineKeyboardButton(("🟢" if st["america_enabled"] else "⚪️") + " Америка", callback_data="session:america"),
    )
    mk.row(
        types.InlineKeyboardButton(("✅" if st["chart_enabled"] else "❌") + " График", callback_data="chart:toggle"),
        types.InlineKeyboardButton(("✅" if st["exchange_type"] == "swap" else "⚪️") + " Futures", callback_data="market:swap"),
        types.InlineKeyboardButton(("✅" if st["exchange_type"] == "spot" else "⚪️") + " Spot", callback_data="market:spot"),
    )
    mk.row(types.InlineKeyboardButton("♻️ Сброс настроек", callback_data="reset"))
    return mk


def get_exchange():
    global _exchange, _markets
    st = load_state()
    ex_type = st.get("exchange_type", "swap")

    # Recreate exchange if type changed.
    if _exchange is None or getattr(_exchange, "_bot_exchange_type", None) != ex_type:
        cls = getattr(ccxt, EXCHANGE_ID)
        _exchange = cls({
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": ex_type},
        })
        _exchange._bot_exchange_type = ex_type
        _markets = None

    if _markets is None:
        _markets = _exchange.load_markets()
    return _exchange


def resolve_symbol(coin: str) -> str:
    coin = normalize_coin(coin)
    ex = get_exchange()
    markets = ex.markets or {}

    candidates = []
    candidates.extend(SYMBOL_ALIASES.get(coin, []))
    candidates.extend([
        f"{coin}/USDT:USDT",
        f"{coin}/USDT",
        f"{coin}/USDC:USDC",
        f"{coin}/USDC",
        f"{coin}/USD",
    ])

    for s in candidates:
        if s in markets:
            return s

    for symbol, info in markets.items():
        try:
            base = str(info.get("base", "")).upper()
            quote = str(info.get("quote", "")).upper()
            settle = str(info.get("settle", "")).upper()
            if base == coin and (quote in ("USDT", "USDC", "USD") or settle in ("USDT", "USDC", "USD")):
                return symbol
        except Exception:
            continue

    raise RuntimeError(
        f"Монета {coin} не найдена на MEXC в режиме {load_state().get('exchange_type')}."
    )


def fetch_ohlcv(symbol: str, timeframe: str) -> List[List[float]]:
    ex = get_exchange()
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=CANDLE_LIMIT)


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    if len(values) < period + 1:
        return [None] * len(values)

    out: List[Optional[float]] = [None] * len(values)
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = mean(gains)
    avg_loss = mean(losses)
    out[period] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return out


def atr(high: List[float], low: List[float], close: List[float], period: int = 14) -> List[Optional[float]]:
    if len(close) < period + 1:
        return [None] * len(close)

    tr = [0.0]
    for i in range(1, len(close)):
        tr.append(max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        ))

    out: List[Optional[float]] = [None] * len(close)
    first = mean(tr[1:period + 1])
    out[period] = first
    prev = first

    for i in range(period + 1, len(close)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev

    return out


def macd(values: List[float]) -> Tuple[List[float], List[float]]:
    e12 = ema(values, 12)
    e26 = ema(values, 26)
    m = [a - b for a, b in zip(e12, e26)]
    sig = ema(m, 9)
    return m, sig


def last_not_none(values, default=None):
    for v in reversed(values):
        if v is not None:
            return v
    return default


def calc_side_on_slice(high: List[float], low: List[float], close: List[float], volume: List[float]) -> Dict:
    price = close[-1]
    e20 = ema(close, 20)[-1]
    e50 = ema(close, 50)[-1]
    e200 = ema(close, 200)[-1] if len(close) >= 200 else ema(close, min(100, len(close)))[-1]
    r = last_not_none(rsi(close, 14), 50.0)
    m, sig = macd(close)
    macd_now = m[-1]
    sig_now = sig[-1]
    a = last_not_none(atr(high, low, close, 14), price * 0.01)

    vol_avg = mean(volume[-21:-1]) if len(volume) > 22 else (mean(volume[:-1]) if len(volume) > 1 else volume[-1])
    vol_boost = volume[-1] > vol_avg * 1.1 if vol_avg else False

    recent_high = max(high[-21:-1]) if len(high) > 22 else max(high[:-1])
    recent_low = min(low[-21:-1]) if len(low) > 22 else min(low[:-1])

    long_score = 0.0
    short_score = 0.0
    reasons = []

    if price > e50:
        long_score += 1
        reasons.append("price>EMA50")
    else:
        short_score += 1
        reasons.append("price<EMA50")

    if e20 > e50:
        long_score += 1
        reasons.append("EMA20>EMA50")
    else:
        short_score += 1
        reasons.append("EMA20<EMA50")

    if price > e200:
        long_score += 1
        reasons.append("price>EMA200")
    else:
        short_score += 1
        reasons.append("price<EMA200")

    if 52 <= r <= 72:
        long_score += 1
        reasons.append("RSI bullish")
    elif 28 <= r <= 48:
        short_score += 1
        reasons.append("RSI bearish")
    elif r > 78:
        short_score += 0.5
        reasons.append("RSI overbought")
    elif r < 22:
        long_score += 0.5
        reasons.append("RSI oversold")

    if macd_now > sig_now:
        long_score += 1
        reasons.append("MACD bullish")
    else:
        short_score += 1
        reasons.append("MACD bearish")

    if price > recent_high:
        long_score += 0.75
        reasons.append("breakout high")
    if price < recent_low:
        short_score += 0.75
        reasons.append("breakdown low")

    if vol_boost:
        if long_score > short_score:
            long_score += 0.5
            reasons.append("volume confirms long")
        elif short_score > long_score:
            short_score += 0.5
            reasons.append("volume confirms short")

    diff = long_score - short_score

    if diff >= 1.25:
        side = "L"
        confidence = min(90, max(52, 50 + diff * 8 + long_score * 3))
    elif diff <= -1.25:
        side = "S"
        confidence = min(90, max(52, 50 + abs(diff) * 8 + short_score * 3))
    else:
        side = "N"
        confidence = max(35, 50 - abs(diff) * 5)

    return {
        "price": price,
        "side": side,
        "confidence": round(confidence),
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi": r,
        "atr": a,
        "long_score": long_score,
        "short_score": short_score,
        "reasons": reasons[-6:],
    }


def backtest_winrate(high: List[float], low: List[float], close: List[float], volume: List[float], timeframe: str) -> Tuple[Optional[int], int]:
    if len(close) < 90:
        return None, 0

    lookahead_by_tf = {"5m": 18, "15m": 16, "1h": 14, "4h": 10, "1d": 7, "1w": 4, "1M": 3}
    lookahead = lookahead_by_tf.get(timeframe, 12)

    wins = 0
    trades = 0
    start = max(60, len(close) - 130)
    end = len(close) - lookahead - 1
    if end <= start:
        return None, 0

    step = 3 if timeframe in ("5m", "15m", "1h") else 1

    for i in range(start, end, step):
        try:
            calc = calc_side_on_slice(high[:i + 1], low[:i + 1], close[:i + 1], volume[:i + 1])
        except Exception:
            continue

        side = calc["side"]
        if side == "N":
            continue

        entry = close[i]
        risk = max(calc["atr"] * 1.15, entry * 0.004)

        if side == "L":
            sl = entry - risk
            tp = entry + risk * 0.8
        else:
            sl = entry + risk
            tp = entry - risk * 0.8

        outcome = None
        for j in range(i + 1, min(i + lookahead + 1, len(close))):
            hi = high[j]
            lo = low[j]

            if side == "L":
                if lo <= sl:
                    outcome = False
                    break
                if hi >= tp:
                    outcome = True
                    break
            else:
                if hi >= sl:
                    outcome = False
                    break
                if lo <= tp:
                    outcome = True
                    break

        if outcome is not None:
            trades += 1
            if outcome:
                wins += 1

    if trades < 5:
        return None, trades

    return round(wins / trades * 100), trades


def analyze_timeframe(symbol: str, timeframe: str) -> Dict:
    candles = fetch_ohlcv(symbol, timeframe)
    if len(candles) < 80:
        raise RuntimeError(f"Мало свечей для {symbol} {timeframe}: {len(candles)}")

    high = [float(c[2]) for c in candles]
    low = [float(c[3]) for c in candles]
    close = [float(c[4]) for c in candles]
    volume = [float(c[5]) for c in candles]

    calc = calc_side_on_slice(high, low, close, volume)
    winrate, trades = backtest_winrate(high, low, close, volume, timeframe)

    calc.update({
        "timeframe": timeframe,
        "winrate": winrate,
        "trades": trades,
        "candles": candles,
    })
    return calc


def session_info(state: Dict) -> Dict:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    t = now.time()

    asia_active = dtime(3, 0) <= t < dtime(11, 0)
    america_active = t >= dtime(16, 30) or t < dtime(0, 30)

    enabled = []
    active = []

    if state.get("asia_enabled"):
        enabled.append("Азия")
        if asia_active:
            active.append("Азия")

    if state.get("america_enabled"):
        enabled.append("Америка")
        if america_active:
            active.append("Америка")

    return {
        "now": now,
        "asia_active": asia_active,
        "america_active": america_active,
        "enabled": enabled,
        "active": active,
    }


def aggregate_signal(coin: str, light: bool = False) -> Dict:
    state = load_state()
    symbol = resolve_symbol(coin)

    tf_results = []
    tfs = TIMEFRAMES if not light else [state["main_timeframe"], "15m", "1h"]
    # remove duplicates preserving order
    tfs = list(dict.fromkeys([tf for tf in tfs if tf in TIMEFRAMES]))

    for tf in tfs:
        try:
            tf_results.append(analyze_timeframe(symbol, tf))
            time.sleep(0.1)
        except Exception as e:
            tf_results.append({
                "timeframe": tf,
                "error": str(e),
                "side": "N",
                "confidence": 0,
                "winrate": None,
                "trades": 0,
            })

    weights = {"5m": 0.15, "15m": 0.18, "1h": 0.27, "4h": 0.20, "1d": 0.13, "1w": 0.05, "1M": 0.02}
    main_tf = state["main_timeframe"]
    for k in weights:
        weights[k] *= 0.85
    weights[main_tf] = weights.get(main_tf, 0.20) + 0.25

    sess = session_info(state)
    session_note = ""

    if state.get("asia_enabled") or state.get("america_enabled"):
        if sess["active"]:
            session_note = f"Фильтр сессий: активно окно {', '.join(sess['active'])}"
            weights["5m"] += 0.03
            weights["15m"] += 0.03
        else:
            session_note = f"Фильтр сессий: сейчас вне выбранных окон ({', '.join(sess['enabled'])})"

    score = 0.0
    total_w = 0.0
    for r in tf_results:
        if "error" in r:
            continue
        side_mult = 1 if r["side"] == "L" else -1 if r["side"] == "S" else 0
        quality = (r.get("winrate") or r["confidence"]) / 100
        w = weights.get(r["timeframe"], 0.20)
        score += side_mult * quality * w
        total_w += w

    score = score / total_w if total_w else 0

    if score >= 0.10:
        final_side = "L"
    elif score <= -0.10:
        final_side = "S"
    else:
        final_side = "N"

    certainty = min(92, max(42, round(50 + abs(score) * 60)))

    if state.get("asia_enabled") or state.get("america_enabled"):
        if sess["active"]:
            certainty = min(95, certainty + 4)
        else:
            certainty = max(20, certainty - 12)

    main = next((r for r in tf_results if r.get("timeframe") == main_tf and "error" not in r), None)
    if main is None:
        main = next((r for r in tf_results if "error" not in r), None)

    if main is None:
        errors = "; ".join([f"{r.get('timeframe')}: {r.get('error')}" for r in tf_results])
        raise RuntimeError(f"Не удалось получить данные с MEXC: {errors}")

    entry = main["price"]
    atr_value = main["atr"] or entry * 0.01
    risk = max(atr_value * 1.15, entry * 0.004)

    if final_side == "L":
        stop = entry - risk
        targets = [entry + risk * x for x in [0.8, 1.3, 2.0, 2.8]]
    elif final_side == "S":
        stop = entry + risk
        targets = [entry - risk * x for x in [0.8, 1.3, 2.0, 2.8]]
    else:
        stop = None
        targets = []

    main_tf_success = main.get("winrate")
    if main_tf_success is None:
        main_tf_success = main.get("confidence", certainty)

    return {
        "coin": normalize_coin(coin),
        "symbol": symbol,
        "side": final_side,
        "certainty": certainty,
        "total_success": certainty,
        "main_tf_success": int(round(main_tf_success)),
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "main": main,
        "timeframes": tf_results,
        "state": state,
        "session": sess,
        "session_note": session_note,
    }


def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "N/A"
    if abs(x) >= 1000:
        return f"${x:,.2f}".replace(",", "")
    if abs(x) >= 1:
        return f"${x:.4f}"
    if abs(x) >= 0.01:
        return f"${x:.5f}"
    return f"${x:.8f}"


def side_text(side: str) -> str:
    return "Long (L)" if side == "L" else "Short (S)" if side == "S" else "Neutral (N)"


def side_emoji(side: str) -> str:
    return "🟢" if side == "L" else "🔴" if side == "S" else "⚪️"


def timeframe_success_line(r: Dict, main_tf: Optional[str] = None) -> str:
    tf = r["timeframe"]
    is_main = tf == main_tf

    if "error" in r:
        line = f"{tf}: ошибка данных"
        return f"⭐ <b>{line}</b>" if is_main else line

    win = r.get("winrate")
    trades = r.get("trades", 0)
    model = r.get("confidence", 0)
    side = r.get("side", "N")

    if win is None:
        success = model
        extra = f"модель {model}%"
    else:
        success = win
        extra = f"отработка TP1 {win}% / {trades} сделок, модель {model}%"

    line = f"{tf}: {side_text(side)}, успешность {success}% ({extra})"
    if is_main:
        return f"⭐ <b>{line}</b>"
    return line


def format_signal(sig: Dict) -> str:
    side = sig["side"]
    state = sig["state"]
    sess = sig["session"]

    lines = []
    lines.append(f"<b>{side_emoji(side)} Trading Signal Bot v{VERSION}</b>")
    lines.append("")
    lines.append(f"Pair: <b>{html_escape(sig['coin'])}/USDT</b>")
    lines.append(f"MEXC symbol: <code>{html_escape(sig['symbol'])}</code>")
    lines.append(f"Direction: <b>{side_text(side)}</b>")
    lines.append(f"Main timeframe: ⭐ <b>{state['main_timeframe']}</b>")
    lines.append(f"<b>Main TF success: {sig['main_tf_success']}%</b>")
    lines.append(f"<b>Total success: {sig['total_success']}%</b>")
    lines.append(f"Setup bias: <b>{sig['main']['timeframe']} {sig['main']['confidence']}% ({sig['main']['side']})</b>")
    lines.append(f"Entry: <b>{fmt_price(sig['entry'])}</b>")

    if side != "N":
        lines.append("")
        lines.append("<b>Targets:</b>")
        for target in sig["targets"]:
            lines.append(f"25% at <b>{fmt_price(target)}</b>")
        lines.append("")
        lines.append(f"Stop loss: <b>{fmt_price(sig['stop'])}</b>")
        lines.append("")
        lines.append(f"Leverage: <b>{state['leverage']}x</b>")
        lines.append(f"Position size: <b>{state['position_size_pct']:g}%</b>")
    else:
        lines.append("")
        lines.append("No trade: нет достаточно сильного совпадения таймфреймов.")

    lines.append("")
    lines.append("<b>Успешность по таймфреймам:</b>")
    for r in sig["timeframes"]:
        lines.append(timeframe_success_line(r, state["main_timeframe"]))

    lines.append("")
    lines.append("<b>Сессии по МСК:</b>")
    lines.append(f"Азия 03:00: <b>{'ON' if state['asia_enabled'] else 'OFF'}</b> — сейчас {'активна' if sess['asia_active'] else 'не активна'}")
    lines.append(f"Америка 16:30: <b>{'ON' if state['america_enabled'] else 'OFF'}</b> — сейчас {'активна' if sess['america_active'] else 'не активна'}")
    if sig["session_note"]:
        lines.append(f"Фильтр: {sig['session_note']}")

    lines.append("")
    lines.append("<b>Main TF indicators:</b>")
    m = sig["main"]
    lines.append(f"RSI: {m['rsi']:.1f}")
    lines.append(f"EMA20/50/200: {fmt_price(m['ema20'])} / {fmt_price(m['ema50'])} / {fmt_price(m['ema200'])}")
    lines.append("")
    lines.append("⚠️ Это алгоритмический сигнал, не финансовая рекомендация. Прибыль не гарантируется.")
    return "\n".join(lines)


def create_chart(sig: Dict) -> Optional[str]:
    if not sig["state"].get("chart_enabled", True):
        return None
    try:
        main = sig["main"]
        candles = main["candles"][-90:]
        if len(candles) < 10:
            return None

        times = [datetime.fromtimestamp(c[0] / 1000) for c in candles]
        dates = mdates.date2num(times)
        opens = [float(c[1]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        vols = [float(c[5]) for c in candles]

        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(10, 6), sharex=True,
            gridspec_kw={"height_ratios": [4, 1]}
        )

        width = (dates[1] - dates[0]) * 0.7 if len(dates) > 1 else 0.03

        for d, o, h, l, c in zip(dates, opens, highs, lows, closes):
            color = "#12a77b" if c >= o else "#e55353"
            ax.plot([d, d], [l, h], color=color, linewidth=1)
            lower = min(o, c)
            height = max(abs(c - o), max(closes) * 0.000001)
            ax.add_patch(Rectangle((d - width / 2, lower), width, height, facecolor=color, edgecolor=color, linewidth=0.8))

        vmax = max(vols) if vols else 1
        for d, v, o, c in zip(dates, vols, opens, closes):
            color = "#12a77b" if c >= o else "#e55353"
            axv.bar(d, v, width=width, color=color, alpha=0.45)
        axv.set_ylim(0, vmax * 1.25)

        entry = sig.get("entry")
        stop = sig.get("stop")
        targets = sig.get("targets") or []

        def hline(y, label, color):
            ax.axhline(y, linestyle="--", linewidth=1.3, color=color)
            ax.text(dates[-1], y, f" {label} {fmt_price(y)} ", va="center", ha="left", fontsize=9, color="white",
                    bbox=dict(facecolor=color, edgecolor=color, boxstyle="round,pad=0.25"))

        if entry:
            hline(entry, "ENTRY", "#3b82f6")
        if stop:
            hline(stop, "SL", "#ef4444")
        for idx, t in enumerate(targets, start=1):
            hline(t, f"TP{idx}", "#22c55e")

        title = f"{sig['coin']}/USDT | {side_text(sig['side'])} | TF {sig['state']['main_timeframe']} | Total {sig['total_success']}%"
        ax.set_title(title, fontsize=13)
        ax.grid(True, alpha=0.25)
        axv.grid(True, alpha=0.20)
        axv.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))

        fig.autofmt_xdate()
        fig.tight_layout()

        path = str(Path(tempfile.gettempdir()) / f"signal_{sig['coin']}_{int(time.time())}.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path
    except Exception:
        return None


def split_text(text: str, max_len: int = 3900) -> List[str]:
    if len(text) <= max_len:
        return [text]
    parts, current, size = [], [], 0
    for line in text.splitlines():
        if size + len(line) + 1 > max_len:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def send_signal(message, coin_text: str):
    if not is_allowed(message):
        deny(message)
        return

    coin = normalize_coin(coin_text)
    if not coin:
        bot.reply_to(message, "Напиши монету, например: <code>/signal BTC</code>")
        return

    wait_msg = bot.send_message(message.chat.id, f"⏳ Считаю сигнал по <b>{html_escape(coin)}</b> на MEXC...")

    try:
        sig = aggregate_signal(coin)
        chart_path = create_chart(sig)

        if chart_path and Path(chart_path).exists():
            with open(chart_path, "rb") as f:
                bot.send_photo(message.chat.id, f, caption=f"📊 {html_escape(sig['coin'])}/USDT — Entry / TP / SL")

        text = format_signal(sig)
        parts = split_text(text)
        bot.edit_message_text(parts[0], chat_id=wait_msg.chat.id, message_id=wait_msg.message_id, disable_web_page_preview=True)
        for part in parts[1:]:
            bot.send_message(message.chat.id, part, disable_web_page_preview=True)

        if chart_path:
            try:
                Path(chart_path).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        bot.edit_message_text(
            f"❌ Ошибка сигнала по <b>{html_escape(coin)}</b>\n\n<code>{html_escape(str(e))}</code>",
            chat_id=wait_msg.chat.id,
            message_id=wait_msg.message_id,
        )


def send_signal_overview(message):
    if not is_allowed(message):
        deny(message)
        return

    st = load_state()
    wait_msg = bot.send_message(message.chat.id, f"⏳ Сканирую {len(st['coins'])} монет на MEXC...")

    rows = []
    errors = []
    for coin in st["coins"]:
        try:
            sig = aggregate_signal(coin, light=True)
            rows.append(sig)
        except Exception as e:
            errors.append((coin, str(e)[:80]))

    rows.sort(key=lambda s: s["certainty"], reverse=True)

    lines = [f"<b>📡 /signal — общий скан v{VERSION}</b>", ""]
    lines.append(f"Биржа: <b>MEXC</b>, режим: <b>{st['exchange_type']}</b>")
    lines.append(f"Основной TF: <b>{st['main_timeframe']}</b>")
    lines.append("")

    if rows:
        lines.append("<b>Лучшие сигналы:</b>")
        for sig in rows[:10]:
            lines.append(
                f"{side_emoji(sig['side'])} <b>{sig['coin']}</b>: {side_text(sig['side'])}, "
                f"Total {sig['total_success']}%, entry {fmt_price(sig['entry'])}"
            )
        lines.append("")
        lines.append("Для полного сигнала напиши, например: <code>/signal BTC</code>")
    else:
        lines.append("Не удалось построить сигналы.")

    if errors:
        lines.append("")
        lines.append("<b>Ошибки:</b>")
        for coin, err in errors[:8]:
            lines.append(f"{coin}: {html_escape(err)}")

    lines.append("")
    lines.append("⚠️ Не финансовая рекомендация.")

    bot.edit_message_text("\n".join(lines), chat_id=wait_msg.chat.id, message_id=wait_msg.message_id)


def settings_text() -> str:
    st = load_state()
    sess = session_info(st)
    return f"""⚙️ <b>Настройки бота</b>

Версия: <b>{VERSION}</b>
Биржа: <b>MEXC</b>
Режим рынка: <b>{st['exchange_type']}</b>
Основной таймфрейм: <b>{st['main_timeframe']}</b>
Плечо: <b>{st['leverage']}x</b>
Размер позиции: <b>{st['position_size_pct']:g}%</b>
График: <b>{'ON' if st['chart_enabled'] else 'OFF'}</b>

Монеты:
<code>{", ".join(st['coins'])}</code>

Сессии по МСК:
Азия 03:00: <b>{'ON' if st['asia_enabled'] else 'OFF'}</b> — сейчас {'активна' if sess['asia_active'] else 'не активна'}
Америка 16:30: <b>{'ON' if st['america_enabled'] else 'OFF'}</b> — сейчас {'активна' if sess['america_active'] else 'не активна'}

Команды:
<code>/signal</code> — общий скан
<code>/signal BTC</code> — полный сигнал
<code>new pol</code> — добавить POL
<code>exit sol</code> — удалить SOL

Все настройки, кроме <code>BOT_TOKEN</code> и <code>ALLOWED_USER_IDS</code>, меняются здесь.
"""


def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}д {h}ч {m}м {s}с"
    if h:
        return f"{h}ч {m}м {s}с"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def ping_text(start: float) -> str:
    bot_ms = int((time.perf_counter() - start) * 1000)

    try:
        t0 = time.perf_counter()
        ex = get_exchange()
        try:
            ex.fetch_time()
        except Exception:
            ex.fetch_ticker(resolve_symbol("BTC"))
        mexc_status = f"{int((time.perf_counter() - t0) * 1000)} ms"
    except Exception as e:
        mexc_status = f"ошибка: {str(e)[:120]}"

    return f"""🏓 <b>Ping</b>

Bot response: <b>{bot_ms} ms</b>
MEXC API: <b>{html_escape(mexc_status)}</b>
Uptime: <b>{format_uptime(time.time() - START_TS)}</b>
Version: <b>{VERSION}</b>
"""


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if not is_allowed(message):
        deny(message)
        return

    text = (
        f"🤖 <b>Trading Signal Bot v{VERSION}</b>\n\n"
        "Кнопка <b>/signal</b> делает общий скан всех монет.\n\n"
        "Команды:\n"
        "<code>/signal</code> — общий скан\n"
        "<code>/signal BTC</code> — полный сигнал по BTC\n"
        "<code>new pol</code> — добавить монету\n"
        "<code>exit sol</code> — удалить монету\n"
        "<code>/settings</code> — настройки\n"
        "<code>/myid</code> — Telegram ID\n\n"
        "⚠️ Это не финансовая рекомендация."
    )
    bot.send_message(message.chat.id, text, reply_markup=keyboard())


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    uid = message.from_user.id if message.from_user else "unknown"
    bot.reply_to(message, f"Твой Telegram ID:\n<code>{uid}</code>")


@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    if not is_allowed(message):
        deny(message)
        return
    bot.send_message(message.chat.id, settings_text(), reply_markup=settings_markup())


@bot.message_handler(commands=["signal"])
def cmd_signal(message):
    if not is_allowed(message):
        deny(message)
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_signal_overview(message)
    else:
        send_signal(message, parts[1])


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    st = load_state()
    data = call.data

    try:
        if data == "noop":
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tf:"):
            tf = data.split(":", 1)[1]
            if tf in TIMEFRAMES:
                st["main_timeframe"] = tf
                save_state(st)
                bot.answer_callback_query(call.id, f"Основной таймфрейм: {tf}")

        elif data == "lev:+":
            st["leverage"] = min(125, int(st["leverage"]) + 1)
            save_state(st)
            bot.answer_callback_query(call.id, f"Плечо: {st['leverage']}x")

        elif data == "lev:-":
            st["leverage"] = max(1, int(st["leverage"]) - 1)
            save_state(st)
            bot.answer_callback_query(call.id, f"Плечо: {st['leverage']}x")

        elif data == "pos:+":
            st["position_size_pct"] = round(min(100, float(st["position_size_pct"]) + 0.1), 2)
            save_state(st)
            bot.answer_callback_query(call.id, f"Размер: {st['position_size_pct']:g}%")

        elif data == "pos:-":
            st["position_size_pct"] = round(max(0.1, float(st["position_size_pct"]) - 0.1), 2)
            save_state(st)
            bot.answer_callback_query(call.id, f"Размер: {st['position_size_pct']:g}%")

        elif data == "session:asia":
            st["asia_enabled"] = not st.get("asia_enabled", False)
            save_state(st)
            bot.answer_callback_query(call.id, "Азия переключена")

        elif data == "session:america":
            st["america_enabled"] = not st.get("america_enabled", False)
            save_state(st)
            bot.answer_callback_query(call.id, "Америка переключена")

        elif data == "chart:toggle":
            st["chart_enabled"] = not st.get("chart_enabled", True)
            save_state(st)
            bot.answer_callback_query(call.id, "График переключён")

        elif data == "market:swap":
            st["exchange_type"] = "swap"
            save_state(st)
            reset_exchange()
            bot.answer_callback_query(call.id, "Рынок: futures/swap")

        elif data == "market:spot":
            st["exchange_type"] = "spot"
            save_state(st)
            reset_exchange()
            bot.answer_callback_query(call.id, "Рынок: spot")

        elif data == "reset":
            reset_state()
            reset_exchange()
            bot.answer_callback_query(call.id, "Настройки сброшены")

        bot.edit_message_text(settings_text(), chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=settings_markup())
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:80]}", show_alert=True)


def reset_exchange():
    global _exchange, _markets
    _exchange = None
    _markets = None


@bot.message_handler(func=lambda m: True)
def text_handler(message):
    if not message.text:
        return

    if not is_allowed(message):
        deny(message)
        return

    text = message.text.strip()
    low = text.lower().strip()

    if text == "/signal":
        send_signal_overview(message)
        return

    if text == "new":
        bot.send_message(message.chat.id, "Чтобы добавить монету, напиши:\n<code>new pol</code>", reply_markup=keyboard())
        return

    if text == "exit":
        st = load_state()
        bot.send_message(message.chat.id, "Чтобы удалить монету, напиши:\n<code>exit sol</code>\n\nТекущие монеты:\n<code>" + ", ".join(st["coins"]) + "</code>", reply_markup=keyboard())
        return

    if text == "⚙️ Настройки":
        cmd_settings(message)
        return

    if text in ("🏓 Ping", "ping", "/ping"):
        start = time.perf_counter()
        bot.send_message(message.chat.id, ping_text(start), reply_markup=keyboard())
        return

    if text == "♻️ Сброс":
        reset_state()
        reset_exchange()
        bot.send_message(message.chat.id, "♻️ Настройки сброшены на значения по умолчанию.", reply_markup=keyboard())
        return

    if "азия" in low and ("🟢" in text or "⚪️" in text or low == "азия"):
        st = load_state()
        st["asia_enabled"] = not st.get("asia_enabled", False)
        save_state(st)
        bot.send_message(message.chat.id, f"Азия: {'ON' if st['asia_enabled'] else 'OFF'}", reply_markup=keyboard())
        return

    if "америка" in low and ("🟢" in text or "⚪️" in text or low == "америка"):
        st = load_state()
        st["america_enabled"] = not st.get("america_enabled", False)
        save_state(st)
        bot.send_message(message.chat.id, f"Америка: {'ON' if st['america_enabled'] else 'OFF'}", reply_markup=keyboard())
        return

    m_new = re.match(r"^/?new\s+(.+)$", low, re.IGNORECASE)
    if m_new:
        coin = normalize_coin(m_new.group(1))
        if not coin:
            bot.reply_to(message, "Пример: <code>new pol</code>")
            return
        st = load_state()
        if coin not in st["coins"]:
            st["coins"].append(coin)
            save_state(st)
            bot.send_message(message.chat.id, f"✅ Монета <b>{coin}</b> добавлена.", reply_markup=keyboard())
        else:
            bot.send_message(message.chat.id, f"ℹ️ Монета <b>{coin}</b> уже есть.", reply_markup=keyboard())
        return

    m_exit = re.match(r"^/?exit\s+(.+)$", low, re.IGNORECASE)
    if m_exit:
        coin = normalize_coin(m_exit.group(1))
        st = load_state()
        if coin in st["coins"]:
            st["coins"].remove(coin)
            save_state(st)
            bot.send_message(message.chat.id, f"✅ Монета <b>{coin}</b> удалена.", reply_markup=keyboard())
        else:
            bot.send_message(message.chat.id, f"ℹ️ Монеты <b>{coin}</b> нет в списке.", reply_markup=keyboard())
        return

    cleaned = normalize_coin(text)
    st = load_state()
    if cleaned in st["coins"] or re.fullmatch(r"[A-Z0-9]{2,12}", cleaned):
        send_signal(message, cleaned)
        return

    bot.reply_to(message, "Напиши <code>/signal</code>, <code>/signal BTC</code>, <code>new pol</code> или <code>exit sol</code>.", reply_markup=keyboard())


if __name__ == "__main__":
    print(f"Trading Signal Bot v{VERSION} started. Exchange={EXCHANGE_ID}")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
