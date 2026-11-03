from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import Flask, flash, g, redirect, render_template, request, send_file, send_from_directory, session, url_for
from PIL import Image, UnidentifiedImageError
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "grandstay-dev-secret-key-change-me"
app.config["TAX_RATE"] = 0.12
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "grandstay.db")
PAYMENT_PROOF_FOLDER = os.path.join(BASE_DIR, "instance", "payment_proofs")
ALLOWED_PAYMENT_PROOF_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_db(query, params=(), one=False):
    rows = get_db().execute(query, params).fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(row) for row in rows]


def get_logged_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query_db("SELECT * FROM users WHERE id = ?", (user_id,), one=True)


@app.context_processor
def inject_user():
    return {"current_user": get_logged_user()}


def login_required(view_func):
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    wrapper.__name__ = view_func.__name__
    return wrapper


def admin_required(view_func):
    def wrapper(*args, **kwargs):
        user = get_logged_user()
        if not user or user["role"] != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    wrapper.__name__ = view_func.__name__
    return wrapper


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def room_is_available(room_id, check_in, check_out, exclude_booking_id=None):
    try:
        start = parse_date(check_in)
        end = parse_date(check_out)
    except ValueError:
        return False

    if end <= start:
        return False

    params = [room_id, end.isoformat(), start.isoformat()]
    query = """
        SELECT COUNT(*) AS count
        FROM bookings
        WHERE room_id = ?
        AND status != 'Cancelled'
        AND check_in < ?
        AND check_out > ?
    """
    if exclude_booking_id is not None:
        query += " AND id != ?"
        params.append(exclude_booking_id)

    count = query_db(query, tuple(params), one=True)
    return (count["count"] if count else 0) == 0


def get_room(room_id):
    return query_db("SELECT * FROM rooms WHERE id = ? AND is_active = 1", (room_id,), one=True)


def get_coupon(code, user_id=None):
    if not code:
        return None
    code = code.strip().upper()
    coupon = query_db(
        "SELECT * FROM coupons WHERE UPPER(code) = UPPER(?) AND is_active = 1",
        (code,),
        one=True,
    )
    if not coupon:
        return None

    today = datetime.today().date().isoformat()
    if coupon["start_date"] > today or coupon["expiry_date"] < today:
        return None

    if user_id is not None:
        usage = query_db(
            "SELECT COUNT(*) AS count FROM bookings WHERE user_id = ? AND coupon_code = ? AND status != 'Cancelled'",
            (user_id, code),
            one=True,
        )
        if usage and usage["count"] >= coupon["per_customer_limit"]:
            return None

    total_usage = query_db(
        "SELECT COUNT(*) AS count FROM bookings WHERE coupon_code = ? AND status != 'Cancelled'",
        (code,),
        one=True,
    )
    if total_usage and total_usage["count"] >= coupon["usage_limit"]:
        return None

    return coupon


def booking_summary(room, check_in, check_out, guests, coupon_code=None, user_id=None):
    if not room:
        return {"error": "Room not found."}

    if not check_in or not check_out:
        return {"error": "Please select both check-in and check-out dates."}

    try:
        start = parse_date(check_in)
        end = parse_date(check_out)
    except ValueError:
        return {"error": "Please choose valid check-in and check-out dates."}

    guests = int(guests or 1)
    if guests < 1:
        return {"error": "Guest count must be at least 1."}
    if guests > int(room["max_guests"]):
        return {"error": f"This room supports a maximum of {room['max_guests']} guests."}

    today = datetime.today().date()
    if start < today:
        return {"error": "Check-in cannot be in the past."}
    if end <= start:
        return {"error": "Check-out must be after check-in."}

    nights = (end - start).days
    room_rate = float(room["price_per_night"])
    subtotal = round(room_rate * nights, 2)
    discount = 0.0
    coupon = None
    coupon_message = ""

    if coupon_code:
        coupon = get_coupon(coupon_code, user_id)
        if coupon is None:
            coupon_message = "This coupon is not valid for your booking."
        else:
            if subtotal < float(coupon["minimum_booking_amount"]):
                coupon_message = "This coupon requires a larger booking total."
            else:
                if coupon["discount_type"] == "percentage":
                    percentage = float(coupon["discount_value"]) / 100
                    discount = subtotal * percentage
                    max_discount = float(coupon["maximum_discount"])
                    if max_discount > 0:
                        discount = min(discount, max_discount)
                else:
                    discount = float(coupon["discount_value"])
                    max_discount = float(coupon["maximum_discount"])
                    if max_discount > 0:
                        discount = min(discount, max_discount)
                discount = round(discount, 2)
                coupon_message = f"Coupon {coupon['code']} applied."

    taxable = round(subtotal - discount, 2)
    taxes = round(taxable * app.config["TAX_RATE"], 2)
    total = round(taxable + taxes, 2)

    return {
        "nights": nights,
        "room_rate": room_rate,
        "subtotal": round(subtotal, 2),
        "discount": round(discount, 2),
        "taxes": taxes,
        "taxable": taxable,
        "total": total,
        "coupon": coupon,
        "coupon_message": coupon_message,
        "check_in": start.isoformat(),
        "check_out": end.isoformat(),
        "guests": guests,
    }


def create_booking_reference():
    return f"GS-{datetime.now().strftime('%d%m%y')}-{uuid.uuid4().hex[:6].upper()}"


