import importlib.util
import json
import os
import smtplib
import uuid
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import create_map_poster as poster

# Load environment variables
load_dotenv()

# Validate required environment variables
def validate_env():
    """Validate required environment variables for production"""
    required_vars = {
        "SECRET_KEY": "Flask secret key for sessions",
    }

    # Only require Stripe keys if Stripe will be used
    if os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_PRICE_ID"):
        required_vars["STRIPE_SECRET_KEY"] = "Stripe API secret key"
        required_vars["STRIPE_PRICE_ID"] = "Stripe price ID for poster product"
        required_vars["STRIPE_WEBHOOK_SECRET"] = "Stripe webhook signing secret"

    missing = []
    for var, description in required_vars.items():
        if not os.getenv(var):
            missing.append(f"{var} ({description})")

    if missing:
        print("ERROR: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        print("\nCreate a .env file with these variables. See .env.example for template.")
        exit(1)

    # Validate numeric values
    try:
        price_cents = int(os.getenv("POSTER_PRICE_CENTS", "2900"))
        if price_cents <= 0:
            raise ValueError("Price must be positive")
    except ValueError as e:
        print(f"ERROR: POSTER_PRICE_CENTS must be a valid positive integer: {e}")
        exit(1)

    try:
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        if smtp_port <= 0 or smtp_port > 65535:
            raise ValueError("Port must be between 1 and 65535")
    except ValueError as e:
        print(f"ERROR: SMTP_PORT must be a valid port number: {e}")
        exit(1)

# Call validation in production mode
if os.getenv("FLASK_ENV") != "development":
    validate_env()

BASE_DIR = Path(__file__).resolve().parent
INVOICES_DIR = BASE_DIR / poster.POSTERS_DIR / "invoices"
ORDERS_DIR = BASE_DIR / poster.POSTERS_DIR / "orders"
PREVIEWS_DIR = BASE_DIR / poster.POSTERS_DIR / "previews"

INVOICES_DIR.mkdir(parents=True, exist_ok=True)
ORDERS_DIR.mkdir(parents=True, exist_ok=True)
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

PRICE_CENTS = int(os.getenv("POSTER_PRICE_CENTS", "2900"))
PRICE_CURRENCY = os.getenv("POSTER_PRICE_CURRENCY", "usd")

# Input validation constants
ALLOWED_DISTANCES = range(3000, 40001)  # 3km to 40km
ALLOWED_DPI = [150, 240, 300]

stripe = None
if importlib.util.find_spec("stripe"):
    import stripe  # type: ignore[no-redef]

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")

SIZE_OPTIONS = {
    # Portrait (Vertical)
    "8x10": (8, 10),
    "12x16": (12, 16),
    "18x24": (18, 24),
    "24x36": (24, 36),
    # Landscape (Horizontal)
    "10x8": (10, 8),
    "16x12": (16, 12),
    "24x18": (24, 18),
    "36x24": (36, 24),
}

EXAMPLE_POSTERS = [
    {
        "filename": "new_york_noir_20260108_164217.png",
        "city": "New York",
        "theme": "Noir",
        "size": "18x24",
    },
    {
        "filename": "barcelona_warm_beige_20260108_172924.png",
        "city": "Barcelona",
        "theme": "Warm Beige",
        "size": "12x16",
    },
    {
        "filename": "tokyo_japanese_ink_20260108_165830.png",
        "city": "Tokyo",
        "theme": "Japanese Ink",
        "size": "18x24",
    },
    {
        "filename": "venice_blueprint_20260108_165527.png",
        "city": "Venice",
        "theme": "Blueprint",
        "size": "12x16",
    },
    {
        "filename": "san_francisco_sunset_20260108_184122.png",
        "city": "San Francisco",
        "theme": "Sunset",
        "size": "24x36",
    },
    {
        "filename": "singapore_neon_cyberpunk_20260108_184503.png",
        "city": "Singapore",
        "theme": "Neon Cyberpunk",
        "size": "18x24",
    },
]
EXAMPLE_POSTER_FILES = {example["filename"] for example in EXAMPLE_POSTERS}


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
    coordinates: tuple[float, float] | None = None  # Store coordinates for theme switching


app = Flask(__name__)

# Configure Flask app security
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") != "development"  # HTTPS only in production
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Initialize rate limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",  # Use Redis in production for persistence
)


def stripe_ready() -> bool:
    return bool(stripe and stripe.api_key and STRIPE_PRICE_ID)


def render_index(error: str | None = None):
    return render_template(
        "index.html",
        themes=poster.AVAILABLE_THEMES,
        size_options=SIZE_OPTIONS.keys(),
        stripe_ready=stripe_ready(),
        price_cents=PRICE_CENTS,
        price_currency=PRICE_CURRENCY.upper(),
        examples=EXAMPLE_POSTERS,
        error=error,
    )


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
    return render_index()


@app.get("/examples/<path:filename>")
def example_poster(filename: str):
    if filename not in EXAMPLE_POSTER_FILES:
        abort(404)
    poster_path = BASE_DIR / poster.POSTERS_DIR / filename
    if not poster_path.exists():
        abort(404)
    return send_file(poster_path, mimetype="image/png")


