"""
NASCAR Market Notifier
----------------------
Checks two places for newly-opened NASCAR betting markets:
  1. Kalshi (free public API)
  2. Major sportsbooks via The Odds API (free events endpoint = 0 credits)

When something new appears, it sends a push notification to your phone
through ntfy (free app).

It remembers what it has already seen in a file called state.json,
so you only ever get notified about genuinely NEW markets.

Settings come from environment variables (GitHub Actions fills these in):
  NTFY_TOPIC     - your private ntfy topic name (required for notifications)
  ODDS_API_KEY   - your key from the-odds-api.com (optional; sportsbook
                   checking is skipped if it's missing)
"""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ---------------------------------------------------------------- settings

STATE_FILE = "state.json"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"

# We match these words against Kalshi event titles/tickers (case-insensitive)
KALSHI_KEYWORDS = ["nascar"]

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()

# When a brand-new sportsbook event appears, spend 1 credit to grab the
# early odds so the notification can show the favorite. Set to "no" to
# keep credit usage at literally zero.
FETCH_EARLY_ODDS = os.environ.get("FETCH_EARLY_ODDS", "yes").lower() == "yes"


# ---------------------------------------------------------------- helpers

def http_get_json(url):
    """Fetch a URL and return parsed JSON (or None on failure)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nascar-notify/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ! request failed: {url.split('?')[0]} -> {e}")
        return None


def send_notification(title, message):
    """Push a notification to the phone via ntfy."""
    if not NTFY_TOPIC:
        print(f"  (no NTFY_TOPIC set — would have sent: {title}: {message})")
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(NTFY_TOPIC)}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "checkered_flag"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30)
        print(f"  -> notified: {title}")
    except Exception as e:
        print(f"  ! notification failed: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None  # first ever run


def save_state(state):
    state["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------- kalshi

def get_kalshi_nascar_events():
    """
    Walk through Kalshi's open events (paginated) and keep anything
    whose title or ticker mentions NASCAR. Returns {ticker: title}.
    """
    found = {}
    cursor = ""
    pages = 0
    while pages < 60:  # safety cap
        url = f"{KALSHI_BASE}/events?status=open&limit=200"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        data = http_get_json(url)
        if not data or "events" not in data:
            break
        for ev in data["events"]:
            text = (ev.get("title", "") + " " + ev.get("event_ticker", "")).lower()
            if any(kw in text for kw in KALSHI_KEYWORDS):
                found[ev["event_ticker"]] = ev.get("title", ev["event_ticker"])
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor:
            break
    print(f"  Kalshi: {len(found)} open NASCAR event(s) found across {pages} page(s)")
    return found


# ---------------------------------------------------------------- odds api

def get_odds_api_nascar_sports():
    """Ask The Odds API which NASCAR-related sport keys exist (free call)."""
    if not ODDS_API_KEY:
        return []
    data = http_get_json(f"{ODDS_BASE}/sports/?all=true&apiKey={ODDS_API_KEY}")
    if not data:
        return []
    return [s["key"] for s in data if "nascar" in s.get("key", "").lower()
            or "nascar" in s.get("title", "").lower()]


def get_odds_api_nascar_events(sport_keys):
    """
    List upcoming NASCAR events at the books (free endpoint, 0 credits).
    Returns {event_id: {"name": ..., "sport": ..., "commence": ...}}.
    """
    found = {}
    for key in sport_keys:
        data = http_get_json(f"{ODDS_BASE}/sports/{key}/events?apiKey={ODDS_API_KEY}")
        if not data:
            continue
        for ev in data:
            name = ev.get("home_team") or ev.get("sport_title") or key
            # Race outrights usually have no home/away; build a sensible name
            if ev.get("home_team") and ev.get("away_team"):
                name = f"{ev['away_team']} @ {ev['home_team']}"
            found[ev["id"]] = {
                "name": name,
                "sport": key,
                "commence": ev.get("commence_time", ""),
            }
    print(f"  Odds API: {len(found)} upcoming NASCAR event(s) across {len(sport_keys)} sport key(s)")
    return found


def get_early_favorite(sport_key, event_id):
    """
    Spend 1 credit to peek at the freshly-posted odds and find the favorite.
    Returns a short string like 'Fav: Kyle Larson +450 (DraftKings)' or ''.
    """
    if not FETCH_EARLY_ODDS:
        return ""
    url = (f"{ODDS_BASE}/sports/{sport_key}/events/{event_id}/odds"
           f"?apiKey={ODDS_API_KEY}&regions=us&markets=outrights&oddsFormat=american")
    data = http_get_json(url)
    if not data or not data.get("bookmakers"):
        return ""
    try:
        book = data["bookmakers"][0]
        outcomes = book["markets"][0]["outcomes"]
        fav = min(outcomes, key=lambda o: o.get("price", 999999))
        price = fav["price"]
        price_str = f"+{price}" if price > 0 else str(price)
        return f"Early favorite: {fav['name']} {price_str} ({book.get('title', 'book')})"
    except Exception:
        return ""


# ---------------------------------------------------------------- main

def main():
    print(f"Run at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"kalshi": {}, "oddsapi": {}}

    # ---- Kalshi
    print("Checking Kalshi...")
    kalshi_now = get_kalshi_nascar_events()
    new_kalshi = {t: title for t, title in kalshi_now.items() if t not in state["kalshi"]}

    # ---- Sportsbooks
    print("Checking sportsbooks (The Odds API)...")
    odds_now = {}
    if ODDS_API_KEY:
        sport_keys = get_odds_api_nascar_sports()
        if sport_keys:
            odds_now = get_odds_api_nascar_events(sport_keys)
        else:
            print("  (no NASCAR sport keys currently listed — offseason or between races)")
    else:
        print("  (ODDS_API_KEY not set — skipping sportsbook check)")
    new_odds = {i: info for i, info in odds_now.items() if i not in state["oddsapi"]}

    # ---- Notify
    if first_run:
        total = len(kalshi_now) + len(odds_now)
        send_notification(
            "NASCAR notifier is live",
            f"Setup complete. Currently tracking {total} existing market(s) "
            f"({len(kalshi_now)} Kalshi, {len(odds_now)} sportsbook). "
            "You'll get a ping whenever something NEW opens.",
        )
    else:
        for ticker, title in new_kalshi.items():
            send_notification(
                "New Kalshi NASCAR market",
                f"{title}\nkalshi.com/markets/{ticker.lower()}",
            )
        for event_id, info in new_odds.items():
            extra = get_early_favorite(info["sport"], event_id)
            when = info["commence"][:10] if info["commence"] else "TBA"
            body = f"{info['name']} (race day: {when})"
            if extra:
                body += f"\n{extra}"
            send_notification("Books just posted a NASCAR market", body)
        if not new_kalshi and not new_odds:
            print("Nothing new this run.")

    # ---- Remember everything we saw. We only ever ADD to memory, never
    # remove — that way a temporary API hiccup can't make old markets look
    # "new" again and spam your phone.
    state["kalshi"].update(kalshi_now)
    state["oddsapi"].update({i: odds_now[i]["name"] for i in odds_now})
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
