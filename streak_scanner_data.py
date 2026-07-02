import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# --- Page Config ---
st.set_page_config(page_title="Upstox Trade Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Analyze Stop Loss and Target hits using multi-day 1-minute historical & intraday data.")

# --- Sidebar: Configuration ---
with st.sidebar:
    st.header("🔑 API & Alerts Setup")
    
    # Upstox API
    default_token = st.secrets.get("UPSTOX_TOKEN", "")
    api_token = st.text_input("Enter Upstox Analytics Token", value=default_token, type="password")
    
    st.markdown("---")
    # Email Alerts Configuration
    st.subheader("📧 Email Alerts")
    enable_emails = st.checkbox("Enable Exit Alerts", value=True)
    alert_email = st.text_input("Alert Email Address", value="9035490861r@gmail.com")
    # Using the provided app password as default
    email_pass = st.text_input("Gmail App Password", value="oeci llhn noig moew", type="password")
    
    st.markdown("---")
    st.markdown("**Trade Parameters**")
    sl_pct = st.number_input("Stop Loss %", value=2.0, step=0.5)
    tgt_pct = st.number_input("Target %", value=5.0, step=0.5)
    st.markdown("---")
    debug_mode = st.checkbox("🐞 Enable Debug Mode", value=False)

# --- Globals for Debugging ---
if "debug_logs" not in st.session_state: st.session_state.debug_logs = []
def log_debug(message):
    if debug_mode: st.session_state.debug_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

# --- Load Liquid Stocks List ---
@st.cache_data(show_spinner=False)
def load_liquid_stocks():
    """Loads the liquid stocks list from the local CSV if it exists."""
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

# --- Email Alert Engine ---
def send_exit_alert(exited_trades_df, recipient, sender, password):
    """Sends an HTML email summary of trades that exited today."""
    if exited_trades_df.empty: return False
    
    subject = f"🚨 Trade Exit Alert: {len(exited_trades_df)} Positions Closed"
    
    # Create HTML table for the email
    html_table = exited_trades_df[['Stock Name', 'Category', 'Status', 'Entry Price', 'Exit Price', 'PnL %']].to_html(index=False, border=1)
    
    body = f"""
    <html>
      <body>
        <h2>Trade Exit Alerts</h2>
        <p>The following live trades have met their exit criteria (SL or Target) today:</p>
        {html_table}
        <br>
        <p><i>Automated via Streamlit Swing Trade Analyzer</i></p>
      </body>
    </html>
    """
    
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
    """Filters for trades that exited TODAY and triggers the email."""
    if not enable_emails or final_df.empty: return
    
    today_str = datetime.today().strftime('%Y-%m-%d')
    # Look for trades that are NOT live and exited today
    exits_today = final_df[
        (final_df['Status'].isin(["SL Hit", "Target Hit"])) & 
        (final_df['Exit Time'].astype(str).str.contains(today_str, na=False))
    ]
    
    if not exits_today.empty:
        success = send_exit_alert(exits_today, alert_email, alert_email, email_pass)
        if success:
            st.toast("✅ Email alert sent for closed trades!")
        else:
            st.toast("⚠️ Failed to send email alert. Check debug logs.", icon="⚠️")

# --- Robust API Handler ---
def robust_api_get(url, headers, max_retries=4):
    for attempt in range(max_retries):
        res = requests.get(url, headers=headers)
        if res.status_code == 200: return res
        elif res.status_code == 429: time.sleep(2 ** attempt) 
        else: time.sleep(1)
    return res 

# --- API Helper Functions ---
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

