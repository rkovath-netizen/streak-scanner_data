import streamlit as st
import pandas as pd
import numpy as np
import requests
import urllib.parse
from datetime import datetime, timedelta
import time
import os
import gzip
import csv
import io
import re

# --- Page Config ---
st.set_page_config(page_title="Upstox Swing Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Consolidate trades, track TSL (1hr ATR adaptive), and sync Option Prices.")

# --- Initialization of Session States ---
DEFAULT_STRATEGIES = [
    "b_ema-x_15mt", "b_ema_x_1hr", "b_rsi_x60_15mt", "b_rsi_x_1hr", 
    "b_vwap_x_15mt", "b_vwap_x_1hr", "b_st_x_15mt", "b_st_x_1hr",
    "s_ema-x_15mt", "s_ema_x_1hr", "s_rsi_x60_15mt", "s_rsi_x_1hr", 
    "s_vwap_x_15mt", "s_vwap_x_1hr", "s_st_x_15mt", "s_st_x_1hr"
]
if "strategies" not in st.session_state: st.session_state.strategies = DEFAULT_STRATEGIES.copy()
if "master_ledger" not in st.session_state: st.session_state.master_ledger = pd.DataFrame()
if "temp_single_trade" not in st.session_state: st.session_state.temp_single_trade = pd.DataFrame()
if "temp_bulk_trades" not in st.session_state: st.session_state.temp_bulk_trades = pd.DataFrame()
if "debug_logs" not in st.session_state: st.session_state.debug_logs = []

# --- Sidebar: Configuration ---
with st.sidebar:
    st.header("🔑 API Setup")
    default_token = st.secrets.get("UPSTOX_TOKEN", "")
    api_token = st.text_input("Enter Upstox Analytics Token", value=default_token, type="password")
    
    st.markdown("---")
    st.subheader("➕ Add Custom Strategy")
    new_strat = st.text_input("New Strategy Name (e.g., b_custom_15mt)")
    if st.button("Add Strategy"):
        if new_strat and new_strat not in st.session_state.strategies:
            st.session_state.strategies.append(new_strat)
            st.success(f"Added {new_strat} to dropdowns!")
            st.rerun()
            
    st.markdown("---")
    st.markdown("**Trade Parameters (Risk & Target)**")
    max_capital_eq = st.number_input("Max Capital per Trade (EQ)", value=20000.0, step=1000.0)
    max_risk = st.number_input("Max Risk per Trade", value=1000.0, step=100.0)
    atr_mult = st.number_input("ATR Trailing Multiplier", value=3.0, step=0.5)
    atr_period = st.number_input("ATR Period", value=14, step=1)
    tgt_pct = st.number_input("Fixed Target %", value=5.0, step=0.5)
    
    st.markdown("---")
    st.subheader("📂 Load Previous Ledger")
    ledger_upload = st.file_uploader("Upload Master Ledger (CSV)", type=["csv"])
    if ledger_upload is not None:
        if st.button("Load Uploaded Ledger"):
            try:
                loaded_df = pd.read_csv(ledger_upload)
                st.session_state.master_ledger = loaded_df
                st.success("Ledger loaded successfully!")
            except Exception as e:
                st.error(f"Error loading ledger: {e}")

# --- Helpers: FNO Data Loading ---
@st.cache_data(show_spinner=False)
def load_fno_details():
    liquid_symbols = set()
    lot_sizes = {}
    liquid_file = 'fno_with_sectors - liquid.csv'
    if os.path.exists(liquid_file):
        try:
            df_liq = pd.read_csv(liquid_file)
            if 'Symbol' in df_liq.columns:
                liquid_symbols = set(df_liq['Symbol'].str.strip().str.upper())
        except: pass
    fno_file = 'fno_with_sectors.csv'
    if os.path.exists(fno_file):
        try:
            df_fno = pd.read_csv(fno_file)
            if 'Symbol' in df_fno.columns and 'lot size' in df_fno.columns:
                for _, row in df_fno.iterrows():
                    sym = str(row['Symbol']).strip().upper()
                    try: lot_sizes[sym] = int(float(row['lot size']))
                    except: pass
        except: pass
    return liquid_symbols, lot_sizes

