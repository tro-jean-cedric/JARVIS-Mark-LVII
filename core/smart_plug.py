"""
core/smart_plug.py — switch the smart plug the home computer is plugged into,
through the plug maker's own cloud, so the always-on server (core/cloud.py)
can start a computer that is completely off.

    ok, text = smart_plug.check(cfg)        # credentials and device, switches NOTHING
    ok, text = smart_plug.cycle(cfg)        # off, a pause, on — after the user confirmed

WHY OFF-THEN-ON
    A computer starts from a plug only when its firmware is set to power on
    when the power comes back ("Restore on AC power loss: Power On" or
    similar). Switching a plug ON that is already on changes nothing, so
    waking is: off, POWER_PAUSE_S seconds, on. If the computer was in fact
    still running, that cuts its power — which is why core/cloud.py only
    does it after the user said yes twice on the phone (core.confirm), and
    only when Wake-on-LAN or a wake URL, which are harmless, are not set up.
    Whether the computer really came back is not decided here: the server
    waits for the desktop to check in, as for every other wake.

SERVICES (the user's own account and keys)
    shelly       Shelly Cloud, the newer device API:
                 POST https://<server>.shelly.cloud/v2/devices/api/set/switch?auth_key=…
                 {"id": <device id>, "channel": <n>, "on": true|false}
                 The server name (Settings → Authorization cloud key in the
                 Shelly app) must end in .shelly.cloud — the key is never sent
                 anywhere else.
    tuya         Tuya / Smart Life through a Tuya IoT cloud project
                 (Access ID + Secret, the device linked to the project):
                 signed OpenAPI requests, a token, then
                 POST /v1.0/devices/<id>/commands  {"commands": [{"code": "switch_1", "value": …}]}
                 The switch's code is read from the device's status.
    voicemonkey  Voice Monkey (Alexa): one trigger runs an Alexa routine the
                 user built to switch the plug off, wait and on again
                 (https://api-v3.voicemonkey.io/trigger?token=…&device=…). It
                 cannot be checked without running the routine, so check()
                 only looks at the settings.

Standard library only: this file runs on a bare server (core/cloud.py MANIFEST).
Keys and tokens go only where each service expects them, and never into a log
line or an error message — errors name the HTTP status and nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

KINDS = ("shelly", "tuya", "voicemonkey")
TIMEOUT_S = 15
POWER_PAUSE_S = 10.0

TUYA_REGIONS = {
    "eu": "https://openapi.tuyaeu.com",
    "eu-w": "https://openapi-weaz.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "us-e": "https://openapi-ueaz.tuyaus.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
    "sg": "https://openapi-sg.iotbing.com",
}
VOICEMONKEY_TRIGGER = "https://api-v3.voicemonkey.io/trigger"
_SHELLY_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.shelly\.cloud$")
_SWITCH_CODE = re.compile(r"^switch(_\d+)?$")
_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# Tests replace these.
_sleep = time.sleep


class PlugError(RuntimeError):
    """A sentence safe to show the user (no keys in it)."""


def _http(method: str, url: str, headers: dict | None = None, body: bytes | None = None):
    """(status, parsed JSON or None). Raises PlugError on no answer at all."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": "jarvis-plug", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read() if hasattr(e, "read") else b"")
    except Exception as e:
        # The address may carry a key; only the error's type leaves here.
        raise PlugError(f"the plug's service could not be reached ({type(e).__name__})") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        data = None
    return status, data


def _status_error(service: str, status: int) -> PlugError:
    if status in (401, 403):
        return PlugError(f"{service} refused the key (HTTP {status})")
    if status == 429:
        return PlugError(f"{service}: too many requests, try again in a minute")
    return PlugError(f"{service} answered HTTP {status}")


# ── settings ─────────────────────────────────────────────────────────────────

def settings(cfg: dict) -> dict:
    """The plug part of the wake settings, trimmed."""
    cfg = cfg or {}
    out = {k: str(cfg.get(k) or "").strip() for k in (
        "plug_kind", "shelly_host", "shelly_auth_key", "shelly_device_id", "shelly_channel",
        "tuya_region", "tuya_client_id", "tuya_secret", "tuya_device_id",
        "voicemonkey_token", "voicemonkey_device")}
    out["plug_kind"] = out["plug_kind"].lower()
    return out


def missing(cfg: dict) -> list[str]:
    """Labels of what still has to be filled in ([] = complete)."""
    s = settings(cfg)
    kind = s["plug_kind"]
    need = {"shelly": [("shelly_host", "Shelly server"), ("shelly_auth_key", "Shelly cloud key"),
                       ("shelly_device_id", "Shelly device ID")],
            "tuya": [("tuya_client_id", "Tuya Access ID"), ("tuya_secret", "Tuya Access Secret"),
                     ("tuya_device_id", "Tuya device ID")],
            "voicemonkey": [("voicemonkey_token", "Voice Monkey token"),
                            ("voicemonkey_device", "Voice Monkey device")]}.get(kind)
    if need is None:
        return ["the plug's service"]
    return [label for key, label in need if not s[key]]


