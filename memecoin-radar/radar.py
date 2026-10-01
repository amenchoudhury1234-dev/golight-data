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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime

# ----------------------------- SETTINGS (edit freely) -----------------------------
NTFY_TOPIC = "scout-alert-c639f2f6f2bd1f4401"   # the topic your phone's ntfy app is subscribed to
FAST_TOPIC = "scout-fast-c639f2f6f2bd1f4401"    # SEPARATE channel for FAST LOTTO (migration take-off) pings - subscribe to
                                                # it in the ntfy app if you want them; the main channel stays story-only
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
RUNNER_MIN_AGE_MINUTES = 20       # snipers dump ~85% within 5 min of launch - let that pass
RUNNER_MIN_M5_CHANGE_ALLOWED = -10  # skip coins falling hard in the last 5 minutes
RUNNER_MAX_H1_CHANGE = 250        # 30 Sep: SGI +196%, SAID +194%, e/acc +825% pinged after the move
RUNNER_MAX_M5_CHANGE = 80         # PATTY pinged on a single +142% 5-min candle

# Graduation rules (pump.fun coins that just migrated)
GRAD_RECHECK_MINUTES = 45         # must SURVIVE this long after migration (GOCARDS died in 15 min)
GRAD_MIN_MCAP = 80_000            # still above the ~$69K migration point after the wait
GRAD_MAX_MCAP = 2_000_000         # IOF pinged "graduated" at $12M after +23,943% - nothing early left

# Keyword rules (catalyst phrases)
KEYWORD_FILE = "keywords.txt"
KEYWORD_MIN_H1_CHANGE = 20
KEYWORD_MIN_H1_VOLUME = 10_000
KEYWORD_MAX_MCAP = 30_000_000

# NARRATIVE MODE (30 Sep review): 11 of 11 checkable pure-momentum pings hit -30% before 2x; 5 peaked within 3 min
# of the ping. So price-only pings (runner, ignition, graduation, second leg) are now SILENT: still paper-traded
# and shown in the 21:00 scorecard, but no phone ping unless the coin matches a real-world story. Phone pings
# come from: CATALYST, RUNNER/IGNITION + REAL STORY, SLEEPER WAKING, SMART MONEY and the X VIP watch.
# Set to False to get the momentum pings back.
NARRATIVE_MODE = True
MOMENTUM_KINDS = {"EARLY RUNNER", "IGNITION", "GRADUATED & HOLDING", "SECOND LEG"}

# Safety / noise
MAX_ALERTS_PER_HOUR = 6
REALERT_IF_MCAP_MULTIPLIED = 2.0  # alert the same coin again only if its mcap doubled since last alert
POLL_SECONDS = 30
KEYWORD_POLL_SECONDS = 180
STATE_FILE = "radar_state.json"
# ------------------------------------------------------------------------------------

