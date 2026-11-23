# Putting Smart Shop on the Google Play Store

Smart Shop is a PWA. To get it on Play you wrap the live website in an Android
package (a "Trusted Web Activity") with PWABuilder and upload that to Play Console.
Do DEPLOY.md first – the app must be online on **https**.

Order of work (about 3–4 weeks in total, mostly waiting):

| # | Step | Time |
|---|------|------|
| 1 | Create a Play developer account + identity check | ~1–3 days |
| 2 | Put the site online (DEPLOY.md) | 1 hour |
| 3 | Make the Android package with PWABuilder | 15 min |
| 4 | Create the app in Play Console, fill listing + policy forms | 1–2 hours |
| 5 | Closed test: 12+ testers for 14 days in a row | 14+ days |
| 6 | Apply for production, Google reviews | a few days |

---------------------------------------------------------------------
## 1. Developer account
* https://play.google.com/console/signup – you must be 18+.
* Choose **Personal** account (an Organisation account needs a D-U-N-S number for a registered business).
* Pay the one-time registration fee and verify your identity (government ID, phone, email).
* Use an email you will keep for years – the app is tied to it.

## 2. Put the site online
Follow DEPLOY.md. Then in `.env` set:
```
DEMO_PAYMENTS=0            # reviewers and users must not see fake payments in production
HTTPS=1
DEVELOPER_NAME=Your Name
CONTACT_EMAIL=you@example.com
SECRET_KEY=<long random text>
```
Check these open without errors:
* `https://YOURSITE/privacy`
* `https://YOURSITE/delete-account`

## 3. Make the Android package (PWABuilder)
1. https://www.pwabuilder.com → paste your site → **Start** → **Package for stores** → **Android**.
2. Options:
   * **Package ID:** `com.yourname.smartshop` – ⚠️ can never be changed later.
   * App name: `Smart Shop`, Launcher name: `Smart Shop`.
   * Leave "Signing key: New" → it creates a key for you.
3. Download the zip. Inside: `app-release-bundle.aab` (upload this to Play), an `.apk` (to test on your own phone),
   `signing.keystore` + `signing-key-info.txt` → **back these up somewhere safe**. Without them you can never update the app.
4. Open `assetlinks.json` from the zip and copy into `.env`:
   ```
   ANDROID_PACKAGE=com.yourname.smartshop
   ANDROID_SHA256=<the sha256 fingerprint from the file>
   ```
   Reload the website. `https://YOURSITE/.well-known/assetlinks.json` must show them.

## 4. Play Console – create the app
**Create app** → name *Smart Shop*, App, Free, accept declarations.

### Store listing (Grow → Store presence → Main store listing)
Images are in the `playstore` folder:
* App icon: `play-icon-512.png` (512×512)
* Feature graphic: `feature-graphic-1024x500.png`
* Phone screenshots: `screen-1-shop.png` … `screen-5-wallet.png` (1080×1920). Retake the home screen with the real map from your live site if you like.

**Short description (max 80 chars):**
```
Order from local shops near you – home delivery or pack & pickup.
```
**Full description:**
```
Smart Shop connects you with the shops in your neighbourhood.

🛒 ORDER FROM LOCAL SHOPS
• See shops near you, sorted by distance, with a map
• Open / closed status and opening hours for every shop
• Browse items, prices and stock

🚚 DELIVERY OR PICKUP – YOUR CHOICE
• Home delivery inside the shop's delivery area
• Pack & Pickup: the shop packs your order, you collect it
• Live delivery time – "arrives in ~10 min" – based on distance, your items and how busy the shop is

💳 PAY YOUR WAY
• UPI (GPay, PhonePe, Paytm, BHIM) straight to the shop
• Card / netbanking
• Cash on delivery or at pickup
• Smart Shop Wallet – instant refunds and faster checkout

📦 TRACK & MANAGE
• Order status: Placed → Accepted → Packed → Out for delivery
• SMS / WhatsApp / email updates
• Cancel before packing and choose where your refund goes

🏪 FOR SHOP OWNERS
• Register your shop, set location, hours, delivery area and UPI ID
• Add items and stock, see incoming orders with due times
• Confirm UPI payments and manage refunds
```
Category: **Shopping**. Contact email: your email. Privacy policy: `https://YOURSITE/privacy`.

