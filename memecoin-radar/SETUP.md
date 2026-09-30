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
3. Restart the radar. It will say "X watch on for: @elonmusk, @realDonaldTrump, @cz_binance, ...".

What you'll then get, usually within about 60 seconds of a post:
- **"@elonmusk POSTED A CONTRACT ADDRESS"** (urgent): he named a coin directly.
- **"@elonmusk just posted – matching coins"**: coins that match his words or emojis (e.g. 🦝 → raccoon → JIMOTHY). This can arrive **before** the coin moves.
- The phrases are also added to the keyword engine, so you get a **CATALYST** ping the moment a matching coin starts to move.

Want more accounts, like WhaleInsider or WatcherGuru? Add them to `X_ACCOUNTS` in `radar.py` (about +$10–15 a month each).

## Sleeper coins (second waves)
`sleepers.txt` lists "story coins" (Jimothy, Super Inu, Tilcayo, Rizzmas). These often pump **again** when a big account reposts their story without naming the coin. JIMOTHY did +331% after Elon posted a raccoon video. The radar checks them every minute and sends a **SLEEPER WAKING** ping when trading suddenly jumps. I'll add new story coins to the file as they appear.

## Daily scorecard and paper trading (automatic)
The radar **paper-trades every ping** with your rules (£50, half out at 2x, stop at −30%) and two alternatives (a wider −50% stop, and selling everything at 2x). It tracks each coin every ~45 seconds for 3 hours, then every 10 minutes for 24 hours.
It also tracks **near-misses the filters rejected**, so we can see what the filters cost us (for example if the "5m dump" filter keeps blocking winners like CROOK).

**Every evening at 21:00** you get a scorecard showing:
- how many pings hit 2x or 5x, and how many got stopped out
- **what the rules would have made in £**, compared with the alternatives
- results by ping type (first ping, SECOND LEG, RE-ALERT, CATALYST...)
- how many filtered-out coins later hit 2x, and which filters blocked them

You can check it any time with `python radar.py --scorecard`. Run `python radar.py --export` to write `pings_export.csv` for a deeper analysis. Send both to Claude.

## Exit alerts for coins you hold (positions.txt)
When you buy something, add a line to `positions.txt`: `<contract address> <average cost in $>` (copy "Average cost" from Coinbase). The radar checks every 30 seconds and sends an **urgent** ping to: sell half at 2x, sell a quarter at 3x, **STOP at −30%**, sell the rest at 40% off the peak, plus a **DUMP WARNING** when sellers flood in. Delete the line when you've sold.

## Ping labels
- **RE-ALERT (doubled):** the coin already pinged and has since doubled. It's a late ping with higher risk; CROOK's re-alert was the one that lost.
- **SECOND LEG:** a pinged coin is re-accelerating right now. So far these have been the better entries.
- Each ping has **GMGN / Chart / RugCheck buttons**, so you can check fees, bundlers and insiders in one tap.

## When it pings
1. Open the DexScreener link in the notification.
2. On GMGN, check that **global fees are at least 1.5 SOL** and that bundlers and snipers are low.
3. Paste the contract address (CA) into the Coinbase app search.
4. **Rules:** GBP 50–100 max, sell half at 2x, hard stop at −30%.

Most pinged coins still die. The radar gets you in early; the rules protect you when it's wrong.
