import os
import time
import gzip
import json
import datetime
import requests

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💰 【仕入れ値・原価設定ボックス】
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRODUCT_COSTS = {
    "B0XXXXXXXX": 1200, 
    "B0YYYYYYYY": 2500,
}
DEFAULT_PROFIT_RATE = 0.4  # 原価未登録商品の利益率（40%）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REFRESH_TOKEN           = os.environ.get("REFRESH_TOKEN")
CLIENT_ID               = os.environ.get("CLIENT_ID")
CLIENT_SECRET           = os.environ.get("CLIENT_SECRET")
INVENTORY_GAS_URL       = os.environ.get("INVENTORY_GAS_URL")
DISCORD_WEBHOOK_SUMMARY = os.environ.get("DISCORD_WEBHOOK_SUMMARY")
MARKETPLACE_ID_JP       = "A1VC38T7YXB528"

def get_access_token():
    url = "https://api.amazon.com/auth/o2/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    res = requests.post(url, data=payload)
    if res.status_code == 200:
        return res.json().get("access_token")
    raise Exception("認証トークンの取得に失敗しました")

def request_and_download_report(token, report_type, extra_body=None):
    """Amazon SP-APIから指定されたレポートを要求してダウンロードする共通関数"""
    url = "https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/reports"
    headers = {"x-amz-access-token": token, "Content-Type": "application/json"}
    
    body = {"reportType": report_type, "marketplaceIds": [MARKETPLACE_ID_JP]}
    if extra_body:
        body.update(extra_body)
        
    res = requests.post(url, headers=headers, json=body)
    if res.status_code != 202:
        raise Exception(f"レポート[{report_type}]要求失敗: {res.text}")
        
    report_id = res.json().get("reportId")
    
    report_doc_id = None
    for _ in range(12):  # 最大2分間待機
        time.sleep(10)
        check_res = requests.get(f"{url}/{report_id}", headers=headers)
        status = check_res.json().get("processingStatus")
        if status == "DONE":
            report_doc_id = check_res.json().get("reportDocumentId")
            break
        elif status == "FATAL":
            raise Exception(f"Amazon側でレポート[{report_type}]の生成が致命的エラーになりました")
            
    if not report_doc_id:
        raise Exception(f"レポート[{report_type}]生成タイムアウト")
        
    doc_res = requests.get(f"https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/documents/{report_doc_id}", headers=headers)
    doc_data = doc_res.json()
    
    download_res = requests.get(doc_data.get("url"))
    content = download_res.content
    if doc_data.get("compressionAlgorithm") == "GZIP":
        content = gzip.decompress(content)
        
    return content.decode('utf-8')

