import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
from datetime import datetime, date, timedelta
from contextlib import contextmanager
import calendar
import re
import os
import hashlib
import binascii
import hmac

# ===========================================================
# 常數設定
# ===========================================================
DB_FILE = "finance_app.db"
CATEGORIES = ["伙食", "交通", "娛樂", "購物", "居住", "醫療", "薪水", "其他"]
DEFAULT_ACCOUNT = "未指定"

CATEGORY_KEYWORD_MAP = {
    "飯": "伙食", "麵": "伙食", "午餐": "伙食", "晚餐": "伙食", "早餐": "伙食",
    "咖啡": "伙食", "飲料": "伙食", "餐廳": "伙食",
    "車": "交通", "捷運": "交通", "公車": "交通", "油": "交通", "高鐵": "交通",
    "計程車": "交通", "停車": "交通",
    "買": "購物", "網購": "購物", "衣服": "購物", "鞋": "購物", "包": "購物",
    "商城": "購物", "電商": "購物",
    "訂閱": "娛樂", "電影": "娛樂", "遊戲": "娛樂",
    "房租": "居住", "水電": "居住", "瓦斯": "居住", "房貸": "居住",
    "醫生": "醫療", "藥": "醫療", "診所": "醫療", "健檢": "醫療",
    "薪水": "薪水", "獎金": "薪水", "紅包": "薪水", "利息": "薪水",
    "補助": "薪水", "補貼": "薪水", "零用錢": "薪水", "得到": "薪水",
    "領到": "薪水", "領": "薪水", "獲得": "薪水", "收": "薪水",
    "收款": "薪水", "收回": "薪水", "收取": "薪水",
}

INCOME_KEYWORDS = ["收入", "薪水", "賺", "進帳", "加薪", "中獎", "紅包", "利息", "退款",
                    "兼職", "分紅", "獎金", "補助", "補貼", "零用錢", "得到", "領到",
                    "領", "獲得", "收", "收款", "收回", "收取"]
NEGATIVE_MARKERS = ["沒", "不", "未"]

CHINESE_NUM_MAP = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
                    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CHINESE_UNIT_MAP = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "億": 100000000}


# ===========================================================
# 資料庫存取層（統一連線管理，取代原先散落各處的 connect/close）
# ===========================================================
@contextmanager
def get_conn():
    """統一管理資料庫連線：成功時自動 commit，失敗時自動 rollback。"""
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_df(sql, params=()):
    """讀取查詢結果為 DataFrame。"""
    with get_conn() as conn:
        return pd.read_sql(sql, conn, params=params)


def user_table_df(table, user, order_by=None, limit=None):
    """讀取某使用者在指定資料表中的所有資料；未登入則回傳空表。"""
    if not user:
        return pd.DataFrame()
    sql = f"SELECT * FROM {table} WHERE user = ?"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return read_df(sql, (user,))


def adjust_balance(conn, account, user, delta):
    conn.execute(
        "UPDATE accounts SET balance = balance + ? WHERE account_name = ? AND user = ?",
        (delta, account, user),
    )


def ensure_account_exists(conn, account, user):
    exists = conn.execute(
        "SELECT 1 FROM accounts WHERE account_name = ? AND user = ?", (account, user)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (account_name, balance, user) VALUES (?, 0, ?)",
            (account, user),
        )


def next_local_id(conn, user):
    row = conn.execute(
        "SELECT COALESCE(MAX(local_id), 0) + 1 FROM transactions WHERE user = ?", (user,)
    ).fetchone()
    return row[0]


def insert_transaction(user, date_str, ttype, category, amount, account, note):
    with get_conn() as conn:
        ensure_account_exists(conn, account, user)
        local_id = next_local_id(conn, user)
        conn.execute(
            "INSERT INTO transactions (local_id, date, type, category, amount, account, note, user) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (local_id, date_str, ttype, category, amount, account, note, user),
        )
        adjust_balance(conn, account, user, -amount if ttype == "expense" else amount)
    return local_id


def delete_transaction(user, local_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, amount, account FROM transactions WHERE local_id = ? AND user = ?",
            (local_id, user),
        ).fetchone()
        if not row:
            return False
        ttype, amount, account = row
        # 還原帳戶餘額
        adjust_balance(conn, account, user, amount if ttype == "expense" else -amount)
        conn.execute("DELETE FROM transactions WHERE local_id = ? AND user = ?", (local_id, user))
        # 重新編排該使用者之 local_id，避免跳號
        conn.execute(
            "UPDATE transactions SET local_id = local_id - 1 WHERE local_id > ? AND user = ?",
            (local_id, user),
        )
        return True


def update_transaction(user, local_id, date_str, ttype, category, amount, account, note):
    with get_conn() as conn:
        old = conn.execute(
            "SELECT type, amount, account FROM transactions WHERE local_id = ? AND user = ?",
            (local_id, user),
        ).fetchone()
        if not old:
            return False
        old_type, old_amount, old_account = old
        # 還原舊交易對帳戶餘額的影響
        adjust_balance(conn, old_account, user, old_amount if old_type == "expense" else -old_amount)
        # 套用新交易的影響
        ensure_account_exists(conn, account, user)
        adjust_balance(conn, account, user, -amount if ttype == "expense" else amount)
        conn.execute(
            "UPDATE transactions SET date=?, type=?, category=?, amount=?, account=?, note=? "
            "WHERE local_id=? AND user=?",
            (date_str, ttype, category, amount, account, note, local_id, user),
        )
        return True


