"""
===============================================================================
          OPTIONS BUYER-EDGE STRATEGY — Multi-Layer Confirmation
                         OpenAlgo Trading Bot
===============================================================================

Buying NSE F&O options (Calls or Puts) only when institutional-grade
confirmation lines up across FIVE independent signal layers — exactly the
same checks used inside the BuyerEdge analysis tool:

  Layer 1 — Technical Trend          (EMA, VWAP, RSI, MACD on spot candles)
  Layer 2 — OI Flow Intelligence     (PCR, Call/Put Flow, Migration)
  Layer 3 — Greeks Engine            (Delta Imbalance, Gamma Regime)
  Layer 4 — Straddle Engine          (IV Regime, Straddle Velocity)
  Layer 5 — Synthetic Futures        (spot-SF co-movement confirmation)

A composite score is computed (range −100 → +100) along with a trap-risk
score (0 → 100).  An order is placed only when:

  • composite score  ≥ MIN_SCORE  (absolute value, bullish or bearish)
  • trap_score       ≤ MAX_TRAP
  • signal is "EXECUTE" (not "WATCH" or "NO_TRADE")

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    python BuyerEdgeStrategy.py

Run via OpenAlgo's /python strategy runner:
    OPENALGO_API_KEY            : injected per-strategy.
    OPENALGO_STRATEGY_EXCHANGE  : exchange for this strategy (NFO / BFO).
    HOST_SERVER / WEBSOCKET_URL : inherited from OpenAlgo's .env.
    No code changes required.

⚠ RISK WARNING
    Long options buying has asymmetric payoff but unlimited theta decay.
    Always set PREMIUM_STOP_PCT and ensure adequate capital. This script
    is for educational purposes; backtest before live use.
===============================================================================
"""

import math
import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from openalgo import api

# ===============================================================================
# CONFIGURATION — all tunable via environment variables
# ===============================================================================

API_KEY  = os.getenv("OPENALGO_API_KEY", "openalgo-apikey")
API_HOST = os.getenv("HOST_SERVER",      "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL",   "ws://127.0.0.1:8765")

# Underlyings to scan
UNDERLYINGS_RAW = os.getenv(
    "UNDERLYINGS",
    "NIFTY,BANKNIFTY,FINNIFTY,RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS,TATAMOTORS",
)
UNDERLYINGS = [u.strip() for u in UNDERLYINGS_RAW.split(",") if u.strip()]

# Exchange where these underlyings trade
SPOT_EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
FNO_EXCHANGE  = os.getenv("FNO_EXCHANGE", "NFO")   # change to BFO for BSE F&O

# Options parameters
DTE_MIN       = int(os.getenv("DTE_MIN",       "7"))    # min days to expiry
DTE_MAX       = int(os.getenv("DTE_MAX",       "30"))   # max days to expiry
OTM_OFFSET    = int(os.getenv("OTM_OFFSET",    "1"))    # strikes OTM from ATM
LOT_MULTIPLIER= int(os.getenv("LOT_MULTIPLIER","1"))    # lots to buy

# Signal thresholds
MIN_SCORE      = int(os.getenv("MIN_SCORE",    "50"))   # minimum |score| to trade
MAX_TRAP       = int(os.getenv("MAX_TRAP",     "50"))   # maximum trap score to trade

# Risk Management
PREMIUM_STOP_PCT = float(os.getenv("PREMIUM_STOP_PCT", "40.0"))   # % loss from entry premium
PREMIUM_TARGET_PCT= float(os.getenv("PREMIUM_TARGET_PCT","80.0"))  # % gain from entry premium

# Technicals (spot candles)
CANDLE_INTERVAL  = os.getenv("CANDLE_INTERVAL",  "15m")
LOOKBACK_DAYS    = int(os.getenv("LOOKBACK_DAYS", "5"))
FAST_EMA_PERIOD  = int(os.getenv("FAST_EMA_PERIOD", "9"))
SLOW_EMA_PERIOD  = int(os.getenv("SLOW_EMA_PERIOD", "21"))
RSI_PERIOD       = int(os.getenv("RSI_PERIOD", "14"))

# Loop interval
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "60"))  # seconds


# ===============================================================================
# TECHNICAL HELPERS (pure-Python, no external TA library required)
# ===============================================================================

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast=12, slow=26, sig=9):
    fast_ema = _ema(series, fast)
    slow_ema = _ema(series, slow)
    macd_line = fast_ema - slow_ema
    sig_line  = _ema(macd_line, sig)
    histogram = macd_line - sig_line
    return macd_line, sig_line, histogram


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP (no groupby — suitable for intraday slice)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tvol = (typical * df["volume"]).cumsum()
    return cum_tvol / cum_vol


def _bbands(series: pd.Series, period=20, num_std=2.0):
    mid   = series.rolling(period).mean()
    std   = series.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


# ===============================================================================
# OI / FLOW HELPERS (from raw option-chain rows)
# ===============================================================================

