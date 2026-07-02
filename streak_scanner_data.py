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
    if not token: return None
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
        pass
    return None

def fetch_1m_candles(instrument_key, date_str, token):
    encoded_key = urllib.parse.quote(instrument_key)
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/1minute/{date_str}/{date_str}"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        data = res.json()
        if 'data' in data and 'candles' in data['data']:
            candles = data['data']['candles']
            candles.reverse() 
            return candles
    return []

def calculate_trade(symbol, trade_date, entry_time, token, sl_p, tgt_p):
    instrument_key = get_instrument_key(symbol, token)
    if not instrument_key: return {"Status": "Error: Symbol Not Found"}

    # Handle various date formats from UI/CSV
    try:
        date_str = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    except:
        return {"Status": "Error: Invalid Date Format"}

    candles = fetch_1m_candles(instrument_key, date_str, token)
    if not candles: return {"Status": "Error: No Candle Data"}

    # Extract HH:MM
    e_time_match = str(entry_time)[:5]

    entry_price = None
    entry_idx = -1
    for i, c in enumerate(candles):
        if c[0].split('T')[1][:5] == e_time_match:
            entry_price, entry_idx = c[1], i
            break

    if entry_price is None: return {"Status": f"Error: Time {e_time_match} not found"}

    sl_price = entry_price * (1 - (sl_p / 100))
    tgt_price = entry_price * (1 + (tgt_p / 100))
    exit_price, exit_time, status = None, None, "Pending"

    for i in range(entry_idx, len(candles)):
        c = candles[i]
        c_close, c_time_curr = c[4], c[0].split('T')[1][:8]
        if c_close <= sl_price:
            exit_price, exit_time, status = c_close, c_time_curr, "SL Hit"
            break
        elif c_close >= tgt_price:
            exit_price, exit_time, status = c_close, c_time_curr, "Target Hit"
            break

    if exit_price is None:
        exit_price, exit_time, status = candles[-1][4], candles[-1][0].split('T')[1][:8], "EOD Exit"

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
tab1, tab2, tab3 = st.tabs(["📊 Interactive Table Input", "📝 Single Trade", "📁 Bulk CSV Upload"])

# TAB 1: INTERACTIVE DATA EDITOR (Behaves like a live blank CSV)
with tab1:
    st.subheader("Build Your Trade List")
    st.markdown("Click inside the table below to add, edit, or delete rows. Once ready, click 'Process Table'.")
    
    # Initialize an empty dataframe layout
    if "df_template" not in st.session_state:
        st.session_state.df_template = pd.DataFrame({
            "Strategy Name": ["Breakout"],
            "Stock Name": ["RELIANCE"],
            "Date": [datetime.today().strftime('%Y-%m-%d')],
            "Entry Time": ["09:15"]
        })

    # The dynamic editor
    edited_df = st.data_editor(
        st.session_state.df_template, 
        num_rows="dynamic", 
        use_container_width=True,
        hide_index=True
    )

    if st.button("Process Table Data", type="primary"):
        if not api_token:
            st.warning("Please enter your API Token in the sidebar first.")
        elif edited_df.empty:
            st.warning("Please add at least one row of data.")
        else:
            results_list = []
            progress_bar = st.progress(0)
            
            for i, row in edited_df.iterrows():
                res = calculate_trade(
                    row.get('Stock Name', ''), 
                    row.get('Date', ''), 
                    row.get('Entry Time', ''), 
                    api_token, sl_pct, tgt_pct
                )
                
                combined_row = row.to_dict()
                combined_row.update(res)
                results_list.append(combined_row)
                progress_bar.progress((i + 1) / len(edited_df))
            
            final_df = pd.DataFrame(results_list)
            st.success("Calculations Complete!")
            st.dataframe(final_df, use_container_width=True)
            
            # Allow download of the final calculated data
            csv_data = final_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="Download Results as CSV",
                data=csv_data,
                file_name="interactive_calculated_trades.csv",
                mime="text/csv",
            )

# TAB 2: SINGLE TRADE
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
        if not api_token: st.warning("Please enter your API Token in the sidebar first.")
        else:
            with st.spinner("Calculating..."):
                result = {"Strategy Name": s_strategy}
                result.update(calculate_trade(s_symbol, s_date, s_time, api_token, sl_pct, tgt_pct))
                st.write(result)

# TAB 3: CSV UPLOAD
with tab3:
    st.subheader("Process Multiple Trades via CSV")
    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        st.dataframe(df.head())
        
        if st.button("Process Uploaded CSV", type="primary"):
            if not api_token: st.warning("Please enter your API Token in the sidebar first.")
            else:
                results_list = []
                progress_bar = st.progress(0)
                for i, row in df.iterrows():
                    res = calculate_trade(
                        row.get('stock name', ''), row.get('date', ''), 
                        row.get('entry time', ''), api_token, sl_pct, tgt_pct
                    )
                    combined = row.to_dict()
                    combined.update(res)
                    results_list.append(combined)
                    progress_bar.progress((i + 1) / len(df))
                
                final_df = pd.DataFrame(results_list)
                st.dataframe(final_df)
                st.download_button("Download Updated CSV", data=final_df.to_csv(index=False).encode('utf-8'), file_name="calculated_trades.csv", mime="text/csv")
