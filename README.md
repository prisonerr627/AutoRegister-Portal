# AutoRegister · AIUB Portal

Automates AIUB semester registration: holds an authenticated portal session alive
(surviving the ~5-min `NAABSUMSMVCFORMSAUTH` cookie rotation), detects the instant
registration opens, and watches for open course sections — notifying Discord and
optionally auto-joining (with timetable-clash handling). Includes a web dashboard.

## How it works
- **Login** — auto-login with your credentials; the math captcha is auto-solved via
  OpenRouter, falling back to a Discord post + manual entry on the dashboard.
- **Poller** (`app/poller.py`) — polls `/Student/Registration/Start`; when open it
  enters `Select2`, reads `GetPreReg2`, and evaluates your alerts.
- **Alerts** — per course (matched by title; the ephemeral per-semester *Class ID*
  is never used as a key). Filter by **section label** or **day/time window**. Tick
  **auto-join** to register the moment a matching section opens. If joining would
  clash with a registered course, either *just alert* or *drop the approved clashing
  course(s) and join* — chosen at alert creation.
- **Catalog** — `Offered Course Report.xlsx` provides the course list for the picker
  and reference timings; live timing always comes from the API.

## Run (Docker)
```bash
cp .env.example .env      # set PORTAL_USERNAME / PORTAL_PASSWORD (key + webhook are baked in)
docker compose up --build -d
# dashboard at http://localhost:8000
```

## Run (local)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export PORTAL_USERNAME=25-62595-2 PORTAL_PASSWORD='...'
uvicorn app.main:app --reload
```

## Config (`.env`)
See `.env.example`. Key vars: `PORTAL_USERNAME`, `PORTAL_PASSWORD`,
`OPENROUTER_API_KEY`, `DISCORD_WEBHOOK`, `POLL_INTERVAL_OPEN/CLOSED`,
`DASHBOARD_TOKEN` (optional API guard), `DASHBOARD_PORT`.

State (cookie jar, alerts SQLite db) lives in `DATA_DIR` (`./data`).

## Debugging
- **Dry-run** (`DRY_RUN=true` or the dashboard toggle): performs every step except the
  real `RegisterSection`/`UnRegisterSection`/`Confirm` calls — those are simulated and
  logged/notified as `[DRY-RUN] would …`. Enforced at the lowest level
  (`portal.register_section`/`unregister_section`/`confirm`), so no mutating request is
  ever sent while it's on. Use it to rehearse auto-join during a real window.
- **Burp/proxy** (`PROXY_URL=http://127.0.0.1:8080`): routes portal traffic through an
  intercepting proxy; TLS verification auto-disables (override via `VERIFY_TLS`). In Docker
  use `host.docker.internal`. A `proxy` pill shows in the dashboard when active.

## Where errors show
Failed register/unregister (real mode) surface in three places: a browser alert on the
manual button, the dashboard **Activity** feed (with the portal's `Error` text), and Discord.
