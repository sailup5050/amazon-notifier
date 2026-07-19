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
    # 💡 制限時でも待てるように待機時間を40回(約6分半)に延長
    for i in range(40):
        time.sleep(10)
        print(f"⏱️ Amazon側の集計完了を待っています... [{report_type}] ({ (i+1)*10 }秒経過)")
        check_res = requests.get(f"{url}/{report_id}", headers=headers)
        res_json = check_res.json()
        status = res_json.get("processingStatus")
        
        if status == "DONE":
            report_doc_id = res_json.get("reportDocumentId")
            break
        elif status == "FATAL" or status == "CANCELLED":
            reason = res_json.get("statusReason", "理由詳細は返されませんでした")
            raise Exception(f"Amazon内部エラーまたは制限({status})。理由: {reason}")
            
    if not report_doc_id:
        raise Exception(f"制限時間を超過しました。")
        
    doc_res = requests.get(f"https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/documents/{report_doc_id}", headers=headers)
    doc_data = doc_res.json()
    
    download_res = requests.get(doc_data.get("url"))
    content = download_res.content
    if doc_data.get("compressionAlgorithm") == "GZIP":
        content = gzip.decompress(content)
        
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            return content.decode('cp932')
        except UnicodeDecodeError:
            return content.decode('utf-8', errors='replace')

# 💡 確実な列取得のための新機能（完全一致と部分一致を使い分ける）
def get_col_idx(headers, keywords, excludes=None):
    excludes = excludes or []
    for k in keywords:
        for i, h in enumerate(headers):
            if h == k and not any(ex in h for ex in excludes): return i
    for k in keywords:
        for i, h in enumerate(headers):
            if k in h and not any(ex in h for ex in excludes): return i
    return None

def parse_listings_tsv(tsv_text, target_map):
    lines = tsv_text.strip().split('\n')
    if len(lines) <= 1: return
    headers = [h.replace('"', '').strip().lower() for h in lines[0].split('\t')]
    
    idx_sku = get_col_idx(headers, ['seller-sku', 'sku', '出品者sku', '出品者'])
    idx_asin = get_col_idx(headers, ['asin1', 'asin', 'product-id', '商品id'])
    idx_name = get_col_idx(headers, ['item-name', 'product-name', 'title', 'name', '商品名', '名称'])
    idx_price = get_col_idx(headers, ['price', 'your-price', '価格'])
    
    for line in lines[1:]:
        cols = line.split('\t')
        if idx_asin is not None and len(cols) > idx_asin:
            asin = cols[idx_asin].replace('"', '').strip()
            if not asin: continue
            
            sku = cols[idx_sku].replace('"', '').strip() if idx_sku is not None and len(cols) > idx_sku else ""
            name = cols[idx_name].replace('"', '').strip() if idx_name is not None and len(cols) > idx_name else ""
            
            price = 0.0
            if idx_price is not None and len(cols) > idx_price:
                try: price = float(cols[idx_price].replace('"', '').replace(',', '').strip())
                except: pass
            
            if len(name) > 100: name = name[:100] + "..."
            
            if asin not in target_map:
                target_map[asin] = {"sku": sku, "title": name, "price": price}
            else:
                if name: target_map[asin]["title"] = name
                if sku: target_map[asin]["sku"] = sku
                if price > 0: target_map[asin]["price"] = price

def parse_fba_inventory(tsv_text, stock_map, backup_map):
    lines = tsv_text.strip().split('\n')
    if len(lines) <= 1: return
    headers = [h.replace('"', '').strip().lower() for h in lines[0].split('\t')]
    
    idx_asin = get_col_idx(headers, ['asin', '商品id'])
    idx_sku = get_col_idx(headers, ['seller-sku', 'sku', '出品者sku', '出品者'])
    idx_name = get_col_idx(headers, ['product-name', 'item-name', 'title', 'name', '商品名', '名称'])
    idx_price = get_col_idx(headers, ['your-price', 'price', '価格'])
    
    idx_qty = get_col_idx(headers, ['afn-fulfillable-quantity', 'afn-total', '販売可能数量'], excludes=['mfn', '出品者'])
    if idx_qty is None:
        idx_qty = get_col_idx(headers, ['fulfillable', 'quantity', 'qty', '数量', '在庫'], excludes=['mfn', '出品者'])
        
    if idx_asin is None: return
        
    for line in lines[1:]:
        cols = line.split('\t')
        if len(cols) > idx_asin:
            asin = cols[idx_asin].replace('"', '').strip()
            if not asin: continue
            
            qty = 0
            if idx_qty is not None and len(cols) > idx_qty:
                try: qty = int(float(cols[idx_qty].replace('"', '').replace(',', '').strip()))
                except: pass
            
            stock_map[asin] = stock_map.get(asin, 0) + qty
            
            if asin not in backup_map:
                backup_map[asin] = {}
            
            if idx_sku is not None and len(cols) > idx_sku:
                s = cols[idx_sku].replace('"', '').strip()
                if s and not backup_map[asin].get('sku'):
                    backup_map[asin]['sku'] = s
            
            if idx_name is not None and len(cols) > idx_name:
                n = cols[idx_name].replace('"', '').strip()
                if n and not backup_map[asin].get('title'):
                    backup_map[asin]['title'] = n[:100] + "..." if len(n)>100 else n
                    
            if idx_price is not None and len(cols) > idx_price:
                try:
                    p = float(cols[idx_price].replace('"', '').replace(',', '').strip())
                    if p > 0 and not backup_map[asin].get('price'):
                        backup_map[asin]['price'] = p
                except: pass

def main():
    try:
        token = get_access_token()
        product_costs = get_spreadsheet_costs()
        
        listings_map = {}
        listings_backup = {}
        fba_stock_map = {}
        traffic_data = {}
        
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 💡 【改修】API制限対策。成功するまで複数のレポートを次々に試します
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print("🔄 Amazonに商品マスタ（商品名・SKU）レポートを要求中...")
        listings_reports = [
            "GET_MERCHANT_LISTINGS_ALL_DATA",       # 全件（停止中含む）
            "GET_MERCHANT_LISTINGS_DATA",           # アクティブのみ
            "GET_MERCHANT_LISTINGS_INACTIVE_DATA"   # 非アクティブのみ
        ]
        for r_type in listings_reports:
            try:
                tsv = request_and_download_report(token, r_type)
                parse_listings_tsv(tsv, listings_map)
                print(f"✅ {r_type} から商品データを取得しました。")
                break  # 成功したら抜ける
            except Exception as e:
                print(f"⚠️ {r_type} 制限/エラーのため次を試します: {e}")
        
        print("🔄 AmazonにFBA在庫レポートを要求中...")
        fba_reports = [
            "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA", # 商品名あり
            "GET_FBA_MYI_ALL_INVENTORY_DATA",          # 商品名あり
            "GET_AFN_INVENTORY_DATA"                   # 商品名なし（在庫数のみ・最終手段）
        ]
        for r_type in fba_reports:
            try:
                fba_tsv = request_and_download_report(token, r_type)
                parse_fba_inventory(fba_tsv, fba_stock_map, listings_backup)
                print(f"✅ FBA在庫レポート({r_type})の解析に成功しました。")
                break  # 成功したら抜ける
            except Exception as e:
                print(f"⚠️ {r_type} 制限/エラーのため次を試します: {e}")
        
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
        
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 👑 ランキング＆アラートデータの生成処理（Discord用）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        DISCORD_TITLE_LIMIT = 60 

        # PVランキング Top5
        top_pv_items = sorted([x for x in sheet_payload if x['pv'] > 0], key=lambda x: x['pv'], reverse=True)[:5]
        pv_ranking_str = ""
        if not top_pv_items:
            pv_ranking_str = " └ 期間内のアクセスデータなし\n"
        else:
            for i, item in enumerate(top_pv_items, 1):
                short_title = item['title'][:DISCORD_TITLE_LIMIT] + "..." if len(item['title']) > DISCORD_TITLE_LIMIT else item['title']
                prefix = " └" if i == len(top_pv_items) else " ├"
                pv_ranking_str += f"{prefix} {i}位: {item['pv']:,} PV ({short_title})\n"

        # 販売数ランキング Top5
        top_sales_items = sorted([x for x in sheet_payload if x['sales_30d'] > 0], key=lambda x: x['sales_30d'], reverse=True)[:5]
        sales_ranking_str = ""
        if not top_sales_items:
            sales_ranking_str = " └ 期間内の販売実績なし\n"
        else:
            for i, item in enumerate(top_sales_items, 1):
                short_title = item['title'][:DISCORD_TITLE_LIMIT] + "..." if len(item['title']) > DISCORD_TITLE_LIMIT else item['title']
                prefix = " └" if i == len(top_sales_items) else " ├"
                sales_ranking_str += f"{prefix} {i}位: {item['sales_30d']:,} 個 ({short_title})\n"

        # 低在庫アラート
        low_stock_items = sorted([x for x in sheet_payload if x['sales_30d'] > 0 and x['quantity'] <= 5], key=lambda x: x['sales_30d'], reverse=True)[:5]
        low_stock_str = ""
        if not low_stock_items:
            low_stock_str = " └ 現在、該当する低在庫商品はありません\n"
        else:
            for i, item in enumerate(low_stock_items, 1):
                short_title = item['title'][:DISCORD_TITLE_LIMIT] + "..." if len(item['title']) > DISCORD_TITLE_LIMIT else item['title']
                prefix = " └" if i == len(low_stock_items) else " ├"
                low_stock_str += f"{prefix} ⚠️ 残り{item['quantity']}個 (月間{item['sales_30d']}個販売): {short_title}\n"

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
            f"🏆 **【注目】アクセスランキング Top5**\n"
            f"{pv_ranking_str}"
            f"-----------------------------------\n"
            f"👑 **【売れ筋】販売個数ランキング Top5**\n"
            f"{sales_ranking_str}"
            f"-----------------------------------\n"
            f"🚨 **【要補充】売れ筋・低在庫アラート (在庫5個以下)**\n"
            f"{low_stock_str}"
            f"━━━━━━━━━━━━━━━━━━━\n"
        )
        
        if DISCORD_WEBHOOK_SUMMARY:
            requests.post(DISCORD_WEBHOOK_SUMMARY, json={"content": discord_msg})
            print("🎉 すべての処理とランキング通知が正常完了しました！")
            
    except Exception as e:
        print(f"❌ エラー発生: {e}")

if __name__ == "__main__":
    main()
