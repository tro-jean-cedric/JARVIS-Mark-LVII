"""core/remote_mode.py — keeping this computer reachable while the user is away.

    keep awake        the system does not go to sleep while JARVIS runs (the
                      screen still turns off on its own schedule):
                        Windows  SetThreadExecutionState held by one thread
                        macOS    caffeinate -i -w <our pid>
                        Linux    systemd-inhibit --what=sleep
    lock when idle    when nobody has touched the computer for N minutes —
                      about when the screen turns off — lock it, so nobody at
                      home uses an unlocked desktop while it stays awake
    screen off now    turn the display off (the computer stays on)
    hibernate         "turn it off completely" — the caller puts this behind
                      core.confirm; this module only does it
    Wake-on-LAN       this PC's MAC address, how to switch WoL on, the settings
                      the cloud agent (plugins/cloud_agent.py) syncs so it can
                      wake the PC, and a magic-packet sender

Settings live in plugin_config["remote_mode"] through memory.config_manager:
keep_awake (bool), lock_when_idle (bool), lock_minutes (int).

No Qt here; every function is safe on a worker thread and never raises to
its caller for an expected failure — it returns (ok, message).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid

NS = "remote_mode"
_OS = platform.system()          # Windows | Darwin | Linux
DEFAULT_LOCK_MINUTES = 10

_lock = threading.Lock()
_awake: dict = {"proc": None, "thread": None, "stop": None, "since": 0.0}
_idle: dict = {"thread": None, "stop": None, "locked_at_idle": False}


def _log(text: str) -> None:
    print(f"[RemoteMode] {text}")


# ── settings ─────────────────────────────────────────────────────────────────
def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NS)
    except Exception:
        cfg = {}
    try:
        minutes = int(cfg.get("lock_minutes") or DEFAULT_LOCK_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_LOCK_MINUTES
    return {"keep_awake": bool(cfg.get("keep_awake", False)),
            "lock_when_idle": bool(cfg.get("lock_when_idle", False)),
            "lock_minutes": max(1, min(240, minutes))}


def save_settings(**values) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, values)


# ── keep awake ───────────────────────────────────────────────────────────────
def awake_supported() -> bool:
    if _OS == "Windows":
        return True
    if _OS == "Darwin":
        return shutil.which("caffeinate") is not None
    if _OS == "Linux":
        return shutil.which("systemd-inhibit") is not None and os.path.isdir("/run/systemd/system")
    return False


def is_awake_held() -> bool:
    with _lock:
        t, p = _awake["thread"], _awake["proc"]
        if t is not None and t.is_alive():
            return True
        return p is not None and p.poll() is None


def _windows_hold(stop: threading.Event, ready: threading.Event, result: list) -> None:
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    k32 = ctypes.windll.kernel32
    k32.SetThreadExecutionState.restype = ctypes.c_uint32
    k32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    # The return value is the PREVIOUS state (0 = failure, but also possibly a
    # fresh thread's state) — so set it twice and check the second answer.
    prev = k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    result.append(bool(prev & ES_SYSTEM_REQUIRED))
    ready.set()
    if not prev:
        return
    try:
        # The request belongs to this thread: it must stay alive to keep it.
        while not stop.wait(60):
            k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    finally:
        k32.SetThreadExecutionState(ES_CONTINUOUS)


def _posix_command() -> list[str]:
    pid = os.getpid()
    if _OS == "Darwin":
        return ["caffeinate", "-i", "-w", str(pid)]
    return ["systemd-inhibit", "--what=sleep", "--mode=block", "--who=JARVIS",
            "--why=Remote Mode: reachable while you are away",
            "sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 30; done"]


def keep_awake(on: bool) -> tuple[bool, str]:
    """Start or stop holding the system awake. Checked: says what really happened."""
    if not on:
        with _lock:
            stop, proc = _awake["stop"], _awake["proc"]
            _awake.update(proc=None, thread=None, stop=None, since=0.0)
        if stop is not None:
            stop.set()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True, "The computer may sleep as usual again."
    if is_awake_held():
        return True, "The computer is already being kept awake."
    if not awake_supported():
        return False, ("This system gives no way to keep it awake from here — change the sleep "
                       "setting in the system's own power settings.")
    if _OS == "Windows":
        stop, ready, result = threading.Event(), threading.Event(), []
        t = threading.Thread(target=_windows_hold, args=(stop, ready, result),
                             daemon=True, name="remote-mode-awake")
        t.start()
        ready.wait(3)
        if not (result and result[0]):
            stop.set()
            return False, "Windows did not accept keeping the computer awake."
        with _lock:
            _awake.update(thread=t, stop=stop, since=time.time())
        return True, "The computer stays awake now (the screen may still turn off)."
    try:
        proc = subprocess.Popen(_posix_command(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as e:
        return False, f"Could not start the keep-awake helper: {e}"
    try:
        out, err = proc.communicate(timeout=1.0)      # still running = holding
        msg = (err or out or b"").decode("utf-8", "replace").strip()[:200]
        _log(f"keep-awake helper ended at once (code {proc.returncode}): {msg}")
        return False, f"The system refused to stay awake{': ' + msg if msg else ''}."
    except subprocess.TimeoutExpired:
        pass
    with _lock:
        _awake.update(proc=proc, since=time.time())
    return True, "The computer stays awake now (the screen may still turn off)."


# ── idle time, screen off, lock ──────────────────────────────────────────────
def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input; None when unknown."""
    try:
        if _OS == "Windows":
            import ctypes

            class _LII(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            lii = _LII()
            lii.cbSize = ctypes.sizeof(_LII)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
        if _OS == "Darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
            return int(m.group(1)) / 1e9 if m else None
        if shutil.which("xprintidle") and os.environ.get("DISPLAY"):
            out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / 1000.0 if out.returncode == 0 else None
    except Exception as e:
        _log(f"idle time unavailable: {e}")
    return None


def lock_now() -> tuple[bool, str]:
    try:
        if _OS == "Windows":
            import ctypes
            ok = bool(ctypes.windll.user32.LockWorkStation())
            return ok, "Locked." if ok else "Windows did not lock."
        from actions.computer_settings import lock_screen
        lock_screen()
        return True, "Locked."
    except Exception as e:
        return False, f"Could not lock the computer: {e}"


def screen_off_now() -> tuple[bool, str]:
    try:
        if _OS == "Windows":
            import ctypes
            # WM_SYSCOMMAND / SC_MONITORPOWER / 2 = off, to every top window
            ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
            return True, "The screen is off."
        if _OS == "Darwin":
            r = subprocess.run(["pmset", "displaysleepnow"], capture_output=True, text=True, timeout=10)
            return r.returncode == 0, ("The screen is off." if r.returncode == 0
                                       else f"The screen could not be turned off: {r.stderr.strip()}")
        if shutil.which("xset") and os.environ.get("DISPLAY"):
            r = subprocess.run(["xset", "dpms", "force", "off"], capture_output=True, text=True, timeout=10)
            return r.returncode == 0, ("The screen is off." if r.returncode == 0
                                       else f"The screen could not be turned off: {r.stderr.strip()}")
    except Exception as e:
        return False, f"The screen could not be turned off: {e}"
    return False, "The screen could not be turned off from here on this system."


def _idle_loop(stop: threading.Event) -> None:
    locked = False
    while not stop.wait(20):
        s = settings()
        idle = idle_seconds()
        if idle is None:
            continue
        if idle < 30:
            locked = False                      # someone is back
        elif not locked and idle >= s["lock_minutes"] * 60:
            ok, msg = lock_now()
            locked = True                       # once per idle stretch
            _log(f"idle {int(idle)} s — {msg}")


def lock_when_idle(on: bool) -> tuple[bool, str]:
    with _lock:
        t, stop = _idle["thread"], _idle["stop"]
        if not on:
            if stop is not None:
                stop.set()
            _idle.update(thread=None, stop=None)
            return True, "The computer will no longer lock by itself."
        if t is not None and t.is_alive():
            return True, "Already locking when idle."
    if idle_seconds() is None:
        return False, ("This system does not tell how long it has been idle "
                       + ("(install xprintidle)" if _OS == "Linux" else "") + ", so it cannot lock by itself.")
    stop = threading.Event()
    t = threading.Thread(target=_idle_loop, args=(stop,), daemon=True, name="remote-mode-lock")
    t.start()
    with _lock:
        _idle.update(thread=t, stop=stop)
    return True, f"It locks itself after {settings()['lock_minutes']} minutes without anyone using it."


# ── hibernate ────────────────────────────────────────────────────────────────
def hibernate_command() -> list[str] | None:
    if _OS == "Windows":
        return ["shutdown", "/h"]
    if _OS == "Darwin":
        # macOS chooses between sleep and hibernation itself (pmset hibernatemode).
        return ["pmset", "sleepnow"]
    if _OS == "Linux" and shutil.which("systemctl"):
        return ["systemctl", "hibernate"]
    return None


def hibernate_now() -> str:
    """Do it — only ever called after the user confirmed (core.confirm)."""
    cmd = hibernate_command()
    if cmd is None:
        return "Hibernating is not available on this system."
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"Hibernate failed: {e}"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:200]
        return f"Hibernate failed (code {r.returncode}){': ' + err if err else ''}."
    return "Hibernating."


