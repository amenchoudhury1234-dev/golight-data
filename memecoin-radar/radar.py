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
GECKO_GAP_SECONDS = 4          # GeckoTerminal's free API returns 429 when calls come back to back
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
        "name": (p.get("baseToken") or {}).get("name", "?"),
        "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
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
def send_ntfy(title, body, click=None, priority="high", tags="rotating_light", actions=None):
    """Publish via ntfy's JSON API so emoji / non-Latin coin names work (HTTP headers can't carry them)."""
    prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 4)
    payload = {"topic": NTFY_TOPIC, "title": title[:120], "message": body[:3900], "priority": prio,
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


def check_links(m):
    """One-tap buttons on the phone notification: GMGN (fees/bundlers/insiders), chart, RugCheck."""
    net = "sol" if m["chain"] == "solana" else m["chain"]
    acts = [{"label": "GMGN", "url": f"https://gmgn.ai/{net}/token/{m['addr']}"}]
    if m["url"]:
        acts.append({"label": "Chart", "url": m["url"]})
    if m["chain"] == "solana":
        acts.append({"label": "RugCheck", "url": f"https://rugcheck.xyz/tokens/{m['addr']}"})
    return acts


COPYCAT_MIN_MCAP = 500_000
COPYCAT_MIN_VOL_H1 = 100_000


def story_coin_symbols():
    """{TICKER: real contract} from sleepers.txt comments like '# Super Inu $SI - ...'."""
    out = {}
    try:
        with open(os.path.join(HERE, SLEEPER_FILE), encoding="utf-8") as f:
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
                    out[sym] = ca
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
AI_MAX_CALLS_PER_DAY = 80          # hard guard on spend (~2-3p per call)
AI_USAGE_FILE = "ai_usage.json"
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


def _ai_usage(add_in=0, add_out=0):
    """Track calls and $ per day in ai_usage.json. Returns today's record."""
    path, day = os.path.join(HERE, AI_USAGE_FILE), datetime.now().strftime("%Y-%m-%d")
    try:
        with open(path) as f:
            u = json.load(f)
    except Exception:
        u = {}
    rec = u.setdefault(day, {"calls": 0, "usd": 0.0})
    if add_in or add_out:
        rec["calls"] += 1
        rec["usd"] = round(rec["usd"] + (add_in * AI_PRICE_IN + add_out * AI_PRICE_OUT) / 1e6, 4)
        try:
            with open(path, "w") as f:
                json.dump(u, f)
        except Exception:
            pass
    return rec


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


def ai_judge(kind, m, extra, rc_notes, flags):
    """Returns dict(verdict, confidence, reason, main_risk) or None if the judge is off/unavailable."""
    client = _ai_client()
    if client is None:
        return None
    if _ai_usage()["calls"] >= AI_MAX_CALLS_PER_DAY:
        log("AI judge: daily cap reached - pinging without it")
        return None
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
    return out


def alert(state, kind, m, extra="", skip_dedupe=False, flags=None):
    real = story_coin_symbols().get(m["symbol"].lstrip("$").upper())
    if real and real != m["addr"]:
        # 30 Sep: the $24K "$SI" copy died (0.16x) but the $1.01M one did 7.2x - big copies have real traction
        if m["mcap"] < COPYCAT_MIN_MCAP or m["vol_h1"] < COPYCAT_MIN_VOL_H1:
            log(f"skipped copycat ${m['symbol']} ({m['addr'][:6]}...) - the real story coin is {real[:6]}...")
            return
        extra = (f"COPYCAT WARNING: not the original ${m['symbol']} ({real[:6]}...), but it has real money "
                 f"behind it. Double-check the CA.\n" + extra)
    if NARRATIVE_MODE and kind in MOMENTUM_KINDS and not (flags or {}).get("story"):
        log_candidate(kind, m, False, ["silent (momentum only)"], flags)   # paper-traded, no phone ping
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
    verdict = ai_judge("ACT NOW" if strong else kind, m, extra, rc_notes, flags)
    flags = dict(flags or {})
    if verdict:
        flags.update(ai=verdict["verdict"], ai_conf=verdict["confidence"])
        if verdict["verdict"] == "SKIP":
            state.alerted[m["addr"]] = m["mcap"]      # don't re-judge it until it doubles
            state.save()
            log_candidate(kind, m, False, [f"AI skip: {verdict['reason'][:80]}"], flags)
            return
        extra = (f"AI CHECK: worth a look ({verdict['confidence']}/5) - {verdict['reason']} "
                 f"Main risk: {verdict['main_risk']}\n" + extra)
    if strong:
        title = f"ACT NOW (10-min window): ${m['symbol']} {fmt_usd(m['mcap'])} {m['chg_h1']:+.0f}% 1h"
        extra = ("STRONG SETUP: buyers heavily outnumber sellers on big volume. If GMGN checks pass, "
                 "enter GBP20-50 now; don't wait for it to 'confirm'.\n" + extra)
    else:
        title = f"{kind}: ${m['symbol']} {fmt_usd(m['mcap'])} ({m['chg_h1']:+.0f}% 1h)"
    if verdict:
        title = "AI OK " + title
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
    if send_ntfy(title, body, click=m["url"] or None, priority="urgent" if strong else "high",
                 tags="rotating_light,moneybag" if strong else "rotating_light", actions=check_links(m)):
        state.record(m["addr"], m["mcap"])
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


async def graduations_loop(state):
    try:
        import websockets  # noqa
    except ImportError:
        log("GRADUATIONS off: run  pip install websockets  to enable pump.fun migration alerts")
        return
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
                    else:                                  # migration off the bonding curve
                        hot_add(mint, "solana")
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
X_ACCOUNTS = [
    "elonmusk",          # biggest single memecoin catalyst (DOGE, JIMOTHY raccoon post)
    "realDonaldTrump",   # mostly on Truth Social (read free); X covers the rest
    "cz_binance",        # his dog/phrases spawn BSC & Solana coins; posts a few times a day (cheap)
    "toly",              # Toly (Anatoly Yakovenko), Solana co-founder - his memes move Solana coins
    "a1lon9",            # Alon, pump.fun co-founder
    "VladTenev",         # Robinhood CEO - following Super Inu's account was an early $SI signal
]
# Optional (busy news accounts, each roughly +$10-15/month): "WhaleInsider", "WatcherGuru", "blknoiz06"
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
                RECENT_VIP.append((time.time(), name, text))
                del RECENT_VIP[:-30]
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
    "lotto": {"stop": 0.0, "half_at": 2.0, "trail": 0.50, "label": "Lotto (no stop, half at 2x, trail 50%)"},
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
    if st["half"] and x <= st["peak"] * (1 - cfg["trail"]):
        st["realised"] += st["frac"] * x
        st.update(frac=0.0, open=False)


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
        L.append(f"AI cost today: {u['calls']} checks, ${u['usd']:.2f}")
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
# 30 Sep ping review (9 coins): a -30% stop lost on 8 of 9 - even the winners (CROOK 7x, SI 7.2x, terrafying 3.3x)
# fell 20-82% first. "Lotto" exits (no stop, half at 2x, trail 50%) were +GBP79 vs -GBP52 for the stop rules.
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
                            send_ntfy(title, body, click=m["url"] or None, priority="urgent",
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
                        fire("trail", f"TRAILING STOP: ${sym} is {POS_TRAIL:.0%} off its peak - sell the rest", status)
                    if dumping_now(m) and time.time() - s.get("dump_t", 0) > 600:
                        s["dump_t"] = time.time()
                        changed = True
                        send_ntfy(f"DUMP WARNING: ${sym} - sells 2x buys in the last 5 min",
                                  status + "\nInsiders may be exiting. Consider selling now.",
                                  click=m["url"] or None, priority="urgent", tags="warning", actions=links)
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
GECKO_FAST_SECONDS = 90
NURSERY_MINUTES = 60            # brand-new pump.fun launches, swept once a minute
NURSERY_MAX = 1500
NURSERY_PROMOTE_MCAP = 12_000   # a launch that gets past this goes onto the fast watch

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
    """GeckoTerminal: pools trending over the last 5 MINUTES + the newest pools (Solana + Base)."""
    out = {}
    for net in ("solana", "base"):
        for path in ("trending_pools?duration=5m&page=1", "new_pools?page=1"):
            try:
                d = gecko_json(f"https://api.geckoterminal.com/api/v2/networks/{net}/{path}")
            except Exception as e:
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
                         positions_loop(state), ignition_loop(state), nursery_loop(state))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped")
