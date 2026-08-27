#!/usr/bin/env python3
"""
FoCo Fuel — local menu server (Python standard library only, no pip installs).

Why this exists:
  The browser can't reliably fetch Dartmouth's menu API directly (cross-origin
  requests get blocked, and the API also rejects some clients). So this tiny
  server sits between the page and Dartmouth: it fetches, filters to 53 Commons,
  caches per day, and hands the page a clean result.

What it serves:
  GET  /             -> the index.html app
  GET  /api/menu     -> today's 53 Commons menu as JSON (optional ?date=YYYYMMDD)
  POST /api/plate    -> AI-composed plates (rules own safety; rules-only fallback)

Run it:   python3 server.py
Then open http://localhost:8747/
"""

import os
import ssl
import json
import re
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import dotenv_values

import rules       # the rules layer (safety + shortlist + fallback plate)
import ai_plates   # the AI layer (Gemini), kept separate from the rules

# --- Config -----------------------------------------------------------------
PORT      = 8747
FOCO_ID   = "alias05"                       # 53 Commons, per the PRD
HANOVER   = ZoneInfo("America/New_York")    # Dartmouth's timezone
API_URL   = "https://menu.dartmouth.edu/menuapi/mealitems?dates={date}"
USER_AGENT = "Mozilla/5.0 (FoCoFuel local server)"  # plain Python UA gets 403'd
HERE      = os.path.dirname(os.path.abspath(__file__))

# Secrets/config come from .env.local ONLY (never hardcoded, never sent to the
# browser). dotenv_values reads the file without mutating the process env.
_ENV        = dotenv_values(os.path.join(HERE, ".env.local"))
GEMINI_KEY  = _ENV.get("GEMINI_API_KEY") or ""
GEMINI_MODEL = _ENV.get("GEMINI_MODEL") or ""
AI_TIMEOUT  = 30  # seconds; the Gemini call must not hang a page load forever

# In-memory cache: { "YYYYMMDD": [ ...filtered alias05 items... ] }.
# Cleared whenever the server restarts. No database, by design.
CACHE = {}


# --- HTTPS with working certificates ----------------------------------------
def make_ssl_context():
    """Build an SSL context that can actually verify Dartmouth's certificate.

    The python.org macOS build ships without a CA bundle, so the default context
    fails with CERTIFICATE_VERIFY_FAILED. Prefer certifi if present, then the
    macOS system bundle, then whatever Python's default is.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    if os.path.exists("/etc/ssl/cert.pem"):        # present on macOS
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()

SSL_CTX = make_ssl_context()


# --- Date helpers -----------------------------------------------------------
def today_hanover():
    """Today's date in Hanover as YYYYMMDD, so the rollover matches Dartmouth."""
    return datetime.now(HANOVER).strftime("%Y%m%d")

def is_valid_date(s):
    return bool(re.fullmatch(r"\d{8}", s or ""))


# --- The core: fetch + filter + cache ---------------------------------------
def fetch_foco(date):
    """Call Dartmouth and return ONLY the alias05 (53 Commons) items.

    Raises on network/HTTP/JSON errors so the caller can report 'fetch-failed'.
    """
    url = API_URL.format(date=date)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as resp:
        data = json.load(resp)
    all_items = data.get("mealItems") or []
    return [it for it in all_items if it.get("mainLocationId") == FOCO_ID]

# --- Sample menu fallback ---------------------------------------------------
# Used ONLY when the live fetch fails or returns nothing. Live is always tried
# first. See sample_menu.json for provenance (real names/macros, reconstructed
# allergen + dietary tags).
SAMPLE_PATH = os.path.join(HERE, "sample_menu.json")

# nutrient id -> (label, unit), matching the live API's nutrients[] entries.
_NUTRIENT_META = {
    "calories":           ("Calories", "kcal"),
    "protein":            ("Protein", "gm"),
    "totalCarbohydrates": ("Total Carbohydrates", "gm"),
    "totalFat":           ("Total Fat", "gm"),
    "dietaryFiber":       ("Dietary Fiber", "gm"),
    "totalSugars":        ("Total Sugars", "gm"),
    "sodium":             ("Sodium", "mg"),
}


