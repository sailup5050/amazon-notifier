import os
import time
import gzip
import json
import datetime
import requests

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💡 仕入れ値はGoogleスプレッドシートの「原価設定」タブから自動取得します
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_PROFIT_RATE = 0.4  # スプレッドシートに登録がない商品の利益率（40%）
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

def get_spreadsheet_costs():
    print("📋 スプレッドシートから仕入れ値データを読み込み中...")
    try:
        res = requests.get(INVENTORY_GAS_URL)
        if res.status_code == 200:
            costs = res.json()
            print(f"✅ 仕入れ値データの読み込み成功 ({len(costs)}件の商品マスタ)")
            return costs
    except Exception as e:
        print(f"⚠️ スプレッドシートからの仕入れ値取得に失敗したため、一律計算を行います: {e}")
    return {}

def request_and_download_report(token, report_type, extra_body=None):
    url = "https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/reports"
    headers = {"x-amz-access-token": token, "Content-Type": "application/json"}
    
    body = {"reportType": report_type, "marketplaceIds": [MARKETPLACE_ID_JP]}
    if extra_body:
        body.update(extra_body)
        
    res = requests.post(url, headers=headers, json=body)
    if res.status_code != 202:
        raise Exception(f"要求失敗 ({res.status_code}): {res.text}")
        
    report_id = res.json().get("reportId")
    
    report_doc_id = None
    for i in range(30):
        time.sleep(10)
        print(f"⏱️ Amazon側の集計完了を待っています... [{report_type}] ({ (i+1)*10 }秒経過)")
        check_res = requests.get(f"{url}/{report_id}", headers=headers)
        res_json = check_res.json()
        status = res_json.get("processingStatus")
        
        if status == "DONE":
            report_doc_id = res_json.get("reportDocumentId")
            break
        elif status == "FATAL":
            reason = res_json.get("statusReason", "理由詳細は返されませんでした")
            raise Exception(f"Amazon内部エラー(FATAL)。理由: {reason}")
            
    if not report_doc_id:
        raise Exception(f"制限時間（5分）を超過しました。")
        
    doc_res = requests.get(f"https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/documents/{report_doc_id}", headers=headers)
    doc_data = doc_res.json()
    
    download_res = requests.get(doc_data.get("url"))
    content = download_res.content
    if doc_data.get("compressionAlgorithm") == "GZIP":
        content = gzip.decompress(content)
        
    # 💡【超重要修正】Amazon Japan特有の文字コード（Shift-JIS）に対応！
    # 英語標準(UTF-8)でエラーになったら、日本語(cp932)で読み込み直します
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            return content.decode('cp932')
        except UnicodeDecodeError:
            return content.decode('utf-8', errors='replace')

def parse_listings_tsv(tsv_text, target_map):
    lines = tsv_text.strip().split('\n')
    if len(lines) <= 1:
        return
    headers = [h.strip().lower() for h in lines[0].split('\t')]
    
    idx_sku = next((i for i, h in enumerate(headers) if 'sku' in h or '出品者' in h), None)
    idx_asin = next((i for i, h in enumerate(headers) if 'asin' in h or 'product-id' in h or '商品id' in h), None)
    idx_name = next((i for i, h in enumerate(headers) if 'name' in h or 'title' in h or '商品名' in h), None)
    idx_price = next((i for i, h in enumerate(headers) if 'price' in h or '価格' in h), None)
    
    for line in lines[1:]:
        cols = line.split('\t')
        if idx_sku is not None and idx_asin is not None and len(cols) > max(idx_sku, idx_asin):
            sku = cols[idx_sku].strip()
            asin = cols[idx_asin].strip()
            name = cols[idx_name].strip() if idx_name is not None and len(cols) > idx_name else ""
            price_str = cols[idx_price].strip() if idx_price is not None and len(cols) > idx_price else "0"
            try: price = float(price_str)
            except: price = 0.0
            
            if len(name) > 50:
                name = name[:50] + "..."
            if asin:
                target_map[asin] = {"sku": sku, "title": name, "price": price}

