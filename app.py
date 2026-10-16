from datetime import datetime, timedelta
import csv
from io import BytesIO, StringIO
import math
import os
import re
import sqlite3
import webbrowser
from contextlib import closing
from threading import Timer

from flask import Flask, abort, flash, g, make_response, redirect, render_template, request, url_for
from PIL import Image, ImageDraw, ImageFont


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.environ.get("LEDGER_DATABASE", os.path.join(BASE_DIR, "ledger.db"))
PORT = int(os.environ.get("LEDGER_PORT", "51743"))
HOST = os.environ.get("LEDGER_HOST", "0.0.0.0")

CATEGORIES = [
    "Vegetables", "Dairy products", "Fruits", "Meat & seafood", "Poultry products", "Bakery",
    "Groceries", "Snacks & junk food", "Dining out", "Beverages",
    "Household supplies", "Personal care", "Clothing", "Healthcare",
    "Transport", "Fuel", "Housing & rent", "Utilities & bills",
    "Subscriptions", "Education", "Entertainment", "Travel", "Gifts & donations",
    "Investment", "Fees & charges", "Taxes", "Other",
]
SPENDERS = ["Maa", "Papa", "Sonal", "On hand cash"]
QUANTITY_UNITS = ["g", "kg", "piece(s)", "pack(s)", "ml", "L", "set(s)", "item", "none"]
MRP_UNITS = [unit for unit in QUANTITY_UNITS if unit != "none"]
SPENDER_COLUMNS = {"Maa": "maa", "Papa": "papa", "Sonal": "sonal", "On hand cash": "on_hand_cash"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-this-secret")
app.config["DATABASE"] = DATABASE


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with closing(sqlite3.connect(app.config["DATABASE"])) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 1 CHECK (quantity > 0),
                quantity_unit TEXT NOT NULL DEFAULT 'item',
                mrp_cents INTEGER CHECK (mrp_cents IS NULL OR mrp_cents >= 0),
                mrp_unit TEXT,
                amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                category TEXT NOT NULL,
                spent_by TEXT NOT NULL DEFAULT 'Not specified',
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS monthly_budgets (
                month TEXT PRIMARY KEY,
                maa INTEGER NOT NULL DEFAULT 0 CHECK (maa >= 0),
                papa INTEGER NOT NULL DEFAULT 0 CHECK (papa >= 0),
                sonal INTEGER NOT NULL DEFAULT 0 CHECK (sonal >= 0),
                on_hand_cash INTEGER NOT NULL DEFAULT 0 CHECK (on_hand_cash >= 0),
                maa_savings INTEGER NOT NULL DEFAULT 0 CHECK (maa_savings >= 0),
                papa_savings INTEGER NOT NULL DEFAULT 0 CHECK (papa_savings >= 0),
                sonal_savings INTEGER NOT NULL DEFAULT 0 CHECK (sonal_savings >= 0),
                on_hand_cash_savings INTEGER NOT NULL DEFAULT 0 CHECK (on_hand_cash_savings >= 0),
                updated_at TEXT NOT NULL,
                savings_updated_at TEXT NOT NULL DEFAULT ''
            )
        """)
        budget_columns = {row[1] for row in db.execute("PRAGMA table_info(monthly_budgets)")}
        for column in ("maa_savings", "papa_savings", "sonal_savings", "on_hand_cash_savings"):
            if column not in budget_columns:
                db.execute(f"ALTER TABLE monthly_budgets ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0 CHECK ({column} >= 0)")
        if "savings_updated_at" not in budget_columns:
            db.execute("ALTER TABLE monthly_budgets ADD COLUMN savings_updated_at TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
        if "spent_by" not in columns:
            db.execute("ALTER TABLE entries ADD COLUMN spent_by TEXT NOT NULL DEFAULT 'Not specified'")
        if "quantity" not in columns:
            db.execute("ALTER TABLE entries ADD COLUMN quantity REAL NOT NULL DEFAULT 1 CHECK (quantity > 0)")
        if "quantity_unit" not in columns:
            db.execute("ALTER TABLE entries ADD COLUMN quantity_unit TEXT NOT NULL DEFAULT 'item'")
        if "mrp_cents" not in columns:
            db.execute("ALTER TABLE entries ADD COLUMN mrp_cents INTEGER CHECK (mrp_cents IS NULL OR mrp_cents >= 0)")
        if "mrp_unit" not in columns:
            db.execute("ALTER TABLE entries ADD COLUMN mrp_unit TEXT")
        db.commit()


def parse_amount(value):
    try:
        amount = round(float(value) * 100)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid amount.")
    if amount < 0 or amount > 999999999999:
        raise ValueError("Amount must be between 0 and 9,999,999,999.99.")
    return amount


def entry_form_values():
    product = request.form.get("product", "").strip()
    category = request.form.get("category", "")
    spent_by = request.form.get("spent_by", "")
    quantity_unit = request.form.get("quantity_unit", "")
    mrp_input = request.form.get("mrp", "").strip()
    mrp_unit = request.form.get("mrp_unit", "")
    if not product:
        raise ValueError("Product or expense name is required.")
    if len(product) > 120:
        raise ValueError("Keep the product name under 120 characters.")
    if category not in CATEGORIES:
        raise ValueError("Choose a category from the list.")
    if spent_by not in SPENDERS and spent_by != "Not specified":
        raise ValueError("Choose who paid for this expense.")
    try:
        quantity = float(request.form.get("quantity", ""))
    except (TypeError, ValueError):
        raise ValueError("Enter a valid quantity.")
    if not math.isfinite(quantity) or quantity <= 0 or quantity > 999999999:
        raise ValueError("Quantity must be greater than 0 and under 1,000,000,000.")
    quantity = round(quantity, 3)
    if quantity <= 0:
        raise ValueError("Quantity must be at least 0.001.")
    if quantity_unit not in QUANTITY_UNITS:
        raise ValueError("Choose a quantity unit.")
    mrp_cents = parse_amount(mrp_input) if mrp_input else None
    if mrp_cents is not None and mrp_unit not in MRP_UNITS:
        raise ValueError("Choose the unit the MRP is priced against.")
    if mrp_cents is None:
        mrp_unit = None
    return product, parse_amount(request.form.get("amount")), category, spent_by, quantity, quantity_unit, mrp_cents, mrp_unit


@app.template_filter("money")
def money(cents):
    return f"₹{cents / 100:,.2f}"


def receipt_font(size, bold=False):
    regular_path = next((path for path in (
        "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf",
    ) if os.path.exists(path)), None)
    bold_path = next((path for path in (
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    ) if os.path.exists(path)), regular_path)
    font_path = bold_path if bold else regular_path
    return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()


def filtered_entries():
    """Build one parameterized filter used by the ledger and CSV export."""
    conditions = []
    values = []
    requested_categories = request.args.getlist("category")
    categories = [category for category in CATEGORIES if category in requested_categories]
    search = request.args.get("q", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if categories:
        conditions.append("category IN (" + ", ".join("?" for _ in categories) + ")")
        values.extend(categories)
    if search:
        conditions.append("product LIKE ? ESCAPE '\\'")
        escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values.append(f"%{escaped_search}%")
    for value, operator, label in ((start_date, ">=", "start"), (end_date, "<=", "end")):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"Enter a valid {label} date.")
            # Preserve the date as recorded in the computer's local timezone.
            conditions.append(f"substr(created_at, 1, 10) {operator} ?")
            values.append(value)
    if start_date and end_date and start_date > end_date:
        raise ValueError("The start date must be on or before the end date.")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = get_db().execute(
        "SELECT * FROM entries" + where + " ORDER BY created_at DESC, id DESC", values
    ).fetchall()
    return rows, categories, search, start_date, end_date


@app.get("/")
def index():
    db = get_db()
    local_now = datetime.now().astimezone()
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    daily_totals = db.execute(
        "SELECT substr(created_at, 1, 10) AS entry_date, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM entries WHERE substr(created_at, 1, 10) IN (?, ?) GROUP BY substr(created_at, 1, 10)",
        (today.isoformat(), yesterday.isoformat()),
    ).fetchall()
    daily_totals = {row["entry_date"]: row["total"] for row in daily_totals}
    current_month = local_now.strftime("%Y-%m")
    budget_month = request.args.get("budget_month", current_month)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", budget_month):
        budget_month = current_month
    try:
        entries, selected_categories, search, start_date, end_date = filtered_entries()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    visible_total = sum(entry["amount_cents"] for entry in entries)
    category_totals = {}
    for entry in entries:
        current = category_totals.setdefault(entry["category"], {"category": entry["category"], "total": 0, "count": 0})
        current["total"] += entry["amount_cents"]
        current["count"] += 1
    category_totals = sorted(category_totals.values(), key=lambda row: row["total"], reverse=True)
    budget = db.execute("SELECT * FROM monthly_budgets WHERE month = ?", (budget_month,)).fetchone()
    allocated = {name: (budget[column] if budget else 0) for name, column in SPENDER_COLUMNS.items()}
    savings_targets = {name: (budget[column + "_savings"] if budget else 0) for name, column in SPENDER_COLUMNS.items()}
    spent_rows = db.execute(
        "SELECT spent_by, COALESCE(SUM(amount_cents), 0) AS spent FROM entries "
        "WHERE substr(created_at, 1, 7) = ? GROUP BY spent_by", (budget_month,)
    ).fetchall()
    spent = {name: 0 for name in SPENDERS}
    for row in spent_rows:
        if row["spent_by"] in spent:
            spent[row["spent_by"]] = row["spent"]
    available = {name: allocated[name] * 100 - spent[name] - savings_targets[name] * 100 for name in SPENDERS}
    budget_total = sum(allocated.values())
    savings_total = sum(savings_targets.values())
    spent_total = sum(spent.values())
    available_total = budget_total * 100 - spent_total - savings_total * 100
    return render_template(
        "index.html", entries=entries, categories=CATEGORIES, quantity_units=QUANTITY_UNITS, mrp_units=MRP_UNITS,
        selected_categories=selected_categories, spenders=SPENDERS, search=search,
        start_date=start_date, end_date=end_date, total=visible_total,
        count=len(entries), category_totals=category_totals,
        visible_total=visible_total, budget_month=budget_month,
        current_month=current_month, allocated=allocated, spent=spent,
        available=available, budget_total=budget_total,
        spent_total=spent_total, available_total=available_total,
        savings_targets=savings_targets, savings_total=savings_total,
        spender_columns=SPENDER_COLUMNS,
        budget_updated_at=budget["updated_at"] if budget else None,
        savings_updated_at=budget["savings_updated_at"] if budget else None,
        today_total=daily_totals.get(today.isoformat(), 0),
        yesterday_total=daily_totals.get(yesterday.isoformat(), 0),
        today_label=today.strftime("%A, %d %b"),
        yesterday_label=yesterday.strftime("%A, %d %b"),
        invoice_default_date=today.isoformat(),
    )


@app.post("/budgets")
def save_budget():
    month = request.form.get("month", "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        flash("Choose a valid month for the expenditure budget.", "error")
        return redirect(url_for("index"))
    amounts = {}
    try:
        for person, column in SPENDER_COLUMNS.items():
            raw_amount = request.form.get(column, "").strip()
            amount = int(raw_amount)
            if amount < 0 or amount > 999999999:
                raise ValueError
            amounts[column] = amount
    except (ValueError, AttributeError):
        flash("Enter a whole, non-negative rupee amount for each person.", "error")
        return redirect(url_for("index", budget_month=month))
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    get_db().execute(
        "INSERT INTO monthly_budgets (month, maa, papa, sonal, on_hand_cash, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(month) DO UPDATE SET "
        "maa = excluded.maa, papa = excluded.papa, sonal = excluded.sonal, "
        "on_hand_cash = excluded.on_hand_cash, updated_at = excluded.updated_at",
        (month, amounts["maa"], amounts["papa"], amounts["sonal"], amounts["on_hand_cash"], updated_at),
    )
    get_db().commit()
    flash("Monthly expenditure amounts saved. Balances include this month's recorded expenses.", "success")
    return redirect(url_for("index", budget_month=month))


@app.post("/savings-targets")
def save_savings_targets():
    month = request.form.get("month", "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        flash("Choose a valid month for the savings target.", "error")
        return redirect(url_for("index"))
    targets = {}
    try:
        for person, column in SPENDER_COLUMNS.items():
            raw_amount = request.form.get(column + "_savings", "").strip()
            amount = int(raw_amount)
            if amount < 0 or amount > 999999999:
                raise ValueError
            targets[column + "_savings"] = amount
    except (ValueError, AttributeError):
        flash("Enter a whole, non-negative rupee savings target for each person.", "error")
        return redirect(url_for("index", budget_month=month))
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    db = get_db()
    db.execute(
        "INSERT INTO monthly_budgets (month, maa_savings, papa_savings, sonal_savings, "
        "on_hand_cash_savings, updated_at, savings_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(month) DO UPDATE SET maa_savings = excluded.maa_savings, "
        "papa_savings = excluded.papa_savings, sonal_savings = excluded.sonal_savings, "
        "on_hand_cash_savings = excluded.on_hand_cash_savings, savings_updated_at = excluded.savings_updated_at",
        (month, targets["maa_savings"], targets["papa_savings"], targets["sonal_savings"],
         targets["on_hand_cash_savings"], updated_at, updated_at),
    )
    db.commit()
    flash("Savings targets saved. They are reserved from this month's spendable amount.", "success")
    return redirect(url_for("index", budget_month=month))


@app.get("/export.csv")
def export_csv():
    try:
        entries, _categories, _search, _start, _end = filtered_entries()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Product / expense", "Quantity", "Unit", "MRP (INR)", "MRP rate unit", "Amount paid (INR)", "Category", "Spent by", "Date and time added"])
    for entry in entries:
        mrp = f"{entry['mrp_cents'] / 100:.2f}" if entry["mrp_cents"] is not None else ""
        writer.writerow([entry["product"], entry["quantity"], entry["quantity_unit"], mrp, entry["mrp_unit"] or "", f"{entry['amount_cents'] / 100:.2f}", entry["category"], entry["spent_by"], entry["created_at"]])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=pocket-ledger.csv"
    return response


@app.get("/invoice.png")
def download_daily_invoice():
    requested_date = request.args.get("date", "").strip()
    try:
        invoice_date = datetime.strptime(requested_date, "%Y-%m-%d").date()
    except ValueError:
        abort(400, description="Choose a valid date for the invoice.")
    entries = get_db().execute(
        "SELECT * FROM entries WHERE substr(created_at, 1, 10) = ? ORDER BY created_at, id",
        (invoice_date.isoformat(),),
    ).fetchall()
    width = 780
    margin = 54
    regular = receipt_font(21)
    small = receipt_font(16)
    bold = receipt_font(22, True)
    heading = receipt_font(36, True)
    total_font = receipt_font(28, True)
    total_cents = sum(entry["amount_cents"] for entry in entries)
    item_layout = []
    item_width = width - margin * 2 - 160
    for entry in entries:
        text = entry["product"]
        words = text.split()
        lines = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and bold.getlength(candidate) > item_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        quantity_detail = (
            f"Quantity: {entry['quantity']:g} {entry['quantity_unit']}"
            if entry["quantity_unit"] != "none" else "Quantity: not applicable"
        )
        mrp_detail = (
            f"MRP: ₹{entry['mrp_cents'] / 100:,.2f} per {entry['mrp_unit']}"
            if entry["mrp_cents"] is not None else "MRP: not provided"
        )
        detail_text = [
            f"{quantity_detail}  ·  {mrp_detail}",
            f"Category: {entry['category']}  ·  Paid by {entry['spent_by']}",
        ]
        detail_lines = []
        detail_width = width - margin * 2
        for detail in detail_text:
            words = detail.split()
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and small.getlength(candidate) > detail_width:
                    detail_lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                detail_lines.append(current)
        item_layout.append((entry, lines or ["Expense"], detail_lines))

    row_height = 88
    height = 270 + sum(
        max(row_height, 31 * len(lines) + 22 * len(details) + 31)
        for _entry, lines, details in item_layout
    ) + 170
    image = Image.new("RGB", (width, height), "#f0f3ef")
    draw = ImageDraw.Draw(image)
    paper = (255, 254, 250)
    ink = (39, 53, 48)
    muted = (126, 143, 134)
    green = (50, 120, 94)
    draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=24, fill=paper, outline=(224, 232, 225), width=2)
    draw.rounded_rectangle((margin, 48, margin + 48, 96), radius=14, fill=(225, 242, 232))
    draw.text((margin + 13, 53), "P", font=bold, fill=green)
    draw.text((margin + 62, 47), "POCKET LEDGER", font=heading, fill=ink)
    draw.text((margin, 119), "DAILY EXPENSE RECEIPT", font=small, fill=green)
    draw.text((margin, 151), invoice_date.strftime("%A, %d %B %Y"), font=bold, fill=ink)
    draw.text((width - margin, 155), f"{len(entries)} item" + ("s" if len(entries) != 1 else ""), font=small, fill=muted, anchor="ra")
    y = 211
    draw.line((margin, y, width - margin, y), fill=(222, 229, 222), width=2)
    y += 22
    draw.text((margin, y), "ITEM", font=small, fill=muted)
    draw.text((width - margin, y), "AMOUNT", font=small, fill=muted, anchor="ra")
    y += 32
    for entry, lines, details in item_layout:
        row_y = y
        for line in lines:
            draw.text((margin, y), line, font=bold, fill=ink)
            y += 31
        amount_text = f"₹{entry['amount_cents'] / 100:,.2f}"
        draw.text((width - margin, row_y), amount_text, font=regular, fill=ink, anchor="ra")
        y += 4
        for detail in details:
            draw.text((margin, y), detail, font=small, fill=muted)
            y += 22
        y += 12
        draw.line((margin, y, width - margin, y), fill=(235, 238, 232), width=1)
        y += 15
    if not entries:
        draw.text((margin, y + 4), "No expenses were recorded for this date.", font=regular, fill=muted)
        y += 57
    draw.line((margin, y + 3, width - margin, y + 3), fill=(190, 211, 197), width=2)
    draw.text((margin, y + 28), "TOTAL EXPENSE", font=bold, fill=ink)
    draw.text((width - margin, y + 22), f"₹{total_cents / 100:,.2f}", font=total_font, fill=green, anchor="ra")
    y += 86
    draw.text((width // 2, y), "A clear record, one expense at a time.", font=small, fill=muted, anchor="ma")
    image = image.crop((0, 0, width, min(height, y + 48)))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "image/png"
    response.headers["Content-Disposition"] = f"attachment; filename=pocket-ledger-receipt-{invoice_date.isoformat()}.png"
    return response


@app.get("/available.png")
def download_available_balance():
    month = request.args.get("month", datetime.now().astimezone().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        abort(400, description="Choose a valid month for the balance report.")
    db = get_db()
    budget = db.execute("SELECT * FROM monthly_budgets WHERE month = ?", (month,)).fetchone()
    allocated = {name: (budget[column] if budget else 0) for name, column in SPENDER_COLUMNS.items()}
    savings_targets = {name: (budget[column + "_savings"] if budget else 0) for name, column in SPENDER_COLUMNS.items()}
    spent_rows = db.execute(
        "SELECT spent_by, COALESCE(SUM(amount_cents), 0) AS spent FROM entries "
        "WHERE substr(created_at, 1, 7) = ? GROUP BY spent_by", (month,),
    ).fetchall()
    spent = {name: 0 for name in SPENDERS}
    for row in spent_rows:
        if row["spent_by"] in spent:
            spent[row["spent_by"]] = row["spent"]
    available = {
        name: allocated[name] * 100 - spent[name] - savings_targets[name] * 100
        for name in SPENDERS
    }
    width = 1060
    margin = 50
    regular = receipt_font(17)
    small = receipt_font(15)
    bold = receipt_font(20, True)
    heading = receipt_font(34, True)
    total_font = receipt_font(27, True)
    height = 700
    image = Image.new("RGB", (width, height), "#f0f3ef")
    draw = ImageDraw.Draw(image)
    paper = (255, 254, 250)
    ink = (39, 53, 48)
    muted = (126, 143, 134)
    green = (50, 120, 94)
    draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=24, fill=paper, outline=(224, 232, 225), width=2)
    draw.rounded_rectangle((margin, 48, margin + 48, 96), radius=14, fill=(225, 242, 232))
    draw.text((margin + 13, 53), "P", font=bold, fill=green)
    draw.text((margin + 62, 47), "POCKET LEDGER", font=heading, fill=ink)
    draw.text((margin, 119), "MONTHLY EXPENDITURE BALANCE", font=small, fill=green)
    draw.text((margin, 151), month, font=bold, fill=ink)
    y = 211
    draw.line((margin, y, width - margin, y), fill=(222, 229, 222), width=2)
    y += 22
    columns = [margin, 360, 530, 695, 870, width - margin]
    headers = ["PERSON", "PLANNED", "SPENT", "SAVINGS", "AVAILABLE"]
    for index, header in enumerate(headers):
        if index == 0:
            draw.text((columns[0], y), header, font=small, fill=muted)
        else:
            draw.text((columns[index + 1] - 8, y), header, font=small, fill=muted, anchor="ra")
    y += 38
    for person in SPENDERS:
        draw.text((columns[0], y), person, font=regular, fill=ink)
        values = [allocated[person], spent[person] / 100, savings_targets[person], available[person] / 100]
        for index, value in enumerate(values, 1):
            draw.text((columns[index + 1] - 8, y), f"₹{value:,.2f}", font=regular, fill=green if index == 4 else ink, anchor="ra")
        y += 53
        draw.line((margin, y, width - margin, y), fill=(235, 238, 232), width=1)
        y += 14
    total_available = sum(available.values())
    draw.text((columns[0], y + 9), "TOTAL AVAILABLE", font=bold, fill=ink)
    draw.text((width - margin, y + 4), f"₹{total_available / 100:,.2f}", font=total_font, fill=green, anchor="ra")
    y += 72
    draw.text((width // 2, y), "Planned funds less recorded spending and savings targets.", font=small, fill=muted, anchor="ma")
    image = image.crop((0, 0, width, min(height, y + 48)))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "image/png"
    response.headers["Content-Disposition"] = f"attachment; filename=pocket-ledger-available-{month}.png"
    return response


@app.post("/entries")
def add_entry():
    try:
        product, amount_cents, category, spent_by, quantity, quantity_unit, mrp_cents, mrp_unit = entry_form_values()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    get_db().execute(
        "INSERT INTO entries (product, quantity, quantity_unit, mrp_cents, mrp_unit, amount_cents, category, spent_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (product, quantity, quantity_unit, mrp_cents, mrp_unit, amount_cents, category, spent_by, created_at),
    )
    get_db().commit()
    flash("Expense added to your ledger.", "success")
    return redirect(url_for("index"))


@app.route("/entries/<int:entry_id>/edit", methods=["GET", "POST"])
def edit_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        abort(404)
    if request.method == "POST":
        try:
            product, amount_cents, category, spent_by, quantity, quantity_unit, mrp_cents, mrp_unit = entry_form_values()
        except ValueError as error:
            flash(str(error), "error")
        else:
            db.execute(
                "UPDATE entries SET product = ?, quantity = ?, quantity_unit = ?, mrp_cents = ?, mrp_unit = ?, amount_cents = ?, "
                "category = ?, spent_by = ? WHERE id = ?",
                (product, quantity, quantity_unit, mrp_cents, mrp_unit, amount_cents, category, spent_by, entry_id),
            )
            db.commit()
            flash("Expense updated.", "success")
            return redirect(url_for("index"))
    return render_template("edit.html", entry=entry, categories=CATEGORIES, spenders=SPENDERS, quantity_units=QUANTITY_UNITS, mrp_units=MRP_UNITS)


@app.post("/entries/<int:entry_id>/delete")
def delete_entry(entry_id):
    db = get_db()
    db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # Flask's debug reloader starts a second process; open the browser only there.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        Timer(1, lambda: webbrowser.open_new(f"http://127.0.0.1:{PORT}")).start()
    app.run(host=HOST, port=PORT, debug=debug)
