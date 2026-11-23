"""
Razorpay online payments (UPI, cards, netbanking, wallets).

Flow:
  1. Server creates a Razorpay order for the amount  -> create_razorpay_order()
  2. Browser opens Razorpay Checkout, customer pays
  3. Razorpay returns payment_id + signature to the browser, which posts them to us
  4. Server checks the signature with the secret key  -> verify_signature()
     Only then is the order marked as PAID.

Get test keys free at https://dashboard.razorpay.com (Settings -> API Keys).
"""
import hashlib
import hmac
import os

import requests


def keys():
    return os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")


def online_enabled():
    key_id, secret = keys()
    return bool(key_id and secret)


def create_razorpay_order(amount_rupees, receipt):
    key_id, secret = keys()
    resp = requests.post(
        "https://api.razorpay.com/v1/orders",
        json={"amount": int(round(amount_rupees * 100)),   # paise
              "currency": "INR", "receipt": receipt},
        auth=(key_id, secret), timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def verify_signature(rp_order_id, rp_payment_id, signature):
    _, secret = keys()
    expected = hmac.new(secret.encode(), f"{rp_order_id}|{rp_payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def refund(payment_id, amount_rupees):
    """Full refund of a Razorpay payment. Returns the refund id."""
    key_id, secret = keys()
    resp = requests.post(f"https://api.razorpay.com/v1/payments/{payment_id}/refund",
                         json={"amount": int(round(amount_rupees * 100))},
                         auth=(key_id, secret), timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


# ---------------------------------------------------------------- demo / test mode
def demo_enabled():
    """Fake 'test' payments for project demos. On only when Razorpay is NOT set up,
    and can be switched off with DEMO_PAYMENTS=0 in .env (do this before real use)."""
    return not online_enabled() and os.environ.get("DEMO_PAYMENTS", "1").strip() != "0"


# ---------------------------------------------------------------- UPI direct to the shop
import io
import re
from urllib.parse import quote

UPI_ID_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$")


def valid_upi_id(upi_id):
    return bool(UPI_ID_RE.match(upi_id or ""))


def upi_link(upi_id, payee_name, amount_rupees, note):
    """Standard UPI deep link - opens GPay / PhonePe / Paytm / BHIM with details filled in."""
    return (f"upi://pay?pa={quote(upi_id, safe='@')}&pn={quote(payee_name)}"
            f"&am={amount_rupees:.2f}&cu=INR&tn={quote(note)}")


def qr_svg(text):
    """QR code as inline SVG (no image files needed)."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]           # drop the <?xml ...?> header
