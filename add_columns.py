import sqlite3

def add_missing_columns():
    conn = sqlite3.connect("sacco.db")
    cursor = conn.cursor()
    
    try:
        # Get existing columns
        cursor.execute("PRAGMA table_info(loans)")
        existing_columns = [col[1] for col in cursor.fetchall()]
        
        print("📋 Existing columns in loans table:")
        for col in existing_columns:
            print(f"   - {col}")
        
        print("\n" + "=" * 50)
        
        # Columns to add
        columns_to_add = {
            'rejected_by': 'TEXT',
            'approved_by': 'TEXT',
            'approved_by_role': 'TEXT',
            'disbursed_by': 'TEXT',
            'disbursed_by_role': 'TEXT'
        }
        
        added = []
        already_exist = []
        
        for col_name, col_type in columns_to_add.items():
            if col_name not in existing_columns:
                print(f"➕ Adding column: {col_name} ({col_type})")
                cursor.execute(f"ALTER TABLE loans ADD COLUMN {col_name} {col_type}")
                added.append(col_name)
            else:
                print(f"✅ Column already exists: {col_name}")
                already_exist.append(col_name)
        
        conn.commit()
        
        print("\n" + "=" * 50)
        print(f"✅ Added {len(added)} columns: {', '.join(added) if added else 'None'}")
        print(f"✅ Already existed: {', '.join(already_exist) if already_exist else 'None'}")
        
        # Verify all columns now exist
        cursor.execute("PRAGMA table_info(loans)")
        all_columns = cursor.fetchall()
        print("\n📋 Updated columns in loans table:")
        for col in all_columns:
            print(f"   - {col[1]} ({col[2]})")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        conn.rollback()
    finally:
        conn.close()
        print("\n✅ Done!")

if __name__ == "__main__":
    print("=" * 60)
    print("🔧 ADDING MISSING COLUMNS TO LOANS TABLE")
    print("=" * 60)
    add_missing_columns()