%%writefile streak_scanner_data.py
import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime

# --- Page Config ---
st.set_page_config(page_title="Upstox Trade Analyzer", page_icon="📈", layout="wide")
st.title("📈 Upstox Intraday Trade Analyzer")
st.markdown("Analyze Stop Loss (2%) and Target (5%) hits using 1-minute historical data.")

# --- Sidebar: API Configuration ---
with st.sidebar:
    st.header("🔑 API Setup")
    api_token = st.text_input("Enter Upstox Analytics Token", type="password", help="Your Bearer token for Upstox API v2")
    st.markdown("---")
    st.markdown("**Parameters**")
    sl_pct = st.number_input("Stop Loss %", value=2.0, step=0.5)
    tgt_pct = st.number_input("Target %", value=5.0, step=0.5)

# --- API Helper Functions ---
@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_key(symbol, token):
    """Maps a basic NSE symbol to an Upstox instrument key."""
    if not token:
        return None
    
    symbol_clean = str(symbol).strip().upper()
    query = urllib.parse.quote(symbol_clean)
    url = f"https://api.upstox.com/v2/instruments/search?query={query}&exchanges=NSE&segments=EQ"
    
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            if 'data' in data and len(data['data']) > 0:
                for inst in data['data']:
                    if inst.get('trading_symbol', '').upper() == symbol_clean:
                        return inst['instrument_key']
                return data['data'][0]['instrument_key']
    except Exception as e:
        st.error(f"Error fetching key for {symbol}: {e}")
    return None

def fetch_1m_candles(instrument_key, date_str, token):
    """Fetches 1-minute historical candles for a specific date."""
    encoded_key = urllib.parse.quote(instrument_key)
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/1minute/{date_str}/{date_str}"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        data = res.json()
        if 'data' in data and 'candles' in data['data']:
            candles = data['data']['candles']
            candles.reverse() # Reverse to chronological order (oldest to newest)
            return candles
    else:
        st.error(f"Data fetch failed for {instrument_key}: {res.text}")
    return []

def calculate_trade(symbol, trade_date, entry_time, token, sl_p, tgt_p):
    """Calculates PnL based on exact entry minute and subsequent 1m closes."""
    instrument_key = get_instrument_key(symbol, token)
    if not instrument_key:
        return {"Status": "Error: Symbol Not Found"}

    # Format date to YYYY-MM-DD
    if isinstance(trade_date, datetime):
        date_str = trade_date.strftime("%Y-%m-%d")
    else:
        date_str = pd.to_datetime(trade_date).strftime("%Y-%m-%d")

    candles = fetch_1m_candles(instrument_key, date_str, token)
    if not candles:
        return {"Status": "Error: No Candle Data"}

    # Format entry time to match API (HH:MM)
    if isinstance(entry_time, str):
        e_time_match = entry_time[:5]
    else:
        e_time_match = entry_time.strftime("%H:%M")

    # 1. Find Entry
    entry_price = None
    entry_idx = -1
    for i, c in enumerate(candles):
        c_time = c[0].split('T')[1][:5]
        if c_time == e_time_match:
            entry_price = c[1] # Open price of the entry candle
            entry_idx = i
            break

    if entry_price is None:
        return {"Status": f"Error: Entry time {e_time_match} not found"}

    # 2. Track SL and Target
    sl_price = entry_price * (1 - (sl_p / 100))
    tgt_price = entry_price * (1 + (tgt_p / 100))
    
    exit_price = None
    exit_time = None
    status = "Pending"

    for i in range(entry_idx, len(candles)):
        c = candles[i]
        c_close = c[4]
        c_time_curr = c[0].split('T')[1][:8]

        if c_close <= sl_price:
            exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"
            break
        elif c_close >= tgt_price:
            exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
            break

    # EOD Exit
    if exit_price is None:
        last_c = candles[-1]
        exit_price, exit_time, status = last_c[4], last_c[0].split('T')[1][:8], "EOD Exit"

    pnl = exit_price - entry_price
    pnl_pct = (pnl / entry_price) * 100

    return {
        "Instrument Key": instrument_key,
        "Entry Price": round(entry_price, 2),
        "Exit Price": round(exit_price, 2),
        "Exit Time": exit_time,
        "Status": status,
        "PnL (1 qty)": round(pnl, 2),
        "PnL %": round(pnl_pct, 2)
    }

# --- Main App Tabs ---
tab1, tab2 = st.tabs(["📝 Single Trade Manual Input", "📁 Bulk CSV Upload"])

# TAB 1: Single Trade
with tab1:
    st.subheader("Evaluate a Single Trade")
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        s_strategy = st.text_input("Strategy Name", value="Breakout")
    with col2:
        s_symbol = st.text_input("NSE Stock Name", value="RELIANCE")
    with col3:
        s_date = st.date_input("Trade Date")
    with col4:
        s_time = st.time_input("Entry Time", value=pd.to_datetime("09:15").time())
    with col5:
        st.markdown("<br>", unsafe_allow_html=True)
        calc_btn = st.button("Calculate", type="primary", use_container_width=True)

    if calc_btn:
        if not api_token:
            st.warning("Please enter your API Token in the sidebar first.")
        else:
            with st.spinner("Fetching data and calculating..."):
                result = calculate_trade(s_symbol, s_date, s_time, api_token, sl_pct, tgt_pct)
                # Insert the strategy name at the beginning of the result dictionary
                final_result = {"Strategy": s_strategy}
                final_result.update(result)
                st.write(final_result)

# TAB 2: CSV Upload
with tab2:
    st.subheader("Process Multiple Trades via CSV")
    st.markdown("Your CSV must contain columns: `stock name`, `date` (YYYY-MM-DD), `entry time` (HH:MM), `tf`, `strategy name`")
    
    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])
    
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        st.write("Preview of Uploaded Data:", df.head())
        
        if st.button("Process All Rows", type="primary"):
            if not api_token:
                st.warning("Please enter your API Token in the sidebar first.")
            else:
                results_list = []
                progress_bar = st.progress(0)
                
                for i, row in df.iterrows():
                    symbol = row.get('stock name', '')
                    trade_date = row.get('date', '')
                    entry_time = str(row.get('entry time', ''))
                    
                    res = calculate_trade(symbol, trade_date, entry_time, api_token, sl_pct, tgt_pct)
                    
                    # Merge original CSV columns (including 'strategy name') with the new calculations
                    combined_row = row.to_dict()
                    combined_row.update(res)
                    results_list.append(combined_row)
                    
                    progress_bar.progress((i + 1) / len(df))
                
                final_df = pd.DataFrame(results_list)
                
                # Reorder columns to make Strategy Name prominent if it exists in the uploaded CSV
                cols = final_df.columns.tolist()
                if 'strategy name' in cols:
                    cols.insert(0, cols.pop(cols.index('strategy name')))
                    final_df = final_df[cols]

                st.success("Calculations Complete!")
                st.dataframe(final_df)
                
                csv_data = final_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="Download Updated CSV",
                    data=csv_data,
                    file_name="calculated_trades_with_strategies.csv",
                    mime="text/csv",
                )