UA = {"User-Agent": "memecoin-radar/1.0", "Accept": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


GECKO_LOCK = threading.Lock()
# GeckoTerminal's free API returns 429 when calls come close together. 1 Oct overnight at a 4s gap: 44 x 429,
# each one a 5-min back-off, so 676 calls were skipped - the trending/new-pool feeds were mostly OFF all night.
GECKO_GAP_SECONDS = 8
GECKO_BACKOFF_SECONDS = 300    # after a 429, leave it alone for 5 min
_gecko = {"last": 0.0, "until": 0.0}


def gecko_json(url):
    """GeckoTerminal call, spaced out and backing off after 'Too Many Requests'."""
    with GECKO_LOCK:
        now = time.time()
        if now < _gecko["until"]:
            raise RuntimeError(f"backing off after rate limit ({_gecko['until'] - now:.0f}s left)")
        time.sleep(max(0.0, _gecko["last"] + GECKO_GAP_SECONDS - now))
        _gecko["last"] = time.time()
        try:
            return http_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _gecko["until"] = time.time() + GECKO_BACKOFF_SECONDS
            raise


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


# Safety thresholds (from research on top traders' GMGN/Axiom presets + rug post-mortems, 2026-09-30)
MAX_INSIDER_PCT = 15        # RugCheck-flagged insider wallets' combined % of supply
MAX_SINGLE_HOLDER_PCT = 20  # any one non-pool wallet
MAX_TOP10_PCT = 30          # top-10 non-pool holders combined (consensus 20-30%)
MAX_DEV_PCT = 10            # creator's own holding
MAX_TRANSFER_FEE_PCT = 10
HARD_REJECT_RISKS = ("creator history of rugged", "single holder ownership", "top 10 holders high ownership",
                     "honeypot", "freeze authority", "mint authority")
MIN_LP_LOCKED_PCT = 80      # 30 Sep: $si16z ("super intelligence") ran $10K -> $120K in 15 min on a Meteora pool with
                            # 0% of its liquidity locked; the creator pulled it right at our ping (liquidity -> $60).
_rc_cache = {}


def rugcheck(mint):
    """Full RugCheck report -> (ok, notes, verified). Higher RugCheck score = WORSE.
    Rejects on: rugged, active mint/freeze, insiders >15%, single holder >20%, top-10 >30%, dev >10%,
    transfer fee >10%, known hard-risk names, or 2+ 'danger' risks. Pool/AMM accounts are excluded."""
    now = time.time()
    if mint in _rc_cache and now - _rc_cache[mint][0] < 300:
        return _rc_cache[mint][1]
    try:
        r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        time.sleep(1.0)  # RugCheck allows ~1 req/s
    except Exception as e:
        res = (True, f"RugCheck unavailable ({type(e).__name__}) - UNVERIFIED, check GMGN manually", False)
        _rc_cache[mint] = (now, res)
        return res
    reasons, warns = [], []
    if r.get("rugged"):
        reasons.append("already rugged")
    if r.get("mintAuthority") or (r.get("token") or {}).get("mintAuthority"):
        reasons.append("mint authority active")
    if r.get("freezeAuthority") or (r.get("token") or {}).get("freezeAuthority"):
        reasons.append("freeze authority active")
    known = r.get("knownAccounts") or {}
    def is_pool(h):
        for key in (h.get("owner"), h.get("address")):
            if key and str((known.get(key) or {}).get("type", "")).upper() in ("AMM", "LOCKER", "POOL"):
                return True
        return False
    holders = [h for h in (r.get("topHolders") or []) if not is_pool(h)]
    pcts = sorted((float(h.get("pct") or 0) for h in holders), reverse=True)
    top1, top10 = (pcts[0] if pcts else 0), sum(pcts[:10])
    insider_pct = sum(float(h.get("pct") or 0) for h in holders if h.get("insider"))
    creator = r.get("creator")
    dev_pct = sum(float(h.get("pct") or 0) for h in holders if creator and h.get("owner") == creator)
    if insider_pct > MAX_INSIDER_PCT:
        reasons.append(f"insiders hold {insider_pct:.0f}%")
    if top1 > MAX_SINGLE_HOLDER_PCT:
        reasons.append(f"one wallet holds {top1:.0f}%")
    if top10 > MAX_TOP10_PCT:
        reasons.append(f"top-10 hold {top10:.0f}%")
    if dev_pct > MAX_DEV_PCT:
        reasons.append(f"dev holds {dev_pct:.0f}%")
    # Pullable liquidity: no pool with locked/burned LP (pump.fun migrations burn it) AND almost no independent LP
    # providers. The real $SI (9aqm..., 8x from our ping) has a burned-LP pumpswap pool + many Meteora LPs = fine;
    # $si16z had one Meteora pool, 0% locked, 0-1 providers = the creator pulled it.
    markets = [mk for mk in (r.get("markets") or []) if isinstance(mk, dict)]
    safe_pool = any(float(((mk.get("lp") or {}).get("lpLockedPct")) or 0) >= MIN_LP_LOCKED_PCT
                    or "pump" in str(mk.get("marketType", "")).lower() for mk in markets)
    few_lps = int(r.get("totalLPProviders") or 0) <= 1 or any("lp providers" in str(x.get("name", "")).lower()
                                                               for x in (r.get("risks") or []))
    if markets and not safe_pool and few_lps:
        reasons.append("liquidity not locked and only 1 LP provider - the dev can pull it (rug)")
    fee = float(((r.get("transferFee") or {}).get("pct")) or 0)
    if fee > MAX_TRANSFER_FEE_PCT:
        reasons.append(f"transfer fee {fee:.0f}%")
    risks = r.get("risks") or []
    names = [str(x.get("name", "")) for x in risks]
    for n in names:
        if any(k in n.lower() for k in HARD_REJECT_RISKS):
            reasons.append(n)
    danger = [n for n, x in zip(names, risks) if str(x.get("level", "")).lower() == "danger"]
    if len(danger) >= 2:
        reasons.append("2+ danger risks")
    if r.get("insiderNetworks"):
        warns.append(f"insider network detected ({len(r['insiderNetworks'])})")
    if int(r.get("graphInsidersDetected") or 0) > 0:
        warns.append(f"{r.get('graphInsidersDetected')} linked insider wallets")
    score = r.get("score_normalised", r.get("score"))
    notes = (f"RugCheck {score} (higher=worse) | top10 {top10:.0f}% | top1 {top1:.0f}% | insiders {insider_pct:.0f}%"
             + (f" | dev {dev_pct:.0f}%" if creator else "")
             + (f" | WARN: {'; '.join(warns)}" if warns else ""))
    if reasons:
        notes += " | REJECT: " + "; ".join(dict.fromkeys(reasons))
    res = (not reasons, notes, True)
    _rc_cache[mint] = (now, res)
    return res


def goplus_base(addr):
    """Base (EVM) honeypot/tax check via GoPlus (free, no key). -> (ok, notes, verified)"""
    try:
        r = http_json(f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={addr}")
        d = (r.get("result") or {}).get(addr.lower()) or {}
    except Exception as e:
        return True, f"GoPlus unavailable ({type(e).__name__}) - UNVERIFIED, check manually", False
    if not d:
        return True, "GoPlus: token not indexed yet - UNVERIFIED", False
    bad = []
    for k, label in (("is_honeypot", "honeypot"), ("cannot_sell_all", "can't sell all"),
                     ("owner_change_balance", "owner can change balances"), ("hidden_owner", "hidden owner"),
                     ("transfer_pausable", "transfers pausable")):
        if str(d.get(k)) == "1":
            bad.append(label)
    tax = max(float(d.get("sell_tax") or 0), float(d.get("buy_tax") or 0))
    if tax > 0.10:
        bad.append(f"tax {tax*100:.0f}%")
    notes = f"GoPlus: sell tax {float(d.get('sell_tax') or 0)*100:.0f}%" + (f" | REJECT: {'; '.join(bad)}" if bad else "")
    return (not bad), notes, True


# ----------------------------- pair metrics -----------------------------
def metrics(p):
    g = lambda d, k: float((p.get(d) or {}).get(k) or 0)
    tx = p.get("txns") or {}
    h1 = tx.get("h1") or {}
    created = p.get("pairCreatedAt") or 0
    age_h = (time.time() * 1000 - created) / 3_600_000 if created else 9999
    return {
        "name": ((p.get("baseToken") or {}).get("name") or "?").strip(),
        # strip(): DexScreener has e.g. "Pnut " with a trailing space, which broke exact ticker matches
        "symbol": ((p.get("baseToken") or {}).get("symbol") or "?").strip(),
        "addr": (p.get("baseToken") or {}).get("address", ""),
        "chain": p.get("chainId", "?"),
        "url": p.get("url", ""),
        "mcap": float(p.get("marketCap") or p.get("fdv") or 0),
        "liq": g("liquidity", "usd"),
        "vol_h1": g("volume", "h1"),
        "vol_m5": g("volume", "m5"),
        "vol_h24": g("volume", "h24"),
        "chg_m5": g("priceChange", "m5"),
        "chg_h1": g("priceChange", "h1"),
        "chg_h6": g("priceChange", "h6"),
        "buys_h1": int(h1.get("buys") or 0),
        "sells_h1": int(h1.get("sells") or 0),
        "age_h": age_h,
        "price": float(p.get("priceUsd") or 0),
        "buys_m5": int((tx.get("m5") or {}).get("buys") or 0),
        "sells_m5": int((tx.get("m5") or {}).get("sells") or 0),
        "dex": p.get("dexId", ""),
        "has_social": bool(((p.get("info") or {}).get("socials")) or ((p.get("info") or {}).get("websites"))),
    }


def dumping_now(m):
    """5-minute sell pressure: sells outnumber buys 2:1 while price falls (insider exit / post-migration dump)."""
    return m["sells_m5"] > 2 * max(m["buys_m5"], 1) and m["chg_m5"] < 0


def on_bonding_curve(m):
    return m["dex"] == "pumpfun"


def runner_reasons(m):
    """Why a coin is NOT an early runner (empty list = it passes). Mirrors the runner rules exactly."""
    r = []
    if m["chain"] not in CHAINS:
        r.append("chain")
    if not (RUNNER_MIN_MCAP <= m["mcap"] <= RUNNER_MAX_MCAP):
        r.append("mcap range")
    if m["age_h"] > RUNNER_MAX_AGE_HOURS:
        r.append("too old")
    if not (m["chg_h1"] >= RUNNER_MIN_H1_CHANGE or m["chg_m5"] >= RUNNER_MIN_M5_CHANGE):
        r.append("not moving")
    if m["vol_h1"] < RUNNER_MIN_H1_VOLUME:
        r.append("low volume")
    if m["buys_h1"] < RUNNER_MIN_H1_BUYS:
        r.append("few buys")
    if m["buys_h1"] < m["sells_h1"]:
        r.append("sells>buys 1h")
    if m["liq"] < RUNNER_MIN_LIQUIDITY:
        r.append("low liquidity")
    if m["age_h"] * 60 < RUNNER_MIN_AGE_MINUTES:
        r.append("under 20 min old")
    if m["chg_m5"] < RUNNER_MIN_M5_CHANGE_ALLOWED:
        r.append("falling 5m")
    if dumping_now(m):
        r.append("5m dump")
    if m["chg_h1"] > RUNNER_MAX_H1_CHANGE:
        r.append("already ran")
    if m["chg_m5"] > RUNNER_MAX_M5_CHANGE:
        r.append("candle already vertical")
    if m["chg_h1"] < 0:
        r.append("dead-cat bounce")   # HERO (-31% 1h, +28% 5m) and SARKA (-24% 1h): a bounce inside a dump
    if not m["has_social"]:
        r.append("no socials")
    return r


def is_early_runner(m):
    return not runner_reasons(m)


def is_keyword_mover(m):
    return (
        m["chain"] in CHAINS
        and m["mcap"] <= KEYWORD_MAX_MCAP
        and m["mcap"] >= RUNNER_MIN_MCAP
        and m["liq"] >= RUNNER_MIN_LIQUIDITY
        and m["chg_m5"] >= RUNNER_MIN_M5_CHANGE_ALLOWED
        and not dumping_now(m)
        and m["vol_h1"] >= KEYWORD_MIN_H1_VOLUME
        and m["chg_h1"] >= KEYWORD_MIN_H1_CHANGE
        and m["buys_h1"] >= m["sells_h1"]
    )


# ----------------------------- alerting -----------------------------
def send_ntfy(title, body, click=None, priority="high", tags="rotating_light", actions=None, topic=None):
    """Publish via ntfy's JSON API so emoji / non-Latin coin names work (HTTP headers can't carry them)."""
    prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 4)
    payload = {"topic": topic or NTFY_TOPIC, "title": title[:120], "message": body[:3900], "priority": prio,
               "tags": [t for t in tags.split(",") if t]}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = [dict(a, action="view", clear=False) for a in actions[:3]]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
    except Exception as e:
        log(f"ntfy FAILED: {e}")
        return False
    try:
        log(f"ntfy sent: {title}")
    except UnicodeEncodeError:   # Windows consoles can't always print emoji
        log("ntfy sent: " + title.encode("ascii", "replace").decode())
    return ok


REJECTED = set()   # coins that failed safety checks (so a later ping isn't mislabelled as a re-alert)


def coinbase_url(m):
    """Link that opens the coin straight in the Coinbase app, ready for Buy & sell (tested 1 Oct on the user's
    Android: Super Intelligence, Mr Crookshanks, Starship SpaceX Coin). Coinbase's own share links look like
    coinbase.com/price/<name-slug>-solana-<contract in lowercase>-token. If the name has no plain letters/digits
    (emoji, non-Latin) or the coin is on Base (format not tested), fall back to coinbase.com/price/<contract>,
    which opens Coinbase's search with only that coin listed (one extra tap)."""
    import re
    name = (m.get("name") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # Only plain names (letters/digits/spaces) are verified to map exactly; punctuation ("Act I : The AI Prophecy")
    # might slug differently on Coinbase's side, so those use the search link, which always works.
    if slug and m.get("chain") == "solana" and re.fullmatch(r"[A-Za-z0-9 ]+", name):
        return f"https://www.coinbase.com/price/{slug}-solana-{m['addr'].lower()}-token"
    return f"https://www.coinbase.com/price/{m['addr']}"


def check_links(m):
    """One-tap buttons on the phone notification (ntfy allows 3): Buy on Coinbase, GMGN (fees/bundlers/insiders),
    chart. The RugCheck summary is already in the notification text."""
    net = "sol" if m["chain"] == "solana" else m["chain"]
    acts = [{"label": "Buy on Coinbase", "url": coinbase_url(m)},
            {"label": "GMGN", "url": f"https://gmgn.ai/{net}/token/{m['addr']}"}]
    if m["url"]:
        acts.append({"label": "Chart", "url": m["url"]})
    return acts


COPYCAT_MIN_MCAP = 500_000
COPYCAT_MIN_VOL_H1 = 100_000


def story_coin_symbols():
    """{TICKER: {real contracts}} from sleepers.txt and auto_sleepers.txt comments like '# Super Inu $SI - ...'.
    A ticker can have several real coins: 30 Sep, Super Inu (DEW9dS...) and Super Intelligence (9aqmJj..., 8.7x)
    are both $SI, and mapping $SI to one address blocked the other as a "copycat"."""
    out = {}
    for fn in (SLEEPER_FILE, AUTO_SLEEPER_FILE):
        try:
            with open(os.path.join(HERE, fn), encoding="utf-8") as f:
                for ln in f:
                    ca, _, note = ln.partition("#")
                    ca = ca.strip()
                    if not ca:
                        continue
                    words = note.split()
                    syms = {w.lstrip("$").upper() for w in words if w.startswith("$")}
                    if not syms and words:          # no $TICKER in the note -> first word is the ticker (JIMOTHY)
                        syms.add(words[0].upper())
                    for sym in syms:
                        out.setdefault(sym, set()).add(ca)
        except FileNotFoundError:
            pass
    return out


# ----------------------------- AI JUDGE (Claude Opus 5.5 second opinion) -----------------------------
# The rules above are fast but can't "see" that a coin already ran, is a caller pump or a weak copy. Before a
# phone ping, the coin's numbers + its story go to Claude, which answers PING or SKIP with a one-line reason.
# SKIPs are silent but still paper-traded, so the scorecard shows whether the judge actually helps.
# Needs: pip install anthropic, and your API key in anthropic_key.txt (private, never commit it).
# No key file = judge off and pings work exactly as before.
AI_KEY_FILE = "anthropic_key.txt"
AI_MODEL = "claude-opus-5-5"
AI_EFFORT = "medium"
AI_MAX_CALLS_PER_DAY = 120        # hard guard on spend (~1-3p per call)
AI_USAGE_FILE = "ai_usage.json"
AI_DEEP_MAX_PER_DAY = 8            # live web cross-checks (~10-20p each: up to 3 searches + reading results)
AI_DEEP_SEARCHES = 3
AI_DAILY_BUDGET_USD = 1.00         # hard daily $ cap for all AI checks (max ~$30/month); 1 Oct: $0.50 ran out by 08:00
AI_DEEP_RESERVE_USD = 0.20         # a web cross-check only starts if this much of today's budget is left
AI_PRICE_SEARCH = 0.01             # $ per web search
AI_PRICE_IN, AI_PRICE_OUT = 4.00, 20.00   # $ per million tokens (Opus 5.5)
RECENT_VIP = []                    # (time, account, text) from the X watch, fed to the judge
_AI = {"client": None, "tried": False, "lock": threading.Lock()}

JUDGE_SYSTEM = """You are the final filter for a UK beginner's memecoin alert radar. They buy Solana/Base coins by hand
in the Coinbase app (1-3 minutes to get in), with GBP20-50 per coin they can afford to lose completely: no stop,
sell half at 2x, sell the rest 50% off the peak. A phone ping should mean "worth a look right now".

Answer PING only if an early, still-developing move with a real reason to keep running is plausible; otherwise
SKIP. What the user's own data (30 Sep 2026) showed:
- Pure price spikes pinged near the top: 5 of 11 peaked within 3 minutes of the ping. Late = +150%+ in the hour
  with no fresh catalyst, a single vertical 5-min candle, or mcap already many times where the story started.
- Winners had a real story or huge organic activity: a Trump-phrase coin ("super intelligence" -> $SI) at $1.01M
  did 7.2x in 3h; CROOK (2,200+ buys/hour, big volume) did 7x after first dipping 80%.
- Coins pinged in the first minutes after migrating off pump.fun, or while down on the hour (dead-cat bounce),
  went to ~0. Tiny copies of a story coin died; a copy with far more volume than rivals ran.
- Red flags: sells rising vs buys, thin liquidity vs mcap (<5%), insider/holder warnings, no socials, a story
  that is days old, a phrase that only loosely matches the coin name, caller/bundle-driven pumps.
Judge only from the data given; say so if something important is missing. Be decisive and brief."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PING", "SKIP"]},
        "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "reason": {"type": "string", "description": "One sentence, max 25 words, plain English."},
        "main_risk": {"type": "string", "description": "Biggest risk in max 12 words."},
    },
    "required": ["verdict", "confidence", "reason", "main_risk"],
    "additionalProperties": False,
}


def _ai_client():
    with _AI["lock"]:
        if not _AI["tried"]:
            _AI["tried"] = True
            try:
                with open(os.path.join(HERE, AI_KEY_FILE), encoding="utf-8") as f:
                    key = f.read().strip()
                import anthropic
                _AI["client"] = anthropic.Anthropic(api_key=key, timeout=60, max_retries=1)
                log(f"AI judge on ({AI_MODEL}, max {AI_MAX_CALLS_PER_DAY} checks/day)")
            except FileNotFoundError:
                log("AI judge off: put your Anthropic API key in anthropic_key.txt to turn it on")
            except ImportError:
                log("AI judge off: run  pip install anthropic")
        return _AI["client"]


def _ai_usage(add_in=0, add_out=0, searches=0, deep=False):
    """Track calls and $ per day in ai_usage.json. Returns today's record."""
    path, day = os.path.join(HERE, AI_USAGE_FILE), datetime.now().strftime("%Y-%m-%d")
    try:
        with open(path) as f:
            u = json.load(f)
    except Exception:
        u = {}
    rec = u.setdefault(day, {"calls": 0, "usd": 0.0})
    rec.setdefault("deep", 0)
    if add_in or add_out:
        rec["calls"] += 1
        rec["deep"] += 1 if deep else 0
        rec["usd"] = round(rec["usd"] + (add_in * AI_PRICE_IN + add_out * AI_PRICE_OUT) / 1e6
                           + searches * AI_PRICE_SEARCH, 4)
        try:
            with open(path, "w") as f:
                json.dump(u, f)
        except Exception:
            pass
    return rec


def story_backing(flags):
    """(phrase, sorted sources, strong?) for the story behind a ping. Strong = at least one strong source (a VIP/
    Trump post, the AI routine, Polymarket, a news account, your keywords.txt) or 2+ independent weak ones."""
    flags = flags or {}
    phrase = flags.get("story") or flags.get("keyword") or ""
    if not phrase:
        return "", [], False
    if phrase.startswith("@") or flags.get("launch_src", "").startswith("@"):
        return phrase, ["x-news" if phrase.startswith("@") else "x-vip"], True
    srcs = set(AUTO.sources(phrase)) if AUTO is not None else set()
    if phrase in load_keywords():
        srcs.add("manual")
    strong = bool(srcs & STRONG_SOURCES) or len(srcs & WEAK_SOURCES) >= 2
    if (" " not in phrase.strip() and not phrase.startswith("@") and len(srcs) < 2
            and not srcs & {"manual", "x-vip"}):      # a VIP's own X post (raccoon emoji etc.) still counts alone
        strong = False      # one word from one source ("industries" in one Trump post) isn't a story
    return phrase, sorted(srcs), strong


def _coin_brief(kind, m, extra, rc_notes, flags):
    now = time.time()
    story = (flags or {}).get("story") or (flags or {}).get("keyword") or ""
    lines = [
        f"Ping type: {kind}",
        f"Coin: {m['name']} (${m['symbol']}) on {m['chain']}, dex {m['dex'] or '?'}"
        f"{' (still on pump.fun bonding curve)' if on_bonding_curve(m) else ''}",
        f"Market cap {fmt_usd(m['mcap'])} | liquidity {fmt_usd(m['liq'])} | pool age {m['age_h']:.1f}h | "
        f"socials listed: {'yes' if m['has_social'] else 'no'}",
        f"Price change: 5m {m['chg_m5']:+.0f}% | 1h {m['chg_h1']:+.0f}% | 6h {m['chg_h6']:+.0f}%",
        f"Volume: 5m {fmt_usd(m['vol_m5'])} | 1h {fmt_usd(m['vol_h1'])} | 24h {fmt_usd(m['vol_h24'])}",
        f"Buys/sells: 5m {m['buys_m5']}/{m['sells_m5']} | 1h {m['buys_h1']}/{m['sells_h1']}",
        f"Safety check: {rc_notes}",
    ]
    if story:
        age = ""
        if AUTO is not None and story in AUTO.seen:
            age = f" (radar first saw this phrase trending {(now - AUTO.seen[story]) / 3600:.1f}h ago)"
        lines.append(f'Matched real-world phrase: "{story}"{age}')
        _, srcs, strong = story_backing(flags)
        if srcs:
            lines.append(f"Phrase seen in: {', '.join(srcs)} ({'cross-referenced' if strong else 'SINGLE weak source'})")
    snaps = SNAPS.get(m["addr"]) or []
    if len(snaps) >= 3:
        pts = [f"{(now - t) / 60:.0f}m ago {p / m['price']:.2f}x" for t, p in snaps[-8:] if m["price"]]
        lines.append("Radar's own price history vs now: " + ", ".join(pts))
    prev = [p for p in _load_pings() if p.get("addr") == m["addr"]]
    if prev:
        lines.append(f"Seen by the radar {len(prev)} time(s) before, first at {fmt_usd(prev[0]['mcap'])} "
                     f"{(now - prev[0]['t']) / 3600:.1f}h ago")
    vip = [f"@{a} {(now - t) / 3600:.1f}h ago: {txt[:200]}" for t, a, txt in RECENT_VIP[-8:] if now - t < 24 * 3600]
    if vip:
        lines.append("Recent VIP X posts:\n" + "\n".join(vip))
    if extra.strip():
        lines.append("Radar notes: " + extra.strip()[:600])
    return "\n".join(lines)


AI_MIN_CONFIDENCE = 3   # 1 Oct: "Three Falcons" got PING at 2/5 (48 linked insider wallets) and dumped ~90% in minutes


def _min_confidence(out):
    """A half-hearted PING (1-2/5) is treated as a SKIP."""
    try:
        if out.get("verdict") == "PING" and int(out.get("confidence") or 0) < AI_MIN_CONFIDENCE:
            return dict(out, verdict="SKIP", reason=f"low confidence ({out.get('confidence')}/5): {out.get('reason', '')}")
    except (TypeError, ValueError):
        pass
    return out


def ai_judge(kind, m, extra, rc_notes, flags):
    """Returns dict(verdict, confidence, reason, main_risk) or None if the judge is off/unavailable."""
    client = _ai_client()
    if client is None:
        return None
    today = _ai_usage()
    if today["calls"] >= AI_MAX_CALLS_PER_DAY or today["usd"] >= AI_DAILY_BUDGET_USD:
        log(f"AI judge: daily budget reached (${today['usd']:.2f}) - unchecked story/momentum pings are held back")
        return "BUDGET"
    import anthropic
    import anthropic
    req = dict(model=AI_MODEL, max_tokens=4000, system=JUDGE_SYSTEM,
               output_config={"effort": AI_EFFORT, "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
               messages=[{"role": "user", "content": _coin_brief(kind, m, extra, rc_notes, flags)}])
    try:
        try:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **req)
        except (anthropic.BadRequestError, TypeError) as e:
            if "fallback" not in str(e).lower() and "betas" not in str(e).lower():
                raise
            resp = client.messages.create(**req)   # fallback parameter not accepted on this account/SDK
    except anthropic.AuthenticationError:
        log("AI judge: API key rejected - check anthropic_key.txt")
        return None
    except anthropic.APIStatusError as e:
        log(f"AI judge error {e.status_code}: {str(e)[:120]}")
        return None
    except anthropic.APIConnectionError as e:
        log(f"AI judge unreachable: {e}")
        return None
    except Exception as e:                      # never let the judge block a ping
        log(f"AI judge failed ({type(e).__name__}: {str(e)[:120]}) - pinging without it")
        return None
    rec = _ai_usage(resp.usage.input_tokens, resp.usage.output_tokens)
    if resp.stop_reason == "refusal":
        log("AI judge declined this one - pinging without it")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        log(f"AI judge: unreadable answer {text[:80]!r}")
        return None
    log(f"AI judge ${m['symbol']}: {out['verdict']} ({out['confidence']}/5) {out['reason']} "
        f"[today {rec['calls']} checks, ${rec['usd']:.2f}]")
    return _min_confidence(out)


DEEP_SYSTEM = JUDGE_SYSTEM + """

LIVE CROSS-CHECK: you have web search (max 3 searches). Use it to cross-reference the story, not to research
crypto in general. Check: (1) is the real-world story/post real, recent (hours, not days) and actually spreading
(news, X, TikTok, Reddit - several independent places)? (2) are people on X/crypto news talking about THIS coin
(name/ticker/CA), or is it one of many copies? (3) any scam/rug/bundle/caller-dump warnings about it? A story seen
in only one place, or a coin nobody mentions, is a SKIP unless the on-chain numbers are exceptional.
End your answer with ONE line of JSON only, exactly:
{"verdict": "PING" or "SKIP", "confidence": 1-5, "reason": "<max 25 words>", "main_risk": "<max 12 words>",
 "sources_found": "<max 15 words: where you saw the story/coin>"}"""


def ai_deep_check(kind, m, extra, rc_notes, flags):
    """Second, slower opinion with live web search. Returns the parsed dict or None (then the quick verdict stands)."""
    import re
    client = _ai_client()
    today = _ai_usage()
    if (client is None or today.get("deep", 0) >= AI_DEEP_MAX_PER_DAY
            or today["usd"] + AI_DEEP_RESERVE_USD > AI_DAILY_BUDGET_USD):
        return None
    msgs = [{"role": "user", "content": _coin_brief(kind, m, extra, rc_notes, flags)}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": AI_DEEP_SEARCHES}]
    tin = tout = searches = 0
    try:
        for _ in range(3):                     # resume at most twice if the server pauses the search loop
            resp = client.messages.create(model=AI_MODEL, max_tokens=8000, system=DEEP_SYSTEM, tools=tools,
                                          output_config={"effort": AI_EFFORT}, messages=msgs)
            tin += resp.usage.input_tokens
            tout += resp.usage.output_tokens
            stu = getattr(resp.usage, "server_tool_use", None)
            searches += int(getattr(stu, "web_search_requests", 0) or 0) if stu else 0
            if resp.stop_reason != "pause_turn":
                break
            msgs = [msgs[0], {"role": "assistant", "content": resp.content}]
    except Exception as e:
        log(f"AI deep check failed ({type(e).__name__}: {str(e)[:120]}) - using the quick verdict")
        return None
    rec = _ai_usage(tin, tout, searches, deep=True)
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    found = re.findall(r"\{[^{}]*\"verdict\"[^{}]*\}", text)
    try:
        out = json.loads(found[-1])
        assert out["verdict"] in ("PING", "SKIP")
    except Exception:
        log(f"AI deep check: unreadable answer {text[-120:]!r}")
        return None
    log(f"AI deep check ${m['symbol']}: {out['verdict']} ({out.get('confidence')}/5) {out.get('reason')} | "
        f"seen: {out.get('sources_found', '?')} [{searches} searches; today {rec['calls']} checks, ${rec['usd']:.2f}]")
    return _min_confidence(out)


SERIAL_COPY_MIN = 5          # this many coins with the same ticker on the chain = a copy wave


def serial_copies(m):
    """(how many coins share this exact ticker on this chain, is THIS one the most traded of them?)."""
    tick = m["symbol"].replace("$", "").strip().upper()
    if not tick:
        return 0, True
    try:
        same = [metrics(p) for p in dex_search(tick)]
    except Exception:
        return 0, True
    same = [s for s in same if s["chain"] == m["chain"] and s["symbol"].replace("$", "").strip().upper() == tick]
    addrs = {s["addr"] for s in same} | {m["addr"]}
    top = max(same + [m], key=lambda s: s["vol_h1"])
    return len(addrs), top["addr"] == m["addr"]


def alert(state, kind, m, extra="", skip_dedupe=False, flags=None):
    reals = story_coin_symbols().get(m["symbol"].lstrip("$").upper()) or set()
    if reals and m["addr"] not in reals:
        real = ", ".join(sorted(a[:6] + "..." for a in reals))
        # 30 Sep: the $24K "$SI" copy died (0.16x) but the $1.01M one did 7.2x - big copies have real traction
        if m["mcap"] < COPYCAT_MIN_MCAP or m["vol_h1"] < COPYCAT_MIN_VOL_H1:
            log(f"skipped copycat ${m['symbol']} ({m['addr'][:6]}...) - the real story coin is {real}")
            return
        extra = (f"COPYCAT WARNING: not the original ${m['symbol']} ({real}), but it has real money "
                 f"behind it. Double-check the CA.\n" + extra)
    if NARRATIVE_MODE and kind in MOMENTUM_KINDS and not (flags or {}).get("story"):
        log_candidate(kind, m, False, ["silent (momentum only)"], flags)   # paper-traded, no phone ping
        return
    phrase, srcs, strong_story = story_backing(flags)
    if phrase and not strong_story:
        # e.g. a coin matching a phrase that's only on Reddit: wait until a 2nd source (Google, news, a VIP) agrees
        log_candidate(kind, m, False, [f"single source ({', '.join(srcs) or '?'})"], flags)
        second_look_add(m, kind, phrase, flags)
        return
    prev = state.alerted.get(m["addr"])
    if not skip_dedupe and not state.should_alert(m["addr"], m["mcap"]):
        return
    if m["chain"] == "solana":
        ok, rc_notes, verified = rugcheck(m["addr"])
    else:
        ok, rc_notes, verified = goplus_base(m["addr"])
    if not ok:
        log(f"Safety FAIL, skipped {m['symbol']}: {rc_notes}")
        REJECTED.add(m["addr"])
        state.alerted[m["addr"]] = m["mcap"]  # don't recheck the same rug every minute
        state.save()
        log_candidate(kind, m, False, ["safety: " + rc_notes.split("REJECT: ")[-1]])
        return
    if (prev is not None and not skip_dedupe and m["addr"] not in REJECTED
            and kind in ("EARLY RUNNER", "RUNNER + REAL STORY", "BONDING-CURVE LOTTO")):
        kind = "RE-ALERT (doubled)"
        extra = ("LATE PING: this coin already pinged and has since DOUBLED. Higher reversal risk - "
                 "CROOK's re-alert at $289K was the one that lost. Prefer SECOND LEG pings.\n" + extra)
    # ACT NOW only for coins that are verified safe, OFF the bonding curve and have survived 45+ min since
    # migration (the new pool's age) - GOCARDS dumped -96% within 15 min of migrating. Never on late re-alerts.
    strong = (verified and not on_bonding_curve(m) and m["age_h"] * 60 >= 45 and kind != "RE-ALERT (doubled)"
              and m["chg_h1"] >= 100 and m["chg_m5"] >= 0 and m["buys_h1"] >= 1.5 * max(m["sells_h1"], 1)
              and m["chg_h1"] <= 300 and m["vol_h1"] >= 50_000 and m["mcap"] <= 1_000_000)
    if not strong and not state.under_cap():          # the hourly cap never blocks an ACT NOW
        log(f"hourly alert cap reached, skipping {m['symbol']}")
        log_candidate(kind, m, False, ["hourly cap"])
        return
    if on_bonding_curve(m) and kind == "EARLY RUNNER":
        kind = "BONDING-CURVE LOTTO"
        extra = ("Still on pump.fun's bonding curve: highest-risk stage (most dumps happen at/just after "
                 "migration). Lottery size only, or wait for it to migrate and hold 45+ min.\n" + extra)
    if not verified:
        extra = "SAFETY UNVERIFIED - check GMGN (bundlers/insiders/top10) before anything.\n" + extra
    copies, leader = serial_copies(m)
    if copies >= SERIAL_COPY_MIN and not leader:
        # 1 Oct: "IGNITION $SIC $21K" pinged - the 25th+ "Super Intelligence Cat" in 14h; the original was dead at $2K
        log(f"skipped serial copy ${m['symbol']} ({m['addr'][:6]}...) - {copies} coins share this ticker, "
            "another one is trading more")
        log_candidate(kind, m, False, [f"serial copy ({copies} with this ticker)"], flags)
        return
    verdict = ai_judge("ACT NOW" if strong else kind, m, extra, rc_notes, flags)
    if verdict == "BUDGET":
        verdict = None
        # 1 Oct: the $0.50 AI budget was used up by 07:57 and the copy above went out unchecked. Without the AI
        # only the strongest signals still ping (VIP contract addresses and listings never come through here).
        if kind not in ("SLEEPER WAKING", "SMART MONEY", "NEWS MENTION") and not (phrase and strong_story):
            log_candidate(kind, m, False, ["AI budget used - held back"], flags)
            return
        if phrase and strong_story:
            extra = (f"AI NOT CHECKED (today's AI budget is used up) - but the story \"{phrase}\" is cross-referenced "
                     f"({', '.join(srcs)}). Check RugCheck/GMGN yourself before anything.\n" + extra)
    # A fresh Elon/Trump/VIP post IS the cross-check - skip the slow web search (up to ~60s) so the ping is fast.
    vip_fresh = "x-vip" in srcs or str((flags or {}).get("launch_src", "")).startswith("@")
    if (verdict and verdict["verdict"] == "PING" and not vip_fresh
            and (phrase or kind in ("SLEEPER WAKING", "SMART MONEY", "NEWS MENTION"))):
        deep = ai_deep_check(kind, m, extra, rc_notes, flags)
        if deep:
            verdict = dict(deep, deep=True)
    flags = dict(flags or {})
    if verdict:
        flags.update(ai=verdict["verdict"], ai_conf=verdict.get("confidence"), ai_deep=bool(verdict.get("deep")))
        if verdict["verdict"] == "SKIP":
            state.alerted[m["addr"]] = m["mcap"]      # don't re-judge it until it doubles
            state.save()
            log_candidate(kind, m, False, [f"AI skip: {verdict['reason'][:80]}"], flags)
            return
        seen = f" Story/coin seen in: {verdict['sources_found']}." if verdict.get("sources_found") else ""
        extra = (f"AI CHECK{' (web cross-checked)' if verdict.get('deep') else ''}: worth a look "
                 f"({verdict.get('confidence')}/5) - {verdict.get('reason')}{seen} "
                 f"Main risk: {verdict.get('main_risk')}\n" + extra)
    if strong:
        title = f"ACT NOW (10-min window): ${m['symbol']} {fmt_usd(m['mcap'])} {m['chg_h1']:+.0f}% 1h"
        extra = ("STRONG SETUP: buyers heavily outnumber sellers on big volume. If GMGN checks pass, "
                 "enter GBP20-50 now; don't wait for it to 'confirm'.\n" + extra)
    else:
        title = f"{kind}: ${m['symbol']} {fmt_usd(m['mcap'])} ({m['chg_h1']:+.0f}% 1h)"
    if verdict:
        title = ("AI+WEB OK " if verdict.get("deep") else "AI OK ") + title
    body = (
        f"{m['name']} (${m['symbol']}) on {m['chain']}\n"
        f"CA: {m['addr']}\n"
        f"MCap {fmt_usd(m['mcap'])} | Liq {fmt_usd(m['liq'])} | Vol 1h {fmt_usd(m['vol_h1'])}\n"
        f"5m {m['chg_m5']:+.0f}% | 1h {m['chg_h1']:+.0f}% | buys/sells 1h {m['buys_h1']}/{m['sells_h1']} | age {m['age_h']:.1f}h\n"
        f"{rc_notes}\n{extra}\n"
        f"BEFORE BUYING: tap GMGN - global fees >=1.5 SOL, bundlers/snipers low. Paste CA into Coinbase.\n"
        f"PLAN (lotto): only GBP20-50 you can lose completely - no stop, most winners dip 50-80% first. "
        f"Sell half at 2x. Add it to positions.txt for exit alerts."
    )
    if send_ntfy(title, body, click=coinbase_url(m), priority="urgent" if strong else "high",
                 tags="rotating_light,moneybag" if strong else "rotating_light", actions=check_links(m)):
        state.record(m["addr"], m["mcap"])
        try:   # tell the cloud Catalyst routine we pinged this coin, so it doesn't re-alert it as "new"
            urllib.request.urlopen(urllib.request.Request(
                f"https://ntfy.sh/{MEM_TOPIC}", method="POST", headers=UA,
                data=f"{m['addr']}|{m['symbol']}|{int(m['mcap'])}|{time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}|{kind}".encode()),
                timeout=10)
        except Exception:
            pass
        log_ping(kind if not strong else "ACT NOW", m,
                 dict({"verified": verified, "insider_warn": "WARN" in rc_notes}, **flags))


# ----------------------------- loops -----------------------------
GECKO_POLL_SECONDS = 120


def gecko_trending_tokens():
    """Extra discovery: GeckoTerminal's free trending pools (Solana + Base). DexScreener's 'latest' feeds only
    list coins whose teams PAID for a profile/boost, so organic runners were invisible to the runner scan."""
    out = {}
    for net in ("solana", "base"):
        try:
            d = gecko_json(f"https://api.geckoterminal.com/api/v2/networks/{net}/trending_pools?page=1")
        except Exception as e:
            log(f"geckoterminal ({net}) unavailable: {e}")
            continue
        for pool in d.get("data") or []:
            tid = ((((pool.get("relationships") or {}).get("base_token") or {}).get("data")) or {}).get("id", "")
            if tid.startswith(net + "_"):          # id looks like "solana_<mint>" / "base_<0x...>"
                out.setdefault(net, set()).add(tid.split("_", 1)[1])
    return out


async def runners_loop(state):
    loop = asyncio.get_running_loop()
    last_gecko = 0
    while True:
        try:
            latest = await loop.run_in_executor(None, dex_latest_tokens)
            if time.time() - last_gecko >= GECKO_POLL_SECONDS:
                last_gecko = time.time()
                gk = await loop.run_in_executor(None, gecko_trending_tokens)
                for c, addrs in gk.items():
                    latest[c] = list(set(latest.get(c, [])) | addrs)
            for chain, addrs in latest.items():
                pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, chain, addrs)
                for p in pairs.values():
                    m = metrics(p)
                    if IGN_MIN_MCAP <= m["mcap"] <= IGN_MAX_MCAP and m["age_h"] <= RUNNER_MAX_AGE_HOURS:
                        hot_add(m["addr"], m["chain"])
                    near = (m["chain"] in CHAINS and m["mcap"] >= RUNNER_MIN_MCAP
                            and m["vol_h1"] >= RUNNER_MIN_H1_VOLUME and m["chg_h1"] >= RUNNER_MIN_H1_CHANGE)
                    if near:
                        WATCH[m["addr"]] = (time.time(), m["chain"])   # near-miss or ping -> second-leg watch
                    reasons = runner_reasons(m)
                    if not reasons:
                        story = real_world_match(m)
                        if story:
                            await loop.run_in_executor(None, lambda: alert(
                                state, "RUNNER + REAL STORY", m,
                                f'REAL-WORLD STORY: matches trending "{story}" - these run longer than random '
                                f'coins (TILCAYO, Super Inu pattern).', flags={"story": story}))
                        else:
                            await loop.run_in_executor(None, alert, state, "EARLY RUNNER", m, "")
                    elif near and m["addr"] not in state.alerted:
                        # near-miss: don't ping, but track it so the scorecard shows what the filters cost us
                        await loop.run_in_executor(None, log_candidate, "EARLY RUNNER", m, False, reasons)
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
KW_TOPIC = "scout-kw-c639f2f6f2bd1f4401"
MEM_TOPIC = "scout-mem-c639f2f6f2bd1f4401"   # shared memory of pinged coins (the cloud routine re-publishes it)
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
    for q in re.findall(r'["“‘]([^"”’]{3,40})["”’]|(?<!\w)\'([^\']{3,40})\'(?!\w)', text):
        q = q[0] or q[1] if isinstance(q, tuple) else q
        q = _clean(q)
        if 1 <= len(q.split()) <= 6:
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


# ----------------------------- VIP POSTS A CONTRACT ADDRESS (the fastest, biggest signal) -----------------------------
# $TRUMP (Jan 2025, ~$8.7B in two days) started with Trump posting the contract address himself, on Truth Social AND
# X. Both are now checked for addresses: X every 15s (x_vip_loop), Truth Social every 30s (free RSS, truth_ca_loop).
_CA_SEEN = set()


def vip_contract_ping(who, text):
    """Urgent ping for every Solana (base58) or Base (0x...) address in a VIP post, once per address."""
    import re
    text = re.sub(r"https?://\S+", " ", text or "")           # links contain long random strings
    found = [(ca, "solana") for ca in re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)]
    found += [(ca, "base") for ca in re.findall(r"\b0x[a-fA-F0-9]{40}\b", text)]
    for ca, chain in found:
        if ca in _CA_SEEN:
            continue
        _CA_SEEN.add(ca)
        cb = f"https://www.coinbase.com/price/{ca}"
        send_ntfy(f"{who} POSTED A CONTRACT ADDRESS", f"{text[:300]}\n\nCA ({chain}): {ca}\n"
                  "Copycats appear within seconds - use ONLY this exact CA. Check GMGN first.",
                  click=cb, priority="urgent", tags="rotating_light",
                  actions=[{"label": "Buy on Coinbase", "url": cb},
                           {"label": "Chart", "url": f"https://dexscreener.com/{chain}/{ca}"}])


TRUTH_CA_POLL_SECONDS = 30


async def truth_ca_loop(state):
    """Trump's Truth Social (free RSS mirror) every 30s, only looking for contract addresses."""
    import html
    import xml.etree.ElementTree as ET
    loop = asyncio.get_running_loop()
    first = True
    while True:
        try:
            def fetch():
                req = urllib.request.Request(TRUMP_FEED, headers={"User-Agent": UA["User-Agent"]})
                with urllib.request.urlopen(req, timeout=15) as r:
                    return ET.fromstring(r.read())
            root = await loop.run_in_executor(None, fetch)
            for it in list(root.iter("item"))[:10]:
                text = html.unescape((it.findtext("title") or "") + " " + (it.findtext("description") or ""))
                if first:                            # don't ping for addresses in old posts at startup
                    import re
                    _CA_SEEN.update(re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b|\b0x[a-fA-F0-9]{40}\b",
                                               re.sub(r"https?://\S+", " ", text)))
                else:
                    vip_contract_ping("TRUMP (Truth Social)", text)
            first = False
        except Exception as e:
            log(f"Truth Social address watch: feed unavailable ({type(e).__name__})")
        await asyncio.sleep(TRUTH_CA_POLL_SECONDS)


