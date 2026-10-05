from flask import Flask, jsonify, render_template, request, redirect, session, flash, url_for, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
import os
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()


# ============================================================
# POSTGRESQL ADAPTER — makes PostgreSQL look like SQLite
# ============================================================
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    class PostgresCursorAdapter:
        """Wraps a psycopg2 cursor. Converts ? → %s and provides sqlite3.Row-like access."""
        def __init__(self, real_cursor):
            self._cur = real_cursor

        def execute(self, query, params=None):
            query = query.replace("?", "%s")
            if params is None:
                self._cur.execute(query)
            else:
                self._cur.execute(query, params)
            return self

        def executemany(self, query, params_seq):
            query = query.replace("?", "%s")
            self._cur.executemany(query, params_seq)
            return self

        def executescript(self, script):
            self._cur.execute(script)
            return self

        def fetchone(self):
            return self._cur.fetchone()

        def fetchall(self):
            return self._cur.fetchall()

        def fetchmany(self, size=None):
            return self._cur.fetchmany(size) if size else self._cur.fetchmany()

        @property
        def rowcount(self):
            return self._cur.rowcount

        @property
        def lastrowid(self):
            try:
                self._cur.execute("SELECT LASTVAL()")
                return self._cur.fetchone()["lastval"]
            except Exception:
                return None

        def close(self):
            return self._cur.close()

        def __iter__(self):
            return iter(self._cur)

    class PostgresConnectionAdapter:
        """Wraps a psycopg2 connection. Mimics sqlite3.Connection."""
        def __init__(self, real_conn):
            self._conn = real_conn

        def execute(self, query, params=None):
            cur = self._conn.cursor(cursor_factory=RealDictCursor)
            adapted = PostgresCursorAdapter(cur)
            adapted.execute(query, params)
            return adapted

        def cursor(self):
            cur = self._conn.cursor(cursor_factory=RealDictCursor)
            return PostgresCursorAdapter(cur)

        def commit(self):
            self._conn.commit()

        def rollback(self):
            self._conn.rollback()

        def close(self):
            self._conn.close()

        @property
        def row_factory(self):
            return None

        @row_factory.setter
        def row_factory(self, value):
            pass

    def _pg_connect():
        return PostgresConnectionAdapter(psycopg2.connect(DATABASE_URL))

    print("✅ Using PostgreSQL database")
else:
    def _pg_connect():
        return None
    print("✅ Using SQLite database (local dev)")


# ============================================================
# APP SETUP
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key")

app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'receipts'), exist_ok=True)

# ============================================================
# ROW / FETCH HELPERS — robust for SQLite AND Postgres
# ============================================================
def row_to_dict(row):
    """Convert a DB row to a plain dict — works with sqlite3.Row,
    psycopg2 RealDictRow, dict, or fallback tuple."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return {k: row[k] for k in row.keys()}
    except AttributeError:
        pass
    try:
        return dict(row)
    except Exception:
        return {}


def fetchval(db, sql, params=()):
    """Return the first column of the first row of a query."""
    cur = db.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return list(row.values())[0]
    try:
        return row[0]
    except (TypeError, KeyError):
        return None


def is_kac_member_clause():
    """SQL fragment identifying KAC-enrolled members."""
    return "status = 'active' AND COALESCE(kac_paid, 0) > 0"


def get_db():
    """Returns SQLite locally, PostgreSQL on Render."""
    if DATABASE_URL:
        return _pg_connect()
    conn = sqlite3.connect("sacco.db")
    conn.row_factory = sqlite3.Row
    return conn


def fetchval(conn, query, params=None):
    """Return the first column of the first row. Works on both DBs."""
    if params is None:
        params = ()
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return list(row.values())[0]
    return row[0]


def row_to_dict(row):
    """Convert a SQLite Row or PostgreSQL dict into a plain dict."""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return row


def log_action(action, target=None, details=None):
    """
    Insert an audit log entry.
    Fails silently so it never blocks the main request.
    """
    try:
        user_id   = session.get("user_id")
        user_name = session.get("full_name") or session.get("email") or "Unknown"
        user_role = session.get("role") or "anonymous"

        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ip = ip.split(",")[0].strip() if ip else ""
        ua = (request.headers.get("User-Agent") or "")[:300]

        db = get_db()
        try:
            PH = "%s" if DATABASE_URL else "?"
            db.execute(f"""
                INSERT INTO system_logs (
                    user_id, user_name, user_role,
                    action, target, details,
                    ip_address, user_agent, created_at
                )
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})
            """, (
                user_id, user_name, user_role,
                action, target, details,
                ip, ua,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ log_action failed: {e}")


# ============================================================
# REGISTER CHAT BLUEPRINT
# ============================================================
from chat_api import chat_api
app.register_blueprint(chat_api)


# ============================================================
# CREATE DATABASE
# ============================================================
def create_database():
    conn = get_db()
    cursor = conn.cursor()

    is_pg = bool(DATABASE_URL)
    PH = "%s" if is_pg else "?"

    def get_columns(table_name):
        if is_pg:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table_name,)
            )
            rows = cursor.fetchall()
            return [r["column_name"] for r in rows]
        else:
            cursor.execute(f"PRAGMA table_info({table_name})")
            return [col[1] for col in cursor.fetchall()]

    def table_exists(table_name):
        if is_pg:
            cursor.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                (table_name,)
            )
            return cursor.fetchone() is not None
        else:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            return cursor.fetchone() is not None

    # ============================================================
    # COLUMN MIGRATIONS
    # ============================================================

    if table_exists("users"):
        existing = get_columns("users")
        for col_name, col_type in {
            'kai_shares': 'INTEGER DEFAULT 0',
            'ks_shares': 'INTEGER DEFAULT 0',
            'kac_paid': 'REAL DEFAULT 0',
            'kac_used': 'REAL DEFAULT 0',
            'kac_paid_date': 'TEXT',
            'registration_fee_paid': 'INTEGER DEFAULT 0',
            'registration_fee_paid_date': 'TEXT'
        }.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                    print(f"✅ Added column to users: {col_name}")
                except Exception as e:
                    print(f"⚠️ Could not add column {col_name}: {e}")
                    conn.rollback()
                    cursor = conn.cursor()

    if table_exists("system_settings"):
        existing = get_columns("system_settings")
        for col_name, col_type in {
            'kai_share_price': 'INTEGER DEFAULT 100000',
            'ks_share_price': 'INTEGER DEFAULT 10000',
            'kac_annual_fee': 'INTEGER DEFAULT 100000',
            'kac_condolence_amount': 'INTEGER DEFAULT 20000',
            'kac_death_amount': 'INTEGER DEFAULT 40000',
            'registration_fee': 'INTEGER DEFAULT 20000',
            'contingency_fund_total': 'REAL DEFAULT 0',
            'contingency_rate': 'REAL DEFAULT 10'
        }.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE system_settings ADD COLUMN {col_name} {col_type}")
                    print(f"✅ Added column to system_settings: {col_name}")
                except Exception as e:
                    print(f"⚠️ Could not add column {col_name}: {e}")
                    conn.rollback()
                    cursor = conn.cursor()

    if table_exists("savings_deposits"):
        existing = get_columns("savings_deposits")
        for col_name, col_type in {
            'savings_type': "TEXT DEFAULT 'KAI'",
            'shares': 'INTEGER DEFAULT 0'
        }.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE savings_deposits ADD COLUMN {col_name} {col_type}")
                    print(f"✅ Added column to savings_deposits: {col_name}")
                except Exception as e:
                    print(f"⚠️ Could not add column {col_name}: {e}")
                    conn.rollback()
                    cursor = conn.cursor()

    if table_exists("repayments"):
        existing = get_columns("repayments")
        for col_name, col_type in {
            'interest_paid': 'REAL DEFAULT 0',
            'principal_paid': 'REAL DEFAULT 0',
            'balance_after': 'REAL DEFAULT 0',
            'notes': 'TEXT'
        }.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE repayments ADD COLUMN {col_name} {col_type}")
                    print(f"✅ Added column to repayments: {col_name}")
                except Exception as e:
                    print(f"⚠️ Could not add column {col_name}: {e}")
                    conn.rollback()
                    cursor = conn.cursor()

    if table_exists("loans"):
        existing = get_columns("loans")
        for col_name, col_type in {
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
            'loan_type': "TEXT DEFAULT 'standard'",
            'disbursed_amount': 'REAL DEFAULT 0',
            'total_fees_paid': 'REAL DEFAULT 0',
            'total_penalties': 'REAL DEFAULT 0',
            'accrued_interest': 'REAL DEFAULT 0',
            'last_interest_applied_date': 'TEXT',
            'total_interest_charged': 'REAL DEFAULT 0',
            'send_to_type': 'TEXT',
            'send_to_value': 'TEXT',
            'send_to_secondary': 'TEXT',
            'next_interest_date': 'TEXT',
        }.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE loans ADD COLUMN {col_name} {col_type}")
                    print(f"✅ Added column to loans: {col_name}")
                except Exception as e:
                    print(f"⚠️ Could not add column {col_name}: {e}")
                    conn.rollback()
                    cursor = conn.cursor()

    if table_exists("loans"):
        try:
            cursor.execute("""
                UPDATE loans
                SET last_interest_applied_date = COALESCE(
                    disbursed_date, approved_date, loan_start_date, application_date
                )
                WHERE last_interest_applied_date IS NULL
                AND status IN ('disbursed', 'active', 'approved')
            """)
            rows = cursor.rowcount
            if rows > 0:
                print(f"✅ Backfilled last_interest_applied_date for {rows} loan(s)")
        except Exception as e:
            print(f"⚠️ Backfill warning: {e}")
            conn.rollback()
            cursor = conn.cursor()

    if table_exists("notifications"):
        existing = get_columns("notifications")
        for col_name, col_type in {
            'notification_type': 'TEXT',
            'link': 'TEXT',
            'is_read': 'INTEGER DEFAULT 0',
        }.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE notifications ADD COLUMN {col_name} {col_type}")
                    print(f"✅ Added column to notifications: {col_name}")
                except Exception as e:
                    print(f"⚠️ Could not add column {col_name}: {e}")
                    conn.rollback()
                    cursor = conn.cursor()

    # ============================================================
    # CREATE TABLES
    # ============================================================
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"

    # ---- USERS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS users (
        id {pk},
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
        registration_date TEXT,
        kai_shares INTEGER DEFAULT 0,
        ks_shares INTEGER DEFAULT 0,
        kac_paid REAL DEFAULT 0,
        kac_used REAL DEFAULT 0,
        kac_paid_date TEXT,
        registration_fee_paid INTEGER DEFAULT 0,
        registration_fee_paid_date TEXT
    )
    """)

    # ---- LOANS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS loans (
        id {pk},
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
        created_at TEXT,
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
        send_to_type TEXT,
        send_to_value TEXT,
        send_to_secondary TEXT
    )
    """)

    # ---- NOTIFICATIONS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS notifications (
        id {pk},
        user_id INTEGER NOT NULL,
        type TEXT DEFAULT 'general',
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        link TEXT,
        created_at TEXT,
        is_read INTEGER DEFAULT 0
    )
    """)

    # ---- LOAN GUARANTORS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS loan_guarantors (
        id {pk},
        loan_id INTEGER NOT NULL,
        guarantor_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT,
        relationship TEXT,
        status TEXT DEFAULT 'active',
        created_at TEXT
    )
    """)

    # ---- REPAYMENTS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS repayments (
        id {pk},
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
        created_at TEXT
    )
    """)

    # ---- SAVINGS DEPOSITS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS savings_deposits (
        id {pk},
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        savings_type TEXT DEFAULT 'KAI',
        shares INTEGER DEFAULT 0,
        deposit_date TEXT NOT NULL,
        payment_method TEXT,
        receipt_number TEXT,
        notes TEXT,
        created_at TEXT
    )
    """)

    # ---- GUARANTOR TRACKING ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS guarantor_tracking (
        id {pk},
        guarantor_id INTEGER NOT NULL,
        loan_id INTEGER NOT NULL,
        member_name TEXT NOT NULL,
        amount_guaranteed REAL NOT NULL,
        outstanding_balance REAL DEFAULT 0,
        repayment_status TEXT DEFAULT 'on_track',
        last_updated TEXT
    )
    """)

    # ---- PUBLICITY ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS announcements (
        id {pk},
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT,
        created_by INTEGER
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS events (
        id {pk},
        title TEXT NOT NULL,
        event_date TEXT NOT NULL,
        event_time TEXT,
        location TEXT NOT NULL,
        description TEXT,
        created_at TEXT,
        created_by INTEGER
    )
    """)

    # ---- SYSTEM SETTINGS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS system_settings (
        id {pk},
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
        kac_condolence_amount INTEGER DEFAULT 20000,
        kac_death_amount INTEGER DEFAULT 40000,
        registration_fee INTEGER DEFAULT 20000,
        contingency_fund_total REAL DEFAULT 0,
        contingency_rate REAL DEFAULT 10,
        updated_at TEXT
    )
    """)

    # ---- SYSTEM LOGS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS system_logs (
        id {pk},
        user_id INTEGER,
        user_name TEXT,
        user_role TEXT,
        action TEXT NOT NULL,
        target TEXT,
        details TEXT,
        ip_address TEXT,
        user_agent TEXT,
        created_at TEXT
    )
    """)

    # ---- KAC CLAIMS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS kac_claims (
        id {pk},
        claim_number TEXT UNIQUE NOT NULL,
        claim_type TEXT NOT NULL,
        affected_user_id INTEGER NOT NULL,
        affected_name TEXT NOT NULL,
        register_entry_id INTEGER,
        deduction_amount REAL NOT NULL,
        members_charged INTEGER DEFAULT 0,
        total_collected REAL DEFAULT 0,
        description TEXT,
        event_date TEXT NOT NULL,
        created_by INTEGER,
        created_at TEXT,
        status TEXT DEFAULT 'active',
        reversed_at TEXT,
        reversed_by INTEGER,
        reversal_reason TEXT
    )
    """)

    # ---- KAC CLAIM DEDUCTIONS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS kac_claim_deductions (
        id {pk},
        claim_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        amount_deducted REAL NOT NULL,
        kac_before REAL NOT NULL,
        kac_after REAL NOT NULL,
        created_at TEXT
    )
    """)

    # ---- MEMBER CONDOLENCE REGISTER ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS member_condolence_register (
        id {pk},
        user_id INTEGER NOT NULL,
        full_name TEXT NOT NULL,
        relationship TEXT,
        phone TEXT,
        slot_number INTEGER NOT NULL,
        status TEXT DEFAULT 'active',
        deceased_date TEXT,
        deceased_claim_id INTEGER,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    # ---- KAC YEAR CONTRIBUTIONS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS kac_year_contributions (
        id {pk},
        user_id INTEGER NOT NULL,
        year INTEGER NOT NULL,
        paid REAL DEFAULT 0,
        used REAL DEFAULT 0,
        target REAL DEFAULT 100000,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE (user_id, year)
    )
    """)

    # ---- KS INTEREST RECORDS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS ks_interest_records (
        id {pk},
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        total_interest_earned REAL NOT NULL DEFAULT 0,
        contingency_amount REAL NOT NULL DEFAULT 0,
        distributable_amount REAL NOT NULL DEFAULT 0,
        total_ks_savings REAL NOT NULL DEFAULT 0,
        total_members_credited INTEGER DEFAULT 0,
        status TEXT DEFAULT 'recorded',
        entered_by INTEGER,
        entered_at TEXT,
        notes TEXT,
        UNIQUE(year, month)
    )
    """)

    # ---- KS INTEREST ALLOCATIONS ----
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS ks_interest_allocations (
        id {pk},
        record_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        ks_savings_at_month REAL NOT NULL DEFAULT 0,
        interest_share REAL NOT NULL DEFAULT 0,
        paid INTEGER DEFAULT 0,
        paid_at TEXT,
        created_at TEXT
    )
    """)

    # ============================================================
    # INDEXES
    # ============================================================
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_logs_user_id ON system_logs(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_logs_action ON system_logs(action)",
        "CREATE INDEX IF NOT EXISTS idx_logs_created_at ON system_logs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_logs_target ON system_logs(target)",
        "CREATE INDEX IF NOT EXISTS idx_kac_claims_user ON kac_claims(affected_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_kac_claims_type ON kac_claims(claim_type)",
        "CREATE INDEX IF NOT EXISTS idx_kac_claims_status ON kac_claims(status)",
        "CREATE INDEX IF NOT EXISTS idx_kac_deductions_claim ON kac_claim_deductions(claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_kac_deductions_user ON kac_claim_deductions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_kac_year_user ON kac_year_contributions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_kac_year_year ON kac_year_contributions(year)",
        "CREATE INDEX IF NOT EXISTS idx_condolence_register_user ON member_condolence_register(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_condolence_register_status ON member_condolence_register(status)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read)",
        "CREATE INDEX IF NOT EXISTS idx_loans_user_id ON loans(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status)",
        "CREATE INDEX IF NOT EXISTS idx_repayments_loan_id ON repayments(loan_id)",
        "CREATE INDEX IF NOT EXISTS idx_repayments_user_id ON repayments(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_savings_deposits_user_id ON savings_deposits(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_ks_int_records_year_month ON ks_interest_records(year, month)",
        "CREATE INDEX IF NOT EXISTS idx_ks_int_alloc_record ON ks_interest_allocations(record_id)",
        "CREATE INDEX IF NOT EXISTS idx_ks_int_alloc_user ON ks_interest_allocations(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_ks_int_alloc_year ON ks_interest_allocations(year)",
    ]
    for sql in indexes:
        try:
            cursor.execute(sql)
        except Exception:
            conn.rollback()
            cursor = conn.cursor()

    # ============================================================
    # BACKFILL KAC YEAR TRACKER
    # ============================================================
    try:
        current_year = datetime.now().year
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        existing_kac_users = cursor.execute("""
            SELECT id,
                   COALESCE(kac_paid, 0) AS paid,
                   COALESCE(kac_used, 0) AS used
            FROM users
            WHERE status = 'active'
              AND COALESCE(kac_paid, 0) > 0
        """).fetchall()

        inserted = 0
        for u in existing_kac_users:
            u_dict = dict(u) if not isinstance(u, dict) else u
            uid = u_dict.get('id')
            if uid is None:
                continue

            already = cursor.execute(f"""
                SELECT 1 FROM kac_year_contributions
                WHERE user_id = {PH} AND year = {PH}
            """, (uid, current_year)).fetchone()

            if already:
                continue

            cursor.execute(f"""
                INSERT INTO kac_year_contributions
                    (user_id, year, paid, used, target, created_at, updated_at)
                VALUES ({PH},{PH},{PH},{PH},100000,{PH},{PH})
            """, (
                uid,
                current_year,
                u_dict.get('paid') or 0,
                u_dict.get('used') or 0,
                now_str,
                now_str
            ))
            inserted += 1

        if inserted > 0:
            print(f"✅ Backfilled {inserted} KAC year contribution row(s) for {current_year}")
        else:
            print(f"✅ KAC year contributions already up to date for {current_year}")

    except Exception as e:
        print(f"⚠️ KAC year backfill warning: {e}")
        conn.rollback()
        cursor = conn.cursor()

    # ============================================================
    # DEFAULT SETTINGS
    # ============================================================
    cursor.execute("SELECT COUNT(*) AS c FROM system_settings")
    row = cursor.fetchone()
    count = row["c"] if is_pg else row[0]
    if count == 0:
        cursor.execute(f"""
            INSERT INTO system_settings (
                sacco_name, registration_number, savings_interest_rate,
                loan_interest_rate, penalty_rate, max_loan_amount,
                min_loan_amount, max_tenure, kai_share_price,
                ks_share_price, kac_annual_fee, kac_condolence_amount,
                kac_death_amount, registration_fee,
                contingency_fund_total, contingency_rate, updated_at
            ) VALUES (
                {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH},
                {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}
            )
        """, (
            'Karacel Association', 'SACCO/REG/2024/001', 6.5, 12, 5,
            '10000000', '10000', 24, 100000, 10000, 100000, 20000, 40000, 20000,
            0, 10,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))

    # ============================================================
    # DEFAULT ADMIN
    # ============================================================
    cursor.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'")
    row = cursor.fetchone()
    admin_count = row["c"] if is_pg else row[0]
    if admin_count == 0:
        cursor.execute(f"""
            INSERT INTO users (
                full_name, gender, dob, sacco_number,
                email, phone, address, password, role, status,
                savings_balance, next_of_kin_name, relationship,
                next_of_kin_phone, registration_date
            )
            VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})
        """, (
            "System Administrator", "Male", "1990-01-01", "ADM001",
            "admin@sacco.com", "0700000000", "Head Office",
            generate_password_hash("admin123"),
            "admin", "active", 0, None, None, None,
            datetime.now().strftime('%Y-%m-%d')
        ))

    conn.commit()
    conn.close()
    print("✅ Database created/updated successfully with all tables!")


# ============================================================
# RUN DATABASE CREATION
# ============================================================
create_database()

try:
    from chat_api import create_chat_table
    create_chat_table()
except Exception as e:
    print(f"⚠️ Could not create chat table: {e}")

# ============================================
# LOAN HELPER FUNCTIONS
# ============================================
def get_start_month(application_date):
    return datetime.strptime(application_date, '%Y-%m-%d').month


def get_remaining_months(application_date):
    start_date = datetime.strptime(application_date, '%Y-%m-%d')
    return 12 - start_date.month + 1


def calculate_loan_end_date(application_date):
    year = datetime.strptime(application_date, '%Y-%m-%d').year
    return datetime(year, 12, 31).strftime('%Y-%m-%d')


def generate_loan_reference():
    year = datetime.now().strftime('%Y')
    db = get_db()
    try:
        count = fetchval(db, "SELECT COUNT(*) FROM loans") or 0
        return f"LN-{year}-{str(count + 1).zfill(4)}"
    finally:
        try:
            db.close()
        except Exception:
            pass


def get_interest_rate(amount):
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
    db = get_db()
    try:
        total_savings = fetchval(db, """
            SELECT COALESCE(SUM(amount), 0) as total 
            FROM savings_deposits 
            WHERE user_id = ?
        """, (user_id,)) or 0
        threshold = total_savings * 0.95
        return loan_amount <= threshold, threshold, total_savings
    finally:
        try:
            db.close()
        except Exception:
            pass


def check_guarantor_eligibility(phone, email):
    db = get_db()
    try:
        count = fetchval(db, """
            SELECT COUNT(*) as count 
            FROM loan_guarantors 
            WHERE (phone = ? OR email = ?) 
            AND status IN ('active')
        """, (phone, email)) or 0
        return count < 2, count
    finally:
        try:
            db.close()
        except Exception:
            pass


def send_email(to_email, subject, body, html_body=None):
    try:
        print(f"EMAIL TO: {to_email}")
        print(f"SUBJECT: {subject}")
        print(f"BODY: {body}")
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


def send_sms(phone, message):
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
    return render_template("website/about.html")


