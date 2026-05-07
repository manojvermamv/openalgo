"""
Buyer Edge Service
State engine that answers one question: "Are sellers still in control,
or are they being forced to reprice?"

Outputs one of three signals: NO_TRADE / WATCH / EXECUTE

Layered modules:
  Module 1 — Market State Engine
  Module 2 — OI Intelligence
  Module 3 — Greeks Engine
  Module 4 — Straddle Engine
  Module 5 — Signal Engine

Snapshot cache keyed by (underlying, exchange, expiry) with TTL=300s.
"""

import time
import math
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from services.buyer_edge_synthetic_service import _compute_synthetic_future_context
from services.buyer_edge_utils import get_buyer_edge_quote_exchange
from services.history_service import get_history
from services.option_chain_service import get_option_chain
from services.option_greeks_service import (
    DEFAULT_INTEREST_RATES,
    parse_option_symbol,
    calculate_time_to_expiry,
)
from services.strategy_chart_service import _resolve_trading_window
from utils.logging import get_logger
from utils.datetime_utils import IST, to_ist_epoch, get_ist_now

logger = get_logger(__name__)

# Snapshot cache
_SNAPSHOT_MAX = 20
_SNAPSHOT_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_SNAPSHOT_TTL = 300  # seconds

def _get_snapshot(key: tuple) -> dict | None:
    entry = _SNAPSHOT_CACHE.get(key)
    if entry and (time.monotonic() - entry["ts"]) < _SNAPSHOT_TTL:
        _SNAPSHOT_CACHE.move_to_end(key)
        return entry["data"]
    return None

def _set_snapshot(key: tuple, data: dict) -> None:
    _SNAPSHOT_CACHE[key] = {"ts": time.monotonic(), "data": data}
    _SNAPSHOT_CACHE.move_to_end(key)
    while len(_SNAPSHOT_CACHE) > _SNAPSHOT_MAX:
        _SNAPSHOT_CACHE.popitem(last=False)

# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

def _compute_market_state(closes: list[float]) -> dict[str, str]:
    """Compute Trend, Regime, and Location."""
    if len(closes) < 5:
        return {"trend": "Neutral", "regime": "Compression", "location": "Mid"}

    last = closes[-1]
    mid = len(closes) // 2
    prior, recent = closes[:mid], closes[mid:]
    p_h, p_l = max(prior), min(prior)
    r_h, r_l = max(recent), min(recent)

    if r_h > p_h and r_l > p_l: trend = "Bullish"
    elif r_h < p_h and r_l < p_l: trend = "Bearish"
    else: trend = "Neutral"

    full_window = closes
    f_h, f_l = max(full_window), min(full_window)
    span = f_h - f_l
    
    recent_window = closes[-5:]
    r_range = max(recent_window) - min(recent_window)
    
    if span == 0: regime, location = "Compression", "Mid"
    else:
        ratio = r_range / span
        if ratio < 0.40: regime = "Compression"
        elif ratio > 0.70: regime = "Expansion"
        else: regime = "Neutral"

        if last > f_h or last < f_l: location = "Breakout"
        elif last >= f_h - 0.20 * span: location = "Range High"
        elif last <= f_l + 0.20 * span: location = "Range Low"
        else: location = "Mid"

    return {"trend": trend, "regime": regime, "location": location}


