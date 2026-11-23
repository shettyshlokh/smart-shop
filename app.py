"""
Smart Shop - Multi-shop ordering app
-----------------------------------
* Shop owners register, set shop name, opening/closing time, delivery fee,
  and add/edit/remove items.
* Customers browse open shops, add items to cart and choose:
      1) HOME DELIVERY  (address required, delivery fee added)
      2) PACK & PICKUP  (shop packs the order, customer collects it)
* Owners see incoming orders and update their status.

Run:  pip install flask
      python app.py
Open: http://127.0.0.1:5000
"""
import math
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, g, render_template, request, redirect,
                   url_for, session, flash, abort, send_from_directory, jsonify)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

try:                                    # load settings from .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import payments
from notify import notify_customer, notify_owner

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "shopcart.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)   # correct https links when hosted
app.config.update(SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("HTTPS", "0") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))   # stay logged in, like an app

AWAITING = "Awaiting payment"          # online order created, not paid yet
ORDER_STATUSES = ["Placed", "Accepted", "Packed", "Out for delivery",
                  "Ready for pickup", "Completed", "Cancelled"]

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    address TEXT,
    phone TEXT,
    open_time TEXT NOT NULL DEFAULT '09:00',
    close_time TEXT NOT NULL DEFAULT '21:00',
    delivery_fee REAL NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('delivery', 'pickup')),
    address TEXT,
    pickup_time TEXT,
    subtotal REAL NOT NULL,
    delivery_fee REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'Placed',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    phone TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    address TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_txns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount REAL NOT NULL,              -- + money in, - money out
    reason TEXT NOT NULL,
    order_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    price REAL NOT NULL,
    qty INTEGER NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


NEW_ORDER_COLUMNS = {                   # added for payments + messages
    "customer_email": "TEXT",
    "payment_method": "TEXT NOT NULL DEFAULT 'cash'",     # 'cash' or 'online'
    "payment_status": "TEXT NOT NULL DEFAULT 'Unpaid'",   # 'Unpaid' / 'Paid' / 'Failed'
    "rp_order_id": "TEXT",
    "rp_payment_id": "TEXT",
    "customer_id": "INTEGER REFERENCES customers(id)",
    "distance_km": "REAL",          # shop -> customer, at the time of ordering
    "eta_minutes": "INTEGER",       # estimated minutes to deliver / be ready
    "eta_at": "TEXT",               # clock time it should arrive / be ready
    "payment_ref": "TEXT",          # UPI reference (UTR) / Razorpay payment id / refund id
    "wallet_used": "REAL NOT NULL DEFAULT 0",   # part of the total paid from the wallet
    "amount_due": "REAL",           # total - wallet_used = what is paid by cash/UPI/card
    "refund_to": "TEXT",            # 'wallet' or 'original'
    "cancel_reason": "TEXT",
    "cancelled_by": "TEXT",         # 'customer' or 'shop'
}


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # upgrade an old database without deleting data
        existing = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
        for col, typ in NEW_ORDER_COLUMNS.items():
            if col not in existing:
                db.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
        if "item_id" not in {r[1] for r in db.execute("PRAGMA table_info(order_items)")}:
            db.execute("ALTER TABLE order_items ADD COLUMN item_id INTEGER")
        # location columns
        for table, cols in {"shops": {"lat": "REAL", "lng": "REAL", "upi_id": "TEXT",
                                      "delivery_radius_km": "REAL NOT NULL DEFAULT 5"},
                            "customers": {"lat": "REAL", "lng": "REAL"}}.items():
            have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        db.execute("UPDATE orders SET amount_due = total WHERE amount_due IS NULL")


# ---------------------------------------------------------------- location helpers
def distance_km(lat1, lng1, lat2, lng2):
    """Straight-line distance between two points on Earth (Haversine formula)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_latlng(lat, lng):
    """Return (lat, lng) as floats if valid, else None."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng
    return None


def user_location():
    """Customer's location: from the session, else from their saved account."""
    loc = session.get("loc")
    if loc:
        return tuple(loc)
    me = current_customer()
    if me and me["lat"] is not None:
        return me["lat"], me["lng"]
    return None


def save_user_location(loc):
    session["loc"] = list(loc)
    if session.get("customer_id"):
        db = get_db()
        db.execute("UPDATE customers SET lat=?, lng=? WHERE id=?",
                   (loc[0], loc[1], session["customer_id"]))
        db.commit()


def shop_distance(shop, loc):
    if loc is None or shop["lat"] is None:
        return None
    return distance_km(loc[0], loc[1], shop["lat"], shop["lng"])


def delivers_to(shop, dist):
    """None = unknown (no location), True/False = inside/outside delivery radius."""
    if dist is None:
        return None
    return dist <= shop["delivery_radius_km"]


# ---------------------------------------------------------------- delivery time (ETA)
# Change these numbers to tune the estimate.
ETA_BASE_MIN = 5          # every order: accept + bill + bag
ETA_PER_ITEM_MIN = 1      # each item (unit) to pick and pack
ETA_MAX_PACK_MIN = 20     # packing never counted as more than this
ETA_PER_QUEUED_MIN = 4    # each order already waiting at the shop
ETA_MIN_PER_KM = 4        # travel speed ~15 km/h in city traffic
ETA_UNKNOWN_TRAVEL = 10   # travel time when the customer's distance is unknown
ETA_MINIMUM = 10          # never promise less than this
ETA_TYPICAL_ITEMS = 3     # basket size used for the estimate on the shop list
ACTIVE_STATUSES = ("Placed", "Accepted", "Packed")   # orders still being prepared


def orders_in_queue(shop_id, exclude_id=None):
    q = "SELECT COUNT(*) FROM orders WHERE shop_id=? AND status IN (?,?,?)"
    args = [shop_id, *ACTIVE_STATUSES]
    if exclude_id:
        q += " AND id != ?"
        args.append(exclude_id)
    return get_db().execute(q, args).fetchone()[0]


def estimate_eta(item_count, mode, distance, queue):
    """Return (minutes, breakdown). Rounded up to the next 5 minutes, at least ETA_MINIMUM."""
    pack = min(ETA_BASE_MIN + ETA_PER_ITEM_MIN * item_count, ETA_MAX_PACK_MIN)
    wait = queue * ETA_PER_QUEUED_MIN
    travel = 0
    if mode == "delivery":
        travel = round(distance * ETA_MIN_PER_KM) if distance is not None else ETA_UNKNOWN_TRAVEL
    raw = pack + wait + travel
    minutes = max(ETA_MINIMUM, math.ceil(raw / 5) * 5)
    return minutes, {"pack": pack, "count": item_count, "queue": queue, "wait": wait,
                     "travel": travel, "mode": mode}


