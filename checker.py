#!/usr/bin/env python3
"""
Lindner Hotel Antwerp room-availability watcher.

Checks the stay 2026-07-16 -> 2026-07-20 (2 adults, 1 room) on two sources:

  1. Booking.com  -- PRIMARY, reliable from the cloud. Booking's `roomTable`
                     GraphQL returns an authoritative `isSoldOut` boolean for
                     the exact stay and is reachable from datacenter IPs with a
                     plain HTTPS POST (curl_cffi Chrome TLS impersonation). No
                     browser, no rendering, not bot-blocked. This is the
                     workhorse that detects an opening.
  2. Hyatt.com    -- the hotel's own engine (Lindner = JdV by Hyatt), where you
                     book direct. Best-effort: it's behind Kasada bot-protection
                     that often blocks datacenter IPs, so we load the real page
                     in a headless browser and try its JSON a few times. When it
                     gets through it confirms direct availability; when blocked
                     it just reports "couldn't check" (no harm — Booking covers).

On a rising edge (sold out -> available) on EITHER source it opens a GitHub
issue that @mentions you (GitHub emails you) and, if Gmail SMTP secrets are set,
sends a direct email too. The alert always includes the Hyatt direct booking
link so you book direct. Per-source state in state.json => one alert per opening.
"""

import json
import os
import re
import sys
import time
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

# ----------------------------- configuration --------------------------------
CHECK_IN = "2026-07-16"
CHECK_OUT = "2026-07-20"
ADULTS = 2
ROOMS = 1
KIDS = 0
STOP_ON_OR_AFTER = "2026-07-16"        # window closes at check-in
REPEAT_ALERT_HOURS = 6                  # re-remind if still open after N hours

# Booking.com (primary, datacenter-reliable)
BOOKING_PAGENAME = "lindner-hotel-antwerp"
BOOKING_COUNTRY = "be"
BOOKING_GRAPHQL = "https://www.booking.com/dml/graphql?lang=en-us"
BOOKING_LINK = (
    f"https://www.booking.com/hotel/{BOOKING_COUNTRY}/{BOOKING_PAGENAME}.html"
    f"?checkin={CHECK_IN}&checkout={CHECK_OUT}&group_adults={ADULTS}&no_rooms={ROOMS}&group_children={KIDS}"
)

# Hyatt (direct, best-effort)
HYATT_SPID = "ANRJA"
HYATT_ROOMS_PAGE = (
    f"https://www.hyatt.com/shop/rooms/{HYATT_SPID}"
    f"?checkinDate={CHECK_IN}&checkoutDate={CHECK_OUT}&adults={ADULTS}&rooms={ROOMS}&kids={KIDS}"
)
HYATT_JSON = (
    f"https://www.hyatt.com/shop/service/rooms/roomrates/{HYATT_SPID}"
    f"?checkinDate={CHECK_IN}&checkoutDate={CHECK_OUT}&rooms={ROOMS}&adults={ADULTS}&kids={KIDS}"
)
HYATT_BOOK_LINK = HYATT_ROOMS_PAGE
LINDNER_PAGE = "https://www.lindnerhotels.com/en/hotels/lindner-hotel-antwerp"

STATE_FILE = "state.json"
OWNER_HANDLE = os.environ.get("OWNER_HANDLE", "jwang815")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORCE_ALERT = os.environ.get("FORCE_ALERT", "").lower() in ("1", "true", "yes")


def log(*a):
    print(*a, flush=True)


