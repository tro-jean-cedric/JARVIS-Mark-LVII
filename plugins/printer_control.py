"""
plugins/printer_control.py — the user's 3D printer, by voice, over the home network.

Bambu Lab (MQTT, LAN mode), OctoPrint and Moonraker/Klipper are supported; the
protocol work lives in plugins/_printer_core.py. This file maps what the user
asked for onto it, keeps an optional watcher that speaks up at the halfway
mark, near the end, when the print finishes and when it fails, and puts the
one irreversible action — stopping a print — behind the on-screen confirmation.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

from plugins import _announce
from plugins import _printer_core as core

PLUGIN = {
    "name": "printer_control",
    "description": (
        "Voice control of the user's PHYSICAL 3D PRINTER over the local network "
        "(Bambu Lab, OctoPrint or Klipper/Moonraker). Use for anything about the "
        "printer or the print job: status, progress and time left; pause, resume "
        "or stop the print; set the nozzle or bed temperature; cool down; change "
        "the speed profile; the part-cooling fan; the chamber light; homing; a "
        "picture from the printer's camera; and spoken updates while it prints. "
        "Examples: 'how is my print going', 'how long is left', 'pause the print', "
        "'heat the nozzle to 220', 'bed to 60', 'cool it down', 'speed to sport', "
        "'fan to 50 percent', 'light on', 'show me the printer camera', 'tell me "
        "when the print is done'. Stopping asks the user to confirm on screen by "
        "itself - just call action='stop'. Never for paper printers or the screen."
    ),
    "brief": "The user's 3D printer: status, pause/resume/stop, temps, cool down, speed, fan, light, camera, updates.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "'status', 'pause', 'resume', 'stop' (cancels the print, asks "
                               "for on-screen confirmation), 'set_nozzle_temp', 'set_bed_temp', "
                               "'cooldown', 'set_speed', 'fan_on', 'fan_off', 'light_on', "
                               "'light_off', 'home', 'camera' (one picture from the printer "
                               "camera on the display), 'monitor_on', 'monitor_off'.",
            },
            "value": {
                "type": "NUMBER",
                "description": "Temperature in °C for set_nozzle_temp / set_bed_temp; fan percent "
                               "0-100 for fan_on; speed profile for set_speed: 1=silent, "
                               "2=standard, 3=sport, 4=ludicrous.",
            },
            "speed": {
                "type": "STRING",
                "description": "set_speed by name instead of number: silent, standard, sport, ludicrous.",
            },
        },
        "required": ["action"],
    },
}


def _test(values: dict):
    b = None
    try:
        b = core.backend(values)
        st = b.status()
        return True, "Connected. " + core.describe(st)
    except core.PrinterError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Unexpected error: {e}"
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:
                pass


PLUGIN_SETTINGS = {
    "namespace": core.NS,
    "title": "3D PRINTER",
    "fields": [
        {"key": "brand", "label": "Printer type", "type": "choice",
         "options": ["bambu", "octoprint", "moonraker"], "default": "bambu"},
        {"key": "ip", "label": "IP address / host", "type": "text",
         "placeholder": "e.g. 192.168.1.50 (Bambu: Settings > WLAN on the printer)"},
        {"key": "access_code", "label": "Bambu: LAN access code", "type": "password",
         "placeholder": "8-digit code from the printer's network screen"},
        {"key": "serial", "label": "Bambu: serial number", "type": "text",
         "placeholder": "e.g. 01P00A123456789"},
        {"key": "api_key", "label": "OctoPrint / Moonraker: API key", "type": "password",
         "placeholder": "OctoPrint: Settings > API (Moonraker: empty if none)"},
        {"key": "port", "label": "OctoPrint / Moonraker: port (optional)", "type": "text",
         "placeholder": "e.g. 7125 for Moonraker, empty for 80"},
        {"key": "camera_url", "label": "OctoPrint / Moonraker: camera snapshot URL (optional)",
         "type": "text", "placeholder": "default: <host>/webcam/?action=snapshot"},
        {"key": "light_on_gcode", "label": "Klipper/OctoPrint: light-on G-code (optional)",
         "type": "text", "placeholder": "default SET_PIN PIN=caselight VALUE=1 / M355 S1"},
        {"key": "light_off_gcode", "label": "Klipper/OctoPrint: light-off G-code (optional)",
         "type": "text", "placeholder": "default SET_PIN PIN=caselight VALUE=0 / M355 S0"},
    ],
    "action": {"label": "CONNECT & TEST", "run": _test},
}

_SPEED_WORDS = {"silent": 1, "quiet": 1, "standard": 2, "normal": 2, "sport": 3, "fast": 3,
                "ludicrous": 4, "max": 4, "fastest": 4}


# ── Spoken updates while printing ────────────────────────────────────────────
class _Monitor:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.said: set[str] = set()

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        self.stop.clear()
        self.said = set()
        self.thread = threading.Thread(target=self._loop, name="printer-monitor", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        misses = 0
        last_state = ""
        while not self.stop.wait(45):
            try:
                st = core.backend().status()
                misses = 0
            except Exception as e:
                misses += 1
                if misses == 4:
                    _announce.say(f"Tell the user the 3D printer has not answered for a few "
                                  f"minutes ({e}); you keep trying.")
                continue
            if st.state == "printing" and st.progress is not None:
                if st.progress >= 50 and "half" not in self.said:
                    self.said.add("half")
                    left = f", about {core.fmt_minutes(st.remaining_min)} left" if st.remaining_min else ""
                    _announce.say(f"Tell the user their 3D print is halfway done, at about "
                                  f"{st.progress:.0f} percent{left}.")
                if (st.progress >= 90 or (st.remaining_min is not None and st.remaining_min <= 10
                                          and st.progress > 50)) and "near" not in self.said:
                    self.said.add("near")
                    _announce.say("Tell the user their 3D print is almost finished"
                                  + (f", about {core.fmt_minutes(st.remaining_min)} left."
                                     if st.remaining_min else "."))
            if st.state == "finished" and last_state in ("printing", "paused", ""):
                if last_state:
                    _announce.say("Tell the user their 3D print has finished successfully.")
                    self.stop.set()
            if st.state == "failed" and last_state in ("printing", "paused", "preparing"):
                _announce.say("Warn the user that their 3D print has failed or was stopped"
                              + (f" ({st.error})." if st.error else "."))
                self.stop.set()
            if st.state == "paused" and last_state == "printing":
                _announce.say("Tell the user the 3D print has paused"
                              + (f": {st.error}." if st.error else "."))
            last_state = st.state


_monitor = _Monitor()


def _confirm_stop(b) -> str:
    try:
        from core import confirm
    except Exception:
        return "I can't ask for confirmation right now, so I have not stopped the print."

    def do_stop() -> str:
        try:
            b.stop()
        except Exception as e:
            _announce.say(f"Tell the user the print could not be stopped: {e}")
            return f"stop failed: {e}"
        time.sleep(3)
        try:
            st = b.status()
            if st.state in ("printing",):
                _announce.say("Tell the user the stop command was sent but the printer still "
                              "reports printing; they should check it.")
                return "stop sent, still printing"
        except Exception:
            pass
        _announce.say("Tell the user the 3D print has been stopped.")
        return "print stopped"

    return confirm.request(
        "printer_stop", "Stop the 3D print",
        "This cancels the current print. It can't be resumed.", do_stop)


def _show_camera(b, player) -> str:
    try:
        jpeg = b.snapshot()
    except core.PrinterError as e:
        return str(e)
    except Exception as e:
        return f"I couldn't open the printer camera: {e}"
    if player is None or not hasattr(player, "show_image"):
        return "The printer camera can only be shown on the desktop display, and there isn't one here."
    path = os.path.join(tempfile.gettempdir(), "jarvis_printer_camera.jpg")
    try:
        with open(path, "wb") as f:
            f.write(jpeg)
        player.show_image(path, "3D printer camera")
    except Exception as e:
        return f"I got a picture from the printer but couldn't show it: {e}"
    return "A picture from the printer camera is on the display (a snapshot, not a live stream)."


def run(parameters: dict, player=None, session_memory=None) -> str:
    _announce.remember(player)
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower().replace(" ", "_").replace("-", "_")
    value = p.get("value")
    try:
        value = None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        value = None

    if not action:
        return ("What would you like me to do with the printer: status, pause, resume, stop, "
                "temperature, speed, fan, or the light?")

    if action in ("monitor_off", "stop_monitor", "stop_updates"):
        if not _monitor.running():
            return "I wasn't watching the print."
        _monitor.stop.set()
        return "I've stopped watching the print."

    try:
        b = core.backend()
    except core.PrinterError as e:
        return str(e)
    except Exception as e:
        return f"I couldn't set up the printer connection: {e}"

    try:
        if action in ("status", "progress", "time_left", "state"):
            return core.describe(b.status())

        if action in ("monitor_on", "monitor", "notify", "updates"):
            if _monitor.running():
                return "I'm already keeping an eye on the print and will tell you how it's going."
            st = b.status()
            _monitor.start()
            prefix = core.describe(st) + " " if st.state in ("printing", "paused", "preparing") else \
                "Nothing is printing right now, but I'll watch for the next print. "
            return prefix + ("I'll tell you when it's halfway, when it's nearly done, and the "
                             "moment it finishes or if anything goes wrong.")

        if action == "pause":
            b.pause()
            return _verify(b, "paused", "The print is paused.",
                           "I sent the pause command but the printer doesn't report paused yet.")
        if action == "resume":
            b.resume()
            return _verify(b, "printing", "The print is running again.",
                           "I sent the resume command but the printer doesn't report printing yet.")
        if action in ("stop", "cancel"):
            st = b.status()
            if st.state not in ("printing", "paused", "preparing"):
                return "Nothing is printing, so there is nothing to stop."
            return _confirm_stop(b)

        if action in ("set_nozzle_temp", "nozzle", "set_nozzle"):
            if value is None or not 0 <= value <= 320:
                return "Tell me a nozzle temperature between 0 and 320 degrees."
            b.set_nozzle(value)
            return f"Nozzle target set to {value:.0f} degrees." if value else "Nozzle heater off."
        if action in ("set_bed_temp", "bed", "set_bed"):
            if value is None or not 0 <= value <= 120:
                return "Tell me a bed temperature between 0 and 120 degrees."
            b.set_bed(value)
            return f"Bed target set to {value:.0f} degrees." if value else "Bed heater off."
        if action in ("cooldown", "cool_down", "heaters_off"):
            b.cooldown()
            return "Cooling down: the nozzle and bed heaters are off."

        if action in ("set_speed", "speed"):
            level = None
            name = str(p.get("speed") or "").strip().lower()
            if name in _SPEED_WORDS:
                level = _SPEED_WORDS[name]
            elif value is not None and 1 <= int(value) <= 4:
                level = int(value)
            if level is None:
                return "Which speed: silent, standard, sport, or ludicrous?"
            b.set_speed(level)
            return f"Print speed set to {core.SPEED_NAMES[level]}."

        if action in ("fan_on", "fan"):
            pct = 100 if value is None else max(0, min(100, int(value)))
            b.set_fan(pct)
            return f"Part-cooling fan set to {pct} percent." if pct else "Part-cooling fan turned off."
        if action == "fan_off":
            b.set_fan(0)
            return "Part-cooling fan turned off."

        if action in ("light_on", "light_off"):
            b.set_light(action == "light_on")
            return "Chamber light " + ("on." if action == "light_on" else "off.")

        if action == "home":
            st = b.status()
            if st.state in ("printing", "paused", "preparing"):
                return "I won't home the head while a print is running or paused."
            b.home()
            return "Homing the print head."

        if action in ("camera", "camera_on", "snapshot", "picture"):
            return _show_camera(b, player)
        if action == "camera_off":
            try:
                if player is not None:
                    player.stop_video()
            except Exception:
                pass
            return "Closed the printer picture."

        return (f"I don't know the printer action '{action}'. I can report status, pause, resume, "
                "stop, set temperatures, cool down, change speed, fan, light, home, or show the camera.")
    except core.PrinterError as e:
        return str(e)
    except Exception as e:
        return f"The printer command failed: {e}"


def _verify(b, want: str, ok_msg: str, unsure_msg: str) -> str:
    for _ in range(4):
        time.sleep(1.0)
        try:
            if b.status().state == want:
                return ok_msg
        except Exception:
            pass
    return unsure_msg