def eta_text(minutes):
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h} hr {m} min" if m else f"{h} hr"
    return f"{minutes} min"


# ---------------------------------------------------------------- wallet
def wallet_balance(customer_id):
    if not customer_id:
        return 0.0
    v = get_db().execute("SELECT COALESCE(SUM(amount), 0) FROM wallet_txns WHERE customer_id=?",
                         (customer_id,)).fetchone()[0]
    return round(v, 2)


def wallet_add(customer_id, amount, reason, order_id=None):
    """Add (+) or take (-) money. The caller commits."""
    get_db().execute("""INSERT INTO wallet_txns (customer_id, amount, reason, order_id, created_at)
                        VALUES (?,?,?,?,?)""",
                     (customer_id, round(amount, 2), reason, order_id,
                      datetime.now().strftime("%Y-%m-%d %H:%M")))


CUSTOMER_CANCELLABLE = ("Awaiting payment", "Placed", "Accepted")
CANCEL_REASONS = ["Ordered by mistake", "Want to change items", "Delivery is taking too long",
                  "Found a better price", "Other"]


def cancel_order(order, by, reason, refund_to="original"):
    """Cancel an order: put stock back and return the money.
    refund_to = 'wallet' (instant) or 'original' (bank / UPI / card).
    Returns a sentence for the customer message. The caller commits."""
    db = get_db()
    oid, cid = order["id"], order["customer_id"]
    if order["status"] != AWAITING:          # stock was taken when the order was confirmed
        for line in db.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall():
            if line["item_id"]:
                db.execute("UPDATE items SET stock = stock + ? WHERE id=?",
                           (line["qty"], line["item_id"]))
    db.execute("UPDATE orders SET status='Cancelled', cancel_reason=?, cancelled_by=? WHERE id=?",
               (reason, by, oid))

    notes = []
    wallet_part = order["wallet_used"] or 0
    if wallet_part > 0 and cid:
        wallet_add(cid, wallet_part, f"Refund: order #{oid} cancelled", oid)
        notes.append(f"Rs.{wallet_part:.2f} is back in your Smart Shop wallet")

    due = order["amount_due"] if order["amount_due"] is not None else order["total"]
    status = order["payment_status"]
    method = order["payment_method"]
    if due > 0 and method not in ("cash", "wallet") and status in ("Paid", "Verifying"):
        if refund_to == "wallet" and status == "Paid" and cid:
            wallet_add(cid, due, f"Refund: order #{oid} cancelled", oid)
            db.execute("UPDATE orders SET payment_status='Refunded', refund_to='wallet' WHERE id=?",
                       (oid,))
            notes.append(f"Rs.{due:.2f} refunded to your Smart Shop wallet (available now)")
        elif method == "online" and order["rp_payment_id"]:
            try:
                rid = payments.refund(order["rp_payment_id"], due)
                db.execute("""UPDATE orders SET payment_status='Refunded', refund_to='original',
                              payment_ref=? WHERE id=?""", (rid, oid))
                notes.append(f"Rs.{due:.2f} refund started to your card/bank (5-7 days)")
            except Exception as exc:
                print(f"[razorpay refund] {exc}")
                db.execute("UPDATE orders SET payment_status='Refund due' WHERE id=?", (oid,))
                notes.append(f"the shop will refund your Rs.{due:.2f}")
        elif method == "demo":
            db.execute("UPDATE orders SET payment_status='Refunded', refund_to='original' WHERE id=?",
                       (oid,))
            notes.append(f"test payment of Rs.{due:.2f} refunded")
        else:                                 # UPI paid straight to the shop: shop returns it
            db.execute("UPDATE orders SET payment_status='Refund due', refund_to=? WHERE id=?",
                       (refund_to, oid))
            notes.append(f"the shop will return your Rs.{due:.2f}"
                         + (" to your wallet after checking the payment"
                            if refund_to == "wallet" else " to your UPI account"))
    elif wallet_part > 0 and due == 0:
        db.execute("UPDATE orders SET payment_status='Refunded', refund_to='wallet' WHERE id=?",
                   (oid,))
    return (" " + "; ".join(notes)[0].upper() + "; ".join(notes)[1:] + ".") if notes else ""


# ---------------------------------------------------------------- payment methods
PAYMENT_LABELS = {"cash": "Cash", "upi": "UPI", "online": "Card / Netbanking",
                  "demo": "Test payment", "wallet": "Wallet"}


def payment_methods(shop):
    """Payment options this shop can accept right now."""
    methods = ["cash"]
    if shop["upi_id"]:
        methods.append("upi")
    if payments.online_enabled():
        methods.append("online")
    elif payments.demo_enabled():
        methods.append("demo")
    return methods


# ---------------------------------------------------------------- helpers
def is_open(shop, now=None):
    """True if the shop is open right now. Handles overnight shops (e.g. 20:00-02:00)."""
    now = (now or datetime.now()).strftime("%H:%M")
    o, c = shop["open_time"], shop["close_time"]
    if o == c:                      # same time = open 24 hours
        return True
    if o < c:
        return o <= now < c
    return now >= o or now < c      # crosses midnight


