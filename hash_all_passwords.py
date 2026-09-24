import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect("sacco.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT id, full_name, sacco_number, password FROM users").fetchall()

migrated = 0
skipped = 0

for r in rows:
    pw = r["password"] or ""
    # Skip if already hashed
    if pw.startswith(("pbkdf2:", "scrypt:")) or ("$" in pw[:30] and len(pw) > 60):
        print(f"⏭️  Skipped (already hashed): {r['sacco_number']} - {r['full_name']}")
        skipped += 1
        continue

    hashed = generate_password_hash(pw)
    conn.execute("UPDATE users SET password = ? WHERE id = ?", (hashed, r["id"]))
    print(f"🔐 Hashed: {r['sacco_number']} - {r['full_name']} (was: {pw})")
    migrated += 1

conn.commit()
conn.close()

print()
print(f"✅ Done. Migrated {migrated} password(s). Skipped {skipped} (already hashed).")
print()
print("👉 Now try logging in with your normal password — it should work.")