@app.route("/contact")
def contact():
    return render_template("website/contact.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        sacco_number = request.form["sacco_number"].strip().upper()
        password = request.form["password"]

        conn = get_db()
        try:
            user_row = conn.execute(
                "SELECT * FROM users WHERE sacco_number = ?",
                (sacco_number,)
            ).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if user_row:
            user = row_to_dict(user_row)
            if check_password_hash(user["password"], password):
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


@app.route("/staff/member-portal")
def staff_member_portal():
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") not in ["admin", "chairperson", "treasurer", "secretary", "publicity"]:
        flash('Access denied. Only staff members can access this portal.', 'danger')
        return redirect("/login")

    return redirect(url_for('member_dashboard', staff_view=True))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ============================================
# ADMIN DASHBOARD
# ------------------------------------------------------------
# Accessible by: admin, chairperson, treasurer, secretary
# Every visit is recorded in system_logs
# ============================================
@app.route("/admin/dashboard")
def admin_dashboard():
    # ---- Access control ----
    allowed_roles = ("admin", "chairperson", "treasurer", "secretary")
    if session.get("role") not in allowed_roles:
        flash('Access denied', 'danger')
        return redirect("/login")

    # ---- Audit log (appears under /admin/logs) ----
    log_action(
        action="view_dashboard",
        target="admin_dashboard",
        details=(
            f"Accessed by {session.get('role', 'unknown')} "
            f"({session.get('full_name', 'unknown')})"
        )
    )

    conn = get_db()
    try:
        # -------- Member stats --------
        total_members = fetchval(conn, """
            SELECT COUNT(*) FROM users WHERE LOWER(role) = 'member'
        """) or 0

        total_savings = fetchval(conn, """
            SELECT COALESCE(SUM(savings_balance), 0)
            FROM users
            WHERE LOWER(role) = 'member'
        """) or 0

        if DATABASE_URL:
            monthly_savings = fetchval(conn, """
                SELECT COALESCE(SUM(amount), 0)
                FROM savings_deposits
                WHERE NULLIF(deposit_date, '')::timestamp >= date_trunc('month', CURRENT_DATE)
            """) or 0
        else:
            monthly_savings = fetchval(conn, """
                SELECT COALESCE(SUM(amount), 0)
                FROM savings_deposits
                WHERE deposit_date >= date('now', 'start of month')
            """) or 0

        total_deposits = fetchval(conn, "SELECT COUNT(*) FROM savings_deposits") or 0

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
                COALESCE((
                    SELECT COUNT(*) FROM loans
                    WHERE user_id = u.id
                    AND status IN ('approved', 'disbursed', 'active')
                ), 0) AS active_loans_count
            FROM users u
            WHERE LOWER(u.role) = 'member'
            ORDER BY u.id DESC
        """).fetchall()

        # -------- Loan stats --------
        total_loans = fetchval(conn, """
            SELECT COALESCE(SUM(amount), 0)
            FROM loans
            WHERE status IN ('approved', 'disbursed', 'active')
        """) or 0

        active_loans = fetchval(conn, """
            SELECT COUNT(*)
            FROM loans
            WHERE status IN ('approved', 'disbursed', 'active')
        """) or 0

        pending_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'pending'") or 0
        approved_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'approved'") or 0
        rejected_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'rejected'") or 0
        disbursed_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'disbursed'") or 0
        completed_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'completed'") or 0

        loan_applications = conn.execute("""
            SELECT
                l.*,
                u.full_name,
                u.savings_balance,
                COALESCE((
                    SELECT COUNT(*) FROM loan_guarantors
                    WHERE loan_id = l.id AND status = 'active'
                ), 0) AS total_guarantors,
                COALESCE((
                    SELECT COUNT(*) FROM loan_guarantors
                    WHERE loan_id = l.id AND status = 'pending'
                ), 0) AS pending_guarantors
            FROM loans l
            JOIN users u ON l.user_id = u.id
            ORDER BY
                CASE
                    WHEN l.status = 'pending'   THEN 1
                    WHEN l.status = 'approved'  THEN 2
                    WHEN l.status = 'disbursed' THEN 3
                    WHEN l.status = 'active'    THEN 4
                    WHEN l.status = 'completed' THEN 5
                    WHEN l.status = 'rejected'  THEN 6
                END,
                l.application_date DESC
            LIMIT 50
        """).fetchall()

        recent_activities = conn.execute("""
            SELECT 'deposit' AS type, sd.amount, sd.deposit_date AS date,
                   u.full_name, u.sacco_number
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            UNION ALL
            SELECT 'repayment' AS type, r.amount, r.payment_date AS date,
                   u.full_name, u.sacco_number
            FROM repayments r
            JOIN users u ON r.user_id = u.id
            WHERE r.status = 'completed'
            ORDER BY date DESC
            LIMIT 10
        """).fetchall()

        # -------- Staff --------
        staff_users = conn.execute("""
            SELECT
                u.*,
                COUNT(DISTINCT l.id)  AS loans_processed,
                COUNT(DISTINCT sd.id) AS deposits_processed
            FROM users u
            LEFT JOIN loans l             ON l.user_id = u.id
            LEFT JOIN savings_deposits sd ON sd.user_id = u.id
            WHERE LOWER(u.role) IN ('admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            GROUP BY u.id
            ORDER BY
                CASE
                    WHEN LOWER(u.role) = 'admin'       THEN 1
                    WHEN LOWER(u.role) = 'chairperson' THEN 2
                    WHEN LOWER(u.role) = 'treasurer'   THEN 3
                    WHEN LOWER(u.role) = 'secretary'   THEN 4
                    WHEN LOWER(u.role) = 'publicity'   THEN 5
                END,
                u.full_name
        """).fetchall()

        staff_counts = {
            'treasurer': fetchval(conn, "SELECT COUNT(*) FROM users WHERE LOWER(role) = 'treasurer'") or 0,
            'secretary': fetchval(conn, "SELECT COUNT(*) FROM users WHERE LOWER(role) = 'secretary'") or 0,
            'publicity': fetchval(conn, "SELECT COUNT(*) FROM users WHERE LOWER(role) = 'publicity'") or 0,
            'admin':     fetchval(conn, "SELECT COUNT(*) FROM users WHERE LOWER(role) IN ('admin', 'chairperson')") or 0,
        }

        today = datetime.now().strftime('%Y-%m-%d')

        # -------- Settings --------
        default_settings = {
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
            'registration_fee': 20000,
        }

        settings = default_settings.copy()
        try:
            settings_row = conn.execute(
                "SELECT * FROM system_settings LIMIT 1"
            ).fetchone()
            if settings_row:
                settings.update(row_to_dict(settings_row))
        except Exception as e:
            print(f"Error loading settings: {e}")

        # ---- Flag for the template so it can hide admin-only widgets ----
        is_full_admin = session.get("role") in ("admin", "chairperson")

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
            settings=settings,
            is_full_admin=is_full_admin,   # ← new
        )

    except Exception as e:
        import traceback
        return (
            "<pre style='background:#0a0a0a;color:#ff6b6b;padding:24px;"
            "font-size:14px;line-height:1.6;font-family:monospace;"
            "white-space:pre-wrap;'>ADMIN DASHBOARD ERROR:\n\n"
            + traceback.format_exc() +
            "</pre>",
            500
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# ADMIN — SYSTEM LOGS
# ------------------------------------------------------------
# Accessible by: admin, chairperson (treasurer & secretary
# still cannot view logs — only their access is recorded).
# ============================================================
@app.route("/admin/logs")
def admin_system_logs():
    if session.get("role") not in ("admin", "chairperson"):
        flash('Access denied', 'danger')
        return redirect("/login")

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # ---------------- Filters ----------------
        filter_user   = (request.args.get("user") or "").strip()
        filter_action = (request.args.get("action") or "").strip()
        filter_target = (request.args.get("target") or "").strip()
        filter_from   = (request.args.get("from") or "").strip()
        filter_to     = (request.args.get("to") or "").strip()

        where = ["1=1"]
        params = []

        if filter_user:
            where.append(f"(LOWER(user_name) LIKE {PH} OR CAST(user_id AS TEXT) = {PH})")
            params.append(f"%{filter_user.lower()}%")
            params.append(filter_user)

        if filter_action:
            where.append(f"action = {PH}")
            params.append(filter_action)

        if filter_target:
            where.append(f"LOWER(target) LIKE {PH}")
            params.append(f"%{filter_target.lower()}%")

        if filter_from:
            where.append(f"created_at >= {PH}")
            params.append(filter_from + " 00:00:00")

        if filter_to:
            where.append(f"created_at <= {PH}")
            params.append(filter_to + " 23:59:59")

        where_sql = " AND ".join(where)

        # ---------------- Fetch logs ----------------
        logs = db.execute(f"""
            SELECT id, user_id, user_name, user_role,
                   action, target, details,
                   ip_address, created_at
            FROM system_logs
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT 500
        """, tuple(params)).fetchall()
        logs = logs or []

        # ---------------- Summary counts ----------------
        total_logs = fetchval(db, "SELECT COUNT(*) FROM system_logs") or 0

        # Treasurer dashboard visits
        treasurer_access_count = fetchval(db, """
            SELECT COUNT(*) FROM system_logs
            WHERE target = 'treasurer_dashboard'
        """) or 0

        # Admin dashboard visits (from treasurer + secretary + admin)
        admin_dashboard_access_count = fetchval(db, """
            SELECT COUNT(*) FROM system_logs
            WHERE target = 'admin_dashboard'
        """) or 0

        # Secretary-only accesses to admin_dashboard
        admin_dashboard_secretary_count = fetchval(db, """
            SELECT COUNT(*) FROM system_logs
            WHERE target = 'admin_dashboard'
              AND user_role = 'secretary'
        """) or 0

        # Treasurer-only accesses to admin_dashboard
        admin_dashboard_treasurer_count = fetchval(db, """
            SELECT COUNT(*) FROM system_logs
            WHERE target = 'admin_dashboard'
              AND user_role = 'treasurer'
        """) or 0

        # ---------------- Unique accessors per dashboard ----------------
        # Treasurer dashboard
        treasurer_accessors = db.execute("""
            SELECT user_name, user_role, COUNT(*) AS hits
            FROM system_logs
            WHERE target = 'treasurer_dashboard'
            GROUP BY user_name, user_role
            ORDER BY hits DESC
        """).fetchall()
        treasurer_accessors = treasurer_accessors or []

        # Admin dashboard — every role that opened it
        admin_dashboard_accessors = db.execute("""
            SELECT user_name, user_role, COUNT(*) AS hits
            FROM system_logs
            WHERE target = 'admin_dashboard'
            GROUP BY user_name, user_role
            ORDER BY hits DESC
        """).fetchall()
        admin_dashboard_accessors = admin_dashboard_accessors or []

        # ---------------- Distinct actions ----------------
        actions_rows = db.execute("""
            SELECT DISTINCT action FROM system_logs
            WHERE action IS NOT NULL
            ORDER BY action
        """).fetchall()
        actions_rows = actions_rows or []

        actions_list = []
        for a in actions_rows:
            d = row_to_dict(a)
            if d and d.get("action"):
                actions_list.append(d["action"])

        return render_template(
            "admin/system-logs.html",
            logs=logs,
            total_logs=total_logs,

            # Treasurer dashboard
            treasurer_access_count=treasurer_access_count,
            unique_accessors=treasurer_accessors,

            # Admin dashboard
            admin_dashboard_access_count=admin_dashboard_access_count,
            admin_dashboard_secretary_count=admin_dashboard_secretary_count,
            admin_dashboard_treasurer_count=admin_dashboard_treasurer_count,
            admin_dashboard_accessors=admin_dashboard_accessors,

            # Filters + dropdowns
            actions=actions_list,
            filter_user=filter_user,
            filter_action=filter_action,
            filter_target=filter_target,
            filter_from=filter_from,
            filter_to=filter_to,
        )

    except Exception as e:
        import traceback
        return (
            "<pre style='background:#0a0a0a;color:#ff6b6b;padding:24px;"
            "font-size:14px;line-height:1.6;font-family:monospace;"
            "white-space:pre-wrap;'>ADMIN LOGS ERROR:\n\n"
            + traceback.format_exc() +
            "</pre>",
            500
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
        
# ============================================================
# TREASURER DASHBOARD
# ============================================================
@app.route("/treasurer/dashboard")
def treasurer_dashboard():
    if session.get("role") not in ["treasurer", "secretary", "admin", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    log_action(
        action="view_dashboard",
        target="treasurer_dashboard",
        details=f"Accessed by {session.get('role', 'unknown')}"
    )

    conn = get_db()
    try:
        # ============================================================
        # MEMBER LIST QUERIES
        # ============================================================
        members = conn.execute("""
            SELECT 
                id, full_name, sacco_number, email, phone, status,
                savings_balance, registration_date, gender, dob, address,
                role, kai_shares, ks_shares,
                COALESCE(kac_paid, 0) AS kac_paid,
                COALESCE(kac_used, 0) AS kac_used,
                registration_fee_paid,
                next_of_kin_name, next_of_kin_phone, relationship
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

        staff_members = conn.execute("""
            SELECT 
                id, full_name, sacco_number, email, phone, status,
                savings_balance, role, kai_shares, ks_shares,
                COALESCE(kac_paid, 0) AS kac_paid,
                COALESCE(kac_used, 0) AS kac_used,
                registration_fee_paid
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            ORDER BY full_name ASC
        """).fetchall()

        regular_members = conn.execute("""
            SELECT 
                id, full_name, sacco_number, email, phone, status,
                savings_balance, role, kai_shares, ks_shares,
                COALESCE(kac_paid, 0) AS kac_paid,
                COALESCE(kac_used, 0) AS kac_used,
                registration_fee_paid
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) = 'member'
            ORDER BY full_name ASC
        """).fetchall()

        recent_deposits = conn.execute("""
            SELECT 
                sd.id, sd.user_id, sd.amount, sd.savings_type, sd.shares,
                sd.deposit_date, sd.payment_method, sd.receipt_number, sd.notes,
                u.full_name, u.sacco_number, u.role
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
            ORDER BY sd.deposit_date DESC, sd.created_at DESC
            LIMIT 20
        """).fetchall()

        recent_repayments = conn.execute("""
            SELECT 
                r.id, r.loan_id, r.user_id, r.amount,
                r.interest_paid, r.principal_paid, r.balance_after,
                r.payment_date, r.payment_method, r.transaction_ref,
                r.notes, r.status, r.created_at,
                u.full_name, u.sacco_number, u.role, l.loan_number
            FROM repayments r
            JOIN users u ON r.user_id = u.id
            JOIN loans l ON r.loan_id = l.id
            WHERE r.status = 'completed'
            AND u.status = 'active'
            ORDER BY r.payment_date DESC
            LIMIT 20
        """).fetchall()

        all_deposits = conn.execute("""
            SELECT 
                sd.id, sd.user_id, sd.amount, sd.savings_type, sd.shares,
                sd.deposit_date, sd.payment_method, sd.receipt_number, sd.notes,
                u.full_name, u.sacco_number, u.role
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
            ORDER BY sd.deposit_date DESC, sd.created_at DESC
        """).fetchall()

        total_members = len(members)
        total_regular_members = len(regular_members)
        total_staff_members = len(staff_members)

        # ============================================================
        # CORE SAVINGS FIGURES
        # ------------------------------------------------------------
        # total_savings = money currently in member savings accounts.
        # This DROPS when a loan is disbursed (money leaves the pool)
        # and RISES on deposits + repayment principal.
        # ============================================================
        total_savings = fetchval(conn, """
            SELECT COALESCE(SUM(savings_balance), 0) 
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
        """)

        total_deposits = fetchval(conn, """
            SELECT COALESCE(SUM(sd.amount), 0) 
            FROM savings_deposits sd
            JOIN users u ON sd.user_id = u.id
            WHERE u.status = 'active'
        """)

        if DATABASE_URL:
            monthly_deposits = fetchval(conn, """
                SELECT COALESCE(SUM(sd.amount), 0) 
                FROM savings_deposits sd
                JOIN users u ON sd.user_id = u.id
                WHERE u.status = 'active'
                AND NULLIF(sd.deposit_date, '')::timestamp >= date_trunc('month', CURRENT_DATE)
            """)
        else:
            monthly_deposits = fetchval(conn, """
                SELECT COALESCE(SUM(sd.amount), 0) 
                FROM savings_deposits sd
                JOIN users u ON sd.user_id = u.id
                WHERE u.status = 'active'
                AND sd.deposit_date >= date('now', 'start of month')
            """)

        # ============================================================
        # LOAN ↔ SAVINGS RECONCILIATION
        # ------------------------------------------------------------
        # Since loans come OUT of savings:
        #   Total Savings + Outstanding Loans ≈ constant pool
        #   (deposits add to pool, disbursements move pool → loans,
        #    repayments move loans → pool)
        # ============================================================
        total_loan_outstanding = fetchval(conn, """
            SELECT COALESCE(SUM(current_balance), 0)
            FROM loans
            WHERE status IN ('disbursed', 'active')
        """) or 0

        total_principal_disbursed = fetchval(conn, """
            SELECT COALESCE(SUM(amount), 0)
            FROM loans
            WHERE status IN ('disbursed', 'active', 'completed')
        """) or 0

        total_principal_repaid = fetchval(conn, """
            SELECT COALESCE(SUM(principal_paid), 0)
            FROM loans
            WHERE status IN ('disbursed', 'active', 'completed')
        """) or 0

        # What was paid OUT of members' savings for loans (all time)
        total_loan_disbursed_from_savings = total_principal_disbursed

        # Net effect on savings from loan activity so far
        # (negative = money still out; positive = fully returned)
        net_loan_impact_on_savings = total_principal_repaid - total_principal_disbursed

        # The "pool" view: savings + what's still out on loans
        pool_balance = (total_savings or 0) + total_loan_outstanding

        # ============================================================
        # LOAN STATUS COUNTS
        # ============================================================
        pending_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'pending'")
        approved_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'approved'")
        active_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status IN ('disbursed', 'active')")
        completed_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'completed'")
        rejected_loans = fetchval(conn, "SELECT COUNT(*) FROM loans WHERE status = 'rejected'")
                # ---- Total amount disbursed (all-time principal of disbursed/active/completed loans) ----
        total_disbursed_amount = fetchval(conn, """
            SELECT COALESCE(SUM(amount), 0)
            FROM loans
            WHERE status IN ('disbursed', 'active', 'completed')
        """) or 0

        # ============================================================
        # KAC AGGREGATES (NET BALANCE MODEL)
        # ============================================================
        kai_total = ks_total = kac_total = registration_fees_total = 0
        kai_members = ks_members = kac_members = registration_fees_count = 0
        kai_shares = ks_shares = 0

        kac_total_paid = 0
        kac_total_used = 0
        kac_debt_count = 0
        kac_debt_total = 0

        settings = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings:
            settings_dict = row_to_dict(settings)
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
            user_dict = row_to_dict(user)

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

            kac_paid = int(user_dict.get('kac_paid') or 0)
            kac_used = int(user_dict.get('kac_used') or 0)

            if kac_paid > 0:
                kac_members += 1
                kac_total_paid += kac_paid
                kac_total_used += kac_used
                if kac_paid < kac_used:
                    kac_debt_count += 1
                    kac_debt_total += (kac_used - kac_paid)

            if user_dict.get('registration_fee_paid'):
                registration_fees_total += registration_fee
                registration_fees_count += 1

        kac_total = kac_total_paid - kac_total_used

        # ============================================================
        # LOAN APPLICATIONS
        # ============================================================
        loan_applications = conn.execute("""
            SELECT 
                l.id, l.loan_number, l.amount, l.interest_rate, l.interest_amount,
                l.total_repayment, l.monthly_installment, l.tenure, l.purpose,
                l.repayment_plan, l.status, l.application_date, l.approved_date,
                l.disbursed_date, l.completed_date, l.current_balance,
                l.interest_accrued, l.due_date, l.loan_start_date, l.loan_end_date,
                l.rejection_reason, l.admin_rejection_reason, l.application_fee,
                l.application_fee_paid, l.net_loan_amount, l.total_interest_accrued,
                l.interest_paid,
                l.send_to_type, l.send_to_value, l.send_to_secondary,
                u.full_name, u.sacco_number, u.phone, u.email,
                u.savings_balance, u.role
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE u.status = 'active'
            ORDER BY l.application_date DESC
            LIMIT 50
        """).fetchall()

        all_loan_applications = []
        for loan_row in loan_applications:
            loan = row_to_dict(loan_row)
            guarantors = conn.execute("""
                SELECT id, guarantor_name, phone, email, relationship, status
                FROM loan_guarantors 
                WHERE loan_id = %s 
                ORDER BY id
            """ if DATABASE_URL else """
                SELECT id, guarantor_name, phone, email, relationship, status
                FROM loan_guarantors 
                WHERE loan_id = ? 
                ORDER BY id
            """, (loan['id'],)).fetchall()
            loan['guarantors'] = [row_to_dict(g) for g in guarantors] if guarantors else []
            all_loan_applications.append(loan)

        # ============================================================
        # ACTIVE LOANS (for repayment dropdown)
        # ============================================================
        active_loans_list = conn.execute("""
            SELECT 
                l.id, l.loan_number, l.amount, l.interest_rate, l.interest_amount,
                l.total_repayment, l.monthly_installment, l.tenure, l.purpose,
                l.repayment_plan, l.status, l.application_date, l.approved_date,
                l.disbursed_date, l.completed_date, l.current_balance,
                l.interest_accrued, l.due_date, l.loan_start_date, l.loan_end_date,
                l.disbursed_amount, l.application_fee, l.application_fee_paid,
                l.net_loan_amount, l.total_interest_accrued, l.interest_paid,
                l.send_to_type, l.send_to_value, l.send_to_secondary,
                u.full_name, u.sacco_number, u.phone, u.email, u.role
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status IN ('disbursed', 'active')
            AND u.status = 'active'
            ORDER BY l.application_date DESC
        """).fetchall()

        completed_loans_list = conn.execute("""
            SELECT 
                l.id, l.loan_number, l.amount, l.interest_rate, l.interest_amount,
                l.total_repayment, l.monthly_installment, l.tenure, l.purpose,
                l.repayment_plan, l.status, l.application_date, l.approved_date,
                l.disbursed_date, l.completed_date, l.current_balance,
                l.interest_accrued, l.due_date, l.loan_start_date, l.loan_end_date,
                l.disbursed_amount, l.application_fee, l.application_fee_paid,
                l.net_loan_amount, l.total_interest_accrued, l.interest_paid,
                u.full_name, u.sacco_number, u.phone, u.email, u.role
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.status = 'completed'
            AND u.status = 'active'
            ORDER BY l.completed_date DESC, l.application_date DESC
        """).fetchall()

        total_interest_accrued = fetchval(conn, "SELECT COALESCE(SUM(total_interest_accrued), 0) FROM loans")
        total_interest_paid = fetchval(conn, "SELECT COALESCE(SUM(interest_paid), 0) FROM loans")
        total_interest_outstanding = (total_interest_accrued or 0) - (total_interest_paid or 0)

        highest_borrower = {'name': 'N/A', 'total': 0}
        highest_interest_borrower = {'name': 'N/A', 'interest': 0}
        total_loan_fees = 0

        current_year = datetime.now().year
        year_start = f"{current_year}-01-01"
        year_end = f"{current_year}-12-31"
        placeholder = "%s" if DATABASE_URL else "?"

        loans_this_year = conn.execute(f"""
            SELECT 
                l.user_id, u.full_name, u.role,
                COUNT(l.id) as loan_count,
                COALESCE(SUM(l.amount), 0) as total_borrowed,
                COALESCE(SUM(l.interest_paid), 0) as total_interest_paid,
                COALESCE(SUM(l.total_interest_accrued), 0) as total_interest_accrued
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.application_date >= {placeholder} AND l.application_date <= {placeholder}
            AND l.status IN ('disbursed', 'active', 'completed')
            AND u.status = 'active'
            GROUP BY l.user_id, u.full_name, u.role
            ORDER BY total_borrowed DESC
        """, (year_start, year_end)).fetchall()

        total_loan_fees = fetchval(conn, f"""
            SELECT COUNT(*) * 1000 as total_fees
            FROM loans
            WHERE application_date >= {placeholder} AND application_date <= {placeholder}
            AND status IN ('disbursed', 'active', 'completed', 'approved')
        """, (year_start, year_end)) or 0

        if loans_this_year and len(loans_this_year) > 0:
            top_borrower = row_to_dict(loans_this_year[0])
            highest_borrower = {
                'name': top_borrower['full_name'],
                'total': top_borrower['total_borrowed'] or 0,
                'count': top_borrower['loan_count'] or 0,
                'role': top_borrower['role'] or 'member'
            }

            loans_list = []
            for loan in loans_this_year:
                loan_d = row_to_dict(loan)
                loans_list.append({
                    'full_name': loan_d['full_name'],
                    'total_interest_paid': loan_d['total_interest_paid'] or 0,
                    'total_interest_accrued': loan_d['total_interest_accrued'] or 0,
                    'role': loan_d['role'] or 'member'
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

        print("=" * 60)
        print("TREASURER DASHBOARD LOADED")
        print(f"Total Active Users: {total_members}")
        print(f"Total Savings (in pool): {total_savings:,.0f}")
        print(f"Out on Loans: {total_loan_outstanding:,.0f}")
        print(f"Pool (savings + loans): {pool_balance:,.0f}")
        print(f"Principal Disbursed (all-time): {total_principal_disbursed:,.0f}")
        print(f"Principal Repaid (all-time): {total_principal_repaid:,.0f}")
        print(f"KAC Net Balance: {kac_total:,.0f}")
        print("=" * 60)

        return render_template(
            "treasurer/treasurer-dashboard.html",
            members=members,
            regular_members=regular_members,
            staff_members=staff_members,
            total_members=total_members,
            total_regular_members=total_regular_members,
            total_staff_members=total_staff_members,
            total_savings=total_savings,
            total_deposits=total_deposits,
            monthly_deposits=monthly_deposits,
            all_deposits=all_deposits,
            recent_deposits=recent_deposits,

            # ============================================================
            # LOAN ↔ SAVINGS FIGURES
            # ============================================================
            total_loan_outstanding=total_loan_outstanding,
            total_principal_disbursed=total_principal_disbursed,
            total_principal_repaid=total_principal_repaid,
            net_loan_impact_on_savings=net_loan_impact_on_savings,
            pool_balance=pool_balance,

            # KAC
            kai_total=kai_total,
            ks_total=ks_total,
            kac_total=kac_total,
            kac_total_paid=kac_total_paid,
            kac_total_used=kac_total_used,
            kac_debt_count=kac_debt_count,
            kac_debt_total=kac_debt_total,
            kac_annual_fee=kac_annual_fee,
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
            total_disbursed_amount=total_disbursed_amount,
            active_loans_list=active_loans_list,
            completed_loans_list=completed_loans_list,
            all_loan_applications=all_loan_applications,
            total_interest_accrued=total_interest_accrued,
            total_interest_paid=total_interest_paid,
            total_interest_outstanding=total_interest_outstanding,
            highest_borrower=highest_borrower,
            highest_interest_borrower=highest_interest_borrower,
            total_loan_fees=total_loan_fees,
            recent_repayments=recent_repayments,
            now=datetime.now()
        )

    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('login'))
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ============================================================
# TREASURER — RECORD SAVINGS DEPOSIT
# ============================================================
@app.route("/treasurer/savings/deposit", methods=["GET", "POST"])
def treasurer_savings_deposit():
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied. Only treasurer can record deposits.', 'danger')
        return redirect("/login")

    PH = "%s" if DATABASE_URL else "?"

    # ============================================================
    # POST — process the deposit
    # ============================================================
    if request.method == "POST":
        user_id        = request.form.get('user_id')
        amount         = float(request.form.get('amount', 0) or 0)
        savings_type   = request.form.get('savings_type', 'KAI')
        deposit_date   = request.form.get('deposit_date', datetime.now().strftime('%Y-%m-%d'))
        payment_method = request.form.get('payment_method', 'cash')
        receipt_number = request.form.get('receipt_number', '')
        notes          = request.form.get('notes', '')

        if not user_id:
            flash('Please select a member or staff', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))

        if amount <= 0:
            flash('Amount must be greater than 0', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))

        db = get_db()
        try:
            # ---- Verify user exists ----
            user_row = db.execute(
                f"SELECT id, role, full_name FROM users WHERE id = {PH}",
                (user_id,)
            ).fetchone()
            if not user_row:
                flash('User not found', 'danger')
                return redirect(url_for('treasurer_savings_deposit'))

            user = row_to_dict(user_row)

            # ---- Load settings ----
            settings_row = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
            if settings_row:
                settings = row_to_dict(settings_row)
                kai_share_price  = settings.get('kai_share_price')  or 100000
                ks_share_price   = settings.get('ks_share_price')   or 10000
                kac_annual_fee   = settings.get('kac_annual_fee')   or 100000
                registration_fee = settings.get('registration_fee') or 20000
            else:
                kai_share_price  = 100000
                ks_share_price   = 10000
                kac_annual_fee   = 100000
                registration_fee = 20000

            # ============================================================
            # KAC VALIDATION — prevent overpayment
            # ============================================================
            new_kac = None
            fully_paid = 0

            if savings_type == 'KAC':
                current_row = db.execute(
                    f"SELECT COALESCE(kac_paid, 0) AS current_kac FROM users WHERE id = {PH}",
                    (user_id,)
                ).fetchone()
                current_kac = float(row_to_dict(current_row).get('current_kac') or 0)
                remaining = max(0, kac_annual_fee - current_kac)

                if current_kac >= kac_annual_fee:
                    flash(
                        f'{user["full_name"]} has already fully paid KAC '
                        f'(UGX {kac_annual_fee:,.0f}).',
                        'warning'
                    )
                    return redirect(url_for('treasurer_savings_deposit'))

                if amount > remaining:
                    flash(
                        f'KAC payment exceeds remaining balance. '
                        f'Current: UGX {current_kac:,.0f} / {kac_annual_fee:,.0f} · '
                        f'Remaining: UGX {remaining:,.0f}',
                        'warning'
                    )
                    return redirect(url_for('treasurer_savings_deposit'))

            # ---- Compute shares ----
            shares = 0
            if savings_type == 'KAI':
                shares = int(amount / kai_share_price) if kai_share_price > 0 else 0
            elif savings_type == 'KS':
                shares = int(amount / ks_share_price) if ks_share_price > 0 else 0
            # KAC, REGISTRATION — no shares

            # ---- Insert deposit ----
            db.execute(f"""
                INSERT INTO savings_deposits (
                    user_id, amount, savings_type, shares, deposit_date,
                    payment_method, receipt_number, notes, created_at
                )
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})
            """, (
                user_id, amount, savings_type, shares, deposit_date,
                payment_method, receipt_number, notes,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))

            # ============================================================
            # UPDATE USER BALANCES
            # ============================================================

            if savings_type == 'KAI':
                db.execute(f"""
                    UPDATE users
                       SET savings_balance = COALESCE(savings_balance, 0) + {PH},
                           kai_shares      = COALESCE(kai_shares, 0) + {PH}
                     WHERE id = {PH}
                """, (amount, shares, user_id))

            elif savings_type == 'KS':
                db.execute(f"""
                    UPDATE users
                       SET savings_balance = COALESCE(savings_balance, 0) + {PH},
                           ks_shares       = COALESCE(ks_shares, 0) + {PH}
                     WHERE id = {PH}
                """, (amount, shares, user_id))

            elif savings_type == 'KAC':
                # Running total of KAC, capped at annual fee
                current_row = db.execute(
                    f"SELECT COALESCE(kac_paid, 0) AS current_kac FROM users WHERE id = {PH}",
                    (user_id,)
                ).fetchone()
                current_kac = float(row_to_dict(current_row).get('current_kac') or 0)

                new_kac = current_kac + amount
                if new_kac > kac_annual_fee:
                    new_kac = kac_annual_fee

                fully_paid = 1 if new_kac >= kac_annual_fee else 0
                paid_date_value = deposit_date if fully_paid else None

                db.execute(f"""
                    UPDATE users
                       SET savings_balance = COALESCE(savings_balance, 0) + {PH},
                           kac_paid        = {PH},
                           kac_paid_date   = COALESCE({PH}, kac_paid_date)
                     WHERE id = {PH}
                """, (amount, new_kac, paid_date_value, user_id))

            elif savings_type == 'REGISTRATION':
                # Does NOT touch savings_balance — marks registration as paid
                db.execute(f"""
                    UPDATE users
                       SET registration_fee_paid      = 1,
                           registration_fee_paid_date = {PH}
                     WHERE id = {PH}
                """, (deposit_date, user_id))

            db.commit()

            # 🔍 LOG
            log_action(
                action="savings_deposit",
                target=f"user:{user_id}",
                details=f"{savings_type} deposit UGX {amount:,.0f} for {user['full_name']}"
            )

            flash(f'Deposit of UGX {amount:,.0f} recorded for {user["full_name"]} ({savings_type})!', 'success')

            # ---- Debug ----
            print("=" * 60)
            print("DEPOSIT RECORDED")
            print(f"User: {user['full_name']} ({user['role']})")
            print(f"Type: {savings_type}")
            print(f"Amount: UGX {amount:,.0f}")
            print(f"Shares: {shares}")
            if savings_type == 'KAC' and new_kac is not None:
                print(f"KAC total now: UGX {new_kac:,.0f} / {kac_annual_fee:,.0f}")
                if fully_paid:
                    print("KAC FULLY PAID")
            print("=" * 60)

            flash(
                f'Deposit of UGX {amount:,.0f} recorded for {user["full_name"]} ({savings_type})!',
                'success'
            )
            return redirect(url_for('treasurer_dashboard') + '?panel=savings')

        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                db.rollback()
            except Exception:
                pass
            flash(f'Error recording deposit: {str(e)}', 'danger')
            return redirect(url_for('treasurer_savings_deposit'))
        finally:
            try:
                db.close()
            except Exception:
                pass

    # ============================================================
    # GET — show the form
    # ============================================================
    db = get_db()
    try:
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
                    WHEN LOWER(role) = 'member'      THEN 1
                    WHEN LOWER(role) = 'admin'       THEN 2
                    WHEN LOWER(role) = 'chairperson' THEN 3
                    WHEN LOWER(role) = 'treasurer'   THEN 4
                    WHEN LOWER(role) = 'secretary'   THEN 5
                    WHEN LOWER(role) = 'publicity'   THEN 6
                END,
                full_name ASC
        """).fetchall()

        # Split for the dropdown
        staff_members = []
        regular_members = []
        for u_row in all_users:
            u = row_to_dict(u_row)
            if (u.get('role') or '').lower() != 'member':
                staff_members.append(u)
            else:
                regular_members.append(u)

        completed_loans = fetchval(db,
            "SELECT COUNT(*) FROM loans WHERE status = 'completed'"
        ) or 0

        return render_template(
            "treasurer/savings-deposit.html",
            members=all_users,
            staff_members=staff_members,
            regular_members=regular_members,
            completed_loans=completed_loans
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
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
# ADMIN — APPROVE / REJECT LOAN (deducts savings on approval)
# ============================================================
@app.route("/admin/loan/approve/<int:loan_id>", methods=["POST"])
def admin_approve_loan(loan_id):
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please login first"}), 401

    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Access denied"}), 403

    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()
    reason = (data.get("reason") or "").strip()

    if action not in ("approve", "reject"):
        return jsonify({"success": False, "message": "Invalid action"}), 400

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today   = datetime.now().strftime('%Y-%m-%d')

    try:
        loan_row = db.execute(f"""
            SELECT
                l.*,
                u.id AS applicant_id,
                COALESCE(u.savings_balance, 0) AS current_savings,
                u.full_name,
                u.email,
                u.phone
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = {PH}
        """, (loan_id,)).fetchone()

        if not loan_row:
            return jsonify({"success": False, "message": "Loan not found"}), 404

        loan = row_to_dict(loan_row)

        # ============================================================
        # REJECT — no money moves
        # ============================================================
        if action == "reject":
            if not reason:
                return jsonify({"success": False, "message": "Rejection reason required"}), 400

            db.execute(f"""
                UPDATE loans
                SET status = 'rejected',
                    rejected_date = {PH},
                    rejection_reason = {PH},
                    rejected_by = {PH}
                WHERE id = {PH}
            """, (now_str, reason, session.get('full_name', 'Admin'), loan_id))

            try:
                db.execute(f"""
                    INSERT INTO notifications
                        (user_id, type, title, message, link, is_read, created_at)
                    VALUES ({PH}, 'loan_rejection', {PH}, {PH}, '/member/dashboard', 0, {PH})
                """, (
                    loan['applicant_id'],
                    'Loan Application Rejected',
                    f"Your loan {loan['loan_number']} has been rejected.\n\nReason: {reason}",
                    now_str
                ))
            except Exception as e:
                print(f"⚠️ Notification insert failed: {e}")

            db.commit()
            return jsonify({
                "success": True,
                "message": "Loan rejected successfully. The applicant has been notified."
            })

        # ============================================================
        # APPROVE — DEDUCT SAVINGS HERE
        # ============================================================
        if loan.get("status") != "approved":
            return jsonify({
                "success": False,
                "message": f"Loan is '{loan.get('status')}', not awaiting final approval."
            }), 400

        loan_principal  = float(loan['amount'] or 0)
        current_savings = float(loan['current_savings'] or 0)

        # 10% savings requirement
        required = loan_principal * 0.10
        if current_savings < required:
            return jsonify({
                "success": False,
                "message": (
                    f"Member needs at least 10% savings "
                    f"(UGX {required:,.0f}). Current: UGX {current_savings:,.0f}"
                )
            }), 400

        # Deduct principal from savings
        new_savings = current_savings - loan_principal
        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        balance  = float(loan.get('total_repayment') or loan.get('amount') or 0)

        db.execute("BEGIN TRANSACTION")

        # 1) Deduct from savings
        db.execute(f"""
            UPDATE users
            SET savings_balance = COALESCE(savings_balance, 0) - {PH}
            WHERE id = {PH}
        """, (loan_principal, loan['applicant_id']))

        # 2) Audit row
        try:
            db.execute(f"""
                INSERT INTO savings_deposits
                    (user_id, amount, savings_type, deposit_date,
                     payment_method, receipt_number, notes)
                VALUES ({PH}, {PH}, 'LOAN_DISBURSEMENT', {PH}, 'internal', {PH}, {PH})
            """, (
                loan['applicant_id'],
                -loan_principal,
                today,
                f"DISB-{loan['loan_number']}",
                f"Loan {loan['loan_number']} approved & disbursed by Admin — deducted from savings"
            ))
        except Exception as e:
            print(f"⚠️ Could not log disbursement: {e}")

        # 3) Mark as disbursed
        db.execute(f"""
            UPDATE loans
            SET status = 'disbursed',
                approved_date = {PH},
                disbursed_date = {PH},
                disbursement_date = {PH},
                disbursed_amount = {PH},
                loan_start_date = {PH},
                loan_end_date = {PH},
                due_date = {PH},
                current_balance = {PH},
                approved_by = {PH},
                approved_by_role = 'admin',
                disbursed_by = {PH},
                disbursed_by_role = 'admin'
            WHERE id = {PH}
        """, (
            today, today, today, loan_principal,
            today, end_date, end_date,
            balance,
            session.get('full_name', 'Admin'),
            session.get('full_name', 'Admin'),
            loan_id
        ))

        # 4) Notify member
        try:
            db.execute(f"""
                INSERT INTO notifications
                    (user_id, type, title, message, link, is_read, created_at)
                VALUES ({PH}, 'loan_disbursed', {PH}, {PH}, '/member/dashboard', 0, {PH})
            """, (
                loan['applicant_id'],
                'Loan Approved & Disbursed',
                (
                    f"Your loan {loan['loan_number']} has been approved and disbursed. "
                    f"UGX {loan_principal:,.0f} was deducted from your savings. "
                    f"Please repay by {end_date}."
                ),
                now_str
            ))
        except Exception as e:
            print(f"⚠️ Notification insert failed: {e}")

        db.commit()

        return jsonify({
            "success": True,
            "message": (
                f"Loan approved & disbursed. "
                f"UGX {loan_principal:,.0f} deducted from savings. "
                f"New savings: UGX {new_savings:,.0f}."
            ),
            "deducted": loan_principal,
            "new_savings": new_savings
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass

# ============================================================
# TREASURER — KAC CLAIMS PAGE
# ============================================================
@app.route("/treasurer/kac-claims")
def treasurer_kac_claims_page():
    if session.get("role") not in ["treasurer", "admin", "chairperson", "secretary"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    log_action(
        action="view_kac_claims",
        target="treasurer_kac_claims",
        details=f"Accessed by {session.get('role', 'unknown')}"
    )

    conn = get_db()
    try:
        # Settings
        settings_row = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        settings = row_to_dict(settings_row) if settings_row else {}
        if not isinstance(settings, dict):
            settings = {}
        kac_condolence_amount = int(settings.get('kac_condolence_amount') or 20000)
        kac_death_amount      = int(settings.get('kac_death_amount') or 40000)
        kac_annual_fee        = int(settings.get('kac_annual_fee') or 100000)

        # ------------------------------------------------------------
        # KAC MEMBERS ONLY (paid at least once)
        # ------------------------------------------------------------
        staff_members = conn.execute("""
            SELECT 
                id, full_name, sacco_number, role,
                COALESCE(kac_paid, 0) AS kac_paid,
                COALESCE(kac_used, 0) AS kac_used
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) IN ('admin', 'chairperson', 'treasurer', 'secretary', 'publicity')
            AND COALESCE(kac_paid, 0) > 0
            ORDER BY full_name ASC
        """).fetchall()

        regular_members = conn.execute("""
            SELECT 
                id, full_name, sacco_number, role,
                COALESCE(kac_paid, 0) AS kac_paid,
                COALESCE(kac_used, 0) AS kac_used
            FROM users 
            WHERE status = 'active'
            AND LOWER(role) = 'member'
            AND COALESCE(kac_paid, 0) > 0
            ORDER BY full_name ASC
        """).fetchall()

        # Stats
        eligible_count  = 0
        partial_count   = 0
        debt_risk_count = 0

        for m_row in list(staff_members) + list(regular_members):
            m = row_to_dict(m_row)
            kac_paid = int(m.get('kac_paid') or 0)
            kac_used = int(m.get('kac_used') or 0)
            if kac_paid >= kac_annual_fee and kac_paid >= kac_used:
                eligible_count += 1
            elif kac_paid > 0:
                partial_count += 1
            if (kac_paid - kac_used) < kac_death_amount:
                debt_risk_count += 1

        chargeable_count = len(staff_members) + len(regular_members)

        # Claims list
        claims_rows = conn.execute("""
            SELECT 
                c.*,
                r.full_name AS register_entry_name,
                r.relationship AS register_relationship,
                r.slot_number AS register_slot
            FROM kac_claims c
            LEFT JOIN member_condolence_register r ON r.id = c.register_entry_id
            ORDER BY c.created_at DESC
        """).fetchall()

        kac_claims = [row_to_dict(c) for c in claims_rows]
        total_claims = len(kac_claims)
        total_condolences = sum(1 for c in kac_claims if c.get('claim_type') == 'condolence')
        total_deaths      = sum(1 for c in kac_claims if c.get('claim_type') == 'death')
        total_collected_all = sum(
            float(c.get('total_collected') or 0) for c in kac_claims
            if c.get('status') != 'reversed'
        )

        # Register counts
        register_count_rows = conn.execute("""
            SELECT user_id, COUNT(*) AS cnt
            FROM member_condolence_register
            GROUP BY user_id
        """).fetchall()
        register_counts = {}
        for r in register_count_rows:
            rd = row_to_dict(r)
            register_counts[rd['user_id']] = rd['cnt']

        return render_template(
            "treasurer/treasurer-kac.html",
            staff_members=staff_members,
            regular_members=regular_members,
            kac_claims=kac_claims,
            total_claims=total_claims,
            total_condolences=total_condolences,
            total_deaths=total_deaths,
            total_collected_all=total_collected_all,
            eligible_count=eligible_count,
            partial_count=partial_count,
            chargeable_count=chargeable_count,
            debt_risk_count=debt_risk_count,
            register_counts=register_counts,
            kac_condolence_amount=kac_condolence_amount,
            kac_death_amount=kac_death_amount,
            kac_annual_fee=kac_annual_fee,
            now=datetime.now()
        )

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("=" * 70)
        print("KAC CLAIMS PAGE ERROR")
        print(tb)
        print("=" * 70)
        return f"<pre>KAC page error:\n\n{tb}</pre>", 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------
# RECORD A CLAIM — charges ONLY KAC members (may go negative)
# ------------------------------------------------------------
@app.route("/treasurer/kac/claim", methods=["POST"])
def treasurer_kac_claim():
    print("=" * 60)
    print("[KAC CLAIM] route entered")
    print("[KAC CLAIM] session role:", session.get("role"))

    if session.get("role") not in ["treasurer", "admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    print("[KAC CLAIM] payload:", data)

    claim_type = (data.get('claim_type') or '').lower()
    affected_user_id = data.get('affected_user_id')
    event_date = data.get('event_date') or datetime.now().strftime('%Y-%m-%d')
    register_entry_id = data.get('register_entry_id')
    description = (data.get('description') or '').strip()

    if claim_type not in ('condolence', 'death'):
        return jsonify({'success': False, 'message': 'Invalid claim type'}), 400
    if not affected_user_id:
        return jsonify({'success': False, 'message': 'Affected member is required'}), 400

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # ---- Load deduction amount from settings ----
        settings_row = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        s = row_to_dict(settings_row) if settings_row else {}
        if not isinstance(s, dict):
            s = {}
        deduction = int(s.get('kac_death_amount') or 40000) if claim_type == 'death' \
                    else int(s.get('kac_condolence_amount') or 20000)
        print("[KAC CLAIM] deduction:", deduction)

        # ---- Load affected user ----
        affected_row = db.execute(
            f"SELECT id, full_name FROM users WHERE id = {PH}",
            (affected_user_id,)
        ).fetchone()
        if not affected_row:
            return jsonify({'success': False, 'message': 'Affected member not found'}), 404
        affected = row_to_dict(affected_row)
        print("[KAC CLAIM] affected:", affected)

        # ---- Generate claim number ----
        year = datetime.now().year
        count = fetchval(db, f"""
            SELECT COUNT(*) FROM kac_claims WHERE claim_number LIKE {PH}
        """, (f"KAC-{year}-%",)) or 0
        claim_number = f"KAC-{year}-{int(count) + 1:04d}"
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # ---- Insert the claim ----
        if DATABASE_URL:
            row = db.execute(f"""
                INSERT INTO kac_claims (
                    claim_number, claim_type, affected_user_id, affected_name,
                    register_entry_id, deduction_amount, event_date, description,
                    created_by, created_at, status
                ) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},'active')
                RETURNING id
            """, (claim_number, claim_type, affected_user_id, affected['full_name'],
                  register_entry_id, deduction, event_date, description,
                  session['user_id'], now_str)).fetchone()
            claim_id = row['id'] if isinstance(row, dict) else row[0]
        else:
            cur = db.cursor()
            cur.execute("""
                INSERT INTO kac_claims (
                    claim_number, claim_type, affected_user_id, affected_name,
                    register_entry_id, deduction_amount, event_date, description,
                    created_by, created_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'active')
            """, (claim_number, claim_type, affected_user_id, affected['full_name'],
                  register_entry_id, deduction, event_date, description,
                  session['user_id'], now_str))
            claim_id = cur.lastrowid
        print("[KAC CLAIM] inserted claim id:", claim_id)

        # ============================================================
        # Charge ONLY KAC members
        # ============================================================
        everyone = db.execute(f"""
            SELECT id,
                   COALESCE(kac_paid, 0) AS kac_paid,
                   COALESCE(kac_used, 0) AS kac_used
            FROM users
            WHERE {is_kac_member_clause()}
        """).fetchall()
        print("[KAC CLAIM] KAC members to charge:", len(everyone))

        charged = 0
        total = 0
        debt_count = 0
        current_year = datetime.now().year

        for m_row in everyone:
            m = row_to_dict(m_row)

            before = int(m.get('kac_paid') or 0) - int(m.get('kac_used') or 0)
            deduct_now = deduction
            after = before - deduct_now

            if after < 0:
                debt_count += 1

            # 1) Lifetime used
            db.execute(f"""
                UPDATE users
                SET kac_used = COALESCE(kac_used, 0) + {PH}
                WHERE id = {PH}
            """, (deduct_now, m['id']))

            # 2) Year tracker
            existing = db.execute(f"""
                SELECT id FROM kac_year_contributions
                WHERE user_id = {PH} AND year = {PH}
            """, (m['id'], current_year)).fetchone()

            if existing:
                db.execute(f"""
                    UPDATE kac_year_contributions
                    SET used = COALESCE(used, 0) + {PH},
                        updated_at = {PH}
                    WHERE user_id = {PH} AND year = {PH}
                """, (deduct_now, now_str, m['id'], current_year))
            else:
                db.execute(f"""
                    INSERT INTO kac_year_contributions
                        (user_id, year, paid, used, target, created_at, updated_at)
                    VALUES ({PH},{PH},{PH},{PH},100000,{PH},{PH})
                """, (m['id'], current_year, m.get('kac_paid') or 0,
                      deduct_now, now_str, now_str))

            # 3) Audit row
            db.execute(f"""
                INSERT INTO kac_claim_deductions (
                    claim_id, user_id, amount_deducted, kac_before, kac_after, created_at
                ) VALUES ({PH},{PH},{PH},{PH},{PH},{PH})
            """, (claim_id, m['id'], deduct_now, before, after, now_str))

            charged += 1
            total += deduct_now

        # ---- Update claim totals ----
        db.execute(f"""
            UPDATE kac_claims SET members_charged = {PH}, total_collected = {PH}
            WHERE id = {PH}
        """, (charged, total, claim_id))

        # ---- Mark register slot as deceased (condolence only) ----
        if claim_type == 'condolence' and register_entry_id:
            db.execute(f"""
                UPDATE member_condolence_register
                SET status = 'deceased',
                    deceased_date = {PH},
                    deceased_claim_id = {PH},
                    updated_at = {PH}
                WHERE id = {PH} AND user_id = {PH}
            """, (event_date, claim_id, now_str, register_entry_id, affected_user_id))

        db.commit()
        print(f"[KAC CLAIM] SUCCESS — charged {charged}, total {total}, debt {debt_count}")

        message = (
            f'{claim_type.title()} claim recorded — '
            f'{charged} KAC member(s) charged, UGX {total:,.0f} total.'
        )
        if debt_count > 0:
            message += f' {debt_count} member(s) now owe the SACCO.'

        return jsonify({
            'success': True,
            'message': message,
            'claim_number': claim_number,
            'claim_id': claim_id,
            'charged': charged,
            'total_collected': total,
            'debt_count': debt_count
        })

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        tb = traceback.format_exc()
        print("=" * 70)
        print("KAC CLAIM ERROR")
        print(tb)
        print("=" * 70)
        return jsonify({
            'success': False,
            'message': f'{type(e).__name__}: {str(e)}'
        }), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# REVERSE A CLAIM
# ------------------------------------------------------------
@app.route("/treasurer/kac/claim/<int:claim_id>/reverse", methods=["POST"])
def treasurer_kac_reverse(claim_id):
    if session.get("role") not in ["treasurer", "admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'success': False, 'message': 'Reversal reason is required'}), 400

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        claim_row = db.execute(
            f"SELECT * FROM kac_claims WHERE id = {PH}", (claim_id,)
        ).fetchone()
        if not claim_row:
            return jsonify({'success': False, 'message': 'Claim not found'}), 404
        claim = row_to_dict(claim_row)
        if claim['status'] == 'reversed':
            return jsonify({'success': False, 'message': 'Already reversed'}), 400

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            claim_year = datetime.strptime(str(claim['event_date'])[:10], '%Y-%m-%d').year
        except Exception:
            claim_year = datetime.now().year

        deds = db.execute(
            f"SELECT * FROM kac_claim_deductions WHERE claim_id = {PH}", (claim_id,)
        ).fetchall()

        for d_row in deds:
            d = row_to_dict(d_row)

            db.execute(f"""
                UPDATE users SET kac_used = COALESCE(kac_used, 0) - {PH}
                WHERE id = {PH}
            """, (d['amount_deducted'], d['user_id']))

            db.execute(f"""
                UPDATE kac_year_contributions
                SET used = COALESCE(used, 0) - {PH},
                    updated_at = {PH}
                WHERE user_id = {PH} AND year = {PH}
            """, (d['amount_deducted'], now_str, d['user_id'], claim_year))

        if claim['claim_type'] == 'condolence' and claim.get('register_entry_id'):
            db.execute(f"""
                UPDATE member_condolence_register
                SET status = 'active',
                    deceased_date = NULL,
                    deceased_claim_id = NULL,
                    updated_at = {PH}
                WHERE id = {PH}
            """, (now_str, claim['register_entry_id']))

        db.execute(f"""
            UPDATE kac_claims
            SET status = 'reversed',
                reversed_at = {PH},
                reversed_by = {PH},
                reversal_reason = {PH}
            WHERE id = {PH}
        """, (now_str, session['user_id'], reason, claim_id))

        db.commit()
        return jsonify({
            'success': True,
            'message': 'Claim reversed and members refunded.'
        })

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# YEAR ROLLOVER — starts a fresh year for KAC contributions
# ------------------------------------------------------------
@app.route("/treasurer/kac/year-rollover", methods=["POST"])
def treasurer_kac_year_rollover():
    if session.get("role") not in ["treasurer", "admin", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        current_year = datetime.now().year
        new_year = current_year + 1
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        members = db.execute(f"""
            SELECT id, COALESCE(kac_paid, 0) AS kac_paid
            FROM users
            WHERE {is_kac_member_clause()}
        """).fetchall()

        created = 0
        for m_row in members:
            m = row_to_dict(m_row)

            existing = db.execute(f"""
                SELECT id FROM kac_year_contributions
                WHERE user_id = {PH} AND year = {PH}
            """, (m['id'], new_year)).fetchone()
            if existing:
                continue

            db.execute(f"""
                INSERT INTO kac_year_contributions
                    (user_id, year, paid, used, target, created_at, updated_at)
                VALUES ({PH},{PH},0,0,100000,{PH},{PH})
            """, (m['id'], new_year, now_str, now_str))

            db.execute(f"""
                UPDATE users
                SET kac_year = {PH}, kac_year_paid = 0
                WHERE id = {PH}
            """, (new_year, m['id']))

            created += 1

        db.commit()

        try:
            log_action(
                action="kac_year_rollover",
                target="all_users",
                details=(f"Rolled over to {new_year} by {session.get('role')}. "
                         f"Savings preserved; 100,000/year obligation set.")
            )
        except Exception:
            pass

        return jsonify({
            'success': True,
            'message': (f'Rolled over to {new_year}. {created} KAC member(s) '
                        f'now have a fresh UGX 100,000 target. '
                        f'Savings from previous years were NOT affected.')
        })

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# Backwards-compatible alias
@app.route("/treasurer/kac/year-reset", methods=["POST"])
def treasurer_kac_year_reset_alias():
    return treasurer_kac_year_rollover()


# ------------------------------------------------------------
# CLAIM DETAILS (JSON for the View modal)
# ------------------------------------------------------------
@app.route("/treasurer/kac/claim/<int:claim_id>/details")
def treasurer_kac_claim_details(claim_id):
    if session.get("role") not in ["treasurer", "admin", "chairperson", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"
    try:
        claim_row = db.execute(f"""
            SELECT
                c.*,
                r.full_name AS register_entry_name,
                r.relationship AS register_relationship,
                r.slot_number AS register_slot
            FROM kac_claims c
            LEFT JOIN member_condolence_register r ON r.id = c.register_entry_id
            WHERE c.id = {PH}
        """, (claim_id,)).fetchone()

        if not claim_row:
            return jsonify({'success': False, 'message': 'Claim not found'}), 404

        claim = row_to_dict(claim_row)

        ded_rows = db.execute(f"""
            SELECT d.*, u.full_name, u.sacco_number, u.role
            FROM kac_claim_deductions d
            JOIN users u ON u.id = d.user_id
            WHERE d.claim_id = {PH}
            ORDER BY d.id ASC
        """, (claim_id,)).fetchall()

        deductions = [row_to_dict(d) for d in ded_rows]

        return jsonify({
            'success': True,
            'claim': claim,
            'deductions': deductions
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# MEMBER'S CONDOLENCE REGISTER — read
# ------------------------------------------------------------
@app.route("/treasurer/member/condolence-register/<int:user_id>", methods=["GET"])
def treasurer_get_member_register(user_id):
    if session.get("role") not in ["treasurer", "admin", "chairperson", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"
    try:
        rows = db.execute(f"""
            SELECT id, slot_number, full_name, relationship, phone, status, deceased_date
            FROM member_condolence_register
            WHERE user_id = {PH}
            ORDER BY slot_number ASC
        """, (user_id,)).fetchall()

        register = [row_to_dict(r) for r in rows]
        return jsonify({'success': True, 'register': register})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# ADD REGISTER ENTRY (max 10 lifetime)
# ------------------------------------------------------------
@app.route("/treasurer/member/condolence-register/add", methods=["POST"])
def treasurer_add_register_entry():
    if session.get("role") not in ["treasurer", "admin", "chairperson", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    full_name = (data.get('full_name') or '').strip()
    relationship = (data.get('relationship') or '').strip()
    phone = (data.get('phone') or '').strip()

    if not user_id or not full_name:
        return jsonify({'success': False, 'message': 'Member and name are required'}), 400

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"
    try:
        total = fetchval(db, f"""
            SELECT COUNT(*) FROM member_condolence_register WHERE user_id = {PH}
        """, (user_id,)) or 0
        if int(total) >= 10:
            return jsonify({
                'success': False,
                'message': 'Member has used all 10 slots. Deceased persons are not replaced.'
            }), 400

        max_slot = fetchval(db, f"""
            SELECT COALESCE(MAX(slot_number), 0) FROM member_condolence_register
            WHERE user_id = {PH}
        """, (user_id,)) or 0
        next_slot = int(max_slot) + 1

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if DATABASE_URL:
            row = db.execute(f"""
                INSERT INTO member_condolence_register
                    (user_id, slot_number, full_name, relationship, phone,
                     status, created_at, updated_at)
                VALUES ({PH},{PH},{PH},{PH},{PH},'active',{PH},{PH})
                RETURNING id
            """, (user_id, next_slot, full_name, relationship, phone,
                  now_str, now_str)).fetchone()
            new_id = row['id'] if isinstance(row, dict) else row[0]
        else:
            cur = db.cursor()
            cur.execute("""
                INSERT INTO member_condolence_register
                    (user_id, slot_number, full_name, relationship, phone,
                     status, created_at, updated_at)
                VALUES (?,?,?,?,?,'active',?,?)
            """, (user_id, next_slot, full_name, relationship, phone,
                  now_str, now_str))
            new_id = cur.lastrowid

        db.commit()
        return jsonify({
            'success': True,
            'message': f'{full_name} added to slot {next_slot}.',
            'id': new_id,
            'slot_number': next_slot
        })
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# REMOVE REGISTER ENTRY (only if not deceased)
# ------------------------------------------------------------
@app.route("/treasurer/member/condolence-register/<int:entry_id>/remove", methods=["POST"])
def treasurer_remove_register_entry(entry_id):
    if session.get("role") not in ["treasurer", "admin", "chairperson", "secretary"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"
    try:
        row = db.execute(
            f"SELECT status FROM member_condolence_register WHERE id = {PH}",
            (entry_id,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'message': 'Entry not found'}), 404

        r = row_to_dict(row)
        if r['status'] == 'deceased':
            return jsonify({
                'success': False,
                'message': 'Cannot remove a deceased entry — it is permanently closed.'
            }), 400

        db.execute(
            f"DELETE FROM member_condolence_register WHERE id = {PH}",
            (entry_id,)
        )
        db.commit()
        return jsonify({'success': True, 'message': 'Removed.'})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass

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
# TREASURER - APPROVE / REJECT LOAN (deducts savings on approval)
# ============================================================
@app.route("/treasurer/loan/action/<int:loan_id>", methods=["POST"])
def treasurer_approve_loan(loan_id):
    print(f"Loan action called for loan {loan_id}")

    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Please login first'}), 401

    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request'}), 400

    action = data.get('action')
    reason = (data.get('reason') or '').strip()

    if action not in ['approve', 'reject']:
        return jsonify({'success': False, 'message': 'Invalid action'}), 400

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today   = datetime.now().strftime('%Y-%m-%d')

    try:
        # ---- Load loan + applicant + current savings ----
        loan_row = db.execute(f"""
            SELECT
                l.*,
                u.id AS applicant_id,
                COALESCE(u.savings_balance, 0) AS current_savings,
                u.full_name,
                u.email,
                u.phone
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = {PH}
        """, (loan_id,)).fetchone()

        if not loan_row:
            return jsonify({'success': False, 'message': 'Loan not found'}), 404

        loan = row_to_dict(loan_row)

        # ============================================================
        # REJECT LOAN — no money moves
        # ============================================================
        if action == 'reject':
            if not reason:
                return jsonify({'success': False, 'message': 'Rejection reason required'}), 400

            db.execute(f"""
                UPDATE loans
                SET
                    status = 'rejected',
                    rejected_date = {PH},
                    rejection_reason = {PH},
                    rejected_by = {PH}
                WHERE id = {PH}
            """, (now_str, reason, session.get('full_name', 'Treasurer'), loan_id))

            try:
                db.execute(f"""
                    INSERT INTO notifications
                        (user_id, title, message, notification_type, is_read, created_at)
                    VALUES ({PH}, {PH}, {PH}, 'loan_rejection', 0, {PH})
                """, (
                    loan['applicant_id'],
                    'Loan Application Rejected',
                    f"Your loan application {loan['loan_number']} has been rejected.\n\nReason: {reason}",
                    now_str
                ))
            except Exception as e:
                print(f"⚠️ Notification insert failed: {e}")

            db.commit()

            try:
                log_action(
                    action="loan_reject",
                    target=f"loan:{loan_id}",
                    details=f"Rejected loan {loan['loan_number']} — Reason: {reason}"
                )
            except Exception:
                pass

            return jsonify({
                'success': True,
                'message': 'Loan rejected successfully. The applicant has been notified.'
            })

        # ============================================================
        # APPROVE LOAN — DEDUCT SAVINGS HERE
        # ============================================================
        if loan['status'] != 'pending':
            return jsonify({
                'success': False,
                'message': f"Loan is '{loan['status']}', not pending"
            }), 400

        loan_principal  = float(loan['amount'] or 0)
        current_savings = float(loan['current_savings'] or 0)

        # 10% savings requirement (existing rule)
        required = loan_principal * 0.10
        if current_savings < required:
            return jsonify({
                'success': False,
                'message': (
                    f"Member needs at least 10% savings "
                    f"(UGX {required:,.0f}). Current: UGX {current_savings:,.0f}"
                )
            }), 400

        # ============================================================
        # The SACCO model: the loan PRINCIPAL is deducted from savings.
        # Interest is charged on the balance and repaid separately.
        # If savings < principal → member goes into a saving debt.
        # ============================================================
        new_savings = current_savings - loan_principal

        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        balance  = float(loan['total_repayment'] or loan['amount'] or 0)

        db.execute("BEGIN TRANSACTION")

        # 1) DEDUCT principal from member's savings
        db.execute(f"""
            UPDATE users
            SET savings_balance = COALESCE(savings_balance, 0) - {PH}
            WHERE id = {PH}
        """, (loan_principal, loan['applicant_id']))

        # 2) Audit trail — negative savings_deposits row
        try:
            db.execute(f"""
                INSERT INTO savings_deposits
                    (user_id, amount, savings_type, deposit_date,
                     payment_method, receipt_number, notes)
                VALUES ({PH}, {PH}, 'LOAN_DISBURSEMENT', {PH}, 'internal', {PH}, {PH})
            """, (
                loan['applicant_id'],
                -loan_principal,
                today,
                f"DISB-{loan['loan_number']}",
                f"Loan {loan['loan_number']} approved & disbursed — deducted from savings"
            ))
        except Exception as e:
            print(f"⚠️ Could not log disbursement to savings_deposits: {e}")

        # 3) Flip the loan to approved + disbursed in one shot
        db.execute(f"""
            UPDATE loans
            SET
                status = 'disbursed',
                approved_date = {PH},
                disbursed_date = {PH},
                disbursement_date = {PH},
                disbursed_amount = {PH},
                loan_start_date = {PH},
                loan_end_date = {PH},
                due_date = {PH},
                current_balance = {PH},
                approved_by = {PH},
                approved_by_role = {PH},
                disbursed_by = {PH},
                disbursed_by_role = {PH}
            WHERE id = {PH}
        """, (
            today, today, today, loan_principal,
            today, end_date, end_date,
            balance,
            session.get('full_name', 'Treasurer'),
            session.get('role', 'treasurer'),
            session.get('full_name', 'Treasurer'),
            session.get('role', 'treasurer'),
            loan_id
        ))

        # 4) Notify the member
        try:
            db.execute(f"""
                INSERT INTO notifications
                    (user_id, title, message, notification_type, is_read, created_at)
                VALUES ({PH}, {PH}, {PH}, 'loan_disbursed', 0, {PH})
            """, (
                loan['applicant_id'],
                'Loan Approved & Disbursed',
                (
                    f"Your loan {loan['loan_number']} has been approved and disbursed. "
                    f"UGX {loan_principal:,.0f} was deducted from your savings. "
                    f"Please repay by {end_date}."
                ),
                now_str
            ))
        except Exception as e:
            print(f"⚠️ Notification insert failed: {e}")

        db.commit()

        try:
            log_action(
                action="loan_approve",
                target=f"loan:{loan_id}",
                details=(
                    f"Approved & disbursed loan {loan['loan_number']} for {loan['full_name']}. "
                    f"Deducted UGX {loan_principal:,.0f} from savings. "
                    f"New savings: UGX {new_savings:,.0f}."
                )
            )
        except Exception:
            pass

        return jsonify({
            'success': True,
            'message': (
                f"Loan approved & disbursed. "
                f"UGX {loan_principal:,.0f} deducted from savings. "
                f"New savings: UGX {new_savings:,.0f}."
            ),
            'deducted': loan_principal,
            'new_savings': new_savings
        })

    except Exception as e:
        print(f"Error processing loan action: {e}")
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass

# ============================================================
# TREASURER - DISBURSE LOAN (deducts from member's savings)
# ============================================================
@app.route("/treasurer/loan/disburse/<int:loan_id>", methods=["POST"])
def treasurer_disburse_loan(loan_id):
    print(f"💰 Disburse called for loan {loan_id}")

    if "user_id" not in session:
        return jsonify({'success': False, 'message': 'Please login first'}), 401

    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    db = get_db()
    db.row_factory = sqlite3.Row

    try:
        # ----- Load the loan + member savings -----
        loan = db.execute("""
            SELECT l.*, u.id AS member_id, u.full_name AS member_name,
                   COALESCE(u.savings_balance, 0) AS member_savings
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()

        if not loan:
            db.close()
            return jsonify({'success': False, 'message': 'Loan not found'}), 404

        if loan['status'] != 'approved':
            db.close()
            return jsonify({
                'success': False,
                'message': f'Loan must be approved first. Current status: {loan["status"]}'
            }), 400

        # ----- Determine loan amount to deduct -----
        # We deduct the PRINCIPAL (loan['amount']) from savings.
        # (total_repayment includes interest — interest is NOT taken from savings.)
        loan_principal = float(loan['amount'] or 0)
        member_savings = float(loan['member_savings'] or 0)

        if loan_principal <= 0:
            db.close()
            return jsonify({'success': False, 'message': 'Loan amount is invalid'}), 400

        # ----- Check sufficient savings -----
        if member_savings < loan_principal:
            db.close()
            return jsonify({
                'success': False,
                'message': (
                    f"Insufficient savings. {loan['member_name']} has "
                    f"UGX {member_savings:,.0f} but this loan requires "
                    f"UGX {loan_principal:,.0f}."
                )
            }), 400

        # ----- Compute dates + balance -----
        today = datetime.now().strftime('%Y-%m-%d')
        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        balance = float(loan['total_repayment'] or loan['amount'] or 0)
        new_savings = member_savings - loan_principal

        db.execute("BEGIN TRANSACTION")

        # ============================================================
        # 1) DEDUCT from member's savings (money leaves the pool)
        # ============================================================
        db.execute("""
            UPDATE users
            SET savings_balance = COALESCE(savings_balance, 0) - ?
            WHERE id = ?
        """, (loan_principal, loan['member_id']))

        # ============================================================
        # 2) Log the outflow as a savings transaction for audit trail
        #    (Negative amount = money leaving savings)
        # ============================================================
        try:
            db.execute("""
                INSERT INTO savings_deposits
                    (user_id, amount, savings_type, deposit_date,
                     payment_method, receipt_number, notes)
                VALUES (?, ?, 'LOAN_DISBURSEMENT', ?, 'internal', ?, ?)
            """, (
                loan['member_id'],
                -loan_principal,
                today,
                f"DISB-{loan['loan_number']}",
                f"Loan {loan['loan_number']} disbursed — deducted from savings"
            ))
        except Exception as e:
            # If LOAN_DISBURSEMENT isn't a recognised savings_type,
            # skip the log but keep the deduction above
            print(f"⚠️ Could not log disbursement to savings_deposits: {e}")

        # ============================================================
        # 3) Mark loan as disbursed
        # ============================================================
        db.execute("""
            UPDATE loans
            SET status = 'disbursed',
                disbursement_date = ?,
                disbursed_date = ?,
                loan_start_date = ?,
                loan_end_date = ?,
                due_date = ?,
                current_balance = ?,
                disbursed_by = ?,
                disbursed_by_role = ?
            WHERE id = ?
        """, (
            today, today, today, end_date, end_date,
            balance,
            session.get('full_name', 'Treasurer'),
            session.get('role', 'treasurer'),
            loan_id
        ))

        db.commit()
        db.close()

        return jsonify({
            'success': True,
            'message': (
                f"💰 Loan {loan['loan_number']} disbursed. "
                f"UGX {loan_principal:,.0f} deducted from savings. "
                f"New savings balance: UGX {new_savings:,.0f}."
            ),
            'deducted': loan_principal,
            'new_savings': new_savings
        })

    except Exception as e:
        db.rollback()
        db.close()
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# TREASURER - ENTER REPAYMENT
# GET  → redirect to dashboard (the repayments panel is inside it)
# POST → save the repayment, credit principal back to savings
# ============================================================
@app.route("/treasurer/repayment/enter", methods=["GET", "POST"])
def treasurer_enter_repayment():
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied. Only treasurer can enter repayments.', 'danger')
        return redirect("/login")

    # ============================================================
    # GET — the dashboard already contains the repayments panel.
    # ============================================================
    if request.method == "GET":
        return redirect(url_for('treasurer_dashboard') + '#repayments')

    # ============================================================
    # POST — process repayment
    # ============================================================
    db = get_db()
    db.row_factory = sqlite3.Row

    try:
        loan_id = int(request.form.get('loan_id') or 0)
        amount = float(request.form.get('amount', 0) or 0)
        payment_method = request.form.get('payment_method', 'cash')
        transaction_ref = request.form.get('transaction_ref', '')
        notes = request.form.get('notes', '')

        # Round to whole shillings to avoid 49999.9999 drift
        amount = int(round(amount))

        if amount <= 0:
            flash('Amount must be greater than 0', 'danger')
            return redirect(url_for('treasurer_dashboard') + '#repayments')

        # ----- Load loan + member's current savings -----
        loan = db.execute("""
            SELECT 
                l.*, 
                u.id AS member_id, 
                u.full_name, 
                u.sacco_number,
                COALESCE(u.savings_balance, 0) AS member_savings
            FROM loans l
            JOIN users u ON l.user_id = u.id
            WHERE l.id = ?
        """, (loan_id,)).fetchone()

        if not loan:
            flash('Loan not found', 'danger')
            return redirect(url_for('treasurer_dashboard') + '#repayments')

        if loan['status'] not in ['approved', 'disbursed', 'active']:
            flash(f'Cannot make payment on loan with status: {loan["status"]}', 'danger')
            return redirect(url_for('treasurer_dashboard') + '#repayments')

        # ----- Integer-safe current balance -----
        raw_balance = loan['current_balance'] if loan['current_balance'] is not None else loan['amount']
        current_balance = int(round(float(raw_balance or 0)))

        if amount > current_balance:
            flash(
                f'Payment amount (UGX {amount:,.0f}) exceeds current balance '
                f'(UGX {current_balance:,.0f})',
                'danger'
            )
            return redirect(url_for('treasurer_dashboard') + '#repayments')

        # ----- Interest-first allocation, integer-safe -----
        total_interest = int(round(float(loan['total_interest_accrued'] or 0)))
        interest_paid_so_far = int(round(float(loan['interest_paid'] or 0)))
        interest_remaining = max(0, total_interest - interest_paid_so_far)

        if amount >= interest_remaining:
            interest_paid = interest_remaining
            principal_paid = amount - interest_remaining
        else:
            interest_paid = amount
            principal_paid = 0

        new_balance = max(0, current_balance - amount)

        # ----- New member savings after principal returns -----
        member_savings_before = int(round(float(loan['member_savings'] or 0)))
        member_savings_after = member_savings_before + principal_paid

        db.execute("BEGIN TRANSACTION")

        current_datetime = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        current_date = datetime.now().strftime('%Y-%m-%d')

        # ============================================================
        # 1) RETURN THE PRINCIPAL TO MEMBER'S SAVINGS
        #    (Interest is NOT returned — it stays as SACCO income.)
        # ============================================================
        if principal_paid > 0:
            db.execute("""
                UPDATE users
                SET savings_balance = COALESCE(savings_balance, 0) + ?
                WHERE id = ?
            """, (principal_paid, loan['member_id']))

            # Audit trail — positive amount = money returning to savings
            try:
                db.execute("""
                    INSERT INTO savings_deposits
                        (user_id, amount, savings_type, deposit_date,
                         payment_method, receipt_number, notes)
                    VALUES (?, ?, 'LOAN_REPAYMENT', ?, ?, ?, ?)
                """, (
                    loan['member_id'],
                    principal_paid,
                    current_date,
                    payment_method,
                    transaction_ref or f"REPAY-{loan['loan_number']}",
                    f"Principal repayment for loan {loan['loan_number']} — credited to savings"
                ))
            except Exception as e:
                # If savings_deposits schema doesn't allow LOAN_REPAYMENT type,
                # skip the log — the savings credit above still works
                print(f"⚠️ Could not log repayment to savings_deposits: {e}")

        # ============================================================
        # 2) Record the repayment row
        # ============================================================
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

        # ============================================================
        # 3) Update the loan
        # ============================================================
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
            flash(
                f'📊 Interest paid: UGX {interest_paid:,.0f} | '
                f'Principal paid: UGX {principal_paid:,.0f}',
                'info'
            )
            if principal_paid > 0:
                flash(
                    f'💰 UGX {principal_paid:,.0f} returned to '
                    f'{loan["full_name"]}\'s savings. '
                    f'New savings: UGX {member_savings_after:,.0f}',
                    'success'
                )

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
            flash(
                f'📊 Interest paid: UGX {interest_paid:,.0f} | '
                f'Principal paid: UGX {principal_paid:,.0f}',
                'info'
            )
            if principal_paid > 0:
                flash(
                    f'💰 UGX {principal_paid:,.0f} returned to '
                    f'{loan["full_name"]}\'s savings. '
                    f'New savings: UGX {member_savings_after:,.0f}',
                    'success'
                )
            flash(f'💳 Remaining loan balance: UGX {new_balance:,.0f}', 'info')

        return redirect(url_for('treasurer_dashboard') + '#repayments')

    except Exception as e:
        db.rollback()
        db.close()
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard') + '#repayments')
    