@app.template_filter("clock")
def clock_filter(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return ""


@app.context_processor
def inject_helpers():
    return {"is_open": is_open, "eta_text": eta_text, "pay_labels": PAYMENT_LABELS,
            "wallet": wallet_balance(session.get("customer_id")), "cart_count": sum(session.get("cart", {}).values()),
            "customer": current_customer()}


def current_customer():
    cid = session.get("customer_id")
    if not cid:
        return None
    return get_db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()


def safe_next(target):
    """Only allow redirects to pages on this site."""
    return target if target and target.startswith("/") and not target.startswith("//") else None


def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_customer() is None:
            flash("Please log in to continue.")
            return redirect(url_for("customer_login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "shop_id" not in session:
            flash("Please log in as a shop owner.")
            return redirect(url_for("owner_login"))
        return view(*args, **kwargs)
    return wrapped


def get_shop(shop_id):
    shop = get_db().execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    if shop is None:
        abort(404)
    return shop


def cart_details():
    """Return (shop, lines, subtotal) for the cart in the session."""
    cart = session.get("cart", {})
    shop_id = session.get("cart_shop")
    if not cart or not shop_id:
        return None, [], 0.0
    db = get_db()
    shop = db.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    lines, subtotal = [], 0.0
    for item_id, qty in cart.items():
        item = db.execute("SELECT * FROM items WHERE id=? AND shop_id=?",
                          (int(item_id), shop_id)).fetchone()
        if item:
            line_total = item["price"] * qty
            subtotal += line_total
            lines.append({"item": item, "qty": qty, "total": line_total})
    return shop, lines, subtotal


# ---------------------------------------------------------------- app (PWA) files
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(STATIC_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"          # phones pick up new versions quickly
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC_DIR, "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/.well-known/assetlinks.json")
def assetlinks():
    """Digital Asset Links for the Android app (removes the browser bar).
    Set ANDROID_PACKAGE and ANDROID_SHA256 in .env after making the APK with PWABuilder."""
    pkg, sha = os.environ.get("ANDROID_PACKAGE", ""), os.environ.get("ANDROID_SHA256", "")
    if not (pkg and sha):
        return jsonify([])
    return jsonify([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": pkg,
                   "sha256_cert_fingerprints": [s.strip() for s in sha.split(",")]},
    }])


@app.before_request
def keep_logged_in():
    session.permanent = True


@app.route("/account")
@customer_required
def account():
    return render_template("account.html", me=current_customer())


# ---------------------------------------------------------------- privacy & account deletion
# (required by Google Play: privacy policy + in-app and web account deletion)
ACTIVE_ORDER_STATUSES = ("Placed", "Accepted", "Packed", "Out for delivery", "Ready for pickup")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html",
                           contact=os.environ.get("CONTACT_EMAIL", "your-email@example.com"),
                           developer=os.environ.get("DEVELOPER_NAME", "the Smart Shop team"),
                           updated="23 November 2026")


@app.route("/delete-account")
def delete_account_info():
    """Public page - this is the 'web link' you give Google Play for account deletion."""
    return render_template("delete_info.html",
                           contact=os.environ.get("CONTACT_EMAIL", "your-email@example.com"))


@app.route("/account/delete", methods=["GET", "POST"])
@customer_required
def customer_delete():
    db = get_db()
    me = current_customer()
    active = db.execute(f"""SELECT COUNT(*) FROM orders WHERE customer_id=? AND
                            (status IN ({",".join("?" * len(ACTIVE_ORDER_STATUSES))})
                             OR payment_status='Refund due')""",
                        (me["id"], *ACTIVE_ORDER_STATUSES)).fetchone()[0]
    if request.method == "POST":
        if active:
            flash("You have orders in progress or a refund pending. Please wait until they are finished.")
        elif not check_password_hash(me["password_hash"], request.form.get("password", "")):
            flash("Wrong password.")
        elif not request.form.get("confirm"):
            flash("Please tick the box to confirm.")
        else:
            for o in db.execute("SELECT * FROM orders WHERE customer_id=? AND status=?",
                                (me["id"], AWAITING)).fetchall():
                cancel_order(o, "customer", "Account deleted")
            # keep the shop's sales records, but remove everything that identifies the person
            db.execute("""UPDATE orders SET customer_name='Deleted user', customer_phone='deleted',
                          customer_email=NULL, address=NULL, customer_id=NULL WHERE customer_id=?""",
                       (me["id"],))
            db.execute("DELETE FROM wallet_txns WHERE customer_id=?", (me["id"],))
            db.execute("DELETE FROM customers WHERE id=?", (me["id"],))
            db.commit()
            session.clear()
            flash("Your account and personal data have been deleted. Thank you for using Smart Shop.")
            return redirect(url_for("index"))
    return render_template("delete_account.html", me=me, active=active, kind="customer",
                           balance=wallet_balance(me["id"]))


@app.route("/owner/delete", methods=["GET", "POST"])
@owner_required
def owner_delete():
    db = get_db()
    shop = get_shop(session["shop_id"])
    active = db.execute(f"""SELECT COUNT(*) FROM orders WHERE shop_id=? AND
                            (status IN ({",".join("?" * len(ACTIVE_ORDER_STATUSES))})
                             OR payment_status IN ('Refund due', 'Verifying'))""",
                        (shop["id"], *ACTIVE_ORDER_STATUSES)).fetchone()[0]
    if request.method == "POST":
        if active:
            flash("Finish or cancel all open orders and pending refunds first.")
        elif not check_password_hash(shop["password_hash"], request.form.get("password", "")):
            flash("Wrong password.")
        elif not request.form.get("confirm"):
            flash("Please tick the box to confirm.")
        else:
            db.execute("DELETE FROM shops WHERE id=?", (shop["id"],))   # items + orders cascade
            db.commit()
            session.pop("shop_id", None)
            flash("Your shop account and all its data have been deleted.")
            return redirect(url_for("index"))
    return render_template("delete_account.html", me=shop, active=active, kind="shop", balance=0)


# ---------------------------------------------------------------- customer side
RADIUS_CHOICES = [2, 5, 10, 25]


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    new_loc = parse_latlng(request.args.get("lat"), request.args.get("lng"))
    if new_loc:                                   # customer just shared / searched a location
        save_user_location(new_loc)
        session["loc_label"] = request.args.get("label", "")[:80] or "your location"
        return redirect(url_for("index", q=q or None, r=request.args.get("r")))
    loc = user_location()
    try:
        radius = float(request.args.get("r") or 0)  # 0 = show all
    except ValueError:
        radius = 0

    db = get_db()
    rows = db.execute("SELECT * FROM shops WHERE name LIKE ? ORDER BY name",
                      (f"%{q}%",)).fetchall()
    shops = []
    for s in rows:
        d = shop_distance(s, loc)
        if radius and (d is None or d > radius):
            continue
        dl = delivers_to(s, d)
        eta, _ = estimate_eta(ETA_TYPICAL_ITEMS, "pickup" if dl is False else "delivery",
                              d, orders_in_queue(s["id"]))
        shops.append({**dict(s), "distance": d, "delivers": dl, "eta": eta})
    # nearest first; shops without a location go last
    shops.sort(key=lambda s: (s["distance"] is None, s["distance"] or 0, s["name"]))
    return render_template("index.html", shops=shops, q=q, loc=loc, radius=radius,
                           radius_choices=RADIUS_CHOICES,
                           loc_label=session.get("loc_label", "your saved location"))


@app.route("/location/clear")
def clear_location():
    session.pop("loc", None)
    session.pop("loc_label", None)
    if session.get("customer_id"):
        db = get_db()
        db.execute("UPDATE customers SET lat=NULL, lng=NULL WHERE id=?", (session["customer_id"],))
        db.commit()
    return redirect(url_for("index"))


