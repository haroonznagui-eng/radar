"""Pending-order setups on any market: "buy if it reaches X, target Y, stop Z".

Every run (GitHub Actions, every 15 minutes) looks at 15-minute candles for each instrument in
universe.txt (gold, oil, indices, currencies, crypto, bonds, ETFs):
  1. A tight box: the last 8 completed candles (2 hours) span between 1x and 2.5x the 15-minute
     ATR, and the last close is inside the box.
  2. Trend filter: long only above a rising EMA200 (15m, ~50 hours); short only below a falling one.
  3. Long:  Buy Stop at box high + 0.1 ATR, stop at the box middle, target = entry + 2 x risk.
     Short: Sell Stop at box low - 0.1 ATR, stop at the box middle, target = entry - 2 x risk.
  4. The order is valid for 2 hours. One alert per instrument and direction every 4 hours.
The alert reaches the phone BEFORE the entry price, so the user places a pending order with its
target and stop and the platform fills it. Not backtested, by the user's choice (2026-11-25).
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
MAX_ALERTS = 3


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


def find_setup(sym, name, now):
    rows = bars_15m(sym)
    rows = [r for r in rows if r[0] + 900 <= now]  # completed candles only
    if len(rows) < EMA_LEN + BOX_BARS + 5:
        return None
    if now - (rows[-1][0] + 900) > 30 * 60:
        return None  # market closed or data stale
    t, o, h, l, c = zip(*rows)
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    atr = sum(trs[-ATR_LEN:]) / ATR_LEN
    if atr <= 0:
        return None
    hi, lo = max(h[-BOX_BARS:]), min(l[-BOX_BARS:])
    width = hi - lo
    last = c[-1]
    if not (1.0 * atr <= width <= 2.5 * atr) or not (lo < last < hi):
        return None
    e = ema(list(c), EMA_LEN)
    rising, falling = e[-1] > e[-1 - BOX_BARS], e[-1] < e[-1 - BOX_BARS]
    mid = (hi + lo) / 2
    if last > e[-1] and rising:
        side, entry = "buy", hi + 0.1 * atr
        stop = mid
        target = entry + 2 * (entry - stop)
    elif last < e[-1] and falling:
        side, entry = "sell", lo - 0.1 * atr
        stop = mid
        target = entry - 2 * (stop - entry)
    else:
        return None
    return {"symbol": sym, "name": name, "side": side, "last": last, "entry": entry, "stop": stop,
            "target": target, "risk": abs(entry - stop), "atr": atr, "tightness": width / atr,
            "candle_end": t[-1] + 900}


def message(s, now):
    buy = s["side"] == "buy"
    f = lambda x: fmt(x, s["atr"])  # noqa: E731
    lines = [
        "%s %s: أمر %s معلّق" % ("🟢" if buy else "🔴", s["name"], "شراء (Buy Stop)" if buy else "بيع (Sell Stop)"),
        "السعر الآن %s" % f(s["last"]),
        "%s إذا وصل %s" % ("اشترِ" if buy else "بِع", f(s["entry"])),
        "الهدف %s | الوقف %s" % (f(s["target"]), f(s["stop"])),
        "المخاطرة %s والربح %s (1:2)" % (f(s["risk"]), f(2 * s["risk"])),
        "صالح حتى %s بتوقيت باريس. إن لم يتفعّل، ألغِ الأمر." % paris_time(now + VALID_MIN * 60),
        "الأسعار من Yahoo (%s). إن اختلف سعر منصتك، طبّق نفس المسافات على سعرها." % s["symbol"],
        "تنبيه رادار بلا اختبار تاريخي، بطلبك. ليس نصيحة مالية.",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    now = time.time()
    os.makedirs(os.path.join(HERE, "state"), exist_ok=True)
    spath = os.path.join(HERE, "state", "setups.json")
    sent = json.load(open(spath, encoding="utf-8")) if os.path.exists(spath) else {}

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
        if not a.dry_run:
            try:
                notify("%s %s" % ("BUY" if s["side"] == "buy" else "SELL", s["symbol"]), body)
            except Exception as e:
                print("notify failed: %s" % e)
            sent["%s|%s" % (s["symbol"], s["side"])] = now
    print("setups found %d, new %d" % (len(found), len(fresh)))
    if not a.dry_run:
        with open(spath, "w", encoding="utf-8") as fh:
            json.dump(sent, fh)


if __name__ == "__main__":
    main()
