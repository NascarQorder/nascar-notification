"""
NASCAR Market Notifier  (v2)
----------------------------
Checks two places for newly-opened NASCAR betting markets:
  1. Kalshi (free public API)
  2. Major sportsbooks via The Odds API (free events endpoint = 0 credits)

New in v2:
  - Pauses politely between Kalshi pages and retries when rate-limited (fixes 429 errors)
  - Each source "seeds" its memory separately on its first successful check,
    so a failed run can never cause missed or duplicate notifications
"""

import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

STATE_FILE = "state.json"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"
KALSHI_KEYWORDS = ["nascar"]

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
FETCH_EARLY_ODDS = os.environ.get("FETCH_EARLY_ODDS", "yes").lower() == "yes"


def http_get_json(url, tries=3):
    """Fetch a URL and return parsed JSON. Backs off and retries if the
    site says 'slow down' (error 429). Returns None if it truly fails."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nascar-notify/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                wait = 3 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s and retrying...")
                time.sleep(wait)
                continue
            print(f"  ! request failed: {url.split('?')[0]} -> {e}")
            return None
        except Exception as e:
            print(f"  ! request failed: {url.split('?')[0]} -> {e}")
            return None
    return None


def send_notification(title, message):
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
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    for src in ("kalshi", "oddsapi"):
        state.setdefault(src, {})
        state.setdefault(src + "_seeded", False)
    return state


def save_state(state):
    state["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def get_kalshi_nascar_events():
    """Walk Kalshi's open events pages (politely, with pauses).
    Returns (found_dict, completed_ok)."""
    found = {}
    cursor = ""
    pages = 0
    while pages < 60:
        url = f"{KALSHI_BASE}/events?status=open&limit=200"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        data = http_get_json(url)
        if data is None or "events" not in data:
            print(f"  Kalshi: scan interrupted after {pages} page(s) — will try again next run")
            return found, False
        for ev in data["events"]:
            text = (ev.get("title", "") + " " + ev.get("event_ticker", "")).lower()
            if any(kw in text for kw in KALSHI_KEYWORDS):
                found[ev["event_ticker"]] = ev.get("title", ev["event_ticker"])
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor:
            break
        time.sleep(0.7)  # be polite between pages so Kalshi doesn't block us
    print(f"  Kalshi: {len(found)} open NASCAR event(s) found across {pages} page(s)")
    return found, True


def get_odds_api_nascar_events():
    """List upcoming NASCAR events at the books (all free calls).
    Returns (found_dict, completed_ok)."""
    if not ODDS_API_KEY:
        print("  (ODDS_API_KEY not set — skipping sportsbook check)")
        return {}, False
    sports = http_get_json(f"{ODDS_BASE}/sports/?all=true&apiKey={ODDS_API_KEY}")
    if sports is None:
        return {}, False
    keys = [s["key"] for s in sports if "nascar" in s.get("key", "").lower()
            or "nascar" in s.get("title", "").lower()]
    if not keys:
        print("  Odds API: no NASCAR listed right now (offseason or between races) — that's fine")
        return {}, True
    found = {}
    for key in keys:
        data = http_get_json(f"{ODDS_BASE}/sports/{key}/events?apiKey={ODDS_API_KEY}")
        if data is None:
            return found, False
        for ev in data:
            name = ev.get("home_team") or ev.get("sport_title") or key
            if ev.get("home_team") and ev.get("away_team"):
                name = f"{ev['away_team']} @ {ev['home_team']}"
            found[ev["id"]] = {"name": name, "sport": key,
                               "commence": ev.get("commence_time", "")}
    print(f"  Odds API: {len(found)} upcoming NASCAR event(s)")
    return found, True


def get_early_favorite(sport_key, event_id):
    """Spend 1 credit to peek at fresh odds and name the favorite."""
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


def main():
    print(f"Run at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    state = load_state()

    print("Checking Kalshi...")
    kalshi_now, kalshi_ok = get_kalshi_nascar_events()
    print("Checking sportsbooks (The Odds API)...")
    odds_now, odds_ok = get_odds_api_nascar_events()

    # ---- Kalshi side
    if kalshi_ok:
        if not state["kalshi_seeded"]:
            state["kalshi"] = dict(kalshi_now)
            state["kalshi_seeded"] = True
            send_notification(
                "Kalshi tracking is live",
                f"Now watching Kalshi — {len(kalshi_now)} existing NASCAR market(s) "
                "memorized. You'll get a ping for anything NEW.",
            )
        else:
            for ticker, title in kalshi_now.items():
                if ticker not in state["kalshi"]:
                    send_notification("New Kalshi NASCAR market",
                                      f"{title}\nkalshi.com/markets/{ticker.lower()}")
            state["kalshi"].update(kalshi_now)
    else:
        print("  (Kalshi check incomplete — memory untouched, will retry next run)")

    # ---- Sportsbook side
    if odds_ok:
        if not state["oddsapi_seeded"]:
            state["oddsapi"] = {i: odds_now[i]["name"] for i in odds_now}
            state["oddsapi_seeded"] = True
            send_notification(
                "Sportsbook tracking is live",
                f"Now watching the books — {len(odds_now)} existing NASCAR event(s) "
                "memorized. You'll get a ping for anything NEW.",
            )
        else:
            for event_id, info in odds_now.items():
                if event_id not in state["oddsapi"]:
                    extra = get_early_favorite(info["sport"], event_id)
                    when = info["commence"][:10] if info["commence"] else "TBA"
                    body = f"{info['name']} (race day: {when})"
                    if extra:
                        body += f"\n{extra}"
                    send_notification("Books just posted a NASCAR market", body)
            state["oddsapi"].update({i: odds_now[i]["name"] for i in odds_now})
    else:
        print("  (sportsbook check incomplete — memory untouched, will retry next run)")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