@app.route("/shop/<int:shop_id>")
def shop_page(shop_id):
    shop = get_shop(shop_id)
    items = get_db().execute(
        "SELECT * FROM items WHERE shop_id=? AND available=1 ORDER BY name",
        (shop_id,)).fetchall()
    dist = shop_distance(shop, user_location())
    queue = orders_in_queue(shop_id)
    return render_template("shop.html", shop=shop, items=items, distance=dist,
                           delivers=delivers_to(shop, dist), queue=queue,
                           eta_delivery=estimate_eta(ETA_TYPICAL_ITEMS, "delivery", dist, queue)[0],
                           eta_pickup=estimate_eta(ETA_TYPICAL_ITEMS, "pickup", dist, queue)[0])


@app.route("/cart/add/<int:item_id>", methods=["POST"])
def cart_add(item_id):
    item = get_db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None or not item["available"]:
        abort(404)
    qty = max(1, int(request.form.get("qty", 1)))

    # A cart holds items from one shop only
    if session.get("cart_shop") not in (None, item["shop_id"]):
        session["cart"] = {}
        flash("Your cart had items from another shop, so it was cleared.")
    session["cart_shop"] = item["shop_id"]

    cart = session.get("cart", {})
    new_qty = cart.get(str(item_id), 0) + qty
    if new_qty > item["stock"]:
        flash(f"Only {item['stock']} of {item['name']} in stock.")
        new_qty = item["stock"]
    if new_qty > 0:
        cart[str(item_id)] = new_qty
    session["cart"] = cart
    flash(f"Added {item['name']} to cart.")
    return redirect(url_for("shop_page", shop_id=item["shop_id"]))


@app.route("/cart", methods=["GET", "POST"])
def cart_view():
    if request.method == "POST":            # update quantities
        cart = {}
        for key, val in request.form.items():
            if key.startswith("qty_"):
                q = int(val or 0)
                if q > 0:
                    cart[key[4:]] = q
        session["cart"] = cart
        if not cart:
            session.pop("cart_shop", None)
        return redirect(url_for("cart_view"))
    shop, lines, subtotal = cart_details()
    return render_template("cart.html", shop=shop, lines=lines, subtotal=subtotal)


@app.route("/cart/clear")
def cart_clear():
    session.pop("cart", None)
    session.pop("cart_shop", None)
    return redirect(url_for("index"))


def order_link(order):
    return url_for("order_status", order_id=order["id"], phone=order["customer_phone"],
                   _external=True)


def finalize_order(order_id):
    """Reduce stock and send 'order placed' messages. Called once the order is confirmed
    (immediately for cash, after successful payment for online)."""
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    shop = get_shop(order["shop_id"])
    lines = db.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    for line in lines:
        db.execute("UPDATE items SET stock = MAX(stock - ?, 0) WHERE id=? AND shop_id=?",
                   (line["qty"], line["item_id"], shop["id"]))
    eta, _ = estimate_eta(sum(l["qty"] for l in lines), order["mode"], order["distance_km"],
                          orders_in_queue(shop["id"], exclude_id=order_id))
    eta_at = datetime.now() + timedelta(minutes=eta)
    db.execute("UPDATE orders SET eta_minutes=?, eta_at=? WHERE id=?",
               (eta, eta_at.strftime("%Y-%m-%d %H:%M"), order_id))
    db.commit()
    when = f"arrives in ~{eta_text(eta)} (by {eta_at:%I:%M %p})" if order["mode"] == "delivery" \
        else f"ready in ~{eta_text(eta)} (by {eta_at:%I:%M %p})"

    how = ("Home Delivery to " + order["address"]) if order["mode"] == "delivery" \
        else "Pack & Pickup" + (f" at {order['pickup_time']}" if order["pickup_time"] else "")
    pay = {"Paid": "Paid from wallet" if order["payment_method"] == "wallet" else "Paid online",
           "Verifying": "UPI payment being verified"}.get(
        order["payment_status"],
        f"Pay Rs.{order['amount_due']:.2f} cash on "
        + ("delivery" if order["mode"] == "delivery" else "pickup"))
    notify_customer(
        order["customer_phone"], order["customer_email"],
        f"Order #{order_id} placed at {shop['name']}",
        f"Hi {order['customer_name']}, your order #{order_id} at {shop['name']} is placed. "
        f"Total Rs.{order['total']:.2f} ({pay}). {how} - {when}. Track: {order_link(order)}")
    items_txt = ", ".join(f"{l['item_name']} x{l['qty']}" for l in lines)
    notify_owner(
        shop["owner_email"], f"New order #{order_id} - {order['mode'].upper()}",
        f"New order #{order_id} from {order['customer_name']} ({order['customer_phone']}).\n"
        f"{how}\nItems: {items_txt}\nTotal: Rs.{order['total']:.2f} - {pay}\n"
        f"Promised to customer: {when}")


