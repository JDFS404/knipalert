"""
SnipAlert tasks — the three jobs, all posting in Dutch:

  bot_poll()     : read Discord, handle check/boek/status/help replies
  watcher_run()  : alert if an EARLIER slot opens on your currently-booked day
  reminder_run() : biweekly nudge when you're due for a cut
"""
import datetime
import re
import sys

from . import core
from .core import (
    sh_times, sh_dates, sh_get, sh_create, sh_cancel, gcal_add, gcal_delete,
    discord_post, discord_messages, load_state, save_state,
    parse_date, parse_time, nl_date, next_dow, today, llm_command,
    get_appts, upcoming,
)

DUE_AFTER_DAYS = 10
SAT_AFTERNOON_FROM = "12:00:00"
MAX_SLOTS_SHOWN = 8

HELP = (
    ":scissors: **Zo werkt Knip Alert** 💈\n"
    "• `check <datum>` — vrije knip-tijden op die dag. Bv. `check 20 juni`, "
    "`check 2026-06-20`, `check zaterdag`, `check morgen`, "
    "`check zaterdag over 2 weken`, `check volgende week vrijdag`.\n"
    "• `<datum>` — alleen een datum sturen mag ook.\n"
    "• `wanneer wel?` / `eerstvolgende` — toont de eerstvolgende dag met vrije plek.\n"
    "• `boek <tijd>` — boekt die tijd op de laatst getoonde dag, zet 'm in je "
    "agenda én annuleert je vorige afspraak. Bv. `boek 11:00` of `boek zaterdag 14:00`.\n"
    "• `geknipt` / `geweest [datum]` — meld dat je geknipt bent (ook buiten het "
    "systeem om); ik reset dan de 2-weken-teller. Bv. `geknipt` of `geweest gisteren`.\n"
    "• `status` — je huidige afspraak tonen.\n"
    "• `annuleer` — je huidige afspraak annuleren.\n"
    "• `help` — dit overzicht."
)