def _compute_oi_intelligence(chain: list[dict], prev_snapshot: dict | None) -> dict[str, Any]:
    """Call/Put walls and OI migration."""
    c_oi, p_oi = {}, {}
    for item in chain:
        strike = item["strike"]
        c_oi[strike] = (item.get("ce") or {}).get("oi", 0) or 0
        p_oi[strike] = (item.get("pe") or {}).get("oi", 0) or 0

    c_wall = max(c_oi, key=c_oi.get, default=0) if c_oi else 0
    p_wall = max(p_oi, key=p_oi.get, default=0) if p_oi else 0

    migrating, direction, roll = False, "Stable", False

    if prev_snapshot:
        p_c_oi, p_p_oi = prev_snapshot.get("call_oi", {}), prev_snapshot.get("put_oi", {})
        c_delta = sum(abs(c_oi.get(s, 0) - p_c_oi.get(s, 0)) for s in set(c_oi) | set(p_c_oi))
        p_delta = sum(abs(p_oi.get(s, 0) - p_p_oi.get(s, 0)) for s in set(p_oi) | set(p_p_oi))
        total = sum(c_oi.values()) + sum(p_oi.values())
        if total > 0 and (c_delta + p_delta) / total > 0.05:
            migrating = True
            direction = "Call-heavy" if c_delta > p_delta else "Put-heavy"

        p_c_wall, p_p_wall = prev_snapshot.get("call_wall", 0), prev_snapshot.get("put_wall", 0)
        strikes = sorted(c_oi.keys())
        step = (strikes[-1] - strikes[0]) / max(len(strikes) - 1, 1) if len(strikes) >= 2 else 1
        roll = abs(c_wall - p_c_wall) >= step or abs(p_wall - p_p_wall) >= step

    return {
        "call_wall": c_wall, "put_wall": p_wall,
        "oi_migrating": migrating, "migration_direction": direction, "oi_roll_detected": roll,
        "_call_oi": c_oi, "_put_oi": p_oi
    }


def _compute_greeks_engine(chain: list[dict], spot: float, exch: str, prev_snapshot: dict | None) -> dict[str, Any]:
    """Delta imbalance and gamma regime using optimized Black-76."""
    try:
        from py_vollib.black.greeks.analytical import delta as b_delta, gamma as b_gamma
        from py_vollib.black.implied_volatility import implied_volatility as b_iv
    except ImportError:
        return {"delta_imbalance": 0, "gamma_regime": "Mean-Reversion"}

    t_c_delta, t_p_delta, net_gamma = 0.0, 0.0, 0.0
    rate = DEFAULT_INTEREST_RATES.get(exch, 0) / 100.0
    
    # Get TTE once
    tte = 0.0
    for item in chain:
        sym = (item.get("ce") or {}).get("symbol") or (item.get("pe") or {}).get("symbol")
        if sym:
            try:
                _, exp, _, _ = parse_option_symbol(sym, exch)
                tte, _ = calculate_time_to_expiry(exp)
                if tte > 0: break
            except: continue
    
    if tte <= 0: return {"delta_imbalance": 0, "gamma_regime": "Mean-Reversion"}

    for item in chain:
        for side, flag in [("ce", "c"), ("pe", "p")]:
            entry = item.get(side) or {}
            ltp = entry.get("ltp", 0) or 0
            if ltp <= 0: continue
            
            try:
                iv = b_iv(ltp, spot, item["strike"], rate, tte, flag)
                if not math.isfinite(iv) or iv <= 0: continue
                
                d = b_delta(flag, spot, item["strike"], tte, rate, iv)
                g = b_gamma(flag, spot, item["strike"], tte, rate, iv)
                
                if not math.isfinite(d) or not math.isfinite(g): continue
                
                oi = entry.get("oi", 0) or 0
                ls = entry.get("lotsize", 1) or 1
                
                if flag == "c": t_c_delta += d; net_gamma += g * oi * ls
                else: t_p_delta += d; net_gamma -= g * oi * ls
            except: continue

    di = round(t_c_delta + t_p_delta, 4)
    if not math.isfinite(di): di = 0.0
    
    vel = "Stable"
    if prev_snapshot:
        p_di = prev_snapshot.get("delta_imbalance", 0)
        if abs(di - p_di) > 0.05: vel = "Rising" if di > p_di else "Falling"

    return {
        "delta_imbalance": di, "delta_velocity": vel,
        "gamma_regime": "Expansion" if net_gamma < 0 else "Mean-Reversion",
        "total_call_delta": round(t_c_delta, 4), "total_put_delta": round(t_p_delta, 4)
    }




