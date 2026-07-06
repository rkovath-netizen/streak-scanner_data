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

# --- Page Config ---
st.set_page_config(page_title="Upstox Swing Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Consolidate trades, track TSL, Cash MTM, and sync exact Option Prices.")

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
    atr_mult = st.number_input("ATR Trailing Multiplier", value=3.0, step=0.5)
    atr_period = st.number_input("ATR Period", value=14, step=1)
    tgt_pct = st.number_input("Fixed Target %", value=5.0, step=0.5)
    
    st.markdown("---")
    st.subheader("📂 Load Previous Ledger")
    ledger_upload = st.file_uploader("Upload Master Ledger (CSV)", type=["csv"])
    if ledger_upload is not None:
        if st.button("Load Uploaded Ledger"):
            try:
                st.session_state.master_ledger = pd.read_csv(ledger_upload)
                st.success("Ledger loaded successfully!")
            except Exception as e:
                st.error(f"Error loading ledger: {e}")

# --- Helpers: FNO Data Loading ---
@st.cache_data(show_spinner=False)
def load_fno_details():
    liquid_symbols = set()
    lot_sizes = {}
    liquid_file = 'fno_with_sectors.csv'
    if os.path.exists(liquid_file):
        try:
            df = pd.read_csv(liquid_file)
            if 'Symbol' in df.columns:
                liquid_symbols = set(df['Symbol'].str.strip().str.upper())
            if 'Symbol' in df.columns and 'lot size' in df.columns:
                for _, row in df.iterrows():
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

# --- Helpers: Advanced API from Upstox Script ---
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

# --- Options Resolution API Integration ---
def get_underlying_key(symbol_input):
    mapping = {
        "NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "FINNIFTY": "NSE_INDEX|Nifty Fin Service", "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
        "SENSEX": "BSE_INDEX|SENSEX", "BANKEX": "BSE_INDEX|BANKEX"
    }
    return mapping.get(symbol_input, f"NSE_EQ|{symbol_input}")

