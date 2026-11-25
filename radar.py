"""Fast-mover radar: alert the phone when a US stock is surging, using the rule in radar/rules.json.

Runs every few minutes from GitHub Actions (.github/workflows/radar.yml), standard library only.
1. Candidates: Yahoo's day_gainers, small_cap_gainers and most_actives lists.
2. For each candidate, 5-minute bars for the last 10 days, regular session.
3. The SAME signal as radar/backtest.py, checked on the last few COMPLETED bars of today.
4. One alert per stock per day (state in radar/state/, kept between runs by actions/cache).
5. Push to the phone with ntfy (NTFY_TOPIC) and/or Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).

This is an alert, not a team proposal: it skips the debate in CLAUDE.md by the user's explicit
choice (2026-11-25). It never places orders.

    python3 radar/radar.py            # live
    python3 radar/radar.py --dry-run  # print, do not send, do not record
    python3 radar/radar.py --test     # send one test notification
"""
import argparse
import calendar
import concurrent.futures
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (radar; personal use)"}
SCREENS = ("day_gainers", "small_cap_gainers", "most_actives")
LOOKBACK_BARS = 3  # check the last 3 completed bars (~15 min) so a late run does not miss a signal


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_rules():
    with open(os.path.join(HERE, "rules.json"), encoding="utf-8") as fh:
        return json.load(fh)


def candidates(rules):
    out = {}
    for scr in SCREENS:
        url = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?"
               "scrIds=%s&count=100" % scr)
        try:
            quotes = get_json(url)["finance"]["result"][0]["quotes"]
        except Exception as e:
            print("screen %s failed: %s" % (scr, e))
            continue
        for q in quotes:
            sym, price = q.get("symbol"), q.get("regularMarketPrice")
            chg = q.get("regularMarketChangePercent")
            if not sym or price is None or "." in sym:
                continue
            if not (rules["min_price"] <= price <= rules["max_price"]):
                continue
            # the rule needs the stock up on the day; skip the rest before fetching any chart
            if chg is not None and chg < rules["day_up_pct"] - 0.5:
                continue
            out[sym] = q
    return out


def day_bars(sym, bar_min=5, rng=None):
    rng = rng or ("7d" if bar_min == 1 else "10d")
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%dm" % (
        urllib.parse.quote(sym), rng, bar_min)
    res = get_json(url)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    days, cur_date = [], None
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c) or v is None:
            continue
        d = time.gmtime(t)[:3]
        if d != cur_date:
            days.append({"date": "%04d-%02d-%02d" % d, "t": [], "o": [], "h": [], "l": [], "c": [], "v": []})
            cur_date = d
        for k, x in zip(("t", "o", "h", "l", "c", "v"), (t, o, h, l, c, v)):
            days[-1][k].append(x)
    return days


def running_vwap(h, l, c, v):
    pv = vol = 0.0
    out = []
    for hh, ll, cc, vv in zip(h, l, c, v):
        pv += (hh + ll + cc) / 3 * vv
        vol += vv
        out.append(pv / vol if vol else cc)
    return out


def signal_features(o, h, l, c, v, vwap, t, n, avg, prev_close):
    """Everything known at the CLOSE of bar t (no later bar is read). Shared with backtest.py."""
    rng = h[t] - l[t]
    past = range(max(0, t - 12), t)
    return {
        "rise": c[t] / c[t - n] - 1,
        "day_up": c[t] / prev_close - 1,
        "rel_vol": sum(v[t - n + 1:t + 1]) / (avg * n),
        "vwap_ext": c[t] / vwap[t] - 1,
        "gap": o[0] / prev_close - 1,
        "new_high": 1.0 if t == 0 or c[t] >= max(h[:t]) else 0.0,
        "upper_wick": (h[t] - c[t]) / rng if rng > 0 else 0.0,
        "bar_range": (sum((h[k] - l[k]) / c[k] for k in past) / len(past)) if t > 0 else 0.0,
        "tod_bar": float(t),
        "price": c[t],
    }


