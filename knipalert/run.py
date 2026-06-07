"""
Knip Alert runner — Discord Gateway (websocket) for near-instant responses.

  • messages       : pushed in real-time via the Gateway -> handled in <1s
  • earlier-watcher : every 15 min (background task)
  • biweekly nudge  : Wed + Fri at REMINDER_HOUR (background task)

Blocking work (SalonHub / Google / REST posts) runs in a thread so it never
stalls the websocket heartbeat. Keep alive with `restart: unless-stopped`.
"""
import os
import re
import time
import asyncio
import collections
import traceback

import discord

HEARTBEAT_FILE = "/tmp/alive"

from . import core
from . import tasks

WATCH_EVERY = 15 * 60
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "9"))
MORNING_HOUR = int(os.environ.get("REMINDER_MORNING_HOUR", "8"))
REMINDER_DAYS = {2, 4}  # Wed, Fri (Mon=0)
CHANNEL_ID = int(core.DISCORD_CHANNEL)
ALLOWED = {x.strip() for x in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",") if x.strip()}

intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
client = discord.Client(intents=intents)

_lock = asyncio.Lock()
_seen = collections.deque(maxlen=300)
tree = discord.app_commands.CommandTree(client)


async def _run_text(interaction, text):
    """Run an equivalent text command (posts to the channel) and ack the slash."""
    if ALLOWED and str(interaction.user.id) not in ALLOWED:
        await interaction.response.send_message("Niet gemachtigd.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with _lock:
        state = core.load_state()
        try:
            await asyncio.to_thread(tasks._handle, text, state)
        except Exception:
            traceback.print_exc()
    await interaction.followup.send("✓ verstuurd", ephemeral=True)


@tree.command(name="check", description="Vrije kniptijden op een dag")
@discord.app_commands.describe(datum="bv. zaterdag, 20 juni, 2026-06-20, morgen")
async def _sc_check(interaction, datum: str):
    await _run_text(interaction, f"check {datum}")


@tree.command(name="boek", description="Boek een tijd (op de laatst getoonde of opgegeven dag)")
@discord.app_commands.describe(tijd="bv. 11:00", datum="optioneel, bv. zaterdag")
async def _sc_boek(interaction, tijd: str, datum: str = ""):
    await _run_text(interaction, f"boek {datum} {tijd}".strip())


@tree.command(name="verzet", description="Verzet je eerstvolgende afspraak")
@discord.app_commands.describe(tijd="nieuwe tijd, bv. 14:00", datum="optioneel, bv. zaterdag")
async def _sc_verzet(interaction, tijd: str, datum: str = ""):
    await _run_text(interaction, f"verzet {datum} {tijd}".strip())


@tree.command(name="wanneer", description="Eerstvolgende vrije dag")
async def _sc_wanneer(interaction):
    await _run_text(interaction, "eerstvolgende")


@tree.command(name="wachtlijst", description="Hou een dag in de gaten voor openingen")
@discord.app_commands.describe(datum="bv. 14 juni (leeg = toon je wachtlijst)")
async def _sc_wachtlijst(interaction, datum: str = ""):
    await _run_text(interaction, (f"hou {datum} in de gaten" if datum else "wachtlijst"))


@tree.command(name="status", description="Je geplande afspraken")
async def _sc_status(interaction):
    await _run_text(interaction, "status")


@tree.command(name="historie", description="Je laatste knipbeurten + cadans")
async def _sc_historie(interaction):
    await _run_text(interaction, "historie")


@tree.command(name="annuleer", description="Annuleer een afspraak")
@discord.app_commands.describe(datum="optioneel, bv. 2026-06-19")
async def _sc_annuleer(interaction, datum: str = ""):
    await _run_text(interaction, f"annuleer {datum}".strip())


@tree.command(name="help", description="Overzicht van commando's")
async def _sc_help(interaction):
    await _run_text(interaction, "help")


@client.event
async def on_ready():
    print(f"Knip Alert gateway connected as {client.user} "
          f"(channel {CHANNEL_ID}, gcal={'on' if core.GCAL_ENABLED else 'off'})", flush=True)
    client.loop.create_task(watch_loop())
    client.loop.create_task(reminder_loop())
    client.loop.create_task(heartbeat_loop())
    client.loop.create_task(watchdog_loop())
    client.loop.create_task(backup_loop())
    # register slash commands (needs the bot invited with applications.commands scope)
    for g in client.guilds:
        try:
            await tree.sync(guild=g)
            print(f"slash commands synced to guild {g.id}", flush=True)
        except Exception:
            traceback.print_exc()


@client.event
async def on_interaction(interaction):
    # one-click "boek HH:MM" buttons
    try:
        if interaction.type != discord.InteractionType.component:
            return
        if ALLOWED and str(interaction.user.id) not in ALLOWED:
            await interaction.response.send_message("Niet gemachtigd.", ephemeral=True)
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("book|"):
            return
        _, bdate, hhmm = cid.split("|", 2)
        await interaction.response.defer()
        async with _lock:
            state = core.load_state()
            ok, msg = await asyncio.to_thread(tasks.book_slot, state, bdate, hhmm)
        await interaction.followup.send(msg)
    except Exception:
        traceback.print_exc()


@client.event
async def on_message(msg):
    if msg.author.bot or msg.webhook_id:
        return
    if msg.channel.id != CHANNEL_ID:
        return
    if ALLOWED and str(msg.author.id) not in ALLOWED:
        return  # only authorised users may command the bot
    if msg.id in _seen:
        return  # already handled (gateway re-delivery)
    _seen.append(msg.id)
    content = re.sub(r"<@!?\d+>", "", msg.content or "").strip()
    if not content:
        return
    async with _lock:  # one message at a time -> no state clobbering
        state = core.load_state()
        try:
            await asyncio.to_thread(tasks._handle, content, state)
        except Exception:
            traceback.print_exc()
        core.save_state(state)


async def heartbeat_loop():
    # touch a file while connected -> Docker HEALTHCHECK marks unhealthy if it goes stale
    while True:
        try:
            if client.is_ready():
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(str(int(time.time())))
        except Exception:
            pass
        await asyncio.sleep(30)


async def watch_loop():
    while True:
        for fn in (tasks.watcher_run, tasks.waitlist_run):
            try:
                await asyncio.to_thread(fn)
            except Exception:
                traceback.print_exc()
        await asyncio.sleep(WATCH_EVERY)


async def watchdog_loop():
    # self-heal: if disconnected from Discord for too long, exit -> restart:unless-stopped
    down_since = None
    while True:
        await asyncio.sleep(60)
        if client.is_ready() and not client.is_closed():
            down_since = None
        else:
            down_since = down_since or time.time()
            if time.time() - down_since > 300:
                print("watchdog: disconnected >5min, exiting for restart", flush=True)
                os._exit(1)


async def backup_loop():
    last = None
    while True:
        d = core.today()
        if d != last:
            try:
                await asyncio.to_thread(core.backup_state)
                last = d
            except Exception:
                traceback.print_exc()
        await asyncio.sleep(3600)


async def reminder_loop():
    nudge_done = None
    morning_done = None
    while True:
        now = core.now_local()
        if now.hour == MORNING_HOUR and morning_done != now.date():
            try:
                await asyncio.to_thread(tasks.morning_reminder)
                morning_done = now.date()
            except Exception:
                traceback.print_exc()
        if (now.weekday() in REMINDER_DAYS and now.hour == REMINDER_HOUR
                and nudge_done != now.date()):
            try:
                await asyncio.to_thread(tasks.reminder_run)
                nudge_done = now.date()
            except Exception:
                traceback.print_exc()
        await asyncio.sleep(60)


def main():
    client.run(core.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
