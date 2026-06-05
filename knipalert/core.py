"""
SnipAlert core — config, SalonHub + Discord HTTP, state, NL date/time parsing,
and Google Calendar helpers. Shared by the bot / watcher / reminder tasks.

All configuration comes from environment variables (see .env.example).
"""
import os
import re
import json
import base64
import datetime
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


# --- config -----------------------------------------------------------------
def env(key, default=None, required=False):
    v = os.environ.get(key, default)
    if required and not v:
        raise SystemExit(f"Missing required env var: {key}")
    return v


CLIENT = env("SALONHUB_CLIENT", "barbershopalan")
SALON = env("SALONHUB_SALON", "amsterdam")
TREATMENT = int(env("SALONHUB_TREATMENT", "1"))     # Haircut (€35)
EMPLOYEE = int(env("SALONHUB_EMPLOYEE", "1"))       # Alan
SALONHUB_KEY = env("SALONHUB_KEY", "663c0816-c2fa-4895-be64-a5af94d8ece0")
SALONHUB_API = "https://public.salonhub.nl/v2/api/"

DISCORD_TOKEN = env("DISCORD_BOT_TOKEN", required=True)
DISCORD_CHANNEL = env("DISCORD_CHANNEL_ID", required=True)
DISCORD_API = "https://discord.com/api/v10"

CUSTOMER = {
    "name": env("CUSTOMER_NAME", ""),
    "email": env("CUSTOMER_EMAIL", ""),
    "telephone": env("CUSTOMER_PHONE", ""),
}

TIMEZONE = env("TIMEZONE", "Europe/Amsterdam")
DURATION_MIN = int(env("DURATION_MIN", "30"))
STATE_PATH = env("STATE_PATH", "/data/state.json")

