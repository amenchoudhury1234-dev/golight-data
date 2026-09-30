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
                        story = real_world_match(m)
                        if story:
                            await loop.run_in_executor(None, alert, state, "RUNNER + REAL STORY", m,
                                                       f'REAL-WORLD STORY: matches trending "{story}" - these run '
                                                       f'longer than random coins (TILCAYO, Super Inu pattern).')
                        else:
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


# ----------------------------- AUTO KEYWORDS (you don't have to add any) -----------------------------
# "Attention arbitrage": mainstream attention (Trump's posts, Google searches, Wikipedia spikes, the AI
# routine's picks) shows up BEFORE crypto Twitter prices it in. We harvest trending phrases automatically,
# then check DexScreener for a Solana/Base coin with that name/ticker that is starting to move.

AUTO_KEYWORD_TTL_HOURS = 48
KW_TOPIC = "scout-kw-c639f2f6f2bd1f4401"   # the hourly Claude routine publishes AI-picked phrases here
TRUMP_FEED = "https://trumpstruth.org/feed"  # public RSS mirror of Trump's Truth Social posts

STOP = set("""the a an and or of to in on for with at by from is are was were be been it this that
these those as not but if then so we you they he she i our your their my me us them his her its
will would can could should just very more most much many all any some no yes new now today big
great good bad best news breaking update video live watch thank thanks president trump donald america
american united states people country world day time year years week""".split())


def _clean(s):
    return " ".join("".join(ch if ch.isalnum() or ch == " " else " " for ch in s).split())


def extract_phrases(text):
    """Pull memeable candidates from a post: quoted phrases, ALL-CAPS words, Capitalised 1-3 word phrases."""
    import re
    out = set()
    for q in re.findall(r'["“]([^"”]{3,40})["”]', text):
        q = _clean(q)
        if 1 <= len(q.split()) <= 4:
            out.add(q.lower())
    for w in re.findall(r"\b[A-Z]{4,15}\b", text):          # Trump-style CAPS words
        if w.lower() not in STOP:
            out.add(w.lower())
    for ph in re.findall(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2})\b", text):
        words = [w for w in ph.split() if w.lower() not in STOP]
        if words:
            out.add(" ".join(words).lower())
    return out


def fetch_rss_titles(url, limit=40):
    import xml.etree.ElementTree as ET
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "") + " " + (it.findtext("description") or "")
        items.append(t)
        if len(items) >= limit:
            break
    return items


def harvest_trump():
    try:
        phrases = set()
        for t in fetch_rss_titles(TRUMP_FEED, 15):
            phrases |= extract_phrases(t)
        return phrases
    except Exception as e:
        log(f"trump feed unavailable: {e}")
        return set()


def harvest_google_trends():
    phrases = set()
    for geo in ("US", "GB"):
        try:
            for t in fetch_rss_titles(f"https://trends.google.com/trending/rss?geo={geo}", 25):
                # the RSS title is the search term itself
                term = _clean(t.split("  ")[0])[:40].lower()
                if term and term not in STOP:
                    phrases.add(" ".join(term.split()[:4]))
        except Exception as e:
            log(f"google trends ({geo}) unavailable: {e}")
    return phrases


def harvest_wikipedia_spikes():
    """Pages that jumped into yesterday's top views vs the day before (new animals, people, events)."""
    from datetime import timedelta
    def top(day):
        url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/"
               f"{day:%Y/%m/%d}")
        arts = http_json(url)["items"][0]["articles"]
        return [a["article"] for a in arts[:200]]
    try:
        d1 = datetime.utcnow() - timedelta(days=1)
        y, before = top(d1), set(top(d1 - timedelta(days=1)))
        out = set()
        for a in y[:120]:
            if a in before or ":" in a or a in ("Main_Page",):
                continue
            out.add(_clean(a.replace("_", " ")).lower())
        return out
    except Exception as e:
        log(f"wikipedia spikes unavailable: {e}")
        return set()


