"""
Self-test for starting the home computer with its smart plug
(core/smart_plug.py) and how the cloud server uses it (core/cloud.py, the
phone confirmation in plugins/telegram_remote.py, plugins/cloud_agent.py).

    python -m core._plug_tests

No request leaves the computer: each service's HTTP is a stand-in.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import hmac
import io
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import smart_plug as sp  # noqa: E402

_failures: list[str] = []
_log = io.StringIO()
SECRETS = ("SHELLYKEY-123456", "TUYASECRET-abcdef", "VMTOKEN-987654")


def check(name: str, cond: bool, detail: str = "") -> None:
    sys.__stdout__.write(f"{'PASS' if cond else 'FAIL'}  {name}"
                         + (f"  — {detail}" if detail and not cond else "") + "\n")
    if not cond:
        _failures.append(name)


class Http:
    """Records requests; `answer(method, url, headers, body)` -> (status, data)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": json.loads(body) if body else None})
        r = self.answer(method, url, headers or {}, body)
        if isinstance(r, Exception):
            raise r
        return r


SHELLY = {"plug_kind": "shelly", "shelly_host": "https://shelly-103-eu.shelly.cloud/",
          "shelly_auth_key": SECRETS[0], "shelly_device_id": "a8032ab12345", "shelly_channel": "0"}
TUYA = {"plug_kind": "tuya", "tuya_region": "eu", "tuya_client_id": "clientid123",
        "tuya_secret": SECRETS[1], "tuya_device_id": "bf1234567890abcdef"}
VM = {"plug_kind": "voicemonkey", "voicemonkey_token": SECRETS[2], "voicemonkey_device": "pc-power"}


def use(http):
    sp._http = http
    return http


def test_settings() -> None:
    check("settings: complete Shelly is configured", sp.configured(SHELLY))
    check("settings: missing fields named", sp.missing({"plug_kind": "tuya", "tuya_client_id": "x"})
          == ["Tuya Access Secret", "Tuya device ID"])
    check("settings: 'none' is not configured", not sp.configured({"plug_kind": "none"}))
    ok, text = sp.check({"plug_kind": "shelly"})
    check("check: incomplete says what is missing", not ok and "Shelly cloud key" in text, text)


def test_shelly() -> None:
    h = use(Http(lambda m, u, hd, b: (200, None)))
    ok, text = sp.switch(SHELLY, False)
    c = h.calls[0]
    q = parse_qs(urlsplit(c["url"]).query)
    check("shelly: v2 switch address on the user's server",
          c["url"].startswith("https://shelly-103-eu.shelly.cloud/v2/devices/api/set/switch?")
          and c["method"] == "POST", c["url"])
    check("shelly: key only in auth_key", q.get("auth_key") == [SECRETS[0]])
    check("shelly: body", c["body"] == {"id": "a8032ab12345", "channel": 0, "on": False}
          and c["headers"].get("Content-Type") == "application/json", str(c))
    check("shelly: switch reported", ok and "told off" in text, text)

    for bad in ("evil.example.com", "shelly.cloud.evil.com", "x.shelly.cloud@evil.com"):
        h = use(Http(lambda m, u, hd, b: (200, None)))
        ok, text = sp.switch({**SHELLY, "shelly_host": bad}, True)
        check(f"shelly: key never sent to {bad}", not ok and not h.calls and ".shelly.cloud" in text, text)

    for status, word in ((401, "refused the key"), (429, "too many requests"), (500, "HTTP 500")):
        use(Http(lambda m, u, hd, b, s=status: (s, None)))
        ok, text = sp.switch(SHELLY, True)
        check(f"shelly: HTTP {status} explained", not ok and word in text, text)

    use(Http(lambda m, u, hd, b: (200, [{"id": "a8032ab12345", "online": 1}])))
    ok, text = sp.check(SHELLY)
    check("shelly check: online, nothing switched",
          ok and "online" in text and "/set/" not in sp._http.calls[0]["url"], text)
    use(Http(lambda m, u, hd, b: (200, [{"id": "a8032ab12345", "online": 0}])))
    check("shelly check: offline said", "offline" in sp.check(SHELLY)[1])
    use(Http(lambda m, u, hd, b: (200, [])))
    check("shelly check: unknown device", not sp.check(SHELLY)[0])