@app.post("/create")
@limiter.limit("20 per hour")  # Lenient rate limit as requested
def create():
    city = request.form.get("city", "").strip()
    country = request.form.get("country", "").strip()
    theme = request.form.get("theme", "feature_based")

    # Validate distance with proper error handling
    try:
        distance = int(request.form.get("distance", "29000"))
        if distance not in ALLOWED_DISTANCES:
            return render_index(error="Distance must be between 3,000 and 40,000 meters.")
    except ValueError:
        return render_index(error="Invalid distance value.")

    # Validate DPI
    try:
        dpi = int(request.form.get("dpi", "300"))
        if dpi not in ALLOWED_DPI:
            return render_index(error="DPI must be 150, 240, or 300.")
    except ValueError:
        return render_index(error="Invalid DPI value.")

    size = request.form.get("size", "12x16")
    email = request.form.get("email", "").strip() or None

    # Validate email format if provided
    if email:
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            return render_index(error="Invalid email address format.")

    if not city or not country:
        return render_index(error="City and country are required.")

    if size not in SIZE_OPTIONS:
        return render_index(error="Unsupported size selection.")

    # Validate theme
    if theme not in poster.AVAILABLE_THEMES:
        return render_index(error="Invalid theme selected.")

    coords = poster.get_coordinates(city, country)
    if coords is None:
        return render_index(
            error="We could not find that city. Please double-check the spelling."
        )

    # Create preview with watermark
    preview_id = uuid.uuid4().hex
    preview_filename = f"preview_{preview_id}_{theme}.png"
    preview_path = PREVIEWS_DIR / preview_filename

    # Generate lower-res preview (faster, smaller file)
    preview_dpi = 150
    poster.THEME = poster.load_theme(theme)
    poster.create_poster(
        city=city,
        country=country,
        point=coords,
        dist=distance,
        output_file=str(preview_path),
        figsize=SIZE_OPTIONS[size],
        dpi=preview_dpi,
        watermark=True,
    )

    invoice_id = preview_id
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
        poster_filename="",  # Will be generated after purchase
        invoice_filename=invoice_filename,
        created_at=datetime.now(timezone.utc).isoformat(),
        paid=False,
        coordinates=coords,
    )
    save_order(order)
    build_invoice(order)

    # Redirect to preview page
    return redirect(url_for("preview", session_id=order.session_id))


@app.get("/success")
def success():
    # Use order_id instead of session_id (preserves our internal UUID)
    order_id = request.args.get("order_id")
    if not order_id:
        abort(400)

    try:
        order = load_order(order_id)
    except FileNotFoundError:
        abort(404)

    # Webhook already generated poster and marked as paid
    if not order.paid:
        # Webhook hasn't processed yet - show processing message
        return render_template("processing.html", order=order)

    # If no poster filename, it means payment wasn't through Stripe (dev mode)
    if not order.poster_filename:
        # Generate poster for non-Stripe orders
        if order.coordinates is None:
            coords = poster.get_coordinates(order.city, order.country)
        else:
            coords = order.coordinates

        poster_filename = (
            f"{order.city.lower().replace(' ', '_')}_{order.theme}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        poster_path = BASE_DIR / poster.POSTERS_DIR / poster_filename

        poster.THEME = poster.load_theme(order.theme)
        poster.create_poster(
            city=order.city,
            country=order.country,
            point=coords,
            dist=order.distance,
            output_file=str(poster_path),
            figsize=SIZE_OPTIONS[order.size],
            dpi=order.dpi,
            watermark=False,
        )

        order.poster_filename = poster_filename
        order.paid = True
        save_order(order)

    email_sent = False
    if order.email and not os.getenv("EMAIL_SENT_FLAG"):
        email_sent = send_email(order)

    return render_template(
        "result.html",
        order=order,
        email_sent=email_sent,
        stripe_ready=stripe_ready(),
    )


@app.get("/preview/<session_id>")
def preview(session_id: str):
    try:
        order = load_order(session_id)
    except FileNotFoundError:
        abort(404)

    return render_template(
        "preview.html",
        order=order,
        themes=poster.AVAILABLE_THEMES,
        stripe_ready=stripe_ready(),
        price_cents=PRICE_CENTS,
        price_currency=PRICE_CURRENCY.upper(),
    )


@app.get("/preview-image/<session_id>/<theme_name>")
@limiter.limit("50 per hour")  # Higher limit for theme switching
def preview_image(session_id: str, theme_name: str):
    try:
        order = load_order(session_id)
    except FileNotFoundError:
        abort(404)

    if theme_name not in poster.AVAILABLE_THEMES:
        abort(400)

    # Check if preview already exists for this theme
    preview_filename = f"preview_{session_id}_{theme_name}.png"
    preview_path = PREVIEWS_DIR / preview_filename

    if not preview_path.exists():
        # Generate preview for this theme
        if order.coordinates is None:
            # Fallback: get coordinates again
            coords = poster.get_coordinates(order.city, order.country)
        else:
            coords = order.coordinates

        poster.THEME = poster.load_theme(theme_name)
        poster.create_poster(
            city=order.city,
            country=order.country,
            point=coords,
            dist=order.distance,
            output_file=str(preview_path),
            figsize=SIZE_OPTIONS[order.size],
            dpi=150,  # Lower DPI for previews
            watermark=True,
        )

    return send_file(preview_path, mimetype="image/png")


@app.post("/purchase/<session_id>")
@limiter.limit("10 per hour")  # Prevent checkout spam
def purchase(session_id: str):
    try:
        order = load_order(session_id)
    except FileNotFoundError:
        abort(404)

    # Update theme if user changed it
    selected_theme = request.form.get("theme", order.theme)
    if selected_theme != order.theme:
        order.theme = selected_theme
        save_order(order)

    if stripe_ready():
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=url_for("success", _external=True) + f"?order_id={order.session_id}",
            cancel_url=url_for("cancel", _external=True) + f"?order_id={order.session_id}",
            metadata={"order_id": order.session_id},  # Preserve internal UUID
        )
        # DON'T overwrite session_id - keep internal UUID for order tracking
        return redirect(session.url)

    # No payment needed - generate final poster
    return redirect(url_for("generate_final", session_id=order.session_id))


