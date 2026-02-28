# MailHub (OpenClaw Skill)

A unified email/calendar/contacts assistant for OpenClaw with safe account linking, reminders, triage, reply drafting/sending, and credit-card bill analysis.

## Execution Flow (Single Entry)

`mailhub jobs run` is the only command that should be scheduled by OpenClaw automation.

Runtime flow:
1. First-run gate: check config confirmation.
2. Health gate: run internal doctor checks.
3. Provider gate: require at least one bound account.
4. Core pipeline: poll -> triage/suggest -> auto-reply (if enabled).
5. Time-based tasks: run digest/billing only when local schedule slots are due.
6. Persist slot markers to avoid duplicate same-slot execution.

Blocking conditions:
- Config not confirmed -> returns checklist and exits (use `mailhub config --confirm` or `mailhub jobs run --confirm-config`).
- No provider bound -> returns bind hint (use `mailhub bind`).

## Quickstart

### 1) Install (recommended for OpenClaw)
Clone to `~/.openclaw/skills/mailhub`, then run:
```bash
~/.openclaw/skills/mailhub/setup ~/.openclaw/skills/mailhub
```

This creates:
- local venv: `~/.openclaw/skills/mailhub/.venv`
- launcher: `~/.local/bin/mailhub`
- state dir: `~/.openclaw/state/mailhub`

If `mailhub` is still not found, add:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 2) Alternative install (manual)
```bash
uv pip install -e .
```

Note: `uv pip install` installs into the selected Python environment. If that env is not activated or its `bin` directory is not in `PATH`, `mailhub` will show as "command not found".

### 3) Set state dir
```bash
export MAILHUB_STATE_DIR="$HOME/.openclaw/state/mailhub"
mailhub doctor
```

### 4) First-run config confirmation
The first execution requires config confirmation. Review and confirm once:
```bash
mailhub config
mailhub config --confirm
```

You can modify any setting at any time:
```bash
mailhub settings-set toggles.mail_alerts_mode suggested
mailhub settings-set toggles.scheduler_tz Asia/Shanghai
mailhub settings-set toggles.digest_times_local 09:00,18:00
mailhub settings-set toggles.digest_weekdays mon,tue,wed,thu,fri
mailhub settings-set toggles.billing_days_of_month 1,15,28
mailhub settings-set toggles.billing_times_local 10:00,20:00
```

### 5) Unified account binding
Use one entrypoint and choose provider with `1/2/3/4/5`:
```bash
mailhub bind
```

For OpenClaw deployments, OAuth client IDs can also be injected through environment secrets:
- `GOOGLE_OAUTH_CLIENT_ID` (optional `GOOGLE_OAUTH_CLIENT_SECRET`)
- `MS_OAUTH_CLIENT_ID`

Advanced direct provider commands remain available:
```bash
mailhub auth google --scopes gmail,calendar,contacts
mailhub auth microsoft --scopes mail,calendar,contacts
mailhub auth imap --email you@example.com --imap-host imap.example.com --smtp-host smtp.example.com
```

### 6) One-entry automation runtime
Use only one automation command:
```bash
mailhub jobs run
```

- If config is not confirmed: it returns checklist and blocks execution.
- If no account is bound: it asks you to run `mailhub bind` (or opens bind menu in interactive mode).
- If schedule conditions are met: it runs digest/billing tasks according to settings.
- Otherwise it runs poll/triage/auto-reply flow based on toggles.

Recommended automation cadence:
```bash
*/15 * * * * mailhub jobs run
```

### 7) Independent commands (manual or troubleshooting)
```bash
mailhub doctor
mailhub inbox poll --since 15m
mailhub triage day --date today
mailhub reply prepare --index 1
mailhub reply send --index 1 --confirm-text "yes send"
```

## Default Config Baseline

The project ships with safe defaults:
- `mail_alerts_mode=off`
- `auto_reply=off`
- `auto_reply_send=off`
- `bill_analysis=off`
- `poll_since=15m`
- `scheduler_tz=UTC`
- `digest_weekdays=mon,tue,wed,thu,fri`
- `digest_times_local=09:00`
- `billing_days_of_month=1`
- `billing_times_local=10:00`

You can inspect and change them at runtime:
```bash
mailhub settings-show
mailhub settings-set toggles.scheduler_tz Asia/Shanghai
```

## Running Continuously (recommended model)

MailHub is designed as a stateless CLI + local state DB, not a long-running daemon by default.

- Preferred in OpenClaw: run only `mailhub jobs run` on a schedule (for example every 15 minutes) via automation/scheduler.
- If you need server-style always-on behavior, run your own process manager (`systemd`, `supervisord`, or cron) to invoke `mailhub jobs run` periodically.
- Keep send operations (`mailhub reply send` / `reply auto --dry-run false`) behind explicit opt-in.

## Security

- Never store passwords in plaintext.
- IMAP/SMTP uses app-specific passwords entered locally.
- OAuth tokens stored via OS keychain when possible; else encrypted local file.
- Use OpenClaw environment secrets for OAuth client IDs when available.

## Notes

This repo provides a safe MVP skeleton. You should review and adjust scopes, disclosure line, and provider-specific policies for your deployment.

Current MVP limits:
- Billing detection/analyze currently focuses on recent/today-first data flow.
- Calendar create/update commands are planned; current stable command is `mailhub cal agenda`.

## LICENSE（MIT）
See `LICENSE`.