def test_cycle() -> None:
    pauses = []
    sp._sleep = pauses.append
    h = use(Http(lambda m, u, hd, b: (200, None)))
    ok, text = sp.cycle(SHELLY)
    ons = [c["body"]["on"] for c in h.calls]
    check("cycle: off, pause, on", ok and ons == [False, True] and pauses == [sp.POWER_PAUSE_S], f"{ons} {pauses}")
    n = {"i": 0}

    def second_fails(m, u, hd, b):
        n["i"] += 1
        return (200, None) if n["i"] == 1 else (500, None)
    use(Http(second_fails))
    ok, text = sp.cycle(SHELLY)
    check("cycle: off worked, on failed -> says so plainly", not ok and "switched OFF" in text
          and "maker's app" in text, text)
    use(Http(lambda m, u, hd, b: (401, None)))
    ok, text = sp.cycle(SHELLY)
    check("cycle: off refused -> nothing more", not ok and "could not be switched off" in text, text)


def test_tuya() -> None:
    sp._tuya_tokens.clear()

    def answer(m, u, hd, b):
        path = urlsplit(u).path
        if path == "/v1.0/token":
            return 200, {"success": True, "result": {"access_token": "TOKEN1", "expire_time": 7200}}
        if path.endswith("/status"):
            return 200, {"success": True, "result": [{"code": "countdown_1", "value": 0},
                                                     {"code": "switch_1", "value": True}]}
        if path.endswith("/commands"):
            return 200, {"success": True, "result": True}
        return 404, None
    h = use(Http(answer))
    ok, text = sp.switch(TUYA, True)
    paths = [urlsplit(c["url"]).path for c in h.calls]
    check("tuya: token, status, command", ok and paths == ["/v1.0/token", f"/v1.0/devices/{TUYA['tuya_device_id']}/status",
                                                         f"/v1.0/devices/{TUYA['tuya_device_id']}/commands"], str(paths))
    check("tuya: EU data centre", h.calls[0]["url"].startswith("https://openapi.tuyaeu.com/"))
    cmd = h.calls[2]
    check("tuya: the switch's own code and value", cmd["body"] == {"commands": [{"code": "switch_1", "value": True}]})
    hd = cmd["headers"]
    check("tuya: headers", hd["client_id"] == "clientid123" and hd["sign_method"] == "HMAC-SHA256"
          and hd["access_token"] == "TOKEN1" and SECRETS[1] not in json.dumps(hd), str(hd))
    # The signature, worked out here independently from Tuya's published rule.
    body = json.dumps(cmd["body"]).encode()
    string_to_sign = "POST\n" + hashlib.sha256(body).hexdigest() + "\n\n" + urlsplit(cmd["url"]).path
    expect = hmac.new(SECRETS[1].encode(), ("clientid123" + "TOKEN1" + hd["t"] + hd["nonce"]
                                            + string_to_sign).encode(), hashlib.sha256).hexdigest().upper()
    check("tuya: request signature", hd["sign"] == expect)
    tok = h.calls[0]["headers"]
    empty = hashlib.sha256(b"").hexdigest()
    expect_tok = hmac.new(SECRETS[1].encode(), ("clientid123" + tok["t"] + tok["nonce"] + "GET\n" + empty
                                                + "\n\n/v1.0/token?grant_type=1").encode(),
                          hashlib.sha256).hexdigest().upper()
    check("tuya: token signature (no token, query included)", tok["sign"] == expect_tok and "access_token" not in tok)
    # Tuya's own worked examples (developer.tuya.com, "Sign Requests for Cloud
    # Authorization"): the token request and a request with a token, both
    # signing two extra headers; the query below is deliberately out of order.
    hdrs = (("area_id", "29a33e8796834b1efa6"), ("call_id", "8afdb70ab2ed11eb85290242ac130003"))
    args = ("1KAD46OrT9HafiKdsXeg", "4OHBOnWOqaEC1mWXOpVL3yV50s0qGSRC", "1588925778000",
            "5138cc3a9033d69856923fd07b491173", "GET")
    check("tuya: Tuya's published token example", sp.tuya_sign(
        *args, "/v1.0/token?grant_type=1", b"", "", hdrs)
        == "9E48A3E93B302EEECC803C7241985D0A34EB944F40FB573C7B5C2A82158AF13E")
    check("tuya: Tuya's published business example (query sorted)", sp.tuya_sign(
        *args, "/v2.0/apps/schema/users?page_size=50&page_no=1", b"",
        "3f4eda2bdec17232f67c0b188af3eec1", hdrs)
        == "AE4481C692AA80B25F3A7E12C3A5FD9BBF6251539DD78E565A1A72A508A88784")
    n = len(h.calls)
    sp.switch(TUYA, False)
    check("tuya: token reused", [urlsplit(c["url"]).path for c in h.calls[n:]][0] != "/v1.0/token")

    sp._tuya_tokens.clear()
    use(Http(lambda m, u, hd, b: (200, {"success": False, "code": 1004, "msg": "sign invalid"})))
    ok, text = sp.switch(TUYA, True)
    check("tuya: refused keys explained", not ok and "Access ID / Secret" in text, text)
    sp._tuya_tokens.clear()

    def no_switch(m, u, hd, b):
        if "token" in u:
            return 200, {"success": True, "result": {"access_token": "T", "expire_time": 7200}}
        return 200, {"success": True, "result": [{"code": "cur_power", "value": 5}]}
    use(Http(no_switch))
    ok, text = sp.switch(TUYA, True)
    check("tuya: a device without a switch refused", not ok and "no on/off switch" in text, text)
    ok, text = sp.check({**TUYA, "tuya_region": "mars"})
    check("tuya: unknown data centre", not ok and "data centre" in text, text)


