import importlib.util
import json
import os
import smtplib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

import create_map_poster as poster

BASE_DIR = Path(__file__).resolve().parent
INVOICES_DIR = BASE_DIR / poster.POSTERS_DIR / "invoices"
ORDERS_DIR = BASE_DIR / poster.POSTERS_DIR / "orders"

INVOICES_DIR.mkdir(parents=True, exist_ok=True)
ORDERS_DIR.mkdir(parents=True, exist_ok=True)

PRICE_CENTS = int(os.getenv("POSTER_PRICE_CENTS", "2900"))
PRICE_CURRENCY = os.getenv("POSTER_PRICE_CURRENCY", "usd")

stripe = None
if importlib.util.find_spec("stripe"):
    import stripe  # type: ignore[no-redef]

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")

SIZE_OPTIONS = {
    "8x10": (8, 10),
    "12x16": (12, 16),
    "18x24": (18, 24),
    "24x36": (24, 36),
}


@dataclass
class Order:
    session_id: str
    city: str
    country: str
    theme: str
    distance: int
    size: str
    dpi: int
    email: str | None
    poster_filename: str
    invoice_filename: str
    created_at: str
    paid: bool = False


app = Flask(__name__)


def stripe_ready() -> bool:
    return bool(stripe and stripe.api_key and STRIPE_PRICE_ID)


def save_order(order: Order) -> None:
    order_path = ORDERS_DIR / f"{order.session_id}.json"
    order_path.write_text(json.dumps(asdict(order), indent=2), encoding="utf-8")


def load_order(session_id: str) -> Order:
    order_path = ORDERS_DIR / f"{session_id}.json"
    if not order_path.exists():
        raise FileNotFoundError
    data = json.loads(order_path.read_text(encoding="utf-8"))
    return Order(**data)


def build_invoice(order: Order) -> None:
    invoice_path = INVOICES_DIR / order.invoice_filename
    invoice_data = {
        "invoice_id": order.invoice_filename.replace(".json", ""),
        "created_at": order.created_at,
        "city": order.city,
        "country": order.country,
        "theme": order.theme,
        "distance_meters": order.distance,
        "size": order.size,
        "dpi": order.dpi,
        "price_cents": PRICE_CENTS,
        "currency": PRICE_CURRENCY,
        "email": order.email or "",
    }
    invoice_path.write_text(json.dumps(invoice_data, indent=2), encoding="utf-8")


