"""Pending-order setups on any market: "buy if it reaches X, target Y, stop Z".

Every run (GitHub Actions, every 15 minutes) looks at 15-minute candles for each instrument in
universe.txt (gold, oil, indices, currencies, crypto, bonds, ETFs):
  1. A box: the last 8 completed candles (2 hours), its width within the ATR range in PARAMS,
     and the last close inside the box.
  2. Trend filter: long only above a rising EMA200 (15m, ~50 hours); short only below a falling one.
  3. Long:  Buy Stop at box high + 0.1 ATR; Short: Sell Stop at box low - 0.1 ATR. Stop and target
     per PARAMS (setups_params.json, learned by trading/radar/setups_review.py).
  4. Skip when the instrument's spread is too large a part of the risk, or a learned filter fails.
  5. The order is valid for 2 hours. One alert per instrument and direction every 4 hours.
The alert reaches the phone BEFORE the entry price, so the user places a pending order with its
target and stop and the platform fills it.
2026-11-26 review: the first rule (stop at box middle) lost -0.19 R per trade over 60 days; the
learned rule (wider box, stop beyond the box, spread <= 10% of risk, strong EMA slope) made
+0.03 R per trade on the untouched test period: near break-even, not a strong edge.
Alerts only: never places orders. Not financial advice.

    python3 setups.py            # live
    python3 setups.py --dry-run  # print only
"""
import argparse
import concurrent.futures
import json
import os
import time
import urllib.parse

from radar import get_json, notify, paris_time

HERE = os.path.dirname(os.path.abspath(__file__))
BOX_BARS = 8
ATR_LEN = 14
EMA_LEN = 200
VALID_MIN = 120
REPEAT_MIN = 240
MAX_ALERTS = 2  # the trained rule finds ~200 setups a day across 113 instruments: send the best 2 per scan


# Not tradable as such (VIX, yield indices), or managed currencies whose steady drift fakes breakouts
SKIP = {"^VIX", "^IRX", "^FVX", "^TNX", "^TYX", "USDTRY=X", "USDCNH=X"}


def load_universe():
    out = []
    with open(os.path.join(HERE, "universe.txt"), encoding="utf-8") as fh:
        for line in fh:
            if "|" in line:
                sym, name = line.strip().split("|", 1)
                if sym not in SKIP:
                    out.append((sym, name))
    return out


def bars_15m(sym):
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=10d&interval=15m" % urllib.parse.quote(sym)
    res = get_json(url)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None not in (o, h, l, c):
            rows.append((t, o, h, l, c))
    return rows


def ema(xs, n):
    k = 2.0 / (n + 1)
    out, e = [], xs[0]
    for x in xs:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def fmt(x, atr):
    """Enough decimals that one ATR shows at least 2 significant digits (EUR/USD -> 5, gold -> 1-2)."""
    d = 0
    while atr * 10 ** d < 10 and d < 5:
        d += 1
    return "{:,.{d}f}".format(x, d=d)


# Typical retail spread (price units) per instrument. A trade whose stop is so tight that the
# spread eats a big part of the risk is noise, not a setup. Unknown symbols: 0.03% of the price.
SPREADS = {
    "EURUSD=X": 0.00012, "GBPUSD=X": 0.00015, "AUDUSD=X": 0.00012, "NZDUSD=X": 0.00018,
    "USDCAD=X": 0.00018, "USDCHF=X": 0.00016, "EURGBP=X": 0.00015, "EURCHF=X": 0.0002,
    "USDJPY=X": 0.012, "EURJPY=X": 0.02, "GBPJPY=X": 0.03, "AUDJPY=X": 0.02,
    "USDMXN=X": 0.004, "USDZAR=X": 0.012, "USDBRL=X": 0.004, "DX-Y.NYB": 0.02,
    "GC=F": 0.4, "SI=F": 0.03, "PL=F": 2.0, "PA=F": 3.0, "HG=F": 0.003,
    "CL=F": 0.03, "BZ=F": 0.03, "NG=F": 0.004, "RB=F": 0.003,
    "ZW=F": 0.5, "ZC=F": 0.5, "ZS=F": 1.0, "KC=F": 0.3, "SB=F": 0.03, "CC=F": 10.0, "CT=F": 0.1, "LE=F": 0.2,
    "^GSPC": 0.5, "^NDX": 1.5, "^DJI": 3.0, "^RUT": 0.5, "^GDAXI": 1.5, "^FCHI": 1.0, "^FTSE": 1.0,
    "^STOXX50E": 1.5, "^N225": 10.0, "^HSI": 5.0, "000001.SS": 2.0, "^BSESN": 10.0,
    "ZB=F": 0.03, "ZN=F": 0.016,
}


