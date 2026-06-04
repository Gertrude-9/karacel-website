from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import sqlite3

app = Flask(__name__)

# ============================================
# VULNERABILITY 1: Hardcoded Secret Key
# CWE-798: Use of Hard-coded Credentials
# ============================================
app.secret_key = "hardcoded_secret_key_12345_please_change_me"  # VULNERABLE: Anyone who reads code can forge sessions


def get_db():
    db = sqlite3.connect("bank.db")
    db.row_factory = sqlite3.Row
    return db


def setup_database():
    db = get_db()

    # Create tables
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
            full_name TEXT,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            phone TEXT,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'teller',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ============================================
    # VULNERABILITY 2: Hardcoded Default Credentials (Plaintext)
    # CWE-798: Default credentials with known passwords
    # ============================================
    db.execute("""
        INSERT OR IGNORE INTO staff 
        (full_name, username, password, role, is_active)
        VALUES ('System Admin', 'admin', 'admin123', 'admin', 1)
    """)
    
    db.execute("""
        INSERT OR IGNORE INTO staff 
        (full_name, username, password, role, is_active)
        VALUES ('Bank Manager', 'manager', 'manager123', 'manager', 1)
    """)
    
    db.execute("""
        INSERT OR IGNORE INTO staff 
        (full_name, username, password, role, is_active)
        VALUES ('Head Teller', 'teller', 'teller123', 'teller', 1)
    """)

    # ============================================
    # VULNERABILITY 3: Plaintext PIN Storage
    # CWE-256: Unprotected Storage of Credentials
    # ============================================
    db.execute("""
        INSERT OR IGNORE INTO bank_accounts 
        (account_number, account_name, bank_pin, balance, account_type)
        VALUES ('ACC1001', 'John Doe', '1234', 500000, 'Savings Account')
    """)
    
    db.execute("""
        INSERT OR IGNORE INTO bank_accounts 
        (account_number, account_name, bank_pin, balance, account_type)
        VALUES ('ACC1002', 'Jane Smith', '5678', 750000, 'Current Account')
    """)
    
    db.execute("""
        INSERT OR IGNORE INTO bank_accounts 
        (account_number, account_name, bank_pin, balance, account_type)
        VALUES ('ACC1003', 'Bob Johnson', '9012', 250000, 'Savings Account')
    """)

    # Create customer_notes table for Stored XSS
    db.execute("""
        CREATE TABLE IF NOT EXISTS customer_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            staff_id INTEGER,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.commit()
    db.close()


@app.route("/")
def splash():
    return render_template("splash.html")


# ============================================
# VULNERABILITY 4: SQL Injection in Staff Login
# CWE-89: Improper Neutralization of Special Elements used in SQL Command
# OWASP Top 10: A03:2021 – Injection
# ============================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        db = get_db()
        
        # VULNERABLE: String concatenation allows SQL injection
        # Example payload: admin' OR '1'='1' --
        staff_query = f"""
            SELECT id, full_name, username, email, phone, password, role, is_active
            FROM staff
            WHERE username='{username}' AND password='{password}' AND is_active=1
        """
        print(f"[DEBUG] Staff Query: {staff_query}")  # VULNERABILITY: Debug logging
        staff = db.execute(staff_query).fetchone()

        if staff:
            session.clear()
            session["staff_id"] = staff[0]
            session["staff"] = staff[2]
            session["role"] = staff[6]
            db.close()

            if staff[6] == "admin":
                return redirect(url_for("admin_dashboard"))
            elif staff[6] in ["manager", "teller"]:
                return redirect(url_for("staff_dashboard"))
            else:
                return redirect(url_for("login"))

        # ============================================
        # VULNERABILITY 5: SQL Injection in User Login
        # Same pattern - string concatenation
        # ============================================
        user_query = f"""
            SELECT *
            FROM users
            WHERE username='{username}' AND password='{password}'
        """
        print(f"[DEBUG] User Query: {user_query}")
        user = db.execute(user_query).fetchone()

        db.close()

        if user:
            session.clear()
            session["user_id"] = user[0]
            session["username"] = user[1]
            return redirect(url_for("dashboard"))

        # VULNERABILITY: Information disclosure - reveals which username exists
        return f"Invalid username or password for: {username}"

    return render_template("login.html")


# ============================================
# VULNERABILITY 6: Plaintext Password Storage
# CWE-256: Plaintext storage of passwords during registration
# ============================================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        account_number = request.form["account_number"].strip()
        bank_pin = request.form["bank_pin"].strip()
        password = request.form["password"]  # VULNERABLE: Stored as plaintext!
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            return "Passwords do not match"

        db = get_db()

        # VULNERABILITY: SQL Injection in user check
        existing_user_query = f"""
            SELECT *
            FROM users
            WHERE username='{username}'
        """
        existing_user = db.execute(existing_user_query).fetchone()

        if existing_user:
            db.close()
            return "Username already exists"

        # VULNERABILITY: SQL Injection in bank account check
        bank_account_query = f"""
            SELECT *
            FROM bank_accounts
            WHERE account_number='{account_number}' AND bank_pin='{bank_pin}'
        """
        bank_account = db.execute(bank_account_query).fetchone()

        if bank_account is None:
            db.close()
            return "Invalid bank account number or PIN"

        if bank_account[7] == 1:
            db.close()
            return "This bank account is already registered"

        # VULNERABILITY: SQL Injection in INSERT
        db.execute(f"""
            INSERT INTO users (username, email, password, account_number)
            VALUES ('{username}', '{email}', '{password}', '{account_number}')
        """)

        db.execute(f"""
            UPDATE bank_accounts
            SET is_registered=1
            WHERE account_number='{account_number}'
        """)

        db.commit()
        db.close()

        return redirect(url_for("login"))

    return render_template("register.html")


# ============================================
# VULNERABILITY 7: SQL Injection in Dashboard
# Multiple injection points in user queries
# ============================================
@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))

    db = get_db()

    user_query = f"""
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
        WHERE users.username='{session["username"]}'
    """
    user = db.execute(user_query).fetchone()

    if user is None:
        db.close()
        session.clear()
        return redirect(url_for("login"))

    # VULNERABILITY: SQL Injection in transactions query
    transactions_query = f"""
        SELECT *
        FROM transactions
        WHERE sender_username='{session["username"]}' OR receiver_account='{user[4]}'
        ORDER BY id DESC
    """
    transactions = db.execute(transactions_query).fetchall()

    db.close()

    return render_template("dashboard.html", user=user, transactions=transactions)


# ============================================
# VULNERABILITY 8: Stored XSS + SQL Injection in Transfer
# CWE-79: Improper Neutralization of Input During Web Page Generation
# Stored XSS via reference field
# ============================================
@app.route("/transfer", methods=["GET", "POST"])
def transfer():
    if "username" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("transfer.html")

    transfer_type = request.form["transfer_type"]
    receiver_account = request.form["recipient_id"].strip()
    reference = request.form.get("reference", "")
    
    # VULNERABILITY: Stored XSS - reference not sanitized
    # Example payload: <script>alert('XSS')</script>

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

    # VULNERABILITY: SQL Injection in sender query
    sender_query = f"""
        SELECT users.username, users.account_number, bank_accounts.balance
        FROM users
        JOIN bank_accounts
        ON users.account_number = bank_accounts.account_number
        WHERE users.username='{session["username"]}'
    """
    sender = db.execute(sender_query).fetchone()

    if sender is None:
        db.close()
        session.clear()
        return redirect(url_for("login"))

    # VULNERABILITY: SQL Injection in receiver query
    receiver_query = f"""
        SELECT users.username, users.account_number, bank_accounts.balance
        FROM users
        JOIN bank_accounts
        ON users.account_number = bank_accounts.account_number
        WHERE bank_accounts.account_number='{receiver_account}'
    """
    receiver = db.execute(receiver_query).fetchone()

    if receiver is None:
        db.close()
        return "Receiver account does not exist"

    if sender[1] == receiver_account:
        db.close()
        return "You cannot transfer to your own account"

    if sender[2] < amount:
        db.close()
        return "Insufficient balance"

    # VULNERABILITY: SQL Injection in UPDATE statements
    db.execute(f"""
        UPDATE bank_accounts
        SET balance = balance - {amount}
        WHERE account_number='{sender[1]}'
    """)

    db.execute(f"""
        UPDATE bank_accounts
        SET balance = balance + {amount}
        WHERE account_number='{receiver_account}'
    """)

    # VULNERABILITY: SQL Injection and Stored XSS combined
    db.execute(f"""
        INSERT INTO transactions
        (sender_username, sender_account, receiver_account, transfer_type, amount, reference)
        VALUES ('{sender[0]}', '{sender[1]}', '{receiver_account}', 'internal', {amount}, '{reference}')
    """)

    db.commit()
    db.close()

    return redirect(url_for("dashboard"))


@app.route("/transactions")
def transactions():
    if "username" not in session:
        return redirect(url_for("login"))

    username = session["username"]
    db = get_db()

    # VULNERABILITY: SQL Injection in transactions query
    transactions_query = f"""
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
                WHEN sender_username = '{username}' THEN 'sent'
                ELSE 'received'
            END as type
        FROM transactions
        WHERE sender_username = '{username}'
           OR receiver_account IN (
                SELECT account_number FROM users WHERE username = '{username}'
           )
        ORDER BY created_at DESC
    """
    transactions = db.execute(transactions_query).fetchall()

    db.close()

    total_sent = sum(t[1] for t in transactions if t[8] == "sent")
    total_received = sum(t[1] for t in transactions if t[8] == "received")

    stats = {
        "total_count": len(transactions),
        "total_sent": total_sent,
        "total_received": total_received,
        "net_flow": total_received - total_sent
    }

    return render_template("transactions.html", transactions=transactions, stats=stats)


@app.route("/support")
def support():
    if "username" not in session:
        return redirect(url_for("login"))
    return render_template("support.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================
# VULNERABILITY 9: Reflected XSS in Search
# CWE-79: Reflected Cross-site Scripting
# OWASP Top 10: A03:2021 – Injection
# ============================================
@app.route("/search")
def search():
    query = request.args.get("q", "")
    # VULNERABLE: Raw f-string returns unescaped user input
    # Example: /search?q=<script>alert('XSS')</script>
    return f"""
        <h2>Search Results for: {query}</h2>
        <p>No results found for '{query}'.</p>
        <a href="/">Go back</a>
    """


# ============================================
# VULNERABILITY 10: Broken Access Control (IDOR)
# CWE-639: Authorization Bypass Through User-Controlled Key
# OWASP Top 10: A01:2021 – Broken Access Control
# ============================================
@app.route('/account/<int:account_id>')
def view_account_details(account_id):
    """VULNERABLE: No authorization check - any user can view ANY account"""
    if "username" not in session:
        return redirect(url_for("login"))
    
    db = get_db()
    # VULNERABILITY: No check that logged-in user owns this account
    # Example: /account/1, /account/2, /account/3
    account = db.execute(f"""
        SELECT * FROM bank_accounts WHERE id={account_id}
    """).fetchone()
    db.close()
    
    if account:
        return render_template("account_details.html", account=account)
    else:
        return "Account not found", 404


@app.route('/user/<int:user_id>/profile')
def view_user_profile(user_id):
    """VULNERABLE: IDOR - Change user_id in URL to see other users' data"""
    if "username" not in session:
        return redirect(url_for("login"))
    
    db = get_db()
    # VULNERABILITY: No authorization check
    user = db.execute(f"""
        SELECT id, username, email, account_number FROM users WHERE id={user_id}
    """).fetchone()
    db.close()
    
    if user:
        return render_template("profile.html", user=user)
    return "User not found", 404


@app.route('/transaction/<int:transaction_id>')
def view_transaction_details(transaction_id):
    """VULNERABLE: IDOR - Any user can view any transaction"""
    if "username" not in session:
        return redirect(url_for("login"))
    
    db = get_db()
    transaction = db.execute(f"""
        SELECT * FROM transactions WHERE id={transaction_id}
    """).fetchone()
    db.close()
    
    if transaction:
        return render_template("transaction_detail.html", transaction=transaction)
    return "Transaction not found", 404


# ============================================
# VULNERABILITY 11: Missing CSRF Protection
# CWE-352: Cross-Site Request Forgery
# OWASP Top 10: A01:2021 – Broken Access Control
# ============================================
# All POST endpoints lack CSRF tokens - attackers can forge requests


# ============================================
# VULNERABILITY 12: Admin Routes Missing Role Checks
# CWE-862: Missing Authorization
# ============================================
@app.route("/admin/dashboard")
def admin_dashboard():
    # VULNERABILITY: Only checks if logged in, not if admin!
    # Any staff member can access admin dashboard
    if "staff" not in session:
        return redirect(url_for("login"))

    db = get_db()

    # VULNERABILITY: Exposes bank_pin in query results
    accounts = db.execute("""
        SELECT 
            b.id,
            b.account_number,
            b.account_name,
            b.bank_pin,
            b.balance,
            b.account_type,
            b.branch_code,
            b.is_registered,
            u.email
        FROM bank_accounts b
        LEFT JOIN users u ON b.account_number = u.account_number
        ORDER BY b.id DESC
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
            # VULNERABILITY: SQL Injection in INSERT
            db.execute(f"""
                INSERT INTO bank_accounts
                (account_number, account_name, bank_pin, balance, account_type, branch_code)
                VALUES ('{account_number}', '{account_name}', '{bank_pin}', {balance}, '{account_type}', '{branch_code}')
            """)

            db.commit()
            db.close()

            return redirect(url_for("admin_dashboard"))

        except sqlite3.IntegrityError:
            db.close()
            return "Account number already exists"

    return render_template("create_account.html")


# ============================================
# VULNERABILITY 13: Stored XSS in Customer Notes
# ============================================
@app.route('/staff/customer/<int:customer_id>/note', methods=['POST'])
def add_customer_note(customer_id):
    """VULNERABLE: Stored XSS - notes displayed without escaping"""
    if "staff" not in session:
        return redirect(url_for("login"))
    
    note = request.form.get('note', '')
    
    db = get_db()
    # VULNERABILITY: No sanitization of note
    db.execute(f"""
        INSERT INTO customer_notes (customer_id, staff_id, note, created_at)
        VALUES ({customer_id}, {session['staff_id']}, '{note}', datetime('now'))
    """)
    db.commit()
    db.close()
    
    return redirect(url_for('view_customer', customer_id=customer_id))


@app.route("/admin/update_customer", methods=["POST"])
def update_customer():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()

    db = get_db()

    try:
        # VULNERABILITY: SQL Injection in UPDATE
        db.execute(f"""
            UPDATE bank_accounts
            SET account_name = '{data.get("name")}',
                balance = {data.get("balance")},
                account_type = '{data.get("account_type")}',
                branch_code = '{data.get("branch_code")}',
                is_registered = {data.get("status")}
            WHERE id = {data.get("id")}
        """)

        db.commit()
        db.close()

        return jsonify({"success": True})

    except Exception as e:
        db.close()
        # VULNERABILITY: Detailed error messages expose system info
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/admin/delete_customer", methods=["POST"])
def delete_customer():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()
    customer_id = data.get("id")

    db = get_db()

    # VULNERABILITY: SQL Injection in DELETE
    db.execute(f"""
        DELETE FROM bank_accounts
        WHERE id={customer_id}
    """)

    db.commit()
    db.close()

    return jsonify({"success": True})


@app.route("/admin/add_staff", methods=["POST"])
def admin_add_staff():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()

    full_name = data.get("full_name", "").strip()
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()
    password = data.get("password", "").strip()  # VULNERABILITY: Plaintext password
    role = data.get("role", "teller").strip()

    if not username or not password:
        return jsonify({"success": False, "message": "Username and password required"}), 400

    db = get_db()

    try:
        # VULNERABILITY: SQL Injection in INSERT
        db.execute(f"""
            INSERT INTO staff (full_name, username, email, phone, password, role, is_active)
            VALUES ('{full_name}', '{username}', '{email}', '{phone}', '{password}', '{role}', 1)
        """)

        db.commit()
        return jsonify({"success": True, "message": "Staff added successfully"})

    except sqlite3.IntegrityError:
        return jsonify({"success": False, "message": "Username already exists"}), 400

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        db.close()


@app.route("/admin/get_staff")
def admin_get_staff():
    if "staff" not in session:
        return jsonify([]), 401

    db = get_db()
    staff_members = db.execute("""
        SELECT id, full_name, username, email, phone, role, is_active, created_at
        FROM staff
        ORDER BY id DESC
    """).fetchall()
    db.close()

    staff_list = []
    for s in staff_members:
        staff_list.append({
            "id": s[0],
            "full_name": s[1],
            "username": s[2],
            "email": s[3],
            "phone": s[4],
            "role": s[5],
            "is_active": s[6],
            "created_at": s[7]
        })

    return jsonify(staff_list)


@app.route("/admin/update_staff", methods=["POST"])
def admin_update_staff():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()

    staff_id = data.get("id")
    full_name = data.get("full_name", "").strip()
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()
    role = data.get("role", "teller").strip()
    is_active = data.get("is_active", 1)
    password = data.get("password", "").strip()

    db = get_db()

    try:
        if password:
            db.execute(f"""
                UPDATE staff
                SET full_name='{full_name}', username='{username}', email='{email}', 
                    phone='{phone}', role='{role}', is_active={is_active}, password='{password}'
                WHERE id={staff_id}
            """)
        else:
            db.execute(f"""
                UPDATE staff
                SET full_name='{full_name}', username='{username}', email='{email}', 
                    phone='{phone}', role='{role}', is_active={is_active}
                WHERE id={staff_id}
            """)

        db.commit()
        return jsonify({"success": True})

    except sqlite3.IntegrityError:
        return jsonify({"success": False, "message": "Username already exists"}), 400

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        db.close()


@app.route("/admin/reset_staff_password", methods=["POST"])
def admin_reset_staff_password():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()
    staff_id = data.get("id")
    password = data.get("password", "").strip()

    if not password:
        return jsonify({"success": False, "message": "Password is required"}), 400

    db = get_db()
    # VULNERABILITY: SQL Injection + Plaintext password
    db.execute(f"UPDATE staff SET password='{password}' WHERE id={staff_id}")
    db.commit()
    db.close()

    return jsonify({"success": True})


@app.route("/admin/delete_staff", methods=["POST"])
def admin_delete_staff():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()
    staff_id = data.get("id")

    db = get_db()
    db.execute(f"DELETE FROM staff WHERE id={staff_id}")  # VULNERABILITY: SQL Injection
    db.commit()
    db.close()

    return jsonify({"success": True})


@app.route("/admin/logout")
@app.route("/staff/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/staff/dashboard")
def staff_dashboard():
    if "staff" not in session:
        return redirect(url_for("login"))

    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    db = get_db()

    total_customers = db.execute("SELECT COUNT(*) FROM bank_accounts").fetchone()[0]
    total_balance = db.execute("SELECT COALESCE(SUM(balance), 0) FROM bank_accounts").fetchone()[0]
    today_transactions = db.execute("SELECT COUNT(*) FROM transactions WHERE DATE(created_at)=DATE('now')").fetchone()[0]

    stats = {
        "total_customers": total_customers,
        "total_balance": total_balance,
        "today_transactions": today_transactions
    }

    customers = db.execute("""
        SELECT account_number, account_name, balance, account_type, branch_code, is_registered
        FROM bank_accounts
        ORDER BY id DESC
        LIMIT 10
    """).fetchall()

    transactions = db.execute("""
        SELECT sender_account, receiver_account, amount, reference, created_at
        FROM transactions
        ORDER BY id DESC
        LIMIT 10
    """).fetchall()

    db.close()

    return render_template(
        "staff_dashboard.html",
        staff_name=session.get("staff"),
        role=session.get("role"),
        stats=stats,
        customers=customers,
        transactions=transactions
    )


@app.route("/staff/search_customer")
def staff_search_customer():
    if "staff" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    query = request.args.get("q", "")
    
    db = get_db()
    
    # VULNERABILITY: SQL Injection in search
    customer_query = f"""
        SELECT account_number, account_name, balance, account_type, is_registered
        FROM bank_accounts
        WHERE account_number='{query}' OR account_name LIKE '%{query}%'
        LIMIT 1
    """
    customer = db.execute(customer_query).fetchone()
    
    db.close()
    
    if customer:
        return jsonify({
            "account_number": customer[0],
            "account_name": customer[1],
            "balance": customer[2],
            "account_type": customer[3],
            "is_registered": customer[4]
        })
    else:
        return jsonify(None)


@app.route("/staff/create_account", methods=["POST"])
def staff_create_account():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    
    data = request.get_json()
    
    account_number = data.get("account_number")
    account_name = data.get("account_name")
    bank_pin = data.get("bank_pin")
    balance = data.get("balance", 0)
    account_type = data.get("account_type", "Savings Account")
    
    db = get_db()
    
    try:
        # VULNERABILITY: SQL Injection and plaintext PIN
        db.execute(f"""
            INSERT INTO bank_accounts
            (account_number, account_name, bank_pin, balance, account_type, branch_code, is_registered)
            VALUES ('{account_number}', '{account_name}', '{bank_pin}', {balance}, '{account_type}', '8TECH001', 0)
        """)
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        db.close()


@app.route("/staff/process_transaction", methods=["POST"])
def staff_process_transaction():
    if "staff" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    
    data = request.get_json()
    
    from_account = data.get("from_account")
    to_account = data.get("to_account")
    amount = data.get("amount")
    reference = data.get("reference", "")
    
    db = get_db()
    
    sender = db.execute(f"""
        SELECT account_number, account_name, balance
        FROM bank_accounts
        WHERE account_number='{from_account}'
    """).fetchone()
    
    receiver = db.execute(f"""
        SELECT account_number, account_name
        FROM bank_accounts
        WHERE account_number='{to_account}'
    """).fetchone()
    
    if not sender or not receiver:
        return jsonify({"success": False, "message": "Account not found"}), 404
    
    if sender[2] < amount:
        return jsonify({"success": False, "message": "Insufficient balance"}), 400
    
    db.execute(f"""
        UPDATE bank_accounts
        SET balance = balance - {amount}
        WHERE account_number='{from_account}'
    """)
    
    db.execute(f"""
        UPDATE bank_accounts
        SET balance = balance + {amount}
        WHERE account_number='{to_account}'
    """)
    
    # VULNERABILITY: SQL Injection and Stored XSS
    db.execute(f"""
        INSERT INTO transactions
        (sender_username, sender_account, receiver_account, transfer_type, amount, reference)
        VALUES ('staff_teller', '{from_account}', '{to_account}', 'staff_transfer', {amount}, '{reference}')
    """)
    
    db.commit()
    db.close()
    
    return jsonify({"success": True})


@app.route("/staff/logout")
def staff_logout_route():
    session.clear()
    return redirect(url_for("login"))

# @app.route("/account/<int:account_id>")
# def view_account(account_id):
#     if "staff_username" not in session:
#         return redirect(url_for("staff_login"))

#     db = get_db()
#     db.row_factory = sqlite3.Row

#     account = db.execute("""
#         SELECT id, account_number, account_name, user_id, balance, account_type, branch, status, email
#         FROM accounts
#         WHERE id = ?
#     """, (account_id,)).fetchone()

#     accounts = db.execute("""
#         SELECT id, account_number, account_name, user_id, balance, account_type, branch, status, email
#         FROM accounts
#     """).fetchall()

#     db.close()

#     return render_template("admin_dashboard.html", account=account, accounts=accounts)

# ============================================
# VULNERABILITY 14: Debug Mode Enabled in Production
# CWE-489: Active Debug Code
# ============================================
if __name__ == "__main__":
    setup_database()
    app.run(host="0.0.0.0", port=5000, debug=True)  # VULNERABILITY: Debug mode enabled