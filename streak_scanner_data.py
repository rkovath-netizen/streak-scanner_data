import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime, timedelta
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# --- Page Config ---
st.set_page_config(page_title="Upstox Trade Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Consolidate trades, handle Buy/Sell strategies automatically, and track Option PnL.")

# --- Defined Standard Inputs ---
STRATEGIES = [
    "b_ema-x_15mt", "b_ema_x_1hr", "b_rsi_x60_15mt", "b_rsi_x_1hr", 
    "b_vwap_x_15mt", "b_vwap_x_1hr", "b_st_x_15mt", "b_st_x_1hr",
    "s_ema-x_15mt", "s_ema_x_1hr", "s_rsi_x60_15mt", "s_rsi_x_1hr", 
    "s_vwap_x_15mt", "s_vwap_x_1hr", "s_st_x_15mt", "s_st_x_1hr"
]

TF_OPTIONS = {"5m": 5, "15m": 15, "30m": 30, "1hr": 60, "1day": 1440}

# --- Initialization of Session States ---
if "master_ledger" not in st.session_state: st.session_state.master_ledger = pd.DataFrame()
if "temp_single_trade" not in st.session_state: st.session_state.temp_single_trade = pd.DataFrame()
if "temp_bulk_trades" not in st.session_state: st.session_state.temp_bulk_trades = pd.DataFrame()
if "debug_logs" not in st.session_state: st.session_state.debug_logs = []

# --- Sidebar: Configuration ---
with st.sidebar:
    st.header("🔑 API & Alerts Setup")
    default_token = st.secrets.get("UPSTOX_TOKEN", "")
    api_token = st.text_input("Enter Upstox Analytics Token", value=default_token, type="password")
    
    st.markdown("---")
    st.subheader("📧 Email Alerts")
    enable_emails = st.checkbox("Enable Exit Alerts", value=True)
    alert_email = st.text_input("Alert Email Address", value="9035490861r@gmail.com")
    email_pass = st.text_input("Gmail App Password", value="oeci llhn noig moew", type="password")
    
    st.markdown("---")
    st.markdown("**Trade Parameters**")
    sl_pct = st.number_input("Stop Loss %", value=2.0, step=0.5)
    tgt_pct = st.number_input("Target %", value=5.0, step=0.5)
    
    st.markdown("---")
    st.subheader("⏱️ Timeframe Setup")
    tf_label = st.selectbox("Standard Timeframe", options=list(TF_OPTIONS.keys()), index=3)
    tf_minutes = TF_OPTIONS[tf_label]
    
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

    st.markdown("---")
    st.subheader("🔥 Option Fetcher")
    enable_opt_updater = st.checkbox("Enable Option Updater Tool", value=False)
            
    st.markdown("---")
    debug_mode = st.checkbox("🐞 Enable Debug Mode", value=False)

def log_debug(message):
    if debug_mode: st.session_state.debug_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

# --- Helper: Missing Value Checker ---
def is_missing(val):
    """Safely checks if a cell is truly empty or filled with string 'None', 'NaN', etc."""
    if pd.isna(val): return True
    if str(val).strip().lower() in ['nan', 'none', '', 'null', '<na>']: return True
    return False

# --- NFO Master Data Fetcher (Instant Speed) ---
@st.cache_data(ttl=3600*24, show_spinner=False)
def load_upstox_nfo_instruments():
    """Downloads the official Upstox NFO database to instantly map Option Keys and Lot Sizes."""
    try:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NFO.csv.gz"
        df = pd.read_csv(url)
        df.columns = [str(c).lower().strip() for c in df.columns]
        if 'strike' in df.columns:
            df['strike'] = pd.to_numeric(df['strike'], errors='coerce')
        if 'expiry' in df.columns:
            df['expiry'] = pd.to_datetime(df['expiry'], errors='coerce')
        return df
    except Exception as e:
        log_debug(f"Failed to load NFO master: {e}")
        return pd.DataFrame()

