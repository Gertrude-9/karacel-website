from flask import Flask, jsonify, render_template, request, redirect, session, flash, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
from datetime import datetime, timedelta
import re
import os
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from io import BytesIO
from flask import send_file

app = Flask(__name__)
app.secret_key = "karacel_secret_key"

# Configuration for file uploads
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB limit

# Create upload directory if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'receipts'), exist_ok=True)


def get_db():
    conn = sqlite3.connect("sacco.db")
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# REGISTER CHAT BLUEPRINT
# ============================================================
from chat_api import chat_api, create_chat_table

app.register_blueprint(chat_api)
create_chat_table()


def create_database():
    conn = get_db()
    cursor = conn.cursor()
    
    # ============================================================
    # FIRST: CHECK AND ADD MISSING COLUMNS TO EXISTING TABLES
    # ============================================================
    # Check users table
    cursor.execute("PRAGMA table_info(users)")
    existing_columns = [col[1] for col in cursor.fetchall()]
    
    columns_to_add = {
        'kai_shares': 'INTEGER DEFAULT 0',
        'ks_shares': 'INTEGER DEFAULT 0',
        'kac_paid': 'INTEGER DEFAULT 0',
        'kac_paid_date': 'TEXT',
        'registration_fee_paid': 'INTEGER DEFAULT 0',
        'registration_fee_paid_date': 'TEXT'
    }
    
    for col_name, col_type in columns_to_add.items():
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added column to users: {col_name}")
            except Exception as e:
                print(f"⚠️ Could not add column {col_name}: {e}")
    
    # Check system_settings table
    cursor.execute("PRAGMA table_info(system_settings)")
    existing_settings_columns = [col[1] for col in cursor.fetchall()]
    
    settings_columns_to_add = {
        'kai_share_price': 'INTEGER DEFAULT 100000',
        'ks_share_price': 'INTEGER DEFAULT 10000',
        'kac_annual_fee': 'INTEGER DEFAULT 100000',
        'registration_fee': 'INTEGER DEFAULT 20000'
    }
    
    for col_name, col_type in settings_columns_to_add.items():
        if col_name not in existing_settings_columns:
            try:
                cursor.execute(f"ALTER TABLE system_settings ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added column to system_settings: {col_name}")
            except Exception as e:
                print(f"⚠️ Could not add column {col_name}: {e}")
    
    # Check savings_deposits table
    cursor.execute("PRAGMA table_info(savings_deposits)")
    existing_deposits_columns = [col[1] for col in cursor.fetchall()]
    
    deposits_columns_to_add = {
        'savings_type': 'TEXT DEFAULT "KAI"',
        'shares': 'INTEGER DEFAULT 0'
    }
    
    for col_name, col_type in deposits_columns_to_add.items():
        if col_name not in existing_deposits_columns:
            try:
                cursor.execute(f"ALTER TABLE savings_deposits ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added column to savings_deposits: {col_name}")
            except Exception as e:
                print(f"⚠️ Could not add column {col_name}: {e}")
    
    # Check repayments table
    cursor.execute("PRAGMA table_info(repayments)")
    existing_repayments_columns = [col[1] for col in cursor.fetchall()]
    
    repayments_columns_to_add = {
        'interest_paid': 'REAL DEFAULT 0',
        'principal_paid': 'REAL DEFAULT 0',
        'balance_after': 'REAL DEFAULT 0',
        'notes': 'TEXT'
    }
    
    for col_name, col_type in repayments_columns_to_add.items():
        if col_name not in existing_repayments_columns:
            try:
                cursor.execute(f"ALTER TABLE repayments ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added column to repayments: {col_name}")
            except Exception as e:
                print(f"⚠️ Could not add column {col_name}: {e}")
    
    # Check loans table
    cursor.execute("PRAGMA table_info(loans)")
    existing_loans_columns = [col[1] for col in cursor.fetchall()]
    
    loans_columns_to_add = {
        'total_interest_accrued': 'REAL DEFAULT 0',
        'principal_paid': 'REAL DEFAULT 0',
        'interest_paid': 'REAL DEFAULT 0',
        'months_paid': 'INTEGER DEFAULT 0',
        'original_balance': 'REAL DEFAULT 0',
        'total_interest_calculated': 'REAL DEFAULT 0',
        'due_date': 'TEXT',
        'application_fee': 'REAL DEFAULT 1000',
        'application_fee_paid': 'INTEGER DEFAULT 0',
        'net_loan_amount': 'REAL DEFAULT 0',
        'loan_type': 'TEXT DEFAULT "standard"',
        'disbursed_amount': 'REAL DEFAULT 0',
        'total_fees_paid': 'REAL DEFAULT 0',
        'total_penalties': 'REAL DEFAULT 0',
        # ✅ NEW: Simple interest tracking
        'accrued_interest': 'REAL DEFAULT 0',
        'last_interest_applied_date': 'TEXT',
        'total_interest_charged': 'REAL DEFAULT 0'
    }
    
    for col_name, col_type in loans_columns_to_add.items():
        if col_name not in existing_loans_columns:
            try:
                cursor.execute(f"ALTER TABLE loans ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added column to loans: {col_name}")
            except Exception as e:
                print(f"⚠️ Could not add column {col_name}: {e}")

    # ============================================================
    # BACKFILL: Set last_interest_applied_date for existing loans
    # ============================================================
    try:
        cursor.execute("""
            UPDATE loans
            SET last_interest_applied_date = COALESCE(
                disbursed_date,
                approved_date,
                loan_start_date,
                application_date
            )
            WHERE last_interest_applied_date IS NULL
            AND status IN ('disbursed', 'active', 'approved')
        """)
        rows_updated = cursor.rowcount
        if rows_updated > 0:
            print(f"✅ Backfilled last_interest_applied_date for {rows_updated} existing loan(s)")
    except Exception as e:
        print(f"⚠️ Backfill warning: {e}")

    
    # ============================================================
    # CHECK NOTIFICATIONS TABLE FOR MISSING COLUMNS
    # ============================================================
    cursor.execute("PRAGMA table_info(notifications)")
    existing_notifications_columns = [col[1] for col in cursor.fetchall()]
    
    # Add missing columns to notifications if they don't exist
    notifications_columns_to_add = {
        'link': 'TEXT',
        'is_read': 'INTEGER DEFAULT 0'
    }
    
    # Check if 'type' column exists, if not add it
    if 'type' not in existing_notifications_columns:
        try:
            cursor.execute("ALTER TABLE notifications ADD COLUMN type TEXT DEFAULT 'general'")
            print("✅ Added column to notifications: type")
        except Exception as e:
            print(f"⚠️ Could not add column type: {e}")
    
    for col_name, col_type in notifications_columns_to_add.items():
        if col_name not in existing_notifications_columns:
            try:
                cursor.execute(f"ALTER TABLE notifications ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added column to notifications: {col_name}")
            except Exception as e:
                print(f"⚠️ Could not add column {col_name}: {e}")
    
    # ============================================================
    # NOW CREATE TABLES IF THEY DON'T EXIST (with all columns)
    # ============================================================
    # Create users table with ALL savings type columns
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        gender TEXT,
        dob TEXT,
        sacco_number TEXT UNIQUE NOT NULL,
        email TEXT,
        phone TEXT,
        address TEXT,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'member',
        status TEXT DEFAULT 'active',
        savings_balance REAL DEFAULT 0,
        next_of_kin_name TEXT,
        relationship TEXT,
        next_of_kin_phone TEXT,
        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        kai_shares INTEGER DEFAULT 0,
        ks_shares INTEGER DEFAULT 0,
        kac_paid INTEGER DEFAULT 0,
        kac_paid_date TEXT,
        registration_fee_paid INTEGER DEFAULT 0,
        registration_fee_paid_date TEXT
    )
    """)
    
    # Create loans table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loan_number TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        interest_rate REAL NOT NULL,
        interest_amount REAL NOT NULL,
        total_repayment REAL NOT NULL,
        monthly_installment REAL NOT NULL,
        tenure INTEGER DEFAULT 3,
        purpose TEXT,
        repayment_plan TEXT,
        status TEXT DEFAULT 'pending',
        application_date TEXT NOT NULL,
        approved_date TEXT,
        disbursed_date TEXT,
        completed_date TEXT,
        rejected_date TEXT,
        rejection_reason TEXT,
        admin_rejection_reason TEXT,
        current_balance REAL DEFAULT 0,
        interest_accrued REAL DEFAULT 0,
        last_interest_date TEXT,
        months_overdue INTEGER DEFAULT 0,
        start_month INTEGER DEFAULT 0,
        end_month INTEGER DEFAULT 12,
        last_payment_date TEXT,
        last_payment_amount REAL DEFAULT 0,
        loan_start_date TEXT,
        loan_end_date TEXT,
        disbursement_date TEXT,
        approved_by TEXT,
        approved_by_role TEXT,
        disbursed_by TEXT,
        disbursed_by_role TEXT,
        rejected_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        total_interest_accrued REAL DEFAULT 0,
        principal_paid REAL DEFAULT 0,
        interest_paid REAL DEFAULT 0,
        months_paid INTEGER DEFAULT 0,
        original_balance REAL DEFAULT 0,
        total_interest_calculated REAL DEFAULT 0,
        due_date TEXT,
        application_fee REAL DEFAULT 1000,
        application_fee_paid INTEGER DEFAULT 0,
        net_loan_amount REAL DEFAULT 0,
        loan_type TEXT DEFAULT 'standard',
        disbursed_amount REAL DEFAULT 0,
        total_fees_paid REAL DEFAULT 0,
        total_penalties REAL DEFAULT 0,
        accrued_interest REAL DEFAULT 0,
        last_interest_applied_date TEXT,
        total_interest_charged REAL DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Create notifications table with type column (not notification_type)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT DEFAULT 'general',
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        link TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Create loan guarantors table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS loan_guarantors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loan_id INTEGER NOT NULL,
        guarantor_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT,
        relationship TEXT,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (loan_id) REFERENCES loans(id)
    )
    """)

    # Create repayments table with interest tracking
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS repayments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        interest_paid REAL DEFAULT 0,
        principal_paid REAL DEFAULT 0,
        balance_after REAL DEFAULT 0,
        payment_date TEXT NOT NULL,
        payment_method TEXT,
        transaction_ref TEXT,
        notes TEXT,
        status TEXT DEFAULT 'completed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (loan_id) REFERENCES loans(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Create savings deposits table with savings type
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS savings_deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        savings_type TEXT DEFAULT 'KAI',
        shares INTEGER DEFAULT 0,
        deposit_date TEXT NOT NULL,
        payment_method TEXT,
        receipt_number TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Create guarantor tracking table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS guarantor_tracking (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guarantor_id INTEGER NOT NULL,
        loan_id INTEGER NOT NULL,
        member_name TEXT NOT NULL,
        amount_guaranteed REAL NOT NULL,
        outstanding_balance REAL DEFAULT 0,
        repayment_status TEXT DEFAULT 'on_track',
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (guarantor_id) REFERENCES users(id),
        FOREIGN KEY (loan_id) REFERENCES loans(id)
    )
    """)

    # ============================================================
    # PUBLICITY TABLES
    # ============================================================
    
    # Create announcements table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """)

    # Create events table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        event_date DATE NOT NULL,
        event_time TIME,
        location TEXT NOT NULL,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """)

    # Create newsletters table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS newsletters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        recipients INTEGER DEFAULT 0,
        created_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """)

    # Create social posts table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS social_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """)

    # Create system settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sacco_name TEXT DEFAULT 'Karacel Association',
        registration_number TEXT DEFAULT 'SACCO/REG/2024/001',
        savings_interest_rate REAL DEFAULT 6.5,
        loan_interest_rate REAL DEFAULT 12,
        penalty_rate REAL DEFAULT 5,
        max_loan_amount TEXT DEFAULT '10000000',
        min_loan_amount TEXT DEFAULT '10000',
        max_tenure INTEGER DEFAULT 24,
        kai_share_price INTEGER DEFAULT 100000,
        ks_share_price INTEGER DEFAULT 10000,
        kac_annual_fee INTEGER DEFAULT 100000,
        registration_fee INTEGER DEFAULT 20000,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Insert default settings if none exist
    settings_exists = cursor.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0]
    if settings_exists == 0:
        cursor.execute("""
            INSERT INTO system_settings (
                sacco_name, registration_number, savings_interest_rate,
                loan_interest_rate, penalty_rate, max_loan_amount,
                min_loan_amount, max_tenure, kai_share_price,
                ks_share_price, kac_annual_fee, registration_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'Karacel Association',
            'SACCO/REG/2024/001',
            6.5,
            12,
            5,
            '10000000',
            '10000',
            24,
            100000,
            10000,
            100000,
            20000
        ))

    # Create admin user only if no users exist
    admin_exists = cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
    if admin_exists == 0:
        cursor.execute("""
            INSERT INTO users (
                full_name, gender, dob, sacco_number,
                email, phone, address,
                password, role, status,
                savings_balance,
                next_of_kin_name, relationship, next_of_kin_phone
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "System Administrator",
            "Male",
            "1990-01-01",
            "ADM001",
            "admin@sacco.com",
            "0700000000",
            "Head Office",
            "admin123",
            "admin",
            "active",
            0,
            None,
            None,
            None
        ))

    # ============================================================
    # CREATE INDEXES FOR BETTER PERFORMANCE
    # ============================================================
    
    # Notifications indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at)")
    
    # Announcements indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_announcements_created_at ON announcements(created_at)")
    
    # Events indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date)")
    
    # Newsletters indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_newsletters_sent_date ON newsletters(sent_date)")
    
    # Social posts indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_social_posts_created_at ON social_posts(created_at)")
    
    # Loans indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_loans_user_id ON loans(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_loans_application_date ON loans(application_date)")
    
    # Repayments indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_repayments_loan_id ON repayments(loan_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_repayments_user_id ON repayments(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_repayments_payment_date ON repayments(payment_date)")
    
    # Savings deposits indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_savings_deposits_user_id ON savings_deposits(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_savings_deposits_deposit_date ON savings_deposits(deposit_date)")

    conn.commit()
    conn.close()
    print("✅ Database created/updated successfully with all tables!")
    print("📊 Tables created: users, loans, notifications, loan_guarantors, repayments, savings_deposits, guarantor_tracking, announcements, events, newsletters, social_posts, system_settings")

create_database()


# ============================================
# LOAN HELPER FUNCTIONS
# ============================================
from datetime import datetime


def get_start_month(application_date):
    """Get the starting month of the loan (1-12)"""
    return datetime.strptime(application_date, '%Y-%m-%d').month


def get_remaining_months(application_date):
    """Calculate remaining months until December of same year"""
    start_date = datetime.strptime(application_date, '%Y-%m-%d')
    return 12 - start_date.month + 1


def calculate_loan_end_date(application_date):
    """Loan must end on 31st December of application year"""
    year = datetime.strptime(application_date, '%Y-%m-%d').year
    return datetime(year, 12, 31).strftime('%Y-%m-%d')


def generate_loan_reference():
    """Generate unique loan reference number"""
    year = datetime.now().strftime('%Y')
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM loans")
    count = cursor.fetchone()['count'] + 1
    db.close()
    return f"LN-{year}-{str(count).zfill(4)}"


def get_interest_rate(amount):
    """Interest rate according to loan amount"""
    if 10000 <= amount <= 1999999:
        return 5
    elif 2000000 <= amount <= 4999999:
        return 3
    elif 5000000 <= amount <= 9999999:
        return 2
    elif amount >= 10000000:
        return 1
    return 0


def check_loan_eligibility(user_id, loan_amount):
    """Check if member is eligible for loan based on savings"""
    db = get_db()
    total_savings = db.execute("""
        SELECT COALESCE(SUM(amount), 0) as total 
        FROM savings_deposits 
        WHERE user_id = ?
    """, (user_id,)).fetchone()['total']
    db.close()
    
    # 95% of savings threshold
    threshold = total_savings * 0.95
    return loan_amount <= threshold, threshold, total_savings


def check_guarantor_eligibility(phone, email):
    """Check if a guarantor is eligible (has guaranteed less than 2 loans)"""
    db = get_db()
    cursor = db.cursor()
    
    cursor.execute("""
        SELECT COUNT(*) as count 
        FROM loan_guarantors 
        WHERE (phone = ? OR email = ?) 
        AND status IN ('active')
    """, (phone, email))
    
    count = cursor.fetchone()['count']
    db.close()
    
    return count < 2, count


def send_email(to_email, subject, body, html_body=None):
    """Send email notification - Manual sending only"""
    try:
        print(f"EMAIL TO: {to_email}")
        print(f"SUBJECT: {subject}")
        print(f"BODY: {body}")
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


def send_sms(phone, message):
    """Send SMS notification - Manual sending only"""
    try:
        print(f"SMS TO: {phone}")
        print(f"MESSAGE: {message}")
        return True
    except Exception as e:
        print(f"SMS error: {e}")
        return False


# ============================================
# TEMPLATE FILTERS
# ============================================
@app.template_filter('format_number')
def format_number(value):
    if value is None:
        return '0'
    try:
        return f"{int(float(value)):,}"
    except (ValueError, TypeError):
        return str(value)


@app.template_filter('sum')
def sum_filter(values, attribute=None):
    if not values:
        return 0
    if attribute:
        total = 0
        for item in values:
            if hasattr(item, attribute):
                total += getattr(item, attribute) or 0
        return total
    return sum(values) if values else 0


app.jinja_env.filters['format_number'] = format_number
app.jinja_env.filters['sum'] = sum_filter


# ============================================
# AUTHENTICATION ROUTES
# ============================================
ROLE_ROUTES = {
    "admin":       "/admin/dashboard",
    "chairperson": "/admin/dashboard",
    "treasurer":   "/treasurer/dashboard",
    "secretary":   "/secretary/dashboard",
    "publicity":   "/publicity/dashboard",
    "member":      "/member/dashboard",
}

@app.route("/")
def splash():
    if session.get("logged_in"):
        return redirect(ROLE_ROUTES.get(session.get("role"), "/home"))
    return render_template("splash.html")

@app.route("/home")
def home():
    return render_template("website/home-page.html")

@app.route("/about")
def about():
    """About Us page"""
    return render_template("website/about.html")


