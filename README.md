# MailHub (OpenClaw Skill)

A unified email/calendar/contacts assistant for OpenClaw with safe account linking, reminders, triage, reply drafting/sending, and credit-card bill analysis.

## Quickstart

### 1) Install (uv recommended)
```bash
uv pip install -e .
```

### 2) Set state dir
```bash
export MAILHUB_STATE_DIR="$HOME/.openclaw/state/mailhub"
mailhub doctor
```

### 3) Link accounts
IMAP/SMTP (recommended to start):
```bash
mailhub auth imap --email you@example.com --imap-host imap.example.com --smtp-host smtp.example.com
```

Google (OAuth browser flow + PKCE):
- Preferred: run `mailhub wizard wizard` and save OAuth Client ID in settings.
- Fallback: set `GOOGLE_OAUTH_CLIENT_ID` (optional `GOOGLE_OAUTH_CLIENT_SECRET` for confidential clients).
```bash
mailhub auth google --scopes gmail,calendar,contacts
```

Microsoft (OAuth device code flow):
- Preferred: run `mailhub wizard wizard` and save OAuth Client ID in settings.
- Fallback: set `MS_OAUTH_CLIENT_ID`.
```bash
mailhub auth microsoft --scopes mail,calendar,contacts
```

### 4) Run daily triage
```bash
mailhub inbox ingest --date today
mailhub triage day --date today
```

### 5) Reply (manual)
After triage prints reply-needed items with indices:
```bash
mailhub reply prepare --index 1
mailhub reply send --index 1 --confirm-text "yes send"
```

## Security

- Never store passwords in plaintext.
- IMAP/SMTP uses app-specific passwords entered locally.
- OAuth tokens stored via OS keychain when possible; else encrypted local file.

## Notes

This repo provides a safe MVP skeleton. You should review and adjust scopes, disclosure line, and provider-specific policies for your deployment.

## LICENSE（MIT）
See `LICENSE`.
