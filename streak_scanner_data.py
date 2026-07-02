import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime
import time

# --- Page Config ---
st.set_page_config(page_title="Upstox Trade Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")
st.markdown("Analyze Stop Loss and Target hits using multi-day 1-minute historical data.")

# --- Sidebar: API Configuration ---
with st.sidebar:
    st.header("🔑 API Setup")
    default_token = st.secrets.get("UPSTOX_TOKEN", "")
    api_token = st.text_input("Enter Upstox Analytics Token", value=default_token, type="password")
    st.markdown("---")
    st.markdown("**Parameters**")
    sl_pct = st.number_input("Stop Loss %", value=2.0, step=0.5)
    tgt_pct = st.number_input("Target %", value=5.0, step=0.5)

# --- Robust API Handler ---
def robust_api_get(url, headers, max_retries=4):
    """Handles API requests with exponential backoff for rate limits (429)."""
    for attempt in range(max_retries):
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            return res
        elif res.status_code == 429:
            # Exponential backoff: sleeps 1s, 2s, 4s, 8s
            time.sleep(2 ** attempt) 
        else:
            # For other errors (like 502 Bad Gateway), wait 1 second and retry
            time.sleep(1)
    return res # Return the last response if all retries fail

# --- API Helper Functions ---
@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_key(symbol, token):
    if not token: return None
    symbol_clean = str(symbol).strip().upper()
    query = urllib.parse.quote(symbol_clean)
    url = f"https://api.upstox.com/v2/instruments/search?query={query}&exchanges=NSE&segments=EQ"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    
    try:
        res = robust_api_get(url, headers)
        if res.status_code == 200:
            data = res.json()
            if 'data' in data and len(data['data']) > 0:
                for inst in data['data']:
                    if inst.get('trading_symbol', '').upper() == symbol_clean:
                        return inst['instrument_key']
                return data['data'][0]['instrument_key']
    except Exception:
        pass
    return None

def fetch_1m_candles_multiday(instrument_key, from_date_str, token):
    encoded_key = urllib.parse.quote(instrument_key)
    to_date_str = datetime.today().strftime('%Y-%m-%d')
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/1minute/{to_date_str}/{from_date_str}"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    
    res = robust_api_get(url, headers)
    
    if res.status_code == 200:
        data = res.json()
        if 'data' in data and 'candles' in data['data'] and data['data']['candles']:
            candles = data['data']['candles']
            candles.reverse() 
            return candles, 200
    return [], res.status_code

def calculate_trade(symbol, trade_date, entry_time, token, sl_p, tgt_p):
    instrument_key = get_instrument_key(symbol, token)
    if not instrument_key: return {"Instrument Key": None, "Status": "Error: Symbol Not Found"}

    try:
        from_date_str = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    except Exception:
        return {"Instrument Key": instrument_key, "Status": "Error: Invalid Date Format"}

    candles, status_code = fetch_1m_candles_multiday(instrument_key, from_date_str, token)
    
    if not candles: 
        if status_code != 200:
            return {"Instrument Key": instrument_key, "Status": f"API Error Code: {status_code}"}
        else:
            return {"Instrument Key": instrument_key, "Status": "Error: No Market Data"}

    e_time_match = str(entry_time)[:5]
    entry_price = None
    entry_idx = -1
    
    for i, c in enumerate(candles):
        if c[0].split('T')[0] == from_date_str and c[0].split('T')[1][:5] == e_time_match:
            entry_price, entry_idx = c[1], i
            break

    if entry_price is None: return {"Instrument Key": instrument_key, "Status": f"Error: Time {e_time_match} not found on {from_date_str}"}

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

def generate_summary(df):
    stats = []
    if 'Stock Name' not in df.columns or 'Status' not in df.columns: return pd.DataFrame()
    
    valid_df = df[~df['Status'].astype(str).str.contains("Error", na=False)]
    
    for stock, group in valid_df.groupby("Stock Name"):
        group = group.sort_values("Date")
        wins = len(group[group["Status"] == "Target Hit"])
        losses = len(group[group["Status"] == "SL Hit"])
        completed = wins + losses
        winrate = (wins / completed * 100) if completed > 0 else 0
        avg_bars = group["Bars in Trade"].mean()
        tot_pnl = group["PnL (1 qty)"].sum()
        tot_pnl_pct = group["PnL %"].sum()
        
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
            "Stock Name": stock,
            "Trades": len(group),
            "Wins": wins,
            "Losses": losses,
            "Win Rate %": round(winrate, 1),
            "Avg Bars": round(avg_bars, 0) if pd.notna(avg_bars) else 0,
            "Total PnL": round(tot_pnl, 2),
            "Total PnL %": round(tot_pnl_pct, 2),
            "Max DD Amount": round(max_dd, 2),
            "Max DD %": round(max_dd_pct, 2)
        })
    return pd.DataFrame(stats)

