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
    sh_times, sh_dates, sh_get, sh_create, sh_cancel, gcal_add, gcal_delete, gcal_busy,
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
    "• `tot wanneer?` / `laatste` — tot hoe ver de agenda openstaat + de laatste vrije dag.\n"
    "• `boek <tijd>` — boekt die tijd op de laatst getoonde dag, zet 'm in je "
    "agenda én annuleert je vorige afspraak. Bv. `boek 11:00` of `boek zaterdag 14:00`.\n"
    "• `geknipt` / `geweest [datum]` — meld dat je geknipt bent (ook buiten het "
    "systeem om); ik reset dan de 2-weken-teller. Bv. `geknipt` of `geweest gisteren`.\n"
    "• `verzet <tijd>` — je eerstvolgende afspraak verzetten (oude wordt geannuleerd). "
    "Bv. `verzet zaterdag 14:00`.\n"
    "• `hou <datum> in de gaten` — wachtlijst: ik ping zodra er op die dag een plek vrijkomt.\n"
    "• `status` — je geplande afspraken · `annuleer` — afspraak annuleren.\n"
    "• `historie` — je laatste knipbeurten + gemiddelde cadans.\n"
    "• `help` — dit overzicht."
)


# --- booking helpers (shared by text commands + buttons) --------------------
def book_slot(state, bdate, hhmm, force=False):
    """Book bdate@hhmm. Returns (ok, message). Appends to state on success."""
    target = f"{hhmm}:00"
    if target not in sh_times(bdate):
        return False, (f":warning: **{hhmm}** is niet (meer) vrij op {nl_date(bdate)}. "
                       f"Stuur `check {bdate}` voor de actuele tijden.")
    if not force:
        clash = gcal_busy(bdate, hhmm)
        if clash:
            return False, (f":warning: Je hebt om **{hhmm}** op {nl_date(bdate)} al {clash} "
                           f"in je agenda. Toch boeken? Stuur `boek {hhmm} toch`.")
    res = sh_create(bdate, target)
    if res.get("status") != "confirmed":
        return False, f":x: Boeken lukte niet ({res}). Probeer 't via de site."
    try:
        gcal_id = gcal_add(bdate, hhmm)
        agenda = " In je agenda gezet." if gcal_id else ""
    except Exception as e:
        gcal_id, agenda = None, f" (Agenda-event lukte niet: {e})"
    get_appts(state).append({"appointment": res["appointment"], "token": res["token"],
                             "date": bdate, "time": target, "gcal_event_id": gcal_id})
    state.pop("watch_earlier", None)
    save_state(state)
    return True, (f":white_check_mark: Geboekt: **{nl_date(bdate)} om {hhmm}** "
                  f"(knippen bij Alan, €35).{agenda}")


def cancel_appt(state, a):
    """Cancel a tracked appointment dict (needs token). Returns True on success."""
    if not a.get("token"):
        return False
    if sh_cancel(a["appointment"], a["token"]):
        gcal_delete(a.get("gcal_event_id"))
        try:
            get_appts(state).remove(a)
        except ValueError:
            pass
        save_state(state)
        return True
    return False


def slot_buttons(date, slots, limit=10):
    """Discord action-rows of 'boek HH:MM' buttons for a day's slots."""
    rows, row = [], []
    for s in slots[:limit]:
        hhmm = s[:5]
        row.append({"type": 2, "style": 1, "label": f"boek {hhmm}",
                    "custom_id": f"book|{date}|{hhmm}"})
        if len(row) == 5:
            rows.append({"type": 1, "components": row}); row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows


def add_history(state, date_iso):
    h = state.setdefault("history", [])
    if date_iso not in h:
        h.append(date_iso)
        h.sort()


def window_days(target_iso, radius=5):
    """Available days within ±radius days of a target date (soonest first)."""
    t = datetime.date.fromisoformat(target_iso)
    return [d for d in sh_dates(limit=120)
            if abs((datetime.date.fromisoformat(d) - t).days) <= radius]


