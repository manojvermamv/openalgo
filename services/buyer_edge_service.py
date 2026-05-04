"""
Buyer Edge Service
State engine that answers one question: "Are sellers still in control,
or are they being forced to reprice?"

Outputs one of three signals: NO_TRADE / WATCH / EXECUTE

Five layered modules:
  Module 1 — Market State Engine  (structure override)
  Module 2 — OI Intelligence      (call/put walls + migration)
  Module 3 — Greeks Engine        (delta imbalance + gamma regime)
  Module 4 — Straddle Engine      (ATM straddle + BE bands + rolling)
  Module 5 — Signal Engine        (score-based decision engine)

Snapshot cache keyed by (underlying, exchange, expiry) with TTL=300s
enables velocity calculations (ΔOI, ΔDelta, ΔStraddle) across calls.
A separate history-fallback cache stores the last successful closes
series for up to 12 h so market-closed / holiday calls still produce
valid market-state output.
"""

import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import pytz

from services.buyer_edge_utils import get_buyer_edge_quote_exchange
from services.history_service import get_history
from services.option_chain_service import get_option_chain
from services.option_greeks_service import (
    DEFAULT_INTEREST_RATES,
    parse_option_symbol,
    calculate_time_to_expiry,
)
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Snapshot cache — LRU-bounded module-level dict, no DB writes
# TTL raised to 300 s so velocity metrics survive manual-refresh gaps and
# the intraday lunch pause without resetting to "Stable".
# ---------------------------------------------------------------------------
_SNAPSHOT_MAX = 20
_SNAPSHOT_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_SNAPSHOT_TTL = 300  # seconds (5 minutes)

# History fallback cache — stores last successful closes list per cache_key
# with a 12-hour TTL so holiday / market-closed calls can still compute a
# meaningful Market State instead of falling back to closes=[spot_price].
_HIST_FALLBACK_MAX = 20
_HIST_FALLBACK_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_HIST_FALLBACK_TTL = 12 * 3600  # seconds (12 hours)


def _get_snapshot(key: tuple) -> dict | None:
    entry = _SNAPSHOT_CACHE.get(key)
    if entry and (time.monotonic() - entry["ts"]) < _SNAPSHOT_TTL:
        # Move to end (most-recently-used)
        _SNAPSHOT_CACHE.move_to_end(key)
        return entry["data"]
    return None


def _set_snapshot(key: tuple, data: dict) -> None:
    _SNAPSHOT_CACHE[key] = {"ts": time.monotonic(), "data": data}
    _SNAPSHOT_CACHE.move_to_end(key)
    # Evict oldest entry when over capacity
    while len(_SNAPSHOT_CACHE) > _SNAPSHOT_MAX:
        _SNAPSHOT_CACHE.popitem(last=False)


def _get_hist_fallback(key: tuple) -> list[float] | None:
    entry = _HIST_FALLBACK_CACHE.get(key)
    if entry and (time.monotonic() - entry["ts"]) < _HIST_FALLBACK_TTL:
        _HIST_FALLBACK_CACHE.move_to_end(key)
        return entry["data"]
    return None


def _set_hist_fallback(key: tuple, closes: list[float]) -> None:
    _HIST_FALLBACK_CACHE[key] = {"ts": time.monotonic(), "data": closes}
    _HIST_FALLBACK_CACHE.move_to_end(key)
    while len(_HIST_FALLBACK_CACHE) > _HIST_FALLBACK_MAX:
        _HIST_FALLBACK_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Helper: resolve trading window for history calls
# ---------------------------------------------------------------------------
# Approximate trading minutes per bar for each supported interval
_MINUTES_PER_BAR: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
}
_TRADING_MINUTES_PER_DAY = 375  # 9:15 – 15:30 IST


