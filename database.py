import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set!")

def get_conn():
    """Return a new Postgres connection."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def setup_database():
    """Create all tables on startup if they don't exist."""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            weekly_budget REAL DEFAULT 3000,
            total_spent REAL DEFAULT 0,
            monthly_income REAL DEFAULT 25000,
            fixed_bills REAL DEFAULT 10000,
            setup_complete BOOLEAN DEFAULT FALSE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            description TEXT,
            amount REAL,
            category TEXT,
            emotional_trigger TEXT,
            type TEXT DEFAULT 'expense',
            date TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            id SERIAL PRIMARY KEY,
            name TEXT,
            target REAL,
            current REAL DEFAULT 0,
            emoji TEXT,
            months INTEGER DEFAULT 3
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            income REAL DEFAULT 25000,
            bills REAL DEFAULT 10000
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS targets (
            category TEXT PRIMARY KEY,
            amount REAL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_categories (
            name TEXT PRIMARY KEY,
            emoji TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fixed_bills (
            id SERIAL PRIMARY KEY,
            name TEXT,
            amount REAL
        )
    ''')

    # Seed the users row if empty
    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    row = cursor.fetchone()
    if row["cnt"] == 0:
        cursor.execute("INSERT INTO users (weekly_budget, total_spent) VALUES (3000, 0)")

    conn.commit()
    cursor.close()
    conn.close()