# --- Equity Market Data API ---
def robust_api_get(url, headers, max_retries=4):
    for attempt in range(max_retries):
        res = requests.get(url, headers=headers)
        if res.status_code == 200: return res
        elif res.status_code == 429: time.sleep(2 ** attempt) 
        else: time.sleep(1)
    return res 

@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_key(symbol, token):
    if not token: return None
    symbol_clean = str(symbol).strip().upper()
    query = urllib.parse.quote(symbol_clean)
    url = f"https://api.upstox.com/v2/instruments/search?query={query}&exchanges=NSE&segments=EQ"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    res = robust_api_get(url, headers)
    
    if res.status_code == 200:
        data = res.json()
        if 'data' in data and len(data['data']) > 0:
            for inst in data['data']:
                if inst.get('trading_symbol', '').upper() == symbol_clean:
                    return inst['instrument_key']
            return data['data'][0]['instrument_key']
    return None

def fetch_all_candles(instrument_key, from_date_str, token):
    encoded_key = urllib.parse.quote(instrument_key)
    today_str = datetime.today().strftime('%Y-%m-%d')
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    all_candles = []
    
    hist_url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/1minute/{today_str}/{from_date_str}"
    hist_res = robust_api_get(hist_url, headers)
    if hist_res.status_code == 200:
        data = hist_res.json()
        if 'data' in data and 'candles' in data['data']:
            candles = data['data']['candles']
            candles.reverse() 
            all_candles.extend(candles)

    intra_url = f"https://api.upstox.com/v2/historical-candle/intraday/{encoded_key}/1minute"
    intra_res = robust_api_get(intra_url, headers)
    if intra_res.status_code == 200:
        data = intra_res.json()
        if 'data' in data and 'candles' in data['data']:
            candles = data['data']['candles']
            candles.reverse()
            existing_timestamps = {c[0] for c in all_candles}
            for c in candles:
                if c[0] not in existing_timestamps:
                    all_candles.append(c)

    all_candles.sort(key=lambda x: x[0])
    return all_candles, hist_res.status_code

def parse_excel_date(date_str):
    """Safely converts Excel formats like '7/3/2026 11:15' into Upstox-compatible strings."""
    if is_missing(date_str): return None, None
    try:
        dt_obj = pd.to_datetime(date_str)
        return dt_obj.strftime('%Y-%m-%d'), dt_obj.strftime('%H:%M')
    except:
        return None, None

