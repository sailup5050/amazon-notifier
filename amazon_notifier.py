import os
import datetime
import time
import sqlite3
import requests

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💰 【原価・粗利計算のカスタマイズ設定】
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定①：商品（ASIN）ごとに個別に原価（仕入れ値）を設定する場合
# 例: "ASINコード": 仕入れ値(円)
# ここに登録した商品が売れると、販売価格からこの原価を引いて粗利を計算します。
PRODUCT_COSTS = {
    "B0XXXXXXXX": 1200, 
    "B0YYYYYYYY": 2500,
}

# 設定②：上記に登録していない商品が一律で売れた場合の「粗利率」
# 例：原価がわからない商品は、売上の「60%」を利益とする場合は 0.6 にします。
DEFAULT_PROFIT_RATE = 0.6
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🛠️ 【自動運用設定】GitHubの暗箱（環境変数）から安全に鍵を読み込みます
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REFRESH_TOKEN         = os.environ.get("REFRESH_TOKEN")
CLIENT_ID             = os.environ.get("CLIENT_ID")
CLIENT_SECRET         = os.environ.get("CLIENT_SECRET")
DISCORD_WEBHOOK       = os.environ.get("DISCORD_WEBHOOK")

# 🕒 過去48時間の注文一覧を「何時間ごと」に通知するか
ORDER_INTERVAL_HOURS   = 2
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MARKETPLACE_ID_JP = "A1VC38T7YXB528"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "amazon_orders.db")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 💡 売上集計用に高度化した新しいテーブルを作成します
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_sales (
            order_id TEXT PRIMARY KEY,
            status TEXT,
            total_sales REAL,
            total_profit REAL,
            purchase_date TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_access_token():
    url = "https://api.amazon.com/auth/o2/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    response = requests.post(url, data=payload)
    if response.status_code == 200:
        return response.json().get("access_token")
    raise Exception(f"認証トークンの取得に失敗: {response.text}")

def format_to_jst(utc_date_str):
    try:
        date_str = utc_date_str.replace('Z', '')
        if '.' in date_str:
            date_str = date_str.split('.')[0]
        utc_dt = datetime.datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        jst_dt = utc_dt.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
        return jst_dt.strftime('%Y/%m/%d %H:%M')
    except:
        return utc_date_str

def format_to_jst_iso(utc_date_str):
    try:
        date_str = utc_date_str.replace('Z', '')
        if '.' in date_str:
            date_str = date_str.split('.')[0]
        utc_dt = datetime.datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        jst_dt = utc_dt.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
        return jst_dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')

def get_order_items(order_id, token):
    url = f"https://sellingpartnerapi-fe.amazon.com/orders/v0/orders/{order_id}/orderItems"
    headers = {"x-amz-access-token": token}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("payload", {}).get("OrderItems", [])
    return []

def calculate_sales_and_profit(order_id, current_status, order_total_amount, token):
    """注文内の商品ごとに売上と粗利（利益）を正確に計算する"""
    if current_status == "Pending":
        return 0.0, 0.0
        
    try:
        order_total = float(order_total_amount)
    except:
        order_total = 0.0
        
    items = get_order_items(order_id, token)
    if not items:
        return order_total, order_total * DEFAULT_PROFIT_RATE
        
    total_sales = 0.0
    total_profit = 0.0
    
    for item in items:
        asin = item.get('ASIN')
        qty = int(item.get('QuantityOrdered', 1))
        item_sales = float(item.get('ItemPrice', {}).get('Amount', 0.0))
        
        if item_sales == 0.0 and len(items) == 1:
            item_sales = order_total
            
        # 原価引き算ロジック
        if asin in PRODUCT_COSTS:
            item_cost = PRODUCT_COSTS[asin] * qty
            item_profit = item_sales - item_cost
        else:
            item_profit = item_sales * DEFAULT_PROFIT_RATE
            
        total_sales += item_sales
        total_profit += item_profit
        
    if total_sales == 0.0 and order_total > 0.0:
        total_sales = order_total
        total_profit = order_total * DEFAULT_PROFIT_RATE
        
    return total_sales, total_profit

def check_new_and_shipped_orders(token):
    three_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"🔄 新着・発送状況をスキャン中...")
    
    url = "https://sellingpartnerapi-fe.amazon.com/orders/v0/orders"
    headers = {"x-amz-access-token": token}
    params = {
        "MarketplaceIds": MARKETPLACE_ID_JP,
        "CreatedAfter": three_days_ago,
        "OrderStatuses": "Pending,Unshipped,PartiallyShipped,Shipped"
    }
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"❌ 注文取得に失敗: {response.text}")
        return
        
    orders = response.json().get("payload", {}).get("Orders", [])
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    for order in orders:
        order_id = order.get('AmazonOrderId')
        current_status = order.get('OrderStatus')
        order_total_amount = order.get('OrderTotal', {}).get('Amount', '0')
        purchase_date_raw = order.get('PurchaseDate')
        
        purchase_date_iso = format_to_jst_iso(purchase_date_raw)
        purchase_date_display = format_to_jst(purchase_date_raw)
        
        cursor.execute("SELECT status FROM order_sales WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        
        # 売上高と粗利を計算
        sales, profit = calculate_sales_and_profit(order_id, current_status, order_total_amount, token)
        
        if row is None:
            items = get_order_items(order_id, token)
            status_label = "【保留中】" if current_status == "Pending" else ""
            if current_status == "Pending" or not order_total_amount or str(order_total_amount) in ["0", "0.00", "0.0"]:
                total_price_display = "確認中（保留・処理中注文）"
            else:
                total_price_display = f"￥{order_total_amount}"
            
            for item in items:
                title = item.get('Title', '商品名取得不可')
                qty = item.get('QuantityOrdered', 1)
                
                msg = (
                    f"🎉 **【Amazon】✨新着注文が入りました！{status_label}**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📅 注文日時: {purchase_date_display} (日本時間)\n"
                    f"📦 商品名: {title}\n"
                    f"🔢 数量: {qty}\n"
                    f"💰 金額: {total_price_display}\n"
                    f"🆔 注文ID: {order_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_discord(msg)
            cursor.execute("INSERT INTO order_sales VALUES (?, ?, ?, ?, ?, ?)", 
                           (order_id, current_status, sales, profit, purchase_date_iso, datetime.datetime.now().isoformat()))
            
        else:
            past_status = row[0]
            if past_status != current_status:
                if past_status != "Shipped" and current_status == "Shipped":
                    items = get_order_items(order_id, token)
                    for item in items:
                        title = item.get('Title', '商品名取得不可')
                        
                        msg = (
                            f"🚚 **【Amazon】📦商品が発送されました！**\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"📅 注文日時: {purchase_date_display} (日本時間)\n"
                            f"📦 商品名: {title}\n"
                            f"🆔 注文ID: {order_id}\n"
                            f"✨ ステータス: Amazon側で出荷完了を確認しました\n"
                            f"━━━━━━━━━━━━━━━━━━━"
                        )
                        send_discord(msg)
                
                # 保留から確定に変わった場合なども含め、売上データを最新状態に更新
                cursor.execute("UPDATE order_sales SET status = ?, total_sales = ?, total_profit = ?, updated_at = ? WHERE order_id = ?", 
                               (current_status, sales, profit, datetime.datetime.now().isoformat(), order_id))
    
    conn.commit()
    conn.close()

def check_and_send_order_summary(token):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = 'last_order_summary_time'")
    row = cursor.fetchone()
    
    now = datetime.datetime.now()
    if row is not None and now - datetime.datetime.fromisoformat(row[0]) < datetime.timedelta(hours=ORDER_INTERVAL_HOURS):
        print("🕒 注文一覧の通知時間ではないためスキップします。")
        conn.close()
        return
        
    print("📋 過去48時間の注文一覧を生成中...")
    forty_eight_hours_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    url = "https://sellingpartnerapi-fe.amazon.com/orders/v0/orders"
    headers = {"x-amz-access-token": token}
    params = {
        "MarketplaceIds": MARKETPLACE_ID_JP,
        "CreatedAfter": forty_eight_hours_ago,
        "OrderStatuses": "Pending,Unshipped,PartiallyShipped,Shipped"
    }
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"❌ 一覧取得失敗: {response.text}")
        conn.close()
        return
        
    orders = response.json().get("payload", {}).get("Orders", [])
    if not orders:
        summary_message = "📋 **【Amazon】過去48時間以内に注文はありませんでした。**"
    else:
        summary_message = f"📋 **【Amazon】過去48時間の注文一覧（合計: {len(orders)} 件）**\n━━━━━━━━━━━━━━━━━━━\n"
        for index, order in enumerate(orders, 1):
            order_id = order.get('AmazonOrderId')
            status = order.get('OrderStatus')
            
            if status == "Pending":
                status_ja = "保留中"
            elif status in ["Unshipped", "PartiallyShipped"]:
                status_ja = "未出荷"
            else:
                status_ja = "発送済み"
                
            amount = order.get('OrderTotal', {}).get('Amount')
            if status == "Pending" or not amount or str(amount) in ["0", "0.00", "0.0"]:
                total_price = "確認中（保留・処理中注文）"
            else:
                total_price = f"￥{amount}"
            
            purchase_date_jst = format_to_jst(order.get('PurchaseDate'))
            
            items = get_order_items(order_id, token)
            titles = [f"{item.get('Title', '不明')} (x{item.get('QuantityOrdered', 1)})" for item in items]
            summary_message += (
                f"🔹 {index}. 【{status_ja}】 {total_price}\n"
                f" ├ 注文日時: {purchase_date_jst}\n"
                f" └ 商品: {' / '.join(titles)}\n"
                f" └ ID: {order_id}\n"
                f"-----------------------------------\n"
            )
        summary_message += "━━━━━━━━━━━━━━━━━━━"
        
    send_discord(summary_message)
    cursor.execute("INSERT OR REPLACE INTO config VALUES ('last_order_summary_time', ?)", (now.isoformat(),))
    conn.commit()
    conn.close()
    print("✅ 過去48時間の一覧通知完了。")

def check_and_send_sales_report():
    """💡 【新設】12時間ごとに直近12時間と当月の売上・粗利を集計して通知する"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = 'last_sales_report_time'")
    row = cursor.fetchone()
    
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    
    if row is not None:
        last_time = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        if now_jst - last_time < datetime.timedelta(hours=12):
            print("🕒 定期売上報告の通知時間ではないためスキップします。")
            conn.close()
            return
            
    print("💰 定期売上レポート（12時間/今月/粗利）を生成中...")
    
    time_12h_ago = (now_jst - datetime.timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
    time_month_start = now_jst.strftime('%Y-%m-01 00:00:00')
    
    # 直近12時間の売上高・粗利を集計 (Pendingは売上未確定のため除外)
    cursor.execute("""
        SELECT SUM(total_sales), SUM(total_profit) FROM order_sales 
        WHERE purchase_date >= ? AND status != 'Pending'
    """, (time_12h_ago,))
    row_12h = cursor.fetchone()
    sales_12h = row_12h[0] if row_12h[0] is not None else 0.0
    profit_12h = row_12h[1] if row_12h[1] is not None else 0.0
    
    # 当月に入ってからの累計売上高・粗利を集計
    cursor.execute("""
        SELECT SUM(total_sales), SUM(total_profit) FROM order_sales 
        WHERE purchase_date >= ? AND status != 'Pending'
    """, (time_month_start,))
    row_month = cursor.fetchone()
    sales_month = row_month[0] if row_month[0] is not None else 0.0
    profit_month = row_month[1] if row_month[1] is not None else 0.0
    
    current_month_str = now_jst.strftime('%m月')
    report_time_str = now_jst.strftime('%Y/%m/%d %H:%M')
    
    msg = (
        f"📊 **【Amazon】定期売上・利益報告**\n"
        f"⏱️ 集計日時: {report_time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 **直近12時間の業績**\n"
        f" ├ 💰 売上高: ￥{int(sales_12h):,}\n"
        f" └ ✨ 粗利益: ￥{int(profit_12h):,}\n"
        f"-----------------------------------\n"
        f"📅 **{current_month_str}の累計業績**\n"
        f" ├ 💰 総売上: ￥{int(sales_month):,}\n"
        f" └ ✨ 総粗利: ￥{int(profit_month):,}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"※保留中(Pending)の注文は集計に含まれていません。"
    )
    
    send_discord(msg)
    cursor.execute("INSERT OR REPLACE INTO config VALUES ('last_sales_report_time', ?)", (now_jst.isoformat(),))
    conn.commit()
    conn.close()
    print("✅ 定期売上報告の通知完了。")

def send_discord(text):
    try:
        if len(text) > 2000:
            requests.post(DISCORD_WEBHOOK, json={"content": text[:1900] + "\n...(続く)"})
            requests.post(DISCORD_WEBHOOK, json={"content": "続き:\n" + text[1900:]})
        else:
            requests.post(DISCORD_WEBHOOK, json={"content": text})
    except Exception as e:
        print(f"Discord送信エラー: {e}")

if __name__ == "__main__":
    try:
        init_db()
        token = get_access_token()
        
        check_new_and_shipped_orders(token)
        check_and_send_order_summary(token)
        check_and_send_sales_report()  # 💡 売上集計タスクを実行
        
        print("🎉 すべての処理が正常に終了しました。")
    except Exception as e:
        print(f"❌ エラーが発生しました: {e}")