@app.route("/contact")
def contact():
    """Contact page"""
    return render_template("website/contact.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        sacco_number = request.form["sacco_number"].strip().upper()
        password = request.form["password"]  # don't strip passwords

        conn = get_db()
        try:
            conn.row_factory = sqlite3.Row
            user = conn.execute(
                "SELECT * FROM users WHERE sacco_number = ?",
                (sacco_number,)
            ).fetchone()
        finally:
            conn.close()

        if user and user["password"] == password:      # ← plain-text comparison
            session.update({
                "user_id":      user["id"],
                "sacco_number": user["sacco_number"],
                "full_name":    user["full_name"],
                "role":         user["role"],
                "logged_in":    True,
            })
            return redirect(ROLE_ROUTES.get(user["role"], "/member/dashboard"))

        flash("Invalid SACCO number or password", "error")

    return render_template("login.html")


# ============================================================
# STAFF MEMBER PORTAL ACCESS ROUTE
# ============================================================
@app.route("/staff/member-portal")
def staff_member_portal():
    """Staff access to member portal"""
    if "user_id" not in session:
        return redirect("/login")
    
    # All staff roles can access member portal
    if session.get("role") not in ["admin", "chairperson", "treasurer", "secretary", "publicity"]:
        flash('Access denied. Only staff members can access this portal.', 'danger')
        return redirect("/login")
    
    # Redirect to member dashboard with staff_view flag
    return redirect(url_for('member_dashboard', staff_view=True))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ============================================
# ADMIN DASHBOARD
# ============================================
@app.route("/admin/dashboard")
def admin_dashboard():
    if session.get("role") != "admin":
        return redirect("/login")

    conn = get_db()

    total_members = conn.execute("""
        SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member'
    """).fetchone()[0]

    total_savings = conn.execute("""
        SELECT COALESCE(SUM(savings_balance), 0) 
        FROM users 
        WHERE LOWER(role) = 'member'
    """).fetchone()[0]

    monthly_savings = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) 
        FROM savings_deposits 
        WHERE deposit_date >= date('now', 'start of month')
    """).fetchone()[0]

    total_deposits = conn.execute("""
        SELECT COUNT(*) FROM savings_deposits
    """).fetchone()[0]

    recent_deposits = conn.execute("""
        SELECT sd.*, u.full_name, u.sacco_number
        FROM savings_deposits sd
        JOIN users u ON sd.user_id = u.id
        ORDER BY sd.deposit_date DESC
        LIMIT 5
    """).fetchall()

    members = conn.execute("""
        SELECT 
            u.*,
            COALESCE((SELECT COUNT(*) FROM loans WHERE user_id = u.id AND status IN ('approved', 'disbursed', 'active')), 0) as active_loans_count
        FROM users u 
        WHERE LOWER(u.role) = 'member'
        ORDER BY u.id DESC
    """).fetchall()

    total_loans = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) 
        FROM loans 
        WHERE status IN ('approved', 'disbursed', 'active')
    """).fetchone()[0]

    active_loans = conn.execute("""
        SELECT COUNT(*) 
        FROM loans 
        WHERE status IN ('approved', 'disbursed', 'active')
    """).fetchone()[0]

    pending_loans = conn.execute("""
        SELECT COUNT(*) 
        FROM loans 
        WHERE status = 'pending'
    """).fetchone()[0]

    approved_loans = conn.execute("""
        SELECT COUNT(*) 
        FROM loans 
        WHERE status = 'approved'
    """).fetchone()[0]

    rejected_loans = conn.execute("""
        SELECT COUNT(*) 
        FROM loans 
        WHERE status = 'rejected'
    """).fetchone()[0]

    disbursed_loans = conn.execute("""
        SELECT COUNT(*) 
        FROM loans 
        WHERE status = 'disbursed'
    """).fetchone()[0]

    completed_loans = conn.execute("""
        SELECT COUNT(*) 
        FROM loans 
        WHERE status = 'completed'
    """).fetchone()[0]

    loan_applications = conn.execute("""
        SELECT 
            l.*, 
            u.full_name, 
            u.savings_balance,
            COALESCE((SELECT COUNT(*) FROM loan_guarantors WHERE loan_id = l.id AND status = 'active'), 0) as total_guarantors,
            COALESCE((SELECT COUNT(*) FROM loan_guarantors WHERE loan_id = l.id AND status = 'pending'), 0) as pending_guarantors
        FROM loans l
        JOIN users u ON l.user_id = u.id
        ORDER BY 
            CASE 
                WHEN l.status = 'pending' THEN 1
                WHEN l.status = 'approved' THEN 2
                WHEN l.status = 'disbursed' THEN 3
                WHEN l.status = 'active' THEN 4
                WHEN l.status = 'completed' THEN 5
                WHEN l.status = 'rejected' THEN 6
            END,
            l.application_date DESC
        LIMIT 50
    """).fetchall()

    recent_activities = conn.execute("""
        SELECT 'deposit' as type, sd.amount, sd.deposit_date as date, u.full_name, u.sacco_number 
        FROM savings_deposits sd
        JOIN users u ON sd.user_id = u.id
        UNION ALL
        SELECT 'repayment' as type, r.amount, r.payment_date as date, u.full_name, u.sacco_number 
        FROM repayments r
        JOIN users u ON r.user_id = u.id
        WHERE r.status = 'completed'
        ORDER BY date DESC
        LIMIT 10
    """).fetchall()

    staff_users = conn.execute("""
        SELECT 
            u.*,
            COUNT(DISTINCT l.id) as loans_processed,
            COUNT(DISTINCT sd.id) as deposits_processed
        FROM users u
        LEFT JOIN loans l ON l.user_id = u.id
        LEFT JOIN savings_deposits sd ON sd.user_id = u.id
        WHERE LOWER(u.role) IN ('admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
        GROUP BY u.id
        ORDER BY 
            CASE 
                WHEN LOWER(u.role) = 'admin' THEN 1
                WHEN LOWER(u.role) = 'chairperson' THEN 2
                WHEN LOWER(u.role) = 'treasurer' THEN 3
                WHEN LOWER(u.role) = 'secretary' THEN 4
                WHEN LOWER(u.role) = 'publicity' THEN 5
            END,
            u.full_name
    """).fetchall()

    staff_counts = {
        'treasurer': conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'treasurer'").fetchone()[0],
        'secretary': conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'secretary'").fetchone()[0],
        'publicity': conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'publicity'").fetchone()[0],
        'admin': conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) IN ('admin', 'chairperson')").fetchone()[0]
    }

    today = datetime.now().strftime('%Y-%m-%d')

    # Get settings
    settings = {}
    try:
        conn.row_factory = sqlite3.Row
        settings_row = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings_row:
            settings = dict(settings_row)
        else:
            settings = {
                'sacco_name': 'Karacel Association',
                'registration_number': 'SACCO/REG/2024/001',
                'savings_interest_rate': 6.5,
                'loan_interest_rate': 12,
                'penalty_rate': 5,
                'max_loan_amount': '10,000,000',
                'min_loan_amount': '10,000',
                'max_tenure': 24,
                'kai_share_price': 100000,
                'ks_share_price': 10000,
                'kac_annual_fee': 100000,
                'registration_fee': 20000
            }
    except Exception as e:
        print(f"Error loading settings: {e}")
        settings = {
            'sacco_name': 'Karacel Association',
            'registration_number': 'SACCO/REG/2024/001',
            'savings_interest_rate': 6.5,
            'loan_interest_rate': 12,
            'penalty_rate': 5,
            'max_loan_amount': '10,000,000',
            'min_loan_amount': '10,000',
            'max_tenure': 24,
            'kai_share_price': 100000,
            'ks_share_price': 10000,
            'kac_annual_fee': 100000,
            'registration_fee': 20000
        }

    conn.close()

    return render_template(
        "admin/admin-dashboard.html",
        members=members,
        total_members=total_members,
        total_savings=total_savings,
        monthly_savings=monthly_savings,
        total_deposits=total_deposits,
        recent_deposits=recent_deposits,
        total_loans=total_loans,
        active_loans=active_loans,
        pending_loans=pending_loans,
        approved_loans=approved_loans,
        rejected_loans=rejected_loans,
        disbursed_loans=disbursed_loans,
        completed_loans=completed_loans,
        loan_applications=loan_applications,
        recent_activities=recent_activities,
        staff_users=staff_users,
        staff_counts=staff_counts,
        today=today,
        now=datetime.now(),
        settings=settings
    )


# ============================================================
# CONTEXT PROCESSOR - Global template variables
# ============================================================
@app.context_processor
def inject_globals():
    """Inject common variables into all templates"""
    from datetime import datetime
    
    completed_loans = 0
    try:
        if 'user_id' in session:
            role = session.get('role', '')
            if role in ['treasurer', 'admin', 'chairperson']:
                db = get_db()
                completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
                db.close()
    except Exception as e:
        print(f"Error getting completed_loans: {e}")
    
    return {
        'now': datetime.now(),
        'completed_loans': completed_loans
    }


