"""
plugins/home_assistant.py — the smart light in the user's room (Tuya / Smart Life).

Two ways to reach the bulb, chosen in the settings:

  local   tinytuya talks straight to the bulb on the home network. Needs the
          device id, its IP address, its local key and the protocol version
          (3.1 / 3.3 / 3.4 / 3.5). Fast, works without the internet.
  cloud   tinytuya's Cloud client talks to the user's own Tuya IoT project
          (iot.tuya.com): Access ID, Access Secret, data centre region and the
          device id. Slower, but needs no local key.

Every failure is turned into a sentence that says exactly which setting to fix,
because "could not control the light" helps nobody standing in a dark room.
"""
from __future__ import annotations

import colorsys
import re
import threading

PLUGIN = {
    "name": "home_assistant",
    "description": (
        "Controls the user's Tuya / Smart Life SMART LIGHT (bulb or LED strip) in "
        "their room: turn it on or off, set a colour or a mood/scene, set the "
        "brightness, set warm or cool white, or report whether it is on and how "
        "it is set. Examples: 'turn the light on', 'lights off', 'make it blue', "
        "'set the light to sunset', 'movie mode', 'dim it to 30 percent', 'warm "
        "white', 'is the light on'. Map the request onto 'action' and fill "
        "'color', 'brightness' or 'warmth'. Any language. Physical light only - "
        "not the screen brightness (that is computer_settings)."
    ),
    "brief": "The user's smart light (Tuya/Smart Life): on, off, colour, mood, brightness, white warmth, state.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "'on', 'off', 'toggle', 'color' (a colour or a mood/scene "
                               "name), 'brightness', 'white' (white light, use 'warmth'), "
                               "'scene' (a mood), 'status', or 'list_scenes'.",
            },
            "color": {
                "type": "STRING",
                "description": "Colour or mood in English: 'red', 'purple', '#ff8800', "
                               "'ocean', 'sunset', 'romantic', 'movie', 'focus', 'relax', "
                               "'party', 'night', 'reading'.",
            },
            "brightness": {
                "type": "INTEGER",
                "description": "Brightness 0-100 percent (0 turns the light off). Can go with a colour.",
            },
            "warmth": {
                "type": "INTEGER",
                "description": "For 'white': 0 = coolest, 100 = warmest white.",
            },
        },
        "required": ["action"],
    },
}


def _test_connection(values: dict):
    """Settings-panel button: reach the light with what was typed and report."""
    try:
        light = _Light(values)
        st = light.status()
        return True, f"Connected. {_describe(st)}"
    except _LightError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Unexpected error: {e}"


PLUGIN_SETTINGS = {
    "namespace": "home_assistant",
    "title": "SMART LIGHT (TUYA / SMART LIFE)",
    "fields": [
        {"key": "mode", "label": "Connection", "type": "choice",
         "options": ["local", "cloud"], "default": "local"},
        {"key": "device_id", "label": "Device ID", "type": "text",
         "placeholder": "e.g. bf1234567890abcdefgh"},
        {"key": "ip", "label": "Local: bulb IP address", "type": "text",
         "placeholder": "e.g. 192.168.1.42 (or 'auto' to search the network)"},
        {"key": "local_key", "label": "Local: local key", "type": "password",
         "placeholder": "16-character key from 'python -m tinytuya wizard'"},
        {"key": "version", "label": "Local: protocol version", "type": "choice",
         "options": ["3.3", "3.1", "3.4", "3.5"], "default": "3.3"},
        {"key": "api_key", "label": "Cloud: Access ID", "type": "text",
         "placeholder": "iot.tuya.com project Access ID"},
        {"key": "api_secret", "label": "Cloud: Access Secret", "type": "password"},
        {"key": "region", "label": "Cloud: data centre", "type": "choice",
         "options": ["eu", "us", "cn", "in", "us-e", "eu-w", "sg"], "default": "eu"},
    ],
    "action": {"label": "CONNECT & TEST", "run": _test_connection},
}