def parse_fba_inventory(tsv_text, stock_map, backup_map):
    lines = tsv_text.strip().split('\n')
    if len(lines) <= 1:
        return
    headers = [h.strip().lower() for h in lines[0].split('\t')]
    
    idx_asin = next((i for i, h in enumerate(headers) if 'asin' in h or '商品id' in h), None)
    idx_qty = next((i for i, h in enumerate(headers) if 'quantity' in h or 'qty' in h or 'fulfillable' in h or '数量' in h or '販売可能' in h), None)
    idx_sku = next((i for i, h in enumerate(headers) if 'sku' in h or '出品者' in h), None)
    idx_name = next((i for i, h in enumerate(headers) if 'name' in h or 'title' in h or '商品名' in h), None)
    idx_price = next((i for i, h in enumerate(headers) if 'price' in h or '価格' in h), None)
    
    if idx_asin is None or idx_qty is None:
        return
    for line in lines[1:]:
        cols = line.split('\t')
        if len(cols) > max(idx_asin, idx_qty):
            asin = cols[idx_asin].strip()
            qty_str = cols[idx_qty].strip()
            try: qty = int(qty_str)
            except: qty = 0
            
            if asin:
                stock_map[asin] = stock_map.get(asin, 0) + qty
                
                if asin not in backup_map:
                    backup_map[asin] = {}
                if idx_sku is not None and len(cols) > idx_sku and cols[idx_sku].strip():
                    backup_map[asin]['sku'] = cols[idx_sku].strip()
                if idx_name is not None and len(cols) > idx_name and cols[idx_name].strip():
                    name = cols[idx_name].strip()
                    backup_map[asin]['title'] = name[:50] + "..." if len(name) > 50 else name
                if idx_price is not None and len(cols) > idx_price and cols[idx_price].strip():
                    try: backup_map[asin]['price'] = float(cols[idx_price].strip())
                    except: pass

