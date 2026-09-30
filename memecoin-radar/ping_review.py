"""
Ping review: what did every radar ping actually do afterwards?

Reads your recent pings from ntfy, looks up each coin's 1-minute price history (GeckoTerminal), and prints:
market cap at the ping, the best it reached after, the worst drop before that, where it is now, and what the
paper rules (sell half at 2x, stop -30%) would have done. It also posts the table to a private ntfy topic so
Claude can read it.

    python ping_review.py            # last 12 hours
    python ping_review.py 24         # last 24 hours
Read-only: it never trades or touches a wallet.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

ALERT_TOPIC = "scout-alert-c639f2f6f2bd1f4401"
REVIEW_TOPIC = "scout-review-c639f2f6f2bd1f4401"   # Claude reads the result from here
UA = {"User-Agent": "memecoin-radar-review/1.0", "Accept": "application/json"}
NETS = {"solana": "solana", "base": "base"}


def get(url):
    """GET with patience for GeckoTerminal's free limit: waits 30s, 60s, 90s on 'Too Many Requests'."""
    for wait in (30, 60, 90, None):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait is None:
                raise
            print(f"   (rate limited - waiting {wait}s)")
            time.sleep(wait)


def fetch_pings(hours):
    raw = get(f"https://ntfy.sh/{ALERT_TOPIC}/json?poll=1&since={hours}h")
    out = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") != "message":
            continue
        msg = d.get("message", "")
        ca = re.search(r"CA: (\S+)", msg)
        mc = re.search(r"MCap \$([\d.]+)([KMB]?)", msg)
        chain = re.search(r"\) on (\w+)", msg)
        if not (ca and mc):
            continue
        mcap = float(mc.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[mc.group(2)]
        kind = d.get("title", "").split(":")[0].split(" (")[0]
        out.append({"t": d["time"], "title": d.get("title", ""), "kind": kind, "ca": ca.group(1), "mcap": mcap,
                    "chain": (chain.group(1) if chain else "solana").lower()})
    return out


def best_pool(chain, ca):
    pairs = json.loads(get(f"https://api.dexscreener.com/tokens/v1/{chain}/{ca}")) or []
    pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == ca] or pairs
    if not pairs:
        return None, None, None
    p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    return p.get("pairAddress"), (p.get("baseToken") or {}).get("symbol", "?"), \
        float(p.get("marketCap") or p.get("fdv") or 0)


def candles(chain, pool, since_ts):
    """1-minute candles [ts, o, h, l, c, v] from since_ts to now (GeckoTerminal, up to 1000 per call)."""
    rows, before = [], int(time.time())
    for _ in range(3):
        d = json.loads(get(f"https://api.geckoterminal.com/api/v2/networks/{NETS[chain]}/pools/{pool}/ohlcv/minute"
                           f"?aggregate=1&limit=1000&before_timestamp={before}&currency=usd"))
        lst = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        if not lst:
            break
        rows += lst
        oldest = min(r[0] for r in lst)
        if oldest <= since_ts:
            break
        before = oldest
        time.sleep(6)
    return sorted({r[0]: r for r in rows}.values())


def paper(xs, stop=0.70, trail=0.40):
    """Sell half at 2x, stop before that (stop=0 means no stop), then trail off the peak. xs = [(low, high, close)]
    as multiples of the entry price. A stop fills at that minute's close if it gapped through (e/acc went to 0.01x)."""
    peak, half, realised = 1.0, False, 0.0
    for lo, hi, cl in xs:
        if not half:
            if stop and lo <= stop:   # assume the stop hit first if both happen in the same minute (worst case)
                return min(stop, cl)
            if hi >= 2.0:
                half, realised, peak = True, 1.0, hi
                continue
        else:
            peak = max(peak, hi)
            if lo <= peak * (1 - trail):
                return realised + 0.5 * min(peak * (1 - trail), cl)
    last = xs[-1][2] if xs else 1.0
    return realised + (0.5 if half else 1.0) * last


def fmt(x):
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}K"


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    pings = fetch_pings(hours)
    print(f"{len(pings)} coin pings in the last {hours}h - looking each one up (about 5s per coin)...\n")
    lines, total, total_lotto = [], 0.0, 0.0
    for p in pings:
        try:
            pool, sym, mcap_now = best_pool(p["chain"], p["ca"])
            if not pool:
                lines.append(f"{p['title'][:40]} | no pool found")
                continue
            cs = candles(p["chain"], pool, p["t"])
            after = [c for c in cs if c[0] >= p["t"] - 60]
            if not after:
                lines.append(f"{p['title'][:40]} | no price history")
                continue
            entry = after[0][4] if after[0][0] < p["t"] else after[0][1]
            xs = [(c[3] / entry, c[2] / entry, c[4] / entry) for c in after]
            peak_i = max(range(len(xs)), key=lambda i: xs[i][1])
            peak_x = xs[peak_i][1]
            low_before_peak = min(x[0] for x in xs[: peak_i + 1])
            mins_to_peak = (after[peak_i][0] - p["t"]) / 60
            now_x = mcap_now / p["mcap"] if p["mcap"] else 0
            res = paper(xs)
            lotto = paper(xs, stop=0, trail=0.50)
            total += (res - 1) * 50
            total_lotto += (lotto - 1) * 50
            t = time.strftime("%H:%M", time.localtime(p["t"]))
            lines.append(f"{t} {p['kind'][:14]} ${sym} at {fmt(p['mcap'])} | peak {peak_x:.2f}x after {mins_to_peak:.0f}m"
                         f" (dipped to {low_before_peak:.2f}x first) | now {now_x:.2f}x ({fmt(mcap_now)})"
                         f" | rules {(res - 1) * 50:+.0f} | lotto {(lotto - 1) * 50:+.0f} GBP")
        except Exception as e:
            lines.append(f"{p['title'][:40]} | error: {e}")
        print(lines[-1])
        time.sleep(6)
    summary = (f"TOTAL if GBP50 on every ping: rules (stop -30%) {total:+.0f} GBP | "
               f"lotto (no stop, half at 2x, trail 50%) {total_lotto:+.0f} GBP")
    print("\n" + summary)
    body = "\n".join(lines + [summary])
    req = urllib.request.Request("https://ntfy.sh/", headers={"Content-Type": "application/json", **UA}, method="POST",
                                 data=json.dumps({"topic": REVIEW_TOPIC, "title": "Ping review",
                                                  "message": body[:3900], "priority": 2}).encode("utf-8"))
    try:
        urllib.request.urlopen(req, timeout=20)
        print("(sent to Claude's review topic)")
    except Exception as e:
        print(f"(couldn't send to ntfy: {e})")


if __name__ == "__main__":
    main()