def harvest_ai_topic():
    """Phrases the hourly Claude 'Catalyst watch' routine publishes (one per line) to the KW ntfy topic."""
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{KW_TOPIC}/json?poll=1&since=48h", headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            lines = r.read().decode("utf-8").splitlines()
        out = set()
        for ln in lines:
            try:
                msg = json.loads(ln).get("message", "")
            except Exception:
                continue
            for kw in msg.splitlines():
                kw = _clean(kw).lower()
                if 2 <= len(kw) <= 40:
                    out.add(kw)
        return out
    except Exception as e:
        log(f"AI keyword topic unavailable: {e}")
        return set()


def acronym(phrase):
    ws = phrase.split()
    return "".join(w[0] for w in ws).lower() if len(ws) >= 2 else ""


def coin_matches(phrase, m):
    """True if the coin's name/ticker genuinely matches the phrase (not just a loose search hit)."""
    p = phrase.replace(" ", "").lower()
    name = m["name"].replace(" ", "").lower()
    sym = m["symbol"].replace("$", "").lower()
    if len(p) >= 4 and (p in name or name in p and len(name) >= 4 or p == sym):
        return True
    ac = acronym(phrase)
    if ac and len(ac) >= 2 and sym == ac:              # "super intelligence" -> $SI
        return True
    first = phrase.split()[0].lower() if phrase.split() else ""
    if len(first) >= 4 and name.startswith(first) and any(x in name for x in ("inu", "coin", "cat", "dog")):
        return True                                   # "super ..." -> "Super Inu"
    return False


def harvest_polymarket_mentions():
    """PREDICTION-MARKET EDGE: Polymarket 'mention markets' ("What will Trump say during X?") list the exact
    words traders expect a VIP to say at an UPCOMING speech - before it happens. We load those words as
    keywords ahead of time, so the moment a word is said the radar is already watching matching coins."""
    import re
    out = set()
    try:
        events = http_json("https://gamma-api.polymarket.com/events?closed=false&limit=300&order=volume&ascending=false")
    except Exception as e:
        log(f"polymarket unavailable: {e}")
        return out
    for ev in events or []:
        title = str(ev.get("title", ""))
        if not re.search(r"\b(say|says|mention|mentions)\b", title, re.I):
            continue
        for mk in ev.get("markets") or []:
            word = mk.get("groupItemTitle") or ""
            if not word:
                q = re.findall(r'["“]([^"”]{2,40})["”]', str(mk.get("question", "")))
                word = q[0] if q else ""
            word = _clean(word).lower()
            if 2 <= len(word) <= 40 and word not in STOP:
                out.add(word)
    return out


AUTO = None  # shared AutoKeywords instance (runners loop uses it to tag real-world-backed coins)


class AutoKeywords:
    def __init__(self):
        self.seen = {}        # phrase -> first-seen timestamp
        self.last_harvest = 0

    def refresh(self):
        if time.time() - self.last_harvest < 15 * 60:
            return
        self.last_harvest = time.time()
        found = set()
        found |= harvest_ai_topic()
        found |= harvest_trump()
        found |= harvest_google_trends()
        found |= harvest_wikipedia_spikes()
        found |= harvest_polymarket_mentions()
        now = time.time()
        for ph in found:
            self.seen.setdefault(ph, now)
        cutoff = now - AUTO_KEYWORD_TTL_HOURS * 3600
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        log(f"auto-keywords: {len(found)} harvested, {len(self.seen)} active")

    def active(self, limit=80):
        newest = sorted(self.seen.items(), key=lambda kv: -kv[1])
        return [k for k, _ in newest[:limit]]


def real_world_match(m):
    """Return the trending real-world phrase this coin matches, if any (Trump post / Google / Wikipedia /
    Polymarket / AI routine). Coins backed by a real story tend to run longer than random pump.fun coins."""
    if AUTO is None:
        return None
    for kw in AUTO.active(200):
        if coin_matches(kw, m):
            return kw
    return None