def main():
    try:
        token = get_access_token()
        product_costs = get_spreadsheet_costs()
        
        listings_map = {}
        listings_backup = {}
        fba_stock_map = {}
        traffic_data = {}
        
        print("🔄 Amazonに出品レポート(軽量版)を要求中...")
        try:
            listings_tsv = request_and_download_report(token, "GET_MERCHANT_LISTINGS_DATA")
            parse_listings_tsv(listings_tsv, listings_map)
            print("✅ 軽量版出品レポートからマスターデータを構築しました。")
        except Exception as l_e:
            print(f"⚠️ 軽量版制限のため全データ版で再トライします: {l_e}")
            try:
                listings_tsv = request_and_download_report(token, "GET_MERCHANT_LISTINGS_ALL_DATA")
                parse_listings_tsv(listings_tsv, listings_map)
                print("✅ 全データ版出品レポートからマスターデータを構築しました。")
            except Exception as l_e2:
                print(f"⚠️ 出品レポートAPI制限。在庫データ側から復元します: {l_e2}")
        
        print("🔄 AmazonにFBA在庫レポートを要求中...")
        try:
            fba_tsv = request_and_download_report(token, "GET_AFN_INVENTORY_DATA")
            parse_fba_inventory(fba_tsv, fba_stock_map, listings_backup)
            print("✅ FBA在庫レポート(AFN版)の解析に成功しました。")
        except Exception as e1:
            print(f"⚠️ AFN版在庫取得スキップ: {e1}。予備の在庫レポートを試します...")
            try:
                fba_tsv = request_and_download_report(token, "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA")
                parse_fba_inventory(fba_tsv, fba_stock_map, listings_backup)
                print("✅ 予備のFBA在庫レポートの解析に成功しました。")
            except Exception as e2:
                print(f"⚠️ 在庫レポートが両方スキップされました: {e2}")
        
        print("🔄 直近30日間のPV・アクセスデータを取得中...")
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            start_date = (now - datetime.timedelta(days=32)).strftime('%Y-%m-%dT00:00:00Z')
            end_date = (now - datetime.timedelta(days=2)).strftime('%Y-%m-%dT00:00:00Z')
            
            traffic_json_str = request_and_download_report(
                token, 
                "GET_SALES_AND_TRAFFIC_REPORT", 
                extra_body={"dataStartTime": start_date, "dataEndTime": end_date}
            )
            traffic_data = json.loads(traffic_json_str)
            print("✅ ビジネスレポート（PV・売上）の取得に成功しました。")
        except Exception as tr_e:
            print(f"❌ ビジネスレポートの取得に失敗したため、処理を中断します: {tr_e}")
            return
        
        traffic_map = {}
        for item in traffic_data.get("salesAndTrafficByAsin", []):
            asin = item.get("childAsin") or item.get("parentAsin") or item.get("asin")
            if not asin:
                continue
            
            sales_stats = item.get("salesByAsin", {})
            traffic_stats = item.get("trafficByAsin", {})
            
            pv = traffic_stats.get("pageViews", 0)
            sessions = traffic_stats.get("sessions", 0)
            units_sold = sales_stats.get("unitsOrdered", 0)
            revenue = float(sales_stats.get("orderedProductSales", {}).get("amount", 0.0))
            
            traffic_map[asin] = {
                "pv": pv, "sessions": sessions, "units_sold": units_sold, "revenue": revenue
            }

        all_asins = set(listings_map.keys()) | set(fba_stock_map.keys()) | set(traffic_map.keys())
        sheet_payload = []
        
        total_stock_items = 0
        total_stock_qty = 0
        total_pv_30d = 0
        total_sessions_30d = 0
        total_sales_30d = 0
        total_revenue_30d = 0
        total_profit_30d = 0
        
        for asin in all_asins:
            l_data = listings_map.get(asin, {})
            b_data = listings_backup.get(asin, {})
            
            sku = l_data.get("sku") or b_data.get("sku") or "自動取得エラー(出品停止等の可能性)"
            title = l_data.get("title") or b_data.get("title") or f"出品中商品 ({asin})"
            price = l_data.get("price") or b_data.get("price") or 0.0
            
            qty = fba_stock_map.get(asin, 0)
            t_data = traffic_map.get(asin, {"pv": 0, "sessions": 0, "units_sold": 0, "revenue": 0.0})
            pv = t_data["pv"]
            sessions = t_data["sessions"]
            units_sold = t_data["units_sold"]
            revenue = t_data["revenue"]
            
            if price == 0.0 and units_sold > 0 and revenue > 0:
                price = revenue / units_sold
            
            if asin in product_costs:
                cost = float(product_costs[asin])
                profit_30d = revenue - (units_sold * cost)
            else:
                profit_30d = revenue * DEFAULT_PROFIT_RATE
                cost = price * (1 - DEFAULT_PROFIT_RATE)
                
            if qty > 0:
                total_stock_items += 1
                total_stock_qty += qty
            total_pv_30d += pv
            total_sessions_30d += sessions
            total_sales_30d += units_sold
            total_revenue_30d += revenue
            total_profit_30d += profit_30d
            
            if qty > 0 or pv > 0 or units_sold > 0:
                sheet_payload.append({
                    "sku": sku, "asin": asin, "title": title, "quantity": qty,
                    "price": int(price), "cost": int(cost), "pv": pv, "sessions": sessions,
                    "sales_30d": units_sold, "revenue_30d": int(revenue), "profit_30d": int(profit_30d)
                })
            
        print("📊 Googleスプレッドシートへ完全データを送信中...")
        requests.post(INVENTORY_GAS_URL, json=sheet_payload)
        
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
        )
        
        if DISCORD_WEBHOOK_SUMMARY:
            requests.post(DISCORD_WEBHOOK_SUMMARY, json={"content": discord_msg})
            print("🎉 すべての処理が正常完了しました！")
            
    except Exception as e:
        print(f"❌ エラー発生: {e}")

if __name__ == "__main__":
    main()