# ----------------------------- EXCHANGE LISTINGS (2024's most repeatable catalyst) -----------------------------
# 80% of the memecoins Binance listed in 2024 jumped after the listing (ACT +1,000%, MOODENG +100% on a futures
# listing alone, NEIRO ~7,600% over its run). Upbit (Korea) listings are known for sudden pumps too. Both publish
# announcements on free endpoints; checked every 30s. The listing IS the catalyst, so no AI check (speed).
LISTING_POLL_SECONDS = 30
LISTING_MIN_MCAP = 5_000_000      # 30 Sep test: Binance listed Hyperliquid's HYPE; a $101K Solana "HYPE" is a copy
BINANCE_LISTINGS = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                    "?type=1&catalogId=48&pageNo=1&pageSize=10")
UPBIT_LISTINGS = "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=10&category=trade"
LISTING_SKIP_WORDS = ("bstock", "stock", "tradfi", "collateral", "delist", "removal", "margin", "loans", "earn",
                      "simple earn", "vip loan", "copy trading", "convert")


def _listing_tickers(title):
    """Tickers named in a listing headline: '(PNUT)', 'NEIROUSDT', '1000CATUSDT' -> PNUT, NEIRO, CAT."""
    import re
    found = set(re.findall(r"\(([A-Z0-9]{2,12})\)", title))
    for pair in re.findall(r"\b([A-Z0-9]{2,15})USD[TC]?\b", title):
        found.add(re.sub(r"^(1000000|1000)", "", pair))
    return {t for t in found if t and not t.isdigit() and t not in FEED_TICKER_IGNORE}


def _fetch_listings():
    """[(source, id, title)] of recent NEW-listing announcements from Binance and Upbit."""
    out = []
    try:
        d = http_json(BINANCE_LISTINGS)
        for a in (((d.get("data") or {}).get("catalogs") or [{}])[0].get("articles") or []):
            t = a.get("title", "")
            if not any(w in t.lower() for w in LISTING_SKIP_WORDS):
                out.append(("Binance", f"b{a.get('id')}", t))
    except Exception as e:
        log(f"listings: Binance unavailable ({type(e).__name__})")
    try:
        d = http_json(UPBIT_LISTINGS)
        for n in ((d.get("data") or {}).get("notices") or []):
            t = n.get("title", "")
            if "신규 거래지원" in t:                      # "new trading support" = a new listing
                out.append(("Upbit", f"u{n.get('id')}", t))
    except Exception as e:
        log(f"listings: Upbit unavailable ({type(e).__name__})")
    return out


async def listings_loop(state):
    loop = asyncio.get_running_loop()
    seen, first = set(), True
    while True:
        try:
            for source, aid, title in await loop.run_in_executor(None, _fetch_listings):
                if aid in seen:
                    continue
                seen.add(aid)
                if first:                               # don't ping old announcements at startup
                    continue
                tickers = _listing_tickers(title)
                log(f"listing: {source} - {title[:100]} | tickers: {', '.join(sorted(tickers)) or 'none'}")
                for tick in tickers:
                    same = [m for m in (metrics(p) for p in await loop.run_in_executor(None, dex_search, tick))
                            if m["symbol"].replace("$", "").upper() == tick and m["liq"] >= 20_000]
                    top = max(same, key=lambda m: m["vol_h24"], default=None)
                    # the listed coin = the highest-volume coin with that ticker on ANY chain; only ping if that one
                    # is on Solana/Base (30 Sep test: Binance's NEIRO is on Ethereum - a Solana "NEIRO" is a copy)
                    if not top or top["chain"] not in CHAINS:
                        log(f"listing: ${tick} - the real coin is " +
                            (f"on {top['chain']}" if top else "not on a DEX") + ", not buyable in Coinbase - no ping")
                        continue
                    if top["mcap"] < LISTING_MIN_MCAP:    # exchanges list established coins; a tiny match is a copy
                        log(f"listing: ${tick} - best Solana/Base match is only {fmt_usd(top['mcap'])}, "
                            "probably a copy of a coin on another chain - no ping")
                        continue
                    best = top
                    body = (f"{source} announced: {title[:220]}\n\n{best['name']} (${best['symbol']}) on {best['chain']}\n"
                            f"CA: {best['addr']}\nMCap {fmt_usd(best['mcap'])} | Liq {fmt_usd(best['liq'])} | "
                            f"1h {best['chg_h1']:+.0f}% | 5m {best['chg_m5']:+.0f}%\n"
                            "Big-exchange listings often pump memecoins within minutes (2024: 80% of Binance's memecoin "
                            "listings rose, ACT +1,000%). Highest-volume coin with this ticker shown - check the CA.")
                    send_ntfy(f"LISTING: {source} -> ${best['symbol']} {fmt_usd(best['mcap'])}", body,
                              click=coinbase_url(best), priority="urgent", tags="rotating_light,bank",
                              actions=check_links(best))
                    log_ping("LISTING", best, {"story": f"{source} listing", "listing": source})
            first = False
        except Exception as e:
            log(f"listings loop error: {e}")
        await asyncio.sleep(LISTING_POLL_SECONDS)


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


REDDIT_FEEDS = ["https://www.reddit.com/r/all/top/.rss?t=hour"]
REDDIT_EXTRA = ["https://www.reddit.com/r/aww/top/.rss?t=day", "https://www.reddit.com/r/nextfuckinglevel/top/.rss?t=day"]
_REDDIT_TURN = [0]


def harvest_reddit():
    """Viral posts (animals, clips, memes) often become coins hours later - Jimothy the raccoon was a viral clip."""
    import re
    phrases = set()
    _REDDIT_TURN[0] += 1
    feeds = REDDIT_FEEDS + [REDDIT_EXTRA[_REDDIT_TURN[0] % len(REDDIT_EXTRA)]]
    for i, url in enumerate(feeds):
        if i:
            time.sleep(4)                      # Reddit returns 429 when feeds are fetched back to back
        try:
            for t in fetch_rss_titles(url, 15):
                phrases |= extract_phrases(re.sub(r"<[^>]+>", " ", t)[:300])
        except Exception as e:
            log(f"reddit feed unavailable ({url.split('/r/')[1].split('/')[0]}): {e}")
    return phrases


NEWS_FEEDS = [
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",                                   # US top stories
    "https://news.google.com/rss/search?q=Trump+when:2h&hl=en-US&gl=US&ceid=US:en",            # anything Trump, last 2h
    "https://news.google.com/rss/search?q=viral+OR+typo+OR+gaffe+when:3h&hl=en-US&gl=US&ceid=US:en",
    # 1 Oct: "I Am Jane Doe" (~30x, a viral women's-support movement around the Cornell case on CNN/BuzzFeed) wasn't a
    # Trump/Elon/typo story - social movements and big US/entertainment stories need their own feeds.
    "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=movement+OR+tiktok+OR+%22goes+viral%22+OR+outrage+when:6h&hl=en-US&gl=US&ceid=US:en",
]


def harvest_news():
    """Google News headlines. 30 Sep: the White House 'President of the Unites States' typo (TechCrunch/Newsweek)
    turned a pump.fun coin into a ~260x; our radar only heard about it from the 2-hourly routine, far too late.
    Returns {phrase: set(outlets)} so a phrase in 2+ different outlets counts as a strong, cross-referenced story."""
    import re
    import xml.etree.ElementTree as ET
    out = {}
    for i, url in enumerate(NEWS_FEEDS):
        if i:
            time.sleep(2)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=15) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            log(f"news feed unavailable: {e}")
            continue
        for it in list(root.iter("item"))[:40]:
            title = it.findtext("title") or ""
            outlet = (it.findtext("source") or title.rsplit(" - ", 1)[-1]).strip().lower()
            title = title.rsplit(" - ", 1)[0]
            for ph in extract_phrases(title):
                out.setdefault(ph, set()).add(outlet)
    return out


def harvest_wikipedia_spikes():
    """Pages that jumped into yesterday's top views vs the day before (new animals, people, events)."""
    from datetime import timedelta
    def top(day):
        url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/"
               f"{day:%Y/%m/%d}")
        arts = http_json(url)["items"][0]["articles"]
        return [a["article"] for a in arts[:200]]
    try:
        from datetime import timezone
        d1 = datetime.now(timezone.utc) - timedelta(days=1)
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

# ----------------------------- LAUNCH CLUSTERS (a story before it's in the news) -----------------------------
# People who just saw a viral clip launch coins named after it within minutes - usually hours before news sites
# write about it. 3+ DIFFERENT creators using the same word inside 15 min = a launch cluster (weak source) -
# but only if that's a SPIKE: at least 3x the word's own normal rate over the last 6h. A brand-new word (a new
# story) still triggers at 3 creators; words that are in launches all day ("space", "rocket") need a real surge.
# 30 Sep first hour at a flat 3: 91 clusters/hour, and "space" + Wikipedia wasted AI checks on SpaceX/Rocket coins.
LAUNCH_CLUSTER_MIN_CREATORS = 3
LAUNCH_CLUSTER_MINUTES = 15
LAUNCH_BASELINE_HOURS = 6
LAUNCH_SPIKE_MULTIPLE = 3.0
LAUNCH_HISTORY_FILE = "launch_history.json"   # keeps the baseline across restarts
_LAUNCH_META = {"start": time.time(), "loaded": False, "saved": 0.0}
LAUNCH_FILLER = set("""coin token inu pepe doge dogs cats kitty puppy meme memes official based chad moon moons
baby mini mega super ultra king queen lord little shib floki bonk bull bear frog solana pump fun sol trump elon
musk donald president america world money cash rich gold life love happy wife time first real pump launch
community today season world""".split())
_LAUNCH_WORDS = {}   # word -> {creator: time}
_CLUSTER_LOGGED = {}  # word -> time last logged