def main():
    try:
        token = get_access_token()
        
        # 1. FBA在庫レポートの取得 (TSV)
        print("🔄 FBA在庫データを取得中...")
        inventory_tsv = request_and_download_report(token, "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA")
        
        # 2. ビジネスレポート（PV・トラフィック）の取得 (JSON)
        # Amazonのデータ集計ラグ(48時間)を考慮し、32日前〜2日前までの30日間を指定
        print("🔄 直近30日間のPV・アクセスデータを取得中...")
        now = datetime.datetime.now(datetime.timezone.utc)
        start_date = (now - datetime.timedelta(days=32)).strftime('%Y-%m-%dT00:00:00Z')
        end_date = (now - datetime.timedelta(days=2)).strftime('%Y-%m-%dT00:00:00Z')
        
        traffic_json_str = request_and_download_report(
            token, 
            "GET_SALES_AND_TRAFFIC_REPORT", 
            extra_body={"dataStartTime": start_date, "dataEndTime": end_date}
        )
        traffic_data = json.loads(traffic_json_str)
        
        # 3. トラフィック・過去売上データをASINごとにマッピング辞書化
        traffic_map = {}
        for item in traffic_data.get("salesAndTrafficByAsin", []):
            asin = item.get("asin")
            sales_stats = item.get("salesByAsin", {})
            traffic_stats = item.get("trafficByAsin", {})
            
            pv = traffic_stats.get("pageViews", 0)
            sessions = traffic_stats.get("sessions", 0)
            units_sold = sales_stats.get("unitsOrdered", 0)
            revenue = float(sales_stats.get("orderedProductSales", {}).get("amount", 0.0))
            
            traffic_map[asin] = {
                "pv": pv,
                "sessions": sessions,
                "units_sold": units_sold,
                "revenue": revenue
            }

        # 4. 在庫データ(TSV)を解析しながら、ビジネスレポートとマージ
        lines = inventory_tsv.strip().split('\n')
        headers = lines[0].split('\t')
        
        idx_sku = headers.index('sku')
        idx_asin = headers.index('asin')
        idx_name = headers.index('product-name')
        idx_price = headers.index('your-price')
        idx_qty = headers.index('afn-fulfillable-quantity')
        
        sheet_payload = []
        
        # Discordサマリー用の集計変数
        total_stock_items = 0
        total_stock_qty = 0
        total_pv_30d = 0
        total_sessions_30d = 0
        total_sales_30d = 0
        total_revenue_30d = 0
        total_profit_30d = 0
        
        for line in lines[1:]:
            cols = line.split('\t')
            if len(cols) <= max(idx_sku, idx_asin, idx_name, idx_price, idx_qty):
                continue
                
            sku = cols[idx_sku]
            asin = cols[idx_asin]
            title = cols[idx_name][:50] + "..." if len(cols[idx_name]) > 50 else cols[idx_name]
            
            try: qty = int(cols[idx_qty])
            except: qty = 0
            try: price = float(cols[idx_price])
            except: price = 0.0
            
            # 結合：このASINのPV・過去売上データを引っ張る
            t_data = traffic_map.get(asin, {"pv": 0, "sessions": 0, "units_sold": 0, "revenue": 0.0})
            
            pv = t_data["pv"]
            sessions = t_data["sessions"]
            units_sold = t_data["units_sold"]
            revenue = t_data["revenue"]
            
            # 💰 原価・利益の計算ロジック
            if asin in PRODUCT_COSTS:
                cost = float(PRODUCT_COSTS[asin])
                # 過去30日利益 = 過去売上高 - (過去販売個数 × 原価)
                profit_30d = revenue - (units_sold * cost)
            else:
                profit_30d = revenue * DEFAULT_PROFIT_RATE
                cost = price - (price * DEFAULT_PROFIT_RATE)
                
            # サマリー集計に加算
            if qty > 0:
                total_stock_items += 1
                total_stock_qty += qty
            total_pv_30d += pv
            total_sessions_30d += sessions
            total_sales_30d += units_sold
            total_revenue_30d += revenue
            total_profit_30d += profit_30d
            
            sheet_payload.append({
                "sku": sku, "asin": asin, "title": title, "quantity": qty,
                "price": int(price), "cost": int(cost), "pv": pv, "sessions": sessions,
                "sales_30d": units_sold, "revenue_30d": int(revenue), "profit_30d": int(profit_30d)
            })
            
        # 5. スプレッドシートへ送信
        print("📊 Googleスプレッドシートへ最新データを送信中...")
        requests.post(INVENTORY_GAS_URL, json=sheet_payload)
        
        # 6. 新しいDiscordチャンネルへサマリー通知を送信
        print("📢 独立したDiscordチャンネルへ総括サマリーを送信中...")
        report_date = datetime.datetime.now().strftime('%Y/%m/%d')
        
        discord_msg = (
            f"📊 **【Amazon】店舗経営・在庫総括レポート ({report_date})**\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📦 **現在のFBA在庫ステータス**\n"
            f" ├ 種類数: {total_stock_items} 品目\n"
            f" └ 総在庫数: {total_stock_qty} 個\n"
            f"-----------------------------------\n"
            f"📈 **直近30日間のトラフィック (PV状況)**\n"
            f" ├ 👁️ 総ページ閲覧数(PV): {total_pv_30d:,} PV\n"
            f" └ 👥 総訪問セッション数: {total_sessions_30d:,} 回\n"
            f"-----------------------------------\n"
            f"💰 **直近30日間の確定パフォーマンス**\n"
            f" ├ 🔢 確定販売個数: {total_sales_30d} 個\n"
            f" ├ 💵 確定売上高: ￥{int(total_revenue_30d):,}\n"
            f" └ ✨ 期間内確定粗利: ￥{int(total_profit_30d):,}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"※PVおよび確定業績は、Amazonの集計仕様に則り【直近32日前〜2日前までの30日間】のデータを集計しています。"
        )
        
        if DISCORD_WEBHOOK_SUMMARY:
            requests.post(DISCORD_WEBHOOK_SUMMARY, json={"content": discord_msg})
            print("🎉 すべての処理と別チャンネルへのサマリー通知が完了しました！")
        else:
            print("⚠️ DISCORD_WEBHOOK_SUMMARY が設定されていないためDiscord送信をスキップしました。")
            
    except Exception as e:
        print(f"❌ エラー発生: {e}")

if __name__ == "__main__":
    main()
