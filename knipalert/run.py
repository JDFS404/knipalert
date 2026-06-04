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
import asyncio
import collections
import traceback

import discord

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


@client.event
async def on_ready():
    print(f"Knip Alert gateway connected as {client.user} "
          f"(channel {CHANNEL_ID}, gcal={'on' if core.GCAL_ENABLED else 'off'})", flush=True)
    client.loop.create_task(watch_loop())
    client.loop.create_task(reminder_loop())


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


async def watch_loop():
    while True:
        try:
            await asyncio.to_thread(tasks.watcher_run)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(WATCH_EVERY)


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
