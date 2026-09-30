#!/usr/bin/env python3
"""
Memecoin Radar - real-time early-runner alerts to your phone (ALERTS ONLY, never trades).

What it watches (every minute or so):
  1. EARLY RUNNERS  - new Solana/Base coins on DexScreener's latest profiles/boosts feeds that are
                      pumping hard on real volume with more buyers than sellers (MEMECARDS/GOCARDS-type).
  2. GRADUATIONS    - pump.fun coins that just migrated (~$69K) via the PumpPortal live feed; re-checked
                      10 minutes later and only alerted if they are HOLDING (not dumping like GOCARDS).
  3. KEYWORDS       - catalyst phrases you put in keywords.txt (e.g. "super intelligence", "SI").
                      Any Solana/Base coin matching them that starts moving gets flagged (Super Inu-type).

Every candidate is safety-checked with RugCheck (Solana) before it pings you.
Alerts go to your phone through the free ntfy app (topic below).

Requirements: Python 3.9+   and   pip install websockets   (websockets only needed for GRADUATIONS)
Run:          python radar.py
Stop:         Ctrl+C
"""

import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ----------------------------- SETTINGS (edit freely) -----------------------------
NTFY_TOPIC = "scout-alert-c639f2f6f2bd1f4401"   # the topic your phone's ntfy app is subscribed to
CHAINS = {"solana", "base"}                     # chains you can buy in the Coinbase app

# Early-runner rules (new coins)
RUNNER_MIN_MCAP = 30_000          # ignore dust
RUNNER_MAX_MCAP = 5_000_000       # above this it's no longer "early"
RUNNER_MAX_AGE_HOURS = 48         # only young coins
RUNNER_MIN_H1_CHANGE = 50         # % price rise in the last hour ...
RUNNER_MIN_M5_CHANGE = 20         # ... or % rise in the last 5 minutes
RUNNER_MIN_H1_VOLUME = 20_000     # $ traded in the last hour
RUNNER_MIN_H1_BUYS = 100          # real crowd, not 5 wallets
RUNNER_MIN_LIQUIDITY = 8_000      # $ in the pool

# Graduation rules (pump.fun coins that just migrated)
GRAD_RECHECK_MINUTES = 10         # wait, then check it's holding
GRAD_MIN_MCAP = 80_000            # still above the ~$69K migration point after the wait

# Keyword rules (catalyst phrases)
KEYWORD_FILE = "keywords.txt"
KEYWORD_MIN_H1_CHANGE = 20
KEYWORD_MIN_H1_VOLUME = 10_000
KEYWORD_MAX_MCAP = 30_000_000

# Safety / noise
MAX_ALERTS_PER_HOUR = 6
REALERT_IF_MCAP_MULTIPLIED = 2.0  # alert the same coin again only if its mcap doubled since last alert
POLL_SECONDS = 60
KEYWORD_POLL_SECONDS = 180
STATE_FILE = "radar_state.json"
# ------------------------------------------------------------------------------------

UA = {"User-Agent": "memecoin-radar/1.0", "Accept": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fmt_usd(x):
    x = float(x or 0)
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.0f}K"
    return f"${x:.0f}"


# ----------------------------- state / dedupe -----------------------------
class State:
    def __init__(self, path):
        self.path = path
        self.alerted = {}      # token address -> mcap at last alert
        self.sent_times = []   # timestamps of alerts (for hourly cap)
        try:
            with open(path) as f:
                self.alerted = json.load(f).get("alerted", {})
        except Exception:
            pass

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump({"alerted": self.alerted}, f)
        except Exception as e:
            log(f"could not save state: {e}")

    def should_alert(self, addr, mcap):
        last = self.alerted.get(addr)
        if last is None:
            return True
        return mcap >= last * REALERT_IF_MCAP_MULTIPLIED

    def under_cap(self):
        now = time.time()
        self.sent_times = [t for t in self.sent_times if now - t < 3600]
        return len(self.sent_times) < MAX_ALERTS_PER_HOUR

    def record(self, addr, mcap):
        self.alerted[addr] = mcap
        self.sent_times.append(time.time())
        self.save()


# ----------------------------- data sources -----------------------------
def dex_pairs_for_tokens(chain, addresses):
    """Best (highest-liquidity) pair per token via DexScreener /tokens/v1 (max 30 per call)."""
    best = {}
    for i in range(0, len(addresses), 30):
        chunk = ",".join(addresses[i:i + 30])
        try:
            pairs = http_json(f"https://api.dexscreener.com/tokens/v1/{chain}/{chunk}")
        except Exception as e:
            log(f"dexscreener tokens error: {e}")
            continue
        for p in pairs or []:
            addr = (p.get("baseToken") or {}).get("address")
            if not addr:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if addr not in best or liq > float((best[addr].get("liquidity") or {}).get("usd") or 0):
                best[addr] = p
    return best