def send_email(order: Order) -> bool:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    from_email = os.getenv("FROM_EMAIL")

    if not all([smtp_host, smtp_user, smtp_pass, from_email, order.email]):
        return False

    poster_path = BASE_DIR / poster.POSTERS_DIR / order.poster_filename
    invoice_path = INVOICES_DIR / order.invoice_filename
    if not poster_path.exists() or not invoice_path.exists():
        return False

    message = EmailMessage()
    message["Subject"] = f"Your map poster for {order.city}"
    message["From"] = from_email
    message["To"] = order.email
    message.set_content(
        f"Thanks for your order! Your poster and invoice are attached.\n\n"
        f"City: {order.city}\nTheme: {order.theme}\nSize: {order.size}\n"
    )

    message.add_attachment(
        poster_path.read_bytes(),
        maintype="image",
        subtype="png",
        filename=order.poster_filename,
    )
    message.add_attachment(
        invoice_path.read_bytes(),
        maintype="application",
        subtype="json",
        filename=order.invoice_filename,
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(message)
    return True


@app.get("/")
def index():
    return render_template(
        "index.html",
        themes=poster.AVAILABLE_THEMES,
        size_options=SIZE_OPTIONS.keys(),
        stripe_ready=stripe_ready(),
        price_cents=PRICE_CENTS,
        price_currency=PRICE_CURRENCY.upper(),
    )


@app.post("/create")
def create():
    city = request.form.get("city", "").strip()
    country = request.form.get("country", "").strip()
    theme = request.form.get("theme", "feature_based")
    distance = int(request.form.get("distance", "29000"))
    size = request.form.get("size", "12x16")
    dpi = int(request.form.get("dpi", "300"))
    email = request.form.get("email", "").strip() or None

    if not city or not country:
        return render_template(
            "index.html",
            themes=poster.AVAILABLE_THEMES,
            size_options=SIZE_OPTIONS.keys(),
            stripe_ready=stripe_ready(),
            price_cents=PRICE_CENTS,
            price_currency=PRICE_CURRENCY.upper(),
            error="City and country are required.",
        )

    if size not in SIZE_OPTIONS:
        return render_template(
            "index.html",
            themes=poster.AVAILABLE_THEMES,
            size_options=SIZE_OPTIONS.keys(),
            stripe_ready=stripe_ready(),
            price_cents=PRICE_CENTS,
            price_currency=PRICE_CURRENCY.upper(),
            error="Unsupported size selection.",
        )

    coords = poster.get_coordinates(city, country)
    if coords is None:
        return render_template(
            "index.html",
            themes=poster.AVAILABLE_THEMES,
            size_options=SIZE_OPTIONS.keys(),
            stripe_ready=stripe_ready(),
            price_cents=PRICE_CENTS,
            price_currency=PRICE_CURRENCY.upper(),
            error="We could not find that city. Please double-check the spelling.",
        )

    poster_filename = (
        f"{city.lower().replace(' ', '_')}_{theme}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    )
    poster_path = BASE_DIR / poster.POSTERS_DIR / poster_filename
    poster.THEME = poster.load_theme(theme)
    poster.create_poster(
        city=city,
        country=country,
        point=coords,
        dist=distance,
        output_file=str(poster_path),
        figsize=SIZE_OPTIONS[size],
        dpi=dpi,
    )

    invoice_id = uuid.uuid4().hex
    invoice_filename = f"{invoice_id}.json"
    order = Order(
        session_id=invoice_id,
        city=city,
        country=country,
        theme=theme,
        distance=distance,
        size=size,
        dpi=dpi,
        email=email,
        poster_filename=poster_filename,
        invoice_filename=invoice_filename,
        created_at=datetime.now(timezone.utc).isoformat(),
        paid=not stripe_ready(),
    )
    save_order(order)
    build_invoice(order)

    if stripe_ready():
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=url_for("success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("cancel", _external=True) + f"?session_id={order.session_id}",
            metadata={"order_id": order.session_id},
        )
        order.session_id = session.id
        save_order(order)
        return redirect(session.url)

    email_sent = send_email(order) if email else False
    return render_template(
        "result.html",
        order=order,
        email_sent=email_sent,
        stripe_ready=stripe_ready(),
    )


@app.get("/success")
def success():
    session_id = request.args.get("session_id")
    if not session_id:
        abort(400)
    try:
        order = load_order(session_id)
    except FileNotFoundError:
        abort(404)

    if stripe_ready():
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            abort(403)
        order.paid = True
        save_order(order)

    email_sent = send_email(order) if order.email else False
    return render_template(
        "result.html",
        order=order,
        email_sent=email_sent,
        stripe_ready=stripe_ready(),
    )


@app.get("/cancel")
def cancel():
    session_id = request.args.get("session_id")
    return render_template("cancel.html", session_id=session_id)


@app.get("/download/<session_id>/<path:filename>")
def download(session_id: str, filename: str):
    try:
        order = load_order(session_id)
    except FileNotFoundError:
        abort(404)

    if not order.paid:
        abort(403)

    poster_path = BASE_DIR / poster.POSTERS_DIR / order.poster_filename
    invoice_path = INVOICES_DIR / order.invoice_filename
    if filename == order.poster_filename and poster_path.exists():
        return send_file(poster_path, as_attachment=True)
    if filename == order.invoice_filename and invoice_path.exists():
        return send_file(invoice_path, as_attachment=True)
    abort(404)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