# --- Option Update Engine ---
def update_options_in_ledger(token):
    df = st.session_state.master_ledger.copy()
    
    col_strike = next((c for c in df.columns if c.lower() == 'atm strike'), None)
    col_cepe = next((c for c in df.columns if c.lower() == 'ce/pe'), None)
    
    if not col_strike or not col_cepe:
        st.warning("Columns 'atm strike' and 'CE/PE' not found. Please add them via Excel.")
        return

    for col in ['qty', 'Opt Entry', 'Opt Exit', 'Opt PnL', 'Opt PnL %']:
        if col not in df.columns: df[col] = None

    nfo_df = load_upstox_nfo_instruments()
    if nfo_df.empty:
        st.error("Could not load Upstox NFO Master list.")
        return

    progress_bar = st.progress(0)
    updated_count = 0

    for i, row in df.iterrows():
        strike = row.get(col_strike)
        ce_pe = row.get(col_cepe)
        symbol = str(row['Stock Name']).strip().upper()
        
        if is_missing(strike) or is_missing(ce_pe):
            continue
            
        is_live = str(row.get('Status')).strip().lower() == 'live'
        needs_entry = is_missing(row.get('Opt Entry'))
        needs_exit = is_missing(row.get('Opt Exit'))
        
        if needs_entry or is_live or (needs_exit and not is_live):
            try: strike_int = float(strike)
            except: continue
            ce_pe_clean = str(ce_pe).strip().upper()
            
            # Instant memory search against NFO Database
            mask = (
                (nfo_df['name'].str.upper() == symbol) & 
                (nfo_df['strike'] == strike_int) & 
                (nfo_df['tradingsymbol'].str.upper().str.endswith(ce_pe_clean)) &
                (nfo_df['instrument_type'].isin(['OPTSTK', 'OPTIDX']))
            )
            subset = nfo_df[mask]
            
            if subset.empty:
                continue
                
            subset = subset.sort_values('expiry')
            best_match = subset.iloc[0]
            opt_key = best_match['instrument_key']
            official_lot_size = float(best_match.get('lot_size', 1))
            
            # --- Enforce Official Lot Size ---
            qty = row.get('qty')
            if is_missing(qty) or float(qty) == 1:
                q = official_lot_size
            else:
                try: q = float(qty)
                except: q = official_lot_size
            df.at[i, 'qty'] = q
            
            # Parse Dates
            exec_d, exec_t = parse_excel_date(row.get('Execution Time'))
            if not exec_d: continue
            
            candles, _ = fetch_all_candles(opt_key, exec_d, token)
            if candles:
                opt_entry, opt_exit = None, None
                
                # Match Option Entry
                for c in candles:
                    c_d, c_t = c[0].split('T')[0], c[0].split('T')[1][:5]
                    if c_d == exec_d and c_t == exec_t:
                        opt_entry = c[1] 
                        break
                
                # Match Option Exit
                if is_live:
                    opt_exit = candles[-1][4] 
                else:
                    ex_d, ex_t = parse_excel_date(row.get('Exit Time'))
                    if ex_d and ex_t:
                        for c in candles:
                            c_d, c_t = c[0].split('T')[0], c[0].split('T')[1][:5]
                            if c_d == ex_d and c_t == ex_t:
                                opt_exit = c[4]
                                break
                                
                # Calculate PnL (Option Trades are always Long Buys to mirror direction)
                if opt_entry is not None: df.at[i, 'Opt Entry'] = round(opt_entry, 2)
                entry_val = df.at[i, 'Opt Entry'] if not is_missing(df.at[i, 'Opt Entry']) else opt_entry
                
                if opt_exit is not None: df.at[i, 'Opt Exit'] = round(opt_exit, 2)
                exit_val = df.at[i, 'Opt Exit'] if not is_missing(df.at[i, 'Opt Exit']) else opt_exit
                    
                if entry_val is not None and exit_val is not None:
                    pnl = (exit_val - entry_val) * q
                    df.at[i, 'Opt PnL'] = round(pnl, 2)
                    df.at[i, 'Opt PnL %'] = round(((exit_val - entry_val) / entry_val) * 100, 2)
                    updated_count += 1
            time.sleep(0.5) 
        progress_bar.progress((i + 1) / len(df))
        
    st.session_state.master_ledger = df
    if updated_count > 0:
        st.success(f"Successfully fetched and updated option prices for {updated_count} rows!")
    else:
        st.info("Scan complete. No matching option rows needed updating.")