_NS = "home_assistant"

# ── Colours and moods ─────────────────────────────────────────────────────────
_COLOURS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "yellow": (255, 210, 0), "orange": (255, 110, 0), "purple": (140, 0, 255),
    "violet": (160, 60, 255), "pink": (255, 60, 150), "magenta": (255, 0, 255),
    "cyan": (0, 255, 255), "turquoise": (0, 220, 180), "teal": (0, 160, 150),
    "lime": (150, 255, 0), "gold": (255, 180, 0), "amber": (255, 150, 0),
    "indigo": (75, 0, 200), "lavender": (190, 140, 255), "crimson": (220, 20, 60),
    "coral": (255, 100, 70), "salmon": (255, 120, 100), "mint": (100, 255, 170),
    "aqua": (0, 255, 220), "navy": (0, 0, 140), "rose": (255, 40, 110),
    "peach": (255, 160, 110), "white": (255, 255, 255),
}

# A mood is either a colour (rgb + brightness) or a white (warmth + brightness).
_MOODS: dict[str, dict] = {
    "ocean":    {"rgb": (0, 110, 255), "brightness": 70},
    "sunset":   {"rgb": (255, 90, 20), "brightness": 65},
    "romantic": {"rgb": (255, 30, 90), "brightness": 35},
    "movie":    {"rgb": (60, 30, 200), "brightness": 15},
    "cinema":   {"rgb": (60, 30, 200), "brightness": 15},
    "party":    {"rgb": (255, 0, 200), "brightness": 100},
    "forest":   {"rgb": (20, 200, 60), "brightness": 60},
    "fire":     {"rgb": (255, 60, 0), "brightness": 80},
    "candle":   {"white": 100, "brightness": 20},
    "night":    {"rgb": (255, 70, 0), "brightness": 5},
    "relax":    {"white": 85, "brightness": 45},
    "cozy":     {"white": 90, "brightness": 40},
    "warm":     {"white": 100, "brightness": 70},
    "focus":    {"white": 20, "brightness": 100},
    "work":     {"white": 30, "brightness": 100},
    "reading":  {"white": 55, "brightness": 85},
    "daylight": {"white": 10, "brightness": 100},
    "cool":     {"white": 0, "brightness": 90},
    "energize": {"white": 0, "brightness": 100},
}


class _LightError(Exception):
    """A failure with a sentence the user can act on."""


def _cfg(values: dict | None = None) -> dict:
    if values is not None:
        return dict(values)
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(_NS)
    except Exception:
        return {}


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def _parse_colour(text: str) -> tuple[int, int, int] | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    m = re.fullmatch(r"#?([0-9a-f]{6})", t)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"\(?\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)?", t)
    if m:
        return tuple(_clamp(x, 0, 255) for x in m.groups())  # type: ignore
    t = re.sub(r"\b(light|colou?r|please|the|a)\b", " ", t).strip()
    words = t.split()
    if t in _COLOURS:
        return _COLOURS[t]
    for w in reversed(words):            # "dark blue" -> blue, "soft pink" -> pink
        if w in _COLOURS:
            return _COLOURS[w]
    return None


def _colour_by_model(text: str) -> tuple[int, int, int] | None:
    """An unusual colour name ('mauve', 'bordeaux'): ask the model for its RGB."""
    try:
        from core import gemini
        data = gemini.as_json(
            "Give the RGB colour a smart light should show for this colour or mood "
            f"description: {text!r}. Reply with JSON only: "
            '{"r": 0-255, "g": 0-255, "b": 0-255}', tier=gemini.FAST)
        if isinstance(data, dict):
            return (_clamp(data.get("r", 0), 0, 255), _clamp(data.get("g", 0), 0, 255),
                    _clamp(data.get("b", 0), 0, 255))
    except Exception as e:
        print(f"[SmartLight] colour lookup failed: {e}")
    return None