def get_closest_expiry(symbol, trade_date_str, token):
    """Finds the nearest valid expiry on or after the trade date."""
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
    expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    
    if expiry_date < datetime.today().date():
        url = "https://api.upstox.com/v2/expired-instruments/option/contract"
        params = {"instrument_key": get_underlying_key(symbol), "expiry_date": expiry_date_str}
        res = robust_api_get(url, headers, params=params)
        if res and res.status_code == 200 and res.json().get("status") == "success":
            for contract in res.json().get("data", []):
                tsym = contract.get("trading_symbol", "").upper()
                if symbol in tsym and option_type in tsym and str(int(float(strike))) in tsym:
                    return contract.get("instrument_key")
    else:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
        res = requests.get(url)
        if res.status_code == 200:
            target_strike = float(strike)
            with gzip.open(io.BytesIO(res.content), 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    tsym = row.get('tradingsymbol', '').upper()
                    if symbol in tsym and row.get('expiry') == expiry_date_str and option_type in tsym:
                        try: row_strike = float(row.get('strike', 0))
                        except: row_strike = 0.0
                        if row_strike == target_strike:
                            return row.get('instrument_key')
    return None

def get_specific_candle(instrument_key, target_date, target_time, token):
    encoded_key = urllib.parse.quote(instrument_key)
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    if instrument_key.count('|') >= 2:
        url = f"https://api.upstox.com/v2/expired-instruments/historical-candle/{encoded_key}/1minute/{target_date}/{target_date}"
    else:
        url = f"https://api.upstox.com/v3/historical-candle/{encoded_key}/minutes/1/{target_date}/{target_date}"
        
    res = robust_api_get(url, headers)
    if res and res.status_code == 200 and res.json().get("status") == "success":
        target_ts = f"{target_date}T{target_time}"
        for candle in res.json().get("data", {}).get("candles", []):
            if str(candle[0]).startswith(target_ts):
                return candle[1] # Return Open Price
    return None

# --- Core Calculations Engine (Cash & Options) ---
def calculate_trade(symbol, trade_date, trigger_time, strategy_name, token, tgt_p):
    tf_label, tf_minutes = parse_tf_from_strategy(strategy_name)
    symbol_clean = str(symbol).strip().upper()
    is_short = str(strategy_name).strip().lower().startswith('s_')
    
    # Auto Assign Base Variables
    ce_pe = "PE" if is_short else "CE"
    lot_size = lot_sizes_dict.get(symbol_clean, 1)
    instrument_key = get_instrument_key(symbol_clean, token)
    
    result = {
        "Strategy Name": strategy_name, "Stock Name": symbol_clean, "Date": trade_date, 
        "Trigger Time": str(trigger_time)[:5], "Execution Time": None, 
        "ATM Strike": None, "CE/PE": ce_pe, "Lot Size": lot_size,
        "Cash Entry": None, "Cash Exit": None, "CMP": None, 
        "Initial SL": None, "Current TSL": None, 
        "Cash MTM (₹)": 0.0, "Max Loss (₹)": 0.0,
        "Bars in Trade": 0, "Status": "Pending",
        "Opt Entry": None, "Opt Exit": None, "Opt MTM": None
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

    # Calculate ATR
    df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'oi'])
    df['datetime'] = pd.to_datetime(df['timestamp'])
    df_resampled = df.set_index('datetime').resample(f'{tf_minutes}min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    df_resampled['prev_close'] = df_resampled['close'].shift(1)
    df_resampled['tr'] = df_resampled.apply(lambda x: max(x['high'] - x['low'], abs(x['high'] - x['prev_close']), abs(x['low'] - x['prev_close'])) if pd.notna(x['prev_close']) else x['high'] - x['low'], axis=1)
    df_resampled['atr'] = df_resampled['tr'].rolling(int(atr_period)).mean()
    
    df['floor_dt'] = df['datetime'].dt.floor(f'{tf_minutes}min')
    df['atr'] = df['floor_dt'].map(df_resampled['atr']).ffill() 

    # Find Execution Entry
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

    # Estimate ATM Strike dynamically based on cash price magnitude
    step = 50
    if entry_price < 250: step = 2.5
    elif entry_price < 500: step = 5
    elif entry_price < 1000: step = 10
    elif entry_price < 3000: step = 20
    result["ATM Strike"] = int(round(entry_price / step) * step)

    # Trailing Setup
    active_atr = entry_atr if pd.notna(entry_atr) and entry_atr > 0 else (entry_price * 0.01)
    risk_per_share = atr_mult * active_atr
    
    initial_sl = entry_price + risk_per_share if is_short else entry_price - risk_per_share
    current_tsl = initial_sl
    tgt_price = entry_price * (1 - (tgt_p / 100)) if is_short else entry_price * (1 + (tgt_p / 100))
    highest_peak = entry_price
    lowest_trough = entry_price
    
    exit_price, exit_time, status, bars_1m = None, None, "Live", 0
    cmp_price = df.iloc[-1]['close'] # Default CMP to latest close
    
    # Walk forward minute-by-minute
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
    result["Bars in Trade"] = round(bars_1m / (tf_minutes if tf_minutes < 1440 else 375), 1)
    result["Status"] = status
    result["Initial SL"] = round(initial_sl, 2)
    result["Current TSL"] = round(current_tsl, 2)
    
    # Financial Calculations
    if is_short:
        result["Cash MTM (₹)"] = round((entry_price - cmp_price) * lot_size, 2)
        result["Max Loss (₹)"] = round((entry_price - current_tsl) * lot_size, 2)
    else:
        result["Cash MTM (₹)"] = round((cmp_price - entry_price) * lot_size, 2)
        result["Max Loss (₹)"] = round((current_tsl - entry_price) * lot_size, 2)
        
    return result

def update_options_in_ledger(token):
    df = st.session_state.master_ledger.copy()
    progress = st.progress(0)
    
    for i, row in df.iterrows():
        try:
            symbol = str(row.get('Stock Name')).strip().upper()
            strike = row.get('ATM Strike')
            ce_pe = row.get('CE/PE')
            lot_size = float(row.get('Lot Size', 1))
            exec_dt_str = str(row.get('Execution Time'))
            
            if pd.notna(strike) and pd.notna(ce_pe) and pd.notna(exec_dt_str):
                trade_date = exec_dt_str[:10]
                exec_time = exec_dt_str[11:16]
                
                # Use Upstox robust resolution
                expiry_str = get_closest_expiry(symbol, trade_date, token)
                if expiry_str:
                    opt_key = resolve_contract(symbol, expiry_str, strike, ce_pe, token)
                    if opt_key:
                        opt_entry = get_specific_candle(opt_key, trade_date, exec_time, token)
                        if opt_entry is not None:
                            df.at[i, 'Opt Entry'] = opt_entry
                            
                            # Get Exit or CMP
                            exit_dt_str = str(row.get('Exit Time'))
                            if pd.notna(row.get('Exit Time')) and exit_dt_str != 'None':
                                opt_exit = get_specific_candle(opt_key, exit_dt_str[:10], exit_dt_str[11:16], token)
                            else:
                                now = datetime.now()
                                opt_exit = get_specific_candle(opt_key, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), token)
                                
                            if opt_exit is not None:
                                df.at[i, 'Opt Exit'] = opt_exit
                                df.at[i, 'Opt MTM'] = round((opt_exit - opt_entry) * lot_size, 2)
        except Exception as e:
            st.error(f"Error updating options for row {i}: {e}")
        progress.progress((i + 1) / len(df))
        
    st.session_state.master_ledger = df
    st.success("Options Sync Complete!")

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
        if st.button("🚀 Fetch & Update Option Prices (Uses Advanced API)", type="primary"):
            if not api_token: st.warning("Please enter your Upstox Token.")
            else: update_options_in_ledger(api_token)
            
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
                    res = calculate_trade(s_symbol, s_date, s_time, s_strategy, api_token, tgt_pct)
                    st.session_state.temp_single_trade = pd.DataFrame([res])
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
                        for idx, row in combined_df.iterrows():
                            time.sleep(0.2)
                            t_time = row.get('Trigger Time', row.get('time', ''))
                            res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), t_time, row.get('Strategy Name'), api_token, tgt_pct)
                            results.append(res)
                            pb.progress((idx + 1) / len(combined_df))
                        st.session_state.temp_bulk_trades = pd.DataFrame(results)
            if not st.session_state.temp_bulk_trades.empty:
                st.dataframe(st.session_state.temp_bulk_trades)
                if st.button("Confirm Bulk Add to Ledger"):
                    append_to_ledger(st.session_state.temp_bulk_trades)
                    st.session_state.temp_bulk_trades = pd.DataFrame(); st.success("Batch Sync Complete!"); st.rerun()