def _compute_signal(market: dict, oi: dict, greeks: dict, straddle: dict, pcr_series: list[dict] = None, gex_levels: dict = None, ivr_data: dict = None, data_quality: dict = None, synthetic_engine: dict | None = None) -> dict[str, Any]:
    """
    OpenAlgo Signal Intelligence Engine.
    Implements 15 granular components for institutional-grade scoring.
    """
    components = []
    reasons = []
    
    def dir_label(s: float) -> str:
        return "bullish" if s > 0 else "bearish" if s < 0 else "neutral"

    has_series = pcr_series and len(pcr_series) >= 2
    latest = pcr_series[-1] if has_series else None
    prev_bar = pcr_series[-2] if has_series else None
    first = pcr_series[0] if has_series else None

    # 1. Spot Short-term (1)
    s1 = 0
    if latest and prev_bar:
        s1 = 1 if latest["spot"] > prev_bar["spot"] else -1 if latest["spot"] < prev_bar["spot"] else 0
    components.append({"label": "Spot (Short-term)", "score": s1, "max": 1, "direction": dir_label(s1), "note": f"Spot {'UP' if s1>0 else 'DOWN' if s1<0 else 'FLAT'} vs prev bar"})

    # 2. Spot Session (1)
    s2 = 0
    if latest and first:
        s2 = 1 if latest["spot"] > first["spot"] else -1 if latest["spot"] < first["spot"] else 0
    components.append({"label": "Spot (Session)", "score": s2, "max": 1, "direction": dir_label(s2), "note": f"Spot {'UP' if s2>0 else 'DOWN' if s2<0 else 'FLAT'} vs open"})

    # 3. Spot vs VWAP (1)
    s3 = 0
    vwap = 0
    if has_series:
        vwap = sum(p["spot"] for p in pcr_series) / len(pcr_series)
        s3 = 1 if latest["spot"] > vwap else -1 if latest["spot"] < vwap else 0
    components.append({"label": "Spot vs VWAP", "score": s3, "max": 1, "direction": dir_label(s3), "note": f"Spot {'ABOVE' if s3>0 else 'BELOW' if s3<0 else 'AT'} VWAP ({round(vwap, 1)})"})

    # 4. PCR OI Level (1)
    s4 = 0
    pcr_oi = oi.get("current_pcr_oi", 1)
    if pcr_oi >= 1.2: s4 = 1
    elif pcr_oi >= 1.0: s4 = 0.5
    elif pcr_oi <= 0.6: s4 = -1
    elif pcr_oi <= 0.8: s4 = -0.5
    components.append({"label": "PCR OI Level", "score": s4, "max": 1, "direction": dir_label(s4), "note": f"PCR OI at {round(pcr_oi, 2)}"})

    # 5. PCR OI Change (1)
    s5 = 0
    pcr_chg = oi.get("current_pcr_oi_chg", 1)
    if pcr_chg >= 1.2: s5 = 1
    elif pcr_chg >= 0.9: s5 = 0.5
    elif pcr_chg <= 0.5: s5 = -1
    elif pcr_chg <= 0.7: s5 = -0.5
    components.append({"label": "PCR OI Trend", "score": s5, "max": 1, "direction": dir_label(s5), "note": f"Intraday PCR Δ at {round(pcr_chg, 2)}"})

    # 6. CE Flow (2)
    s6 = 0
    ce_note = "Neutral"
    if latest and first:
        ce_oi_delta = latest["ce_oi"] - first["ce_oi"]
        ce_prem_delta = latest["atm_ce_ltp"] - first["atm_ce_ltp"]
        # Simple flow logic
        if ce_oi_delta > 0 and ce_prem_delta > 0.5: s6 = 2; ce_note = "Call Buying"
        elif ce_oi_delta < 0 and ce_prem_delta > 0.5: s6 = 1; ce_note = "Short Covering"
        elif ce_oi_delta > 0 and ce_prem_delta < -0.5: s6 = -2; ce_note = "Call Writing"
        elif ce_oi_delta < 0 and ce_prem_delta < -0.5: s6 = -1; ce_note = "Long Unwinding"
    components.append({"label": "Call Flow", "score": s6, "max": 2, "direction": dir_label(s6), "note": ce_note})

    # 7. PE Flow (2)
    # Correct 4-state classification for puts:
    #   OI↑ + premium↓ → Put Writing (sellers adding, bullish support)
    #   OI↓ + premium↓ → PE Long Unwinding (longs closing, mildly bearish)
    #   OI↑ + premium↑ → Put Buying (buyers adding, bearish pressure)
    #   OI↓ + premium↑ → PE Short Covering (shorts buying back, mildly bullish)
    s7 = 0
    pe_note = "Neutral"
    if latest and first:
        pe_oi_delta = latest["pe_oi"] - first["pe_oi"]
        pe_prem_delta = latest["atm_pe_ltp"] - first["atm_pe_ltp"]
        if pe_oi_delta > 0 and pe_prem_delta < -0.5: s7 = 2; pe_note = "Put Writing"
        elif pe_oi_delta < 0 and pe_prem_delta < -0.5: s7 = -1; pe_note = "PE Long Unwinding"
        elif pe_oi_delta > 0 and pe_prem_delta > 0.5: s7 = -2; pe_note = "Put Buying"
        elif pe_oi_delta < 0 and pe_prem_delta > 0.5: s7 = 1; pe_note = "PE Short Covering"
    components.append({"label": "Put Flow", "score": s7, "max": 2, "direction": dir_label(s7), "note": pe_note})

    # 8. Breadth Bias (1)
    s8 = 0
    if latest:
        ce_adv = latest.get("ce_advances", 0)
        pe_adv = latest.get("pe_advances", 0)
        if ce_adv + pe_adv > 0:
            bias = pe_adv / ce_adv if ce_adv > 0 else 99
            if bias <= 0.67: s8 = 1
            elif bias >= 1.5: s8 = -1
            elif bias <= 0.83: s8 = 0.5
            elif bias >= 1.2: s8 = -0.5
    components.append({"label": "Market Breadth", "score": s8, "max": 1, "direction": dir_label(s8), "note": "Based on CE/PE Advances"})

    # 9. Delta Imbalance (1)
    s9 = 0
    di = greeks.get("delta_imbalance", 0) or 0
    if di >= 0.1: s9 = 1
    elif di >= 0.05: s9 = 0.5
    elif di <= -0.1: s9 = -1
    elif di <= -0.05: s9 = -0.5
    components.append({"label": "Greeks Bias", "score": s9, "max": 1, "direction": dir_label(s9), "note": f"Δ Imbalance {round(di, 3)}"})

    # 10. Market Trend (1)
    s10 = 0
    trend = market.get("trend", "Neutral")
    if trend == "Bullish": s10 = 1
    elif trend == "Bearish": s10 = -1
    components.append({"label": "Engine Trend", "score": s10, "max": 1, "direction": dir_label(s10), "note": trend})

    # ── Institutional Regime Components (11–15) ──────────────────────────────

    # 11. Gamma Regime — regime context only, NOT a directional vote.
    # Gamma regime amplifies or dampens dealer delta-hedging activity.
    # Short-gamma (spot < flip): dealers amplify moves → buyers have edge.
    # Long-gamma (spot > flip): dealers dampen moves → mean-reversion traps.
    # We capture this as a confidence multiplier applied after normalization,
    # NOT as a ±2 directional score (which would bias toward bullish regardless
    # of direction and create spurious signal in sideways markets).
    s11 = 0  # no directional contribution
    gamma_flip = (gex_levels or {}).get("gamma_flip")
    spot_val = latest["spot"] if latest else 0
    _is_short_gamma = False
    if gamma_flip is not None and spot_val:
        _is_short_gamma = spot_val < gamma_flip
        gamma_flip_note = (
            f"Short-gamma (spot below flip {round(gamma_flip,0)}) — moves amplified, buyer edge"
            if _is_short_gamma
            else f"Long-gamma (spot above flip {round(gamma_flip,0)}) — mean-revert traps, buyer at risk"
        )
    else:
        gamma_flip_note = "GEX data unavailable"
    components.append({"label": "Gamma Regime", "score": s11, "max": 2, "direction": "neutral",
                        "note": gamma_flip_note})

    # 12. HVL Side (±1): call delta territory vs put delta territory
    s12 = 0
    hvl = (gex_levels or {}).get("hvl")
    if hvl is not None and spot_val:
        s12 = 1 if spot_val > hvl else -1  # above HVL = call-delta territory (bullish lean)
    components.append({"label": "HVL Side", "score": s12, "max": 1, "direction": dir_label(s12),
                        "note": (f"Spot {'above' if s12>0 else 'below'} HVL ({round(hvl,0)}) — "
                                 f"{'call-delta' if s12>0 else 'put-delta'} territory")
                        if hvl is not None else "GEX data unavailable"})

    # 13. IV Regime (±1): cheap options favour buyers, expensive penalise
    s13 = 0
    iv_rank = (ivr_data or {}).get("iv_rank")
    if iv_rank is not None:
        if iv_rank < 20: s13 = 1    # cheap options — positive EV for buyers
        elif iv_rank > 50: s13 = -1  # expensive options — structural disadvantage
    components.append({"label": "IV Regime", "score": s13, "max": 1, "direction": dir_label(s13),
                        "note": (f"IVR {round(iv_rank,1)}% — {'cheap, buyer edge' if s13>0 else 'expensive, buyer penalised' if s13<0 else 'moderate'}")
                        if iv_rank is not None else "IVR data unavailable"})

    # 14. Synthetic Futures (±1): spot-SF co-movement confirmation
    # +1: both spot and SF rising (options pricing confirms spot bull)
    # -1: both spot and SF falling (options pricing confirms spot bear)
    #  0: neutral, illiquid, wide spread, diverging, spot-flat, or unavailable
    s14 = 0
    sf_note = "SF data unavailable"
    if synthetic_engine:
        _liq = synthetic_engine.get("liquidity_status", "invalid")
        _pressure = synthetic_engine.get("pressure", "neutral")
        _conf = synthetic_engine.get("confirmation", "unavailable")
        if _liq == "invalid":
            sf_note = "SF illiquid — insufficient bid/ask data"
        elif _liq == "wide":
            _sp = synthetic_engine.get("spread_pct")
            sf_note = f"SF wide spread ({round(_sp, 2) if _sp else '?'}%) — executable cost too high"
        elif _conf == "diverging":
            sf_note = synthetic_engine.get("reason", "SF divergence trap detected")
        elif _pressure == "bullish" and _conf == "confirming":
            s14 = 1
            sf_note = synthetic_engine.get("reason", "SF bullish confirmation")
        elif _pressure == "bearish" and _conf == "confirming":
            s14 = -1
            sf_note = synthetic_engine.get("reason", "SF bearish confirmation")
        else:
            sf_note = synthetic_engine.get("reason", f"SF {_pressure} ({_conf})")
    elif latest:
        # Fallback: LTP-series synthetic when no live chain context.
        # Raw basis sign is NOT directional (positive = normal carry). Only
        # display the carry context; do not score.
        sf = latest.get("synthetic_future")
        if sf is not None and spot_val:
            basis_carry = round(sf - spot_val, 1)
            carry_sign = "+" if basis_carry >= 0 else ""
            carry_state = "backwardation" if basis_carry < -(spot_val * 0.001) else "normal"
            sf_note = f"SF carry {carry_sign}{basis_carry} ({carry_state}, LTP-only — no directional score)"
        else:
            sf_note = "SF data unavailable (LTP fallback)"
    components.append({"label": "Synthetic Futures", "score": s14, "max": 1, "direction": dir_label(s14), "note": sf_note})

    # 15. Straddle Velocity (±2): expanding straddle = real directional move;
    #     contracting = IV crush = options buyer trap
    s15 = 0
    s_vel = straddle.get("straddle_velocity", "Flat")
    if s_vel == "Expanding": s15 = 2
    elif s_vel == "Contracting": s15 = -2
    components.append({"label": "Straddle Velocity", "score": s15, "max": 2, "direction": dir_label(s15),
                        "note": f"Straddle {s_vel} — {'real move supports buying' if s15>0 else 'IV crush, avoid naked buying' if s15<0 else 'flat premium'}"})

    # ── Trap Score (0–100) ────────────────────────────────────────────────────
    # A separate risk-of-trap metric; overrides the directional signal when high.
    trap_score = 0
    trap_reasons = []

    # T1: Long-gamma regime — dealers dampen moves, option buyers bleed theta
    if gamma_flip is not None and spot_val and not _is_short_gamma:
        trap_score += 30
        trap_reasons.append(f"Long-gamma regime (spot above flip {round(gamma_flip,0)})")

    # T2: IV crush — straddle contracting while buying
    if s_vel == "Contracting":
        trap_score += 20
        trap_reasons.append("IV contracting (straddle velocity negative)")

    # T3: Expensive options (IVR > 60%)
    if iv_rank is not None and iv_rank > 60:
        trap_score += 20
        trap_reasons.append(f"High IVR {round(iv_rank,1)}% — options structurally overpriced")

    # T4: Synthetic spread or divergence trap
    if synthetic_engine:
        _liq_t = synthetic_engine.get("liquidity_status", "invalid")
        _conf_t = synthetic_engine.get("confirmation", "unavailable")
        _spread_pct_t = synthetic_engine.get("spread_pct")
        if _liq_t == "wide" or (_spread_pct_t and _spread_pct_t > 0.5):
            trap_score += 15
            trap_reasons.append(
                f"SF wide spread {round(_spread_pct_t, 2) if _spread_pct_t else '?'}% — executable cost degrades signal"
            )
        if _conf_t == "diverging":
            trap_score += 15
            trap_reasons.append(
                f"SF divergence: {synthetic_engine.get('reason', 'spot/SF direction disagree')}"
            )
    elif latest and spot_val:
        sf = latest.get("synthetic_future")
        if sf is not None and abs(sf - spot_val) > 0.003 * spot_val:
            trap_score += 15
            trap_reasons.append(f"SF divergence {round(abs(sf - spot_val) / spot_val * 100, 2)}% — option mispricing detected")

    # T5: Spot within 0.5× expected move of absolute gamma wall (pin risk)
    abs_wall = (gex_levels or {}).get("absolute_wall")
    if abs_wall is not None and spot_val:
        exp_move = None
        if ivr_data and ivr_data.get("expiries") and len(ivr_data["expiries"]) > 0:
            exp_move = (ivr_data["expiries"][0] or {}).get("expected_move")
        if exp_move is None:
            exp_move = spot_val * 0.005 * 5  # fallback: ~1.1% (0.5% × √5)
        wall_dist = abs(spot_val - abs_wall)
        if exp_move > 0 and wall_dist < 0.5 * exp_move:
            trap_score += 15
            trap_reasons.append(f"Spot {round(wall_dist,0)} pts from abs gamma wall {round(abs_wall,0)} — pin risk")

    trap_score = min(100, trap_score)

    # ── Final Normalisation ──────────────────────────────────────────────────
    # Max directional raw = 18 (components 1–10 = 12, plus 12/13/14/15 = 6;
    # component 11 gamma regime contributes 0 to direction as it is a regime
    # modifier, not a directional vote). Normalise by 18 so the scale stays
    # anchored at ±100.
    raw_score = sum(c["score"] for c in components)
    base_score = (raw_score / 18) * 100

    # Gamma regime confidence modifier: amplify abs conviction in short-gamma
    # regime (+10%), dampen in long-gamma regime (−10%). Preserves direction.
    if gamma_flip is not None and spot_val:
        multiplier = 1.10 if _is_short_gamma else 0.90
        base_score = base_score * multiplier

    final_score = int(max(-100, min(100, base_score)))

    # Reasons from significant components
    for c in components:
        if abs(c["score"]) >= (c["max"] * 0.5):
            reasons.append(c["note"])

    # Determine Signal (with trap_score override)
    abs_score = abs(final_score)
    if trap_score > 75:
        signal = "NO_TRADE"
        if trap_reasons:
            reasons.insert(0, f"⚠ High trap risk: {trap_reasons[0]}")
    elif abs_score >= 60:
        signal = "EXECUTE" if trap_score <= 50 else "WATCH"
    elif abs_score >= 30:
        signal = "WATCH"
    else:
        signal = "NO_TRADE"

    if final_score > 15: label = "Bullish"
    elif final_score < -15: label = "Bearish"
    else: label = "Neutral"

    component_summary = ", ".join(f"{c['label']}={c['score']}" for c in components)
    logger.debug(
        f"Signal scoring: raw={raw_score:.2f}/18 → base={base_score:.1f} → score={final_score} "
        f"({label}, {signal}, trap={trap_score}, short_gamma={_is_short_gamma}) | {component_summary}"
    )

    return {
        "signal": signal,
        "score": final_score,
        "label": label,
        "trap_score": trap_score,
        "trap_reasons": trap_reasons,
        "reasons": list(set(reasons)),
        "components": components,
        "data_quality": data_quality or {},
        "bias_scores": {
            "market": s10 * 40, # Map to legacy dashboard categories
            "oi": (s4+s5+s6+s7) * 2.5,
            "greeks": s9 * 20,
            "straddle": s3 * 20
        }
    }


