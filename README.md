# PayVerify

Checks the UPI payment screenshots your customers send on WhatsApp, and only marks a payment
**Verified** once the same money has really reached your bank.

- **Works on phones and computers.** It's one responsive web app: iPhone, Android, Mac and Windows
  all use the same site, and it can be installed to the home screen or Dock like a normal app.
- **Separate accounts.** Everyone logs in with their own email and password. Each account has its
  own payments, bank credits, screenshots, WhatsApp number and settings. One account can't see
  another's data (this is covered by automated tests).

## How a payment is checked

1. A customer sends a payment screenshot to your WhatsApp Business number (or you upload one).
2. Meta's AI (the Muse Spark model, via the Meta Model API) reads it: amount, 12-digit UTR, date
   and time, who was paid, which app, and whether it looks edited.
3. **Screenshot checks:** paid to one of your UPI IDs, a successful payment, recent enough, a usual
   amount (optional), and a UTR (or identical image) that hasn't been sent before.
4. **Money check:** a credit with the same UTR and amount must exist in your bank, from a forwarded
   bank SMS, an imported statement, or one you typed in.
5. The status becomes **Verified**, **Auto-approved**, **Needs review** or **Duplicate**, and the
   customer gets an automatic WhatsApp reply. If the bank SMS arrives later, a waiting payment
   becomes Verified by itself and the customer gets a “payment received” message.

### Automatic approval

The **Auto-approve** switch at the top of the Payments screen (also in Settings) picks how
payments get approved:

- **On:** approved as soon as the screenshot passes every check: paid to one of your UPI IDs,
  marked successful, recent, not sent before, your usual amount (if you set any), and no signs of
  editing. No one has to approve anything. These show as **Auto-approved** until your bank confirms
  them. If a bank SMS later shows a different amount, the payment moves back to **Needs review**.
- **Off:** approved only when the same UTR and amount also show up in your bank, or when you
  approve it yourself. This is the safest setting: a fake screenshot can look perfect, but it
  can't put money in your bank.

Turning it on also approves payments that were waiting and pass every check. Turning it off
doesn't take back payments that were already approved.

## Run it on your Mac

You need Python 3.10 or newer.

```bash
cd ~/Desktop/automationApp
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Open `.env` and set `META_MODEL_API_KEY` (create a key at dev.meta.ai → API keys). Then start it:

```bash
.venv/bin/flask --app payverify run --host 0.0.0.0 --port 5050
```

Open http://localhost:5050, create your account, and go to **Settings** to add your UPI IDs.

**On your phone (same Wi-Fi):** find your Mac's address in System Settings → Wi-Fi → Details
(for example `192.168.1.20`) and open `http://192.168.1.20:5050` on the phone.

Want to look around first? `flask --app payverify seed-demo` creates a demo account with sample
payments and prints its password. Use it only on a test copy, not your real database.

## Install it like an app

Installing needs the app to be online over `https://` (see the next section). Then:

- **iPhone / iPad:** open the site in Safari → Share → **Add to Home Screen**.
- **Android:** open it in Chrome → ⋮ → **Install app**.
- **Mac:** Safari → File → **Add to Dock**, or the install icon in Chrome's address bar.

## Going live (needed for WhatsApp)

WhatsApp can only deliver messages to a public `https://` address. Pick one (the
[WhatsApp setup guide](WHATSAPP_SETUP.md) has exact commands):

**A. A small cloud server (recommended).** The included `Dockerfile` runs on Render, Railway,
Fly.io or any VPS. Give it a persistent disk mounted at `/data` (database, screenshots and secret
key live there) and set these environment variables: `META_MODEL_API_KEY`, `SECRET_KEY`,
`PUBLIC_BASE_URL` (your https address), `BEHIND_PROXY=1`, and `SIGNUP_CODE` so strangers can't
create accounts.

**B. Keep it on your Mac** with a tunnel such as ngrok (free plan includes one fixed address) or
Cloudflare Tunnel, which gives you an https address that forwards to port 5050. Set `PUBLIC_BASE_URL` to that address and
`BEHIND_PROXY=1`. Your Mac has to stay on and awake for messages to be processed.

## Connect WhatsApp