# ============================================================
# TREASURER DASHBOARD - FIXED RECENT ACTIVITIES
# ============================================================
@app.route("/treasurer/dashboard")
def treasurer_dashboard():
    if session.get("role") not in ["treasurer", "secretary", "admin", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        # ============================================================
        # GET ALL ACTIVE USERS (MEMBERS + STAFF)
        # ============================================================
        members = conn.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                email,
                phone,
                status,
                savings_balance,
                registration_date,
                gender,
                dob,
                address,
                role,
                kai_shares,
                ks_shares,
                kac_paid,
                registration_fee_paid,
                next_of_kin_name,
                next_of_kin_phone,
                relationship
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            ORDER BY 
                CASE 
                    WHEN LOWER(role) = 'member' THEN 1
                    WHEN LOWER(role) = 'admin' THEN 2
                    WHEN LOWER(role) = 'chairperson' THEN 3
                    WHEN LOWER(role) = 'treasurer' THEN 4
                    WHEN LOWER(role) = 'secretary' THEN 5
                    WHEN LOWER(role) = 'publicity' THEN 6
                END,
                full_name ASC
        """).fetchall()
        
        # ============================================================
        # GET STAFF MEMBERS
        # ============================================================
        staff_members = conn.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                email,
                phone,
                status,
                savings_balance,
                role,
                kai_shares,
                ks_shares,
                kac_paid,
                registration_fee_paid
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            ORDER BY full_name ASC
        """).fetchall()
        
        # ============================================================
        # GET REGULAR MEMBERS ONLY
        # ============================================================
        regular_members = conn.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                email,
                phone,
                status,
                savings_balance,
                role,
                kai_shares,
                ks_shares,
                kac_paid,
                registration_fee_paid
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) = 'member'
            ORDER BY full_name ASC
        """).fetchall()
        
        # ============================================================
        # RECENT DEPOSITS - FIXED: Include ALL users (members + staff)
        # ============================================================
        recent_deposits = conn.execute("""
            SELECT 
                sd.id,
                sd.user_id,
                sd.amount,
                sd.savings_type,
                sd.shares,
                sd.deposit_date,
                sd.payment_method,
                sd.receipt_number,
                sd.notes,
                u.full_name,
                u.sacco_number,
                u.role
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
            ORDER BY sd.deposit_date DESC, sd.created_at DESC
            LIMIT 20
        """).fetchall()
        
        # ============================================================
        # RECENT REPAYMENTS - FIXED: Include ALL users (members + staff)
        # ============================================================
        recent_repayments = conn.execute("""
            SELECT 
                r.id,
                r.loan_id,
                r.user_id,
                r.amount,
                r.interest_paid,
                r.principal_paid,
                r.balance_after,
                r.payment_date,
                r.payment_method,
                r.transaction_ref,
                r.notes,
                r.status,
                r.created_at,
                u.full_name,
                u.sacco_number,
                u.role,
                l.loan_number
            FROM repayments r
            JOIN users u ON r.user_id = u.id
            JOIN loans l ON r.loan_id = l.id
            WHERE r.status = 'completed'
            AND u.status = 'active'
            ORDER BY r.payment_date DESC
            LIMIT 20
        """).fetchall()
        
        # ============================================================
        # ALL DEPOSITS - FIXED: Include ALL users
        # ============================================================
        all_deposits = conn.execute("""
            SELECT 
                sd.id,
                sd.user_id,
                sd.amount,
                sd.savings_type,
                sd.shares,
                sd.deposit_date,
                sd.payment_method,
                sd.receipt_number,
                sd.notes,
                u.full_name,
                u.sacco_number,
                u.role
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
            ORDER BY sd.deposit_date DESC, sd.created_at DESC
        """).fetchall()
        
        # ============================================================
        # STATISTICS - Include ALL users
        # ============================================================
        total_members = len(members)
        total_regular_members = len(regular_members)
        total_staff_members = len(staff_members)
        
        total_savings = conn.execute("""
            SELECT COALESCE(SUM(savings_balance), 0) 
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
        """).fetchone()[0]
        
        total_deposits = conn.execute("""
            SELECT COALESCE(SUM(sd.amount), 0) 
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
        """).fetchone()[0]
        
        monthly_deposits = conn.execute("""
            SELECT COALESCE(SUM(sd.amount), 0) 
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
            AND sd.deposit_date >= date('now', 'start of month')
        """).fetchone()[0]
        
        # ============================================================
        # LOAN STATISTICS
        # ============================================================
        pending_loans = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'pending'").fetchone()[0]
        approved_loans = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'approved'").fetchone()[0]
        active_loans = conn.execute("SELECT COUNT(*) FROM loans WHERE status IN ('disbursed', 'active')").fetchone()[0]
        completed_loans = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
        rejected_loans = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'rejected'").fetchone()[0]
        
        # ============================================================
        # SAVINGS BY TYPE - Include staff
        # ============================================================
        kai_total = 0
        ks_total = 0
        kac_total = 0
        registration_fees_total = 0
        kai_members = 0
        ks_members = 0
        kac_members = 0
        registration_fees_count = 0
        kai_shares = 0
        ks_shares = 0
        
        settings = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            settings_dict = dict(settings)
            kai_share_price = settings_dict.get('kai_share_price', 100000) or 100000
            ks_share_price = settings_dict.get('ks_share_price', 10000) or 10000
            kac_annual_fee = settings_dict.get('kac_annual_fee', 100000) or 100000
            registration_fee = settings_dict.get('registration_fee', 20000) or 20000
        else:
            kai_share_price = 100000
            ks_share_price = 10000
            kac_annual_fee = 100000
            registration_fee = 20000
        
        all_active_users = conn.execute("""
            SELECT * FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
        """).fetchall()
        
        for user in all_active_users:
            user_dict = dict(user)
            
            if user_dict.get('kai_shares'):
                shares = user_dict['kai_shares']
                kai_shares += shares
                kai_total += shares * kai_share_price
                kai_members += 1
            
            if user_dict.get('ks_shares'):
                shares = user_dict['ks_shares']
                ks_shares += shares
                ks_total += shares * ks_share_price
                ks_members += 1
            
            if user_dict.get('kac_paid'):
                kac_total += kac_annual_fee
                kac_members += 1
            
            if user_dict.get('registration_fee_paid'):
                registration_fees_total += registration_fee
                registration_fees_count += 1
        
        # ============================================================
        # LOAN APPLICATIONS - Include staff loans
        # ============================================================
        loan_applications = conn.execute("""
            SELECT 
                l.id,
                l.loan_number,
                l.amount,
                l.interest_rate,
                l.interest_amount,
                l.total_repayment,
                l.monthly_installment,
                l.tenure,
                l.purpose,
                l.repayment_plan,
                l.status,
                l.application_date,
                l.approved_date,
                l.disbursed_date,
                l.completed_date,
                l.current_balance,
                l.interest_accrued,
                l.due_date,
                l.loan_start_date,
                l.loan_end_date,
                l.rejection_reason,
                l.admin_rejection_reason,
                l.application_fee,
                l.application_fee_paid,
                l.net_loan_amount,
                l.total_interest_accrued,
                l.interest_paid,
                u.full_name,
                u.sacco_number,
                u.phone,
                u.account_number,
                u.email,
                u.savings_balance,
                u.role
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE u.status = 'active'
            ORDER BY l.application_date DESC
            LIMIT 50
        """).fetchall()
        
        # Convert to dictionaries and add guarantors
        all_loan_applications = []
        for loan_row in loan_applications:
            loan = dict(loan_row)
            guarantors = conn.execute("""
                SELECT 
                    id,
                    guarantor_name,
                    phone,
                    email,
                    relationship,
                    status
                FROM loan_guarantors 
                WHERE loan_id = ? 
                ORDER BY id
            """, (loan['id'],)).fetchall()
            loan['guarantors'] = [dict(g) for g in guarantors] if guarantors else []
            all_loan_applications.append(loan)
        
        # ============================================================
        # ACTIVE LOANS LIST - Include staff
        # ============================================================
        active_loans_list = conn.execute("""
            SELECT 
                l.id,
                l.loan_number,
                l.amount,
                l.interest_rate,
                l.interest_amount,
                l.total_repayment,
                l.monthly_installment,
                l.tenure,
                l.purpose,
                l.repayment_plan,
                l.status,
                l.application_date,
                l.approved_date,
                l.disbursed_date,
                l.completed_date,
                l.current_balance,
                l.interest_accrued,
                l.due_date,
                l.loan_start_date,
                l.loan_end_date,
                l.disbursed_amount,
                l.application_fee,
                l.application_fee_paid,
                l.net_loan_amount,
                l.total_interest_accrued,
                l.interest_paid,
                u.full_name,
                u.sacco_number,
                u.phone,
                u.account_number,
                u.email,
                u.role
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status IN ('disbursed', 'active')
            AND u.status = 'active'
            ORDER BY l.application_date DESC
        """).fetchall()
        
        # ============================================================
        # COMPLETED LOANS LIST - Include staff
        # ============================================================
        completed_loans_list = conn.execute("""
            SELECT 
                l.id,
                l.loan_number,
                l.amount,
                l.interest_rate,
                l.interest_amount,
                l.total_repayment,
                l.monthly_installment,
                l.tenure,
                l.purpose,
                l.repayment_plan,
                l.status,
                l.application_date,
                l.approved_date,
                l.disbursed_date,
                l.completed_date,
                l.current_balance,
                l.interest_accrued,
                l.due_date,
                l.loan_start_date,
                l.loan_end_date,
                l.disbursed_amount,
                l.application_fee,
                l.application_fee_paid,
                l.net_loan_amount,
                l.total_interest_accrued,
                l.interest_paid,
                u.full_name,
                u.sacco_number,
                u.phone,
                u.account_number,
                u.email,
                u.role
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status = 'completed'
            AND u.status = 'active'
            ORDER BY l.completed_date DESC, l.application_date DESC
        """).fetchall()
        
        # ============================================================
        # INTEREST STATISTICS
        # ============================================================
        total_interest_accrued = conn.execute("SELECT COALESCE(SUM(total_interest_accrued), 0) FROM loans").fetchone()[0]
        total_interest_paid = conn.execute("SELECT COALESCE(SUM(interest_paid), 0) FROM loans").fetchone()[0]
        total_interest_outstanding = total_interest_accrued - total_interest_paid
        
        # ============================================================
        # HIGHEST BORROWER - Include staff
        # ============================================================
        highest_borrower = {'name': 'N/A', 'total': 0}
        highest_interest_borrower = {'name': 'N/A', 'interest': 0}
        total_loan_fees = 0
        
        current_year = datetime.now().year
        year_start = f"{current_year}-01-01"
        year_end = f"{current_year}-12-31"
        
        loans_this_year = conn.execute("""
            SELECT 
                l.user_id,
                u.full_name,
                u.role,
                COUNT(l.id) as loan_count,
                COALESCE(SUM(l.amount), 0) as total_borrowed,
                COALESCE(SUM(l.interest_paid), 0) as total_interest_paid,
                COALESCE(SUM(l.total_interest_accrued), 0) as total_interest_accrued
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.application_date >= ? AND l.application_date <= ?
            AND l.status IN ('disbursed', 'active', 'completed')
            AND u.status = 'active'
            GROUP BY l.user_id
            ORDER BY total_borrowed DESC
        """, (year_start, year_end)).fetchall()
        
        total_loan_fees = conn.execute("""
            SELECT COUNT(*) * 1000 as total_fees
            FROM loans
            WHERE application_date >= ? AND application_date <= ?
            AND status IN ('disbursed', 'active', 'completed', 'approved')
        """, (year_start, year_end)).fetchone()[0] or 0
        
        if loans_this_year and len(loans_this_year) > 0:
            top_borrower = loans_this_year[0]
            highest_borrower = {
                'name': top_borrower['full_name'],
                'total': top_borrower['total_borrowed'] or 0,
                'count': top_borrower['loan_count'] or 0,
                'role': top_borrower['role'] or 'member'
            }
            
            loans_list = []
            for loan in loans_this_year:
                loans_list.append({
                    'full_name': loan['full_name'],
                    'total_interest_paid': loan['total_interest_paid'] or 0,
                    'total_interest_accrued': loan['total_interest_accrued'] or 0,
                    'role': loan['role'] or 'member'
                })
            
            if loans_list:
                sorted_by_interest = sorted(loans_list, key=lambda x: x['total_interest_paid'], reverse=True)
                if sorted_by_interest and sorted_by_interest[0]['total_interest_paid'] > 0:
                    top_interest = sorted_by_interest[0]
                    highest_interest_borrower = {
                        'name': top_interest['full_name'],
                        'interest': top_interest['total_interest_paid'],
                        'accrued': top_interest['total_interest_accrued'],
                        'role': top_interest['role'] or 'member'
                    }
        
        # Debug
        print("=" * 60)
        print(f"🔍 TREASURER DASHBOARD LOADED")
        print(f"📊 Total Active Users: {total_members}")
        print(f"📊 Regular Members: {total_regular_members}")
        print(f"📊 Staff Members: {total_staff_members}")
        print(f"📊 Recent Deposits: {len(recent_deposits)}")
        print(f"📊 Recent Repayments: {len(recent_repayments)}")
        print("=" * 60)
        
        conn.close()
        
        return render_template(
            "treasurer/treasurer-dashboard.html",
            # Members
            members=members,
            regular_members=regular_members,
            staff_members=staff_members,
            total_members=total_members,
            total_regular_members=total_regular_members,
            total_staff_members=total_staff_members,
            
            # Savings
            total_savings=total_savings,
            total_deposits=total_deposits,
            monthly_deposits=monthly_deposits,
            all_deposits=all_deposits,
            recent_deposits=recent_deposits,
            
            # Savings by type
            kai_total=kai_total,
            ks_total=ks_total,
            kac_total=kac_total,
            registration_fees_total=registration_fees_total,
            kai_members=kai_members,
            ks_members=ks_members,
            kac_members=kac_members,
            registration_fees_count=registration_fees_count,
            kai_shares=kai_shares,
            ks_shares=ks_shares,
            
            # Loans
            pending_loans=pending_loans,
            approved_loans=approved_loans,
            active_loans=active_loans,
            completed_loans=completed_loans,
            rejected_loans=rejected_loans,
            active_loans_list=active_loans_list,
            completed_loans_list=completed_loans_list,
            all_loan_applications=all_loan_applications,
            
            # Interest
            total_interest_accrued=total_interest_accrued,
            total_interest_paid=total_interest_paid,
            total_interest_outstanding=total_interest_outstanding,
            
            # Top borrowers
            highest_borrower=highest_borrower,
            highest_interest_borrower=highest_interest_borrower,
            total_loan_fees=total_loan_fees,
            
            # Recent activity
            recent_repayments=recent_repayments,
            
            now=datetime.now()
        )
        
    except Exception as e:
        conn.close()
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('login'))
    
# ============================================================
# TREASURER - SAVINGS DEPOSIT (WITH SAVINGS TYPES) - INCLUDES STAFF - FIXED
# ============================================================
# ============================================================
# TREASURER - SAVINGS DEPOSIT (WITH SAVINGS TYPES) - INCLUDES STAFF
# KAC SUPPORTS INSTALLMENTS UP TO 100,000
# ============================================================
@app.route("/treasurer/savings/deposit", methods=["GET", "POST"])
def treasurer_savings_deposit():
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied. Only treasurer can record deposits.', 'danger')
        return redirect("/login")
    
    if request.method == "POST":
        user_id = request.form.get('user_id')
        amount = float(request.form.get('amount', 0))
        savings_type = request.form.get('savings_type', 'KAI')
        deposit_date = request.form.get('deposit_date', datetime.now().strftime('%Y-%m-%d'))
        payment_method = request.form.get('payment_method', 'cash')
        receipt_number = request.form.get('receipt_number', '')
        notes = request.form.get('notes', '')
        
        if not user_id:
            flash('Please select a member or staff', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))
        
        if amount <= 0:
            flash('Amount must be greater than 0', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))
        
        # Get settings for share prices
        db = get_db()
        db.row_factory = sqlite3.Row
        
        # Verify user exists
        user = db.execute("SELECT id, role, full_name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.close()
            flash('User not found', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))
        
        settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            kai_share_price = settings['kai_share_price'] or 100000
            ks_share_price = settings['ks_share_price'] or 10000
            kac_annual_fee = settings['kac_annual_fee'] or 100000
            registration_fee = settings['registration_fee'] or 20000
        else:
            kai_share_price = 100000
            ks_share_price = 10000
            kac_annual_fee = 100000
            registration_fee = 20000
        
        # ============================================================
        # KAC VALIDATION — prevent overpayment beyond remaining balance
        # ============================================================
        if savings_type == 'KAC':
            current_row = db.execute(
                "SELECT COALESCE(kac_paid, 0) AS current_kac FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
            current_kac = float(current_row['current_kac'] or 0)
            remaining = max(0, kac_annual_fee - current_kac)

            if current_kac >= kac_annual_fee:
                db.close()
                flash(f'{user["full_name"]} has already fully paid KAC (UGX {kac_annual_fee:,.0f}).', 'warning')
                return redirect(url_for('treasurer_savings_deposit'))

            if amount > remaining:
                db.close()
                flash(
                    f'KAC payment exceeds remaining balance. '
                    f'Current: UGX {current_kac:,.0f} / {kac_annual_fee:,.0f} · '
                    f'Remaining: UGX {remaining:,.0f}',
                    'warning'
                )
                return redirect(url_for('treasurer_savings_deposit'))
        
        # Calculate shares based on savings type
        shares = 0
        if savings_type == 'KAI':
            shares = int(amount / kai_share_price) if kai_share_price > 0 else 0
        elif savings_type == 'KS':
            shares = int(amount / ks_share_price) if ks_share_price > 0 else 0
        elif savings_type == 'KAC':
            shares = 0  # KAC is installment-based, no shares
        elif savings_type == 'REGISTRATION':
            shares = 0  # Registration is one-time, no shares
        
        cursor = db.cursor()
        
        # Insert savings deposit with type
        cursor.execute("""
            INSERT INTO savings_deposits (
                user_id, amount, savings_type, shares, deposit_date, 
                payment_method, receipt_number, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, amount, savings_type, shares, deposit_date, payment_method, receipt_number, notes))
        
        # ============================================================
        # UPDATE USER'S SAVINGS BALANCE
        # ============================================================
        if savings_type == 'KAI':
            cursor.execute("""
                UPDATE users 
                SET savings_balance = COALESCE(savings_balance, 0) + ?,
                    kai_shares = COALESCE(kai_shares, 0) + ?
                WHERE id = ?
            """, (amount, shares, user_id))

        elif savings_type == 'KS':
            cursor.execute("""
                UPDATE users 
                SET savings_balance = COALESCE(savings_balance, 0) + ?,
                    ks_shares = COALESCE(ks_shares, 0) + ?
                WHERE id = ?
            """, (amount, shares, user_id))

        elif savings_type == 'KAC':
            # ============================================================
            # KAC — RUNNING TOTAL (installments up to 100,000)
            # ============================================================
            row = db.execute(
                "SELECT COALESCE(kac_paid, 0) AS current_kac FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
            current_kac = float(row['current_kac'] or 0)
            new_kac = current_kac + amount
            if new_kac > kac_annual_fee:
                new_kac = kac_annual_fee

            fully_paid = 1 if new_kac >= kac_annual_fee else 0
            paid_date_value = deposit_date if fully_paid else None

            cursor.execute("""
                UPDATE users 
                SET savings_balance = COALESCE(savings_balance, 0) + ?,
                    kac_paid = ?,
                    kac_paid_date = COALESCE(?, kac_paid_date)
                WHERE id = ?
            """, (amount, new_kac, paid_date_value, user_id))

            # Clear any pending top-up notifications once fully paid
            if fully_paid:
                try:
                    cursor.execute("""
                        UPDATE topup_notifications 
                        SET is_read = 1 
                        WHERE user_id = ? AND is_read = 0
                    """, (user_id,))
                except sqlite3.OperationalError:
                    # Table doesn't exist yet — ignore
                    pass

        elif savings_type == 'REGISTRATION':
            # REGISTRATION does NOT update savings_balance - only marks as paid
            cursor.execute("""
                UPDATE users 
                SET registration_fee_paid = 1,
                    registration_fee_paid_date = ?
                WHERE id = ?
            """, (deposit_date, user_id))
        
        db.commit()
        db.close()
        
        # Debug logging
        print("=" * 60)
        print(f"💰 DEPOSIT RECORDED")
        print(f"👤 User: {user['full_name']} ({user['role']})")
        print(f"📊 Type: {savings_type}")
        print(f"💵 Amount: UGX {amount:,.0f}")
        print(f"📈 Shares: {shares}")
        if savings_type == 'KAC':
            print(f"🎯 KAC total now: UGX {new_kac:,.0f} / {kac_annual_fee:,.0f}")
            if fully_paid:
                print(f"✅ KAC FULLY PAID")
        print("=" * 60)
        
        flash(f'✅ {savings_type} deposit of UGX {amount:,.0f} recorded successfully for {user["full_name"]}!', 'success')
        return redirect(url_for('treasurer_dashboard'))
    
    # ============================================================
    # GET request — show the form
    # ============================================================
    db = get_db()
    db.row_factory = sqlite3.Row
    
    # Get ALL active users (members + staff)
    all_users = db.execute("""
        SELECT 
            id,
            full_name,
            sacco_number,
            phone,
            email,
            role,
            status,
            kai_shares,
            ks_shares,
            kac_paid,
            registration_fee_paid
        FROM users 
        WHERE status = 'active'
        AND LOWER(role) IN ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
        ORDER BY 
            CASE 
                WHEN LOWER(role) = 'member' THEN 1
                WHEN LOWER(role) = 'admin' THEN 2
                WHEN LOWER(role) = 'chairperson' THEN 3
                WHEN LOWER(role) = 'treasurer' THEN 4
                WHEN LOWER(role) = 'secretary' THEN 5
                WHEN LOWER(role) = 'publicity' THEN 6
            END,
            full_name ASC
    """).fetchall()
    
    # Separate members and staff for the dropdown
    staff_members = []
    regular_members = []
    for user in all_users:
        if user['role'] != 'member':
            staff_members.append(user)
        else:
            regular_members.append(user)
    
    completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
    db.close()
    
    return render_template(
        "treasurer/savings-deposit.html", 
        members=all_users,
        staff_members=staff_members,
        regular_members=regular_members,
        completed_loans=completed_loans
    )
# ============================================================
# TREASURER - GET GUARANTOR DETAILS
# ============================================================
@app.route("/treasurer/guarantor/details/<int:guarantor_id>")
def get_guarantor_details(guarantor_id):
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        guarantor_info = db.execute("""
            SELECT 
                id,
                guarantor_name,
                phone,
                email,
                relationship,
                status
            FROM loan_guarantors 
            WHERE id = ?
        """, (guarantor_id,)).fetchone()
        
        if not guarantor_info:
            db.close()
            return jsonify({'success': False, 'message': 'Guarantor not found'}), 404
        
        user = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                phone,
                email,
                savings_balance,
                status
            FROM users 
            WHERE phone = ? OR email = ?
            LIMIT 1
        """, (guarantor_info['phone'], guarantor_info['email'])).fetchone()
        
        if user:
            guarantor_data = dict(user)
        else:
            guarantor_data = {
                'id': None,
                'full_name': guarantor_info['guarantor_name'],
                'sacco_number': 'N/A',
                'phone': guarantor_info['phone'],
                'email': guarantor_info['email'],
                'savings_balance': 0,
                'status': 'guest'
            }
        
        guaranteed_loans = db.execute("""
            SELECT 
                l.id,
                l.loan_number,
                l.amount,
                l.status,
                l.application_date,
                l.current_balance,
                l.total_repayment,
                u.full_name as member_name,
                u.sacco_number as member_sacco
            FROM loan_guarantors lg
            JOIN loans l ON lg.loan_id = l.id
            JOIN users u ON l.user_id = u.id
            WHERE lg.guarantor_name = ? 
            AND lg.phone = ?
            AND lg.status IN ('active', 'accepted')
            ORDER BY l.application_date DESC
        """, (guarantor_info['guarantor_name'], guarantor_info['phone'])).fetchall()
        
        db.close()
        
        return jsonify({
            'success': True,
            'guarantor': guarantor_data,
            'guaranteed_loans': [dict(loan) for loan in guaranteed_loans]
        })
        
    except Exception as e:
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# ADMIN - VIEW LOAN DETAILS
# ============================================================
@app.route("/admin/loan/view/<int:loan_id>")
def admin_view_loan(loan_id):
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        loan = db.execute("""
            SELECT l.*, u.full_name, u.sacco_number, u.email, u.phone, u.savings_balance
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            flash('Loan not found', 'danger')
            db.close()
            return redirect(url_for('admin_dashboard'))
        
        guarantors = db.execute("""
            SELECT * FROM loan_guarantors WHERE loan_id = ?
        """, (loan_id,)).fetchall()
        
        repayments = db.execute("""
            SELECT * FROM repayments WHERE loan_id = ? ORDER BY payment_date DESC
        """, (loan_id,)).fetchall()
        
        total_paid = sum(r['amount'] for r in repayments) if repayments else 0
        
        db.close()
        
        return render_template(
            "treasurer/treasurer-view-loan.html",
            loan=loan,
            guarantors=guarantors,
            repayments=repayments,
            total_paid=total_paid,
            role='admin'
        )
        
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))