## ============================================================
# TREASURER - ADD MEMBER (WITH SAVINGS TYPE SUPPORT) - INCLUDES STAFF
# ============================================================
@app.route("/treasurer/members/add", methods=["GET", "POST"])
def treasurer_add_members():
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied. Only Treasurer, Admin, or Secretary can register users.', 'danger')
        return redirect("/login")

    db = get_db()
    try:
        # ✅ Use fetchval — works on both SQLite and PostgreSQL
        completed_loans = fetchval(db, "SELECT COUNT(*) FROM loans WHERE status = 'completed'") or 0

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
                return render_template("treasurer/add-member.html", completed_loans=completed_loans)

            # ⚠️ Placeholder char differs per DB
            PH = "%s" if DATABASE_URL else "?"

            # Check if SACCO number exists
            existing = db.execute(
                f"SELECT id FROM users WHERE sacco_number = {PH}",
                (sacco_number,)
            ).fetchone()
            if existing:
                flash(f'SACCO number "{sacco_number}" already exists!', 'danger')
                return render_template("treasurer/add-member.html", completed_loans=completed_loans)

            if email:
                existing = db.execute(
                    f"SELECT id FROM users WHERE email = {PH}",
                    (email,)
                ).fetchone()
                if existing:
                    flash(f'Email "{email}" is already registered!', 'danger')
                    return render_template("treasurer/add-member.html", completed_loans=completed_loans)

            existing = db.execute(
                f"SELECT id FROM users WHERE phone = {PH}",
                (phone,)
            ).fetchone()
            if existing:
                flash(f'Phone number "{phone}" already registered!', 'danger')
                return render_template("treasurer/add-member.html", completed_loans=completed_loans)

            # Get settings for share prices
            settings = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
            if settings:
                settings_dict = row_to_dict(settings)
                kai_share_price = settings_dict.get('kai_share_price') or 100000
                ks_share_price = settings_dict.get('ks_share_price') or 10000
                kac_annual_fee = settings_dict.get('kac_annual_fee') or 100000
                registration_fee = settings_dict.get('registration_fee') or 20000
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

            final_savings_balance = savings_balance if savings_balance > 0 else total_savings_from_shares

            # ============================================================
            # INSERT USER — password hash is computed in Python, not in SQL
            # ============================================================
            hashed_password = generate_password_hash(password)

            if DATABASE_URL:
                # PostgreSQL — use RETURNING id
                row = db.execute("""
                    INSERT INTO users (
                        full_name, gender, dob, sacco_number,
                        email, phone, address, password, role, status,
                        savings_balance,
                        next_of_kin_name, relationship, next_of_kin_phone,
                        kai_shares, ks_shares, kac_paid, registration_fee_paid
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    full_name, gender, dob, sacco_number,
                    email, phone, address, hashed_password, role, status,
                    final_savings_balance,
                    next_of_kin_name, relationship, next_of_kin_phone,
                    kai_shares, ks_shares, kac_paid, registration_fee_paid
                )).fetchone()
                user_id = row["id"] if isinstance(row, dict) else row[0]
            else:
                # SQLite — use lastrowid
                cursor = db.cursor()
                cursor.execute("""
                    INSERT INTO users (
                        full_name, gender, dob, sacco_number,
                        email, phone, address, password, role, status,
                        savings_balance,
                        next_of_kin_name, relationship, next_of_kin_phone,
                        kai_shares, ks_shares, kac_paid, registration_fee_paid
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    full_name, gender, dob, sacco_number,
                    email, phone, address, hashed_password, role, status,
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
                db.execute(f"""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date,
                        payment_method, receipt_number, notes
                    )
                    VALUES ({PH}, {PH}, 'KAI', {PH}, {PH}, 'registration', {PH}, {PH})
                """, (
                    user_id, kai_shares * kai_share_price, kai_shares, today,
                    f'REG-KAI-{sacco_number}', f'Initial KAI shares for {full_name}'
                ))

            if ks_shares > 0:
                db.execute(f"""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date,
                        payment_method, receipt_number, notes
                    )
                    VALUES ({PH}, {PH}, 'KS', {PH}, {PH}, 'registration', {PH}, {PH})
                """, (
                    user_id, ks_shares * ks_share_price, ks_shares, today,
                    f'REG-KS-{sacco_number}', f'Initial KS shares for {full_name}'
                ))

            if kac_paid:
                db.execute(f"""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date,
                        payment_method, receipt_number, notes
                    )
                    VALUES ({PH}, {PH}, 'KAC', 1, {PH}, 'registration', {PH}, {PH})
                """, (
                    user_id, kac_annual_fee, today,
                    f'REG-KAC-{sacco_number}', f'KAC payment for {full_name}'
                ))

            if registration_fee_paid:
                db.execute(f"""
                    INSERT INTO savings_deposits (
                        user_id, amount, savings_type, shares, deposit_date,
                        payment_method, receipt_number, notes
                    )
                    VALUES ({PH}, {PH}, 'REGISTRATION', 1, {PH}, 'registration', {PH}, {PH})
                """, (
                    user_id, registration_fee, today,
                    f'REG-REG-{sacco_number}', f'Registration fee for {full_name}'
                ))

            db.commit()

            # Debug (ASCII only)
            print("=" * 60)
            print(f"USER REGISTERED: {full_name} ({role})")
            print(f"KAI: {kai_shares} shares (UGX {kai_shares * kai_share_price:,.0f})")
            print(f"KS: {ks_shares} shares (UGX {ks_shares * ks_share_price:,.0f})")
            print(f"KAC: {'Paid' if kac_paid else 'Not paid'}")
            print(f"Registration: {'Paid' if registration_fee_paid else 'Not paid'}")
            print(f"Total Savings: UGX {final_savings_balance:,.0f}")
            print("=" * 60)

            flash(f'{role.title()} "{full_name}" registered successfully with all savings types!', 'success')
            return redirect(url_for('treasurer_dashboard'))

        # GET request
        return render_template("treasurer/add-member.html", completed_loans=completed_loans)

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        flash(f'Error registering user: {str(e)}', 'danger')
        return render_template("treasurer/add-member.html", completed_loans=completed_loans)
    finally:
        try:
            db.close()
        except Exception:
            pass

# ============================================================
# TREASURER - VIEW USER DETAILS (HTML Page)
# ============================================================
@app.route("/treasurer/members/view/<int:user_id>")
def treasurer_member_details(user_id):
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    # 🔍 LOG
    log_action(
        action="view_member",
        target=f"user:{user_id}",
        details=f"Viewed member #{user_id}"
    )

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # Get ANY user (member OR staff)
        user_row = db.execute(
            f"SELECT * FROM users WHERE id = {PH}",
            (user_id,)
        ).fetchone()

        if not user_row:
            flash('User not found', 'danger')
            return redirect(url_for('treasurer_dashboard'))

        user = row_to_dict(user_row)

        loans = db.execute(
            f"SELECT * FROM loans WHERE user_id = {PH} ORDER BY application_date DESC",
            (user_id,)
        ).fetchall()

        deposits = db.execute(
            f"SELECT * FROM savings_deposits WHERE user_id = {PH} ORDER BY deposit_date DESC",
            (user_id,)
        ).fetchall()

        repayments = db.execute(f"""
            SELECT r.*, l.loan_number
            FROM repayments r
            JOIN loans l ON r.loan_id = l.id
            WHERE r.user_id = {PH}
            ORDER BY r.payment_date DESC
        """, (user_id,)).fetchall()

        completed_loans = fetchval(db,
            "SELECT COUNT(*) FROM loans WHERE status = 'completed'"
        ) or 0

        print("=" * 60)
        print(f"MEMBER DETAILS LOADED: {user.get('full_name')}")
        print(f"Loans: {len(loans)}  Deposits: {len(deposits)}  Repayments: {len(repayments)}")
        print("=" * 60)

        return render_template(
            "treasurer/member-details.html",
            member=user,
            loans=loans,
            deposits=deposits,
            repayments=repayments,
            completed_loans=completed_loans
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
        flash(f'Error loading member: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard'))
    finally:
        try:
            db.close()
        except Exception:
            pass


# ============================================================
# TREASURER - VIEW USER (JSON for Edit Modal)
# ============================================================
@app.route("/treasurer/member/view/<int:user_id>")
def treasurer_member_view_json(user_id):
    """Return complete user data as JSON for AJAX calls"""
    if session.get("role") not in ["treasurer", "admin", "secretary", "chairperson"]:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        user_row = db.execute(f"""
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
            WHERE id = {PH}
        """, (user_id,)).fetchone()

        if not user_row:
            return jsonify({'success': False, 'message': 'User not found'}), 404

        return jsonify({
            'success': True,
            'member': row_to_dict(user_row)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass00


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
        print(f"ðŸ‘¤ USER UPDATED: {user['full_name']} ({user['role']})")
        print(f"ðŸ“Š KAI: +{diff_kai_shares} shares (UGX {diff_kai_shares * kai_share_price:,.0f})")
        print(f"ðŸ“Š KS: +{diff_ks_shares} shares (UGX {diff_ks_shares * ks_share_price:,.0f})")
        print(f"ðŸ“Š KAC: {'Added' if diff_kac > 0 else 'No change'}")
        print(f"ðŸ“Š Registration: {'Added' if diff_reg > 0 else 'No change'}")
        print(f"ðŸ’° Total Savings: UGX {total_savings:,.0f}")
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
    try:
        # ⚠️ Placeholder char differs per DB
        PH = "%s" if DATABASE_URL else "?"

        member = db.execute(
            f"SELECT * FROM users WHERE id = {PH}",
            (user_id,)
        ).fetchone()

        if not member:
            flash("Member not found", "danger")
            return redirect(url_for("login"))

        member_dict = row_to_dict(member)

        # ============================================================
        # TOTAL SAVINGS — KAI + KS only (exclude KAC & Registration)
        # ============================================================
        total_savings = fetchval(db, f"""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM savings_deposits
            WHERE user_id = {PH}
              AND savings_type IN ('KAI', 'KS')
        """, (user_id,)) or 0

        loans_data = db.execute(f"""
            SELECT
                l.*,
                COALESCE((
                    SELECT SUM(amount)
                    FROM repayments
                    WHERE loan_id = l.id
                    AND status = 'completed'
                ), 0) as total_paid
            FROM loans l
            WHERE l.user_id = {PH}
            ORDER BY l.application_date DESC
        """, (user_id,)).fetchall()

        loans = []
        active_loans_count = 0
        active_loans_balance = 0
        total_loans_taken = 0

        for loan_row in loans_data:
            loan = row_to_dict(loan_row)
            total_loans_taken += float(loan.get('amount', 0) or 0)
            loan_total = float(loan.get('total_repayment') or loan.get('amount', 0) or 0)
            total_paid = float(loan.get('total_paid', 0) or 0)
            remaining_balance = max(0, loan_total - total_paid)
            loan['remaining_balance'] = remaining_balance

            if loan.get('status') in ['approved', 'disbursed', 'active']:
                active_loans_count += 1
                active_loans_balance += remaining_balance

            loans.append(loan)

        savings_deposits = db.execute(f"""
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
            WHERE user_id = {PH}
            ORDER BY deposit_date DESC
        """, (user_id,)).fetchall()

        repayments = db.execute(f"""
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
            WHERE l.user_id = {PH}
            ORDER BY r.payment_date DESC
        """, (user_id,)).fetchall()

        guarantors = db.execute(f"""
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
            WHERE l.user_id = {PH}
            ORDER BY lg.id DESC
        """, (user_id,)).fetchall()

        # ✅ Removed `notification_type` — it doesn't exist
        notifications = db.execute(f"""
            SELECT
                id,
                user_id,
                title,
                message,
                type,
                link,
                is_read,
                created_at
            FROM notifications
            WHERE user_id = {PH}
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()

        unread_notifications_count = fetchval(db, f"""
            SELECT COUNT(*) AS count
            FROM notifications
            WHERE user_id = {PH}
            AND is_read = 0
        """, (user_id,)) or 0

        # ============================================================
        # SAVINGS BY TYPE (KAI, KS, KAC, Registration)
        # ============================================================
        settings_row = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings_row:
            settings_dict = row_to_dict(settings_row)
            kai_share_price  = settings_dict.get('kai_share_price')  or 100000
            ks_share_price   = settings_dict.get('ks_share_price')   or 10000
            kac_annual_fee   = settings_dict.get('kac_annual_fee')   or 100000
            registration_fee = settings_dict.get('registration_fee') or 20000
        else:
            kai_share_price  = 100000
            ks_share_price   = 10000
            kac_annual_fee   = 100000
            registration_fee = 20000

        kai_shares   = member_dict.get('kai_shares') or 0
        ks_shares    = member_dict.get('ks_shares') or 0
        reg_fee_paid = member_dict.get('registration_fee_paid') or 0

        # ============================================================
        # KAC — INSTALLMENT-AWARE
        # ============================================================
        kac_paid_raw = member_dict.get('kac_paid', 0)

        if kac_paid_raw is None:
            kac_paid = 0.0
        elif isinstance(kac_paid_raw, bool):
            kac_paid = float(kac_annual_fee) if kac_paid_raw else 0.0
        else:
            try:
                kac_paid = float(kac_paid_raw)
            except (TypeError, ValueError):
                kac_paid = 0.0

        if kac_paid > kac_annual_fee:
            kac_paid = float(kac_annual_fee)

        kac_amount = kac_paid
        kac_fully_paid = (kac_paid >= kac_annual_fee)

        kai_amount = kai_shares * kai_share_price
        ks_amount  = ks_shares  * ks_share_price
        reg_amount = registration_fee if reg_fee_paid else 0

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
            member=member_dict,
            user=member_dict,
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
            kac_paid=kac_paid,
            kac_fully_paid=kac_fully_paid,
            reg_fee_paid=reg_fee_paid,
            kai_amount=kai_amount,
            ks_amount=ks_amount,
            kac_amount=kac_amount,
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
        flash(f"Error loading dashboard: {str(e)}", "danger")
        return redirect(url_for("login"))
    finally:
        try:
            db.close()
        except Exception:
            pass

# ============================================================
# MEMBER - APPLY LOAN
# ============================================================
@app.route("/member/apply-loan", methods=["GET", "POST"])
def member_apply_loan():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    PH = "%s" if DATABASE_URL else "?"
    db = get_db()

    if request.method == "GET":
        try:
            member = db.execute(
                f"SELECT * FROM users WHERE id = {PH}",
                (user_id,)
            ).fetchone()

            total_savings = fetchval(db, f"""
                SELECT COALESCE(SUM(amount), 0) as total
                FROM savings_deposits
                WHERE user_id = {PH}
            """, (user_id,)) or 0

            member_dict = row_to_dict(member)

            from datetime import datetime, timedelta
            now = datetime.now()
            current_year = now.year
            current_date = now.strftime('%d %B %Y')
            due_date = now + timedelta(days=30)
            due_date_formatted = due_date.strftime('%d %B %Y')
            due_date_iso = due_date.strftime('%Y-%m-%d')

            return render_template(
                "member/apply-loan.html",
                member=member_dict,
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
        finally:
            try:
                db.close()
            except Exception:
                pass

    # ============ POST - Submit loan application ============
    try:
        if request.is_json:
            data = request.get_json()
            loan_amount = float(data.get('loan_amount'))
            purpose = data.get('purpose')
            repayment_plan = data.get('repayment_plan', 'monthly')

            # ===== Send-to =====
            send_to_type      = (data.get('send_to_type') or 'phone').strip().lower()
            send_to_value     = (data.get('send_to_value') or '').strip()
            send_to_secondary = (data.get('send_to_secondary') or '').strip()

            # ===== Guarantors =====
            g1_id = data.get('guarantor1_id')
            g1_name = (data.get('guarantor1_name') or '').strip()
            g1_phone = (data.get('guarantor1_phone') or '').strip()
            g1_email = (data.get('guarantor1_email') or '').strip()
            g1_relationship = data.get('guarantor1_relationship', '')

            g2_id = data.get('guarantor2_id')
            g2_name = (data.get('guarantor2_name') or '').strip()
            g2_phone = (data.get('guarantor2_phone') or '').strip()
            g2_email = (data.get('guarantor2_email') or '').strip()
            g2_relationship = data.get('guarantor2_relationship', '')
        else:
            loan_amount = float(request.form.get('loan_amount'))
            purpose = request.form.get('purpose')
            repayment_plan = request.form.get('repayment_plan', 'monthly')

            # ===== Send-to =====
            send_to_type      = (request.form.get('send_to_type') or 'phone').strip().lower()
            send_to_value     = (request.form.get('send_to_value') or '').strip()
            send_to_secondary = (request.form.get('send_to_secondary') or '').strip()

            # ===== Guarantors =====
            g1_id = request.form.get('guarantor1_id')
            g1_name = (request.form.get('guarantor1_name') or '').strip()
            g1_phone = (request.form.get('guarantor1_phone') or '').strip()
            g1_email = (request.form.get('guarantor1_email') or '').strip()
            g1_relationship = request.form.get('guarantor1_relationship', '')

            g2_id = request.form.get('guarantor2_id')
            g2_name = (request.form.get('guarantor2_name') or '').strip()
            g2_phone = (request.form.get('guarantor2_phone') or '').strip()
            g2_email = (request.form.get('guarantor2_email') or '').strip()
            g2_relationship = request.form.get('guarantor2_relationship', '')

        # ---- Normalise send-to type ----
        if send_to_type not in ('phone', 'account', 'both'):
            send_to_type = 'phone'

        # ---- Validate payout destination ----
        if not send_to_value:
            if request.is_json:
                return jsonify({'success': False, 'message': 'Please enter where the money should be sent.'}), 400
            flash('Please enter where the money should be sent.', 'danger')
            return redirect(url_for('member_apply_loan'))

        if send_to_type == 'both' and not send_to_secondary:
            if request.is_json:
                return jsonify({'success': False, 'message': 'Please enter the account number too.'}), 400
            flash('Please enter the account number too.', 'danger')
            return redirect(url_for('member_apply_loan'))

        if send_to_type != 'both':
            send_to_secondary = None

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

        total_savings = fetchval(db, f"""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM savings_deposits
            WHERE user_id = {PH}
        """, (user_id,)) or 0

        savings_threshold = total_savings * 0.95
        guarantors_required = loan_amount > savings_threshold

        # ============================================================
        # GUARANTOR VALIDATION — only when required
        # ============================================================
        if guarantors_required:
            # ---------- 1. Presence check ----------
            if not g1_name or not g1_phone:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Guarantor 1 details are required for this loan amount.'}), 400
                flash('Guarantor 1 details are required for this loan amount.', 'danger')
                return redirect(url_for('member_apply_loan'))

            if not g2_name or not g2_phone:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Guarantor 2 details are required for this loan amount.'}), 400
                flash('Guarantor 2 details are required for this loan amount.', 'danger')
                return redirect(url_for('member_apply_loan'))

            # ---------- 2. Must have been selected from search ----------
            if not g1_id or not g2_id:
                msg = 'Guarantors must be selected from the registered member list.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            # ---------- 3. Convert to int ----------
            try:
                g1_id = int(g1_id)
                g2_id = int(g2_id)
            except (TypeError, ValueError):
                msg = 'Invalid guarantor selection.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            # ---------- 4. Same person / self check ----------
            if g1_id == g2_id:
                msg = 'Guarantor 1 and Guarantor 2 cannot be the same member.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            if g1_id == user_id or g2_id == user_id:
                msg = 'You cannot select yourself as a guarantor.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            # ---------- 5. Verify both exist and are active ----------
            allowed_roles = ('member', 'admin', 'chairperson', 'treasurer', 'secretary', 'publicity')

            g1_row = db.execute(
                f"SELECT id, full_name, phone, email, role FROM users WHERE id = {PH} AND status = 'active'",
                (g1_id,)
            ).fetchone()

            g2_row = db.execute(
                f"SELECT id, full_name, phone, email, role FROM users WHERE id = {PH} AND status = 'active'",
                (g2_id,)
            ).fetchone()

            if not g1_row:
                msg = 'Guarantor 1 is not a registered, active member.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            if not g2_row:
                msg = 'Guarantor 2 is not a registered, active member.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            g1_dict = row_to_dict(g1_row)
            g2_dict = row_to_dict(g2_row)

            if (g1_dict.get('role') or 'member').lower() not in allowed_roles:
                msg = 'Guarantor 1 must be an active SACCO member or staff.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            if (g2_dict.get('role') or 'member').lower() not in allowed_roles:
                msg = 'Guarantor 2 must be an active SACCO member or staff.'
                if request.is_json:
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('member_apply_loan'))

            # ---------- 6. Use canonical DB values ----------
            g1_name  = g1_dict.get('full_name') or g1_name
            g1_phone = g1_dict.get('phone') or g1_phone
            g1_email = g1_dict.get('email') or g1_email

            g2_name  = g2_dict.get('full_name') or g2_name
            g2_phone = g2_dict.get('phone') or g2_phone
            g2_email = g2_dict.get('email') or g2_email

        # ============================================================
        # INSERT LOAN — send-to fields included for both drivers
        # ============================================================
        if DATABASE_URL:
            row = db.execute(f"""
                INSERT INTO loans (
                    loan_number, user_id, amount, interest_rate, interest_amount,
                    total_repayment, monthly_installment, tenure, purpose,
                    repayment_plan, status, application_date,
                    current_balance, last_interest_date,
                    start_month, end_month, total_interest_accrued,
                    principal_paid, interest_paid, months_paid,
                    original_balance, total_interest_calculated, due_date,
                    loan_start_date, loan_end_date,
                    send_to_type, send_to_value, send_to_secondary
                ) VALUES (
                    {PH}, {PH}, {PH}, {PH}, {PH},
                    {PH}, {PH}, {PH}, {PH},
                    {PH}, {PH}, {PH},
                    {PH}, {PH},
                    {PH}, {PH}, {PH},
                    {PH}, {PH}, {PH},
                    {PH}, {PH}, {PH},
                    {PH}, {PH},
                    {PH}, {PH}, {PH}
                )
                RETURNING id
            """, (
                loan_ref, user_id, loan_amount, monthly_rate_percent, interest_amount,
                total_repayment, interest_amount, 1, purpose,
                repayment_plan, 'pending', application_date.strftime('%Y-%m-%d'),
                total_repayment, application_date.strftime('%Y-%m-%d'),
                application_date.month, due_date.month, interest_amount,
                0, 0, 0,
                loan_amount, interest_amount, due_date_str,
                application_date.strftime('%Y-%m-%d'), due_date_str,
                send_to_type, send_to_value, send_to_secondary
            )).fetchone()
            loan_id = row["id"] if isinstance(row, dict) else row[0]
        else:
            cursor = db.cursor()
            cursor.execute("""
                INSERT INTO loans (
                    loan_number, user_id, amount, interest_rate, interest_amount,
                    total_repayment, monthly_installment, tenure, purpose,
                    repayment_plan, status, application_date,
                    current_balance, last_interest_date,
                    start_month, end_month, total_interest_accrued,
                    principal_paid, interest_paid, months_paid,
                    original_balance, total_interest_calculated, due_date,
                    loan_start_date, loan_end_date,
                    send_to_type, send_to_value, send_to_secondary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                loan_ref, user_id, loan_amount, monthly_rate_percent, interest_amount,
                total_repayment, interest_amount, 1, purpose,
                repayment_plan, 'pending', application_date.strftime('%Y-%m-%d'),
                total_repayment, application_date.strftime('%Y-%m-%d'),
                application_date.month, due_date.month, interest_amount,
                0, 0, 0,
                loan_amount, interest_amount, due_date_str,
                application_date.strftime('%Y-%m-%d'), due_date_str,
                send_to_type, send_to_value, send_to_secondary
            ))
            loan_id = cursor.lastrowid

        # ============================================================
        # Insert guarantors
        # ============================================================
        if guarantors_required:
            db.execute(f"""
                INSERT INTO loan_guarantors (loan_id, guarantor_name, phone, email, relationship, status)
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})
            """, (loan_id, g1_name, g1_phone, g1_email, g1_relationship, 'active'))

            db.execute(f"""
                INSERT INTO loan_guarantors (loan_id, guarantor_name, phone, email, relationship, status)
                VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})
            """, (loan_id, g2_name, g2_phone, g2_email, g2_relationship, 'active'))
        else:
            db.execute(f"""
                INSERT INTO loan_guarantors (loan_id, guarantor_name, phone, email, relationship, status)
                VALUES ({PH}, 'No Guarantor Required', 'N/A', 'N/A', 'N/A', 'accepted')
            """, (loan_id,))
            db.execute(f"""
                INSERT INTO loan_guarantors (loan_id, guarantor_name, phone, email, relationship, status)
                VALUES ({PH}, 'No Guarantor Required', 'N/A', 'N/A', 'N/A', 'accepted')
            """, (loan_id,))

        db.commit()

        success_message = 'Loan application submitted successfully!'
        if guarantors_required:
            success_message += ' Guarantors will be contacted manually by the SACCO team.'
        else:
            success_message += ' No guarantors required based on your savings.'

        # ---- Human-readable send-to display ----
        if send_to_type == 'phone':
            send_to_display = 'Phone: ' + send_to_value
        elif send_to_type == 'account':
            send_to_display = 'Account: ' + send_to_value
        else:
            send_to_display = 'Phone: ' + send_to_value + ' | Account: ' + (send_to_secondary or '')

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
                'loan_end_date': due_date_str,
                'send_to_type': send_to_type,
                'send_to_value': send_to_value,
                'send_to_secondary': send_to_secondary or '',
                'send_to_display': send_to_display,
            })

        flash(success_message, 'success')
        return redirect(url_for('treasurer_dashboard') + '#loan_applications')

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
        if request.is_json:
            return jsonify({'success': False, 'message': str(e)}), 500
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('member_apply_loan'))
    finally:
        try:
            db.close()
        except Exception:
            pass

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
# ============================================================
# MEMBER SEARCH FOR GUARANTORS
# ------------------------------------------------------------
# Returns: id, full_name, phone, status, type
# (sacco_number, email, account number NOT returned)
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
    PH = "%s" if DATABASE_URL else "?"

    try:
        search_pattern = f'%{search_term}%'

        users = db.execute(f"""
            SELECT
                id,
                full_name,
                phone,
                status,
                role
            FROM users
            WHERE LOWER(role) IN ('member', 'admin', 'secretary', 'treasurer', 'publicity')
              AND id != {PH}
              AND status = 'active'
              AND (
                  LOWER(full_name) LIKE LOWER({PH}) OR
                  sacco_number LIKE {PH} OR
                  phone LIKE {PH} OR
                  email LIKE {PH}
              )
            ORDER BY
                CASE LOWER(role)
                    WHEN 'member'    THEN 1
                    WHEN 'admin'     THEN 2
                    WHEN 'treasurer' THEN 3
                    WHEN 'secretary' THEN 4
                    WHEN 'publicity' THEN 5
                    ELSE 6
                END,
                full_name ASC
            LIMIT 20
        """, (
            current_user_id,
            search_pattern,
            search_pattern,
            search_pattern,
            search_pattern
        )).fetchall()

        user_list = []
        for user in users:
            user_dict = row_to_dict(user)
            role = (user_dict.get('role') or 'member').lower()

            if role == 'member':
                loan_count = fetchval(db, f"""
                    SELECT COUNT(*)
                    FROM loans
                    WHERE user_id = {PH}
                      AND status IN ('approved', 'disbursed', 'active')
                """, (user_dict['id'],)) or 0
            else:
                loan_count = 0

            # ---- Return ONLY name + phone + status info ----
            user_list.append({
                'id': user_dict['id'],
                'full_name': user_dict['full_name'],
                'phone': user_dict.get('phone') or '',
                'active_loans': loan_count,
                'status': user_dict.get('status') or 'active',
                'type': role,
            })

        return jsonify({
            'success': True,
            'members': user_list,
            'count': len(user_list)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error searching members: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
    finally:
        try:
            db.close()
        except Exception:
            pass

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
        print("ðŸ“Š SAVINGS REPORTS - INCLUDING STAFF")
        print(f"ðŸ“Š Total Users: {total_members}")
        print(f"ðŸ“Š Regular Members: {total_regular_members}")
        print(f"ðŸ“Š Staff Members: {total_staff_members}")
        print(f"ðŸ“Š KAI Members: {kai_members}")
        print(f"ðŸ“Š KS Members: {ks_members}")
        print(f"ðŸ“Š KAC Members: {kac_members}")
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
        print(f"âŒ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('treasurer_dashboard'))

# ============================================================
# ADMIN — MANAGE USERS (list page)
# ============================================================
@app.route("/admin/users/manage")
def admin_manage_users():
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied. Only Admin or Chairperson can manage users.', 'danger')
        return redirect("/login")

    db = get_db()
    try:
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
            'treasurer': fetchval(db, "SELECT COUNT(*) FROM users WHERE LOWER(role) = 'treasurer'") or 0,
            'secretary': fetchval(db, "SELECT COUNT(*) FROM users WHERE LOWER(role) = 'secretary'") or 0,
            'publicity': fetchval(db, "SELECT COUNT(*) FROM users WHERE LOWER(role) = 'publicity'") or 0,
            'admin':     fetchval(db, "SELECT COUNT(*) FROM users WHERE LOWER(role) IN ('admin', 'chairperson')") or 0,
        }

        return render_template(
            "admin/manage-users.html",
            staff_users=staff_users,
            staff_counts=staff_counts,
            now=datetime.now()
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        flash(f'Error loading users: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    finally:
        try: db.close()
        except Exception: pass


# ============================================================
# ADMIN — REGISTER USER
# ============================================================
@app.route("/admin/users/register", methods=["GET", "POST"])
def admin_register_user():
    if session.get("role") not in ["admin", "chairperson"]:
        flash("Access denied. Only Admin or Chairperson can register users.", "danger")
        return redirect(url_for("login"))

    # ---- GET ----
    if request.method == "GET":
        return render_template("admin/register-user.html")

    # ---- POST ----
    full_name = request.form.get("full_name", "").strip()
    email     = request.form.get("email", "").strip()
    phone     = request.form.get("phone", "").strip()
    role      = request.form.get("role", "").strip()
    password  = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not full_name or not email or not role or not password:
        flash("All fields are required.", "danger")
        return render_template("admin/register-user.html")

    if password != confirm_password:
        flash("Passwords do not match.", "danger")
        return render_template("admin/register-user.html")

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return render_template("admin/register-user.html")

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()

    try:
        # Duplicate email check
        existing = db.execute(
            f"SELECT id FROM users WHERE email = {PH}", (email,)
        ).fetchone()
        if existing:
            flash("Email already registered.", "danger")
            return render_template("admin/register-user.html")

        # Generate SACCO number
        sacco_number = (
            f"STAFF-{datetime.now().strftime('%Y%m')}-"
            f"{role[:3].upper()}"
            f"{int(datetime.now().timestamp()) % 1000}"
        )

        # Hash BEFORE SQL
        hashed_password = generate_password_hash(password)

        db.execute(f"""
            INSERT INTO users (
                full_name, email, phone, sacco_number,
                password, role, status, registration_date
            )
            VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, 'active', {PH})
        """, (
            full_name, email, phone, sacco_number,
            hashed_password, role,
            datetime.now().strftime("%Y-%m-%d")
        ))

        db.commit()

        flash(
            f"User {full_name} registered successfully as {role}! "
            f"SACCO Number: {sacco_number}",
            "success"
        )
        return redirect(url_for("admin_manage_users"))

    except Exception as e:
        import traceback
        traceback.print_exc()
        try: db.rollback()
        except Exception: pass
        flash("An error occurred while registering the user.", "danger")
        return render_template("admin/register-user.html")
    finally:
        try: db.close()
        except Exception: pass


# ============================================================
# ADMIN — VIEW USER (JSON for edit modal)
# ============================================================
@app.route("/admin/users/view/<int:user_id>")
def admin_view_user_json(user_id):
    if session.get("role") not in ("admin", "chairperson", "treasurer", "secretary"):
        return jsonify({"success": False, "message": "Access denied"}), 403

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()
    try:
        row = db.execute(f"""
            SELECT id, full_name, email, phone, role, status, sacco_number
            FROM users WHERE id = {PH}
        """, (user_id,)).fetchone()

        if not row:
            return jsonify({"success": False, "message": "User not found"}), 404

        return jsonify({"success": True, "user": row_to_dict(row)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try: db.close()
        except Exception: pass


# ============================================================
# ADMIN — UPDATE USER (JSON — used by edit modal)
# ============================================================
@app.route("/admin/users/update/<int:user_id>", methods=["POST"])
def admin_update_user(user_id):
    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Access denied"}), 403

    data = request.get_json() or {}
    full_name = (data.get("full_name") or "").strip()
    email     = (data.get("email") or "").strip()
    phone     = (data.get("phone") or "").strip()
    role      = (data.get("role") or "").strip()
    status    = (data.get("status") or "active").strip()

    if not full_name or not email:
        return jsonify({"success": False, "message": "Full name and email are required"}), 400

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()
    try:
        user_row = db.execute(
            f"SELECT id FROM users WHERE id = {PH}", (user_id,)
        ).fetchone()
        if not user_row:
            return jsonify({"success": False, "message": "User not found"}), 404

        existing = db.execute(
            f"SELECT id FROM users WHERE email = {PH} AND id != {PH}",
            (email, user_id)
        ).fetchone()
        if existing:
            return jsonify({"success": False, "message": "Email already in use by another user"}), 400

        db.execute(f"""
            UPDATE users
               SET full_name = {PH},
                   email = {PH},
                   phone = {PH},
                   role = {PH},
                   status = {PH}
             WHERE id = {PH}
        """, (full_name, email, phone, role, status, user_id))

        db.commit()
        print(f"✅ Updated user {user_id}: {full_name} ({role})")
        return jsonify({"success": True, "message": "User updated successfully"})

    except Exception as e:
        import traceback
        traceback.print_exc()
        try: db.rollback()
        except Exception: pass
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try: db.close()
        except Exception: pass


# ============================================================
# ADMIN — RESET USER PASSWORD
# ============================================================
@app.route("/admin/users/reset-password/<int:user_id>", methods=["POST"])
def admin_reset_user_password(user_id):
    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Access denied"}), 403

    data = request.get_json() or {}
    new_password = (data.get("new_password") or data.get("password") or "").strip()

    if not new_password or len(new_password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters"}), 400

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()
    try:
        user_row = db.execute(
            f"SELECT id, full_name FROM users WHERE id = {PH}", (user_id,)
        ).fetchone()
        if not user_row:
            return jsonify({"success": False, "message": "User not found"}), 404

        hashed = generate_password_hash(new_password)
        db.execute(
            f"UPDATE users SET password = {PH} WHERE id = {PH}",
            (hashed, user_id)
        )
        db.commit()

        user_dict = row_to_dict(user_row)
        print(f"✅ Reset password for user {user_id}: {user_dict.get('full_name')}")
        return jsonify({"success": True, "message": "Password reset successfully"})

    except Exception as e:
        import traceback
        traceback.print_exc()
        try: db.rollback()
        except Exception: pass
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try: db.close()
        except Exception: pass


# ============================================================
# ADMIN — DELETE USER (FK-safe)
# ============================================================
@app.route("/admin/users/delete/<int:user_id>", methods=["POST", "DELETE"])
def admin_delete_user(user_id):
    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Access denied"}), 403

    # Prevent self-delete
    if session.get("user_id") == user_id:
        return jsonify({"success": False, "message": "You cannot delete your own account"}), 400

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()
    try:
        user_row = db.execute(
            f"SELECT id, full_name, role FROM users WHERE id = {PH}", (user_id,)
        ).fetchone()
        if not user_row:
            return jsonify({"success": False, "message": "User not found"}), 404

        user_dict = row_to_dict(user_row)
        name = user_dict.get("full_name", f"User {user_id}")
        role = user_dict.get("role", "")

        # Prevent deleting the last admin
        if role == "admin":
            admin_count = fetchval(db, "SELECT COUNT(*) FROM users WHERE role = 'admin'") or 0
            if admin_count <= 1:
                return jsonify({"success": False, "message": "Cannot delete the last admin user"}), 400

        # FK-safe order: children before parents
        cleanup = [
            ("chat_messages", "sender_id"),
            ("chat_messages", "receiver_id"),
            ("notifications", "user_id"),
            ("repayments", "user_id"),
            ("savings_deposits", "user_id"),
        ]
        for tbl, col in cleanup:
            try:
                db.execute(f"DELETE FROM {tbl} WHERE {col} = {PH}", (user_id,))
            except Exception as e:
                print(f"⚠️ {tbl}.{col} cleanup: {e}")
                try: db.rollback()
                except Exception: pass

        # Loan-related cleanup
        try:
            db.execute(f"""
                DELETE FROM loan_guarantors
                WHERE loan_id IN (SELECT id FROM loans WHERE user_id = {PH})
            """, (user_id,))
        except Exception as e:
            print(f"⚠️ loan_guarantors cleanup: {e}")
            try: db.rollback()
            except Exception: pass

        try:
            db.execute(f"DELETE FROM loans WHERE user_id = {PH}", (user_id,))
        except Exception as e:
            print(f"⚠️ loans cleanup: {e}")
            try: db.rollback()
            except Exception: pass

        # Delete the user
        db.execute(f"DELETE FROM users WHERE id = {PH}", (user_id,))
        db.commit()

        print(f"✅ Deleted user {user_id}: {name}")
        return jsonify({"success": True, "message": f'User "{name}" deleted successfully'})

    except Exception as e:
        import traceback
        traceback.print_exc()
        try: db.rollback()
        except Exception: pass
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try: db.close()
        except Exception: pass


# ============================================================
# ADMIN — EDIT USER (HTML form — legacy)
# ============================================================
@app.route("/admin/users/edit/<int:user_id>", methods=["GET", "POST"])
def admin_edit_user(user_id):
    if session.get("role") not in ["admin", "chairperson"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    PH = "%s" if DATABASE_URL else "?"
    db = get_db()
    try:
        user_row = db.execute(
            f"SELECT * FROM users WHERE id = {PH}", (user_id,)
        ).fetchone()

        if not user_row:
            flash('User not found', 'danger')
            return redirect(url_for('admin_manage_users'))

        user = row_to_dict(user_row)

        if request.method == "POST":
            full_name = request.form.get('full_name')
            email = request.form.get('email')
            phone = request.form.get('phone')
            role = request.form.get('role')
            status = request.form.get('status')

            # Check duplicate email
            if email:
                existing = db.execute(f"""
                    SELECT id FROM users WHERE email = {PH} AND id != {PH}
                """, (email, user_id)).fetchone()
                if existing:
                    flash('Email already in use by another user', 'danger')
                    return render_template("admin/edit-user.html", user=user)

            db.execute(f"""
                UPDATE users
                   SET full_name = {PH}, email = {PH}, phone = {PH},
                       role = {PH}, status = {PH}
                 WHERE id = {PH}
            """, (full_name, email, phone, role, status, user_id))

            db.commit()
            flash('User updated successfully!', 'success')
            return redirect(url_for('admin_manage_users'))

        return render_template("admin/edit-user.html", user=user, now=datetime.now())

    except Exception as e:
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('admin_manage_users'))
    finally:
        try: db.close()
        except Exception: pass

@app.route("/admin/settings/get")
def get_settings():
    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        conn.close()

        if row:
            return jsonify({"success": True, "settings": dict(row)})

        # Fallback defaults
        return jsonify({
            "success": True,
            "settings": {
                "sacco_name": "Karacel Association",
                "registration_number": "SACCO/REG/2024/001",
                "savings_interest_rate": 6.5,
                "loan_interest_rate": 12,
                "penalty_rate": 5,
                "max_loan_amount": "10,000,000",
                "min_loan_amount": "10,000",
                "max_tenure": 24,
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/admin/settings/update", methods=["POST"])
def update_settings():
    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        data = request.get_json(silent=True) or {}

        # Coerce types safely
        def as_float(key, default=0.0):
            try: return float(data.get(key, default))
            except (TypeError, ValueError): return default

        def as_int(key, default=0):
            try: return int(data.get(key, default))
            except (TypeError, ValueError): return default

        def as_text(key, default=""):
            v = data.get(key, default)
            return str(v).strip() if v is not None else default

        payload = {
            "sacco_name":            as_text("sacco_name", "Karacel Association"),
            "registration_number":   as_text("registration_number"),
            "savings_interest_rate": as_float("savings_interest_rate", 6.5),
            "loan_interest_rate":    as_float("loan_interest_rate", 12.0),
            "penalty_rate":          as_float("penalty_rate", 5.0),
            "max_loan_amount":       as_text("max_loan_amount", "10000000"),
            "min_loan_amount":       as_text("min_loan_amount", "10000"),
            "max_tenure":            as_int("max_tenure", 24),
        }

        conn = get_db()
        conn.row_factory = sqlite3.Row

        # Check if a row exists
        row = conn.execute("SELECT id FROM system_settings LIMIT 1").fetchone()

        if row:
            conn.execute("""
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
                payload["sacco_name"],
                payload["registration_number"],
                payload["savings_interest_rate"],
                payload["loan_interest_rate"],
                payload["penalty_rate"],
                payload["max_loan_amount"],
                payload["min_loan_amount"],
                payload["max_tenure"],
                row["id"]
            ))
        else:
            conn.execute("""
                INSERT INTO system_settings (
                    sacco_name, registration_number,
                    savings_interest_rate, loan_interest_rate, penalty_rate,
                    max_loan_amount, min_loan_amount, max_tenure
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                payload["sacco_name"],
                payload["registration_number"],
                payload["savings_interest_rate"],
                payload["loan_interest_rate"],
                payload["penalty_rate"],
                payload["max_loan_amount"],
                payload["min_loan_amount"],
                payload["max_tenure"]
            ))

        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "message": "Settings saved successfully.",
            "settings": payload
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error: {str(e)}"}), 500

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
from datetime import datetime, timedelta

def resolve_range():
    """Return (start_date, end_date) as 'YYYY-MM-DD' strings based on query params."""
    range_key = request.args.get('range', 'month')
    start = request.args.get('start')
    end = request.args.get('end')
    today = datetime.now().date()

    if range_key == 'today':
        return today.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    if range_key == 'week':
        return (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    if range_key == 'month':
        return today.replace(day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    if range_key == 'quarter':
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    if range_key == 'year':
        return today.replace(month=1, day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    if range_key == 'custom' and start and end:
        return start, end
    return today.replace(day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')


def _to_int(v):
    """Coerce any value to a clean integer."""
    try:
        return int(round(float(v or 0)))
    except (ValueError, TypeError):
        return 0


@app.route("/admin/reports/generate/<string:report_type>")
def generate_report(report_type):
    if session.get("role") not in ["admin", "chairperson", "treasurer", "secretary"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    start, end = resolve_range()
    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # ============================================================
        # LOAD SETTINGS
        # ============================================================
        settings_row = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings_row:
            s = row_to_dict(settings_row)
            kai_share_price  = int(s.get('kai_share_price', 100000) or 100000)
            ks_share_price   = int(s.get('ks_share_price', 10000) or 10000)
            kac_annual_fee   = int(s.get('kac_annual_fee', 100000) or 100000)
            registration_fee = int(s.get('registration_fee', 20000) or 20000)
        else:
            kai_share_price = 100000
            ks_share_price = 10000
            kac_annual_fee = 100000
            registration_fee = 20000

        # ============================================================
        # MEMBERS REPORT
        # ============================================================
        if report_type == 'members':
            members_raw = db.execute("""
                SELECT 
                    id, full_name, sacco_number, phone, email, role, status,
                    COALESCE(savings_balance, 0) AS savings_balance,
                    COALESCE(kai_shares, 0) AS kai_shares,
                    COALESCE(ks_shares, 0) AS ks_shares,
                    COALESCE(kac_paid, 0) AS kac_paid,
                    COALESCE(registration_fee_paid, 0) AS registration_fee_paid,
                    registration_date, gender, dob, address,
                    next_of_kin_name, next_of_kin_phone, relationship
                FROM users 
                WHERE LOWER(role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
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

            members_list = []
            member_count = staff_count = active_count = inactive_count = 0
            members_with_loans = 0
            total_savings = 0
            kai_total = ks_total = 0

            for m in members_raw:
                md = row_to_dict(m)

                # KAC boolean → numeric coercion
                kac_val = md.get('kac_paid') or 0
                try:
                    kac_val = float(kac_val)
                except (ValueError, TypeError):
                    kac_val = 0
                if kac_val == 1:
                    kac_val = kac_annual_fee
                md['kac_paid'] = min(int(kac_val), kac_annual_fee)

                # Active loans count — uses fetchval
                md['active_loans'] = fetchval(db, f"""
                    SELECT COUNT(*) FROM loans 
                    WHERE user_id = {PH} AND status IN ('approved','disbursed','active')
                """, (md['id'],)) or 0

                # Counters
                role = (md.get('role') or 'member').lower()
                status = (md.get('status') or 'active').lower()
                if role == 'member':
                    member_count += 1
                else:
                    staff_count += 1
                if status == 'active':
                    active_count += 1
                else:
                    inactive_count += 1
                if md['active_loans'] > 0:
                    members_with_loans += 1

                total_savings += _to_int(md.get('savings_balance'))
                kai_total += int(md.get('kai_shares') or 0)
                ks_total += int(md.get('ks_shares') or 0)

                members_list.append(md)

            return render_template(
                "admin/reports/member-report.html",
                members=members_list,
                total_members=len(members_list),
                member_count=member_count,
                staff_count=staff_count,
                active_count=active_count,
                inactive_count=inactive_count,
                members_with_loans=members_with_loans,
                total_savings=total_savings,
                kai_total=kai_total,
                ks_total=ks_total,
                range_start=start,
                range_end=end,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )

        # ============================================================
        # FINANCIAL REPORT
        # ============================================================
        elif report_type == 'financial':
            total_users = fetchval(db, """
                SELECT COUNT(*) FROM users
                WHERE status = 'active'
                AND LOWER(role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
            """) or 0

            member_count = fetchval(db, """
                SELECT COUNT(*) FROM users
                WHERE status = 'active' AND LOWER(role) = 'member'
            """) or 0

            staff_count = total_users - member_count

            kai_total = ks_total = kac_total = reg_total = 0
            kai_shares = ks_shares = 0
            kai_members = ks_members = kac_members = reg_members = 0
            total_savings = 0

            all_users = db.execute("""
                SELECT kai_shares, ks_shares, kac_paid, registration_fee_paid, savings_balance
                FROM users
                WHERE status = 'active'
                AND LOWER(role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
            """).fetchall()

            for u in all_users:
                ud = row_to_dict(u)
                total_savings += _to_int(ud.get('savings_balance'))

                kai_sh = int(ud.get('kai_shares') or 0)
                if kai_sh > 0:
                    kai_shares  += kai_sh
                    kai_total   += kai_sh * kai_share_price
                    kai_members += 1

                ks_sh = int(ud.get('ks_shares') or 0)
                if ks_sh > 0:
                    ks_shares  += ks_sh
                    ks_total   += ks_sh * ks_share_price
                    ks_members += 1

                kac_val = ud.get('kac_paid') or 0
                try:
                    kac_val = float(kac_val)
                except (ValueError, TypeError):
                    kac_val = 0
                if kac_val == 1:
                    kac_val = kac_annual_fee
                kac_val = min(kac_val, kac_annual_fee)
                if kac_val > 0:
                    kac_total += int(kac_val)
                    kac_members += 1

                if ud.get('registration_fee_paid'):
                    reg_total += registration_fee
                    reg_members += 1

            raw_loans = db.execute(f"""
                SELECT status, amount, current_balance
                FROM loans
                WHERE application_date >= {PH} AND application_date <= {PH}
            """, (start, end)).fetchall()

            def bucket(statuses):
                c = a = 0
                for l in raw_loans:
                    ld = row_to_dict(l)
                    if (ld.get('status') or '').lower() in statuses:
                        c += 1
                        a += _to_int(ld.get('amount'))
                return c, a

            pending_loans,    pending_amount    = bucket(['pending'])
            approved_loans,   approved_amount   = bucket(['approved'])
            disbursed_loans,  disbursed_amount  = bucket(['disbursed'])
            active_loans,     active_amount     = bucket(['active'])
            completed_loans,  completed_amount  = bucket(['completed'])
            rejected_loans,   rejected_amount   = bucket(['rejected'])

            total_loans = len(raw_loans)
            total_loan_amount = sum(_to_int(row_to_dict(l).get('amount')) for l in raw_loans) or 0

            def pct(n):
                return round((n / total_loans * 100), 1) if total_loans > 0 else 0.0

            total_disbursed = _to_int(fetchval(db, """
                SELECT COALESCE(SUM(amount),0) FROM loans
                WHERE status IN ('disbursed','active','completed')
            """))

            total_repayments = _to_int(fetchval(db, """
                SELECT COALESCE(SUM(amount),0) FROM repayments
                WHERE status = 'completed'
            """))

            total_interest_accrued = _to_int(fetchval(db, """
                SELECT COALESCE(SUM(total_interest_accrued),0) FROM loans
            """))

            total_interest_paid = _to_int(fetchval(db, """
                SELECT COALESCE(SUM(interest_paid),0) FROM loans
            """))

            total_interest_outstanding = max(0, total_interest_accrued - total_interest_paid)

            total_app_fees = _to_int(fetchval(db, f"""
                SELECT COALESCE(SUM(application_fee),0) FROM loans
                WHERE application_date >= {PH} AND application_date <= {PH}
            """, (start, end)))

            staff_rows = db.execute("""
                SELECT role, COALESCE(savings_balance, 0) AS balance
                FROM users
                WHERE status = 'active'
                AND LOWER(role) IN ('admin','chairperson','treasurer','secretary','publicity')
            """).fetchall()

            staff_counts = {'admin': 0, 'treasurer': 0, 'secretary': 0, 'publicity': 0}
            staff_savings = {'admin': 0, 'treasurer': 0, 'secretary': 0, 'publicity': 0, 'total': 0}

            for s_row in staff_rows:
                sd = row_to_dict(s_row)
                role = (sd.get('role') or '').lower()
                balance = _to_int(sd.get('balance'))

                if role in ('admin', 'chairperson'):
                    staff_counts['admin'] += 1
                    staff_savings['admin'] += balance
                elif role == 'treasurer':
                    staff_counts['treasurer'] += 1
                    staff_savings['treasurer'] += balance
                elif role == 'secretary':
                    staff_counts['secretary'] += 1
                    staff_savings['secretary'] += balance
                elif role == 'publicity':
                    staff_counts['publicity'] += 1
                    staff_savings['publicity'] += balance

                staff_savings['total'] += balance

            return render_template(
                "admin/reports/financial-report.html",
                total_users=total_users,
                member_count=member_count,
                staff_count=staff_count,
                total_savings=total_savings,
                kai_total=kai_total, kai_shares=kai_shares, kai_members=kai_members,
                ks_total=ks_total,   ks_shares=ks_shares,   ks_members=ks_members,
                kac_total=kac_total, kac_members=kac_members,
                reg_total=reg_total, reg_members=reg_members,
                total_loans=total_loans,
                total_loan_amount=total_loan_amount,
                total_disbursed=total_disbursed,
                total_repayments=total_repayments,
                pending_loans=pending_loans,     pending_amount=pending_amount,     pct_pending=pct(pending_loans),
                approved_loans=approved_loans,   approved_amount=approved_amount,   pct_approved=pct(approved_loans),
                disbursed_loans=disbursed_loans, disbursed_amount=disbursed_amount, pct_disbursed=pct(disbursed_loans),
                active_loans=active_loans,       active_amount=active_amount,       pct_active=pct(active_loans),
                completed_loans=completed_loans, completed_amount=completed_amount, pct_completed=pct(completed_loans),
                rejected_loans=rejected_loans,   rejected_amount=rejected_amount,   pct_rejected=pct(rejected_loans),
                total_interest_accrued=total_interest_accrued,
                total_interest_paid=total_interest_paid,
                total_interest_outstanding=total_interest_outstanding,
                total_app_fees=total_app_fees,
                staff_counts=staff_counts,
                staff_savings=staff_savings,
                range_start=start,
                range_end=end,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )

        # ============================================================
        # LOAN REPORT
        # ============================================================
        elif report_type == 'loans':
            raw_loans = db.execute(f"""
                SELECT
                    l.id, l.loan_number, l.amount, l.current_balance, l.status,
                    l.application_date, l.approved_date, l.disbursed_date, l.completed_date,
                    l.total_interest_accrued, l.interest_paid, l.application_fee,
                    u.id AS user_id, u.full_name, u.sacco_number, u.role,
                    COALESCE((
                        SELECT COUNT(*) FROM repayments r
                        WHERE r.loan_id = l.id AND r.status = 'completed'
                    ), 0) AS payment_count,
                    COALESCE((
                        SELECT SUM(r.amount) FROM repayments r
                        WHERE r.loan_id = l.id AND r.status = 'completed'
                    ), 0) AS total_paid
                FROM loans l
                JOIN users u ON l.user_id = u.id
                WHERE l.application_date >= {PH} AND l.application_date <= {PH}
                ORDER BY l.application_date DESC
            """, (start, end)).fetchall()

            loans_list = []
            total_amount = total_balance = total_repaid = 0
            total_interest_accrued = total_interest_paid = total_app_fees = total_payments = 0
            pending_count = approved_count = disbursed_count = active_count = 0
            completed_count = rejected_count = 0
            member_loan_count = staff_loan_count = 0

            for r in raw_loans:
                d = row_to_dict(r)
                d['amount']                 = _to_int(d.get('amount'))
                d['current_balance']        = _to_int(d.get('current_balance')) or d['amount']
                d['total_interest_accrued'] = _to_int(d.get('total_interest_accrued'))
                d['interest_paid']          = _to_int(d.get('interest_paid'))
                d['application_fee']        = _to_int(d.get('application_fee'))
                d['payment_count']          = int(d.get('payment_count') or 0)
                d['total_paid']             = _to_int(d.get('total_paid'))

                total_amount           += d['amount']
                total_balance          += d['current_balance']
                total_repaid           += d['total_paid']
                total_interest_accrued += d['total_interest_accrued']
                total_interest_paid    += d['interest_paid']
                total_app_fees         += d['application_fee']
                total_payments         += d['payment_count']

                st = (d.get('status') or 'pending').lower()
                if st == 'pending':     pending_count += 1
                elif st == 'approved':  approved_count += 1
                elif st == 'disbursed': disbursed_count += 1
                elif st == 'active':    active_count += 1
                elif st == 'completed': completed_count += 1
                elif st == 'rejected':  rejected_count += 1

                role = (d.get('role') or 'member').lower()
                if role == 'member':
                    member_loan_count += 1
                else:
                    staff_loan_count += 1

                loans_list.append(d)

            total_loans = len(loans_list)
            total_interest_outstanding = max(0, total_interest_accrued - total_interest_paid)

            return render_template(
                "admin/reports/loan-report.html",
                loans=loans_list,
                total_loans=total_loans,
                total_amount=total_amount,
                total_balance=total_balance,
                total_repaid=total_repaid,
                total_payments=total_payments,
                total_interest_accrued=total_interest_accrued,
                total_interest_paid=total_interest_paid,
                total_interest_outstanding=total_interest_outstanding,
                total_app_fees=total_app_fees,
                pending_count=pending_count,
                approved_count=approved_count,
                disbursed_count=disbursed_count,
                active_count=active_count,
                completed_count=completed_count,
                rejected_count=rejected_count,
                member_loan_count=member_loan_count,
                staff_loan_count=staff_loan_count,
                range_start=start,
                range_end=end,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )

        # ============================================================
        # SAVINGS REPORT
        # ============================================================
        elif report_type == 'savings':
            rows = db.execute(f"""
                SELECT
                    u.id, u.full_name, u.sacco_number, u.role, u.status,
                    COALESCE(u.savings_balance, 0) AS savings_balance,
                    COALESCE(u.kai_shares, 0) AS kai_shares,
                    COALESCE(u.ks_shares, 0) AS ks_shares,
                    COALESCE(u.kac_paid, 0) AS kac_paid,
                    COALESCE(u.registration_fee_paid, 0) AS registration_fee_paid,
                    COALESCE((
                        SELECT COUNT(*) FROM savings_deposits sd
                        WHERE sd.user_id = u.id
                        AND sd.deposit_date >= {PH}
                        AND sd.deposit_date <= {PH}
                    ), 0) AS deposit_count,
                    COALESCE((
                        SELECT SUM(sd.amount) FROM savings_deposits sd
                        WHERE sd.user_id = u.id
                        AND sd.deposit_date >= {PH}
                        AND sd.deposit_date <= {PH}
                    ), 0) AS total_deposited
                FROM users u
                WHERE LOWER(u.role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
                AND u.status = 'active'
                ORDER BY u.savings_balance DESC, u.full_name ASC
            """, (start, end, start, end)).fetchall()

            member_savings_list = []
            member_count = staff_count = 0
            total_savings = total_deposited = 0
            kai_total = ks_total = kac_total = reg_total = 0

            for r in rows:
                d = row_to_dict(r)
                kac_val = d.get('kac_paid') or 0
                try:
                    kac_val = float(kac_val)
                except (ValueError, TypeError):
                    kac_val = 0
                if kac_val == 1:
                    kac_val = kac_annual_fee
                d['kac_paid'] = min(int(kac_val), kac_annual_fee)

                role = (d.get('role') or 'member').lower()
                if role == 'member':
                    member_count += 1
                else:
                    staff_count += 1

                total_savings   += _to_int(d.get('savings_balance'))
                total_deposited += _to_int(d.get('total_deposited'))

                kai_total += int(d.get('kai_shares') or 0) * kai_share_price
                ks_total  += int(d.get('ks_shares')  or 0) * ks_share_price
                kac_total += d['kac_paid']
                if d.get('registration_fee_paid'):
                    reg_total += registration_fee

                member_savings_list.append(d)

            total_deposits = fetchval(db, f"""
                SELECT COUNT(*) FROM savings_deposits
                WHERE deposit_date >= {PH} AND deposit_date <= {PH}
            """, (start, end)) or 0

            top_saver_name = member_savings_list[0]['full_name'] if member_savings_list else 'N/A'

            return render_template(
                "admin/reports/savings-report.html",
                member_savings=member_savings_list,
                total_savers=len(member_savings_list),
                total_savings=total_savings,
                total_deposited=total_deposited,
                total_deposits=total_deposits,
                member_count=member_count,
                staff_count=staff_count,
                top_saver_name=top_saver_name,
                kai_total=kai_total,
                ks_total=ks_total,
                kac_total=kac_total,
                reg_total=reg_total,
                range_start=start,
                range_end=end,
                generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                now=datetime.now(),
                session=session
            )

        else:
            flash('Invalid report type', 'danger')
            return redirect(url_for('admin_dashboard'))

    except Exception as e:
        print(f"Error generating report: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error generating report: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    finally:
        try:
            db.close()
        except Exception:
            pass
    
# ============================================================
# OTHER DASHBOARDS
# ============================================================
@app.route("/chairperson/dashboard")
def chairperson_dashboard():
    if session.get("role") != "chairperson":
        return redirect("/login")
    return render_template("chairperson/chairperson-dashboard.html")

# ============================================================
# SECRETARY DASHBOARD
# ============================================================
@app.route("/secretary/dashboard")
def secretary_dashboard():
    # Allow the same roles that other secretary pages allow
    allowed_roles = {"secretary", "treasurer", "admin", "chairperson"}
    user_role = (session.get("role") or "").strip().lower()

    if user_role not in allowed_roles:
        flash('Access denied', 'danger')
        return redirect("/login")

    log_action(
        action="view_secretary_dashboard",
        target="secretary_dashboard",
        details=f"Accessed by {session.get('role', 'unknown')}"
    )

    db = get_db()
    try:
        # ============================================================
        # ALL MEMBERS
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
        # STATISTICS
        # ============================================================
        total_members = len(members)

        active_members = fetchval(db, """
            SELECT COUNT(*) FROM users 
            WHERE LOWER(role) = 'member' AND status = 'active'
        """) or 0

        total_loans     = fetchval(db, "SELECT COUNT(*) FROM loans") or 0
        pending_loans   = fetchval(db, "SELECT COUNT(*) FROM loans WHERE status = 'pending'") or 0
        active_loans    = fetchval(db, "SELECT COUNT(*) FROM loans WHERE status IN ('disbursed', 'active')") or 0
        completed_loans = fetchval(db, "SELECT COUNT(*) FROM loans WHERE status = 'completed'") or 0

        total_savings = fetchval(db, """
            SELECT COALESCE(SUM(savings_balance), 0) 
            FROM users WHERE LOWER(role) = 'member'
        """) or 0

        # ============================================================
        # RECENT ACTIVITIES
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
        # PENDING LOAN APPLICATIONS
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
        # SYSTEM SETTINGS
        # ============================================================
        settings_row = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        if settings_row:
            settings = row_to_dict(settings_row)
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

        # ============================================================
        # KAC QUICK STATS (for a small widget on the dashboard)
        # ============================================================
        kac_stats = {
            'eligible': fetchval(db, """
                SELECT COUNT(*) FROM users
                WHERE status = 'active'
                  AND LOWER(role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
                  AND COALESCE(kac_paid, 0) >= 100000
            """) or 0,
            'partial': fetchval(db, """
                SELECT COUNT(*) FROM users
                WHERE status = 'active'
                  AND LOWER(role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
                  AND COALESCE(kac_paid, 0) > 0
                  AND COALESCE(kac_paid, 0) < 100000
            """) or 0,
            'total_claims': fetchval(db, """
                SELECT COUNT(*) FROM kac_claims WHERE status = 'active'
            """) or 0
        }

        # ============================================================
        # KS INTEREST QUICK STATS (for a small widget)
        # ============================================================
        ks_interest_stats = {
            'total_interest': fetchval(db, """
                SELECT COALESCE(SUM(total_interest_earned), 0)
                FROM ks_interest_records
            """) or 0,
            'contingency_fund': fetchval(db, """
                SELECT COALESCE(SUM(contingency_amount), 0)
                FROM ks_interest_records
            """) or 0,
            'months_recorded': fetchval(db, """
                SELECT COUNT(*) FROM ks_interest_records
            """) or 0
        }

        # Debug
        print("=" * 60)
        print("SECRETARY DASHBOARD LOADED")
        print(f"Role:            {session.get('role')}")
        print(f"Total Members:   {total_members}")
        print(f"Active Members:  {active_members}")
        print(f"Total Loans:     {total_loans}")
        print(f"Pending Loans:   {pending_loans}")
        print(f"KAC Eligible:    {kac_stats['eligible']}")
        print(f"KS Interest:     {ks_interest_stats['total_interest']}")
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
            kac_stats=kac_stats,
            ks_interest_stats=ks_interest_stats,
            now=datetime.now(),
            session=session
        )

    except Exception as e:
        print(f"Error loading secretary dashboard: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('login'))
    finally:
        try:
            db.close()
        except Exception:
            pass

# ============================================================
# SECRETARY — KS INTEREST & CONTINGENCY FUND
# ============================================================

# Roles allowed to view / manage KS interest
KS_INTEREST_ROLES = {"secretary", "treasurer", "admin", "chairperson"}


def _is_ks_staff():
    """Return True if the current session may access KS interest data."""
    role = (session.get("role") or "").strip().lower()
    return role in KS_INTEREST_ROLES


def _deny_ks_page():
    """Flash + redirect for unauthorized page access."""
    flash('Access denied. KS Interest is restricted to SACCO staff only.', 'danger')
    return redirect("/login")


def _deny_ks_json():
    """JSON 403 for unauthorized API access."""
    return jsonify({'success': False, 'message': 'Access denied'}), 403


# ------------------------------------------------------------
# KS INTEREST PAGE
# ------------------------------------------------------------
@app.route("/secretary/ks-interest")
def secretary_ks_interest():
    if not _is_ks_staff():
        return _deny_ks_page()

    log_action(
        action="view_ks_interest",
        target="secretary_ks_interest",
        details=f"Accessed by {session.get('role', 'unknown')}"
    )

    conn = get_db()
    try:
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass

        # ---- All recorded months ----
        records_rows = conn.execute("""
            SELECT r.*,
                   u.full_name AS entered_by_name
            FROM ks_interest_records r
            LEFT JOIN users u ON u.id = r.entered_by
            ORDER BY r.year DESC, r.month DESC
        """).fetchall()
        records = [row_to_dict(r) for r in records_rows]

        # ---- Totals ----
        total_contingency  = sum((r.get('contingency_amount') or 0) for r in records)
        total_distributed  = sum((r.get('distributable_amount') or 0) for r in records)
        total_interest_all = sum((r.get('total_interest_earned') or 0) for r in records)

        # ---- Settings ----
        current_year = datetime.now().year
        settings_row = conn.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        settings_dict = row_to_dict(settings_row) if settings_row else {}
        ks_share_price   = settings_dict.get('ks_share_price') or 10000
        contingency_rate = settings_dict.get('contingency_rate') or 10

        # ---- Members with KS savings ----
        members_rows = conn.execute("""
            SELECT u.id, u.full_name, u.sacco_number, u.ks_shares, u.role
            FROM users u
            WHERE u.status = 'active'
              AND LOWER(u.role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
              AND COALESCE(u.ks_shares, 0) > 0
            ORDER BY u.full_name ASC
        """).fetchall()

        members = []
        for m_row in members_rows:
            m = row_to_dict(m_row)
            ks_savings = (m['ks_shares'] or 0) * ks_share_price

            total_interest = fetchval(conn, f"""
                SELECT COALESCE(SUM(interest_share), 0)
                FROM ks_interest_allocations
                WHERE user_id = {'%s' if DATABASE_URL else '?'}
                  AND year = {'%s' if DATABASE_URL else '?'}
            """, (m['id'], current_year)) or 0

            members.append({
                'id': m['id'],
                'full_name': m['full_name'],
                'sacco_number': m['sacco_number'],
                'ks_shares': m['ks_shares'] or 0,
                'ks_savings': ks_savings,
                'total_interest': total_interest,
                'role': m['role']
            })

        total_ks_savings_all       = sum(m['ks_savings'] for m in members)
        total_interest_all_members = sum(m['total_interest'] for m in members)

        current_month      = datetime.now().month
        current_year_label = f"{current_year}"

        return render_template(
            "secretary/ks-interest.html",
            records=records,
            members=members,
            total_contingency=total_contingency,
            total_distributed=total_distributed,
            total_interest_all=total_interest_all,
            total_ks_savings_all=total_ks_savings_all,
            total_interest_all_members=total_interest_all_members,
            ks_share_price=ks_share_price,
            contingency_rate=contingency_rate,
            current_year=current_year,
            current_month=current_month,
            current_year_label=current_year_label,
            now=datetime.now()
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('secretary_dashboard'))
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------
# KS INTEREST — RECORD MONTHLY
# ------------------------------------------------------------
@app.route("/secretary/ks-interest/record", methods=["POST"])
def secretary_record_ks_interest():
    if not _is_ks_staff():
        return _deny_ks_json()

    data = request.get_json() or {}
    year           = int(data.get('year') or datetime.now().year)
    month          = int(data.get('month') or datetime.now().month)
    total_interest = float(data.get('total_interest') or 0)
    notes          = (data.get('notes') or '').strip()

    if month < 1 or month > 12:
        return jsonify({'success': False, 'message': 'Invalid month'}), 400
    if total_interest <= 0:
        return jsonify({'success': False, 'message': 'Interest must be greater than zero'}), 400

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # Guard against duplicate months
        existing = db.execute(f"""
            SELECT id FROM ks_interest_records
            WHERE year = {PH} AND month = {PH}
        """, (year, month)).fetchone()

        if existing:
            return jsonify({
                'success': False,
                'message': f'Interest for {year}-{month:02d} is already recorded. Delete it first if you need to re-enter.'
            }), 400

        # ---- Load settings ----
        settings_row = db.execute("SELECT * FROM system_settings LIMIT 1").fetchone()
        s = row_to_dict(settings_row) if settings_row else {}
        contingency_rate  = s.get('contingency_rate') or 10
        ks_share_price    = s.get('ks_share_price') or 10000

        contingency_amount = total_interest * (contingency_rate / 100)
        distributable      = total_interest - contingency_amount

        # ---- KS savers snapshot ----
        ks_savers_rows = db.execute("""
            SELECT id, full_name, COALESCE(ks_shares, 0) AS ks_shares
            FROM users
            WHERE status = 'active'
              AND LOWER(role) IN ('member','admin','chairperson','treasurer','secretary','publicity')
              AND COALESCE(ks_shares, 0) > 0
        """).fetchall()

        ks_savers = []
        for r in ks_savers_rows:
            d = row_to_dict(r)
            savings_amount = (d['ks_shares'] or 0) * ks_share_price
            ks_savers.append({
                'user_id': d['id'],
                'ks_shares': d['ks_shares'],
                'savings': savings_amount
            })

        total_ks_savings = sum(k['savings'] for k in ks_savers)
        if total_ks_savings <= 0:
            return jsonify({
                'success': False,
                'message': 'No KS savings recorded in the system — nothing to allocate.'
            }), 400

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # ---- Insert the record ----
        if DATABASE_URL:
            row = db.execute(f"""
                INSERT INTO ks_interest_records (
                    year, month, total_interest_earned,
                    contingency_amount, distributable_amount,
                    total_ks_savings, total_members_credited,
                    status, entered_by, entered_at, notes
                ) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},'recorded',{PH},{PH},{PH})
                RETURNING id
            """, (year, month, total_interest,
                  contingency_amount, distributable,
                  total_ks_savings, len(ks_savers),
                  session['user_id'], now_str, notes)).fetchone()
            record_id = row['id'] if isinstance(row, dict) else row[0]
        else:
            cur = db.cursor()
            cur.execute("""
                INSERT INTO ks_interest_records (
                    year, month, total_interest_earned,
                    contingency_amount, distributable_amount,
                    total_ks_savings, total_members_credited,
                    status, entered_by, entered_at, notes
                ) VALUES (?,?,?,?,?,?,?,'recorded',?,?,?)
            """, (year, month, total_interest,
                  contingency_amount, distributable,
                  total_ks_savings, len(ks_savers),
                  session['user_id'], now_str, notes))
            record_id = cur.lastrowid

        # ---- Allocate per member ----
        for k in ks_savers:
            share = (k['savings'] * distributable) / total_ks_savings if total_ks_savings > 0 else 0

            db.execute(f"""
                INSERT INTO ks_interest_allocations (
                    record_id, user_id, year, month,
                    ks_savings_at_month, interest_share,
                    paid, created_at
                ) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},0,{PH})
            """, (record_id, k['user_id'], year, month,
                  k['savings'], round(share, 2), now_str))

        # ---- Bump contingency total ----
        db.execute(f"""
            UPDATE system_settings
            SET contingency_fund_total = COALESCE(contingency_fund_total, 0) + {PH}
            WHERE id = (SELECT id FROM system_settings LIMIT 1)
        """, (contingency_amount,))

        db.commit()

        return jsonify({
            'success': True,
            'message': (
                f'Recorded interest for {year}-{month:02d}. '
                f'Contingency: UGX {contingency_amount:,.0f}, '
                f'Distributed to {len(ks_savers)} KS savers.'
            ),
            'record_id': record_id,
            'contingency_amount': contingency_amount,
            'distributable': distributable,
            'members_credited': len(ks_savers)
        })

    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# KS INTEREST — DELETE
# ------------------------------------------------------------
@app.route("/secretary/ks-interest/<int:record_id>/delete", methods=["POST"])
def secretary_delete_ks_interest(record_id):
    if not _is_ks_staff():
        return _deny_ks_json()

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        rec = db.execute(
            f"SELECT * FROM ks_interest_records WHERE id = {PH}", (record_id,)
        ).fetchone()
        if not rec:
            return jsonify({'success': False, 'message': 'Record not found'}), 404

        rec_d = row_to_dict(rec)

        # Remove allocations
        db.execute(
            f"DELETE FROM ks_interest_allocations WHERE record_id = {PH}",
            (record_id,)
        )

        # Reverse contingency total
        db.execute(f"""
            UPDATE system_settings
            SET contingency_fund_total = COALESCE(contingency_fund_total, 0) - {PH}
            WHERE id = (SELECT id FROM system_settings LIMIT 1)
        """, (rec_d.get('contingency_amount') or 0,))

        # Delete the record
        db.execute(
            f"DELETE FROM ks_interest_records WHERE id = {PH}",
            (record_id,)
        )

        db.commit()
        return jsonify({'success': True, 'message': 'Interest record deleted and allocations reversed.'})

    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# KS INTEREST — MEMBER DETAILS JSON
# ------------------------------------------------------------
@app.route("/secretary/ks-interest/member/<int:user_id>")
def secretary_member_interest(user_id):
    # ❌ Members CANNOT see any member's interest, including their own
    if not _is_ks_staff():
        return _deny_ks_json()

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        user_row = db.execute(
            f"SELECT id, full_name, sacco_number, ks_shares FROM users WHERE id = {PH}",
            (user_id,)
        ).fetchone()
        if not user_row:
            return jsonify({'success': False, 'message': 'Member not found'}), 404

        user = row_to_dict(user_row)

        alloc_rows = db.execute(f"""
            SELECT a.*, r.total_interest_earned, r.distributable_amount
            FROM ks_interest_allocations a
            JOIN ks_interest_records r ON r.id = a.record_id
            WHERE a.user_id = {PH}
            ORDER BY a.year DESC, a.month DESC
        """, (user_id,)).fetchall()

        allocations = [row_to_dict(a) for a in alloc_rows]
        total_interest = sum((a.get('interest_share') or 0) for a in allocations)

        settings_row = db.execute("SELECT ks_share_price FROM system_settings LIMIT 1").fetchone()
        ks_price = row_to_dict(settings_row).get('ks_share_price', 10000) if settings_row else 10000

        return jsonify({
            'success': True,
            'member': {
                'id': user['id'],
                'full_name': user['full_name'],
                'sacco_number': user['sacco_number'],
                'ks_shares': user['ks_shares'] or 0,
                'ks_savings': (user['ks_shares'] or 0) * ks_price,
                'total_interest': total_interest
            },
            'allocations': allocations
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass
            
# ============================================================
# PUBLICITY ROUTES
# ============================================================

@app.route("/publicity/dashboard")
def publicity_dashboard():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    conn = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # Member count
        total_members = fetchval(conn, """
            SELECT COUNT(*) FROM users
            WHERE LOWER(role) = 'member' AND status = 'active'
        """) or 0

        # Announcements count
        total_announcements = fetchval(conn, "SELECT COUNT(*) FROM announcements") or 0

        # Upcoming events count — DB-specific date function
        if DATABASE_URL:
            upcoming_events = fetchval(conn, """
                SELECT COUNT(*) FROM events
                WHERE NULLIF(event_date, '')::timestamp >= CURRENT_DATE
            """) or 0
        else:
            upcoming_events = fetchval(conn, """
                SELECT COUNT(*) FROM events
                WHERE event_date >= date('now')
            """) or 0

        # Recent announcements
        announcements = conn.execute("""
            SELECT * FROM announcements
            ORDER BY created_at DESC
            LIMIT 5
        """).fetchall()

        # Upcoming events
        if DATABASE_URL:
            events = conn.execute("""
                SELECT * FROM events
                WHERE NULLIF(event_date, '')::timestamp >= CURRENT_DATE
                ORDER BY event_date ASC
                LIMIT 4
            """).fetchall()
        else:
            events = conn.execute("""
                SELECT * FROM events
                WHERE event_date >= date('now')
                ORDER BY event_date ASC
                LIMIT 4
            """).fetchall()

    except Exception as e:
        print(f"Database Error in publicity_dashboard: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Database error: {str(e)}', 'danger')
        return redirect(url_for('login'))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return render_template(
        "publicity/publicity-dashboard.html",
        total_members=total_members,
        total_announcements=total_announcements,
        upcoming_events=upcoming_events,
        announcements=announcements,
        events=events,
        session=session,
    )


# ============================================================
# ANNOUNCEMENTS
# ============================================================

@app.route("/publicity/announcements")
def publicity_announcements():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    conn = get_db()

    try:
        total_members = fetchval(conn, """
            SELECT COUNT(*) FROM users
            WHERE LOWER(role) = 'member' AND status = 'active'
        """) or 0

        announcements = conn.execute("""
            SELECT a.*,
                   (SELECT COUNT(*) FROM users
                    WHERE LOWER(role) = 'member' AND status = 'active') as recipients
            FROM announcements a
            ORDER BY a.created_at DESC
        """).fetchall()
    except Exception as e:
        import traceback
        traceback.print_exc()
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return render_template(
        "publicity/publicity-announcements.html",
        announcements=announcements,
        total_members=total_members
    )


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
    PH = "%s" if DATABASE_URL else "?"

    try:
        # ✅ Use CURRENT_TIMESTAMP — works on both SQLite and PostgreSQL
        conn.execute(f"""
            INSERT INTO announcements (title, content, created_at, created_by)
            VALUES ({PH}, {PH}, CURRENT_TIMESTAMP, {PH})
        """, (title, content, session.get('user_id')))
        conn.commit()

        # Get all active members
        members = conn.execute("""
            SELECT id FROM users
            WHERE LOWER(role) = 'member' AND status = 'active'
        """).fetchall()

        # Create notifications
        for member in members:
            member_id = member["id"] if isinstance(member, dict) else member[0]
            conn.execute(f"""
                INSERT INTO notifications (user_id, type, title, message, link, created_at, is_read)
                VALUES ({PH}, 'announcement', {PH}, {PH}, '/publicity/announcements', CURRENT_TIMESTAMP, 0)
            """, (member_id, title, content[:200]))

        conn.commit()

        flash(f'Announcement posted and sent to {len(members)} members!', 'success')

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            conn.rollback()
        except Exception:
            pass
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return redirect("/publicity/announcements")


@app.route("/publicity/announcement/delete/<int:id>", methods=['POST'])
def delete_announcement(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    conn = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        conn.execute(f"DELETE FROM announcements WHERE id = {PH}", (id,))
        conn.commit()
        flash('Announcement deleted successfully!', 'success')
    except Exception as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return redirect("/publicity/announcements")


# ============================================================
# EVENTS
# ============================================================

@app.route("/publicity/events")
def publicity_events():
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    conn = get_db()

    try:
        events = conn.execute("""
            SELECT * FROM events
            ORDER BY event_date ASC, event_time ASC
        """).fetchall()
    except Exception as e:
        flash(f'Database error: {str(e)}', 'danger')
        return redirect("/publicity/dashboard")
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
    PH = "%s" if DATABASE_URL else "?"

    try:
        conn.execute(f"""
            INSERT INTO events (title, event_date, event_time, location, description, created_at, created_by)
            VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, CURRENT_TIMESTAMP, {PH})
        """, (title, event_date, event_time or None, location, description, session.get('user_id')))
        conn.commit()

        # Get all active members
        members = conn.execute("""
            SELECT id FROM users
            WHERE LOWER(role) = 'member' AND status = 'active'
        """).fetchall()

        # Create notifications
        for member in members:
            member_id = member["id"] if isinstance(member, dict) else member[0]
            conn.execute(f"""
                INSERT INTO notifications (user_id, type, title, message, link, created_at, is_read)
                VALUES ({PH}, 'event', {PH}, {PH}, '/publicity/events', CURRENT_TIMESTAMP, 0)
            """, (member_id, f"New Event: {title}", f"Join us for {title} on {event_date} at {location}"))

        conn.commit()

        flash(f'Event created and notified {len(members)} members!', 'success')

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            conn.rollback()
        except Exception:
            pass
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return redirect("/publicity/events")


@app.route("/publicity/event/delete/<int:id>", methods=['POST'])
def delete_event(id):
    if session.get("role") not in ["publicity", "secretary", "treasurer", "admin"]:
        flash('Access denied', 'danger')
        return redirect("/login")

    conn = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        conn.execute(f"DELETE FROM events WHERE id = {PH}", (id,))
        conn.commit()
        flash('Event deleted successfully!', 'success')
    except Exception as e:
        flash(f'Database error: {str(e)}', 'danger')
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return redirect("/publicity/events")

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
    """Member chat page â€” shows list of staff, then thread with chosen staff."""
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
    PH = "%s" if DATABASE_URL else "?"

    db = get_db()
    try:
        # Verify target is a staff member
        target_row = db.execute(
            f"SELECT id, role, full_name FROM users WHERE id = {PH}",
            (staff_id,)
        ).fetchone()

        if not target_row:
            return jsonify({"success": False, "message": "Staff not found"}), 404

        target = row_to_dict(target_row)
        target_role = (target.get("role") or "").lower()

        if target_role not in STAFF_ROLES:
            return jsonify({
                "success": False,
                "message": "Members cannot message other members"
            }), 403

        # Insert chat message
        cursor = db.cursor()
        cursor.execute(f"""
            INSERT INTO chat_messages
                (sender_id, receiver_id, message, message_type, is_read)
            VALUES ({PH}, {PH}, {PH}, 'general', 0)
        """, (member_id, staff_id, body))

        # Create notification for the staff member
        sender_name = session.get("full_name", "Member")
        db.execute(f"""
            INSERT INTO notifications
                (user_id, type, title, message, link, created_at, is_read)
            VALUES ({PH}, 'chat', {PH}, {PH}, '/staff/chat', CURRENT_TIMESTAMP, 0)
        """, (
            staff_id,
            f"New message from {sender_name} (Member)",
            body[:200]
        ))

        db.commit()
        return jsonify({"success": True, "message": "Sent"})

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass
# ============================================================
# PUBLICITY CHAT â€” separate from chat_api.py
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
    """Publicity chat page â€” pick any member or other staff to chat with."""
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
        """, (other_id, f"ðŸ“© New message from {sender_name} (Publicity)", body[:200], link))
    except Exception as e:
        print("Notification insert failed (non-fatal):", e)

    db.commit()
    db.close()
    return jsonify({"success": True, "message": "Sent"})

# ============================================================
# YEAR-END ROLLOVER ROUTES
# Paste these in app.py (before `if __name__ == "__main__":`)
# ============================================================

# ------------------------------------------------------------
# PREVIEW — safe, read-only
# ------------------------------------------------------------
@app.route("/admin/year-end/preview")
def year_end_preview():
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") not in ("admin", "chairperson"):
        flash("Only Admin or Chairperson can run year-end.", "danger")
        return redirect(url_for("treasurer_dashboard"))

    db = get_db()
    try:
        # Counts for the preview
        total_users = fetchval(db, "SELECT COUNT(*) FROM users") or 0
        total_loans = fetchval(db, "SELECT COUNT(*) FROM loans") or 0
        total_deposits = fetchval(db, "SELECT COUNT(*) FROM savings_deposits") or 0
        total_repayments = fetchval(db, "SELECT COUNT(*) FROM repayments") or 0
        total_messages = fetchval(db, "SELECT COUNT(*) FROM chat_messages") or 0
        total_notifications = fetchval(db, "SELECT COUNT(*) FROM notifications") or 0

        return render_template(
            "admin/year-end-preview.html",
            total_users=total_users,
            total_loans=total_loans,
            total_deposits=total_deposits,
            total_repayments=total_repayments,
            total_messages=total_messages,
            total_notifications=total_notifications,
        )
    except Exception as e:
        flash(f"Preview error: {e}", "danger")
        return redirect(url_for("treasurer_dashboard"))
    finally:
        try:
            db.close()
        except Exception:
            pass


# ------------------------------------------------------------
# EXECUTE — Simple full reset (no archiving)
# ------------------------------------------------------------
@app.route("/admin/year-end/execute", methods=["POST"])
def year_end_execute():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    if session.get("role") not in ("admin", "chairperson"):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    data = request.get_json() or {}
    confirm = (data.get("confirm_text") or "").strip()

    if confirm != "RESET":
        return jsonify({"success": False, "message": "Type RESET to confirm."}), 400

    db = get_db()
    PH = "%s" if DATABASE_URL else "?"

    try:
        # Get current admin id so we don't delete ourselves
        current_uid = session["user_id"]

        # ============================================================
        # WIPE — FK-safe order: children before parents
        # ============================================================

        # 1. Chat messages (FK to users)
        try:
            db.execute("DELETE FROM chat_messages")
            print("✅ Cleared chat_messages")
        except Exception as e:
            print(f"⚠️ chat_messages: {e}")
            try: db.rollback()
            except Exception: pass

        # 2. Notifications (FK to users)
        try:
            db.execute("DELETE FROM notifications")
            print("✅ Cleared notifications")
        except Exception as e:
            print(f"⚠️ notifications: {e}")
            try: db.rollback()
            except Exception: pass

        # 3. Loan guarantors (FK to loans)
        try:
            db.execute("DELETE FROM loan_guarantors")
            print("✅ Cleared loan_guarantors")
        except Exception as e:
            print(f"⚠️ loan_guarantors: {e}")
            try: db.rollback()
            except Exception: pass

        # 4. Guarantor tracking (FK to loans)
        try:
            db.execute("DELETE FROM guarantor_tracking")
            print("✅ Cleared guarantor_tracking")
        except Exception as e:
            print(f"⚠️ guarantor_tracking: {e}")
            try: db.rollback()
            except Exception: pass

        # 5. Repayments (FK to loans + users)
        try:
            db.execute("DELETE FROM repayments")
            print("✅ Cleared repayments")
        except Exception as e:
            print(f"⚠️ repayments: {e}")
            try: db.rollback()
            except Exception: pass

        # 6. Loans (FK to users)
        try:
            db.execute("DELETE FROM loans")
            print("✅ Cleared loans")
        except Exception as e:
            print(f"⚠️ loans: {e}")
            try: db.rollback()
            except Exception: pass

        # 7. Savings deposits (FK to users)
        try:
            db.execute("DELETE FROM savings_deposits")
            print("✅ Cleared savings_deposits")
        except Exception as e:
            print(f"⚠️ savings_deposits: {e}")
            try: db.rollback()
            except Exception: pass

        # 8. Publicity tables (no FK to users)
        for tbl in ("announcements", "events", "newsletters", "social_posts"):
            try:
                db.execute(f"DELETE FROM {tbl}")
                print(f"✅ Cleared {tbl}")
            except Exception as e:
                print(f"⚠️ {tbl}: {e}")
                try: db.rollback()
                except Exception: pass

        # 9. Delete ALL users except current admin
        try:
            db.execute(f"DELETE FROM users WHERE id != {PH}", (current_uid,))
            print(f"✅ Cleared users (kept admin id={current_uid})")
        except Exception as e:
            print(f"⚠️ users delete: {e}")
            try: db.rollback()
            except Exception: pass

        # 10. Reset settings to defaults
        try:
            db.execute(f"""
                UPDATE system_settings SET
                    sacco_name = 'Karacel Association',
                    registration_number = 'SACCO/REG/2024/001',
                    savings_interest_rate = 6.5,
                    loan_interest_rate = 12,
                    penalty_rate = 5,
                    max_loan_amount = '10000000',
                    min_loan_amount = '10000',
                    max_tenure = 24,
                    kai_share_price = 100000,
                    ks_share_price = 10000,
                    kac_annual_fee = 100000,
                    registration_fee = 20000,
                    updated_at = {PH}
                WHERE id = 1
            """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
            print("✅ Settings reset to defaults")
        except Exception as e:
            print(f"⚠️ settings reset: {e}")
            try: db.rollback()
            except Exception: pass

        # 11. Reset PostgreSQL sequences (so new IDs start at 1)
        if DATABASE_URL:
            for tbl in ("users", "loans", "repayments", "savings_deposits",
                        "notifications", "chat_messages", "loan_guarantors"):
                try:
                    db.execute(f"""
                        SELECT setval(
                            pg_get_serial_sequence('{tbl}', 'id'),
                            COALESCE((SELECT MAX(id) FROM {tbl}), 1),
                            true
                        )
                    """)
                except Exception:
                    try: db.rollback()
                    except Exception: pass

        db.commit()
        print("=" * 60)
        print("✅ SYSTEM RESET COMPLETE")
        print("=" * 60)

        return jsonify({
            "success": True,
            "message": "System reset to defaults. All data cleared. You remain logged in as admin."
        })

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Reset failed: {e}"}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass
# ------------------------------------------------------------
# ARCHIVES â€” browse past years
# ------------------------------------------------------------
@app.route("/admin/archives")
def admin_archives():
    if "user_id" not in session:
        return redirect("/login")
    db = get_db()
    try:
        archives = db.execute("""
            SELECT * FROM archived_years ORDER BY closed_at DESC
        """).fetchall()
        return render_template("admin/archives-list.html", archives=archives)
    finally:
        db.close()


@app.route("/admin/archives/<int:archive_id>")
def admin_archive_detail(archive_id):
    if "user_id" not in session:
        return redirect("/login")
    db = get_db()
    try:
        archive = db.execute(
            "SELECT * FROM archived_years WHERE id = ?", (archive_id,)
        ).fetchone()
        if not archive:
            flash("Archive not found.", "warning")
            return redirect(url_for("admin_archives"))

        users      = db.execute("SELECT * FROM archived_users WHERE archive_id=? ORDER BY full_name", (archive_id,)).fetchall()
        loans      = db.execute("SELECT * FROM archived_loans WHERE archive_id=? ORDER BY application_date DESC", (archive_id,)).fetchall()
        deposits   = db.execute("SELECT * FROM archived_savings_deposits WHERE archive_id=? ORDER BY deposit_date DESC", (archive_id,)).fetchall()
        repayments = db.execute("SELECT * FROM archived_repayments WHERE archive_id=? ORDER BY payment_date DESC", (archive_id,)).fetchall()

        return render_template(
            "admin/archive-detail.html",
            archive=archive,
            users=users, loans=loans,
            deposits=deposits, repayments=repayments
        )
    finally:
        db.close()


# ============================================================
# RUN THE APP
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)

