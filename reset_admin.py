import sqlite3
from werkzeug.security import generate_password_hash

DB = "sacco.db"

sacco_number = "STAFF-202609-ADM544"
new_password = "NewPassword123!"

hashed_password = generate_password_hash(new_password)

conn = sqlite3.connect(DB)

cursor = conn.cursor()

cursor.execute("""
    UPDATE users
    SET password = ?
    WHERE sacco_number = ?
""", (hashed_password, sacco_number))

conn.commit()

if cursor.rowcount == 1:
    print("Admin password reset successfully.")
else:
    print("Admin with that SACCO number was not found.")

conn.close()