import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime
import time

# --- Page Config ---
st.set_page_config(page_title="Upstox Trade Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Analyze Stop Loss and Target hits using multi-day 1-minute historical & intraday data.")

# --- Sidebar: API Configuration ---
with st.sidebar:
    st.header("🔑 API Setup")
    default_token = st.secrets.get("UPSTOX_TOKEN", "")
    api_token = st.text_input("Enter Upstox Analytics Token", value=default_token, type="password")
    st.markdown("---")
    st.markdown("**Parameters**")
    sl_pct = st.number_input("Stop Loss %", value=2.0, step=0.5)
    tgt_pct = st.number_input("Target %", value=5.0, step=0.5)
    st.markdown("---")
    debug_mode = st.checkbox("🐞 Enable Debug Mode", value=False, help="Prints raw API responses to the screen for troubleshooting.")

# --- Globals for Debugging ---
if "debug_logs" not in st.session_state:
    st.session_state.debug_logs = []

def log_debug(message):
    if debug_mode:
        st.session_state.debug_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

# --- Robust API Handler ---
def robust_api_get(url, headers, max_retries=4):
    for attempt in range(max_retries):
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            return res
        elif res.status_code == 429:
            time.sleep(2 ** attempt) 
        else:
            time.sleep(1)
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
    else:
        log_debug(f"Failed to fetch key for {symbol}. Code: {res.status_code}, Response: {res.text}")
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
    else:
        log_debug(f"Historical API Error for {instrument_key}: Code {hist_res.status_code} - {hist_res.text}")

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
    else:
        log_debug(f"Intraday API Error for {instrument_key}: Code {intra_res.status_code} - {intra_res.text}")

    all_candles.sort(key=lambda x: x[0])
    return all_candles, hist_res.status_code

def calculate_trade(symbol, trade_date, entry_time, token, sl_p, tgt_p):
    instrument_key = get_instrument_key(symbol, token)
    if not instrument_key: 
        return {"Instrument Key": None, "Status": "Error: Symbol Not Found"}

    try:
        from_date_str = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    except Exception:
        return {"Instrument Key": instrument_key, "Status": "Error: Invalid Date"}

    candles, hist_status = fetch_all_candles(instrument_key, from_date_str, token)
    if not candles:
        log_debug(f"[{symbol}] 0 candles for date {from_date_str}.")
        return {"Instrument Key": instrument_key, "Status": "Error: No Market Data"}

    e_time_match = str(entry_time)[:5]
    entry_price = None
    entry_idx = -1
    for i, c in enumerate(candles):
        if c[0].split('T')[0] == from_date_str and c[0].split('T')[1][:5] == e_time_match:
            entry_price, entry_idx = c[1], i
            break

    if entry_price is None: 
        log_debug(f"[{symbol}] Entry time {e_time_match} missing.")
        return {"Instrument Key": instrument_key, "Status": f"Error: Time {e_time_match} missing"}

    sl_price = entry_price * (1 - (sl_p / 100))
    tgt_price = entry_price * (1 + (tgt_p / 100))
    exit_price, exit_time = None, None
    status = "Live"
    bars_in_trade = 0

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
        "Instrument Key": instrument_key,
        "Entry Price": round(entry_price, 2),
        "Exit Price": round(exit_price, 2),
        "Exit Time": exit_time,
        "Bars in Trade": bars_in_trade,
        "Status": status,
        "PnL (1 qty)": round(pnl, 2),
        "PnL %": round(pnl_pct, 2)
    }

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
        
        # 1-Hour Bar Conversion
        avg_bars_1m = group["Bars in Trade"].mean()
        avg_bars_1h = avg_bars_1m / 60 if pd.notna(avg_bars_1m) else 0
        
        # MTM vs Booked PnL
        booked_mask = group["Status"] != "Live"
        mtm_mask = group["Status"] == "Live"
        booked_pnl = group.loc[booked_mask, "PnL (1 qty)"].sum()
        mtm_pnl = group.loc[mtm_mask, "PnL (1 qty)"].sum()
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
            "Wins": wins,
            "Losses": losses,
            "Live Trades": live,
            "Win Rate %": round(winrate, 1),
            "Avg 1H Bars": round(avg_bars_1h, 1),
            "Booked PnL": round(booked_pnl, 2),
            "MTM PnL": round(mtm_pnl, 2),
            "Total PnL": round(tot_pnl, 2),
            "Max DD Amount": round(max_dd, 2),
            "Max DD %": round(max_dd_pct, 2)
        })
    return pd.DataFrame(stats)