@app.route("/checkout", methods=["GET", "POST"])
@customer_required
def checkout():
    shop, lines, subtotal = cart_details()
    if not lines:
        flash("Your cart is empty.")
        return redirect(url_for("index"))
    dist = shop_distance(shop, user_location())
    n_items, queue = sum(l["qty"] for l in lines), orders_in_queue(shop["id"])
    ctx = dict(shop=shop, lines=lines, subtotal=subtotal, methods=payment_methods(shop),
               wallet_bal=wallet_balance(session["customer_id"]),
               distance=dist, can_deliver=delivers_to(shop, dist) is not False,
               eta_d=estimate_eta(n_items, "delivery", dist, queue),
               eta_p=estimate_eta(n_items, "pickup", dist, queue))

    if request.method == "POST":
        f = request.form
        name, phone = f.get("name", "").strip(), f.get("phone", "").strip()
        email = f.get("email", "").strip()
        mode, pay_method = f.get("mode"), f.get("payment")
        address, pickup_time = f.get("address", "").strip(), f.get("pickup_time", "").strip()

        errors = []
        if not name or not phone:
            errors.append("Name and phone are required.")
        if mode not in ("delivery", "pickup"):
            errors.append("Choose Delivery or Pack & Pickup.")
        if mode == "delivery" and not address:
            errors.append("Delivery address is required.")
        if mode == "delivery" and not ctx["can_deliver"]:
            errors.append(f"You are {dist:.1f} km away. {shop['name']} delivers only within "
                          f"{shop['delivery_radius_km']:g} km - please choose Pack & Pickup.")
        fee = shop["delivery_fee"] if mode == "delivery" else 0
        total = round(subtotal + fee, 2)
        wallet_used = min(ctx["wallet_bal"], total) if f.get("use_wallet") else 0
        due = round(total - wallet_used, 2)
        if due == 0:
            pay_method = "wallet"                 # wallet covers everything
        elif pay_method not in ctx["methods"]:
            errors.append("Choose a payment method.")
        if not is_open(shop):
            errors.append(f"{shop['name']} is closed now. Opens at {shop['open_time']}.")
        if errors:
            for e in errors:
                flash(e)
            return render_template("checkout.html", form=f, **ctx)

        db = get_db()
        for line in lines:                  # check stock right before ordering
            fresh = db.execute("SELECT stock FROM items WHERE id=?",
                               (line["item"]["id"],)).fetchone()
            if fresh["stock"] < line["qty"]:
                flash(f"Sorry, only {fresh['stock']} of {line['item']['name']} left.")
                return redirect(url_for("cart_view"))

        cur = db.execute(
            """INSERT INTO orders (shop_id, customer_id, customer_name, customer_phone,
                   customer_email, mode, address, pickup_time, subtotal, delivery_fee, total,
                   status, payment_method, payment_status, created_at, distance_km,
                   wallet_used, amount_due)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (shop["id"], session["customer_id"], name, phone, email or None, mode,
             address if mode == "delivery" else None,
             pickup_time if mode == "pickup" else None,
             subtotal, fee, total,
             "Placed" if pay_method in ("cash", "wallet") else AWAITING, pay_method,
             "Paid" if pay_method == "wallet" else "Unpaid",
             datetime.now().strftime("%Y-%m-%d %H:%M"), dist, wallet_used, due))
        order_id = cur.lastrowid
        if wallet_used:
            wallet_add(session["customer_id"], -wallet_used, f"Paid for order #{order_id}", order_id)
        if mode == "delivery":              # remember the address for next time
            db.execute("UPDATE customers SET address=? WHERE id=?",
                       (address, session["customer_id"]))
        for line in lines:
            db.execute("INSERT INTO order_items (order_id, item_id, item_name, price, qty) "
                       "VALUES (?,?,?,?,?)", (order_id, line["item"]["id"], line["item"]["name"],
                                              line["item"]["price"], line["qty"]))
        db.commit()
        session.pop("cart", None)
        session.pop("cart_shop", None)

        if pay_method not in ("cash", "wallet"):
            return redirect(url_for("pay", order_id=order_id, phone=phone))
        finalize_order(order_id)
        return redirect(url_for("order_status", order_id=order_id, phone=phone))

    me = current_customer()
    return render_template("checkout.html", **ctx,
                           form={"name": me["name"], "phone": me["phone"],
                                 "email": me["email"] or "", "address": me["address"] or ""})


def find_order(order_id, phone):
    order = get_db().execute("SELECT * FROM orders WHERE id=? AND customer_phone=?",
                             (order_id, phone)).fetchone()
    if order is None:
        abort(404)
    return order


# ---------------------------------------------------------------- online payment
@app.route("/pay/<int:order_id>")
def pay(order_id):
    phone = request.args.get("phone", "")
    order = find_order(order_id, phone)
    if order["status"] != AWAITING or order["payment_status"] in ("Paid", "Verifying"):
        return redirect(url_for("order_status", order_id=order_id, phone=phone))
    shop = get_shop(order["shop_id"])

    if order["payment_method"] == "upi":
        link = payments.upi_link(shop["upi_id"], shop["name"], order["amount_due"],
                                 f"Smart Shop order {order_id}")
        return render_template("pay_upi.html", order=order, shop=shop, upi_link=link,
                               qr=payments.qr_svg(link))
    if order["payment_method"] == "demo":
        if not payments.demo_enabled():
            abort(404)
        return render_template("pay_demo.html", order=order, shop=shop)

    try:
        rp_order_id = payments.create_razorpay_order(order["amount_due"], f"order_{order_id}")
    except Exception as exc:
        print(f"[razorpay] {exc}")
        flash("Could not start online payment. Please try again in a moment.")
        return redirect(url_for("order_status", order_id=order_id, phone=phone))
    db = get_db()
    db.execute("UPDATE orders SET rp_order_id=? WHERE id=?", (rp_order_id, order_id))
    db.commit()
    return render_template("pay.html", order=order, shop=shop,
                           rp_order_id=rp_order_id, key_id=payments.keys()[0])


@app.route("/pay/<int:order_id>/upi", methods=["POST"])
def pay_upi_submit(order_id):
    phone = request.form.get("phone", "")
    order = find_order(order_id, phone)
    utr = request.form.get("utr", "").strip().upper()
    if order["payment_method"] != "upi" or order["status"] != AWAITING:
        return redirect(url_for("order_status", order_id=order_id, phone=phone))
    if not (utr.isalnum() and 6 <= len(utr) <= 30):
        flash("Enter the UPI reference / UTR number shown in your UPI app after paying "
              "(usually 12 digits).")
        return redirect(url_for("pay", order_id=order_id, phone=phone))
    db = get_db()
    db.execute("""UPDATE orders SET payment_status='Verifying', payment_ref=?, status='Placed'
                  WHERE id=?""", (utr, order_id))
    db.commit()
    finalize_order(order_id)
    flash("Thanks! The shop will confirm your UPI payment shortly.")
    return redirect(url_for("order_status", order_id=order_id, phone=phone))


@app.route("/pay/<int:order_id>/demo", methods=["POST"])
def pay_demo_submit(order_id):
    phone = request.form.get("phone", "")
    order = find_order(order_id, phone)
    if (order["payment_method"] != "demo" or order["status"] != AWAITING
            or not payments.demo_enabled()):
        abort(400)
    db = get_db()
    if request.form.get("result") == "success":
        ref = "TEST" + datetime.now().strftime("%H%M%S") + str(order_id)
        db.execute("""UPDATE orders SET payment_status='Paid', payment_ref=?, status='Placed'
                      WHERE id=?""", (ref, order_id))
        db.commit()
        finalize_order(order_id)
        flash("Test payment successful! 🎉 (no real money was charged)")
    else:
        db.execute("UPDATE orders SET payment_status='Failed' WHERE id=?", (order_id,))
        db.commit()
        flash("Test payment failed. You can try again.")
    return redirect(url_for("order_status", order_id=order_id, phone=phone))


@app.route("/pay/<int:order_id>/verify", methods=["POST"])
def pay_verify(order_id):
    phone = request.form.get("phone", "")
    order = find_order(order_id, phone)
    rp_payment_id = request.form.get("razorpay_payment_id", "")
    ok = (order["rp_order_id"] == request.form.get("razorpay_order_id")
          and payments.verify_signature(order["rp_order_id"], rp_payment_id,
                                        request.form.get("razorpay_signature")))
    db = get_db()
    if ok and order["payment_status"] != "Paid":
        db.execute("""UPDATE orders SET payment_status='Paid', rp_payment_id=?, payment_ref=?,
                      status='Placed' WHERE id=?""", (rp_payment_id, rp_payment_id, order_id))
        db.commit()
        finalize_order(order_id)
        flash("Payment successful! 🎉")
    elif not ok:
        db.execute("UPDATE orders SET payment_status='Failed' WHERE id=?", (order_id,))
        db.commit()
        flash("Payment could not be verified. Please try again.")
    return redirect(url_for("order_status", order_id=order_id, phone=phone))


@app.route("/order/<int:order_id>")
def order_status(order_id):
    phone = request.args.get("phone", "")
    order = find_order(order_id, phone)
    items = get_db().execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    return render_template("order.html", order=order, items=items,
                           shop=get_shop(order["shop_id"]), awaiting=AWAITING,
                           can_cancel=order["status"] in CUSTOMER_CANCELLABLE,
                           reasons=CANCEL_REASONS)


# ---------------------------------------------------------------- customer: cancel order
@app.route("/order/<int:order_id>/cancel", methods=["POST"])
def customer_cancel(order_id):
    phone = request.form.get("phone", "")
    order = find_order(order_id, phone)
    if order["customer_id"] and order["customer_id"] != session.get("customer_id"):
        flash("Please log in to the account that placed this order.")
        return redirect(url_for("customer_login"))
    back = redirect(url_for("order_status", order_id=order_id, phone=phone))
    if order["status"] not in CUSTOMER_CANCELLABLE:
        flash("This order is already packed or on its way, so it can't be cancelled now. "
              "Please call the shop.")
        return back
    reason = request.form.get("reason") or "No reason given"
    if reason == "Other" and request.form.get("other_reason", "").strip():
        reason = request.form["other_reason"].strip()[:200]
    refund_to = "wallet" if request.form.get("refund_to") == "wallet" else "original"
    note = cancel_order(order, "customer", reason, refund_to)
    get_db().commit()
    shop = get_shop(order["shop_id"])
    notify_customer(order["customer_phone"], order["customer_email"],
                    f"Order #{order_id} cancelled",
                    f"Your order #{order_id} at {shop['name']} is cancelled.{note}")
    notify_owner(shop["owner_email"], f"Order #{order_id} cancelled by customer",
                 f"{order['customer_name']} cancelled order #{order_id}.\nReason: {reason}\n"
                 f"Stock has been added back.{note}")
    flash("Order cancelled." + note)
    return back


# ---------------------------------------------------------------- customer: wallet
TOPUP_MAX = 10000


@app.route("/wallet")
@customer_required
def wallet_page():
    txns = get_db().execute("SELECT * FROM wallet_txns WHERE customer_id=? ORDER BY id DESC",
                            (session["customer_id"],)).fetchall()
    return render_template("wallet.html", txns=txns,
                           can_topup=payments.online_enabled() or payments.demo_enabled(),
                           demo=not payments.online_enabled())


@app.route("/wallet/add", methods=["POST"])
@customer_required
def wallet_topup():
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except ValueError:
        amount = 0
    if not 10 <= amount <= TOPUP_MAX:
        flash(f"Enter an amount between Rs.10 and Rs.{TOPUP_MAX}.")
        return redirect(url_for("wallet_page"))
    me = current_customer()
    if payments.online_enabled():
        try:
            rp_order_id = payments.create_razorpay_order(amount, f"wallet_{me['id']}")
        except Exception as exc:
            print(f"[razorpay] {exc}")
            flash("Could not start payment. Please try again.")
            return redirect(url_for("wallet_page"))
        session["topup"] = {"rp_order_id": rp_order_id, "amount": amount}
        return render_template("wallet_pay.html", amount=amount, rp_order_id=rp_order_id,
                               key_id=payments.keys()[0], me=me)
    if payments.demo_enabled():
        session["topup"] = {"demo": True, "amount": amount}
        return render_template("wallet_pay.html", amount=amount, demo=True, me=me)
    abort(404)


@app.route("/wallet/add/verify", methods=["POST"])
@customer_required
def wallet_topup_verify():
    t = session.pop("topup", None)
    if not t:
        return redirect(url_for("wallet_page"))
    db = get_db()
    if t.get("demo"):
        ok = payments.demo_enabled() and request.form.get("result") == "success"
        ref = "TEST"
    else:
        pid = request.form.get("razorpay_payment_id", "")
        ok = (request.form.get("razorpay_order_id") == t["rp_order_id"] and
              payments.verify_signature(t["rp_order_id"], pid, request.form.get("razorpay_signature")))
        ref = pid
    if ok:
        wallet_add(session["customer_id"], t["amount"], f"Added money ({ref})")
        db.commit()
        flash(f"Rs.{t['amount']:.2f} added to your wallet! 🎉")
    else:
        flash("Payment failed - no money was added.")
    return redirect(url_for("wallet_page"))


# ---------------------------------------------------------------- customer accounts
@app.route("/register", methods=["GET", "POST"])
def customer_register():
    nxt = safe_next(request.args.get("next"))
    if request.method == "POST":
        f = request.form
        name, phone = f.get("name", "").strip(), f.get("phone", "").strip()
        email = f.get("email", "").strip().lower() or None
        password = f.get("password", "")
        errors = []
        if not name or not phone or not password:
            errors.append("Name, phone and password are required.")
        if phone and not (phone.lstrip("+").isdigit() and 10 <= len(phone.lstrip("+")) <= 13):
            errors.append("Enter a valid phone number.")
        if password and len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != f.get("confirm", ""):
            errors.append("Passwords do not match.")
        if not errors:
            try:
                db = get_db()
                cur = db.execute(
                    """INSERT INTO customers (name, email, phone, password_hash, created_at)
                       VALUES (?,?,?,?,?)""",
                    (name, email, phone, generate_password_hash(password),
                     datetime.now().strftime("%Y-%m-%d %H:%M")))
                db.commit()
            except sqlite3.IntegrityError:
                errors.append("An account with this phone or email already exists. Please log in.")
        if errors:
            for e in errors:
                flash(e)
            return render_template("customer_register.html", form=f, next=nxt)
        session["customer_id"] = cur.lastrowid
        flash(f"Welcome, {name}! Your account is ready.")
        return redirect(nxt or url_for("index"))
    return render_template("customer_register.html", form={}, next=nxt)


@app.route("/login", methods=["GET", "POST"])
def customer_login():
    nxt = safe_next(request.args.get("next"))
    if request.method == "POST":
        login_id = request.form.get("login", "").strip().lower()
        me = get_db().execute("SELECT * FROM customers WHERE lower(email)=? OR phone=?",
                              (login_id, login_id)).fetchone()
        if me and check_password_hash(me["password_hash"], request.form.get("password", "")):
            session["customer_id"] = me["id"]
            flash(f"Welcome back, {me['name']}!")
            return redirect(nxt or url_for("index"))
        flash("Wrong email/phone or password.")
    return render_template("customer_login.html", next=nxt)


@app.route("/logout")
def customer_logout():
    session.pop("customer_id", None)
    flash("You have been logged out.")
    return redirect(url_for("index"))


@app.route("/my-orders")
@customer_required
def my_orders():
    db = get_db()
    orders = db.execute(
        """SELECT o.*, s.name AS shop_name FROM orders o JOIN shops s ON s.id = o.shop_id
           WHERE o.customer_id=? ORDER BY o.id DESC""", (session["customer_id"],)).fetchall()
    order_items = {o["id"]: db.execute("SELECT * FROM order_items WHERE order_id=?",
                                       (o["id"],)).fetchall() for o in orders}
    return render_template("my_orders.html", orders=orders, order_items=order_items,
                           awaiting=AWAITING)


# ---------------------------------------------------------------- shop owner side
@app.route("/owner/register", methods=["GET", "POST"])
def owner_register():
    if request.method == "POST":
        f = request.form
        if not all([f.get("name"), f.get("email"), f.get("password"),
                    f.get("open_time"), f.get("close_time")]):
            flash("Please fill all required fields.")
            return render_template("owner_register.html", form=f)
        try:
            db = get_db()
            loc = parse_latlng(f.get("lat"), f.get("lng")) or (None, None)
            upi = f.get("upi_id", "").strip()
            if upi and not payments.valid_upi_id(upi):
                flash("UPI ID looks wrong. It should look like name@okaxis or 9876543210@ybl.")
                return render_template("owner_register.html", form=f)
            cur = db.execute(
                """INSERT INTO shops (name, owner_email, password_hash, address, phone,
                       open_time, close_time, delivery_fee, lat, lng, delivery_radius_km, upi_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f["name"].strip(), f["email"].strip().lower(),
                 generate_password_hash(f["password"]), f.get("address", ""),
                 f.get("phone", ""), f["open_time"], f["close_time"],
                 float(f.get("delivery_fee") or 0), loc[0], loc[1],
                 float(f.get("delivery_radius_km") or 5), upi or None))
            db.commit()
        except sqlite3.IntegrityError:
            flash("That email is already registered.")
            return render_template("owner_register.html", form=f)
        session["shop_id"] = cur.lastrowid
        flash("Shop created! Now add your items.")
        return redirect(url_for("dashboard"))
    return render_template("owner_register.html", form={})


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    if request.method == "POST":
        shop = get_db().execute("SELECT * FROM shops WHERE owner_email=?",
                                (request.form.get("email", "").strip().lower(),)).fetchone()
        if shop and check_password_hash(shop["password_hash"], request.form.get("password", "")):
            session["shop_id"] = shop["id"]
            return redirect(url_for("dashboard"))
        flash("Wrong email or password.")
    return render_template("owner_login.html")


