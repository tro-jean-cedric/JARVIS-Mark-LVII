# Remote surfaces: phone dashboard and cloud server

Read before touching `dashboard/` or `core/cloud.py`, or anything about
pairing, tokens, TLS, or controlling JARVIS from another device.

## Phone dashboard (`dashboard/server.py`)

- FastAPI app built in `DashboardServer._build_app` (`dashboard/server.py:847`),
  served by uvicorn from main.py's asyncio loop (`main.py:2721-2730`). Optional:
  if it fails, the app runs without it.
- Plain HTTP on port 8000 by default; HTTPS on 8001 only when config
  `dashboard_https` is true and certs exist (`_ssl_enabled`). Without HTTPS
  the browser lacks `crypto.subtle`, so commands travel in clear. The module
  docstring and the "AES-256-CBC" header are stale (code is AES-256-GCM).
- Pairing: `new_key()` makes a one-time 8-char key (10 min) shown as a QR code
  (`main.py:_make_remote_key`) → `GET /auto-login?key=` → 32-byte bearer token
  (12 h TTL). Login is throttled (6 failures / 5 min → 429).
- Routes: `/login`, `/`, `/auto-login`, `/api/device-login`,
  `/api/revoke-devices`, `/api/command`, `/api/wake`, `/api/upload`,
  `/api/files`, `/uploads/{filename}`, WS `/ws` and `/ws/phone-audio`.
  New routes must check the bearer token like the existing `/api/*` ones.
- The dashboard never calls tools. It queues text, which main.py's
  `_process_dashboard_commands` (`main.py:2540`) sends as a normal user turn,
  and PCM audio relayed into the session. Tools it triggers still receive the
  real `player`. `set_wake_callback` is never wired, so `/api/wake` is a no-op.

## Cloud body (`core/cloud.py`)

A self-hosted, always-on server copy of the assistant (no face, mic or
screen). It answers Telegram, runs tasks and routines, and syncs with the desktop.

- CLI (argparse in `main()`, `core/cloud.py:2894`):
  `python -m core.cloud deploy root@HOST` (run locally; copies `MANIFEST`
  files and installs a systemd unit at `/opt/jarvis-cloud`),
  `python3 -m core.cloud install`, `--pair-code`, `--rotate`, or no args to
  run the service.
- The top level imports only the stdlib and `core.routines.rules`. Keep it
  that way so it can bootstrap on a bare server. If you add a module the
  server needs, add it to `MANIFEST` (`core/cloud.py:107`).
- The link is pinned TLS with a pairing code. `Runtime` (`:1952`) reuses
  `TaskEngine` and `RoutineEngine` with roots in `cloud_state/`.
  `HeadlessPlayer` (`:1577`) stands in for the UI.
- The desktop side (`plugins/cloud_agent.py`, `plugins/telegram_remote.py`)
  is referenced but not present in this checkout.
- Waking the desktop (`home_computer` wake): Wake-on-LAN or a wake URL
  (`send_wake`) first; otherwise the smart plug (`core/smart_plug.py`, the
  user's own Shelly / Tuya / Voice Monkey keys synced from CLOUD AGENT as
  `WAKE_PLUG_KEYS`). The plug cuts the power, so it runs only through
  `core.confirm`: `Runtime.__init__` binds confirm to the phone, and the
  Telegram bridge shows Yes / Yes for it on the server even without
  `phone_control`. Success is the desktop checking in (`watch_wake`).
