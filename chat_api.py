# ============================================================
# chat_api.py
# Complete chat blueprint — pages + API endpoints
# Used by all staff (treasurer, secretary, admin, chairperson, publicity)
# Compatible with both SQLite (local) and PostgreSQL (Render)
# ============================================================
from flask import (
    Blueprint, jsonify, request, session,
    redirect, url_for, render_template
)
import os
import sqlite3
import functools


# ============================================================
# DATABASE DETECTION
# ============================================================
DATABASE_URL = os.environ.get("DATABASE_URL")


# ============================================================
# POSTGRESQL ADAPTER (self-contained for this module)
# ============================================================
if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    _NOOP_STATEMENTS = {
        "BEGIN", "BEGIN TRANSACTION", "BEGIN;",
        "COMMIT", "COMMIT;", "END", "END;"
    }

    def _translate_sqlite_to_pg(query: str) -> str:
        """Convert SQLite-specific SQL to PostgreSQL equivalents."""
        q = query
        # Placeholder conversion
        q = q.replace("?", "%s")

        # Date/time functions
        q = q.replace("datetime('now')", "CURRENT_TIMESTAMP")
        q = q.replace("datetime('now', 'localtime')", "CURRENT_TIMESTAMP")
        q = q.replace("date('now')", "CURRENT_DATE")
        q = q.replace("date('now', 'start of month')",
                      "date_trunc('month', CURRENT_DATE)::date")
        q = q.replace("date('now', '-1 month')",
                      "(CURRENT_DATE - INTERVAL '1 month')::date")
        q = q.replace("date('now', '+1 day')",
                      "(CURRENT_DATE + INTERVAL '1 day')::date")

        # Auto-increment
        q = q.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")

        return q

    class _PgCursor:
        def __init__(self, real_cursor):
            self._cur = real_cursor
            self._last_inserted_id = None

        def execute(self, query, params=None):
            stripped = query.strip().upper().rstrip(";")

            if stripped in _NOOP_STATEMENTS:
                return self

            translated = _translate_sqlite_to_pg(query)

            if params is None:
                self._cur.execute(translated)
            else:
                self._cur.execute(translated, params)

            if stripped.startswith("INSERT"):
                try:
                    self._cur.execute("SELECT LASTVAL()")
                    row = self._cur.fetchone()
                    if row:
                        self._last_inserted_id = list(row.values())[0]
                except Exception:
                    self._last_inserted_id = None

            return self

        def executemany(self, query, params_seq):
            translated = _translate_sqlite_to_pg(query)
            self._cur.executemany(translated, params_seq)
            return self

        def executescript(self, script):
            translated = _translate_sqlite_to_pg(script)
            self._cur.execute(translated)
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
            return self._last_inserted_id

        def close(self):
            return self._cur.close()

        def __iter__(self):
            return iter(self._cur)

    class _PgConnection:
        def __init__(self, real_conn):
            self._conn = real_conn

        def execute(self, query, params=None):
            cur = self._conn.cursor(cursor_factory=RealDictCursor)
            adapted = _PgCursor(cur)
            adapted.execute(query, params)
            return adapted

        def cursor(self):
            cur = self._conn.cursor(cursor_factory=RealDictCursor)
            return _PgCursor(cur)

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
        return _PgConnection(psycopg2.connect(DATABASE_URL))


# ============================================================
# DB helper — self-contained (no circular import with app.py)
# ============================================================
def get_db():
    """
    Returns SQLite locally, PostgreSQL on Render.
    """
    if DATABASE_URL:
        return _pg_connect()
    conn = sqlite3.connect("sacco.db")
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# BLUEPRINT
# ============================================================
chat_api = Blueprint('chat_api', __name__)

STAFF_ROLES = ["admin", "treasurer", "secretary"]

# Which base template each staff role should extend.
BASE_FOR_ROLE = {
    "treasurer": "treasurer/treasurer-base.html",
    "secretary": "secretary/secretary-base.html",
    "admin":     "admin/admin-base.html",
}