liquid_symbols, lot_sizes_dict = load_fno_details()

def parse_tf_from_strategy(strategy_name):
    suffix = str(strategy_name).split('_')[-1].lower()
    if '15m' in suffix: return '15m', 15
    if '1h' in suffix or '60m' in suffix: return '1hr', 60
    if '30m' in suffix: return '30m', 30
    if '5m' in suffix: return '5m', 5
    if '1d' in suffix: return '1day', 1440
    return '1hr', 60 

def cleanup_legacy_columns(df):
    if 'Cash MTM (₹)' in df.columns:
        if 'Cash PnL (₹)' in df.columns:
            df['Cash PnL (₹)'] = df['Cash PnL (₹)'].fillna(df['Cash MTM (₹)'])
        else:
            df['Cash PnL (₹)'] = df['Cash MTM (₹)']
        df.drop(columns=['Cash MTM (₹)'], inplace=True, errors='ignore')
        
    if 'Opt MTM' in df.columns:
        if 'Opt PnL (₹)' in df.columns:
            df['Opt PnL (₹)'] = df['Opt PnL (₹)'].fillna(df['Opt MTM'])
        else:
            df['Opt PnL (₹)'] = df['Opt MTM']
        df.drop(columns=['Opt MTM'], inplace=True, errors='ignore')
        
    for col in ['Cash PnL %', 'Opt PnL %']:
        if col not in df.columns: df[col] = 0.0
    return df

# --- Helpers: Advanced API ---
def robust_api_get(url, headers, max_retries=4, params=None):
    for attempt in range(max_retries):
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200: return res
        elif res.status_code == 429: time.sleep(2 ** attempt) 
        else: time.sleep(1)
    return res 

@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_key(symbol, token):
    if not token: return None
    url = f"https://api.upstox.com/v2/instruments/search?query={urllib.parse.quote(str(symbol).strip().upper())}&exchanges=NSE&segments=EQ"
    res = robust_api_get(url, {'Accept': 'application/json', 'Authorization': f'Bearer {token}'})
    if res and res.status_code == 200 and res.json().get('data'):
        return res.json()['data'][0]['instrument_key']
    return None

def fetch_all_candles(instrument_key, from_date_str, token):
    encoded_key = urllib.parse.quote(instrument_key)
    today_str = datetime.today().strftime('%Y-%m-%d')
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    all_candles = []
    
    hist_url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/1minute/{today_str}/{from_date_str}"
    hist_res = robust_api_get(hist_url, headers)
    if hist_res and hist_res.status_code == 200:
        candles = hist_res.json().get('data', {}).get('candles', [])
        candles.reverse(); all_candles.extend(candles)
        
    intra_url = f"https://api.upstox.com/v2/historical-candle/intraday/{encoded_key}/1minute"
    intra_res = robust_api_get(intra_url, headers)
    if intra_res and intra_res.status_code == 200:
        candles = intra_res.json().get('data', {}).get('candles', [])
        candles.reverse()
        ts = {c[0] for c in all_candles}
        all_candles.extend([c for c in candles if c[0] not in ts])
        
    all_candles.sort(key=lambda x: x[0])
    return all_candles

# --- Cleaned Options Resolution Engine ---
def get_underlying_key(symbol_input):
    mapping = {
        "NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "FINNIFTY": "NSE_INDEX|Nifty Fin Service", "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
        "SENSEX": "BSE_INDEX|SENSEX", "BANKEX": "BSE_INDEX|BANKEX"
    }
    return mapping.get(symbol_input, f"NSE_EQ|{symbol_input}")

