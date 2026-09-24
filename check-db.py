import sqlite3
import os
from werkzeug.security import check_password_hash

print("CWD:", os.getcwd())
print("DB path:", os.path.abspath("sacco.db"))
print()

conn = sqlite3.connect("sacco.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, sacco_number, password FROM users").fetchall()

for r in rows:
    pw = r["password"] or ""
    is_hash = pw.startswith(("pbkdf2:", "scrypt:")) or "$" in pw[:20]
    kind = "HASHED ✅" if is_hash else "PLAINTEXT ❌"
    print(f"ID {r['id']:>3} | {r['sacco_number']:<15} | {kind} | {pw[:40]}")

print()
test_pw = input("Type the password you just set (or press Enter to skip): ").strip()
if test_pw:
    row = conn.execute("SELECT id, full_name, password FROM users WHERE id = 1").fetchone()
    result = check_password_hash(row["password"], test_pw)
    print(f"check_password_hash for ID 1: {result}")
    if not result:
        print("  -> Either the password was never written, or it was written to a different DB file")

conn.close()