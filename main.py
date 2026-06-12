import os
import io
import json
import calendar
from datetime import datetime
from typing import Dict

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
import google.generativeai as genai

from database import get_conn, setup_database

# ---------------------------------------------------------------------------
# App & CORS
# ---------------------------------------------------------------------------
app = FastAPI(title="Safe-To-Spend AI Engine")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "*"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Gemini setup  (key comes from env, never hardcoded)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is not set!")

genai.configure(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------
setup_database()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class Transaction(BaseModel):
    description: str
    amount: float

class SetupData(BaseModel):
    income: float
    bills: float

class SettingsUpdate(BaseModel):
    income: float = None
    bills: float = None

class Goal(BaseModel):
    name: str
    target: float
    current: float
    emoji: str
    months: int

class GoalFund(BaseModel):
    amount: float

class NewTransaction(BaseModel):
    desc: str
    amount: float
    cat: str
    type: str
    date: str

class CustomCategory(BaseModel):
    name: str
    emoji: str

class NewBill(BaseModel):
    name: str
    amount: float

class TargetGoals(BaseModel):
    targets: Dict[str, float]

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------
@app.post("/api/goals")
def add_goal(goal: Goal):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO goals (name, target, current, emoji, months) VALUES (%s,%s,%s,%s,%s)",
        (goal.name, goal.target, goal.current, goal.emoji, goal.months)
    )
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success"}

@app.get("/api/goals")
def get_goals():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, target, current, emoji, months FROM goals")
    goals = [dict(row) for row in cursor.fetchall()]
    cursor.close(); conn.close()
    return goals

@app.put("/api/goals/{goal_id}")
def fund_goal(goal_id: int, fund: GoalFund):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE goals SET current = current + %s WHERE id = %s", (fund.amount, goal_id))
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success"}

@app.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM goals WHERE id = %s", (goal_id,))
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success"}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
@app.post("/api/setup")
async def complete_setup(data: SetupData):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET monthly_income=%s, fixed_bills=%s, setup_complete=TRUE WHERE id=1",
        (data.income, data.bills)
    )
    conn.commit(); cursor.close(); conn.close()
    return {"message": "Setup complete! Vault updated."}

# ---------------------------------------------------------------------------
# User data / dashboard
# ---------------------------------------------------------------------------
@app.get("/api/user-data")
async def get_user_data():
    conn = get_conn()
    cursor = conn.cursor()

    income, bills = 25000, 10000
    try:
        cursor.execute("SELECT income, bills FROM settings WHERE id = 1")
        row = cursor.fetchone()
        if row:
            income, bills = row["income"], row["bills"]
    except Exception:
        pass

    try:
        cursor.execute("SELECT SUM(amount) as s FROM transactions WHERE type = 'expense'")
        r = cursor.fetchone()
        spent = r["s"] or 0
    except Exception:
        spent = 0

    try:
        cursor.execute("SELECT SUM(amount) as s FROM transactions WHERE type = 'income'")
        r = cursor.fetchone()
        extra_income = r["s"] or 0
    except Exception:
        extra_income = 0

    today = datetime.today()
    _, days_in_month = calendar.monthrange(today.year, today.month)
    days_left = days_in_month - today.day + 1
    remaining_pool = (income + extra_income) - bills - spent
    safe_to_spend_today = remaining_pool / days_left if days_left > 0 else remaining_pool

    cursor.execute(
        "SELECT id, description, amount, category, type, date FROM transactions ORDER BY id DESC"
    )
    history = [
        {
            "id": row["id"],
            "description": row["description"],
            "amount": row["amount"],
            "category": row["category"],
            "type": row["type"] or "expense",
            "date": row["date"] or "",
        }
        for row in cursor.fetchall()
    ]

    cursor.close(); conn.close()
    return {
        "setup_complete": True,
        "safe_to_spend_today": round(safe_to_spend_today, 2),
        "total_spent": spent,
        "history": history,
        "income": income,
        "bills": bills,
        "extra_income": extra_income,
    }