# ── Wake-on-LAN ──────────────────────────────────────────────────────────────
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}$")


def mac_address() -> str:
    """This computer's MAC (the adapter Python picks), '' when only a random
    one is known."""
    node = uuid.getnode()
    if (node >> 40) & 1:
        return ""
    return ":".join(f"{(node >> s) & 0xff:02X}" for s in range(40, -1, -8))


def normalize_mac(mac: str) -> str:
    mac = str(mac or "").strip()
    if not _MAC_RE.match(mac):
        raise ValueError("A MAC address has 12 hexadecimal digits, like 1A:2B:3C:4D:5E:6F.")
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, host: str = "255.255.255.255", port: int = 9) -> tuple[bool, str]:
    try:
        pkt = magic_packet(mac)
        port = int(port or 9)
        if not 1 <= port <= 65535:
            raise ValueError("The port must be a number from 1 to 65535.")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sent = s.sendto(pkt, (host or "255.255.255.255", port))
        if sent != len(pkt):
            return False, "The wake packet was not sent completely."
        return True, f"Wake packet sent to {normalize_mac(mac)} via {host}:{port}."
    except ValueError as e:
        return False, str(e)
    except OSError as e:
        return False, f"Could not send the wake packet: {e}"


def wol_settings() -> dict:
    """The Wake-on-LAN fields the cloud agent syncs (plugins/cloud_agent.py)."""
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config("cloud_agent")
    except Exception:
        cfg = {}
    try:
        from core import smart_plug
        plug = smart_plug.configured(cfg)
    except Exception:
        plug = False
    return {"wake_mac": str(cfg.get("wake_mac") or "").strip(),
            "wake_host": str(cfg.get("wake_host") or "").strip(),
            "wake_port": str(cfg.get("wake_port") or "9").strip(),
            "wake_url": str(cfg.get("wake_url") or "").strip(),
            "plug": plug}