def _launch_history_io(now):
    """Load the word history once at start; save it every 10 min (small JSON, 6h window)."""
    path = os.path.join(HERE, LAUNCH_HISTORY_FILE)
    if not _LAUNCH_META["loaded"]:
        _LAUNCH_META["loaded"] = True
        try:
            with open(path) as f:
                d = json.load(f)
            _LAUNCH_META["start"] = min(_LAUNCH_META["start"], float(d.get("start", now)))
            _LAUNCH_WORDS.update({w: dict(s) for w, s in (d.get("words") or {}).items()})
        except Exception:
            pass
    if now - _LAUNCH_META["saved"] >= 600:
        _LAUNCH_META["saved"] = now
        keep = now - LAUNCH_BASELINE_HOURS * 3600
        for w in [w for w, s in _LAUNCH_WORDS.items() if max(s.values()) < keep]:
            _LAUNCH_WORDS.pop(w, None)
        for w, s in _LAUNCH_WORDS.items():
            _LAUNCH_WORDS[w] = {c: t for c, t in s.items() if t >= keep}
        try:
            with open(path, "w") as f:
                json.dump({"start": max(_LAUNCH_META["start"], keep), "words": _LAUNCH_WORDS}, f)
        except Exception as e:
            log(f"could not save launch history: {e}")


def launch_spike(word, now=None):
    """(recent creators in the last 15 min, normal creators per 15 min over the last 6h or None if <1h of history,
    is it a cluster?)."""
    now = now or time.time()
    recent_cut = now - LAUNCH_CLUSTER_MINUTES * 60
    base_cut = max(now - LAUNCH_BASELINE_HOURS * 3600, _LAUNCH_META["start"])
    s = _LAUNCH_WORDS.get(word, {})
    recent = sum(t >= recent_cut for t in s.values())
    span = recent_cut - base_cut
    if span < 3600:                       # not enough history yet: fall back to the plain 3-creator rule
        return recent, None, recent >= LAUNCH_CLUSTER_MIN_CREATORS
    base = sum(base_cut <= t < recent_cut for t in s.values()) / (span / (LAUNCH_CLUSTER_MINUTES * 60))
    return recent, base, recent >= max(LAUNCH_CLUSTER_MIN_CREATORS, LAUNCH_SPIKE_MULTIPLE * base)


def launch_cluster_note(name, symbol, creator):
    import re
    now = time.time()
    _launch_history_io(now)
    # Words of 5+ letters from the name, plus the ticker itself from 4 letters: 1 Oct, "$SARP" copies launched
    # across chains for 2 days and never counted (4 letters). The 6h-baseline spike rule keeps common tickers quiet.
    words = {w for w in re.findall(r"[a-z]{5,}", (name or "").lower()) if w not in LAUNCH_FILLER and w not in STOP}
    sym = re.sub(r"[^a-z]", "", (symbol or "").lower())
    if len(sym) >= 4 and sym not in LAUNCH_FILLER and sym not in STOP:
        words.add(sym)
    for w in words:
        _LAUNCH_WORDS.setdefault(w, {})[creator] = now
        recent, base, cluster = launch_spike(w, now)
        if cluster:
            theme_add(w, "launch cluster")
        if cluster and AUTO is not None:
            AUTO.add(w, "launch-cluster", now)
            AUTO.seen[w] = max(AUTO.seen.get(w, 0), now)   # newest phrases are searched first
            if now - _CLUSTER_LOGGED.get(w, 0) > 30 * 60:
                _CLUSTER_LOGGED[w] = now
                others = sorted(set(AUTO.sources(w)) - {"launch-cluster"})
                normal = "no history yet" if base is None else f"normally {base:.1f}"
                log(f'launch cluster: "{w}" - {recent} creators in {LAUNCH_CLUSTER_MINUTES} min ({normal})'
                    + (f" | also in: {', '.join(others)} (cross-referenced)" if others else " | no other source yet"))


STRONG_SOURCES = {"trump", "ai-routine", "polymarket", "x-vip", "x-news", "manual", "news-multi"}
# need a second, independent source before they count. "launch-cluster" = several different people launching
# pump.fun coins with the same new word within minutes: the earliest trace of a story that's still spreading on
# TikTok/X (which the radar can't read). A cluster + Reddit/Google/Wikipedia/one news outlet = cross-referenced.
WEAK_SOURCES = {"reddit", "google", "wikipedia", "news", "launch-cluster"}


class AutoKeywords:
    def __init__(self):
        self.seen = {}        # phrase -> first-seen timestamp
        self.src = {}         # phrase -> {source: last-seen timestamp}
        self.last_harvest = 0

    def add(self, phrase, source, now=None):
        if " " not in phrase.strip() and phrase.strip().lower() in COMMON_WORDS:
            return
        now = now or time.time()
        self.seen.setdefault(phrase, now)
        self.src.setdefault(phrase, {})[source] = now

    def sources(self, phrase):
        """Independent sources that mention this phrase, or something containing it (e.g. 'raccoon' in a
        Reddit title and in Elon's post)."""
        out = set(self.src.get(phrase, {}))
        if len(phrase) >= 4:
            for ph, srcs in self.src.items():
                if ph != phrase and (phrase in ph or ph in phrase and len(ph) >= 4):
                    out |= set(srcs)
        return out

    def refresh(self):
        if time.time() - self.last_harvest < 10 * 60:
            return
        self.last_harvest = time.time()
        now = time.time()
        found = set()
        for source, fn in (("ai-routine", harvest_ai_topic), ("trump", harvest_trump), ("google", harvest_google_trends),
                           ("wikipedia", harvest_wikipedia_spikes), ("polymarket", harvest_polymarket_mentions),
                           ("reddit", harvest_reddit)):
            for ph in fn():
                self.add(ph, source, now)
                found.add(ph)
        for ph, outlets in harvest_news().items():
            # 1 Oct: single headline words ("dollars", "nation", "micro", "build", "revenue") counted as strong
            # stories, matched random coin names and burned the whole $0.50 AI budget by 08:00. A one-word news
            # phrase is now weak (needs a second source); multi-word phrases from 2+ outlets stay strong.
            multi = len(outlets) >= 2 and len(ph.split()) >= 2
            self.add(ph, "news-multi" if multi else "news", now)
            found.add(ph)
        cutoff = now - AUTO_KEYWORD_TTL_HOURS * 3600
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        self.src = {k: v for k, v in self.src.items() if max(v.values()) >= cutoff}
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
                rivals = [n for n in best.values() if n["addr"] != m["addr"]
                          and n["symbol"].lower() == m["symbol"].lower() and n["liq"] >= 5 * max(m["liq"], 1)]
                if rivals:          # e.g. a $0-liquidity "Space Inu $SI" riding the real $SI
                    log(f"skipped copycat ${m['symbol']} ({m['addr'][:6]}...) - bigger coin shares its ticker")
                    continue
                src = "your keywords.txt" if kw in manual else "auto (Trump posts / Google Trends / Wikipedia / AI routine)"
                await loop.run_in_executor(None, lambda: alert(
                    state, "CATALYST", m, f'Matched trending phrase "{kw}" from {src}', flags={"keyword": kw}))
            await asyncio.sleep(1.2)  # stay well under DexScreener's 300/min search limit
        await asyncio.sleep(KEYWORD_POLL_SECONDS)


# ----------------------------- FAST LOTTO (catch a take-off at the migration "bottom") -----------------------------
# 1 Oct: $MEME moved off pump.fun at 17:16 at $48K, was $344K by 17:21 and $855K at the peak - with no story, so the
# story-only main channel stayed quiet. Hundreds of coins migrate a day and most die, so a ping at every migration is
# useless. Instead each migration is checked at ~45s, 90s, 2.5 and 4 min, and pings ONLY if it is already taking off on
# real money (fast rise, a crowd of buyers far outnumbering sellers, big volume) and passes the safety check.
# These go to a SEPARATE ntfy channel (FAST_TOPIC), no AI (speed + budget), and are paper-traded as "FAST LOTTO" so the
# scorecard shows whether they pay. Pure lottery: GBP10-20, sell half at 2x.
FAST_CHECKS_S = tuple(range(20, 241, 15))   # every 15s for the first 4 min (was 4 checks: LEAFRA pinged at 4 min, $115K)
# Max was $600K. 1 Oct: the takers moved off pump.fun at $30-45K and pinged at $62-115K (𝕏/ACC $62K, Alonmas $74K);
# $SpaceX pinged at $454K because its pool OPENED at ~$430K (supply pre-bought in seconds, "copycat token", high
# holder correlation). User: "early, hopefully under $100K". Anything above $150K here is late or abnormal.
FAST_MIN_MCAP, FAST_MAX_MCAP = 60_000, 150_000
FAST_MIN_RISE = 1.4              # market cap vs our first look at it
FAST_MIN_BUYS_M5 = 60
FAST_BUY_RATIO = 1.5
FAST_MIN_VOL_M5 = 25_000
FAST_MAX_PER_DAY = 12
_FAST = {"day": "", "n": 0, "seen": set()}


def _fast_bar(age_min):
    """The buy/volume bar grows with the coin's age: 60 buys in its first minute is a lot more than at minute 4."""
    f = min(1.0, max(age_min, 0.5) / 3)
    return max(25, FAST_MIN_BUYS_M5 * f), max(10_000, FAST_MIN_VOL_M5 * f)


# ----------------------------- HOT THEMES + EARLY STORY (1 Oct: 𝕏/ACC) -----------------------------
# "𝕏 Accelerationism" ($𝕏/ACC) ran $32K -> $246K in ~15 min after Elon's "super intelligence" post. The radar logged
# an "accelerationism" launch cluster at 19:03, 7 min before its FAST LOTTO ping at $62K, but nothing linked the two.
# A HOT THEME = a launch-cluster word or a phrase from a VIP post in the last hour. A coin named after one is:
#  - watched every 15s while still on pump.fun; when it leads its theme on buying and passes RugCheck (which leaves
#    pump.fun's own curve account out of the holder count) it's logged as EARLY STORY - paper only while
#    EARLY_PINGS is False;
#  - tagged "FAST LOTTO + STORY" if it then passes the normal FAST LOTTO bar after migration.
# Every FAST LOTTO / EARLY STORY ping then gets follow-ups for an hour: one at 2x (the sell-half point) and one if it
# falls 30% from its peak (LEAFRA did 1.54x, then -92% within minutes).
THEME_MINUTES = 60
EARLY_POLL_S = 15
EARLY_WATCH_MINUTES = 25
EARLY_MAX_WATCH = 40
EARLY_MIN_MCAP, EARLY_MAX_MCAP = 12_000, 120_000
EARLY_MIN_BUYS_M5 = 40
EARLY_MIN_VOL_M5 = 5_000
EARLY_MIN_M5_CHANGE = 15
EARLY_MAX_PER_DAY = 8
EARLY_PINGS = False              # user, 1 Oct: "rather be a bit more accurate" - EARLY STORY is paper-tracked only
                                 # (scorecard kind "EARLY STORY") until it proves itself; FAST LOTTO still pings
# Same bar as FAST LOTTO for theme coins: the theme only adds the "+ STORY" label (𝕏/ACC passed the normal bar at
# $62K). Lower these (e.g. 40_000 / 1.2 / 0.7) to let theme coins ping sooner.
FAST_STORY_MIN_MCAP = FAST_MIN_MCAP
FAST_STORY_MIN_RISE = FAST_MIN_RISE
FAST_STORY_BAR = 1.0             # x the normal buys/volume bar
EXIT_WATCH_MINUTES = 60
EXIT_DROP_FROM_PEAK = 0.30
EXIT_DROP_AFTER_2X = 0.50
THEMES = {}                      # theme -> (last seen, source)
RECENT_CREATES = deque()         # (time, mint, name, symbol) of pump.fun launches in the last hour
EARLY = {}                       # mint -> (added, theme, source)
_EARLY = {"day": "", "n": 0, "seen": set()}
EXITS = {}                       # mint -> follow-up state after a fast-channel ping


def _flat(s):
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _theme_hits(theme, name, symbol):
    f = _flat(theme)
    return len(f) >= 5 and (f in _flat(name) or f == _flat(symbol))


def theme_match(name, symbol):
    """(theme, source) of the newest live hot theme this coin is named after, or None."""
    now = time.time()
    for th, (t, src) in sorted(THEMES.items(), key=lambda kv: -kv[1][0]):
        if now - t <= THEME_MINUTES * 60 and _theme_hits(th, name, symbol):
            return th, src
    return None


def theme_add(theme, src):
    now = time.time()
    fresh = theme not in THEMES or now - THEMES[theme][0] > THEME_MINUTES * 60
    THEMES[theme] = (now, src)
    for th in [k for k, (t, _) in THEMES.items() if now - t > THEME_MINUTES * 60]:
        THEMES.pop(th, None)
    if fresh:   # coins launched BEFORE the theme showed up - 𝕏/ACC was one of the launches that made the cluster
        for t, mint, name, symbol in list(RECENT_CREATES):
            if now - t <= THEME_MINUTES * 60 and _theme_hits(theme, name, symbol):
                early_add(mint, theme, src, symbol)


def early_add(mint, theme, src, symbol):
    if mint in EARLY or mint in _EARLY["seen"] or mint in _FAST["seen"]:
        return
    if len(EARLY) >= EARLY_MAX_WATCH:       # full: the oldest watch makes room
        EARLY.pop(min(EARLY, key=lambda a: EARLY[a][0]), None)
    EARLY[mint] = (time.time(), theme, src)
    log(f'early story: ${symbol} matches hot theme "{theme}" ({src}) - watching every {EARLY_POLL_S}s')


def note_create(mint, name, symbol):
    """Every new pump.fun launch: remember it for an hour, and watch it now if it matches a live theme."""
    now = time.time()
    RECENT_CREATES.append((now, mint, name or "", symbol or ""))
    while RECENT_CREATES and now - RECENT_CREATES[0][0] > THEME_MINUTES * 60:
        RECENT_CREATES.popleft()
    hit = theme_match(name, symbol)
    if hit:
        early_add(mint, hit[0], hit[1], symbol)


def early_check():
    """One batched DexScreener look at every theme coin on the early watch; pings the leader of a theme if it's
    taking off on real buying and passes RugCheck."""
    now = time.time()
    for a in [a for a, v in EARLY.items() if now - v[0] > EARLY_WATCH_MINUTES * 60]:
        EARLY.pop(a, None)
    if not EARLY:
        return
    day = time.strftime("%Y-%m-%d")
    if _EARLY["day"] != day:
        _EARLY.update(day=day, n=0, seen=set())
    ms = {a: metrics(p) for a, p in dex_pairs_for_tokens("solana", list(EARLY)).items() if a in EARLY}
    for a, m in sorted(ms.items(), key=lambda kv: -kv[1]["vol_m5"]):
        if a not in EARLY:
            continue
        _, theme, src = EARLY[a]
        leader = max((b for b in ms if b in EARLY and EARLY[b][1] == theme), key=lambda b: ms[b]["vol_m5"])
        if (leader != a or _EARLY["n"] >= EARLY_MAX_PER_DAY or a in _FAST["seen"]
                or not EARLY_MIN_MCAP <= m["mcap"] <= EARLY_MAX_MCAP
                or m["buys_m5"] < EARLY_MIN_BUYS_M5 or m["buys_m5"] < FAST_BUY_RATIO * max(m["sells_m5"], 1)
                or m["vol_m5"] < EARLY_MIN_VOL_M5 or m["chg_m5"] < EARLY_MIN_M5_CHANGE or dumping_now(m)):
            continue
        ok, notes, verified = rugcheck(a)
        EARLY.pop(a, None)
        _EARLY["seen"].add(a)
        if not ok:
            log(f"early story: ${m['symbol']} taking off but failed safety: {notes.split('REJECT: ')[-1][:80]}")
            log_candidate("EARLY STORY", m, False, ["safety"], {"story": theme})
            continue
        if not EARLY_PINGS:   # paper only: the scorecard shows whether these would have paid; FAST LOTTO may still ping
            log(f'early story (paper only, no ping): ${m["symbol"]} {fmt_usd(m["mcap"])} leading "{theme}"')
            log_candidate("EARLY STORY", m, False, ["paper only"], {"story": theme, "launch_src": src})
            continue
        _EARLY["n"] += 1
        _FAST["seen"].add(a)                 # no second ping as FAST LOTTO when it migrates
        where = "still on pump.fun (before migration)" if on_bonding_curve(m) else "just moved off pump.fun"
        body = (f"{m['name']} (${m['symbol']}) is named after the hot theme \"{theme}\" ({src}) and is taking off, "
                f"{where}: {fmt_usd(m['mcap'])}, {max(1, round(m['age_h'] * 60))} min old, +{m['chg_m5']:.0f}% in 5m.\n"
                f"5m: {m['buys_m5']}/{m['sells_m5']} buys/sells, vol {fmt_usd(m['vol_m5'])} | Liq {fmt_usd(m['liq'])}\n"
                f"CA: {a}\n{notes}\n"
                f"Leading the coins named after this theme. EARLIER than FAST LOTTO, so MORE of these die. "
                f"No AI check. LOTTERY ONLY: GBP10-20 max, sell half at 2x - you'll get a 2x / falling follow-up.")
        if send_ntfy(f"EARLY STORY: ${m['symbol']} {fmt_usd(m['mcap'])} - \"{theme}\"", body, click=coinbase_url(m),
                     priority="high", tags="zap,newspaper", actions=check_links(m), topic=FAST_TOPIC):
            log_ping("EARLY STORY", m, {"story": theme, "launch_src": src, "verified": verified})
            exit_add(a, m, "EARLY STORY")


def exit_add(mint, m, kind):
    if m.get("price"):
        EXITS[mint] = {"t": time.time(), "price": m["price"], "peak": m["price"], "kind": kind, "sent2x": False,
                       "rc2": False}
        RC_TRACK[mint] = {"t": time.time(), "kind": kind, "done": set(), "symbol": m.get("symbol", "?"),
                          "name": m.get("name", "?"), "url": m.get("url", "")}


# Extended to 10 and 15 min (1 Oct): WIRED (38x), TRUMP (2.6x, faded) and SHARED (rug) looked the same at the ping;
# the difference shows in the first 5-15 min. Each snapshot also records price vs ping and 5-min buys/sells.
RC_SNAP_S = (0, 45, 120, 300, 600, 900)
RC_TRACK = {}   # mint -> {"t", "kind", "done": set of snapshot offsets taken, "snaps": {offset: counts}}
# Two sections (user idea, 1 Oct): at the ping WIRED (38x) and SHARED (rug) looked identical, so the split is made
# 2 min later. TRUMP: holders 1,231 -> 2,325 in 2 min with 0 linked wallets; SHARED: 411 -> 456 in 45s.
# "REAL BUYERS" (green) = holders up 50%+ in 2 min and <=5 linked wallets; otherwise "QUICK FLIP" (red). Sent to the
# fast channel marked UNPROVEN (user asked for it, colour-coded); results on the scorecard's "label" line.
TIER_AT_S = 120
TIER_MIN_HOLDER_GROWTH = 1.5
TIER_MAX_LINKED = 5