def configured(cfg: dict) -> bool:
    s = settings(cfg)
    return s["plug_kind"] in KINDS and not missing(cfg)


def describe(cfg: dict) -> str:
    return {"shelly": "the Shelly plug", "tuya": "the Tuya / Smart Life plug",
            "voicemonkey": "the Voice Monkey routine"}.get(settings(cfg)["plug_kind"], "the smart plug")


# ── Shelly ───────────────────────────────────────────────────────────────────

def _shelly_host(s: dict) -> str:
    host = re.sub(r"^https?://", "", s["shelly_host"].lower()).split("/")[0].split(":")[0]
    if not _SHELLY_HOST.match(host):
        raise PlugError("the Shelly server must be the one the Shelly app shows, ending in "
                        ".shelly.cloud (Settings → Authorization cloud key)")
    return host


def _shelly(s: dict, path: str, payload: dict):
    host = _shelly_host(s)
    if not _ID.match(s["shelly_device_id"]):
        raise PlugError("the Shelly device ID should be 4 to 64 letters, digits, '-' or '_'")
    url = f"https://{host}{path}?auth_key={urllib.parse.quote(s['shelly_auth_key'], safe='')}"
    status, data = _http("POST", url, {"Content-Type": "application/json"},
                         json.dumps(payload).encode())
    if status != 200:
        raise _status_error("Shelly", status)
    return data


def _shelly_channel(s: dict) -> int:
    try:
        return max(0, int(s["shelly_channel"] or 0))
    except ValueError:
        raise PlugError("the Shelly channel must be a number (0 for a single plug)") from None


def _shelly_switch(s: dict, on: bool) -> None:
    _shelly(s, "/v2/devices/api/set/switch",
            {"id": s["shelly_device_id"], "channel": _shelly_channel(s), "on": bool(on)})


def _shelly_check(s: dict) -> str:
    data = _shelly(s, "/v2/devices/api/get", {"ids": [s["shelly_device_id"]], "select": ["status"]})
    rows = data if isinstance(data, list) else (data or {}).get("data") if isinstance(data, dict) else None
    if not rows:
        raise PlugError("Shelly does not know that device ID on this account")
    online = rows[0].get("online") if isinstance(rows[0], dict) else None
    if online in (0, False):
        return "Shelly knows the plug, but it is offline right now"
    return "Shelly knows the plug and it is online"


# ── Tuya ─────────────────────────────────────────────────────────────────────

_tuya_tokens: dict = {}          # (base, client id) -> (token, expires at)
_tuya_lock = threading.Lock()


def _tuya_base(s: dict) -> str:
    base = TUYA_REGIONS.get((s["tuya_region"] or "eu").lower())
    if not base:
        raise PlugError("the Tuya data centre must be one of: " + ", ".join(TUYA_REGIONS))
    return base


def tuya_sign(client_id: str, secret: str, t: str, nonce: str, method: str, path: str,
              body: bytes = b"", token: str = "", signed_headers: tuple = ()) -> str:
    """Tuya OpenAPI request signature (HMAC-SHA256, upper-case hex).

    The signed text is the method, the body's SHA-256, one "name:value" line
    per header listed in Signature-Headers (none here), and the path with its
    query parameters sorted by name — each part on its own line. Checked
    against both worked examples in Tuya's signature documentation
    (core/_plug_tests.py)."""
    if "?" in path:
        base, query = path.split("?", 1)
        pairs = sorted(p for p in query.split("&") if p)
        path = base + ("?" + "&".join(pairs) if pairs else "")
    headers_part = "".join(f"{k}:{v}\n" for k, v in signed_headers)
    to_sign = (method.upper() + "\n" + hashlib.sha256(body or b"").hexdigest() + "\n"
               + headers_part + "\n" + path)
    message = client_id + token + t + nonce + to_sign
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