def seed_database():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone TEXT,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'customer',
            city TEXT,
            country TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_number INTEGER NOT NULL,
            room_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            price_per_night REAL NOT NULL,
            max_guests INTEGER NOT NULL,
            bed_type TEXT,
            room_size TEXT,
            floor INTEGER,
            amenities TEXT,
            image_url TEXT,
            rating REAL DEFAULT 4.5,
            housekeeping_status TEXT DEFAULT 'Clean',
            maintenance_status TEXT DEFAULT 'Good',
            is_active INTEGER DEFAULT 1
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_reference TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            room_id INTEGER NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT NOT NULL,
            guests INTEGER NOT NULL,
            nights INTEGER NOT NULL,
            room_rate REAL NOT NULL,
            subtotal REAL NOT NULL,
            discount REAL DEFAULT 0,
            taxes REAL DEFAULT 0,
            total_amount REAL NOT NULL,
            coupon_code TEXT,
            status TEXT NOT NULL,
            payment_status TEXT NOT NULL,
            guest_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            cancellation_reason TEXT
        )
        """
    )
    booking_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(bookings)").fetchall()
    }
    if "guest_name" not in booking_columns:
        db.execute("ALTER TABLE bookings ADD COLUMN guest_name TEXT")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            amount_due REAL NOT NULL,
            amount_paid REAL NOT NULL,
            payment_status TEXT NOT NULL,
            transaction_reference TEXT,
            payment_timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            refund_status TEXT DEFAULT 'N/A',
            notes TEXT
        )
        """
    )
    payment_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(payments)").fetchall()
    }
    if "proof_image" not in payment_columns:
        db.execute("ALTER TABLE payments ADD COLUMN proof_image TEXT")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            room_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            discount_type TEXT NOT NULL,
            discount_value REAL NOT NULL,
            minimum_booking_amount REAL DEFAULT 0,
            maximum_discount REAL DEFAULT 0,
            start_date TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            usage_limit INTEGER DEFAULT 10,
            per_customer_limit INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            subject TEXT,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    if not db.execute("SELECT 1 FROM users WHERE email = ?", ("admin@grandstay.com",)).fetchone():
        db.execute(
            "INSERT INTO users (name, email, phone, password_hash, role, city, country) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "GrandStay Admin",
                "admin@grandstay.com",
                "+91 99999 12345",
                generate_password_hash("admin123"),
                "admin",
                "Mumbai",
                "India",
            ),
        )

    if not db.execute("SELECT 1 FROM users WHERE email = ?", ("guest@grandstay.com",)).fetchone():
        db.execute(
            "INSERT INTO users (name, email, phone, password_hash, city, country) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "Aarav Kapoor",
                "guest@grandstay.com",
                "+91 98765 43210",
                generate_password_hash("guest123"),
                "Bengaluru",
                "India",
            ),
        )

    if db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
        rooms = [
            (101, "Standard", "Urban Haven Room", "A calm and contemporary room with plush bedding and a warm city view.", 2500, 2, "Queen Bed", "320 sq ft", 1, "Wi‑Fi, Air Conditioner, Smart TV, Workspace, Coffee Maker, Daily Housekeeping", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 4.6, "Clean", "Good", 1),
            (102, "Standard", "City Light Retreat", "Enjoy a peaceful stay with restful linens, a work desk, and elegant decor.", 2500, 2, "Queen Bed", "320 sq ft", 1, "Wi‑Fi, Air Conditioner, Breakfast Included, Desk, Wardrobe, Housekeeping", "https://images.unsplash.com/photo-1566665797739-1674de7a421a?auto=format&fit=crop&w=900&q=80", 4.5, "Clean", "Good", 1),
            (103, "Standard", "Sundown Standard", "A comfortable standard room with a soothing palette and practical luxury touches.", 2500, 2, "King Bed", "345 sq ft", 1, "Wi‑Fi, Air Conditioner, Bath Essentials, Tea Station, Flat Screen TV", "https://images.unsplash.com/photo-1520250497591-112f2f40a3f4?auto=format&fit=crop&w=900&q=80", 4.4, "Ready", "Good", 1),
            (104, "Standard", "Atelier Stay", "Minimalist comforts, efficient layout, and premium bedding for business or leisure travel.", 2500, 2, "Queen Bed", "310 sq ft", 1, "Wi‑Fi, Air Conditioner, Coffee Maker, Workspace, Premium Linen", "https://images.unsplash.com/photo-1494526585095-c41746248156?auto=format&fit=crop&w=900&q=80", 4.5, "Clean", "Minor Check", 1),
            (105, "Standard", "Harbor Calm", "A simple, elegant room crafted for rest, productivity, and comfort.", 2500, 2, "Double Bed", "330 sq ft", 1, "Wi‑Fi, Air Conditioner, Safe Deposit Box, LED TV, Mini Fridge", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 4.3, "Clean", "Good", 1),
            (201, "Deluxe", "Golden Courtyard Deluxe", "A spacious deluxe room with a rich contemporary style and indulgent room comforts.", 4000, 3, "King Bed", "420 sq ft", 2, "Wi‑Fi, Air Conditioner, Rain Shower, Minibar, Balcony, Smart TV", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 4.7, "Clean", "Good", 1),
            (202, "Deluxe", "Amber Horizon", "Wake up to warm natural tones and enjoy a well-appointed deluxe stay.", 4000, 3, "Queen Bed", "440 sq ft", 2, "Wi‑Fi, Air Conditioner, In-room Dining, Work Desk, Lounge Chair", "https://images.unsplash.com/photo-1551882547-ff40c63fe5fa?auto=format&fit=crop&w=900&q=80", 4.8, "Clean", "Good", 1),
            (203, "Deluxe", "Imperial Calm", "Layered textures and refined finishes create a comfortable and elevated retreat.", 4000, 3, "King Bed", "450 sq ft", 2, "Wi‑Fi, Air Conditioner, Premium Bath Amenities, Sofa, Balcony", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 4.7, "Ready", "Good", 1),
            (204, "Deluxe", "Riverlight Suite Deluxe", "Soft light, premium linen, and thoughtful details define this refined stay.", 4000, 3, "Queen Bed", "430 sq ft", 2, "Wi‑Fi, Air Conditioner, Coffee Bar, Bathtub, Smart TV, Balcony", "https://images.unsplash.com/photo-1522708323590-d24dbb6b0267?auto=format&fit=crop&w=900&q=80", 4.6, "Clean", "Good", 1),
            (205, "Deluxe", "Rosewood Luxe", "An elegant sleeping experience combined with modern amenities and a scenic feel.", 4000, 3, "King Bed", "460 sq ft", 2, "Wi‑Fi, Air Conditioner, Shower Tub, Dining Table, Lounge Seating", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 4.8, "Clean", "Good", 1),
            (301, "Suite", "GrandStay Signature Suite", "An expansive suite with boutique elegance, comfort, and a premium hospitality experience.", 6500, 4, "King Bed", "620 sq ft", 3, "Wi‑Fi, Air Conditioner, Executive Workspace, Lounge Seating, Large TV, City View", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 5.0, "Ready", "Good", 1),
            (302, "Suite", "The Palm Residency", "Designed for extended stays with signature textures and spacious living areas.", 6500, 4, "King Bed", "640 sq ft", 3, "Wi‑Fi, Air Conditioner, Dining Area, Lounge, Marble Bath, Balcony", "https://images.unsplash.com/photo-1566665797739-1674de7a421a?auto=format&fit=crop&w=900&q=80", 4.9, "Clean", "Good", 1),
            (303, "Suite", "Velvet Crown Suite", "This suite combines quiet luxury, elevated design, and panoramic comfort.", 6500, 4, "King Bed", "680 sq ft", 3, "Wi‑Fi, Air Conditioner, Fireplace, Workspace, Bathtub, Butler Service", "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80", 5.0, "Ready", "Good", 1),
            (304, "Suite", "The Residency Oval", "A refined suite with elegant furnishings, smooth finishes, and ample light.", 6500, 4, "Queen Bed", "660 sq ft", 3, "Wi‑Fi, Air Conditioner, Dining Nook, Lounge, Rain Shower, Terrace", "https://images.unsplash.com/photo-1522708323590-d24dbb6b0267?auto=format&fit=crop&w=900&q=80", 4.9, "Clean", "Maintenance", 1),
            (305, "Suite", "Monarch Horizon Suite", "A top-tier suite curated for indulgent stays with luxury services and room-to-room comfort.", 6500, 4, "King Bed", "700 sq ft", 3, "Wi‑Fi, Air Conditioner, Deluxe Bath, Lounge Bay, Private Dining, View Balcony", "https://images.unsplash.com/photo-1551882547-ff40c63fe5fa?auto=format&fit=crop&w=900&q=80", 5.0, "Ready", "Good", 1),
        ]

        db.executemany(
            """
            INSERT INTO rooms (
                room_number, room_type, title, description, price_per_night, max_guests,
                bed_type, room_size, floor, amenities, image_url, rating,
                housekeeping_status, maintenance_status, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rooms,
        )

    if db.execute("SELECT COUNT(*) FROM coupons").fetchone()[0] == 0:
        coupons = [
            ("WELCOME10", "percentage", 10, 0, 1000, "2025-01-01", "2035-12-31", 20, 2, 1),
            ("GRANDSTAY15", "percentage", 15, 0, 1500, "2025-01-01", "2035-12-31", 30, 3, 1),
        ]
        db.executemany(
            "INSERT INTO coupons (code, discount_type, discount_value, minimum_booking_amount, maximum_discount, start_date, expiry_date, usage_limit, per_customer_limit, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            coupons,
        )

    db.commit()


def initialise_app():
    with app.app_context():
        seed_database()


initialise_app()


@app.route("/")
def home():
    rooms = query_db("SELECT * FROM rooms WHERE is_active = 1 ORDER BY price_per_night LIMIT 3")
    reviews = query_db(
        "SELECT r.*, u.name, ro.title AS room_title FROM reviews r JOIN users u ON u.id = r.user_id JOIN rooms ro ON ro.id = r.room_id ORDER BY r.id DESC LIMIT 3"
    )
    return render_template("home.html", rooms=rooms, reviews=reviews)


@app.route("/rooms")
def rooms():
    filters = {
        "room_type": request.args.get("room_type", ""),
        "min_price": request.args.get("min_price", ""),
        "max_price": request.args.get("max_price", ""),
        "guest_capacity": request.args.get("guest_capacity", ""),
        "sort_by": request.args.get("sort_by", "price_low_high"),
    }

    query = "SELECT * FROM rooms WHERE is_active = 1"
    params = []

    if filters["room_type"]:
        query += " AND room_type = ?"
        params.append(filters["room_type"])
    if filters["min_price"]:
        query += " AND price_per_night >= ?"
        params.append(float(filters["min_price"]))
    if filters["max_price"]:
        query += " AND price_per_night <= ?"
        params.append(float(filters["max_price"]))
    if filters["guest_capacity"]:
        query += " AND max_guests >= ?"
        params.append(int(filters["guest_capacity"]))

    if filters["sort_by"] == "price_low_high":
        query += " ORDER BY price_per_night ASC"
    elif filters["sort_by"] == "price_high_low":
        query += " ORDER BY price_per_night DESC"
    elif filters["sort_by"] == "category":
        query += " ORDER BY room_type ASC, price_per_night ASC"
    else:
        query += " ORDER BY rating DESC, price_per_night ASC"

    room_results = query_db(query, tuple(params))
    return render_template("rooms.html", rooms=room_results, filters=filters)


@app.route("/search")
def search():
    return rooms()


@app.route("/room/<int:room_id>")
def room_detail(room_id):
    room = get_room(room_id)
    if not room:
        return render_template("404.html"), 404

    reviews = query_db(
        "SELECT r.*, u.name FROM reviews r JOIN users u ON u.id = r.user_id WHERE r.room_id = ? ORDER BY r.id DESC LIMIT 5",
        (room_id,),
    )
    return render_template("room_detail.html", room=room, reviews=reviews)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        password = request.form.get("password") or ""
        city = (request.form.get("city") or "").strip()
        country = (request.form.get("country") or "").strip()

        if not name or not email or not password:
            flash("Name, email and password are required.", "error")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "error")
            return render_template("register.html")

        if query_db("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
            flash("This email is already registered.", "error")
            return render_template("register.html")

        db = get_db()
        cursor = db.execute(
            "INSERT INTO users (name, email, phone, password_hash, city, country, role) VALUES (?, ?, ?, ?, ?, ?, 'customer')",
            (name, email, phone, generate_password_hash(password), city, country),
        )
        db.commit()
        session["user_id"] = cursor.lastrowid
        session["user_name"] = name
        flash("Registration successful.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = query_db("SELECT * FROM users WHERE email = ?", (email,), one=True)

        if user and check_password_hash(user["password_hash"], password):
            if user["role"] == "admin":
                flash("Use the admin portal to sign in.", "error")
                return redirect(url_for("admin_login"))
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash("Welcome back!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = query_db("SELECT * FROM users WHERE email = ?", (email,), one=True)

        if user and user["role"] == "admin" and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash("Welcome admin!", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin credentials.", "error")

    return render_template("admin_login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@login_required
def dashboard():
    user = get_logged_user()
    bookings = query_db(
        "SELECT b.*, r.title, r.room_number, r.room_type FROM bookings b JOIN rooms r ON r.id = b.room_id WHERE b.user_id = ? ORDER BY b.id DESC LIMIT 5",
        (user["id"],),
    )
    total_bookings = query_db("SELECT COUNT(*) AS count FROM bookings WHERE user_id = ?", (user["id"],), one=True)["count"]
    upcoming = query_db(
        "SELECT COUNT(*) AS count FROM bookings WHERE user_id = ? AND check_in >= DATE('now') AND status != 'Cancelled'",
        (user["id"],),
        one=True,
    )["count"]
    return render_template("dashboard.html", user=user, bookings=bookings, total_bookings=total_bookings, upcoming=upcoming)


app.add_url_rule("/dashboard", view_func=dashboard)


@login_required
@app.route("/profile", methods=["GET", "POST"])
def profile():
    user = get_logged_user()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        city = (request.form.get("city") or "").strip()
        country = (request.form.get("country") or "").strip()

        get_db().execute(
            "UPDATE users SET name = ?, phone = ?, city = ?, country = ? WHERE id = ?",
            (name, phone, city, country, user["id"]),
        )
        get_db().commit()
        session["user_name"] = name
        flash("Your profile has been updated.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


@login_required
@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    user = get_logged_user()
    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not check_password_hash(user["password_hash"], current_password):
            flash("Current password is incorrect.", "error")
            return render_template("change_password.html")
        if len(new_password) < 6:
            flash("New password must be at least 6 characters long.", "error")
            return render_template("change_password.html")
        if new_password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("change_password.html")

        get_db().execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user["id"]),
        )
        get_db().commit()
        flash("Your password has been changed successfully.", "success")
        return redirect(url_for("change_password"))

    return render_template("change_password.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        message = (request.form.get("message") or "").strip()

        if not name or not email or not message:
            flash("Name, email and message are required.", "error")
            return render_template("contact.html")

        get_db().execute(
            "INSERT INTO contact_messages (name, email, phone, subject, message) VALUES (?, ?, ?, ?, ?)",
            (name, email, phone, subject, message),
        )
        get_db().commit()
        flash("Thank you for contacting GrandStay. Our team will get back to you soon.", "success")
        return redirect(url_for("contact"))

    return render_template("contact.html")


@app.route("/reviews", methods=["GET", "POST"])
@login_required
def reviews():
    if request.method == "POST":
        room_id = int(request.form.get("room_id") or 0)
        rating = int(request.form.get("rating") or 5)
        comment = (request.form.get("comment") or "").strip()

        if not room_id:
            flash("Please select a room before submitting a review.", "error")
            return redirect(url_for("reviews"))
        if rating < 1 or rating > 5:
            flash("Rating must be between 1 and 5.", "error")
            return redirect(url_for("reviews"))

        get_db().execute(
            "INSERT INTO reviews (user_id, room_id, rating, comment) VALUES (?, ?, ?, ?)",
            (get_logged_user()["id"], room_id, rating, comment),
        )
        get_db().commit()
        flash("Your review has been submitted.", "success")
        return redirect(url_for("reviews"))

    room_options = query_db("SELECT * FROM rooms WHERE is_active = 1 ORDER BY room_number asc")
    all_reviews = query_db(
        "SELECT r.*, u.name, ro.title AS room_title FROM reviews r JOIN users u ON u.id = r.user_id JOIN rooms ro ON ro.id = r.room_id ORDER BY r.id DESC"
    )
    return render_template("reviews.html", room_options=room_options, reviews=all_reviews)


@app.route("/book/<int:room_id>", methods=["GET", "POST"])
@login_required
def book_room(room_id):
    room = get_room(room_id)
    if not room:
        return render_template("404.html"), 404

    if request.method == "POST":
        guest_name = (request.form.get("guest_name") or "").strip()
        check_in = request.form.get("check_in")
        check_out = request.form.get("check_out")
        guests = request.form.get("guests", 1)
        coupon_code = request.form.get("coupon_code", "")

        if not guest_name:
            flash("Please enter the guest name for this reservation.", "error")
            return render_template(
                "book_room.html",
                room=room,
                summary=None,
                form_data={"guest_name": guest_name, "check_in": check_in, "check_out": check_out, "guests": guests, "coupon_code": coupon_code},
            )
        if len(guest_name) > 100:
            flash("Guest name must be 100 characters or fewer.", "error")
            return render_template(
                "book_room.html",
                room=room,
                summary=None,
                form_data={"guest_name": guest_name, "check_in": check_in, "check_out": check_out, "guests": guests, "coupon_code": coupon_code},
            )

        summary = booking_summary(room, check_in, check_out, guests, coupon_code, get_logged_user()["id"])
        if "error" in summary:
            flash(summary["error"], "error")
            return render_template("book_room.html", room=room, summary=None, form_data={"guest_name": guest_name, "check_in": check_in, "check_out": check_out, "guests": guests, "coupon_code": coupon_code})

        if not room_is_available(room_id, check_in, check_out):
            flash("Selected dates are not available for this room. Please choose another date range.", "error")
            return render_template("book_room.html", room=room, summary=None, form_data={"guest_name": guest_name, "check_in": check_in, "check_out": check_out, "guests": guests, "coupon_code": coupon_code})

        return redirect(
            url_for(
                "checkout",
                room_id=room_id,
                check_in=check_in,
                check_out=check_out,
                guests=guests,
                coupon_code=coupon_code,
                guest_name=guest_name,
            )
        )

    return render_template("book_room.html", room=room, summary=None, form_data={"guest_name": get_logged_user()["name"]})


@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    room_id = request.args.get("room_id") or request.form.get("room_id")
    check_in = request.args.get("check_in") or request.form.get("check_in")
    check_out = request.args.get("check_out") or request.form.get("check_out")
    guests = request.args.get("guests") or request.form.get("guests") or 1
    coupon_code = request.args.get("coupon_code") or request.form.get("coupon_code") or ""
    guest_name = (request.args.get("guest_name") or request.form.get("guest_name") or get_logged_user()["name"]).strip()

    if not room_id:
        flash("Please select a room first.", "error")
        return redirect(url_for("rooms"))

    if not guest_name or len(guest_name) > 100:
        flash("Please provide a valid guest name (100 characters or fewer).", "error")
        return redirect(url_for("book_room", room_id=room_id))

    room = get_room(int(room_id))
    if not room:
        return render_template("404.html"), 404

    summary = booking_summary(room, check_in, check_out, guests, coupon_code, get_logged_user()["id"])
    if "error" in summary:
        flash(summary["error"], "error")
        return redirect(url_for("book_room", room_id=room_id))

    if not room_is_available(room_id, check_in, check_out):
        flash("Selected room is no longer available.", "error")
        return redirect(url_for("book_room", room_id=room_id))

    if request.method == "POST":
        payment_method = request.form.get("payment_method", "")
        payment_methods = ["Cash at Hotel", "UPI", "Debit Card", "Credit Card", "Net Banking", "Bank Transfer"]
        if payment_method not in payment_methods:
            flash("Please select a valid payment method.", "error")
            return redirect(url_for("checkout", room_id=room_id, check_in=check_in, check_out=check_out, guests=guests, coupon_code=coupon_code, guest_name=guest_name))

        proof_filename = None
        if payment_method == "UPI":
            proof_upload = request.files.get("payment_proof")
            if not proof_upload or not proof_upload.filename:
                flash("Upload a screenshot of your UPI payment to submit your booking for verification.", "error")
                return redirect(url_for("checkout", room_id=room_id, check_in=check_in, check_out=check_out, guests=guests, coupon_code=coupon_code, guest_name=guest_name))

            extension = os.path.splitext(proof_upload.filename)[1].lower()
            if extension not in ALLOWED_PAYMENT_PROOF_EXTENSIONS:
                flash("Payment proof must be a JPG, PNG, or WEBP image.", "error")
                return redirect(url_for("checkout", room_id=room_id, check_in=check_in, check_out=check_out, guests=guests, coupon_code=coupon_code, guest_name=guest_name))

            try:
                proof_image = Image.open(proof_upload.stream)
                expected_formats = {
                    ".jpg": "JPEG",
                    ".jpeg": "JPEG",
                    ".png": "PNG",
                    ".webp": "WEBP",
                }
                if proof_image.format != expected_formats[extension]:
                    raise ValueError("Image extension does not match its file format.")
                proof_image.verify()
                proof_upload.stream.seek(0)
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                flash("The payment screenshot is not a valid image. Please upload a clear JPG, PNG, or WEBP screenshot.", "error")
                return redirect(url_for("checkout", room_id=room_id, check_in=check_in, check_out=check_out, guests=guests, coupon_code=coupon_code, guest_name=guest_name))

            proof_filename = f"{uuid.uuid4().hex}{extension}"
            try:
                os.makedirs(PAYMENT_PROOF_FOLDER, exist_ok=True)
                proof_upload.save(os.path.join(PAYMENT_PROOF_FOLDER, proof_filename))
            except OSError:
                flash("The payment screenshot could not be saved. Please try again.", "error")
                return redirect(url_for("checkout", room_id=room_id, check_in=check_in, check_out=check_out, guests=guests, coupon_code=coupon_code, guest_name=guest_name))

        db = get_db()
        booking_reference = create_booking_reference()
        is_upi_payment = payment_method == "UPI"
        is_cash_payment = payment_method == "Cash at Hotel"
        booking_status = "Pending Verification" if is_upi_payment else ("Pending Confirmation" if is_cash_payment else "Confirmed")
        payment_status = "Pending Verification" if is_upi_payment else ("Pending" if is_cash_payment else "Paid")
        amount_paid = summary["total"] if not is_cash_payment and not is_upi_payment else 0
        amount_due = summary["total"] if is_cash_payment or is_upi_payment else 0

        try:
            db.execute("BEGIN IMMEDIATE")
            if not room_is_available(room_id, check_in, check_out):
                db.rollback()
                if proof_filename:
                    try:
                        os.remove(os.path.join(PAYMENT_PROOF_FOLDER, proof_filename))
                    except OSError:
                        app.logger.exception("Could not remove an unlinked payment proof image.")
                flash("Selected room is no longer available. Please choose another date range.", "error")
                return redirect(url_for("book_room", room_id=room_id))

            cursor = db.execute(
                """
                INSERT INTO bookings (
                    booking_reference, user_id, room_id, check_in, check_out, guests,
                    nights, room_rate, subtotal, discount, taxes, total_amount,
                    coupon_code, status, payment_status, guest_name, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_reference,
                    get_logged_user()["id"],
                    room["id"],
                    summary["check_in"],
                    summary["check_out"],
                    summary["guests"],
                    summary["nights"],
                    room["price_per_night"],
                    summary["subtotal"],
                    summary["discount"],
                    summary["taxes"],
                    summary["total"],
                    coupon_code.upper() if coupon_code else None,
                    booking_status,
                    payment_status,
                    guest_name,
                    f"Booking created via {payment_method} payment method.",
                ),
            )
            booking_id = cursor.lastrowid
            transaction_reference = f"TEST-{uuid.uuid4().hex[:10].upper()}"
            payment_notes = "Test payment simulation. No real payment gateway is used."
            if is_cash_payment:
                transaction_reference = "CASH-PENDING"
            elif is_upi_payment:
                transaction_reference = "CUSTOMER-PROOF-UPLOADED"
                payment_notes = "UPI payment screenshot submitted; awaiting admin verification against the booking total."
            db.execute(
                """
                INSERT INTO payments (
                    booking_id, payment_method, amount_due, amount_paid, payment_status,
                    transaction_reference, notes, proof_image
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_id,
                    payment_method,
                    amount_due,
                    amount_paid,
                    payment_status,
                    transaction_reference,
                    payment_notes,
                    proof_filename,
                ),
            )
            db.commit()
            if is_upi_payment:
                flash("Your booking and UPI payment screenshot were submitted. An admin will verify the amount before confirming your booking.", "success")
            else:
                flash("Booking confirmed successfully!", "success")
            return redirect(url_for("confirmation", booking_id=booking_id))
        except sqlite3.Error:
            db.rollback()
            if proof_filename:
                try:
                    os.remove(os.path.join(PAYMENT_PROOF_FOLDER, proof_filename))
                except OSError:
                    app.logger.exception("Could not remove an unlinked payment proof image.")
            flash("Booking could not be completed. Please try again.", "error")
            return redirect(url_for("book_room", room_id=room_id))

    return render_template("checkout.html", room=room, summary=summary, payment_methods=["Cash at Hotel", "UPI", "Debit Card", "Credit Card", "Net Banking", "Bank Transfer"])


@app.route("/confirmation/<int:booking_id>")
@login_required
def confirmation(booking_id):
    booking = query_db(
        "SELECT b.*, COALESCE(NULLIF(b.guest_name, ''), u.name) AS guest_name, r.title, r.room_number, r.room_type FROM bookings b JOIN rooms r ON r.id = b.room_id JOIN users u ON u.id = b.user_id WHERE b.id = ? AND b.user_id = ?",
        (booking_id, get_logged_user()["id"]),
        one=True,
    )
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("my_bookings"))

    payment = query_db("SELECT * FROM payments WHERE booking_id = ?", (booking_id,), one=True)
    return render_template("confirmation.html", booking=booking, payment=payment)


@app.route("/my-bookings")
@login_required
def my_bookings():
    bookings = query_db(
        "SELECT b.*, r.title, r.room_number, r.room_type FROM bookings b JOIN rooms r ON r.id = b.room_id WHERE b.user_id = ? ORDER BY b.id DESC",
        (get_logged_user()["id"],),
    )
    return render_template("my_bookings.html", bookings=bookings)


@app.route("/booking/<int:booking_id>")
@login_required
def booking_detail(booking_id):
    booking = query_db(
        "SELECT b.*, COALESCE(NULLIF(b.guest_name, ''), u.name) AS guest_name, r.title, r.room_number, r.room_type FROM bookings b JOIN rooms r ON r.id = b.room_id JOIN users u ON u.id = b.user_id WHERE b.id = ? AND b.user_id = ?",
        (booking_id, get_logged_user()["id"]),
        one=True,
    )
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("my_bookings"))

    payment = query_db("SELECT * FROM payments WHERE booking_id = ?", (booking_id,), one=True)
    return render_template("booking_detail.html", booking=booking, payment=payment)


@app.route("/invoice/<int:booking_id>")
@login_required
def invoice(booking_id):
    booking = query_db(
        "SELECT b.*, COALESCE(NULLIF(b.guest_name, ''), u.name) AS customer_name, r.title, r.room_number, r.room_type, u.email AS customer_email, u.phone AS customer_phone FROM bookings b JOIN rooms r ON r.id = b.room_id JOIN users u ON u.id = b.user_id WHERE b.id = ? AND b.user_id = ?",
        (booking_id, get_logged_user()["id"]),
        one=True,
    )
    if not booking:
        flash("Invoice not found.", "error")
        return redirect(url_for("my_bookings"))

    payment = query_db("SELECT * FROM payments WHERE booking_id = ?", (booking_id,), one=True)
    return render_template("invoice.html", booking=booking, payment=payment)


@app.route("/download_invoice/<int:booking_id>")
@login_required
def download_invoice(booking_id):
    booking = query_db(
        "SELECT b.*, COALESCE(NULLIF(b.guest_name, ''), u.name) AS customer_name, r.title, r.room_number, r.room_type, u.email AS customer_email, u.phone AS customer_phone FROM bookings b JOIN rooms r ON r.id = b.room_id JOIN users u ON u.id = b.user_id WHERE b.id = ? AND b.user_id = ?",
        (booking_id, get_logged_user()["id"]),
        one=True,
    )
    if not booking:
        flash("Invoice not found.", "error")
        return redirect(url_for("my_bookings"))

    payment = query_db("SELECT * FROM payments WHERE booking_id = ?", (booking_id,), one=True)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setTitle(f"GrandStay Invoice {booking['booking_reference']}")
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(50, 750, "GrandStay Hotel Invoice")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(50, 730, f"Booking Reference: {booking['booking_reference']}")
    pdf.drawString(50, 715, f"Customer: {booking['customer_name']} ({booking['customer_email']})")
    pdf.drawString(50, 700, f"Room: {booking['room_type']} - #{booking['room_number']} {booking['title']}")
    pdf.drawString(50, 685, f"Stay: {booking['check_in']} to {booking['check_out']} ({booking['nights']} nights)")
    pdf.drawString(50, 670, f"Payment Method: {payment['payment_method'] if payment else 'N/A'}")
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(50, 625, "Charges")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(50, 610, f"Room subtotal: ₹{booking['subtotal']:.2f}")
    pdf.drawString(50, 595, f"Discount: -₹{booking['discount']:.2f}")
    pdf.drawString(50, 580, f"Taxes: ₹{booking['taxes']:.2f}")
    pdf.drawString(50, 565, f"Total: ₹{booking['total_amount']:.2f}")
    pdf.drawString(50, 550, f"Amount Paid: ₹{payment['amount_paid'] if payment else 0:.2f}")
    pdf.drawString(50, 535, f"Balance Due: ₹{payment['amount_due'] if payment else 0:.2f}")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(50, 500, "This is a simulated invoice generated for the project demo.")
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{booking['booking_reference']}.pdf", mimetype="application/pdf")


@app.route("/cancel_booking/<int:booking_id>", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    booking = query_db(
        "SELECT * FROM bookings WHERE id = ? AND user_id = ?",
        (booking_id, get_logged_user()["id"]),
        one=True,
    )
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("my_bookings"))

    if booking["status"] in ("Cancelled", "Checked-out"):
        flash("This booking cannot be cancelled.", "error")
        return redirect(url_for("booking_detail", booking_id=booking_id))

    db = get_db()
    db.execute(
        "UPDATE bookings SET status = 'Cancelled', cancellation_reason = ? WHERE id = ?",
        ("Cancelled by guest via the customer portal.", booking_id),
    )
    if booking["payment_status"] == "Pending Verification":
        db.execute(
            """
            UPDATE payments
            SET payment_status = 'Cancelled', amount_due = 0,
                notes = 'Booking cancelled by customer before UPI proof verification.'
            WHERE booking_id = ? AND payment_status = 'Pending Verification'
            """,
            (booking_id,),
        )
        db.execute(
            "UPDATE bookings SET payment_status = 'Cancelled' WHERE id = ?",
            (booking_id,),
        )
    db.commit()
    flash("Booking cancelled successfully.", "success")
    return redirect(url_for("my_bookings"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    room_count = query_db("SELECT COUNT(*) AS count FROM rooms", one=True)["count"]
    booking_count = query_db("SELECT COUNT(*) AS count FROM bookings", one=True)["count"]
    revenue = query_db("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bookings WHERE payment_status = 'Paid'", one=True)["total"]
    rooms = query_db("SELECT * FROM rooms ORDER BY room_number ASC")
    coupons = query_db("SELECT * FROM coupons ORDER BY id DESC")
    pending_payments = query_db(
        """
        SELECT p.id AS payment_id, p.proof_image, p.payment_timestamp,
               b.id AS booking_id, b.booking_reference, b.guest_name,
               b.check_in, b.check_out, b.nights, b.room_rate, b.subtotal,
               b.discount, b.taxes, b.total_amount,
               r.room_number, r.room_type, r.title AS room_title,
               u.name AS account_name, u.email AS customer_email
        FROM payments p
        JOIN bookings b ON b.id = p.booking_id
        JOIN rooms r ON r.id = b.room_id
        JOIN users u ON u.id = b.user_id
        WHERE p.payment_method = 'UPI'
          AND p.payment_status = 'Pending Verification'
          AND b.status = 'Pending Verification'
          AND p.proof_image IS NOT NULL
        ORDER BY p.payment_timestamp ASC
        """
    )
    for payment in pending_payments:
        if not payment["guest_name"]:
            payment["guest_name"] = payment["account_name"]
    return render_template("admin.html", room_count=room_count, booking_count=booking_count, revenue=revenue, rooms=rooms, coupons=coupons, pending_payments=pending_payments)


@app.route("/admin/payment-proof/<int:payment_id>")
@admin_required
def view_payment_proof(payment_id):
    payment = query_db(
        "SELECT proof_image FROM payments WHERE id = ? AND payment_method = 'UPI'",
        (payment_id,),
        one=True,
    )
    if not payment or not payment["proof_image"] or os.path.basename(payment["proof_image"]) != payment["proof_image"]:
        return render_template("404.html"), 404
    return send_from_directory(PAYMENT_PROOF_FOLDER, payment["proof_image"], as_attachment=False)


@app.route("/admin/payment/<int:payment_id>/review", methods=["POST"])
@admin_required
def review_upi_payment(payment_id):
    payment = query_db(
        """
        SELECT p.id, p.payment_status, p.amount_due, b.id AS booking_id,
               b.total_amount, b.booking_reference, b.status AS booking_status
        FROM payments p
        JOIN bookings b ON b.id = p.booking_id
        WHERE p.id = ? AND p.payment_method = 'UPI'
        """,
        (payment_id,),
        one=True,
    )
    if (
        not payment
        or payment["payment_status"] != "Pending Verification"
        or payment["booking_status"] != "Pending Verification"
    ):
        flash("This UPI payment is no longer awaiting verification.", "error")
        return redirect(url_for("admin_dashboard"))

    decision = request.form.get("decision")
    if decision not in ("approve", "reject"):
        flash("Select a valid payment verification action.", "error")
        return redirect(url_for("admin_dashboard"))

    received_amount = Decimal("0.00")
    if decision == "approve":
        try:
            received_amount = Decimal(request.form.get("received_amount", "").strip()).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            flash("Enter the amount shown in the payment screenshot.", "error")
            return redirect(url_for("admin_dashboard"))

        expected_amount = Decimal(str(payment["total_amount"])).quantize(Decimal("0.01"))
        if not received_amount.is_finite() or received_amount != expected_amount:
            flash(f"Amount mismatch. The booking total is ₹{expected_amount:,.2f}; the payment cannot be approved.", "error")
            return redirect(url_for("admin_dashboard"))

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        current_payment = db.execute(
            """
            SELECT p.payment_status, b.status AS booking_status
            FROM payments p
            JOIN bookings b ON b.id = p.booking_id
            WHERE p.id = ?
            """,
            (payment_id,),
        ).fetchone()
        if (
            not current_payment
            or current_payment["payment_status"] != "Pending Verification"
            or current_payment["booking_status"] != "Pending Verification"
        ):
            db.rollback()
            flash("This payment has already been reviewed.", "error")
            return redirect(url_for("admin_dashboard"))

        if decision == "approve":
            db.execute(
                """
                UPDATE payments
                SET payment_status = 'Paid', amount_paid = ?, amount_due = 0,
                    payment_timestamp = CURRENT_TIMESTAMP,
                    notes = 'UPI payment screenshot manually verified against the booking total.'
                WHERE id = ?
                """,
                (float(received_amount), payment_id),
            )
            db.execute(
                "UPDATE bookings SET payment_status = 'Paid', status = 'Confirmed' WHERE id = ?",
                (payment["booking_id"],),
            )
            db.commit()
            flash("UPI payment verified for the exact booking total. Booking confirmed.", "success")
        else:
            db.execute(
                """
                UPDATE payments
                SET payment_status = 'Rejected', amount_paid = 0,
                    notes = 'UPI payment proof rejected by admin; customer should contact the hotel.'
                WHERE id = ?
                """,
                (payment_id,),
            )
            db.execute(
                """
                UPDATE bookings
                SET payment_status = 'Rejected', status = 'Cancelled',
                    cancellation_reason = 'UPI payment proof rejected by admin.'
                WHERE id = ?
                """,
                (payment["booking_id"],),
            )
            db.commit()
            flash("Payment proof rejected and the unpaid booking was cancelled.", "success")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Could not update UPI payment verification state.")
        flash("Payment review could not be saved. Please try again.", "error")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/room/<int:room_id>/price", methods=["POST"])
@admin_required
def update_room_price(room_id):
    price = float(request.form.get("price_per_night", 0))
    if price <= 0:
        flash("Invalid room price.", "error")
        return redirect(url_for("admin_dashboard"))

    get_db().execute("UPDATE rooms SET price_per_night = ? WHERE id = ?", (price, room_id))
    get_db().commit()
    flash("Room price updated successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/coupon", methods=["POST"])
@admin_required
def add_coupon():
    code = (request.form.get("code") or "").strip().upper()
    discount_type = request.form.get("discount_type") or "percentage"
    discount_value = float(request.form.get("discount_value") or 0)
    minimum_booking_amount = float(request.form.get("minimum_booking_amount") or 0)
    maximum_discount = float(request.form.get("maximum_discount") or 0)
    start_date = request.form.get("start_date") or datetime.today().date().isoformat()
    expiry_date = request.form.get("expiry_date") or (datetime.today().date() + timedelta(days=365)).isoformat()
    usage_limit = int(request.form.get("usage_limit") or 10)
    per_customer_limit = int(request.form.get("per_customer_limit") or 1)

    if not code or discount_value <= 0:
        flash("Coupon code and discount value are required.", "error")
        return redirect(url_for("admin_dashboard"))

    get_db().execute(
        """
        INSERT INTO coupons (code, discount_type, discount_value, minimum_booking_amount, maximum_discount,
        start_date, expiry_date, usage_limit, per_customer_limit, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (code, discount_type, discount_value, minimum_booking_amount, maximum_discount, start_date, expiry_date, usage_limit, per_customer_limit),
    )
    get_db().commit()
    flash("Coupon created successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/coupon/<int:coupon_id>/toggle", methods=["POST"])
@admin_required
def toggle_coupon(coupon_id):
    coupon = query_db("SELECT * FROM coupons WHERE id = ?", (coupon_id,), one=True)
    if not coupon:
        flash("Coupon not found.", "error")
        return redirect(url_for("admin_dashboard"))

    new_status = 0 if coupon["is_active"] else 1
    get_db().execute("UPDATE coupons SET is_active = ? WHERE id = ?", (new_status, coupon_id))
    get_db().commit()
    flash("Coupon status updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