def test_voicemonkey() -> None:
    h = use(Http(lambda m, u, hd, b: (200, {"success": True})))
    ok, text = sp.cycle(VM)
    c = h.calls[0]
    q = parse_qs(urlsplit(c["url"]).query)
    check("voicemonkey: one v3 trigger runs the routine", ok and len(h.calls) == 1
          and c["url"].startswith("https://api-v3.voicemonkey.io/trigger?")
          and q == {"token": [SECRETS[2]], "device": ["pc-power"]}, c["url"])
    use(Http(lambda m, u, hd, b: (200, {"success": False})))
    check("voicemonkey: not accepted", not sp.cycle(VM)[0])
    n = len(h.calls)
    ok, text = sp.check(VM)
    check("voicemonkey: check runs nothing", ok and "cannot be tried" in text and len(h.calls) == n)


def test_real_http() -> None:
    saved = sp._http
    sp._http = _REAL_HTTP
    try:
        sp._http("GET", f"http://127.0.0.1:9/?auth_key={SECRETS[0]}")
        check("http: unreachable raises", False)
    except sp.PlugError as e:
        check("http: unreachable -> error type only, never the address", SECRETS[0] not in str(e)
              and "could not be reached" in str(e), str(e))
    finally:
        sp._http = saved


def test_stdlib_only() -> None:
    tree = ast.parse((ROOT / "core" / "smart_plug.py").read_text(encoding="utf-8"))
    tops = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            tops.add(node.module.split(".")[0])
    tops.discard("__future__")
    check("smart_plug uses only the standard library (it runs on a bare server)",
          tops <= set(sys.stdlib_module_names), str(tops - set(sys.stdlib_module_names)))


# ── the server ───────────────────────────────────────────────────────────────

