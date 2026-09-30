# Memecoin Radar: laptop setup (about 10 minutes)

This program runs on your laptop and pings your phone within **1–3 minutes** when:
- **EARLY RUNNER:** a new Solana or Base coin is pumping on real volume, with more buyers than sellers (the MEMECARDS type).
- **GRADUATED & HOLDING:** a pump.fun coin has moved onto a proper exchange and is still holding 10 minutes later (unlike GOCARDS, which dumped at that point).
- **KEYWORD:** a coin matching a news phrase in `keywords.txt` starts moving (the Super Inu type).

Every Solana coin gets a RugCheck safety check before it pings you. The program **only sends alerts**. It never trades and never touches your wallet.

## 1. Install Python (one time)
- **Windows:** go to python.org/downloads, download it and install. **Tick "Add Python to PATH"** during setup.
- **Mac:** open Terminal and type `python3 --version`. If it's missing, install it from python.org.

## 2. Put the files on your laptop
Make a folder, for example `Documents\memecoin-radar`, and put `radar.py` and `keywords.txt` in it.

## 3. Install the one extra package
Open Terminal (Mac) or Command Prompt (Windows) in that folder and run:
```
pip install websockets
```
(On a Mac it may be `pip3 install websockets`.)

## 4. Test that your phone gets pings
```
python radar.py --test
```
You should get a "Radar test" notification in the ntfy app on your phone.

## 5. Start the radar
```
python radar.py
```
Leave the window open. It prints what it checks every minute, and you can stop it with Ctrl+C.
**It only works while the laptop is on and awake.** Set your laptop so it doesn't go to sleep while plugged in.

## 6. Keywords are automatic
The radar finds trending phrases **by itself** every 15 minutes, from:
- **Trump's Truth Social posts:** quoted phrases, ALL-CAPS words and capitalised names.
- **Google Trends:** what the US and UK are searching for right now.
- **Wikipedia:** pages whose views suddenly spiked, such as a new animal or a person in the news.
- **The hourly Claude "Catalyst watch":** its AI picks the memeable phrases from the news and sends them to your radar.
- **Polymarket "mention markets":** bets on the exact words Trump will say at **upcoming** speeches. The radar loads those words **before** the speech, so it's already watching matching coins when he says them.

If a new coin that's pumping also matches one of these real-world phrases, you get a **RUNNER + REAL STORY** ping. Those usually run longer than random coins.

Every 3 minutes it checks each phrase for a Solana or Base coin with a **matching name or ticker** (including initials, e.g. "super intelligence" matches $SI) that is starting to move on real volume. Phrases expire after 48 hours.
You don't need to touch `keywords.txt`. It's only there if you ever want to force a phrase in yourself.
- **Too many pings?** In `radar.py`, raise `RUNNER_MIN_H1_CHANGE` (for example to 100) or `RUNNER_MIN_H1_VOLUME` (for example to 50000).
- **Too few?** Lower them a bit.
- The radar caps itself at 6 alerts an hour.

## Live Elon and Trump X posts (optional, about $5–10 a month)
X no longer lets anyone read posts for free, but its pay-per-use API is cheap: about $0.005 per post, and Elon's and Trump's original posts come to roughly $5–10 a month.
1. Go to **console.x.com**, sign up for a developer account, create a project and an app, and add **$10 of credit**.
2. Copy the app's **Bearer Token**, then paste it into a new file called **`x_token.txt`** in the radar folder. **Keep this file private.**
3. Restart the radar. It will say "X watch on for: @elonmusk, @realDonaldTrump, @cz_binance, @toly, ...".

What you'll then get, usually within about 60 seconds of a post:
- **"@elonmusk POSTED A CONTRACT ADDRESS"** (urgent): he named a coin directly.
- **"@elonmusk just posted – matching coins"**: coins that match his words or emojis (e.g. 🦝 → raccoon → JIMOTHY). This can arrive **before** the coin moves.
- The phrases are also added to the keyword engine, so you get a **CATALYST** ping the moment a matching coin starts to move.

Want more accounts, like WhaleInsider or WatcherGuru? Add them to `X_ACCOUNTS` in `radar.py` (about +$10–15 a month each).

## Sleeper coins (second waves)
`sleepers.txt` lists "story coins" (Jimothy, Super Inu, Tilcayo, Rizzmas). These often pump **again** when a big account reposts their story without naming the coin. JIMOTHY did +331% after Elon posted a raccoon video. The radar checks them every minute and sends a **SLEEPER WAKING** ping when trading suddenly jumps. I'll add new story coins to the file as they appear.

## Daily scorecard and paper trading (automatic)
The radar **paper-trades every ping** with your rules (£50, half out at 2x, stop at −30%) and three alternatives (a wider −50% stop, selling everything at 2x, and a no-stop "lotto" hold). It tracks each coin every ~45 seconds for 3 hours, then every 10 minutes for 24 hours.
It also tracks **near-misses the filters rejected**, so we can see what the filters cost us (for example if the "5m dump" filter keeps blocking winners like CROOK).

**Every evening at 21:00** you get a scorecard showing:
- how many pings hit 2x or 5x, and how many got stopped out
- **what the rules would have made in £**, compared with the alternatives
- results by ping type (first ping, SECOND LEG, RE-ALERT, CATALYST...)
- how many filtered-out coins later hit 2x, and which filters blocked them

