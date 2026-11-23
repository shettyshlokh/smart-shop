"""
Sends messages to customers and shop owners.

Channels (each one works only if its settings are in .env):
  * SMS       - Twilio
  * WhatsApp  - Twilio WhatsApp (sandbox works for testing)
  * Email     - any SMTP server (e.g. Gmail with an App Password)

If nothing is configured, messages are printed in the terminal so the app
still works during development.
"""
import os
import smtplib
import threading
from email.message import EmailMessage

import requests


def _cfg(name, default=""):
    return os.environ.get(name, default).strip()


def normalize_phone(phone):
    """'9876543210' -> '+919876543210' (country code from DEFAULT_COUNTRY_CODE)."""
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return _cfg("DEFAULT_COUNTRY_CODE", "+91") + digits
    return "+" + digits


# ---------------------------------------------------------------- channels
def _twilio_send(to, body):
    sid, token = _cfg("TWILIO_ACCOUNT_SID"), _cfg("TWILIO_AUTH_TOKEN")
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data={"From": to[1], "To": to[0], "Body": body},
        auth=(sid, token), timeout=15)
    resp.raise_for_status()


def send_sms(phone, body):
    if not (_cfg("TWILIO_ACCOUNT_SID") and _cfg("TWILIO_SMS_FROM")):
        print(f"[SMS - not configured] to {phone}: {body}")
        return
    _twilio_send((normalize_phone(phone), _cfg("TWILIO_SMS_FROM")), body)


def send_whatsapp(phone, body):
    if not (_cfg("TWILIO_ACCOUNT_SID") and _cfg("TWILIO_WHATSAPP_FROM")):
        print(f"[WhatsApp - not configured] to {phone}: {body}")
        return
    _twilio_send(("whatsapp:" + normalize_phone(phone),
                  "whatsapp:" + _cfg("TWILIO_WHATSAPP_FROM")), body)


def send_email(to_addr, subject, body):
    if not to_addr:
        return
    if not (_cfg("SMTP_HOST") and _cfg("SMTP_USER")):
        print(f"[Email - not configured] to {to_addr}: {subject}\n{body}")
        return
    msg = EmailMessage()
    msg["From"] = _cfg("SMTP_FROM") or _cfg("SMTP_USER")
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(_cfg("SMTP_HOST"), int(_cfg("SMTP_PORT", "587")), timeout=20) as s:
        s.starttls()
        s.login(_cfg("SMTP_USER"), _cfg("SMTP_PASSWORD"))
        s.send_message(msg)


# ---------------------------------------------------------------- public API
def _run_all(jobs):
    for fn, args in jobs:
        try:
            fn(*args)
        except Exception as exc:          # one failing channel must not stop the others
            print(f"[notify] {fn.__name__} failed: {exc}")


def notify_customer(phone, email, subject, text):
    """Send the same message on every configured channel, in the background."""
    jobs = [(send_sms, (phone, text)), (send_whatsapp, (phone, text)),
            (send_email, (email, subject, text))]
    threading.Thread(target=_run_all, args=(jobs,), daemon=True).start()


def notify_owner(email, subject, text):
    threading.Thread(target=_run_all, args=([(send_email, (email, subject, text))],),
                     daemon=True).start()
