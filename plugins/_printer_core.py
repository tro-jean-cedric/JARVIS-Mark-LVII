"""
plugins/_printer_core.py — talking to a 3D printer on the home network.

Helper for plugins/printer_control.py (leading underscore: the loader skips it).

Three backends behind one interface, picked by the 'brand' setting:

  bambu      Bambu Lab printers in LAN mode: MQTT over TLS on port 8883,
             user 'bblp', the printer's 8-digit access code as the password,
             topics device/<serial>/report and device/<serial>/request.
  octoprint  OctoPrint's REST API with an API key.
  moonraker  Klipper's Moonraker API (Creality K1/Ender-3 V3 with Klipper,
             Voron, Sovol...). An API key only if the user set one up.

Every backend returns the same status dict (see `Status`) so the plugin never
has an `if brand ==` in it. Nothing here touches Qt; everything may raise
PrinterError with a sentence a person can act on.
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

NS = "printer_control"

SPEED_NAMES = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}
# Klipper / OctoPrint have no profiles; the same names map to a feed-rate factor.
SPEED_PERCENT = {1: 50, 2: 100, 3: 124, 4: 166}


class PrinterError(Exception):
    """A failure with a sentence the user can act on."""


@dataclass
class Status:
    online: bool = False
    state: str = "unknown"        # idle | printing | paused | finished | failed | preparing | unknown
    raw_state: str = ""
    progress: Optional[float] = None       # 0-100
    remaining_min: Optional[float] = None
    file: str = ""
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    nozzle: Optional[float] = None
    nozzle_target: Optional[float] = None
    bed: Optional[float] = None
    bed_target: Optional[float] = None
    chamber: Optional[float] = None
    speed_level: Optional[int] = None
    fan_percent: Optional[int] = None
    light_on: Optional[bool] = None
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def config(values: Optional[dict] = None) -> dict:
    if values is not None:
        return dict(values)
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NS)
    except Exception:
        return {}


# ── Bambu Lab ─────────────────────────────────────────────────────────────────
_BAMBU_STATE = {
    "IDLE": "idle", "RUNNING": "printing", "PAUSE": "paused", "FINISH": "finished",
    "FAILED": "failed", "PREPARE": "preparing", "SLICING": "preparing", "INIT": "preparing",
}


class BambuBackend:
    brand = "bambu"

    def __init__(self, cfg: dict):
        self.ip = str(cfg.get("ip") or "").strip()
        self.code = str(cfg.get("access_code") or "").strip()
        self.serial = str(cfg.get("serial") or "").strip().upper()
        if not (self.ip and self.code and self.serial):
            raise PrinterError(
                "A Bambu printer needs its IP address, the 8-digit LAN access code and the "
                "serial number. On the printer's screen open Settings > Network (WLAN) for "
                "the IP and access code; the serial is under Settings > Device. Enter them in "
                "the 3D PRINTER settings and press CONNECT & TEST.")
        try:
            import paho.mqtt.client as mqtt  # noqa: F401
        except Exception:
            raise PrinterError("I can't reach the printer until a component is installed: "
                               "run pip install paho-mqtt")
        self._client = None
        self._lock = threading.Lock()
        self._report: dict = {}
        self._got = threading.Event()
        self._connected = threading.Event()
        self._last_msg = 0.0
        self._conn_error = ""
        self._seq = 0

    # -- connection ----------------------------------------------------------
    def _ensure(self, timeout: float = 8.0) -> None:
        with self._lock:
            if self._client is not None and self._connected.is_set():
                return
            import paho.mqtt.client as mqtt
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     client_id=f"jarvis-{int(time.time())}", protocol=mqtt.MQTTv311)
            except AttributeError:      # paho-mqtt 1.x
                client = mqtt.Client(client_id=f"jarvis-{int(time.time())}", protocol=mqtt.MQTTv311)
            client.username_pw_set("bblp", self.code)
            ctx = ssl.create_default_context()
            # The printer presents a certificate signed by Bambu's own CA for its
            # serial number, not for its IP address; LAN mode is authenticated by
            # the access code, so hostname/CA checks are not meaningful here.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            client.tls_set_context(ctx)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect
            self._connected.clear()
            self._conn_error = ""
            try:
                client.connect(self.ip, 8883, keepalive=30)
            except Exception as e:
                raise PrinterError(f"I couldn't connect to the printer at {self.ip}: {e}. "
                                   "Check that it's powered on, on the same network, and "
                                   "that the IP address in the settings is current.")
            client.loop_start()
            if not self._connected.wait(timeout):
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
                why = self._conn_error or "no answer"
                raise PrinterError(f"The printer at {self.ip} did not accept the connection "
                                   f"({why}). Check the access code - it changes when LAN mode "
                                   "is switched off and on.")
            self._client = client

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        code = getattr(rc, "value", rc)
        try:
            failed = rc.is_failure  # paho 2.x ReasonCode
        except AttributeError:
            failed = bool(code)
        if failed:
            self._conn_error = f"refused: {rc}"
            return
        client.subscribe(f"device/{self.serial}/report")
        self._connected.set()
        self._publish({"pushing": {"sequence_id": self._next(), "command": "pushall"}}, client)

    def _on_disconnect(self, client, userdata, *args):
        self._connected.clear()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", "replace"))
        except Exception:
            return
        p = data.get("print")
        if isinstance(p, dict):
            # Reports after the first full one are deltas: merge them.
            self._report.update(p)
            self._last_msg = time.time()
            if "gcode_state" in self._report or "mc_percent" in self._report:
                self._got.set()

    def _next(self) -> str:
        self._seq += 1
        return str(self._seq)

    def _publish(self, payload: dict, client=None) -> None:
        c = client or self._client
        if c is None:
            raise PrinterError("The printer is offline or unreachable.")
        info = c.publish(f"device/{self.serial}/request", json.dumps(payload))
        if getattr(info, "rc", 0) != 0:
            raise PrinterError("The printer did not take the command (MQTT error "
                               f"{getattr(info, 'rc', '?')}).")

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.loop_stop()
                    self._client.disconnect()
                except Exception:
                    pass
            self._client = None
            self._connected.clear()

    # -- reading -------------------------------------------------------------
    def status(self, timeout: float = 8.0) -> Status:
        self._ensure()
        if time.time() - self._last_msg > 20:
            self._got.clear()
            self._publish({"pushing": {"sequence_id": self._next(), "command": "pushall"}})
        if not self._got.wait(timeout):
            raise PrinterError("Reached the printer but no status arrived. Double-check the "
                               "access code and the serial number, and that the printer is on.")
        r = dict(self._report)

        def f(key):
            try:
                return float(r[key])
            except Exception:
                return None

        def i(key):
            try:
                return int(float(r[key]))
            except Exception:
                return None

        raw = str(r.get("gcode_state") or "")
        st = Status(online=True, raw_state=raw, state=_BAMBU_STATE.get(raw.upper(), raw.lower() or "unknown"))
        st.progress = f("mc_percent")
        st.remaining_min = f("mc_remaining_time")
        st.file = str(r.get("subtask_name") or r.get("gcode_file") or "")
        st.layer, st.total_layers = i("layer_num"), i("total_layer_num")
        st.nozzle, st.nozzle_target = f("nozzle_temper"), f("nozzle_target_temper")
        st.bed, st.bed_target = f("bed_temper"), f("bed_target_temper")
        st.chamber = f("chamber_temper")
        st.speed_level = i("spd_lvl")
        fan = r.get("cooling_fan_speed")
        try:
            # Reported in steps of 0-15.
            st.fan_percent = round(int(fan) * 100 / 15) if fan is not None else None
        except Exception:
            pass
        for light in r.get("lights_report") or []:
            if isinstance(light, dict) and light.get("node") == "chamber_light":
                st.light_on = light.get("mode") == "on"
        err = r.get("print_error")
        if err:
            st.error = f"printer error code {err}"
        return st

    # -- commands ------------------------------------------------------------
    def _print_cmd(self, command: str, param: Optional[str] = None) -> None:
        self._ensure()
        body = {"sequence_id": self._next(), "command": command}
        if param is not None:
            body["param"] = param
        self._publish({"print": body})

    def gcode(self, line: str) -> None:
        self._print_cmd("gcode_line", line.rstrip("\n") + "\n")

    def pause(self):  self._print_cmd("pause")
    def resume(self): self._print_cmd("resume")
    def stop(self):   self._print_cmd("stop")

    def set_nozzle(self, c: float): self.gcode(f"M104 S{int(c)}")
    def set_bed(self, c: float):    self.gcode(f"M140 S{int(c)}")

    def cooldown(self):
        self.gcode("M104 S0\nM140 S0")

    def set_speed(self, level: int):
        self._print_cmd("print_speed", str(int(level)))

    def set_fan(self, percent: int):
        self.gcode(f"M106 P1 S{round(max(0, min(100, percent)) * 255 / 100)}")

    def set_light(self, on: bool):
        self._ensure()
        self._publish({"system": {
            "sequence_id": self._next(), "command": "ledctrl", "led_node": "chamber_light",
            "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
            "loop_times": 0, "interval_time": 0}})

    def home(self):
        self.gcode("G28")

    # -- camera --------------------------------------------------------------
    def snapshot(self, timeout: float = 10.0) -> bytes:
        """One JPEG from the printer's camera.

        P1 / A1 series serve JPEG frames over TLS on port 6000 after an 80-byte
        login; the X1 series serves RTSPS on port 322 (read with OpenCV).
        """
        try:
            return self._snapshot_port6000(timeout)
        except PrinterError:
            raise
        except Exception as first:
            try:
                return self._snapshot_rtsp()
            except PrinterError:
                raise
            except Exception:
                raise PrinterError(f"I couldn't open the printer camera: {first}. If the "
                                   "printer has a camera, turn on 'LAN Only Liveview' in its "
                                   "own settings and ask again.")

    def _snapshot_port6000(self, timeout: float) -> bytes:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
        auth += b"bblp".ljust(32, b"\0") + self.code.encode()[:32].ljust(32, b"\0")
        with socket.create_connection((self.ip, 6000), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=self.ip) as s:
                s.settimeout(timeout)
                s.sendall(auth)
                header = self._read_exact(s, 16)
                size = struct.unpack("<I", header[:4])[0]
                if not 100 < size < 8_000_000:
                    raise RuntimeError("unexpected frame header")
                img = self._read_exact(s, size)
        if not img.startswith(b"\xff\xd8"):
            raise RuntimeError("the camera did not send a picture")
        return img

    @staticmethod
    def _read_exact(s, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("the camera closed the connection")
            buf += chunk
        return buf

    def _snapshot_rtsp(self) -> bytes:
        try:
            import cv2
        except Exception:
            raise PrinterError("I can't open the printer camera until a component is "
                               "installed: pip install opencv-python")
        url = f"rtsps://bblp:{self.code}@{self.ip}:322/streaming/live/1"
        cap = cv2.VideoCapture(url)
        try:
            ok, frame = False, None
            end = time.time() + 8
            while time.time() < end:
                ok, frame = cap.read()
                if ok and frame is not None:
                    break
            if not ok or frame is None:
                raise RuntimeError("the camera stream did not open")
            ok, buf = cv2.imencode(".jpg", frame)
            if not ok:
                raise RuntimeError("could not encode the picture")
            return buf.tobytes()
        finally:
            cap.release()


# ── HTTP backends ─────────────────────────────────────────────────────────────
class _HttpBackend:
    brand = "http"
    default_port = 80

    def __init__(self, cfg: dict):
        self.ip = str(cfg.get("ip") or "").strip()
        if not self.ip:
            raise PrinterError("No printer is set up yet. Open the 3D PRINTER settings and "
                               "add the printer's address first.")
        host = self.ip if "://" in self.ip else f"http://{self.ip}"
        port = str(cfg.get("port") or "").strip()
        if port and ":" not in host.split("://", 1)[1]:
            host = f"{host}:{port}"
        self.base = host.rstrip("/")
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.cfg = cfg

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _req(self, method: str, path: str, **kw):
        import requests
        try:
            r = requests.request(method, self.base + path, headers=self._headers(),
                                 timeout=kw.pop("timeout", 8), **kw)
        except Exception as e:
            why = "timed out" if "timed out" in str(e).lower() else \
                "connection refused" if "refused" in str(e).lower() else str(e)[:80]
            raise PrinterError(f"I couldn't connect to the printer at {self.base} ({why}). Check "
                               "that it's powered on and on the same network.")
        if r.status_code in (401, 403):
            raise PrinterError("The printer refused the API key. Copy it again into the 3D "
                               "PRINTER settings.")
        if r.status_code == 409:
            raise PrinterError("The printer refused: that isn't possible in its current state "
                               "(for example nothing is printing).")
        if r.status_code >= 400:
            raise PrinterError(f"The printer answered with an error ({r.status_code}): "
                               f"{r.text[:160]}")
        try:
            return r.json() if r.content else {}
        except Exception:
            return {}

    def close(self):
        pass

    def snapshot(self, timeout: float = 8.0) -> bytes:
        import requests
        url = str(self.cfg.get("camera_url") or "").strip() or self.base + "/webcam/?action=snapshot"
        try:
            r = requests.get(url, timeout=timeout, headers=self._headers())
        except Exception as e:
            raise PrinterError(f"I couldn't open the printer camera: {e}")
        if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
            raise PrinterError("The printer has no camera snapshot at "
                               f"{url}. Put the right snapshot address in the settings.")
        return r.content

    def light_gcode(self, on: bool) -> str:
        key = "light_on_gcode" if on else "light_off_gcode"
        default = "SET_PIN PIN=caselight VALUE=1" if on else "SET_PIN PIN=caselight VALUE=0"
        if self.brand == "octoprint":
            default = "M355 S1" if on else "M355 S0"
        return str(self.cfg.get(key) or "").strip() or default


class OctoPrintBackend(_HttpBackend):
    brand = "octoprint"

    def status(self) -> Status:
        job = self._req("GET", "/api/job")
        try:
            prn = self._req("GET", "/api/printer")
        except PrinterError:
            prn = {}       # 409 when the printer is not connected to OctoPrint
        raw = str(job.get("state") or "")
        low = raw.lower()
        state = ("printing" if low.startswith("printing") else "paused" if "paus" in low
                 else "failed" if "error" in low or "offline" in low else
                 "idle" if low.startswith("operational") else low or "unknown")
        st = Status(online=True, raw_state=raw, state=state)
        prog = job.get("progress") or {}
        st.progress = prog.get("completion")
        left = prog.get("printTimeLeft")
        st.remaining_min = left / 60.0 if isinstance(left, (int, float)) else None
        st.file = str(((job.get("job") or {}).get("file") or {}).get("name") or "")
        temps = prn.get("temperature") or {}
        t0, bed, ch = temps.get("tool0") or {}, temps.get("bed") or {}, temps.get("chamber") or {}
        st.nozzle, st.nozzle_target = t0.get("actual"), t0.get("target")
        st.bed, st.bed_target = bed.get("actual"), bed.get("target")
        st.chamber = ch.get("actual")
        if st.progress is not None and st.state == "idle" and st.progress >= 100:
            st.state = "finished"
        return st

    def _job(self, command: str, action: Optional[str] = None):
        body = {"command": command}
        if action:
            body["action"] = action
        self._req("POST", "/api/job", json=body)

    def gcode(self, line: str):
        self._req("POST", "/api/printer/command", json={"commands": line.split("\n")})

    def pause(self):  self._job("pause", "pause")
    def resume(self): self._job("pause", "resume")
    def stop(self):   self._job("cancel")

    def set_nozzle(self, c):
        self._req("POST", "/api/printer/tool", json={"command": "target", "targets": {"tool0": int(c)}})

    def set_bed(self, c):
        self._req("POST", "/api/printer/bed", json={"command": "target", "target": int(c)})

    def cooldown(self):
        self.set_nozzle(0)
        self.set_bed(0)

    def set_speed(self, level: int):
        self.gcode(f"M220 S{SPEED_PERCENT.get(int(level), 100)}")

    def set_fan(self, percent: int):
        self.gcode(f"M106 S{round(max(0, min(100, percent)) * 255 / 100)}")

    def set_light(self, on: bool):
        self.gcode(self.light_gcode(on))

    def home(self):
        self._req("POST", "/api/printer/printhead", json={"command": "home", "axes": ["x", "y", "z"]})


class MoonrakerBackend(_HttpBackend):
    brand = "moonraker"

    def status(self) -> Status:
        q = ("/printer/objects/query?print_stats&virtual_sdcard&display_status&extruder"
             "&heater_bed&fan&gcode_move")
        data = (self._req("GET", q).get("result") or {}).get("status") or {}
        ps = data.get("print_stats") or {}
        raw = str(ps.get("state") or "")
        state = {"standby": "idle", "printing": "printing", "paused": "paused",
                 "complete": "finished", "cancelled": "failed", "error": "failed"}.get(raw, raw or "unknown")
        st = Status(online=True, raw_state=raw, state=state)
        prog = (data.get("virtual_sdcard") or {}).get("progress")
        if prog is None:
            prog = (data.get("display_status") or {}).get("progress")
        st.progress = round(prog * 100, 1) if isinstance(prog, (int, float)) else None
        dur = ps.get("print_duration")
        if st.progress and st.progress > 1 and isinstance(dur, (int, float)) and dur > 0:
            st.remaining_min = dur * (100 - st.progress) / st.progress / 60.0
        st.file = str(ps.get("filename") or "")
        info = ps.get("info") or {}
        st.layer, st.total_layers = info.get("current_layer"), info.get("total_layer")
        ex, bed = data.get("extruder") or {}, data.get("heater_bed") or {}
        st.nozzle, st.nozzle_target = ex.get("temperature"), ex.get("target")
        st.bed, st.bed_target = bed.get("temperature"), bed.get("target")
        fan = (data.get("fan") or {}).get("speed")
        st.fan_percent = round(fan * 100) if isinstance(fan, (int, float)) else None
        if raw == "error":
            st.error = str(ps.get("message") or "")
        return st

    def gcode(self, line: str):
        import urllib.parse
        self._req("POST", "/printer/gcode/script?script=" + urllib.parse.quote(line), timeout=20)

    def pause(self):  self._req("POST", "/printer/print/pause")
    def resume(self): self._req("POST", "/printer/print/resume")
    def stop(self):   self._req("POST", "/printer/print/cancel")

    def set_nozzle(self, c): self.gcode(f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={int(c)}")
    def set_bed(self, c):    self.gcode(f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={int(c)}")

    def cooldown(self):
        self.gcode("TURN_OFF_HEATERS")

    def set_speed(self, level: int):
        self.gcode(f"M220 S{SPEED_PERCENT.get(int(level), 100)}")

    def set_fan(self, percent: int):
        self.gcode(f"M106 S{round(max(0, min(100, percent)) * 255 / 100)}")

    def set_light(self, on: bool):
        self.gcode(self.light_gcode(on))

    def home(self):
        self.gcode("G28")


# ── One shared backend instance ───────────────────────────────────────────────
_backend = None
_backend_key = None
_backend_lock = threading.Lock()


def backend(values: Optional[dict] = None):
    """The backend for the saved (or given) settings; reused while they stay the same."""
    global _backend, _backend_key
    cfg = config(values)
    brand = str(cfg.get("brand") or "bambu").strip().lower()
    key = json.dumps(cfg, sort_keys=True, default=str)
    with _backend_lock:
        if values is None and _backend is not None and _backend_key == key:
            return _backend
        if brand in ("bambu", "bambu lab", "bambulab"):
            b = BambuBackend(cfg)
        elif brand == "octoprint":
            b = OctoPrintBackend(cfg)
        elif brand in ("moonraker", "klipper", "creality", "creality/klipper", "mainsail", "fluidd"):
            b = MoonrakerBackend(cfg)
        else:
            raise PrinterError(f"I don't know the printer type '{brand}'. Supported: bambu, "
                               "octoprint, moonraker (Klipper).")
        if values is None:
            if _backend is not None:
                try:
                    _backend.close()
                except Exception:
                    pass
            _backend, _backend_key = b, key
        return b


def describe(st: Status) -> str:
    """A short factual report, for the model to phrase."""
    bits = []
    name = f" '{st.file}'" if st.file else ""
    if st.state in ("printing", "paused", "preparing"):
        bits.append(f"The print{name} is {st.state}")
        if st.progress is not None:
            bits.append(f"{st.progress:.0f}% done")
        if st.remaining_min is not None and st.state != "preparing":
            bits.append(f"about {fmt_minutes(st.remaining_min)} left")
        if st.layer and st.total_layers:
            bits.append(f"layer {st.layer} of {st.total_layers}")
    elif st.state == "finished":
        bits.append(f"The last print{name} has finished")
    elif st.state == "failed":
        bits.append(f"The last print{name} failed or was stopped")
    elif st.state == "idle":
        bits.append("The printer is idle")
    else:
        bits.append(f"The printer reports '{st.raw_state or st.state}'")
    temps = []
    if st.nozzle is not None:
        temps.append(f"nozzle {st.nozzle:.0f}°C" + (f" (target {st.nozzle_target:.0f})" if st.nozzle_target else ""))
    if st.bed is not None:
        temps.append(f"bed {st.bed:.0f}°C" + (f" (target {st.bed_target:.0f})" if st.bed_target else ""))
    if st.chamber:
        temps.append(f"chamber {st.chamber:.0f}°C")
    if temps:
        bits.append(", ".join(temps))
    if st.speed_level:
        bits.append(f"speed {SPEED_NAMES.get(st.speed_level, st.speed_level)}")
    if st.error:
        bits.append(st.error)
    return "; ".join(bits) + "."


def fmt_minutes(m: float) -> str:
    m = max(0, int(round(m)))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} h {mm} min" if mm else f"{h} h"