# --- bot --------------------------------------------------------------------
def _handle(text, state, allow_llm=True):
    low = text.lower().strip()

    # paste a SalonHub confirmation/cancel link -> register it (becomes cancellable)
    m = re.search(r"appointment:(\d+)-token:([0-9a-zA-Z]+)", text)
    if m:
        aid, tok = int(m.group(1)), m.group(2)
        info = sh_get(aid)
        if info.get("status") != "confirmed" or not info.get("date"):
            discord_post(":warning: Kon die afspraak niet vinden of hij is niet (meer) bevestigd.")
            return
        appts = get_appts(state)
        if any(a["appointment"] == aid for a in appts):
            discord_post("Die afspraak kende ik al. 👍")
            return
        try:
            gcal_id = gcal_add(info["date"], info["time"][:5])
        except Exception:
            gcal_id = None
        appts.append({"appointment": aid, "token": tok, "date": info["date"],
                      "time": info["time"], "gcal_event_id": gcal_id})
        save_state(state)
        discord_post(f":white_check_mark: Afspraak geregistreerd: **{nl_date(info['date'])} "
                     f"om {info['time'][:5]}** — nu ook via mij te annuleren"
                     f"{' en in je agenda gezet' if gcal_id else ''}.")
        return

    if low in ("help", "hulp", "?", "commando's", "commandos"):
        discord_post(HELP)
        return

    if low in ("status", "afspraak"):
        up = upcoming(get_appts(state), today().isoformat())
        if up:
            lines = "\n".join(f"• **{nl_date(a['date'])} om {a['time'][:5]}**" for a in up)
            discord_post(f":calendar: Je geplande afspraken:\n{lines}")
        else:
            discord_post("Je hebt op dit moment geen afspraak bij mij bekend.")
        return

    if any(low.startswith(k) for k in ("annuleer", "cancel", "afzeggen")):
        appts = get_appts(state)
        up = upcoming(appts, today().isoformat())
        if not up:
            discord_post("Je hebt geen afspraak om te annuleren.")
            return
        d = parse_date(low)
        if d:
            targets = [a for a in up if a["date"] == d]
            if not targets:
                discord_post(f"Geen afspraak op {nl_date(d)} gevonden.")
                return
            a = targets[0]
        elif len(up) == 1:
            a = up[0]
        else:
            opts = " / ".join(f"`annuleer {x['date']}`" for x in up)
            discord_post(f"Je hebt meerdere afspraken — welke wil je annuleren? {opts}")
            return
        if not a.get("token"):
            discord_post(f":warning: Die afspraak (**{nl_date(a['date'])} om {a['time'][:5]}**) heb ik "
                         "niet zelf geboekt, dus ik mis de annuleer-code. Annuleer 'm via de site/mail.")
            return
        if sh_cancel(a["appointment"], a["token"]):
            gcal_delete(a.get("gcal_event_id"))
            appts.remove(a)
            state.pop("watch_earlier", None)
            save_state(state)
            discord_post(f":wastebasket: Je afspraak van **{nl_date(a['date'])} "
                         f"om {a['time'][:5]}** is geannuleerd.")
        else:
            discord_post(":warning: Annuleren lukte niet — probeer 't via de site.")
        return

    # "ik ben geweest" — log a cut that wasn't booked via the bot; reset the clock
    if any(k in low for k in ("geknipt", "geweest", "kapper gehad", "knipbeurt gehad")):
        tdy = today().isoformat()
        d = parse_date(low) or tdy
        state["last_cut"] = d
        appts = get_appts(state)
        state["appointments"] = [a for a in appts if a["date"] >= tdy]  # drop past only
        state.pop("watch_earlier", None)
        save_state(state)
        nd = (datetime.date.fromisoformat(d) + datetime.timedelta(days=14)).isoformat()
        discord_post(f":scissors: Genoteerd dat je geknipt bent op **{nl_date(d)}**. "
                     f"Ik por je weer rond **{nl_date(nd)}**.")
        return

    # "wanneer wel / eerstvolgende plek" — search forward for the first open day
    if any(k in low for k in ("wanneer wel", "wanneer kan", "eerstvolgende", "eerst mogelijk",
                              "eerste mogelijk", "vroegste", "snelste", "wat is er vrij",
                              "wat is vrij", "eerste plek", "next available")):
        dates = sh_dates()
        if not dates:
            discord_post("Ik zie de komende ~2 weken geen vrije plekken bij Alan.")
            return
        d0 = dates[0]
        slots = sh_times(d0)
        state["context_date"] = d0
        save_state(state)
        pretty = ", ".join(s[:5] for s in slots)
        extra = ""
        if len(dates) > 1:
            extra = "\nDaarna ook vrij op: " + ", ".join(nl_date(x)[:-5] for x in dates[1:4])
        discord_post(f":scissors: Eerstvolgende plek: **{nl_date(d0)}**: {pretty}\n"
                     f"Boeken? Stuur bv. `boek {slots[0][:5]}`.{extra}")
        return

    date = parse_date(low)
    time_hhmm = parse_time(low)
    wants_book = "boek" in low or (time_hhmm and not date)

    if date and not wants_book:
        state["context_date"] = date
        save_state(state)
        slots = sh_times(date)
        if slots:
            pretty = ", ".join(s[:5] for s in slots)
            discord_post(f":scissors: Vrije tijden op **{nl_date(date)}**: {pretty}\n"
                         f"Boeken? Stuur bv. `boek {slots[0][:5]}`.")
        else:
            discord_post(f":no_entry_sign: Geen vrije tijden op **{nl_date(date)}**.")
        return

    if wants_book and time_hhmm:
        bdate = date or state.get("context_date")
        if not bdate:
            discord_post("Welke dag bedoel je? Stuur eerst bv. `check 20 juni`, dan `boek 11:00`.")
            return
        target = f"{time_hhmm}:00"
        if target not in sh_times(bdate):
            discord_post(f":warning: **{time_hhmm}** is niet (meer) vrij op {nl_date(bdate)}. "
                         f"Stuur `check {bdate}` voor de actuele tijden.")
            return
        res = sh_create(bdate, target)
        if res.get("status") != "confirmed":
            discord_post(f":x: Boeken lukte niet ({res}). Probeer 't via de site.")
            return
        try:
            gcal_id = gcal_add(bdate, time_hhmm)
            agenda = " In je agenda gezet." if gcal_id else ""
        except Exception as e:
            gcal_id, agenda = None, f" (Agenda-event lukte niet: {e})"
        appts = get_appts(state)
        appts.append({"appointment": res["appointment"], "token": res["token"],
                      "date": bdate, "time": target, "gcal_event_id": gcal_id})
        state.pop("watch_earlier", None)
        save_state(state)
        discord_post(f":white_check_mark: Geboekt: **{nl_date(bdate)} om {time_hhmm}** "
                     f"(knippen bij Alan, €35).{agenda} Je andere afspraken blijven staan.")
        return

    # rule parser stumped -> optional Claude Haiku fallback (only if a key is set)
    if allow_llm:
        canon = llm_command(text)
        if canon:
            return _handle(canon, state, allow_llm=False)
    discord_post("Sorry, dat snap ik niet. Stuur `help` voor de mogelijkheden.")


