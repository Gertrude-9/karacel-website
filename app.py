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
            elif user["role"] == "chairperson":
                return redirect("/chairperson/dashboard")
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

    # Fixed: Added pending_guarantors count to loan_applications
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
        today=today
    )


# ============================================================
# TREASURER DASHBOARD - COMPLETE RECTIFIED (FIXED)
# ============================================================
@app.route("/treasurer/dashboard")
def treasurer_dashboard():
    if session.get("role") != "treasurer":
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
        
        # ============================================================
        # ALL LOAN APPLICATIONS - WITH GUARANTORS (FIXED)
        # ============================================================
        # Get all loan applications
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
            # Convert Row to dict
            loan = dict(loan_row)
            
            # Get guarantors for this loan
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
            
            # Convert guarantors to list of dicts
            loan['guarantors'] = [dict(g) for g in guarantors] if guarantors else []
            
            all_loan_applications.append(loan)
        
        # Debug print
        print("=" * 60)
        print("🔍 TREASURER DASHBOARD LOADED SUCCESSFULLY")
        print(f"📊 Total Members: {total_members}")
        print(f"📊 Total Savings: {total_savings}")
        print(f"📊 Pending Loans: {pending_loans}")
        print(f"📊 Approved Loans: {approved_loans_count}")
        print(f"📊 Active Loans: {active_loans_count}")
        print(f"📊 Completed Loans: {completed_loans_count}")
        print(f"📊 Rejected Loans: {rejected_loans_count}")
        print(f"📊 Total Loans with Guarantors: {sum(1 for loan in all_loan_applications if loan['guarantors'])}")
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
# GET GUARANTOR DETAILS (FIXED)
# ============================================================
@app.route("/treasurer/guarantor/details/<int:guarantor_id>")
def get_guarantor_details(guarantor_id):
    if session.get("role") not in ["treasurer", "admin"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get guarantor info from loan_guarantors
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
        
        # Try to find the actual member (user) by phone number
        user = db.execute("""
            SELECT 
                id,
                full_name,
                sacco_number,
                phone,
                email,
                savings_balance,
                status,
                registration_date,
                gender,
                dob,
                address,
                next_of_kin_name,
                next_of_kin_phone,
                relationship
            FROM users 
            WHERE phone = ? OR email = ?
            LIMIT 1
        """, (guarantor_info['phone'], guarantor_info['email'])).fetchone()
        
        # If user found, convert to dict
        if user:
            guarantor_data = dict(user)
        else:
            # Create guest guarantor object
            guarantor_data = {
                'id': None,
                'full_name': guarantor_info['guarantor_name'],
                'sacco_number': 'N/A',
                'phone': guarantor_info['phone'],
                'email': guarantor_info['email'],
                'savings_balance': 0,
                'status': 'guest',
                'registration_date': None,
                'gender': None,
                'dob': None,
                'address': None,
                'next_of_kin_name': None,
                'next_of_kin_phone': None,
                'relationship': guarantor_info['relationship']
            }
        
        # Get loans this guarantor has guaranteed
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


# ============================================
# TREASURER - VIEW LOAN DETAILS - FIXED
# ============================================
@app.route("/treasurer/loan/view/<int:loan_id>")
def treasurer_view_loan(loan_id):
    if session.get("role") not in ["treasurer", "admin"]:
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


# ============================================
# TREASURER - APPROVE/REJECT LOAN (1-MONTH LOAN)
# ============================================
@app.route("/treasurer/loan/approve/<int:loan_id>", methods=["POST"])
def treasurer_approve_loan(loan_id):
    if session.get("role") not in ["treasurer", "admin"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    action = data.get('action')
    reason = data.get('reason', '')
    
    if action not in ['approve', 'reject']:
        return jsonify({'success': False, 'message': 'Invalid action'}), 400
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get loan with user details
        loan = db.execute("""
            SELECT l.*, u.savings_balance, u.email, u.phone, u.full_name, u.id as user_id
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            db.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404
        
        # REJECT LOAN
        if action == 'reject':
            if not reason or not reason.strip():
                db.close()
                return jsonify({'success': False, 'message': 'Rejection reason is required'}), 400
            
            db.execute("""
                UPDATE loans 
                SET status = 'rejected', 
                    rejected_date = ?,
                    rejection_reason = ?
                WHERE id = ?
            """, (datetime.now().strftime('%Y-%m-%d'), reason.strip(), loan_id))
            db.commit()
            db.close()
            
            return jsonify({'success': True, 'message': '✅ Loan rejected successfully'})
        
        # APPROVE LOAN
        if loan['status'] in ['approved', 'disbursed', 'active']:
            db.close()
            return jsonify({'success': False, 'message': f'Loan is already {loan["status"]}'}), 400
        
        if loan['status'] != 'pending':
            db.close()
            return jsonify({'success': False, 'message': f'Cannot approve a loan with status: {loan["status"]}'}), 400
        
        # Check if member has enough savings (at least 10% of loan amount)
        required_savings = loan['amount'] * 0.1
        if loan['savings_balance'] < required_savings:
            db.close()
            return jsonify({
                'success': False, 
                'message': f'❌ Member needs at least 10% savings (UGX {required_savings:,.0f}). Current savings: UGX {loan["savings_balance"]:,.0f}'
            }), 400
        
        # ============================================
        # 1-MONTH LOAN APPROVAL - SET START AND END DATES
        # ============================================
        from datetime import datetime, timedelta
        
        approval_date = datetime.now()
        start_date = approval_date.strftime('%Y-%m-%d')
        
        # End date is exactly 30 days from approval date
        # If approved on Aug 6, due date is Sep 5
        end_date = approval_date + timedelta(days=30)
        end_date_str = end_date.strftime('%Y-%m-%d')
        
        # Calculate current balance (total due including interest)
        current_balance = loan['total_repayment'] or loan['amount']
        
        # Approve the loan with start and end dates
        db.execute("""
            UPDATE loans 
            SET status = 'approved', 
                approved_date = ?,
                loan_start_date = ?,
                loan_end_date = ?,
                due_date = ?,
                current_balance = ?,
                last_interest_date = ?,
                next_interest_date = ?
            WHERE id = ?
        """, (
            start_date,  # approved_date
            start_date,  # loan_start_date (starts from approval date)
            end_date_str,  # loan_end_date (30 days later)
            end_date_str,  # due_date (same as end date)
            current_balance,
            start_date,  # last_interest_date
            end_date_str,  # next_interest_date (due date)
            loan_id
        ))
        db.commit()
        db.close()
        
        # Format dates for response
        from datetime import datetime
        start_formatted = datetime.strptime(start_date, '%Y-%m-%d').strftime('%d %B %Y')
        end_formatted = datetime.strptime(end_date_str, '%Y-%m-%d').strftime('%d %B %Y')
        
        return jsonify({
            'success': True, 
            'message': f'✅ Loan approved successfully! Loan period: {start_formatted} to {end_formatted} (30 days)',
            'loan_start_date': start_date,
            'loan_end_date': end_date_str,
            'due_date': end_date_str
        })
        
    except sqlite3.Error as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

# # ============================================
# TREASURER - DISBURSE LOAN (1-MONTH LOAN)
# ============================================
@app.route("/treasurer/loan/disburse/<int:loan_id>", methods=["POST"])
def treasurer_disburse_loan(loan_id):
    if session.get("role") not in ["treasurer", "admin"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get loan with user details
        loan = db.execute("""
            SELECT l.*, u.full_name, u.savings_balance
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            db.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404
        
        if loan['status'] != 'approved':
            db.close()
            return jsonify({'success': False, 'message': f'Loan must be approved first. Current status: {loan["status"]}'}), 400
        
        # ============================================
        # 1-MONTH LOAN DISBURSEMENT - SET START AND END DATES
        # ============================================
        from datetime import datetime, timedelta
        
        disbursement_date = datetime.now()
        start_date = disbursement_date.strftime('%Y-%m-%d')
        
        # End date is exactly 30 days from disbursement date
        # If disbursed on Aug 6, due date is Sep 5
        end_date = disbursement_date + timedelta(days=30)
        end_date_str = end_date.strftime('%Y-%m-%d')
        
        # Calculate current balance (total due including interest)
        current_balance = loan['total_repayment'] or loan['amount']
        
        # Disburse the loan with start and end dates
        db.execute("""
            UPDATE loans 
            SET status = 'disbursed',
                disbursement_date = ?,
                loan_start_date = ?,
                loan_end_date = ?,
                due_date = ?,
                current_balance = ?,
                last_interest_date = ?,
                next_interest_date = ?,
                last_payment_date = NULL,
                last_payment_amount = NULL
            WHERE id = ?
        """, (
            start_date,          # disbursement_date
            start_date,          # loan_start_date (starts from disbursement date)
            end_date_str,        # loan_end_date (30 days later)
            end_date_str,        # due_date (same as end date)
            current_balance,     # current_balance (total due)
            start_date,          # last_interest_date
            end_date_str,        # next_interest_date (due date)
            loan_id
        ))
        db.commit()
        db.close()
        
        # Format dates for response
        from datetime import datetime
        start_formatted = datetime.strptime(start_date, '%Y-%m-%d').strftime('%d %B %Y')
        end_formatted = datetime.strptime(end_date_str, '%Y-%m-%d').strftime('%d %B %Y')
        
        return jsonify({
            'success': True,
            'message': f'💰 Loan disbursed successfully! Loan period: {start_formatted} to {end_formatted} (30 days)',
            'loan_start_date': start_date,
            'loan_end_date': end_date_str,
            'due_date': end_date_str,
            'current_balance': current_balance
        })
        
    except sqlite3.Error as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


# ============================================
# TREASURER - RECORD PAYMENT - FIXED
# ============================================
@app.route("/treasurer/loan/pay", methods=['POST'])
def treasurer_record_payment():
    if session.get("role") not in ["treasurer", "admin"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    loan_id = data.get('loan_id')
    amount_str = data.get('amount', 0)
    payment_method = data.get('payment_method', 'cash')
    
    # Handle amount - could be string or number
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
        # Get loan details with user info
        loan = conn.execute("""
            SELECT l.*, u.full_name, u.sacco_number, u.id as member_id
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()
        
        if not loan:
            conn.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404
        
        # Check if loan is active for payment
        if loan['status'] not in ['approved', 'disbursed', 'active']:
            conn.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot make payment on loan with status: {loan["status"]}'
            }), 400
        
        # Check if loan is already completed
        if loan['status'] == 'completed':
            conn.close()
            return jsonify({'success': False, 'message': 'Loan is already fully paid'}), 400
        
        # Get current balance
        current_balance = float(loan['current_balance'] or loan['amount'] or 0)
        
        # Check if payment exceeds balance
        if amount > current_balance:
            conn.close()
            return jsonify({
                'success': False, 
                'message': f'Payment amount (UGX {amount:,.0f}) exceeds current balance (UGX {current_balance:,.0f})'
            }), 400
        
        # Start transaction
        conn.execute("BEGIN TRANSACTION")
        
        # Record payment
        conn.execute("""
            INSERT INTO repayments (
                loan_id, 
                user_id, 
                amount, 
                payment_date, 
                payment_method,
                status
            )
            VALUES (?, ?, ?, date('now'), ?, 'completed')
        """, (loan_id, loan['member_id'], amount, payment_method))
        
        # Update current balance
        new_balance = current_balance - amount
        conn.execute("""
            UPDATE loans 
            SET current_balance = ?,
                last_payment_date = date('now'),
                last_payment_amount = ?
            WHERE id = ?
        """, (new_balance, amount, loan_id))
        
        # If balance is 0 or less, mark as completed
        if new_balance <= 0:
            conn.execute("""
                UPDATE loans 
                SET status = 'completed', 
                    completed_date = date('now')
                WHERE id = ?
            """, (loan_id,))
            status = 'completed'
        else:
            status = 'active'
            conn.execute("""
                UPDATE loans 
                SET status = 'active'
                WHERE id = ? AND status = 'disbursed'
            """, (loan_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': f'✅ Payment of UGX {amount:,.0f} recorded successfully!',
            'new_balance': new_balance,
            'status': status,
            'is_completed': new_balance <= 0
        })
        
    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


# ============================================
# TREASURER - ENTER REPAYMENT (1-Month Loan)
# ============================================
@app.route("/treasurer/repayment/enter", methods=["GET", "POST"])
def treasurer_enter_repayment():
    if session.get("role") not in ["treasurer", "admin"]:
        flash('Access denied. Only treasurer can enter repayments.', 'danger')
        return redirect("/login")
    
    # If GET request, show the form
    if request.method == "GET":
        db = get_db()
        db.row_factory = sqlite3.Row
        
        # Get active loans for the dropdown (1-month loans)
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
        
        db.close()
        return render_template("treasurer/enter-repayment.html", active_loans=active_loans)
    
    # POST - Process the repayment
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
        
        # Get loan details
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
        
        # Check if loan is active
        if loan['status'] not in ['approved', 'disbursed', 'active']:
            flash(f'Cannot make payment on loan with status: {loan["status"]}', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        if loan['status'] == 'completed':
            flash('Loan is already fully paid', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        # Get current balance (total due including interest)
        current_balance = float(loan['current_balance'] if loan['current_balance'] is not None else loan['amount'] or 0)
        
        # For 1-month loan, check if payment exceeds balance
        if amount > current_balance:
            flash(f'Payment amount (UGX {amount:,.0f}) exceeds current balance (UGX {current_balance:,.0f})', 'danger')
            return redirect(url_for('treasurer_enter_repayment'))
        
        # Calculate payment allocation
        # For 1-month loan: Interest is already included in current_balance
        # Payment goes to reduce the balance (interest first, then principal)
        interest_paid = 0
        principal_paid = 0
        
        # Get original principal
        original_balance = float(loan['original_balance'] or loan['amount'] or 0)
        total_interest = float(loan['interest_amount'] or 0)
        
        # Calculate how much interest is remaining
        interest_remaining = total_interest - float(loan['interest_paid'] or 0)
        
        if amount >= interest_remaining:
            interest_paid = interest_remaining
            principal_paid = amount - interest_remaining
        else:
            interest_paid = amount
            principal_paid = 0
        
        # Calculate new balance
        new_balance = current_balance - amount
        if new_balance < 0:
            new_balance = 0
        
        # Start transaction
        db.execute("BEGIN TRANSACTION")
        
        # Insert repayment
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
        
        # Update loan
        if new_balance <= 0:
            # Loan fully paid
            db.execute("""
                UPDATE loans 
                SET current_balance = 0,
                    status = 'completed',
                    completion_date = ?,
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
            flash(f'✅ Loan fully repaid! UGX {amount:,.0f} paid. Status: COMPLETED', 'success')
        else:
            # Update balance only
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


# ============================================
# TREASURER - SAVINGS DEPOSIT
# ============================================
@app.route("/treasurer/savings/deposit", methods=["GET", "POST"])
def treasurer_savings_deposit():
    if session.get("role") not in ["treasurer", "admin"]:
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
    db.close()
    
    return render_template("treasurer/savings-deposit.html", members=members)


# ============================================
# TREASURER - MEMBER MANAGEMENT
# ============================================
@app.route("/treasurer/members/add", methods=["GET", "POST"])
def treasurer_add_members():
    if session.get("role") not in ["treasurer", "admin"]:
        flash('Access denied. Only Treasurer can register members.', 'danger')
        return redirect("/login")
    
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
            return render_template("treasurer/add-member.html")
        
        db = get_db()
        
        existing = db.execute("SELECT id FROM users WHERE sacco_number = ?", (sacco_number,)).fetchone()
        if existing:
            flash(f'SACCO number "{sacco_number}" already exists!', 'danger')
            db.close()
            return render_template("treasurer/add-member.html")
        
        if email:
            existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                flash(f'Email "{email}" is already registered!', 'danger')
                db.close()
                return render_template("treasurer/add-member.html")
        
        existing = db.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            flash(f'Phone number "{phone}" is already registered!', 'danger')
            db.close()
            return render_template("treasurer/add-member.html")
        
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
            return render_template("treasurer/add-member.html")
    
    return render_template("treasurer/add-member.html")


# ============================================
# MEMBER DASHBOARD - RECTIFIED
# ============================================
@app.route("/member/dashboard")
def member_dashboard():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get member details
        member = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        
        if not member:
            db.close()
            flash("Member not found", "danger")
            return redirect(url_for("login"))
        
        # Get total savings
        total_savings = db.execute("""
            SELECT COALESCE(SUM(amount), 0) as total 
            FROM savings_deposits 
            WHERE user_id = ?
        """, (user_id,)).fetchone()['total']
        
        # Get all loans with repayment calculations
        loans_data = db.execute("""
            SELECT 
                l.*,
                COALESCE((
                    SELECT SUM(amount) 
                    FROM repayments 
                    WHERE loan_id = l.id AND status = 'completed'
                ), 0) as total_paid
            FROM loans l
            WHERE l.user_id = ?
            ORDER BY l.application_date DESC
        """, (user_id,)).fetchall()
        
        # Convert to dictionaries and calculate loan balances
        loans = []
        active_loans_count = 0
        active_loans_balance = 0
        total_loans_taken = 0
        
        for loan_row in loans_data:
            # Convert Row to dict to allow item assignment
            loan = dict(loan_row)
            
            total_loans_taken += float(loan.get('amount', 0) or 0)
            
            # Calculate remaining balance
            loan_total = float(loan.get('total_repayment') or loan.get('amount', 0) or 0)
            total_paid = float(loan.get('total_paid', 0) or 0)
            remaining_balance = max(0, loan_total - total_paid)
            
            # Add calculated field to dict
            loan['remaining_balance'] = remaining_balance
            
            # Count active loans and their balances
            if loan.get('status') in ['approved', 'disbursed', 'active']:
                active_loans_count += 1
                active_loans_balance += remaining_balance
            
            loans.append(loan)
        
        # Get savings deposits
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
        
        # Get repayments
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
        
        # Get guarantors
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
        
        db.close()
        
        print("=" * 60)
        print("🔵 MEMBER DASHBOARD LOADED")
        print(f"🟢 User: {member['full_name']}")
        print(f"🟢 Total Savings: {total_savings}")
        print(f"🟢 Active Loans: {active_loans_count}")
        print(f"🟢 Active Loans Balance: {active_loans_balance}")
        print(f"🟢 Total Loans Taken: {total_loans_taken}")
        print("=" * 60)

        return render_template(
            "member/member-dashboard.html",
            member=member,
            user=member,
            total_savings=total_savings,
            active_loans_count=active_loans_count,
            active_loans_balance=active_loans_balance,
            total_loans_taken=total_loans_taken,
            savings_deposits=savings_deposits,
            loans=loans,  # This is now a list of dictionaries
            repayments=repayments,
            guarantors=guarantors
        )
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        db.close()
        flash(f"Error loading dashboard: {str(e)}", "danger")
        return redirect(url_for("login"))
    
# ============================================
# MEMBER LOAN APPLICATION (1-MONTH LOAN)
# ============================================
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
        
        # Get current date/time and calculate due date (30 days from now)
        from datetime import datetime, timedelta
        now = datetime.now()
        current_year = now.year
        current_date = now.strftime('%d %B %Y')
        
        # Calculate due date (30 days from today)
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
        
        # ============================================
        # 1-MONTH LOAN CALCULATION
        # ============================================
        from datetime import datetime, timedelta
        import calendar
        
        application_date = datetime.now()
        
        # Get monthly interest rate based on loan amount
        # These are MONTHLY rates (5%, 3%, 2%, 1%)
        def get_loan_interest_rate(amount):
            if amount >= 10000 and amount <= 1999999:
                return 5  # 5% monthly
            elif amount >= 2000000 and amount <= 4999999:
                return 3  # 3% monthly
            elif amount >= 5000000 and amount <= 9999999:
                return 2  # 2% monthly
            elif amount >= 10000000:
                return 1  # 1% monthly
            return 0
        
        monthly_rate_percent = get_loan_interest_rate(loan_amount)
        monthly_rate = monthly_rate_percent / 100  # e.g., 5% = 0.05
        
        # Calculate interest for 1 month
        interest_amount = loan_amount * monthly_rate
        total_repayment = loan_amount + interest_amount
        
        # Due date is exactly 1 month from application date
        # If approved on Aug 5, due date is Sep 5
        due_date = application_date + timedelta(days=30)
        due_date_str = due_date.strftime('%Y-%m-%d')
        
        # Generate loan reference
        loan_ref = generate_loan_reference()
        
        # Insert loan into database
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
            monthly_rate_percent,  # Store as percentage (5, 3, 2, 1)
            interest_amount,
            total_repayment, 
            interest_amount,  # monthly_installment is interest for 1-month loan
            1,  # tenure is 1 month
            purpose,
            repayment_plan, 
            'pending', 
            application_date.strftime('%Y-%m-%d'),
            total_repayment,  # current_balance = total due
            application_date.strftime('%Y-%m-%d'),  # last_interest_date
            due_date_str,  # next_interest_date (due date)
            application_date.month, 
            due_date.month,  # end_month is the month of due date
            0,  # total_interest_accrued
            0,  # principal_paid
            0,  # interest_paid
            0,  # months_paid
            loan_amount,  # original_balance (principal only)
            interest_amount,  # total_interest_calculated
            due_date_str,  # due_date
            application_date.strftime('%Y-%m-%d'),  # loan_start_date
            due_date_str  # loan_end_date
        ))
        
        loan_id = cursor.lastrowid
        
        # Check if guarantors are required
        total_savings = db.execute("""
            SELECT COALESCE(SUM(amount), 0) as total 
            FROM savings_deposits 
            WHERE user_id = ?
        """, (user_id,)).fetchone()['total']
        
        savings_threshold = total_savings * 0.95
        guarantors_required = loan_amount > savings_threshold
        
        # Insert guarantors
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
        
        # Add to guarantor tracking
        applicant = db.execute("SELECT full_name FROM users WHERE id = ?", (user_id,)).fetchone()
        cursor.execute("""
            INSERT INTO guarantor_tracking (
                guarantor_id, loan_id, member_name, amount_guaranteed, outstanding_balance
            ) VALUES (?, ?, ?, ?, ?)
        """, (user_id, loan_id, applicant['full_name'], loan_amount, total_repayment))
        
        db.commit()
        db.close()
        
        success_message = 'Loan application submitted successfully!'
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
        if request.is_json:
            return jsonify({'success': False, 'message': str(e)}), 500
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('member_apply_loan'))


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


@app.route("/member/guarantors")
def member_guarantors():
    if "user_id" not in session:
        return redirect("/login")
    
    user_id = session["user_id"]
    db = get_db()
    db.row_factory = sqlite3.Row
    
    try:
        # Get all guarantors for the member's loans
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
        
        # Get all loans for the member (to display loan info)
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
        
        # Debug output
        print("=" * 60)
        print("🔵 MEMBER GUARANTORS LOADED")
        print(f"🟢 User ID: {user_id}")
        print(f"🟢 Guarantors found: {len(guarantors)}")
        print(f"🟢 Loans found: {len(loans)}")
        print("=" * 60)
        
        return render_template(
            "member/member-guarantors.html",
            guarantors=guarantors,
            loans=loans  # <-- This was missing!
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        db.close()
        flash(f"Error loading guarantors: {str(e)}", "danger")
        return redirect(url_for("member_dashboard"))


# ============================================
# OTHER DASHBOARDS
# ============================================
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


# ============================================
# ADMIN - VIEW LOAN DETAILS
# ============================================
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
            return redirect(url_for('admin_dashboard'))
        
        guarantors = db.execute("""
            SELECT * FROM loan_guarantors WHERE loan_id = ?
        """, (loan_id,)).fetchall()
        
        repayments = db.execute("""
            SELECT * FROM repayments WHERE loan_id = ? ORDER BY payment_date DESC
        """, (loan_id,)).fetchall()
        
        total_paid = sum(r['amount'] for r in repayments) if repayments else 0
        
    except Exception as e:
        print(f"Database error: {e}")
        flash(f'Database error: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    finally:
        db.close()
    
    return render_template(
        "admin/admin-view-loan.html",
        loan=loan,
        guarantors=guarantors,
        repayments=repayments,
        total_paid=total_paid,
        role='admin'
    )


# ============================================
# ADMIN - FINAL LOAN APPROVAL
# ============================================
@app.route("/admin/loan/approve/<int:loan_id>", methods=["POST"])
def admin_approve_loan(loan_id):
    if session.get("role") not in ["admin", "chairperson"]:
        return jsonify({'error': 'Access denied. Only Admin or Chairperson can approve loans.'}), 403
    
    data = request.get_json()
    action = data.get('action')
    reason = data.get('reason', '')
    
    db = get_db()
    
    try:
        loan = db.execute("""
            SELECT l.*, u.full_name as member_name, u.email, u.phone
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ? AND l.status = 'approved'
        """, (loan_id,)).fetchone()
        
        if not loan:
            db.close()
            return jsonify({'error': 'Loan not found or not in approved status'}), 404
        
        if action == 'reject':
            db.execute("""
                UPDATE loans 
                SET status = 'rejected', 
                    rejected_date = ?,
                    admin_rejection_reason = ?,
                    rejection_reason = ?
                WHERE id = ?
            """, (datetime.now().strftime('%Y-%m-%d'), reason, reason, loan_id))
            db.commit()
            db.close()
            return jsonify({'success': True, 'message': 'Loan rejected successfully'})
        
        # Final approval - disburse the loan
        db.execute("""
            UPDATE loans 
            SET status = 'disbursed', disbursed_date = ?
            WHERE id = ?
        """, (datetime.now().strftime('%Y-%m-%d'), loan_id))
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': 'Loan disbursed successfully!'})
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================
# ADMIN - STAFF USER MANAGEMENT
# ============================================
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


# ============================================
# INTEREST ACCRUAL FUNCTION (Run Monthly)
# ============================================
def calculate_monthly_interest():
    """Run this function monthly via cron job or scheduled task"""
    db = get_db()
    
    current_date = datetime.now()
    current_year = current_date.year
    
    # Get all active loans
    active_loans = db.execute("""
        SELECT * FROM loans 
        WHERE status IN ('approved', 'disbursed', 'active')
        AND status != 'completed'
    """).fetchall()
    
    for loan in active_loans:
        # Check if past December
        end_date = datetime(current_year, 12, 31)
        
        if current_date > end_date:
            # Mark as completed if past December
            db.execute("""
                UPDATE loans 
                SET status = 'completed', completed_date = ?
                WHERE id = ?
            """, (current_date.strftime('%Y-%m-%d'), loan['id']))
            continue
        
        current_balance = loan['current_balance'] or loan['total_repayment']
        interest_rate = loan['interest_rate'] / 100
        
        # Calculate interest for one month
        interest_amount = current_balance * interest_rate
        new_balance = current_balance + interest_amount
        
        # Update loan balance
        db.execute("""
            UPDATE loans 
            SET current_balance = ?,
                interest_accrued = interest_accrued + ?,
                months_overdue = months_overdue + 1,
                last_interest_date = ?
            WHERE id = ?
        """, (new_balance, interest_amount, current_date.strftime('%Y-%m-%d'), loan['id']))
        
        # Update guarantor tracking
        db.execute("""
            UPDATE guarantor_tracking 
            SET outstanding_balance = ?,
                repayment_status = CASE 
                    WHEN ? > ? THEN 'overdue'
                    ELSE 'on_track'
                END,
                last_updated = ?
            WHERE loan_id = ?
        """, (new_balance, new_balance, loan['total_repayment'], current_date.strftime('%Y-%m-%d %H:%M:%S'), loan['id']))
    
    db.commit()
    db.close()


# ============================================
# DEBUG ROUTE
# ============================================
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
# TREASURER - VIEW MEMBER (AJAX)
# ============================================================
@app.route("/treasurer/members/view/<int:member_id>")
def treasurer_member_details(member_id):
    if session.get("role") not in ["treasurer", "admin"]:
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
        
        # Get member's loans
        loans = db.execute("""
            SELECT * FROM loans WHERE user_id = ? ORDER BY application_date DESC
        """, (member_id,)).fetchall()
        
        # Get member's deposits
        deposits = db.execute("""
            SELECT * FROM savings_deposits WHERE user_id = ? ORDER BY deposit_date DESC
        """, (member_id,)).fetchall()
        
        # Get member's repayments
        repayments = db.execute("""
            SELECT r.*, l.loan_number 
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            WHERE r.user_id = ?
            ORDER BY r.payment_date DESC
        """, (member_id,)).fetchall()
        
        db.close()
        
        return render_template(
            "treasurer/member-details.html",
            member=member,
            loans=loans,
            deposits=deposits,
            repayments=repayments
        )
        
    except Exception as e:
        db.close()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard'))


@app.route("/treasurer/member/update/<int:member_id>", methods=["POST"])
def treasurer_update_member(member_id):
    if session.get("role") not in ["treasurer", "admin"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data'}), 400
    
    db = get_db()
    
    try:
        # Check if member exists
        member = db.execute("""
            SELECT id, sacco_number FROM users WHERE id = ? AND LOWER(role) = 'member'
        """, (member_id,)).fetchone()
        
        if not member:
            db.close()
            return jsonify({'success': False, 'message': 'Member not found'}), 404
        
        # Check for duplicate phone number (excluding current member)
        phone = data.get('phone', '').strip()
        if phone:
            existing = db.execute("""
                SELECT id FROM users WHERE phone = ? AND id != ?
            """, (phone, member_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Phone number already in use by another member'}), 400
        
        # Check for duplicate email (excluding current member)
        email = data.get('email', '').strip()
        if email:
            existing = db.execute("""
                SELECT id FROM users WHERE email = ? AND id != ? AND email != ''
            """, (email, member_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'Email already in use by another member'}), 400
        
        # Check for duplicate SACCO number (excluding current member)
        sacco_number = data.get('sacco_number', '').strip().upper()
        if sacco_number:
            existing = db.execute("""
                SELECT id FROM users WHERE sacco_number = ? AND id != ?
            """, (sacco_number, member_id)).fetchone()
            if existing:
                db.close()
                return jsonify({'success': False, 'message': 'SACCO number already in use by another member'}), 400
        
        # Update member
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
        # Check if member exists
        member = db.execute("""
            SELECT id, full_name, savings_balance FROM users WHERE id = ? AND LOWER(role) = 'member'
        """, (member_id,)).fetchone()
        
        if not member:
            db.close()
            return jsonify({'success': False, 'message': 'Member not found'}), 404
        
        # Check if member has active loans
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
        
        # Check if member has savings balance
        if member['savings_balance'] > 0:
            db.close()
            return jsonify({
                'success': False, 
                'message': f'Cannot delete member with savings balance of UGX {member["savings_balance"]:,.0f}. Please withdraw savings first.'
            }), 400
        
        # Delete member
        db.execute("DELETE FROM users WHERE id = ?", (member_id,))
        db.commit()
        db.close()
        
        return jsonify({'success': True, 'message': f'Member "{member["full_name"]}" deleted successfully'})
        
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'success': False, 'message': str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)