# ============================================================
# DECORATORS
# ============================================================
def staff_required(f):
    """Only allow logged-in staff (JSON APIs)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({'success': False, 'message': 'Not logged in'}), 401
        if (session.get("role") or "").lower() not in STAFF_ROLES:
            return jsonify({'success': False, 'message': 'Access denied'}), 403
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# PAGE ROUTES — one shared template, different base per role
# ============================================================
def _render_chat_page(fallback_base):
    """Pick the correct base template for the current user."""
    if "user_id" not in session:
        return redirect(url_for('login'))

    role = (session.get("role") or "").lower()
    if role not in STAFF_ROLES:
        return redirect(url_for('login'))

    base = BASE_FOR_ROLE.get(role, fallback_base)
    return render_template('treasurer/shared/chat.html', base_template=base)


@chat_api.route('/treasurer/chat')
def treasurer_chat():
    return _render_chat_page("treasurer/treasurer-base.html")


@chat_api.route('/secretary/chat')
def secretary_chat():
    return _render_chat_page("secretary/secretary-base.html")


@chat_api.route('/staff/chat')
def staff_chat():
    return _render_chat_page("treasurer/treasurer-base.html")


# ============================================================
# API — LIST CHAT TARGETS (members + staff)
# ============================================================
@chat_api.route('/api/members/list')
@staff_required
def api_chat_members_list():
    """
    Returns BOTH SACCO members and staff users.
    Each row: { id, full_name, sacco_number, role, status, unread_count, type }
    """
    current_user_id = session['user_id']
    db = get_db()

    try:
        # ---------- 1. SACCO MEMBERS ----------
        members = db.execute("""
            SELECT
                u.id,
                u.full_name,
                u.sacco_number,
                u.role,
                u.status,
                (
                    SELECT COUNT(*)
                    FROM chat_messages
                    WHERE sender_id = u.id
                      AND receiver_id = ?
                      AND is_read = 0
                ) AS unread_count
            FROM users u
            WHERE LOWER(u.role) = 'member'
              AND u.id != ?
            ORDER BY u.full_name ASC
        """, (current_user_id, current_user_id)).fetchall()

        # ---------- 2. STAFF USERS ----------
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
                    FROM chat_messages
                    WHERE sender_id = u.id
                      AND receiver_id = ?
                      AND is_read = 0
                ) AS unread_count
            FROM users u
            WHERE LOWER(u.role) IN ({placeholders})
              AND u.id != ?
            ORDER BY
                CASE LOWER(u.role)
                    WHEN 'admin'        THEN 1
                    WHEN 'chairperson'  THEN 2
                    WHEN 'treasurer'    THEN 3
                    WHEN 'secretary'    THEN 4
                    WHEN 'publicity'    THEN 5
                    ELSE 9
                END,
                u.full_name ASC
        """, (current_user_id, *STAFF_ROLES, current_user_id)).fetchall()

        # ---------- 3. Combine ----------
        results = []
        for row in staff:
            d = dict(row)
            d['type'] = 'staff'
            d['status'] = 'active'
            d['sacco_number'] = d.get('sacco_number') or ''
            results.append(d)

        for row in members:
            d = dict(row)
            d['type'] = 'member'
            d['sacco_number'] = d.get('sacco_number') or ''
            results.append(d)

        db.close()
        return jsonify({'success': True, 'members': results})

    except Exception as e:
        db.close()
        print(f"Error loading chat targets: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# API — GET MESSAGES with a specific user (member OR staff)
# ============================================================
@chat_api.route('/api/chat/messages/<int:other_id>')
@staff_required
def api_chat_messages(other_id):
    current_user_id = session['user_id']
    db = get_db()

    try:
        messages = db.execute("""
            SELECT
                cm.id,
                cm.sender_id,
                cm.receiver_id,
                cm.message,
                cm.message_type,
                cm.is_read,
                cm.created_at,
                u.full_name AS sender_name,
                u.role      AS sender_role
            FROM chat_messages cm
            JOIN users u ON cm.sender_id = u.id
            WHERE (cm.sender_id = ? AND cm.receiver_id = ?)
               OR (cm.sender_id = ? AND cm.receiver_id = ?)
            ORDER BY cm.created_at ASC
            LIMIT 200
        """, (current_user_id, other_id, other_id, current_user_id)).fetchall()

        unread_count = db.execute("""
            SELECT COUNT(*) AS count
            FROM chat_messages
            WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
        """, (other_id, current_user_id)).fetchone()['count']

        db.close()
        return jsonify({
            'success': True,
            'messages': [dict(m) for m in messages],
            'unread_count': unread_count
        })

    except Exception as e:
        db.close()
        print(f"Error loading messages: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# API — SEND MESSAGE to a member OR another staff
# ============================================================
@chat_api.route('/api/chat/send', methods=['POST'])
@staff_required
def api_chat_send():
    data = request.get_json() or {}
    other_id = data.get('member_id')
    message = (data.get('message') or '').strip()
    message_type = data.get('type', 'general')

    if not other_id or not message:
        return jsonify({'success': False, 'message': 'Target and message required'}), 400

    sender_id = session['user_id']
    sender_role = session.get('role', 'staff')

    db = get_db()
    try:
        # Verify target exists (member OR staff)
        target = db.execute(
            "SELECT id, full_name, role FROM users WHERE id = ?",
            (other_id,)
        ).fetchone()

        if not target:
            db.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404

        # Insert chat message
        db.execute("""
            INSERT INTO chat_messages
                (sender_id, receiver_id, message, message_type, is_read)
            VALUES (?, ?, ?, ?, 0)
        """, (sender_id, other_id, message, message_type))

        # Notification for recipient
        sender_name = session.get('full_name') or sender_role.title()
        target_role = (target['role'] or '').lower()
        link = '/staff/chat' if target_role in STAFF_ROLES else '/member/dashboard'

        db.execute("""
            INSERT INTO notifications
                (user_id, type, title, message, link, is_read)
            VALUES (?, 'chat', ?, ?, ?, 0)
        """, (
            other_id,
            f"📩 New message from {sender_name} ({sender_role.title()})",
            message[:200],
            link
        ))

        db.commit()
        db.close()
        return jsonify({'success': True, 'message': 'Message sent successfully'})

    except Exception as e:
        db.rollback()
        db.close()
        print(f"Error sending message: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# API — MARK READ
# ============================================================
@chat_api.route('/api/chat/mark-read', methods=['POST'])
@staff_required
def api_chat_mark_read():
    data = request.get_json() or {}
    other_id = data.get('member_id')

    if not other_id:
        return jsonify({'success': False, 'message': 'Target required'}), 400

    current_user_id = session['user_id']
    db = get_db()
    try:
        db.execute("""
            UPDATE chat_messages
               SET is_read = 1
             WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
        """, (other_id, current_user_id))

        db.commit()
        db.close()
        return jsonify({'success': True, 'message': 'Messages marked as read'})

    except Exception as e:
        db.rollback()
        db.close()
        print(f"Error marking messages as read: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500


def create_chat_table():
    db = get_db()
    try:
        # Guard: make sure `users` exists first (FK dependency)
        try:
            db.execute("SELECT 1 FROM users LIMIT 1")
        except Exception:
            print("⏭️ Skipping chat table — users table not ready yet")
            try:
                db.rollback()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
            return

        pk = "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"

        db.execute(f"""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id {pk},
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                message_type TEXT DEFAULT 'general',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sender_id) REFERENCES users(id),
                FOREIGN KEY (receiver_id) REFERENCES users(id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_sender ON chat_messages(sender_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_receiver ON chat_messages(receiver_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_unread ON chat_messages(receiver_id, is_read)")
        db.commit()
        print("✅ Chat messages table ready")
    except Exception as e:
        print(f"⚠️ Error creating chat table: {str(e)}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass