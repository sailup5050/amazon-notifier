import os
import time
import gzip
import json
import requests

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💰 【仕入れ値・原価設定ボックス】
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# あなたの商品（ASIN）ごとの原価（仕入れ値）をここに登録してください。
# 登録がない商品は、販売価格の「60%（0.6）」を原価として自動計算します。
PRODUCT_COSTS = {
    "B0XXXXXXXX": 1200, 
    "B0YYYYYYYY": 2500,
}
DEFAULT_PROFIT_RATE = 0.4  # 原価未登録商品の利益率（40%）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REFRESH_TOKEN     = os.environ.get("REFRESH_TOKEN")
CLIENT_ID         = os.environ.get("CLIENT_ID")
CLIENT_SECRET     = os.environ.get("CLIENT_SECRET")
INVENTORY_GAS_URL = os.environ.get("INVENTORY_GAS_URL")
MARKETPLACE_ID_JP = "A1VC38T7YXB528"

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

def get_fba_inventory_report(token):
    print("🔄 AmazonにFBA在庫レポートを要求中...")
    url = "https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/reports"
    headers = {"x-amz-access-token": token, "Content-Type": "application/json"}
    body = {
        "reportType": "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
        "marketplaceIds": [MARKETPLACE_ID_JP]
    }
    
    res = requests.post(url, headers=headers, json=body)
    if res.status_code != 202:
        raise Exception(f"レポート要求失敗: {res.text}")
        
    report_id = res.json().get("reportId")
    
    # Amazonの集計待ち（最大60秒）
    report_doc_id = None
    for _ in range(6):
        time.sleep(10)
        check_res = requests.get(f"{url}/{report_id}", headers=headers)
        if check_res.json().get("processingStatus") == "DONE":
            report_doc_id = check_res.json().get("reportDocumentId")
            break
            
    if not report_doc_id:
        raise Exception("Amazon側のレポート生成がタイムアウトしました")
        
    # ドキュメントURLの取得
    doc_res = requests.get(f"https://sellingpartnerapi-fe.amazon.com/reports/2021-06-30/documents/{report_doc_id}", headers=headers)
    doc_data = doc_res.json()
    
    download_res = requests.get(doc_data.get("url"))
    content = download_res.content
    if doc_data.get("compressionAlgorithm") == "GZIP":
        content = gzip.decompress(content)
        
    return content.decode('utf-8')

def parse_and_send_inventory(tsv_text):
    lines = tsv_text.strip().split('\n')
    if not lines:
        print("データが空でした")
        return
        
    headers = lines[0].split('\t')
    
    # 必要な情報の列番号を特定
    try:
        idx_sku = headers.index('sku')
        idx_asin = headers.index('asin')
        idx_name = headers.index('product-name')
        idx_price = headers.index('your-price')
        idx_qty = headers.index('afn-fulfillable-quantity')
    except ValueError as e:
        raise Exception(f"レポートの列構造が想定と異なります: {e}")
        
    payload_data = []
    
    for line in lines[1:]:
        cols = line.split('\t')
        if len(cols) <= max(idx_sku, idx_asin, idx_name, idx_price, idx_qty):
            continue
            
        sku = cols[idx_sku]
        asin = cols[idx_asin]
        title = cols[idx_name][:50] + "..." if len(cols[idx_name]) > 50 else cols[idx_name]
        
        try:
            qty = int(cols[idx_qty])
        except:
            qty = 0
            
        try:
            price = float(cols[idx_price])
        except:
            price = 0.0
            
        # 💰 仕入れ値と利益の計算ロジック
        if asin in PRODUCT_COSTS:
            cost = float(PRODUCT_COSTS[asin])
            profit = price - cost
        else:
            profit = price * DEFAULT_PROFIT_RATE
            cost = price - profit
            
        total_cost = cost * qty
        total_profit = profit * qty
        
        payload_data.append({
            "sku": sku,
            "asin": asin,
            "title": title,
            "quantity": qty,
            "price": int(price),
            "cost": int(cost),
            "total_cost": int(total_cost),
            "profit": int(profit),
            "total_profit": int(total_profit)
        })
        
    print(f"📊 集計完了: {len(payload_data)}件の商品データをGoogleスプレッドシートへ送信中...")
    
    # Googleスプレッドシート(GAS)へPOST送信
    response = requests.post(INVENTORY_GAS_URL, json=payload_data)
    print(f"Google側の応答: {response.text}")

if __name__ == "__main__":
    try:
        token = get_access_token()
        tsv_text = get_fba_inventory_report(token)
        parse_and_send_inventory(tsv_text)
        print("🎉 FBA在庫スプレッドシート更新が正常に完了しました！")
    except Exception as e:
        print(f"❌ エラー発生: {e}")
