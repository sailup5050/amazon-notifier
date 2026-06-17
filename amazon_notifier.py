import os
import datetime
import time
import sqlite3
import requests

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            status TEXT,
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

def get_order_items(order_id, token):
    url = f"https://sellingpartnerapi-fe.amazon.com/orders/v0/orders/{order_id}/orderItems"
    headers = {"x-amz-access-token": token}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("payload", {}).get("OrderItems", [])
    return []

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
        
        # 💡 【新着通知用】保留中の場合は「確認中」、それ以外は「￥金額」にします
        amount = order.get('OrderTotal', {}).get('Amount')
        total_price = "確認中（保留注文）" if current_status == "Pending" or not amount else f"￥{amount}"
        
        purchase_date_jst = format_to_jst(order.get('PurchaseDate'))
        
        cursor.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        
        if row is None:
            items = get_order_items(order_id, token)
            status_
