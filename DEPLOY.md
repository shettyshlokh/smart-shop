# Turning Smart Shop into an app

Smart Shop is a **PWA (Progressive Web App)**: one code base that works as a website,
installs on Android / iPhone / Windows like an app, and can be packaged as an
Android **APK** for the Play Store.

Phones only allow installing, GPS location and the camera on **https** sites, so step 1
is to put the app online.

---------------------------------------------------------------------
## Step 1 – Put it online for free (PythonAnywhere)

1. Sign up at https://www.pythonanywhere.com (free "Beginner" account).
   Your app will live at `https://YOURNAME.pythonanywhere.com`.
2. **Files** tab -> upload `shopcart_vX.zip` -> open a **Bash console** and run:
   ```
   unzip shopcart_v8.zip
   cd shopcart
   pip install --user -r requirements.txt
   cp .env.example .env
   nano .env          # set SECRET_KEY to a long random text, HTTPS=1 (Ctrl+O, Enter, Ctrl+X)
   ```
3. **Web** tab -> **Add a new web app** -> *Manual configuration* -> Python 3.x.
4. In the Web tab, click the **WSGI configuration file** link, delete everything, paste:
   ```
   import sys
   sys.path.insert(0, '/home/YOURNAME/shopcart')
   from wsgi import application
   ```
5. In the Web tab under **Static files** add:  URL `/static/`  ->  Directory `/home/YOURNAME/shopcart/static`
6. Turn on **Force HTTPS**, then press the green **Reload** button.
7. Open `https://YOURNAME.pythonanywhere.com` – done!

> Free accounts can only call websites on PythonAnywhere's allowlist. If SMS / email /
> Razorpay calls fail there with "403 Forbidden", that's the reason – request the site
> be added, or use a paid plan. The app itself, UPI, test payments, wallet and maps
> (which load in the customer's browser) all work fine.

---------------------------------------------------------------------
## Step 2 – Install it on a phone (no store needed)

* **Android (Chrome):** open the site -> tap **⬇ Install app** in the top bar
  (or menu ⋮ -> *Install app*). It gets its own icon, opens full-screen with the bottom tab bar.
* **iPhone (Safari):** tap **Share ⬆** -> **Add to Home Screen**.
* **Windows / Mac (Chrome or Edge):** click the install icon in the address bar.

---------------------------------------------------------------------
## Step 3 – Make an Android APK (for the Play Store or to share the file)

1. Go to https://www.pwabuilder.com, paste `https://YOURNAME.pythonanywhere.com`, click **Start**.
2. It checks the manifest, icons and service worker (all already included).
3. Click **Package for stores** -> **Android** -> **Generate**. Download the zip.
   It contains the `.apk` (install on a phone to test), the `.aab` (for Play Store)
   and a `signing.keystore` + `signing-key-info.txt` — **keep these safe**, you need
   the same key for every future update.
4. **Remove the browser address bar** inside the app: open `assetlinks.json` from the zip,
   copy `package_name` and the `sha256_cert_fingerprints` value into `.env`:
   ```
   ANDROID_PACKAGE=com.yourname.smartshop
   ANDROID_SHA256=AB:CD:...:EF
   ```
   Reload the web app. Check `https://YOURNAME.pythonanywhere.com/.well-known/assetlinks.json`
   shows them.
5. Play Store: needs a Google Play developer account (one-time fee). Upload the `.aab`.
   If you publish, Play re-signs the app – copy the **App signing key SHA-256** from
   Play Console -> *App integrity* and add it to `ANDROID_SHA256` (comma-separated).

---------------------------------------------------------------------
## Before selling to real shops
* `.env`: `DEMO_PAYMENTS=0`, a strong `SECRET_KEY`, real Razorpay **live** keys.
* Take a backup of `shopcart.db` regularly (Files tab -> download).
