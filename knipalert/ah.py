"""Albert Heijn API client — leest je eigen accountdata via de community-gedocumenteerde
OAuth2-flow die ook de Appie-app gebruikt.

Vereiste env-vars:
  AH_USERNAME      — je AH-accounte-mailadres
  AH_PASSWORD      — je AH-wachtwoord
  AH_CLIENT_SECRET — zie .env.example voor hoe je deze ophaalt

Optioneel:
  AH_CLIENT_ID     — standaard 'appie-android'
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import env

_AUTH_URL = "https://api.ah.nl/mobile-auth/v2/auth/token"
_BASE_URL = "https://api.ah.nl"
_UA = "Appie/8.22.3"

AH_USERNAME = env("AH_USERNAME", "")
AH_PASSWORD = env("AH_PASSWORD", "")
AH_CLIENT_ID = env("AH_CLIENT_ID", "appie-android")
AH_CLIENT_SECRET = env("AH_CLIENT_SECRET", "")


def enabled():
    return bool(AH_USERNAME and AH_PASSWORD and AH_CLIENT_SECRET)


# --- HTTP helpers ------------------------------------------------------------

def _form_post(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}") from e


def _get(path, token):
    req = urllib.request.Request(_BASE_URL + path, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} op {path}") from e


# --- Auth --------------------------------------------------------------------

def _authenticate():
    if not enabled():
        raise ValueError(
            "Stel AH_USERNAME, AH_PASSWORD en AH_CLIENT_SECRET in via .env. "
            "Zie .env.example voor instructies."
        )
    return _form_post(_AUTH_URL, {
        "client_id": AH_CLIENT_ID,
        "client_secret": AH_CLIENT_SECRET,
        "grant_type": "password",
        "username": AH_USERNAME,
        "password": AH_PASSWORD,
    })


def _do_refresh(refresh_token):
    return _form_post(_AUTH_URL, {
        "client_id": AH_CLIENT_ID,
        "client_secret": AH_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })


def get_token(state):
    """Geeft een geldig access token; slaat token + expiry op in state['ah_auth'].
    Probeert eerst de refresh token; doet een volledige login als dat mislukt."""
    ah = state.setdefault("ah_auth", {})
    now = time.time()

    if ah.get("access_token") and now < ah.get("expires_at", 0) - 60:
        return ah["access_token"]

    if ah.get("refresh_token"):
        try:
            r = _do_refresh(ah["refresh_token"])
            _store(ah, r, now)
            return ah["access_token"]
        except Exception:
            pass  # refresh verlopen → opnieuw inloggen

    r = _authenticate()
    _store(ah, r, now)
    return ah["access_token"]


def _store(ah, r, now):
    ah["access_token"] = r["access_token"]
    ah["refresh_token"] = r.get("refresh_token") or ah.get("refresh_token", "")
    ah["expires_at"] = now + r.get("expires_in", 7200)


# --- API calls ---------------------------------------------------------------

def get_receipts(token, limit=10):
    """Lijst van bonnen (headers, niet gedetailleerd)."""
    data = _get(f"/ms/nl/receipts?limit={limit}", token)
    return data.get("receipts", [])


def get_receipt_detail(token, transaction_id):
    """Volledige bon met artikelen."""
    return _get(f"/ms/nl/receipts/{transaction_id}", token)


def get_profile(token):
    """Profiel incl. bonuskaart-info."""
    return _get("/ms/nl/customers/v1/profile", token)