def get_closest_expiry(symbol, trade_date_str, token):
    available_expiries = set()
    underlying_key = get_underlying_key(symbol)
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    
    url = "https://api.upstox.com/v2/expired-instruments/expiries"
    res = robust_api_get(url, headers, params={"instrument_key": underlying_key})
    if res and res.status_code == 200 and res.json().get("status") == "success":
        for d in res.json().get("data", []):
            if isinstance(d, str): available_expiries.add(d)
            elif isinstance(d, dict) and "expiry_date" in d: available_expiries.add(d["expiry_date"])
                
    url_csv = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    res_csv = requests.get(url_csv)
    if res_csv.status_code == 200:
        with gzip.open(io.BytesIO(res_csv.content), 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if symbol in row.get('tradingsymbol', '').upper() and row.get('expiry'):
                    available_expiries.add(row.get('expiry'))
                    
    valid_dates = sorted([d for d in available_expiries if d >= trade_date_str])
    return valid_dates[0] if valid_dates else None

def resolve_contract(symbol, expiry_date_str, strike, option_type, token):
    """Stripped of MCX logic. Looks for exact strike first, uses closest strike if exact fails."""
    expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    target_strike = float(strike)
    
    exact_key = None
    closest_key = None
    min_diff = float('inf')

    def process_match(contract_key, tsym, c_strike=None):
        nonlocal exact_key, closest_key, min_diff
        if symbol in tsym and option_type in tsym:
            if c_strike is None:
                match = re.search(r'(\d+(\.\d+)?)(CE|PE)$', tsym)
                if match: c_strike = float(match.group(1))
            
            if c_strike is not None:
                if c_strike == target_strike:
                    exact_key = contract_key
                diff = abs(c_strike - target_strike)
                if diff < min_diff:
                    min_diff = diff
                    closest_key = contract_key

    if expiry_date < datetime.today().date():
        url = "https://api.upstox.com/v2/expired-instruments/option/contract"
        params = {"instrument_key": get_underlying_key(symbol), "expiry_date": expiry_date_str}
        res = robust_api_get(url, headers, params=params)
        if res and res.status_code == 200 and res.json().get("status") == "success":
            for contract in res.json().get("data", []):
                process_match(contract.get("instrument_key"), contract.get("trading_symbol", "").upper())
    else:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
        res = requests.get(url)
        if res.status_code == 200:
            with gzip.open(io.BytesIO(res.content), 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    tsym = row.get('tradingsymbol', '').upper()
                    if row.get('expiry') == expiry_date_str:
                        try: r_strike = float(row.get('strike', 0))
                        except: r_strike = None
                        process_match(row.get('instrument_key'), tsym, r_strike)
                            
    return exact_key if exact_key else closest_key

def get_specific_candle(instrument_key, target_date, target_time, token, return_type='open'):
    encoded_key = urllib.parse.quote(instrument_key)
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    if instrument_key.count('|') >= 2:
        url = f"https://api.upstox.com/v2/expired-instruments/historical-candle/{encoded_key}/1minute/{target_date}/{target_date}"
    else:
        url = f"https://api.upstox.com/v3/historical-candle/{encoded_key}/minutes/1/{target_date}/{target_date}"
        
    res = robust_api_get(url, headers)
    if res and res.status_code == 200 and res.json().get("status") == "success":
        target_ts = f"{target_date}T{target_time}"
        candles = res.json().get("data", {}).get("candles", [])
        candles.sort(key=lambda x: x[0]) 
        for candle in candles:
            if str(candle[0])[:16] >= target_ts:
                return candle[4] if return_type == 'close' else candle[1]
    return None

def refresh_live_cash_metrics(row, token, atr_mult, atr_period, tgt_p):
    strategy_name = row['Strategy Name']
    tf_label, tf_minutes = parse_tf_from_strategy(strategy_name)
    
    # 15m Strategy Trailing Override
    atr_tf_minutes = 60 if tf_minutes == 15 else tf_minutes
    
    is_short = str(strategy_name).strip().lower().startswith('s_')
    symbol_clean = str(row['Stock Name']).strip().upper()
    instrument_key = get_instrument_key(symbol_clean, token)
    
    if not instrument_key: return row
    exec_dt_str = str(row['Execution Time'])
    if pd.isna(exec_dt_str) or exec_dt_str == 'None': return row
    
    exec_dt = pd.to_datetime(exec_dt_str)
    hist_start_str = (exec_dt - timedelta(days=15)).strftime("%Y-%m-%d")
    raw_candles = fetch_all_candles(instrument_key, hist_start_str, token)
    if not raw_candles: return row
    
    df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'oi'])
    df['datetime'] = pd.to_datetime(df['timestamp'])
    
    # Calculate ATR based on overridden timeframe
    df_resampled = df.set_index('datetime').resample(f'{atr_tf_minutes}min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    df_resampled['prev_close'] = df_resampled['close'].shift(1)
    df_resampled['tr'] = df_resampled.apply(lambda x: max(x['high'] - x['low'], abs(x['high'] - x['prev_close']), abs(x['low'] - x['prev_close'])) if pd.notna(x['prev_close']) else x['high'] - x['low'], axis=1)
    df_resampled['atr'] = df_resampled['tr'].rolling(int(atr_period)).mean()
    df['floor_dt_atr'] = df['datetime'].dt.floor(f'{atr_tf_minutes}min')
    df['atr'] = df['floor_dt_atr'].map(df_resampled['atr']).ffill() 
    
    entry_idx = -1
    exec_target_iso = exec_dt.strftime('%Y-%m-%dT%H:%M')
    for i, r in df.iterrows():
        if r['timestamp'][:16] >= exec_target_iso:
            entry_idx = i; break
            
    if entry_idx == -1: return row
    
    entry_price = float(row['Cash Entry'])
    initial_sl = float(row['Initial SL'])
    qty = float(row['Qty'])
    
    active_atr = df.loc[entry_idx, 'atr']
    if pd.isna(active_atr) or active_atr <= 0: active_atr = entry_price * 0.01
        
    current_tsl = initial_sl
    tgt_price = entry_price * (1 - (tgt_p / 100)) if is_short else entry_price * (1 + (tgt_p / 100))
    highest_peak = entry_price
    lowest_trough = entry_price
    
    exit_price, exit_time, status, bars_1m = None, None, "Live", 0
    cmp_price = df.iloc[-1]['close']
    
    for i in range(entry_idx, len(df)):
        bars_1m += 1
        c_close, c_time_curr, c_atr = df.loc[i, 'close'], df.loc[i, 'timestamp'].split('+')[0], df.loc[i, 'atr']
        cmp_price = c_close 
        current_atr = c_atr if pd.notna(c_atr) and c_atr > 0 else active_atr
        
        if is_short:
            lowest_trough = min(lowest_trough, c_close)
            current_tsl = lowest_trough + (atr_mult * current_atr)
            if c_close >= current_tsl: exit_price, exit_time, status = c_close, c_time_curr, "Trailing SL Hit"; break
            elif c_close <= tgt_price: exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"; break
        else:
            highest_peak = max(highest_peak, c_close)
            current_tsl = highest_peak - (atr_mult * current_atr)
            if c_close <= current_tsl: exit_price, exit_time, status = c_close, c_time_curr, "Trailing SL Hit"; break
            elif c_close >= tgt_price: exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"; break

    if exit_price is not None:
        row['Cash Exit'] = round(exit_price, 2)
        row['Exit Time'] = exit_time.replace('T', ' ')
        cmp_price = exit_price 

    row['CMP'] = round(cmp_price, 2)
    row['Bars in Trade'] = round(bars_1m / (tf_minutes if tf_minutes < 1440 else 375), 1)
    row['Status'] = status
    row['Current TSL'] = round(current_tsl, 2)
    
    if is_short:
        row['Cash PnL (₹)'] = round((entry_price - cmp_price) * qty, 2)
        row['Cash PnL %'] = round(((entry_price - cmp_price) / entry_price) * 100, 2) if entry_price > 0 else 0.0
        row['Max Loss (₹)'] = round((entry_price - current_tsl) * qty, 2)
    else:
        row['Cash PnL (₹)'] = round((cmp_price - entry_price) * qty, 2)
        row['Cash PnL %'] = round(((cmp_price - entry_price) / entry_price) * 100, 2) if entry_price > 0 else 0.0
        row['Max Loss (₹)'] = round((current_tsl - entry_price) * qty, 2)
        
    return row

# --- Core Calculations Engine (Cash Baseline) ---
def calculate_trade(symbol, trade_date, trigger_time, strategy_name, token, tgt_p, max_r, max_cap):
    tf_label, tf_minutes = parse_tf_from_strategy(strategy_name)
    
    # 15m Strategy Trailing Override Setup
    atr_tf_minutes = 60 if tf_minutes == 15 else tf_minutes
    
    symbol_clean = str(symbol).strip().upper()
    is_short = str(strategy_name).strip().lower().startswith('s_')
    
    category = "Liquid" if symbol_clean in liquid_symbols else "Others"
    ce_pe = "PE" if is_short else "CE"
    
    has_lot_size = symbol_clean in lot_sizes_dict
    lot_size = lot_sizes_dict.get(symbol_clean, 1) 
    instrument_key = get_instrument_key(symbol_clean, token)
    
    result = {
        "Strategy Name": strategy_name, "Stock Name": symbol_clean, "Date": trade_date, 
        "Category": category, "Trigger Time": str(trigger_time)[:5], "Execution Time": None, 
        "ATM Strike": None, "CE/PE": ce_pe, "Lot Size": lot_size, "Has Lot Size": has_lot_size,
        "Cash Entry": None, "Cash Exit": None, "CMP": None, "Qty": 0,
        "Initial SL": None, "Current TSL": None, 
        "Cash PnL (₹)": 0.0, "Cash PnL %": 0.0, "Max Loss (₹)": 0.0,
        "Bars in Trade": 0, "Status": "Pending",
        "Opt Entry": None, "Opt Exit": None, "Opt PnL (₹)": None, "Opt PnL %": None
    }
    
    if not instrument_key:
        result["Status"] = "Error: Symbol Not Found"; return result
        
    try:
        t_dt = pd.to_datetime(f"{trade_date} {str(trigger_time)[:5]}")
        exec_target_str = (t_dt + timedelta(minutes=int(tf_minutes))).strftime("%Y-%m-%dT%H:%M") 
        hist_start_str = (t_dt - timedelta(days=15)).strftime("%Y-%m-%d")
    except:
        result["Status"] = "Error: Invalid Date/Time"; return result

    raw_candles = fetch_all_candles(instrument_key, hist_start_str, token)
    if not raw_candles:
        result["Status"] = "Error: No Market Data"; return result

    df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'oi'])
    df['datetime'] = pd.to_datetime(df['timestamp'])
    
    # Calculate ATR using overridden timeframe
    df_resampled = df.set_index('datetime').resample(f'{atr_tf_minutes}min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    df_resampled['prev_close'] = df_resampled['close'].shift(1)
    df_resampled['tr'] = df_resampled.apply(lambda x: max(x['high'] - x['low'], abs(x['high'] - x['prev_close']), abs(x['low'] - x['prev_close'])) if pd.notna(x['prev_close']) else x['high'] - x['low'], axis=1)
    df_resampled['atr'] = df_resampled['tr'].rolling(int(atr_period)).mean()
    df['floor_dt_atr'] = df['datetime'].dt.floor(f'{atr_tf_minutes}min')
    df['atr'] = df['floor_dt_atr'].map(df_resampled['atr']).ffill() 

    entry_price, entry_idx, actual_exec_time, entry_atr = None, -1, None, None
    for i, row in df.iterrows():
        if row['timestamp'][:16] >= exec_target_str:
            entry_price, entry_idx, actual_exec_time = row['open'], i, row['timestamp'][:16]
            entry_atr = row['atr']
            break

    if entry_price is None:
        result["Status"] = f"Error: No candles after entry time"; return result
        
    result["Execution Time"] = actual_exec_time.replace('T', ' ')
    result["Cash Entry"] = round(entry_price, 2)

    # Risk size uses 1hr ATR
    active_atr = entry_atr if pd.notna(entry_atr) and entry_atr > 0 else (entry_price * 0.01)
    risk_per_share = atr_mult * active_atr
    
    raw_qty_risk = int(max_r / risk_per_share) if risk_per_share > 0 else 1
    qty_capital_limit = int(max_cap / entry_price) if entry_price > 0 else 1
    
    eq_qty = max(1, min(raw_qty_risk, qty_capital_limit))
    result["Qty"] = eq_qty

    step = 50
    if entry_price < 250: step = 2.5
    elif entry_price < 500: step = 5
    elif entry_price < 1000: step = 10
    elif entry_price < 3000: step = 20
    result["ATM Strike"] = int(round(entry_price / step) * step)

    initial_sl = entry_price + risk_per_share if is_short else entry_price - risk_per_share
    current_tsl = initial_sl
    tgt_price = entry_price * (1 - (tgt_p / 100)) if is_short else entry_price * (1 + (tgt_p / 100))
    highest_peak = entry_price
    lowest_trough = entry_price
    
    exit_price, exit_time, status, bars_1m = None, None, "Live", 0
    cmp_price = df.iloc[-1]['close'] 
    
    # Trailing SL uses 1hr ATR
    for i in range(entry_idx, len(df)):
        bars_1m += 1
        c_close, c_time_curr, c_atr = df.loc[i, 'close'], df.loc[i, 'timestamp'].split('+')[0], df.loc[i, 'atr']
        cmp_price = c_close 
        current_atr = c_atr if pd.notna(c_atr) and c_atr > 0 else active_atr
        
        if is_short:
            lowest_trough = min(lowest_trough, c_close)
            current_tsl = lowest_trough + (atr_mult * current_atr)
            if c_close >= current_tsl: exit_price, exit_time, status = c_close, c_time_curr, "Trailing SL Hit"; break
            elif c_close <= tgt_price: exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"; break
        else:
            highest_peak = max(highest_peak, c_close)
            current_tsl = highest_peak - (atr_mult * current_atr)
            if c_close <= current_tsl: exit_price, exit_time, status = c_close, c_time_curr, "Trailing SL Hit"; break
            elif c_close >= tgt_price: exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"; break

    if exit_price is not None:
        result["Cash Exit"] = round(exit_price, 2)
        result["Exit Time"] = exit_time.replace('T', ' ')
        cmp_price = exit_price 

    result["CMP"] = round(cmp_price, 2)
    result["Bars in Trade"] = round(bars_1m / (tf_minutes if tf_minutes < 1440 else 375), 1) # Display in 15m bar count still
    result["Status"] = status
    result["Initial SL"] = round(initial_sl, 2)
    result["Current TSL"] = round(current_tsl, 2)
    
    if is_short:
        result["Cash PnL (₹)"] = round((entry_price - cmp_price) * eq_qty, 2)
        result["Cash PnL %"] = round(((entry_price - cmp_price) / entry_price) * 100, 2) if entry_price > 0 else 0.0
        result["Max Loss (₹)"] = round((entry_price - current_tsl) * eq_qty, 2)
    else:
        result["Cash PnL (₹)"] = round((cmp_price - entry_price) * eq_qty, 2)
        result["Cash PnL %"] = round(((cmp_price - entry_price) / entry_price) * 100, 2) if entry_price > 0 else 0.0
        result["Max Loss (₹)"] = round((current_tsl - entry_price) * eq_qty, 2)
        
    return result

def update_options_in_ledger(token, atr_mult, atr_period, tgt_p):
    df = st.session_state.master_ledger.copy()
    df = cleanup_legacy_columns(df)
    
    progress = st.progress(0)
    total_rows = len(df)
    
    for row_num, (i, row) in enumerate(df.iterrows()):
        try:
            if str(row.get('Status')).strip().lower() == 'live' or pd.isna(row.get('Exit Time')):
                row = refresh_live_cash_metrics(row, token, atr_mult, atr_period, tgt_p)
                for col in row.index:
                    df.at[i, col] = row[col]

            symbol = str(row.get('Stock Name')).strip().upper()
            strike = row.get('ATM Strike')
            ce_pe = row.get('CE/PE')
            lot_size = float(row.get('Lot Size', 1))
            exec_dt_str = str(row.get('Execution Time'))
            
            existing_opt_entry = row.get('Opt Entry')
            has_entry = pd.notna(existing_opt_entry)
            
            if pd.notna(strike) and pd.notna(ce_pe) and pd.notna(exec_dt_str) and exec_dt_str != 'None':
                trade_date = exec_dt_str[:10]
                exec_time = exec_dt_str[11:16]
                
                expiry_str = get_closest_expiry(symbol, trade_date, token)
                if expiry_str:
                    opt_key = resolve_contract(symbol, expiry_str, strike, ce_pe, token)
                    if opt_key:
                        
                        if has_entry:
                            opt_entry = float(existing_opt_entry)
                        else:
                            opt_entry = get_specific_candle(opt_key, trade_date, exec_time, token, return_type='open')
                            if opt_entry is not None:
                                df.at[i, 'Opt Entry'] = opt_entry
                        
                        if opt_entry is not None:
                            exit_dt_str = str(row.get('Exit Time'))
                            opt_exit = None
                            
                            if pd.notna(row.get('Exit Time')) and exit_dt_str != 'None':
                                opt_exit = get_specific_candle(opt_key, exit_dt_str[:10], exit_dt_str[11:16], token, return_type='close')
                                
                            if opt_exit is None:
                                intra_url = f"https://api.upstox.com/v2/historical-candle/intraday/{urllib.parse.quote(opt_key)}/1minute"
                                intra_res = robust_api_get(intra_url, headers={'Accept': 'application/json', 'Authorization': f'Bearer {token}'})
                                if intra_res and intra_res.status_code == 200:
                                    intra_candles = intra_res.json().get('data', {}).get('candles', [])
                                    if intra_candles:
                                        opt_exit = intra_candles[0][4] 
                                        
                                if opt_exit is None:
                                    now = datetime.now()
                                    past_date = (now - timedelta(days=5)).strftime("%Y-%m-%d")
                                    past_candles = fetch_all_candles(opt_key, past_date, token)
                                    if past_candles:
                                        opt_exit = past_candles[-1][4]
                                
                            if opt_exit is not None:
                                df.at[i, 'Opt Exit'] = opt_exit
                                df.at[i, 'Opt PnL (₹)'] = round((opt_exit - opt_entry) * lot_size, 2)
                                if opt_entry > 0:
                                    df.at[i, 'Opt PnL %'] = round(((opt_exit - opt_entry) / opt_entry) * 100, 2)
                                else:
                                    df.at[i, 'Opt PnL %'] = 0.0
                                    
        except Exception as e:
            st.error(f"Error updating options for row {i}: {e}")
            
        if total_rows > 0:
            progress.progress((row_num + 1) / total_rows)
        
    st.session_state.master_ledger = df
    st.success("Ledger Sync Complete: Options & Cash Metrics Refreshed!")

def parse_uploaded_csv(df, default_strategy):
    if 'seg_sym' in df.columns and 'time' in df.columns:
        df['Stock Name'] = df['seg_sym'].str.replace('NSE:', '', regex=False)
        df['Date'] = pd.to_datetime(df['time']).dt.strftime('%Y-%m-%d')
        df['Trigger Time'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')
    elif 'entry time' in df.columns:
        df.rename(columns={'entry time': 'Trigger Time'}, inplace=True)
    if 'Strategy Name' not in df.columns or df['Strategy Name'].isnull().all():
        df['Strategy Name'] = default_strategy
    return df

def append_to_ledger(new_df):
    if st.session_state.master_ledger.empty:
        st.session_state.master_ledger = new_df.copy()
    else:
        combined = pd.concat([st.session_state.master_ledger, new_df], ignore_index=True)
        dup_subset = ['Stock Name', 'Date', 'Execution Time'] if 'Execution Time' in combined.columns else ['Stock Name', 'Date']
        combined.drop_duplicates(subset=dup_subset, keep='last', inplace=True)
        st.session_state.master_ledger = combined

# --- Tabs ---
tab1, tab2 = st.tabs(["📚 Master Ledger & Options Sync", "📝 Add Trades (Bulk/Single)"])

with tab1:
    st.subheader("Consolidated Ledger")
    if not st.session_state.master_ledger.empty:
        st.session_state.master_ledger = cleanup_legacy_columns(st.session_state.master_ledger)
        if st.button("🚀 Fetch & Update Option Prices (Uses Advanced API)", type="primary"):
            if not api_token: st.warning("Please enter your Upstox Token.")
            else: update_options_in_ledger(api_token, atr_mult, atr_period, tgt_pct)
            
    edited_ledger = st.data_editor(st.session_state.master_ledger, use_container_width=True, num_rows="dynamic")
    col_save, col_dl, col_clear = st.columns([1, 1, 1])
    if col_save.button("💾 Save Edits", type="primary", use_container_width=True):
        st.session_state.master_ledger = edited_ledger
        st.success("Ledger saved!")
    
    csv_data = st.session_state.master_ledger.to_csv(index=False).encode('utf-8')
    col_dl.download_button("📥 Download Ledger (CSV)", data=csv_data, file_name=f"Master_Ledger_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv", mime="text/csv", use_container_width=True)
    if col_clear.button("🗑️ Clear Ledger", use_container_width=True):
        st.session_state.master_ledger = pd.DataFrame(); st.rerun()

with tab2:
    col_s, col_b = st.columns(2)
    with col_s:
        st.subheader("📝 Single Trade Entry")
        s_strategy = st.selectbox("Strategy Name", options=st.session_state.strategies, key="single_strat")
        s_symbol = st.text_input("NSE Stock Name", value="RELIANCE")
        s_date = st.date_input("Trade Date")
        s_time = st.time_input("Trigger Time", value=pd.to_datetime("09:15").time())
        if st.button("Calculate Single Trade"):
            if not api_token: st.warning("Provide API Token.")
            else:
                with st.spinner("Calculating ATR & Constructing Profile..."):
                    res = calculate_trade(s_symbol, s_date, s_time, s_strategy, api_token, tgt_pct, max_risk, max_capital_eq)
                    df_res = pd.DataFrame([res])
                    
                    if not df_res['Has Lot Size'].iloc[0]:
                        st.warning(f"⚠️ Lot size not available for {s_symbol} in fno_with_sectors.csv. Defaulted to 1.")
                        
                    df_res.drop(columns=['Has Lot Size'], inplace=True, errors='ignore')
                    st.session_state.temp_single_trade = cleanup_legacy_columns(df_res)
                    
        if not st.session_state.temp_single_trade.empty:
            st.dataframe(st.session_state.temp_single_trade)
            if st.button("Confirm Single Entry"):
                append_to_ledger(st.session_state.temp_single_trade)
                st.session_state.temp_single_trade = pd.DataFrame(); st.success("Added!"); st.rerun()
                
    with col_b:
        st.subheader("📁 Bulk CSV Processing")
        batch_strategy_name = st.selectbox("Assign Strategy for Batch File", options=st.session_state.strategies, key="bulk_strat")
        uploaded_files = st.file_uploader("Upload Bulk Export CSV(s)", type=["csv"], accept_multiple_files=True)
        if uploaded_files:
            all_dfs = []
            for f in uploaded_files:
                try:
                    f.seek(0) 
                    temp_df = pd.read_csv(f)
                    if not temp_df.empty: all_dfs.append(temp_df)
                except: pass
                    
            if all_dfs:
                combined_df = parse_uploaded_csv(pd.concat(all_dfs, ignore_index=True), batch_strategy_name)
                if st.button("Process Batch Run"):
                    if not api_token: st.warning("Enter API token.")
                    else:
                        results = []
                        pb = st.progress(0)
                        total_bulk_rows = len(combined_df)
                        for row_num, (idx, row) in enumerate(combined_df.iterrows()):
                            time.sleep(0.2)
                            t_time = row.get('Trigger Time', row.get('time', ''))
                            res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), t_time, row.get('Strategy Name'), api_token, tgt_pct, max_risk, max_capital_eq)
                            results.append(res)
                            if total_bulk_rows > 0:
                                pb.progress((row_num + 1) / total_bulk_rows)
                                
                        df_results = pd.DataFrame(results)
                        if 'Has Lot Size' in df_results.columns:
                            missing_lots = df_results[~df_results['Has Lot Size']]['Stock Name'].unique()
                            if len(missing_lots) > 0:
                                st.warning(f"⚠️ Lot size not available in fno_with_sectors.csv for: {', '.join(missing_lots)}. Defaulted to 1.")
                            df_results.drop(columns=['Has Lot Size'], inplace=True, errors='ignore')
                            
                        st.session_state.temp_bulk_trades = cleanup_legacy_columns(df_results)
                        
            if not st.session_state.temp_bulk_trades.empty:
                st.dataframe(st.session_state.temp_bulk_trades)
                if st.button("Confirm Bulk Add to Ledger"):
                    append_to_ledger(st.session_state.temp_bulk_trades)
                    st.session_state.temp_bulk_trades = pd.DataFrame(); st.success("Batch Sync Complete!"); st.rerun()
