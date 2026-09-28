# Sending mail from a server through Microsoft 365 direct send

How an application or a server sends e-mail through an organisation's
Microsoft 365 tenant without SMTP AUTH, OAuth or app passwords. The pattern
is language- and framework-agnostic: anything that can speak SMTP with
STARTTLS works, including dbsync-manager's alerts (`SMTP_*` in `.env`) and
cron mail from the host.

## The pattern in one sentence

The server opens SMTP to the tenant's MX host
(`<mx-prefix>.mail.protection.outlook.com`, port 25), upgrades to TLS with
STARTTLS, authenticates with **nothing**, and sends with a From address on a
domain the tenant owns. An **inbound connector** in the Exchange admin
center authorises the server by its public outbound IP address, which is
what makes external recipients (Gmail and the like) accept the mail as well.

## Why this and not the alternatives

- **App passwords**: often disabled tenant-wide by security policy.
- **OAuth / XOAUTH2**: refresh tokens must be renewed by hand every few
  months. A silent single point of failure when someone forgets.
- **Direct send**: the tenant already is the organisation's mail
  infrastructure. Nothing else needs to be built or renewed.

## Prerequisites (once per tenant and per server IP)

1. **An inbound connector** in the Exchange admin center (Mail flow →
   Connectors, from "Your organization's email server" to Office 365)
   that trusts the server's **public outbound IP address**. One connector
   per IP address, shared by every system behind that address, not one
   per application.
2. **SPF** for the sending domain includes Microsoft:
   `v=spf1 include:spf.protection.outlook.com -all`.
3. **DKIM** enabled for the domain in the Exchange admin center, so that
   recipients see a signature they can verify.
4. **Outbound TCP/25** allowed from the server, and from nothing else on
   the LAN. Restricting port 25 to the servers that need it is what stops
   any other machine on the network from spoofing mail as the domain.
   Containers on a server go out through the host's address, so a rule
   for the host covers them.
5. **A From address on the tenant's domain.** The tenant does not check
   that the mailbox exists, so `noreply@`, `<system-name>@` and similar
   all work. Use a distinct address per system so replies and bounces
   can be told apart.

## Client configuration

| Setting | Value |
|---|---|
| SMTP host | `<mx-prefix>.mail.protection.outlook.com` |
| Port | 25 |
| Security | STARTTLS (plain connection upgraded to TLS) |
| Authentication | **none** |
| From | `<system-name>@<your-domain>` |
| Timeout | 30 seconds is reasonable |

The MX host name is the tenant's MX record: `dig MX <your-domain>` shows
it. The prefix is usually the domain with dots replaced by dashes.

### dbsync-manager (`.env`)

```
SMTP_HOST=<mx-prefix>.mail.protection.outlook.com
SMTP_PORT=25
SMTP_TLS=true
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=dbsync-manager@<your-domain>
```

Leave `SMTP_USER` and `SMTP_PASSWORD` empty; Django then sends without
AUTH. `docker compose up -d` after the change. Test with
`docker compose exec manager python manage.py sendtestemail you@<your-domain>`.

### Python (standard library)

```python
import smtplib
from email.message import EmailMessage

msg = EmailMessage()
msg["From"] = "noreply@example.org"
msg["To"] = "someone@example.com"
msg["Subject"] = "Test"
msg.set_content("Hello from the new system.")

with smtplib.SMTP("example-org.mail.protection.outlook.com", 25, timeout=30) as s:
    s.starttls()
    s.send_message(msg)
```

### Python + Django

```python
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "example-org.mail.protection.outlook.com"
EMAIL_PORT = 25
EMAIL_USE_TLS = True
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = "noreply@example.org"
# EMAIL_HOST_USER and EMAIL_HOST_PASSWORD deliberately unset.
```

### Node.js (nodemailer)

```javascript
const transporter = nodemailer.createTransport({
  host: "example-org.mail.protection.outlook.com",
  port: 25,
  secure: false,        // STARTTLS, not implicit TLS
  requireTLS: true,     // refuse a plaintext fallback
  tls: { minVersion: "TLSv1.2" },
});
await transporter.sendMail({ from: "noreply@example.org", to: "someone@example.com", subject: "Test", text: "Hello." });
```

### Shell (msmtp) for cron mail, backup reports and the like

`/etc/msmtprc`:

```
defaults
auth           off
tls            on
tls_starttls   on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        m365
host           example-org.mail.protection.outlook.com
port           25
from           noreply@example.org

account default : m365
```

Then `msmtp -t < message.txt`, where the file has `To:` and `Subject:`
headers, a blank line and the body. With `msmtp-mta` installed, cron's
own failure mail (`MAILTO=` in the crontab) goes the same way.

## Failure handling in applications

When the mail is a nice-to-have (a confirmation, a notification), **never
let a mail failure roll back the primary data**:

1. Save the primary record in its own transaction.
2. Send the mail in a separate try/except.
3. Record the outcome on the saved record: `mail_sent` (bool),
   `mail_error` (text, truncated to ~1000 characters), `mail_last_attempt`
   (timestamp).
4. Show those fields in the admin or dashboard, so whoever operates the
   system sees "this person never got their confirmation" without reading
   logs.

This matters because direct send fails silently from the sender's point of
view when the server's public IP changes: the connector simply stops
recognising it, and the SMTP transaction is rejected with a 5xx.

## When it stops working, in this order

1. **Read the SMTP reply in the server log.** Microsoft returns explicit
   5xx codes: `550 5.7.64 TenantAttribution; Relay Access Denied` means
   the connector does not recognise the sending IP.
2. **What is the server's public outbound IP now?** `curl ifconfig.me`
   from the server; compare with the connector.
3. **Is outbound TCP/25 open?** `nc -vz <mx-host> 25` from the server. A
   hang means the firewall blocks it.
4. **Does the MX host resolve?** `dig <mx-host>`.
5. **Is DKIM applied?** Send to a Gmail address, open "Show original",
   look for `DKIM: PASS`.

## Plan B

Microsoft Graph with application permissions (`Mail.Send`). More setup
(app registration, client secret, a client library) but independent of IP
addresses and firewall rules. Considered, not built.

## Installation worksheet

Fill in for your tenant and server and keep it with the organisation's own
documentation, not in this repository.

| Item | Value |
|---|---|
| MX host (`dig MX <your-domain>`) | |
| Sending domain (SPF includes Microsoft, DKIM enabled) | |
| Inbound connector name and the public IP it trusts (date) | |
| Firewall rule allowing outbound TCP/25 from the server only | |
| From address per system (manager, cron, other apps) | |
| Test message sent to an external mailbox with `DKIM: PASS` (date) | |
