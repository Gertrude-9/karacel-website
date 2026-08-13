from flask import Flask, jsonify, render_template, request, redirect, session, flash, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import re
import os

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


def create_database():
    conn = get_db()
    cursor = conn.cursor()

    # Create users table
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
        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Create loans table with all required columns
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
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # ============================================================
# CREATE NOTIFICATIONS TABLE
# ============================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        notification_type TEXT DEFAULT 'general',
        is_read INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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

    # Create repayments table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS repayments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            payment_date TEXT NOT NULL,
            payment_method TEXT,
            transaction_ref TEXT,
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (loan_id) REFERENCES loans(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Create savings deposits table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS savings_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
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

    conn.commit()
    conn.close()


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
@app.route("/")
def splash():
    return render_template("splash.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        sacco_number = request.form["sacco_number"].strip().upper()
        password = request.form["password"].strip()

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE sacco_number = ? AND password = ?",
            (sacco_number, password)
        ).fetchone()
        conn.close()

        if user:
            session["user_id"] = user["id"]
            session["sacco_number"] = user["sacco_number"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]

            if user["role"] == "admin":
                return redirect("/admin/dashboard")
            elif user["role"] == "treasurer":
                return redirect("/treasurer/dashboard")
            elif user["role"] == "secretary":
                return redirect("/secretary/dashboard")
            elif user["role"] == "publicity":
                return redirect("/publicity/dashboard")
            else:
                return redirect("/member/dashboard")

        flash("Invalid SACCO number or password", "error")

    return render_template("login.html")


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

    # ============================================================
    # GET SETTINGS
    # ============================================================
    settings = {}
    try:
        conn.row_factory = sqlite3.Row
        settings_row = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings_row:
            settings = dict(settings_row)
        else:
            # Default settings if none exist
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
            'max_tenure': 24
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
        settings=settings  # <-- ADDED THIS LINE
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
# TREASURER DASHBOARD
# ============================================================
@app.route("/treasurer/dashboard")
def treasurer_dashboard():
    if session.get("role") not in ["treasurer", "secretary"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    try:
        # Statistics
        total_members = conn.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member'").fetchone()[0]
        total_savings = conn.execute("SELECT COALESCE(SUM(savings_balance), 0) FROM users WHERE LOWER(role) = 'member'").fetchone()[0]
        total_deposits = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM savings_deposits").fetchone()[0]
        monthly_deposits = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM savings_deposits WHERE deposit_date >= date('now', 'start of month')").fetchone()[0]
        
        # Loan counts by status
        pending_loans = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'pending'").fetchone()[0]
        active_loans_count = conn.execute("SELECT COUNT(*) FROM loans WHERE status IN ('disbursed', 'active')").fetchone()[0]
        approved_loans_count = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'approved'").fetchone()[0]
        completed_loans_count = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
        rejected_loans_count = conn.execute("SELECT COUNT(*) FROM loans WHERE status = 'rejected'").fetchone()[0]
        total_repayments = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM repayments WHERE status = 'completed'").fetchone()[0]
        
        # All members
        members = conn.execute("SELECT * FROM users WHERE LOWER(role) = 'member' AND status = 'active' ORDER BY full_name").fetchall()
        
        # All deposits
        all_deposits = conn.execute("""
            SELECT sd.*, u.full_name, u.sacco_number
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            ORDER BY sd.deposit_date DESC, sd.created_at DESC
        """).fetchall()
        
        # PENDING LOAN APPLICATIONS
        pending_loan_applications = conn.execute("""
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
        
        # APPROVED LOANS (awaiting disbursement)
        approved_loans_list = conn.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number,
                COALESCE((
                    SELECT SUM(amount) 
                    FROM repayments 
                    WHERE loan_id = l.id AND status = 'completed'
                ), 0) as total_paid
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status = 'approved'
            ORDER BY l.application_date DESC
        """).fetchall()
        
        # DISBURSED/ACTIVE LOANS
        active_loans_list = conn.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number,
                u.phone,
                COALESCE((
                    SELECT SUM(amount) 
                    FROM repayments 
                    WHERE loan_id = l.id AND status = 'completed'
                ), 0) as total_paid
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status IN ('disbursed', 'active')
            ORDER BY l.application_date DESC
        """).fetchall()
        
        # COMPLETED LOANS LIST
        completed_loans_list = conn.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number,
                COALESCE((
                    SELECT SUM(amount) 
                    FROM repayments 
                    WHERE loan_id = l.id AND status = 'completed'
                ), 0) as total_paid,
                COALESCE((
                    SELECT COUNT(*) 
                    FROM repayments 
                    WHERE loan_id = l.id AND status = 'completed'
                ), 0) as payment_count
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status = 'completed'
            ORDER BY l.completed_date DESC, l.application_date DESC
        """).fetchall()
        
        # REJECTED LOANS
        rejected_loans_list = conn.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status = 'rejected'
            ORDER BY l.application_date DESC
        """).fetchall()
        
        # Recent deposits
        recent_deposits = conn.execute("""
            SELECT sd.*, u.full_name, u.sacco_number
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            ORDER BY sd.deposit_date DESC
            LIMIT 20
        """).fetchall()
        
        # Recent repayments
        recent_repayments = conn.execute("""
            SELECT 
                r.*, 
                u.full_name, 
                u.sacco_number, 
                l.loan_number,
                COALESCE(r.interest_paid, 0) as interest_paid,
                COALESCE(r.principal_paid, 0) as principal_paid
            FROM repayments r
            JOIN users u ON r.user_id = u.id
            JOIN loans l ON r.loan_id = l.id
            WHERE r.status = 'completed'
            ORDER BY r.payment_date DESC
            LIMIT 20
        """).fetchall()
        
        # ALL LOAN APPLICATIONS - WITH GUARANTORS
        loan_applications = conn.execute("""
            SELECT 
                l.*,
                u.full_name,
                u.sacco_number,
                COALESCE((
                    SELECT SUM(amount) 
                    FROM repayments 
                    WHERE loan_id = l.id AND status = 'completed'
                ), 0) as total_paid
            FROM loans l
            JOIN users u ON l.user_id = u.id
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
        
        print("=" * 60)
        print("🔍 TREASURER DASHBOARD LOADED SUCCESSFULLY")
        print(f"📊 Total Members: {total_members}")
        print(f"📊 Pending Loans: {pending_loans}")
        print("=" * 60)
        
    except sqlite3.Error as e:
        print(f"❌ Database Error: {str(e)}")
        flash(f'Database error: {str(e)}', 'danger')
        return redirect(url_for('login'))
    finally:
        conn.close()
    
    return render_template(
        "treasurer/treasurer-dashboard.html",
        total_members=total_members,
        total_savings=total_savings,
        total_deposits=total_deposits,
        monthly_deposits=monthly_deposits,
        pending_loans=pending_loans,
        active_loans=active_loans_count,
        approved_loans=approved_loans_count,
        completed_loans=completed_loans_count,
        rejected_loans=rejected_loans_count,
        active_loans_list=active_loans_list,
        total_repayments=total_repayments,
        members=members,
        all_deposits=all_deposits,
        pending_loan_applications=pending_loan_applications,
        approved_loans_list=approved_loans_list,
        completed_loans_list=completed_loans_list,
        rejected_loans_list=rejected_loans_list,
        all_loan_applications=all_loan_applications,
        recent_deposits=recent_deposits,
        recent_repayments=recent_repayments,
        now=datetime.now()
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
# TREASURER - VIEW LOAN DETAILS
# ============================================================
@app.route("/treasurer/loan/view/<int:loan_id>")
def treasurer_view_loan(loan_id):
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
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
            role='treasurer'
        )
        
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard'))

# ============================================================
# TREASURER - APPROVE / REJECT LOAN
# ============================================================

@app.route("/treasurer/loan/action/<int:loan_id>", methods=["POST"])
def treasurer_approve_loan(loan_id):

    print(f"🔍 Loan action called for loan {loan_id}")

    if "user_id" not in session:
        return jsonify({
            'success': False,
            'message': 'Please login first'
        }), 401

    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({
            'success': False,
            'message': 'Access denied'
        }), 403

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            'success': False,
            'message': 'Invalid request'
        }), 400

    action = data.get('action')
    reason = (data.get('reason') or '').strip()

    if action not in ['approve', 'reject']:
        return jsonify({
            'success': False,
            'message': 'Invalid action'
        }), 400

    db = get_db()
    db.row_factory = sqlite3.Row

    try:

        # ====================================================
        # GET LOAN + APPLICANT
        # ====================================================

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

            return jsonify({
                'success': False,
                'message': 'Loan not found'
            }), 404


        # ====================================================
        # REJECT LOAN
        # ====================================================

        if action == 'reject':

            if not reason:
                db.close()

                return jsonify({
                    'success': False,
                    'message': 'Rejection reason required'
                }), 400


            # -----------------------------------------------
            # UPDATE LOAN
            # -----------------------------------------------

            db.execute("""
                UPDATE loans
                SET
                    status = 'rejected',
                    rejected_date = datetime('now'),
                    rejection_reason = ?,
                    rejected_by = ?
                WHERE id = ?
            """, (
                reason,
                session.get('full_name', 'Treasurer'),
                loan_id
            ))


            # -----------------------------------------------
            # CREATE NOTIFICATION FOR APPLICANT
            # -----------------------------------------------

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
                (
                    f"Your loan application {loan['loan_number']} "
                    f"has been rejected.\n\n"
                    f"Reason: {reason}"
                ),
                'loan_rejection'
            ))


            # -----------------------------------------------
            # SAVE CHANGES
            # -----------------------------------------------

            db.commit()
            db.close()

            return jsonify({
                'success': True,
                'message': (
                    'Loan rejected successfully. '
                    'The applicant has been notified.'
                )
            })


        # ====================================================
        # APPROVE LOAN
        # ====================================================

        if loan['status'] != 'pending':

            db.close()

            return jsonify({
                'success': False,
                'message': f'Loan is {loan["status"]}, not pending'
            }), 400


        # -----------------------------------------------
        # CHECK 10% SAVINGS
        # -----------------------------------------------

        required = float(loan['amount']) * 0.10
        savings = float(loan['savings_balance'] or 0)

        if savings < required:

            db.close()

            return jsonify({
                'success': False,
                'message': (
                    f'Member needs 10% savings '
                    f'(UGX {required:,.0f}). '
                    f'Current: UGX {savings:,.0f}'
                )
            }), 400


        # -----------------------------------------------
        # APPROVAL DATES
        # -----------------------------------------------

        today = datetime.now().strftime('%Y-%m-%d')

        end_date = (
            datetime.now() + timedelta(days=30)
        ).strftime('%Y-%m-%d')


        # -----------------------------------------------
        # CURRENT BALANCE
        # -----------------------------------------------

        balance = (
            loan['total_repayment']
            if loan['total_repayment'] is not None
            else loan['amount']
        )


        # -----------------------------------------------
        # UPDATE LOAN
        # -----------------------------------------------

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
        """, (
            today,
            today,
            end_date,
            balance,
            session.get('full_name', 'Treasurer'),
            loan_id
        ))


        # -----------------------------------------------
        # NOTIFY APPLICANT OF APPROVAL
        # -----------------------------------------------

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
            (
                f"Your loan application {loan['loan_number']} "
                f"has been approved. "
                f"Please wait for disbursement."
            ),
            'loan_approval'
        ))


        # -----------------------------------------------
        # SAVE
        # -----------------------------------------------

        db.commit()
        db.close()

        return jsonify({
            'success': True,
            'message': (
                '✅ Loan approved! '
                'Waiting for Chairman disbursement.'
            )
        })


    # ====================================================
    # ERROR HANDLING
    # ====================================================

    except Exception as e:

        print(f"❌ Error processing loan action: {e}")

        try:
            db.rollback()
            db.close()
        except:
            pass

        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
# ============================================================
# TREASURER - DISBURSE LOAN (CLEAN VERSION)
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
        # Get loan details with member info
        loan = conn.execute("""
            SELECT l.*, u.full_name, u.sacco_number, u.id as member_id
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            conn.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404
        
        # Check if loan can accept payments
        if loan['status'] not in ['approved', 'disbursed', 'active']:
            conn.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot make payment on loan with status: {loan["status"]}'
            }), 400
        
        if loan['status'] == 'completed':
            conn.close()
            return jsonify({'success': False, 'message': 'Loan is already fully paid'}), 400
        
        # Calculate current balance
        current_balance = float(loan['current_balance'] or loan['amount'] or 0)
        
        # Check if payment amount is valid
        if amount > current_balance:
            conn.close()
            return jsonify({
                'success': False, 
                'message': f'Payment amount (UGX {amount:,.0f}) exceeds current balance (UGX {current_balance:,.0f})'
            }), 400
        
        # Begin transaction
        conn.execute("BEGIN TRANSACTION")
        
        # Record the repayment
        conn.execute("""
            INSERT INTO repayments (
                loan_id, user_id, amount, payment_date, payment_method, status
            )
            VALUES (?, ?, ?, date('now'), ?, 'completed')
        """, (loan_id, loan['member_id'], amount, payment_method))
        
        # Calculate new balance
        new_balance = current_balance - amount
        
        # ============================================================
        # AUTO-COMPLETE: If balance is 50 UGX or less, mark as completed
        # ============================================================
        COMPLETION_THRESHOLD = 50
        is_completed = new_balance <= COMPLETION_THRESHOLD
        
        if is_completed:
            # Write off the remaining small balance and mark as completed
            conn.execute("""
                UPDATE loans 
                SET current_balance = 0,
                    status = 'completed',
                    completed_date = date('now'),
                    last_payment_date = date('now'),
                    last_payment_amount = ?
                WHERE id = ?
            """, (amount, loan_id))
            status = 'completed'
            message = f'✅ LOAN COMPLETED! Final payment of UGX {amount:,.0f} made. Remaining balance of UGX {new_balance:,.0f} has been written off.'
            print(f"✅ Loan {loan_id} marked as COMPLETED. Balance: {new_balance} UGX (<= {COMPLETION_THRESHOLD} UGX threshold)")
        else:
            # Update balance only
            conn.execute("""
                UPDATE loans 
                SET current_balance = ?,
                    status = 'active',
                    last_payment_date = date('now'),
                    last_payment_amount = ?
                WHERE id = ?
            """, (new_balance, amount, loan_id))
            status = 'active'
            message = f'✅ Payment of UGX {amount:,.0f} recorded successfully! Remaining: UGX {new_balance:,.0f}'
            print(f"💰 Payment recorded for loan {loan_id}. New balance: {new_balance} UGX")
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': message,
            'new_balance': new_balance,
            'status': status,
            'is_completed': is_completed
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