def test_cloud() -> None:
    from core import cloud, confirm
    check("cloud: smart_plug.py goes to the server", "core/smart_plug.py" in cloud.MANIFEST)
    check("cloud: plug alone counts as a way to wake", cloud.wake_configured(SHELLY)
          and not cloud.signal_configured(SHELLY) and cloud.plug_configured(SHELLY))

    tmp = Path(tempfile.mkdtemp())
    state = cloud.CloudState(tmp / "state")
    woke, logs = [], []

    class Rt:
        def watch_wake(self, chat):
            woke.append(chat)

        def log(self, text):
            logs.append(text)

        def relay_output(self, *a):
            pass
    tb = cloud.ToolBox.__new__(cloud.ToolBox)
    tb.state, tb.rt, tb.workspace, tb._borrowed = state, Rt(), tmp, {}
    shown = []
    saved_bind = (confirm._show_cb, confirm._hide_cb, confirm._log_cb)
    confirm.bind(lambda t, d: shown.append((t, d)), lambda: None, logs.append)
    pauses = []
    sp._sleep = pauses.append
    ctx = {"origin": "telegram", "chat": 42}
    try:
        state.wake_cfg.update(lambda d: (d.clear(), d.update(SHELLY)))
        h = use(Http(lambda m, u, hd, b: (200, None)))
        out = tb.execute("home_computer", {"action": "wake"}, dict(ctx))
        check("wake (plug only): asks on the phone first, nothing switched",
              "CONFIRMATION_PENDING" in out and "phone" in out and not h.calls
              and shown and "smart plug" in shown[0][0], out)
        out2 = tb.execute("home_computer", {"action": "wake"}, dict(ctx))
        check("wake: no second question stacked", "already waiting" in out2, out2)
        confirm.resolve(False)
        time.sleep(0.2)
        check("declined: nothing switched", not h.calls and not woke)

        tb.execute("home_computer", {"action": "wake"}, dict(ctx))
        confirm.resolve(True)
        for _ in range(50):
            if woke:
                break
            time.sleep(0.05)
        check("confirmed: off, pause, on, then waits for the computer",
              [c["body"]["on"] for c in h.calls] == [False, True] and woke == [42] and pauses, f"{h.calls} {woke}")

        n = len(h.calls)
        out = tb.execute("home_computer", {"action": "command", "instruction": "open Steam"}, dict(ctx))
        check("command while off (plug only): asks, and queues nothing",
              "CONFIRMATION_PENDING" in out and "NOT queued" in out and not state.pending_commands()
              and len(h.calls) == n, out)
        confirm.resolve(False)

        state.wake_cfg.update(lambda d: d.update(wake_mac="AA:BB:CC:DD:EE:FF", wake_host="home.example.net"))
        sent = []
        saved_send = cloud.send_wake
        cloud.send_wake = lambda cfg: (sent.append(cfg) or "magic packet sent")
        try:
            shown.clear()
            out = tb.execute("home_computer", {"action": "wake"}, dict(ctx))
            check("wake with Wake-on-LAN too: the harmless way, no question",
                  sent and not shown and "wake signal sent" in out and len(h.calls) == n, out)
        finally:
            cloud.send_wake = saved_send

        decl = [d for d in cloud._DECLS if d["name"] == "home_computer"][0]
        check("declaration tells the model about the phone question", "yes on the phone" in decl["description"])
    finally:
        confirm.bind(*saved_bind)
        confirm.resolve(False)


def test_sync() -> None:
    from core import cloud
    from plugins import cloud_agent as ca
    check("desktop sends every plug key the server reads", set(ca._plug_keys()) == set(cloud.WAKE_PLUG_KEYS)
          and set(cloud.WAKE_PLUG_KEYS) <= {f["key"] for f in ca.PLUGIN_SETTINGS["fields"]})
    src = (ROOT / "core" / "cloud.py").read_text(encoding="utf-8")
    body = src[src.index("    def apply_sync"):src.index("    def _merge_memory")]
    check("server copies the plug keys it is sent", "WAKE_PLUG_KEYS" in body)