async def keywords_loop(state):
    global AUTO
    loop = asyncio.get_running_loop()
    auto = AUTO = AutoKeywords()
    while True:
        await loop.run_in_executor(None, auto.refresh)
        manual = load_keywords()
        for kw in manual + [k for k in auto.active() if k not in manual]:
            pairs = await loop.run_in_executor(None, dex_search, kw)
            best = {}
            for p in pairs:
                m = metrics(p)
                if m["chain"] in CHAINS and m["addr"] and (kw in manual or coin_matches(kw, m)):
                    if m["addr"] not in best or m["vol_h24"] > best[m["addr"]]["vol_h24"]:
                        best[m["addr"]] = m
            movers = sorted((m for m in best.values() if is_keyword_mover(m)), key=lambda m: -m["vol_h1"])
            for m in movers[:2]:
                src = "your keywords.txt" if kw in manual else "auto (Trump posts / Google Trends / Wikipedia / AI routine)"
                await loop.run_in_executor(None, alert, state, "CATALYST", m,
                                           f'Matched trending phrase "{kw}" from {src}')
            await asyncio.sleep(1.2)  # stay well under DexScreener's 300/min search limit
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


# ----------------------------- SMART WALLETS (follow wallets, not influencers) -----------------------------
# Quiet wallets with long, verifiable on-chain records (from research on 2026-09-30; addresses came from
# public trackers - verify on gmgn.ai/sol/address/<addr> before trusting). A single wallet buying is noise;
# we alert only when 2+ of them buy the SAME coin within SMART_WINDOW_HOURS ("confluence").
SMART_WALLETS = {
    "Euris (anon, ~$10.5M realised, ~82% win rate)": "DfMxre4cKmvogbLrPigxmibVTTQDuzjdXojWzjCXXhzj",
    "Gake (~$2.5M/3mo, slower style)": "DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm",
    "GMGN smart-money anon": "H72yLkhTnoBfhBTXXaj1RBXuirm8s8G5fcVh2XpQLggM",
    "Loopierr (Kolscan)": "9yYya3F5EJoLnBNKW6z4bZvyQytMXzDcpU5D6yYr4jqL",
}
SMART_MIN_WALLETS = 2
SMART_WINDOW_HOURS = 6
SOLANA_RPC = "https://api.mainnet-beta.solana.com"   # free public RPC; swap for a Helius URL if you get a key
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",   # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(SOLANA_RPC, data=body, headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode()).get("result")