def load_sample_menu(date):
    """Expand sample_menu.json into the EXACT shape the live API returned.

    Every item is stamped with `date`, so date/meal matching, the dietary
    filter, role tagging and plate composition all behave identically to live.
    Returns [] if the file is missing or unreadable (never raises).
    """
    try:
        with open(SAMPLE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[sample] could not load {SAMPLE_PATH}: {type(e).__name__}: {e}", flush=True)
        return []

    items = []
    for entry in raw.get("items") or []:
        nutrients = []
        for nid, value in (entry.get("macros") or {}).items():
            label, unit = _NUTRIENT_META.get(nid, (nid, ""))
            # null stays null -> downstream renders "unknown", never 0.
            nutrients.append({"id": nid, "label": label, "value": value, "unit": unit})
        items.append({
            "id": entry.get("id"),
            "itemName": entry.get("name"),
            "mainLocationId": FOCO_ID,
            "mainLocationLabel": "53 Commons",
            "portionSize": entry.get("portionSize"),
            "nutrients": nutrients,
            "containsAllergens": [{"id": a.lower(), "label": a} for a in (entry.get("allergens") or [])],
            "meetsPreferences": [{"id": p.lower(), "label": p} for p in (entry.get("preferences") or [])],
            # Stamp the requested date so served_at() matches exactly as live.
            "datesAvailable": [{
                "date": date,
                "menus": [{"mealPeriod": m, "subLocation": "Sample Menu", "isSpecial": False}
                          for m in (entry.get("meals") or [])],
            }],
        })
    return items


def get_menu(date):
    """Return a response dict for `date`, using the cache when possible.

    Live Dartmouth is ALWAYS the primary path. Only if that fails (or yields no
    FoCo items) do we fall back to the bundled sample menu, flagged so the UI
    can mark it. `source` is "live" or "sample".

    Shape (mealItems is the SAME shape the recommendation module consumes):
      { "state": "ok",      "date", "source": "live"|"sample", "reason", "count", "mealItems": [...] }
      { "state": "no-menu", "date", "reason": "no-items"|"fetch-failed", "count": 0, "mealItems": [] }
    """
    def sample_response(reason):
        """Serve the sample menu, clearly flagged, so the app stays demoable."""
        items = load_sample_menu(date)
        if not items:
            print(f"[menu] {date} EMPTY: live failed ({reason}) and no sample "
                  f"available", flush=True)
            return {"state": "no-menu", "date": date, "reason": reason,
                    "count": 0, "mealItems": []}
        print(f"[menu] {date} FALLBACK to SAMPLE menu ({len(items)} items) — "
              f"live failed: {reason}", flush=True)
        # Deliberately NOT cached: every request retries live first, so the app
        # recovers by itself the moment a real menu source works again.
        return {"state": "ok", "date": date, "source": "sample",
                "reason": reason, "count": len(items), "mealItems": items}
    # 1) Cache hit — repeat loads on the same day never re-hit Dartmouth.
    #    Only LIVE results are ever cached (see below).
    if date in CACHE:
        items = CACHE[date]
        print(f"[menu] {date} served from cache ({len(items)} items)", flush=True)
        return {"state": "ok", "date": date, "source": "live", "reason": None,
                "count": len(items), "mealItems": items}

    # 2) PRIMARY PATH: try the live Dartmouth fetch.
    try:
        items = fetch_foco(date)
    except Exception as e:
        print(f"[menu] {date} live fetch failed: {type(e).__name__}: {e}", flush=True)
        return sample_response("fetch-failed")

    # 3) Fetch worked but nothing for FoCo — the OTHER distinct empty case.
    if not items:
        print(f"[menu] {date} Dartmouth returned 0 alias05 items", flush=True)
        return sample_response("no-items")

    # 4) Real live data — cache it (only non-empty, so a late-posted menu can
    #    still appear later the same day) and return it.
    CACHE[date] = items
    print(f"[menu] {date} fetched from Dartmouth: {len(items)} alias05 items "
          f"(cached)", flush=True)
    return {"state": "ok", "date": date, "source": "live", "reason": None,
            "count": len(items), "mealItems": items}


# --- Plate composition: rules (safety) -> AI -> validate -> rules fallback ---
def _resolve(ids, id_map):
    """Turn plate ids into detail objects the page can render."""
    out = []
    for i in ids:
        it = id_map.get(i)
        if it is None:
            continue
        out.append({
            "id": i,
            "name": it.get("itemName"),
            "portionSize": it.get("portionSize"),
            "role": rules.role_of(it),
            "macros": rules.macros_of(it),
        })
    return out


def compose_plates(date, meal, demand, prefs):
    """Full pipeline. Rules filter for safety; the AI only sees a safe shortlist;
    Python validates every returned id; anything short of success falls back to
    the rules-only plate. Returns a dict ready to send as JSON."""
    menu = get_menu(date)
    if menu["state"] != "ok":
        return {"state": menu["state"], "date": date, "meal": meal,
                "demand": demand, "reason": menu["reason"], "plates": []}

    # 1) Rules own safety: filter to real, meal-appropriate, user-safe items.
    safe = rules.safe_items(menu["mealItems"], date, meal, prefs)
    if not safe:
        print(f"[plate] {date} {meal}: no items pass the user's filters", flush=True)
        return {"state": "no-safe-items", "date": date, "meal": meal,
                "demand": demand, "reason": "no-safe-items", "plates": []}

    # 2/3) Tag roles + build the id-keyed shortlist the AI is allowed to see.
    shortlist = rules.build_shortlist(safe)
    id_map = {rules.item_id(it): it for it in safe}
    shortlist_ids = set(id_map.keys())

    def finish(source, plates, fallback_reason=None):
        out = []
        for p in plates:
            items = _resolve(p["items"], id_map)
            # Anchor the cue to this plate's actual carb, if it has one.
            carb_name = next((it["name"] for it in items if it["role"] == "carb"), None)
            out.append({"items": items, "why": p["why"],
                        "portionCue": rules.portion_cue(demand, carb_name)})
        return {"state": "ok", "date": date, "meal": meal, "demand": demand,
                "source": source, "fallbackReason": fallback_reason,
                # "live" or "sample" — lets the UI mark demo data clearly.
                "menuSource": menu.get("source", "live"),
                "shortlistCount": len(shortlist), "plates": out}

    # --- Try the AI path -----------------------------------------------------
    fallback_reason = None
    if not GEMINI_KEY:
        fallback_reason = "no GEMINI_API_KEY configured"
    else:
        try:
            ai = ai_plates.generate_plates(shortlist, demand, GEMINI_KEY,
                                           GEMINI_MODEL, AI_TIMEOUT)
            # 4) Validate in Python: every id must exist in the shortlist.
            valid, dropped = [], 0
            for p in ai:
                if p["items"] and all(i in shortlist_ids for i in p["items"]):
                    valid.append(p)
                else:
                    dropped += 1
            if dropped:
                print(f"[plate] {date} {meal}: dropped {dropped} AI plate(s) "
                      f"referencing unknown ids", flush=True)
            if valid:
                print(f"[plate] {date} {meal}: AI produced {len(valid)} valid "
                      f"plate(s) (model={GEMINI_MODEL})", flush=True)
                return finish("ai", valid[:3])
            fallback_reason = "AI returned no plates with valid ids"
        except Exception as e:
            fallback_reason = f"AI call failed ({type(e).__name__}: {e})"

    # --- Fallback: rules-only plate (still fully functional) ------------------
    print(f"[plate] {date} {meal}: FALLBACK to rules — {fallback_reason}", flush=True)
    rules_plates = rules.compose_rules_plate(safe, demand)
    return finish("rules", rules_plates, fallback_reason)


# --- HTTP plumbing ----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # ---- API route ----
        if self.path.startswith("/api/menu"):
            # Parse an optional ?date=YYYYMMDD; default to today in Hanover.
            date = None
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for pair in query.split("&"):
                    if pair.startswith("date="):
                        date = pair.split("=", 1)[1]
            if date and not is_valid_date(date):
                return self._send_json(
                    {"state": "error", "reason": "bad-date",
                     "message": "date must be YYYYMMDD"}, code=400)
            if not date:
                date = today_hanover()
            return self._send_json(get_menu(date))

        # ---- Static app ----
        if self.path in ("/", "/index.html"):
            return self._send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")

        self._send_json({"error": "not found", "path": self.path}, code=404)

    def do_POST(self):
        if not self.path.startswith("/api/plate"):
            return self._send_json({"error": "not found", "path": self.path}, code=404)

        # Parse the JSON body: { date?, meal, demand, prefs:{vegetarian,glutenFree,avoid[]} }
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send_json({"state": "error", "reason": "bad-json"}, code=400)

        date = payload.get("date")
        if date and not is_valid_date(date):
            return self._send_json({"state": "error", "reason": "bad-date"}, code=400)
        if not date:
            date = today_hanover()
        meal = payload.get("meal") or "Lunch"
        demand = payload.get("demand") or "moderate"
        prefs = payload.get("prefs") or {}

        return self._send_json(compose_plates(date, meal, demand, prefs))

    # Quieter, friendlier request log line.
    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    # Show whether AI is wired up — but NEVER print the key itself.
    ai_state = (f"on (model={GEMINI_MODEL})" if GEMINI_KEY else "off — rules only (no key)")
    print(f"FoCo Fuel server running →  http://localhost:{PORT}/")
    print(f"API check →  http://localhost:{PORT}/api/menu")
    print(f"AI plates →  POST http://localhost:{PORT}/api/plate   [AI: {ai_state}]")
    print("Press Ctrl+C to stop.\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
