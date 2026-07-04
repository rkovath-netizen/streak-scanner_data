import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime, timedelta
import time
import os

# --- Setup ---
st.set_page_config(page_title="Upstox Swing Analyzer", layout="wide")
st.title("📈 Upstox Swing Trade Analyzer")

STRATEGIES = [
    "b_ema-x_15mt", "b_ema_x_1hr", "b_rsi_x60_15mt", "b_rsi_x_1hr", 
    "b_vwap_x_15mt", "b_vwap_x_1hr", "b_st_x_15mt", "b_st_x_1hr",
    "s_ema-x_15mt", "s_ema_x_1hr", "s_rsi_x60_15mt", "s_rsi_x_1hr", 
    "s_vwap_x_15mt", "s_vwap_x_1hr", "s_st_x_15mt", "s_st_x_1hr"
]

if "master_ledger" not in st.session_state: st.session_state.master_ledger = pd.DataFrame()

# --- API Engine ---
def get_opt_details(symbol, strike, ce_pe, token):
    query = f"{symbol} {int(strike)} {ce_pe.upper()}"
    url = f"https://api.upstox.com/v2/instruments/search?query={urllib.parse.quote(query)}&exchanges=NFO&segments=OPT"
    res = requests.get(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    if res.status_code == 200 and res.json().get('data'):
        return res.json()['data'][0]
    return None

def get_price_at_time(key, date_str, time_str, token):
    url = f"https://api.upstox.com/v2/historical-candle/{urllib.parse.quote(key)}/1minute/{date_str}/{date_str}"
    res = requests.get(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    if res.status_code == 200 and res.json().get('data'):
        for c in res.json()['data']['candles']:
            if c[0][11:16] == time_str: return c[1]
    return None

# --- Updater Logic ---
def update_options_in_ledger(token):
    df = st.session_state.master_ledger.copy()
    for col in ['qty', 'Opt Entry', 'Opt Exit', 'Opt PnL', 'Opt PnL %']:
        if col not in df.columns: df[col] = None
    
    col_strike = next((c for c in df.columns if c.lower() == 'atm strike'), None)
    col_cepe = next((c for c in df.columns if c.lower() == 'ce/pe'), None)
    
    if not col_strike or not col_cepe:
        st.warning("Ensure 'atm strike' and 'CE/PE' columns exist.")
        return

    progress = st.progress(0)
    for i, row in df.iterrows():
        if pd.notna(row.get(col_strike)) and pd.notna(row.get(col_cepe)):
            details = get_opt_details(row['Stock Name'], row[col_strike], row[col_cepe], token)
            if details:
                df.at[i, 'qty'] = details.get('lot_size')
                exec_dt = pd.to_datetime(row['Execution Time'])
                entry = get_price_at_time(details['instrument_key'], exec_dt.strftime('%Y-%m-%d'), exec_dt.strftime('%H:%M'), token)
                df.at[i, 'Opt Entry'] = entry
                
                if pd.notna(row.get('Exit Time')):
                    exit_dt = pd.to_datetime(row['Exit Time'])
                    exit_p = get_price_at_time(details['instrument_key'], exit_dt.strftime('%Y-%m-%d'), exit_dt.strftime('%H:%M'), token)
                else:
                    exit_p = get_price_at_time(details['instrument_key'], datetime.now().strftime('%Y-%m-%d'), datetime.now().strftime('%H:%M'), token)
                
                df.at[i, 'Opt Exit'] = exit_p
                if entry and exit_p:
                    df.at[i, 'Opt PnL'] = (exit_p - entry) * details.get('lot_size')
        progress.progress((i + 1) / len(df))
    st.session_state.master_ledger = df
    st.success("Options updated!")

# --- UI ---
token = st.sidebar.text_input("Upstox Token", type="password")
uploaded = st.sidebar.file_uploader("Upload Ledger", type="csv")
if uploaded and st.sidebar.button("Load"): st.session_state.master_ledger = pd.read_csv(uploaded)

if st.sidebar.button("🚀 Fetch & Update Option Prices"):
    update_options_in_ledger(token)

st.data_editor(st.session_state.master_ledger, use_container_width=True)