def bot_poll():
    state = load_state()
    if "last_id" not in state:
        msgs = discord_messages()
        state["last_id"] = msgs[0]["id"] if msgs else "0"
        save_state(state)
        return

    msgs = discord_messages(after=state["last_id"])
    for msg in sorted(msgs, key=lambda m: int(m["id"])):
        state["last_id"] = msg["id"]
        if msg.get("webhook_id") or msg.get("author", {}).get("bot"):
            continue
        content = re.sub(r"<@!?\d+>", "", msg.get("content") or "").strip()
        if not content:
            continue
        try:
            _handle(content, state)
        except Exception as e:
            print(f"handle error: {e}", file=sys.stderr)
            try:
                discord_post(f":x: Er ging iets mis: {e}")
            except Exception:
                pass
        save_state(state)
    save_state(state)


# --- earlier-slot watcher ---------------------------------------------------
def watcher_run():
    if not core.within_active_hours():
        return  # don't ping during quiet hours
    state = load_state()
    up = upcoming(get_appts(state), today().isoformat())
    if not up:
        return  # nothing booked
    ap = up[0]  # watch the soonest upcoming appointment
    booked = ap["time"][:5]
    earlier = sorted(s for s in sh_times(ap["date"]) if s[:5] < booked)
    prev = set(state.get("watch_earlier", []))
    new = [s for s in earlier if s not in prev]
    if new:
        pretty = ", ".join(s[:5] for s in new)
        discord_post(
            f":scissors: **Eerder plekje vrij op {nl_date(ap['date'])}!**\n"
            f"Nieuw vóór je {booked}: **{pretty}**\n"
            f"Eerder knippen? Stuur bv. `boek {new[0][:5]}`.")
    state["watch_earlier"] = earlier
    save_state(state)


# --- biweekly reminder ------------------------------------------------------
def _line_for(d, slots):
    if not slots:
        return f"**{nl_date(d)[:-5]}**: (vol)"
    shown = ", ".join(s[:5] for s in slots[:MAX_SLOTS_SHOWN])
    more = " …" if len(slots) > MAX_SLOTS_SHOWN else ""
    return f"**{nl_date(d)[:-5]}**: {shown}{more}"


def morning_reminder():
    """Day-of nudge: if there's an appointment today, remind once."""
    state = load_state()
    tdy_iso = today().isoformat()
    todays = [a for a in get_appts(state) if a["date"] == tdy_iso]
    if todays and state.get("reminded_date") != tdy_iso:
        for a in todays:
            discord_post(f":bell: **Vandaag om {a['time'][:5]} knippen bij Alan!** 💈 Tot zo. ✂️")
        state["reminded_date"] = tdy_iso
        save_state(state)


def reminder_run():
    state = load_state()
    tdy = today()
    tdy_iso = tdy.isoformat()

    appts = get_appts(state)
    past = [a for a in appts if a["date"] < tdy_iso]
    future = [a for a in appts if a["date"] >= tdy_iso]
    if past:
        state["last_cut"] = max(a["date"] for a in past)
        state["appointments"] = future
        state.pop("watch_earlier", None)
        save_state(state)
    if future:
        return  # already booked; nothing to nudge

    last_cut = state.get("last_cut")
    days_since = (tdy - datetime.date.fromisoformat(last_cut)).days if last_cut else 999
    if days_since < DUE_AFTER_DAYS:
        return

    fri, sat, sun = (next_dow(tdy, 4), next_dow(tdy, 5), next_dow(tdy, 6))
    fri_slots = sh_times(fri.isoformat())
    sat_slots = [s for s in sh_times(sat.isoformat()) if s >= SAT_AFTERNOON_FROM]
    sun_slots = sh_times(sun.isoformat())

    since = (f"Het is ~{days_since} dagen geleden." if last_cut
             else "Tijd om er weer eens langs te gaan.")
    discord_post(
        ":scissors: **Tijd voor je knipbeurt bij Alan!** 💈\n"
        f"{since} Vrije tijden op je voorkeursdagen:\n"
        f"{_line_for(fri, fri_slots)}\n"
        f"{_line_for(sat, sat_slots)} _(middag)_\n"
        f"{_line_for(sun, sun_slots)}\n"
        "Boeken? Stuur bv. `boek vrijdag 18:30`, `boek zaterdag 14:00` of `boek zondag 13:00`.")