# ===========================================================
# 資料庫初始化與遷移
# ===========================================================
def init_db():
    with get_conn() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                local_id INTEGER, date TEXT, type TEXT, category TEXT,
                amount REAL, account TEXT, note TEXT, user TEXT,
                PRIMARY KEY (user, local_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recurring_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day_of_month INTEGER, type TEXT,
                category TEXT, amount REAL, account TEXT, note TEXT,
                last_processed_month TEXT, user TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_name TEXT, balance REAL, user TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal_name TEXT, target_amount REAL,
                current_amount REAL, target_date TEXT, user TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, password_hash TEXT, created_at TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)
        ''')

        # 自動為舊資料表補上 user 欄位
        for table in ("transactions", "recurring_bills", "accounts", "savings_goals"):
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [c[1] for c in cursor.fetchall()]
            if "user" not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN user TEXT")

        cursor.execute("PRAGMA table_info(users)")
        user_cols = [c[1] for c in cursor.fetchall()]
        if "password_hash" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "display_name" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")

        # 首次初始化清理舊資料
        cursor.execute("SELECT value FROM app_meta WHERE key = 'initialized'")
        if cursor.fetchone() is None:
            for table in ("transactions", "recurring_bills", "accounts", "savings_goals"):
                cursor.execute(f"DELETE FROM {table}")
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            )
            if cursor.fetchone():
                cursor.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('transactions', 'recurring_bills', 'accounts', 'savings_goals')"
                )
            cursor.execute("INSERT INTO app_meta (key, value) VALUES ('initialized', '1')")

        # 遷移舊 schema 至 local_id 制
        cursor.execute("PRAGMA table_info(transactions)")
        trans_cols = [c[1] for c in cursor.fetchall()]
        if "local_id" not in trans_cols or "id" in trans_cols:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS transactions_new (local_id INTEGER, date TEXT, "
                "type TEXT, category TEXT, amount REAL, account TEXT, note TEXT, user TEXT, "
                "PRIMARY KEY(user, local_id))"
            )
            cursor.execute("PRAGMA table_info(transactions)")
            trans_cols = [c[1] for c in cursor.fetchall()]

            if "id" in trans_cols:
                cursor.execute(
                    "SELECT id, date, type, category, amount, account, note, user "
                    "FROM transactions ORDER BY id ASC"
                )
                rows = cursor.fetchall()
            elif "local_id" in trans_cols:
                cursor.execute(
                    "SELECT local_id, date, type, category, amount, account, note, user "
                    "FROM transactions ORDER BY local_id ASC"
                )
                rows = cursor.fetchall()
            else:
                cursor.execute(
                    "SELECT date, type, category, amount, account, note, user "
                    "FROM transactions ORDER BY rowid ASC"
                )
                rows = [(None,) + row for row in cursor.fetchall()]

            by_user = {}
            for row in rows:
                _, date_val, type_val, cat_val, amt_val, acc_val, note_val, user_val = row
                user_val = user_val or ""
                by_user[user_val] = by_user.get(user_val, 0) + 1
                cursor.execute(
                    "INSERT OR REPLACE INTO transactions_new "
                    "(local_id, date, type, category, amount, account, note, user) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (by_user[user_val], date_val, type_val, cat_val, amt_val, acc_val, note_val, user_val),
                )

            cursor.execute("DROP TABLE IF EXISTS transactions")
            cursor.execute("ALTER TABLE transactions_new RENAME TO transactions")


init_db()


# ===========================================================
# 自動化：固定收支處理
# ===========================================================
def check_recurring_bills():
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, day_of_month, type, category, amount, account, note, "
            "last_processed_month, user FROM recurring_bills"
        )
        bills = cursor.fetchall()

        today = datetime.now()
        current_month_str = today.strftime("%Y-%m")
        current_day = today.day

        new_count = 0
        for bill_id, day_of_month, b_type, b_cat, b_amount, b_acc, b_note, last_month, b_user in bills:
            if current_day >= day_of_month and last_month != current_month_str:
                trans_date = today.strftime("%Y-%m-%d")
                local_id = next_local_id(conn, b_user)
                cursor.execute(
                    "INSERT INTO transactions (local_id, date, type, category, amount, "
                    "account, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (local_id, trans_date, b_type, b_cat, b_amount, b_acc, f"[自動] {b_note}", b_user),
                )
                cursor.execute(
                    "UPDATE recurring_bills SET last_processed_month = ? WHERE id = ?",
                    (current_month_str, bill_id),
                )
                adjust_balance(conn, b_acc, b_user, -b_amount if b_type == "expense" else b_amount)
                new_count += 1

        if new_count:
            st.toast(f"已自動生成 {new_count} 筆本月固定收支紀錄！", icon="💡")


check_recurring_bills()


# ===========================================================
# 自然語言記帳解析
# ===========================================================
def parse_date_from_text(text):
    today = datetime.now()

    for keyword, delta in (("前天", 2), ("昨天", 1), ("今天", 0)):
        if keyword in text:
            target = today - timedelta(days=delta)
            return target.strftime("%Y-%m-%d"), re.sub(keyword, "", text).strip()

    match = re.search(r"(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            target = datetime(year, month, day)
            return target.strftime("%Y-%m-%d"), text.replace(match.group(0), "").strip()
        except ValueError:
            pass

    match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        try:
            target = datetime(today.year, month, day)
            return target.strftime("%Y-%m-%d"), text.replace(match.group(0), "").strip()
        except ValueError:
            pass

    return today.strftime("%Y-%m-%d"), text.strip()


def parse_chinese_number(text):
    text = text.strip().replace("元", "").replace("塊", "").replace("整", "")
    if not text:
        return None
    if text in CHINESE_NUM_MAP:
        return float(CHINESE_NUM_MAP[text])

    total, current = 0, 0
    for ch in text:
        if ch in CHINESE_NUM_MAP:
            current = current * 10 + CHINESE_NUM_MAP[ch] if current else CHINESE_NUM_MAP[ch]
        elif ch in CHINESE_UNIT_MAP:
            unit = CHINESE_UNIT_MAP[ch]
            current = current or 1
            total += current * unit
            current = 0
    total += current
    return float(total) if total or text in {"零", "〇"} else None


def parse_chat_input(text, user=None):
    text = text.strip()

    save_match = re.search(r"存\s*(\d+)\s*(?:到|給)?\s*(.+)", text)
    if save_match:
        return {"action": "save_goal", "amount": float(save_match.group(1)), "goal": save_match.group(2).strip()}

    date_str, text = parse_date_from_text(text)

    has_income = any(k in text for k in INCOME_KEYWORDS)
    has_negative = any(neg in text for neg in NEGATIVE_MARKERS)
    trans_type = "income" if (has_income and not has_negative) else "expense"

    amount, amount_text = None, None
    amount_match = re.search(r"(\d+(?:\.\d+)?)", text)
    if amount_match:
        amount_text = amount_match.group(1)
        amount = float(amount_text)
    else:
        cn_match = re.search(r"([零一二三四五六七八九兩十百千萬億]+)", text)
        if cn_match:
            amount_text = cn_match.group(1)
            amount = parse_chinese_number(amount_text)

    if amount is None:
        return None

    cleaned = text
    if amount_text:
        cleaned = cleaned.replace(amount_text, "", 1)
    for word in ("支出", "收入", "花了", "買", "花", "元", "塊"):
        cleaned = cleaned.replace(word, "")

    df_acc = user_table_df("accounts", user) if user else read_df("SELECT account_name FROM accounts")
    account = DEFAULT_ACCOUNT
    for acc in df_acc.get("account_name", []):
        if acc in cleaned:
            account = acc
            cleaned = cleaned.replace(acc, "")
            break

    category = "其他"
    for cat in CATEGORIES:
        if cat in cleaned:
            category = cat
            cleaned = cleaned.replace(cat, "")
            break
    if category == "其他":
        for keyword, mapped in CATEGORY_KEYWORD_MAP.items():
            if keyword in cleaned:
                category = mapped
                break

    note = cleaned.strip() or f"{category} {date_str}"

    return {
        "action": "transaction", "date": date_str, "type": trans_type,
        "category": category, "amount": amount, "account": account, "note": note,
    }


# ===========================================================
# 身分驗證工具
# ===========================================================
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return binascii.hexlify(salt).decode() + ":" + binascii.hexlify(dk).decode()


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt_hex, dk_hex = stored_hash.split(":")
        salt, dk = binascii.unhexlify(salt_hex), binascii.unhexlify(dk_hex)
        new_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return hmac.compare_digest(new_dk, dk)
    except Exception:
        return False


# ===========================================================
# 共用小工具
# ===========================================================
def build_daily_summary(df):
    summary = {}
    if df.empty:
        return summary
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    for _, row in df.iterrows():
        key = row["date"].strftime("%Y-%m-%d")
        bucket = summary.setdefault(key, {"income": 0.0, "expense": 0.0, "count": 0})
        bucket[row["type"] if row["type"] in ("income", "expense") else "expense"] += float(row["amount"])
        bucket["count"] += 1
    return summary


def render_sortable_table(df, key_prefix, width="stretch"):
    if df.empty:
        st.dataframe(df, width=width)
        return df

    columns = list(df.columns)
    sort_col_key, sort_asc_key = f"{key_prefix}_sort_col", f"{key_prefix}_sort_asc"
    st.session_state.setdefault(sort_col_key, columns[0])
    st.session_state.setdefault(sort_asc_key, False)

    header_cols = st.columns(len(columns))
    for idx, col in enumerate(columns):
        label = col
        if st.session_state[sort_col_key] == col:
            label += " 🔽" if not st.session_state[sort_asc_key] else " 🔼"
        if header_cols[idx].button(label, key=f"{key_prefix}_header_{col}"):
            if st.session_state[sort_col_key] == col:
                st.session_state[sort_asc_key] = not st.session_state[sort_asc_key]
            else:
                st.session_state[sort_col_key], st.session_state[sort_asc_key] = col, False

    try:
        sorted_df = df.sort_values(
            by=st.session_state[sort_col_key], ascending=st.session_state[sort_asc_key], ignore_index=True
        )
    except Exception:
        sorted_df = df.copy()

    st.dataframe(sorted_df, width=width)
    return sorted_df


def csv_download_button(df, filename, label="📥 匯出 CSV"):
    """新增功能：讓使用者能將任何表格資料一鍵匯出成 CSV（Excel 可直接開啟）。"""
    if df.empty:
        return
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(label, data=csv_bytes, file_name=filename, mime="text/csv")


# ===========================================================
# 介面樣式（美化）
# ===========================================================
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700;900&display=swap');

html, body, [class*="css"]  { font-family: 'Noto Sans TC', sans-serif; }

.main .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1200px; }

.hero-title {
    background: linear-gradient(120deg, #6366f1 0%, #06b6d4 55%, #10b981 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    font-size: 2.3rem; font-weight: 900; margin-bottom: 0; line-height: 1.3;
}
.hero-sub { color: #6b7280; font-size: 0.95rem; margin-top: -4px; margin-bottom: 1.1rem; }

div[data-testid="stMetric"] {
    background: linear-gradient(145deg, #ffffff, #f4f6fb);
    border: 1px solid #e8eaf0; border-radius: 16px; padding: 1rem 1.1rem;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.05);
}

.stButton>button, .stFormSubmitButton>button, .stDownloadButton>button {
    border-radius: 10px; font-weight: 600; transition: all .15s ease;
}
.stButton>button:hover, .stFormSubmitButton>button:hover, .stDownloadButton>button:hover {
    transform: translateY(-1px); box-shadow: 0 4px 14px rgba(99, 102, 241, 0.25);
}

section[data-testid="stSidebar"] { background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%); }

div[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; }

.stChatMessage { border-radius: 14px; }

hr { margin: 1.3rem 0; opacity: 0.4; }

.day-cell {
    min-height: 108px; padding: 8px 10px; border-radius: 12px;
    background: #fafbfe; transition: all .15s ease;
}
.day-cell:hover { box-shadow: 0 2px 10px rgba(15,23,42,0.08); }
</style>
"""


# ===========================================================
# 頁面設定與標題
# ===========================================================
st.set_page_config(page_title="智慧記帳與個人財務管家", page_icon="💰", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
st.markdown('<p class="hero-title">💰 智慧記帳與個人財務管家</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="hero-sub">AI 對話記帳・日曆總覽・財務儀表板・存錢目標，一站打理你的錢包</p>',
    unsafe_allow_html=True,
)

# ---------------- 使用者註冊 / 登入 ----------------
st.sidebar.markdown("### 👤 使用者註冊 / 登入")
st.session_state.setdefault("user", None)

auth_mode = st.sidebar.radio("帳號操作", ("登入", "註冊"), horizontal=True)

if auth_mode == "註冊":
    with st.sidebar.form("register_form"):
        reg_account = st.text_input("帳號（登入用，英數）")
        reg_display = st.text_input("名稱（顯示用）")
        reg_pw = st.text_input("密碼", type="password")
        reg_pw2 = st.text_input("再次輸入密碼", type="password")
        submit_register = st.form_submit_button("註冊", width="stretch")

    if submit_register:
        account, display = reg_account.strip(), reg_display.strip()
        missing = [name for val, name in
                   ((account, "帳號"), (display, "名稱"), (reg_pw, "密碼"), (reg_pw2, "再次輸入密碼")) if not val]
        if missing:
            st.sidebar.error(f"請輸入：{', '.join(missing)}。")
        elif reg_pw != reg_pw2:
            st.sidebar.error("兩次密碼輸入不相符。")
        elif len(reg_pw) < 6:
            st.sidebar.error("密碼長度至少需要 6 個字元。")
        elif not re.match(r"^[A-Za-z0-9]+$", account):
            st.sidebar.error("帳號只可包含英文字母與數字。")
        else:
            with get_conn() as conn:
                account_taken = bool(conn.execute("SELECT 1 FROM users WHERE username = ?", (account,)).fetchone())
                if not account_taken:
                    pw_hash = hash_password(reg_pw)
                    conn.execute(
                        "INSERT INTO users (username, password_hash, display_name, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (account, pw_hash, display, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
            if account_taken:
                st.sidebar.error("帳號已存在，請換一個。")
            else:
                st.sidebar.success("註冊成功，已自動登入。")
                st.session_state.user = account
                st.session_state.display_name = display
                st.rerun()

elif auth_mode == "登入":
    login_account = st.sidebar.text_input("帳號（登入）")
    login_pw = st.sidebar.text_input("密碼", type="password")
    if st.sidebar.button("登入", width="stretch"):
        account = login_account.strip()
        row = read_df(
            "SELECT password_hash, display_name FROM users WHERE username = ?", (account,)
        )
        if row.empty:
            st.sidebar.error("帳號不存在，請先註冊。")
        else:
            stored, display = row.iloc[0]["password_hash"], row.iloc[0]["display_name"]
            if not stored:
                st.sidebar.error("此帳號尚未設定密碼，請重新註冊。")
            elif verify_password(stored, login_pw):
                st.sidebar.success("登入成功。")
                st.session_state.user = account
                st.session_state.display_name = display
                st.rerun()
            else:
                st.sidebar.error("密碼錯誤。")

if st.session_state.get("user"):
    disp = st.session_state.get("display_name") or ""
    acct = st.session_state.get("user")
    st.sidebar.markdown(f"目前使用者：**{disp} ({acct})**" if disp else f"目前使用者：**{acct}**")
    if st.sidebar.button("登出", width="stretch"):
        st.session_state.user = None
        st.session_state.display_name = None
        st.rerun()

st.sidebar.divider()
st.sidebar.markdown("### 🧭 功能導覽")
app_mode = st.sidebar.radio(
    "選擇模式",
    ["🤖 聊天記帳助手", "📋 交易紀錄總管", "📅 日曆檢視", "📊 財務儀表板",
     "🎯 存錢目標管理", "⚙️ 固定收支與帳戶管理"],
    label_visibility="collapsed",
)

CURRENT_USER = st.session_state.get("user")


# ===========================================================
# 頁面：🤖 聊天記帳助手
# ===========================================================
if app_mode == "🤖 聊天記帳助手":
    st.subheader("聊天機器人快速記帳")
    st.markdown(
        "輸入自然語言，系統自動幫你分類並記帳！例如："
        "*「午餐排骨飯 120 現金」*、*「昨天 95 現金」*、*「7/15 Netflix 390」*、*「存 3000 到 日本旅遊」*"
    )
    st.caption("提示：可以輸入日期、分類、帳戶與存錢目標，系統會自動解析。")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = [
            {"role": "assistant", "content": "哈囉！我是你的記帳小幫手。今天有什麼開銷或存錢計畫嗎？"}
        ]

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("請輸入記帳或存錢內容..."):
        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        if not CURRENT_USER:
            reply = "請先在側邊欄登入（使用 帳號 與 密碼），才能開始記帳。"
            parsed = None
        else:
            parsed = parse_chat_input(prompt, CURRENT_USER)

            if parsed is None:
                reply = "抱歉，我讀不太懂這筆記帳金額。請包含數字，例如：`午餐 120`"
            elif parsed["action"] == "save_goal":
                goal_name, save_amt = parsed["goal"], parsed["amount"]
                with get_conn() as conn:
                    res = conn.execute(
                        "SELECT current_amount, target_amount FROM savings_goals "
                        "WHERE goal_name = ? AND user = ?",
                        (goal_name, CURRENT_USER),
                    ).fetchone()
                    if res:
                        new_amt = res[0] + save_amt
                        conn.execute(
                            "UPDATE savings_goals SET current_amount = ? WHERE goal_name = ? AND user = ?",
                            (new_amt, goal_name, CURRENT_USER),
                        )
                        reply = (f"成功存入 **{save_amt:,.0f} 元** 到『{goal_name}』！"
                                 f"目前進度：{new_amt:,.0f} / {res[1]:,.0f} 元 ({int(new_amt / res[1] * 100)}%)")
                    else:
                        reply = f"找不到名為『{goal_name}』的存錢目標，請先至管理頁面建立。"
            elif parsed["action"] == "transaction":
                insert_transaction(
                    CURRENT_USER, parsed["date"], parsed["type"], parsed["category"],
                    parsed["amount"], parsed["account"], parsed["note"],
                )
                t_sign = "-" if parsed["type"] == "expense" else "+"
                reply = (f"✅ 記帳成功：【{parsed['category']}】{t_sign}{parsed['amount']:,.0f} 元 "
                          f"({parsed['account']}) | 備註：{parsed['note']}")
            else:
                reply = "發生未知錯誤。"

        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        with st.chat_message("assistant"):
            st.markdown(reply)
            if parsed and parsed.get("action") == "transaction":
                with st.expander("解析結果 (Parsed transaction)"):
                    st.json({k: parsed.get(k) for k in ("date", "type", "category", "amount", "account", "note")})

    st.divider()
    st.subheader("📝 近期即時帳目（最新 5 筆）")
    if not CURRENT_USER:
        st.info("請先登入以檢視個人記帳資料。")
    else:
        recent_df = user_table_df("transactions", CURRENT_USER, order_by="local_id DESC", limit=5)
        if recent_df.empty:
            st.info("目前尚無記帳資料。")
        else:
            st.dataframe(recent_df, width="stretch")
            st.caption("如需篩選、編輯或刪除交易，請至左側「📋 交易紀錄總管」頁面。")


# ===========================================================
# 頁面：📋 交易紀錄總管（新增功能：完整篩選、編輯、刪除、匯出）
# ===========================================================
elif app_mode == "📋 交易紀錄總管":
    st.subheader("📋 交易紀錄總管")

    if not CURRENT_USER:
        st.info("請先登入以檢視個人記帳資料。")
    else:
        df_all = user_table_df("transactions", CURRENT_USER, order_by="local_id DESC")
        if df_all.empty:
            st.info("目前尚無記帳資料，先到「🤖 聊天記帳助手」記一筆吧！")
        else:
            df_all["date"] = pd.to_datetime(df_all["date"])

            with st.expander("🔍 篩選條件", expanded=True):
                f1, f2, f3, f4 = st.columns(4)
                date_range = f1.date_input(
                    "日期範圍",
                    value=(df_all["date"].min().date(), df_all["date"].max().date()),
                )
                type_filter = f2.multiselect("類型", ["expense", "income"],
                                              format_func=lambda x: "支出" if x == "expense" else "收入")
                cat_filter = f3.multiselect("分類", sorted(df_all["category"].unique()))
                keyword = f4.text_input("備註關鍵字搜尋")

            filtered = df_all.copy()
            if isinstance(date_range, tuple) and len(date_range) == 2:
                start, end = date_range
                filtered = filtered[
                    (filtered["date"].dt.date >= start) & (filtered["date"].dt.date <= end)
                ]
            if type_filter:
                filtered = filtered[filtered["type"].isin(type_filter)]
            if cat_filter:
                filtered = filtered[filtered["category"].isin(cat_filter)]
            if keyword:
                filtered = filtered[filtered["note"].str.contains(keyword, case=False, na=False)]

            c1, c2, c3 = st.columns(3)
            c1.metric("篩選後筆數", len(filtered))
            c2.metric("篩選後支出", f"${filtered[filtered['type'] == 'expense']['amount'].sum():,.0f}")
            c3.metric("篩選後收入", f"${filtered[filtered['type'] == 'income']['amount'].sum():,.0f}")

            display_df = filtered.copy()
            display_df["date"] = display_df["date"].dt.strftime("%Y-%m-%d")
            st.dataframe(display_df, width="stretch")
            csv_download_button(display_df, "交易紀錄.csv")

            st.divider()
            st.markdown("### ✏️ 編輯 / 刪除交易")
            target_id = st.selectbox("選擇要編輯或刪除的交易 ID", [None] + list(df_all["local_id"]))

            if target_id:
                row = df_all[df_all["local_id"] == target_id].iloc[0]
                with st.form("edit_trans_form"):
                    ec1, ec2, ec3 = st.columns(3)
                    e_date = ec1.date_input("日期", value=row["date"].date())
                    e_type = ec2.selectbox("類型", ["expense", "income"],
                                            index=0 if row["type"] == "expense" else 1,
                                            format_func=lambda x: "支出" if x == "expense" else "收入")
                    e_cat = ec3.selectbox("分類", CATEGORIES,
                                           index=CATEGORIES.index(row["category"]) if row["category"] in CATEGORIES else 0)
                    ec4, ec5 = st.columns(2)
                    e_amount = ec4.number_input("金額", value=float(row["amount"]), step=10.0)
                    df_acc_names = user_table_df("accounts", CURRENT_USER)["account_name"].tolist()
                    acc_options = df_acc_names or [row["account"]]
                    e_account = ec5.selectbox(
                        "帳戶", acc_options,
                        index=acc_options.index(row["account"]) if row["account"] in acc_options else 0,
                    )
                    e_note = st.text_input("備註", value=row["note"])

                    col_save, col_del = st.columns(2)
                    save_btn = col_save.form_submit_button("💾 儲存更新", width="stretch")
                    del_btn = col_del.form_submit_button("🗑️ 刪除此筆", width="stretch")

                    if save_btn:
                        update_transaction(
                            CURRENT_USER, int(target_id), str(e_date), e_type, e_cat,
                            float(e_amount), e_account, e_note,
                        )
                        st.success(f"已更新交易 ID {target_id}！")
                        st.rerun()

                    if del_btn:
                        delete_transaction(CURRENT_USER, int(target_id))
                        st.success(f"已刪除交易 ID {target_id} 並同步還原帳戶餘額！")
                        st.rerun()


# ===========================================================
# 頁面：📅 日曆檢視
# ===========================================================
elif app_mode == "📅 日曆檢視":
    st.subheader("📅 月曆與週曆收支檢視")

    df_trans = user_table_df("transactions", CURRENT_USER)

    if not CURRENT_USER:
        st.info("請先登入以檢視個人日曆摘要。")
    elif df_trans.empty:
        st.info("目前尚無交易資料，先記帳後就能看到日曆摘要。")
    else:
        df_trans["date"] = pd.to_datetime(df_trans["date"])
        daily_summary = build_daily_summary(df_trans)

        today = date.today()
        first_day = date(today.year, today.month, 1)
        last_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])

        st.markdown(f"### 本月日曆：{today.year} 年 {today.month} 月")
        weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        header_cols = st.columns(7)
        for idx, label in enumerate(weekdays):
            header_cols[idx].caption(label)

        start_offset = first_day.weekday()
        cells = [None] * start_offset + [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
        while len(cells) % 7 != 0:
            cells.append(None)

        for week_index in range(len(cells) // 7):
            week_cells = cells[week_index * 7:(week_index + 1) * 7]
            cols = st.columns(7)
            for col_idx, day_value in enumerate(week_cells):
                with cols[col_idx]:
                    if day_value is None:
                        st.markdown("<div class='day-cell'></div>", unsafe_allow_html=True)
                    else:
                        summary = daily_summary.get(
                            day_value.strftime("%Y-%m-%d"), {"income": 0.0, "expense": 0.0, "count": 0}
                        )
                        is_today = day_value == today
                        border = "border: 2px solid #6366f1;" if is_today else "border: 1px solid #e5e7eb;"
                        st.markdown(
                            f"<div class='day-cell' style='{border}'>"
                            f"<strong>{day_value.day}</strong><br/>"
                            f"<span style='color:#059669'>收入 {summary['income']:,.0f}</span><br/>"
                            f"<span style='color:#dc2626'>支出 {summary['expense']:,.0f}</span><br/>"
                            f"<span style='color:#6b7280'>筆數 {summary['count']}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

        st.divider()
        st.markdown("### 本週每日總覽")
        week_start = today - timedelta(days=today.weekday())
        week_cols = st.columns(7)
        for idx, day_value in enumerate(week_start + timedelta(days=i) for i in range(7)):
            with week_cols[idx]:
                summary = daily_summary.get(day_value.strftime("%Y-%m-%d"), {"income": 0.0, "expense": 0.0, "count": 0})
                st.markdown(f"#### {day_value.month}/{day_value.day}")
                st.metric("收入", f"${summary['income']:,.0f}")
                st.metric("支出", f"${summary['expense']:,.0f}")
                st.caption(f"交易筆數：{summary['count']}")

        st.divider()
        st.markdown("### 指定日期明細")
        selected_day = st.date_input("選擇日期", value=today)
        day_df = df_trans[df_trans["date"].dt.strftime("%Y-%m-%d") == selected_day.strftime("%Y-%m-%d")].copy()
        if day_df.empty:
            st.info(f"{selected_day.strftime('%Y-%m-%d')} 沒有交易紀錄。")
        else:
            day_df = day_df[["date", "type", "category", "amount", "account", "note"]]
            day_df["date"] = day_df["date"].dt.strftime("%Y-%m-%d")
            render_sortable_table(day_df, key_prefix="day_detail", width="stretch")


# ===========================================================
# 頁面：📊 財務儀表板
# ===========================================================
elif app_mode == "📊 財務儀表板":
    st.subheader("📊 個人財務總覽與數據洞察")

    df_trans = user_table_df("transactions", CURRENT_USER)
    df_acc = user_table_df("accounts", CURRENT_USER)

    if df_trans.empty:
        st.info("請先至聊天頁面記錄一些收支，即可在這裡看到豐富的視覺化圖表！")
    else:
        df_trans["date"] = pd.to_datetime(df_trans["date"])

        # 新增功能：月份選擇器，不再侷限於「當月」
        available_months = sorted(df_trans["date"].dt.strftime("%Y-%m").unique(), reverse=True)
        current_month_str = datetime.now().strftime("%Y-%m")
        default_idx = available_months.index(current_month_str) if current_month_str in available_months else 0
        selected_month = st.selectbox("📆 選擇檢視月份", available_months, index=default_idx)

        df_month = df_trans[df_trans["date"].dt.strftime("%Y-%m") == selected_month]
        total_expense = df_month[df_month["type"] == "expense"]["amount"].sum()
        total_income = df_month[df_month["type"] == "income"]["amount"].sum()
        net_worth = df_acc["balance"].sum()

        monthly_budget = st.sidebar.number_input("設定本月總預算 (元)", value=0, step=1000)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric(f"{selected_month} 總支出", f"${total_expense:,.0f}")
        col2.metric(f"{selected_month} 總收入", f"${total_income:,.0f}")
        col3.metric("總資產淨值", f"${net_worth:,.0f}")

        year, month = map(int, selected_month.split("-"))
        days_in_month = calendar.monthrange(year, month)[1]
        today = datetime.now()
        day_today = today.day if selected_month == current_month_str else days_in_month
        days_left = max(1, days_in_month - day_today)
        safe_budget_left = max(0, monthly_budget - total_expense)
        safe_daily = safe_budget_left / days_left
        col4.metric("今日可用額度 (Safe-to-spend)", f"${safe_daily:,.0f} /天",
                    delta=f"剩餘預算 ${safe_budget_left:,.0f}")

        df_exp = df_month[df_month["type"] == "expense"]
        avg_daily = predicted_expense = 0
        if not df_exp.empty:
            df_daily = df_exp.groupby(df_exp["date"].dt.date)["amount"].sum().reset_index()
            avg_daily = df_daily["amount"].mean()
            predicted_expense = avg_daily * days_in_month

        if monthly_budget > 0 and total_expense >= monthly_budget:
            st.error("⚠️ 本月已超過預算！請優先檢視支出與調整花費。")
        elif monthly_budget > 0 and total_expense >= monthly_budget * 0.8:
            st.warning("⚠️ 支出已達 80% 預算，請注意剩餘資金。")
        elif monthly_budget > 0:
            st.success("目前預算仍在安全範圍內。")

        st.markdown(f"*本月平均每日支出約 ${avg_daily:,.0f}，依此速度預計本月支出 ${predicted_expense:,.0f}。*")
        st.divider()

        st.markdown("### 🎯 本月預算消耗進度")
        budget_pct = min(1.0, total_expense / monthly_budget) if monthly_budget > 0 else 0.0
        st.progress(budget_pct, text=f"已花費 {total_expense:,.0f} / 預算 {monthly_budget:,.0f} 元 ({int(budget_pct * 100)}%)")

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("### 🥧 本月分類支出占比")
            if not df_exp.empty:
                fig_pie = px.pie(df_exp, names="category", values="amount", hole=0.4,
                                  color_discrete_sequence=px.colors.qualitative.Pastel)
                st.plotly_chart(fig_pie, width="stretch")
            else:
                st.info("本月尚無支出紀錄。")

        with col_b:
            st.markdown("### 📈 每日累積支出趨勢")
            if not df_exp.empty:
                df_daily = df_exp.groupby(df_exp["date"].dt.date)["amount"].sum().reset_index()
                df_daily["cumulative"] = df_daily["amount"].cumsum()
                fig_line = px.line(df_daily, x="date", y="cumulative", markers=True, title="本月累積花費曲線")
                st.plotly_chart(fig_line, width="stretch")
            else:
                st.info("本月尚無支出紀錄。")

        st.divider()
        st.markdown("### 💳 各帳戶資產分佈")
        if not df_acc.empty:
            fig_bar = px.bar(df_acc, x="account_name", y="balance", color="account_name",
                              text="balance", title="各帳戶餘額")
            st.plotly_chart(fig_bar, width="stretch")
        else:
            st.info("尚未建立任何帳戶。")

        st.divider()
        st.markdown("### 📥 匯出本月資料")
        csv_download_button(df_month, f"{selected_month}_財務資料.csv", label="📥 匯出本月交易 CSV")


# ===========================================================
# 頁面：🎯 存錢目標管理
# ===========================================================
elif app_mode == "🎯 存錢目標管理":
    st.subheader("🎯 存錢目標進度管理")

    df_goals = user_table_df("savings_goals", CURRENT_USER)
    today = datetime.now()

    col1, col2 = st.columns([2, 1])

    with col1:
        if df_goals.empty:
            st.info("目前尚無存錢目標，請在右側新增一個目標。")
        for _, row in df_goals.iterrows():
            target, current = row["target_amount"], row["current_amount"]
            pct = min(1.0, current / target) if target > 0 else 1.0
            remaining = max(0, target - current)
            try:
                due_date = datetime.strptime(row["target_date"], "%Y-%m-%d")
            except Exception:
                due_date = today
            days_left = max(0, (due_date - today).days)
            months_left = max(1, round(days_left / 30)) if days_left > 0 else 1
            monthly_needed = remaining / months_left if remaining > 0 else 0

            if remaining <= 0:
                status_text = "✅ 已達成目標！恭喜你。"
            elif days_left == 0:
                status_text = f"距離目標日今天，尚需存 {remaining:,.0f} 元。"
            else:
                status_text = f"剩餘 {remaining:,.0f} 元，距離目標日還有 {days_left} 天，建議每月存 {monthly_needed:,.0f} 元。"

            st.markdown(f"#### 🏆 {row['goal_name']}")
            st.progress(pct, text=f"進度：${current:,.0f} / ${target:,.0f} ({int(pct * 100)}%) | 預定目標日：{row['target_date']}")
            st.caption(status_text)
            st.markdown("---")

    with col2:
        st.markdown("### ➕ 新增存錢目標")
        with st.form("new_goal_form"):
            new_name = st.text_input("目標名稱")
            new_target = st.number_input("目標金額", value=0, step=5000)
            new_initial = st.number_input("初始已存金額", value=0, step=1000)
            new_date = st.date_input("預計達標日期", value=date(today.year + 1, 1, 1))
            submit_goal = st.form_submit_button("建立目標", width="stretch")

            if submit_goal and new_name:
                try:
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT INTO savings_goals (goal_name, target_amount, current_amount, "
                            "target_date, user) VALUES (?, ?, ?, ?, ?)",
                            (new_name, new_target, new_initial, str(new_date), CURRENT_USER),
                        )
                    st.success(f"成功建立存錢目標：{new_name}！")
                    st.rerun()
                except Exception as e:
                    st.error(f"建立失敗（可能名稱重複）：{e}")

        if not df_goals.empty:
            st.markdown("### ✏️ 更新或刪除目標")
            goal_names = df_goals["goal_name"].tolist()
            with st.form("edit_goal_form"):
                selected_goal = st.selectbox("選擇目標", goal_names, key="edit_goal_select")
                current_value = int(df_goals.loc[df_goals["goal_name"] == selected_goal, "current_amount"].iloc[0])
                updated_current = st.number_input("目前已存金額", value=current_value, step=1000)
                if st.form_submit_button("更新目標進度", width="stretch"):
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE savings_goals SET current_amount = ? WHERE goal_name = ? AND user = ?",
                            (updated_current, selected_goal, CURRENT_USER),
                        )
                    st.success(f"已更新『{selected_goal}』的已存金額。")
                    st.rerun()

            with st.form("delete_goal_form"):
                selected_delete = st.selectbox("選擇要刪除的目標", goal_names, key="delete_goal_select")
                if st.form_submit_button("刪除目標", width="stretch"):
                    with get_conn() as conn:
                        conn.execute(
                            "DELETE FROM savings_goals WHERE goal_name = ? AND user = ?",
                            (selected_delete, CURRENT_USER),
                        )
                    st.success(f"已刪除存錢目標：{selected_delete}")
                    st.rerun()


# ===========================================================
# 頁面：⚙️ 固定收支與帳戶管理
# ===========================================================
elif app_mode == "⚙️ 固定收支與帳戶管理":
    st.subheader("⚙️ 固定收支與資產帳戶設定")

    tab1, tab2 = st.tabs(["固定收支設定", "資產帳戶設定"])

    with tab1:
        st.markdown("在此設定每月自動扣款或入帳的固定項目（如房租、訂閱費、固定薪資）。")
        df_bills = user_table_df("recurring_bills", CURRENT_USER)
        df_acc_list = user_table_df("accounts", CURRENT_USER)

        current_month_str = datetime.now().strftime("%Y-%m")
        if not df_bills.empty:
            df_bills["本月已處理"] = df_bills["last_processed_month"] == current_month_str
        render_sortable_table(df_bills, key_prefix="bills_list", width="stretch")

        if not df_bills.empty:
            bill_options = [
                f"{row['id']}: {row['note']} ({'收入' if row['type'] == 'income' else '支出'})"
                for _, row in df_bills.iterrows()
            ]
            with st.form("delete_bill_form"):
                selected_bill = st.selectbox("選擇要刪除的固定收支", bill_options, key="delete_bill_select")
                if st.form_submit_button("刪除固定收支", width="stretch"):
                    bill_id = int(selected_bill.split(":")[0])
                    with get_conn() as conn:
                        conn.execute(
                            "DELETE FROM recurring_bills WHERE id = ? AND user = ?", (bill_id, CURRENT_USER)
                        )
                    st.success("已刪除固定收支項目。")
                    st.rerun()

        with st.form("add_bill_form"):
            st.markdown("#### 新增固定收支")
            b_day = st.number_input("每月扣款日 (1-31)", min_value=1, max_value=31, value=1)
            b_type = st.selectbox("類型", ["expense", "income"], format_func=lambda x: "支出" if x == "expense" else "收入")
            b_cat = st.selectbox("分類", CATEGORIES)
            b_amt = st.number_input("金額", value=0, step=100)
            b_acc = st.selectbox("扣款/入帳帳戶", df_acc_list["account_name"] if not df_acc_list.empty else [])
            b_note = st.text_input("名稱備註 (如: 房租、Spotify)")

            if st.form_submit_button("新增固定收支", width="stretch"):
                if not b_acc:
                    st.error("請先在「資產帳戶設定」建立至少一個帳戶。")
                else:
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT INTO recurring_bills (day_of_month, type, category, amount, "
                            "account, note, last_processed_month, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (b_day, b_type, b_cat, b_amt, b_acc, b_note, "", CURRENT_USER),
                        )
                    st.success("固定收支新增成功！")
                    st.rerun()

    with tab2:
        st.markdown("在此管理你的資產帳戶（現金、銀行帳戶、悠遊卡等）。")
        df_accounts = user_table_df("accounts", CURRENT_USER)
        render_sortable_table(df_accounts, key_prefix="account_list", width="stretch")
        if not df_accounts.empty:
            csv_download_button(df_accounts, "帳戶列表.csv")

        with st.form("add_account_form"):
            st.markdown("#### 新增帳戶")
            acc_name = st.text_input("帳戶名稱 (例如: 富邦銀行)")
            acc_bal = st.number_input("初始餘額", value=0, step=1000)

            if st.form_submit_button("新增帳戶", width="stretch") and acc_name:
                try:
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT INTO accounts (account_name, balance, user) VALUES (?, ?, ?)",
                            (acc_name, acc_bal, CURRENT_USER),
                        )
                    st.success(f"成功新增帳戶：{acc_name}")
                    st.rerun()
                except Exception as e:
                    st.error(f"新增失敗（帳戶名稱可能重複）：{e}")

        if not df_accounts.empty:
            st.markdown("### ✏️ 更新或刪除帳戶")
            account_names = df_accounts["account_name"].tolist()
            selected_account = st.selectbox("選擇帳戶", account_names, key="edit_account_select")
            selected_balance = float(df_accounts.loc[df_accounts["account_name"] == selected_account, "balance"].iloc[0])

            with st.form("edit_account_form"):
                new_balance = st.number_input("調整餘額", value=int(selected_balance), step=1000)
                if st.form_submit_button("更新帳戶餘額", width="stretch"):
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE accounts SET balance = ? WHERE account_name = ? AND user = ?",
                            (new_balance, selected_account, CURRENT_USER),
                        )
                    st.success(f"已更新帳戶『{selected_account}』的餘額。")
                    st.rerun()

            with st.form("delete_account_form"):
                delete_account = st.selectbox("選擇要刪除的帳戶", account_names, key="delete_account_select")
                if st.form_submit_button("刪除帳戶", width="stretch"):
                    with get_conn() as conn:
                        tx_count = conn.execute(
                            "SELECT COUNT(*) FROM transactions WHERE account = ? AND user = ?",
                            (delete_account, CURRENT_USER),
                        ).fetchone()[0]
                        bill_count = conn.execute(
                            "SELECT COUNT(*) FROM recurring_bills WHERE account = ? AND user = ?",
                            (delete_account, CURRENT_USER),
                        ).fetchone()[0]
                        can_delete = tx_count == 0 and bill_count == 0
                        if can_delete:
                            conn.execute(
                                "DELETE FROM accounts WHERE account_name = ? AND user = ?",
                                (delete_account, CURRENT_USER),
                            )
                    if can_delete:
                        st.success(f"已刪除帳戶：{delete_account}")
                        st.rerun()
                    else:
                        st.error("無法刪除：該帳戶仍有交易或固定收支紀錄。請先移除相關紀錄後再試。")