## ============================================================
# TREASURER - ENTER REPAYMENT (FIXED)
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
        
        # Get completed loans count for sidebar badge
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
        
        # Calculate interest and principal - FIXED: Use direct indexing instead of .get()
        interest_paid = 0
        principal_paid = 0
        
        total_interest = float(loan['interest_amount'] or 0)
        # FIXED: Access interest_paid directly using index, not .get()
        interest_paid_so_far = float(loan['interest_paid'] if loan['interest_paid'] is not None else 0)
        interest_remaining = total_interest - interest_paid_so_far
        
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
                payment_date, payment_method, transaction_ref, notes,
                balance_after, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
        """, (
            loan_id, 
            loan['member_id'], 
            amount, 
            interest_paid, 
            principal_paid,
            current_datetime, 
            payment_method, 
            transaction_ref,
            notes,
            new_balance
        ))
        
        # ============================================================
        # AUTO-COMPLETE: If balance is 50 UGX or less, mark as completed
        # ============================================================
        COMPLETION_THRESHOLD = 50
        is_completed = new_balance <= COMPLETION_THRESHOLD
        
        if is_completed:
            # Write off the remaining small balance and mark as completed
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
            flash(f'✅ LOAN COMPLETED! Final payment of UGX {amount:,.0f} made. Remaining balance of UGX {new_balance:,.0f} has been written off.', 'success')
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
# TREASURER - SAVINGS DEPOSIT (FIXED)
# ============================================================
@app.route("/treasurer/savings/deposit", methods=["GET", "POST"])
def treasurer_savings_deposit():
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        flash('Access denied. Only treasurer can record deposits.', 'danger')
        return redirect("/login")
    
    if request.method == "POST":
        user_id = request.form.get('user_id')
        amount = float(request.form.get('amount', 0))
        deposit_date = request.form.get('deposit_date', datetime.now().strftime('%Y-%m-%d'))
        payment_method = request.form.get('payment_method', 'cash')
        receipt_number = request.form.get('receipt_number', '')
        notes = request.form.get('notes', '')
        
        if amount <= 0:
            flash('Amount must be greater than 0', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))
        
        db = get_db()
        cursor = db.cursor()
        
        cursor.execute("""
            INSERT INTO savings_deposits (user_id, amount, deposit_date, payment_method, receipt_number, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, amount, deposit_date, payment_method, receipt_number, notes))
        
        cursor.execute("UPDATE users SET savings_balance = savings_balance + ? WHERE id = ?", (amount, user_id))
        
        db.commit()
        db.close()
        
        flash(f'Savings deposit of UGX {amount:,.0f} recorded successfully!', 'success')
        return redirect(url_for('treasurer_dashboard'))
    
    db = get_db()
    members = db.execute("SELECT * FROM users WHERE LOWER(role) = 'member' AND status = 'active' ORDER BY full_name").fetchall()
    
    # Get completed loans count for sidebar badge
    completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
    
    db.close()
    
    return render_template(
        "treasurer/savings-deposit.html", 
        members=members,
        completed_loans=completed_loans
    )