You can check it any time with `python radar.py --scorecard`. Run `python radar.py --export` to write `pings_export.csv` for a deeper analysis. Send both to Claude.

## Exit alerts for coins you hold (positions.txt)
When you buy something, add a line to `positions.txt`: `<contract address> <average cost in $>` (copy "Average cost" from Coinbase). The radar checks every 30 seconds and sends an **urgent** ping to: sell half at 2x, sell a quarter at 3x, and sell the rest at 50% off the peak. There is no stop by default (set `POS_HARD_STOP = 0.70` in radar.py if you want one), plus a **DUMP WARNING** when sellers flood in. Delete the line when you've sold.

## Narrative mode (on by default)
On 30 Sep, every price-only ping we could check fell 30% before reaching 2x, and most peaked within 3 minutes of the ping. So your phone now pings **only when there is a story**:
- **CATALYST:** a coin matching a phrase from Trump, Elon, the news or trends
- **RUNNER / IGNITION + REAL STORY:** a pumping coin that matches a trending phrase
- **SLEEPER WAKING:** Jimothy, Super Inu and the other story coins
- **SMART MONEY:** 2+ tracked wallets buying the same coin
- **X VIP posts:** needs `x_token.txt`

Price-only coins are still tracked silently on paper and shown in the 21:00 scorecard. To get those pings back, set `NARRATIVE_MODE = False` in `radar.py`.

## STORY LAUNCH pings (earliest entry on a story coin)
Every new pump.fun coin is checked the moment it's created. If its name or ticker matches something Elon, Trump or another watched account posted in the last 2 hours (or a live trending phrase), the radar watches it at 3, 8, 15 and 30 minutes. It pings only if the coin is getting real buyers **and** is leading the copycats on volume. This is where 10–100x entries come from ($10–50K market cap), but it's also the riskiest ping, so lottery size only.

## More sources (added 30 Sep)
- **@WhaleInsider and @WatcherGuru on X:** news accounts that often report story coins first ("Vlad Tenev follows Super Inu $SI" came from WhaleInsider hours before the big run). They only ping when they name a **$TICKER** that's a live Solana/Base coin (**NEWS MENTION**), and it still goes through the safety check and the AI check. Cost: roughly $5–10 a month each in X credit.
- **Reddit:** the hottest posts on r/all, r/aww and r/nextfuckinglevel feed the phrase list, because viral animals and clips often become coins hours later.
- **Automatic second-wave watch:** any story coin that does 3x+ is added to `auto_sleepers.txt` and watched like Jimothy for a second wave. Delete a line to stop watching it.

## Moonbag
The scorecard also tracks a "moonbag" version of the lotto plan: after the trailing sell, keep 15% forever. 100x coins usually dip 50%+ several times on the way, which shakes out every trailing stop. The moonbag is how you're still holding when one runs.

## AI check (Claude Opus 5.5)
Before a phone ping, the radar sends the coin's numbers and its story to Claude Opus 5.5, which answers **PING** or **SKIP** with a one-line reason.
- Pings that pass start with **"AI OK"** and include the reason and the main risk.
- SKIPs are silent but still paper-traded. The 21:00 scorecard compares what the AI passed with what it skipped, and shows today's cost.
- **Cost:** about 1–3p per check, capped at 80 checks a day (`AI_MAX_CALLS_PER_DAY`). With narrative mode it's usually 5–15 checks a day, about £3–7 a month.
- **Setup:** run `pip install anthropic`, then paste your API key from console.anthropic.com into `anthropic_key.txt` (private; never share or commit it). Use prepaid credit with auto-reload **off**.
- If there's no key, or the AI is down, the radar pings exactly as before.

## IGNITION pings (catching the START of a move)
Earlier pings needed +50% in the hour, so they arrived after the move (SGI at $151K, HERO/SARKA on a bounce inside a dump). The radar now also:
- watches **every new pump.fun launch**, every migration, and GeckoTerminal's "trending in the last 5 minutes"
- re-checks up to 450 young coins **every 20 seconds** and keeps its own price history
- sends an **IGNITION** ping when 5-minute volume jumps 3x+ over its earlier pace, buyers outnumber sellers about 2:1, and price is at a **new high** (not a bounce)

Coins that are down on the hour (dead-cat bounces) no longer ping as runners or second legs. IGNITION pings come earlier and fail more often, so treat them as lottery-size only. The scorecard will show whether they pay.

## Ping labels
- **RE-ALERT (doubled):** the coin already pinged and has since doubled. It's a late ping with higher risk; CROOK's re-alert was the one that lost.
- **SECOND LEG:** a pinged coin is re-accelerating right now. So far these have been the better entries.
- Each ping has **GMGN / Chart / RugCheck buttons**, so you can check fees, bundlers and insiders in one tap.

## When it pings
1. Open the DexScreener link in the notification.
2. On GMGN, check that **global fees are at least 1.5 SOL** and that bundlers and snipers are low.
3. Paste the contract address (CA) into the Coinbase app search.
4. **Rules (lotto style, from the 30 Sep review):** only £20–50 you can afford to lose completely. **No stop**: even the winners (CROOK 7x, SI 7.2x) fell 50–80% first, and a −30% stop lost on 8 of 9 coins. Sell half at 2x, then sell the rest once it's 50% off its peak.

Most pinged coins still die. The radar gets you in early; the rules protect you when it's wrong.