@app.route("/owner/logout")
def owner_logout():
    session.pop("shop_id", None)
    return redirect(url_for("index"))


@app.route("/owner")
@owner_required
def dashboard():
    db = get_db()
    shop = get_shop(session["shop_id"])
    items = db.execute("SELECT * FROM items WHERE shop_id=? ORDER BY name",
                       (shop["id"],)).fetchall()
    orders = db.execute(
        """SELECT * FROM orders WHERE shop_id=? AND status != ?
           ORDER BY CASE WHEN status IN ('Completed','Cancelled') THEN 1 ELSE 0 END,
                    CASE WHEN status IN ('Completed','Cancelled') THEN -id ELSE eta_at END""",
        (shop["id"], AWAITING)).fetchall()
    order_items = {o["id"]: db.execute("SELECT * FROM order_items WHERE order_id=?",
                                       (o["id"],)).fetchall() for o in orders}
    return render_template("dashboard.html", shop=shop, items=items, orders=orders,
                           order_items=order_items, statuses=ORDER_STATUSES,
                           now=datetime.now().strftime("%Y-%m-%d %H:%M"),
                           queue=orders_in_queue(shop["id"]))


@app.route("/owner/settings", methods=["POST"])
@owner_required
def owner_settings():
    f = request.form
    db = get_db()
    loc = parse_latlng(f.get("lat"), f.get("lng")) or (None, None)
    upi = f.get("upi_id", "").strip()
    if upi and not payments.valid_upi_id(upi):
        flash("UPI ID looks wrong. It should look like name@okaxis or 9876543210@ybl.")
        return redirect(url_for("dashboard"))
    db.execute("UPDATE shops SET upi_id=? WHERE id=?", (upi or None, session["shop_id"]))
    db.execute("""UPDATE shops SET name=?, address=?, phone=?, open_time=?, close_time=?,
                  delivery_fee=?, lat=?, lng=?, delivery_radius_km=? WHERE id=?""",
               (f["name"], f.get("address", ""), f.get("phone", ""), f["open_time"],
                f["close_time"], float(f.get("delivery_fee") or 0), loc[0], loc[1],
                float(f.get("delivery_radius_km") or 5), session["shop_id"]))
    db.commit()
    flash("Shop details saved.")
    return redirect(url_for("dashboard"))


