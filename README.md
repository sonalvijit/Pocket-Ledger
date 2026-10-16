# Pocket Ledger

A small personal expense ledger built with Flask and SQLite. Record a product or expense, its quantity and unit, MRP rate and the unit it applies to, amount paid, category, who paid, and the date/time it was entered. Entries are stored locally in `ledger.db`.

Quantity units include grams (`g`), kilograms (`kg`), pieces, packs, millilitres (`ml`), litres (`L`), sets, items, and **None (not applicable)** for expenses like mobile recharge.

## Preview

![Pocket Ledger screenshot](img/Screenshot%202026-10-13%20093005.png)

## Run locally

1. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install Flask and start the app. Your default browser opens the ledger automatically:

   ```powershell
   pip install -r requirements.txt
   python app.py
   ```

If the browser does not open automatically, visit <http://127.0.0.1:51743>. Set `LEDGER_PORT` to use a different port. The server listens on your local network by default; from another device on the same Wi-Fi, open `http://<this-computer-local-IP>:51743` (find the IPv4 address with `ipconfig` on Windows). Set `LEDGER_HOST` if you want to change the bind address.

The database file is created automatically on first run. Amounts are shown in Indian rupees (₹). Each new entry receives the computer's local date and time. Set `LEDGER_DATABASE` to use a different SQLite file, or `SECRET_KEY` to set a private Flask session key. Set `FLASK_DEBUG=1` only while developing locally.

New entries include a **Spent by** choice: Maa, Papa, Sonal, or On hand cash. Existing entries are preserved during the database update and show **Not specified** until you edit them.

Use **Set monthly amounts** to enter each source's planned rupee amount for a month. Recorded expenses are deducted from the matching source's available balance for the month in which they were entered. The dashboard shows planned, spent, and remaining amounts by source and in total.

Use **Monthly Savings Target** to reserve a whole-rupee amount from each source for next month. Savings targets are deducted from this month's ready-to-spend amount and are stored with their own update timestamp. They are targets only; after you save money, include the actual carried-forward amount in next month's expenditure plan.

Search by product name, select one or more categories, and filter by a start and end date to see the total for that period. Use **CSV** to download the currently matching entries for a spreadsheet.

## Categories

Vegetables, dairy products, fruits, meat & seafood, poultry products, bakery, groceries, snacks & junk food, dining out, beverages, household supplies, personal care, clothing, healthcare, transport, fuel, housing & rent, utilities & bills, subscriptions, education, entertainment, travel, gifts & donations, investment, fees & charges, taxes, and other.