def calculate_trade(symbol, trade_date, entry_time, token, sl_p, tgt_p):
    symbol_clean = str(symbol).strip().upper()
    category = "Liquid" if symbol_clean in liquid_symbols else "Others"
    
    instrument_key = get_instrument_key(symbol_clean, token)
    if not instrument_key: 
        return {"Category": category, "Instrument Key": None, "Status": "Error: Symbol Not Found"}

    try:
        from_date_str = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    except Exception:
        return {"Category": category, "Instrument Key": instrument_key, "Status": "Error: Invalid Date"}

    candles, hist_status = fetch_all_candles(instrument_key, from_date_str, token)
    if not candles:
        return {"Category": category, "Instrument Key": instrument_key, "Status": "Error: No Market Data"}

    e_time_match = str(entry_time)[:5]
    entry_price, entry_idx = None, -1
    
    for i, c in enumerate(candles):
        if c[0].split('T')[0] == from_date_str and c[0].split('T')[1][:5] == e_time_match:
            entry_price, entry_idx = c[1], i
            break

    if entry_price is None: 
        return {"Category": category, "Instrument Key": instrument_key, "Status": f"Error: Time missing"}

    sl_price = entry_price * (1 - (sl_p / 100))
    tgt_price = entry_price * (1 + (tgt_p / 100))
    exit_price, exit_time, status, bars_in_trade = None, None, "Live", 0

    for i in range(entry_idx, len(candles)):
        bars_in_trade += 1
        c = candles[i]
        c_close, c_time_curr = c[4], c[0].split('+')[0]
        
        if c_close <= sl_price:
            exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"
            break
        elif c_close >= tgt_price:
            exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
            break

    if exit_price is None:
        exit_price, exit_time = candles[-1][4], candles[-1][0].split('+')[0]

    pnl = exit_price - entry_price
    pnl_pct = (pnl / entry_price) * 100

    return {
        "Category": category,
        "Instrument Key": instrument_key,
        "Entry Price": round(entry_price, 2),
        "Exit Price": round(exit_price, 2),
        "Exit Time": exit_time,
        "Bars in Trade": bars_in_trade,
        "Status": status,
        "PnL (1 qty)": round(pnl, 2),
        "PnL %": round(pnl_pct, 2)
    }

def parse_uploaded_csv(df):
    """Automatically formats known export files into the required schema."""
    if 'seg_sym' in df.columns and 'time' in df.columns:
        df['Stock Name'] = df['seg_sym'].str.replace('NSE:', '', regex=False)
        df['Date'] = pd.to_datetime(df['time']).dt.strftime('%Y-%m-%d')
        df['Entry Time'] = pd.to_datetime(df['time']).dt.strftime('%H:%M')
        if 'Strategy Name' not in df.columns:
            df['Strategy Name'] = "Bulk Export"
    return df

def generate_summary(df, group_column):
    stats = []
    if group_column not in df.columns or 'Status' not in df.columns: return pd.DataFrame()
    valid_df = df[~df['Status'].astype(str).str.contains("Error", na=False)]
    
    for name, group in valid_df.groupby(group_column):
        group = group.sort_values("Date")
        wins = len(group[group["Status"] == "Target Hit"])
        losses = len(group[group["Status"] == "SL Hit"])
        live = len(group[group["Status"] == "Live"])
        completed = wins + losses
        winrate = (wins / completed * 100) if completed > 0 else 0
        
        avg_bars_1m = group["Bars in Trade"].mean()
        avg_bars_1h = avg_bars_1m / 60 if pd.notna(avg_bars_1m) else 0
        
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
            group_column: name,
            "Total Trades": len(group),
            "Wins": wins, "Losses": losses, "Live Trades": live,
            "Win Rate %": round(winrate, 1), "Avg 1H Bars": round(avg_bars_1h, 1),
            "Booked PnL": round(booked_pnl, 2), "MTM PnL": round(mtm_pnl, 2),
            "Total PnL": round(tot_pnl, 2), "Max DD Amount": round(max_dd, 2), "Max DD %": round(max_dd_pct, 2)
        })
    return pd.DataFrame(stats)

# --- Main App ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Table Input", "📝 Single Trade", "📁 Bulk CSV", "📈 Summary Stats"])

if "final_results" not in st.session_state:
    st.session_state.final_results = pd.DataFrame()

def display_debug_logs():
    if debug_mode and st.session_state.debug_logs:
        with st.expander("🛠️ Debug Logs (Click to expand)", expanded=True):
            for log in st.session_state.debug_logs: st.text(log)