# ---------------------------------------------------------------------------
# Analyze (legacy endpoint)
# ---------------------------------------------------------------------------
@app.post("/analyze")
async def analyze_transaction(tx: Transaction):
    system_instruction = """
    You are an expert Indian financial transaction analyzer.
    Analyze the raw UPI/Bank transaction description and amount.
    Return ONLY a valid JSON object with keys:
    - "merchant": clean business name
    - "category": broad category
    - "is_essential": boolean
    - "emotional_trigger": 2-word spending trigger
    """
    prompt = f"Transaction string: {tx.description} | Amount: ₹{tx.amount}"
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        ai_insight = json.loads(response.text)

        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT weekly_budget, total_spent FROM users WHERE id=1")
        user_data = cursor.fetchone()
        current_budget = user_data["weekly_budget"]
        spent_so_far   = user_data["total_spent"]
        new_total_spent = spent_so_far + tx.amount
        safe_left = current_budget - new_total_spent

        cursor.execute("UPDATE users SET total_spent=%s WHERE id=1", (new_total_spent,))
        cursor.execute(
            "INSERT INTO transactions (description, amount, category, emotional_trigger) VALUES (%s,%s,%s,%s)",
            (ai_insight["merchant"], tx.amount, ai_insight["category"], ai_insight["emotional_trigger"])
        )
        conn.commit(); cursor.close(); conn.close()

        return {"transaction_insight": ai_insight, "budget_update": {"safe_to_spend_left": safe_left}}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.post("/api/settings")
def update_settings(settings: SettingsUpdate):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, income, bills FROM settings WHERE id=1")
    existing = cursor.fetchone()
    if existing is None:
        cursor.execute(
            "INSERT INTO settings (id, income, bills) VALUES (1, %s, %s)",
            (settings.income or 0, settings.bills or 0)
        )
    else:
        new_income = settings.income if settings.income is not None else existing["income"]
        new_bills  = settings.bills  if settings.bills  is not None else existing["bills"]
        cursor.execute("UPDATE settings SET income=%s, bills=%s WHERE id=1", (new_income, new_bills))
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success", "message": "Vault settings updated!"}

# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
@app.post("/api/transactions")
def add_transaction(txn: NewTransaction):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO transactions (description, amount, category, type, date) VALUES (%s,%s,%s,%s,%s)",
        (txn.desc, txn.amount, txn.cat, txn.type, txn.date)
    )
    if txn.type == "expense":
        cursor.execute("UPDATE users SET total_spent = total_spent + %s WHERE id=1", (txn.amount,))
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success", "message": "Expense saved to Vault!"}

@app.delete("/api/transactions/{tx_id}")
async def delete_transaction(tx_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT amount FROM transactions WHERE id=%s", (tx_id,))
    tx = cursor.fetchone()
    if not tx:
        cursor.close(); conn.close()
        return {"error": "Transaction not found"}
    cursor.execute("DELETE FROM transactions WHERE id=%s", (tx_id,))
    cursor.execute("UPDATE users SET total_spent = total_spent - %s WHERE id=1", (tx["amount"],))
    conn.commit(); cursor.close(); conn.close()
    return {"message": "Transaction deleted and budget refunded!"}

@app.put("/api/transactions/{tx_id}")
def update_transaction(tx_id: int, txn: NewTransaction):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE transactions SET description=%s, amount=%s, category=%s, type=%s, date=%s WHERE id=%s",
        (txn.desc, txn.amount, txn.cat, txn.type, txn.date, tx_id)
    )
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success", "message": "Vault successfully updated!"}

# ---------------------------------------------------------------------------
# AI Coach
# ---------------------------------------------------------------------------
@app.get("/api/coach")
def get_ai_coach():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        current_month = datetime.now().strftime("%Y-%m")
        cursor.execute(
            "SELECT description, amount, category FROM transactions WHERE type='expense' AND date LIKE %s",
            (f"{current_month}%",)
        )
        expenses = cursor.fetchall()
        cursor.close(); conn.close()

        if not expenses:
            return {"advice": "You haven't spent anything this month! Keep it up."}

        expense_list = "\n".join([f"- {r['description']}: ₹{r['amount']} ({r['category']})" for r in expenses])
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
You are a brutally honest, slightly sarcastic but helpful financial coach.
Here are my expenses for this month:
{expense_list}

