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
st.set_page_config(page_title="Upstox Swing Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Consolidate trades, handle Buy/Sell strategies automatically, and track statistical hold times.")

# --- Defined Standard Inputs ---
STRATEGIES = [
    "b_ema-x_15mt", "b_ema_x_1hr", "b_rsi_x60_15mt", "b_rsi_x_1hr", 
    "b_vwap_x_15mt", "b_vwap_x_1hr", "b_st_x_15mt", "b_st_x_1hr",
    "s_ema-x_15mt", "s_ema_x_1hr", "s_rsi_x60_15mt", "s_rsi_x_1hr", 
    "s_vwap_x_15mt", "s_vwap_x_1hr", "s_st_x_15mt", "s_st_x_1hr"
]

TF_OPTIONS = {
    "5m": 5, 
    "15m": 15, 
    "30m": 30, 
    "1hr": 60, 
    "1day": 1440 
}

# --- Initialization of Session States ---
if "master_ledger" not in st.session_state:
    st.session_state.master_ledger = pd.DataFrame()
if "temp_single_trade" not in st.session_state:
    st.session_state.temp_single_trade = pd.DataFrame()
if "temp_bulk_trades" not in st.session_state:
    st.session_state.temp_bulk_trades = pd.DataFrame()
if "debug_logs" not in st.session_state: 
    st.session_state.debug_logs = []

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
    enable_opt_updater = st.checkbox("Enable Option Updater Tool", value=True)
            
    st.markdown("---")
    debug_mode = st.checkbox("🐞 Enable Debug Mode", value=False)

def log_debug(message):
    if debug_mode: st.session_state.debug_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

# --- Load F&O Lot Sizes for Auto Qty Lookup ---
@st.cache_data(show_spinner=False)
def load_fno_details():
    liquid_symbols = set()
    lot_sizes = {}
    liquid_file = 'fno_with_sectors - liquid.csv'
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
        except Exception as e:
            log_debug(f"Error reading liquid CSV: {e}")
    return liquid_symbols, lot_sizes

liquid_symbols, lot_sizes = load_fno_details()

# --- Helpers: Email & API ---
def send_exit_alert(exited_trades_df, recipient, sender, password):
    if exited_trades_df.empty: return False
    subject = f"🚨 Trade Exit Alert: {len(exited_trades_df)} Positions Closed"
    html_table = exited_trades_df[['Stock Name', 'Strategy Name', 'Status', 'Entry Price', 'Exit Price', 'PnL %']].to_html(index=False, border=1)
    body = f"<html><body><h2>Trade Exit Alerts</h2>{html_table}</body></html>"
    msg = MIMEMultipart()
    msg['From'] = sender; msg['To'] = recipient; msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender, password); server.send_message(msg); server.quit()
        return True
    except Exception as e:
        log_debug(f"Email failed: {e}"); return False

def check_and_send_alerts(final_df):
    if not enable_emails or final_df.empty: return
    today_str = datetime.today().strftime('%Y-%m-%d')
    exits_today = final_df[(final_df['Status'].isin(["SL Hit", "Target Hit"])) & (final_df['Exit Time'].astype(str).str.contains(today_str, na=False))]
    if not exits_today.empty and send_exit_alert(exits_today, alert_email, alert_email, email_pass):
        st.toast("✅ Email alert sent!")

def robust_api_get(url, headers, max_retries=4):
    for attempt in range(max_retries):
        res = requests.get(url, headers=headers)
        if res.status_code == 200: return res
        elif res.status_code == 429: time.sleep(2 ** attempt) 
        else: time.sleep(1)
    return res 

@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_key(symbol, token, segment="EQ", opt_query=None):
    if not token: return None
    symbol_clean = str(symbol).strip().upper()
    if segment == "OPT" and opt_query:
        url = f"https://api.upstox.com/v2/instruments/search?query={urllib.parse.quote(opt_query)}&exchanges=NFO&segments=OPT"
    else:
        url = f"https://api.upstox.com/v2/instruments/search?query={urllib.parse.quote(symbol_clean)}&exchanges=NSE&segments=EQ"
    res = robust_api_get(url, {'Accept': 'application/json', 'Authorization': f'Bearer {token}'})
    if res and res.status_code == 200 and res.json().get('data'):
        return res.json()['data'][0]['instrument_key'] if segment == "EQ" else res.json()['data'][0]
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