def hours_until(a):
    """Hours from now (local) until appointment a."""
    appt = datetime.datetime.fromisoformat(f"{a['date']}T{a['time']}")
    now = core.now_local().replace(tzinfo=None)
    return (appt - now).total_seconds() / 3600


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

    # Any URL (e.g. a SalonHub annuleer/edit link) -> never NLU-guess on it.
    if "http" in low or "salonhub.nl" in low:
        discord_post(":information_source: Die SalonHub-link is versleuteld, die kan ik niet "
                     "uitlezen. Wil je annuleren? Stuur dan gewoon `annuleer`.")
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

    if low in ("historie", "geschiedenis", "history", "hoe vaak", "cadans"):
        lc = state.get("last_cut")
        alld = sorted(set(state.get("history", []) + ([lc] if lc else [])))
        if not alld:
            discord_post("Nog geen knipbeurten geregistreerd.")
            return
        lines = "\n".join(f"• {nl_date(d)}" for d in alld[-6:])
        gap = ""
        if len(alld) >= 2:
            ds = [datetime.date.fromisoformat(x) for x in alld]
            gaps = [(ds[i] - ds[i - 1]).days for i in range(1, len(ds))]
            gap = f"\nGemiddeld elke **{round(sum(gaps) / len(gaps))} dagen**."
        discord_post(f":scissors: Je laatste knipbeurten:\n{lines}{gap}")
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
        if hours_until(a) < 24:
            discord_post(f":no_entry: **{nl_date(a['date'])} om {a['time'][:5]}** is binnen 24 uur — "
                         "online annuleren kan dan niet meer. Bel of app Alan om af te zeggen.")
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
        add_history(state, d)
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
                     f"Tik een knop of stuur bv. `boek {slots[0][:5]}`.{extra}",
                     components=slot_buttons(d0, slots))
        return

    # "tot wanneer / laatste plek / hoe ver staat de agenda open"
    if any(k in low for k in ("laatste", "laatstvolgende", "tot wanneer", "hoe ver",
                              "verste", "laatst mogelijk", "agenda open", "hoe lang vooruit",
                              "tot hoe ver", "laatste tijd", "tot welke")):
        dates = sh_dates(limit=120)
        if not dates:
            discord_post("Ik zie momenteel geen vrije plekken bij Alan.")
            return
        first, last = dates[0], dates[-1]
        slots = sh_times(last)
        state["context_date"] = last
        save_state(state)
        msg = (f":calendar: De agenda van Alan staat open van **{nl_date(first)}** "
               f"t/m **{nl_date(last)}** ({len(dates)} dagen met plek).")
        if slots:
            msg += ("\nLaatste vrije dag **" + nl_date(last) + "**: "
                    + ", ".join(s[:5] for s in slots)
                    + f"\nTik een knop of stuur bv. `boek {slots[0][:5]}`.")
        discord_post(msg, components=slot_buttons(last, slots))
        return

    # waitlist: "hou <datum> in de gaten" / "wachtlijst [datum]"
    if "in de gaten" in low or low.startswith(("wachtlijst", "waitlist")):
        wl = state.setdefault("waitlist", {})
        d = parse_date(low)
        if d:
            wl[d] = sh_times(d)  # baseline now -> only alert on NEW openings
            save_state(state)
            discord_post(f":eyes: Ik hou **{nl_date(d)}** in de gaten en ping je zodra er een "
                         f"plek vrijkomt. (`wachtlijst` toont je lijst.)")
        elif wl:
            days = ", ".join(nl_date(x) for x in sorted(wl))
            discord_post(f":eyes: Op je wachtlijst: {days}.")
        else:
            discord_post("Je wachtlijst is leeg. Stuur bv. `hou 14 juni in de gaten`.")
        return

    # "verzet [datum] <tijd>" — reschedule the soonest cancellable appointment
    if "verzet" in low or "verplaats" in low:
        up = [a for a in upcoming(get_appts(state), today().isoformat()) if a.get("token")]
        if not up:
            discord_post("Ik heb geen (door mij geboekte) afspraak om te verzetten.")
            return
        old = up[0]
        if hours_until(old) < 24:
            discord_post(f":no_entry: Je afspraak (**{nl_date(old['date'])} om {old['time'][:5]}**) is "
                         "binnen 24 uur — verzetten/annuleren kan dan niet meer via mij. Bel Alan.")
            return
        ntime = parse_time(low)
        if not ntime:
            discord_post(f"Naar welke tijd wil je **{nl_date(old['date'])} om {old['time'][:5]}** "
                         f"verzetten? Bv. `verzet zaterdag 14:00`.")
            return
        ndate = parse_date(low) or old["date"]
        ok, msg = book_slot(state, ndate, ntime, force=True)
        if ok:
            cancel_appt(state, old)
            msg += f" Verzet vanaf je oude afspraak ({nl_date(old['date'])} om {old['time'][:5]}), die is geannuleerd."
        discord_post(msg)
        return

    date = parse_date(low)
    time_hhmm = parse_time(low)
    wants_book = "boek" in low or (time_hhmm and not date)

    # fuzzy "rond / ongeveer / die week" -> show a WINDOW of open days, not one exact day
    if not wants_book and any(w in low for w in
                              ("rondom", "ongeveer", "ergens", "in de buurt", "die week", "rond")):
        target = date or state.get("context_date") or today().isoformat()
        days = window_days(target, radius=5)
        if not days:
            discord_post(f"Rond **{nl_date(target)}** zie ik geen vrije plekken. "
                         f"Stuur `tot wanneer?` voor de hele agenda.")
            return
        state["context_date"] = days[0]
        save_state(state)
        lines = [f"**{nl_date(d)[:-5]}**: " + ", ".join(s[:5] for s in sh_times(d)[:6]) for d in days[:5]]
        discord_post(f":scissors: Vrije dagen rond **{nl_date(target)}**:\n" + "\n".join(lines)
                     + f"\nBoeken? Stuur bv. `boek {days[0]} 11:00` (of `check {days[0]}`).")
        return

    if date and not wants_book:
        state["context_date"] = date
        save_state(state)
        slots = sh_times(date)
        if slots:
            pretty = ", ".join(s[:5] for s in slots)
            discord_post(f":scissors: Vrije tijden op **{nl_date(date)}**: {pretty}\n"
                         f"Tik een knop of stuur bv. `boek {slots[0][:5]}`.",
                         components=slot_buttons(date, slots))
        else:
            near = window_days(date, radius=6)
            sug = (" Dichtstbij wel vrij: " + ", ".join(nl_date(x)[:-5] for x in near[:4])) if near else ""
            discord_post(f":no_entry_sign: Geen vrije tijden op **{nl_date(date)}** (vaak dicht ma/di)." + sug)
        return

    if wants_book and time_hhmm:
        bdate = date or state.get("context_date")
        if not bdate:
            discord_post("Welke dag bedoel je? Stuur eerst bv. `check 20 juni`, dan `boek 11:00`.")
            return
        force = "toch" in low or "forceer" in low
        ok, msg = book_slot(state, bdate, time_hhmm, force=force)
        discord_post(msg + (" Je andere afspraken blijven staan." if ok else ""))
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
            f"Eerder knippen? Stuur bv. `boek {new[0][:5]}`.",
            components=slot_buttons(ap["date"], new))
    state["watch_earlier"] = earlier
    save_state(state)


