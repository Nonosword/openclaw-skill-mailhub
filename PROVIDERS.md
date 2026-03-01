# Provider Binding Guide

This document keeps provider-specific setup steps out of `README.md`.

## Google (Gmail + Calendar + Contacts)

1. In Google Cloud Console, enable:
   - Gmail API
   - Google Calendar API
   - People API
2. Configure OAuth consent screen.
3. Create OAuth Client ID (Desktop app).
4. Set:
   - `GOOGLE_OAUTH_CLIENT_ID=<your_client_id>`
   - `GOOGLE_OAUTH_CLIENT_SECRET=<your_client_secret>`
5. Bind:
   - `mailhub bind --provider google --scopes gmail,calendar,contacts`

## Microsoft (Outlook + Calendar + Contacts)

1. In Microsoft Entra admin center, create an App Registration.
2. Enable public client flow (device code).
3. Add Graph delegated permissions:
   - `Mail.Read`, `Mail.Send`
   - `Calendars.Read`
   - `Contacts.Read`
   - `offline_access`, `openid`, `profile`, `email`
4. Set:
   - `MS_OAUTH_CLIENT_ID=<your_client_id>`
5. Bind:
   - `mailhub bind --provider microsoft --scopes mail,calendar,contacts`

## Apple / iCloud (Calendar + Contacts)

MailHub uses CalDAV/CardDAV for Apple account integration (not OAuth client id/secret flow).

1. Generate Apple app-specific password.
2. Use typical iCloud hosts:
   - Calendar: `caldav.icloud.com`
   - Contacts: `contacts.icloud.com`
3. Bind:
   - `mailhub bind --provider caldav --username "<apple_id_email>" --host "caldav.icloud.com"`
   - `mailhub bind --provider carddav --username "<apple_id_email>" --host "contacts.icloud.com"`

## Gmail App Password via IMAP/SMTP (Optional)

If you choose IMAP/SMTP instead of Google OAuth:

- `mailhub bind --provider imap --email "<gmail_address>" --imap-host imap.gmail.com --smtp-host smtp.gmail.com`

## Notes

- OAuth client credential resolution order is:
  - `os.environ` > `.env` > `settings.json`
- After successful mail-capable bind, MailHub performs one bootstrap incremental pull.