Give me a 2-3 sentence financial roast/advice. Short, punchy, highlight waste. No markdown bolding.
"""
        response = model.generate_content(prompt)
        return {"advice": response.text.strip()}
    except Exception as e:
        return {"advice": "AI brain crashed. Try again!"}

# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.get("/api/admin-data")
def get_admin_data():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT category, emotional_trigger, amount FROM transactions")
    rows = cursor.fetchall()
    cursor.close(); conn.close()

    categories_math, emotions_math = {}, {}
    for row in rows:
        cat = row["category"]; emo = row["emotional_trigger"]; amt = row["amount"]
        if cat: categories_math[cat] = categories_math.get(cat, 0) + (amt or 0)
        if emo: emotions_math[emo]   = emotions_math.get(emo, 0)   + (amt or 0)
    return {"categories": categories_math, "emotions": emotions_math}

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
@app.post("/api/targets")
def update_targets(goals: TargetGoals):
    conn = get_conn()
    cursor = conn.cursor()
    for cat, amt in goals.targets.items():
        cursor.execute(
            "INSERT INTO targets (category, amount) VALUES (%s,%s) ON CONFLICT (category) DO UPDATE SET amount=EXCLUDED.amount",
            (cat, amt)
        )
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success", "message": "Targets locked in!"}

@app.get("/api/targets")
def get_targets():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT category, amount FROM targets")
    targets = {row["category"]: row["amount"] for row in cursor.fetchall()}
    cursor.close(); conn.close()
    return targets

# ---------------------------------------------------------------------------
# Scan receipt (Vision AI)
# ---------------------------------------------------------------------------
@app.post("/api/scan-receipt")
async def scan_receipt(file: UploadFile = File(...)):
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = """
Analyze this payment screenshot, UPI SMS, or bank message.
Extract:
1. Merchant Name — real business name, not UPI ID. Strip @ybl/@icici etc.
2. Total Amount — number only, no currency symbol.
3. Category — one of: Food, Transport, Shopping, Entertainment, Fixed Bills, Health, Education, Other

Respond ONLY with raw JSON, no markdown. Example:
{"merchant": "Swiggy", "amount": 240, "category": "Food"}
"""
        response = model.generate_content([prompt, image])
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    except Exception as e:
        return {"error": "Failed to read image"}

# ---------------------------------------------------------------------------
# Custom categories
# ---------------------------------------------------------------------------
@app.post("/api/categories")
def add_category(cat: CustomCategory):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO custom_categories (name, emoji) VALUES (%s,%s) ON CONFLICT (name) DO UPDATE SET emoji=EXCLUDED.emoji",
        (cat.name, cat.emoji)
    )
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success"}

@app.get("/api/categories")
def get_categories():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name, emoji FROM custom_categories")
    cats = [dict(row) for row in cursor.fetchall()]
    cursor.close(); conn.close()
    return cats

# ---------------------------------------------------------------------------
# Fixed bills
# ---------------------------------------------------------------------------
@app.post("/api/bills")
def add_bill(bill: NewBill):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO fixed_bills (name, amount) VALUES (%s,%s)", (bill.name, bill.amount))
    conn.commit(); cursor.close(); conn.close()
    return {"status": "success"}

@app.get("/api/bills")
def get_bills():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, amount FROM fixed_bills")
    bills = [dict(row) for row in cursor.fetchall()]
    cursor.close(); conn.close()
    return bills

@app.delete("/api/bills/{bill_id}")
def delete_bill(bill_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM fixed_bills WHERE id=%s", (bill_id,))
    conn.commit(); cursor.close(); conn.close()
    return {"status": "deleted"}
