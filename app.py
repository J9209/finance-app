import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date, timedelta
import calendar
import re

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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            type TEXT,
            category TEXT,
            amount REAL,
            account TEXT,
            note TEXT
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
            last_processed_month TEXT
        )
    ''')
    
    # 3. Accounts table (for net worth)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT UNIQUE,
            balance REAL
        )
    ''')
    
    # 4. Savings goals table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_name TEXT UNIQUE,
            target_amount REAL,
            current_amount REAL,
            target_date TEXT
        )
    ''')
    
    conn.commit()
    
    # Seed default accounts or goals if empty
    cursor.execute("SELECT COUNT(*) FROM accounts")
    if cursor.fetchone()[0] == 0:
        default_accounts = [("現金", 5000), ("台新銀行", 30000), ("悠遊卡", 500)]
        cursor.executemany("INSERT INTO accounts (account_name, balance) VALUES (?, ?)", default_accounts)
        conn.commit()
    
    cursor.execute("SELECT COUNT(*) FROM savings_goals")
    if cursor.fetchone()[0] == 0:
        default_goals = [("日本旅遊", 40000, 10000, "2026-12-31"), ("緊急預備金", 100000, 50000, "2027-06-30")]
        cursor.executemany("INSERT INTO savings_goals (goal_name, target_amount, current_amount, target_date) VALUES (?, ?, ?, ?)", default_goals)
        conn.commit()
        
    cursor.execute("SELECT COUNT(*) FROM recurring_bills")
    if cursor.fetchone()[0] == 0:
        default_bills = [(5, "expense", "居住", 12000, "台新銀行", "房租", ""), (10, "expense", "娛樂", 390, "台新銀行", "Netflix 訂閱", "")]
        cursor.executemany("INSERT INTO recurring_bills (day_of_month, type, category, amount, account, note, last_processed_month) VALUES (?, ?, ?, ?, ?, ?, ?)", default_bills)
        conn.commit()

    conn.close()

init_db()

# ---------------------------------------------------------
# HELPER FUNCTIONS & AUTOMATION
# ---------------------------------------------------------
def check_recurring_bills():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, day_of_month, type, category, amount, account, note, last_processed_month FROM recurring_bills")
    bills = cursor.fetchall()
    
    today = datetime.now()
    current_month_str = today.strftime("%Y-%m")
    current_day = today.day
    
    new_records = []
    for bill in bills:
        bill_id, day_of_month, b_type, b_cat, b_amount, b_acc, b_note, last_month = bill
        if current_day >= day_of_month and last_month != current_month_str:
            trans_date = today.strftime("%Y-%m-%d")
            new_records.append((trans_date, b_type, b_cat, b_amount, b_acc, f"[自動] {b_note}"))
            cursor.execute("UPDATE recurring_bills SET last_processed_month = ? WHERE id = ?", (current_month_str, bill_id))
            
            if b_type == "expense":
                cursor.execute("UPDATE accounts SET balance = balance - ? WHERE account_name = ?", (b_amount, b_acc))
            else:
                cursor.execute("UPDATE accounts SET balance = balance + ? WHERE account_name = ?", (b_amount, b_acc))
                
    if new_records:
        cursor.executemany("INSERT INTO transactions (date, type, category, amount, account, note) VALUES (?, ?, ?, ?, ?, ?)", new_records)
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


def parse_chat_input(text):
    text = text.strip()
    
    save_match = re.search(r'存\s*(\d+)\s*(?:到|給)?\s*(.+)', text)
    if save_match:
        return {"action": "save_goal", "amount": float(save_match.group(1)), "goal": save_match.group(2).strip()}
        
    trans_type = "expense"
    date_str, text = parse_date_from_text(text)

    income_keywords = ["收入", "薪水", "賺", "進帳", "加薪", "中獎", "紅包", "利息", "退款", "兼職", "分紅"]
    negative_markers = ["沒", "不", "未"]
    has_income = any(k in text for k in income_keywords)
    has_negative = any(neg in text for neg in negative_markers)
    if has_income and not has_negative:
        trans_type = "income"
    
    amount_match = re.search(r'(\d+(?:\.\d+)?)', text)
    if not amount_match:
        return None
    amount = float(amount_match.group(1))
    
    cleaned = text.replace(amount_match.group(1), '', 1)
    cleaned = cleaned.replace("支出", "").replace("收入", "")

    conn = sqlite3.connect(DB_FILE)
    df_acc = pd.read_sql("SELECT account_name FROM accounts", conn)
    conn.close()
    
    account = "現金"
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
            "房租": "居住"
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

# ---------------------------------------------------------
# STREAMLIT UI LAYOUT
# ---------------------------------------------------------
st.set_page_config(page_title="Personal Finance AI & Dashboard", page_icon="💰", layout="wide")

st.title("💰 智慧記帳與個人財務管家")

st.sidebar.title("功能導覽")
app_mode = st.sidebar.radio("選擇模式", ["🤖 聊天記帳助手", "📊 財務儀表板", "🎯 存錢目標管理", "⚙️ 固定收支與帳戶管理"])

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
            
        parsed = parse_chat_input(prompt)
        
        # 獨立建立連線處理聊天輸入
        action_conn = sqlite3.connect(DB_FILE)
        
        if parsed is None:
            reply = "抱歉，我讀不太懂這筆記帳金額。請包含數字，例如：`午餐 120`"
        elif parsed["action"] == "save_goal":
            goal_name = parsed["goal"]
            save_amt = parsed["amount"]
            
            cursor = action_conn.cursor()
            cursor.execute("SELECT current_amount, target_amount FROM savings_goals WHERE goal_name = ?", (goal_name,))
            res = cursor.fetchone()
            if res:
                new_amt = res[0] + save_amt
                cursor.execute("UPDATE savings_goals SET current_amount = ? WHERE goal_name = ?", (new_amt, goal_name))
                action_conn.commit()
                reply = f"成功存入 **{save_amt:,.0f} 元** 到『{goal_name}』！目前進度：{new_amt:,.0f} / {res[1]:,.0f} 元 ({int(new_amt/res[1]*100)}%)"
            else:
                reply = f"找不到名為『{goal_name}』的存錢目標，請先至管理頁面建立。"
        elif parsed["action"] == "transaction":
            cursor = action_conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (date, type, category, amount, account, note) VALUES (?, ?, ?, ?, ?, ?)",
                (parsed["date"], parsed["type"], parsed["category"], parsed["amount"], parsed["account"], parsed["note"])
            )
            if parsed["type"] == "expense":
                cursor.execute("UPDATE accounts SET balance = balance - ? WHERE account_name = ?", (parsed["amount"], parsed["account"]))
            else:
                cursor.execute("UPDATE accounts SET balance = balance + ? WHERE account_name = ?", (parsed["amount"], parsed["account"]))
            action_conn.commit()
            
            t_sign = "-" if parsed["type"] == "expense" else "+"
            reply = f"✅ 記帳成功：【{parsed['category']}】{t_sign}{parsed['amount']:,.0f} 元 ({parsed['account']}) | 備註：{parsed['note']}"
        else:
            reply = "發生未知錯誤。"
            
        action_conn.close()
            
        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        with st.chat_message("assistant"):
            st.markdown(reply)
            
    st.divider()
    st.subheader("📝 今日/近期即時帳目清單")
    
    # 獨立建立連線讀取清單
    list_conn = sqlite3.connect(DB_FILE)
    df_trans = pd.read_sql("SELECT * FROM transactions ORDER BY id DESC LIMIT 10", list_conn)
    list_conn.close()
    
    if not df_trans.empty:
        st.dataframe(df_trans, width='stretch')
        
        del_id = st.selectbox("選擇要刪除的交易 ID (修正誤記)", [None] + list(df_trans['id']))
        if del_id:
            if st.button("刪除此筆交易"):
                del_conn = sqlite3.connect(DB_FILE)
                cursor = del_conn.cursor()
                cursor.execute("SELECT type, amount, account FROM transactions WHERE id = ?", (del_id,))
                t_row = cursor.fetchone()
                if t_row:
                    t_type, t_amt, t_acc = t_row
                    if t_type == "expense":
                        cursor.execute("UPDATE accounts SET balance = balance + ? WHERE account_name = ?", (t_amt, t_acc))
                    else:
                        cursor.execute("UPDATE accounts SET balance = balance - ? WHERE account_name = ?", (t_amt, t_acc))
                    cursor.execute("DELETE FROM transactions WHERE id = ?", (del_id,))
                    del_conn.commit()
                del_conn.close()
                st.success(f"已刪除交易 ID {del_id} 並同步還原帳戶餘額！")
                st.rerun()
    else:
        st.info("目前尚無記帳資料。")

elif app_mode == "📊 財務儀表板":
    st.subheader("📊 個人財務總覽與數據洞察")
    
    dash_conn = sqlite3.connect(DB_FILE)
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
        
        monthly_budget = st.sidebar.number_input("設定本月總預算 (元)", value=15000, step=1000)
        
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
            new_target = st.number_input("目標金額", value=30000, step=5000)
            new_initial = st.number_input("初始已存金額", value=0, step=1000)
            new_date = st.date_input("預計達標日期", value=date(2026, 12, 31))
            
            submit_goal = st.form_submit_button("建立目標")
            if submit_goal and new_name:
                add_conn = sqlite3.connect(DB_FILE)
                cursor = add_conn.cursor()
                try:
                    cursor.execute("INSERT INTO savings_goals (goal_name, target_amount, current_amount, target_date) VALUES (?, ?, ?, ?)",
                                   (new_name, new_target, new_initial, str(new_date)))
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
                    cursor.execute("UPDATE savings_goals SET current_amount = ? WHERE goal_name = ?", (updated_current, selected_goal))
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
                    cursor.execute("DELETE FROM savings_goals WHERE goal_name = ?", (selected_delete,))
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
        df_bills = pd.read_sql("SELECT * FROM recurring_bills", set_conn)
        df_acc_list = pd.read_sql("SELECT account_name FROM accounts", set_conn)
        set_conn.close()

        current_month_str = datetime.now().strftime("%Y-%m")
        if not df_bills.empty:
            df_bills['本月已處理'] = df_bills['last_processed_month'] == current_month_str
        st.dataframe(df_bills, width='stretch')

        if not df_bills.empty:
            bill_options = [f"{row['id']}: {row['note']} ({'收入' if row['type']=='income' else '支出'})" for _, row in df_bills.iterrows()]
            with st.form("delete_bill_form"):
                selected_bill = st.selectbox("選擇要刪除的固定收支", bill_options, key="delete_bill_select")
                delete_bill = st.form_submit_button("刪除固定收支")
                if delete_bill:
                    bill_id = int(selected_bill.split(":")[0])
                    del_conn = sqlite3.connect(DB_FILE)
                    cursor = del_conn.cursor()
                    cursor.execute("DELETE FROM recurring_bills WHERE id = ?", (bill_id,))
                    del_conn.commit()
                    del_conn.close()
                    st.success("已刪除固定收支項目。")
                    st.rerun()

        with st.form("add_bill_form"):
            st.markdown("#### 新增固定收支")
            b_day = st.number_input("每月扣款日 (1-31)", min_value=1, max_value=31, value=1)
            b_type = st.selectbox("類型", ["expense", "income"], format_func=lambda x: "支出" if x=="expense" else "收入")
            b_cat = st.selectbox("分類", ["伙食", "交通", "娛樂", "購物", "居住", "醫療", "薪水", "其他"])
            b_amt = st.number_input("金額", value=500, step=100)
            b_acc = st.selectbox("扣款/入帳帳戶", df_acc_list['account_name'])
            b_note = st.text_input("名稱備註 (如: 房租、Spotify)")
            
            submitted_bill = st.form_submit_button("新增固定收支")
            if submitted_bill:
                bill_conn = sqlite3.connect(DB_FILE)
                cursor = bill_conn.cursor()
                cursor.execute("INSERT INTO recurring_bills (day_of_month, type, category, amount, account, note, last_processed_month) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (b_day, b_type, b_cat, b_amt, b_acc, b_note, ""))
                bill_conn.commit()
                bill_conn.close()
                st.success("固定收支新增成功！")
                st.rerun()
                
    with tab2:
        st.markdown("在此管理你的資產帳戶（現金、銀行帳戶、悠遊卡等）。")
        acc_conn = sqlite3.connect(DB_FILE)
        df_accounts = pd.read_sql("SELECT * FROM accounts", acc_conn)
        acc_conn.close()
        
        st.dataframe(df_accounts, width='stretch')
        
        with st.form("add_account_form"):
            st.markdown("#### 新增帳戶")
            acc_name = st.text_input("帳戶名稱 (例如: 富邦銀行)")
            acc_bal = st.number_input("初始餘額", value=10000, step=1000)
            
            submitted_acc = st.form_submit_button("新增帳戶")
            if submitted_acc and acc_name:
                new_acc_conn = sqlite3.connect(DB_FILE)
                cursor = new_acc_conn.cursor()
                try:
                    cursor.execute("INSERT INTO accounts (account_name, balance) VALUES (?, ?)", (acc_name, acc_bal))
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
                    cursor.execute("UPDATE accounts SET balance = ? WHERE account_name = ?", (new_balance, selected_account))
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
                    cursor.execute("SELECT COUNT(*) FROM transactions WHERE account = ?", (delete_account,))
                    tx_count = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM recurring_bills WHERE account = ?", (delete_account,))
                    bill_count = cursor.fetchone()[0]
                    if tx_count > 0 or bill_count > 0:
                        st.error("無法刪除：該帳戶仍有交易或固定收支紀錄。請先移除相關紀錄後再試。")
                    else:
                        cursor.execute("DELETE FROM accounts WHERE account_name = ?", (delete_account,))
                        check_conn.commit()
                        st.success(f"已刪除帳戶：{delete_account}")
                        st.rerun()
                    check_conn.close()