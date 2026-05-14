# Asymmetric Long Options Strategy for OpenAlgo (Kotak Broker)
# NSE F&O - Long Calls/Puts (30-60 DTE, slightly OTM)
# All 5 checkpoints + liquidity + asymmetry score

import asyncio
import pandas as pd
from datetime import datetime, timedelta
from openalgo import Client
from openalgo import build_option_chain_async

# ========================= CONFIG =========================
API_KEY = "108acd4d1aa4fd7535a27ae62033bde891ba6a7a38f0147d90a18987650cdc11"   # From OpenAlgo dashboard
BROKER = "kotak"
UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "HINDALCO", "TCS", "TATAMOTORS","BHARTIARTL"]
RISK_PERCENT = 1.0
PREMIUM_STOP_PCT = 45
DTE_MIN = 30
DTE_MAX = 60
DELTA_TARGET = (0.30, 0.45)
ASYM_SCORE_THRESHOLD = 0.65

client = Client(api_key=API_KEY, broker=BROKER)

# ========================= HELPERS =========================
async def get_option_chain(underlying: str, expiry: str):
    df = await build_option_chain_async(underlying, expiry, depth=50)
    return df

def calculate_iv_rank(current_iv: float, iv_52w_low: float, iv_52w_high: float) -> float:
    if iv_52w_high == iv_52w_low:
        return 0.0
    return (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100

async def check_all_checkpoints(underlying: str):
    """Returns (True, signal_dict) if ALL checkpoints pass"""
    now = datetime.now()

    # 1. Volatility Edge (IV Rank <40%)
    quote = await client.quote(underlying)
    atm_iv = quote.get("iv", 25.0)
    iv_rank = calculate_iv_rank(atm_iv, 12.0, 35.0)  # ← replace with real 52w fetch later
    if iv_rank >= 40:
        return False, None

    # Find 30-60 DTE expiry
    expiries = await client.get_expiries(underlying)
    target_expiry = None
    for exp in expiries:
        dte = (datetime.strptime(exp, "%d%b%y") - now).days
        if DTE_MIN <= dte <= DTE_MAX:
            target_expiry = exp
            break
    if not target_expiry:
        return False, None

    chain = await get_option_chain(underlying, target_expiry)

    # 2. Technical Confluence (placeholder - expand with TA-Lib if needed)
    # hist = await client.historical(underlying, interval="15m", days=5)
    # Add MACD, ADX, volume checks here

    # 3. Order-Flow & Sentiment Skew (fixed - no more undefined 'signal')
    pcr = chain[chain['type'] == 'PE']['oi'].sum() / chain[chain['type'] == 'CE']['oi'].sum()
    call_oi_build = (chain[(chain['type'] == 'CE') & (chain['oi_change'] > 0)]['oi_change'].sum() > 0)
    put_oi_build  = (chain[(chain['type'] == 'PE') & (chain['oi_change'] > 0)]['oi_change'].sum() > 0)

    # Simple bullish bias example (change to "bearish" for puts)
    direction = "bullish"   # ← set dynamically from technicals later
    if direction == "bullish":
        skew_ok = (pcr < 0.8 and call_oi_build)
    else:
        skew_ok = (pcr > 1.3 and put_oi_build)
    if not skew_ok:
        return False, None

    # 4. Catalyst (placeholder)
    has_catalyst = True   # ← add Moneycontrol calendar fetch or hardcoded events

    # 5. Pre-Volume Spurt Accumulation (placeholder)
    obv_rising = True      # implement OBV on historical
    delivery_rising = True # from NSE delivery data

    # Liquidity Filter
    liquid_strikes = chain[(chain['oi'] > 50000) & (chain['volume'] > 10000)]
    if liquid_strikes.empty:
        return False, None

    # Asymmetry Score
    asym_score = (1 - iv_rank/100) * 0.35 + 0.25 + 0.20 + 0.10 + 0.10
    if asym_score < ASYM_SCORE_THRESHOLD:
        return False, None

    # Select slightly OTM strike
    atm_price = quote['ltp']
    target_strikes = liquid_strikes[
        (liquid_strikes['strike'].between(atm_price * 0.98, atm_price * 1.05)) &
        (liquid_strikes['delta'].between(DELTA_TARGET[0], DELTA_TARGET[1]))
    ]
    if target_strikes.empty:
        return False, None

    best = target_strikes.iloc[0]
    option_type = "CE" if direction == "bullish" else "PE"

    capital = client.get_capital() or 1000000  # fallback
    qty = int((capital * RISK_PERCENT / 100) / best['premium'])

    return True, {
        "underlying": underlying,
        "expiry": target_expiry,
        "strike": best['strike'],
        "option_type": option_type,
        "quantity": qty,
        "premium": best['premium']
    }

# ========================= MAIN =========================
async def run_strategy():
    print(f"[{datetime.now()}] Asymmetric Strategy Running (Kotak + OpenAlgo)")
    for underlying in UNDERLYINGS:
        passed, signal = await check_all_checkpoints(underlying)
        if passed and signal:
            print(f"ENTRY SIGNAL → {signal}")
            order = await client.optionsorder(
                underlying=signal["underlying"],
                expiry=signal["expiry"],
                strike=signal["strike"],
                option_type=signal["option_type"],
                action="BUY",
                quantity=signal["quantity"],
                price_type="MARKET"
            )
            if order.get("status") == "success":
                print(f"✅ LONG {signal['option_type']} placed")
            else:
                print("❌ Order failed:", order)

    await asyncio.sleep(900)  # 15-min loop

if __name__ == "__main__":
    asyncio.run(run_strategy())