def token_buys_in_tx(tx, owner):
    """Mints whose balance for `owner` went UP in this transaction (i.e. the wallet bought them)."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return set()
    def bal(lst):
        out = {}
        for b in lst or []:
            if b.get("owner") == owner:
                amt = float(((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0)
                out[b.get("mint")] = out.get(b.get("mint"), 0) + amt
        return out
    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    return {m for m, v in post.items() if m and m not in IGNORE_MINTS and v > pre.get(m, 0) * 1.0001}


async def smart_wallets_loop(state):
    loop = asyncio.get_running_loop()
    seen_sigs = {w: set() for w in SMART_WALLETS.values()}
    first_pass = {w: True for w in SMART_WALLETS.values()}
    buys = {}   # mint -> {wallet_label: timestamp}
    while True:
        for label, wallet in SMART_WALLETS.items():
            try:
                sigs = await loop.run_in_executor(None, rpc, "getSignaturesForAddress", [wallet, {"limit": 15}])
            except Exception as e:
                log(f"smart wallet RPC error ({label[:12]}): {e}")
                continue
            new = [s["signature"] for s in (sigs or []) if s.get("signature") not in seen_sigs[wallet]]
            seen_sigs[wallet].update(new)
            if first_pass[wallet]:          # don't replay history on startup
                first_pass[wallet] = False
                continue
            for sig in new[:10]:
                try:
                    tx = await loop.run_in_executor(None, rpc, "getTransaction",
                                                    [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                except Exception:
                    continue
                for mint in token_buys_in_tx(tx, wallet):
                    buys.setdefault(mint, {})[label] = time.time()
                    log(f"smart wallet {label.split(' (')[0]} bought {mint[:8]}...")
                await asyncio.sleep(0.5)
            await asyncio.sleep(1)
        # confluence check
        cutoff = time.time() - SMART_WINDOW_HOURS * 3600
        for mint, who in list(buys.items()):
            who = {k: t for k, t in who.items() if t >= cutoff}
            if not who:
                buys.pop(mint, None)
                continue
            buys[mint] = who
            if len(who) >= SMART_MIN_WALLETS:
                pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", [mint])
                p = pairs.get(mint)
                if p:
                    m = metrics(p)
                    if m["mcap"] <= KEYWORD_MAX_MCAP and m["liq"] >= RUNNER_MIN_LIQUIDITY:
                        names = ", ".join(k.split(" (")[0] for k in who)
                        await loop.run_in_executor(None, alert, state, "SMART MONEY", m,
                                                   f"{len(who)} proven quiet wallets bought within {SMART_WINDOW_HOURS}h: {names}")
        await asyncio.sleep(POLL_SECONDS)


# ----------------------------- X / TWITTER VIP WATCH (Elon, Trump...) -----------------------------
# Needs an X API bearer token (pay-per-use, ~$0.005 per post read; Elon+Trump originals ~ $5-10/month).
# Put the token in x_token.txt next to this file. Without it, this detector is simply off.
X_ACCOUNTS = ["elonmusk", "realDonaldTrump"]      # add e.g. "WhaleInsider", "WatcherGuru" (each ~ +$10-15/mo)
X_POLL_SECONDS = 60
X_TOKEN_FILE = "x_token.txt"
EMOJI_WORDS = {"🦝": "raccoon", "🐸": "frog", "🐕": "dog", "🐶": "dog", "🐈": "cat", "🐱": "cat", "🦛": "hippo",
               "🐿": "squirrel", "🐧": "penguin", "🦍": "gorilla", "🐂": "bull", "🦅": "eagle", "🐻": "bear",
               "🐒": "monkey", "🐵": "monkey", "🦊": "fox", "🐹": "hamster", "🐰": "bunny", "🦈": "shark",
               "🐳": "whale", "🦄": "unicorn", "🐷": "pig", "🐢": "turtle", "🦖": "dino", "🚀": "rocket", "🍌": "banana"}


def x_get(path, token):
    req = urllib.request.Request("https://api.x.com/2/" + path,
                                 headers={"Authorization": f"Bearer {token}", **UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def vip_phrases(text):
    import re
    text = re.sub(r"https?://\S+", " ", text)   # drop links
    phrases = set(extract_phrases(text))
    for emo, word in EMOJI_WORDS.items():
        if emo in text:
            phrases.add(word)
    for tag in re.findall(r"[#$]([A-Za-z][A-Za-z0-9]{1,15})", text):
        phrases.add(tag.lower())
    # plain lowercase nouns in very short posts (Elon often posts 1-5 words)
    words = [w for w in _clean(text).lower().split() if len(w) >= 4 and w not in STOP]
    if len(words) <= 5:
        phrases.update(words)
    return {p for p in phrases if 2 <= len(p) <= 40}


async def x_vip_loop(state):
    import re
    try:
        with open(os.path.join(HERE, X_TOKEN_FILE)) as f:
            token = f.read().strip()
    except FileNotFoundError:
        log("X watch off: add your X API bearer token to x_token.txt to track Elon/Trump posts live")
        return
    loop = asyncio.get_running_loop()
    ids, since = {}, {}
    for name in X_ACCOUNTS:
        try:
            ids[name] = (await loop.run_in_executor(None, x_get, f"users/by/username/{name}", token))["data"]["id"]
        except Exception as e:
            log(f"X: couldn't resolve @{name}: {e}")
    log(f"X watch on for: {', '.join('@' + n for n in ids)}")
    while True:
        for name, uid in ids.items():
            q = "exclude=replies,retweets&tweet.fields=created_at&max_results=5"
            if since.get(name):
                q += f"&since_id={since[name]}"
            try:
                res = await loop.run_in_executor(None, x_get, f"users/{uid}/tweets?{q}", token)
            except Exception as e:
                log(f"X error @{name}: {e}")
                continue
            posts = res.get("data") or []
            if not posts:
                continue
            first_run = name not in since
            since[name] = posts[0]["id"]
            if first_run:            # don't act on old posts at startup
                continue
            for post in posts:
                text = post.get("text", "")
                log(f"@{name} posted: {text[:80]!r}")
                # 1) VIP named a contract address directly -> immediate urgent ping
                for ca in re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text):
                    send_ntfy(f"@{name} POSTED A CONTRACT ADDRESS", f"{text[:300]}\n\nCA: {ca}\n"
                              "Copycats appear within seconds - use ONLY this exact CA. Check GMGN first.",
                              click=f"https://dexscreener.com/solana/{ca}", priority="urgent", tags="rotating_light")
                # 2) memeable phrases -> feed the keyword engine + ping existing matching coins BEFORE they move
                phrases = vip_phrases(text)
                if AUTO is not None:
                    now = time.time()
                    for ph in phrases:
                        AUTO.seen[ph] = now      # newest = searched first, every 3 minutes
                matches = []
                for ph in list(phrases)[:6]:
                    for p in await loop.run_in_executor(None, dex_search, ph):
                        m = metrics(p)
                        if (m["chain"] in CHAINS and m["addr"] and coin_matches(ph, m)
                                and m["liq"] >= 20_000 and m["mcap"] <= KEYWORD_MAX_MCAP):
                            matches.append((ph, m))
                    await asyncio.sleep(1)
                best = {}
                for ph, m in matches:
                    if m["addr"] not in best or m["vol_h24"] > best[m["addr"]][1]["vol_h24"]:
                        best[m["addr"]] = (ph, m)
                top = sorted(best.values(), key=lambda x: -x[1]["vol_h24"])[:3]
                if top:
                    lines = "\n".join(f'- "{ph}" -> {m["name"]} ${m["symbol"]} {fmt_usd(m["mcap"])} | CA {m["addr"]}'
                                      for ph, m in top)
                    send_ntfy(f"@{name} just posted - matching coins", f'"{text[:200]}"\n\nExisting coins that match:\n'
                              f"{lines}\n\nThese haven't necessarily moved yet - this is the EARLIEST possible heads-up "
                              "(JIMOTHY did +331% after Elon's raccoon post). Check GMGN, max GBP50-100, half out at 2x.",
                              click=top[0][1]["url"] or None, priority="high", tags="bird")
            await asyncio.sleep(1)
        await asyncio.sleep(X_POLL_SECONDS)


# ----------------------------- SLEEPERS (second-wave detector) -----------------------------
# Coins tied to a famous animal/character/phrase often pump AGAIN when a VIP reposts the story, even without
# naming the coin: JIMOTHY (viral raccoon) went $3.8M -> $16.2M (+331%) on Aug 8 2026 after Elon posted a
# raccoon video; Super Inu went +130% on Sep 30 when Trump repeated "super intelligence". We watch these
# known "story coins" every minute and ping the moment volume wakes up. Add CAs to sleepers.txt (one per line,
# optional "# note").
SLEEPER_FILE = "sleepers.txt"
SLEEPER_MIN_H1_CHANGE = 20
SLEEPER_VOL_MULTIPLE = 3.0      # last hour's volume vs the average hour of the last 24h


def load_sleepers():
    try:
        with open(os.path.join(HERE, SLEEPER_FILE)) as f:
            return [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]
    except FileNotFoundError:
        return []


async def sleepers_loop(state):
    loop = asyncio.get_running_loop()
    while True:
        cas = load_sleepers()
        if cas:
            pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", cas)
            for p in pairs.values():
                m = metrics(p)
                avg_hour = m["vol_h24"] / 24 if m["vol_h24"] else 0
                if (m["chg_h1"] >= SLEEPER_MIN_H1_CHANGE and avg_hour > 0
                        and m["vol_h1"] >= SLEEPER_VOL_MULTIPLE * avg_hour
                        and m["buys_h1"] >= m["sells_h1"] and m["mcap"] <= KEYWORD_MAX_MCAP):
                    await loop.run_in_executor(None, alert, state, "SLEEPER WAKING", m,
                                               f"Known story coin waking up: 1h volume is {m['vol_h1']/avg_hour:.1f}x its "
                                               f"normal hour. Check X/news for a VIP repost (JIMOTHY/Elon pattern).")
        await asyncio.sleep(POLL_SECONDS)


async def main():
    state = State(os.path.join(HERE, STATE_FILE))
    log("Memecoin Radar starting - ALERTS ONLY. Phone topic: " + NTFY_TOPIC)
    if "--test" in sys.argv:
        send_ntfy("Radar test", "Memecoin Radar is connected to your phone.", tags="white_check_mark")
        return
    await asyncio.gather(runners_loop(state), keywords_loop(state), graduations_loop(state),
                         smart_wallets_loop(state), sleepers_loop(state), x_vip_loop(state))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped")