def save_wol_settings(mac: str = None, host: str = None, port=None, url: str = None) -> tuple[bool, str]:
    values = {}
    try:
        if mac is not None:
            values["wake_mac"] = normalize_mac(mac) if str(mac).strip() else ""
        if host is not None:
            h = str(host).strip()
            if h and not re.match(r"^[A-Za-z0-9.\-]+$", h):
                raise ValueError("That address does not look right — an IP address or a name "
                                 "like home.example.net.")
            values["wake_host"] = h
        if port is not None:
            p = int(str(port).strip() or 9)
            if not 1 <= p <= 65535:
                raise ValueError("The port must be a number from 1 to 65535.")
            values["wake_port"] = str(p)
        if url is not None:
            u = str(url).strip()
            if u and not u.lower().startswith(("http://", "https://")):
                raise ValueError("The link must start with http:// or https://.")
            values["wake_url"] = u
    except (TypeError, ValueError) as e:
        return False, str(e)
    if not values:
        return False, "Nothing to change."
    from memory.config_manager import save_plugin_config
    save_plugin_config("cloud_agent", values)
    return True, "Saved. The cloud agent passes it on at its next sync."


def wol_howto() -> str:
    mac = wol_settings()["wake_mac"] or mac_address() or "unknown (look it up in the network settings)"
    steps = {
        "Windows": "Device Manager → Network adapters → your wired adapter → Power Management: "
                   "allow this device to wake the computer; Advanced: enable 'Wake on Magic Packet'. "
                   "Turning Fast Startup off (Control Panel → Power Options) helps waking from shutdown.",
        "Darwin": "System Settings → Energy / Battery → Options: turn on 'Wake for network access'. "
                  "Macs wake from sleep this way, not from a full shutdown.",
        "Linux": "Run: sudo ethtool -s <interface> wol g (and make it persistent in your network "
                 "manager), then check that the BIOS allows waking from the network.",
    }.get(_OS, "Enable Wake-on-LAN in the network adapter settings and the BIOS.")
    try:
        if int(mac[:2], 16) & 0x02:      # locally administered: virtual/private
            mac += (" (this looks like a virtual or private address — use the wired "
                    "adapter's address from the network settings instead)")
    except ValueError:
        pass
    return (f"This computer's MAC address: {mac}. {steps} Also enable Wake-on-LAN in the BIOS/UEFI "
            f"(often 'Wake on LAN' or 'Power On by PCI-E'); it needs a network cable, not Wi-Fi. "
            f"From another device on the same network, send a magic packet to that MAC address; "
            f"from outside, your router must forward a UDP port (often 9) to this computer, and the "
            f"cloud agent's wake settings (public address and port) let it do that for you.")