# ------------------------------- state I/O ----------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ----------------------- source: Booking.com (primary) ----------------------
def check_booking():
    """(available|None, mode). Authoritative `isSoldOut` via roomTable GraphQL."""
    try:
        from curl_cffi import requests as creq
    except Exception as e:
        log(f"[booking] curl_cffi missing: {e}")
        return None, "curl_cffi-missing"

    headers = {
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.booking.com",
        "Referer": f"https://www.booking.com/hotel/{BOOKING_COUNTRY}/{BOOKING_PAGENAME}.html",
    }
    query = ("query RoomTable($input: RoomTableQueryInput!) { "
             "roomTable(input: $input) { ... on RoomTableQueryResult { "
             "isSoldOut hotelId propertyName } } }")
    payload = {
        "operationName": "RoomTable",
        "variables": {"input": {
            "highlightedBlocks": [],
            "pagenameDetails": {"countryCode": BOOKING_COUNTRY, "pagename": BOOKING_PAGENAME},
            "searchConfig": {
                "childrenAges": [], "nbAdults": ADULTS, "nbChildren": KIDS, "nbRooms": ROOMS,
                "searchConfigDate": {"checkin": CHECK_IN, "checkout": CHECK_OUT},
            },
        }},
        "extensions": {},
        "query": query,
    }
    for attempt in range(3):
        try:
            r = creq.post(BOOKING_GRAPHQL, json=payload, impersonate="chrome",
                          timeout=30, headers=headers)
            if r.status_code == 200:
                rt = ((r.json().get("data") or {}).get("roomTable") or {})
                if isinstance(rt.get("isSoldOut"), bool):
                    avail = not rt["isSoldOut"]
                    log(f"[booking] isSoldOut={rt['isSoldOut']} -> available={avail} "
                        f"(hotel={rt.get('propertyName')})")
                    return avail, "graphql"
                log(f"[booking] try {attempt+1}: unexpected payload {str(rt)[:120]}")
            else:
                log(f"[booking] try {attempt+1}: HTTP {r.status_code}")
        except Exception as e:
            log(f"[booking] try {attempt+1} error: {e}")
        time.sleep(3)
    return None, "error"


# ----------------------- source: Hyatt (best-effort) ------------------------
def check_hyatt(page):
    """(available|None, mode). None = blocked/indeterminate."""
    try:
        page.goto(HYATT_ROOMS_PAGE, wait_until="domcontentloaded", timeout=45000)
        for attempt in range(4):
            page.wait_for_timeout(8000 if attempt == 0 else 5000)
            try:
                res = page.evaluate(
                    """async (url) => {
                        try {
                            const r = await fetch(url, {headers: {accept: 'application/json'}});
                            return {status: r.status, body: (await r.text()).slice(0, 30000)};
                        } catch (e) { return {status: -1, body: String(e)}; }
                    }""", HYATT_JSON)
                status, body = res.get("status"), res.get("body", "")
                if status == 200 and body.strip().startswith("{"):
                    data = json.loads(body)
                    rr = data.get("roomRates") or {}
                    info = json.dumps(data.get("responseInfo") or {})
                    avail = len(rr) > 0 and "soldOut" not in info
                    log(f"[hyatt] JSON ok (try {attempt+1}): roomRates={len(rr)} available={avail}")
                    return avail, f"json/try{attempt+1}"
                log(f"[hyatt] JSON try {attempt+1}: status={status}")
            except Exception as e:
                log(f"[hyatt] json try {attempt+1} error: {e}")

        text = (page.inner_text("body") or "")
        low = text.lower()
        if any(k in low for k in ["are you a robot", "verify you are human", "captcha",
                                  "request unsuccessful", "access denied"]):
            log("[hyatt] DOM blocked (bot-check)")
            return None, "blocked"
        soldout = ("not available during those dates" in low
                   or "this hotel is not available" in low
                   or "no rooms available" in low)
        markers = (low.count("/night") + low.count("per night")
                   + len(re.findall(r"view rates|select room|book now", low)))
        if markers >= 2 and not soldout:
            log(f"[hyatt] DOM AVAILABLE (markers={markers})")
            return True, "dom"
        if soldout:
            log("[hyatt] DOM SOLD OUT")
            return False, "dom"
        log("[hyatt] indeterminate (Kasada-blocked from datacenter)")
        return None, "indeterminate"
    except Exception as e:
        log(f"[hyatt] error: {e}")
        return None, "error"


# ------------------------------- alerting -----------------------------------
def status_word(v):
    return {True: "AVAILABLE ✅", False: "sold out", None: "couldn't check"}[v]