def spread(sym, price):
    if sym in SPREADS:
        return SPREADS[sym]
    if sym.endswith("-USD"):
        return price * 0.0005  # crypto
    return max(0.01, price * 0.0003)  # ETFs and stocks


PARAMS = {"min_width_atr": 1.0, "max_width_atr": 2.5, "target_r": 2.0, "stop": "mid", "filters": [],
          "max_spread_r": 0.25}
_params_file = os.path.join(HERE, "setups_params.json")
if os.path.exists(_params_file):
    with open(_params_file, encoding="utf-8") as _fh:
        PARAMS.update(json.load(_fh))


def evaluate(rows, now, params=None, ema_series=None):
    """Pure: completed 15m rows (t, o, h, l, c) up to `now` -> a setup dict or None.
    Shared by the live scanner and the backtest, so both apply exactly the same rule."""
    p = params or PARAMS
    if len(rows) < EMA_LEN + BOX_BARS + 5:
        return None
    if now - (rows[-1][0] + 900) > 30 * 60:
        return None  # market closed or data stale
    t, o, h, l, c = zip(*rows)
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(len(c) - ATR_LEN, len(c))]
    atr = sum(trs) / ATR_LEN
    if atr <= 0:
        return None
    hi, lo = max(h[-BOX_BARS:]), min(l[-BOX_BARS:])
    width = hi - lo
    last = c[-1]
    if not (p["min_width_atr"] * atr <= width <= p["max_width_atr"] * atr) or not (lo < last < hi):
        return None
    e = ema_series if ema_series is not None else ema(list(c), EMA_LEN)  # backtest precomputes it
    rising, falling = e[-1] > e[-1 - BOX_BARS], e[-1] < e[-1 - BOX_BARS]
    mid = (hi + lo) / 2
    if last > e[-1] and rising:
        side, entry = "buy", hi + 0.1 * atr
        stop = mid if p["stop"] == "mid" else lo - 0.1 * atr
        target = entry + p["target_r"] * (entry - stop)
    elif last < e[-1] and falling:
        side, entry = "sell", lo - 0.1 * atr
        stop = mid if p["stop"] == "mid" else hi + 0.1 * atr
        target = entry - p["target_r"] * (stop - entry)
    else:
        return None
    feats = {
        "tightness": width / atr,
        "atr_pct": atr / last,
        "ema_dist_atr": (last - e[-1]) / atr * (1 if side == "buy" else -1),
        "ema_slope_atr": (e[-1] - e[-1 - BOX_BARS]) / atr * (1 if side == "buy" else -1),
        "box_pos": (last - lo) / width if side == "buy" else (hi - last) / width,
        "hour_utc": float(time.gmtime(t[-1] + 900).tm_hour),
        "run_8": (c[-1] - c[-9]) / atr * (1 if side == "buy" else -1),
    }
    for flt in p.get("filters", []):
        if flt["feature"] not in feats:
            continue  # spread_r is applied in find_setup through max_spread_r
        x = feats[flt["feature"]]
        if (flt["op"] == ">=" and x < flt["value"]) or (flt["op"] == "<=" and x > flt["value"]):
            return None
    return {"side": side, "last": last, "entry": entry, "stop": stop, "target": target,
            "risk": abs(entry - stop), "atr": atr, "tightness": width / atr, "candle_end": t[-1] + 900,
            "f": feats, "target_r": p["target_r"]}