def get_buyer_edge_data(
    underlying: str, 
    exchange: str, 
    expiry_date: str, 
    strike_count: int, 
    api_key: str, 
    lb_bars: int = 20, 
    lb_tf: str = "3m",
    atm_mode: str = "auto",
    manual_strike: float | None = None,
    pcr_series: list[dict] = None,
    gex_levels: dict | None = None,
    ivr_data: dict | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Main calculation engine."""
    try:
        # Resolve effective ATM strike for cache/snapshot key
        # We need the spot first to determine auto-ATM if not manual
        # but the snapshot depends on the strike.
        # Let's use a temporary key for initial data fetch, then update it.
        temp_cache_key = (underlying.upper(), exchange.upper(), expiry_date.upper())

        opt_exch = exchange.upper()
        if opt_exch in ("NSE_INDEX", "NSE"): opt_exch = "NFO"
        elif opt_exch in ("BSE_INDEX", "BSE"): opt_exch = "BFO"

        success, resp, status = get_option_chain(underlying, exchange, expiry_date, strike_count, api_key)
        if not success: return False, resp, status

        chain = resp.get("chain", [])
        spot = resp.get("underlying_ltp", 0)
        
        # Determine effective ATM
        if atm_mode == "manual" and manual_strike:
            atm = float(manual_strike)
        else:
            atm = resp.get("atm_strike", 0)
            if not atm and chain:
                # Fallback: Find closest strike to spot in the chain
                try:
                    atm = min(chain, key=lambda x: abs(x["strike"] - spot))["strike"]
                except: atm = 0

        if not atm:
            return False, {"status": "error", "message": "Failed to determine ATM strike"}, 400

        # Finalize cache key with ATM strike
        cache_key = (underlying.upper(), exchange.upper(), expiry_date.upper(), atm)
        prev_snapshot = _get_snapshot(cache_key)

        # History for Market State (Robust lookback for weekends/holidays)
        start_date, end_date = _resolve_trading_window(3, IST) # 3 days buffer
        s_u, r_u, _ = get_history(underlying.upper(), get_buyer_edge_quote_exchange(underlying, exchange), lb_tf, start_date, end_date, api_key)
        
        closes = []
        if s_u and r_u.get("data"):
            closes = [float(c["close"]) for c in r_u["data"]][-lb_bars:]

        if not closes:
            logger.warning(
                f"No history candles for {underlying}@{exchange} [{lb_tf}] "
                f"(window: {start_date}→{end_date}) — market may be closed; "
                "using spot price as sole input for market state"
            )

        market_state = _compute_market_state(closes or [spot])
        oi_result = _compute_oi_intelligence(chain, prev_snapshot)
        greeks_result = _compute_greeks_engine(chain, spot, opt_exch, prev_snapshot)
        
        # Simple straddle calculation for signal
        atm_ce = next((i.get("ce", {}) for i in chain if i["strike"] == atm), {})
        atm_pe = next((i.get("pe", {}) for i in chain if i["strike"] == atm), {})
        s_price = (atm_ce.get("ltp", 0) or 0) + (atm_pe.get("ltp", 0) or 0)
        
        s_vel = "Flat"
        if prev_snapshot and prev_snapshot.get("straddle_price"):
            p_sp = prev_snapshot["straddle_price"]
            if s_price > p_sp * 1.02: s_vel = "Expanding"
            elif s_price < p_sp * 0.98: s_vel = "Contracting"

        straddle_result = {
            "atm_strike": atm,
            "atm_ce_ltp": round(atm_ce.get("ltp", 0) or 0, 2),
            "atm_pe_ltp": round(atm_pe.get("ltp", 0) or 0, 2),
            "straddle_price": round(s_price, 2), "straddle_velocity": s_vel,
            "upper_be": round(atm + s_price, 2),
            "lower_be": round(atm - s_price, 2),
            "be_distance_pct": round(min(abs(spot - (atm + s_price)), abs(spot - (atm - s_price))) / spot * 100, 2) if spot > 0 else 99
        }

        # Build data quality flags — surface availability of each enrichment
        # source so the signal engine can reduce confidence when data is missing
        # rather than silently omitting a scoring dimension.
        greeks_ok = bool(greeks_result.get("delta_imbalance") != 0 or greeks_result.get("gamma_regime") == "Expansion")
        iv_ok = bool(ivr_data and ivr_data.get("iv_rank") is not None)
        gex_fresh = bool(gex_levels and gex_levels.get("gamma_flip") is not None)
        pcr_bars = len(pcr_series) if pcr_series else 0
        market_open = bool(closes)  # False if history returned nothing (market closed / holiday)

        # Synthetic future context (bid/ask-aware, no extra API call)
        syn_ctx = _compute_synthetic_future_context(chain, spot, atm, pcr_series)

        data_quality = {
            "greeks_available": greeks_ok,
            "iv_available": iv_ok,
            "gex_cache_fresh": gex_fresh,
            "pcr_series_bars": pcr_bars,
            "market_open": market_open,
            "synthetic_available": syn_ctx.get("synthetic_ltp") is not None,
            "synthetic_bidask_available": syn_ctx.get("synthetic_mid") is not None,
            "synthetic_liquidity_status": syn_ctx.get("liquidity_status"),
            "synthetic_spread_pct": syn_ctx.get("spread_pct"),
            "synthetic_ltp_inside_market": syn_ctx.get("ltp_inside_market"),
        }

        signal = _compute_signal(market_state, oi_result, greeks_result, straddle_result, pcr_series=pcr_series, gex_levels=gex_levels, ivr_data=ivr_data, data_quality=data_quality, synthetic_engine=syn_ctx)
        
        # Save snapshot
        _set_snapshot(cache_key, {
            "call_oi": oi_result.pop("_call_oi", {}), "put_oi": oi_result.pop("_put_oi", {}),
            "call_wall": oi_result["call_wall"], "put_wall": oi_result["put_wall"],
            "delta_imbalance": greeks_result["delta_imbalance"], "straddle_price": s_price
        })

        return True, {
            "status": "success", "underlying": underlying.upper(), "spot": spot,
            "timestamp": get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST"),
            "market_state": market_state, "oi_intelligence": oi_result,
            "greeks_engine": greeks_result, "straddle_engine": straddle_result,
            "synthetic_engine": syn_ctx, "signal_engine": signal
        }, 200

    except Exception as e:
        logger.exception(f"Error in get_buyer_edge_data: {e}")
        return False, {"status": "error", "message": str(e)}, 500

