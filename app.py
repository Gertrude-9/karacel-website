from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import sqlite3

app = Flask(__name__)
app.secret_key = "bank_secret_key"


def get_db():
    return sqlite3.connect("bank.db")


def setup_database():
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT UNIQUE,
            account_name TEXT,
            bank_pin TEXT,
            balance REAL DEFAULT 0,
            account_type TEXT DEFAULT 'Savings Account',
            branch_code TEXT DEFAULT '8TECH001',
            is_registered INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            email TEXT,
            password TEXT,
            account_number TEXT UNIQUE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_username TEXT,
            sender_account TEXT,
            receiver_account TEXT,
            transfer_type TEXT,
            amount REAL,
            reference TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'admin'
        )
    """)

    db.execute("""
        INSERT OR IGNORE INTO staff (username, password, role)
        VALUES (?, ?, ?)
    """, ("admin", "admin123", "admin"))

    db.commit()
    db.close()


@app.route("/")
def splash():
    return render_template("splash.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        db = get_db()

        staff = db.execute("""
            SELECT *
            FROM staff
            WHERE username=? AND password=?
        """, (username, password)).fetchone()

        if staff:
            session.clear()
            session["staff"] = staff[1]
            session["role"] = staff[3]
            db.close()
            return redirect(url_for("admin_dashboard"))

        user = db.execute("""
            SELECT *
            FROM users
            WHERE username=? AND password=?
        """, (username, password)).fetchone()

        db.close()

        if user:
            session.clear()
            session["user_id"] = user[0]
            session["username"] = user[1]
            return redirect(url_for("dashboard"))

        return "Invalid username or password"

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        account_number = request.form["account_number"].strip()
        bank_pin = request.form["bank_pin"].strip()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            return "Passwords do not match"

        db = get_db()

        existing_user = db.execute("""
            SELECT *
            FROM users
            WHERE username=?
        """, (username,)).fetchone()

        if existing_user:
            db.close()
            return "Username already exists"

        bank_account = db.execute("""
            SELECT *
            FROM bank_accounts
            WHERE account_number=? AND bank_pin=?
        """, (account_number, bank_pin)).fetchone()

        if bank_account is None:
            db.close()
            return "Invalid bank account number or PIN"

        if bank_account[7] == 1:
            db.close()
            return "This bank account is already registered for online banking"

        db.execute("""
            INSERT INTO users (username, email, password, account_number)
            VALUES (?, ?, ?, ?)
        """, (username, email, password, account_number))

        db.execute("""
            UPDATE bank_accounts
            SET is_registered=1
            WHERE account_number=?
        """, (account_number,))

        db.commit()
        db.close()

        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))

    db = get_db()

    user = db.execute("""
        SELECT 
            users.id,
            users.username,
            users.email,
            users.password,
            users.account_number,
            bank_accounts.account_name,
            bank_accounts.balance,
            bank_accounts.account_type,
            bank_accounts.branch_code
        FROM users
        JOIN bank_accounts
        ON users.account_number = bank_accounts.account_number
        WHERE users.username=?
    """, (session["username"],)).fetchone()

    if user is None:
        db.close()
        session.clear()
        return redirect(url_for("login"))

    transactions = db.execute("""
        SELECT *
        FROM transactions
        WHERE sender_username=? OR receiver_account=?
        ORDER BY id DESC
    """, (session["username"], user[4])).fetchall()

    db.close()

    return render_template(
        "dashboard.html",
        user=user,
        transactions=transactions
    )


@app.route("/transfer", methods=["GET", "POST"])
def transfer():
    if "username" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("transfer.html")

    transfer_type = request.form["transfer_type"]
    receiver_account = request.form["recipient_id"].strip()
    reference = request.form.get("reference", "")

    if transfer_type != "bank":
        return "Only internal bank transfers are allowed"

    try:
        amount = float(request.form["amount"])
    except ValueError:
        return "Invalid amount"

    if amount <= 0:
        return "Amount must be greater than zero"

    if amount % 10000 != 0:
        return "Amount must be in multiples of 10,000"

    db = get_db()

    sender = db.execute("""
        SELECT users.username, users.account_number, bank_accounts.balance
        FROM users
        JOIN bank_accounts
        ON users.account_number = bank_accounts.account_number
        WHERE users.username=?
    """, (session["username"],)).fetchone()

    if sender is None:
        db.close()
        session.clear()
        return redirect(url_for("login"))

    receiver = db.execute("""
        SELECT users.username, users.account_number, bank_accounts.balance
        FROM users
        JOIN bank_accounts
        ON users.account_number = bank_accounts.account_number
        WHERE bank_accounts.account_number=?
    """, (receiver_account,)).fetchone()

    if receiver is None:
        db.close()
        return "Receiver account does not exist in the system"

    if sender[1] == receiver_account:
        db.close()
        return "You cannot transfer to your own account"

    if sender[2] < amount:
        db.close()
        return "Insufficient balance"

    db.execute("""
        UPDATE bank_accounts
        SET balance = balance - ?
        WHERE account_number=?
    """, (amount, sender[1]))

    db.execute("""
        UPDATE bank_accounts
        SET balance = balance + ?
        WHERE account_number=?
    """, (amount, receiver_account))

    db.execute("""
        INSERT INTO transactions
        (sender_username, sender_account, receiver_account, transfer_type, amount, reference)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        sender[0],
        sender[1],
        receiver_account,
        "internal",
        amount,
        reference
    ))

    db.commit()
    db.close()

    return redirect(url_for("dashboard"))