def _trading_window(bars: int = 20, interval: str = "5m") -> tuple[str, str]:
    """Return (start_date, end_date) strings that cover at least `bars` candles.

    Calculates the minimum calendar-day window needed based on the interval
    and requested bar count, adding a 3× buffer to account for weekends and
    market holidays.
    """
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()

    minutes_per_bar = _MINUTES_PER_BAR.get(interval, 5)
    total_trading_minutes = bars * minutes_per_bar
    # Trading days needed (ceil division)
    trading_days = -(-total_trading_minutes // _TRADING_MINUTES_PER_DAY)  # ceil
    # 3× buffer for weekends / holidays, minimum 2 calendar days
    calendar_days = max(2, trading_days * 3)

    start = today - timedelta(days=calendar_days)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Module 1 — Market State Engine
# ---------------------------------------------------------------------------
def _compute_market_state(closes: list[float]) -> dict[str, str]:
    """
    Compute Trend, Regime, and Location from a list of close prices.

    Trend (HH/HL vs LH/LL swing logic):
      Split the close series into a prior half and a recent half, then:
      - Bullish:  Higher High (recent_max > prior_max) AND Higher Low  (recent_min > prior_min)
      - Bearish:  Lower High  (recent_max < prior_max) AND Lower Low   (recent_min < prior_min)
      - Neutral:  Neither condition fully met (sideways)

    Regime (range vs expansion):
      - Compare recent volatility window to longer-term window.
      - Compression: recent range < 0.4 × longer range
      - Expansion:   recent range > 0.7 × longer range
      - Neutral range: in between

    Location relative to the observed price range:
      - Breakout:   price is beyond the historical high or low
      - Range High: price is in the top 20 % of the range
      - Range Low:  price is in the bottom 20 % of the range
      - Mid:        everything else
    """
    if len(closes) < 5:
        return {"trend": "Neutral", "regime": "Compression", "location": "Mid"}

    last = closes[-1]

    # --- Trend: HH+HL = Bullish, LH+LL = Bearish, neither = Neutral ---
    mid = len(closes) // 2
    prior = closes[:mid]
    recent = closes[mid:]

    prior_high = max(prior)
    prior_low = min(prior)
    recent_high = max(recent)
    recent_low = min(recent)

    if recent_high > prior_high and recent_low > prior_low:
        trend = "Bullish"
    elif recent_high < prior_high and recent_low < prior_low:
        trend = "Bearish"
    else:
        trend = "Neutral"

    # --- Regime ---
    recent_window = closes[-5:]
    full_window = closes
    recent_range = max(recent_window) - min(recent_window)
    full_range = max(full_window) - min(full_window)

    if full_range == 0:
        regime = "Compression"
    else:
        ratio = recent_range / full_range
        if ratio < 0.40:
            regime = "Compression"
        elif ratio > 0.70:
            regime = "Expansion"
        else:
            # Ratio 0.40–0.70: ambiguous transition zone — expose as "Neutral"
            # so the signal engine can treat it as WATCH-eligible instead of
            # discarding it as Compression.
            regime = "Neutral"

    # --- Location ---
    range_low = min(full_window)
    range_high = max(full_window)
    span = range_high - range_low

    if span == 0:
        location = "Mid"
    elif last > range_high:
        location = "Breakout"
    elif last < range_low:
        location = "Breakout"
    elif last >= range_high - 0.20 * span:
        location = "Range High"
    elif last <= range_low + 0.20 * span:
        location = "Range Low"
    else:
        location = "Mid"

    return {"trend": trend, "regime": regime, "location": location}


# ---------------------------------------------------------------------------
# Module 2 — OI Intelligence
# ---------------------------------------------------------------------------
def _compute_oi_intelligence(
    chain: list[dict],
    prev_snapshot: dict | None,
) -> dict[str, Any]:
    """
    Call wall, put wall, OI migration, OI roll detection.
    """
    call_oi: dict[float, int] = {}
    put_oi: dict[float, int] = {}

    for item in chain:
        strike = item["strike"]
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}
        call_oi[strike] = ce.get("oi", 0) or 0
        put_oi[strike] = pe.get("oi", 0) or 0

    call_wall = max(call_oi, key=call_oi.get, default=0) if call_oi else 0
    put_wall = max(put_oi, key=put_oi.get, default=0) if put_oi else 0

    # OI migration (any strike changed by >10 %)
    oi_migrating = False
    migration_direction = "Stable"

    if prev_snapshot:
        prev_call_oi = prev_snapshot.get("call_oi", {})
        prev_put_oi = prev_snapshot.get("put_oi", {})
        call_oi_delta = sum(
            abs(call_oi.get(s, 0) - prev_call_oi.get(s, 0))
            for s in set(call_oi) | set(prev_call_oi)
        )
        put_oi_delta = sum(
            abs(put_oi.get(s, 0) - prev_put_oi.get(s, 0))
            for s in set(put_oi) | set(prev_put_oi)
        )
        total_oi = sum(call_oi.values()) + sum(put_oi.values())
        if total_oi > 0:
            migration_pct = (call_oi_delta + put_oi_delta) / total_oi
            if migration_pct > 0.05:  # >5 % aggregate shift
                oi_migrating = True
                if call_oi_delta > put_oi_delta:
                    migration_direction = "Call-heavy"
                else:
                    migration_direction = "Put-heavy"

        # OI roll: has the call_wall or put_wall strike shifted by at least
        # one full strike interval? A single-tick noise shift (< 1 interval)
        # is ignored to avoid phantom roll signals.
        prev_call_wall = prev_snapshot.get("call_wall", 0)
        prev_put_wall = prev_snapshot.get("put_wall", 0)
        # Compute the minimum strike step from available strikes
        sorted_strikes = sorted(call_oi.keys())
        strike_step = (
            (sorted_strikes[-1] - sorted_strikes[0]) / max(len(sorted_strikes) - 1, 1)
            if len(sorted_strikes) >= 2
            else 0
        )
        min_roll_distance = max(strike_step, 1)  # at least 1 point even if step=0
        call_wall_shifted = abs(call_wall - prev_call_wall) >= min_roll_distance
        put_wall_shifted = abs(put_wall - prev_put_wall) >= min_roll_distance
        oi_roll_detected = call_wall_shifted or put_wall_shifted
    else:
        oi_roll_detected = False

    return {
        "call_wall": call_wall,
        "put_wall": put_wall,
        "oi_migrating": oi_migrating,
        "migration_direction": migration_direction,
        "oi_roll_detected": oi_roll_detected,
        # stored for next call's velocity calc
        "_call_oi": call_oi,
        "_put_oi": put_oi,
    }