def test_confirm_binding() -> None:
    from core import cloud, confirm
    saved = (confirm._show_cb, confirm._hide_cb, confirm._log_cb)
    confirm.bind(None, None, None)
    tmp = Path(tempfile.mkdtemp())
    try:
        rt = cloud.Runtime(state_dir=tmp / "s", workspace=tmp / "w")
        check("server: confirmations bound to the phone at start", confirm._show_cb is not None)
        out = confirm.request("t", "Test", "d", lambda: "x", surface="phone")
        check("server: a confirmation is accepted and names the phone", "CONFIRMATION_PENDING" in out
              and "on the phone" in out, out)
        confirm.resolve(False)
        del rt
    except Exception as e:
        check("server runtime could be built for the binding test", False, f"{type(e).__name__}: {e}")
    try:
        confirm.bind(lambda t, d: None, lambda: None)
        out = confirm.request("t", "Test", "d", lambda: "x")
        check("desktop wording unchanged (HUD)", "on the HUD" in out and "on screen" in out, out)
        confirm.resolve(False)
    finally:
        confirm.bind(*saved)


def test_phone_buttons() -> None:
    """The bridge asks a waiting confirmation on the server even though the
    server's copy of the Telegram settings has no phone_control."""
    from plugins import telegram_remote as tr
    from core.cloud import HeadlessPlayer
    sent = []
    saved = (tr._enqueue, tr._turn_active, tr._cfg, tr.ops, dict(tr._relay), dict(tr._state))

    class Ops:
        @staticmethod
        def pending_confirmation():
            return "Switch the computer's smart plug off and on to start it"
    try:
        tr._enqueue = lambda chat, text, markup=None: sent.append((chat, text, markup))
        tr._turn_active = lambda: True
        tr._cfg = lambda: {}
        tr.ops = Ops
        tr._relay["player"] = HeadlessPlayer()
        tr._state.update(gen=99, turn_chat=42, turn_open=True, turn_until=time.time() + 60)
        tr._asks.clear()
        t = threading.Thread(target=tr._watch_loop, args=(99,), daemon=True)
        t.start()
        time.sleep(1.6)
        tr._state["gen"] = 0
        t.join(3)
        check("server: the phone gets Yes / No for the plug question",
              sent and "smart plug" in sent[0][1] and sent[0][2] and "inline_keyboard" in sent[0][2], str(sent))
    finally:
        tr._enqueue, tr._turn_active, tr._cfg, tr.ops = saved[:4]
        tr._relay.clear(); tr._relay.update(saved[4])
        tr._state.clear(); tr._state.update(saved[5])
        tr._asks.clear()


def test_desktop() -> None:
    from plugins import cloud_agent as ca
    h = use(Http(lambda m, u, hd, b: (200, [{"id": "a8032ab12345", "online": 1}])))
    ok, text = ca._test_connection({**SHELLY})
    check("CHECK CONNECTION: plug checked without switching", "Shelly knows the plug" in text
          and all("/set/" not in c["url"] for c in h.calls), text)
    ok, text = ca._test_connection({"plug_kind": "none"})
    check("CHECK CONNECTION: no plug, no plug line", "plug" not in text.lower(), text)
    from core import remote_mode as rm
    saved = rm.wol_settings
    rm.wol_settings = lambda: {"wake_mac": "", "wake_host": "", "wake_port": "9", "wake_url": "", "plug": True}
    try:
        check("Remote Mode status mentions the plug", "smart plug" in rm.status_text(), rm.status_text())
    finally:
        rm.wol_settings = saved
    from core import setup_guides
    name, g = setup_guides.find("how do I set up the smart plug")
    check("setup guide found by voice", name == "smart_plug" and any("BIOS" in str(s) for s in g["steps"]))


_REAL_HTTP = sp._http


def main_() -> int:
    saved_sleep = sp._sleep
    try:
        with contextlib.redirect_stdout(_log):
            test_settings()
            test_shelly()
            test_cycle()
            test_tuya()
            test_voicemonkey()
            test_real_http()
            test_stdlib_only()
            test_cloud()
            test_sync()
            test_confirm_binding()
            test_phone_buttons()
            test_desktop()
    finally:
        sp._http, sp._sleep = _REAL_HTTP, saved_sleep
    log = _log.getvalue()
    check("no key or token in any log line", not any(s in log for s in SECRETS))
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
