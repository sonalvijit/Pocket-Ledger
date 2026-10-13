from datetime import datetime
import csv
from io import StringIO
import os
import re
import sqlite3
import webbrowser
from contextlib import closing
from threading import Timer

from flask import Flask, abort, flash, g, make_response, redirect, render_template, request, url_for


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.environ.get("LEDGER_DATABASE", os.path.join(BASE_DIR, "ledger.db"))

CATEGORIES = [
    "Vegetables", "Dairy products", "Fruits", "Meat & seafood", "Bakery",
    "Groceries", "Snacks & junk food", "Dining out", "Beverages",
    "Household supplies", "Personal care", "Clothing", "Healthcare",
    "Transport", "Fuel", "Housing & rent", "Utilities & bills",
    "Subscriptions", "Education", "Entertainment", "Travel", "Gifts & donations",
    "Investment", "Fees & charges", "Taxes", "Other",
]
SPENDERS = ["Maa", "Papa", "Sonal", "On hand cash"]
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
    if not product:
        raise ValueError("Product or expense name is required.")
    if len(product) > 120:
        raise ValueError("Keep the product name under 120 characters.")
    if category not in CATEGORIES:
        raise ValueError("Choose a category from the list.")
    if spent_by not in SPENDERS and spent_by != "Not specified":
        raise ValueError("Choose who paid for this expense.")
    return product, parse_amount(request.form.get("amount")), category, spent_by


@app.template_filter("money")
def money(cents):
    return f"₹{cents / 100:,.2f}"


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
        "index.html", entries=entries, categories=CATEGORIES,
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
    writer.writerow(["Product / expense", "Amount (INR)", "Category", "Spent by", "Date and time added"])
    for entry in entries:
        writer.writerow([entry["product"], f"{entry['amount_cents'] / 100:.2f}", entry["category"], entry["spent_by"], entry["created_at"]])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=pocket-ledger.csv"
    return response


@app.post("/entries")
def add_entry():
    try:
        product, amount_cents, category, spent_by = entry_form_values()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    get_db().execute(
        "INSERT INTO entries (product, amount_cents, category, spent_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (product, amount_cents, category, spent_by, created_at),
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
            product, amount_cents, category, spent_by = entry_form_values()
        except ValueError as error:
            flash(str(error), "error")
        else:
            db.execute(
                "UPDATE entries SET product = ?, amount_cents = ?, category = ?, spent_by = ? WHERE id = ?",
                (product, amount_cents, category, spent_by, entry_id),
            )
            db.commit()
            flash("Expense updated.", "success")
            return redirect(url_for("index"))
    return render_template("edit.html", entry=entry, categories=CATEGORIES, spenders=SPENDERS)


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
        Timer(1, lambda: webbrowser.open_new("http://127.0.0.1:5000")).start()
    app.run(debug=debug)
