from db import get_conn

conn = get_conn()
for table in ("items", "emails", "proposals", "events"):
    print(f"\n--- {table} ---")
    for row in conn.execute(f"SELECT * FROM {table}"):
        print(dict(row))