# ============================================================
# TREASURER - ADD MEMBER
# ============================================================
@app.route("/treasurer/members/add", methods=["GET", "POST"])
def treasurer_add_members():
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        flash('Access denied. Only Treasurer, Admin, or Secretary can register members.', 'danger')
        return redirect("/login")
    
    # ============================================================
    # GET COMPLETED LOANS COUNT FOR SIDEBAR BADGE
    # ============================================================
    db = get_db()
    completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
    db.close()
    
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
            return render_template("treasurer/add-member.html", completed_loans=completed_loans)
        
        db = get_db()
        
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
                full_name, gender, dob, sacco_number,
                email, phone, address,
                password, role, status,
                savings_balance,
                next_of_kin_name, relationship, next_of_kin_phone
            ))
            
            user_id = cursor.lastrowid
            
            if savings_balance > 0:
                cursor.execute("""
                    INSERT INTO savings_deposits (
                        user_id, amount, deposit_date, payment_method, receipt_number, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    user_id,
                    savings_balance,
                    datetime.now().strftime('%Y-%m-%d'),
                    'registration',
                    f'REG-{sacco_number}',
                    f'Initial registration savings for {full_name}'
                ))
            
            db.commit()
            db.close()
            
            flash(f'Member "{full_name}" registered successfully!', 'success')
            return redirect(url_for('treasurer_dashboard'))
            
        except Exception as e:
            db.rollback()
            db.close()
            flash(f'Error registering member: {str(e)}', 'danger')
            return render_template("treasurer/add-member.html", completed_loans=completed_loans)
    
    return render_template("treasurer/add-member.html", completed_loans=completed_loans)


# ============================================================
# MEMBER DASHBOARD
# ============================================================
@app.route("/member/dashboard")
def member_dashboard():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
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
        # TOTAL SAVINGS
        # ============================================================

        total_savings = db.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM savings_deposits
            WHERE user_id = ?
        """, (user_id,)).fetchone()['total']


        # ============================================================
        # LOANS
        # ============================================================

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

            total_loans_taken += float(
                loan.get('amount', 0) or 0
            )

            loan_total = float(
                loan.get('total_repayment')
                or loan.get('amount', 0)
                or 0
            )

            total_paid = float(
                loan.get('total_paid', 0) or 0
            )

            remaining_balance = max(
                0,
                loan_total - total_paid
            )

            loan['remaining_balance'] = remaining_balance

            if loan.get('status') in [
                'approved',
                'disbursed',
                'active'
            ]:
                active_loans_count += 1
                active_loans_balance += remaining_balance

            loans.append(loan)


        # ============================================================
        # SAVINGS DEPOSITS
        # ============================================================

        savings_deposits = db.execute("""
            SELECT
                id,
                amount,
                deposit_date,
                payment_method,
                receipt_number,
                notes,
                created_at
            FROM savings_deposits
            WHERE user_id = ?
            ORDER BY deposit_date DESC
        """, (user_id,)).fetchall()


        # ============================================================
        # REPAYMENTS
        # ============================================================

        repayments = db.execute("""
            SELECT
                r.id,
                r.loan_id,
                r.user_id,
                r.amount,
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


        # ============================================================
        # GUARANTORS
        # ============================================================

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


        # ============================================================
        # NOTIFICATIONS
        # ============================================================

        notifications = db.execute("""
            SELECT
                id,
                user_id,
                title,
                message,
                notification_type,
                is_read,
                created_at
            FROM notifications
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()


        # ============================================================
        # UNREAD NOTIFICATIONS COUNT
        # ============================================================

        unread_notifications_count = db.execute("""
            SELECT COUNT(*) AS count
            FROM notifications
            WHERE user_id = ?
            AND is_read = 0
        """, (user_id,)).fetchone()['count']


        # ============================================================
        # CLOSE DATABASE
        # ============================================================

        db.close()


        # ============================================================
        # MEMBER DASHBOARD
        # ============================================================

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

            # NOTIFICATIONS
            notifications=notifications,
            unread_notifications_count=unread_notifications_count
        )


    except Exception as e:

        import traceback
        traceback.print_exc()

        try:
            db.close()
        except:
            pass

        flash(
            f"Error loading dashboard: {str(e)}",
            "danger"
        )

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
        
        def get_loan_interest_rate(amount):
            if amount >= 10000 and amount <= 1999999:
                return 5
            elif amount >= 2000000 and amount <= 4999999:
                return 3
            elif amount >= 5000000 and amount <= 9999999:
                return 2
            elif amount >= 10000000:
                return 1
            return 0
        
        monthly_rate_percent = get_loan_interest_rate(loan_amount)
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
            0,
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
# OTHER DASHBOARDS
# ============================================================
@app.route("/chairperson/dashboard")
def chairperson_dashboard():
    if session.get("role") != "chairperson":
        return redirect("/login")
    return render_template("chairperson/chairperson-dashboard.html")


