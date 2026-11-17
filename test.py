import csv
import sqlite3
from pathlib import Path
from typing import List, Tuple, Optional

# --- CONFIG: set your workspace path here ---
WORKSPACE_PATH = Path(
    r"C:\Users\seer\AppData\Local\Packages\Shapr3D.Shapr3D_j403pzjkqtqbj\TempState\aa92a1dd-177d-4ed5-b718-23f6c4f8eff5\workspace"
)
OUT_CSV = WORKSPACE_PATH.parent / "workspace_text_dump.csv"
# -------------------------------------------


def get_tables(conn: sqlite3.Connection) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [r[0] for r in cur.fetchall()]


def get_columns(conn: sqlite3.Connection, table: str) -> List[Tuple[str, Optional[str]]]:
    """Return [(name, declared_type), ...] for a table."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
    return [(r[1], (r[2] or "").upper()) for r in cur.fetchall()]


def value_to_text(val) -> Optional[str]:
    """Convert a DB value to a printable string (TEXT or BLOB -> str); otherwise None."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, (bytes, bytearray, memoryview)):
        try:
            # Best-effort UTF-8 decode; ignore undecodable bytes
            return bytes(val).decode("utf-8", errors="ignore")
        except Exception:
            return None
    # Ignore numbers and other types
    return None


def should_keep(s: str) -> bool:
    """Filter: non-empty after strip, and must NOT contain '{' or '}'."""
    s2 = s.strip()
    if not s2:
        return False
    if "{" in s2 or "}" in s2:
        return False
    return True


def main() -> None:
    if not WORKSPACE_PATH.exists():
        raise FileNotFoundError(f"Workspace not found: {WORKSPACE_PATH}")

    conn = sqlite3.connect(str(WORKSPACE_PATH))
    conn.row_factory = sqlite3.Row

    rows_written = 0
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["TableName", "ColName", "Value"])

        for table in get_tables(conn):
            cols = get_columns(conn, table)
            if not cols:
                continue

            # Pull all rows; we'll inspect column values one-by-one
            try:
                cur = conn.execute(f"SELECT * FROM {table}")
            except sqlite3.DatabaseError:
                continue

            for dbrow in cur:
                for colname, decltype in cols:
                    try:
                        val = dbrow[colname]
                    except Exception:
                        continue
                    text = value_to_text(val)
                    if text is None:
                        continue
                    if should_keep(text):
                        writer.writerow([table, colname, text.strip()])
                        rows_written += 1

    conn.close()
    print(f"Done. Wrote {rows_written} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
