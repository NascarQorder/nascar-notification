"""
Sportsbook Probe  (rev 2)
-------------------------
Knocks on the data doors of DraftKings, Caesars, BetMGM, and FanDuel from
GitHub's computers and prints what each one answers. Sends nothing to your
phone and changes nothing. We use this log to decide which books we can
watch directly (no middleman) for instant market-open alerts.
"""

import json
import urllib.request

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

CANDIDATES = [
    # ---- DraftKings
    ("DraftKings event groups (v5)",
     "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups?format=json"),
    ("DraftKings display groups (v3)",
     "https://sportsbook.draftkings.com/sites/US-SB/api/v3/featured/displaygroups?format=json"),
    ("DraftKings nav (nash)",
     "https://sportsbook-nash.draftkings.com/sites/US-SB/api/v1/nav?format=json"),

    # ---- Caesars
    ("Caesars sports list (NJ)",
     "https://api.americanwagering.com/regions/us/locations/nj/brands/czr/sb/v3/sports"),
    ("Caesars sports list (NY)",
     "https://api.americanwagering.com/regions/us/locations/ny/brands/czr/sb/v3/sports"),

    # ---- BetMGM
    ("BetMGM fixtures API (NJ, no access id)",
     "https://sports.nj.betmgm.com/cds-api/betting-offer/v3/producttree?lang=en-us&country=US"),
    ("BetMGM widget API (NJ)",
     "https://sports.nj.betmgm.com/en/sports/api/widget/widgetdata?layoutSize=Large&page=SportLobby&sportId=27&widgetId=/mobilesports-v1.0/layout/layout_us/modules/sportgrid"),

    # ---- FanDuel
    ("FanDuel sports catalog (NJ)",
     "https://sbapi.nj.sportsbook.fanduel.com/api/content-managed-page?page=SPORT&_ak=FhMFpcPWXMeyZxOx&timezone=America%2FNew_York"),
    ("FanDuel homepage API (NJ)",
     "https://sbapi.nj.sportsbook.fanduel.com/api/sports?_ak=FhMFpcPWXMeyZxOx"),
]


def find_keyword(obj, keyword, path="", hits=None, limit=20):
    if hits is None:
        hits = []
    if len(hits) >= limit:
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_keyword(v, keyword, f"{path}.{k}", hits, limit)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:300]):
            find_keyword(v, keyword, f"{path}[{i}]", hits, limit)
    elif isinstance(obj, str) and keyword in obj.lower():
        hits.append(f"{path} = {obj[:80]}")
    return hits


def probe(name, url):
    print(f"\n=== {name} ===")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            print(f"Status: {resp.status}, size: {len(raw):,} bytes")
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                text = raw[:200].decode("utf-8", errors="replace")
                print(f"Not JSON. Starts with: {text!r}")
                return
            if isinstance(data, dict):
                print(f"Top-level keys: {list(data.keys())[:15]}")
            elif isinstance(data, list):
                print(f"Top-level: list, {len(data)} items")
                if data and isinstance(data[0], dict):
                    print(f"First item keys: {list(data[0].keys())[:15]}")
            hits = find_keyword(data, "nascar")
            if hits:
                print(f"NASCAR mentions ({len(hits)}):")
                for h in hits:
                    print(f"  {h}")
            else:
                hits2 = find_keyword(data, "motor")
                if hits2:
                    print(f"No 'nascar', but 'motor' mentions ({len(hits2)}):")
                    for h in hits2[:8]:
                        print(f"  {h}")
                else:
                    print("No 'nascar' or 'motor' strings found.")
    except urllib.error.HTTPError as e:
        print(f"BLOCKED/ERROR: HTTP {e.code} {e.reason}")
    except Exception as e:
        print(f"FAILED: {e}")


if __name__ == "__main__":
    print("Sportsbook probe rev2 — testing which doors are open from GitHub...")
    for name, url in CANDIDATES:
        probe(name, url)
    print("\nProbe complete. Send this whole log to Claude.")