# ---------------------------------------------------------------------------
# Module 3 — Greeks Engine
# ---------------------------------------------------------------------------
def _batch_black76_greeks(
    chain: list[dict],
    spot_price: float,
    options_exchange: str,
) -> dict[str, dict[str, float]]:
    """
    Compute Black-76 delta and gamma for every CE/PE entry in the chain
    in a single pass, sharing the TTE calculation across all strikes.

    Returns a dict keyed by option_symbol → {"delta": float, "gamma": float}.

    By parsing the expiry and computing TTE only once (all options on the same
    chain share the same expiry), and by calling py_vollib directly instead of
    routing through the calculate_greeks() service wrapper, this reduces the
    O(2·N) function-call overhead (parse, TTE, IV, Greeks) to O(1) fixed setup
    + O(N) tight inner-loop iterations.
    """
    result: dict[str, dict[str, float]] = {}

    try:
        from py_vollib.black.greeks.analytical import delta as black_delta
        from py_vollib.black.greeks.analytical import gamma as black_gamma
        from py_vollib.black.implied_volatility import implied_volatility as black_iv
    except ImportError:
        return result

    # Find the first valid symbol to parse expiry (shared across entire chain)
    expiry_dt = None
    interest_rate = DEFAULT_INTEREST_RATES.get(options_exchange, 0) / 100.0
    for item in chain:
        for side in ("ce", "pe"):
            entry = item.get(side) or {}
            sym = entry.get("symbol")
            if sym:
                try:
                    _, expiry_dt, _, _ = parse_option_symbol(sym, options_exchange)
                    break
                except Exception:
                    continue
        if expiry_dt:
            break

    if expiry_dt is None:
        return result

    # Compute TTE once — shared for all strikes
    time_to_expiry_years, _ = calculate_time_to_expiry(expiry_dt)
    if time_to_expiry_years <= 0:
        return result

    for item in chain:
        for side, flag in (("ce", "c"), ("pe", "p")):
            entry = item.get(side) or {}
            sym = entry.get("symbol")
            ltp = entry.get("ltp", 0) or 0
            if not sym or ltp <= 0:
                continue
            try:
                _, _, strike, _ = parse_option_symbol(sym, options_exchange)
                intrinsic = max(spot_price - strike, 0) if flag == "c" else max(strike - spot_price, 0)
                time_value = ltp - intrinsic
                if time_value <= 0:
                    # Deep ITM: use theoretical values
                    result[sym] = {"delta": 1.0 if flag == "c" else -1.0, "gamma": 0.0}
                    continue
                iv = black_iv(ltp, spot_price, strike, interest_rate, time_to_expiry_years, flag)
                if not iv or iv <= 0:
                    continue
                d = black_delta(flag, spot_price, strike, time_to_expiry_years, interest_rate, iv)
                g = black_gamma(flag, spot_price, strike, time_to_expiry_years, interest_rate, iv)
                result[sym] = {"delta": float(d or 0), "gamma": float(g or 0)}
            except Exception as exc:
                logger.debug(f"_batch_black76_greeks {sym}: {exc}")

    return result