Follow **[WHATSAPP_SETUP.md](WHATSAPP_SETUP.md)**: step by step, about 30 minutes. In short:
create a Meta app with the WhatsApp product, copy the **phone number ID**, a permanent
**system-user access token** and the **app secret** into PayVerify, then paste PayVerify's webhook
URL and verify token into Meta and subscribe to **messages**. It uses Meta's official WhatsApp
Business Cloud API, so your number isn't at risk of being banned.

**Settings → WhatsApp** shows a live checklist (online, details saved, webhook verified by Meta,
messages arriving, last problem), a **Check connection** button, and a **Send test message** button.

Each account connects its own number. A number used with the Cloud API can't be used in the
normal WhatsApp app at the same time, so many businesses use a second number for this.

## Confirm the money arrived

The automatic way is to forward your bank's “credited” SMS (**Settings → Bank SMS**):

- **Android:** an SMS-to-webhook forwarding app posts each bank SMS to your private link.
- **iPhone:** a Shortcuts automation (“When I get a message containing *credited*”) does the same.

PayVerify keeps only “money credited” messages and ignores OTPs and other SMS. It also ignores SMS
from ordinary phone numbers, because banks always send from names like `AX-HDFCBK`; this stops
someone texting you a fake “credited” message. You can restrict it further to your bank's sender
name.

Other ways, on the **Bank** screen: paste an SMS, import a statement (CSV or XLSX from net banking;
importing the same file twice is safe), or add a credit by hand.

## Excel export

**Payments → Excel** downloads one workbook with a tab per month (“Oct 2026”). Each row has the
date received, customer name and phone, amount, UTR, payment app, payment date and time, status,
reason/notes, and a link to the payment. A new month's tab appears by itself; the bottom of each
tab totals the verified amount. (`/export.xlsx?month=2026-10` downloads a single month.)

## Accounts and security

- Passwords are hashed (scrypt). After 5 wrong attempts, logins for that email pause for 15 minutes.
- Changing your password signs out your other devices. **Settings → Account & security** has
  “Sign out everywhere”.
- WhatsApp tokens are stored encrypted, using a key derived from `SECRET_KEY`.
- Every database query is filtered by the logged-in account. Screenshots are stored in a folder
  per account and only served to their owner. Account pages are sent with `Cache-Control: no-store`
  and the offline cache never stores them, so a shared phone or computer doesn't keep someone
  else's data after they log out.
- Webhook links contain a long secret per account, and WhatsApp messages must carry a valid Meta
  signature made with that account's app secret.
- There's no email-based password reset. Whoever runs the server can set a new password:
  `flask --app payverify reset-password someone@example.com`.

## Settings reference

| Variable | What it does |
|---|---|
| `META_MODEL_API_KEY` | Meta Model API key (dev.meta.ai) for reading screenshots. Without it, payments can still be entered by hand. |
| `SECRET_KEY` | Signs logins and encrypts saved tokens. Created automatically if empty. |
| `PUBLIC_BASE_URL` | Public https address, used for webhook links and Excel links. |
| `BEHIND_PROXY` | `1` when behind a proxy or tunnel, so https and client IPs are detected correctly. |
| `ALLOW_SIGNUPS` / `SIGNUP_CODE` | Turn sign-ups off, or require an invite code. |
| `PAYVERIFY_AI_MODEL` | Meta model (default `muse-spark-1.3`). Don't use `-contributor` models: Meta trains on those requests. |
| `PAYVERIFY_AI_EFFORT` | `minimal`, `low` (default), `medium`, `high` or `xhigh`. Higher is slower and costs more. |
| `DATABASE_URL`, `INSTANCE_DIR` | Where data is stored. Default: SQLite and files in `./instance`. |
| `MAX_UPLOAD_MB` | Largest upload allowed (default 15). |

Each screenshot is one request to Meta's AI. Screenshots go to Meta's servers (as WhatsApp messages
already do); on the standard tier Meta doesn't train on them.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest
```

The tests replace Meta's AI and WhatsApp with stand-ins, so they don't need keys or the internet.

Notes:

- When an update adds database columns, they're added automatically on start-up; existing data is
  kept.
- Run a single server process (`--workers 1`, as in the Dockerfile). Background jobs and the login
  rate limit live in memory, and SQLite suits one process.
- WhatsApp only allows free-form replies within 24 hours of the customer's last message. A
  “payment received” reply for a bank SMS that arrives later than that won't be delivered.
- `scripts/make_icons.py` regenerates the app icons.