def _compute_pcr(chain_rows: list[dict]) -> float:
    """Put-Call Ratio by OI."""
    ce_oi = sum(r.get("ce_oi", 0) or 0 for r in chain_rows)
    pe_oi = sum(r.get("pe_oi", 0) or 0 for r in chain_rows)
    if ce_oi == 0:
        return 1.0
    return pe_oi / ce_oi


def _call_wall(chain_rows: list[dict]) -> float | None:
    if not chain_rows:
        return None
    return max(chain_rows, key=lambda r: r.get("ce_oi", 0))["strike"]


def _put_wall(chain_rows: list[dict]) -> float | None:
    if not chain_rows:
        return None
    return max(chain_rows, key=lambda r: r.get("pe_oi", 0))["strike"]


def _classify_ce_flow(chain_rows: list[dict]) -> tuple[int, str]:
    """
    Classify Call flow from OI-change and premium-change columns.
    Returns (score, label):
      +2  Call Buying     (bearish — call buyers adding)
      +1  Short Covering  (mildly bullish)
      -1  Long Unwinding  (mildly bearish)
      -2  Call Writing    (bullish — call sellers adding, cap overhead)
    """
    ce_oi_chg  = sum(r.get("ce_oi_chg", 0) or 0 for r in chain_rows)
    ce_ltp_chg = sum(r.get("ce_ltp_chg", 0) or 0 for r in chain_rows)
    if ce_oi_chg > 0 and ce_ltp_chg > 0.5:
        return 2, "Call Buying"
    if ce_oi_chg < 0 and ce_ltp_chg > 0.5:
        return 1, "CE Short Covering"
    if ce_oi_chg > 0 and ce_ltp_chg < -0.5:
        return -2, "Call Writing"
    if ce_oi_chg < 0 and ce_ltp_chg < -0.5:
        return -1, "CE Long Unwinding"
    return 0, "CE Neutral"


def _classify_pe_flow(chain_rows: list[dict]) -> tuple[int, str]:
    """
    Classify Put flow using the correct 4-state model (same as BuyerEdge):
      OI↑ + premium↓ → Put Writing       (bullish, +2)
      OI↑ + premium↑ → Put Buying        (bearish, −2)
      OI↓ + premium↑ → PE Short Covering (mildly bullish, +1)
      OI↓ + premium↓ → PE Long Unwinding (mildly bearish, −1)
    """
    pe_oi_chg  = sum(r.get("pe_oi_chg", 0) or 0 for r in chain_rows)
    pe_ltp_chg = sum(r.get("pe_ltp_chg", 0) or 0 for r in chain_rows)
    if pe_oi_chg > 0 and pe_ltp_chg < -0.5:
        return 2, "Put Writing"
    if pe_oi_chg > 0 and pe_ltp_chg > 0.5:
        return -2, "Put Buying"
    if pe_oi_chg < 0 and pe_ltp_chg > 0.5:
        return 1, "PE Short Covering"
    if pe_oi_chg < 0 and pe_ltp_chg < -0.5:
        return -1, "PE Long Unwinding"
    return 0, "PE Neutral"


# ===============================================================================
# SCORING ENGINE  (mirrors BuyerEdge _compute_signal)
# ===============================================================================