# --- Core Calculations Engine (Equity) ---
def calculate_trade(symbol, trade_date, trigger_time, strategy_name, token, sl_p, tgt_p, tf_label, tf_minutes):
    symbol_clean = str(symbol).strip().upper()
    category = "Liquid" if symbol_clean in liquid_symbols else "Others"
    instrument_key = get_instrument_key(symbol_clean, token)
    is_short = str(strategy_name).strip().lower().startswith('s_')
    
    trigger_time_str = str(trigger_time)[:5]
    try:
        t_dt = pd.to_datetime(f"{trade_date} {trigger_time_str}")
        exec_target_str = (t_dt + timedelta(minutes=int(tf_minutes))).strftime("%Y-%m-%dT%H:%M") 
    except:
        exec_target_str = f"{trade_date}T{trigger_time_str}"
        
    result = {
        "Strategy Name": strategy_name, "Stock Name": symbol_clean, "Category": category, 
        "Date": trade_date, "Timeframe": tf_label, "Trigger Time": trigger_time_str, 
        "Execution Time": None, "Entry Price": None, "Exit Price": None, 
        "Exit Time": None, "Bars in Trade": 0, "Status": "Pending", "PnL (1 qty)": None, "PnL %": None
    }
    
    if not instrument_key:
        result["Status"] = "Error: Symbol Not Found"; return result
    candles = fetch_all_candles(instrument_key, trade_date, token)
    if not candles:
        result["Status"] = "Error: No Market Data"; return result

    entry_price, entry_idx, actual_exec_time = None, -1, None
    for i, c in enumerate(candles):
        if c[0][:16] >= exec_target_str:
            entry_price, entry_idx, actual_exec_time = c[1], i, c[0][:16]
            break

    if entry_price is None:
        result["Status"] = f"Error: No candles after entry time"; return result

    result["Execution Time"] = actual_exec_time.replace('T', ' ')
    sl_price = entry_price * (1 + (sl_p / 100)) if is_short else entry_price * (1 - (sl_p / 100))
    tgt_price = entry_price * (1 - (tgt_p / 100)) if is_short else entry_price * (1 + (tgt_p / 100))
        
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

    if exit_price is None: exit_price, exit_time = candles[-1][4], candles[-1][0].split('+')[0]

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

