# Security Policy

## Data Handling
- MailHub stores tokens and secrets locally only.
- Preferred storage: OS keychain via `keyring`.
- Fallback: encrypted local file using AES-GCM (passphrase from env or local prompt).
- No telemetry is collected.

## Credentials
- Never enter passwords in chat.
- For IMAP/SMTP providers, use app-specific passwords entered only into the local CLI prompt.

## Dangerous Actions
- Sending emails and creating/updating calendar events require explicit user confirmation unless auto-send is enabled by the user.
- This project does not delete emails or cancel events by default.

## Reporting
If you find a security issue, please open a GitHub issue with minimal sensitive data.