# ── status and start-up ──────────────────────────────────────────────────────
def status() -> dict:
    s = settings()
    w = wol_settings()
    return {
        "keep_awake_setting": s["keep_awake"], "kept_awake_now": is_awake_held(),
        "awake_supported": awake_supported(),
        "lock_when_idle": s["lock_when_idle"], "lock_minutes": s["lock_minutes"],
        "locking_now": bool(_idle["thread"] is not None and _idle["thread"].is_alive()),
        "hibernate_supported": hibernate_command() is not None,
        "mac": w["wake_mac"] or mac_address(), "wol_host": w["wake_host"],
        "wol_port": w["wake_port"], "wake_url": bool(w["wake_url"]), "plug": w["plug"],
    }


def status_text() -> str:
    st = status()
    bits = [("kept awake" if st["kept_awake_now"] else "may sleep as usual")
            + ("" if st["awake_supported"] else " (keeping awake is not supported here)"),
            (f"locks after {st['lock_minutes']} idle minutes" if st["locking_now"]
             else "does not lock by itself"),
            f"MAC {st['mac'] or 'unknown'}"]
    if st["wol_host"] or st["wake_url"]:
        bits.append("cloud wake is set up")
    elif st["plug"]:
        bits.append("the server can start it with the smart plug (after a yes on the phone)")
    return "Remote Mode: " + "; ".join(bits) + "."


def apply_settings(values: dict) -> tuple[bool, str]:
    """Switch keep-awake and lock-when-idle to match `values` now (the
    settings panel's APPLY NOW; the panel has already saved them)."""
    v = dict(values or {})
    try:
        minutes = max(1, min(240, int(str(v.get("lock_minutes") or DEFAULT_LOCK_MINUTES).strip())))
    except ValueError:
        return False, "Minutes before locking must be a whole number (1 to 240)."
    save_settings(lock_minutes=minutes)
    problems = []
    ok, msg = keep_awake(bool(v.get("keep_awake")))
    if not ok:
        problems.append(msg)
    lock_when_idle(False)                     # restart so a new minute count takes effect
    if v.get("lock_when_idle"):
        ok, msg = lock_when_idle(True)
        if not ok:
            problems.append(msg)
    if problems:
        return False, " ".join(problems)
    return True, status_text()


def settings_section() -> dict:
    """Remote Mode's section in ⚙ PLUGIN SETTINGS (main.py adds it to the
    plugins' sections): the saved switches, APPLY NOW, and the guide."""
    s = settings()
    try:
        from core import setup_guides
        guide = setup_guides.all_guides().get(NS)
    except Exception:
        guide = None
    return {
        "plugin": NS, "namespace": NS, "title": "Remote Mode",
        "fields": [
            {"key": "keep_awake", "type": "toggle", "default": False,
             "label": "Keep the computer awake while the assistant runs (the screen still turns off)"},
            {"key": "lock_when_idle", "type": "toggle", "default": False,
             "label": "Lock it when nobody has used it for a while"},
            {"key": "lock_minutes", "type": "text", "default": str(DEFAULT_LOCK_MINUTES),
             "label": "Minutes without use before it locks"},
        ],
        "values": {"keep_awake": s["keep_awake"], "lock_when_idle": s["lock_when_idle"],
                   "lock_minutes": str(s["lock_minutes"])},
        "action": {"label": "APPLY NOW", "run": apply_settings},
        "guide": guide,
    }


def start_from_settings(log=None) -> None:
    """Apply the saved switches when the app starts. Safe to call twice."""
    s = settings()
    say = log or _log
    if s["keep_awake"]:
        ok, msg = keep_awake(True)
        say(f"SYS: Remote Mode — {msg}")
    if s["lock_when_idle"]:
        ok, msg = lock_when_idle(True)
        if not ok:
            say(f"SYS: Remote Mode — {msg}")