@app.route("/owner/item/add", methods=["POST"])
@owner_required
def item_add():
    f = request.form
    db = get_db()
    db.execute("INSERT INTO items (shop_id, name, price, stock, available) VALUES (?,?,?,?,1)",
               (session["shop_id"], f["name"].strip(), float(f["price"]), int(f["stock"])))
    db.commit()
    flash(f"Added {f['name']}.")
    return redirect(url_for("dashboard"))


@app.route("/owner/item/<int:item_id>/update", methods=["POST"])
@owner_required
def item_update(item_id):
    f = request.form
    db = get_db()
    db.execute("""UPDATE items SET name=?, price=?, stock=?, available=?
                  WHERE id=? AND shop_id=?""",
               (f["name"], float(f["price"]), int(f["stock"]),
                1 if f.get("available") else 0, item_id, session["shop_id"]))
    db.commit()
    flash("Item updated.")
    return redirect(url_for("dashboard"))


@app.route("/owner/item/<int:item_id>/delete", methods=["POST"])
@owner_required
def item_delete(item_id):
    db = get_db()
    db.execute("DELETE FROM items WHERE id=? AND shop_id=?", (item_id, session["shop_id"]))
    db.commit()
    flash("Item deleted.")
    return redirect(url_for("dashboard"))


@app.route("/owner/order/<int:order_id>/payment", methods=["POST"])
@owner_required
def order_payment(order_id):
    """Owner confirms (or rejects) a UPI payment after checking their bank/UPI app."""
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=? AND shop_id=?",
                       (order_id, session["shop_id"])).fetchone()
    action = request.form.get("action")
    shop = get_shop(session["shop_id"])
    if order is not None and order["payment_status"] == "Refund due" and action in ("returned", "to_wallet"):
        if action == "to_wallet" and order["customer_id"]:
            wallet_add(order["customer_id"], order["amount_due"],
                       f"Refund from {shop['name']}: order #{order_id}", order_id)
            db.execute("UPDATE orders SET payment_status='Refunded', refund_to='wallet' WHERE id=?",
                       (order_id,))
            msg = f"Rs.{order['amount_due']:.2f} for order #{order_id} was added to your Smart Shop wallet."
        else:
            db.execute("UPDATE orders SET payment_status='Refunded', refund_to='original' WHERE id=?",
                       (order_id,))
            msg = f"{shop['name']} has returned Rs.{order['amount_due']:.2f} for order #{order_id} to your UPI account."
        db.commit()
        notify_customer(order["customer_phone"], order["customer_email"],
                        f"Order #{order_id}: refund", msg)
        flash("Refund recorded.")
        return redirect(url_for("dashboard") + "#orders")
    if order is None or order["payment_status"] != "Verifying":
        return redirect(url_for("dashboard") + "#orders")
    if action == "confirm":
        db.execute("UPDATE orders SET payment_status='Paid' WHERE id=?", (order_id,))
        msg = f"{shop['name']} received your UPI payment of Rs.{order['amount_due']:.2f} for order #{order_id}. Thank you!"
    else:
        db.execute("UPDATE orders SET payment_status='Unpaid', payment_method='cash' WHERE id=?",
                   (order_id,))
        msg = (f"{shop['name']} could not find your UPI payment for order #{order_id} "
               f"(ref {order['payment_ref']}). Please pay by cash on "
               f"{'delivery' if order['mode'] == 'delivery' else 'pickup'}, or call {shop['phone']}.")
    db.commit()
    notify_customer(order["customer_phone"], order["customer_email"],
                    f"Order #{order_id}: payment update", f"{msg} Track: {order_link(order)}")
    return redirect(url_for("dashboard") + "#orders")


