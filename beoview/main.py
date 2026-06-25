"""
BeoView smart access — lichtgewicht web-server voor slimme deuropener.

  GET  /          web UI (grote "Deur open" knop + live belnotificaties)
  POST /open      opent de deur via DOOR_OPEN_URL
  POST /ring      ontvangt een beldruk-webhook van je videofoon/relay
  GET  /ws        WebSocket voor live push naar de browser
  GET  /history   laatste HISTORY_SIZE belletjes (JSON)

Werkt met elke HTTP-aanroepbare deuropener, bv.:
  Shelly 1 relay  DOOR_OPEN_URL=http://192.168.1.50/relay/0?turn=on&timer=2
  2N intercom     DOOR_OPEN_URL=http://192.168.1.51/api/switch/ctrl?switch=1&action=trigger
  Home Assistant  DOOR_OPEN_URL=http://ha:8123/api/services/switch/turn_on  DOOR_OPEN_METHOD=POST
"""
import os
import json
import datetime
import asyncio
import urllib.request
from collections import deque
from aiohttp import web

# --- config ------------------------------------------------------------------
def _env(key, default=""):
    return os.environ.get(key, default)

DOOR_OPEN_URL     = _env("DOOR_OPEN_URL")
DOOR_OPEN_METHOD  = _env("DOOR_OPEN_METHOD", "GET")
DOOR_OPEN_BODY    = _env("DOOR_OPEN_BODY")        # JSON-string voor POST, bv. {"entity_id":"switch.voordeur"}
DOOR_OPEN_TIMEOUT = int(_env("DOOR_OPEN_TIMEOUT", "5"))
# Verplicht header-geheim voor inkomende beldruk-webhooks (X-Ring-Secret: <waarde>).
RING_SECRET  = _env("RING_SECRET")
# Bearer-token voor POST /open vanuit de browser of curl (leeg = geen auth).
API_SECRET   = _env("API_SECRET")
PORT         = int(_env("PORT", "8080"))
HISTORY_SIZE = int(_env("HISTORY_SIZE", "20"))

# --- state -------------------------------------------------------------------
_ring_history: deque = deque(maxlen=HISTORY_SIZE)
_ws_clients: set = set()


# --- helpers -----------------------------------------------------------------
def _trigger_door():
    if not DOOR_OPEN_URL:
        raise ValueError("DOOR_OPEN_URL is niet ingesteld in .env")
    body_obj = None
    if DOOR_OPEN_BODY:
        try:
            body_obj = json.loads(DOOR_OPEN_BODY)
        except ValueError:
            pass
    data = json.dumps(body_obj).encode() if body_obj is not None else None
    req = urllib.request.Request(DOOR_OPEN_URL, data=data, method=DOOR_OPEN_METHOD)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=DOOR_OPEN_TIMEOUT) as r:
        r.read()


def _now_str():
    return datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S")


# --- HTTP handlers -----------------------------------------------------------
async def handle_index(request):
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    return web.FileResponse(path)


async def handle_open(request):
    if API_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_SECRET}":
            raise web.HTTPUnauthorized(reason="Ongeldige API_SECRET")
    try:
        await asyncio.to_thread(_trigger_door)
        return web.json_response({"ok": True, "message": "Deur open"})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)


async def handle_ring(request):
    if RING_SECRET:
        if request.headers.get("X-Ring-Secret") != RING_SECRET:
            raise web.HTTPForbidden(reason="Ongeldige X-Ring-Secret")
    ts = _now_str()
    _ring_history.appendleft(ts)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_str(ts)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)
    return web.Response(text="ok")


async def handle_history(request):
    return web.json_response(list(_ring_history))


async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        _ws_clients.discard(ws)
    return ws


# --- app factory -------------------------------------------------------------
def make_app():
    app = web.Application()
    app.router.add_get("/",        handle_index)
    app.router.add_post("/open",   handle_open)
    app.router.add_post("/ring",   handle_ring)
    app.router.add_get("/history", handle_history)
    app.router.add_get("/ws",      handle_ws)
    return app


if __name__ == "__main__":
    door_status = f"DOOR_OPEN_URL={DOOR_OPEN_URL!r}" if DOOR_OPEN_URL else "WAARSCHUWING: DOOR_OPEN_URL niet ingesteld"
    print(f"BeoView smart access op poort {PORT}  —  {door_status}", flush=True)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=None)