def find_setup(sym, name, now):
    rows = [r for r in bars_15m(sym) if r[0] + 900 <= now]  # completed candles only
    s = evaluate(rows, now)
    if not s:
        return None
    s.update(symbol=sym, name=name, spread_r=spread(sym, s["last"]) / s["risk"])
    if s["spread_r"] > PARAMS.get("max_spread_r", 0.25):
        return None  # the stop is too tight for this instrument's spread
    return s


def message(s, now):
    buy = s["side"] == "buy"
    f = lambda x: fmt(x, s["atr"])  # noqa: E731
    lines = [
        "%s %s: أمر %s معلّق" % ("🟢" if buy else "🔴", s["name"], "شراء (Buy Stop)" if buy else "بيع (Sell Stop)"),
        "السعر الآن %s" % f(s["last"]),
        "%s إذا وصل %s" % ("اشترِ" if buy else "بِع", f(s["entry"])),
        "الهدف %s | الوقف %s" % (f(s["target"]), f(s["stop"])),
        "المخاطرة %s والربح %s (1:%g)" % (f(s["risk"]), f(s["target_r"] * s["risk"]), s["target_r"]),
        "صالح حتى %s بتوقيت باريس. إن لم يتفعّل، ألغِ الأمر." % paris_time(now + VALID_MIN * 60),
        "الأسعار من Yahoo (%s). إن اختلف سعر منصتك، طبّق نفس المسافات على سعرها." % s["symbol"],
        PARAMS.get("note", "تنبيه رادار بلا اختبار تاريخي، بطلبك.") + " ليس نصيحة مالية.",
    ]
    return "\n".join(lines)


def scan_once(sent, dry_run):
    now = time.time()

    def one(item):
        try:
            return find_setup(item[0], item[1], now)
        except Exception as e:
            print("  %s: %s" % (item[0], e))
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        found = [s for s in pool.map(one, load_universe()) if s]
    fresh = [s for s in found if now - sent.get("%s|%s" % (s["symbol"], s["side"]), 0) > REPEAT_MIN * 60]
    fresh.sort(key=lambda s: s["tightness"])
    for s in fresh[:MAX_ALERTS]:
        body = message(s, now)
        print(body + "\n", flush=True)
        if not dry_run:
            try:
                notify("%s %s" % ("BUY" if s["side"] == "buy" else "SELL", s["symbol"]), body)
            except Exception as e:
                print("notify failed: %s" % e)
            sent["%s|%s" % (s["symbol"], s["side"])] = now
    print("%s setups found %d, new %d" % (time.strftime("%H:%M", time.gmtime(now)), len(found), len(fresh)),
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop-min", type=int, default=0,
                    help="keep running this many minutes, scanning 1 minute after each quarter hour "
                         "(GitHub skips frequent schedules on new repos, so one job covers ~6 hours)")
    a = ap.parse_args()
    os.makedirs(os.path.join(HERE, "state"), exist_ok=True)
    spath = os.path.join(HERE, "state", "setups.json")
    sent = json.load(open(spath, encoding="utf-8")) if os.path.exists(spath) else {}
    stop = time.time() + 60 * a.loop_min
    while True:
        try:
            scan_once(sent, a.dry_run)
        except Exception as e:
            print("scan failed: %s" % e, flush=True)
        if not a.dry_run:
            with open(spath, "w", encoding="utf-8") as fh:
                json.dump(sent, fh)
        wait = 900 - (time.time() % 900) + 60  # next quarter hour + 1 minute (candle completed)
        if time.time() + wait > stop:
            break
        time.sleep(wait)


if __name__ == "__main__":
    main()