### App content (Policy → App content) – suggested answers
Read Google's help text next to each question and answer for **your** setup – you are responsible for these.

* **Privacy policy:** `https://YOURSITE/privacy`
* **App access:** "All or some functionality is restricted" → add login details for the reviewer:
  * Customer: `9999999999` / `demo123`
  * Shop owner: `demo@shop.com` / `demo123`
  * Note: "Pay with Cash on Delivery to place a test order."
* **Ads:** No ads.
* **Content rating:** category *All other app types* → answer No to violence, sexuality, gambling etc.
  "Can users interact / exchange info?" → users share name, phone and address with the shop they order from.
  "Shares user's location with other users?" → No (customers' locations are not shown to anyone; shop addresses are public business info).
* **Target audience:** 18 and over. Not designed for children.
* **News app:** No. **Government app:** No. **Financial features:** none of the listed (the app sells physical goods – Google Play billing is not used for physical goods).
* **Data safety:**
  * Collects data: **Yes**. Encrypted in transit: **Yes** (https).
  * Users can request deletion: **Yes** → web link `https://YOURSITE/delete-account`.
  * Data types and purposes:
    | Data type | Collected | Purpose | Required? |
    |---|---|---|---|
    | Name | Yes | App functionality, account management | Required |
    | Email address | Yes | Account management, communications | Optional |
    | Phone number | Yes | Account, app functionality, communications | Required |
    | Address | Yes | App functionality (delivery) | Optional (pickup needs none) |
    | Precise / approximate location | Yes | App functionality (nearby shops, delivery area) | Optional |
    | Purchase history | Yes | App functionality | Required |
    | Other financial info (UPI ref, wallet) | Yes | App functionality | Required |
  * Card numbers are typed into Razorpay's checkout, not stored by Smart Shop – check Razorpay's own Data safety guidance and Google's "data handled by payment processors" note before answering.
  * The shop receives the customer's name/phone/address because the customer places the order with that shop – read Google's definition of "sharing" (user-initiated transfers) and decide how to declare it.

## 5. Closed testing (new personal accounts must do this)
Rules for personal accounts created after 13 Nov 2023:
**at least 12 testers, opted in continuously for 14 days**, before you can apply for production.
1. Testing → **Closed testing** → Create track → upload the `.aab` → Countries: India → Review & roll out.
2. Testers: create an email list with 15–20 classmates/family (Gmail accounts they use on their Android phone).
3. Send them the **opt-in link**. Each tester must: open the link → *Become a tester* → install from Play → keep it installed 14 days.
4. Ask them to actually use it (place a cash test order, check the wallet) and send feedback.
5. Don't keep pushing new builds during the 14 days – upload your best build on day 1.

## 6. Apply for production
Dashboard → **Apply for production** → answer the questions honestly and in detail
(who tested, what feedback you got, what you fixed, why the app is ready).
After approval: Production → Create release → upload the `.aab` → roll out. Review usually takes a few days.

### After Google re-signs your app
Play App Signing gives your app a second key. Copy **Setup → App signing → App signing key certificate SHA-256**
and add it to `.env` with a comma:
```
ANDROID_SHA256=<PWABuilder key SHA>,<Play app signing SHA>
```
Reload the site – otherwise the Play-installed app shows a browser address bar.

## Updating the app later
Most changes (pages, features, prices) need **no Play update** – you just update the website.
Only change the Play package if you change the icon, name or app package settings (re-run PWABuilder with the **same** signing key).
