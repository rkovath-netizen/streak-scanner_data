import streamlit as st
import pandas as pd
import numpy as np
import requests
import urllib.parse
from datetime import datetime, timedelta
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# --- Page Config ---
st.set_page_config(page_title="Upstox Swing Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Consolidate trades, auto-calculate Trade Size via ATR, and run Trailing Stop Losses.")

# --- Defined Standard Inputs ---
STRATEGIES = [
    "b_ema-x_15mt", "b_ema_x_1hr", "b_rsi_x60_15mt", "b_rsi_x_1hr", 
    "b_vwap_x_15mt", "b_vwap_x_1hr", "b_st_x_15mt", "b_st_x_1hr",
    "s_ema-x_15mt", "s_ema_x_1hr", "s_rsi_x60_15mt", "s_rsi_x_1hr", 
    "s_vwap_x_15mt", "s_vwap_x_1hr", "s_st_x_15mt", "s_st_x_1hr"
]
TF_OPTIONS = {"5m": 5, "15m": 15, "30m": 30, "1hr": 60, "1day": 1440}

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
    st.markdown("**Trade Parameters (Risk & Target)**")
    max_risk = st.number_input("Max Risk per Trade (₹)", value=1000.0, step=100.0, help="Used with ATR to calculate Trade Qty.")
    atr_mult = st.number_input("ATR Trailing Multiplier", value=3.0, step=0.5, help="Distance of the trailing SL from the peak.")
    atr_period = st.number_input("ATR Period", value=14, step=1)
    tgt_pct = st.number_input("Fixed Target %", value=5.0, step=0.5, help="Leaves a fixed upside target.")
    
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

    debug_mode = st.checkbox("🐞 Enable Debug Mode", value=False)

def log_debug(message):
    if debug_mode: st.session_state.debug_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

@st.cache_data(show_spinner=False)
def load_fno_details():
    liquid_symbols = set()
    liquid_file = 'fno_with_sectors - liquid.csv'
    if os.path.exists(liquid_file):
        try:
            df = pd.read_csv(liquid_file)
            if 'Symbol' in df.columns:
                liquid_symbols = set(df['Symbol'].str.strip().str.upper())
        except: pass
    return liquid_symbols

liquid_symbols = load_fno_details()

# --- Helpers: API ---
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