# Tab 1: Interactive Table
with tab1:
    st.subheader("Build Your Trade List")
    if "df_template" not in st.session_state:
        st.session_state.df_template = pd.DataFrame({"Strategy Name": ["Breakout"], "Stock Name": ["RELIANCE"], "Date": [datetime.today().strftime('%Y-%m-%d')], "Entry Time": ["09:15"]})

    edited_df = st.data_editor(st.session_state.df_template, num_rows="dynamic", use_container_width=True, hide_index=True)

    if st.button("Process Table Data", type="primary", key="btn_table"):
        st.session_state.debug_logs = []
        if not api_token: st.warning("Please enter your API Token.")
        elif edited_df.empty: st.warning("Please add at least one row.")
        else:
            results_list = []
            progress_bar = st.progress(0)
            st.cache_data.clear() 
            
            for i, row in edited_df.iterrows():
                time.sleep(0.5) 
                res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), row.get('Entry Time', ''), api_token, sl_pct, tgt_pct)
                combined = row.to_dict()
                combined.update(res)
                results_list.append(combined)
                progress_bar.progress((i + 1) / len(edited_df))
                
            st.session_state.final_results = pd.DataFrame(results_list)
            st.success("Calculations Complete!")
            st.dataframe(st.session_state.final_results, use_container_width=True)
            check_and_send_alerts(st.session_state.final_results)
            display_debug_logs()

# Tab 2: Single Trade
with tab2:
    st.subheader("Evaluate a Single Trade")
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1: s_strategy = st.text_input("Strategy Name", value="Breakout")
    with col2: s_symbol = st.text_input("NSE Stock Name", value="RELIANCE")
    with col3: s_date = st.date_input("Trade Date")
    with col4: s_time = st.time_input("Entry Time", value=pd.to_datetime("09:15").time())
    with col5:
        st.markdown("<br>", unsafe_allow_html=True)
        calc_btn = st.button("Calculate Single", type="primary", use_container_width=True)

    if calc_btn:
        st.session_state.debug_logs = []
        if not api_token: st.warning("Please enter your API Token.")
        else:
            with st.spinner("Calculating..."):
                result = {"Strategy Name": s_strategy, "Stock Name": s_symbol, "Date": s_date, "Entry Time": str(s_time)}
                result.update(calculate_trade(s_symbol, s_date, s_time, api_token, sl_pct, tgt_pct))
                single_df = pd.DataFrame([result])
                st.write(result)
                check_and_send_alerts(single_df)
            display_debug_logs()

# Tab 3: CSV Upload
with tab3:
    st.subheader("Process Multiple Trades via CSV")
    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        df = parse_uploaded_csv(df) # Automatically handle the streak export format
        
        st.write("Preview of parsed data:", df.head(3))
        
        if st.button("Process Uploaded CSV", type="primary"):
            st.session_state.debug_logs = []
            if not api_token: st.warning("Please enter your API Token.")
            else:
                results_list = []
                progress_bar = st.progress(0)
                st.cache_data.clear() 
                for i, row in df.iterrows():
                    time.sleep(0.5)
                    res = calculate_trade(row.get('Stock Name', row.get('stock name', '')), row.get('Date', row.get('date', '')), row.get('Entry Time', row.get('entry time', '')), api_token, sl_pct, tgt_pct)
                    combined = row.to_dict()
                    combined.update(res)
                    results_list.append(combined)
                    progress_bar.progress((i + 1) / len(df))
                
                st.session_state.final_results = pd.DataFrame(results_list)
                st.success("Calculations Complete!")
                st.dataframe(st.session_state.final_results)
                check_and_send_alerts(st.session_state.final_results)
                display_debug_logs()

# Tab 4: Summary Stats
with tab4:
    st.subheader("Portfolio Performance Summary")
    if st.session_state.final_results.empty:
        st.info("Process some trades in the Table or CSV tabs first to generate a summary.")
    else:
        valid_res = st.session_state.final_results[~st.session_state.final_results['Status'].astype(str).str.contains("Error", na=False)]
        
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