GCAL_ENABLED = env("GCAL_ENABLED", "true").lower() == "true"
GCAL_CALENDAR_ID = env("GCAL_CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS = env("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/gcal-sa.json")
# Service-account key as base64 of the JSON (preferred for Komodo/env-only deploys).
# Falls back to the file at GOOGLE_APPLICATION_CREDENTIALS if unset.
GOOGLE_CREDENTIALS_B64 = env("GOOGLE_CREDENTIALS_B64", "")

LOCATION = env("BARBER_LOCATION", "Bilderdijkstraat 92H, 1053KX Amsterdam")
BOOK_URL = "https://widget.salonhub.nl/a/barbershopalan/amsterdam/link.html"

# Branding for Discord embeds (images hosted on the salon's own site)
BRAND_NAME = "Barbershop Alan"
BRAND_COLOR = 0xE23B3B  # barbershop red
BRAND_PHOTO = "https://barbershopalan.nl/wp-content/uploads/2024/08/11212.jpg"
BRAND_LOGO = "https://barbershopalan.nl/wp-content/uploads/2024/08/Logo-w.png"
# Icon shown on every embed (GitHub-hosted cartoon); override via BRAND_ICON_URL.
BRAND_ICON = env("BRAND_ICON_URL",
                 "https://cdn.jsdelivr.net/gh/JDFS404/knipalert@main/assets/snipalert-bot-icon.png")

# Optional Claude Haiku fallback (only used when the rule parser is stumped).
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-haiku-4-5")

MONTHS_NL = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
WEEKDAYS_NL = {  # Monday=0
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3, "vrijdag": 4,
    "zaterdag": 5, "zondag": 6, "ma": 0, "di": 1, "wo": 2, "do": 3, "vr": 4,
    "za": 5, "zo": 6,
}
NUM_NL = {
    "een": 1, "één": 1, "1": 1, "twee": 2, "2": 2, "drie": 3, "3": 3,
    "vier": 4, "4": 4, "vijf": 5, "5": 5, "zes": 6, "6": 6,
}
DOW_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


# --- time helpers -----------------------------------------------------------
def now_local():
    if ZoneInfo:
        return datetime.datetime.now(ZoneInfo(TIMEZONE))
    return datetime.datetime.now()


QUIET_START = int(env("QUIET_START_HOUR", "8"))
QUIET_END = int(env("QUIET_END_HOUR", "22"))


def within_active_hours():
    """True during waking hours — proactive pings stay quiet outside this window."""
    return QUIET_START <= now_local().hour < QUIET_END


def today():
    return now_local().date()


# --- generic HTTP -----------------------------------------------------------
def http(method, url, headers=None, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


# --- SalonHub ---------------------------------------------------------------
def _sh_headers():
    return {"Authorization": f"Bearer {SALONHUB_KEY}", "Accept-Language": "nl",
            "Content-Type": "application/json", "User-Agent": "SnipAlert/1.0"}


def sh_times(date):
    url = (f"{SALONHUB_API}OnlineAppointment.Remote.Times/get"
           f"?client={CLIENT}&salon={SALON}&treatment={TREATMENT}"
           f"&employee={EMPLOYEE}&date={date}")
    return sorted(t["time"] for t in http("GET", url, headers=_sh_headers()).get("times", []))


def sh_dates(limit=14):
    """Upcoming dates that have availability (soonest first)."""
    url = (f"{SALONHUB_API}OnlineAppointment.Remote.Dates/get"
           f"?client={CLIENT}&salon={SALON}&treatment={TREATMENT}"
           f"&employee={EMPLOYEE}&limit={limit}")
    return [d["date"] for d in http("GET", url, headers=_sh_headers()).get("dates", [])]


def sh_get(appointment):
    url = (f"{SALONHUB_API}OnlineAppointment.Remote.Appointments/get"
           f"?client={CLIENT}&salon={SALON}&appointment={appointment}")
    try:
        return http("GET", url, headers=_sh_headers())
    except Exception:
        return {}


def sh_create(date, time_hms):
    if not all(CUSTOMER.values()):
        raise SystemExit("Customer details (CUSTOMER_NAME/EMAIL/PHONE) not set")
    body = {
        "settings": {"application": "nl.salonhub.widget", "guid": "",
                     "email": {"enabled": True, "urls": {
                         "cancel": "https://barbershopalan.nl/#salonhub-cancel-client:{CLIENT}-salon:{SALON}-appointment:{APPOINTMENT}-token:{TOKEN}",
                         "verify": "https://barbershopalan.nl/#salonhub-confirm-client:{CLIENT}-salon:{SALON}-appointment:{APPOINTMENT}-code:{CODE}"}}},
        "appointment": {"date": date, "treatments": [
            {"time": time_hms, "employee": EMPLOYEE, "treatment": TREATMENT}]},
        "customer": dict(CUSTOMER),
    }
    url = (f"{SALONHUB_API}OnlineAppointment.Remote.Appointments/create"
           f"?client={CLIENT}&salon={SALON}")
    return http("POST", url, headers=_sh_headers(), body=body)


def sh_cancel(appointment, token):
    url = (f"{SALONHUB_API}OnlineAppointment.Remote.Appointments/cancel"
           f"?client={CLIENT}&salon={SALON}&appointment={appointment}&token={token}")
    try:
        http("GET", url, headers=_sh_headers())
        return True
    except Exception:
        return False


# --- Discord ----------------------------------------------------------------
def _d_headers():
    return {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json",
            "User-Agent": "SnipAlert/1.0"}


def discord_messages(after=None, limit=50):
    url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL}/messages?limit={limit}"
    if after:
        url += f"&after={after}"
    return http("GET", url, headers=_d_headers())


def discord_post(content, image=None):
    embed = {
        "color": BRAND_COLOR,
        "author": {"name": BRAND_NAME, "icon_url": BRAND_ICON},
        "description": content,
    }
    if image:
        embed["image"] = {"url": image}
    http("POST", f"{DISCORD_API}/channels/{DISCORD_CHANNEL}/messages",
         headers=_d_headers(), body={"embeds": [embed]})


# --- state ------------------------------------------------------------------
def load_state():
    for path in (STATE_PATH, STATE_PATH + ".bak"):
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    try:
        if os.path.exists(STATE_PATH):
            os.replace(STATE_PATH, STATE_PATH + ".bak")
    except Exception:
        pass
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def get_appts(state):
    """Appointment list, migrating the legacy single-appointment field."""
    appts = state.get("appointments")
    if appts is None:
        appts = [state["appointment"]] if state.get("appointment") else []
        state["appointments"] = appts
        state.pop("appointment", None)
    return appts


def upcoming(appts, today_iso):
    return sorted([a for a in appts if a["date"] >= today_iso],
                  key=lambda a: (a["date"], a["time"]))


# --- NL parsing -------------------------------------------------------------
def next_dow(base, target_dow, strict_future=True):
    delta = (target_dow - base.weekday()) % 7
    if strict_future:
        delta = delta or 7
    return base + datetime.timedelta(days=delta)


def parse_date(text, ref=None):
    ref = ref or today()
    t = text.lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(0)
    if "overmorgen" in t:
        return (ref + datetime.timedelta(days=2)).isoformat()
    if "morgen" in t:
        return (ref + datetime.timedelta(days=1)).isoformat()
    if "vandaag" in t:
        return ref.isoformat()
    if "eergisteren" in t:
        return (ref - datetime.timedelta(days=2)).isoformat()
    if "gisteren" in t:
        return (ref - datetime.timedelta(days=1)).isoformat()
    m = re.search(r"\b(\d{1,2})\s+([a-z]+)", t)                       # "20 juni"
    if m and m.group(2) in MONTHS_NL:
        day, mon = int(m.group(1)), MONTHS_NL[m.group(2)]
        year = ref.year + (1 if mon < ref.month else 0)
        try:
            return datetime.date(year, mon, day).isoformat()
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?", t)   # dd-mm(-yyyy)
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else ref.year + (1 if mon < ref.month else 0)
        if year < 100:
            year += 2000
        try:
            return datetime.date(year, mon, day).isoformat()
        except ValueError:
            return None

    # relative offsets: "over 2 weken", "over 3 dagen", "volgende week"
    extra = 0
    mw = re.search(r"over\s+(\w+)\s+(?:weken|week)\b", t)
    if mw and mw.group(1) in NUM_NL:
        extra += NUM_NL[mw.group(1)] * 7
    md = re.search(r"over\s+(\w+)\s+(?:dagen|dag)\b", t)
    if md and md.group(1) in NUM_NL:
        extra += NUM_NL[md.group(1)]
    if "volgende week" in t:
        extra += 7

    for name, dow in WEEKDAYS_NL.items():                            # weekday (+offset)
        if re.search(rf"\b{name}\b", t):
            return (next_dow(ref, dow) + datetime.timedelta(days=extra)).isoformat()

    if extra:
        return (ref + datetime.timedelta(days=extra)).isoformat()
    return None


def parse_time(text):
    t = text.lower()
    m = re.search(r"\b(\d{1,2})[:.u](\d{2})\b", t)       # 11:00 / 11.00 / 11u00
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2})\s*u(?:ur)?\b", t)        # 11u / 11 uur
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def nl_date(date):
    d = date if isinstance(date, datetime.date) else datetime.date.fromisoformat(date)
    return f"{DOW_NL[d.weekday()]} {d.day}-{d.month:02d}-{d.year}"