# --- Core Calculations Engine (ATR & Trailing SL) ---
def calculate_trade(symbol, trade_date, trigger_time, strategy_name, token, tgt_p, tf_label, tf_minutes):
    symbol_clean = str(symbol).strip().upper()
    category = "Liquid" if symbol_clean in liquid_symbols else "Others"
    instrument_key = get_instrument_key(symbol_clean, token)
    is_short = str(strategy_name).strip().lower().startswith('s_')
    
    result = {
        "Strategy Name": strategy_name, "Stock Name": symbol_clean, "Category": category, 
        "Date": trade_date, "Timeframe": tf_label, "Trigger Time": str(trigger_time)[:5], 
        "Execution Time": None, "Entry Price": None, "Exit Price": None, 
        "Exit Time": None, "Bars in Trade": 0, "Status": "Pending", "Qty": 0, "Total PnL (₹)": 0.0
    }
    
    if not instrument_key:
        result["Status"] = "Error: Symbol Not Found"; return result
        
    # Fetch extra historical data (15 days back) to warm up the 14-period ATR
    try:
        t_dt = pd.to_datetime(f"{trade_date} {str(trigger_time)[:5]}")
        exec_target_str = (t_dt + timedelta(minutes=int(tf_minutes))).strftime("%Y-%m-%dT%H:%M") 
        hist_start_str = (t_dt - timedelta(days=15)).strftime("%Y-%m-%d")
    except:
        result["Status"] = "Error: Invalid Date/Time"; return result

    raw_candles = fetch_all_candles(instrument_key, hist_start_str, token)
    if not raw_candles:
        result["Status"] = "Error: No Market Data"; return result

    # 1. Convert to DataFrame and calculate ATR
    df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'oi'])
    df['datetime'] = pd.to_datetime(df['timestamp'])
    
    # Resample to strategy timeframe to calculate true ATR
    df_resampled = df.set_index('datetime').resample(f'{tf_minutes}min').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna()
    
    df_resampled['prev_close'] = df_resampled['close'].shift(1)
    df_resampled['tr'] = df_resampled.apply(
        lambda x: max(x['high'] - x['low'], abs(x['high'] - x['prev_close']), abs(x['low'] - x['prev_close'])) 
        if pd.notna(x['prev_close']) else x['high'] - x['low'], axis=1
    )
    df_resampled['atr'] = df_resampled['tr'].rolling(int(atr_period)).mean()
    
    # Map ATR back to 1-minute data
    df['floor_dt'] = df['datetime'].dt.floor(f'{tf_minutes}min')
    df['atr'] = df['floor_dt'].map(df_resampled['atr'])
    df['atr'] = df['atr'].ffill() # Forward fill so every minute has the current active ATR

    # 2. Find Execution Entry
    entry_price, entry_idx, actual_exec_time, entry_atr = None, -1, None, None
    for i, row in df.iterrows():
        if row['timestamp'][:16] >= exec_target_str:
            entry_price, entry_idx, actual_exec_time = row['open'], i, row['timestamp'][:16]
            entry_atr = row['atr']
            break

    if entry_price is None:
        result["Status"] = f"Error: No candles after entry time"; return result
        
    result["Execution Time"] = actual_exec_time.replace('T', ' ')

    # 3. Position Sizing
    # If ATR is missing (not enough historical data), fallback to 1% risk to avoid division by zero
    active_atr = entry_atr if pd.notna(entry_atr) and entry_atr > 0 else (entry_price * 0.01)
    risk_per_share = atr_mult * active_atr
    qty = max(1, int(max_risk / risk_per_share)) # Always trade at least 1 share
    result["Qty"] = qty

    # 4. Trailing Loop Setup
    tgt_price = entry_price * (1 - (tgt_p / 100)) if is_short else entry_price * (1 + (tgt_p / 100))
    highest_peak = entry_price
    lowest_trough = entry_price
    
    exit_price, exit_time, status, bars_1m = None, None, "Live", 0
    
    # Walk forward minute-by-minute
    for i in range(entry_idx, len(df)):
        bars_1m += 1
        c_close = df.loc[i, 'close']
        c_time_curr = df.loc[i, 'timestamp'].split('+')[0]
        c_atr = df.loc[i, 'atr']
        
        # Dynamically update trailing distance if ATR changes, otherwise use initial
        current_atr = c_atr if pd.notna(c_atr) and c_atr > 0 else active_atr
        
        if is_short:
            lowest_trough = min(lowest_trough, c_close)
            trailing_sl = lowest_trough + (atr_mult * current_atr)
            
            if c_close >= trailing_sl: 
                exit_price, exit_time, status = c_close, c_time_curr, "Trailing SL Hit"
                break
            elif c_close <= tgt_price: 
                exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
                break
        else:
            highest_peak = max(highest_peak, c_close)
            trailing_sl = highest_peak - (atr_mult * current_atr)
            
            if c_close <= trailing_sl: 
                exit_price, exit_time, status = c_close, c_time_curr, "Trailing SL Hit"
                break
            elif c_close >= tgt_price: 
                exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
                break

    if exit_price is None: 
        exit_price, exit_time = df.iloc[-1]['close'], df.iloc[-1]['timestamp'].split('+')[0]

    tf_bars = round(bars_1m / (tf_minutes if tf_minutes < 1440 else 375), 1)
    pnl_1_qty = (entry_price - exit_price) if is_short else (exit_price - entry_price)

    result["Entry Price"] = round(entry_price, 2)
    result["Exit Price"] = round(exit_price, 2)
    result["Exit Time"] = exit_time.replace('T', ' ') if exit_time else None
    result["Bars in Trade"] = tf_bars
    result["Status"] = status
    result["Total PnL (₹)"] = round(pnl_1_qty * qty, 2)
    
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

