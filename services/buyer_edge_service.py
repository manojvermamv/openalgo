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
  Module 5 — Signal Engine        (deterministic decision tree)

Snapshot cache keyed by (underlying, exchange, expiry) with TTL=60s
enables velocity calculations (ΔOI, ΔDelta, ΔStraddle) across calls.
"""

import time
from datetime import datetime, timedelta
from typing import Any

import pytz

from services.history_service import get_history
from services.option_chain_service import get_option_chain
from services.option_greeks_service import calculate_greeks
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Snapshot cache — module-level dict, no DB writes
# ---------------------------------------------------------------------------
_SNAPSHOT_CACHE: dict[tuple, dict] = {}
_SNAPSHOT_TTL = 60  # seconds


def _get_snapshot(key: tuple) -> dict | None:
    entry = _SNAPSHOT_CACHE.get(key)
    if entry and (time.monotonic() - entry["ts"]) < _SNAPSHOT_TTL:
        return entry["data"]
    return None


def _set_snapshot(key: tuple, data: dict) -> None:
    _SNAPSHOT_CACHE[key] = {"ts": time.monotonic(), "data": data}


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

    Trend (HH/HL vs LL/LH swing logic):
      - Bullish:  last_close > prev_close AND last_close > max(closes[:-1])   (simple HH proxy)
      - Bearish:  last_close < prev_close AND last_close < min(closes[:-1])
      - Neutral:  otherwise

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
    prev = closes[-2]

    # --- Trend ---
    highs = closes[:-1]
    lows = closes[:-1]
    max_prev = max(highs)
    min_prev = min(lows)

    if last > prev and last > max_prev:
        trend = "Bullish"
    elif last < prev and last < min_prev:
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
            # Ratio between 0.40 and 0.70: ambiguous — treat as Compression
            # to avoid false signals. EXECUTE requires Expansion + Breakout,
            # so a conservative default reduces noise.
            regime = "Compression"

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

        # OI roll: has the call_wall or put_wall strike shifted?
        prev_call_wall = prev_snapshot.get("call_wall", 0)
        prev_put_wall = prev_snapshot.get("put_wall", 0)
        oi_roll_detected = (call_wall != prev_call_wall) or (put_wall != prev_put_wall)
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
def _compute_greeks_engine(
    chain: list[dict],
    spot_price: float,
    options_exchange: str,
    prev_snapshot: dict | None,
) -> dict[str, Any]:
    """
    Delta imbalance (with velocity) and gamma regime.
    Calls calculate_greeks() for each ATM±3 strike to keep the request fast.
    """
    total_call_delta = 0.0
    total_put_delta = 0.0
    net_gamma = 0.0  # sum(gamma * OI * lotsize) — same as GEX

    for item in chain:
        strike = item["strike"]
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}

        ce_ltp = ce.get("ltp", 0) or 0
        pe_ltp = pe.get("ltp", 0) or 0
        ce_oi = ce.get("oi", 0) or 0
        pe_oi = pe.get("oi", 0) or 0
        lot_size = ce.get("lotsize", 1) or pe.get("lotsize", 1) or 1

        if ce.get("symbol") and ce_ltp > 0 and ce_oi > 0:
            try:
                ok, resp, _ = calculate_greeks(
                    option_symbol=ce["symbol"],
                    exchange=options_exchange,
                    spot_price=spot_price,
                    option_price=ce_ltp,
                )
                if ok and resp.get("status") == "success":
                    g = resp.get("greeks", {})
                    total_call_delta += g.get("delta", 0) or 0
                    net_gamma += (g.get("gamma", 0) or 0) * ce_oi * lot_size
            except Exception as exc:
                logger.debug(f"Greeks CE {ce.get('symbol')}: {exc}")

        if pe.get("symbol") and pe_ltp > 0 and pe_oi > 0:
            try:
                ok, resp, _ = calculate_greeks(
                    option_symbol=pe["symbol"],
                    exchange=options_exchange,
                    spot_price=spot_price,
                    option_price=pe_ltp,
                )
                if ok and resp.get("status") == "success":
                    g = resp.get("greeks", {})
                    total_put_delta += g.get("delta", 0) or 0
                    net_gamma -= (g.get("gamma", 0) or 0) * pe_oi * lot_size
            except Exception as exc:
                logger.debug(f"Greeks PE {pe.get('symbol')}: {exc}")

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
    Deterministic decision tree → NO_TRADE / WATCH / EXECUTE

    Priority:
      EXECUTE (requires all 4 conditions):
        - Structure break (location=Breakout)
        - Straddle expanding
        - OI roll detected
        - Delta velocity rising

      WATCH (3 conditions, no structure break yet):
        - Price near BE (be_distance_pct < 0.5 %)
        - AND (delta_velocity=Rising OR straddle_velocity=Flat/Expanding)
        - AND regime is Expansion OR location is Range High/Low

      NO_TRADE (default / seller comfort):
        - Regime=Compression
        - OR straddle contracting AND no OI migration

    confidence = count of EXECUTE conditions met (0-4)
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

    if execute_conditions == 4:
        signal = "EXECUTE"
        confidence = 4
        return {"signal": signal, "confidence": confidence, "reasons": reasons}

    # WATCH conditions
    watch_score = 0
    watch_reasons: list[str] = []
    if be_distance_pct < 0.5:
        watch_score += 1
        watch_reasons.append(f"Price near breakeven ({be_distance_pct:.2f}% away)")
    if delta_velocity == "Rising":
        watch_score += 1
        watch_reasons.append("Delta imbalance rising")
    if straddle_velocity in ("Flat", "Expanding"):
        watch_score += 1
        watch_reasons.append(f"Straddle premium {straddle_velocity.lower()}")
    if regime == "Expansion" or location in ("Range High", "Range Low"):
        watch_score += 1
        watch_reasons.append(f"Regime={regime}, Location={location}")

    if watch_score >= 3:
        signal = "WATCH"
        confidence = watch_score
        return {"signal": signal, "confidence": confidence, "reasons": watch_reasons}

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
        lb_tf:        Timeframe for historical bars (default "5m")

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

        # --- Fetch candle history for Market State (single broker call) ---
        start_date, end_date = _trading_window(bars=lb_bars, interval=lb_tf)
        closes: list[float] = []

        # Determine correct symbol/exchange for history
        base_symbol = underlying.upper()
        hist_exchange = exchange.upper()
        if hist_exchange in ("NFO", "BFO"):
            # For index options, fetch the index itself
            from services.straddle_chart_service import _get_quote_exchange
            hist_exchange = _get_quote_exchange(base_symbol, exchange)

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
            closes = [float(r["close"]) for r in rows if r.get("close") is not None]
            # Use last lb_bars bars
            closes = closes[-lb_bars:] if len(closes) > lb_bars else closes

        if not closes:
            # Fallback: use spot from chain as a single point
            closes = [spot_price]

        # --- Run the 5 modules ---
        market_state = _compute_market_state(closes)

        oi_result = _compute_oi_intelligence(chain, prev_snapshot)

        greeks_result = _compute_greeks_engine(chain, spot_price, options_exchange, prev_snapshot)

        straddle_result = _compute_straddle_engine(
            chain, atm_strike, spot_price, oi_result, prev_snapshot
        )

        signal_result = _compute_signal(market_state, oi_result, greeks_result, straddle_result)

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