# --- Calculation Engine (Equity) ---
def calculate_trade(symbol, trade_date, trigger_time, strategy_name, token, sl_p, tgt_p, tf_label, tf_minutes):
    symbol_clean = str(symbol).strip().upper()
    instrument_key = get_instrument_key(symbol_clean, token)
    is_short = str(strategy_name).strip().lower().startswith('s_')
    
    trigger_time_str = str(trigger_time)[:5]
    try:
        t_dt = pd.to_datetime(f"{trade_date} {trigger_time_str}")
        exec_dt = t_dt + timedelta(minutes=int(tf_minutes))
        exec_target_str = exec_dt.strftime("%Y-%m-%dT%H:%M") 
    except:
        exec_target_str = f"{trade_date}T{trigger_time_str}"
        
    result = {
        "Strategy Name": strategy_name, "Stock Name": symbol_clean, 
        "Date": trade_date, "Timeframe": tf_label, "Trigger Time": trigger_time_str, 
        "Execution Time": None, "Entry Price": None, "Exit Price": None, 
        "Exit Time": None, "Bars in Trade": 0, "Status": "Pending", "PnL (1 qty)": None, "PnL %": None
    }
    
    if not instrument_key: 
        result["Status"] = "Error: Symbol Not Found"
        return result

    candles, _ = fetch_all_candles(instrument_key, trade_date, token)
    if not candles: return result

    entry_price, entry_idx, actual_exec_time = None, -1, None
    for i, c in enumerate(candles):
        c_time_str = c[0][:16] 
        if c_time_str >= exec_target_str:
            entry_price = c[1] 
            entry_idx = i
            actual_exec_time = c_time_str
            break

    if entry_price is None: 
        result["Status"] = f"Error: No candles after {exec_target_str}"
        return result

    result["Execution Time"] = actual_exec_time.replace('T', ' ')

    if is_short:
        sl_price, tgt_price = entry_price * (1 + (sl_p / 100)), entry_price * (1 - (tgt_p / 100))
    else:
        sl_price, tgt_price = entry_price * (1 - (sl_p / 100)), entry_price * (1 + (tgt_p / 100))
        
    exit_price, exit_time, status, bars_1m = None, None, "Live", 0

    for i in range(entry_idx, len(candles)):
        bars_1m += 1
        c_close, c_time_curr = candles[i][4], candles[i][0].split('+')[0]
        
        if is_short:
            if c_close >= sl_price: exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"; break
            elif c_close <= tgt_price: exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"; break
        else:
            if c_close <= sl_price: exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"; break
            elif c_close >= tgt_price: exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"; break

    if exit_price is None:
        exit_price, exit_time = candles[-1][4], candles[-1][0].split('+')[0]

    tf_bars = round(bars_1m / (tf_minutes if tf_minutes < 1440 else 375), 1)
    pnl = (entry_price - exit_price) if is_short else (exit_price - entry_price)

    result["Entry Price"] = round(entry_price, 2)
    result["Exit Price"] = round(exit_price, 2)
    result["Exit Time"] = exit_time.replace('T', ' ') if exit_time else None
    result["Bars in Trade"] = tf_bars
    result["Status"] = status
    result["PnL (1 qty)"] = round(pnl, 2)
    result["PnL %"] = round((pnl / entry_price) * 100, 2)

    return result

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

# --- Main App Interface ---
tab1, tab2, tab3 = st.tabs(["📚 Master Ledger", "📝 Add Single Trade", "📁 Add Bulk CSV"])

with tab1:
    st.subheader("Your Consolidated Master Ledger")
    
    if st.session_state.master_ledger.empty:
        st.info("Your ledger is currently empty. Add trades using the Single Trade or Bulk CSV tabs.")
    else:
        if enable_opt_updater:
            st.markdown("---")
            st.markdown("### 🔥 Options Data Updater")
            st.markdown("Scans the ledger for manually entered `atm strike` and `CE/PE`. **Auto-fills `qty` from official NFO data.** Fetches live/historical option data instantly.")
            if st.button("🚀 Fetch & Update Option Prices", type="primary"):
                if not api_token: st.warning("Please enter your API Token in the sidebar.")
                else:
                    with st.spinner("Scanning and fetching option prices..."):
                        update_options_in_ledger(api_token)
            st.markdown("---")
            
        edited_ledger = st.data_editor(st.session_state.master_ledger, use_container_width=True, num_rows="dynamic")
        col_save, col_dl, col_clear = st.columns([1, 1, 1])
        
        if col_save.button("💾 Save Edits", type="primary", use_container_width=True):
            st.session_state.master_ledger = edited_ledger
            st.success("Ledger edits saved successfully!")
            
        current_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        download_filename = f"Master_Ledger_{current_time_str}.csv"
        csv_data = st.session_state.master_ledger.to_csv(index=False).encode('utf-8')
        col_dl.download_button("📥 Download Ledger (CSV)", data=csv_data, file_name=download_filename, mime="text/csv", use_container_width=True)

        if col_clear.button("🗑️ Clear Ledger", use_container_width=True):
            st.session_state.master_ledger = pd.DataFrame()
            st.rerun()

