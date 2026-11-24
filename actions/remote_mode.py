"""remote_mode — keep this computer reachable while the user is away.

The work is in core/remote_mode.py; this turns a tool call into it. Switches
are saved, so they come back on the next start (main.py applies them).
Hibernating is irreversible enough to go through core.confirm: the user
presses CONFIRM on the HUD (or Yes on the phone, through telegram_remote).
"""
from __future__ import annotations


def remote_mode(parameters: dict, player=None) -> str:
    from core import remote_mode as rm
    a = dict(parameters or {})
    action = str(a.get("action") or "status").strip().lower()
    try:
        if action == "status":
            return rm.status_text()

        if action in ("keep_awake", "awake"):
            on = a.get("on", True) is not False
            ok, msg = rm.keep_awake(on)
            if ok:
                rm.save_settings(keep_awake=on)
            return msg

        if action == "lock_when_idle":
            on = a.get("on", True) is not False
            if a.get("minutes"):
                rm.save_settings(lock_minutes=max(1, min(240, int(a["minutes"]))))
            ok, msg = rm.lock_when_idle(on)
            if ok:
                rm.save_settings(lock_when_idle=on)
            return msg

        if action == "lock_now":
            return rm.lock_now()[1]

        if action == "screen_off":
            return rm.screen_off_now()[1]

        if action == "hibernate":
            if rm.hibernate_command() is None:
                return "Hibernating is not available on this system."
            from core import confirm
            if confirm.pending_title():
                return "Another confirmation is already waiting; answer that one first."
            return confirm.request(
                "remote_mode.hibernate", "Turn the computer off completely (hibernate)",
                "Open apps keep their state, but the assistant goes offline until the "
                "computer is switched on again (power button, Wake-on-LAN or a smart plug).",
                rm.hibernate_now)

        if action == "wake_info":
            return rm.wol_howto()

        if action == "wake_setup":
            ok, msg = rm.save_wol_settings(
                mac=a.get("mac"), host=a.get("host"), port=a.get("port"), url=a.get("url"))
            return msg

        if action == "send_wake":
            mac = str(a.get("mac") or "").strip()
            if not mac:
                return "Which computer? Give its MAC address."
            ok, msg = rm.send_wol(mac, str(a.get("host") or "255.255.255.255"),
                                  int(a.get("port") or 9))
            return msg

        return ("I can: status, keep_awake (on true/false), lock_when_idle (on, minutes), "
                "lock_now, screen_off, hibernate, wake_info, wake_setup, send_wake.")
    except Exception as e:
        return f"Remote Mode failed: {e}"


TOOL = {
    "name": "remote_mode",
    "description": (
        "Remote Mode — keep this computer reachable while the user is away. "
        "keep_awake (on true/false): stop it going to sleep while the assistant runs (the "
        "screen still turns off); lock_when_idle (on, minutes): lock it after that many "
        "minutes nobody used it; lock_now; screen_off; hibernate: 'turn it off completely' "
        "(the user confirms on screen or on the phone); wake_info: this PC's MAC address and "
        "how to enable Wake-on-LAN so another device can switch it on; wake_setup (host = "
        "public IP/DDNS, port, mac, or url of a smart plug): saved for the cloud agent to wake "
        "it; send_wake (mac, host, port): send a Wake-on-LAN packet to another computer; "
        "status: what is on."),
    "brief": "Keep the PC awake, lock when idle, hibernate, Wake-on-LAN info/setup.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": [
                "status", "keep_awake", "lock_when_idle", "lock_now", "screen_off",
                "hibernate", "wake_info", "wake_setup", "send_wake"]},
            "on": {"type": "BOOLEAN"},
            "minutes": {"type": "INTEGER"},
            "mac": {"type": "STRING"},
            "host": {"type": "STRING"},
            "port": {"type": "INTEGER"},
            "url": {"type": "STRING"},
        },
        "required": ["action"],
    },
    "handler": remote_mode,
}