def _tuya_request(s: dict, method: str, path: str, payload=None, token: str = ""):
    base = _tuya_base(s)
    body = json.dumps(payload).encode() if payload is not None else b""
    t, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
    headers = {"client_id": s["tuya_client_id"], "t": t, "nonce": nonce,
               "sign_method": "HMAC-SHA256",
               "sign": tuya_sign(s["tuya_client_id"], s["tuya_secret"], t, nonce, method, path,
                                 body, token)}
    if token:
        headers["access_token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    status, data = _http(method, base + path, headers, body or None)
    if status != 200:
        raise _status_error("Tuya", status)
    if not isinstance(data, dict):
        raise PlugError("Tuya's answer was not in the expected form")
    if not data.get("success"):
        code = data.get("code")
        if code in (1004, 1010, 1011):
            raise PlugError(f"Tuya refused the Access ID / Secret (code {code})")
        if code == 2017 or code == 1106:
            raise PlugError(f"the device is not linked to this Tuya cloud project (code {code})")
        raise PlugError(f"Tuya refused the request (code {code})")
    return data.get("result")


def _tuya_token(s: dict) -> str:
    key = (_tuya_base(s), s["tuya_client_id"])
    with _tuya_lock:
        got = _tuya_tokens.get(key)
        if got and got[1] > time.time() + 60:
            return got[0]
    res = _tuya_request(s, "GET", "/v1.0/token?grant_type=1")
    token = str((res or {}).get("access_token") or "")
    if not token:
        raise PlugError("Tuya gave no access token")
    with _tuya_lock:
        _tuya_tokens[key] = (token, time.time() + int((res or {}).get("expire_time") or 3600))
    return token


def _tuya_switch_code(s: dict, token: str) -> str:
    dev = urllib.parse.quote(s["tuya_device_id"], safe="")
    status = _tuya_request(s, "GET", f"/v1.0/devices/{dev}/status", token=token)
    codes = [str(x.get("code")) for x in (status or []) if isinstance(x, dict)]
    for c in codes:
        if _SWITCH_CODE.match(c):
            return c
    raise PlugError("that Tuya device has no on/off switch")


def _tuya_switch(s: dict, on: bool) -> None:
    token = _tuya_token(s)
    code = _tuya_switch_code(s, token)
    dev = urllib.parse.quote(s["tuya_device_id"], safe="")
    _tuya_request(s, "POST", f"/v1.0/devices/{dev}/commands",
                  {"commands": [{"code": code, "value": bool(on)}]}, token=token)


def _tuya_check(s: dict) -> str:
    token = _tuya_token(s)
    code = _tuya_switch_code(s, token)
    return f"Tuya accepted the keys and the plug's switch ({code}) answers"


# ── Voice Monkey ─────────────────────────────────────────────────────────────

def _voicemonkey_trigger(s: dict) -> None:
    q = urllib.parse.urlencode({"token": s["voicemonkey_token"], "device": s["voicemonkey_device"]})
    status, data = _http("GET", f"{VOICEMONKEY_TRIGGER}?{q}")
    if status != 200:
        raise _status_error("Voice Monkey", status)
    if isinstance(data, dict) and data.get("success") is False:
        raise PlugError("Voice Monkey did not accept the trigger (check the token and the device "
                        "name, and the monthly allowance)")


# ── public ───────────────────────────────────────────────────────────────────

def check(cfg: dict) -> tuple[bool, str]:
    """Are the settings complete and does the service know the plug? Switches
    nothing. Never raises."""
    s = settings(cfg)
    gaps = missing(cfg)
    if gaps:
        return False, "Smart plug: still to fill in — " + ", ".join(gaps) + "."
    try:
        if s["plug_kind"] == "shelly":
            return True, _shelly_check(s) + "."
        if s["plug_kind"] == "tuya":
            return True, _tuya_check(s) + "."
        return True, ("Voice Monkey is filled in. It cannot be tried without running the "
                      "routine, which would switch the plug.")
    except PlugError as e:
        return False, f"Smart plug: {e}."


def switch(cfg: dict, on: bool) -> tuple[bool, str]:
    """One switch (Shelly, Tuya) or the routine (Voice Monkey). Never raises."""
    s = settings(cfg)
    if not configured(cfg):
        return False, "the smart plug is not set up"
    try:
        if s["plug_kind"] == "shelly":
            _shelly_switch(s, on)
        elif s["plug_kind"] == "tuya":
            _tuya_switch(s, on)
        else:
            _voicemonkey_trigger(s)
    except PlugError as e:
        return False, str(e)
    return True, f"{describe(cfg)} was told {'on' if on else 'off'}"


def cycle(cfg: dict) -> tuple[bool, str]:
    """Off, POWER_PAUSE_S seconds, on — or the Voice Monkey routine, which
    does the same in Alexa. Only after the user confirmed. Never raises."""
    s = settings(cfg)
    if not configured(cfg):
        return False, "the smart plug is not set up"
    if s["plug_kind"] == "voicemonkey":
        ok, text = switch(cfg, True)
        return ok, ("the Voice Monkey routine was started (it switches the plug off and on)"
                    if ok else text)
    ok, text = switch(cfg, False)
    if not ok:
        return False, f"the plug could not be switched off: {text}"
    _sleep(POWER_PAUSE_S)
    ok, text = switch(cfg, True)
    if not ok:
        return False, (f"the plug was switched OFF but could not be switched back on: {text}. "
                       f"Turn it back on with the plug maker's app")
    return True, f"{describe(cfg)} was switched off and on again"