@app.get("/generate-final/<session_id>")
def generate_final(session_id: str):
    try:
        order = load_order(session_id)
    except FileNotFoundError:
        abort(404)

    # Generate final high-res poster without watermark
    if order.coordinates is None:
        coords = poster.get_coordinates(order.city, order.country)
    else:
        coords = order.coordinates

    poster_filename = (
        f"{order.city.lower().replace(' ', '_')}_{order.theme}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    )
    poster_path = BASE_DIR / poster.POSTERS_DIR / poster_filename

    poster.THEME = poster.load_theme(order.theme)
    poster.create_poster(
        city=order.city,
        country=order.country,
        point=coords,
        dist=order.distance,
        output_file=str(poster_path),
        figsize=SIZE_OPTIONS[order.size],
        dpi=order.dpi,
        watermark=False,  # No watermark for purchased version
    )

    order.poster_filename = poster_filename
    order.paid = True
    save_order(order)

    email_sent = send_email(order) if order.email else False
    return render_template(
        "result.html",
        order=order,
        email_sent=email_sent,
        stripe_ready=stripe_ready(),
    )


@app.post("/webhook/stripe")
def stripe_webhook():
    """Handle Stripe webhook events for payment confirmation"""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        print("ERROR: STRIPE_WEBHOOK_SECRET not configured")
        abort(500, "Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        # Invalid payload
        print("ERROR: Invalid webhook payload")
        abort(400)
    except stripe.error.SignatureVerificationError:
        # Invalid signature
        print("ERROR: Invalid webhook signature")
        abort(400)

    # Handle checkout.session.completed
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        # Get our internal order ID from metadata
        order_id = session["metadata"].get("order_id")
        if not order_id:
            print(f"Warning: No order_id in Stripe session {session['id']}")
            return {"status": "ignored"}, 200

        try:
            order = load_order(order_id)
        except FileNotFoundError:
            print(f"Warning: Order {order_id} not found for Stripe session {session['id']}")
            return {"status": "ignored"}, 200

        # Mark as paid and generate final poster
        if not order.paid:
            print(f"Processing payment for order {order_id}")
            order.paid = True

            # Generate final high-res poster without watermark
            if order.coordinates is None:
                coords = poster.get_coordinates(order.city, order.country)
            else:
                coords = order.coordinates

            poster_filename = (
                f"{order.city.lower().replace(' ', '_')}_{order.theme}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
            poster_path = BASE_DIR / poster.POSTERS_DIR / poster_filename

            poster.THEME = poster.load_theme(order.theme)
            poster.create_poster(
                city=order.city,
                country=order.country,
                point=coords,
                dist=order.distance,
                output_file=str(poster_path),
                figsize=SIZE_OPTIONS[order.size],
                dpi=order.dpi,
                watermark=False,  # No watermark for paid version
            )

            order.poster_filename = poster_filename
            save_order(order)

            # Send email if provided
            if order.email:
                try:
                    send_email(order)
                    print(f"Email sent to {order.email} for order {order_id}")
                except Exception as e:
                    print(f"Failed to send email for order {order_id}: {e}")

            print(f"Order {order_id} completed successfully")

    return {"status": "success"}, 200


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
    # Development mode check
    is_dev = os.getenv("FLASK_ENV", "production") == "development"

    if is_dev:
        print("Running in DEVELOPMENT mode")
        app.run(debug=True, host="127.0.0.1", port=8000)
    else:
        print("ERROR: Do not run app.py directly in production!")
        print("Use: gunicorn -w 4 -b 0.0.0.0:8000 app:app")
        print("Or deploy to a platform like Railway/Heroku with Procfile")
        exit(1)