# --- Main App ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Table Input", "📝 Single Trade", "📁 Bulk CSV", "📈 Summary Stats"])

if "final_results" not in st.session_state:
    st.session_state.final_results = pd.DataFrame()

def display_debug_logs():
    if debug_mode and st.session_state.debug_logs:
        with st.expander("🛠️ Debug Logs (Click to expand)", expanded=True):
            for log in st.session_state.debug_logs:
                st.text(log)

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
            display_debug_logs()

with tab2:
    st.subheader("Evaluate a Single Trade")
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1: s_strategy = st.text_input("Strategy Name", value="Breakout")
    with col2: s_symbol = st.text_input("NSE Stock Name", value="RELIANCE")
    with col3: s_date = st.date_input("Trade Date")
    with col4: s_time = st.time_input("Entry Time", value=pd.to_datetime("09:15").time())
    with col5:
        st.markdown("<br>", unsafe_allow_html=True)
        calc_btn = st.button("Calculate Single", type="primary", use_container_width=True, key="btn_single")

    if calc_btn:
        st.session_state.debug_logs = []
        if not api_token: st.warning("Please enter your API Token.")
        else:
            with st.spinner("Calculating..."):
                result = {"Strategy Name": s_strategy, "Stock Name": s_symbol, "Date": s_date}
                result.update(calculate_trade(s_symbol, s_date, s_time, api_token, sl_pct, tgt_pct))
                st.write(result)
            display_debug_logs()

with tab3:
    st.subheader("Process Multiple Trades via CSV")
    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        if st.button("Process Uploaded CSV", type="primary", key="btn_csv"):
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
                display_debug_logs()

with tab4:
    st.subheader("Portfolio Performance Summary")
    if st.session_state.final_results.empty:
        st.info("Process some trades in the Table or CSV tabs first to generate a summary.")
    else:
        # Generate overall portfolio values
        valid_res = st.session_state.final_results[~st.session_state.final_results['Status'].astype(str).str.contains("Error", na=False)]
        
        if not valid_res.empty:
            total_booked_pnl = valid_res.loc[valid_res["Status"] != "Live", "PnL (1 qty)"].sum()
            total_mtm_pnl = valid_res.loc[valid_res["Status"] == "Live", "PnL (1 qty)"].sum()
            total_trades = len(valid_res)
            wins = len(valid_res[valid_res["Status"] == "Target Hit"])
            losses = len(valid_res[valid_res["Status"] == "SL Hit"])
            winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Booked PnL", f"₹{total_booked_pnl:.2f}", help="Sum of PnL from closed trades (SL/Target Hits)")
            c2.metric("Total MTM PnL", f"₹{total_mtm_pnl:.2f}", help="Floating PnL from trades still 'Live'")
            c3.metric("Overall Winrate", f"{winrate:.1f}%")
            c4.metric("Total Trades", total_trades)
            st.markdown("---")
            
            # 1. Strategy Summary
            st.markdown("### 🏆 Strategy-wise Summary")
            strategy_col_name = 'Strategy Name' if 'Strategy Name' in st.session_state.final_results.columns else 'strategy name'
            if strategy_col_name in st.session_state.final_results.columns:
                strat_summary_df = generate_summary(st.session_state.final_results, strategy_col_name)
                st.dataframe(strat_summary_df, use_container_width=True)
            else:
                st.warning("No 'Strategy Name' column found in your data.")
                
            st.markdown("<br>", unsafe_allow_html=True)
            
            # 2. Stock Summary
            st.markdown("### 🏢 Stock-wise Summary")
            stock_col_name = 'Stock Name' if 'Stock Name' in st.session_state.final_results.columns else 'stock name'
            stock_summary_df = generate_summary(st.session_state.final_results, stock_col_name)
            st.dataframe(stock_summary_df, use_container_width=True)
            
        else:
            st.warning("Could not generate summary. Check if all data processed successfully.")