@app.route("/transactions")
def transactions():
    if "username" not in session:
        return redirect(url_for("login"))

    username = session["username"]

    db = get_db()

    transactions = db.execute("""
        SELECT 
            id,
            amount,
            reference,
            transfer_type,
            created_at,
            sender_username,
            sender_account,
            receiver_account,
            CASE 
                WHEN sender_username = ? THEN 'sent'
                ELSE 'received'
            END as type
        FROM transactions
        WHERE sender_username = ?
           OR receiver_account IN (
                SELECT account_number FROM users WHERE username = ?
           )
        ORDER BY created_at DESC
    """, (username, username, username)).fetchall()

    db.close()

    total_sent = sum(t[1] for t in transactions if t[8] == "sent")
    total_received = sum(t[1] for t in transactions if t[8] == "received")

    stats = {
        "total_count": len(transactions),
        "total_sent": total_sent,
        "total_received": total_received,
        "net_flow": total_received - total_sent
    }

    return render_template(
        "transactions.html",
        transactions=transactions,
        stats=stats
    )


@app.route("/support")
def support():
    if "username" not in session:
        return redirect(url_for("login"))

    return render_template("support.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# ADMIN / STAFF SECTION
# =========================

@app.route("/admin/dashboard")
def admin_dashboard():
    if "staff" not in session:
        return redirect(url_for("login"))

    db = get_db()

    accounts = db.execute("""
        SELECT *
        FROM bank_accounts
        ORDER BY id DESC
    """).fetchall()

    registered_users = db.execute("""
        SELECT *
        FROM users
        ORDER BY id DESC
    """).fetchall()

    total_balance = db.execute("""
        SELECT COALESCE(SUM(balance), 0)
        FROM bank_accounts
    """).fetchone()[0]

    all_transactions = db.execute("""
        SELECT 
        t.id,
        s.account_name AS sender_name,
        r.account_name AS receiver_name,
        t.amount,
        'Internal Transfer',
        t.reference,
        t.created_at,
        t.sender_account,
        t.receiver_account
    FROM transactions t
    LEFT JOIN bank_accounts s ON t.sender_account = s.account_number
    LEFT JOIN bank_accounts r ON t.receiver_account = r.account_number
    ORDER BY t.created_at DESC
""").fetchall()
    db.close()

    return render_template(
        "admin_dashboard.html",
        accounts=accounts,
        registered_users=registered_users,
        total_balance=total_balance,
        all_transactions=all_transactions
    )


@app.route("/admin/create-account", methods=["GET", "POST"])
def create_account():
    if "staff" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        account_number = request.form["account_number"].strip()
        account_name = request.form["account_name"].strip()
        bank_pin = request.form["bank_pin"].strip()
        account_type = request.form["account_type"]
        branch_code = request.form["branch_code"].strip()

        try:
            balance = float(request.form["balance"])
        except ValueError:
            return "Invalid opening balance"

        if balance < 0:
            return "Opening balance cannot be negative"

        db = get_db()

        try:
            db.execute("""
                INSERT INTO bank_accounts
                (account_number, account_name, bank_pin, balance, account_type, branch_code)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                account_number,
                account_name,
                bank_pin,
                balance,
                account_type,
                branch_code
            ))

            db.commit()
            db.close()

            return redirect(url_for("admin_dashboard"))

        except sqlite3.IntegrityError:
            db.close()
            return "Account number already exists"

    return render_template("create_account.html")


@app.route("/admin/update_customer", methods=["POST"])
def update_customer():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()

    customer_id = data.get("id")
    name = data.get("name")
    balance = data.get("balance")
    account_type = data.get("account_type")
    branch_code = data.get("branch_code")
    status = data.get("status")

    db = get_db()

    try:
        db.execute("""
            UPDATE bank_accounts
            SET account_name = ?,
                balance = ?,
                account_type = ?,
                branch_code = ?,
                is_registered = ?
            WHERE id = ?
        """, (name, balance, account_type, branch_code, status, customer_id))

        db.commit()
        db.close()

        return jsonify({"success": True}), 200

    except Exception as e:
        db.close()
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/admin/delete_customer", methods=["POST"])
def delete_customer():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()
    customer_id = data.get("id")

    db = get_db()

    db.execute("""
        DELETE FROM bank_accounts
        WHERE id=?
    """, (customer_id,))

    db.commit()
    db.close()

    return jsonify({"success": True})


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    setup_database()
    app.run(debug=True)