STATUS_MESSAGES = {
    "Accepted": "{shop} has accepted your order #{id}.",
    "Packed": "Your order #{id} from {shop} is packed.",
    "Out for delivery": "Your order #{id} from {shop} is out for delivery!",
    "Ready for pickup": "Your order #{id} is packed and ready for pickup at {shop}, {address}.",
    "Completed": "Order #{id} completed. Thank you for shopping at {shop}!",
    "Cancelled": "Sorry, your order #{id} at {shop} was cancelled. Call {phone} for help.",
}


@app.route("/owner/order/<int:order_id>/status", methods=["POST"])
@owner_required
def order_update(order_id):
    status = request.form.get("status")
    if status in ORDER_STATUSES:
        db = get_db()
        order = db.execute("SELECT * FROM orders WHERE id=? AND shop_id=?",
                           (order_id, session["shop_id"])).fetchone()
        if order and order["status"] != status:
            refund_note = ""
            if status == "Cancelled":
                refund_note = cancel_order(order, "shop", request.form.get("reason")
                                           or "Cancelled by shop", "original")
            else:
                db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
            if status == "Completed" and order["payment_method"] == "cash":
                db.execute("UPDATE orders SET payment_status='Paid' WHERE id=?", (order_id,))
            db.commit()
            shop = get_shop(session["shop_id"])
            text = STATUS_MESSAGES.get(status, "Order #{id} status: " + status).format(
                id=order_id, shop=shop["name"], address=shop["address"], phone=shop["phone"])
            notify_customer(order["customer_phone"], order["customer_email"],
                            f"Order #{order_id}: {status}",
                            f"{text}{refund_note} Track: {order_link(order)}")
    return redirect(url_for("dashboard") + "#orders")


# ---------------------------------------------------------------- demo data
def seed_demo():
    """Demo data: shop (demo@shop.com / demo123) and customer (9999999999 / demo123)."""
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""UPDATE shops SET lat=12.9756, lng=77.6050
                      WHERE owner_email='demo@shop.com' AND lat IS NULL""")
        db.execute("""UPDATE shops SET upi_id='sharmastore@okaxis'
                      WHERE owner_email='demo@shop.com' AND upi_id IS NULL""")
        if not db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]:
            db.execute("""INSERT INTO customers (name, email, phone, password_hash, address,
                          created_at) VALUES (?,?,?,?,?,?)""",
                       ("Demo Customer", "customer@demo.com", "9999999999",
                        generate_password_hash("demo123"), "Flat 12, Green Park",
                        datetime.now().strftime("%Y-%m-%d %H:%M")))
        if db.execute("SELECT COUNT(*) FROM shops").fetchone()[0]:
            return
        cur = db.execute(
            """INSERT INTO shops (name, owner_email, password_hash, address, phone,
                   open_time, close_time, delivery_fee, lat, lng, delivery_radius_km)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("Sharma General Store", "demo@shop.com", generate_password_hash("demo123"),
             "12 MG Road", "9876543210", "08:00", "22:00", 25, 12.9756, 77.6050, 5))
        sid = cur.lastrowid
        db.execute("UPDATE shops SET upi_id='sharmastore@okaxis' WHERE id=?", (sid,))
        db.executemany("INSERT INTO items (shop_id, name, price, stock) VALUES (?,?,?,?)",
                       [(sid, "Milk 1L", 56, 40), (sid, "Bread", 45, 25),
                        (sid, "Eggs (12)", 84, 30), (sid, "Rice 5kg", 320, 15),
                        (sid, "Sugar 1kg", 48, 20)])


if __name__ == "__main__":
    init_db()
    seed_demo()
    app.run(debug=True, host="0.0.0.0", port=5000)