def _tier_read(mint, s):
    h0, h2 = (s["snaps"].get(0) or [0, 0, 0]), (s["snaps"].get(TIER_AT_S) or [0, 0, 0])
    if not (h0[2] and h2[2]):
        return
    growth, linked = h2[2] / h0[2], max(h0[1], h2[1])
    # "REAL BUYERS" (was "RUNNER SIGNS"): TRUMP had this profile (holders 1,231 -> 4,499, 0-5 linked) but peaked at
    # 2.6x and faded - it means "more time to sell", not "will run like WIRED".
    tier = "REAL BUYERS" if growth >= TIER_MIN_HOLDER_GROWTH and linked <= TIER_MAX_LINKED else "QUICK FLIP"
    _flag_ping(mint, s["kind"], tier=tier, holder_growth=round(growth, 2))
    read = (f"Holders {h0[2]:,} -> {h2[2]:,} in 2 min (+{(growth - 1) * 100:.0f}%), "
            f"{linked} linked insider wallets.")
    if mint in EXITS:
        EXITS[mint]["read"] = read
    sym = s.get("symbol", mint[:6])
    log(f"tier: ${sym} {tier} - holders x{growth:.2f}, {linked} linked")
    # User asked to see it on the fast channel, colour-coded (ntfy can't colour text; the tag emoji is the colour).
    m = {"name": s.get("name", sym), "symbol": sym, "addr": mint, "chain": "solana", "url": s.get("url", "")}
    runner = tier == "REAL BUYERS"
    send_ntfy(f"{tier}: ${sym}",
              f"{read}\n" + ("Real buyers piling in, no insider cluster - so far these gave a window to sell (WIRED "
                             "38x, TRUMP 2.6x then faded). NOT a promise of a big run."
                             if runner else
                             "Slow holder growth or an insider cluster - looks like the quick rugs (SHARED, YAP).")
              + f"\nEARLY READ, UNPROVEN: being tested until ~8 Oct (scorecard 'label' line).\nCA: {mint}",
              click=coinbase_url(m), priority="high" if runner else "default",
              tags="green_circle" if runner else "red_circle", actions=check_links(m), topic=FAST_TOPIC)


def rc_counts(mint):
    """[insider networks, linked insider wallets, holders] from a fresh RugCheck report (None on failure)."""
    try:
        r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        time.sleep(1.0)
        return [len(r.get("insiderNetworks") or []), int(r.get("graphInsidersDetected") or 0),
                int(r.get("totalHolders") or 0)]
    except Exception:
        return None


def _flag_ping(addr, kind, **fl):
    """Add record-only test results to the newest pings_log entry for this coin + kind."""
    with PINGS_LOCK:
        pings = _load_pings()
        for p in reversed(pings):
            if p.get("addr") == addr and p.get("kind") == kind:
                p.setdefault("f", {}).update(fl)
                _save_pings(pings)
                return


# ----------------------------- RECORD-ONLY TESTS (1 Oct, user: "if anything compromises accuracy, we don't") ----
# Nothing below changes what reaches the phone. Each idea is paper-tracked; the scorecard's "TESTS" line shows whether
# it would have caught big winners without adding junk (or would have skipped losers without killing winners).
# 1) SECOND LOOK: $DOMAIN ("SI Strategy") was seen at $41K at 17:48 with one weak clue (a "domain" launch cluster)
#    and correctly held back - 107 such coins: only 13% hit 2x, lottery avg 0.36x - but it ran 35x ($backpack 53x).
#    Each held-back coin is looked at again after 10 and 20 min; if it's STILL climbing on real buying, that
#    strength is the missing second clue -> logged as "SECOND LOOK" (paper only).
# 2) FAST LOTTO filters, on every FAST LOTTO ping: average trade size (YAP: $22/trade, rugged; 𝕏/ACC: $76, ran 6x),
#    RugCheck's insider-network flag at the ping, and a second RugCheck 45s later (its wallet analysis fills in late;
#    but the AI skipped $DOMAIN partly for an insider network and it still ran 35x - so these stay record-only).
SECOND_LOOK_MIN = (10, 20)
SECOND_LOOK_MIN_RISE = 1.3       # price vs when it was held back
SECOND_LOOK_MIN_LIQ = 10_000
FAST_TEST_MIN_AVG_TRADE = 40     # $ per trade in the last 5 min
SECOND_LOOK = {}                 # addr -> {"t", "chain", "kind", "price", "mcap", "phrase", "flags", "done"}


def second_look_add(m, kind, phrase, flags):
    if m.get("price") and m["addr"] not in SECOND_LOOK and len(SECOND_LOOK) < 300:
        SECOND_LOOK[m["addr"]] = {"t": time.time(), "chain": m["chain"], "kind": kind, "price": m["price"],
                                  "mcap": m["mcap"], "phrase": phrase, "flags": dict(flags or {}), "done": 0}


def second_look_check():
    now = time.time()
    due = {}
    for a, s in list(SECOND_LOOK.items()):
        if s["done"] >= len(SECOND_LOOK_MIN):
            SECOND_LOOK.pop(a, None)
        elif now - s["t"] >= SECOND_LOOK_MIN[s["done"]] * 60:
            due.setdefault(s["chain"], []).append(a)
    for chain, addrs in due.items():
        pairs = dex_pairs_for_tokens(chain, addrs)
        for a in addrs:
            s = SECOND_LOOK.get(a)
            if not s:
                continue
            s["done"] += 1
            p = pairs.get(a)
            if not p:
                continue
            m = metrics(p)
            if (m["price"] >= SECOND_LOOK_MIN_RISE * s["price"] and m["buys_h1"] > m["sells_h1"]
                    and m["buys_m5"] >= m["sells_m5"] and m["chg_m5"] > -10 and not dumping_now(m)
                    and m["liq"] >= SECOND_LOOK_MIN_LIQ):
                SECOND_LOOK.pop(a, None)
                log(f'second look (paper only, no ping): ${m["symbol"]} {fmt_usd(s["mcap"])} -> {fmt_usd(m["mcap"])} '
                    f'after {(now - s["t"]) / 60:.0f} min, still climbing ("{s["phrase"]}")')
                SHADOW_SEEN.pop(a, None)      # a separate paper entry from the held-back one
                log_candidate("SECOND LOOK", m, False, ["paper only"],
                              dict(s["flags"], story=s["phrase"], held_kind=s["kind"],
                                   held_mcap=round(s["mcap"]), wait_min=round((now - s["t"]) / 60)))


def exit_check():
    """Follow-ups for fast-channel pings: one at 2x, one when it falls 30% from its peak (then the watch ends)."""
    now = time.time()
    for a in [a for a, e in EXITS.items() if now - e["t"] > EXIT_WATCH_MINUTES * 60]:
        EXITS.pop(a, None)
    if not EXITS and not RC_TRACK:
        return
    for a, e in list(EXITS.items()):      # record-only test: a fresh RugCheck 45s after the ping
        if not e["rc2"] and now - e["t"] >= 45:
            e["rc2"] = True
            _rc_cache.pop(a, None)
            ok2, notes2, _ = rugcheck(a)
            _flag_ping(a, e["kind"], rc2_ok=ok2, rc2_insider_net="insider network" in notes2)
    # record-only: when do linked wallets show up? (1 Oct, now: runners WIRED/Alonmas/𝕏/ACC had 4/0/0 linked
    # wallets and 800-4,100 holders; rugs Heinrich/YAP/LEAFRA/MUSE had 235/135/33/16 and 235-814 holders -
    # but at the ping most showed none). Snapshot at 0s, 45s, 2 min and 5 min after the ping. Kept apart from EXITS:
    # a FALLING ping ends the follow-ups, but the rugs' 2/5-min snapshots are the ones we need most ($SHARED).
    for a, s in list(RC_TRACK.items()):
        todo = [off for off in RC_SNAP_S if off not in s["done"]]
        if not todo:
            RC_TRACK.pop(a, None)
            continue
        if now - s["t"] >= todo[0]:
            s["done"].add(todo[0])
            counts = rc_counts(a)
            s.setdefault("snaps", {})[todo[0]] = counts
            mk = {}
            p_ = dex_pairs_for_tokens("solana", [a]).get(a)
            if p_:
                mm = metrics(p_)
                mk = {f"mk_{todo[0]}s": [round(mm["mcap"]), mm["buys_m5"], mm["sells_m5"]]}
            _flag_ping(a, s["kind"], **{f"rc_{todo[0]}s": counts}, **mk)
            if todo[0] == TIER_AT_S:
                _tier_read(a, s)
            break                           # one RugCheck call per pass (~1/s limit)
    for a, p in dex_pairs_for_tokens("solana", list(EXITS)).items():
        e = EXITS.get(a)
        m = metrics(p)
        if not e or not m["price"]:
            continue
        e["peak"] = max(e["peak"], m["price"])
        x, peak_x, mins = m["price"] / e["price"], e["peak"] / e["price"], (now - e["t"]) / 60
        stats = (f"5m: {m['buys_m5']}/{m['sells_m5']} buys/sells, vol {fmt_usd(m['vol_m5'])} | "
                 f"Liq {fmt_usd(m['liq'])}\n" + (e["read"] + "\n" if e.get("read") else "") + f"CA: {a}")
        if not e["sent2x"] and x >= 2:
            e["sent2x"] = True
            send_ntfy(f"2x: ${m['symbol']} {fmt_usd(m['mcap'])} (x{x:.1f} since the ping)",
                      f"{m['name']} is at x{x:.1f} {mins:.0f} min after its {e['kind']} ping. In the lottery plan this "
                      f"is the sell-half point. You'll get one more ping if it falls 50% from its peak.\n{stats}",
                      click=coinbase_url(m), priority="high", tags="moneybag", actions=check_links(m),
                      topic=FAST_TOPIC)
        # After a 2x, swings are bigger: 1 Oct Alonmas got FALLING at x3.58 (-38% from peak), then went to x6.1.
        # Runners that have doubled get -50% from the peak; a real rug (𝕏/ACC -99%) still trips it at once.
        elif (m["price"] <= (1 - (EXIT_DROP_AFTER_2X if e["sent2x"] else EXIT_DROP_FROM_PEAK)) * e["peak"]
              or m["liq"] < 1000):
            EXITS.pop(a, None)
            drop = 1 - m["price"] / e["peak"]
            peak_txt = (f"never rose after its {e['kind']} ping" if peak_x < 1.02 else
                        f"peaked at x{peak_x:.2f} after its {e['kind']} ping")
            send_ntfy(f"FALLING: ${m['symbol']} down {drop:.0%} from its peak (x{x:.2f} vs the ping)",
                      f"{m['name']} {peak_txt} and is now x{x:.2f} vs the ping price, "
                      f"{mins:.0f} min later. Most of these keep falling. Last follow-up for this coin.\n{stats}",
                      click=coinbase_url(m), priority="high", tags="warning", actions=check_links(m),
                      topic=FAST_TOPIC)


EXIT_POLL_S = 5   # follow-ups look every 5s (was 15: MUSE's FALLING ping came at -48%, not -30%). PumpPortal's
                  # live trade stream needs an API key funded from a SOL wallet - not used (no wallets, user rule).


def exit_restore():
    """Follow-ups lived only in memory: the 21:12 restart on 1 Oct dropped Alonmas's (it then went 9x -> 0.04x with no
    FALLING ping). Rebuild them from pings_log for fast-channel pings still inside the watch window."""
    now = time.time()
    with PINGS_LOCK:
        pings = _load_pings()
    for p in pings:
        if not (p.get("pinged") and p.get("kind") in ("FAST LOTTO", "EARLY STORY") and p.get("price")):
            continue
        if now - p["t"] < max(RC_SNAP_S) + 30:
            # snapshots whose moment passed during the restart are skipped, not taken late
            RC_TRACK[p["addr"]] = {"t": p["t"], "kind": p["kind"], "symbol": p.get("symbol", "?"),
                                   "name": p.get("name", "?"), "url": p.get("url", ""),
                                   "snaps": {int(k[3:-1]): v for k, v in (p.get("f") or {}).items()
                                             if k.startswith("rc_") and k.endswith("s") and k[3:-1].isdigit()},
                                   "done": {off for off in RC_SNAP_S
                                            if f"rc_{off}s" in (p.get("f") or {}) or now - p["t"] > off + 30}}
        if (now - p["t"] < EXIT_WATCH_MINUTES * 60
                and (p.get("last_x") or 1) > (1 - EXIT_DROP_AFTER_2X) * (p.get("peak_x") or 1)):  # not already crashed
            EXITS[p["addr"]] = {"t": p["t"], "price": p["price"], "peak": p["price"] * (p.get("peak_x") or 1),
                                "kind": p["kind"], "sent2x": (p.get("peak_x") or 1) >= 2, "rc2": True}
    if EXITS:
        names = {p["addr"]: p["symbol"] for p in pings if p.get("addr") in EXITS}
        log(f"follow-ups restored after restart: {', '.join(names.values())}")


async def early_story_loop(state):
    loop = asyncio.get_running_loop()
    try:
        exit_restore()
    except Exception as e:
        log(f"exit_restore error: {e}")
    last_slow = 0.0
    while True:
        fns = [exit_check]
        if time.time() - last_slow >= EARLY_POLL_S:
            last_slow = time.time()
            fns += [early_check, second_look_check]
        for fn in fns:
            try:
                await loop.run_in_executor(None, fn)
            except Exception as e:
                log(f"{fn.__name__} error: {e}")
        await asyncio.sleep(EXIT_POLL_S)


# 3) FAST EARLY (record-only, 1 Oct): MUSE moved off pump.fun at $41K and pinged at $70K, near the top of a short
#    staged pump. Would a lower bar ($45K, 1.3x rise, 80% of the buys/volume bar) catch more of the run, or just
#    more losers? Logged once per coin as "FAST EARLY" (paper only); the real FAST LOTTO rule is untouched.
FAST_EARLY_MIN_MCAP = 45_000
FAST_EARLY_MIN_RISE = 1.3
FAST_EARLY_BAR = 0.8
_FAST_EARLY_SEEN = set()


def fast_early_paper(mint, m, rise, chg_ok=False):
    if mint in _FAST_EARLY_SEEN:
        return
    buys_bar, vol_bar = _fast_bar(m["age_h"] * 60)
    if (FAST_EARLY_MIN_MCAP <= m["mcap"] <= FAST_MAX_MCAP
            and (rise >= FAST_EARLY_MIN_RISE or chg_ok)
            and m["buys_m5"] >= buys_bar * FAST_EARLY_BAR and m["buys_m5"] >= FAST_BUY_RATIO * max(m["sells_m5"], 1)
            and m["vol_m5"] >= vol_bar * FAST_EARLY_BAR and not dumping_now(m)):
        _FAST_EARLY_SEEN.add(mint)
        if len(_FAST_EARLY_SEEN) > 5000:
            _FAST_EARLY_SEEN.clear()
        ok, notes, _ = rugcheck(mint)
        if ok:
            SHADOW_SEEN.pop(mint, None)
            log_candidate("FAST EARLY", m, False, ["paper only"],
                          {"rise": round(rise, 2), "avg_trade": round(m["vol_m5"] / max(m["buys_m5"] + m["sells_m5"], 1)),
                           "insider_net": "insider network" in notes})


# Copycats (1 Oct): within minutes of WIRED's run (38x) dozens of "WIRED"/"WIREDCAT"/"WIREDINU"/"WDOG" coins
# launched, and $SpaceX carried RugCheck's "Copycat token" risk. A copy of a coin that just ran is the classic quick
# rug, so FAST LOTTO skips a coin whose name or ticker matches one already pinged in the last 12h (different
# address) or that RugCheck calls a copycat. Skipped ones are paper-logged ("copycat") so the scorecard can check it.
COPYCAT_HOURS = 12


def _fast_copycat_of(m):
    n, s = _flat(m["name"]), _flat(m["symbol"])
    with PINGS_LOCK:
        pings = _load_pings()
    cut = time.time() - COPYCAT_HOURS * 3600
    for p in reversed(pings):
        if p["t"] < cut:
            break
        if p.get("pinged") and p.get("addr") != m["addr"]:
            pn, ps = _flat(p.get("name")), _flat(p.get("symbol"))
            if ((len(ps) >= 3 and ps in (s, n)) or (len(pn) >= 4 and pn == n)
                    or (len(ps) >= 5 and (ps in s or ps in n))):     # WIREDCAT, WIREDINU, wiredworm
                return p.get("symbol")
    return None


def _rc_copycat(mint):
    try:
        r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")    # same 5-min cache window upstream
        return any("copycat" in str(x.get("name", "")).lower() for x in r.get("risks") or [])
    except Exception:
        return False


def fast_lotto_check(mint, first_mcap):
    """One look at a freshly migrated coin. Returns (metrics, first_mcap) and pings if it's taking off."""
    p = dex_pairs_for_tokens("solana", [mint]).get(mint)
    if not p:
        return first_mcap
    m = metrics(p)
    first_look = not first_mcap
    first_mcap = first_mcap or m["mcap"]
    rise = m["mcap"] / first_mcap if first_mcap else 1
    # DexScreener's "+X% in 5 min" counts the pool's opening jump, so it only stands in for a rise on our FIRST look
    # (no baseline yet). 1 Oct: $SpaceX passed at x1.1 since our first look because its 5-min change read +866%.
    chg_ok = first_look and m["chg_m5"] >= 50
    day = time.strftime("%Y-%m-%d")
    if _FAST["day"] != day:
        _FAST.update(day=day, n=0, seen=set())
    # own thread: its RugCheck (~1s) must never delay a real FAST LOTTO ping
    threading.Thread(target=fast_early_paper, args=(mint, m, rise, chg_ok), daemon=True).start()
    th = theme_match(m["name"], m["symbol"])
    buys_bar, vol_bar = _fast_bar(m["age_h"] * 60)
    min_mcap, min_rise = FAST_MIN_MCAP, FAST_MIN_RISE
    if th:      # named after a hot theme (𝕏/ACC): a lower bar, so it pings sooner
        buys_bar, vol_bar = buys_bar * FAST_STORY_BAR, vol_bar * FAST_STORY_BAR
        min_mcap, min_rise = FAST_STORY_MIN_MCAP, FAST_STORY_MIN_RISE
    if (mint in _FAST["seen"] or _FAST["n"] >= FAST_MAX_PER_DAY
            or not min_mcap <= m["mcap"] <= FAST_MAX_MCAP
            or (rise < min_rise and not chg_ok)
            or m["buys_m5"] < buys_bar or m["buys_m5"] < FAST_BUY_RATIO * max(m["sells_m5"], 1)
            or m["vol_m5"] < vol_bar or dumping_now(m)):
        return first_mcap
    ok, notes, verified = rugcheck(mint)
    _FAST["seen"].add(mint)
    if not ok:
        log(f"fast lotto: ${m['symbol']} taking off but failed safety: {notes.split('REJECT: ')[-1][:80]}")
        log_candidate("FAST LOTTO", m, False, ["safety"])
        return first_mcap
    copy_of = _fast_copycat_of(m) or ("RugCheck copycat" if _rc_copycat(mint) else None)
    if copy_of:
        log(f"fast lotto: ${m['symbol']} taking off but it's a copycat of {copy_of} - skipped (paper-logged)")
        log_candidate("FAST LOTTO", m, False, ["copycat"], {"copy_of": copy_of})
        return first_mcap
    _FAST["n"] += 1
    body = (f"{m['name']} (${m['symbol']}) just moved off pump.fun and is TAKING OFF: {fmt_usd(first_mcap)} -> "
            f"{fmt_usd(m['mcap'])} in ~{max(1, round(m['age_h'] * 60))} min.\n"
            f"5m: {m['buys_m5']}/{m['sells_m5']} buys/sells, vol {fmt_usd(m['vol_m5'])} | Liq {fmt_usd(m['liq'])}\n"
            f"CA: {mint}\n{notes}\n"
            + (f"STORY: named after the hot theme \"{th[0]}\" ({th[1]}). No AI check. " if th else
               "NO STORY, NO AI CHECK - pure momentum. ")
            + "Most of these still die within the hour. "
              "LOTTERY ONLY: GBP10-20 max, sell half at 2x, expect to lose it. 2x / falling follow-ups will come.")
    if send_ntfy(f"FAST LOTTO{' + STORY' if th else ''}: ${m['symbol']} {fmt_usd(m['mcap'])} "
                 f"(x{rise:.1f} since migration)", body,
                 click=coinbase_url(m),
                 priority="high", tags="zap", actions=check_links(m), topic=FAST_TOPIC):
        avg_trade = m["vol_m5"] / max(m["buys_m5"] + m["sells_m5"], 1)
        log_ping("FAST LOTTO", m, {"rise": round(rise, 2), "verified": verified,
                                   "avg_trade": round(avg_trade), "insider_net": "insider network" in notes,
                                   "first_look": first_look, "first_mcap": round(first_mcap),
                                   **({"story": th[0], "launch_src": th[1]} if th else {})})
        exit_add(mint, m, "FAST LOTTO")
    return first_mcap