def _compute_greeks_engine(
    chain: list[dict],
    spot_price: float,
    options_exchange: str,
    prev_snapshot: dict | None,
) -> dict[str, Any]:
    """
    Delta imbalance (with velocity) and gamma regime.
    Uses a single-pass batch Black-76 computation shared across all strikes
    to avoid per-strike parse/TTE overhead.
    """
    total_call_delta = 0.0
    total_put_delta = 0.0
    net_gamma = 0.0  # sum(gamma * OI * lotsize) — same as GEX

    # Pre-compute all greeks in one pass (3.1 optimisation)
    greeks_map = _batch_black76_greeks(chain, spot_price, options_exchange)

    for item in chain:
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}

        ce_ltp = ce.get("ltp", 0) or 0
        pe_ltp = pe.get("ltp", 0) or 0
        ce_oi = ce.get("oi", 0) or 0
        pe_oi = pe.get("oi", 0) or 0
        lot_size = ce.get("lotsize", 1) or pe.get("lotsize", 1) or 1

        if ce.get("symbol") and ce_ltp > 0 and ce_oi > 0:
            g = greeks_map.get(ce["symbol"], {})
            total_call_delta += g.get("delta", 0) or 0
            net_gamma += (g.get("gamma", 0) or 0) * ce_oi * lot_size

        if pe.get("symbol") and pe_ltp > 0 and pe_oi > 0:
            g = greeks_map.get(pe["symbol"], {})
            total_put_delta += g.get("delta", 0) or 0
            net_gamma -= (g.get("gamma", 0) or 0) * pe_oi * lot_size

    # Put deltas from Black-76 are negative by convention; summing CE and PE
    # deltas directly gives the net directional imbalance.
    # Positive → more call (bullish) pressure; negative → more put pressure.
    delta_imbalance = round(total_call_delta + total_put_delta, 4)
    gamma_regime = "Expansion" if net_gamma < 0 else "Mean-Reversion"

    # Delta velocity
    delta_velocity = "Stable"
    if prev_snapshot:
        prev_di = prev_snapshot.get("delta_imbalance", 0)
        change = delta_imbalance - prev_di
        if abs(change) > 0.05:
            delta_velocity = "Rising" if change > 0 else "Falling"

    return {
        "total_call_delta": round(total_call_delta, 4),
        "total_put_delta": round(total_put_delta, 4),
        "delta_imbalance": delta_imbalance,
        "delta_velocity": delta_velocity,
        "gamma_regime": gamma_regime,
        "net_gamma": round(net_gamma, 2),
    }