def _mood(text: str) -> dict | None:
    t = (text or "").strip().lower()
    for word in re.findall(r"[a-z]+", t):
        if word in _MOODS:
            return _MOODS[word]
    if "warm white" in t or "soft warm" in t:
        return {"white": 90, "brightness": 70}
    if "cool white" in t or "cold white" in t:
        return {"white": 0, "brightness": 90}
    return None


# ── The two backends behind one small interface ──────────────────────────────
class _Light:
    def __init__(self, values: dict | None = None):
        cfg = _cfg(values)
        self.mode = str(cfg.get("mode") or "local").strip().lower()
        self.device_id = str(cfg.get("device_id") or "").strip()
        if not self.device_id or self.device_id.startswith("PUT_YOUR"):
            raise _LightError(
                "The smart light isn't set up yet. Open the plugin settings (SMART "
                "LIGHT), enter the Device ID and the connection details, then press "
                "CONNECT & TEST.")
        try:
            import tinytuya  # noqa: F401
        except Exception:
            raise _LightError("I need the Tuya library first. Run: pip install tinytuya")
        if self.mode == "cloud":
            self._init_cloud(cfg)
        else:
            self._init_local(cfg)

    # -- local ---------------------------------------------------------------
    def _init_local(self, cfg: dict) -> None:
        import tinytuya
        key = str(cfg.get("local_key") or "").strip()
        ip = str(cfg.get("ip") or "").strip() or "Auto"
        ver = str(cfg.get("version") or "3.3").strip()
        if not key:
            raise _LightError(
                "The light's local key is missing. Get it with 'python -m tinytuya "
                "wizard' (it needs a free iot.tuya.com project once) and paste it into "
                "the SMART LIGHT settings, or switch the connection to 'cloud'.")
        try:
            version = float(ver)
        except ValueError:
            raise _LightError(f"The protocol version '{ver}' is not a number; use 3.1, 3.3, 3.4 or 3.5.")
        self.dev = tinytuya.BulbDevice(self.device_id, "Auto" if ip.lower() == "auto" else ip,
                                       key, version=version)
        self.dev.set_socketTimeout(4)
        self.dev.set_socketRetryLimit(1)
        self.ip = ip

    def _local_check(self, result, what: str):
        if isinstance(result, dict) and ("Error" in result or "Err" in result):
            code = str(result.get("Err") or "")
            if code in ("901", "905", "902"):
                raise _LightError(
                    f"I couldn't reach the light at {self.ip}. Check it has power and "
                    "Wi-Fi, that the IP address in the settings is still right (routers "
                    "hand out new ones), and that this computer is on the same network.")
            if code == "914":
                raise _LightError(
                    "The light answered but refused the key. The local key or the "
                    "protocol version in the settings is wrong; the key changes every "
                    "time the bulb is re-paired in Smart Life.")
            if code == "907":
                raise _LightError("This light doesn't support that.")
            raise _LightError(f"The light reported an error while {what}: "
                              f"{result.get('Error') or code}.")
        return result

    # -- cloud ---------------------------------------------------------------
    def _init_cloud(self, cfg: dict) -> None:
        import tinytuya
        key = str(cfg.get("api_key") or "").strip()
        secret = str(cfg.get("api_secret") or "").strip()
        region = str(cfg.get("region") or "eu").strip()
        if not key or not secret or key.startswith("PUT_YOUR"):
            raise _LightError(
                "Enter your Tuya Access ID and Access Secret (from your project on "
                "iot.tuya.com) in the SMART LIGHT settings first.")
        try:
            self.cloud = tinytuya.Cloud(apiRegion=region, apiKey=key, apiSecret=secret,
                                        apiDeviceID=self.device_id)
        except Exception as e:
            raise _LightError(f"Tuya's cloud could not be reached: {e}. Check the internet connection.")
        err = getattr(self.cloud, "error", None)
        if err:
            raise _LightError(self._cloud_message(err))

    @staticmethod
    def _cloud_message(resp) -> str:
        if isinstance(resp, dict):
            code = str(resp.get("code") or resp.get("Err") or "")
            msg = str(resp.get("msg") or resp.get("Payload") or resp.get("Error") or "")
        else:
            code, msg = "", str(resp)
        low = msg.lower()
        if code in ("1004", "1005", "911") or "sign invalid" in low or "token" in low:
            return ("Tuya didn't accept the Access ID and Secret. Copy both again from "
                    "your project on iot.tuya.com, and check the data centre matches the "
                    "one the project uses.")
        if code == "28841002" or "expired" in low or "subscription" in low:
            return ("Your Tuya cloud trial has ended or the device API isn't enabled. On "
                    "iot.tuya.com open Cloud > Cloud Services > IoT Core and extend the "
                    "free trial, then try again.")
        if code in ("1106", "2017") or "permission" in low:
            return ("Tuya refused that light for this project. On iot.tuya.com link your "
                    "Smart Life account to the project (Devices > Link App Account) and "
                    "check the Device ID.")
        if code in ("2008", "1109", "1102") or "not exist" in low or "device" in low and "not" in low:
            return ("Tuya doesn't know that Device ID. Copy it again from the device list "
                    "on iot.tuya.com.")
        if "connect" in low or "timeout" in low:
            return "Tuya's cloud didn't answer. Check the internet connection."
        return f"Tuya's cloud refused the request: {msg or code or 'no reason given'}."

    def _cloud_send(self, commands: list[dict]) -> None:
        resp = self.cloud.sendcommand(self.device_id, {"commands": commands})
        if not (isinstance(resp, dict) and resp.get("success")):
            raise _LightError(self._cloud_message(resp))

    def _cloud_codes(self) -> dict:
        resp = self.cloud.getstatus(self.device_id)
        if not (isinstance(resp, dict) and resp.get("success")):
            raise _LightError(self._cloud_message(resp))
        return {d.get("code"): d.get("value") for d in resp.get("result") or []}

    # -- the interface ---------------------------------------------------------
    def status(self) -> dict:
        """{'on': bool|None, 'mode': str, 'brightness': int|None, 'rgb': tuple|None, 'warmth': int|None}"""
        if self.mode == "cloud":
            c = self._cloud_codes()
            out = {"on": c.get("switch_led", c.get("switch_1")),
                   "mode": str(c.get("work_mode") or ""), "brightness": None,
                   "rgb": None, "warmth": None}
            bv = c.get("bright_value_v2", c.get("bright_value"))
            hi = 1000 if "bright_value_v2" in c else 255
            if isinstance(bv, (int, float)):
                out["brightness"] = round(100 * bv / hi)
            tv = c.get("temp_value_v2", c.get("temp_value"))
            if isinstance(tv, (int, float)):
                out["warmth"] = 100 - round(100 * tv / (1000 if "temp_value_v2" in c else 255))
            cd = c.get("colour_data_v2", c.get("colour_data"))
            if isinstance(cd, str):
                try:
                    import json
                    cd = json.loads(cd)
                except Exception:
                    cd = None
            if isinstance(cd, dict) and out["mode"] == "colour":
                smax = 1000 if "colour_data_v2" in c else 255
                r, g, b = colorsys.hsv_to_rgb((cd.get("h", 0) % 360) / 360.0,
                                              cd.get("s", 0) / smax, 1.0)
                out["rgb"] = (round(r * 255), round(g * 255), round(b * 255))
                out["brightness"] = round(100 * cd.get("v", 0) / smax)
            return out
        st = self._local_check(self.dev.status(), "reading its state")
        dps = (st or {}).get("dps") or {}
        on = dps.get("20", dps.get("1"))
        out = {"on": on if isinstance(on, bool) else None, "mode": "", "brightness": None,
               "rgb": None, "warmth": None}
        try:
            out["mode"] = str(self.dev.get_mode(state=st) or "")
        except Exception:
            pass
        try:
            out["brightness"] = int(self.dev.get_brightness_percentage(state=st))
        except Exception:
            pass
        try:
            out["warmth"] = 100 - int(self.dev.get_colourtemp_percentage(state=st))
        except Exception:
            pass
        if out["mode"] == "colour":
            try:
                out["rgb"] = tuple(self.dev.colour_rgb(state=st))
            except Exception:
                pass
        return out

    def power(self, on: bool) -> None:
        if self.mode == "cloud":
            self._cloud_send([{"code": "switch_led", "value": bool(on)}])
        else:
            self._local_check(self.dev.turn_on() if on else self.dev.turn_off(),
                              "switching it")

    def set_rgb(self, rgb: tuple[int, int, int], brightness: int | None = None) -> None:
        r, g, b = rgb
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if brightness is not None:
            v = _clamp(brightness, 1, 100) / 100.0
        if self.mode == "cloud":
            self._cloud_send([
                {"code": "switch_led", "value": True},
                {"code": "work_mode", "value": "colour"},
                {"code": "colour_data_v2",
                 "value": {"h": int(h * 360), "s": int(s * 1000), "v": max(10, int(v * 1000))}},
            ])
            return
        self.power(True)
        self._local_check(self.dev.set_hsv(h, s, max(0.01, v)), "setting the colour")

    def set_brightness(self, pct: int) -> None:
        pct = _clamp(pct, 1, 100)
        if self.mode == "cloud":
            st = self.status()
            if st.get("mode") == "colour" and st.get("rgb"):
                self.set_rgb(st["rgb"], pct)
                return
            self._cloud_send([{"code": "switch_led", "value": True},
                              {"code": "bright_value_v2", "value": max(10, pct * 10)}])
            return
        self.power(True)
        self._local_check(self.dev.set_brightness_percentage(pct), "setting the brightness")

    def set_white(self, warmth: int, brightness: int | None) -> None:
        warmth = _clamp(warmth, 0, 100)
        if self.mode == "cloud":
            cmds = [{"code": "switch_led", "value": True},
                    {"code": "work_mode", "value": "white"},
                    {"code": "temp_value_v2", "value": int((100 - warmth) * 10)}]
            if brightness is not None:
                cmds.append({"code": "bright_value_v2", "value": max(10, _clamp(brightness, 1, 100) * 10)})
            self._cloud_send(cmds)
            return
        self.power(True)
        cur = brightness
        if cur is None:
            try:
                cur = self.status().get("brightness") or 100
            except _LightError:
                cur = 100
        self._local_check(self.dev.set_white_percentage(_clamp(cur, 1, 100), 100 - warmth),
                          "setting the white")