def passes(rules, f):
    if (f["rise"] < rules["rise_pct"] / 100 or f["day_up"] < rules["day_up_pct"] / 100
            or f["rel_vol"] < rules["rel_vol"] or f["vwap_ext"] < 0):
        return False
    for flt in rules.get("filters", []):
        x = f[flt["feature"]]
        if flt["op"] == ">=" and x < flt["value"]:
            return False
        if flt["op"] == "<=" and x > flt["value"]:
            return False
    return True


BASELINE = {}  # loop mode: {(symbol, date): (avg bar volume of the 5 previous days, previous close)}


def check(sym, rules, now):
    bar_min = rules.get("bar_minutes", 5)
    date = time.strftime("%Y-%m-%d", time.gmtime(now))
    key = (sym, date)
    if key in BASELINE:  # the previous days do not change during the day: fetch only today
        days = day_bars(sym, bar_min, "1d")
        avg, prev_close = BASELINE[key]
    else:
        days = day_bars(sym, bar_min)
        min_bars = 350 if bar_min == 1 else 70
        prev = [d for d in days[:-1] if len(d["c"]) >= min_bars][-5:]
        if len(prev) < 3 or len(days) < 2:
            return None
        avg = sum(sum(d["v"]) for d in prev) / sum(len(d["v"]) for d in prev)
        prev_close = days[-2]["c"][-1]
        BASELINE[key] = (avg, prev_close)
    if not days:
        return None
    today = days[-1]
    if today["date"] != date:
        return None  # market closed today
    # completed bars only: a bar is complete bar_min minutes after its timestamp
    done = sum(1 for t in today["t"] if t + 60 * bar_min <= now)
    if done == 0 or avg <= 0 or prev_close <= 0:
        return None
    o, h, l, c, v = (today[k] for k in ("o", "h", "l", "c", "v"))
    vwap = running_vwap(h, l, c, v)
    n = rules["bars"]
    first, last = rules["first_signal_bar"], rules["last_signal_bar"]
    for t in range(max(first, n, done - LOOKBACK_BARS), min(done - 1, last) + 1):
        if c[t - n] <= 0:
            continue
        f = signal_features(o, h, l, c, v, vwap, t, n, avg, prev_close)
        if passes(rules, f):
            closed_at = today["t"][t] + 60 * bar_min
            if now - closed_at > 60 * rules.get("max_alert_age_min", 99):
                return None  # too late: the backtest shows the edge is gone after a few minutes
            return {"symbol": sym, "price": c[t], "rise": f["rise"], "day_up": f["day_up"],
                    "rel_vol": f["rel_vol"], "bar_time": today["t"][t], "closed_at": closed_at,
                    "last_price": c[done - 1]}
    return None


def paris_time(ts):
    """Europe/Paris without tzdata: UTC+2 from 01:00 UTC on the last Sunday of March to the
    last Sunday of October, UTC+1 otherwise."""
    y = time.gmtime(ts).tm_year

    def last_sunday_0100(month):
        d = datetime.date(y, month, calendar.monthrange(y, month)[1])
        d -= datetime.timedelta(days=(d.weekday() - 6) % 7)
        return datetime.datetime(d.year, d.month, d.day, 1, tzinfo=datetime.timezone.utc).timestamp()
    off = 7200 if last_sunday_0100(3) <= ts < last_sunday_0100(10) else 3600
    return time.strftime("%H:%M", time.gmtime(ts + off))


