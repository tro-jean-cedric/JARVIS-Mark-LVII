"""
plugins/_google_core.py — one Google sign-in shared by the Google plugins.

Helper module (leading underscore: the plugin loader skips it). Used by
gmail_control, calendar_control and subscription_radar.

HOW THE USER CONNECTS
    Google does not hand out API access to an app it has never heard of, so the
    user creates their own free "Desktop app" OAuth client in Google Cloud and
    gives it to JARVIS in one of three ways (first match wins):

      1. Client ID + Client Secret typed into ⚙ → PLUGIN SETTINGS → Google;
      2. the path of the downloaded client JSON in the same form;
      3. the downloaded file dropped into config/ as client_secret*.json.

    The Client ID field also accepts the whole downloaded JSON pasted in.

    Pressing AUTHORIZE in that form, or simply asking for mail or calendar,
    opens Google's consent page in the browser (the installed-app loopback
    flow, run on a worker thread — never the Qt thread). The resulting token
    is kept in config/token_google.json, which .gitignore already excludes.

ONE TOKEN, SCOPES PER SERVICE
    Each service declares its own scopes in SERVICES. The token asks for all of
    them at once, so the user signs in once for Gmail and Calendar together,
    and every call checks that ITS scopes were actually granted (Google lets
    the user untick boxes on the consent page).

USAGE
    from plugins import _google_core as g
    svc, err = g.service("gmail", player)
    if err:
        return err                    # a spoken, step-by-step explanation
    try:
        svc.users().messages().list(userId="me").execute()
    except Exception as e:
        return g.explain_error(e, "gmail", player)

Nothing here raises to a caller; every failure becomes a sentence.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

# Google grants fewer scopes than asked when the user unticks a box; oauthlib
# would raise on that. We check what was granted ourselves instead.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

NAMESPACE = "google"           # shared PLUGIN_SETTINGS namespace
TOKEN_NAME = "token_google.json"
AUTH_TIMEOUT_S = 300           # how long the browser consent may take
HTTP_TIMEOUT_S = 25            # per API request

SERVICES: dict[str, dict] = {
    "gmail": {
        "label": "Gmail",
        "api": ("gmail", "v1"),
        "api_name": "Gmail API",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    },
    "calendar": {
        "label": "Google Calendar",
        "api": ("calendar", "v3"),
        "api_name": "Google Calendar API",
        "scopes": [
            "https://www.googleapis.com/auth/calendar.events",
        ],
    },
}

LIB_HINT = "pip install google-auth-oauthlib google-api-python-client"

_lock = threading.RLock()
_creds = None                                  # cached google Credentials
_auth_threads: dict[str, threading.Thread] = {}
_last_auth_error = ""


# ── Where things live ────────────────────────────────────────────────────────

def _config_dir() -> Path:
    try:
        from memory.config_manager import get_base_dir
        base = Path(get_base_dir())
    except Exception:
        base = Path(__file__).resolve().parent.parent
    d = base / "config"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def token_path() -> Path:
    return _config_dir() / TOKEN_NAME


def all_scopes() -> list[str]:
    out: list[str] = []
    for spec in SERVICES.values():
        for s in spec["scopes"]:
            if s not in out:
                out.append(s)
    return out


def libs_available() -> bool:
    try:
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        return True
    except Exception:
        return False


# ── Reading the OAuth client the user supplied ──────────────────────────────

def _setting(key: str, default=""):
    try:
        from memory.config_manager import get_plugin_setting
        v = get_plugin_setting(NAMESPACE, key, default)
        return default if v is None else v
    except Exception:
        return default


def _client_from_json(data) -> tuple[Optional[dict], str]:
    """A {"installed": {...}} config from a parsed client file, or an error."""
    if not isinstance(data, dict):
        return None, "That client file isn't valid JSON from Google Cloud."
    if "web" in data and "installed" not in data:
        return None, ("That OAuth client is a 'Web application' one. JARVIS needs a "
                      "'Desktop app' client: in Google Cloud open APIs & Services, "
                      "Credentials, press Create credentials, OAuth client ID, choose "
                      "Desktop app, and use that one instead.")
    block = data.get("installed", data)
    cid = str(block.get("client_id") or "").strip()
    secret = str(block.get("client_secret") or "").strip()
    if not cid or not secret:
        return None, ("The client file looks incomplete: it needs both client_id "
                      "and client_secret. Download it again from Google Cloud, or "
                      "type the Client ID and Client Secret in instead.")
    return _make_client(cid, secret)


def _make_client(cid: str, secret: str) -> tuple[Optional[dict], str]:
    cid, secret = cid.strip().strip('"').strip(), secret.strip().strip('"').strip()
    if not cid or not secret:
        return None, ("Add your Google OAuth Client ID and Client Secret in the "
                      "Google section of the plugin settings first.")
    if not cid.endswith(".apps.googleusercontent.com"):
        return None, ("That Client ID doesn't look like a Google one — it should end "
                      "in .apps.googleusercontent.com. Copy it again from Google Cloud.")
    return {"installed": {
        "client_id": cid,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}, ""


def read_client(values: Optional[dict] = None) -> tuple[Optional[dict], str]:
    """The OAuth client config, from settings (or `values`, the form being
    saved) or a client_secret*.json in config/. Returns (config, error)."""
    get = (lambda k: str((values or {}).get(k) or "").strip()) if values is not None \
        else (lambda k: str(_setting(k, "") or "").strip())
    cid, secret, path = get("client_id"), get("client_secret"), get("client_file")

    # The whole downloaded JSON pasted into a text field.
    for blob in (cid, secret):
        if blob.startswith("{"):
            try:
                return _client_from_json(json.loads(blob))
            except ValueError:
                return None, ("The text pasted into the settings looks like the client "
                              "file but is cut off. Paste the whole file, or just the "
                              "Client ID and Client Secret.")

    if cid or secret:
        return _make_client(cid, secret)

    candidates: list[Path] = []
    if path:
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = _config_dir().parent / p
        if not p.is_file():
            return None, f"I can't find the client file at {p}. Check the path in the Google settings."
        candidates.append(p)
    else:
        candidates.extend(sorted(_config_dir().glob("client_secret*.json")))
    for p in candidates:
        try:
            return _client_from_json(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            return None, f"{p.name} isn't valid JSON. Download it again from Google Cloud."
        except Exception as e:
            return None, f"I couldn't read {p.name}: {e}"

    return None, setup_steps_short()


def setup_steps_short() -> str:
    return ("Google isn't set up yet. Open the plugin settings, Google section: add "
            "the Client ID and Client Secret of a 'Desktop app' OAuth client from "
            "console.cloud.google.com (or the path of its downloaded JSON), save, "
            "then press Authorize.")


SETUP_HELP = (
    "One-time setup, about ten minutes: "
    "1) console.cloud.google.com → create a project. "
    "2) APIs & Services → Library → enable the Gmail API and the Google Calendar API. "
    "3) OAuth consent screen (Google Auth Platform) → External; add your own Google "
    "address under Audience → Test users. "
    "4) Credentials → Create credentials → OAuth client ID → Desktop app. "
    "5) Paste its Client ID and Client Secret here (or the path of the downloaded JSON, "
    "or drop it into config/ as client_secret.json), SAVE, then AUTHORIZE."
)


# ── Credentials ──────────────────────────────────────────────────────────────

def _granted(creds) -> set[str]:
    g = getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None) or []
    if isinstance(g, str):
        g = g.split()
    return set(g)


def _save_creds(creds) -> str:
    """Write the token; returns '' or an error."""
    try:
        data = json.loads(creds.to_json())
        granted = sorted(_granted(creds))
        if granted:
            data["scopes"] = granted
        path = token_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(path)
        return ""
    except Exception as e:
        return str(e)


def _load_creds() -> tuple[object, str]:
    """(credentials, '') when usable, or (None, reason). Refreshes if expired.
    Reasons starting with 'revoked:' mean the token must be thrown away."""
    global _creds
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except Exception:
        return None, "nolib"
    with _lock:
        creds = _creds
        if creds is None:
            path = token_path()
            if not path.exists():
                return None, "none"
            try:
                creds = Credentials.from_authorized_user_file(str(path))
            except Exception as e:
                return None, f"revoked:the stored token is unreadable ({e})"
        if creds.valid:
            _creds = creds
            return creds, ""
        if not creds.refresh_token:
            return None, "revoked:the stored token has no refresh token"
        try:
            creds.refresh(Request())
        except Exception as e:
            msg = str(e)
            if "invalid_grant" in msg or "invalid_client" in msg or "unauthorized" in msg.lower():
                return None, f"revoked:{msg}"
            return None, f"network:{msg}"
        _creds = creds
        _save_creds(creds)
        return creds, ""


def _forget_token() -> None:
    global _creds
    with _lock:
        _creds = None
        try:
            token_path().unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[google] could not delete the old token: {e}")


def is_connected(service_key: str) -> bool:
    """True when a stored token covers this service's scopes. Does not open a
    browser. May refresh the token over the network."""
    creds, _ = _load_creds()
    return creds is not None and set(SERVICES[service_key]["scopes"]) <= _granted(creds)


# ── The consent flow ─────────────────────────────────────────────────────────

def _run_flow(client: dict) -> tuple[bool, str]:
    """Blocking installed-app flow. Call on a worker thread only."""
    global _creds, _last_auth_error
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception:
        return False, f"Missing library. Run: {LIB_HINT}"
    try:
        flow = InstalledAppFlow.from_client_config(client, all_scopes())
        creds = flow.run_local_server(
            host="localhost", port=0, open_browser=True,
            timeout_seconds=AUTH_TIMEOUT_S,
            authorization_prompt_message="JARVIS: finish the Google sign-in in your browser: {url}",
            success_message="JARVIS is connected to your Google account. You can close this tab.",
            prompt="consent", access_type="offline",
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        low = msg.lower()
        if "access_denied" in low or "accessdenied" in low:
            reason = ("Google's page was cancelled or access wasn't allowed. If it said "
                      "'Access blocked', add your Google address as a Test user under "
                      "Google Auth Platform → Audience, then press Authorize again.")
        elif "invalid_client" in low or "unauthorized_client" in low:
            reason = ("Google doesn't accept this Client ID and Secret together. Copy both "
                      "again from the same 'Desktop app' client in Google Cloud.")
        elif "redirect_uri_mismatch" in low:
            reason = "The OAuth client must be a 'Desktop app' client, not a web one."
        elif "nonetype" in low or "last_request_uri" in low or "timed out" in low:
            reason = ("Google didn't send the sign-in back in time. Press Authorize again "
                      "and finish every step in the browser.")
        else:
            reason = f"Authorization failed or was cancelled: {msg[:200]}"
        _last_auth_error = reason
        return False, reason

    missing = set(all_scopes()) - _granted(creds)
    err = _save_creds(creds)
    if err:
        _last_auth_error = f"Authorized, but couldn't save the token: {err}"
        return False, _last_auth_error
    with _lock:
        _creds = creds
    _last_auth_error = ""
    if missing:
        labels = sorted({spec["label"] for spec in SERVICES.values()
                         if set(spec["scopes"]) & missing})
        return True, ("Google connected, but not every permission was ticked — "
                      f"{', '.join(labels)} won't work. Press Authorize again and "
                      "tick every box.")
    return True, "Connected: this Google account now works for Gmail and Calendar."


def _start_background_auth(service_key: str, player=None) -> str:
    """Kick the consent flow off on a worker thread; returns what to say now."""
    label = SERVICES.get(service_key, {}).get("label", "Google")
    client, err = read_client()
    if client is None:
        return err
    with _lock:
        t = _auth_threads.get(NAMESPACE)
        if t is not None and t.is_alive():
            return (f"Google's sign-in page is already open in your browser for {label}. "
                    "Finish it there, then ask me again.")

        def worker():
            ok, msg = _run_flow(client)
            try:
                from plugins import _announce
                _announce.remember(player)
                if ok:
                    _announce.say(f"{msg} The user can now repeat their {label} request.")
                else:
                    _announce.say(f"The Google sign-in for {label} did not finish: {msg}")
            except Exception:
                print(f"[google] {msg}")

        t = threading.Thread(target=worker, daemon=True, name="google-oauth")
        _auth_threads[NAMESPACE] = t
        t.start()
    return (f"I opened Google's sign-in page in your browser to connect {label}. "
            "Choose your account, allow every permission, then ask me again.")


def authorize_action(values: dict) -> tuple[bool, str]:
    """PLUGIN_SETTINGS action. The settings overlay runs this on its own worker
    thread with the values in the form, so blocking here is fine."""
    if not libs_available():
        return False, f"Missing library. Run: {LIB_HINT}"
    client, err = read_client(values)
    if client is None:
        return False, err
    with _lock:
        t = _auth_threads.get(NAMESPACE)
        if t is not None and t.is_alive():
            return False, "Google's sign-in page is already open — finish it in the browser."
        _auth_threads[NAMESPACE] = threading.current_thread()
    try:
        _forget_token()
        return _run_flow(client)
    finally:
        with _lock:
            if _auth_threads.get(NAMESPACE) is threading.current_thread():
                _auth_threads.pop(NAMESPACE, None)


PLUGIN_SETTINGS_SHARED = {
    "namespace": NAMESPACE,
    "title": "Google (Gmail · Calendar · Subscriptions)",
    "fields": [
        {"key": "client_id", "label": "OAuth Client ID", "type": "text",
         "placeholder": "….apps.googleusercontent.com (or paste the whole client JSON)"},
        {"key": "client_secret", "label": "OAuth Client Secret", "type": "password",
         "placeholder": "from the same Desktop-app client"},
        {"key": "client_file", "label": "…or path to client_secret.json", "type": "text",
         "placeholder": "optional — config/client_secret*.json is found automatically"},
    ],
    "action": {"label": "AUTHORIZE (opens your browser)", "run": authorize_action},
}


# ── Building a service ───────────────────────────────────────────────────────

def service(service_key: str, player=None):
    """(googleapiclient Resource, '') or (None, spoken explanation).

    When no usable token exists, starts the browser consent on a worker thread
    and returns a sentence telling the user to finish it."""
    spec = SERVICES[service_key]
    if not libs_available():
        return None, f"I need the Google libraries first. Run: {LIB_HINT}"
    creds, why = _load_creds()
    if creds is None:
        if why == "nolib":
            return None, f"I need the Google libraries first. Run: {LIB_HINT}"
        if why.startswith("network:"):
            return None, (f"I couldn't reach Google to renew the {spec['label']} session. "
                          "Check the internet connection.")
        client, err = read_client()
        if client is None:
            return None, err
        prefix = ""
        if why.startswith("revoked:"):
            _forget_token()
            prefix = ("Google ended the previous sign-in — that happens every 7 days while "
                      "the Google Cloud app is in Testing, or after a password change. ")
        return None, prefix + _start_background_auth(service_key, player)
    if not set(spec["scopes"]) <= _granted(creds):
        return None, ("Google didn't give me every permission for "
                      f"{spec['label']}. " + _start_background_auth(service_key, player))
    try:
        from googleapiclient.discovery import build
        http = None
        try:
            import httplib2
            import google_auth_httplib2
            http = google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_S))
        except Exception:
            http = None
        api, ver = spec["api"]
        if http is not None:
            svc = build(api, ver, http=http, cache_discovery=False)
        else:
            svc = build(api, ver, credentials=creds, cache_discovery=False)
        return svc, ""
    except Exception as e:
        return None, explain_error(e, service_key, player)


def explain_error(exc: Exception, service_key: str, player=None) -> str:
    """Turn an API exception into one helpful spoken sentence."""
    spec = SERVICES.get(service_key, {"label": "Google", "api_name": "API"})
    label = spec["label"]
    status = None
    body = ""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, "status", 0) or 0)
            body = (exc.content or b"").decode("utf-8", "replace") \
                if isinstance(exc.content, (bytes, bytearray)) else str(exc.content)
    except Exception:
        pass
    text = f"{exc} {body}"
    low = text.lower()
    print(f"[google] {label} error: {text[:300]}")

    if "service_disabled" in low or "accessnotconfigured" in low or \
            "has not been used in project" in low or "it is disabled" in low:
        return (f"The {spec['api_name']} is switched off in your Google Cloud project. "
                f"Open console.cloud.google.com, APIs & Services, Library, enable the "
                f"{spec['api_name']}, wait a minute, then ask me again.")
    if "access_token_scope_insufficient" in low or "insufficientpermissions" in low \
            or "insufficient authentication scopes" in low:
        return (f"Google didn't give me every permission for {label}. "
                + _start_background_auth(service_key, player))
    if status == 401 or "invalid_grant" in low:
        _forget_token()
        return ("Google withdrew access. " + _start_background_auth(service_key, player))
    if status == 429 or "ratelimitexceeded" in low or "userratelimitexceeded" in low:
        return f"{label} had too many requests just now. Wait a minute and ask me again."
    if status and status >= 500:
        return f"{label} had a problem on Google's side. Try again in a moment."
    if status == 404:
        return f"{label} couldn't find that item any more."
    if status:
        return f"{label} refused the request (error {status})."
    if any(k in low for k in ("timed out", "timeout", "name resolution", "connection",
                              "unreachable", "ssl", "socket")):
        return f"I couldn't reach Google for {label}. Check the internet connection."
    return f"{label} failed: {str(exc)[:160]}"


def status_text() -> str:
    """Short human description of the connection, for help/status replies."""
    if not libs_available():
        return f"Google libraries missing ({LIB_HINT})."
    client, err = read_client()
    if client is None:
        return "Not set up: no OAuth client yet."
    t = _auth_threads.get(NAMESPACE)
    if t is not None and t.is_alive():
        return "Sign-in in progress in the browser."
    if not token_path().exists():
        return "Client set up, not authorized yet." + (
            f" Last attempt: {_last_auth_error}" if _last_auth_error else "")
    return "Authorized."