def _describe(st: dict) -> str:
    if st.get("on") is False:
        return "The light is off."
    parts = ["The light is on" if st.get("on") else "The light's power state is unknown"]
    if st.get("rgb"):
        r, g, b = st["rgb"]
        parts.append(f"showing the colour #{r:02x}{g:02x}{b:02x} ({_nearest_name(st['rgb'])})")
    elif st.get("mode") == "white" and st.get("warmth") is not None:
        w = st["warmth"]
        parts.append("in " + ("warm" if w >= 66 else "neutral" if w >= 33 else "cool") + " white")
    elif st.get("mode") in ("scene", "music"):
        parts.append(f"in {st['mode']} mode")
    if st.get("brightness") is not None:
        parts.append(f"at {st['brightness']} percent brightness")
    return ", ".join(parts) + "."


def _nearest_name(rgb) -> str:
    best, dist = "", 1e9
    for name, c in _COLOURS.items():
        d = sum((a - b) ** 2 for a, b in zip(rgb, c))
        if d < dist:
            best, dist = name, d
    return best


_lock = threading.Lock()   # one conversation with the bulb at a time


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower().replace(" ", "_")
    colour = str(p.get("color") or p.get("colour") or "").strip()
    bright = p.get("brightness")
    warmth = p.get("warmth")
    try:
        bright = None if bright in (None, "") else int(float(bright))
    except (TypeError, ValueError):
        bright = None
    try:
        warmth = None if warmth in (None, "") else int(float(warmth))
    except (TypeError, ValueError):
        warmth = None

    if action in ("list_scenes", "scenes", "moods"):
        return ("Moods I know: " + ", ".join(sorted(_MOODS)) +
                ". Colours: any colour name or a hex code like #ff8800.")
    if not action:
        if colour:
            action = "color"
        elif bright is not None:
            action = "brightness"
        else:
            return "What should I do with the light: turn it on or off, set a colour, or a brightness?"

    with _lock:
        try:
            light = _Light()
            if action in ("on", "turn_on", "power_on"):
                light.power(True)
                if bright is not None and bright > 0:
                    light.set_brightness(bright)
                msg = "The light is on" + (f" at {bright} percent." if bright else ".")
            elif action in ("off", "turn_off", "power_off"):
                light.power(False)
                msg = "The light is off."
            elif action == "toggle":
                st = light.status()
                light.power(not bool(st.get("on")))
                msg = "The light is now " + ("off." if st.get("on") else "on.")
            elif action in ("brightness", "dim", "brighten"):
                if bright is None:
                    return "What brightness would you like, from 1 to 100 percent?"
                if bright <= 0:
                    light.power(False)
                    msg = "The light is off."
                else:
                    light.set_brightness(bright)
                    msg = (f"Brightness set to {_clamp(bright, 1, 100)} percent "
                           "(it keeps its colour; use a colour or 'white' to change that).")
            elif action in ("white", "warm_white", "cool_white", "temperature"):
                if warmth is None:
                    warmth = 100 if "warm" in action or "warm" in colour.lower() else \
                        0 if ("cool" in action or "cool" in colour.lower() or "cold" in colour.lower()) else 60
                light.set_white(warmth, bright)
                msg = ("Set to " + ("warm" if warmth >= 66 else "neutral" if warmth >= 33 else "cool")
                       + " white" + (f" at {bright} percent." if bright else "."))
            elif action in ("color", "colour", "scene", "mood"):
                if not colour:
                    return ('status=failed action=home_assistant reason="no colour given: call '
                            "again with color = the colour or mood asked for (e.g. 'warm', "
                            "'purple', 'ocean')\"")
                if bright is not None and bright <= 0:
                    light.power(False)
                    return "The light is off."
                mood = _mood(colour)
                rgb = None if mood else _parse_colour(colour)
                if mood is None and rgb is None:
                    rgb = _colour_by_model(colour)
                if mood is not None:
                    b = bright if bright is not None else mood.get("brightness")
                    if "white" in mood:
                        light.set_white(mood["white"], b)
                    else:
                        light.set_rgb(mood["rgb"], b)
                    msg = f"Set the light to {colour}" + (f" at {b} percent." if b else ".")
                elif rgb is not None:
                    if rgb == (255, 255, 255):
                        light.set_white(50 if warmth is None else warmth, bright)
                    else:
                        light.set_rgb(rgb, bright)
                    msg = f"The light is now {colour}" + (f" at {bright} percent." if bright else ".")
                else:
                    return (f"I don't know the colour '{colour}'. Try a plain colour name, "
                            "a hex code, or a mood like ocean, sunset or focus.")
            elif action in ("status", "state", "get_state"):
                return _describe(light.status())
            else:
                return (f"I don't know the light action '{action}'. I can turn it on or off, "
                        "set a colour or mood, brightness, white warmth, or tell you its state.")
        except _LightError as e:
            return str(e)
        except Exception as e:
            return f"Sorry, I couldn't control the light: {e}"

    if player is not None:
        try:
            player.write_log(f"SmartLight: {msg}")
        except Exception:
            pass
    return msg