# ---------------------------------------------------------------------------
# Module 4 — Straddle Engine
# ---------------------------------------------------------------------------
def _compute_straddle_engine(
    chain: list[dict],
    atm_strike: float,
    spot_price: float,
    oi_result: dict,
    prev_snapshot: dict | None,
) -> dict[str, Any]:
    """
    ATM straddle price, velocity, breakeven bands, BE distance, rolling detection.
    """
    atm_ce_ltp = 0.0
    atm_pe_ltp = 0.0

    for item in chain:
        if item["strike"] == atm_strike:
            ce = item.get("ce") or {}
            pe = item.get("pe") or {}
            atm_ce_ltp = ce.get("ltp", 0) or 0
            atm_pe_ltp = pe.get("ltp", 0) or 0
            break

    straddle_price = round(atm_ce_ltp + atm_pe_ltp, 2)
    upper_be = round(atm_strike + straddle_price, 2)
    lower_be = round(atm_strike - straddle_price, 2)

    # Distance of spot from nearest BE (as % of spot)
    dist_upper = abs(spot_price - upper_be)
    dist_lower = abs(spot_price - lower_be)
    nearest_be_dist = min(dist_upper, dist_lower)
    be_distance_pct = round((nearest_be_dist / spot_price) * 100, 4) if spot_price > 0 else 99.0

    # Straddle velocity
    straddle_velocity = "Flat"
    if prev_snapshot:
        prev_sp = prev_snapshot.get("straddle_price", straddle_price)
        if prev_sp > 0:
            change_pct = (straddle_price - prev_sp) / prev_sp
            if change_pct > 0.02:
                straddle_velocity = "Expanding"
            elif change_pct < -0.02:
                straddle_velocity = "Contracting"

    return {
        "atm_strike": atm_strike,
        "atm_ce_ltp": round(atm_ce_ltp, 2),
        "atm_pe_ltp": round(atm_pe_ltp, 2),
        "straddle_price": straddle_price,
        "straddle_velocity": straddle_velocity,
        "upper_be": upper_be,
        "lower_be": lower_be,
        "be_distance_pct": be_distance_pct,
        "oi_roll_detected": oi_result.get("oi_roll_detected", False),
    }