# --- optional Claude Haiku fallback -----------------------------------------
def llm_command(text):
    """When the rule parser is stumped, ask Claude Haiku to normalise the message
    into a canonical command string (e.g. 'boek 2026-06-20 14:00'). Returns None
    if disabled or on any error."""
    if not ANTHROPIC_API_KEY:
        return None
    tdy = today()
    system = (
        "Je zet een Nederlands bericht voor een kappers-boekingsbot om naar JSON.\n"
        f"Vandaag is {tdy.isoformat()} ({DOW_NL[tdy.weekday()]}), tijdzone Europe/Amsterdam.\n"
        "Velden: action ('check'|'next'|'last'|'boek'|'geweest'|'status'|'annuleer'|'help'|'none'), "
        "date (YYYY-MM-DD of null), time (HH:MM of null). Reken relatieve datums uit "
        "(bv. 'volgende week vrijdag', 'over 2 weken'). Gebruik 'next' bij vage "
        "beschikbaarheidsvragen zonder concrete dag (bv. 'wanneer kan ik', 'wat is vrij', "
        "'eerste plek', 'wanneer wel'). Gebruik 'last' bij vragen over hoe ver vooruit / de "
        "laatste mogelijkheid (bv. 'tot wanneer', 'laatste plek', 'hoe ver staat de agenda open'). "
        "Wees ruimhartig: kies liever 'check'/'next'/'last' dan 'none'. Antwoord met UITSLUITEND JSON."
    )
    body = {"model": ANTHROPIC_MODEL, "max_tokens": 200, "system": system,
            "messages": [{"role": "user", "content": text}]}
    try:
        r = http("POST", "https://api.anthropic.com/v1/messages",
                 headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                          "content-type": "application/json"}, body=body)
        raw = r["content"][0]["text"]
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0) if m else raw)
    except Exception:
        return None
    a = (d.get("action") or "none").lower()
    date, tm = d.get("date"), d.get("time")
    if a == "next":
        return "eerstvolgende"
    if a == "last":
        return "laatste"
    if a == "check" and date:
        return f"check {date}"
    if a == "boek" and tm:
        return f"boek {date} {tm}" if date else f"boek {tm}"
    if a == "geweest":
        return f"geweest {date}" if date else "geweest"
    if a in ("status", "annuleer", "help"):
        return a
    return None


# --- Google Calendar (service account) --------------------------------------
def _gcal_service():
    from googleapiclient.discovery import build
    from google.oauth2 import service_account
    scopes = ["https://www.googleapis.com/auth/calendar"]
    if GOOGLE_CREDENTIALS_B64:
        info = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_B64))
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDENTIALS, scopes=scopes)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def gcal_add(date, hhmm):
    """Create a 30-min event; returns the event id (or None if calendar disabled)."""
    if not GCAL_ENABLED:
        return None
    hh, mm = (int(x) for x in hhmm.split(":"))
    start = datetime.datetime.fromisoformat(date).replace(hour=hh, minute=mm)
    end = start + datetime.timedelta(minutes=DURATION_MIN)
    event = {
        "summary": "Knippen bij Barbershop Alan",
        "location": LOCATION,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    ev = _gcal_service().events().insert(calendarId=GCAL_CALENDAR_ID, body=event).execute()
    return ev.get("id")


def gcal_delete(event_id):
    if not (GCAL_ENABLED and event_id):
        return
    try:
        _gcal_service().events().delete(calendarId=GCAL_CALENDAR_ID, eventId=event_id).execute()
    except Exception:
        pass