async def fast_lotto_watch(mint):
    loop = asyncio.get_running_loop()
    start, first = time.time(), None
    for s_ in FAST_CHECKS_S:
        await asyncio.sleep(max(0, start + s_ - time.time()))
        try:
            first = await loop.run_in_executor(None, fast_lotto_check, mint, first)
        except Exception as e:
            log(f"fast lotto error: {e}")
        if mint in _FAST["seen"]:
            return


async def graduations_loop(state):
    try:
        import websockets  # noqa
    except ImportError:
        log("GRADUATIONS off: run  pip install websockets  to enable pump.fun migration alerts")
        return
    import re
    import websockets
    loop = asyncio.get_running_loop()

    async def recheck(mint):
        await asyncio.sleep(15 * 60)
        first = (await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", [mint])).get(mint)
        if not first or dumping_now(metrics(first)):
            return
        p15 = metrics(first)["price"]
        await asyncio.sleep((GRAD_RECHECK_MINUTES - 15) * 60)
        p = (await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", [mint])).get(mint)
        if not p:
            return
        m = metrics(p)
        if p15 and m["price"] < 0.7 * p15:      # gave back >30% since the 15-min mark -> insiders exiting
            return
        if (GRAD_MIN_MCAP <= m["mcap"] <= GRAD_MAX_MCAP and m["chg_h1"] <= 500 and m["chg_m5"] >= 0 and not dumping_now(m) and m["buys_h1"] >= m["sells_h1"]
                and m["vol_h1"] >= RUNNER_MIN_H1_VOLUME and m["liq"] >= RUNNER_MIN_LIQUIDITY):
            await loop.run_in_executor(None, alert, state, "GRADUATED & HOLDING", m,
                                       f"Migrated off pump.fun ~{GRAD_RECHECK_MINUTES} min ago and still holding. "
                                       f"Migration is where GOCARDS got dumped - be quick with stops.")

    while True:
        try:
            async with websockets.connect("wss://pumpportal.fun/api/data", ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log("connected to PumpPortal (new launches + graduations)")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    mint = msg.get("mint")
                    if not mint:
                        continue
                    if msg.get("txType") == "create":     # brand-new pump.fun launch
                        nursery_add(mint)
                        launch_cluster_note(msg.get("name"), msg.get("symbol"), msg.get("traderPublicKey") or mint)
                        note_create(mint, msg.get("name"), msg.get("symbol"))
                        for a in [a for a, v in STORY_LAUNCHES.items() if time.time() - v[0] > 3600]:
                            STORY_LAUNCHES.pop(a, None)
                        hit = story_launch_match(msg.get("name"), msg.get("symbol"))
                        # Copies of a story coin already on the sleepers list (dozens of new "$SI" a night) can never
                        # ping (alert() drops small copycats) - don't let them take watch slots. 1 Oct: the slots
                        # were full of $SI copies when $SII launched at 03:52; it ran ~9x unwatched.
                        tick = re.sub(r"[^A-Z0-9]", "", (msg.get("symbol") or "").upper())
                        if hit and tick and tick in story_coin_symbols():
                            hit = None
                        if hit and len(STORY_LAUNCHES) < STORY_LAUNCH_MAX_WATCH:
                            STORY_LAUNCHES[mint] = (time.time(), hit[0], hit[1])
                            log(f"story launch: ${msg.get('symbol')} matches \"{hit[0]}\" ({hit[1]}) - watching")
                            asyncio.create_task(story_launch_watch(state, mint, hit[0], hit[1]))
                    else:                                  # migration off the bonding curve
                        hot_add(mint, "solana")
                        asyncio.create_task(recheck(mint))
                        asyncio.create_task(fast_lotto_watch(mint))
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
X_ACCOUNTS = [
    "elonmusk",          # biggest single memecoin catalyst (DOGE, JIMOTHY raccoon post)
    "realDonaldTrump",   # mostly on Truth Social (read free); X covers the rest
    "cz_binance",        # his dog/phrases spawn BSC & Solana coins; posts a few times a day (cheap)
    "toly",              # Toly (Anatoly Yakovenko), Solana co-founder - his memes move Solana coins
    "a1lon9",            # Alon, pump.fun co-founder
    "VladTenev",         # Robinhood CEO - following Super Inu's account was an early $SI signal
    "MELANIATRUMP",      # launched $MELANIA herself (Jan 2025, +12,000% in 24h); posts rarely, so pennies a month
]
# News aggregators: they break story-coin news first ("JUST IN: Vlad Tenev follows Super Inu $SI" came from
# WhaleInsider hours before the big run). They post a lot (~$5-10/month each), so their posts only ping when they
# name a $TICKER that's a live Solana/Base coin; their headlines also go to the AI judge as context.
X_FEED_ACCOUNTS = ["WhaleInsider", "WatcherGuru"]
X_ACCOUNTS += X_FEED_ACCOUNTS
FEED_TICKER_IGNORE = {"BTC", "ETH", "SOL", "XRP", "BNB", "USDT", "USDC", "DOGE", "ADA", "TRX", "SUI", "TON",
                      "AVAX", "LINK", "DOT", "LTC", "SHIB", "PEPE", "HYPE", "TRUMP", "MSTR", "COIN", "TSLA", "NVDA"}
X_POLL_SECONDS = 60
# The accounts whose single post can start a run are checked every 15s (empty checks aren't billed by X - only
# posts returned are). On an X "429 Too Many Requests" that account's interval doubles (max 2 min), then recovers.
X_FAST_ACCOUNTS = {"elonmusk", "realDonaldTrump", "cz_binance"}
X_FAST_POLL_SECONDS = 15
# Words these accounts post about every day - a coin named after them doesn't get a surprise wave of buyers.
# 30 Sep: Elon's "Starship Flight 14" post matched a $43K STARSHIP coin that didn't move at all.
VIP_ROUTINE_WORDS = {
    "elonmusk": {"starship", "spacex", "tesla", "grok", "xai", "falcon", "rocket", "mars", "neuralink", "optimus",
                 "cybertruck", "dragon", "starlink", "boring", "flight", "launch", "physics", "moon", "telescopes"},
    "realDonaldTrump": {"america", "american", "maga", "trump", "president", "great", "again", "border", "biden",
                        "democrats", "election", "country"},
}
VIP_MATCH_MIN_VOL_H24 = 10_000   # a matching coin must already have some life, not a dead pool
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


# 1 Oct: Elon's "Three Falcons ready to fly simultaneously" pinged $READY and $three - everyday words, nothing to do
# with the post. A single word only counts if it's NOT a common English word (animals, names, memes still count).
COMMON_WORDS = set("""one two three four five six seven eight nine ten hundred thousand million billion first second
third last next new old big small great good best better bad worse worst ready set go going gone come coming back
today tomorrow yesterday tonight week weeks month year years time times day days night morning soon now later
just very really much many more most less few lot lots all any some every each other another same different
make made making take took taking give gave get got getting keep kept put let say said tell told think thought
know knew want need like love look looks see seen watch work works working play live life world people person
man men woman women thing things way ways part place home house team game games news true false real fact
yes no not never ever always maybe probably sure right left high low long short fast slow hard easy free full
open close closed start started end ended done fly flying flight launch launched launching simultaneously
amazing awesome incredible cool nice wow huge massive major important interesting exactly absolutely
company business market money price stock stocks deal deals plan plans report update support system service
power energy water fire earth space future past present history order orders law laws rule rules vote votes
country nation state states city america american president government house senate congress
industry industries industrial jobs job workers worker economy economic trade trades tariff tariffs tax taxes
billion billions trillion dollar dollars quarter percent record records growth investment investments factory
factories manufacturing companies corporation bank banks fund funds stock crypto coin coins token tokens meme memes
military war peace border security deal agreement court judge case cases trial crime police school schools
health care hospital doctor drugs medical media fake press report reports reporter statement official officials
leader leaders party democrat democrats republican republicans election elections campaign poll polls voters
history historic win winning wins loss lost success successful failed failure disaster total complete beautiful
strong weak smart stupid crazy sad tremendous incredible fantastic terrible horrible wonderful happy thank thanks
everyone everybody nobody somebody something nothing anything family friend friends nation's world's""".split())


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
    return {p for p in phrases if 2 <= len(p) <= 40 and not (" " not in p and p.lower() in COMMON_WORDS)}


def _post_age_min(post):
    try:
        import calendar
        t = calendar.timegm(time.strptime(post.get("created_at", "")[:19], "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - t) / 60
    except Exception:
        return 0


async def feed_post_tickers(state, acct, text):
    """A news account named a $TICKER: find the live Solana/Base coin with that exact ticker and send it to the
    normal alert path (safety check + AI judge)."""
    import re
    loop = asyncio.get_running_loop()
    for tick in dict.fromkeys(t.upper() for t in re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b", text)):
        if tick in FEED_TICKER_IGNORE:
            continue
        best = None
        for p in await loop.run_in_executor(None, dex_search, tick):
            m = metrics(p)
            if (m["chain"] in CHAINS and m["symbol"].replace("$", "").upper() == tick and m["liq"] >= 20_000
                    and RUNNER_MIN_MCAP <= m["mcap"] <= KEYWORD_MAX_MCAP and m["vol_h24"] >= VIP_MATCH_MIN_VOL_H24
                    and (best is None or m["vol_h24"] > best["vol_h24"])):
                best = m
        if best:
            await loop.run_in_executor(None, lambda: alert(
                state, "NEWS MENTION", best,
                f'@{acct} just posted about ${tick}: "{text[:220]}". News accounts naming a coin is how Super Inu '
                f'started its run. Check it is the right CA (highest volume shown).',
                flags={"story": f"@{acct} ${tick}"}))
        await asyncio.sleep(1)


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
    ids_path = os.path.join(HERE, "x_ids.json")      # cache account ids: each lookup is a paid X read
    try:
        with open(ids_path) as f:
            cached = json.load(f)
    except Exception:
        cached = {}
    for name in X_ACCOUNTS:
        if cached.get(name):
            ids[name] = cached[name]
            continue
        try:
            ids[name] = (await loop.run_in_executor(None, x_get, f"users/by/username/{name}", token))["data"]["id"]
        except Exception as e:
            log(f"X: couldn't resolve @{name}: {e}")
    try:
        with open(ids_path, "w") as f:
            json.dump(ids, f)
    except Exception:
        pass
    log(f"X watch on for: {', '.join('@' + n for n in ids)}")
    since_path = os.path.join(HERE, "x_since.json")  # resume from the last seen post: restarts cost no X reads
    try:
        with open(since_path) as f:
            since.update({k: v for k, v in json.load(f).items() if k in ids})
    except Exception:
        pass
    hb = {"t": time.time(), "polls": 0, "posts": 0, "errors": 0, "last_err": "", "last_post": time.time(),
          "warned": False}
    poll_iv, next_due = {}, {}                           # per-account check interval and next check time
    while True:
        if time.time() - hb["t"] >= 30 * 60:        # heartbeat every 30 min so you can see it's alive
            log(f"X watch alive: {hb['polls']} polls, {hb['posts']} new posts, {hb['errors']} errors in 30 min"
                + (f" (last error: {hb['last_err'][:80]})" if hb["errors"] else ""))
            hb.update(t=time.time(), polls=0, posts=0, errors=0, last_err="")
        if time.time() - hb["last_post"] > 3 * 3600 and not hb["warned"]:
            hb["warned"] = True                        # 8 accounts incl. WatcherGuru never go 3h silent
            send_ntfy("X watch may be stuck", "No new post from any of the watched X accounts for 3 hours. "
                      "Check console.x.com credit/usage and the radar log for 'X error'.", priority="default",
                      tags="warning")
        for name, uid in ids.items():
            base_iv = X_FAST_POLL_SECONDS if name in X_FAST_ACCOUNTS else X_POLL_SECONDS
            iv = poll_iv.setdefault(name, base_iv)
            if time.time() < next_due.get(name, 0):
                continue
            next_due[name] = time.time() + iv
            q = "exclude=replies,retweets&tweet.fields=created_at&max_results=5"
            if since.get(name):
                q += f"&since_id={since[name]}"
            hb["polls"] += 1
            try:
                res = await loop.run_in_executor(None, x_get, f"users/{uid}/tweets?{q}", token)
                poll_iv[name] = base_iv
            except Exception as e:
                if "429" in str(e):
                    poll_iv[name] = min(iv * 2, 120)
                    next_due[name] = time.time() + poll_iv[name]
                    log(f"X rate limit @{name}: checking every {poll_iv[name]}s for now")
                else:
                    log(f"X error @{name}: {e}")
                hb["errors"] += 1
                hb["last_err"] = str(e)
                continue
            posts = res.get("data") or []
            if not posts:
                continue
            hb["posts"] += len(posts)
            hb["last_post"] = time.time()
            hb["warned"] = False
            first_run = name not in since
            since[name] = posts[0]["id"]
            try:
                with open(since_path, "w") as f:
                    json.dump(since, f)
            except Exception:
                pass
            if first_run:            # don't act on old posts at startup
                continue
            posts = [p for p in posts if _post_age_min(p) <= 120]   # after a long outage, skip stale posts
            for post in posts:
                text = post.get("text", "")
                log(f"@{name} posted: {text[:80]!r}")
                RECENT_VIP.append((time.time(), name, text))
                del RECENT_VIP[:-30]
                # 1) VIP named a contract address directly -> immediate urgent ping
                vip_contract_ping(f"@{name}", text)
                if name in X_FEED_ACCOUNTS:
                    if AUTO is not None:
                        for ph in vip_phrases(text):
                            AUTO.src.setdefault(ph, {})["x-news"] = time.time()
                    await feed_post_tickers(state, name, text)
                    continue
                # 2) memeable phrases -> feed the keyword engine + ping existing matching coins BEFORE they move
                phrases = vip_phrases(text)
                if AUTO is not None:
                    now = time.time()
                    for ph in phrases:
                        AUTO.add(ph, "x-vip", now)
                        AUTO.seen[ph] = now      # newest = searched first, every 3 minutes
                matches = []
                routine = VIP_ROUTINE_WORDS.get(name, set())
                norm = lambda w: w[:-1] if w.endswith("s") and len(w) > 4 else w    # "falcons" = "falcon"
                search = [p for p in phrases if not {norm(w) for w in p.split()} <= routine
                          and not {norm(w) for w in p.split()} <= (routine | COMMON_WORDS)][:6]
                for ph in search:            # hot themes: coins named after it get the early watch
                    theme_add(ph, f"@{name} post")
                # all phrase searches at once (was one per second): the phone buzzes ~5s sooner after a VIP post
                results = await asyncio.gather(*(loop.run_in_executor(None, dex_search, ph) for ph in search))
                for ph, pairs in zip(search, results):
                    for p in pairs:
                        m = metrics(p)
                        if (m["chain"] in CHAINS and m["addr"] and coin_matches(ph, m)
                                and m["liq"] >= 20_000 and m["mcap"] <= KEYWORD_MAX_MCAP
                                and m["vol_h24"] >= VIP_MATCH_MIN_VOL_H24):
                            matches.append((ph, m))
                best = {}
                for ph, m in matches:
                    if m["addr"] not in best or m["vol_h24"] > best[m["addr"]][1]["vol_h24"]:
                        best[m["addr"]] = (ph, m)
                top = sorted(best.values(), key=lambda x: -x[1]["vol_h24"])[:3]
                judged = []
                for ph, m in top[:2]:      # AI: is the post really ABOUT what this coin is named after?
                    v = await loop.run_in_executor(None, ai_judge, "X VIP MATCH", m,
                                                   f'@{name} just posted: "{text[:240]}". The radar matched the word '
                                                   f'"{ph}" to this coin. PING only if the post is clearly about the '
                                                   f'thing/animal/character/meme the coin is named after.',
                                                   "not checked yet (VIP heads-up)", {"story": ph})
                    # "BUDGET" = daily AI budget used up: treat like AI unavailable - a VIP heads-up still goes out
                    if v is None or v == "BUDGET" or v["verdict"] == "PING":
                        judged.append((ph, m))
                    else:
                        log(f"VIP match skipped by AI: ${m['symbol']} ('{ph}') - {v['reason']}")
                top = judged
                if top:
                    lines = "\n".join(f'- "{ph}" -> {m["name"]} ${m["symbol"]} {fmt_usd(m["mcap"])} | vol 24h '
                                      f'{fmt_usd(m["vol_h24"])} | 1h {m["chg_h1"]:+.0f}% | CA {m["addr"]}'
                                      for ph, m in top)
                    send_ntfy(f"@{name} just posted - matching coins", f'"{text[:200]}"\n\nExisting coins that match:\n'
                              f"{lines}\n\nThese haven't necessarily moved yet - this is the EARLIEST possible heads-up "
                              "(JIMOTHY did +331% after Elon's raccoon post). Only buy if volume starts jumping in the next "
                              "few minutes - no buyers = no move. Check GMGN, GBP20-50 max, half out at 2x.",
                              click=coinbase_url(top[0][1]), priority="urgent" if name in X_FAST_ACCOUNTS else "high",
                              tags="bird", actions=check_links(top[0][1]))
            await asyncio.sleep(0.2)
        await asyncio.sleep(1)


# ----------------------------- SLEEPERS (second-wave detector) -----------------------------
# Coins tied to a famous animal/character/phrase often pump AGAIN when a VIP reposts the story, even without
# naming the coin: JIMOTHY (viral raccoon) went $3.8M -> $16.2M (+331%) on Aug 8 2026 after Elon posted a
# raccoon video; Super Inu went +130% on Sep 30 when Trump repeated "super intelligence". We watch these
# known "story coins" every minute and ping the moment volume wakes up. Add CAs to sleepers.txt (one per line,
# optional "# note").
SLEEPER_FILE = "sleepers.txt"
SLEEPER_MIN_H1_CHANGE = 20
SLEEPER_VOL_MULTIPLE = 3.0      # last hour's volume vs the average hour of the last 24h


AUTO_SLEEPER_FILE = "auto_sleepers.txt"   # written by the radar: story coins that already did 3x+
AUTO_SLEEPER_MIN_X = 3.0


def load_sleepers():
    out = []
    for fn in (SLEEPER_FILE, AUTO_SLEEPER_FILE):
        try:
            with open(os.path.join(HERE, fn), encoding="utf-8") as f:
                out += [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]
        except FileNotFoundError:
            pass
    return list(dict.fromkeys(out))


def add_auto_sleepers(entries):
    """entries: [(addr, note)]. Jimothy and Super Inu both had big SECOND waves days after the first run."""
    have = set(load_sleepers())
    new = [(a, n) for a, n in entries if a not in have]
    if not new:
        return
    try:
        with open(os.path.join(HERE, AUTO_SLEEPER_FILE), "a", encoding="utf-8") as f:
            for a, n in new:
                f.write(f"{a}  # {n}\n")
        log(f"auto-sleepers: now watching {', '.join(n.split(' ')[0] for _, n in new)} for a second wave")
    except Exception as e:
        log(f"could not write auto-sleepers: {e}")


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


# ----------------------------- SCORECARD + PAPER TRADING (learning loop) -----------------------------
# Every ping AND every near-miss the filters rejected ("shadow") is logged with its features, then tracked
# every ~45s for 3h and every 10 min to 24h. Each one is paper-traded with the current rules plus two
# alternatives, so the daily scorecard shows what the rules WOULD have made - and what the filters cost us.

PINGS_FILE = "pings_log.json"
SCORECARD_HOUR = 21           # local time for the daily scorecard ping
OUTCOME_CHECK_MINUTES = 10
PAPER_STAKE_GBP = 50
SHADOW_REPEAT_HOURS = 6
STRATEGIES = {
    "rules": {"stop": 0.70, "half_at": 2.0, "trail": 0.40, "label": "Stop rules (half at 2x, stop -30%)"},
    "wide": {"stop": 0.50, "half_at": 2.0, "trail": 0.40, "label": "Wider stop (-50%)"},
    "quick": {"stop": 0.70, "all_at": 2.0, "label": "Sell everything at 2x"},
    # 1 Oct: all 7 FAST LOTTO coins ended as rugs (0.03-0.08x); several peaked at 1.5-1.7x before dumping
    "quick15": {"stop": 0.70, "all_at": 1.5, "label": "Sell everything at 1.5x"},
    "lotto": {"stop": 0.0, "half_at": 2.0, "trail": 0.50, "label": "Lotto (no stop, half at 2x, trail 50%)"},
    "moonbag": {"stop": 0.0, "half_at": 2.0, "trail": 0.50, "bag": 0.15,
                "label": "Moonbag (lotto, but keep 15% forever)"},
}
PINGS_LOCK = threading.Lock()
SHADOW_SEEN = {}


def _load_pings():
    try:
        with open(os.path.join(HERE, PINGS_FILE)) as f:
            return json.load(f)
    except Exception:
        return []


def _save_pings(pings):
    try:
        with open(os.path.join(HERE, PINGS_FILE), "w") as f:
            json.dump(pings[-3000:], f)
    except Exception as e:
        log(f"could not save pings log: {e}")


def _new_sim():
    return {k: {"open": True, "frac": 1.0, "realised": 0.0, "peak": 1.0, "half": False, "stopped": False}
            for k in STRATEGIES}


def _sim_step(st, x, cfg):
    """Advance one paper trade (1.0 = the stake) by one price sample x (= price / entry price)."""
    if not st["open"] or x <= 0:
        return
    st["peak"] = max(st["peak"], x)
    if "all_at" in cfg:
        if x >= cfg["all_at"]:
            st["realised"] += st["frac"] * cfg["all_at"]
            st.update(frac=0.0, open=False, half=True)
        elif x <= cfg["stop"]:
            st["realised"] += st["frac"] * x      # sell at the price we actually see (gaps included)
            st.update(frac=0.0, open=False, stopped=True)
        return
    if not st["half"]:
        if x >= cfg["half_at"]:
            st["realised"] += 0.5 * cfg["half_at"]
            st.update(frac=0.5, half=True)
        elif x <= cfg["stop"]:
            st["realised"] += x
            st.update(frac=0.0, open=False, stopped=True)
            return
    if st["half"] and not st.get("trailed") and x <= st["peak"] * (1 - cfg["trail"]):
        bag = min(cfg.get("bag", 0.0), st["frac"])          # moonbag: a slice that is never sold on the trail
        st["realised"] += (st["frac"] - bag) * x
        st.update(frac=bag, trailed=True, open=bag > 0)


def _sim_value(st, last_x):
    return st["realised"] + st["frac"] * (last_x or 0)


def log_candidate(kind, m, pinged=True, reasons=None, flags=None):
    now = time.time()
    if not pinged:
        if now - SHADOW_SEEN.get(m["addr"], 0) < SHADOW_REPEAT_HOURS * 3600:
            return
        SHADOW_SEEN[m["addr"]] = now
    if not m.get("price"):
        return
    f = {"m5": round(m["chg_m5"], 1), "h1": round(m["chg_h1"], 1),
         "bs": round(m["buys_h1"] / max(m["sells_h1"], 1), 2),
         "liq_mc": round(m["liq"] / m["mcap"], 3) if m["mcap"] else 0, "age_h": round(m["age_h"], 2),
         "curve": on_bonding_curve(m), "social": m["has_social"], "hour": datetime.now().hour, "dex": m["dex"]}
    f.update(flags or {})
    entry = {"t": now, "kind": kind, "pinged": pinged, "reasons": reasons or [], "symbol": m["symbol"],
             "name": m["name"], "addr": m["addr"], "chain": m["chain"], "mcap": m["mcap"], "price": m["price"],
             "url": m["url"], "f": f, "peak_x": 1.0, "trough_x": 1.0, "last_x": 1.0,
             "x_1h": None, "x_6h": None, "x_24h": None, "sim": _new_sim(), "done": False}
    with PINGS_LOCK:
        pings = _load_pings()
        pings.append(entry)
        _save_pings(pings)


def log_ping(kind, m, flags=None):
    log_candidate(kind, m, True, None, flags)


def update_prices(prices):
    """prices: {addr: (price_usd, liquidity_usd)}. Advances every open (<24h) logged coin's paper trades."""
    now = time.time()
    new_sleepers = []
    with PINGS_LOCK:
        pings = _load_pings()
        changed = False
        for p in pings:
            if p.get("done") or not p.get("price"):
                continue
            age = now - p["t"]
            if p["addr"] in prices and prices[p["addr"]][0]:
                price, liq = prices[p["addr"]]
                x = price / p["price"]
                if liq < 1000:            # pool drained / rugged: you can't really sell
                    x = min(x, 0.05)
                p["last_x"] = round(x, 4)
                p["peak_x"] = max(p.get("peak_x") or 1.0, x)
                p["trough_x"] = min(p.get("trough_x") or 1.0, x)
                sim = p.setdefault("sim", _new_sim())
                for k, cfg in STRATEGIES.items():
                    if k in sim:
                        _sim_step(sim[k], x, cfg)
                if (p["peak_x"] >= AUTO_SLEEPER_MIN_X and p.get("chain") == "solana" and not p.get("sleeper")
                        and ((p.get("f") or {}).get("story") or (p.get("f") or {}).get("keyword"))):
                    p["sleeper"] = True
                    new_sleepers.append((p["addr"], f"${p['symbol']} auto: {p['kind']} did {p['peak_x']:.1f}x "
                                                    f"({datetime.now():%d %b})"))
                for key, secs in (("x_1h", 3600), ("x_6h", 6 * 3600), ("x_24h", 24 * 3600)):
                    if p.get(key) is None and age >= secs:
                        p[key] = round(x, 3)
                changed = True
            if age >= 24 * 3600 and (p["addr"] in prices or age >= 26 * 3600):
                for st in (p.get("sim") or {}).values():
                    if st["open"]:
                        st["realised"] += st["frac"] * (p.get("last_x") or 0)
                        st.update(frac=0.0, open=False)
                p["done"] = True
                changed = True
        if changed:
            _save_pings(pings)
    add_auto_sleepers(new_sleepers)


def _prices_from_pairs(pairs):
    return {a: (float(pr.get("priceUsd") or 0), float((pr.get("liquidity") or {}).get("usd") or 0))
            for a, pr in pairs.items()}


def outcomes_update():
    with PINGS_LOCK:
        pings = _load_pings()
    now = time.time()
    by_chain = {}
    for p in pings:
        if not p.get("done") and p.get("price") and now - p["t"] <= 26 * 3600:
            by_chain.setdefault(p["chain"], set()).add(p["addr"])
    prices = {}
    for chain, addrs in by_chain.items():
        prices.update(_prices_from_pairs(dex_pairs_for_tokens(chain, list(addrs))))
    update_prices(prices)


def _pnl(p, k):
    st = (p.get("sim") or {}).get(k)
    return None if not st else (_sim_value(st, p.get("last_x", 1.0)) - 1) * PAPER_STAKE_GBP


def _gbp(v):
    return f"{'+' if v >= 0 else '-'}GBP{abs(v):.0f}"


def scorecard_text(hours=24):
    allp = [p for p in _load_pings() if time.time() - p["t"] <= hours * 3600]
    pings = [p for p in allp if p.get("pinged", True)]
    shadow = [p for p in allp if not p.get("pinged", True)]
    if not allp:
        return "No pings in the last 24h."
    hit2 = sum((p.get("peak_x") or 0) >= 2 for p in pings)
    hit5 = sum((p.get("peak_x") or 0) >= 5 for p in pings)
    stopped = sum(bool(((p.get("sim") or {}).get("rules") or {}).get("stopped")) for p in pings)
    L = [f"PINGS {len(pings)} | peaked 2x+: {hit2} | 5x+: {hit5} | stopped out (-30%) before 2x: {stopped}",
         f"PAPER TRADING (GBP{PAPER_STAKE_GBP} on every ping, approx.):"]
    for k, cfg in STRATEGIES.items():
        vals = [v for v in (_pnl(p, k) for p in pings) if v is not None]
        if vals:
            L.append(f"- {cfg['label']}: {_gbp(sum(vals))} | {sum(v > 0 for v in vals)}/{len(vals)} in profit")
    kinds = {}
    for p in pings:
        k = kinds.setdefault(p["kind"], [0, 0, 0.0])
        k[0] += 1
        k[1] += (p.get("peak_x") or 0) >= 2
        k[2] += _pnl(p, "rules") or 0
    L.append("By type (pings / hit 2x / rules result): " +
             "; ".join(f"{k} {v[0]}/{v[1]}/{_gbp(v[2])}" for k, v in kinds.items()))
    if shadow:
        s2 = [p for p in shadow if (p.get("peak_x") or 0) >= 2]
        blocked = {}
        for p in s2:
            for r in p.get("reasons") or ["?"]:
                blocked[r] = blocked.get(r, 0) + 1
        L.append(f"FILTERED OUT: {len(shadow)} near-misses | {len(s2)} later hit 2x | if they'd pinged, "
                 f"rules would have made {_gbp(sum(_pnl(p, 'rules') or 0 for p in shadow))}")
        if blocked:
            L.append("Filters that blocked the 2x ones: " +
                     ", ".join(f"{r} x{n}" for r, n in sorted(blocked.items(), key=lambda kv: -kv[1])[:5]))
    judged = [p for p in allp if (p.get("f") or {}).get("ai")]
    if judged:
        for v in ("PING", "SKIP"):
            grp = [p for p in judged if p["f"]["ai"] == v]
            if grp:
                L.append(f"AI {'passed' if v == 'PING' else 'skipped'}: {len(grp)} | hit 2x: "
                         f"{sum((p.get('peak_x') or 0) >= 2 for p in grp)} | lotto result "
                         f"{_gbp(sum(_pnl(p, 'lotto') or 0 for p in grp))}")
        u = _ai_usage()
        L.append(f"AI cost today: {u['calls']} checks ({u.get('deep', 0)} web cross-checks), ${u['usd']:.2f}")
    def grp_line(label, grp):
        return (f"{label} {len(grp)} (2x: {sum((p.get('peak_x') or 0) >= 2 for p in grp)}, "
                f"5x: {sum((p.get('peak_x') or 0) >= 5 for p in grp)}, lotto {_gbp(sum(_pnl(p, 'lotto') or 0 for p in grp))})")
    tests = []
    for kind in ("SECOND LOOK", "EARLY STORY", "FAST EARLY"):
        grp = [p for p in shadow if p["kind"] == kind and "paper only" in (p.get("reasons") or [])]
        if grp:
            tests.append(grp_line(kind, grp))
    fl = [p for p in pings if p["kind"] == "FAST LOTTO" and "avg_trade" in (p.get("f") or {})]
    if fl:
        cut = [p for p in fl if p["f"]["avg_trade"] < FAST_TEST_MIN_AVG_TRADE]
        tests.append(grp_line(f"FAST LOTTO avg trade <${FAST_TEST_MIN_AVG_TRADE} would skip", cut))
        tests.append(grp_line("2nd RugCheck would skip", [p for p in fl if p["f"].get("rc2_ok") is False]))
        for tier in ("REAL BUYERS", "QUICK FLIP"):
            grp = [p for p in fl if p["f"].get("tier") == tier]
            if grp:
                tests.append(grp_line(f"label {tier}", grp))
    if tests:
        L.append("TESTS (paper only, no pings): " + "; ".join(tests))
    best = sorted(pings, key=lambda p: -(p.get("peak_x") or 0))[:3]
    if best:
        L.append("Best: " + "; ".join(f"{p['kind']} ${p['symbol']} peak {p.get('peak_x', 1):.1f}x "
                                      f"(rules {_gbp(_pnl(p, 'rules') or 0)})" for p in best))
    L.append("Send this to Claude to tune. (Sampled every ~45s for 3h, then every 10 min.)")
    return "\n".join(L)


def export_csv():
    import csv
    path = os.path.join(HERE, "pings_export.csv")
    rows = _load_pings()
    fkeys = sorted({k for r in rows for k in (r.get("f") or {})})
    cols = ["time", "kind", "pinged", "symbol", "addr", "chain", "mcap", "peak_x", "trough_x", "x_1h", "x_6h",
            "x_24h", "reasons"] + [f"f_{k}" for k in fkeys] + [f"pnl_{k}" for k in STRATEGIES]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([datetime.fromtimestamp(r["t"]).isoformat(timespec="minutes"), r.get("kind"),
                        r.get("pinged", True), r.get("symbol"), r.get("addr"), r.get("chain"), round(r.get("mcap") or 0),
                        r.get("peak_x"), r.get("trough_x"), r.get("x_1h"), r.get("x_6h"), r.get("x_24h"),
                        "|".join(r.get("reasons") or [])] + [(r.get("f") or {}).get(k) for k in fkeys] +
                       [round(_pnl(r, k) or 0, 2) for k in STRATEGIES])
    return path


async def scorecard_loop(state):
    loop = asyncio.get_running_loop()
    last_day = None
    while True:
        try:
            await loop.run_in_executor(None, outcomes_update)
            now = datetime.now()
            if now.hour == SCORECARD_HOUR and last_day != now.date():
                last_day = now.date()
                send_ntfy("Radar daily scorecard", scorecard_text(), priority="default", tags="bar_chart")
        except Exception as e:
            log(f"scorecard error: {e}")
        await asyncio.sleep(OUTCOME_CHECK_MINUTES * 60)


# ----------------------------- POSITION EXIT ALERTS -----------------------------
# Exits are where money is made or lost (HOLDOWEEN, GOCARDS, CROOK). List what you hold in positions.txt as
# "<contract address> <average cost in $>" (Coinbase shows "Average cost"). Checked every 30s:
# 2x -> sell half | 3x -> sell another quarter | trail 50% off the peak after 2x | optional hard stop | 5-min dump.
POSITIONS_FILE = "positions.txt"
POS_STATE_FILE = "positions_state.json"
POS_POLL_SECONDS = 30
# 30 Sep ping review (all 29 pings, GBP50 each): BOTH exit styles lost - stop -30%: -GBP159, lotto (no stop, half at
# 2x, trail 50%): -GBP104. Lotto lost less because winners (CROOK 7x, terrafying 3.3x) fell 76-82% first and the stop
# sold them at the bottom. Most of those 29 were price-only pings that are now silent (narrative mode).
# So by default there is no stop: the SIZE is your stop (only put in what you can lose completely).
POS_HARD_STOP = None         # e.g. 0.70 to bring back a -30% stop alert
POS_TRAIL = 0.50             # after selling half at 2x, sell the rest when it's this far off its peak


def load_positions():
    out = []
    try:
        with open(os.path.join(HERE, POSITIONS_FILE), encoding="utf-8") as f:
            for ln in f:
                parts = ln.split("#")[0].split()
                if len(parts) >= 2:
                    try:
                        out.append((parts[0], float(parts[1].replace("$", "").replace(",", ""))))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return out


async def positions_loop(state):
    loop = asyncio.get_running_loop()
    try:
        with open(os.path.join(HERE, POS_STATE_FILE)) as f:
            pst = json.load(f)
    except Exception:
        pst = {}
    while True:
        try:
            pos = load_positions()
            if pos:
                by_chain = {}
                for addr, _ in pos:
                    by_chain.setdefault("base" if addr.lower().startswith("0x") else "solana", []).append(addr)
                found = {}
                for c, addrs in by_chain.items():
                    got = await loop.run_in_executor(None, dex_pairs_for_tokens, c, addrs)
                    found.update({k.lower(): v for k, v in got.items()})
                changed = False
                for addr, cost in pos:
                    pr = found.get(addr.lower())
                    if not pr or cost <= 0:
                        continue
                    m = metrics(pr)
                    x = m["price"] / cost
                    s = pst.setdefault(f"{addr}|{cost}", {"peak": x, "done": [], "dump_t": 0})
                    s["peak"] = max(s["peak"], x)
                    sym, links = m["symbol"], check_links(m)

                    def fire(ev, title, body):
                        nonlocal changed
                        if ev not in s["done"]:
                            s["done"].append(ev)
                            changed = True
                            send_ntfy(title, body, click=coinbase_url(m), priority="urgent",
                                      tags="rotating_light,moneybag", actions=links)

                    status = f"${sym} is {x:.2f}x your cost | MCap {fmt_usd(m['mcap'])} | 5m {m['chg_m5']:+.0f}%"
                    if x >= 2:
                        fire("2x", f"TAKE PROFIT: ${sym} hit 2x - sell HALF now",
                             status + f"\nSell half now = your whole stake back. The rest rides until it's {POS_TRAIL:.0%} off its peak.")
                    if x >= 3:
                        fire("3x", f"${sym} hit 3x - sell another quarter", status)
                    if POS_HARD_STOP and x <= POS_HARD_STOP and "2x" not in s["done"]:
                        fire("stop", f"STOP: ${sym} is -{(1 - x) * 100:.0f}% - sell now",
                             status + "\nHard stop reached. Sell and move on - no hoping.")
                    if "2x" in s["done"] and x <= s["peak"] * (1 - POS_TRAIL):
                        fire("trail", f"TRAILING STOP: ${sym} is {POS_TRAIL:.0%} off its peak - sell the rest",
                             status + "\nOptional: keep a small moonbag (10-15%) in case it's a 100x story coin.")
                    if dumping_now(m) and time.time() - s.get("dump_t", 0) > 600:
                        s["dump_t"] = time.time()
                        changed = True
                        send_ntfy(f"DUMP WARNING: ${sym} - sells 2x buys in the last 5 min",
                                  status + "\nInsiders may be exiting. Consider selling now.",
                                  click=coinbase_url(m), priority="urgent", tags="warning", actions=links)
                if changed:
                    with open(os.path.join(HERE, POS_STATE_FILE), "w") as f:
                        json.dump(pst, f)
        except Exception as e:
            log(f"positions loop error: {e}")
        await asyncio.sleep(POS_POLL_SECONDS)


# ----------------------------- IGNITION (catch the START of a move) -----------------------------
# 30 Sep screenshots: SGI pinged at $151K after +196% in the hour (Coinbase showed +4.4K%), and HERO/SARKA/HERZOGIAN
# pinged after their runs. Every trigger above waits for a move to be FINISHED (+50% 1h). This loop keeps a fast
# watch on hundreds of young coins, takes its own price snapshot every ~20s, and pings when 5-min volume and buyers
# suddenly surge while price is pushing to a NEW HIGH - usually the first 10-30% of a move, not the last.
# Coins get on the watch from: every new pump.fun launch (PumpPortal), every migration, GeckoTerminal's
# "trending in the last 5 minutes" and newest pools, and DexScreener's feeds.
IGNITION_POLL_SECONDS = 20
IGN_MIN_MCAP = 15_000
IGN_MAX_MCAP = 1_500_000
IGN_MIN_AGE_MINUTES = 8         # past the launch-sniper dump
IGN_MIN_VOL_M5 = 6_000          # $ traded in the last 5 min
IGN_MIN_BUYS_M5 = 25            # a crowd, not 3 wallets
IGN_BUY_RATIO = 1.8             # buys vs sells in the last 5 min
IGN_VOL_SURGE = 3.0             # last 5 min's volume vs the coin's own average 5 min earlier in the hour
IGN_MIN_M5 = 8                  # the move has started ...
IGN_MAX_M5 = 80                 # ... but isn't already a finished vertical candle
IGN_MIN_H1 = -5                 # below this it's a dead-cat bounce inside a dump (HERO, SARKA)
IGN_MAX_H1 = 150                # ran more than this already -> late; SECOND LEG handles it
IGN_MIN_LIQ = 5_000             # (not checked on the bonding curve)
IGN_NEW_HIGH = 0.95             # price must be within 5% of the highest we've seen in 30 min (breakout, not bounce)
IGN_HOT_MINUTES = 90            # how long a coin stays on the fast watch
IGN_MAX_HOT = 450
GECKO_FAST_SECONDS = 150         # with the 8s gap: ~2 GeckoTerminal calls/min in total, comfortably under the limit
NURSERY_MINUTES = 60            # brand-new pump.fun launches, swept once a minute
NURSERY_MAX = 1500
NURSERY_PROMOTE_MCAP = 12_000   # a launch that gets past this goes onto the fast watch

STORY_LAUNCH_VIP_HOURS = 2     # a new coin named after something a VIP posted in the last 2h
STORY_LAUNCH_CHECKS_MIN = (3, 8, 15, 30)
STORY_LAUNCH_MIN_MCAP = 15_000
STORY_LAUNCH_MIN_BUYS_H1 = 40
STORY_LAUNCH_MAX_WATCH = 200    # was 60 and filled up with $SI copies overnight (1 Oct) - see the create handler
STORY_LAUNCHES = {}            # mint -> (time, phrase, source)

HOT = {}        # addr -> (added_time, chain)
NURSERY = {}    # mint -> added_time (solana)
SNAPS = {}      # addr -> [(time, price)]


def hot_add(addr, chain):
    if chain in CHAINS and addr and addr not in HOT:
        HOT[addr] = (time.time(), chain)


def nursery_add(mint):
    NURSERY[mint] = time.time()
    if len(NURSERY) > NURSERY_MAX:
        for a in sorted(NURSERY, key=NURSERY.get)[: len(NURSERY) - NURSERY_MAX]:
            NURSERY.pop(a, None)


def _launch_matches(phrase, lite):
    """Stricter than coin_matches for brand-new coins: 1-2 letter tickers ($SB, $7) match too many phrases by
    acronym alone, so an acronym only counts if it's 3+ letters."""
    if not coin_matches(phrase, lite):
        return False
    sym = lite["symbol"].replace("$", "").lower()
    p = phrase.replace(" ", "").lower()
    return len(sym) >= 3 or p in lite["name"].replace(" ", "").lower() or p == sym and len(p) >= 3


def story_launch_match(name, symbol):
    """Does a brand-new pump.fun coin's name/ticker match something a VIP just posted, or a live trending phrase?"""
    lite = {"name": name or "", "symbol": symbol or ""}
    now = time.time()
    for t, acct, text in reversed(RECENT_VIP):
        if now - t <= STORY_LAUNCH_VIP_HOURS * 3600 and acct not in X_FEED_ACCOUNTS:
            routine = VIP_ROUTINE_WORDS.get(acct, set()) | COMMON_WORDS
            norm = lambda w: w[:-1] if w.endswith("s") and len(w) > 4 else w
            for ph in vip_phrases(text):
                if {norm(w) for w in ph.split()} <= routine:
                    continue          # "three falcons" from a routine SpaceX post isn't a meme
                if len(ph) >= 4 and _launch_matches(ph, lite):
                    return ph, f"@{acct} post {(now - t) / 60:.0f} min ago"
    if AUTO is not None:
        for kw in AUTO.active(200):
            if len(kw) >= 5 and _launch_matches(kw, lite) and story_backing({"story": kw})[2]:
                return kw, "trending phrase"
    return None


async def story_launch_watch(state, mint, phrase, src):
    """Don't ping at creation (snipers dump ~85% in 5 min). Check at 3/8/15/30 min and ping if it's getting real
    buyers AND it's the leading coin among the launches for this phrase (copies appear within seconds)."""
    loop = asyncio.get_running_loop()
    hot_add(mint, "solana")
    start = time.time()
    for mins in STORY_LAUNCH_CHECKS_MIN:
        await asyncio.sleep(max(0, start + mins * 60 - time.time()))
        rivals = [a for a, (_, ph, _) in STORY_LAUNCHES.items() if ph == phrase]
        pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", rivals)
        me = pairs.get(mint)
        if not me:
            continue
        m = metrics(me)
        leader = max(pairs.values(), key=lambda p: float((p.get("volume") or {}).get("h1") or 0))
        if (m["mcap"] >= STORY_LAUNCH_MIN_MCAP and m["buys_h1"] >= STORY_LAUNCH_MIN_BUYS_H1
                and m["buys_h1"] >= m["sells_h1"] and not dumping_now(m) and m["chg_m5"] >= -10
                and (leader.get("baseToken") or {}).get("address") == mint):
            await loop.run_in_executor(None, lambda: alert(
                state, "STORY LAUNCH", m,
                f'NEW COIN NAMED AFTER "{phrase}" ({src}), {mins} min old and leading {len(rivals)} copies '
                f'on volume. Earliest possible entry on a story coin - also the riskiest. Lottery size.',
                skip_dedupe=False, flags={"story": phrase, "launch_src": src}))
            return
    STORY_LAUNCHES.pop(mint, None)


def vol_surge(m):
    """Last 5 min's volume vs the average 5 min over the rest of the hour."""
    rest = max(m["vol_h1"] - m["vol_m5"], 0) / 11
    return m["vol_m5"] / max(rest, 1)


def ignition_reasons(m, snaps):
    """Why a coin is NOT igniting right now (empty list = ping). snaps = our own [(t, price)] history."""
    r = []
    if m["chain"] not in CHAINS:
        r.append("chain")
    if not IGN_MIN_MCAP <= m["mcap"] <= IGN_MAX_MCAP:
        r.append("mcap range")
    if m["age_h"] * 60 < IGN_MIN_AGE_MINUTES:
        r.append("too new")
    if m["age_h"] > RUNNER_MAX_AGE_HOURS:
        r.append("too old")
    if m["chg_m5"] < IGN_MIN_M5:
        r.append("not moving")
    if m["chg_m5"] > IGN_MAX_M5:
        r.append("candle already vertical")
    if m["chg_h1"] < IGN_MIN_H1:
        r.append("dead-cat bounce")
    if m["chg_h1"] > IGN_MAX_H1:
        r.append("already ran")
    if m["vol_m5"] < IGN_MIN_VOL_M5:
        r.append("low 5m volume")
    if vol_surge(m) < IGN_VOL_SURGE:
        r.append("no volume surge")
    if m["buys_m5"] < IGN_MIN_BUYS_M5:
        r.append("few 5m buys")
    if m["buys_m5"] < IGN_BUY_RATIO * max(m["sells_m5"], 1):
        r.append("sellers keeping up")
    if not on_bonding_curve(m) and m["liq"] < IGN_MIN_LIQ:
        r.append("low liquidity")
    if dumping_now(m):
        r.append("5m dump")
    prev = [p for t, p in snaps if p > 0]
    if not prev:
        r.append("no history yet")     # wait one more snapshot (~20s) so a single spiky print can't ping
    else:
        if m["price"] < prev[-1]:
            r.append("reversing")      # still igniting must mean still going up right now
        if m["price"] < IGN_NEW_HIGH * max(prev):
            r.append("below recent high")
    return r


def gecko_fast_tokens():
    """GeckoTerminal: pools trending over the last 5 MINUTES (Solana + Base) + Base's newest pools. Solana's new
    pump.fun coins already arrive instantly from PumpPortal, so Solana new_pools was a wasted call."""
    out = {}
    for net, path in (("solana", "trending_pools?duration=5m&page=1"), ("base", "trending_pools?duration=5m&page=1"),
                      ("base", "new_pools?page=1")):
        try:
            d = gecko_json(f"https://api.geckoterminal.com/api/v2/networks/{net}/{path}")
        except Exception as e:
            if "backing off" not in str(e):          # one line per real failure, not one per skipped call
                log(f"geckoterminal fast feed ({net}) unavailable: {e}")
            continue
        for pool in d.get("data") or []:
            tid = ((((pool.get("relationships") or {}).get("base_token") or {}).get("data")) or {}).get("id", "")
            if tid.startswith(net + "_"):
                out.setdefault(net, set()).add(tid.split("_", 1)[1])
    return out


async def nursery_loop(state):
    """Sweep every new pump.fun launch once a minute; the ones that get real money move to the fast watch."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            now = time.time()
            for a, t in list(NURSERY.items()):
                if now - t > NURSERY_MINUTES * 60:
                    NURSERY.pop(a, None)
            mints = list(NURSERY)
            if mints:
                pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, "solana", mints)
                promoted = 0
                for addr, pr in pairs.items():
                    m = metrics(pr)
                    if m["mcap"] >= NURSERY_PROMOTE_MCAP and m["vol_m5"] > 0:
                        hot_add(addr, "solana")
                        NURSERY.pop(addr, None)
                        promoted += 1
                log(f"launch sweep: {len(mints)} new launches checked, {promoted} moved to the fast watch")
        except Exception as e:
            log(f"launch sweep error: {e}")
        await asyncio.sleep(60)


async def ignition_loop(state):
    loop = asyncio.get_running_loop()
    last_gecko = 0
    last_log = 0
    while True:
        try:
            now = time.time()
            if now - last_gecko >= GECKO_FAST_SECONDS:
                last_gecko = now
                for c, addrs in (await loop.run_in_executor(None, gecko_fast_tokens)).items():
                    for a in addrs:
                        hot_add(a, c)
            for a, (t, _) in list(HOT.items()):
                if now - t > IGN_HOT_MINUTES * 60:
                    HOT.pop(a, None)
                    SNAPS.pop(a, None)
            if len(HOT) > IGN_MAX_HOT:
                for a in sorted(HOT, key=lambda k: HOT[k][0])[: len(HOT) - IGN_MAX_HOT]:
                    HOT.pop(a, None)
                    SNAPS.pop(a, None)
            by_chain = {}
            for a, (_, c) in HOT.items():
                by_chain.setdefault(c, []).append(a)
            for chain, addrs in by_chain.items():
                pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, chain, addrs)
                for addr, pr in pairs.items():
                    m = metrics(pr)
                    snaps = [(t, p) for t, p in SNAPS.get(addr, []) if now - t <= 30 * 60]
                    reasons = ignition_reasons(m, snaps)
                    SNAPS[addr] = (snaps + [(now, m["price"])])[-120:]
                    if not reasons:
                        surge = round(vol_surge(m), 1)
                        extra = (f"IGNITION: 5-min volume {surge}x its pace earlier this hour, "
                                 f"{m['buys_m5']}/{m['sells_m5']} buys/sells in 5 min, price at a new high. "
                                 f"This is the START of a move - most ignitions fizzle. Lottery size only.")
                        story = real_world_match(m)
                        if story:
                            extra = f'REAL-WORLD STORY: matches trending "{story}".\n' + extra
                        await loop.run_in_executor(None, lambda: alert(
                            state, "IGNITION", m, extra, flags={"surge": surge, "story": story or ""}))
                    elif (len(reasons) <= 2 and "no history yet" not in reasons and "mcap range" not in reasons
                          and m["chg_m5"] >= IGN_MIN_M5 and vol_surge(m) >= IGN_VOL_SURGE
                          and m["addr"] not in state.alerted):
                        # near-miss: tracked on paper so the scorecard shows which ignition filter costs us
                        await loop.run_in_executor(None, lambda: log_candidate(
                            "IGNITION", m, False, reasons, {"surge": round(vol_surge(m), 1)}))
            if now - last_log >= 300:
                last_log = now
                log(f"ignition watch: {len(HOT)} coins on the 20-second watch, {len(NURSERY)} new launches queued")
        except Exception as e:
            log(f"ignition loop error: {e}")
        await asyncio.sleep(IGNITION_POLL_SECONDS)


# ----------------------------- SECOND LEG (re-acceleration) -----------------------------
# CROOK (30 Sep): pinged at 13:56, dipped ~50%, then a vertical +400% leg started ~14:08 and our next ping came
# 4-5 min late. Every pinged coin and every near-miss is re-checked every 45s for 3h; we ping the moment it
# re-accelerates: 5-min volume >= 3x its recent pace, price +15% in 5 min, buys >= 1.5x sells in 5 min.
WATCH = {}                   # addr -> (added_time, chain)
WATCH_HOURS = 6              # CROOK and terrafying fell ~80% after the ping and only ran 2-3.5h later
LEG_POLL_SECONDS = 45
LEG_MIN_M5_CHANGE = 15
LEG_VOL_MULTIPLE = 3.0
LEG_REALERT_MINUTES = 30
LEG_MIN_H1_CHANGE = 0        # SARKA's "second leg" came at -24% 1h: a bounce inside a dump, not a new leg
LEG_MIN_AGE_MINUTES = 10     # HERZOGIAN's came at pool age 0.0h: the migration spike itself


async def second_leg_loop(state):
    loop = asyncio.get_running_loop()
    last_leg = {}
    while True:
        try:
            now = time.time()
            for p in _load_pings():                       # everything we've pinged recently
                if now - p["t"] <= WATCH_HOURS * 3600:
                    WATCH.setdefault(p["addr"], (p["t"], p["chain"]))
            for a, (t, _) in list(WATCH.items()):
                if now - t > WATCH_HOURS * 3600:
                    WATCH.pop(a, None)
            by_chain = {}
            for a, (_, c) in WATCH.items():
                by_chain.setdefault(c, []).append(a)
            price_updates = {}
            for chain, addrs in by_chain.items():
                pairs = await loop.run_in_executor(None, dex_pairs_for_tokens, chain, addrs)
                price_updates.update(_prices_from_pairs(pairs))
                for addr, pr in pairs.items():
                    m = metrics(pr)
                    pace_5m = m["vol_h1"] / 12 if m["vol_h1"] else 0
                    if (m["chg_m5"] >= LEG_MIN_M5_CHANGE and pace_5m > 0
                            and m["chg_h1"] >= LEG_MIN_H1_CHANGE and m["age_h"] * 60 >= LEG_MIN_AGE_MINUTES
                            and m["vol_m5"] >= LEG_VOL_MULTIPLE * pace_5m
                            and m["buys_m5"] >= 1.5 * max(m["sells_m5"], 1)
                            and m["liq"] >= RUNNER_MIN_LIQUIDITY and m["mcap"] <= KEYWORD_MAX_MCAP
                            and now - last_leg.get(addr, 0) > LEG_REALERT_MINUTES * 60):
                        last_leg[addr] = now
                        await loop.run_in_executor(None, lambda: alert(
                            state, "SECOND LEG", m,
                            f"Re-accelerating NOW: 5-min volume {m['vol_m5']/pace_5m:.1f}x its recent pace, "
                            f"{m['buys_m5']}/{m['sells_m5']} buys/sells in 5 min. Legs like this can be caller-driven "
                            f"and reverse fast - tight stop.", skip_dedupe=True))
            await loop.run_in_executor(None, update_prices, price_updates)   # paper trades, ~45s resolution
        except Exception as e:
            log(f"second-leg loop error: {e}")
        await asyncio.sleep(LEG_POLL_SECONDS)


async def main():
    state = State(os.path.join(HERE, STATE_FILE))
    log("Memecoin Radar starting - ALERTS ONLY. Phone topic: " + NTFY_TOPIC)
    if "--scorecard" in sys.argv:
        outcomes_update()
        print(scorecard_text())
        return
    if "--export" in sys.argv:
        print("wrote " + export_csv())
        return
    if "--test" in sys.argv:
        send_ntfy("Radar test", "Memecoin Radar is connected to your phone.", tags="white_check_mark")
        return
    await asyncio.gather(runners_loop(state), keywords_loop(state), graduations_loop(state),
                         smart_wallets_loop(state), sleepers_loop(state), x_vip_loop(state),
                         scorecard_loop(state), second_leg_loop(state),
                         positions_loop(state), ignition_loop(state), nursery_loop(state), truth_ca_loop(state),
                         listings_loop(state), early_story_loop(state))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped")
