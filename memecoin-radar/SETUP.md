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

Every 3 minutes it checks each phrase for a Solana or Base coin with a **matching name or ticker** (including initials, e.g. "super intelligence" matches $SI) that is starting to move on real volume. Phrases expire after 48 hours.
You don't need to touch `keywords.txt`. It's only there if you ever want to force a phrase in yourself.
- **Too many pings?** In `radar.py`, raise `RUNNER_MIN_H1_CHANGE` (for example to 100) or `RUNNER_MIN_H1_VOLUME` (for example to 50000).
- **Too few?** Lower them a bit.
- The radar caps itself at 6 alerts an hour.

## When it pings
1. Open the DexScreener link in the notification.
2. On GMGN, check that **global fees are at least 1.5 SOL** and that bundlers and snipers are low.
3. Paste the contract address (CA) into the Coinbase app search.
4. **Rules:** GBP 50–100 max, sell half at 2x, hard stop at −30%.

Most pinged coins still die. The radar gets you in early; the rules protect you when it's wrong.