def waitlist_run():
    """Watch arbitrary days the user put on the waitlist; ping on new openings."""
    if not core.within_active_hours():
        return
    state = load_state()
    wl = state.get("waitlist", {})
    if not wl:
        return
    tdy = today().isoformat()
    changed = False
    for d in list(wl.keys()):
        if d < tdy:
            del wl[d]
            changed = True
            continue
        cur = sh_times(d)
        new = [s for s in cur if s not in set(wl[d])]
        if new:
            pretty = ", ".join(s[:5] for s in new)
            discord_post(f":eyes: **Wachtlijst — plek vrij op {nl_date(d)}!**\n"
                         f"Nieuw: **{pretty}**\nBoeken? Tik een knop of `boek {new[0][:5]}`.",
                         components=slot_buttons(d, new))
        if cur != wl[d]:
            wl[d] = cur
            changed = True
    if changed:
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


def deadline_reminder():
    """Heads-up while you can still cancel: fires when an appointment is 24-48h out
    (i.e. >24h before it locks). Once per appointment."""
    state = load_state()
    appts = get_appts(state)
    warned = state.setdefault("deadline_warned", [])
    ids = {a["appointment"] for a in appts}
    warned[:] = [w for w in warned if w in ids]  # prune past ones
    changed = False
    for a in appts:
        h = hours_until(a)
        if 24 < h <= 48 and a["appointment"] not in warned:
            appt = datetime.datetime.fromisoformat(f"{a['date']}T{a['time']}")
            deadline = appt - datetime.timedelta(hours=24)
            discord_post(
                f":alarm_clock: **Laatste kans om te annuleren/verzetten.**\n"
                f"Je knipbeurt is **{nl_date(a['date'])} om {a['time'][:5]}**. "
                f"Tot **{nl_date(deadline.date())[:-5]} {deadline.strftime('%H:%M')}** "
                f"(24u van tevoren) kan ik 'm nog annuleren of verzetten — daarna alleen via Alan.")
            warned.append(a["appointment"])
            changed = True
    if changed:
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
        for a in past:
            add_history(state, a["date"])
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