def compute_composite_score(
    spot: float,
    df_spot: pd.DataFrame,
    chain_rows: list[dict],
    atm_ce_ltp: float,
    atm_pe_ltp: float,
    iv_rank: float | None,
    straddle_price: float | None,
    prev_straddle_price: float | None,
    sf_ltp: float | None,          # synthetic-future LTP (from near-month future quote)
    ce_bid: float | None,
    ce_ask: float | None,
    pe_bid: float | None,
    pe_ask: float | None,
) -> dict:
    """
    Compute a composite directional score (−100 → +100) and trap_score (0 → 100).

    The logic mirrors the 15-component BuyerEdge signal engine so that the
    same institutional-grade checks used in the web tool are replicated here.

    Returns a dict with keys:
        score, label, signal, trap_score, trap_reasons,
        reasons, direction, components
    """
    components = []
    reasons    = []

    def _dir(s: float) -> str:
        return "bullish" if s > 0 else "bearish" if s < 0 else "neutral"

    # ── LAYER 1: Technical Trend ─────────────────────────────────────────────
    # L1-a: Fast EMA vs Slow EMA (spot trend)
    s1 = 0
    trend_note = "Insufficient candles"
    if df_spot is not None and len(df_spot) >= SLOW_EMA_PERIOD + 2:
        fast = _ema(df_spot["close"], FAST_EMA_PERIOD)
        slow = _ema(df_spot["close"], SLOW_EMA_PERIOD)
        if fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-3] <= slow.iloc[-3]:
            s1 = 1
            trend_note = f"Bullish EMA crossover ({FAST_EMA_PERIOD}/{SLOW_EMA_PERIOD})"
        elif fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-3] >= slow.iloc[-3]:
            s1 = -1
            trend_note = f"Bearish EMA crossover ({FAST_EMA_PERIOD}/{SLOW_EMA_PERIOD})"
        elif fast.iloc[-2] > slow.iloc[-2]:
            s1 = 0.5
            trend_note = "Fast EMA above Slow EMA (bullish)"
        elif fast.iloc[-2] < slow.iloc[-2]:
            s1 = -0.5
            trend_note = "Fast EMA below Slow EMA (bearish)"
    components.append({"label": "EMA Trend", "score": s1, "max": 1, "direction": _dir(s1), "note": trend_note})

    # L1-b: RSI context
    s2 = 0
    rsi_note = "RSI unavailable"
    if df_spot is not None and len(df_spot) >= RSI_PERIOD + 2:
        rsi = _rsi(df_spot["close"], RSI_PERIOD)
        rsi_val = rsi.iloc[-2]
        if rsi_val > 55:
            s2 = 1
            rsi_note = f"RSI {rsi_val:.1f} — bullish momentum"
        elif rsi_val < 45:
            s2 = -1
            rsi_note = f"RSI {rsi_val:.1f} — bearish momentum"
        else:
            rsi_note = f"RSI {rsi_val:.1f} — neutral"
    components.append({"label": "RSI Momentum", "score": s2, "max": 1, "direction": _dir(s2), "note": rsi_note})

    # L1-c: MACD Histogram
    s3 = 0
    macd_note = "MACD unavailable"
    if df_spot is not None and len(df_spot) >= 35:
        _, _, hist = _macd(df_spot["close"])
        h_now  = hist.iloc[-2]
        h_prev = hist.iloc[-3]
        if h_now > 0 and h_now > h_prev:
            s3 = 1
            macd_note = "MACD Histogram expanding positive"
        elif h_now < 0 and h_now < h_prev:
            s3 = -1
            macd_note = "MACD Histogram expanding negative"
        elif h_now > 0:
            s3 = 0.5
            macd_note = "MACD Histogram positive (contracting)"
        elif h_now < 0:
            s3 = -0.5
            macd_note = "MACD Histogram negative (contracting)"
    components.append({"label": "MACD Histogram", "score": s3, "max": 1, "direction": _dir(s3), "note": macd_note})

    # L1-d: Price vs VWAP
    s4 = 0
    vwap_note = "VWAP unavailable"
    if df_spot is not None and len(df_spot) >= 5 and "volume" in df_spot.columns:
        vwap = _vwap(df_spot)
        vwap_val = vwap.iloc[-2]
        if spot > vwap_val:
            s4 = 1
            vwap_note = f"Spot {spot:.1f} above VWAP {vwap_val:.1f}"
        else:
            s4 = -1
            vwap_note = f"Spot {spot:.1f} below VWAP {vwap_val:.1f}"
    components.append({"label": "Spot vs VWAP", "score": s4, "max": 1, "direction": _dir(s4), "note": vwap_note})

    # ── LAYER 2: OI Flow Intelligence ────────────────────────────────────────
    # L2-a: PCR OI Level
    pcr = _compute_pcr(chain_rows)
    s5 = 0
    if pcr >= 1.2:    s5 = 1
    elif pcr >= 1.0:  s5 = 0.5
    elif pcr <= 0.6:  s5 = -1
    elif pcr <= 0.8:  s5 = -0.5
    components.append({"label": "PCR OI Level", "score": s5, "max": 1, "direction": _dir(s5), "note": f"PCR OI {pcr:.2f}"})

    # L2-b: Call Flow
    s6, ce_flow_label = _classify_ce_flow(chain_rows)
    components.append({"label": "Call OI Flow", "score": s6, "max": 2, "direction": _dir(s6), "note": ce_flow_label})

    # L2-c: Put Flow  (4-state model — same as BuyerEdge)
    s7, pe_flow_label = _classify_pe_flow(chain_rows)
    components.append({"label": "Put OI Flow", "score": s7, "max": 2, "direction": _dir(s7), "note": pe_flow_label})

    # L2-d: OI Wall position (call-wall above → bullish, put-wall below → bearish)
    s8 = 0
    cw = _call_wall(chain_rows)
    pw = _put_wall(chain_rows)
    wall_note = "OI walls unavailable"
    if cw and pw and spot:
        if spot < cw and spot > pw:
            # Spot between walls — direction from which wall is closer
            if (cw - spot) > (spot - pw):
                s8 = 0.5
                wall_note = f"Spot between walls (call wall {cw} far → mild bullish)"
            else:
                s8 = -0.5
                wall_note = f"Spot between walls (put wall {pw} close → mild bearish)"
        elif spot >= cw:
            s8 = -1
            wall_note = f"Spot {spot} at/above call wall {cw} — overhead resistance"
        elif spot <= pw:
            s8 = 1
            wall_note = f"Spot {spot} at/below put wall {pw} — downside supported"
    components.append({"label": "OI Wall Position", "score": s8, "max": 1, "direction": _dir(s8), "note": wall_note})

    # ── LAYER 3: Greeks Engine ───────────────────────────────────────────────
    # L3-a: Delta Imbalance (simple proxy from ATM LTPs when chain has no greeks)
    # Positive di (CE cheaper relative to PE) = bearish dealer exposure → bullish price pressure
    s9 = 0
    di_note = "Delta imbalance unavailable"
    di = 0.0
    if atm_ce_ltp and atm_pe_ltp and atm_pe_ltp > 0:
        di = (atm_pe_ltp - atm_ce_ltp) / ((atm_pe_ltp + atm_ce_ltp) / 2)
        if di >= 0.10:    s9 = 1;   di_note = f"Delta bias {di:.3f} — put premium heavy (bullish)"
        elif di >= 0.05:  s9 = 0.5; di_note = f"Delta bias {di:.3f} — mild put premium"
        elif di <= -0.10: s9 = -1;  di_note = f"Delta bias {di:.3f} — call premium heavy (bearish)"
        elif di <= -0.05: s9 = -0.5;di_note = f"Delta bias {di:.3f} — mild call premium"
        else:             di_note = f"Delta bias {di:.3f} — balanced"
    components.append({"label": "Greeks Bias (Δ)", "score": s9, "max": 1, "direction": _dir(s9), "note": di_note})

    # L3-b: Gamma Regime — regime context (same as BuyerEdge component 11)
    # No directional vote; used as confidence multiplier later.
    s10 = 0   # always 0 — non-directional
    gamma_note = "Gamma flip unavailable (no GEX data)"
    _is_short_gamma = None   # unknown without GEX
    components.append({"label": "Gamma Regime", "score": s10, "max": 2, "direction": "neutral", "note": gamma_note})

    # ── LAYER 4: Straddle & IV ───────────────────────────────────────────────
    # L4-a: IV Regime (IVR) — cheap options favour buyers, expensive penalise
    s11 = 0
    iv_note = "IVR unavailable"
    if iv_rank is not None:
        if iv_rank < 20:
            s11 = 1
            iv_note = f"IVR {iv_rank:.1f}% — cheap options, buyer structural edge"
        elif iv_rank > 50:
            s11 = -1
            iv_note = f"IVR {iv_rank:.1f}% — expensive options, structural disadvantage"
        else:
            iv_note = f"IVR {iv_rank:.1f}% — moderate"
    components.append({"label": "IV Regime (IVR)", "score": s11, "max": 1, "direction": _dir(s11), "note": iv_note})

    # L4-b: Straddle Velocity — expanding = real move, contracting = IV crush trap
    s12 = 0
    straddle_note = "Straddle velocity unavailable"
    straddle_vel  = "Flat"
    if straddle_price and prev_straddle_price and prev_straddle_price > 0:
        chg_pct = (straddle_price - prev_straddle_price) / prev_straddle_price * 100
        if chg_pct >= 3:
            s12 = 2
            straddle_vel  = "Expanding"
            straddle_note = f"Straddle expanding {chg_pct:+.1f}% — real directional move, buyer edge"
        elif chg_pct <= -3:
            s12 = -2
            straddle_vel  = "Contracting"
            straddle_note = f"Straddle contracting {chg_pct:+.1f}% — IV crush, avoid naked buying"
        else:
            straddle_note = f"Straddle flat ({chg_pct:+.1f}%)"
    components.append({"label": "Straddle Velocity", "score": s12, "max": 2, "direction": _dir(s12), "note": straddle_note})

    # ── LAYER 5: Synthetic Futures (spot-SF co-movement) ────────────────────
    # Mirrors BuyerEdge component 14.  +1 when SF and spot both move up/down;
    # −1 or 0 on divergence/wide-spread/unavailable.
    s13 = 0
    sf_note = "SF data unavailable"
    if sf_ltp and spot:
        basis = sf_ltp - spot
        spread_pct = None
        if ce_bid and ce_ask and ce_bid > 0:
            spread_pct = (ce_ask - ce_bid) / ((ce_ask + ce_bid) / 2) * 100
        if abs(basis) > spot * 0.015:
            # Large divergence — SF and spot disagree
            s13 = 0
            sf_note = f"SF divergence {basis:+.1f} ({abs(basis)/spot*100:.2f}% of spot) — signal suppressed"
        elif spread_pct is not None and spread_pct > 1.5:
            # Wide bid-ask on the option itself — executable cost too high
            s13 = 0
            sf_note = f"Wide option spread {spread_pct:.1f}% — executable cost degrades signal"
        else:
            carry = "normal" if basis >= -(spot * 0.001) else "backwardation"
            if basis > spot * 0.001:
                s13 = 1
                sf_note = f"SF carry {basis:+.1f} ({carry}) — bullish premium confirmation"
            elif basis < -(spot * 0.001):
                s13 = -1
                sf_note = f"SF carry {basis:+.1f} ({carry}) — bearish premium confirmation"
            else:
                sf_note = f"SF carry {basis:+.1f} ({carry}) — neutral"
    components.append({"label": "Synthetic Futures", "score": s13, "max": 1, "direction": _dir(s13), "note": sf_note})

    # ── Trap Score ───────────────────────────────────────────────────────────
    trap_score   = 0
    trap_reasons = []

    # T1: IV crush risk — straddle contracting
    if straddle_vel == "Contracting":
        trap_score += 25
        trap_reasons.append("Straddle contracting — IV crush trap")

    # T2: Expensive options (IVR > 60%)
    if iv_rank is not None and iv_rank > 60:
        trap_score += 20
        trap_reasons.append(f"High IVR {iv_rank:.1f}% — options structurally overpriced")

    # T3: SF basis divergence
    if sf_ltp and spot and abs(sf_ltp - spot) > spot * 0.015:
        trap_score += 15
        trap_reasons.append(f"SF divergence {abs(sf_ltp-spot)/spot*100:.2f}% — possible mispricing")

    # T4: Wide option spread
    if ce_bid and ce_ask and ce_bid > 0:
        sp = (ce_ask - ce_bid) / ((ce_ask + ce_bid) / 2) * 100
        if sp > 1.5:
            trap_score += 15
            trap_reasons.append(f"Wide CE spread {sp:.1f}% — high slippage cost")
    if pe_bid and pe_ask and pe_bid > 0:
        sp = (pe_ask - pe_bid) / ((pe_ask + pe_bid) / 2) * 100
        if sp > 1.5:
            trap_score += 15
            trap_reasons.append(f"Wide PE spread {sp:.1f}% — high slippage cost")

    # T5: PCR extreme — reversal risk
    if pcr > 2.5:
        trap_score += 10
        trap_reasons.append(f"PCR OI {pcr:.2f} — extreme put skew, reversal risk")
    elif pcr < 0.4:
        trap_score += 10
        trap_reasons.append(f"PCR OI {pcr:.2f} — extreme call skew, reversal risk")

    trap_score = min(100, trap_score)

    # ── Final Score ──────────────────────────────────────────────────────────
    # Max directional raw = 15 (1+1+1+1 + 1+2+2+1 + 1+0 + 1+2 + 1 = 15)
    raw_score  = sum(c["score"] for c in components)
    base_score = (raw_score / 15) * 100
    # No gamma-flip multiplier (no GEX data available in this standalone script)
    final_score = int(max(-100, min(100, base_score)))

    # Collect reasons from significant components
    for c in components:
        if abs(c["score"]) >= (c["max"] * 0.5):
            reasons.append(c["note"])

    abs_score = abs(final_score)
    if trap_score > 75:
        signal = "NO_TRADE"
        if trap_reasons:
            reasons.insert(0, f"⚠ High trap risk: {trap_reasons[0]}")
    elif abs_score >= MIN_SCORE:
        signal = "EXECUTE" if trap_score <= MAX_TRAP else "WATCH"
    elif abs_score >= 30:
        signal = "WATCH"
    else:
        signal = "NO_TRADE"

    label     = "Bullish" if final_score > 15 else "Bearish" if final_score < -15 else "Neutral"
    direction = "CE" if final_score > 0 else "PE"

    return {
        "score":       final_score,
        "label":       label,
        "signal":      signal,
        "direction":   direction,
        "trap_score":  trap_score,
        "trap_reasons":trap_reasons,
        "reasons":     list(set(reasons)),
        "components":  components,
    }


