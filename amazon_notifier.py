import os
import datetime
import time
import gzip
import json
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

# 🕒 閲覧数トップ5を「何時間ごと」に通知するか
TRAFFIC_INTERVAL_HOURS = 24
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

def get_product_title_api(asin, token):
    """Amazon公式のカタログAPIを使って、ASINから正確な商品名を取得する"""
    try:
        url = f"https://sellingpartnerapi-fe.amazon.com/catalog/2022-04-01/items/{asin}"
        headers = {"x-amz-access-token": token}
        params = {
            "marketplaceIds": MARKETPLACE_ID_JP,
            "includedData": "summaries"
        }
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200:
            summaries = res.json().get("summaries", [])
            if summaries:
                title = summaries[0].get("itemName", "商品名が未設定です")
                if len(title) > 60:
                    title = title[:60] + "..."
                return title
        else:
            print(f"カタログAPIエラー(ASIN: {asin}): {res.text}")
    except Exception as e:
        print(f"カタログAPI接続失敗(ASIN: {asin}): {e}")
    return "商品名を取得できませんでした"

def check_new_and_shipped_orders(token):
    three_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"🔄 新着・発送状況をスキャン中...")
    
    url = "https://sellingpartnerapi-fe.amazon.com/orders/v0/orders"
    headers = {"x-amz-access-token": token}
    params = {
        "MarketplaceIds": MARKETPLACE_ID_JP,
        "CreatedAfter": three_days_ago,
        "OrderStatuses": "Unshipped,PartiallyShipped,Shipped"
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
        total_price = order.get('OrderTotal', {}).get('Amount', '0')
        purchase_date_jst = format_to_jst(order.get('PurchaseDate'))
        
        cursor.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        
        if row is None:
            items = get_order_items(order_id, token)
            for item in items:
                title = item.get('Title', '商品名取得不可')
                qty = item.get('QuantityOrdered', 1)
                
                msg = (
                    f"🎉 **【Amazon】✨新着注文が入りました！**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📅 注文日時: {purchase_date_jst} (日本時間)\n"
                    f"📦 商品名: {title}\n"
                    f"🔢 数量: {qty}\n"
                    f"💰 金額: ￥{total_price}\n"
                    f"🆔 注文ID: {order_id}\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_discord(msg)
            cursor.execute("INSERT INTO orders VALUES (?, ?, ?)", (order_id, current_status, datetime.datetime.now().isoformat()))
            
        else:
            past_status = row[0]
            if past_status != "Shipped" and current_status == "Shipped":
                items = get_order_items(order_id, token)
                for item in items:
                    title = item.get('Title', '商品名取得不可')
                    
                    msg = (
                        f"🚚 **【Amazon】📦商品が発送されました！**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📅 注文日時: {purchase_date_jst} (日本時間)\n"
                        f"📦 商品名: {title}\n"
                        f"🆔 注文ID: {order_id}\n"
                        f"✨ ステータス: Amazon側で出荷完了を確認しました\n"
                        f"━━━━━━━━━━━━━━━━━━━"
                    )
                    send_discord(msg)
                cursor.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", (current_status, datetime.datetime.now().isoformat(), order_id))
    
    conn.commit()
    conn.close()

def check_and_send_order_summary(token):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = 'last_order_summary_time'")
    row = cursor.fetchone()
    
    now = datetime.datetime.now()
    if row is not None and now - datetime.datetime.fromisoformat(row[0]) < datetime.timedelta(hours=ORDER_INTERVAL_HOHours):
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
        "OrderStatuses": "Unshipped,PartiallyShipped,Shipped"
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
            status_ja = "未出荷" if status in ["Unshipped", "PartiallyShipped"] else "発送済み"
            total_price = order.get('OrderTotal', {}).get('Amount', '0')
            purchase_date_jst = format_to_jst(order.get('PurchaseDate'))
            
            items = get_order_items(order_id, token)
            titles = [f"{item.get('Title', '不明')} (x{item.get('QuantityOrdered', 1)})" for item in items]
            summary_message += (
                f"🔹 {index}. 【{status_ja}】 ￥{total_price}\n"
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

def check_and_send_traffic_report(token):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = 'last_traffic_summary_time'")
    row = cursor.fetchone()
    
    now = datetime.datetime.now()
    if row is not None and now - datetime.datetime.fromisoformat(row[0]) < datetime.timedelta(hours=TRAFFIC_INTERVAL_HOURS):
        print("🕒 閲覧数レポートの通知時間ではないためスキップします。")
        conn.close()
        return
        
    print("📊 閲覧数（トラフィック）レポートを生成リクエスト中...")
    start_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).strftime('%Y-%m-%dT00:00:00Z')
    end_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime('%Y-%m-%dT23:59:59Z')
    
    url = "https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/reports"
    headers = {"x-amz-access-token": token, "Content-Type": "application/json"}
    body = {
        "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
        "marketplaceIds": [MARKETPLACE_ID_JP],
        "dataStartTime": start_time,
        "dataEndTime": end_time
    }
    
    res = requests.post(url, headers=headers, json=body)
    if res.status_code != 202:
        print(f"❌ レポート要求に失敗: {res.text}")
        conn.close()
        return
        
    report_id = res.json().get("reportId")
    print("⏳ Amazonが集計中（約20秒待機）...")
    
    report_doc_id = None
    for _ in range(6):
        time.sleep(10)
        check_url = f"https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/reports/{report_id}"
        check_res = requests.get(check_url, headers=headers)
        if check_res.json().get("processingStatus") == "DONE":
            report_doc_id = check_res.json().get("reportDocumentId")
            break
            
    if not report_doc_id:
        print("❌ レポート生成タイムアウト")
        conn.close()
        return
        
    doc_url = f"https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/documents/{report_doc_id}"
    doc_res = requests.get(doc_url, headers=headers)
    doc_data = doc_res.json()
    
    download_url = doc_data.get("url")
    compression = doc_data.get("compressionAlgorithm")
    
    download_res = requests.get(download_url)
    raw_content = download_res.content
    
    if compression == "GZIP":
        try:
            raw_content = gzip.decompress(raw_content)
            print("📦 Amazonの圧縮データを正常に解凍しました。")
        except Exception as gzip_err:
            print(f"⚠️ 解凍処理で警告が発生しました: {gzip_err}")

    try:
        report_data = json.loads(raw_content.decode('utf-8'))
    except Exception as parse_err:
        print(f"❌ JSONパースエラーが発生しました: {parse_err}")
        conn.close()
        return
        
    asin_data = report_data.get("salesAndTrafficByAsin", [])
    
    if asin_data:
        sorted_items = sorted(asin_data, key=lambda x: x.get("trafficByAsin", {}).get("pageViews", 0), reverse=True)
        top5_message = "📈 **【Amazon】直近の閲覧数トップ5商品**\n━━━━━━━━━━━━━━━━━━━\n"
        
        for index, item in enumerate(sorted_items[:5], 1):
            asin = item.get("asin")
            views = item.get("trafficByAsin", {}).get("pageViews", 0)
            sessions = item.get("trafficByAsin", {}).get("sessions", 0)
            
            # 💡 最新の公式カタログAPIから商品名を取得
            product_title = get_product_title_api(asin, token)
            
            top5_message += (
                f"🥇第{index}位\n"
                f"📦 商品名: {product_title}\n"
                f"🔗 ASIN: {asin} (https://www.amazon.co.jp/dp/{asin})\n"
                f"👀 閲覧数(PV): {views}回 / 訪問者数: {sessions}人\n"
                f"-----------------------------------\n"
            )
        top5_message += "━━━━━━━━━━━━━━━━━━━"
        send_discord(top5_message)
    else:
        print("閲覧数データが空でした。")
        
    cursor.execute("INSERT OR REPLACE INTO config VALUES ('last_traffic_summary_time', ?)", (now.isoformat(),))
    conn.commit()
    conn.close()
    print("✅ 閲覧数トップ5の通知完了。")

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
        check_and_send_traffic_report(token)
        
        print("🎉 すべての処理が正常に終了しました。")
    except Exception as e:
        print(f"❌ エラーが発生しました: {e}")