# ============================================================
# TREASURER - VIEW LOAN DETAILS
# ============================================================
@app.route("/treasurer/loan/view/<int:loan_id>")
def treasurer_view_loan(loan_id):
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        loan = db.execute("""
            SELECT l.*, u.full_name, u.sacco_number, u.email, u.phone, u.savings_balance
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            flash('Loan not found', 'danger')
            db.close()
            # Redirect based on role
            role = session.get('role')
            if role == 'admin' or role == 'chairperson':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('treasurer_dashboard'))
        
        guarantors = db.execute("""
            SELECT * FROM loan_guarantors WHERE loan_id = ?
        """, (loan_id,)).fetchall()
        
        repayments = db.execute("""
            SELECT * FROM repayments WHERE loan_id = ? ORDER BY payment_date DESC
        """, (loan_id,)).fetchall()
        
        total_paid = sum(r['amount'] for r in repayments) if repayments else 0
        
        db.close()
        
        return render_template(
            "treasurer/treasurer-view-loan.html",
            loan=loan,
            guarantors=guarantors,
            repayments=repayments,
            total_paid=total_paid,
            role=session.get('role', 'treasurer')
        )
        
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        # Redirect based on role
        role = session.get('role')
        if role == 'admin' or role == 'chairperson':
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('treasurer_dashboard'))


# ============================================================
# TREASURER - APPROVE / REJECT LOAN
# ============================================================
@app.route("/treasurer/loan/action/<int:loan_id>", methods=["POST"])
def treasurer_approve_loan(loan_id):
    print(f"🔍 Loan action called for loan {loan_id}")

    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Please login first'}), 401

    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data = request.get_json(silent=True)

    if not data:
        return jsonify({'success': False, 'message': 'Invalid request'}), 400

    action = data.get('action')
    reason = (data.get('reason') or '').strip()

    if action not in ['approve', 'reject']:
        return jsonify({'success': False, 'message': 'Invalid action'}), 400

    db = get_db()
    db.row_factory = sqlite3.Row

    try:
        loan = db.execute("""
            SELECT
                l.*,
                u.id AS applicant_id,
                u.savings_balance,
                u.full_name,
                u.email,
                u.phone
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()

        if not loan:
            db.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404

        # REJECT LOAN
        if action == 'reject':
            if not reason:
                db.close()
                return jsonify({'success': False, 'message': 'Rejection reason required'}), 400

            db.execute("""
                UPDATE loans
                SET
                    status = 'rejected',
                    rejected_date = datetime('now'),
                    rejection_reason = ?,
                    rejected_by = ?
                WHERE id = ?
            """, (reason, session.get('full_name', 'Treasurer'), loan_id))

            db.execute("""
                INSERT INTO notifications (
                    user_id,
                    title,
                    message,
                    notification_type,
                    is_read,
                    created_at
                )
                VALUES (?, ?, ?, ?, 0, datetime('now'))
            """, (
                loan['applicant_id'],
                'Loan Application Rejected',
                f"Your loan application {loan['loan_number']} has been rejected.\n\nReason: {reason}",
                'loan_rejection'
            ))

            db.commit()
            db.close()

            return jsonify({
                'success': True,
                'message': 'Loan rejected successfully. The applicant has been notified.'
            })

        # APPROVE LOAN
        if loan['status'] != 'pending':
            db.close()
            return jsonify({
                'success': False,
                'message': f'Loan is {loan["status"]}, not pending'
            }), 400

        required = float(loan['amount']) * 0.10
        savings = float(loan['savings_balance'] or 0)

        if savings < required:
            db.close()
            return jsonify({
                'success': False,
                'message': f'Member needs 10% savings (UGX {required:,.0f}). Current: UGX {savings:,.0f}'
            }), 400

        today = datetime.now().strftime('%Y-%m-%d')
        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        balance = loan['total_repayment'] if loan['total_repayment'] is not None else loan['amount']

        db.execute("""
            UPDATE loans
            SET
                status = 'approved',
                approved_date = ?,
                loan_start_date = ?,
                loan_end_date = ?,
                current_balance = ?,
                approved_by = ?,
                approved_by_role = 'treasurer'
            WHERE id = ?
        """, (today, today, end_date, balance, session.get('full_name', 'Treasurer'), loan_id))

        db.execute("""
            INSERT INTO notifications (
                user_id,
                title,
                message,
                notification_type,
                is_read,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, datetime('now'))
        """, (
            loan['applicant_id'],
            'Loan Application Approved',
            f"Your loan application {loan['loan_number']} has been approved. Please wait for disbursement.",
            'loan_approval'
        ))

        db.commit()
        db.close()

        return jsonify({
            'success': True,
            'message': '✅ Loan approved! Waiting for Chairman disbursement.'
        })

    except Exception as e:
        print(f"❌ Error processing loan action: {e}")
        try:
            db.rollback()
            db.close()
        except:
            pass
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# TREASURER - DISBURSE LOAN
# ============================================================
@app.route("/treasurer/loan/disburse/<int:loan_id>", methods=["POST"])
def treasurer_disburse_loan(loan_id):
    print(f"💰 Disburse called for loan {loan_id}")
    
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Please login first'}), 401
    
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
        
        if not loan:
            db.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404
        
        if loan['status'] != 'approved':
            db.close()
            return jsonify({'success': False, 'message': f'Loan must be approved first. Status: {loan["status"]}'}), 400
        
        today = datetime.now().strftime('%Y-%m-%d')
        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        balance = loan['total_repayment'] or loan['amount']
        
        db.execute("""
            UPDATE loans 
            SET status = 'disbursed',
                disbursement_date = ?,
                loan_start_date = ?,
                loan_end_date = ?,
                due_date = ?,
                current_balance = ?,
                disbursed_by = ?,
                disbursed_by_role = ?
            WHERE id = ?
        """, (today, today, end_date, end_date, balance, session.get('full_name', 'Treasurer'), session.get('role', 'treasurer'), loan_id))
        
        db.commit()
        db.close()
        
        return jsonify({
            'success': True,
            'message': '💰 Loan disbursed successfully!'
        })
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# TREASURER - RECORD PAYMENT (FIXED & ENHANCED)
# ============================================================
@app.route("/treasurer/loan/pay", methods=['POST'])
def treasurer_record_payment():
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    loan_id = data.get('loan_id')
    amount_str = data.get('amount', 0)
    payment_method = data.get('payment_method', 'cash')
    
    try:
        if isinstance(amount_str, str):
            amount = float(amount_str.replace(',', ''))
        else:
            amount = float(amount_str)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Invalid amount format'}), 400
    
    if not loan_id or amount <= 0:
        return jsonify({'success': False, 'message': 'Loan ID and valid amount are required'}), 400
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        loan = conn.execute("""
            SELECT l.*, u.full_name, u.sacco_number, u.id as member_id
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            conn.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404
        
        if loan['status'] not in ['approved', 'disbursed', 'active']:
            conn.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot make payment on loan with status: {loan["status"]}'
            }), 400
        
        if loan['status'] == 'completed':
            conn.close()
            return jsonify({'success': False, 'message': 'Loan is already fully paid'}), 400
        
        current_balance = float(loan['current_balance'] or loan['amount'] or 0)
        
        if amount > current_balance:
            conn.close()
            return jsonify({
                'success': False, 
                'message': f'Payment amount (UGX {amount:,.0f}) exceeds current balance (UGX {current_balance:,.0f})'
            }), 400
        
        conn.execute("BEGIN TRANSACTION")
        
        # Calculate interest and principal
        total_interest = float(loan['total_interest_accrued'] or 0)
        interest_paid_so_far = float(loan['interest_paid'] or 0)
        interest_remaining = max(0, total_interest - interest_paid_so_far)
        
        if amount >= interest_remaining:
            interest_paid = interest_remaining
            principal_paid = amount - interest_remaining
        else:
            interest_paid = amount
            principal_paid = 0
        
        # Record the repayment
        conn.execute("""
            INSERT INTO repayments (
                loan_id, user_id, amount, interest_paid, principal_paid,
                balance_after, payment_date, payment_method, status
            )
            VALUES (?, ?, ?, ?, ?, ?, date('now'), ?, 'completed')
        """, (loan_id, loan['member_id'], amount, interest_paid, principal_paid, current_balance - amount, payment_method))
        
        new_balance = current_balance - amount
        COMPLETION_THRESHOLD = 50
        is_completed = new_balance <= COMPLETION_THRESHOLD
        
        if is_completed:
            conn.execute("""
                UPDATE loans 
                SET current_balance = 0,
                    status = 'completed',
                    completed_date = date('now'),
                    last_payment_date = date('now'),
                    last_payment_amount = ?,
                    interest_paid = COALESCE(interest_paid, 0) + ?,
                    principal_paid = COALESCE(principal_paid, 0) + ?
                WHERE id = ?
            """, (amount, interest_paid, principal_paid, loan_id))
            status = 'completed'
            message = f'✅ LOAN COMPLETED! Final payment of UGX {amount:,.0f} made.'
        else:
            conn.execute("""
                UPDATE loans 
                SET current_balance = ?,
                    status = 'active',
                    last_payment_date = date('now'),
                    last_payment_amount = ?,
                    interest_paid = COALESCE(interest_paid, 0) + ?,
                    principal_paid = COALESCE(principal_paid, 0) + ?
                WHERE id = ?
            """, (new_balance, amount, interest_paid, principal_paid, loan_id))
            status = 'active'
            message = f'✅ Payment of UGX {amount:,.0f} recorded successfully! Remaining: UGX {new_balance:,.0f}'
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': message,
            'new_balance': new_balance,
            'status': status,
            'is_completed': is_completed,
            'interest_paid': interest_paid,
            'principal_paid': principal_paid
        })
        
    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        print(f"❌ Database Error in payment: {str(e)}")
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"❌ Error in payment: {str(e)}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


# ============================================================
# TREASURER - ENTER REPAYMENT
# ============================================================
@app.route("/treasurer/repayment/enter", methods=["GET", "POST"])
def treasurer_enter_repayment():
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        flash('Access denied. Only treasurer can enter repayments.', 'danger')
        return redirect("/login")
    
    if request.method == "GET":
        db = get_db()
        db.row_factory = sqlite3.Row
        
        active_loans = db.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number,
                u.phone
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status IN ('approved', 'disbursed', 'active')
            ORDER BY l.application_date DESC
        """).fetchall()
        
        completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
        
        db.close()
        
        return render_template(
            "treasurer/enter-repayment.html", 
            active_loans=active_loans,
            completed_loans=completed_loans
        )
    
    # POST - Process repayment
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        loan_id = int(request.form.get('loan_id'))
        amount = float(request.form.get('amount', 0))
        payment_method = request.form.get('payment_method', 'cash')
        transaction_ref = request.form.get('transaction_ref', '')
        notes = request.form.get('notes', '')
        
        if amount <= 0:
            flash('Amount must be greater than 0', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        loan = db.execute("""
            SELECT 
                l.*, 
                u.id as member_id, 
                u.full_name, 
                u.sacco_number
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            flash('Loan not found', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        if loan['status'] not in ['approved', 'disbursed', 'active']:
            flash(f'Cannot make payment on loan with status: {loan["status"]}', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        if loan['status'] == 'completed':
            flash('Loan is already fully paid', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        current_balance = float(loan['current_balance'] if loan['current_balance'] is not None else loan['amount'] or 0)
        
        if amount > current_balance:
            flash(f'Payment amount (UGX {amount:,.0f}) exceeds current balance (UGX {current_balance:,.0f})', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        # Calculate interest and principal
        interest_paid = 0
        principal_paid = 0
        
        total_interest = float(loan['total_interest_accrued'] or 0)
        interest_paid_so_far = float(loan['interest_paid'] if loan['interest_paid'] is not None else 0)
        interest_remaining = max(0, total_interest - interest_paid_so_far)
        
        if amount >= interest_remaining:
            interest_paid = interest_remaining
            principal_paid = amount - interest_remaining
        else:
            interest_paid = amount
            principal_paid = 0
        
        new_balance = current_balance - amount
        if new_balance < 0:
            new_balance = 0
        
        db.execute("BEGIN TRANSACTION")
        
        current_datetime = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        db.execute("""
            INSERT INTO repayments (
                loan_id, user_id, amount, interest_paid, principal_paid,
                balance_after, payment_date, payment_method, transaction_ref, notes,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
        """, (
            loan_id, 
            loan['member_id'], 
            amount, 
            interest_paid, 
            principal_paid,
            new_balance,
            current_datetime, 
            payment_method, 
            transaction_ref,
            notes
        ))
        
        COMPLETION_THRESHOLD = 50
        is_completed = new_balance <= COMPLETION_THRESHOLD
        
        if is_completed:
            db.execute("""
                UPDATE loans 
                SET current_balance = 0,
                    status = 'completed',
                    completed_date = ?,
                    last_payment_date = ?,
                    last_payment_amount = ?,
                    interest_paid = COALESCE(interest_paid, 0) + ?,
                    principal_paid = COALESCE(principal_paid, 0) + ?
                WHERE id = ?
            """, (
                current_datetime,
                current_datetime,
                amount,
                interest_paid,
                principal_paid,
                loan_id
            ))
            db.commit()
            db.close()
            flash(f'✅ LOAN COMPLETED! Final payment of UGX {amount:,.0f} made.', 'success')
        else:
            db.execute("""
                UPDATE loans 
                SET current_balance = ?,
                    status = 'active',
                    last_payment_date = ?,
                    last_payment_amount = ?,
                    interest_paid = COALESCE(interest_paid, 0) + ?,
                    principal_paid = COALESCE(principal_paid, 0) + ?
                WHERE id = ?
            """, (
                new_balance,
                current_datetime,
                amount,
                interest_paid,
                principal_paid,
                loan_id
            ))
            db.commit()
            db.close()
            flash(f'✅ Payment of UGX {amount:,.0f} recorded successfully!', 'success')
            flash(f'📊 Interest paid: UGX {interest_paid:,.0f} | Principal paid: UGX {principal_paid:,.0f}', 'info')
            flash(f'💰 Remaining balance: UGX {new_balance:,.0f}', 'info')
        
        return redirect(url_for('treasurer_dashboard'))
        
    except Exception as e:
        db.rollback()
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_enter_repayment'))


# ============================================================
# TREASURER - ADD MEMBER (WITH SAVINGS TYPE SUPPORT) - INCLUDES STAFF
# ============================================================
@app.route("/treasurer/members/add", methods=["GET", "POST"])
def treasurer_add_members():
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied. Only Treasurer, Admin, or Secretary can register users.', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
    
    if request.method == "POST":
        full_name = request.form.get('full_name', '').strip()
        gender = request.form.get('gender', '')
        dob = request.form.get('dob', '')
        sacco_number = request.form.get('sacco_number', '').strip().upper()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()
        password = request.form.get('password', 'password123').strip()
        role = request.form.get('role', 'member')
        status = request.form.get('status', 'active')
        savings_balance = float(request.form.get('savings_balance', 0) or 0)
        next_of_kin_name = request.form.get('next_of_kin_name', '').strip()
        relationship = request.form.get('relationship', '')
        next_of_kin_phone = request.form.get('next_of_kin_phone', '').strip()
        
        # Savings type specific fields
        kai_shares = int(request.form.get('kai_shares', 0) or 0)
        ks_shares = int(request.form.get('ks_shares', 0) or 0)
        kac_paid = 1 if request.form.get('kac_paid') == 'on' else 0
        registration_fee_paid = 1 if request.form.get('registration_fee_paid') == 'on' else 0
        
        errors = []
        if not full_name:
            errors.append('Full name is required')
        if not sacco_number:
            errors.append('SACCO number is required')
        if not phone:
            errors.append('Phone number is required')
        if not dob:
            errors.append('Date of birth is required')
        if not next_of_kin_name:
            errors.append('Next of kin name is required')
        if not next_of_kin_phone:
            errors.append('Next of kin phone is required')
        
        if errors:
            for error in errors:
                flash(error, 'danger')
            db.close()
            return render_template("treasurer/add-member.html", completed_loans=completed_loans)
        
        # Check if SACCO number exists
        existing = db.execute("SELECT id FROM users WHERE sacco_number = ?", (sacco_number,)).fetchone()
        if existing:
            flash(f'SACCO number "{sacco_number}" already exists!', 'danger')
            db.close()
            return render_template("treasurer/add-member.html", completed_loans=completed_loans)
        
        if email:
            existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                flash(f'Email "{email}" is already registered!', 'danger')
                db.close()
                return render_template("treasurer/add-member.html", completed_loans=completed_loans)
        
        existing = db.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            flash(f'Phone number "{phone}" is already registered!', 'danger')
            db.close()
            return render_template("treasurer/add-member.html", completed_loans=completed_loans)
        
        try:
            cursor = db.cursor()
            
            # Get settings for share prices
            settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
            if settings:
                kai_share_price = settings['kai_share_price'] or 100000
                ks_share_price = settings['ks_share_price'] or 10000
                kac_annual_fee = settings['kac_annual_fee'] or 100000
                registration_fee = settings['registration_fee'] or 20000
            else:
                kai_share_price = 100000
                ks_share_price = 10000
                kac_annual_fee = 100000
                registration_fee = 20000
            
            # Calculate total savings from shares
            total_savings_from_shares = (kai_shares * kai_share_price) + (ks_shares * ks_share_price)
            if kac_paid:
                total_savings_from_shares += kac_annual_fee
            if registration_fee_paid:
                total_savings_from_shares += registration_fee
            
            # Use the provided savings_balance or calculate from shares
            final_savings_balance = savings_balance if savings_balance > 0 else total_savings_from_shares
            
            # ============================================================
            # INSERT USER (ALLOW STAFF ROLES)
            # ============================================================
            cursor.execute("""
                INSERT INTO users (
                    full_name, gender, dob, sacco_number,
                    email, phone, address,
                    password, role, status,
                    savings_balance,
                    next_of_kin_name, relationship, next_of_kin_phone,
                    kai_shares, ks_shares, kac_paid, registration_fee_paid
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                full_name, gender, dob, sacco_number,
                email, phone, address,
                password, role, status,
                final_savings_balance,
                next_of_kin_name, relationship, next_of_kin_phone,
                kai_shares, ks_shares, kac_paid, registration_fee_paid
            ))
            
            user_id = cursor.lastrowid
            
            # ============================================================
            # RECORD SAVINGS DEPOSITS FOR EACH TYPE
            # ============================================================
            today = datetime.now().strftime('%Y-%m-%d')
            
            if kai_shares > 0:
                cursor.execute("""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date, 
                        payment_method, receipt_number, notes
                    )
                    VALUES (?, ?, 'KAI', ?, ?, 'registration', ?, ?)
                """, (user_id, kai_shares * kai_share_price, kai_shares, today, f'REG-KAI-{sacco_number}', f'Initial KAI shares for {full_name}'))
            
            if ks_shares > 0:
                cursor.execute("""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date, 
                        payment_method, receipt_number, notes
                    )
                    VALUES (?, ?, 'KS', ?, ?, 'registration', ?, ?)
                """, (user_id, ks_shares * ks_share_price, ks_shares, today, f'REG-KS-{sacco_number}', f'Initial KS shares for {full_name}'))
            
            if kac_paid:
                cursor.execute("""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date, 
                        payment_method, receipt_number, notes
                    )
                    VALUES (?, ?, 'KAC', 1, ?, 'registration', ?, ?)
                """, (user_id, kac_annual_fee, today, f'REG-KAC-{sacco_number}', f'KAC payment for {full_name}'))
            
            if registration_fee_paid:
                cursor.execute("""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date, 
                        payment_method, receipt_number, notes
                    )
                    VALUES (?, ?, 'REGISTRATION', 1, ?, 'registration', ?, ?)
                """, (user_id, registration_fee, today, f'REG-REG-{sacco_number}', f'Registration fee for {full_name}'))
            
            db.commit()
            db.close()
            
            # Debug
            print("=" * 60)
            print(f"✅ USER REGISTERED: {full_name} ({role})")
            print(f"📊 KAI: {kai_shares} shares (UGX {kai_shares * kai_share_price:,.0f})")
            print(f"📊 KS: {ks_shares} shares (UGX {ks_shares * ks_share_price:,.0f})")
            print(f"📊 KAC: {'Paid' if kac_paid else 'Not paid'}")
            print(f"📊 Registration: {'Paid' if registration_fee_paid else 'Not paid'}")
            print(f"💰 Total Savings: UGX {final_savings_balance:,.0f}")
            print("=" * 60)
            
            flash(f'✅ {role.title()} "{full_name}" registered successfully with all savings types!', 'success')
            return redirect(url_for('treasurer_dashboard'))
            
        except Exception as e:
            db.rollback()
            db.close()
            flash(f'Error registering user: {str(e)}', 'danger')
            return render_template("treasurer/add-member.html", completed_loans=completed_loans)
    
    db.close()
    return render_template("treasurer/add-member.html", completed_loans=completed_loans)

# ============================================================
# TREASURER - VIEW USER DETAILS (HTML Page) - WORKS FOR ALL
# ============================================================
@app.route("/treasurer/members/view/<int:user_id>")
def treasurer_member_details(user_id):
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get ANY user (member OR staff) - removed role filter
        user = db.execute("""
            SELECT * FROM users WHERE id = ?
        """, (user_id,)).fetchone()
        
        if not user:
            flash('User not found', 'danger')
            return redirect(url_for('treasurer_dashboard'))
        
        loans = db.execute("""
            SELECT * FROM loans WHERE user_id = ? ORDER BY application_date DESC
        """, (user_id,)).fetchall()
        
        deposits = db.execute("""
            SELECT * FROM savings_deposits WHERE user_id = ? ORDER BY deposit_date DESC
        """, (user_id,)).fetchall()
        
        repayments = db.execute("""
            SELECT r.*, l.loan_number 
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            WHERE r.user_id = ?
            ORDER BY r.payment_date DESC
        """, (user_id,)).fetchall()
        
        completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
        
        db.close()
        
        return render_template(
            "treasurer/member-details.html",
            member=user,
            loans=loans,
            deposits=deposits,
            repayments=repayments,
            completed_loans=completed_loans
        )
        
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard'))


# ============================================================
# TREASURER - VIEW USER (JSON for Edit Modal) - FULL DATA
# ============================================================
@app.route("/treasurer/member/view/<int:user_id>")
def treasurer_member_view_json(user_id):
    """Return complete user data as JSON for AJAX calls"""
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get ALL user data including all registration fields
        user = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                phone,
                email,
                gender,
                dob,
                address,
                savings_balance,
                status,
                registration_date,
                role,
                next_of_kin_name,
                next_of_kin_phone,
                relationship,
                kai_shares,
                ks_shares,
                kac_paid,
                registration_fee_paid
            FROM users 
            WHERE id = ?
        """, (user_id,)).fetchone()
        
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        db.close()
        
        return jsonify({
            'success': True,
            'member': dict(user)
        })
        
    except Exception as e:
        db.close()
        print(f"❌ Error getting user: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# TREASURER - UPDATE USER (Member or Staff) - WORKS FOR ALL
# ============================================================
# ============================================================
# TREASURER - UPDATE USER (ALL FIELDS) - WITH DEPOSIT RECORDS
# ============================================================
@app.route("/treasurer/member/update/<int:user_id>", methods=["POST"])
def treasurer_update_member(user_id):
    """Update ANY user (member OR staff) with ALL fields and create deposit records"""
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Check if user exists
        user = db.execute("SELECT id, role, full_name, sacco_number FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Prevent editing admin by non-admin users
        if user['role'] == 'admin' and session.get('role') != 'admin':
            db.close()
            return jsonify({'success': False, 'message': 'Only Admin can edit another Admin'}), 403
        
        # Validate unique fields
        phone = data.get('phone', '').strip()
        if phone:
            existing = db.execute("SELECT id FROM users WHERE phone = ? AND id != ?", (phone, user_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Phone number already in use'}), 400
        
        email = data.get('email', '').strip()
        if email:
            existing = db.execute("SELECT id FROM users WHERE email = ? AND id != ? AND email != ''", (email, user_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Email already in use'}), 400
        
        sacco_number = data.get('sacco_number', '').strip().upper()
        if sacco_number:
            existing = db.execute("SELECT id FROM users WHERE sacco_number = ? AND id != ?", (sacco_number, user_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'SACCO number already in use'}), 400
        
        # Get settings for share prices
        settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            kai_share_price = settings['kai_share_price'] or 100000
            ks_share_price = settings['ks_share_price'] or 10000
            kac_annual_fee = settings['kac_annual_fee'] or 100000
            registration_fee = settings['registration_fee'] or 20000
        else:
            kai_share_price = 100000
            ks_share_price = 10000
            kac_annual_fee = 100000
            registration_fee = 20000
        
        # Get existing shares
        existing_user = db.execute("""
            SELECT kai_shares, ks_shares, kac_paid, registration_fee_paid, savings_balance
            FROM users WHERE id = ?
        """, (user_id,)).fetchone()
        
        # Get new shares from form
        new_kai_shares = int(data.get('kai_shares', existing_user['kai_shares'] or 0))
        new_ks_shares = int(data.get('ks_shares', existing_user['ks_shares'] or 0))
        new_kac_paid = 1 if data.get('kac_paid') else 0
        new_reg_paid = 1 if data.get('registration_fee_paid') else 0
        
        # Calculate differences
        diff_kai_shares = new_kai_shares - (existing_user['kai_shares'] or 0)
        diff_ks_shares = new_ks_shares - (existing_user['ks_shares'] or 0)
        diff_kac = new_kac_paid - (existing_user['kac_paid'] or 0)
        diff_reg = new_reg_paid - (existing_user['registration_fee_paid'] or 0)
        
        # Calculate total savings from shares
        total_savings = (new_kai_shares * kai_share_price) + (new_ks_shares * ks_share_price)
        if new_kac_paid:
            total_savings += kac_annual_fee
        if new_reg_paid:
            total_savings += registration_fee
        
        # ============================================================
        # UPDATE USER
        # ============================================================
        db.execute("""
            UPDATE users 
            SET 
                full_name = ?,
                sacco_number = ?,
                phone = ?,
                email = ?,
                gender = ?,
                dob = ?,
                address = ?,
                next_of_kin_name = ?,
                next_of_kin_phone = ?,
                relationship = ?,
                status = ?,
                savings_balance = ?,
                kai_shares = ?,
                ks_shares = ?,
                kac_paid = ?,
                registration_fee_paid = ?
            WHERE id = ?
        """, (
            data.get('full_name', '').strip(),
            sacco_number,
            phone,
            email,
            data.get('gender', ''),
            data.get('dob', ''),
            data.get('address', ''),
            data.get('next_of_kin_name', ''),
            data.get('next_of_kin_phone', ''),
            data.get('relationship', ''),
            data.get('status', 'active'),
            total_savings,
            new_kai_shares,
            new_ks_shares,
            new_kac_paid,
            new_reg_paid,
            user_id
        ))
        
        # ============================================================
        # CREATE DEPOSIT RECORDS FOR ANY CHANGES
        # ============================================================
        today = datetime.now().strftime('%Y-%m-%d')
        
        # KAI Shares - If increased, record the difference as a deposit
        if diff_kai_shares > 0:
            kai_amount = diff_kai_shares * kai_share_price
            db.execute("""
                INSERT INTO savings_deposits (user_id, amount, savings_type, shares, deposit_date, payment_method, receipt_number, notes)
                VALUES (?, ?, 'KAI', ?, ?, 'edit', ?, 'KAI shares added via edit')
            """, (user_id, kai_amount, diff_kai_shares, today, f'EDIT-KAI-{user["sacco_number"]}'))
        
        # KS Shares - If increased, record the difference as a deposit
        if diff_ks_shares > 0:
            ks_amount = diff_ks_shares * ks_share_price
            db.execute("""
                INSERT INTO savings_deposits (user_id, amount, savings_type, shares, deposit_date, payment_method, receipt_number, notes)
                VALUES (?, ?, 'KS', ?, ?, 'edit', ?, 'KS shares added via edit')
            """, (user_id, ks_amount, diff_ks_shares, today, f'EDIT-KS-{user["sacco_number"]}'))
        
        # KAC - If newly paid, record as a deposit
        if diff_kac > 0:
            db.execute("""
                INSERT INTO savings_deposits (user_id, amount, savings_type, shares, deposit_date, payment_method, receipt_number, notes)
                VALUES (?, ?, 'KAC', 1, ?, 'edit', ?, 'KAC payment added via edit')
            """, (user_id, kac_annual_fee, today, f'EDIT-KAC-{user["sacco_number"]}'))
        
        # Registration Fee - If newly paid, record as a deposit (if you want it to count as savings)
        # If Registration should NOT count as savings, comment this out
        if diff_reg > 0:
            db.execute("""
                INSERT INTO savings_deposits (user_id, amount, savings_type, shares, deposit_date, payment_method, receipt_number, notes)
                VALUES (?, ?, 'REGISTRATION', 1, ?, 'edit', ?, 'Registration fee added via edit')
            """, (user_id, registration_fee, today, f'EDIT-REG-{user["sacco_number"]}'))
        
        db.commit()
        db.close()
        
        # Debug - print to console
        print("=" * 60)
        print(f"👤 USER UPDATED: {user['full_name']} ({user['role']})")
        print(f"📊 KAI: +{diff_kai_shares} shares (UGX {diff_kai_shares * kai_share_price:,.0f})")
        print(f"📊 KS: +{diff_ks_shares} shares (UGX {diff_ks_shares * ks_share_price:,.0f})")
        print(f"📊 KAC: {'Added' if diff_kac > 0 else 'No change'}")
        print(f"📊 Registration: {'Added' if diff_reg > 0 else 'No change'}")
        print(f"💰 Total Savings: UGX {total_savings:,.0f}")
        print("=" * 60)
        
        return jsonify({'success': True, 'message': 'User updated successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        print(f"Error updating user: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# TREASURER - DELETE USER (Member or Staff) - WORKS FOR ALL
# ============================================================
@app.route("/treasurer/member/delete/<int:user_id>", methods=["DELETE"])
def treasurer_delete_member(user_id):
    """Delete ANY user (member OR staff)"""
    if session.get("role") not in ["treasurer", "admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    
    try:
        # Get ANY user (member OR staff)
        user = db.execute("""
            SELECT id, full_name, role, savings_balance FROM users WHERE id = ?
        """, (user_id,)).fetchone()
        
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Prevent deleting self
        if user_id == session.get('user_id'):
            db.close()
            return jsonify({'success': False, 'message': 'You cannot delete your own account'}), 400
        
        # Prevent deleting the last admin
        if user['role'] == 'admin':
            admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                db.close()
                return jsonify({'success': False, 'message': 'Cannot delete the last admin user'}), 400
        
        # Check for active loans
        active_loans = db.execute("""
            SELECT COUNT(*) as count FROM loans 
            WHERE user_id = ? AND status IN ('pending', 'approved', 'disbursed', 'active')
        """, (user_id,)).fetchone()[0]
        
        if active_loans > 0:
            db.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot delete user with {active_loans} active loan(s)'
            }), 400
        
        # If user has savings, warn but allow deletion (admin only)
        if user['savings_balance'] > 0 and session.get('role') != 'admin':
            db.close()
            return jsonify({
                'success': False, 
                'message': f'User has savings balance of UGX {user["savings_balance"]:,.0f}. Only Admin can delete.'
            }), 400
        
        # Delete the user
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': f'User "{user["full_name"]}" deleted successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        print(f"Error deleting user: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# TREASURER - GET USER FOR EDIT (Alias) - WORKS FOR ALL
# ============================================================
@app.route("/treasurer/user/view/<int:user_id>")
def treasurer_user_view_json(user_id):
    """Alias for treasurer_member_view_json - works for all users"""
    return treasurer_member_view_json(user_id)


# ============================================================
# TREASURER - UPDATE USER (Alias) - WORKS FOR ALL
# ============================================================
@app.route("/treasurer/user/update/<int:user_id>", methods=["POST"])
def treasurer_user_update(user_id):
    """Alias for treasurer_update_member - works for all users"""
    return treasurer_update_member(user_id)


# ============================================================
# TREASURER - DELETE USER (Alias) - WORKS FOR ALL
# ============================================================
@app.route("/treasurer/user/delete/<int:user_id>", methods=["DELETE"])
def treasurer_user_delete(user_id):
    """Alias for treasurer_delete_member - works for all users"""
    return treasurer_delete_member(user_id)


# ============================================================
# MEMBER DASHBOARD - UPDATED FOR STAFF ACCESS
# ============================================================
@app.route("/member/dashboard")
def member_dashboard():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    user_role = session.get("role", "member")
    is_staff_view = request.args.get('staff_view', False)
    
    db = get_db()
    db.row_factory = sqlite3.Row

    try:
        member = db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not member:
            db.close()
            flash("Member not found", "danger")
            return redirect(url_for("login"))

        # ============================================================
        # TOTAL SAVINGS — KAI + KS only (exclude KAC & Registration)
        # ============================================================
        total_savings = db.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM savings_deposits
            WHERE user_id = ?
              AND savings_type IN ('KAI', 'KS')
        """, (user_id,)).fetchone()['total']

        loans_data = db.execute("""
            SELECT
                l.*,
                COALESCE((
                    SELECT SUM(amount)
                    FROM repayments
                    WHERE loan_id = l.id
                    AND status = 'completed'
                ), 0) as total_paid
            FROM loans l
            WHERE l.user_id = ?
            ORDER BY l.application_date DESC
        """, (user_id,)).fetchall()

        loans = []
        active_loans_count = 0
        active_loans_balance = 0
        total_loans_taken = 0

        for loan_row in loans_data:
            loan = dict(loan_row)
            total_loans_taken += float(loan.get('amount', 0) or 0)
            loan_total = float(loan.get('total_repayment') or loan.get('amount', 0) or 0)
            total_paid = float(loan.get('total_paid', 0) or 0)
            remaining_balance = max(0, loan_total - total_paid)
            loan['remaining_balance'] = remaining_balance

            if loan.get('status') in ['approved', 'disbursed', 'active']:
                active_loans_count += 1
                active_loans_balance += remaining_balance

            loans.append(loan)

        savings_deposits = db.execute("""
            SELECT
                id,
                amount,
                savings_type,
                shares,
                deposit_date,
                payment_method,
                receipt_number,
                notes,
                created_at
            FROM savings_deposits
            WHERE user_id = ?
            ORDER BY deposit_date DESC
        """, (user_id,)).fetchall()

        repayments = db.execute("""
            SELECT
                r.id,
                r.loan_id,
                r.user_id,
                r.amount,
                r.interest_paid,
                r.principal_paid,
                r.balance_after,
                r.payment_date,
                r.payment_method,
                r.transaction_ref,
                r.status,
                r.created_at,
                l.loan_number
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            WHERE l.user_id = ?
            ORDER BY r.payment_date DESC
        """, (user_id,)).fetchall()

        guarantors = db.execute("""
            SELECT
                lg.id,
                lg.loan_id,
                lg.guarantor_name,
                lg.phone,
                lg.email,
                lg.relationship,
                lg.status,
                lg.created_at,
                l.loan_number,
                l.amount,
                l.status as loan_status
            FROM loan_guarantors lg
            JOIN loans l ON lg.loan_id = l.id
            WHERE l.user_id = ?
            ORDER BY lg.id DESC
        """, (user_id,)).fetchall()

        notifications = db.execute("""
            SELECT
                id,
                user_id,
                title,
                message,
                notification_type,
                type,
                link,
                is_read,
                created_at
            FROM notifications
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()

        unread_notifications_count = db.execute("""
            SELECT COUNT(*) AS count
            FROM notifications
            WHERE user_id = ?
            AND is_read = 0
        """, (user_id,)).fetchone()['count']

        # ============================================================
        # SAVINGS BY TYPE (KAI, KS, KAC, Registration)
        # ============================================================
        settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            kai_share_price  = settings['kai_share_price']  or 100000
            ks_share_price   = settings['ks_share_price']   or 10000
            kac_annual_fee   = settings['kac_annual_fee']   or 100000
            registration_fee = settings['registration_fee'] or 20000
        else:
            kai_share_price  = 100000
            ks_share_price   = 10000
            kac_annual_fee   = 100000
            registration_fee = 20000

        kai_shares   = member['kai_shares'] or 0
        ks_shares    = member['ks_shares'] or 0
        reg_fee_paid = member['registration_fee_paid'] or 0

        # ============================================================
        # KAC — INSTALLMENT-AWARE
        # kac_paid holds the running total (0 → kac_annual_fee)
        # ============================================================
        try:
            kac_paid_raw = member['kac_paid']
        except (IndexError, KeyError):
            kac_paid_raw = 0

        # Coerce to a float, handle None / bool / string safely
        if kac_paid_raw is None:
            kac_paid = 0.0
        elif isinstance(kac_paid_raw, bool):
            # Legacy boolean — treat as fully paid or nothing
            kac_paid = float(kac_annual_fee) if kac_paid_raw else 0.0
        else:
            try:
                kac_paid = float(kac_paid_raw)
            except (TypeError, ValueError):
                kac_paid = 0.0

        # Cap at the annual fee
        if kac_paid > kac_annual_fee:
            kac_paid = float(kac_annual_fee)

        # The actual amount paid toward KAC (this is what the template shows)
        kac_amount = kac_paid

        # Booleans for the template
        kac_fully_paid = (kac_paid >= kac_annual_fee)

        kai_amount = kai_shares * kai_share_price
        ks_amount  = ks_shares  * ks_share_price
        reg_amount = registration_fee if reg_fee_paid else 0

        db.close()

        # Check if user is staff (has a staff role)
        is_staff = user_role in ["admin", "chairperson", "treasurer", "secretary", "publicity"]
        
        # Role dashboard URLs for navigation back
        role_dashboards = {
            "admin": "/admin/dashboard",
            "chairperson": "/admin/dashboard",
            "treasurer": "/treasurer/dashboard",
            "secretary": "/secretary/dashboard",
            "publicity": "/publicity/dashboard",
            "member": "/member/dashboard"
        }
        role_dashboard_url = role_dashboards.get(user_role, "/member/dashboard")
        
        # Role display name
        role_display_names = {
            "admin": "Admin",
            "chairperson": "Chairperson",
            "treasurer": "Treasurer",
            "secretary": "Secretary",
            "publicity": "Publicity",
            "member": "Member"
        }
        role_display = role_display_names.get(user_role, "Member")

        return render_template(
            "member/member-dashboard.html",
            member=member,
            user=member,
            total_savings=total_savings,
            active_loans_count=active_loans_count,
            active_loans_balance=active_loans_balance,
            total_loans_taken=total_loans_taken,
            savings_deposits=savings_deposits,
            loans=loans,
            repayments=repayments,
            guarantors=guarantors,
            notifications=notifications,
            unread_notifications_count=unread_notifications_count,
            # Staff related variables
            is_staff=is_staff,
            user_role=user_role,
            role_display=role_display,
            role_dashboard_url=role_dashboard_url,
            staff_view=is_staff_view,
            # Savings by type
            kai_shares=kai_shares,
            ks_shares=ks_shares,
            kac_paid=kac_paid,                 # numeric running total
            kac_fully_paid=kac_fully_paid,     # boolean flag
            reg_fee_paid=reg_fee_paid,
            kai_amount=kai_amount,
            ks_amount=ks_amount,
            kac_amount=kac_amount,             # actual amount paid (0 → fee)
            reg_amount=reg_amount,
            kai_share_price=kai_share_price,
            ks_share_price=ks_share_price,
            kac_annual_fee=kac_annual_fee,
            registration_fee=registration_fee,
            now=datetime.now()
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            db.close()
        except:
            pass
        flash(f"Error loading dashboard: {str(e)}", "danger")
        return redirect(url_for("login"))


# ============================================================
# MEMBER - APPLY LOAN
# ============================================================
@app.route("/member/apply-loan", methods=["GET", "POST"])
def member_apply_loan():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    
    if request.method == "GET":
        member = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        
        total_savings = db.execute("""
            SELECT COALESCE(SUM(amount), 0) as total 
            FROM savings_deposits 
            WHERE user_id = ?
        """, (user_id,)).fetchone()['total']
        
        db.close()
        
        from datetime import datetime, timedelta
        now = datetime.now()
        current_year = now.year
        current_date = now.strftime('%d %B %Y')
        due_date = now + timedelta(days=30)
        due_date_formatted = due_date.strftime('%d %B %Y')
        due_date_iso = due_date.strftime('%Y-%m-%d')
        
        return render_template(
            "member/apply-loan.html", 
            member=member, 
            total_savings=total_savings,
            max_loan_amount=10000000,
            loan_interest_rate=12,
            current_year=current_year,
            now=now,
            current_date=current_date,
            due_date=due_date,
            due_date_formatted=due_date_formatted,
            due_date_iso=due_date_iso
        )
    
    # POST - Submit loan application
    try:
        if request.is_json:
            data = request.get_json()
            loan_amount = float(data.get('loan_amount'))
            purpose = data.get('purpose')
            repayment_plan = data.get('repayment_plan', 'monthly')
            
            g1_name = data.get('guarantor1_name', '')
            g1_phone = data.get('guarantor1_phone', '')
            g1_email = data.get('guarantor1_email', '')
            g1_relationship = data.get('guarantor1_relationship', '')
            
            g2_name = data.get('guarantor2_name', '')
            g2_phone = data.get('guarantor2_phone', '')
            g2_email = data.get('guarantor2_email', '')
            g2_relationship = data.get('guarantor2_relationship', '')
        else:
            loan_amount = float(request.form.get('loan_amount'))
            purpose = request.form.get('purpose')
            repayment_plan = request.form.get('repayment_plan', 'monthly')
            
            g1_name = request.form.get('guarantor1_name', '')
            g1_phone = request.form.get('guarantor1_phone', '')
            g1_email = request.form.get('guarantor1_email', '')
            g1_relationship = request.form.get('guarantor1_relationship', '')
            
            g2_name = request.form.get('guarantor2_name', '')
            g2_phone = request.form.get('guarantor2_phone', '')
            g2_email = request.form.get('guarantor2_email', '')
            g2_relationship = request.form.get('guarantor2_relationship', '')
        
        if loan_amount < 10000 or loan_amount > 10000000:
            if request.is_json:
                return jsonify({'success': False, 'message': 'Loan amount must be between UGX 10,000 and UGX 10,000,000'}), 400
            flash('Loan amount must be between UGX 10,000 and UGX 10,000,000', 'danger')
            return redirect(url_for('member_apply_loan'))
        
        from datetime import datetime, timedelta
        
        application_date = datetime.now()
        
        monthly_rate_percent = get_interest_rate(loan_amount)
        monthly_rate = monthly_rate_percent / 100
        interest_amount = loan_amount * monthly_rate
        total_repayment = loan_amount + interest_amount
        due_date = application_date + timedelta(days=30)
        due_date_str = due_date.strftime('%Y-%m-%d')
        loan_ref = generate_loan_reference()
        
        total_savings = db.execute("""
            SELECT COALESCE(SUM(amount), 0) as total 
            FROM savings_deposits 
            WHERE user_id = ?
        """, (user_id,)).fetchone()['total']
        
        savings_threshold = total_savings * 0.95
        guarantors_required = loan_amount > savings_threshold
        
        if guarantors_required:
            if not g1_name or not g1_phone:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Guarantor 1 details are required for this loan amount'}), 400
                flash('Guarantor 1 details are required for this loan amount', 'danger')
                return redirect(url_for('member_apply_loan'))
            
            if not g2_name or not g2_phone:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Guarantor 2 details are required for this loan amount'}), 400
                flash('Guarantor 2 details are required for this loan amount', 'danger')
                return redirect(url_for('member_apply_loan'))
            
            if g1_name.lower() == g2_name.lower() or g1_phone == g2_phone:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Guarantor 1 and Guarantor 2 must be different'}), 400
                flash('Guarantor 1 and Guarantor 2 must be different', 'danger')
                return redirect(url_for('member_apply_loan'))
        
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO loans (
                loan_number, user_id, amount, interest_rate, interest_amount,
                total_repayment, monthly_installment, tenure, purpose,
                repayment_plan, status, application_date,
                current_balance, last_interest_date, next_interest_date,
                start_month, end_month, total_interest_accrued,
                principal_paid, interest_paid, months_paid,
                original_balance, total_interest_calculated, due_date,
                loan_start_date, loan_end_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            loan_ref, 
            user_id, 
            loan_amount, 
            monthly_rate_percent,
            interest_amount,
            total_repayment, 
            interest_amount,
            1,
            purpose,
            repayment_plan, 
            'pending', 
            application_date.strftime('%Y-%m-%d'),
            total_repayment,
            application_date.strftime('%Y-%m-%d'),
            due_date_str,
            application_date.month, 
            due_date.month,
            interest_amount,
            0,
            0,
            0,
            loan_amount,
            interest_amount,
            due_date_str,
            application_date.strftime('%Y-%m-%d'),
            due_date_str
        ))
        
        loan_id = cursor.lastrowid
        
        if guarantors_required:
            cursor.execute("""
                INSERT INTO loan_guarantors (
                    loan_id, guarantor_name, phone, email, relationship, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (loan_id, g1_name, g1_phone, g1_email, g1_relationship, 'active'))
            
            cursor.execute("""
                INSERT INTO loan_guarantors (
                    loan_id, guarantor_name, phone, email, relationship, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (loan_id, g2_name, g2_phone, g2_email, g2_relationship, 'active'))
        else:
            cursor.execute("""
                INSERT INTO loan_guarantors (
                    loan_id, guarantor_name, phone, email, relationship, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (loan_id, 'No Guarantor Required', 'N/A', 'N/A', 'N/A', 'accepted'))
            
            cursor.execute("""
                INSERT INTO loan_guarantors (
                    loan_id, guarantor_name, phone, email, relationship, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (loan_id, 'No Guarantor Required', 'N/A', 'N/A', 'N/A', 'accepted'))
        
        db.commit()
        db.close()
        
        success_message = '✅ Loan application submitted successfully!'
        if guarantors_required:
            success_message += ' Guarantors will be contacted manually by the SACCO team.'
        else:
            success_message += ' No guarantors required based on your savings.'
        
        if request.is_json:
            return jsonify({
                'success': True,
                'message': success_message,
                'loan_number': loan_ref,
                'loan_id': loan_id,
                'tenure': 1,
                'monthly_installment': interest_amount,
                'total_repayment': total_repayment,
                'total_interest': interest_amount,
                'guarantors_required': guarantors_required,
                'monthly_rate': monthly_rate,
                'repayment_plan': repayment_plan,
                'due_date': due_date_str,
                'loan_start_date': application_date.strftime('%Y-%m-%d'),
                'loan_end_date': due_date_str
            })
        
        flash(success_message, 'success')
        return redirect(url_for('treasurer_dashboard') + '#loan_applications')
        
    except Exception as e:
        db.rollback()
        db.close()
        import traceback
        traceback.print_exc()
        if request.is_json:
            return jsonify({'success': False, 'message': str(e)}), 500
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('member_apply_loan'))


# ============================================================
# MEMBER - REPAYMENTS
# ============================================================
@app.route("/member/repayments")
def member_repayments():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        repayments = db.execute("""
            SELECT 
                r.id,
                r.loan_id,
                r.user_id,
                r.amount,
                r.interest_paid,
                r.principal_paid,
                r.balance_after,
                r.payment_date,
                r.payment_method,
                r.transaction_ref,
                r.status,
                r.created_at,
                l.loan_number,
                l.amount as loan_amount,
                l.status as loan_status,
                l.current_balance
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            WHERE r.user_id = ?
            ORDER BY r.payment_date DESC
        """, (user_id,)).fetchall()
        
        loans = db.execute("""
            SELECT 
                l.*,
                COALESCE(l.rejection_reason, l.admin_rejection_reason, '') as rejection_reason
            FROM loans l
            WHERE l.user_id = ?
            ORDER BY l.application_date DESC
        """, (user_id,)).fetchall()
        
        db.close()
        
        return render_template(
            "member/member-repayments.html",
            repayments=repayments,
            loans=loans
        )
        
    except sqlite3.Error as e:
        db.close()
        flash(f'Database error: {str(e)}', 'danger')
        return redirect(url_for('member_dashboard'))
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('member_dashboard'))


# ============================================================
# MEMBER - SAVINGS
# ============================================================
@app.route("/member/savings")
def member_savings():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    
    try:
        member = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        
        if not member:
            db.close()
            flash('Member not found', 'danger')
            return redirect(url_for('member_dashboard'))
        
        savings_deposits = db.execute("""
            SELECT * FROM savings_deposits 
            WHERE user_id = ? 
            ORDER BY deposit_date DESC, created_at DESC
        """, (user_id,)).fetchall()
        
        savings_balance = float(member['savings_balance'] or 0)
        total_savings = savings_balance
        
        db.close()
        
        return render_template(
            "member/member-savings.html", 
            savings_deposits=savings_deposits,
            savings_balance=savings_balance,
            total_savings=total_savings,
            member=member
        )
        
    except Exception as e:
        db.close()
        flash(f'Error loading savings: {str(e)}', 'danger')
        return redirect(url_for('member_dashboard'))


# ============================================================
# MEMBER - GUARANTORS
# ============================================================
@app.route("/member/guarantors")
def member_guarantors():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        guarantors = db.execute("""
            SELECT 
                lg.*,
                l.loan_number,
                l.amount,
                l.status as loan_status,
                l.application_date,
                l.interest_amount,
                l.total_repayment
            FROM loan_guarantors lg
            JOIN loans l ON lg.loan_id = l.id
            WHERE l.user_id = ?
            ORDER BY l.application_date DESC, lg.id DESC
        """, (user_id,)).fetchall()
        
        loans = db.execute("""
            SELECT 
                id,
                loan_number,
                amount,
                interest_amount,
                total_repayment,
                status,
                application_date,
                approved_date,
                disbursed_date,
                completed_date,
                current_balance,
                created_at
            FROM loans 
            WHERE user_id = ?
            ORDER BY application_date DESC
        """, (user_id,)).fetchall()
        
        db.close()
        
        return render_template(
            "member/member-guarantors.html",
            guarantors=guarantors,
            loans=loans
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        db.close()
        flash(f"Error loading guarantors: {str(e)}", "danger")
        return redirect(url_for("member_dashboard"))


# ============================================================
# MEMBER SEARCH FOR GUARANTORS
# ============================================================
@app.route("/member/search-members", methods=["GET"])
def search_members():
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    search_term = request.args.get('q', '').strip()
    current_user_id = session["user_id"]
    
    if not search_term or len(search_term) < 2:
        return jsonify({
            'success': True,
            'members': [],
            'message': 'Please enter at least 2 characters'
        })
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        search_pattern = f'%{search_term}%'
        members = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                phone,
                email,
                savings_balance,
                status
            FROM users 
            WHERE LOWER(role) = 'member' 
            AND id != ?
            AND status = 'active'
            AND (
                LOWER(full_name) LIKE LOWER(?) OR
                sacco_number LIKE ? OR
                phone LIKE ? OR
                email LIKE ?
            )
            ORDER BY full_name ASC
            LIMIT 20
        """, (current_user_id, search_pattern, search_pattern, search_pattern, search_pattern)).fetchall()
        
        member_list = []
        for member in members:
            loan_count = db.execute("""
                SELECT COUNT(*) as count 
                FROM loans 
                WHERE user_id = ? AND status IN ('approved', 'disbursed', 'active')
            """, (member['id'],)).fetchone()['count']
            
            member_list.append({
                'id': member['id'],
                'full_name': member['full_name'],
                'sacco_number': member['sacco_number'],
                'phone': member['phone'] or '',
                'email': member['email'] or '',
                'savings_balance': member['savings_balance'] or 0,
                'active_loans': loan_count,
                'status': member['status']
            })
        
        db.close()
        
        return jsonify({
            'success': True,
            'members': member_list,
            'count': len(member_list)
        })
        
    except Exception as e:
        db.close()
        print(f"Error searching members: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@app.route("/treasurer/savings-reports")
def treasurer_savings_reports():
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # ============================================================
        # GET ALL USERS (MEMBERS + STAFF)
        # ============================================================
        members = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                phone,
                email,
                status,
                role,
                savings_balance,
                kai_shares,
                ks_shares,
                kac_paid,
                registration_fee_paid,
                registration_date
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            ORDER BY 
                CASE 
                    WHEN LOWER(role) = 'member' THEN 1
                    WHEN LOWER(role) = 'admin' THEN 2
                    WHEN LOWER(role) = 'chairperson' THEN 3
                    WHEN LOWER(role) = 'treasurer' THEN 4
                    WHEN LOWER(role) = 'secretary' THEN 5
                    WHEN LOWER(role) = 'publicity' THEN 6
                END,
                full_name ASC
        """).fetchall()
        
        total_members = len(members)
        
        # ============================================================
        # CALCULATE STATISTICS
        # ============================================================
        total_regular_members = 0
        total_staff_members = 0
        
        # Calculate savings by type
        kai_total = 0
        ks_total = 0
        kac_total = 0
        registration_fees_total = 0
        kai_members = 0
        ks_members = 0
        kac_members = 0
        registration_fees_count = 0
        kai_shares = 0
        ks_shares = 0
        
        # Get settings for share prices
        settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            kai_share_price = settings['kai_share_price'] or 100000
            ks_share_price = settings['ks_share_price'] or 10000
            kac_annual_fee = settings['kac_annual_fee'] or 100000
            registration_fee = settings['registration_fee'] or 20000
        else:
            kai_share_price = 100000
            ks_share_price = 10000
            kac_annual_fee = 100000
            registration_fee = 20000
        
        for member in members:
            member_dict = dict(member)
            
            # Count roles
            if member_dict.get('role') == 'member':
                total_regular_members += 1
            else:
                total_staff_members += 1
            
            # KAI
            if member_dict.get('kai_shares'):
                shares = member_dict['kai_shares']
                kai_shares += shares
                kai_total += shares * kai_share_price
                kai_members += 1
            
            # KS
            if member_dict.get('ks_shares'):
                shares = member_dict['ks_shares']
                ks_shares += shares
                ks_total += shares * ks_share_price
                ks_members += 1
            
            # KAC
            if member_dict.get('kac_paid'):
                kac_total += kac_annual_fee
                kac_members += 1
            
            # Registration Fee
            if member_dict.get('registration_fee_paid'):
                registration_fees_total += registration_fee
                registration_fees_count += 1
        
        db.close()
        
        # Debug
        print("=" * 60)
        print("📊 SAVINGS REPORTS - INCLUDING STAFF")
        print(f"📊 Total Users: {total_members}")
        print(f"📊 Regular Members: {total_regular_members}")
        print(f"📊 Staff Members: {total_staff_members}")
        print(f"📊 KAI Members: {kai_members}")
        print(f"📊 KS Members: {ks_members}")
        print(f"📊 KAC Members: {kac_members}")
        print("=" * 60)
        
        return render_template(
            "treasurer/savings-reports.html",
            members=members,
            total_members=total_members,
            total_regular_members=total_regular_members,
            total_staff_members=total_staff_members,
            kai_total=kai_total,
            ks_total=ks_total,
            kac_total=kac_total,
            registration_fees_total=registration_fees_total,
            kai_members=kai_members,
            ks_members=ks_members,
            kac_members=kac_members,
            registration_fees_count=registration_fees_count,
            kai_shares=kai_shares,
            ks_shares=ks_shares
        )
        
    except Exception as e:
        db.close()
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard'))

# ============================================================
# ADMIN - EDIT USER
# ============================================================
@app.route("/admin/users/edit/<int:user_id>", methods=["GET", "POST"])
def admin_edit_user(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        
        if not user:
            flash('User not found', 'danger')
            return redirect(url_for('admin_manage_users'))
        
        if request.method == "POST":
            full_name = request.form.get('full_name')
            email = request.form.get('email')
            phone = request.form.get('phone')
            role = request.form.get('role')
            status = request.form.get('status')
            
            # Check if email is already used by another user
            if email:
                existing = db.execute("""
                    SELECT id FROM users WHERE email = ? AND id != ?
                """, (email, user_id)).fetchone()
                if existing:
                    flash('Email already in use by another user', 'danger')
                    db.close()
                    return render_template("admin/edit-user.html", user=user)
            
            db.execute("""
                UPDATE users 
                SET full_name = ?, email = ?, phone = ?, role = ?, status = ?
                WHERE id = ?
            """, (full_name, email, phone, role, status, user_id))
            
            db.commit()
            db.close()
            
            flash('User updated successfully!', 'success')
            return redirect(url_for('admin_manage_users'))
        
        db.close()
        return render_template("admin/edit-user.html", user=user, now=datetime.now())
        
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('admin_manage_users'))

# ============================================================
# ADMIN - MANAGE USERS
# ============================================================
@app.route("/admin/users/manage")
def admin_manage_users():
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied. Only Admin or Chairperson can manage users.', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get all staff users
        staff_users = db.execute("""
            SELECT * FROM users 
            WHERE LOWER(role) IN ('admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            ORDER BY 
                CASE 
                    WHEN LOWER(role) = 'admin' THEN 1
                    WHEN LOWER(role) = 'chairperson' THEN 2
                    WHEN LOWER(role) = 'treasurer' THEN 3
                    WHEN LOWER(role) = 'secretary' THEN 4
                    WHEN LOWER(role) = 'publicity' THEN 5
                END,
                full_name
        """).fetchall()
        
        # Get staff counts
        staff_counts = {
            'treasurer': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'treasurer'").fetchone()[0],
            'secretary': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'secretary'").fetchone()[0],
            'publicity': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'publicity'").fetchone()[0],
            'admin': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) IN ('admin', 'chairperson')").fetchone()[0]
        }
        
        db.close()
        
        return render_template(
            "admin/manage-users.html",
            staff_users=staff_users,
            staff_counts=staff_counts,
            now=datetime.now()
        )
        
    except Exception as e:
        db.close()
        flash(f'Error loading users: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    
# ============================================================
# ADMIN - REGISTER USER
# ============================================================
@app.route("/admin/users/register", methods=["GET", "POST"])
def admin_register_user():
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied. Only Admin or Chairperson can register users.', 'danger')
        return redirect("/login")
    
    if request.method == "POST":
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        role = request.form.get('role')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not full_name or not email or not role:
            flash('All fields are required', 'danger')
            return render_template("admin/register-user.html")
        
        if password != confirm_password:
            flash('Passwords do not match', 'danger')
            return render_template("admin/register-user.html")
        
        if len(password) < 6:
            flash('Password must be at least 6 characters', 'danger')
            return render_template("admin/register-user.html")
        
        sacco_number = f"STAFF-{datetime.now().strftime('%Y%m')}-{role[:3].upper()}{int(datetime.now().timestamp()) % 1000}"
        
        db = get_db()
        
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash('Email already registered', 'danger')
            db.close()
            return render_template("admin/register-user.html")
        
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO users (
                full_name, email, phone, sacco_number, password, role, status, registration_date
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """, (full_name, email, phone, sacco_number, password, role, datetime.now().strftime('%Y-%m-%d')))
        
        db.commit()
        db.close()
        
        flash(f'User {full_name} registered successfully as {role}!', 'success')
        return redirect(url_for('admin_manage_users'))
    
    return render_template("admin/register-user.html")

# ============================================================
# ADMIN - DELETE USER
# ============================================================
@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
def admin_delete_user(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        user = db.execute("SELECT id, full_name, role FROM users WHERE id = ?", (user_id,)).fetchone()
        
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        if user_id == session.get('user_id'):
            db.close()
            return jsonify({'success': False, 'message': 'You cannot delete your own account'}), 400
        
        if user['role'] == 'admin':
            admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                db.close()
                return jsonify({'success': False, 'message': 'Cannot delete the last admin user'}), 400
        
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': f'User "{user["full_name"]}" deleted successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route("/admin/users/reset-password/<int:user_id>", methods=["POST"])
def admin_reset_password(user_id):

    # Check authorization
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({
            "success": False,
            "message": "Access denied"
        }), 403

    db = get_db()

    try:
        # Find user
        user = db.execute(
            """
            SELECT id, username, name
            FROM users
            WHERE id = ?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            return jsonify({
                "success": False,
                "message": "User not found"
            }), 404

        # Get JSON request body
        data = request.get_json(silent=True)

        if not isinstance(data, dict):
            return jsonify({
                "success": False,
                "message": "Invalid request"
            }), 400

        # Get password
        new_password = data.get("password")

        if not isinstance(new_password, str) or not new_password:
            return jsonify({
                "success": False,
                "message": "Password is required"
            }), 400

        # Server-side password validation
        if len(new_password) < 8:
            return jsonify({
                "success": False,
                "message": "Password must be at least 8 characters long"
            }), 400

        # Hash password before storing it
        hashed_password = generate_password_hash(new_password)

        # Update password
        db.execute(
            """
            UPDATE users
            SET password = ?
            WHERE id = ?
            """,
            (hashed_password, user_id)
        )

        db.commit()

        return jsonify({
            "success": True,
            "message": "Password changed successfully"
        }), 200

    except Exception as e:
        db.rollback()

        print("Password reset error:", e)

        return jsonify({
            "success": False,
            "message": "Failed to reset password"
        }), 500

    finally:
        db.close()

# ============================================================
# ADMIN - VIEW USER (JSON)
# ============================================================
@app.route("/admin/users/view/<int:user_id>")
def admin_view_user(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        user = db.execute("""
            SELECT id, full_name, email, phone, sacco_number, role, status
            FROM users WHERE id = ?
        """, (user_id,)).fetchone()
        
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        db.close()
        return jsonify({'success': True, 'user': dict(user)})
        
    except Exception as e:
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# ADMIN - GENERATE REPORTS
# ============================================================
@app.route("/admin/reports/generate/<string:report_type>")
def generate_report(report_type):
    if session.get("role") not in ["admin", "chairperson", "treasurer"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        if report_type == 'members':
            # Get all members with their details
            members = db.execute("""
                SELECT 
                    id, full_name, sacco_number, phone, email,
                    savings_balance, status, registration_date,
                    next_of_kin_name, next_of_kin_phone, gender, dob, address,
                    COALESCE(kai_shares, 0) as kai_shares,
                    COALESCE(ks_shares, 0) as ks_shares,
                    COALESCE(kac_paid, 0) as kac_paid,
                    COALESCE(registration_fee_paid, 0) as registration_fee_paid
                FROM users 
                WHERE LOWER(role) = 'member'
                ORDER BY full_name
            """).fetchall()
            
            members_list = []
            for member in members:
                member_dict = dict(member)
                loan_count = db.execute("""
                    SELECT COUNT(*) FROM loans 
                    WHERE user_id = ? AND status IN ('disbursed', 'active')
                """, (member_dict['id'],)).fetchone()[0]
                member_dict['active_loans'] = loan_count
                members_list.append(member_dict)
            
            db.close()
            
            return render_template(
                "admin/reports/member-report.html",
                members=members_list,
                total_members=len(members_list),
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )
            
        elif report_type == 'financial':
            # Financial summary data
            total_members = db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member'").fetchone()[0]
            total_savings = db.execute("SELECT COALESCE(SUM(savings_balance), 0) FROM users WHERE LOWER(role) = 'member'").fetchone()[0]
            
            total_loans = db.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
            pending_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'pending'").fetchone()[0]
            approved_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'approved'").fetchone()[0]
            disbursed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'disbursed'").fetchone()[0]
            active_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status IN ('disbursed', 'active')").fetchone()[0]
            completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
            rejected_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'rejected'").fetchone()[0]
            
            total_loan_amount = db.execute("SELECT COALESCE(SUM(amount), 0) FROM loans").fetchone()[0]
            total_disbursed = db.execute("SELECT COALESCE(SUM(amount), 0) FROM loans WHERE status IN ('disbursed', 'active', 'completed')").fetchone()[0]
            total_repayments = db.execute("SELECT COALESCE(SUM(amount), 0) FROM repayments WHERE status = 'completed'").fetchone()[0]
            total_interest = db.execute("SELECT COALESCE(SUM(interest_paid), 0) FROM repayments WHERE status = 'completed'").fetchone()[0]
            
            # Savings by type
            kai_total = 0
            ks_total = 0
            kac_total = 0
            reg_total = 0
            all_members = db.execute("SELECT * FROM users WHERE LOWER(role) = 'member'").fetchall()
            for member in all_members:
                member_dict = dict(member)
                if member_dict.get('kai_shares'):
                    kai_total += member_dict['kai_shares'] * 100000
                if member_dict.get('ks_shares'):
                    ks_total += member_dict['ks_shares'] * 10000
                if member_dict.get('kac_paid'):
                    kac_total += 100000
                if member_dict.get('registration_fee_paid'):
                    reg_total += 20000
            
            # Get staff counts
            staff_counts = {
                'treasurer': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'treasurer'").fetchone()[0],
                'secretary': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'secretary'").fetchone()[0],
                'publicity': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'publicity'").fetchone()[0],
                'admin': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) IN ('admin', 'chairperson')").fetchone()[0]
            }
            
            db.close()
            
            # Debug - print to console
            print("=" * 60)
            print("📊 GENERATING FINANCIAL REPORT")
            print(f"📊 Total Members: {total_members}")
            print(f"📊 Total Savings: {total_savings}")
            print(f"📊 Template: admin/reports/financial-report.html")
            print("=" * 60)
            
            return render_template(
                "admin/reports/financial-report.html",
                total_members=total_members,
                total_savings=total_savings,
                total_loans=total_loans,
                pending_loans=pending_loans,
                approved_loans=approved_loans,
                disbursed_loans=disbursed_loans,
                active_loans=active_loans,
                completed_loans=completed_loans,
                rejected_loans=rejected_loans,
                total_loan_amount=total_loan_amount,
                total_disbursed=total_disbursed,
                total_repayments=total_repayments,
                total_interest=total_interest,
                kai_total=kai_total,
                ks_total=ks_total,
                kac_total=kac_total,
                reg_total=reg_total,
                staff_counts=staff_counts,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )
            
        elif report_type == 'loans':
            # FIXED: Proper SQL syntax for subqueries
            loans = db.execute("""
                SELECT 
                    l.*,
                    u.full_name,
                    u.sacco_number,
                    u.phone,
                    u.email,
                    COALESCE(
                        (SELECT COUNT(*) FROM repayments 
                         WHERE loan_id = l.id AND status = 'completed'), 0
                    ) as payment_count,
                    COALESCE(
                        (SELECT SUM(amount) FROM repayments 
                         WHERE loan_id = l.id AND status = 'completed'), 0
                    ) as total_paid
                FROM loans l
                JOIN users u ON l.user_id = u.id
                ORDER BY l.application_date DESC
            """).fetchall()
            
            loans_list = [dict(loan) for loan in loans]
            total_loans = len(loans_list)
            total_amount = sum(l.get('amount', 0) for l in loans_list) if loans_list else 0
            total_balance = sum(l.get('current_balance', 0) for l in loans_list) if loans_list else 0
            
            db.close()
            
            return render_template(
                "admin/reports/loan-report.html",
                loans=loans_list,
                total_loans=total_loans,
                total_amount=total_amount,
                total_balance=total_balance,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )
            
        elif report_type == 'savings':
            # Get all savings deposits with member details
            savings = db.execute("""
                SELECT 
                    sd.*,
                    u.full_name,
                    u.sacco_number,
                    u.phone,
                    u.email
                FROM savings_deposits sd
                JOIN users u ON sd.user_id = u.id
                ORDER BY sd.deposit_date DESC
            """).fetchall()
            
            member_savings = db.execute("""
                SELECT 
                    u.id,
                    u.full_name,
                    u.sacco_number,
                    u.savings_balance,
                    u.kai_shares,
                    u.ks_shares,
                    u.kac_paid,
                    u.registration_fee_paid,
                    COUNT(sd.id) as deposit_count,
                    COALESCE(SUM(sd.amount), 0) as total_deposited
                FROM users u
                LEFT JOIN savings_deposits sd ON u.id = sd.user_id
                WHERE LOWER(u.role) = 'member'
                GROUP BY u.id
                ORDER BY u.savings_balance DESC
            """).fetchall()
            
            savings_list = [dict(s) for s in savings]
            member_savings_list = [dict(m) for m in member_savings]
            total_savings_amount = sum(m.get('savings_balance', 0) for m in member_savings_list) if member_savings_list else 0
            
            db.close()
            
            return render_template(
                "admin/reports/savings-report.html",
                savings=savings_list,
                member_savings=member_savings_list,
                total_savings=total_savings_amount,
                total_deposits=len(savings_list),
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )
            
        else:
            flash('Invalid report type', 'danger')
            return redirect(url_for('admin_dashboard'))
            
    except Exception as e:
        db.close()
        print(f"❌ Error generating report: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error generating report: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))

# ============================================================
# OTHER DASHBOARDS
# ============================================================
@app.route("/chairperson/dashboard")
def chairperson_dashboard():
    if session.get("role") != "chairperson":
        return redirect("/login")
    return render_template("chairperson/chairperson-dashboard.html")


# ============================================================
# SECRETARY DASHBOARD - FULL VERSION
# ============================================================
@app.route("/secretary/dashboard")
def secretary_dashboard():
    if session.get("role") != "secretary":
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # ============================================================
        # GET ALL MEMBERS FOR CHAT
        # ============================================================
        members = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                email,
                phone,
                status,
                savings_balance,
                registration_date,
                gender,
                dob,
                address
            FROM users 
            WHERE LOWER(role) = 'member'
            ORDER BY full_name ASC
        """).fetchall()
        
        # ============================================================
        # GET STATISTICS FOR DASHBOARD
        # ============================================================
        total_members = len(members)
        active_members = db.execute("""
            SELECT COUNT(*) FROM users 
            WHERE LOWER(role) = 'member' AND status = 'active'
        """).fetchone()[0]
        
        total_loans = db.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
        pending_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'pending'").fetchone()[0]
        active_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status IN ('disbursed', 'active')").fetchone()[0]
        completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
        
        total_savings = db.execute("""
            SELECT COALESCE(SUM(savings_balance), 0) 
            FROM users WHERE LOWER(role) = 'member'
        """).fetchone()[0]
        
        # ============================================================
        # GET RECENT ACTIVITIES
        # ============================================================
        recent_activities = db.execute("""
            SELECT 
                'deposit' as type,
                sd.amount,
                sd.deposit_date as date,
                u.full_name,
                u.sacco_number
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            UNION ALL
            SELECT 
                'repayment' as type,
                r.amount,
                r.payment_date as date,
                u.full_name,
                u.sacco_number
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            JOIN users u ON l.user_id = u.id
            WHERE r.status = 'completed'
            UNION ALL
            SELECT 
                'loan' as type,
                l.amount,
                l.application_date as date,
                u.full_name,
                u.sacco_number
            FROM loans l
            JOIN users u ON l.user_id = u.id
            ORDER BY date DESC
            LIMIT 20
        """).fetchall()
        
        # ============================================================
        # GET PENDING LOAN APPLICATIONS
        # ============================================================
        pending_loan_applications = db.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number,
                u.savings_balance
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status = 'pending'
            ORDER BY l.application_date DESC
        """).fetchall()
        
        # ============================================================
        # GET SYSTEM SETTINGS
        # ============================================================
        settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            settings = dict(settings)
        else:
            settings = {
                'sacco_name': 'Karacel Association',
                'registration_number': 'SACCO/REG/2024/001',
                'savings_interest_rate': 6.5,
                'loan_interest_rate': 12,
                'penalty_rate': 5,
                'max_loan_amount': '10,000,000',
                'min_loan_amount': '10,000',
                'max_tenure': 24
            }
        
        db.close()
        
        # Debug - print to console
        print("=" * 60)
        print(f"🔍 SECRETARY DASHBOARD LOADED")
        print(f"📊 Total Members: {total_members}")
        print(f"📊 Active Members: {active_members}")
        print(f"📊 Total Loans: {total_loans}")
        print(f"📊 Pending Loans: {pending_loans}")
        print("=" * 60)
        
        return render_template(
            "secretary/secretary-dashboard.html",
            members=members,
            total_members=total_members,
            active_members=active_members,
            total_loans=total_loans,
            pending_loans=pending_loans,
            active_loans=active_loans,
            completed_loans=completed_loans,
            total_savings=total_savings,
            recent_activities=recent_activities,
            pending_loan_applications=pending_loan_applications,
            settings=settings,
            now=datetime.now(),
            session=session
        )
        
    except Exception as e:
        db.close()
        print(f"❌ Error loading secretary dashboard: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('login'))


# ============================================================
# PUBLICITY ROUTES - FULL CRUD
# ============================================================

@app.route("/publicity/dashboard")
def publicity_dashboard():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        # Get total members
        total_members = conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member' AND status = 'active'").fetchone()[0]
        
        # Get announcements count
        total_announcements = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        
        # Get upcoming events count
        upcoming_events = conn.execute("SELECT COUNT(*) FROM events WHERE event_date >= date('now')").fetchone()[0]
        
        # Get newsletters count
        total_newsletters = conn.execute("SELECT COUNT(*) FROM newsletters").fetchone()[0]
        
        # Get recent announcements
        announcements = conn.execute("""
            SELECT * FROM announcements 
            ORDER BY created_at DESC 
            LIMIT 5
        """).fetchall()
        
        # Get upcoming events
        events = conn.execute("""
            SELECT * FROM events 
            WHERE event_date >= date('now')
            ORDER BY event_date ASC 
            LIMIT 4
        """).fetchall()
        
    except sqlite3.Error as e:
        print(f"❌ Database Error: {str(e)}")
        flash(f'Database error: {str(e)}', 'danger')
        return redirect(url_for('login'))
    finally:
        conn.close()
    
    return render_template(
        "publicity/publicity-dashboard.html",
        total_members=total_members,
        total_announcements=total_announcements,
        upcoming_events=upcoming_events,
        total_newsletters=total_newsletters,
        announcements=announcements,
        events=events,
        session=session,  
    )


@app.route("/publicity/announcements")
def publicity_announcements():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        total_members = conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member' AND status = 'active'").fetchone()[0]
        announcements = conn.execute("""
            SELECT a.*, 
                   (SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member' AND status = 'active') as recipients
            FROM announcements a
            ORDER BY a.created_at DESC
        """).fetchall()
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        conn.close()
    
    return render_template("publicity/publicity-announcements.html", 
                          announcements=announcements, 
                          total_members=total_members)


@app.route("/publicity/announcement/create", methods=['POST'])
def create_announcement():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    
    if not title or not content:
        flash('Please fill in all fields', 'danger')
        return redirect("/publicity/announcements")
    
    conn = get_db()
    
    try:
        # Insert announcement
        conn.execute("""
            INSERT INTO announcements (title, content, created_at, created_by)
            VALUES (?, ?, datetime('now'), ?)
        """, (title, content, session.get('user_id')))
        conn.commit()
        
        # Get all active members
        members = conn.execute("""
            SELECT id FROM users WHERE LOWER(role) = 'member' AND status = 'active'
        """).fetchall()
        
        # Create notifications for all members
        for member in members:
            conn.execute("""
                INSERT INTO notifications (user_id, type, title, message, link, created_at, is_read)
                VALUES (?, 'announcement', ?, ?, '/publicity/announcements', datetime('now'), 0)
            """, (member['id'], title, content[:200]))
        
        conn.commit()
        
        flash(f'✅ Announcement posted and sent to {len(members)} members!', 'success')
        
    except sqlite3.Error as e:
        print(f"❌ Database Error: {str(e)}")
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/announcements")


@app.route("/publicity/announcement/delete/<int:id>", methods=['POST'])
def delete_announcement(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    
    try:
        conn.execute("DELETE FROM announcements WHERE id = ?", (id,))
        conn.commit()
        flash('Announcement deleted successfully!', 'success')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/announcements")


# ============================================================
# EVENTS ROUTES
# ============================================================

@app.route("/publicity/events")
def publicity_events():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        events = conn.execute("""
            SELECT * FROM events 
            ORDER BY event_date ASC, event_time ASC
        """).fetchall()
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        conn.close()
    
    return render_template("publicity/publicity-events.html", events=events)


@app.route("/publicity/event/create", methods=['POST'])
def create_event():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    title = request.form.get('title', '').strip()
    event_date = request.form.get('event_date', '').strip()
    event_time = request.form.get('event_time', '').strip()
    location = request.form.get('location', '').strip()
    description = request.form.get('description', '').strip()
    
    if not title or not event_date or not location:
        flash('Please fill in all required fields', 'danger')
        return redirect("/publicity/events")
    
    conn = get_db()
    
    try:
        conn.execute("""
            INSERT INTO events (title, event_date, event_time, location, description, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
        """, (title, event_date, event_time or None, location, description, session.get('user_id')))
        conn.commit()
        
        # Get all active members
        members = conn.execute("""
            SELECT id FROM users WHERE LOWER(role) = 'member' AND status = 'active'
        """).fetchall()
        
        # Create notifications for all members
        for member in members:
            conn.execute("""
                INSERT INTO notifications (user_id, type, title, message, link, created_at, is_read)
                VALUES (?, 'event', ?, ?, '/publicity/events', datetime('now'), 0)
            """, (member['id'], f"📅 New Event: {title}", f"Join us for {title} on {event_date} at {location}"))
        
        conn.commit()
        
        flash(f'✅ Event created and notified {len(members)} members!', 'success')
        
    except sqlite3.Error as e:
        print(f"❌ Database Error: {str(e)}")
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/events")


@app.route("/publicity/event/delete/<int:id>", methods=['POST'])
def delete_event(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    
    try:
        conn.execute("DELETE FROM events WHERE id = ?", (id,))
        conn.commit()
        flash('Event deleted successfully!', 'success')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/events")


# ============================================================
# NEWSLETTER ROUTES
# ============================================================

@app.route("/publicity/newsletters")
def publicity_newsletters():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        total_members = conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member' AND status = 'active'").fetchone()[0]
        newsletters = conn.execute("""
            SELECT n.*,
                   (SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member' AND status = 'active') as recipients
            FROM newsletters n
            ORDER BY n.sent_date DESC
        """).fetchall()
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        conn.close()
    
    return render_template("publicity/publicity-newsletters.html", 
                          newsletters=newsletters, 
                          total_members=total_members)


@app.route("/publicity/newsletter/create", methods=['POST'])
def create_newsletter():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    
    if not title or not content:
        flash('Please fill in all fields', 'danger')
        return redirect("/publicity/newsletters")
    
    conn = get_db()
    
    try:
        # Get all active members
        members = conn.execute("""
            SELECT id FROM users WHERE LOWER(role) = 'member' AND status = 'active'
        """).fetchall()
        
        # Insert newsletter
        conn.execute("""
            INSERT INTO newsletters (title, content, sent_date, recipients, created_by)
            VALUES (?, ?, datetime('now'), ?, ?)
        """, (title, content, len(members), session.get('user_id')))
        conn.commit()
        
        # Create notifications for all members
        for member in members:
            conn.execute("""
                INSERT INTO notifications (user_id, type, title, message, link, created_at, is_read)
                VALUES (?, 'newsletter', ?, ?, '/publicity/newsletters', datetime('now'), 0)
            """, (member['id'], f"📰 Newsletter: {title}", content[:200]))
        
        conn.commit()
        
        flash(f'✅ Newsletter sent to {len(members)} members!', 'success')
        
    except sqlite3.Error as e:
        print(f"❌ Database Error: {str(e)}")
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/newsletters")


@app.route("/publicity/newsletter/delete/<int:id>", methods=['POST'])
def delete_newsletter(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    
    try:
        conn.execute("DELETE FROM newsletters WHERE id = ?", (id,))
        conn.commit()
        flash('Newsletter deleted successfully!', 'success')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/newsletters")


# ============================================================
# SOCIAL MEDIA ROUTES
# ============================================================

@app.route("/publicity/social")
def publicity_social():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        social_posts = conn.execute("""
            SELECT * FROM social_posts
            ORDER BY created_at DESC
        """).fetchall()
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        conn.close()
    
    return render_template("publicity/publicity-social.html", social_posts=social_posts)


@app.route("/publicity/social/create", methods=['POST'])
def create_social_post():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    platform = request.form.get('platform', '').strip()
    post = request.form.get('post', '').strip()
    
    if not platform or not post:
        flash('Please fill in all fields', 'danger')
        return redirect("/publicity/social")
    
    conn = get_db()
    
    try:
        conn.execute("""
            INSERT INTO social_posts (platform, content, created_at, created_by)
            VALUES (?, ?, datetime('now'), ?)
        """, (platform, post, session.get('user_id')))
        conn.commit()
        
        flash(f'✅ Posted to {platform}!', 'success')
        
    except sqlite3.Error as e:
        print(f"❌ Database Error: {str(e)}")
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/social")


@app.route("/publicity/social/delete/<int:id>", methods=['POST'])
def delete_social_post(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    
    try:
        conn.execute("DELETE FROM social_posts WHERE id = ?", (id,))
        conn.commit()
        flash('Social post deleted successfully!', 'success')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/social")


# ============================================================
# NOTIFICATIONS ROUTES
# ============================================================

@app.route("/publicity/notifications")
def publicity_notifications():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        notifications = conn.execute("""
            SELECT n.*, u.full_name as member_name
            FROM notifications n
            JOIN users u ON n.user_id = u.id
            ORDER BY n.created_at DESC
            LIMIT 100
        """).fetchall()
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        conn.close()
    
    return render_template("publicity/publicity-notifications.html", notifications=notifications)


@app.route("/publicity/notification/delete/<int:id>", methods=['POST'])
def delete_notification(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    
    try:
        conn.execute("DELETE FROM notifications WHERE id = ?", (id,))
        conn.commit()
        flash('Notification deleted!', 'success')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/notifications")


@app.route("/publicity/notifications/clear", methods=['POST'])
def clear_all_notifications():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    
    try:
        conn.execute("DELETE FROM notifications")
        conn.commit()
        flash('All notifications cleared!', 'success')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        conn.close()
    
    return redirect("/publicity/notifications")


# ============================================================
# MEMBER - NOTIFICATIONS (Updated for staff)
# ============================================================
@app.route("/member/notifications")
def member_notifications():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        notifications = db.execute("""
            SELECT * FROM notifications
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
        
        db.close()
        
        return render_template("member/notifications.html", notifications=notifications)
        
    except Exception as e:
        db.close()
        flash(f"Error loading notifications: {str(e)}", "danger")
        return redirect(url_for("member_dashboard"))


# ============================================================
# MARK NOTIFICATION AS READ
# ============================================================
@app.route("/notification/mark-read/<int:notification_id>", methods=['POST'])
def mark_notification_read(notification_id):
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    db = get_db()
    try:
        db.execute("""
            UPDATE notifications 
            SET is_read = 1 
            WHERE id = ? AND user_id = ?
        """, (notification_id, session['user_id']))
        db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route("/notifications/mark-all-read", methods=['POST'])
def mark_all_notifications_read():
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    db = get_db()
    try:
        db.execute("""
            UPDATE notifications 
            SET is_read = 1 
            WHERE user_id = ?
        """, (session['user_id'],))
        db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/savings')
def savings():
    return render_template('/website/savings.html')

@app.route('/credit')
def credit():
    return render_template('website/credit.html')

@app.route('/welfare')
def welfare():
    return render_template('website/welfare.html')


# ============================================================
# MEMBER <-> STAFF CHAT (member side)
# Member picks a staff member to chat with.
# ============================================================
STAFF_ROLES = ["admin", "chairperson", "treasurer", "secretary", "publicity"]


@app.route("/member/chat")
def member_chat():
    """Member chat page — shows list of staff, then thread with chosen staff."""
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    db.row_factory = sqlite3.Row
    member = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    db.close()

    if not member:
        flash("Member not found", "danger")
        return redirect("/login")

    return render_template(
        "member/member-chat.html",
        member=member,
        active_page="chat",
        unread_count=0,
        notifications=[]
    )


@app.route("/api/member-chat/staff-list")
def api_member_staff_list():
    """List of staff the member can chat with."""
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    member_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row

    placeholders = ",".join("?" * len(STAFF_ROLES))

    staff = db.execute(f"""
        SELECT
            u.id,
            u.full_name,
            u.sacco_number,
            u.role,
            u.status,
            (
                SELECT COUNT(*)
                FROM chat_messages cm
                WHERE cm.sender_id = u.id
                  AND cm.receiver_id = ?
                  AND cm.is_read = 0
            ) AS unread_count
        FROM users u
        WHERE LOWER(u.role) IN ({placeholders})
          AND u.status = 'active'
        ORDER BY
            CASE LOWER(u.role)
                WHEN 'treasurer'   THEN 1
                WHEN 'secretary'   THEN 2
                WHEN 'admin'       THEN 3
                WHEN 'chairperson' THEN 4
                WHEN 'publicity'   THEN 5
                ELSE 9
            END,
            u.full_name ASC
    """, (member_id, *STAFF_ROLES)).fetchall()

    db.close()

    return jsonify({
        "success": True,
        "staff": [{
            "id": s["id"],
            "full_name": s["full_name"],
            "sacco_number": s["sacco_number"] or "",
            "role": s["role"],
            "status": s["status"] or "active",
            "unread_count": s["unread_count"] or 0,
        } for s in staff]
    })


@app.route("/api/member-chat/messages/<int:staff_id>")
def api_member_chat_messages(staff_id):
    """Messages between THIS member and one staff member."""
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    member_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row

    target = db.execute("SELECT id, role, full_name FROM users WHERE id = ?", (staff_id,)).fetchone()
    if not target:
        db.close()
        return jsonify({"success": False, "message": "Staff not found"}), 404
    if (target["role"] or "").lower() not in STAFF_ROLES:
        db.close()
        return jsonify({"success": False, "message": "Can only chat with staff"}), 403

    msgs = db.execute("""
        SELECT
            cm.id,
            cm.sender_id,
            cm.receiver_id,
            cm.message,
            cm.is_read,
            cm.created_at,
            u.full_name AS sender_name,
            u.role      AS sender_role
        FROM chat_messages cm
        JOIN users u ON cm.sender_id = u.id
        WHERE (cm.sender_id = ? AND cm.receiver_id = ?)
           OR (cm.sender_id = ? AND cm.receiver_id = ?)
        ORDER BY cm.created_at ASC
        LIMIT 300
    """, (member_id, staff_id, staff_id, member_id)).fetchall()

    # Mark messages FROM this staff as read by the member
    db.execute("""
        UPDATE chat_messages
        SET is_read = 1
        WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
    """, (staff_id, member_id))
    db.commit()
    db.close()

    return jsonify({
        "success": True,
        "messages": [dict(m) for m in msgs]
    })


@app.route("/api/member-chat/send", methods=["POST"])
def api_member_chat_send():
    """Member sends to a staff member."""
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    body = (data.get("message") or "").strip()
    staff_id = data.get("staff_id")

    if not staff_id:
        return jsonify({"success": False, "message": "Pick a staff member first"}), 400
    if not body:
        return jsonify({"success": False, "message": "Message is empty"}), 400
    if len(body) > 2000:
        return jsonify({"success": False, "message": "Message too long"}), 400

    member_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row

    target = db.execute("SELECT id, role, full_name FROM users WHERE id = ?", (staff_id,)).fetchone()
    if not target:
        db.close()
        return jsonify({"success": False, "message": "Staff not found"}), 404
    if (target["role"] or "").lower() not in STAFF_ROLES:
        db.close()
        return jsonify({"success": False, "message": "Members cannot message other members"}), 403

    cursor = db.cursor()
    cursor.execute("""
        INSERT INTO chat_messages
            (sender_id, receiver_id, message, message_type, is_read)
        VALUES (?, ?, ?, 'general', 0)
    """, (member_id, staff_id, body))

    sender_name = session.get("full_name", "Member")
    db.execute("""
        INSERT INTO notifications
            (user_id, type, title, message, link, created_at, is_read)
        VALUES (?, 'chat', ?, ?, '/staff/chat', datetime('now'), 0)
    """, (staff_id, f"📩 New message from {sender_name} (Member)", body[:200]))

    db.commit()
    db.close()

    return jsonify({"success": True, "message": "Sent"})

# ============================================================
# PUBLICITY CHAT — separate from chat_api.py
# Renders a member-chat-style page for publicity staff only.
# ============================================================
PUBLICITY_ALLOWED_ROLES = ["publicity"]     # only publicity can use these routes
CHAT_STAFF_ROLES = ["admin", "chairperson", "treasurer", "secretary", "publicity"]


def _require_publicity():
    """Return None if OK, else a redirect response."""
    if "user_id" not in session:
        return redirect("/login")
    role = (session.get("role") or "").lower()
    if role not in PUBLICITY_ALLOWED_ROLES:
        return redirect("/login")
    return None


@app.route("/publicity/chat")
def publicity_chat():
    """Publicity chat page — pick any member or other staff to chat with."""
    bad = _require_publicity()
    if bad:
        return bad

    db = get_db()
    db.row_factory = sqlite3.Row
    me = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    db.close()

    if not me:
        flash("User not found", "danger")
        return redirect("/login")

    return render_template(
        "publicity/publicity-chat.html",
        member=me,
        active_page="chat",
        unread_count=0,
        notifications=[]
    )


@app.route("/api/publicity-chat/contacts")
def api_publicity_chat_contacts():
    """Everyone publicity can chat with: all members + all other staff."""
    bad = _require_publicity()
    if bad:
        return jsonify({"success": False, "message": "Access denied"}), 403

    me_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(CHAT_STAFF_ROLES))

    members = db.execute("""
        SELECT
            u.id, u.full_name, u.sacco_number, u.role, u.status,
            (SELECT COUNT(*) FROM chat_messages cm
             WHERE cm.sender_id = u.id AND cm.receiver_id = ? AND cm.is_read = 0
            ) AS unread_count
        FROM users u
        WHERE LOWER(u.role) = 'member' AND u.id != ?
        ORDER BY u.full_name ASC
    """, (me_id, me_id)).fetchall()

    staff = db.execute(f"""
        SELECT
            u.id, u.full_name, u.sacco_number, u.role, u.status,
            (SELECT COUNT(*) FROM chat_messages cm
             WHERE cm.sender_id = u.id AND cm.receiver_id = ? AND cm.is_read = 0
            ) AS unread_count
        FROM users u
        WHERE LOWER(u.role) IN ({placeholders}) AND u.id != ?
        ORDER BY u.full_name ASC
    """, (me_id, *CHAT_STAFF_ROLES, me_id)).fetchall()

    db.close()

    contacts = []
    for row in staff:
        d = dict(row); d["type"] = "staff"; d["sacco_number"] = d.get("sacco_number") or ""
        contacts.append(d)
    for row in members:
        d = dict(row); d["type"] = "member"; d["sacco_number"] = d.get("sacco_number") or ""
        contacts.append(d)

    return jsonify({"success": True, "contacts": contacts})


@app.route("/api/publicity-chat/messages/<int:other_id>")
def api_publicity_chat_messages(other_id):
    bad = _require_publicity()
    if bad:
        return jsonify({"success": False, "message": "Access denied"}), 403

    me_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row

    target = db.execute("SELECT id FROM users WHERE id = ?", (other_id,)).fetchone()
    if not target:
        db.close()
        return jsonify({"success": False, "message": "Contact not found"}), 404

    msgs = db.execute("""
        SELECT cm.id, cm.sender_id, cm.receiver_id, cm.message, cm.is_read,
               cm.created_at, u.full_name AS sender_name, u.role AS sender_role
        FROM chat_messages cm
        JOIN users u ON cm.sender_id = u.id
        WHERE (cm.sender_id = ? AND cm.receiver_id = ?)
           OR (cm.sender_id = ? AND cm.receiver_id = ?)
        ORDER BY cm.created_at ASC
        LIMIT 300
    """, (me_id, other_id, other_id, me_id)).fetchall()

    db.execute("""
        UPDATE chat_messages SET is_read = 1
        WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
    """, (other_id, me_id))
    db.commit()
    db.close()

    return jsonify({"success": True, "messages": [dict(m) for m in msgs]})


@app.route("/api/publicity-chat/send", methods=["POST"])
def api_publicity_chat_send():
    bad = _require_publicity()
    if bad:
        return jsonify({"success": False, "message": "Access denied"}), 403

    data = request.get_json(silent=True) or {}
    other_id = data.get("receiver_id")
    body = (data.get("message") or "").strip()

    if not other_id:
        return jsonify({"success": False, "message": "Pick a contact first"}), 400
    if not body:
        return jsonify({"success": False, "message": "Message is empty"}), 400

    try:
        other_id = int(other_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid contact"}), 400

    me_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row

    target = db.execute("SELECT id, role FROM users WHERE id = ?", (other_id,)).fetchone()
    if not target:
        db.close()
        return jsonify({"success": False, "message": "Contact not found"}), 404

    db.execute("""
        INSERT INTO chat_messages (sender_id, receiver_id, message, message_type, is_read)
        VALUES (?, ?, ?, 'general', 0)
    """, (me_id, other_id, body))

    sender_name = session.get("full_name") or "Publicity"
    link = "/staff/chat" if (target["role"] or "").lower() in CHAT_STAFF_ROLES else "/member/chat"

    try:
        db.execute("""
            INSERT INTO notifications (user_id, type, title, message, link, created_at, is_read)
            VALUES (?, 'chat', ?, ?, ?, datetime('now'), 0)
        """, (other_id, f"📩 New message from {sender_name} (Publicity)", body[:200], link))
    except Exception as e:
        print("Notification insert failed (non-fatal):", e)

    db.commit()
    db.close()
    return jsonify({"success": True, "message": "Sent"})

# ============================================================
# RUN THE APP
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)