def dex_latest_tokens():
    """Newest token profiles + boosts (DexScreener), grouped by chain."""
    out = {}
    for url in ("https://api.dexscreener.com/token-profiles/latest/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1"):
        try:
            items = http_json(url)
        except Exception as e:
            log(f"dexscreener feed error: {e}")
            continue
        for it in items or []:
            chain, addr = it.get("chainId"), it.get("tokenAddress")
            if chain in CHAINS and addr:
                out.setdefault(chain, set()).add(addr)
    return {c: list(a) for c, a in out.items()}


def dex_search(q):
    try:
        return (http_json("https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(q)) or {}).get("pairs") or []
    except Exception as e:
        log(f"dexscreener search error for {q!r}: {e}")
        return []


def rugcheck(mint):
    """Returns (ok, notes). ok=False on serious red flags. Unknown -> ok=True with note."""
    try:
        r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    except Exception as e:
        return True, f"RugCheck unavailable ({type(e).__name__}) - check manually"
    risks = r.get("risks") or []
    names = [str(x.get("name", "")) for x in risks]
    danger = [str(x.get("name", "")) for x in risks if str(x.get("level", "")).lower() == "danger"]
    lowered = " ".join(names).lower()
    hard_fail = ("mint authority" in lowered) or ("freeze authority" in lowered) or len(danger) >= 2
    score = r.get("score_normalised", r.get("score"))
    notes = f"RugCheck score {score}; risks: {', '.join(names[:4]) or 'none listed'}"
    return (not hard_fail), notes


# ----------------------------- pair metrics -----------------------------
def metrics(p):
    g = lambda d, k: float((p.get(d) or {}).get(k) or 0)
    tx = p.get("txns") or {}
    h1 = tx.get("h1") or {}
    created = p.get("pairCreatedAt") or 0
    age_h = (time.time() * 1000 - created) / 3_600_000 if created else 9999
    return {
        "name": (p.get("baseToken") or {}).get("name", "?"),
        "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
        "addr": (p.get("baseToken") or {}).get("address", ""),
        "chain": p.get("chainId", "?"),
        "url": p.get("url", ""),
        "mcap": float(p.get("marketCap") or p.get("fdv") or 0),
        "liq": g("liquidity", "usd"),
        "vol_h1": g("volume", "h1"),
        "vol_h24": g("volume", "h24"),
        "chg_m5": g("priceChange", "m5"),
        "chg_h1": g("priceChange", "h1"),
        "chg_h6": g("priceChange", "h6"),
        "buys_h1": int(h1.get("buys") or 0),
        "sells_h1": int(h1.get("sells") or 0),
        "age_h": age_h,
    }


def is_early_runner(m):
    return (
        m["chain"] in CHAINS
        and RUNNER_MIN_MCAP <= m["mcap"] <= RUNNER_MAX_MCAP
        and m["age_h"] <= RUNNER_MAX_AGE_HOURS
        and (m["chg_h1"] >= RUNNER_MIN_H1_CHANGE or m["chg_m5"] >= RUNNER_MIN_M5_CHANGE)
        and m["vol_h1"] >= RUNNER_MIN_H1_VOLUME
        and m["buys_h1"] >= RUNNER_MIN_H1_BUYS
        and m["buys_h1"] >= m["sells_h1"]
        and m["liq"] >= RUNNER_MIN_LIQUIDITY
    )


def is_keyword_mover(m):
    return (
        m["chain"] in CHAINS
        and m["mcap"] <= KEYWORD_MAX_MCAP
        and m["vol_h1"] >= KEYWORD_MIN_H1_VOLUME
        and m["chg_h1"] >= KEYWORD_MIN_H1_CHANGE
        and m["buys_h1"] >= m["sells_h1"]
    )


# ----------------------------- alerting -----------------------------
def send_ntfy(title, body, click=None, priority="high", tags="rotating_light"):
    headers = {"Title": title[:120], "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
    except Exception as e:
        log(f"ntfy FAILED: {e}")
        return False
    log(f"ntfy sent: {title}")
    return ok


def alert(state, kind, m, extra=""):
    if not state.should_alert(m["addr"], m["mcap"]):
        return
    if not state.under_cap():
        log(f"hourly alert cap reached, skipping {m['symbol']}")
        return
    ok, rc_notes = (True, "RugCheck: n/a on Base - check GoPlus/GMGN")
    if m["chain"] == "solana":
        ok, rc_notes = rugcheck(m["addr"])
    if not ok:
        log(f"RugCheck FAIL, skipped {m['symbol']}: {rc_notes}")
        state.alerted[m["addr"]] = m["mcap"]  # don't recheck the same rug every minute
        state.save()
        return
    # Strong setup = big move on heavy volume with buyers clearly outnumbering sellers -> urgent "act now" ping
    strong = (m["chg_h1"] >= 100 and m["buys_h1"] >= 1.5 * max(m["sells_h1"], 1)
              and m["vol_h1"] >= 50_000 and m["mcap"] <= 1_000_000)
    if strong:
        title = f"ACT NOW (10-min window): ${m['symbol']} {fmt_usd(m['mcap'])} {m['chg_h1']:+.0f}% 1h"
        extra = ("STRONG SETUP: buyers heavily outnumber sellers on big volume. If GMGN checks pass, "
                 "enter GBP50-100 now; don't wait for it to 'confirm'.\n" + extra)
    else:
        title = f"{kind}: ${m['symbol']} {fmt_usd(m['mcap'])} ({m['chg_h1']:+.0f}% 1h)"
    body = (
        f"{m['name']} (${m['symbol']}) on {m['chain']}\n"
        f"CA: {m['addr']}\n"
        f"MCap {fmt_usd(m['mcap'])} | Liq {fmt_usd(m['liq'])} | Vol 1h {fmt_usd(m['vol_h1'])}\n"
        f"5m {m['chg_m5']:+.0f}% | 1h {m['chg_h1']:+.0f}% | buys/sells 1h {m['buys_h1']}/{m['sells_h1']} | age {m['age_h']:.1f}h\n"
        f"{rc_notes}\n{extra}\n"
        f"BEFORE BUYING: check global fees >=1.5 SOL + bundlers/snipers on GMGN, paste CA into Coinbase.\n"
        f"PLAN: max GBP50-100, sell half at 2x, hard stop -30%. Most of these die - it's a watch, not a promise."
    )
    if send_ntfy(title, body, click=m["url"] or None, priority="urgent" if strong else "high",
                 tags="rotating_light,moneybag" if strong else "rotating_light"):
        state.record(m["addr"], m["mcap"])


# ----------------------------- loops -----------------------------
async def runners_loop(state):
    loop = asyncio.get_running_loop()
    while True:
        try:
            latest = await loop.run_in_executor(None, dex_latest_tokens)
            for chain, addrs in latest.items():
                pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, chain, addrs)
                for p in pairs.values():
                    m = metrics(p)
                    if is_early_runner(m):
                        await loop.run_in_executor(None, alert, state, "EARLY RUNNER", m, "")
            log(f"runner scan done ({sum(len(a) for a in latest.values())} fresh tokens checked)")
        except Exception as e:
            log(f"runner loop error: {e}")
        await asyncio.sleep(POLL_SECONDS)


def load_keywords():
    path = os.path.join(HERE, KEYWORD_FILE)
    try:
        with open(path) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        return []


async def keywords_loop(state):
    loop = asyncio.get_running_loop()
    while True:
        for kw in load_keywords():
            pairs = await loop.run_in_executor(None, dex_search, kw)
            best = {}
            for p in pairs:
                m = metrics(p)
                if m["chain"] in CHAINS and m["addr"]:
                    if m["addr"] not in best or m["vol_h24"] > best[m["addr"]]["vol_h24"]:
                        best[m["addr"]] = m
            movers = sorted((m for m in best.values() if is_keyword_mover(m)), key=lambda m: -m["vol_h1"])
            for m in movers[:2]:
                await loop.run_in_executor(None, alert, state, "KEYWORD", m, f'Matched catalyst keyword: "{kw}"')
            await asyncio.sleep(3)  # be gentle with the API
        await asyncio.sleep(KEYWORD_POLL_SECONDS)


async def graduations_loop(state):
    try:
        import websockets  # noqa
    except ImportError:
        log("GRADUATIONS off: run  pip install websockets  to enable pump.fun migration alerts")
        return
    import websockets
    loop = asyncio.get_running_loop()

    async def recheck(mint):
        await asyncio.sleep(GRAD_RECHECK_MINUTES * 60)
        pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", [mint])
        p = pairs.get(mint)
        if not p:
            return
        m = metrics(p)
        if (m["mcap"] >= GRAD_MIN_MCAP and m["chg_m5"] >= 0 and m["buys_h1"] >= m["sells_h1"]
                and m["vol_h1"] >= RUNNER_MIN_H1_VOLUME and m["liq"] >= RUNNER_MIN_LIQUIDITY):
            await loop.run_in_executor(None, alert, state, "GRADUATED & HOLDING", m,
                                       f"Migrated off pump.fun ~{GRAD_RECHECK_MINUTES} min ago and still holding. "
                                       f"Migration is where GOCARDS got dumped - be quick with stops.")

    while True:
        try:
            async with websockets.connect("wss://pumpportal.fun/api/data", ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log("connected to PumpPortal (graduations)")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    mint = msg.get("mint")
                    if mint:  # every message on this subscription is a migration event
                        asyncio.create_task(recheck(mint))
        except Exception as e:
            log(f"PumpPortal connection lost ({e}); reconnecting in 15s")
            await asyncio.sleep(15)


async def main():
    state = State(os.path.join(HERE, STATE_FILE))
    log("Memecoin Radar starting - ALERTS ONLY. Phone topic: " + NTFY_TOPIC)
    if "--test" in sys.argv:
        send_ntfy("Radar test", "Memecoin Radar is connected to your phone.", tags="white_check_mark")
        return
    await asyncio.gather(runners_loop(state), keywords_loop(state), graduations_loop(state))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped")