def generate_summary(df, group_column):
    stats = []
    if group_column not in df.columns or 'Status' not in df.columns: return pd.DataFrame()
    valid_df = df[~df['Status'].astype(str).str.contains("Error", na=False)]
    for name, group in valid_df.groupby(group_column):
        wins = len(group[group["Status"] == "Target Hit"])
        losses = len(group[group["Status"] == "Trailing SL Hit"])
        live = len(group[group["Status"] == "Live"])
        completed = wins + losses
        winrate = (wins / completed * 100) if completed > 0 else 0
        
        tot_pnl = group["Total PnL (₹)"].sum()
        stats.append({
            group_column: name, "Total Trades": len(group), "Wins": wins, "Losses": losses, "Live Trades": live,
            "Win Rate %": round(winrate, 1), "Total PnL (₹)": round(tot_pnl, 2)
        })
    return pd.DataFrame(stats)

def append_to_ledger(new_df):
    if st.session_state.master_ledger.empty:
        st.session_state.master_ledger = new_df.copy()
    else:
        combined = pd.concat([st.session_state.master_ledger, new_df], ignore_index=True)
        dup_subset = ['Stock Name', 'Date', 'Execution Time'] if 'Execution Time' in combined.columns else ['Stock Name', 'Date']
        combined.drop_duplicates(subset=dup_subset, keep='last', inplace=True)
        st.session_state.master_ledger = combined

# --- Tabs ---
tab1, tab2, tab3 = st.tabs(["📚 Master Ledger", "📝 Add Trades (Bulk/Single)", "📊 Summary Stats"])

with tab1:
    st.subheader("Your Consolidated Master Ledger")
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
        s_strategy = st.selectbox("Strategy Name", options=STRATEGIES, key="single_strat")
        s_symbol = st.text_input("NSE Stock Name", value="RELIANCE")
        s_date = st.date_input("Trade Date")
        s_time = st.time_input("Trigger Time", value=pd.to_datetime("09:15").time())
        if st.button("Calculate Single Trade"):
            if not api_token: st.warning("Provide API Token.")
            else:
                with st.spinner("Calculating ATR & Trade Size..."):
                    res = calculate_trade(s_symbol, s_date, s_time, s_strategy, api_token, tgt_pct, tf_label, tf_minutes)
                    st.session_state.temp_single_trade = pd.DataFrame([res])
        if not st.session_state.temp_single_trade.empty:
            st.dataframe(st.session_state.temp_single_trade)
            if st.button("Confirm Single Entry"):
                append_to_ledger(st.session_state.temp_single_trade)
                st.session_state.temp_single_trade = pd.DataFrame(); st.success("Added!"); st.rerun()
                
    with col_b:
        st.subheader("📁 Bulk CSV Processing")
        batch_strategy_name = st.selectbox("Assign Strategy for Batch File", options=STRATEGIES, key="bulk_strat")
        uploaded_files = st.file_uploader("Upload Bulk Export CSV(s)", type=["csv"], accept_multiple_files=True)
        if uploaded_files:
            all_dfs = []
            for f in uploaded_files:
                try:
                    f.seek(0) # Reset file pointer for Streamlit
                    temp_df = pd.read_csv(f)
                    if not temp_df.empty: 
                        all_dfs.append(temp_df)
                except pd.errors.EmptyDataError:
                    pass
                except Exception as e:
                    st.error(f"Error reading file {f.name}: {e}")
                    
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
                            res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), t_time, row.get('Strategy Name'), api_token, tgt_pct, tf_label, tf_minutes)
                            results.append(res)
                            pb.progress((idx + 1) / len(combined_df))
                        st.session_state.temp_bulk_trades = pd.DataFrame(results)
            if not st.session_state.temp_bulk_trades.empty:
                st.dataframe(st.session_state.temp_bulk_trades)
                if st.button("Confirm Bulk Add to Ledger"):
                    append_to_ledger(st.session_state.temp_bulk_trades)
                    st.session_state.temp_bulk_trades = pd.DataFrame(); st.success("Batch Sync Complete!"); st.rerun()

with tab3:
    st.subheader("Performance Analytics Summary")
    if not st.session_state.master_ledger.empty:
        valid_res = st.session_state.master_ledger[~st.session_state.master_ledger['Status'].astype(str).str.contains("Error", na=False)]
        if not valid_res.empty:
            st.markdown("### 🏆 Strategy-wise Breakdown")
            st.dataframe(generate_summary(valid_res, 'Strategy Name'), use_container_width=True)
            st.markdown("### 🏢 Stock-wise Breakdown")
            st.dataframe(generate_summary(valid_res, 'Stock Name'), use_container_width=True)