def message(hit, rules):
    p = hit["last_price"]
    tgt, stp = rules["target_pct"], rules["stop_pct"]
    bar_min = rules.get("bar_minutes", 5)
    age = rules.get("max_alert_age_min")
    lines = [
        "⚡ %s يصعد بسرعة: +%.1f%% في %d دقيقة، +%.1f%% اليوم، الحجم ×%.1f" % (
            hit["symbol"], hit["rise"] * 100, rules["bars"] * bar_min, hit["day_up"] * 100, hit["rel_vol"]),
        "السعر الآن تقريباً %.2f$ (الإشارة %s بتوقيت باريس، الإرسال %s)" % (
            p, paris_time(hit["closed_at"]), paris_time(time.time())),
    ]
    if age:
        lines.append("⏱ ادخل فقط خلال %d دقيقة من الإشارة (حتى %s). بعدها تجاهل: الاختبار يُظهر أن الفرصة تختفي." % (
            age + 1, paris_time(hit["closed_at"] + 60 * (age + 1))))
    lines += [
        "خطة القاعدة: هدف +%.1f%% (%.2f$)، وقف −%.1f%% (%.2f$)، خروج بعد %d دقيقة كحد أقصى" % (
            tgt, p * (1 + tgt / 100), stp, p * (1 - stp / 100), rules["max_hold_min"]),
        rules.get("history_note") or "تاريخياً: ربحت %s%% من %s إشارة (اختبار على بيانات لم تُستخدم في الاختيار)" % (
            rules["test_win_pct"], rules["test_n"]),
        "تنبيه رادار فقط، بلا نقاش الفريق. ليس نصيحة مالية.",
    ]
    return "\n".join(lines)


def notify(title, body):
    sent = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic), data=body.encode("utf-8"),
                                     headers={"Title": title, "Priority": "high", "Tags": "zap"})
        urllib.request.urlopen(req, timeout=15).read()
        sent = True
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": body}).encode("utf-8")
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data=data, timeout=15).read()
        sent = True
    if not sent:
        print("(no NTFY_TOPIC or TELEGRAM_* set: printed only)")
    return sent


def state_path(now):
    os.makedirs(os.path.join(HERE, "state"), exist_ok=True)
    return os.path.join(HERE, "state", "alerted-%s.json" % time.strftime("%Y-%m-%d", time.gmtime(now)))


def scan_once(rules, alerted, dry_run):
    now = time.time()
    cands = candidates(rules)

    def one(sym):
        try:
            return check(sym, rules, now)
        except Exception as e:
            print("  %s: %s" % (sym, e))
            return None
    todo = [s for s in cands if s not in alerted]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        hits = [h for h in pool.map(one, todo) if h]
    hits.sort(key=lambda h: -h["rel_vol"])
    for hit in hits[:rules.get("max_alerts_per_run", 3)]:
        body = message(hit, rules)
        print(body + "\n", flush=True)
        if not dry_run:
            try:
                notify("Radar: %s" % hit["symbol"], body)
            except Exception as e:
                print("notify failed: %s" % e)
            alerted.append(hit["symbol"])
    print("%s candidates %d, hits %d" % (time.strftime("%H:%M:%S", time.gmtime(now)), len(cands), len(hits)),
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--loop-until-utc", metavar="HH:MM",
                    help="keep scanning once a minute until this UTC time (one Actions job per day)")
    a = ap.parse_args()
    if a.test:
        notify("Radar test", "✅ رادار التداول متصل. ستصلك التنبيهات هنا.")
        return
    rules = load_rules()
    path = state_path(time.time())
    alerted = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    if not a.loop_until_utc:
        scan_once(rules, alerted, a.dry_run)
    else:
        hh, mm = (int(x) for x in a.loop_until_utc.split(":"))
        g = time.gmtime()
        stop = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, hh, mm, 0))
        while time.time() < stop:
            try:
                scan_once(rules, alerted, a.dry_run)
            except Exception as e:
                print("scan failed: %s" % e, flush=True)
            # next scan ~15 s after the next minute starts (Yahoo needs a moment to publish the bar)
            time.sleep(max(5, 60 - (time.time() % 60) + 15))
    if not a.dry_run:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(alerted, fh)


if __name__ == "__main__":
    sys.exit(main())