@app.route("/secretary/dashboard")
def secretary_dashboard():
    if session.get("role") != "secretary":
        return redirect("/login")
    return render_template("secretary/secretary-dashboard.html")


@app.route("/publicity/dashboard")
def publicity_dashboard():
    if session.get("role") != "publicity":
        return redirect("/login")
    return render_template("publicity/publicity-dashboard.html")


@app.route("/guarantor/tracking")
def guarantor_tracking():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    
    guarantor_loans = db.execute("""
        SELECT gt.*, l.loan_number, l.status, l.application_date,
               u.full_name as member_name
        FROM guarantor_tracking gt
        JOIN loans l ON gt.loan_id = l.id
        JOIN users u ON l.user_id = u.id
        WHERE gt.guarantor_id = ?
        ORDER BY gt.last_updated DESC
    """, (user_id,)).fetchall()
    
    db.close()
    
    return render_template(
        "guarantor/tracking.html",
        guarantor_loans=guarantor_loans
    )


# ============================================================
# ADMIN - FINAL APPROVE/REJECT LOAN
# ============================================================
@app.route("/admin/loan/approve/<int:loan_id>", methods=["POST"])
def admin_approve_loan(loan_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    action = data.get('action')
    reason = data.get('reason', '').strip()
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        loan = db.execute("""
            SELECT l.*, u.full_name, u.email, u.phone, u.id as user_id
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            db.close()
            return jsonify({'error': 'Loan not found'}), 404
        
        if loan['status'] != 'approved':
            db.close()
            return jsonify({'error': f'Loan must be approved first. Current status: {loan["status"]}'}), 400
        
        if action == 'reject':
            if not reason:
                db.close()
                return jsonify({'error': 'Rejection reason is required'}), 400
            
            db.execute("""
                UPDATE loans 
                SET status = 'rejected', 
                    rejected_date = datetime('now'),
                    admin_rejection_reason = ?,
                    rejection_reason = ?,
                    rejected_by = ?
                WHERE id = ?
            """, (reason, reason, session.get('full_name', 'Admin'), loan_id))
            db.commit()
            db.close()
            return jsonify({'success': True, 'message': 'Loan rejected successfully'})
        
        # Final Approve - Disburse the loan
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
        """, (today, today, end_date, end_date, balance, session.get('full_name', 'Admin'), session.get('role', 'admin'), loan_id))
        
        # Create notification for member
        db.execute("""
            INSERT INTO notifications (
                user_id, title, message, notification_type, is_read, created_at
            )
            VALUES (?, ?, ?, ?, 0, datetime('now'))
        """, (
            loan['user_id'],
            'Loan Disbursed',
            f'Your loan {loan["loan_number"]} of UGX {loan["amount"]:,.0f} has been disbursed. Your repayment period starts today.',
            'loan_disbursed'
        ))
        
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': '✅ Loan approved and disbursed successfully!'})
        
    except Exception as e:
        db.rollback()
        db.close()
        print(f"❌ Error in admin approve: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================
# ADMIN - VIEW LOAN DETAILS (Reuses Treasurer Template)
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
        
        guarantors = db.execute("SELECT * FROM loan_guarantors WHERE loan_id = ?", (loan_id,)).fetchall()
        repayments = db.execute("SELECT * FROM repayments WHERE loan_id = ? ORDER BY payment_date DESC", (loan_id,)).fetchall()
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
# ADMIN - GENERATE REPORTS (FIXED)
# ============================================================
@app.route("/admin/reports/generate/<string:report_type>")
def generate_report(report_type):
    if session.get("role") not in ["admin", "chairperson"]:
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
                    next_of_kin_name, next_of_kin_phone, gender, dob, address
                FROM users 
                WHERE LOWER(role) = 'member'
                ORDER BY full_name
            """).fetchall()
            
            # Convert to dictionaries and add loan counts (FIXED)
            members_list = []
            for member in members:
                # Convert sqlite3.Row to dictionary
                member_dict = dict(member)
                loan_count = db.execute("""
                    SELECT COUNT(*) FROM loans WHERE user_id = ? AND status IN ('disbursed', 'active')
                """, (member_dict['id'],)).fetchone()[0]
                member_dict['active_loans'] = loan_count  # ✅ Now this works
                members_list.append(member_dict)
            
            db.close()
            return render_template(
                "admin/reports/member-report.html",
                members=members_list,  # Pass list of dictionaries
                total_members=len(members_list),
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                report_title="Member Report",
                now=datetime.now()
            )
            
        elif report_type == 'financial':
            # Financial summary data
            total_members = db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member'").fetchone()[0]
            total_savings = db.execute("SELECT COALESCE(SUM(savings_balance), 0) FROM users WHERE LOWER(role) = 'member'").fetchone()[0]
            
            # Loan statistics
            total_loans = db.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
            pending_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'pending'").fetchone()[0]
            approved_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'approved'").fetchone()[0]
            disbursed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'disbursed'").fetchone()[0]
            completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
            rejected_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'rejected'").fetchone()[0]
            
            # Total loan amounts
            total_loan_amount = db.execute("SELECT COALESCE(SUM(amount), 0) FROM loans").fetchone()[0]
            total_disbursed = db.execute("SELECT COALESCE(SUM(amount), 0) FROM loans WHERE status IN ('disbursed', 'active', 'completed')").fetchone()[0]
            
            # Repayments
            total_repayments = db.execute("SELECT COALESCE(SUM(amount), 0) FROM repayments WHERE status = 'completed'").fetchone()[0]
            
            # Staff counts
            staff_counts = {
                'treasurer': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'treasurer'").fetchone()[0],
                'secretary': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'secretary'").fetchone()[0],
                'publicity': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = 'publicity'").fetchone()[0],
                'admin': db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) IN ('admin', 'chairperson')").fetchone()[0]
            }
            
            db.close()
            
            return render_template(
                "admin/reports/financial-report.html",
                total_members=total_members,
                total_savings=total_savings,
                total_loans=total_loans,
                pending_loans=pending_loans,
                approved_loans=approved_loans,
                disbursed_loans=disbursed_loans,
                completed_loans=completed_loans,
                rejected_loans=rejected_loans,
                total_loan_amount=total_loan_amount,
                total_disbursed=total_disbursed,
                total_repayments=total_repayments,
                staff_counts=staff_counts,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                report_title="Financial Summary Report",
                now=datetime.now()
            )
            
        elif report_type == 'loans':
            # Get all loans with member details
            loans = db.execute("""
                SELECT 
                    l.*,
                    u.full_name,
                    u.sacco_number,
                    u.phone,
                    u.email,
                    COALESCE((
                        SELECT COUNT(*) FROM repayments 
                        WHERE loan_id = l.id AND status = 'completed'
                    ), 0) as payment_count,
                    COALESCE((
                        SELECT SUM(amount) FROM repayments 
                        WHERE loan_id = l.id AND status = 'completed'
                    ), 0) as total_paid
                FROM loans l
                JOIN users u ON l.user_id = u.id
                ORDER BY l.application_date DESC
            """).fetchall()
            
            # Get loan statistics - convert to list of dicts if needed
            loans_list = [dict(loan) for loan in loans]  # Convert to dictionaries
            
            total_loans = len(loans_list)
            total_amount = sum(l['amount'] for l in loans_list) if loans_list else 0
            total_balance = sum(l.get('current_balance', 0) or 0 for l in loans_list) if loans_list else 0
            
            db.close()
            
            return render_template(
                "admin/reports/loan-report.html",
                loans=loans_list,  # Pass list of dictionaries
                total_loans=total_loans,
                total_amount=total_amount,
                total_balance=total_balance,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                report_title="Loan Portfolio Report",
                now=datetime.now()
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
            
            # Get member savings summaries
            member_savings = db.execute("""
                SELECT 
                    u.id,
                    u.full_name,
                    u.sacco_number,
                    u.savings_balance,
                    COUNT(sd.id) as deposit_count,
                    COALESCE(SUM(sd.amount), 0) as total_deposited
                FROM users u
                LEFT JOIN savings_deposits sd ON u.id = sd.user_id
                WHERE LOWER(u.role) = 'member'
                GROUP BY u.id
                ORDER BY u.savings_balance DESC
            """).fetchall()
            
            # Convert to dictionaries
            savings_list = [dict(s) for s in savings]
            member_savings_list = [dict(m) for m in member_savings]
            
            total_savings_amount = sum(m['savings_balance'] for m in member_savings_list) if member_savings_list else 0
            
            db.close()
            
            return render_template(
                "admin/reports/savings-report.html",
                savings=savings_list,  # Pass list of dictionaries
                member_savings=member_savings_list,  # Pass list of dictionaries
                total_savings=total_savings_amount,
                total_deposits=len(savings_list),
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                report_title="Savings Report",
                now=datetime.now()
            )
            
        else:
            flash('Invalid report type', 'danger')
            return redirect(url_for('admin_dashboard'))
            
    except Exception as e:
        db.close()
        flash(f'Error generating report: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    
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
# ADMIN - UPDATE USER
# ============================================================
@app.route("/admin/users/update/<int:user_id>", methods=["POST"])
def admin_update_user(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    db = get_db()
    
    try:
        user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Check if email is already used by another user
        email = data.get('email', '').strip()
        if email:
            existing = db.execute("""
                SELECT id FROM users WHERE email = ? AND id != ? AND email != ''
            """, (email, user_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Email already in use by another user'}), 400
        
        # Check if phone is already used by another user
        phone = data.get('phone', '').strip()
        if phone:
            existing = db.execute("""
                SELECT id FROM users WHERE phone = ? AND id != ? AND phone != ''
            """, (phone, user_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Phone number already in use by another user'}), 400
        
        db.execute("""
            UPDATE users 
            SET full_name = ?,
                email = ?,
                phone = ?,
                role = ?,
                status = ?
            WHERE id = ?
        """, (
            data.get('full_name', '').strip(),
            email,
            phone,
            data.get('role', 'member'),
            data.get('status', 'active'),
            user_id
        ))
        
        db.commit()
        db.close()
        return jsonify({'success': True, 'message': 'User updated successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# ADMIN - RESET PASSWORD
# ============================================================
@app.route("/admin/users/reset-password/<int:user_id>", methods=["POST"])
def admin_reset_password(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    import secrets
    import string
    
    db = get_db()
    
    try:
        user = db.execute("SELECT id, full_name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Generate a random password
        alphabet = string.ascii_letters + string.digits
        new_password = ''.join(secrets.choice(alphabet) for _ in range(10))
        
        db.execute("""
            UPDATE users SET password = ? WHERE id = ?
        """, (new_password, user_id))
        
        db.commit()
        db.close()
        return jsonify({
            'success': True, 
            'message': 'Password reset successfully',
            'new_password': new_password
        })
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# ADMIN - DELETE USER (FIXED - Accepts both DELETE and POST)
# ============================================================
@app.route("/admin/users/delete/<int:user_id>", methods=["DELETE", "POST"])
def admin_delete_user(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        user = db.execute("SELECT id, full_name, role, sacco_number FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Prevent deleting the last admin
        if user['role'] == 'admin':
            admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                db.close()
                return jsonify({'success': False, 'message': 'Cannot delete the last admin user'}), 400
        
        # Prevent deleting self
        if user_id == session.get('user_id'):
            db.close()
            return jsonify({'success': False, 'message': 'You cannot delete your own account'}), 400
        
        # Check if user has active loans
        if user['role'] == 'member':
            active_loans = db.execute("""
                SELECT COUNT(*) FROM loans 
                WHERE user_id = ? AND status IN ('pending', 'approved', 'disbursed', 'active')
            """, (user_id,)).fetchone()[0]
            
            if active_loans > 0:
                db.close()
                return jsonify({
                    'success': False, 
                    'message': f'Cannot delete member with {active_loans} active loan(s). Please resolve loans first.'
                }), 400
            
            # Check if member has savings balance
            savings = db.execute("SELECT savings_balance FROM users WHERE id = ?", (user_id,)).fetchone()
            if savings and savings['savings_balance'] > 0:
                db.close()
                return jsonify({
                    'success': False, 
                    'message': f'Cannot delete member with savings balance of UGX {savings["savings_balance"]:,.0f}. Please withdraw savings first.'
                }), 400
        
        # Delete related records first (foreign key constraints)
        # Delete loan guarantors
        db.execute("DELETE FROM loan_guarantors WHERE loan_id IN (SELECT id FROM loans WHERE user_id = ?)", (user_id,))
        # Delete repayments
        db.execute("DELETE FROM repayments WHERE user_id = ?", (user_id,))
        # Delete loans
        db.execute("DELETE FROM loans WHERE user_id = ?", (user_id,))
        # Delete savings deposits
        db.execute("DELETE FROM savings_deposits WHERE user_id = ?", (user_id,))
        # Delete notifications
        db.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
        # Finally delete the user
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': f'User "{user["full_name"]}" deleted successfully'})
        
    except sqlite3.IntegrityError as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': f'Database constraint error: {str(e)}'}), 400
    except Exception as e:
        db.rollback()
        db.close()
        print(f"Delete user error: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# ADMIN - GET SETTINGS
# ============================================================
@app.route("/admin/settings/get")
def get_settings():
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Check if settings table exists, create if not
        db.execute("""
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
        
        # Get settings
        settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        
        if not settings:
            # Insert default settings
            db.execute("""
                INSERT INTO system_settings (
                    sacco_name, registration_number, savings_interest_rate,
                    loan_interest_rate, penalty_rate, max_loan_amount,
                    min_loan_amount, max_tenure
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('Karacel Association', 'SACCO/REG/2024/001', 6.5, 12, 5, '10000000', '10000', 24)
            )
            db.commit()
            settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        
        db.close()
        return jsonify({'success': True, 'settings': dict(settings)})
        
    except Exception as e:
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# ADMIN - UPDATE SETTINGS
# ============================================================
@app.route("/admin/settings/update", methods=["POST"])
def update_settings():
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    db = get_db()
    
    try:
        # Ensure settings table exists
        db.execute("""
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
        
        # Check if settings exist
        settings = db.execute("SELECT id FROM system_settings LIMIT 1").fetchone()
        
        if settings:
            db.execute("""
                UPDATE system_settings SET
                    sacco_name = ?,
                    registration_number = ?,
                    savings_interest_rate = ?,
                    loan_interest_rate = ?,
                    penalty_rate = ?,
                    max_loan_amount = ?,
                    min_loan_amount = ?,
                    max_tenure = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                data.get('sacco_name', 'Karacel Association'),
                data.get('registration_number', 'SACCO/REG/2024/001'),
                data.get('savings_interest_rate', 6.5),
                data.get('loan_interest_rate', 12),
                data.get('penalty_rate', 5),
                data.get('max_loan_amount', '10000000'),
                data.get('min_loan_amount', '10000'),
                data.get('max_tenure', 24),
                settings['id']
            ))
        else:
            db.execute("""
                INSERT INTO system_settings (
                    sacco_name, registration_number, savings_interest_rate,
                    loan_interest_rate, penalty_rate, max_loan_amount,
                    min_loan_amount, max_tenure
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('sacco_name', 'Karacel Association'),
                data.get('registration_number', 'SACCO/REG/2024/001'),
                data.get('savings_interest_rate', 6.5),
                data.get('loan_interest_rate', 12),
                data.get('penalty_rate', 5),
                data.get('max_loan_amount', '10000000'),
                data.get('min_loan_amount', '10000'),
                data.get('max_tenure', 24)
            ))
        
        db.commit()
        db.close()
        return jsonify({'success': True, 'message': 'Settings updated successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# ADMIN - USER MANAGEMENT
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


@app.route("/admin/users/manage")
def admin_manage_users():
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied. Only Admin or Chairperson can manage users.', 'danger')
        return redirect("/login")
    
    db = get_db()
    
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
        staff_counts=staff_counts
    )


# ============================================================
# DEBUG ROUTE
# ============================================================
@app.route("/debug/repayments/<int:user_id>")
def debug_member_repayments(user_id):
    db = get_db()
    db.row_factory = sqlite3.Row
    
    repayments = db.execute("""
        SELECT * FROM repayments WHERE user_id = ?
    """, (user_id,)).fetchall()
    
    db.close()
    
    return f"Found {len(repayments)} repayments for user {user_id}"


# ============================================================
# TREASURER - VIEW MEMBER DETAILS (HTML Page)
# ============================================================
@app.route("/treasurer/members/view/<int:member_id>")
def treasurer_member_details(member_id):
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        flash('Access denied', 'danger')
        return redirect("/login")
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        member = db.execute("""
            SELECT * FROM users WHERE id = ? AND LOWER(role) = 'member'
        """, (member_id,)).fetchone()
        
        if not member:
            flash('Member not found', 'danger')
            return redirect(url_for('treasurer_dashboard'))
        
        loans = db.execute("""
            SELECT * FROM loans WHERE user_id = ? ORDER BY application_date DESC
        """, (member_id,)).fetchall()
        
        deposits = db.execute("""
            SELECT * FROM savings_deposits WHERE user_id = ? ORDER BY deposit_date DESC
        """, (member_id,)).fetchall()
        
        repayments = db.execute("""
            SELECT r.*, l.loan_number 
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            WHERE r.user_id = ?
            ORDER BY r.payment_date DESC
        """, (member_id,)).fetchall()
        
        completed_loans = db.execute("SELECT COUNT(*) FROM loans WHERE status = 'completed'").fetchone()[0]
        
        db.close()
        
        return render_template(
            "treasurer/member-details.html",
            member=member,
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
# TREASURER - VIEW MEMBER (JSON for Edit Modal)
# ============================================================
@app.route("/treasurer/member/view/<int:member_id>")
def treasurer_member_view_json(member_id):
    """Return member data as JSON for AJAX calls"""
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        member = db.execute("""
            SELECT id, full_name, sacco_number, phone, email, 
                   gender, dob, address, savings_balance, status, 
                   registration_date, next_of_kin_name, next_of_kin_phone, relationship
            FROM users 
            WHERE id = ? AND LOWER(role) = 'member'
        """, (member_id,)).fetchone()
        
        if not member:
            db.close()
            return jsonify({'success': False, 'message': 'Member not found'}), 404
        
        db.close()
        
        return jsonify({
            'success': True,
            'member': dict(member)
        })
        
    except Exception as e:
        db.close()
        print(f"❌ Error getting member: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# TREASURER - UPDATE MEMBER
# ============================================================
@app.route("/treasurer/member/update/<int:member_id>", methods=["POST"])
def treasurer_update_member(member_id):
    if session.get("role") not in ["treasurer", "admin", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    db = get_db()
    
    try:
        member = db.execute("""
            SELECT id, sacco_number FROM users WHERE id = ? AND LOWER(role) = 'member'
        """, (member_id,)).fetchone()
        
        if not member:
            db.close()
            return jsonify({'success': False, 'message': 'Member not found'}), 404
        
        phone = data.get('phone', '').strip()
        if phone:
            existing = db.execute("""
                SELECT id FROM users WHERE phone = ? AND id != ?
            """, (phone, member_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Phone number already in use by another member'}), 400
        
        email = data.get('email', '').strip()
        if email:
            existing = db.execute("""
                SELECT id FROM users WHERE email = ? AND id != ? AND email != ''
            """, (email, member_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Email already in use by another member'}), 400
        
        sacco_number = data.get('sacco_number', '').strip().upper()
        if sacco_number:
            existing = db.execute("""
                SELECT id FROM users WHERE sacco_number = ? AND id != ?
            """, (sacco_number, member_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'SACCO number already in use by another member'}), 400
        
        db.execute("""
            UPDATE users 
            SET full_name = ?,
                sacco_number = ?,
                phone = ?,
                email = ?,
                gender = ?,
                dob = ?,
                address = ?,
                status = ?
            WHERE id = ?
        """, (
            data.get('full_name', '').strip(),
            sacco_number,
            phone,
            email,
            data.get('gender', ''),
            data.get('dob', ''),
            data.get('address', ''),
            data.get('status', 'active'),
            member_id
        ))
        
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': 'Member updated successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        print(f"Error updating member: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# TREASURER - DELETE MEMBER
# ============================================================
@app.route("/treasurer/member/delete/<int:member_id>", methods=["DELETE"])
def treasurer_delete_member(member_id):
    if session.get("role") not in ["treasurer", "admin"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    
    try:
        member = db.execute("""
            SELECT id, full_name, savings_balance FROM users WHERE id = ? AND LOWER(role) = 'member'
        """, (member_id,)).fetchone()
        
        if not member:
            db.close()
            return jsonify({'success': False, 'message': 'Member not found'}), 404
        
        active_loans = db.execute("""
            SELECT COUNT(*) as count FROM loans 
            WHERE user_id = ? AND status IN ('pending', 'approved', 'disbursed', 'active')
        """, (member_id,)).fetchone()[0]
        
        if active_loans > 0:
            db.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot delete member with {active_loans} active loan(s). Please resolve loans first.'
            }), 400
        
        if member['savings_balance'] > 0:
            db.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot delete member with savings balance of UGX {member["savings_balance"]:,.0f}. Please withdraw savings first.'
            }), 400
        
        db.execute("DELETE FROM users WHERE id = ?", (member_id,))
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': f'Member "{member["full_name"]}" deleted successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


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


# ============================================================
# GET ALL MEMBERS
# ============================================================
@app.route("/member/get-members", methods=["GET"])
def get_all_members():
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    current_user_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
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
            ORDER BY full_name ASC
            LIMIT 50
        """, (current_user_id,)).fetchall()
        
        member_list = []
        for member in members:
            member_list.append({
                'id': member['id'],
                'full_name': member['full_name'],
                'sacco_number': member['sacco_number'],
                'phone': member['phone'] or '',
                'email': member['email'] or '',
                'savings_balance': member['savings_balance'] or 0
            })
        
        db.close()
        
        return jsonify({
            'success': True,
            'members': member_list,
            'count': len(member_list)
        })
        
    except Exception as e:
        db.close()
        print(f"Error getting members: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# GET MEMBER BY ID
# ============================================================
@app.route("/member/get-member/<int:member_id>", methods=["GET"])
def get_member_by_id(member_id):
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    current_user_id = session["user_id"]
    
    if member_id == current_user_id:
        return jsonify({
            'success': False,
            'message': 'You cannot select yourself as a guarantor'
        }), 400
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        member = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                phone,
                email,
                savings_balance,
                status,
                gender,
                dob,
                address,
                registration_date
            FROM users 
            WHERE id = ? AND LOWER(role) = 'member' AND status = 'active'
        """, (member_id,)).fetchone()
        
        if not member:
            db.close()
            return jsonify({
                'success': False,
                'message': 'Member not found or inactive'
            }), 404
        
        loan_count = db.execute("""
            SELECT COUNT(*) as count 
            FROM loans 
            WHERE user_id = ? AND status IN ('approved', 'disbursed', 'active')
        """, (member_id,)).fetchone()['count']
        
        db.close()
        
        return jsonify({
            'success': True,
            'member': {
                'id': member['id'],
                'full_name': member['full_name'],
                'sacco_number': member['sacco_number'],
                'phone': member['phone'] or '',
                'email': member['email'] or '',
                'savings_balance': member['savings_balance'] or 0,
                'active_loans': loan_count,
                'status': member['status'],
                'gender': member['gender'],
                'dob': member['dob'],
                'address': member['address'],
                'registration_date': member['registration_date']
            }
        })
        
    except Exception as e:
        db.close()
        print(f"Error getting member: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500


# ============================================================
# CHECK IF MEMBER CAN BE GUARANTOR
# ============================================================
@app.route("/member/check-guarantor/<int:member_id>", methods=["GET"])
def check_guarantor_eligibility(member_id):
    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        guarantee_count = db.execute("""
            SELECT COUNT(*) as count 
            FROM loan_guarantors 
            WHERE phone IN (SELECT phone FROM users WHERE id = ?)
            AND status IN ('active', 'accepted')
        """, (member_id,)).fetchone()['count']
        
        db.close()
        
        is_eligible = guarantee_count < 2
        
        return jsonify({
            'success': True,
            'is_eligible': is_eligible,
            'current_guarantees': guarantee_count,
            'max_allowed': 2,
            'message': f'This member is currently guaranteeing {guarantee_count} loan(s). Maximum allowed is 2.' if not is_eligible else 'This member is eligible to be a guarantor.'
        })
        
    except Exception as e:
        db.close()
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500


# ============================================================
# DEBUG - CHECK REGISTERED ROUTES
# ============================================================
print("=" * 60)
print("🔍 ALL REGISTERED ROUTES:")
for rule in app.url_map.iter_rules():
    print(f"   {rule.rule}")
print("=" * 60)

# Check if approve route is registered
routes = [str(rule.rule) for rule in app.url_map.iter_rules()]
if '/treasurer/loan/approve/<int:loan_id>' in routes:
    print("✅ APPROVE ROUTE IS REGISTERED!")
else:
    print("❌ LOAN ACTION ROUTE IS NOT REGISTERED!")

if '/treasurer/loan/disburse/<int:loan_id>' in routes:
    print("✅ DISBURSE ROUTE IS REGISTERED!")
else:
    print("❌ DISBURSE ROUTE IS NOT REGISTERED!")
print("=" * 60)

@app.route("/debug/completed-loans")
def debug_completed_loans():
    if session.get("role") != "treasurer":
        return jsonify({'error': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Check all loans with status 'completed'
        completed = db.execute("""
            SELECT id, loan_number, status, completed_date, current_balance 
            FROM loans 
            WHERE status = 'completed'
        """).fetchall()
        
        # Check all loans (to see what statuses exist)
        all_loans = db.execute("""
            SELECT id, loan_number, status, current_balance 
            FROM loans 
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()
        
        db.close()
        
        return jsonify({
            'completed_count': len(completed),
            'completed_loans': [dict(c) for c in completed],
            'all_loans': [dict(l) for l in all_loans]
        })
        
    except Exception as e:
        db.close()
        return jsonify({'error': str(e)}), 500


# ============================================================
# RUN THE APP
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)