# ===============================================================================
# OPTION SELECTION HELPER
# ===============================================================================

def select_option_strike(
    chain_rows: list[dict],
    spot: float,
    option_type: str,   # "CE" or "PE"
    otm_offset: int,
) -> dict | None:
    """
    Pick a slightly OTM strike that is `otm_offset` strikes away from ATM.
    Returns the chain row dict or None if unavailable.
    """
    strikes = sorted(set(r["strike"] for r in chain_rows if "strike" in r))
    if not strikes:
        return None

    # Find ATM
    atm = min(strikes, key=lambda x: abs(x - spot))
    idx = strikes.index(atm)

    if option_type == "CE":
        target_idx = min(idx + otm_offset, len(strikes) - 1)
    else:  # PE
        target_idx = max(idx - otm_offset, 0)

    target_strike = strikes[target_idx]

    # Return the row for this strike + option_type
    for row in chain_rows:
        if row.get("strike") == target_strike:
            if option_type in (row.get("option_type", ""), ""):
                return row

    # Fallback: return any row with this strike
    for row in chain_rows:
        if row.get("strike") == target_strike:
            return row

    return None


# ===============================================================================
# MAIN STRATEGY BOT
# ===============================================================================

class OptionsBuyerEdgeBot:
    """
    Multi-layer options buyer-edge bot.

    Each underlying is scanned independently. Positions are tracked in a
    simple dict keyed by underlying.  SL/Target are monitored via WebSocket LTP.
    """

    def __init__(self):
        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)

        self.strategy_name = os.getenv("STRATEGY_NAME", "OptionsBuyerEdge")

        # {underlying: {"symbol": str, "entry_premium": float, "qty": int,
        #               "option_type": str, "sl": float, "tgt": float}}
        self.positions: dict[str, dict] = {}
        self.ltp_map:   dict[str, float] = {}
        self.exit_lock  = threading.Lock()
        self.exit_queue: set[str] = set()
        self.running    = True
        self.stop_event = threading.Event()

        print("[BOT] Options Buyer-Edge Strategy started")
        print(f"[BOT] Underlyings: {', '.join(UNDERLYINGS)}")
        print(f"[BOT] DTE range: {DTE_MIN}–{DTE_MAX} days | OTM offset: {OTM_OFFSET}")
        print(f"[BOT] Score threshold: {MIN_SCORE} | Max trap: {MAX_TRAP}")
        print(f"[BOT] Premium SL: {PREMIUM_STOP_PCT}% | Target: {PREMIUM_TARGET_PCT}%")

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _on_ltp(self, data: dict):
        if data.get("type") != "market_data":
            return
        sym = data.get("symbol", "")
        ltp = float(data.get("data", {}).get("ltp", 0) or 0)
        if not ltp:
            return
        self.ltp_map[sym] = ltp

        # Check SL / Target for this symbol
        for ul, pos in list(self.positions.items()):
            if pos.get("symbol") != sym:
                continue
            with self.exit_lock:
                if sym in self.exit_queue:
                    continue
                entry = pos["entry_premium"]
                sl    = pos["sl"]
                tgt   = pos["tgt"]
                reason = None
                if ltp <= sl:
                    reason = f"STOPLOSS HIT (LTP {ltp:.2f} ≤ SL {sl:.2f})"
                elif ltp >= tgt:
                    reason = f"TARGET HIT (LTP {ltp:.2f} ≥ TGT {tgt:.2f})"
                if reason:
                    self.exit_queue.add(sym)
                    print(f"\n[ALERT] {ul} {pos['option_type']}: {reason}")
                    t = threading.Thread(
                        target=self._place_exit, args=(ul, reason), daemon=True
                    )
                    t.start()

    def _ws_thread(self):
        try:
            print("[WS] Connecting...")
            self.client.connect()
            # Initial subscription will be updated as positions open
            print("[WS] Connected")
            while not self.stop_event.is_set():
                time.sleep(1)
        except Exception as exc:
            print(f"[WS ERROR] {exc}")
        finally:
            try:
                self.client.disconnect()
            except Exception:
                pass

    def _subscribe(self, exchange: str, symbol: str):
        try:
            self.client.subscribe_ltp(
                [{"exchange": exchange, "symbol": symbol}],
                on_data_received=self._on_ltp,
            )
            print(f"[WS] Subscribed {symbol}")
        except Exception as exc:
            print(f"[WS] Subscribe error: {exc}")

    def _unsubscribe(self, exchange: str, symbol: str):
        try:
            self.client.unsubscribe_ltp([{"exchange": exchange, "symbol": symbol}])
        except Exception:
            pass

    # ── Data Fetchers ────────────────────────────────────────────────────────

    def _fetch_spot_candles(self, symbol: str) -> pd.DataFrame | None:
        try:
            end   = datetime.now()
            start = end - timedelta(days=LOOKBACK_DAYS)
            df = self.client.history(
                symbol=symbol,
                exchange=SPOT_EXCHANGE,
                interval=CANDLE_INTERVAL,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
            if df is None or len(df) < SLOW_EMA_PERIOD + 5:
                return None
            return df
        except Exception as exc:
            print(f"[DATA] Candle fetch error for {symbol}: {exc}")
            return None

    def _fetch_option_chain(self, symbol: str) -> list[dict]:
        """
        Fetch option chain via OpenAlgo.
        Expects a list-of-dicts with keys: strike, ce_oi, pe_oi, ce_ltp, pe_ltp,
        ce_oi_chg, pe_oi_chg, ce_ltp_chg, pe_ltp_chg, ce_bid, ce_ask, pe_bid, pe_ask
        """
        try:
            raw = self.client.optionchain(symbol=symbol, exchange=FNO_EXCHANGE)
            if not raw:
                return []
            # Normalize to list if returned as dict
            if isinstance(raw, dict):
                rows = raw.get("data", raw.get("chain", []))
            else:
                rows = raw
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            print(f"[DATA] Option chain error for {symbol}: {exc}")
            return []

    def _fetch_quote(self, symbol: str, exchange: str) -> dict:
        try:
            return self.client.quotes(symbol=symbol, exchange=exchange) or {}
        except Exception:
            return {}

    def _fetch_iv_rank(self, symbol: str) -> float | None:
        """
        Attempt to get IVR from OpenAlgo's IVR endpoint.
        Falls back to None if unavailable.
        """
        try:
            result = self.client.ivr(symbol=symbol, exchange=FNO_EXCHANGE)
            if result and isinstance(result, dict):
                return result.get("iv_rank")
        except Exception:
            pass
        return None

    def _get_executed_price(self, order_id: str) -> float | None:
        for _ in range(5):
            time.sleep(2)
            try:
                resp = self.client.orderstatus(
                    order_id=order_id, strategy=self.strategy_name
                )
                od = resp.get("data", {})
                if od.get("order_status") == "complete":
                    p = float(od.get("average_price", 0))
                    if p > 0:
                        return p
                if od.get("order_status") in ("rejected", "cancelled"):
                    return None
            except Exception:
                pass
        return None

    # ── Order Placement ──────────────────────────────────────────────────────

    def _place_entry(self, underlying: str, option_symbol: str, qty: int) -> bool:
        print(f"[ORDER] BUY {qty} × {option_symbol}")
        try:
            resp = self.client.placeorder(
                strategy=self.strategy_name,
                symbol=option_symbol,
                exchange=FNO_EXCHANGE,
                action="BUY",
                quantity=qty,
                price_type="MARKET",
                product="NRML",
            )
            if resp.get("status") == "success":
                order_id = resp.get("orderid")
                executed = self._get_executed_price(order_id)
                if executed:
                    sl  = round(executed * (1 - PREMIUM_STOP_PCT / 100), 2)
                    tgt = round(executed * (1 + PREMIUM_TARGET_PCT / 100), 2)
                    self.positions[underlying] = {
                        "symbol":          option_symbol,
                        "entry_premium":   executed,
                        "qty":             qty,
                        "option_type":     option_symbol[-2:],   # "CE" or "PE"
                        "sl":              sl,
                        "tgt":             tgt,
                    }
                    self._subscribe(FNO_EXCHANGE, option_symbol)
                    print(f"[ENTRY] {underlying} | {option_symbol} × {qty} @ ₹{executed:.2f}")
                    print(f"        SL ₹{sl:.2f} | TGT ₹{tgt:.2f}")
                    return True
                print("[ERROR] Could not confirm executed price")
            else:
                print(f"[ERROR] Order failed: {resp}")
        except Exception as exc:
            print(f"[ERROR] Entry order exception: {exc}")
        return False

    def _place_exit(self, underlying: str, reason: str):
        pos = self.positions.get(underlying)
        if not pos:
            with self.exit_lock:
                self.exit_queue.discard(pos["symbol"] if pos else "")
            return
        opt_sym = pos["symbol"]
        print(f"[EXIT] Closing {underlying} {opt_sym} — {reason}")
        try:
            resp = self.client.placeorder(
                strategy=self.strategy_name,
                symbol=opt_sym,
                exchange=FNO_EXCHANGE,
                action="SELL",
                quantity=pos["qty"],
                price_type="MARKET",
                product="NRML",
            )
            if resp.get("status") == "success":
                exit_price = self.ltp_map.get(opt_sym, pos["entry_premium"])
                pnl = (exit_price - pos["entry_premium"]) * pos["qty"]
                print(f"[EXIT] {underlying}: exit ₹{exit_price:.2f} | P&L ₹{pnl:.2f}")
            else:
                print(f"[EXIT ERROR] {resp}")
        except Exception as exc:
            print(f"[EXIT ERROR] {exc}")
        finally:
            self._unsubscribe(FNO_EXCHANGE, opt_sym)
            self.positions.pop(underlying, None)
            with self.exit_lock:
                self.exit_queue.discard(opt_sym)

    # ── Signal Loop ──────────────────────────────────────────────────────────

    def _scan_underlying(self, symbol: str):
        if symbol in self.positions:
            return   # already in a trade for this underlying

        print(f"\n[SCAN] {symbol}")

        # --- gather data ---
        df_spot = self._fetch_spot_candles(symbol)
        spot_q  = self._fetch_quote(symbol, SPOT_EXCHANGE)
        spot    = float(spot_q.get("ltp", 0) or 0)
        if not spot:
            print(f"[SCAN] {symbol}: no spot LTP, skipping")
            return

        chain_rows = self._fetch_option_chain(symbol)
        if not chain_rows:
            print(f"[SCAN] {symbol}: empty option chain, skipping")
            return

        # ATM CE and PE LTP (for Greeks proxy)
        strikes = sorted(set(r.get("strike", 0) for r in chain_rows if r.get("strike")))
        atm = min(strikes, key=lambda x: abs(x - spot)) if strikes else None
        atm_ce_ltp = atm_pe_ltp = None
        ce_bid = ce_ask = pe_bid = pe_ask = None
        if atm:
            for row in chain_rows:
                if row.get("strike") == atm:
                    atm_ce_ltp = float(row.get("ce_ltp") or 0) or None
                    atm_pe_ltp = float(row.get("pe_ltp") or 0) or None
                    ce_bid = float(row.get("ce_bid") or 0) or None
                    ce_ask = float(row.get("ce_ask") or 0) or None
                    pe_bid = float(row.get("pe_bid") or 0) or None
                    pe_ask = float(row.get("pe_ask") or 0) or None
                    break

        # Straddle price (ATM CE + ATM PE)
        straddle_price = (
            (atm_ce_ltp + atm_pe_ltp) if atm_ce_ltp and atm_pe_ltp else None
        )
        # Previous straddle price — use a simple proxy: 1% lower (placeholder;
        # ideally stored from the previous scan cycle).
        prev_straddle_price = straddle_price * 0.99 if straddle_price else None

        # Synthetic future (near-month future LTP as SF proxy)
        sf_q   = self._fetch_quote(f"{symbol}FUT", FNO_EXCHANGE)
        sf_ltp = float(sf_q.get("ltp", 0) or 0) or None

        # IV Rank
        iv_rank = self._fetch_iv_rank(symbol)

        # --- compute score ---
        result = compute_composite_score(
            spot             = spot,
            df_spot          = df_spot,
            chain_rows       = chain_rows,
            atm_ce_ltp       = atm_ce_ltp,
            atm_pe_ltp       = atm_pe_ltp,
            iv_rank          = iv_rank,
            straddle_price   = straddle_price,
            prev_straddle_price = prev_straddle_price,
            sf_ltp           = sf_ltp,
            ce_bid           = ce_bid,
            ce_ask           = ce_ask,
            pe_bid           = pe_bid,
            pe_ask           = pe_ask,
        )

        score      = result["score"]
        signal     = result["signal"]
        label      = result["label"]
        trap_score = result["trap_score"]
        direction  = result["direction"]

        print(f"[SCORE] {symbol}: {score:+d} ({label}) | trap={trap_score} | signal={signal}")
        for note in result["reasons"][:4]:
            print(f"        • {note}")

        if signal != "EXECUTE":
            print(f"[SKIP] {symbol}: signal={signal}, not executing")
            return

        # --- select strike ---
        opt_row = select_option_strike(chain_rows, spot, direction, OTM_OFFSET)
        if not opt_row:
            print(f"[SKIP] {symbol}: could not select OTM strike")
            return

        target_strike = opt_row.get("strike")
        option_ltp    = float(opt_row.get(f"{direction.lower()}_ltp") or opt_row.get("ltp", 0))
        if not option_ltp:
            print(f"[SKIP] {symbol}: option LTP is 0")
            return

        # Build option symbol (OpenAlgo standard format: e.g. NIFTY25MAY2424000CE)
        # Requires knowing the expiry.  We use the symbol from the chain row if present.
        opt_symbol = opt_row.get("symbol") or opt_row.get(f"{direction.lower()}_symbol")
        if not opt_symbol:
            print(f"[SKIP] {symbol}: option symbol not in chain row")
            return

        qty = LOT_MULTIPLIER * int(opt_row.get("lotsize", 1) or 1)
        if qty <= 0:
            qty = LOT_MULTIPLIER

        print(f"[SIGNAL] {symbol}: BUY {direction} | strike={target_strike} | premium=₹{option_ltp:.2f} | qty={qty}")

        self._place_entry(symbol, opt_symbol, qty)

    def _strategy_thread(self):
        print("[STRATEGY] Thread started — scanning every "
              f"{SIGNAL_CHECK_INTERVAL}s")
        while not self.stop_event.is_set():
            try:
                for ul in UNDERLYINGS:
                    if self.stop_event.is_set():
                        break
                    self._scan_underlying(ul)
            except Exception as exc:
                print(f"[STRATEGY ERROR] {exc}")
            time.sleep(SIGNAL_CHECK_INTERVAL)

    # ── Run ──────────────────────────────────────────────────────────────────

    def run(self):
        print("=" * 70)
        print(" OPTIONS BUYER-EDGE STRATEGY — Multi-Layer Confirmation")
        print("=" * 70)
        print(f" Underlyings : {', '.join(UNDERLYINGS)}")
        print(f" Spot exch   : {SPOT_EXCHANGE}  |  F&O exch: {FNO_EXCHANGE}")
        print(f" DTE         : {DTE_MIN}–{DTE_MAX} days  |  OTM offset: {OTM_OFFSET}")
        print(f" Score gate  : ≥{MIN_SCORE}  |  Trap gate: ≤{MAX_TRAP}")
        print(f" Premium SL  : {PREMIUM_STOP_PCT}%  |  Target: {PREMIUM_TARGET_PCT}%")
        print(f" Candle      : {CANDLE_INTERVAL}  |  Lookback: {LOOKBACK_DAYS}d")
        print(f" Loop        : every {SIGNAL_CHECK_INTERVAL}s")
        print("=" * 70)
        print("Press Ctrl+C to stop\n")

        ws_t = threading.Thread(target=self._ws_thread, daemon=True)
        ws_t.start()
        time.sleep(2)   # let WS connect

        st_t = threading.Thread(target=self._strategy_thread, daemon=True)
        st_t.start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Stopping bot...")
            self.running = False
            self.stop_event.set()

            # Close all open positions
            for ul in list(self.positions.keys()):
                print(f"[SHUTDOWN] Closing {ul} position...")
                self._place_exit(ul, "Bot Shutdown")

            ws_t.join(timeout=5)
            st_t.join(timeout=5)
            print("[DONE] Bot stopped.")


# ===============================================================================
# ENTRY POINT
# ===============================================================================

if __name__ == "__main__":
    if not API_KEY or API_KEY == "openalgo-apikey":
        print(
            "[WARNING] OPENALGO_API_KEY is not set in environment.\n"
            "          Export it before running: export OPENALGO_API_KEY=your-key"
        )

    print("=" * 70)
    print(" OPTIONS BUYER-EDGE STRATEGY — READY")
    print("=" * 70)
    print(f" Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70 + "\n")

    bot = OptionsBuyerEdgeBot()
    bot.run()