# ---------------------------------------------------------------------------
# Module 5 — Signal Engine
# ---------------------------------------------------------------------------
def _compute_signal(
    market_state: dict,
    oi_result: dict,
    greeks_result: dict,
    straddle_result: dict,
) -> dict[str, Any]:
    """
    Score-based signal engine → NO_TRADE / WATCH / EXECUTE

    EXECUTE conditions (score 0–4):
      1. Structure break   (location = Breakout)
      2. Straddle expanding
      3. OI roll detected  (wall shifted by ≥ 1 strike interval)
      4. Delta velocity rising

      EXECUTE if ≥ 3 / 4 conditions met  (changed from strict all-4)

    WATCH conditions (score 0–4):
      1. Price near BE (be_distance_pct < 0.5 %)
      2. Delta velocity rising
      3. Straddle velocity = "Expanding"  (Flat no longer counts)
      4. Regime ∈ {Expansion, Neutral} OR location ∈ {Range High, Range Low}

      WATCH if execute_conditions >= 2 OR watch_score >= 3

    NO_TRADE (default / seller comfort):
      Regime = Compression AND no significant execute or watch conditions.

    confidence = count of EXECUTE conditions met (0–4)
    """
    location = market_state.get("location", "Mid")
    regime = market_state.get("regime", "Compression")
    straddle_velocity = straddle_result.get("straddle_velocity", "Flat")
    oi_roll = straddle_result.get("oi_roll_detected", False)
    delta_velocity = greeks_result.get("delta_velocity", "Stable")
    oi_migrating = oi_result.get("oi_migrating", False)
    be_distance_pct = straddle_result.get("be_distance_pct", 99.0)

    reasons: list[str] = []
    execute_conditions = 0

    # EXECUTE conditions
    if location == "Breakout":
        execute_conditions += 1
        reasons.append("Structure breakout detected")
    if straddle_velocity == "Expanding":
        execute_conditions += 1
        reasons.append("Straddle premium expanding")
    if oi_roll:
        execute_conditions += 1
        reasons.append("OI roll detected (sellers repositioning)")
    if delta_velocity == "Rising":
        execute_conditions += 1
        reasons.append("Delta imbalance rising (hedging pressure building)")

    # EXECUTE: ≥ 3/4 conditions (relaxed from strict 4/4)
    if execute_conditions >= 3:
        return {"signal": "EXECUTE", "confidence": execute_conditions, "reasons": reasons}

    # WATCH conditions (separate scoring)
    watch_score = 0
    watch_reasons: list[str] = []
    if be_distance_pct < 0.5:
        watch_score += 1
        watch_reasons.append(f"Price near breakeven ({be_distance_pct:.2f}% away)")
    if delta_velocity == "Rising":
        watch_score += 1
        watch_reasons.append("Delta imbalance rising")
    # "Flat" no longer counts — only active expansion is a valid WATCH signal
    if straddle_velocity == "Expanding":
        watch_score += 1
        watch_reasons.append("Straddle premium expanding")
    # Neutral regime is now WATCH-eligible (previously only Expansion qualified)
    if regime in ("Expansion", "Neutral") or location in ("Range High", "Range Low"):
        watch_score += 1
        watch_reasons.append(f"Regime={regime}, Location={location}")

    # WATCH if ≥ 2 execute conditions already met, or watch score ≥ 3
    if execute_conditions >= 2 or watch_score >= 3:
        signal = "WATCH"
        confidence = max(execute_conditions, watch_score)
        return {"signal": signal, "confidence": confidence, "reasons": watch_reasons or reasons}

    # NO_TRADE default
    no_trade_reasons: list[str] = []
    if regime == "Compression":
        no_trade_reasons.append("Market in compression (sellers in control)")
    if straddle_velocity == "Contracting" and not oi_migrating:
        no_trade_reasons.append("Straddle contracting, OI stable (theta trap)")
    if not no_trade_reasons:
        no_trade_reasons.append("Insufficient conditions for WATCH or EXECUTE")

    return {
        "signal": "NO_TRADE",
        "confidence": execute_conditions,
        "reasons": no_trade_reasons,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def get_buyer_edge_data(
    underlying: str,
    exchange: str,
    expiry_date: str,
    strike_count: int,
    api_key: str,
    lb_bars: int = 20,
    lb_tf: str = "3m",
) -> tuple[bool, dict[str, Any], int]:
    """
    Compute all 5 modules and return a buyer-edge signal.

    Args:
        underlying:   Underlying symbol (e.g., NIFTY, BANKNIFTY)
        exchange:     Exchange (e.g., NSE_INDEX, NFO, BSE_INDEX, BFO)
        expiry_date:  Expiry in DDMMMYY format (e.g., 06FEB26)
        strike_count: Strikes around ATM (default 10 is enough for greeks)
        api_key:      OpenAlgo API key
        lb_bars:      Number of historical bars to use for Market State (default 20)
        lb_tf:        Timeframe for historical bars (default "3m")

    Returns:
        Tuple of (success, response_dict, status_code)
    """
    try:
        cache_key = (underlying.upper(), exchange.upper(), expiry_date.upper())
        prev_snapshot = _get_snapshot(cache_key)

        # --- Determine options exchange for Greeks ---
        options_exchange = exchange.upper()
        if options_exchange in ("NSE_INDEX", "NSE"):
            options_exchange = "NFO"
        elif options_exchange in ("BSE_INDEX", "BSE"):
            options_exchange = "BFO"

        # --- Fetch option chain (single broker call) ---
        success, chain_response, status_code = get_option_chain(
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry_date,
            strike_count=strike_count,
            api_key=api_key,
        )
        if not success:
            return False, chain_response, status_code

        chain = chain_response.get("chain", [])
        atm_strike = chain_response.get("atm_strike", 0)
        spot_price = chain_response.get("underlying_ltp", 0) or 0

        if not spot_price:
            return False, {"status": "error", "message": "Could not determine spot price"}, 500

        # --- Fetch candle history for Market State ---
        # data_mode tracks the quality of the closes series for the frontend badge.
        start_date, end_date = _trading_window(bars=lb_bars, interval=lb_tf)
        closes: list[float] = []
        data_mode = "spot_only"  # default — will be upgraded below

        base_symbol = underlying.upper()
        hist_exchange = get_buyer_edge_quote_exchange(base_symbol, exchange)

        success_h, resp_h, _ = get_history(
            symbol=base_symbol,
            exchange=hist_exchange,
            interval=lb_tf,
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
        )
        if success_h:
            rows = resp_h.get("data", [])
            fresh_closes = [float(r["close"]) for r in rows if r.get("close") is not None]
            if fresh_closes:
                closes = fresh_closes[-lb_bars:] if len(fresh_closes) > lb_bars else fresh_closes
                data_mode = "realtime"
                # Persist for future holiday / closed-market fallback
                _set_hist_fallback(cache_key, closes)

        if not closes:
            # Try 12-hour history fallback (holiday / market closed / broker down)
            fallback_closes = _get_hist_fallback(cache_key)
            if fallback_closes:
                closes = fallback_closes
                data_mode = "last_day_fallback"
            else:
                # Last resort: single-point spot — all structure calculations trivial
                closes = [spot_price]
                data_mode = "spot_only"

        # --- Run the 5 modules ---
        market_state = _compute_market_state(closes)

        oi_result = _compute_oi_intelligence(chain, prev_snapshot)

        greeks_result = _compute_greeks_engine(chain, spot_price, options_exchange, prev_snapshot)

        straddle_result = _compute_straddle_engine(
            chain, atm_strike, spot_price, oi_result, prev_snapshot
        )

        signal_result = _compute_signal(market_state, oi_result, greeks_result, straddle_result)
        # Attach data_mode so frontend can display an appropriate badge
        signal_result["data_mode"] = data_mode

        # --- Save new snapshot for next call ---
        new_snapshot = {
            "call_oi": oi_result.pop("_call_oi", {}),
            "put_oi": oi_result.pop("_put_oi", {}),
            "call_wall": oi_result["call_wall"],
            "put_wall": oi_result["put_wall"],
            "delta_imbalance": greeks_result["delta_imbalance"],
            "straddle_price": straddle_result["straddle_price"],
            "spot_price": spot_price,
        }
        _set_snapshot(cache_key, new_snapshot)

        ist = pytz.timezone("Asia/Kolkata")
        timestamp = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")

        return (
            True,
            {
                "status": "success",
                "underlying": base_symbol,
                "expiry_date": expiry_date.upper(),
                "spot": spot_price,
                "timestamp": timestamp,
                "market_state": market_state,
                "oi_intelligence": oi_result,
                "greeks_engine": greeks_result,
                "straddle_engine": straddle_result,
                "signal_engine": signal_result,
            },
            200,
        )

    except Exception as exc:
        logger.exception(f"Error in get_buyer_edge_data: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500