def build_body(results, modes):
    b, h = results.get("booking"), results.get("hyatt")
    lines = [
        f"@{OWNER_HANDLE} — availability changed for **Lindner Hotel Antwerp** "
        f"({CHECK_IN} → {CHECK_OUT}, {ADULTS} adults).",
        "",
        f"- **Booking.com:** {status_word(b)}  _({modes.get('booking','')})_",
        f"- **Hyatt (direct):** {status_word(h)}  _({modes.get('hyatt','')})_",
        "",
        "**Book now — go straight to Hyatt to book direct (can resell fast):**",
        f"- ⭐ Hyatt (official, dates pre-filled): {HYATT_BOOK_LINK}",
        f"- Booking.com: {BOOKING_LINK}",
        f"- Hotel site: {LINDNER_PAGE}",
        "",
        f"_Checked {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._",
    ]
    return "\n".join(lines)


def create_github_issue(title, body):
    if not (GITHUB_REPO and GITHUB_TOKEN):
        log("[alert] no token/repo; skipping issue")
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues",
        data=json.dumps({"title": title, "body": body}).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "lindner-watcher")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"[alert] issue created (HTTP {r.status})")
            return True
    except urllib.error.HTTPError as e:
        log(f"[alert] issue failed: {e.code} {e.read()[:200]}")
        return False


def send_email(subject, body):
    user, pw = os.environ.get("MAIL_USERNAME"), os.environ.get("MAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO", "jwang815@gmail.com")
    if not (user and pw):
        return False
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        log("[alert] email sent")
        return True
    except Exception as e:
        log(f"[alert] email failed: {e}")
        return False


# --------------------------------- main -------------------------------------
def main():
    now = datetime.now(timezone.utc)
    if now.date().isoformat() >= STOP_ON_OR_AFTER:
        log(f"[guard] {now.date()} >= {STOP_ON_OR_AFTER}; watch window over.")
        return 0

    state = load_state()
    results, modes = {}, {}

    # 1) Booking — primary, reliable, no browser.
    results["booking"], modes["booking"] = check_booking()

    # 2) Hyatt — best-effort via headless browser (often Kasada-blocked).
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                locale="en-US", timezone_id="Europe/Brussels",
                viewport={"width": 1280, "height": 1900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"))
            page = ctx.new_page()
            results["hyatt"], modes["hyatt"] = check_hyatt(page)
            browser.close()
    except Exception as e:
        log(f"[hyatt] playwright unavailable: {e}")
        results["hyatt"], modes["hyatt"] = None, "no-browser"

    log(f"[result] booking={results['booking']}({modes['booking']}) "
        f"hyatt={results['hyatt']}({modes['hyatt']})")

    # Per-source rising-edge detection.
    newly_open = []
    for src in ("booking", "hyatt"):
        prev = state.get(src, "UNKNOWN")
        v = results[src]
        if v is True:
            if prev != "AVAILABLE":
                newly_open.append(src)
            state[src] = "AVAILABLE"
        elif v is False:
            state[src] = "SOLDOUT"
        # None -> leave previous state untouched

    any_open = any(results[s] is True for s in ("booking", "hyatt"))
    last_alert = state.get("last_alert_iso")
    stale = False
    if any_open and last_alert:
        try:
            stale = now - datetime.fromisoformat(last_alert) > timedelta(hours=REPEAT_ALERT_HOURS)
        except Exception:
            stale = True

    should_alert = FORCE_ALERT or bool(newly_open) or (any_open and stale)

    if should_alert:
        if FORCE_ALERT and not any_open:
            title = f"[TEST] 🏨 Lindner Antwerp watcher — pipeline OK ({CHECK_IN}→{CHECK_OUT})"
            body = ("**Forced test alert — the alert pipeline works.** "
                    "Neither source is reporting availability right now.\n\n") + build_body(results, modes)
        else:
            who = ", ".join(s.upper() for s in newly_open) or "UPDATE"
            title = f"🏨 Lindner Antwerp ROOM AVAILABLE ({who}) — {CHECK_IN}→{CHECK_OUT}"
            body = build_body(results, modes)
        create_github_issue(title, body)
        send_email(title, body)
        state["last_alert_iso"] = now.isoformat()
    else:
        log("[alert] none needed.")

    state["updated"] = now.isoformat()
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