with tab2:
    st.subheader("Evaluate & Add a Single Trade")
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1: s_strategy = st.selectbox("Strategy Name", options=STRATEGIES)
    with col2: s_symbol = st.text_input("NSE Stock Name", value="RELIANCE")
    with col3: s_date = st.date_input("Trade Date")
    with col4: s_time = st.time_input("Trigger Time", value=pd.to_datetime("09:15").time())
    with col5:
        st.markdown("<br>", unsafe_allow_html=True)
        calc_btn = st.button("Step 1: Calculate Trade", use_container_width=True)

    if calc_btn:
        st.session_state.debug_logs = []
        if not api_token: st.warning("Please enter your API Token.")
        else:
            with st.spinner("Calculating offset entry time..."):
                res = calculate_trade(s_symbol, s_date, s_time, s_strategy, api_token, sl_pct, tgt_pct, tf_label, tf_minutes)
                st.session_state.temp_single_trade = pd.DataFrame([res])

    if not st.session_state.temp_single_trade.empty:
        st.markdown("### ✅ Review Calculation")
        edited_single = st.data_editor(st.session_state.temp_single_trade, use_container_width=True, num_rows="dynamic")
        
        if st.button("Step 2: Confirm & Add to Master Ledger", type="primary"):
            append_to_ledger(edited_single)
            st.session_state.temp_single_trade = pd.DataFrame() 
            st.success("Trade successfully added to Master Ledger!")
            time.sleep(1) 
            st.rerun()

with tab3:
    st.subheader("Process & Add Multiple Trades via CSV")
    batch_strategy_name = st.selectbox("Assign a Strategy Name for this batch:", options=STRATEGIES)
    uploaded_files = st.file_uploader("Upload Bulk Export CSV(s)", type=["csv"], accept_multiple_files=True)
    
    if uploaded_files:
        all_dfs = []
        for file in uploaded_files:
            try:
                temp_df = pd.read_csv(file)
                if not temp_df.empty: all_dfs.append(temp_df)
            except: pass
            
        if all_dfs:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            combined_df = parse_uploaded_csv(combined_df, batch_strategy_name)
            
            if st.button("Step 1: Process Batch Calculations"):
                st.session_state.debug_logs = []
                if not api_token: st.warning("Please enter your API Token.")
                else:
                    results_list = []
                    progress_bar = st.progress(0)
                    st.cache_data.clear() 
                    for i, row in combined_df.iterrows():
                        time.sleep(0.5)
                        trigger_t = row.get('Trigger Time', row.get('time', ''))
                        st_name = row.get('Strategy Name', batch_strategy_name)
                        res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), trigger_t, st_name, api_token, sl_pct, tgt_pct, tf_label, tf_minutes)
                        results_list.append(res)
                        progress_bar.progress((i + 1) / len(combined_df))
                    
                    st.session_state.temp_bulk_trades = pd.DataFrame(results_list)

    if not st.session_state.temp_bulk_trades.empty:
        st.markdown("### ✅ Review Batch Calculations")
        edited_bulk = st.data_editor(st.session_state.temp_bulk_trades, use_container_width=True, num_rows="dynamic")
        
        if st.button("Step 2: Confirm & Add Batch to Master Ledger", type="primary"):
            append_to_ledger(edited_bulk)
            st.session_state.temp_bulk_trades = pd.DataFrame() 
            st.success("Batch successfully added to Master Ledger!")
            time.sleep(1)
            st.rerun()
