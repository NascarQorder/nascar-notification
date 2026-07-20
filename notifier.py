"""
NASCAR Market Notifier  (v4)
----------------------------
Pings your phone when NASCAR betting markets open, per source:

  Kalshi        -> a ping for every new NASCAR market (free API)
  Sportsbooks   -> a ping when a race FIRST appears at any book, then
                   "watch mode": re-checks that race every couple hours and
                   pings you as EACH book (DK, Caesars, MGM, FanDuel, ...)
                   opens its own odds. Watch ends when the board fills up
                   or after a few days.

Credit budget (The Odds API free tier = 500/month):
  - detecting new races: 0 credits (events endpoint is free)
  - each watch-mode check: 1 credit
  - typical race: 15-40 credits total. Plenty of headroom.
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

# Watch-mode dials (safe defaults; change via repo secrets only if needed)
WATCH_CHECK_HOURS = float(os.environ.get("WATCH_CHECK_HOURS", "2"))   # hours between odds checks per race
WATCH_MAX_DAYS = float(os.environ.get("WATCH_MAX_DAYS", "3"))         # give up watching after this long
WATCH_DONE_AT_BOOKS = int(os.environ.get("WATCH_DONE_AT_BOOKS", "10"))  # stop once this many books posted


def now_utc():
    return datetime.now(timezone.utc)


def http_get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nascar-notify/4.0"})
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
    state.setdefault("watching", {})
    return state


def save_state(state):
    state["last_checked"] = now_utc().isoformat(timespec="seconds")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------- kalshi

def get_kalshi_nascar_events():
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
        time.sleep(0.7)
    print(f"  Kalshi: {len(found)} open NASCAR event(s) found across {pages} page(s)")
    return found, True


# ---------------------------------------------------------------- odds api

def get_odds_api_nascar_events():
    if not ODDS_API_KEY:
        print("  (ODDS_API_KEY not set — skipping sportsbook check)")
        return {}, False
    sports = http_get_json(f"{ODDS_BASE}/sports/?all=true&apiKey={ODDS_API_KEY}")
    if sports is None:
        return {}, False
    keys = [s["key"] for s in sports if "nascar" in s.get("key", "").lower()
            or "nascar" in s.get("title", "").lower()]
    if not keys:
        print("  Odds API: no NASCAR listed right now (between races/offseason) — that's fine")
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


def _fmt(price):
    return f"+{price}" if price > 0 else str(price)


def get_books_snapshot(sport_key, event_id):
    """Spend 1 credit: returns {book_title: 'favorite +price'} for every
    US book that currently has this race posted. None on failure."""
    url = (f"{ODDS_BASE}/sports/{sport_key}/events/{event_id}/odds"
           f"?apiKey={ODDS_API_KEY}&regions=us&markets=outrights&oddsFormat=american")
    data = http_get_json(url)
    if data is None:
        return None
    books = {}
    for book in data.get("bookmakers", []):
        title = book.get("title", "?")
        fav_text = ""
        try:
            outcomes = book["markets"][0]["outcomes"]
            fav = min(outcomes, key=lambda o: o.get("price", 999999))
            fav_text = f"{fav['name']} {_fmt(fav['price'])}"
        except Exception:
            pass
        books[title] = fav_text
    return books


def hours_since(iso_string):
    try:
        then = datetime.fromisoformat(iso_string)
        return (now_utc() - then).total_seconds() / 3600.0
    except Exception:
        return 999999


# ---------------------------------------------------------------- main

def main():
    print(f"Run at {now_utc().isoformat(timespec='seconds')}")
    state = load_state()

    print("Checking Kalshi...")
    kalshi_now, kalshi_ok = get_kalshi_nascar_events()
    print("Checking sportsbooks (The Odds API)...")
    odds_now, odds_ok = get_odds_api_nascar_events()

    # ---- Kalshi: one ping per new market
    if kalshi_ok:
        if not state["kalshi_seeded"]:
            state["kalshi"] = dict(kalshi_now)
            state["kalshi_seeded"] = True
            send_notification("Kalshi tracking is live",
                              f"Now watching Kalshi — {len(kalshi_now)} existing NASCAR "
                              "market(s) memorized. You'll get a ping for anything NEW.")
        else:
            for ticker, title in kalshi_now.items():
                if ticker not in state["kalshi"]:
                    send_notification("New Kalshi NASCAR market",
                                      f"{title}\nkalshi.com/markets/{ticker.lower()}")
            state["kalshi"].update(kalshi_now)
    else:
        print("  (Kalshi check incomplete — memory untouched, will retry next run)")

    # ---- Sportsbooks: race-level detection + per-book watch mode
    if odds_ok:
        if not state["oddsapi_seeded"]:
            state["oddsapi"] = {i: odds_now[i]["name"] for i in odds_now}
            state["oddsapi_seeded"] = True
            send_notification("Sportsbook tracking is live",
                              f"Now watching the books — {len(odds_now)} existing NASCAR "
                              "event(s) memorized. New races get per-book open alerts.")
        else:
            for event_id, info in odds_now.items():
                if event_id in state["oddsapi"]:
                    continue
                # Brand-new race: ping, then start watching for per-book opens
                snapshot = get_books_snapshot(info["sport"], event_id) or {}
                when = info["commence"][:10] if info["commence"] else "TBA"
                if snapshot:
                    first_books = ", ".join(sorted(snapshot.keys()))
                    body = (f"{info['name']} (race day: {when})\n"
                            f"Open so far: {first_books}\n"
                            "Watching for the rest of the books...")
                else:
                    body = f"{info['name']} (race day: {when})\nWatching for books to post odds..."
                send_notification("New NASCAR race at the books", body)
                state["watching"][event_id] = {
                    "sport": info["sport"],
                    "name": info["name"],
                    "books": sorted(snapshot.keys()),
                    "started": now_utc().isoformat(timespec="seconds"),
                    "last_check": now_utc().isoformat(timespec="seconds"),
                }
            state["oddsapi"].update({i: odds_now[i]["name"] for i in odds_now})

        # -- Watch mode: re-check watched races on their own slower clock
        for event_id in list(state["watching"].keys()):
            w = state["watching"][event_id]
            age_days = hours_since(w["started"]) / 24.0
            if age_days > WATCH_MAX_DAYS or len(w["books"]) >= WATCH_DONE_AT_BOOKS:
                print(f"  watch ended for: {w['name']} ({len(w['books'])} books posted)")
                del state["watching"][event_id]
                continue
            if hours_since(w["last_check"]) < WATCH_CHECK_HOURS:
                continue  # not due yet — costs nothing
            snapshot = get_books_snapshot(w["sport"], event_id)
            w["last_check"] = now_utc().isoformat(timespec="seconds")
            if snapshot is None:
                continue
            new_books = [b for b in snapshot if b not in w["books"]]
            for b in sorted(new_books):
                fav = snapshot[b]
                body = f"{w['name']}"
                if fav:
                    body += f"\nTheir favorite: {fav}"
                send_notification(f"{b} just opened NASCAR odds", body)
            w["books"] = sorted(set(w["books"]) | set(snapshot.keys()))
            print(f"  watching {w['name']}: {len(w['books'])} book(s) posted")
    else:
        print("  (sportsbook check incomplete — memory untouched, will retry next run)")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