# --- Main App Tabs ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Table Input", "📝 Single Trade", "📁 Bulk CSV", "📈 Summary Stats"])

if "final_results" not in st.session_state:
    st.session_state.final_results = pd.DataFrame()

with tab1:
    st.subheader("Build Your Trade List")
    if "df_template" not in st.session_state:
        st.session_state.df_template = pd.DataFrame({"Strategy Name": ["Breakout"], "Stock Name": ["RELIANCE"], "Date": [datetime.today().strftime('%Y-%m-%d')], "Entry Time": ["09:15"]})

    edited_df = st.data_editor(st.session_state.df_template, num_rows="dynamic", use_container_width=True, hide_index=True)

    if st.button("Process Table Data", type="primary"):
        if not api_token: st.warning("Please enter your API Token.")
        elif edited_df.empty: st.warning("Please add at least one row.")
        else:
            results_list = []
            progress_bar = st.progress(0)
            
            st.cache_data.clear() 
            
            for i, row in edited_df.iterrows():
                # Increased base delay slightly to smooth out batch processing
                time.sleep(0.5) 
                res = calculate_trade(row.get('Stock Name', ''), row.get('Date', ''), row.get('Entry Time', ''), api_token, sl_pct, tgt_pct)
                combined = row.to_dict()
                combined.update(res)
                results_list.append(combined)
                progress_bar.progress((i + 1) / len(edited_df))
                
            st.session_state.final_results = pd.DataFrame(results_list)
            st.success("Calculations Complete! Check the Summary Stats tab.")
            st.dataframe(st.session_state.final_results, use_container_width=True)

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
        if not api_token: st.warning("Please enter your API Token.")
        else:
            with st.spinner("Calculating..."):
                result = {"Strategy Name": s_strategy, "Stock Name": s_symbol, "Date": s_date}
                result.update(calculate_trade(s_symbol, s_date, s_time, api_token, sl_pct, tgt_pct))
                st.write(result)

with tab3:
    st.subheader("Process Multiple Trades via CSV")
    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        if st.button("Process Uploaded CSV", type="primary"):
            if not api_token: st.warning("Please enter your API Token.")
            else:
                results_list = []
                progress_bar = st.progress(0)
                st.cache_data.clear() 
                for i, row in df.iterrows():
                    # Increased base delay slightly
                    time.sleep(0.5)
                    res = calculate_trade(row.get('Stock Name', row.get('stock name', '')), row.get('Date', row.get('date', '')), row.get('Entry Time', row.get('entry time', '')), api_token, sl_pct, tgt_pct)
                    combined = row.to_dict()
                    combined.update(res)
                    results_list.append(combined)
                    progress_bar.progress((i + 1) / len(df))
                
                st.session_state.final_results = pd.DataFrame(results_list)
                st.success("Calculations Complete!")
                st.dataframe(st.session_state.final_results)

with tab4:
    st.subheader("Portfolio Performance Summary")
    if st.session_state.final_results.empty:
        st.info("Process some trades in the Table or CSV tabs first to generate a summary.")
    else:
        summary_df = generate_summary(st.session_state.final_results)
        if not summary_df.empty:
            total_pnl = summary_df['Total PnL'].sum()
            total_wins = summary_df['Wins'].sum()
            total_trades = summary_df['Trades'].sum()
            overall_winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0
            
            c1, c2, c3 = st.columns(3)
            c1.metric("Overall PnL", f"₹{total_pnl:.2f}")
            c2.metric("Overall Winrate", f"{overall_winrate:.1f}%")
            c3.metric("Total Trades Processed", total_trades)
            st.markdown("---")
            st.dataframe(summary_df, use_container_width=True)
        else:
            st.warning("Could not generate summary. Check if all data processed successfully.")