# --- Core Calculations Engine (Options On-Demand) ---
def update_options_in_ledger(token):
    df = st.session_state.master_ledger.copy()
    col_strike = next((c for c in df.columns if c.lower() == 'atm strike'), None)
    col_cepe = next((c for c in df.columns if c.lower() == 'ce/pe'), None)
    
    if not col_strike or not col_cepe:
        st.warning("Please ensure 'atm strike' and 'CE/PE' columns exist in your ledger layout.")
        return

    for col in ['qty', 'Opt Entry', 'Opt Exit', 'Opt PnL', 'Opt PnL %']:
        if col not in df.columns: df[col] = None

    progress_bar = st.progress(0)
    updated_count = 0

    for i, row in df.iterrows():
        strike = row.get(col_strike)
        ce_pe = row.get(col_cepe)
        symbol = str(row['Stock Name']).strip().upper()
        
        if pd.isna(strike) or pd.isna(ce_pe) or str(strike).strip().lower() in ['nan', 'none', '']: continue
        
        is_live = str(row.get('Status')).strip().lower() == 'live'
        if is_missing := (pd.isna(row.get('Opt Entry')) or (pd.isna(row.get('Opt Exit')) and not is_live) or is_live):
            try:
                opt_query = f"{symbol} {int(float(strike))} {str(ce_pe).strip().upper()}"
                contract_data = get_instrument_key(symbol, token, segment="OPT", opt_query=opt_query)
                if not contract_data: continue
                
                opt_key = contract_data['instrument_key']
                official_lot = float(contract_data.get('lot_size', 1))
                
                # Assign lot size
                q = float(row.get('qty')) if pd.notna(row.get('qty')) and float(row.get('qty')) != 1 else official_lot
                df.at[i, 'qty'] = q
                
                # Parse date format dynamically
                exec_dt = pd.to_datetime(row['Execution Time'])
                opt_candles = fetch_all_candles(opt_key, exec_dt.strftime('%Y-%m-%d'), token)
                
                if opt_candles:
                    opt_entry, opt_exit = None, None
                    e_d, e_t = exec_dt.strftime('%Y-%m-%d'), exec_dt.strftime('%H:%M')
                    
                    for c in opt_candles:
                        if c[0][:10] == e_d and c[0][11:16] == e_t:
                            opt_entry = c[1]; break
                    
                    if is_live:
                        opt_exit = opt_candles[-1][4]
                    elif pd.notna(row.get('Exit Time')):
                        ex_dt = pd.to_datetime(row['Exit Time'])
                        ex_d, ex_t = ex_dt.strftime('%Y-%m-%d'), ex_dt.strftime('%H:%M')
                        for c in opt_candles:
                            if c[0][:10] == ex_d and c[0][11:16] == ex_t:
                                opt_exit = c[4]; break
                                
                    if opt_entry is not None: df.at[i, 'Opt Entry'] = round(opt_entry, 2)
                    if opt_exit is not None: df.at[i, 'Opt Exit'] = round(opt_exit, 2)
                    
                    if opt_entry and opt_exit:
                        pnl = (opt_exit - opt_entry) * q
                        df.at[i, 'Opt PnL'] = round(pnl, 2)
                        df.at[i, 'Opt PnL %'] = round(((opt_exit - opt_entry) / opt_entry) * 100, 2)
                        updated_count += 1
                time.sleep(0.3)
            except Exception as e:
                log_debug(f"Row {i} Options error: {e}")
        progress_bar.progress((i + 1) / len(df))
        
    st.session_state.master_ledger = df
    st.success(f"Options sync complete! Updated {updated_count} rows.")

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
        losses = len(group[group["Status"] == "SL Hit"])
        live = len(group[group["Status"] == "Live"])
        completed = wins + losses
        winrate = (wins / completed * 100) if completed > 0 else 0
        mean_bars = group["Bars in Trade"].mean()
        std_bars = group["Bars in Trade"].std(ddof=0)
        target_exit_bars = mean_bars + std_bars if pd.notna(std_bars) else mean_bars
        
        booked = group.loc[group["Status"] != "Live", "PnL (1 qty)"].sum()
        mtm = group.loc[group["Status"] == "Live", "PnL (1 qty)"].sum()
        tot = group["PnL (1 qty)"].sum()
        opt_pnl = group["Opt PnL"].sum() if "Opt PnL" in group.columns else 0
        
        stats.append({
            group_column: name, "Total Trades": len(group), "Wins": wins, "Losses": losses, "Live Trades": live,
            "Win Rate %": round(winrate, 1), "Avg Bars": round(mean_bars, 1) if pd.notna(mean_bars) else 0,
            "Target Exit Bars (Mean+1σ)": round(target_exit_bars, 1) if pd.notna(target_exit_bars) else 0,
            "Total Eq PnL": round(tot, 2), "Total Opt PnL": round(opt_pnl, 2)
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
tab1, tab2, tab3 = st.tabs(["📚 Master Ledger", "往 Add Trades (Bulk/Single)", "📊 Summary Stats"])

with tab1:
    st.subheader("Your Consolidated Master Ledger")
    if enable_opt_updater and not st.session_state.master_ledger.empty:
        if st.button("🚀 Fetch & Update Option Prices", type="primary"):
            if not api_token: st.warning("Please enter your Upstox Token in the sidebar setup.")
            else: update_options_in_ledger(api_token)
            
    edited_ledger = st.data_editor(st.session_state.master_ledger, use_container_width=True, num_rows="dynamic")
    col_save, col_dl, col_clear = st.columns([1, 1, 1])
    if col_save.button("💾 Save Edits", type="primary", use_container_width=True):
        st.session_state.master_ledger = edited_ledger
        st.success("Ledger saved!")
    
    current_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    csv_data = st.session_state.master_ledger.to_csv(index=False).encode('utf-8')
    col_dl.download_button("📥 Download Ledger (CSV)", data=csv_data, file_name=f"Master_Ledger_{current_time_str}.csv", mime="text/csv", use_container_width=True)
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
                res = calculate_trade(s_symbol, s_date, s_time, s_strategy, api_token, sl_pct, tgt_pct, tf_label, tf_minutes)
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
            all_dfs = [pd.read_csv(f) for f in uploaded_files]
            combined_df = parse_uploaded_csv(pd.concat(all_dfs, ignore_index=True), batch_strategy_name)
            if st.button("Process Batch Run"):
                if not api_token: st.warning("Enter API token.")
                else:
                    results = []
                    pb = st.progress(0)
                    for idx, row in combined_df.iterrows():
                        time.sleep(0.2)
                        t_time = row.get('Trigger Time', row.get('time', ''))
                        res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), t_time, row.get('Strategy Name'), api_token, sl_pct, tgt_pct, tf_label, tf_minutes)
                        results.append(res)
                        pb.progress((idx + 1) / len(combined_df))
                    st.session_state.temp_bulk_trades = pd.DataFrame(results)
            if not st.session_state.temp_bulk_trades.empty:
                st.dataframe(st.session_state.temp_bulk_trades)
                if st.button("Confirm Bulk Add to Ledger"):
                    append_to_ledger(st.session_state.temp_bulk_trades)
                    check_and_send_alerts(st.session_state.temp_bulk_trades)
                    st.session_state.temp_bulk_trades = pd.DataFrame(); st.success("Batch Sync Complete!"); st.rerun()

with tab3:
    st.subheader("Performance Analytics Summary")
    if st.session_state.master_ledger.empty: st.info("Ledger is empty.")
    else:
        valid_res = st.session_state.master_ledger[~st.session_state.master_ledger['Status'].astype(str).str.contains("Error", na=False)]
        if not valid_res.empty:
            st.markdown("### 🏆 Strategy-wise Breakdown")
            st.dataframe(generate_summary(valid_res, 'Strategy Name'), use_container_width=True)
            st.markdown("### 🏢 Stock-wise Breakdown")
            st.dataframe(generate_summary(valid_res, 'Stock Name'), use_container_width=True)
