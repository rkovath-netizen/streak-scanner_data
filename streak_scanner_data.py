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
    "1day": 1440 # 24 hours offset ensures it executes the next day
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
    tf_label = st.selectbox("Standard Timeframe", options=list(TF_OPTIONS.keys()), index=3, help="Automatically sets the Execution Offset and calculates Bars in Trade.")
    tf_minutes = TF_OPTIONS[tf_label]
    
    st.markdown("---")
    st.subheader("📂 Load Previous Ledger")
    ledger_upload = st.file_uploader("Upload Master Ledger (CSV)", type=["csv"])
    if ledger_upload is not None:
        # BUG FIX: Added a button so it doesn't overwrite the master ledger on every single app rerun
        if st.button("Load Uploaded Ledger"):
            try:
                st.session_state.master_ledger = pd.read_csv(ledger_upload)
                st.success("Ledger loaded successfully!")
            except Exception as e:
                st.error(f"Error loading ledger: {e}")
            
    st.markdown("---")
    debug_mode = st.checkbox("🐞 Enable Debug Mode", value=False)

def log_debug(message):
    if debug_mode: st.session_state.debug_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

# --- Load Liquid Stocks List ---
@st.cache_data(show_spinner=False)
def load_liquid_stocks():
    liquid_file = 'fno_with_sectors - liquid.csv'
    if os.path.exists(liquid_file):
        try:
            df = pd.read_csv(liquid_file)
            if 'Symbol' in df.columns:
                return set(df['Symbol'].str.strip().str.upper())
        except Exception as e:
            log_debug(f"Error reading liquid stocks CSV: {e}")
    return set()

liquid_symbols = load_liquid_stocks()

# --- Helpers: Email & API ---
def send_exit_alert(exited_trades_df, recipient, sender, password):
    if exited_trades_df.empty: return False
    subject = f"🚨 Trade Exit Alert: {len(exited_trades_df)} Positions Closed"
    html_table = exited_trades_df[['Stock Name', 'Strategy Name', 'Status', 'Entry Price', 'Exit Price', 'PnL %']].to_html(index=False, border=1)
    body = f"<html><body><h2>Trade Exit Alerts</h2><p>The following live trades have met their exit criteria (SL or Target) today:</p>{html_table}<br><p><i>Automated via Streamlit Swing Trade Analyzer</i></p></body></html>"
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        log_debug(f"Email failed to send: {e}")
        return False

def check_and_send_alerts(final_df):
    if not enable_emails or final_df.empty: return
    today_str = datetime.today().strftime('%Y-%m-%d')
    exits_today = final_df[(final_df['Status'].isin(["SL Hit", "Target Hit"])) & (final_df['Exit Time'].astype(str).str.contains(today_str, na=False))]
    if not exits_today.empty:
        if send_exit_alert(exits_today, alert_email, alert_email, email_pass):
            st.toast("✅ Email alert sent for closed trades!")
        else:
            st.toast("⚠️ Failed to send email alert. Check debug logs.", icon="⚠️")

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

