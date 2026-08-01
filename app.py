import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date, timedelta
import calendar
import re
import os
import hashlib
import binascii
import hmac

# ---------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------
DB_FILE = "finance_app.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 1. Transactions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            local_id INTEGER,
            date TEXT,
            type TEXT,
            category TEXT,
            amount REAL,
            account TEXT,
            note TEXT,
            user TEXT,
            PRIMARY KEY (user, local_id)
        )
    ''')
    
    # 2. Recurring bills table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recurring_bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_month INTEGER,
            type TEXT,
            category TEXT,
            amount REAL,
            account TEXT,
            note TEXT,
            last_processed_month TEXT,
            user TEXT
        )
    ''')
    
    # 3. Accounts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT,
            balance REAL,
            user TEXT
        )
    ''')
    
    # 4. Savings goals table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_name TEXT,
            target_amount REAL,
            current_amount REAL,
            target_date TEXT,
            user TEXT
        )
    ''')
    
    # 5. Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT,
            created_at TEXT
        )
    ''')

    # 6. App metadata table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # --- 關鍵改善 1：自動檢查並為舊資料表補上 'user' 欄位（避免 Migration 報錯） ---
    tables = ['transactions', 'recurring_bills', 'accounts', 'savings_goals']
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [column[1] for column in cursor.fetchall()]
        if 'user' not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN user TEXT")
    # 若舊版 users table 沒有 password_hash，則補上
    cursor.execute("PRAGMA table_info(users)")
    user_cols = [c[1] for c in cursor.fetchall()]
    if 'password_hash' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if 'display_name' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")

    # --- 關鍵改善 2：首次初始化清理舊資料邏輯 ---
    cursor.execute("SELECT value FROM app_meta WHERE key = 'initialized'")
    initialized = cursor.fetchone()
    if initialized is None:
        cursor.execute("DELETE FROM transactions")
        cursor.execute("DELETE FROM recurring_bills")
        cursor.execute("DELETE FROM accounts")
        cursor.execute("DELETE FROM savings_goals")
        
        # 安全重置 AUTOINCREMENT 序號（避免 sqlite_sequence 不存在時噴錯）
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
        if cursor.fetchone():
            cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('transactions', 'recurring_bills', 'accounts', 'savings_goals')")
            
        cursor.execute("INSERT INTO app_meta (key, value) VALUES ('initialized', '1')")

    # --- 新增 local_id 欄位：每個使用者的交易序號（1-based） ---
    cursor.execute("PRAGMA table_info(transactions)")
    trans_cols = [c[1] for c in cursor.fetchall()]
    if 'local_id' not in trans_cols or 'id' in trans_cols:
        # 若遺留舊 schema，先將資料搬到暫存表，去除 id 欄並保留 local_id 與 user
        cursor.execute("CREATE TABLE IF NOT EXISTS transactions_new (local_id INTEGER, date TEXT, type TEXT, category TEXT, amount REAL, account TEXT, note TEXT, user TEXT, PRIMARY KEY(user, local_id))")
        cursor.execute("PRAGMA table_info(transactions)")
        trans_cols = [c[1] for c in cursor.fetchall()]

        if 'id' in trans_cols:
            cursor.execute("SELECT id, date, type, category, amount, account, note, user FROM transactions ORDER BY id ASC")
            rows = cursor.fetchall()
            rows = [(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]) for row in rows]
        elif 'local_id' in trans_cols:
            cursor.execute("SELECT local_id, date, type, category, amount, account, note, user FROM transactions ORDER BY local_id ASC")
            rows = cursor.fetchall()
        else:
            cursor.execute("SELECT date, type, category, amount, account, note, user FROM transactions ORDER BY rowid ASC")
            rows = cursor.fetchall()
            rows = [(None, row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in rows]

        # 重建 local_id，保證每個使用者從 1 開始連續編號
        by_user = {}
        for row in rows:
            _, date_val, type_val, cat_val, amt_val, acc_val, note_val, user_val = row
            user_val = user_val or ''
            if user_val not in by_user:
                by_user[user_val] = 0
            by_user[user_val] += 1
            next_local = by_user[user_val]
            cursor.execute(
                "INSERT OR REPLACE INTO transactions_new (local_id, date, type, category, amount, account, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (next_local, date_val, type_val, cat_val, amt_val, acc_val, note_val, user_val)
            )

        cursor.execute("DROP TABLE IF EXISTS transactions")
        cursor.execute("ALTER TABLE transactions_new RENAME TO transactions")

    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------
# HELPER FUNCTIONS & AUTOMATION
# ---------------------------------------------------------
def check_recurring_bills():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, day_of_month, type, category, amount, account, note, last_processed_month, user FROM recurring_bills")
    bills = cursor.fetchall()
    
    today = datetime.now()
    current_month_str = today.strftime("%Y-%m")
    current_day = today.day
    
    new_records = []
    for bill in bills:
        bill_id, day_of_month, b_type, b_cat, b_amount, b_acc, b_note, last_month, b_user = bill
        if current_day >= day_of_month and last_month != current_month_str:
            trans_date = today.strftime("%Y-%m-%d")
            new_records.append((trans_date, b_type, b_cat, b_amount, b_acc, f"[自動] {b_note}", b_user))
            cursor.execute("UPDATE recurring_bills SET last_processed_month = ? WHERE id = ?", (current_month_str, bill_id))

            if b_type == "expense":
                cursor.execute("UPDATE accounts SET balance = balance - ? WHERE account_name = ? AND user = ?", (b_amount, b_acc, b_user))
            else:
                cursor.execute("UPDATE accounts SET balance = balance + ? WHERE account_name = ? AND user = ?", (b_amount, b_acc, b_user))
                
    if new_records:
        for rec in new_records:
            trans_date, b_type, b_cat, b_amount, b_acc, b_note, b_user = rec
            cursor.execute("SELECT COALESCE(MAX(local_id),0)+1 FROM transactions WHERE user = ?", (b_user,))
            next_local = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO transactions (local_id, date, type, category, amount, account, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (next_local, trans_date, b_type, b_cat, b_amount, b_acc, b_note, b_user)
            )
        conn.commit()
        st.toast(f"已自動生成 {len(new_records)} 筆本月固定收支紀錄！", icon="💡")
    conn.close()

check_recurring_bills()


def parse_date_from_text(text):
    today = datetime.now()
    date_text = text

    if "前天" in date_text:
        target = today - timedelta(days=2)
        date_text = re.sub(r'前天', '', date_text)
        return target.strftime("%Y-%m-%d"), date_text.strip()
    if "昨天" in date_text:
        target = today - timedelta(days=1)
        date_text = re.sub(r'昨天', '', date_text)
        return target.strftime("%Y-%m-%d"), date_text.strip()
    if "今天" in date_text:
        target = today
        date_text = re.sub(r'今天', '', date_text)
        return target.strftime("%Y-%m-%d"), date_text.strip()

    match = re.search(r'(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?', date_text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            target = datetime(year, month, day)
            date_text = date_text.replace(match.group(0), '')
            return target.strftime("%Y-%m-%d"), date_text.strip()
        except ValueError:
            pass

    match = re.search(r'(\d{1,2})月(\d{1,2})日', date_text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        try:
            target = datetime(today.year, month, day)
            date_text = date_text.replace(match.group(0), '')
            return target.strftime("%Y-%m-%d"), date_text.strip()
        except ValueError:
            pass

    return today.strftime("%Y-%m-%d"), date_text.strip()


def parse_chinese_number(text):
    text = text.strip().replace('元', '').replace('塊', '').replace('整', '')
    if not text:
        return None

    mapping = {
        '零': 0, '〇': 0, '一': 1, '二': 2, '兩': 2, '三': 3, '四': 4,
        '五': 5, '六': 6, '七': 7, '八': 8, '九': 9
    }
    units = {'十': 10, '百': 100, '千': 1000, '萬': 10000, '億': 100000000}

    if text in mapping:
        return float(mapping[text])

    total = 0
    current = 0
    for ch in text:
        if ch in mapping:
            current = current * 10 + mapping[ch] if current else mapping[ch]
        elif ch in units:
            unit = units[ch]
            if current == 0:
                current = 1
            total += current * unit
            current = 0
    total += current
    return float(total) if total or text in {'零', '〇'} else None


def parse_chat_input(text, user=None):
    text = text.strip()
    
    save_match = re.search(r'存\s*(\d+)\s*(?:到|給)?\s*(.+)', text)
    if save_match:
        return {"action": "save_goal", "amount": float(save_match.group(1)), "goal": save_match.group(2).strip()}
        
    trans_type = "expense"
    date_str, text = parse_date_from_text(text)

    income_keywords = ["收入", "薪水", "賺", "進帳", "加薪", "中獎", "紅包", "利息", "退款", "兼職", "分紅", "獎金", "補助", "補貼","零用錢","得到","領到","領","獲得","收","收款","收回","收取"]
    negative_markers = ["沒", "不", "未"]
    has_income = any(k in text for k in income_keywords)
    has_negative = any(neg in text for neg in negative_markers)
    if has_income and not has_negative:
        trans_type = "income"
    
    amount = None
    amount_text = None
    amount_match = re.search(r'(\d+(?:\.\d+)?)', text)
    if amount_match:
        amount_text = amount_match.group(1)
        amount = float(amount_match.group(1))
    else:
        chinese_amount_match = re.search(r'([零一二三四五六七八九兩十百千萬億]+)', text)
        if chinese_amount_match:
            amount_text = chinese_amount_match.group(1)
            amount = parse_chinese_number(amount_text)

    if amount is None:
        return None
    
    cleaned = text
    if amount_text:
        cleaned = cleaned.replace(amount_text, '', 1)
    cleaned = cleaned.replace("支出", "").replace("收入", "")
    cleaned = cleaned.replace("花了", "").replace("買", "").replace("花", "").replace("元", "").replace("塊", "")

    conn = sqlite3.connect(DB_FILE)
    if user:
        df_acc = pd.read_sql("SELECT account_name FROM accounts WHERE user = ?", conn, params=(user,))
    else:
        df_acc = pd.read_sql("SELECT account_name FROM accounts", conn)
    conn.close()
    
    account = "未指定"
    for acc in df_acc['account_name']:
        if acc in cleaned:
            account = acc
            cleaned = cleaned.replace(acc, "")
            break
            
    categories = ["伙食", "交通", "娛樂", "購物", "居住", "醫療", "薪水", "其他"]
    category = "其他"
    for cat in categories:
        if cat in cleaned:
            category = cat
            cleaned = cleaned.replace(cat, "")
            break
    
    if category == "其他":
        category_map = {
            "飯": "伙食",
            "麵": "伙食",
            "午餐": "伙食",
            "晚餐": "伙食",
            "早餐": "伙食",
            "咖啡": "伙食",
            "飲料": "伙食",
            "餐廳": "伙食",
            "車": "交通",
            "捷運": "交通",
            "公車": "交通",
            "油": "交通",
            "高鐵": "交通",
            "計程車": "交通",
            "停車": "交通",
            "買": "購物",
            "網購": "購物",
            "衣服": "購物",
            "鞋": "購物",
            "包": "購物",
            "商城": "購物",
            "電商": "購物",
            "訂閱": "娛樂",
            "電影": "娛樂",
            "遊戲": "娛樂",
            "房租": "居住",
            "水電": "居住",
            "瓦斯": "居住",
            "房貸": "居住",
            "醫生": "醫療",
            "藥": "醫療",
            "診所": "醫療",
            "健檢": "醫療",
            "薪水": "薪水",
            "獎金": "薪水",
            "紅包": "薪水",
            "利息": "薪水",
            "補助": "薪水",
            "補貼": "薪水",
            "零用錢": "薪水",
            "得到": "薪水",
            "領到": "薪水",
            "領": "薪水",
            "獲得": "薪水",
            "收": "薪水",
            "收款": "薪水",
            "收回": "薪水",
            "收取": "薪水"
        }
        for keyword, mapped in category_map.items():
            if keyword in cleaned:
                category = mapped
                break
            
    note = cleaned.strip()
    if not note:
        note = f"{category} {date_str}"
        
    return {
        "action": "transaction",
        "date": date_str,
        "type": trans_type,
        "category": category,
        "amount": amount,
        "account": account,
        "note": note
    }


# --------------------------
# Authentication helpers
# --------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return binascii.hexlify(salt).decode() + ':' + binascii.hexlify(dk).decode()


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt_hex, dk_hex = stored_hash.split(':')
        salt = binascii.unhexlify(salt_hex)
        dk = binascii.unhexlify(dk_hex)
        new_dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return hmac.compare_digest(new_dk, dk)
    except Exception:
        return False


def build_daily_summary(df):
    summary = {}
    if df.empty:
        return summary

    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])

    for _, row in df.iterrows():
        day_key = row['date'].strftime('%Y-%m-%d')
        if day_key not in summary:
            summary[day_key] = {'income': 0.0, 'expense': 0.0, 'count': 0}
        if row['type'] == 'income':
            summary[day_key]['income'] += float(row['amount'])
        else:
            summary[day_key]['expense'] += float(row['amount'])
        summary[day_key]['count'] += 1

    return summary


def render_sortable_table(df, key_prefix, width='stretch'):
    if df.empty:
        st.dataframe(df, width=width)
        return df

    columns = list(df.columns)
    sort_col_key = f"{key_prefix}_sort_col"
    sort_asc_key = f"{key_prefix}_sort_asc"
    if sort_col_key not in st.session_state:
        st.session_state[sort_col_key] = columns[0]
    if sort_asc_key not in st.session_state:
        st.session_state[sort_asc_key] = False

    header_cols = st.columns(len(columns))
    for idx, col in enumerate(columns):
        label = col
        if st.session_state[sort_col_key] == col:
            label += " 🔽" if not st.session_state[sort_asc_key] else " 🔼"
        if header_cols[idx].button(label, key=f"{key_prefix}_header_{col}"):
            if st.session_state[sort_col_key] == col:
                st.session_state[sort_asc_key] = not st.session_state[sort_asc_key]
            else:
                st.session_state[sort_col_key] = col
                st.session_state[sort_asc_key] = False

    try:
        sorted_df = df.sort_values(by=st.session_state[sort_col_key], ascending=st.session_state[sort_asc_key], ignore_index=True)
    except Exception:
        sorted_df = df.copy()

    st.dataframe(sorted_df, width=width)
    return sorted_df

# ---------------------------------------------------------
# STREAMLIT UI LAYOUT
# ---------------------------------------------------------
st.set_page_config(page_title="Personal Finance AI & Dashboard", page_icon="💰", layout="wide")

st.title("💰 智慧記帳與個人財務管家")
# --- 使用者註冊 / 登入（含密碼）
st.sidebar.markdown("**使用者註冊 / 登入**")
if 'user' not in st.session_state:
    st.session_state.user = None

auth_mode = st.sidebar.radio("帳號操作", ("登入", "註冊"))
if auth_mode == "註冊":
    with st.sidebar.form("register_form"):
        reg_account = st.text_input("帳號（登入用，英數）", key='reg_account')
        reg_display = st.text_input("名稱（顯示用）", key='reg_display')
        reg_pw = st.text_input("密碼", type='password', key='reg_pw')
        reg_pw2 = st.text_input("再次輸入密碼", type='password', key='reg_pw2')
        submit_register = st.form_submit_button("註冊")

    if submit_register:
        account = reg_account.strip()
        display = reg_display.strip()
        missing = []
        if not account:
            missing.append('帳號')
        if not display:
            missing.append('名稱')
        if not reg_pw:
            missing.append('密碼')
        if not reg_pw2:
            missing.append('再次輸入密碼')

        if missing:
            st.sidebar.error(f"請輸入：{', '.join(missing)}。")
        elif reg_pw != reg_pw2:
            st.sidebar.error("兩次密碼輸入不相符。")
        elif not re.match(r'^[A-Za-z0-9]+$', account):
            st.sidebar.error("帳號只可包含英文字母與數字。")
        else:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM users WHERE username = ?", (account,))
            if cur.fetchone():
                st.sidebar.error("帳號已存在，請換一個。")
                conn.close()
            else:
                pw_hash = hash_password(reg_pw)
                cur.execute("INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                            (account, pw_hash, display, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
                conn.close()
                st.sidebar.success("註冊成功，已自動登入。")
                st.session_state.user = account
                st.session_state.display_name = display
                st.rerun()

elif auth_mode == "登入":
    login_account = st.sidebar.text_input("帳號（登入）", key='login_account')
    login_pw = st.sidebar.text_input("密碼", type='password', key='login_pw')
    if st.sidebar.button("登入"):
        account = login_account.strip()
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT password_hash, display_name FROM users WHERE username = ?", (account,))
        row = cur.fetchone()
        conn.close()
        if not row:
            st.sidebar.error("帳號不存在，請先註冊。")
        else:
            stored, display = row[0], row[1]
            if not stored:
                st.sidebar.error("此帳號尚未設定密碼，請重新註冊。")
            elif verify_password(stored, login_pw):
                st.sidebar.success("登入成功。")
                st.session_state.user = account
                st.session_state.display_name = display
                st.rerun()
            else:
                st.sidebar.error("密碼錯誤。")

if st.session_state.get('user'):
    disp = st.session_state.get('display_name') or ''
    acct = st.session_state.get('user')
    if disp:
        st.sidebar.markdown(f"目前使用者：**{disp} ({acct})**")
    else:
        st.sidebar.markdown(f"目前使用者：**{acct}**")
    if st.sidebar.button("登出"):
        st.session_state.user = None
        st.session_state.display_name = None
        st.rerun()

st.sidebar.title("功能導覽")
app_mode = st.sidebar.radio("選擇模式", ["🤖 聊天記帳助手", "� 日曆檢視", "�📊 財務儀表板", "🎯 存錢目標管理", "⚙️ 固定收支與帳戶管理"])

if app_mode == "🤖 聊天記帳助手":
    st.subheader("聊天機器人快速記帳")
    st.markdown("輸入自然語言，系統自動幫你分類並記帳！例如：*「午餐排骨飯 120 現金」*、*「昨天 95 現金」*、*「7/15 Netflix 390」*、*「存 3000 到 日本旅遊」*")
    st.markdown("**提示：** 可以輸入日期、分類、帳戶與存錢目標，系統會自動解析。")
    
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
        if not st.session_state.get('user'):
            reply = "請先在側邊欄登入（使用 帳號 與 密碼），才能開始記帳。"
            st.session_state.chat_history.append({"role": "assistant", "content": reply})
            with st.chat_message("assistant"):
                st.markdown(reply)
            st.rerun()  # 替換掉原本錯誤的 continue，重新整理畫面並結束本次觸發
        else:
            # TODO: 使用者已切換，在此處理 AI 記帳邏輯
            pass

        parsed = parse_chat_input(prompt, st.session_state.user)

        # 獨立建立連線處理聊天輸入
        action_conn = sqlite3.connect(DB_FILE)

        if parsed is None:
            reply = "抱歉，我讀不太懂這筆記帳金額。請包含數字，例如：`午餐 120`"
        elif parsed["action"] == "save_goal":
            goal_name = parsed["goal"]
            save_amt = parsed["amount"]
            cursor = action_conn.cursor()
            cursor.execute("SELECT current_amount, target_amount FROM savings_goals WHERE goal_name = ? AND user = ?", (goal_name, st.session_state.user))
            res = cursor.fetchone()
            if res:
                new_amt = res[0] + save_amt
                cursor.execute("UPDATE savings_goals SET current_amount = ? WHERE goal_name = ? AND user = ?", (new_amt, goal_name, st.session_state.user))
                action_conn.commit()
                reply = f"成功存入 **{save_amt:,.0f} 元** 到『{goal_name}』！目前進度：{new_amt:,.0f} / {res[1]:,.0f} 元 ({int(new_amt/res[1]*100)}%)"
            else:
                reply = f"找不到名為『{goal_name}』的存錢目標，請先至管理頁面建立。"
        elif parsed["action"] == "transaction":
            cursor = action_conn.cursor()
            cursor.execute("SELECT 1 FROM accounts WHERE account_name = ? AND user = ?", (parsed["account"], st.session_state.user))
            if cursor.fetchone() is None:
                cursor.execute(
                    "INSERT OR IGNORE INTO accounts (account_name, balance, user) VALUES (?, ?, ?)",
                    (parsed["account"], 0, st.session_state.user)
                )

            # 計算使用者專屬的 local_id（逐筆遞增）
            cursor.execute("SELECT COALESCE(MAX(local_id),0)+1 FROM transactions WHERE user = ?", (st.session_state.user,))
            next_local = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO transactions (local_id, date, type, category, amount, account, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (next_local, parsed["date"], parsed["type"], parsed["category"], parsed["amount"], parsed["account"], parsed["note"], st.session_state.user)
            )
            if parsed["type"] == "expense":
                cursor.execute("UPDATE accounts SET balance = balance - ? WHERE account_name = ? AND user = ?", (parsed["amount"], parsed["account"], st.session_state.user))
            else:
                cursor.execute("UPDATE accounts SET balance = balance + ? WHERE account_name = ? AND user = ?", (parsed["amount"], parsed["account"], st.session_state.user))
            action_conn.commit()
            
            t_sign = "-" if parsed["type"] == "expense" else "+"
            reply = f"✅ 記帳成功：【{parsed['category']}】{t_sign}{parsed['amount']:,.0f} 元 ({parsed['account']}) | 備註：{parsed['note']}"
        else:
            reply = "發生未知錯誤。"
            
        action_conn.close()
            
        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        with st.chat_message("assistant"):
            st.markdown(reply)
            # 顯示解析結果讓使用者確認（避免編碼顯示誤解）
            try:
                if parsed and parsed.get("action") == "transaction":
                    with st.expander("解析結果 (Parsed transaction)"):
                        st.json({
                            "date": parsed.get("date"),
                            "type": parsed.get("type"),
                            "category": parsed.get("category"),
                            "amount": parsed.get("amount"),
                            "account": parsed.get("account"),
                            "note": parsed.get("note"),
                        })
            except Exception:
                # 不應該阻斷主流程，若顯示失敗則靜默略過
                pass
            
    st.divider()
    st.subheader("📝 今日/近期即時帳目清單")
    
    # 獨立建立連線讀取清單（僅顯示已登入使用者的交易）
    list_conn = sqlite3.connect(DB_FILE)
    if st.session_state.get('user'):
        df_trans = pd.read_sql("SELECT * FROM transactions WHERE user = ? ORDER BY local_id DESC LIMIT 10", list_conn, params=(st.session_state.user,))
    else:
        # 未登入時不要顯示他人資料
        df_trans = pd.DataFrame(columns=['local_id','date','type','category','amount','account','note','user'])
    list_conn.close()
    
    if not st.session_state.get('user'):
        st.info("請先登入以檢視個人記帳資料。")
    elif not df_trans.empty:
        render_sortable_table(df_trans, key_prefix='trans_list', width='stretch')
        
        del_id = st.selectbox("選擇要刪除的交易 ID (修正誤記)", [None] + list(df_trans['local_id']))
        if del_id:
            if st.button("刪除此筆交易"):
                del_conn = sqlite3.connect(DB_FILE)
                cursor = del_conn.cursor()
                cursor.execute("SELECT type, amount, account FROM transactions WHERE local_id = ? AND user = ?", (del_id, st.session_state.user))
                t_row = cursor.fetchone()
                if t_row:
                    t_type, t_amt, t_acc = t_row
                    if t_type == "expense":
                        cursor.execute("UPDATE accounts SET balance = balance + ? WHERE account_name = ? AND user = ?", (t_amt, t_acc, st.session_state.user))
                    else:
                        cursor.execute("UPDATE accounts SET balance = balance - ? WHERE account_name = ? AND user = ?", (t_amt, t_acc, st.session_state.user))
                    cursor.execute("DELETE FROM transactions WHERE local_id = ? AND user = ?", (del_id, st.session_state.user))
                    # 重新編排該使用者之 local_id，避免跳號
                    cursor.execute("UPDATE transactions SET local_id = local_id - 1 WHERE local_id > ? AND user = ?", (del_id, st.session_state.user))
                    del_conn.commit()
                del_conn.close()
                st.success(f"已刪除交易 ID {del_id} 並同步還原帳戶餘額！")
                st.rerun()
    else:
        st.info("目前尚無記帳資料。")

elif app_mode == "� 日曆檢視":
    st.subheader("📅 月曆與週曆收支檢視")

    cal_conn = sqlite3.connect(DB_FILE)
    if st.session_state.get('user'):
        df_trans = pd.read_sql("SELECT * FROM transactions WHERE user = ?", cal_conn, params=(st.session_state.user,))
    else:
        df_trans = pd.DataFrame()
    cal_conn.close()

    if not st.session_state.get('user'):
        st.info("請先登入以檢視個人日曆摘要。")
    elif df_trans.empty:
        st.info("目前尚無交易資料，先記帳後就能看到日曆摘要。")
    else:
        df_trans['date'] = pd.to_datetime(df_trans['date'])
        daily_summary = build_daily_summary(df_trans)

        today = date.today()
        current_year = today.year
        current_month = today.month
        first_day = date(current_year, current_month, 1)
        last_day = date(current_year, current_month, calendar.monthrange(current_year, current_month)[1])

        st.markdown(f"### 本月日曆：{current_year} 年 {current_month} 月")
        weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        cols = st.columns(7)
        for idx, label in enumerate(weekdays):
            with cols[idx]:
                st.caption(label)

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
                        st.markdown("<div style='min-height: 100px'></div>", unsafe_allow_html=True)
                    else:
                        day_key = day_value.strftime('%Y-%m-%d')
                        summary = daily_summary.get(day_key, {'income': 0.0, 'expense': 0.0, 'count': 0})
                        is_today = day_value == today
                        border = "border: 2px solid #3b82f6;" if is_today else "border: 1px solid #e5e7eb;"
                        st.markdown(
                            f"<div style='min-height: 110px; padding: 8px; border-radius: 8px; {border}'>"
                            f"<strong>{day_value.day}</strong><br/>"
                            f"收入: {summary['income']:,.0f}<br/>"
                            f"支出: {summary['expense']:,.0f}<br/>"
                            f"筆數: {summary['count']}"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

        st.divider()
        st.markdown("### 本週每日總覽")
        week_start = today - timedelta(days=today.weekday())
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        week_cols = st.columns(7)
        for idx, day_value in enumerate(week_dates):
            with week_cols[idx]:
                day_key = day_value.strftime('%Y-%m-%d')
                summary = daily_summary.get(day_key, {'income': 0.0, 'expense': 0.0, 'count': 0})
                st.markdown(f"#### {day_value.month}/{day_value.day}")
                st.metric("收入", f"${summary['income']:,.0f}")
                st.metric("支出", f"${summary['expense']:,.0f}")
                st.caption(f"交易筆數：{summary['count']}")

        st.divider()
        st.markdown("### 指定日期明細")
        selected_day = st.date_input("選擇日期", value=today)
        selected_key = selected_day.strftime('%Y-%m-%d')
        day_df = df_trans[df_trans['date'].dt.strftime('%Y-%m-%d') == selected_key].copy()
        if day_df.empty:
            st.info(f"{selected_day.strftime('%Y-%m-%d')} 沒有交易紀錄。")
        else:
            day_df = day_df[['date', 'type', 'category', 'amount', 'account', 'note']]
            day_df['date'] = day_df['date'].dt.strftime('%Y-%m-%d')
            render_sortable_table(day_df, key_prefix='day_detail', width='stretch')

elif app_mode == "�📊 財務儀表板":
    st.subheader("📊 個人財務總覽與數據洞察")
    
    dash_conn = sqlite3.connect(DB_FILE)
    if st.session_state.get('user'):
        df_trans = pd.read_sql("SELECT * FROM transactions WHERE user = ?", dash_conn, params=(st.session_state.user,))
        df_acc = pd.read_sql("SELECT * FROM accounts WHERE user = ?", dash_conn, params=(st.session_state.user,))
    else:
        df_trans = pd.read_sql("SELECT * FROM transactions", dash_conn)
        df_acc = pd.read_sql("SELECT * FROM accounts", dash_conn)
    dash_conn.close()
    
    if df_trans.empty:
        st.info("請先至聊天頁面記錄一些收支，即可在這裡看到豐富的視覺化圖表！")
    else:
        df_trans['date'] = pd.to_datetime(df_trans['date'])
        current_month = datetime.now().strftime("%Y-%m")
        df_month = df_trans[df_trans['date'].dt.strftime("%Y-%m") == current_month]
        
        total_expense = df_month[df_month['type'] == 'expense']['amount'].sum()
        total_income = df_month[df_month['type'] == 'income']['amount'].sum()
        net_worth = df_acc['balance'].sum()
        
        monthly_budget = st.sidebar.number_input("設定本月總預算 (元)", value=0, step=1000)
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("本月總支出", f"${total_expense:,.0f}")
        col2.metric("本月總收入", f"${total_income:,.0f}")
        col3.metric("總資產淨值", f"${net_worth:,.0f}")
        
        today = datetime.now()
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        day_today = today.day
        days_left = max(1, days_in_month - day_today)
        safe_budget_left = max(0, monthly_budget - total_expense)
        safe_daily = safe_budget_left / days_left
        col4.metric("今日可用額度 (Safe-to-spend)", f"${safe_daily:,.0f} /天", delta=f"剩餘預算 ${safe_budget_left:,.0f}")
        
        df_exp = df_month[df_month['type'] == 'expense']
        avg_daily = 0
        predicted_expense = 0
        if not df_exp.empty:
            df_daily = df_exp.groupby(df_exp['date'].dt.date)['amount'].sum().reset_index()
            avg_daily = df_daily['amount'].mean()
            predicted_expense = avg_daily * days_in_month

        if total_expense >= monthly_budget:
            st.error("⚠️ 本月已超過預算！請優先檢視支出與調整花費。")
        elif total_expense >= monthly_budget * 0.8:
            st.warning("⚠️ 支出已達 80% 預算，請注意剩餘資金。")
        else:
            st.success("目前預算仍在安全範圍內。")

        st.markdown(f"*本月平均每日支出約 ${avg_daily:,.0f}，依此速度預計本月支出 ${predicted_expense:,.0f}。*")
        st.divider()
        
        st.markdown("### 🎯 本月預算消耗進度")
        budget_pct = min(1.0, total_expense / monthly_budget if monthly_budget > 0 else 1.0)
        st.progress(budget_pct, text=f"已花費 {total_expense:,.0f} / 預算 {monthly_budget:,.0f} 元 ({int(budget_pct*100)}%)")
        
        col_a, col_b = st.columns(2)
        
        with col_a:
            st.markdown("### 🥧 本月分類支出占比")
            df_exp = df_month[df_month['type'] == 'expense']
            if not df_exp.empty:
                fig_pie = px.pie(df_exp, names='category', values='amount', hole=0.4, color_discrete_sequence=px.colors.qualitative.Pastel)
                st.plotly_chart(fig_pie, width='stretch')
            else:
                st.info("本月尚無支出紀錄。")
                
        with col_b:
            st.markdown("### 📈 每日累積支出趨勢")
            if not df_exp.empty:
                df_daily = df_exp.groupby(df_exp['date'].dt.date)['amount'].sum().reset_index()
                df_daily['cumulative'] = df_daily['amount'].cumsum()
                fig_line = px.line(df_daily, x='date', y='cumulative', markers=True, title="本月累積花費曲線")
                st.plotly_chart(fig_line, width='stretch')
            else:
                st.info("本月尚無支出紀錄。")
                
        st.divider()
        st.markdown("### 💳 各帳戶資產分佈")
        fig_bar = px.bar(df_acc, x='account_name', y='balance', color='account_name', text='balance', title="各帳戶餘額")
        st.plotly_chart(fig_bar, width='stretch')

elif app_mode == "🎯 存錢目標管理":
    st.subheader("🎯 存錢目標進度管理")
    
    goal_conn = sqlite3.connect(DB_FILE)
    if st.session_state.get('user'):
        df_goals = pd.read_sql("SELECT * FROM savings_goals WHERE user = ?", goal_conn, params=(st.session_state.user,))
    else:
        df_goals = pd.read_sql("SELECT * FROM savings_goals", goal_conn)
    goal_conn.close()
    today = datetime.now()
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        if df_goals.empty:
            st.info("目前尚無存錢目標，請在右側新增一個目標。")
        for idx, row in df_goals.iterrows():
            g_name = row['goal_name']
            target = row['target_amount']
            current = row['current_amount']
            t_date = row['target_date']
            pct = min(1.0, current / target if target > 0 else 1.0)
            remaining = max(0, target - current)
            try:
                due_date = datetime.strptime(t_date, "%Y-%m-%d")
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
            
            st.markdown(f"#### 🏆 {g_name}")
            st.progress(pct, text=f"進度：${current:,.0f} / ${target:,.0f} ({int(pct*100)}%) | 預定目標日：{t_date}")
            st.caption(status_text)
            st.markdown("---")
            
    with col2:
        st.markdown("### ➕ 新增存錢目標")
        with st.form("new_goal_form"):
            new_name = st.text_input("目標名稱")
            new_target = st.number_input("目標金額", value=0, step=5000)
            new_initial = st.number_input("初始已存金額", value=0, step=1000)
            new_date = st.date_input("預計達標日期", value=date(2026, 12, 31))
            
            submit_goal = st.form_submit_button("建立目標")
            if submit_goal and new_name:
                add_conn = sqlite3.connect(DB_FILE)
                cursor = add_conn.cursor()
                try:
                    cursor.execute("INSERT INTO savings_goals (goal_name, target_amount, current_amount, target_date, user) VALUES (?, ?, ?, ?, ?)",
                                   (new_name, new_target, new_initial, str(new_date), st.session_state.user))
                    add_conn.commit()
                    st.success(f"成功建立存錢目標：{new_name}！")
                    st.rerun()
                except Exception as e:
                    st.error(f"建立失敗（可能名稱重複）：{e}")
                finally:
                    add_conn.close()

        if not df_goals.empty:
            st.markdown("### ✏️ 更新或刪除目標")
            goal_names = df_goals['goal_name'].tolist()
            with st.form("edit_goal_form"):
                selected_goal = st.selectbox("選擇目標", goal_names, key="edit_goal_select")
                current_value = int(df_goals.loc[df_goals['goal_name'] == selected_goal, 'current_amount'].iloc[0])
                updated_current = st.number_input("目前已存金額", value=current_value, step=1000)
                submit_update = st.form_submit_button("更新目標進度")
                if submit_update:
                    edit_conn = sqlite3.connect(DB_FILE)
                    cursor = edit_conn.cursor()
                    cursor.execute("UPDATE savings_goals SET current_amount = ? WHERE goal_name = ? AND user = ?", (updated_current, selected_goal, st.session_state.user))
                    edit_conn.commit()
                    edit_conn.close()
                    st.success(f"已更新『{selected_goal}』的已存金額。")
                    st.rerun()
            
            with st.form("delete_goal_form"):
                selected_delete = st.selectbox("選擇要刪除的目標", goal_names, key="delete_goal_select")
                submit_delete = st.form_submit_button("刪除目標")
                if submit_delete:
                    del_conn = sqlite3.connect(DB_FILE)
                    cursor = del_conn.cursor()
                    cursor.execute("DELETE FROM savings_goals WHERE goal_name = ? AND user = ?", (selected_delete, st.session_state.user))
                    del_conn.commit()
                    del_conn.close()
                    st.success(f"已刪除存錢目標：{selected_delete}")
                    st.rerun()

elif app_mode == "⚙️ 固定收支與帳戶管理":
    st.subheader("⚙️ 固定收支與資產帳戶設定")
    
    tab1, tab2 = st.tabs(["固定收支設定", "資產帳戶設定"])
    
    with tab1:
        st.markdown("在此設定每月自動扣款或入帳的固定項目（如房租、訂閱費、固定薪資）。")
        set_conn = sqlite3.connect(DB_FILE)
        if st.session_state.get('user'):
            df_bills = pd.read_sql("SELECT * FROM recurring_bills WHERE user = ?", set_conn, params=(st.session_state.user,))
            df_acc_list = pd.read_sql("SELECT account_name FROM accounts WHERE user = ?", set_conn, params=(st.session_state.user,))
        else:
            df_bills = pd.read_sql("SELECT * FROM recurring_bills", set_conn)
            df_acc_list = pd.read_sql("SELECT account_name FROM accounts", set_conn)
        set_conn.close()

        current_month_str = datetime.now().strftime("%Y-%m")
        if not df_bills.empty:
            df_bills['本月已處理'] = df_bills['last_processed_month'] == current_month_str
        render_sortable_table(df_bills, key_prefix='bills_list', width='stretch')

        if not df_bills.empty:
            bill_options = [f"{row['id']}: {row['note']} ({'收入' if row['type']=='income' else '支出'})" for _, row in df_bills.iterrows()]
            with st.form("delete_bill_form"):
                selected_bill = st.selectbox("選擇要刪除的固定收支", bill_options, key="delete_bill_select")
                delete_bill = st.form_submit_button("刪除固定收支")
                if delete_bill:
                    bill_id = int(selected_bill.split(":")[0])
                    del_conn = sqlite3.connect(DB_FILE)
                    cursor = del_conn.cursor()
                    cursor.execute("DELETE FROM recurring_bills WHERE id = ? AND user = ?", (bill_id, st.session_state.user))
                    del_conn.commit()
                    del_conn.close()
                    st.success("已刪除固定收支項目。")
                    st.rerun()

        with st.form("add_bill_form"):
            st.markdown("#### 新增固定收支")
            b_day = st.number_input("每月扣款日 (1-31)", min_value=1, max_value=31, value=1)
            b_type = st.selectbox("類型", ["expense", "income"], format_func=lambda x: "支出" if x=="expense" else "收入")
            b_cat = st.selectbox("分類", ["伙食", "交通", "娛樂", "購物", "居住", "醫療", "薪水", "其他"])
            b_amt = st.number_input("金額", value=0, step=100)
            b_acc = st.selectbox("扣款/入帳帳戶", df_acc_list['account_name'])
            b_note = st.text_input("名稱備註 (如: 房租、Spotify)")
            
            submitted_bill = st.form_submit_button("新增固定收支")
            if submitted_bill:
                bill_conn = sqlite3.connect(DB_FILE)
                cursor = bill_conn.cursor()
                cursor.execute("INSERT INTO recurring_bills (day_of_month, type, category, amount, account, note, last_processed_month, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (b_day, b_type, b_cat, b_amt, b_acc, b_note, "", st.session_state.user))
                bill_conn.commit()
                bill_conn.close()
                st.success("固定收支新增成功！")
                st.rerun()
                
    with tab2:
        st.markdown("在此管理你的資產帳戶（現金、銀行帳戶、悠遊卡等）。")
        acc_conn = sqlite3.connect(DB_FILE)
        if st.session_state.get('user'):
            df_accounts = pd.read_sql("SELECT * FROM accounts WHERE user = ?", acc_conn, params=(st.session_state.user,))
        else:
            df_accounts = pd.read_sql("SELECT * FROM accounts", acc_conn)
        acc_conn.close()
        
        render_sortable_table(df_accounts, key_prefix='account_list', width='stretch')
        
        with st.form("add_account_form"):
            st.markdown("#### 新增帳戶")
            acc_name = st.text_input("帳戶名稱 (例如: 富邦銀行)")
            acc_bal = st.number_input("初始餘額", value=0, step=1000)
            
            submitted_acc = st.form_submit_button("新增帳戶")
            if submitted_acc and acc_name:
                new_acc_conn = sqlite3.connect(DB_FILE)
                cursor = new_acc_conn.cursor()
                try:
                    cursor.execute("INSERT INTO accounts (account_name, balance, user) VALUES (?, ?, ?)", (acc_name, acc_bal, st.session_state.user))
                    new_acc_conn.commit()
                    st.success(f"成功新增帳戶：{acc_name}")
                    st.rerun()
                except Exception as e:
                    st.error(f"新增失敗（帳戶名稱可能重複）：{e}")
                finally:
                    new_acc_conn.close()

        if not df_accounts.empty:
            st.markdown("### ✏️ 更新或刪除帳戶")
            account_names = df_accounts['account_name'].tolist()
            selected_account = st.selectbox("選擇帳戶", account_names, key="edit_account_select")
            selected_balance = float(df_accounts.loc[df_accounts['account_name'] == selected_account, 'balance'].iloc[0])
            with st.form("edit_account_form"):
                new_balance = st.number_input("調整餘額", value=int(selected_balance), step=1000)
                update_acc = st.form_submit_button("更新帳戶餘額")
                if update_acc:
                    update_conn = sqlite3.connect(DB_FILE)
                    cursor = update_conn.cursor()
                    cursor.execute("UPDATE accounts SET balance = ? WHERE account_name = ? AND user = ?", (new_balance, selected_account, st.session_state.user))
                    update_conn.commit()
                    update_conn.close()
                    st.success(f"已更新帳戶『{selected_account}』的餘額。")
                    st.rerun()

            with st.form("delete_account_form"):
                delete_account = st.selectbox("選擇要刪除的帳戶", account_names, key="delete_account_select")
                submit_delete_account = st.form_submit_button("刪除帳戶")
                if submit_delete_account:
                    check_conn = sqlite3.connect(DB_FILE)
                    cursor = check_conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM transactions WHERE account = ? AND user = ?", (delete_account, st.session_state.user))
                    tx_count = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM recurring_bills WHERE account = ? AND user = ?", (delete_account, st.session_state.user))
                    bill_count = cursor.fetchone()[0]
                    if tx_count > 0 or bill_count > 0:
                        st.error("無法刪除：該帳戶仍有交易或固定收支紀錄。請先移除相關紀錄後再試。")
                    else:
                        cursor.execute("DELETE FROM accounts WHERE account_name = ? AND user = ?", (delete_account, st.session_state.user))
                        check_conn.commit()
                        st.success(f"已刪除帳戶：{delete_account}")
                        st.rerun()
                    check_conn.close()
