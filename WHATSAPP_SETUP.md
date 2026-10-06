# Connecting PayVerify to WhatsApp

When this is done, a customer sends a payment screenshot to your WhatsApp Business number and,
with no one touching anything:

1. WhatsApp (Meta) forwards the message to PayVerify.
2. Meta's AI (Muse Spark) reads the amount, UTR, date and who was paid.
3. PayVerify runs its checks and, with **Auto-approve** on, approves the payment.
4. The customer gets a reply on WhatsApp, and the payment lands in that month's Excel tab.

It takes about 30 minutes the first time. You need: a Facebook account, a phone that can receive
an SMS or call for verification, and this Mac (or a server).

PayVerify's **Settings → WhatsApp** page has a live checklist that ticks off each part as it starts
working, so keep it open while you go through these steps.

---

## Step 1: Get a Meta AI key (reads the screenshots)

1. Go to **https://dev.meta.ai** and sign in with your Meta account.
2. Open **API keys** → **Create key**, and copy it.
3. In the PayVerify folder, open `.env` (copy `.env.example` if you don't have one) and set:

   ```
   META_MODEL_API_KEY=paste-your-key-here
   ```

Use the normal `muse-spark-1.3` model (the default). Avoid the `-contributor` models: they're
cheaper because Meta trains on what you send, and these are your customers' payments.

## Step 2: Put PayVerify online

WhatsApp delivers each message to your server over the internet, so PayVerify needs a public
`https://` address. Pick one option.

### Option A: from this Mac with ngrok (good for testing; the Mac must stay on)

1. Create a free account at **ngrok.com**. In its dashboard, copy your **authtoken**, and under
   **Domains** claim your free fixed address.
2. Install and connect ngrok:

   ```bash
   brew install ngrok
   ```

   ```bash
   ngrok config add-authtoken YOUR-NGROK-AUTHTOKEN
   ```

3. In `.env`, set the address ngrok gave you:

   ```
   PUBLIC_BASE_URL=https://your-name.ngrok-free.app
   BEHIND_PROXY=1
   ```

4. Start PayVerify in one Terminal window:

   ```bash
   cd ~/Desktop/automationApp && .venv/bin/flask --app payverify run --port 5050
   ```

5. Start the tunnel in a second Terminal window:

   ```bash
   ngrok http 5050 --url https://your-name.ngrok-free.app
   ```

Open your ngrok address on your phone: you'll see the PayVerify login. (ngrok shows a one-time
warning page in browsers; that doesn't affect WhatsApp.)

### Option B: an always-on server (for daily use)

Use the included `Dockerfile` on any host that offers a persistent disk, for example Railway or a
small VPS. Mount the disk at `/data`, and set `META_MODEL_API_KEY`, `SECRET_KEY`,
`PUBLIC_BASE_URL`, `BEHIND_PROXY=1` and `SIGNUP_CODE`. Free tiers that sleep or wipe their disk
will lose messages or data.

## Step 3: Create the Meta app

1. Go to **https://developers.facebook.com**, log in, and accept the developer terms if asked.
2. **My Apps → Create app**. Choose the use case **Connect with customers through WhatsApp**
   (on older screens: app type **Business**), and select or create your **Meta Business
   portfolio**.
3. Open **WhatsApp → API Setup** in the app's left menu. Meta gives you a free **test number**.
   - Under **To**, add your personal WhatsApp number and confirm the code Meta sends. The test
     number can only message numbers on this list.
   - Copy the **Phone number ID** shown for the test number.

## Step 4: Create a permanent access token

The token on the API Setup page expires after 24 hours, so make a permanent one:

1. Go to **https://business.facebook.com** → **Settings** → **Users → System users** → **Add**.
   Give it any name and the **Admin** role.
2. Click **Assign assets**: give it your app (full control) and your WhatsApp account (full control).
3. Click **Generate token**, select your app, set expiry to **Never**, and tick
   `whatsapp_business_messaging` and `whatsapp_business_management`. Copy the token.

## Step 5: Enter the details in PayVerify

1. Back in your Meta app: **App settings → Basic → App secret → Show**, and copy it.
2. In PayVerify, open **Settings → WhatsApp** and fill in the **Phone number ID**, **Access token**
   and **App secret**. Press **Save**, then **Check connection**: it should show your business name.
3. Use **Send a test message** with your personal number. You should get Meta's “Hello World”
   message on WhatsApp.

## Step 6: Connect the webhook (how messages reach PayVerify)

1. In your Meta app: **WhatsApp → Configuration → Webhook → Edit**.
2. Paste the **Callback URL** and **Verify token** shown on PayVerify's Settings → WhatsApp page,
   then press **Verify and save**. PayVerify's checklist now shows “Webhook verified by Meta”.
3. Under **Webhook fields**, find **messages** and press **Subscribe**.

## Step 7: Test it end to end

1. In PayVerify, add your UPI IDs (**Settings → Checks & replies**) and turn on **Auto-approve**
   (the switch at the top of the Payments screen).
2. From your personal WhatsApp, send a real payment screenshot to the test number.
3. Within a few seconds it appears on the Payments screen, approved or flagged, and you get the
   automatic reply. The checklist now shows “Messages arriving”.

If nothing arrives, the **Last problem** line on Settings → WhatsApp usually says why.

## Step 8: Switch to your real business number

1. In **WhatsApp Manager** (business.facebook.com → WhatsApp accounts), **Add phone number**,
   verify it by SMS or call, and wait for the display name to be approved.
2. Put the new number's **Phone number ID** into PayVerify and press Save. The webhook stays the
   same.
3. Meta may ask you to **verify your business** before you can start conversations with many
   customers. Replying to customers who message you first works before that.

**About your current number:** a number connected this way normally can't stay logged in to the
regular WhatsApp or WhatsApp Business app. Meta's “coexistence” feature can keep a WhatsApp
Business app number working in both. It's set up through a WhatsApp partner (a Business Solution
Provider), not these steps, and some app features (such as broadcast lists and disappearing
messages) stop working. The simplest path is a separate number just for payments.

Message charges: replies to customers are sent through Meta's paid platform, so check Meta's
current WhatsApp pricing page for your country.

---

## Optional, later: let customers pay inside WhatsApp

In India, Meta's **WhatsApp Payments** lets you send a “Review and pay” message. The customer pays
with any UPI app without leaving WhatsApp, and Meta notifies your server when the payment succeeds.
No screenshots or AI are needed, and fakes are impossible. It requires a payment gateway account
(Razorpay, PayU, BillDesk or Zaakpay) linked in WhatsApp Manager. PayVerify doesn't do this yet.
