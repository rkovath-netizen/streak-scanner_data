import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime
import time
import os

st.set_page_config(layout="wide")

if "master_ledger" not in st.session_state: st.session_state.master_ledger = pd.DataFrame()

# --- Upstox API Functions ---
def get_opt_details(symbol, strike, ce_pe, token):
    query = f"{symbol} {int(strike)} {ce_pe.upper()}"
    url = f"https://api.upstox.com/v2/instruments/search?query={urllib.parse.quote(query)}&exchanges=NFO&segments=OPT"
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get('data'):
        return res.json()['data'][0]
    return None

def get_price_at_time(key, date_str, time_str, token):
    url = f"https://api.upstox.com/v2/historical-candle/{urllib.parse.quote(key)}/1minute/{date_str}/{date_str}"
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get('data'):
        for c in res.json()['data']['candles']:
            if c[0][11:16] == time_str: return c[1] # Returns Open price
    return None

# --- UI ---
st.title("📈 Fully Automated Options Ledger")
api_token = st.text_input("Enter Upstox Token", type="password")
uploaded_file = st.file_uploader("Upload Master Ledger", type="csv")

if uploaded_file and st.button("Process Ledger"):
    df = pd.read_csv(uploaded_file)
    progress = st.progress(0)
    
    for i, row in df.iterrows():
        if pd.notna(row.get('atm strike')) and pd.notna(row.get('CE/PE')):
            # 1. Resolve Instrument
            details = get_opt_details(row['Stock Name'], row['atm strike'], row['CE/PE'], api_token)
            if details:
                df.at[i, 'qty'] = details.get('lot_size')
                
                # 2. Parse Dates using Pandas flexible parser
                exec_dt = pd.to_datetime(row['Execution Time'])
                
                # 3. Get Entry Price
                entry = get_price_at_time(details['instrument_key'], exec_dt.strftime('%Y-%m-%d'), exec_dt.strftime('%H:%M'), api_token)
                df.at[i, 'Opt Entry'] = entry
                
                # 4. Get Exit/CMP
                if pd.notna(row.get('Exit Time')):
                    exit_dt = pd.to_datetime(row['Exit Time'])
                    exit_price = get_price_at_time(details['instrument_key'], exit_dt.strftime('%Y-%m-%d'), exit_dt.strftime('%H:%M'), api_token)
                else: # MTM for Live
                    exit_price = get_price_at_time(details['instrument_key'], datetime.now().strftime('%Y-%m-%d'), datetime.now().strftime('%H:%M'), api_token)
                
                df.at[i, 'Opt Exit'] = exit_price
                if entry and exit_price:
                    df.at[i, 'Opt PnL'] = (exit_price - entry) * details.get('lot_size')
        
        progress.progress((i + 1) / len(df))
    
    st.session_state.master_ledger = df
    st.success("Updates complete!")

st.data_editor(st.session_state.master_ledger, use_container_width=True)