def calculate_trade(symbol, trade_date, trigger_time, strategy_name, token, sl_p, tgt_p, tf_label, tf_minutes):
    symbol_clean = str(symbol).strip().upper()
    category = "Liquid" if symbol_clean in liquid_symbols else "Others"
    instrument_key = get_instrument_key(symbol_clean, token)
    
    is_short = str(strategy_name).strip().lower().startswith('s_')
    
    trigger_time_str = str(trigger_time)[:5]
    try:
        t_dt = pd.to_datetime(f"{trade_date} {trigger_time_str}")
        exec_dt = t_dt + timedelta(minutes=int(tf_minutes))
        exec_target_str = exec_dt.strftime("%Y-%m-%dT%H:%M") 
    except Exception:
        exec_target_str = f"{trade_date}T{trigger_time_str}"
        
    result = {
        "Strategy Name": strategy_name, "Stock Name": symbol_clean, "Category": category, 
        "Date": trade_date, "Timeframe": tf_label, "Trigger Time": trigger_time_str, 
        "Execution Time": None, "Entry Price": None, "Exit Price": None, 
        "Exit Time": None, "Bars in Trade": 0, "Status": "Pending", "PnL (1 qty)": None, "PnL %": None
    }
    
    if not instrument_key: 
        result["Status"] = "Error: Symbol Not Found"
        return result

    candles, _ = fetch_all_candles(instrument_key, trade_date, token)
    if not candles: 
        result["Status"] = "Error: No Market Data"
        return result

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
        sl_price = entry_price * (1 + (sl_p / 100))
        tgt_price = entry_price * (1 - (tgt_p / 100))
    else:
        sl_price = entry_price * (1 - (sl_p / 100))
        tgt_price = entry_price * (1 + (tgt_p / 100))
        
    exit_price, exit_time, status = None, None, "Live"
    bars_1m = 0

    for i in range(entry_idx, len(candles)):
        bars_1m += 1
        c = candles[i]
        c_close, c_time_curr = c[4], c[0].split('+')[0]
        
        if is_short:
            if c_close >= sl_price:
                exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"
                break
            elif c_close <= tgt_price:
                exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
                break
        else:
            if c_close <= sl_price:
                exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"
                break
            elif c_close >= tgt_price:
                exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
                break

    if exit_price is None:
        exit_price, exit_time = candles[-1][4], candles[-1][0].split('+')[0]

    tf_bars = round(bars_1m / (tf_minutes if tf_minutes < 1440 else 375), 1)

    if is_short:
        pnl = entry_price - exit_price
    else:
        pnl = exit_price - entry_price
        
    pnl_pct = (pnl / entry_price) * 100

    result["Entry Price"] = round(entry_price, 2)
    result["Exit Price"] = round(exit_price, 2)
    result["Exit Time"] = exit_time.replace('T', ' ') if exit_time else None
    result["Bars in Trade"] = tf_bars
    result["Status"] = status
    result["PnL (1 qty)"] = round(pnl, 2)
    result["PnL %"] = round(pnl_pct, 2)

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
        losses = len(group[group["Status"] == "SL Hit"])
        live = len(group[group["Status"] == "Live"])
        completed = wins + losses
        winrate = (wins / completed * 100) if completed > 0 else 0
        
        mean_bars = group["Bars in Trade"].mean()
        std_bars = group["Bars in Trade"].std(ddof=0)
        target_exit_bars = mean_bars + std_bars if pd.notna(std_bars) else mean_bars
        
        booked_pnl = group.loc[group["Status"] != "Live", "PnL (1 qty)"].sum()
        mtm_pnl = group.loc[group["Status"] == "Live", "PnL (1 qty)"].sum()
        tot_pnl = group["PnL (1 qty)"].sum()
        
        cum_pnl, peak, max_dd = 0, 0, 0
        for pnl in group["PnL (1 qty)"]:
            if pd.notna(pnl):
                cum_pnl += pnl
                if cum_pnl > peak: peak = cum_pnl
                dd = peak - cum_pnl
                if dd > max_dd: max_dd = dd
        max_entry = group["Entry Price"].max()
        max_dd_pct = (max_dd / max_entry * 100) if pd.notna(max_entry) and max_entry > 0 else 0
        
        stats.append({
            group_column: name, "Total Trades": len(group), "Wins": wins, "Losses": losses, "Live Trades": live,
            "Win Rate %": round(winrate, 1), 
            "Avg Bars": round(mean_bars, 1) if pd.notna(mean_bars) else 0,
            "Target Exit Bars (Mean+1σ)": round(target_exit_bars, 1) if pd.notna(target_exit_bars) else 0,
            "Booked PnL": round(booked_pnl, 2), "MTM PnL": round(mtm_pnl, 2),
            "Total PnL": round(tot_pnl, 2), "Max DD Amount": round(max_dd, 2), "Max DD %": round(max_dd_pct, 2)
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

def display_debug_logs():
    if debug_mode and st.session_state.debug_logs:
        with st.expander("🛠️ Debug Logs (Click to expand)", expanded=True):
            for log in st.session_state.debug_logs: st.text(log)

# --- Main App ---
tab1, tab2, tab3, tab4 = st.tabs(["📚 Master Ledger", "📝 Add Single Trade", "📁 Add Bulk CSV", "📈 Summary Stats"])

with tab1:
    st.subheader("Your Consolidated Master Ledger")
    st.markdown("💡 **Tip:** Click directly into the table below to manually rename strategies or delete unwanted rows. Click 'Save Edits' to update.")
    
    if st.session_state.master_ledger.empty:
        st.info("Your ledger is currently empty. Add trades using the Single Trade or Bulk CSV tabs.")
    else:
        edited_ledger = st.data_editor(st.session_state.master_ledger, use_container_width=True, num_rows="dynamic")
        col_save, col_dl, col_clear = st.columns([1, 1, 1])
        
        if col_save.button("💾 Save Edits", type="primary", use_container_width=True):
            st.session_state.master_ledger = edited_ledger
            st.success("Ledger edits saved successfully! Summary stats have been updated.")
            
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
            display_debug_logs()

    if not st.session_state.temp_single_trade.empty:
        st.markdown("### ✅ Review Calculation")
        edited_single = st.data_editor(st.session_state.temp_single_trade, use_container_width=True, num_rows="dynamic")
        
        if st.button("Step 2: Confirm & Add to Master Ledger", type="primary"):
            append_to_ledger(edited_single)
            check_and_send_alerts(edited_single)
            st.session_state.temp_single_trade = pd.DataFrame() 
            st.success("Trade successfully added to Master Ledger!")
            time.sleep(1) 
            st.rerun()

with tab3:
    st.subheader("Process & Add Multiple Trades via CSV(s)")
    batch_strategy_name = st.selectbox("Assign a Strategy Name for this batch:", options=STRATEGIES)
    uploaded_files = st.file_uploader("Upload Bulk Export CSV(s)", type=["csv"], accept_multiple_files=True)
    
    if uploaded_files:
        all_dfs = []
        for file in uploaded_files:
            try:
                temp_df = pd.read_csv(file)
                if not temp_df.empty: all_dfs.append(temp_df)
            except pd.errors.EmptyDataError: pass
            except Exception as e: st.error(f"Error reading file '{file.name}': {e}")
            
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
                    display_debug_logs()

    if not st.session_state.temp_bulk_trades.empty:
        st.markdown("### ✅ Review Batch Calculations")
        edited_bulk = st.data_editor(st.session_state.temp_bulk_trades, use_container_width=True, num_rows="dynamic")
        
        if st.button("Step 2: Confirm & Add Batch to Master Ledger", type="primary"):
            append_to_ledger(edited_bulk)
            check_and_send_alerts(edited_bulk)
            st.session_state.temp_bulk_trades = pd.DataFrame() 
            st.success("Batch successfully added to Master Ledger!")
            time.sleep(1)
            st.rerun()

with tab4:
    st.subheader("Portfolio Performance Summary")
    if st.session_state.master_ledger.empty:
        st.info("Your Master Ledger is empty.")
    else:
        valid_res = st.session_state.master_ledger[~st.session_state.master_ledger['Status'].astype(str).str.contains("Error", na=False)]
        
        if not valid_res.empty:
            total_booked_pnl = valid_res.loc[valid_res["Status"] != "Live", "PnL (1 qty)"].sum()
            total_mtm_pnl = valid_res.loc[valid_res["Status"] == "Live", "PnL (1 qty)"].sum()
            total_trades = len(valid_res)
            wins = len(valid_res[valid_res["Status"] == "Target Hit"])
            losses = len(valid_res[valid_res["Status"] == "SL Hit"])
            winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Booked PnL", f"₹{total_booked_pnl:.2f}")
            c2.metric("Total MTM PnL", f"₹{total_mtm_pnl:.2f}")
            c3.metric("Overall Winrate", f"{winrate:.1f}%")
            c4.metric("Total Trades", total_trades)
                
            st.markdown("---")
            
            st.markdown("### 📊 Liquid vs. Others Category Summary")
            if 'Category' in valid_res.columns:
                cat_summary_df = generate_summary(valid_res, 'Category')
                st.dataframe(cat_summary_df, use_container_width=True)
            
            st.markdown("### 🏆 Strategy-wise Summary")
            strategy_col = 'Strategy Name' if 'Strategy Name' in valid_res.columns else 'strategy name'
            if strategy_col in valid_res.columns:
                strat_summary_df = generate_summary(valid_res, strategy_col)
                st.dataframe(strat_summary_df, use_container_width=True)
                
            st.markdown("### 🏢 Stock-wise Summary")
            stock_col = 'Stock Name' if 'Stock Name' in valid_res.columns else 'stock name'
            stock_summary_df = generate_summary(valid_res, stock_col)
            st.dataframe(stock_summary_df, use_container_width=True)
