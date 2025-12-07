import streamlit as st
import requests
from curl_cffi import requests as cffi_requests
import pandas as pd
import concurrent.futures
import asyncio
import websockets
import json
import os
import logging
from datetime import datetime, timedelta
import time

# ==========================================
# ⚙️ 설정 & 환경
# ==========================================
HL_REF_CODE = "HACHEBI"     
EX_REF_CODE = "HACHEBI"     
BN_REF_CODE = "115177638"

REQUEST_TIMEOUT = 5  # 타임아웃 10초 -> 5초로 단축 (빠른 실패 후 재시도)
MAX_WORKERS = 50     # 워커 수 증가
CACHE_TTL = 15    
EDGEX_WS_URL = "wss://quote.edgex.exchange/api/v1/public/ws"
PAIRS_FILE = "edgex_pairs.json"
SCALES_FILE = "funding_scales.json"
MIN_SPREAD = 0.5

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json"
}

st.set_page_config(
    page_title="Arbitrage Pro", 
    layout="wide", 
    page_icon="⚡️",
    initial_sidebar_state="expanded"
)

# ==========================================
# 🎨 [디자인] Compact & Gradient UI
# ==========================================
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@500;700&display=swap');
    
    .stApp { background-color: #ffffff; color: #0f172a; font-family: 'Inter', sans-serif; }
    h3 { color: #0f172a !important; font-weight: 800 !important; letter-spacing: -0.5px; margin-bottom: 0 !important; }
    
    .table-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; margin-top: 15px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
    thead th { background: #f8fafc; color: #64748b; font-weight: 600; font-size: 11px; padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; text-transform: uppercase; }
    td { padding: 8px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; color: #334155; white-space: nowrap; }
    
    .market-row { display: flex; align-items: center; gap: 4px; flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; -ms-overflow-style: none; scrollbar-width: none; }
    .market-row::-webkit-scrollbar { display: none; }

    .trade-badge { display: inline-flex; align-items: center; border-radius: 4px; padding: 2px 6px; font-size: 11px; text-decoration: none !important; transition: all 0.2s ease; box-shadow: 0 1px 2px rgba(0,0,0,0.05); height: 22px; white-space: nowrap; border: 1px solid transparent; }
    .trade-badge:hover { transform: translateY(-1px); box-shadow: 0 3px 5px rgba(0,0,0,0.1); z-index: 10; }

    .badge-long { background: #f0fdf4; border-color: #86efac; color: #14532d; }
    .badge-long .rate-val { color: #15803d; }
    .badge-short { background: #fef2f2; border-color: #fca5a5; color: #7f1d1d; }
    .badge-short .rate-val { color: #b91c1c; }
    .badge-mid { background: linear-gradient(135deg, #ffffff 0%, #f1f5f9 100%); border-color: #cbd5e1; color: #475569; }
    .badge-mid:hover { background: #f8fafc; border-color: #94a3b8; }

    .ex-name { font-weight: 800; margin-right: 4px; font-size: 10px; letter-spacing: -0.5px; }
    .rate-val { font-weight: 700; margin-right: 4px; font-family: 'Roboto Mono', monospace; font-size: 10px; }
    .oi-val { font-size: 9px; font-weight: 500; padding-left: 4px; border-left: 1px solid rgba(0,0,0,0.1); color: #64748b; font-family: 'Roboto Mono', monospace; }
    
    .arrow-icon { color: #cbd5e1; font-size: 10px; margin: 0 1px; }
    .spread-text { font-weight: 800; color: #2563eb; font-size: 13px; letter-spacing: -0.5px; }
    
    .gap-pill { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; font-family: 'Roboto Mono', monospace; }
    .gap-pos { background: #ecfdf5; color: #16a34a; border: 1px solid #dcfce7; }
    .gap-neg { background: #fff1f2; color: #e11d48; border: 1px solid #ffe4e6; }

    section[data-testid="stSidebar"] { background-color: #f8fafc; border-right: 1px solid #e2e8f0; }
    .status-box { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; margin-bottom: 16px; }
    .status-item { display: flex; justify-content: space-between; margin-bottom: 4px; font-size: 12px; color: #64748b; }
    .status-item b { color: #0f172a; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 🛠 유틸리티: URL, 심볼, 보정
# ==========================================

# [빠른 요청] 일반 Requests (보안벽 낮은 곳용)
def make_fast_request(url, method='GET', **kwargs):
    try:
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
        kwargs.setdefault('headers', HEADERS)
        if method == 'GET': response = requests.get(url, **kwargs)
        else: response = requests.post(url, **kwargs)
        if response.status_code == 200: return response
    except: pass
    return None

# [보안 우회] Curl_Cffi (보안벽 높은 곳용)
def make_secure_request(url, method='GET', **kwargs):
    try:
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
        kwargs.setdefault('impersonate', "chrome110")
        if method == 'GET': response = cffi_requests.get(url, **kwargs)
        else: response = cffi_requests.post(url, **kwargs)
        if response.status_code == 200: return response
    except: pass
    return None

def normalize_apy(hourly_rate):
    if hourly_rate is None: return 0.0
    apy = hourly_rate * 24 * 365 * 100
    if abs(apy) > 2000: apy /= 100 
    if abs(apy) > 2000: apy /= 100
    return apy

def calculate_apy_from_hourly(hourly_rate):
    if hourly_rate is None: return 0.0
    return hourly_rate * 24 * 365 * 100

def get_market_url(exchange, coin):
    coin = coin.upper()
    if exchange == 'HL': return f"https://app.hyperliquid.xyz/trade/{coin}?ref={HL_REF_CODE}"
    elif exchange == 'EX': return f"https://app.extended.exchange/perp/{coin}-USD?ref={EX_REF_CODE}"
    elif exchange == 'BN': return f"https://www.binance.com/en/futures/{coin}USDT?ref={BN_REF_CODE}"
    elif exchange == 'EDX': return f"https://pro.edgex.exchange/trade/{coin}USD"
    elif exchange == 'VR': return f"https://omni.variational.io/perpetual/{coin}"
    return "#"

def safe_float(val, default=0.0):
    try: return float(val) if val is not None else default
    except: return default

# ==========================================
# 📡 데이터 수집 함수 (하이브리드 모드)
# ==========================================

def get_hyperliquid_data():
    # HL은 보안벽이 낮음 -> 일반 requests 사용 (속도 UP)
    try:
        url = "https://api.hyperliquid.xyz/info"
        response = make_fast_request(url, method='POST', json={"type": "metaAndAssetCtxs"})
        if not response: return {}
        data = response.json()
        result = {}
        for i, coin in enumerate(data[0]['universe']):
            name = coin['name']
            assets = data[1][i]
            price = safe_float(assets.get('midPx'))
            if price > 0:
                apy = calculate_apy_from_hourly(safe_float(assets.get('funding')))
                result[name] = {
                    'apy': apy,
                    'price': price,
                    'oi': safe_float(assets.get('openInterest')) * price
                }
        return result
    except: return {}

def get_extended_data():
    # EX도 일반 requests로 충분할 때가 많음 (안되면 secure로 변경)
    try:
        url = "https://starknet.app.extended.exchange/api/v1/info/markets"
        response = make_secure_request(url) # EX는 secure 권장
        if not response: return {}
        result = {}
        for item in response.json()['data']:
            name = item['name'].split('-')[0]
            stats = item.get('marketStats', {})
            price = safe_float(stats.get('markPrice'))
            if price > 0:
                apy = calculate_apy_from_hourly(safe_float(stats.get('fundingRate')))
                result[name] = {
                    'apy': apy,
                    'price': price,
                    'oi': safe_float(stats.get('openInterest')) * price
                }
        return result
    except: return {}

def get_binance_data():
    # BN은 secure 필수
    try:
        f_res = make_secure_request("https://fapi.binance.com/fapi/v1/premiumIndex")
        t_res = make_secure_request("https://fapi.binance.com/fapi/v1/ticker/24hr")
        
        if not f_res or not t_res: return {}
        
        tickers = {t['symbol']: t for t in t_res.json()}
        result = {}
        for item in f_res.json():
            if item['symbol'].endswith("USDT"):
                name = item['symbol'].replace("USDT", "")
                ticker = tickers.get(item['symbol'], {})
                price = safe_float(ticker.get('lastPrice'))
                if price > 0:
                    raw_8h = safe_float(item.get('lastFundingRate'))
                    apy = calculate_apy_from_hourly(raw_8h / 8)
                    result[name] = {
                        'apy': apy,
                        'price': price,
                        'oi': 0.0 # 별도 수집
                    }
        return result
    except: return {}

def get_omni_data():
    # VR은 secure 필수
    try:
        url = "https://omni.variational.io/api/metadata/supported_assets"
        response = make_secure_request(
            url, 
            headers={"Origin": "https://omni.variational.io", "Referer": "https://omni.variational.io/markets"}
        )
        if response.status_code != 200: return {}
        data = response.json()
        result = {}
        for symbol, info_list in data.items():
            if not info_list: continue
            item = info_list[0]
            name = item.get('asset')
            price = safe_float(item.get('price'))
            
            raw_decimal_apy = safe_float(item.get('funding_rate'))
            apy = raw_decimal_apy * 100 

            oi_data = item.get('open_interest', {})
            total_oi = safe_float(oi_data.get('long_open_interest')) + safe_float(oi_data.get('short_open_interest'))

            if name and price > 0:
                result[name] = {
                    'apy': apy, 
                    'price': price, 
                    'oi': total_oi
                }
        return result
    except: return {}

@st.cache_data(ttl=3600)
def get_edgex_pairs_map():
    mapping = {}
    if os.path.exists(PAIRS_FILE):
        try:
            with open(PAIRS_FILE, "r", encoding='utf-8') as f: mapping = json.load(f)
        except: pass

    if not mapping or len(mapping) < 10:
        try:
            url = "https://api.edgex.exchange/api/v1/public/instruments"
            # EdgeX는 secure
            resp = make_secure_request(url)
            if resp.status_code == 200:
                data = resp.json().get('data', [])
                new_mapping = {}
                for item in data:
                    cid = str(item.get('contractId'))
                    sym = item.get('symbol')
                    if not sym: continue
                    base = sym.replace("-USDT", "").replace("USDT", "")
                    scale = 1
                    if base.startswith("1000") and not base.startswith("10000"): base = base[4:]; scale = 1000
                    elif base.upper().startswith("K") and len(base)>1 and base[1].isupper(): base = base[1:]; scale = 1000
                    elif base.startswith("1M"): base = base[2:]; scale = 1000000
                    new_mapping[cid] = {'display': base, 'scale': scale, 'url_symbol': sym}
                if new_mapping:
                    mapping = new_mapping
                    with open(PAIRS_FILE, "w", encoding='utf-8') as f: json.dump(mapping, f, indent=4)
        except: pass
    return mapping

def get_edgex_data():
    id_map = get_edgex_pairs_map()
    if not id_map: return {} 

    async def fetch():
        res = {}
        temp_data = {} 
        try:
            async with websockets.connect(EDGEX_WS_URL, close_timeout=2) as ws:
                cids = list(id_map.keys())
                chunk_size = 50
                for i in range(0, len(cids), chunk_size):
                    chunk = cids[i:i+chunk_size]
                    for cid in chunk:
                        await ws.send(json.dumps({"type":"subscribe","channel":f"ticker.{cid}"}))
                        await ws.send(json.dumps({"type":"subscribe","channel":f"fundingRate.{cid}"}))
                    await asyncio.sleep(0.1) 
                
                end = time.time() + 3.0 # 수집 시간 4s -> 3s 단축
                while time.time() < end:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        d = json.loads(msg)
                        if d.get("type") == "quote-event":
                            items = d.get("content", {}).get("data", [])
                            if not items: continue
                            item = items[0]
                            cid = str(item.get('contractId'))
                            if cid not in temp_data: temp_data[cid] = {'price':0, 'rate':0, 'oi':0}
                            curr = temp_data[cid]
                            
                            if safe_float(item.get('lastPrice')) > 0: curr['price'] = safe_float(item['lastPrice'])
                            if 'fundingRate' in item: curr['rate'] = safe_float(item['fundingRate'])
                            if 'openInterest' in item: curr['oi'] = safe_float(item['openInterest'])
                    except: pass
        except: pass
        
        for cid, data in temp_data.items():
            if cid not in id_map: continue
            info = id_map[cid]
            if data['price'] > 0:
                apy = normalize_apy(data['rate'])
                res[info['display']] = {
                    'apy': apy,
                    'price': data['price'] / info['scale'],
                    'oi': data['oi'] * data['price'],
                    'url_symbol': info['url_symbol']
                }
        return res
    try: return asyncio.run(fetch())
    except: return {}

def fetch_binance_ois_parallel(coins):
    if not coins: return {}
    results = {}
    
    # Binance OI는 cffi로 (보안)
    def get_oi(c):
        try:
            r = cffi_requests.get(
                f"https://fapi.binance.com/fapi/v1/openInterest?symbol={c}USDT", 
                impersonate="chrome110", timeout=2
            )
            if r.status_code == 200: return safe_float(r.json().get('openInterest'))
        except: pass
        return 0.0

    # 워커 수 50개로 늘려서 병렬 처리 가속
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        futures = {ex.submit(get_oi, c): c for c in coins}
        for future in concurrent.futures.as_completed(futures):
            c = futures[future]
            try: results[c] = future.result()
            except: results[c] = 0.0
    return results

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_all_data():
    # 멀티스레드로 각 거래소 데이터 동시에 긁어오기 (가장 빠른 방법)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            'hyperliquid': ex.submit(get_hyperliquid_data),
            'extended': ex.submit(get_extended_data),
            'binance': ex.submit(get_binance_data),
            'edgex': ex.submit(get_edgex_data),
            'omni': ex.submit(get_omni_data)
        }
        return {k: f.result() for k, f in futures.items()}

# ==========================================
# 📊 화면 렌더링
# ==========================================

col_t, col_r = st.columns([8, 1])
with col_t: st.markdown("### ✨ Arbitrage Scanner")
with col_r:
    if st.button("↻ REFRESH", type="primary"):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Scanning..."):
    data_map = load_all_data()

with st.sidebar:
    st.markdown("#### 📡 Status")
    html = "<div class='status-box'>"
    names = {'hyperliquid':'HL', 'extended':'EX', 'binance':'BN', 'edgex':'EDX', 'omni':'VR'}
    for k, v in names.items():
        count = len(data_map.get(k, {}))
        icon = "✅" if count > 0 else "🔴"
        html += f"<div class='status-item'><span>{v}</span><b>{icon} {count}</b></div>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)
    
    sort_by = st.selectbox("Sort By", ["Max Spread", "Best Price Gap", "Volume"])
    min_spr = st.slider("Min Spread (%)", 0.0, 50.0, MIN_SPREAD, 0.5)
    st.caption(f"Update: {datetime.now().strftime('%H:%M:%S')}")

all_coins = set().union(*[d.keys() for d in data_map.values()])
table_rows = []

bn_target_coins = [c for c in all_coins if c in data_map['binance']]
bn_ois_qty = fetch_binance_ois_parallel(bn_target_coins)

for c in all_coins:
    markets = []
    for ex_key, code in names.items():
        if c in data_map[ex_key]:
            d = data_map[ex_key][c]
            oi = bn_ois_qty.get(c, 0) * d['price'] if ex_key == 'binance' else d.get('oi', 0)
            url_sym = d.get('url_symbol', None)
            markets.append({'code': code, 'apy': d['apy'], 'price': d['price'], 'oi': oi, 'ex_key': ex_key, 'url_symbol': url_sym})
    
    if len(markets) >= 2:
        markets.sort(key=lambda x: x['apy'])
        spread = markets[-1]['apy'] - markets[0]['apy']
        best_long = markets[0]
        best_short = markets[-1]
        gap = (best_short['price'] - best_long['price']) / best_long['price'] * 100
        
        if spread >= min_spr:
            table_rows.append({'coin': c, 'spread': spread, 'gap': gap, 'markets': markets})

if table_rows:
    df = pd.DataFrame(table_rows)
    if sort_by == "Max Spread": df = df.sort_values('spread', ascending=False)
    elif sort_by == "Best Price Gap": df = df.sort_values('gap', ascending=False)
    elif sort_by == "Volume": 
        df['max_oi'] = df['markets'].apply(lambda x: max([m['oi'] for m in x]))
        df = df.sort_values('max_oi', ascending=False)
    
    c1, c2 = st.columns(2)
    c1.metric("Opportunities", len(df))
    c2.metric("Top Spread", f"{df['spread'].max():.2f}%")

    html_rows = ""
    for _, row in df.iterrows():
        badges_html = "<div class='market-row'>"
        for i, m in enumerate(row['markets']):
            link = get_market_url(m['code'], row['coin'])
            
            oi_val = m['oi']
            if oi_val >= 1e6: oi_str = f"${oi_val/1e6:.1f}M"
            elif oi_val >= 1e3: oi_str = f"${oi_val/1e3:.0f}K"
            elif oi_val > 0: oi_str = f"${oi_val:.0f}"
            else: oi_str = "-"
            
            style_class = "trade-badge badge-mid"
            if i == 0: style_class = "trade-badge badge-long"
            elif i == len(row['markets']) - 1: style_class = "trade-badge badge-short"
            
            arrow = "<span class='arrow-icon'>›</span>" if i < len(row['markets']) - 1 else ""
            badges_html += f"<a href='{link}' target='_blank' class='{style_class}'><span class='ex-name'>{m['code']}</span><span class='rate-val'>{m['apy']:.0f}%</span><span class='oi-val'>{oi_str}</span></a>{arrow}"
        
        badges_html += "</div>"
        gap_val = row['gap']
        gap_cls = "gap-pos" if gap_val >= 0 else "gap-neg"
        gap_html = f"<span class='gap-pill {gap_cls}'>{gap_val:+.2f}%</span>"
        html_rows += f"<tr><td style='width:90px; font-weight:700;'>{row['coin']}</td><td style='width:90px;'><span class='spread-text'>+{row['spread']:.0f}%</span></td><td style='width:80px;'>{gap_html}</td><td>{badges_html}</td></tr>"

    table_html = f"<div class='table-card'><table><thead><tr><th width='90'>Asset</th><th width='90'>Spread</th><th width='80'>Price Gap</th><th>Markets Flow ( Rate: Low → High )</th></tr></thead><tbody>{html_rows}</tbody></table></div>"
    st.markdown(table_html, unsafe_allow_html=True)
else:
    st.info("No arbitrage opportunities found.")
