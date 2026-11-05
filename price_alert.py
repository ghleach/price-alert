"""
price_alert.py - Stock price target alerts via Telegram, LINE, SMS or phone call.

SETUP
-----
pip install yfinance requests twilio

Set only the environment variables for the channels you want (others are skipped):

  Telegram (free, recommended):
    1. In Telegram, message @BotFather -> /newbot -> copy the token.
    2. Send any message to your new bot, then open
       https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}
    TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...

  LINE (Messaging API - LINE Notify closed on 31 Mar 2025):
    1. Create a channel at developers.line.biz (Messaging API).
    2. Issue a long-lived channel access token; add the official account as a friend.
    3. Your user ID is on the channel's "Basic settings" page.
    LINE_CHANNEL_TOKEN=...   LINE_USER_ID=...
    (Free plan includes a monthly quota of push messages.)

  State-file privacy (recommended if the repo is public):
    ALERT_KEY_SECRET=...  any long random string. Fired alerts are stored as an
    HMAC keyed with this, so the committed alert_state.json can't be reversed
    to recover your tickers/targets. Changing it re-arms every alert.

  SMS / phone call (Twilio, paid per message/call):
    TWILIO_SID=...  TWILIO_TOKEN=...  TWILIO_FROM=+1...  TWILIO_TO=+66...
    TWILIO_CALL=1      # optional: also phone you, not just SMS

RUN
---
  python price_alert.py            # loop forever, checking every CHECK_SECONDS
  python price_alert.py --once     # single check (use with cron / GitHub Actions)
  python price_alert.py --test     # send a test notification and exit

Each alert fires once. To re-arm it, change its target (or delete its line in
alert_state.json).
"""

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yfinance as yf

# ---------------------------------------------------------------- CONFIG ---
# direction: "above" fires when price >= target, "below" when price <= target
# On GitHub, set these in a repository variable/secret named ALERTS_JSON instead
# (same format, as JSON) so your targets aren't visible in the code.
DEFAULT_ALERTS = [
    {"ticker": "AAPL", "target": 250.0, "direction": "above"},
    {"ticker": "NVDA", "target": 150.0, "direction": "below"},
]
CHECK_SECONDS = 300                      # 5 minutes
STATE_FILE = Path(__file__).with_name("alert_state.json")
# ---------------------------------------------------------------------------


def load_alerts() -> tuple[list, str | None]:
    """Return (alerts, error). A bad ALERTS_JSON gives an error message instead of a crash."""
    raw = (os.getenv("ALERTS_JSON") or "").strip()
    if not raw:
        return DEFAULT_ALERTS, None
    try:
        alerts = json.loads(raw)
        if isinstance(alerts, dict):
            alerts = [alerts]
        if not isinstance(alerts, list):
            raise ValueError("must be a list in square brackets [...]")
        for i, a in enumerate(alerts, 1):
            if not isinstance(a, dict):
                raise ValueError(f"alert {i} must be in curly braces {{...}}")
            missing = {"ticker", "target", "direction"} - a.keys()
            if missing:
                raise ValueError(f"alert {i} is missing {', '.join(sorted(missing))}")
            a["target"] = float(a["target"])
            a["direction"] = str(a["direction"]).lower()
            if a["direction"] not in ("above", "below"):
                raise ValueError(f"alert {i} direction must be \"above\" or \"below\"")
        return alerts, None
    except Exception as e:
        return [], f"ALERTS_JSON problem: {e}"


def _num(info, *keys) -> float | None:
    for k in keys:
        try:
            v = info.get(k)
        except Exception:
            v = None
        if v is not None:
            v = float(v)
            if v == v and v > 0:            # skip NaN / zero
                return v
    return None


def get_quote(ticker: str) -> tuple[float, float, float] | None:
    """(last, day_high, day_low) from Yahoo Finance (may be delayed ~15 min).

    The day's high/low lets us catch a target that was crossed between checks,
    even if the price has since moved back.
    """
    try:
        info = yf.Ticker(ticker).fast_info
        last = _num(info, "lastPrice", "last_price")
        if last is None:
            return None
        high = _num(info, "dayHigh", "day_high") or last
        low = _num(info, "dayLow", "day_low") or last
        return last, max(high, last), min(low, last)
    except Exception as e:
        print(f"[{ticker}] price fetch failed: {e}")
        return None


