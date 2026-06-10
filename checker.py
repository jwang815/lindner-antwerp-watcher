#!/usr/bin/env python3
"""
Lindner Hotel Antwerp room-availability watcher.

Checks the stay 2026-07-16 -> 2026-07-20 (2 adults, 1 room) on TWO sources,
independently, because OTA and direct inventory can differ:

  1. Hyatt.com  -- the hotel's own engine (Lindner Antwerp = JdV by Hyatt).
                   Authoritative / where you will actually book direct.
                   Sits behind Kasada bot-protection that often blocks
                   datacenter IPs; we load the real page in a headless browser
                   and read the rendered room list, and also try the JSON API.
  2. Trip.com   -- reliable from the cloud; good corroborating signal.

On a rising edge (sold out -> available) on EITHER source it opens a GitHub
issue that @mentions you (GitHub turns that into an email) and, if Gmail SMTP
secrets are set, sends a direct email too. Per-source state in state.json means
one alert per opening, not one per check.
"""

import json
import os
import re
import sys
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

TRIP_URL = (
    "https://us.trip.com/hotels/antwerp-hotel-detail-2195779/"
    "lindner-wtc-hotel-and-city-lounge/"
    f"?checkIn={CHECK_IN}&checkOut={CHECK_OUT}&adult={ADULTS}&children={KIDS}&curr=EUR"
)
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


# --------------------------- source: Hyatt ----------------------------------
def check_hyatt(page):
    """(available|None, mode). None = blocked/indeterminate."""
    try:
        page.goto(HYATT_ROOMS_PAGE, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(7000)

        # (a) authoritative JSON via in-page fetch (shares Kasada cookies)
        try:
            res = page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, {headers: {accept: 'application/json'}});
                        return {status: r.status, body: (await r.text()).slice(0, 20000)};
                    } catch (e) { return {status: -1, body: String(e)}; }
                }""",
                HYATT_JSON,
            )
            if res.get("status") == 200 and res.get("body", "").strip().startswith("{"):
                data = json.loads(res["body"])
                rr = data.get("roomRates") or {}
                info = json.dumps(data.get("responseInfo") or {})
                avail = len(rr) > 0 and "soldOut" not in info
                log(f"[hyatt] JSON authoritative: roomRates={len(rr)} available={avail}")
                return avail, "json"
        except Exception as e:
            log(f"[hyatt] json attempt error: {e}")

        # (b) rendered DOM fallback
        text = (page.inner_text("body") or "")
        low = text.lower()
        if any(k in low for k in ["are you a robot", "verify you are human", "captcha",
                                  "request unsuccessful", "access denied"]):
            log("[hyatt] DOM blocked (bot-check)")
            return None, "blocked"
        soldout = ("not available during those dates" in low
                   or "this hotel is not available" in low
                   or "no rooms available" in low)
        avail_markers = (low.count("/night") + low.count("per night")
                         + len(re.findall(r"view rates|select room|book now", low)))
        # a real room list shows several rate cards + nightly pricing
        if avail_markers >= 2 and not soldout:
            log(f"[hyatt] DOM AVAILABLE (markers={avail_markers})")
            return True, "dom"
        if soldout:
            log("[hyatt] DOM SOLD OUT")
            return False, "dom"
        log("[hyatt] DOM indeterminate (likely Kasada-blocked from datacenter)")
        return None, "indeterminate"
    except Exception as e:
        log(f"[hyatt] error: {e}")
        return None, "error"


# --------------------------- source: Trip.com -------------------------------
def check_trip(page):
    try:
        page.goto(TRIP_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(9000)
        text = (page.inner_text("body") or "")
        low = text.lower()
        if any(k in low for k in ["are you a robot", "verify you are human", "px-captcha", "captcha"]):
            log("[trip] blocked")
            return None, "blocked"
        reserve = 0
        for name in ("Reserve", "Book", "Select"):
            try:
                reserve += page.get_by_role("button", name=re.compile(name, re.I)).count()
            except Exception:
                pass
        has_total = bool(re.search(r"total price", low))
        soldout = ("not currently accepting bookings" in low) or ("view other hotels" in low)
        if reserve > 0 or has_total:
            m = re.search(r"€\s?([0-9][0-9.,]{1,7})", text)
            price = ("€" + m.group(1)) if m else None
            log(f"[trip] AVAILABLE (reserve={reserve}, total={has_total}, price={price})")
            return True, f"reserve={reserve} price={price}"
        if soldout:
            log("[trip] SOLD OUT")
            return False, "sold out"
        log("[trip] indeterminate")
        return None, "indeterminate"
    except Exception as e:
        log(f"[trip] error: {e}")
        return None, "error"


# ------------------------------- alerting -----------------------------------
def status_word(v):
    return {True: "AVAILABLE ✅", False: "sold out", None: "couldn't check"}[v]


def build_body(results, modes):
    h, t = results.get("hyatt"), results.get("trip")
    lines = [
        f"@{OWNER_HANDLE} — availability changed for **Lindner Hotel Antwerp**.",
        "",
        f"- **Stay:** {CHECK_IN} → {CHECK_OUT} ({ADULTS} adults, {ROOMS} room)",
        f"- **Hyatt (direct / book here):** {status_word(h)}  _({modes.get('hyatt','')})_",
        f"- **Trip.com:** {status_word(t)}  _({modes.get('trip','')})_",
        "",
        "**Book now — it can sell out again fast:**",
        f"- Hyatt (official, your dates pre-filled): {HYATT_BOOK_LINK}",
        f"- Trip.com: {TRIP_URL}",
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
        data=json.dumps({"title": title, "body": body}).encode(), method="POST",
    )
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

    from playwright.sync_api import sync_playwright

    state = load_state()
    results, modes = {}, {}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(
            locale="en-US", timezone_id="Europe/Brussels",
            viewport={"width": 1280, "height": 1900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        results["hyatt"], modes["hyatt"] = check_hyatt(page)
        results["trip"], modes["trip"] = check_trip(page)
        browser.close()

    log(f"[result] hyatt={results['hyatt']}({modes['hyatt']}) "
        f"trip={results['trip']}({modes['trip']})")

    # Per-source rising-edge detection.
    newly_open = []
    for src in ("hyatt", "trip"):
        prev = state.get(src, "UNKNOWN")
        v = results[src]
        if v is True:
            if prev != "AVAILABLE":
                newly_open.append(src)
            state[src] = "AVAILABLE"
        elif v is False:
            state[src] = "SOLDOUT"
        # v is None -> leave previous state untouched (don't lose memory on a blocked run)

    # Repeat-reminder if something is still open and it's been a while.
    any_open = any(results[s] is True for s in ("hyatt", "trip"))
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
                    "Both sources currently sold out.\n\n") + build_body(results, modes)
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