# ------------------------------------------------------------- NOTIFIERS ---
# Each notifier returns True if it sent, False if its channel isn't configured,
# and raises if sending failed.
def send_telegram(msg: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": msg},
        timeout=10,
    )
    r.raise_for_status()
    return True


def send_line(msg: str) -> bool:
    token, user = os.getenv("LINE_CHANNEL_TOKEN"), os.getenv("LINE_USER_ID")
    if not (token and user):
        return False
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"to": user, "messages": [{"type": "text", "text": msg}]},
        timeout=10,
    )
    r.raise_for_status()
    return True


def send_twilio(msg: str) -> bool:
    sid, tok = os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN")
    frm, to = os.getenv("TWILIO_FROM"), os.getenv("TWILIO_TO")
    if not all([sid, tok, frm, to]):
        return False
    from twilio.rest import Client

    client = Client(sid, tok)
    client.messages.create(body=msg, from_=frm, to=to)
    if os.getenv("TWILIO_CALL") == "1":
        client.calls.create(
            twiml=f"<Response><Say>{msg}</Say><Pause length='1'/><Say>{msg}</Say></Response>",
            from_=frm,
            to=to,
        )
    return True


NOTIFIERS = [send_telegram, send_line, send_twilio]


def notify(msg: str) -> int:
    """Send to every configured channel; return how many actually delivered."""
    sent = 0
    for fn in NOTIFIERS:
        try:
            if fn(msg):
                sent += 1
        except Exception as e:
            print(f"{fn.__name__} failed: {e}")
    if not sent:
        print("WARNING: no notification was delivered (no channel configured, or all failed)")
    return sent


# ---------------------------------------------------------------- STATE ---
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def alert_key(a: dict) -> str:
    # With ALERT_KEY_SECRET set, keys are an HMAC, so the committed state file
    # can't be brute-forced back to your tickers/targets. Without it this is a
    # plain hash, which is fine locally but reversible in a public repo.
    raw = f"{a['ticker'].upper()}|{a['direction']}|{float(a['target'])}".encode()
    secret = os.getenv("ALERT_KEY_SECRET")
    if secret:
        return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()[:16]
    return hashlib.sha256(raw).hexdigest()[:16]


# ----------------------------------------------------------------- MAIN ---
def check_once(alerts: list) -> None:
    state = load_state()
    for a in alerts:
        key = alert_key(a)
        if state.get(key):          # already fired - don't spam
            continue
        quote = get_quote(a["ticker"])
        if quote is None:
            continue
        price, high, low = quote
        if a["direction"] == "above":
            extreme, hit = high, high >= a["target"]
        else:
            extreme, hit = low, low <= a["target"]
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"{stamp} {a['ticker']}: {price:.2f} (day {low:.2f}-{high:.2f}, "
              f"target {a['direction']} {a['target']})")
        if hit:
            msg = f"Price alert: {a['ticker']} is {price:.2f}"
            if extreme != price:
                word = "high" if a["direction"] == "above" else "low"
                msg += f" (day {word} {extreme:.2f})"
            msg += f", {a['direction']} your target of {a['target']}."
            if notify(msg):
                state[key] = datetime.now(timezone.utc).isoformat()
                save_state(state)
            # else: leave it armed so the next run tries again


def describe(alerts: list) -> str:
    return "\n".join(f"- {a['ticker']} {a['direction']} {a['target']:g}" for a in alerts)


def main() -> None:
    alerts, error = load_alerts()
    if error:
        print(error)
    if "--test" in sys.argv:
        if error:
            sent = notify(f"Test from price_alert.py - notifications work, BUT:\n{error}")
        else:
            sent = notify("Test from price_alert.py - notifications are working.\n"
                          f"Watching {len(alerts)} alert(s):\n{describe(alerts)}")
        if not sent:
            sys.exit(1)
        return
    if error:
        sys.exit(1)
    if "--once" in sys.argv:
        check_once(alerts)
        return
    print(f"Watching {len(alerts)} alert(s) every {CHECK_SECONDS}s. Ctrl+C to stop.")
    while True:
        check_once(alerts)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
