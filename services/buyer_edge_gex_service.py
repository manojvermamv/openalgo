"""
Buyer Edge — Advanced GEX Levels Service

Computes per-strike Gamma Exposure (GEX) and derives the key market-maker
levels that options buyers use to navigate gamma risk:

  - Net GEX per strike   = CE_GEX − PE_GEX
  - CE_GEX               = gamma * CE_OI * lot_size
  - PE_GEX               = gamma * PE_OI * lot_size  (treated negative in dealer book)
  - Gamma Flip / HVL     = strike where cumulative Net GEX changes sign
  - Call Gamma Wall      = highest positive Net GEX strike above spot
  - Put Gamma Wall       = most negative Net GEX strike below spot
  - Absolute GEX Wall    = strike with highest |Net GEX|

Two modes:
  - Selected (⊙): single expiry supplied via expiry_date
  - Cumulative (∑): multiple expiries supplied via expiry_dates list, GEX
    aggregated across all expiries per strike

No changes to any other service or blueprint.
"""

from typing import Any

from services.option_chain_service import get_option_chain
from services.option_greeks_service import (
    DEFAULT_INTEREST_RATES,
    parse_option_symbol,
    calculate_time_to_expiry,
)
from utils.logging import get_logger
from collections import OrderedDict
import time

logger = get_logger(__name__)

_MAX_EXPIRIES = 4
_GEX_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_GEX_CACHE_MAX = 20
_GEX_CACHE_TTL = 12 * 3600  # 12 hours fallback for market-closed / holidays


def _get_gex_cache(key: tuple) -> dict | None:
    entry = _GEX_CACHE.get(key)
    if entry:
        age_s = time.monotonic() - entry["ts"]
        if age_s < _GEX_CACHE_TTL:
            _GEX_CACHE.move_to_end(key)
            logger.debug(f"GEX cache hit for {key}: age={age_s:.0f}s")
            return entry["data"]
        logger.debug(f"GEX cache expired for {key}: age={age_s:.0f}s > TTL={_GEX_CACHE_TTL}s")
    return None


def _set_gex_cache(key: tuple, data: dict) -> None:
    _GEX_CACHE[key] = {"ts": time.monotonic(), "data": data}
    _GEX_CACHE.move_to_end(key)
    while len(_GEX_CACHE) > _GEX_CACHE_MAX:
        _GEX_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Inline batch Black-76 gamma computation for GEX (3.4 optimisation)
# ---------------------------------------------------------------------------

def _batch_gamma_for_gex(
    chain: list[dict],
    spot_price: float,
    options_exchange: str,
) -> dict[str, float]:
    """
    Compute Black-76 gamma for all CE/PE entries in one pass, sharing TTE.

    Returns a dict: option_symbol → gamma value.
    Falls back to empty dict if py_vollib is unavailable.
    """
    result: dict[str, float] = {}

    try:
        from py_vollib.black.greeks.analytical import gamma as black_gamma
        from py_vollib.black.implied_volatility import implied_volatility as black_iv
    except ImportError:
        return result

    interest_rate = DEFAULT_INTEREST_RATES.get(options_exchange, 0) / 100.0

    # Parse expiry once from the first valid symbol (all options on same chain share it)
    expiry_dt = None
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

    time_to_expiry_years, _ = calculate_time_to_expiry(expiry_dt)
    if time_to_expiry_years <= 0:
        return result

    for item in chain:
        for side, flag in (("ce", "c"), ("pe", "p")):
            entry = item.get(side) or {}
            sym = entry.get("symbol")
            ltp = entry.get("ltp", 0) or 0
            oi = entry.get("oi", 0) or 0
            if not sym or ltp <= 0 or oi <= 0:
                continue
            try:
                _, _, strike, _ = parse_option_symbol(sym, options_exchange)
                intrinsic = max(spot_price - strike, 0) if flag == "c" else max(strike - spot_price, 0)
                if ltp - intrinsic <= 0:
                    result[sym] = 0.0  # deep ITM: gamma effectively 0
                    continue
                iv = black_iv(ltp, spot_price, strike, interest_rate, time_to_expiry_years, flag)
                if not iv or iv <= 0:
                    continue
                g = black_gamma(flag, spot_price, strike, time_to_expiry_years, interest_rate, iv)
                result[sym] = float(g or 0)
            except Exception as exc:
                logger.debug(f"_batch_gamma_for_gex {sym}: {exc}")

    return result


# ---------------------------------------------------------------------------
# Core per-chain GEX computation
# ---------------------------------------------------------------------------

def _compute_gex_from_chain(
    chain: list[dict],
    spot_price: float,
    options_exchange: str,
) -> list[dict]:
    """
    Compute per-strike GEX from a single option chain snapshot.

    Uses the batch gamma helper (_batch_gamma_for_gex) to compute all
    gammas in one pass (shared TTE) instead of N individual service calls.

    Returns a list of dicts:
        strike, ce_oi, pe_oi, ce_gex, pe_gex, net_gex
    """
    gamma_map = _batch_gamma_for_gex(chain, spot_price, options_exchange)
    result = []
    for item in chain:
        strike = item["strike"]
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}

        ce_oi = ce.get("oi", 0) or 0
        pe_oi = pe.get("oi", 0) or 0
        lot_size = ce.get("lotsize") or pe.get("lotsize") or 1

        ce_gamma = gamma_map.get(ce.get("symbol", ""), 0.0) if ce_oi > 0 else 0.0
        pe_gamma = gamma_map.get(pe.get("symbol", ""), 0.0) if pe_oi > 0 else 0.0

        ce_gex = ce_gamma * ce_oi * lot_size
        pe_gex = pe_gamma * pe_oi * lot_size
        net_gex = ce_gex - pe_gex

        result.append(
            {
                "strike": strike,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_gex": round(ce_gex, 2),
                "pe_gex": round(pe_gex, 2),
                "net_gex": round(net_gex, 2),
            }
        )

    return result


def _derive_gex_levels(
    gex_chain: list[dict],
    spot_price: float,
) -> dict[str, Any]:
    """
    Derive Gamma Flip / HVL, Call/Put Gamma Walls, Absolute GEX Wall
    from a sorted (by strike) GEX chain.

    Gamma Flip / HVL: strike at which the cumulative sum of net_gex (from
    lowest strike upward) first crosses zero. Below that strike dealers are
    short-gamma (amplify moves); above it they are long-gamma (dampen moves).

    Returns dict with keys:
        gamma_flip, hvl, call_gamma_wall, put_gamma_wall, absolute_wall,
        total_net_gex, zero_gamma
    """
    if not gex_chain:
        return {
            "gamma_flip": None,
            "hvl": None,
            "call_gamma_wall": None,
            "put_gamma_wall": None,
            "absolute_wall": None,
            "total_net_gex": 0,
            "zero_gamma": None,
        }

    sorted_chain = sorted(gex_chain, key=lambda x: x["strike"])
    total_net_gex = sum(x["net_gex"] for x in sorted_chain)

    # ---------- Gamma Flip (HVL) via cumulative sum sign change ----------
    gamma_flip = None
    cumsum = 0.0
    prev_sign = None
    for item in sorted_chain:
        prev_cumsum = cumsum
        cumsum += item["net_gex"]
        sign = 1 if cumsum >= 0 else -1
        if prev_sign is not None and sign != prev_sign:
            # Linear interpolation between prev strike and this strike
            # to find the exact zero-crossing
            prev_item = sorted_chain[sorted_chain.index(item) - 1]
            if item["net_gex"] != prev_item["net_gex"]:
                frac = -prev_cumsum / (cumsum - prev_cumsum)
                gamma_flip = round(
                    prev_item["strike"]
                    + frac * (item["strike"] - prev_item["strike"]),
                    2,
                )
            else:
                gamma_flip = item["strike"]
            break
        prev_sign = sign

    hvl = gamma_flip  # They refer to the same concept in TanukiTrade

    # ---------- Call Gamma Wall (above spot, max positive Net GEX) ----------
    above_spot = [x for x in sorted_chain if x["strike"] > spot_price and x["net_gex"] > 0]
    call_gamma_wall = (
        max(above_spot, key=lambda x: x["net_gex"])["strike"]
        if above_spot
        else None
    )

    # ---------- Put Gamma Wall (below spot, most negative Net GEX) ----------
    below_spot = [x for x in sorted_chain if x["strike"] < spot_price and x["net_gex"] < 0]
    put_gamma_wall = (
        min(below_spot, key=lambda x: x["net_gex"])["strike"]
        if below_spot
        else None
    )

    # ---------- Absolute Wall (highest |Net GEX|, any strike) ----------
    absolute_wall = (
        max(sorted_chain, key=lambda x: abs(x["net_gex"]))["strike"]
        if sorted_chain
        else None
    )

    # ---------- Gamma Punch Targets (nearest negative net_gex strikes) ----------
    # When spot crosses a strike with net_gex < 0, dealers must delta-hedge
    # aggressively → momentum punch / acceleration zone.
    above_neg = [x for x in sorted_chain if x["strike"] > spot_price and x["net_gex"] < 0]
    below_neg = [x for x in sorted_chain if x["strike"] < spot_price and x["net_gex"] < 0]

    upside_punch_target: float | None = (
        min(above_neg, key=lambda x: x["strike"])["strike"] if above_neg else None
    )
    downside_punch_target: float | None = (
        max(below_neg, key=lambda x: x["strike"])["strike"] if below_neg else None
    )

    # punch_proximity_pct: how close spot is to the nearest punch target expressed
    # as a percentage of the expected move (fallback: 0.5% of spot as proxy).
    # Use the NEAREST of upside and downside targets (not always upside-first).
    punch_proximity_pct: float | None = None
    if spot_price > 0:
        expected_move_fallback = spot_price * 0.005  # 0.5% of spot
        # Pick the closer punch target
        up_dist = abs(upside_punch_target - spot_price) if upside_punch_target is not None else None
        dn_dist = abs(downside_punch_target - spot_price) if downside_punch_target is not None else None
        if up_dist is not None and dn_dist is not None:
            punch_target = upside_punch_target if up_dist <= dn_dist else downside_punch_target
        elif up_dist is not None:
            punch_target = upside_punch_target
        elif dn_dist is not None:
            punch_target = downside_punch_target
        else:
            punch_target = None
        if punch_target is not None and expected_move_fallback > 0:
            punch_dist = abs(punch_target - spot_price)
            punch_proximity_pct = round(punch_dist / expected_move_fallback * 100, 1)

    return {
        "gamma_flip": gamma_flip,
        "hvl": hvl,
        "call_gamma_wall": call_gamma_wall,
        "put_gamma_wall": put_gamma_wall,
        "absolute_wall": absolute_wall,
        "total_net_gex": round(total_net_gex, 2),
        "zero_gamma": gamma_flip,  # alias used by some charting libs
        "upside_punch_target": upside_punch_target,
        "downside_punch_target": downside_punch_target,
        "punch_proximity_pct": punch_proximity_pct,
    }


# ---------------------------------------------------------------------------
# Public API: single expiry
# ---------------------------------------------------------------------------

def get_gex_levels(
    underlying: str,
    exchange: str,
    expiry_date: str,
    strike_count: int,
    api_key: str,
) -> tuple[bool, dict[str, Any], int]:
    """
    Compute Advanced GEX Levels for a single expiry.

    Returns:
        (success, response_dict, status_code)
    """
    try:
        cache_key = (underlying.upper(), exchange.upper(), expiry_date.upper(), strike_count)
        options_exchange = exchange.upper()
        if options_exchange in ("NSE_INDEX", "NSE"):
            options_exchange = "NFO"
        elif options_exchange in ("BSE_INDEX", "BSE"):
            options_exchange = "BFO"

        success, chain_resp, sc = get_option_chain(
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry_date,
            strike_count=strike_count,
            api_key=api_key,
        )

        if not success:
            # Fallback to cache if live fetch fails (market closed / holiday)
            fallback = _get_gex_cache(cache_key)
            if fallback:
                logger.info(
                    f"GEX [{underlying}|{expiry_date}] live fetch failed — "
                    "serving last-known data (market-closed / holiday / broker error)"
                )
                # Mark as fallback in response
                fallback["data_mode"] = "last_day_fallback"
                return True, fallback, 200
            return False, chain_resp, sc

        chain = chain_resp.get("chain", [])
        spot_price = chain_resp.get("underlying_ltp", 0) or 0

        if not chain or not spot_price:
            # Fallback to cache if live data is empty (market closed but call succeeded)
            fallback = _get_gex_cache(cache_key)
            if fallback:
                logger.info(
                    f"GEX [{underlying}|{expiry_date}] empty chain/spot "
                    "(chain={len(chain)}, spot={spot_price}) — "
                    "serving last-known data; market may be closed"
                )
                fallback["data_mode"] = "last_day_fallback"
                return True, fallback, 200
            
            if not spot_price:
                return False, {"status": "error", "message": "Could not determine spot price"}, 500
            return False, {"status": "error", "message": "No option chain data available"}, 404

        gex_chain = _compute_gex_from_chain(chain, spot_price, options_exchange)
        levels = _derive_gex_levels(gex_chain, spot_price)

        logger.debug(
            f"GEX [{underlying}|{expiry_date}] live: {len(gex_chain)} strikes, "
            f"spot={spot_price}, gamma_flip={levels.get('gamma_flip')}, "
            f"total_net_gex={levels.get('total_net_gex')}"
        )

        response = {
            "status": "success",
            "mode": "selected",
            "underlying": underlying.upper(),
            "expiry_date": expiry_date.upper(),
            "spot_price": spot_price,
            "expiries_used": [expiry_date.upper()],
            "chain": gex_chain,
            "levels": levels,
            "data_mode": "realtime",
        }
        _set_gex_cache(cache_key, response)

        return (True, response, 200)

    except Exception as exc:
        logger.exception(f"Error in get_gex_levels: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500


# ---------------------------------------------------------------------------
# Public API: multi-expiry (cumulative ∑)
# ---------------------------------------------------------------------------

def get_gex_levels_cumulative(
    underlying: str,
    exchange: str,
    expiry_dates: list[str],
    strike_count: int,
    api_key: str,
) -> tuple[bool, dict[str, Any], int]:
    """
    Aggregate GEX across multiple expiries (Cumulative ∑ model).

    Up to _MAX_EXPIRIES expiries are processed to limit broker calls.

    Returns:
        (success, response_dict, status_code)
    """
    try:
        cache_key = (underlying.upper(), exchange.upper(), tuple(sorted(expiry_dates)), strike_count)
        expiry_dates = expiry_dates[:_MAX_EXPIRIES]
        if not expiry_dates:
            return False, {"status": "error", "message": "At least one expiry required"}, 400

        options_exchange = exchange.upper()
        if options_exchange in ("NSE_INDEX", "NSE"):
            options_exchange = "NFO"
        elif options_exchange in ("BSE_INDEX", "BSE"):
            options_exchange = "BFO"

        spot_price = 0.0
        per_expiry: list[dict] = []
        # Aggregated GEX keyed by strike
        agg: dict[float, dict[str, float]] = {}

        for exp in expiry_dates:
            success, chain_resp, sc = get_option_chain(
                underlying=underlying,
                exchange=exchange,
                expiry_date=exp,
                strike_count=strike_count,
                api_key=api_key,
            )
            if not success:
                logger.warning(f"Skipping expiry {exp}: {chain_resp.get('message')}")
                continue

            chain = chain_resp.get("chain", [])
            if not spot_price:
                spot_price = chain_resp.get("underlying_ltp", 0) or 0

            gex_chain = _compute_gex_from_chain(chain, spot_price, options_exchange)
            levels = _derive_gex_levels(gex_chain, spot_price)

            per_expiry.append(
                {
                    "expiry_date": exp.upper(),
                    "chain": gex_chain,
                    "levels": levels,
                }
            )

            for row in gex_chain:
                s = row["strike"]
                if s not in agg:
                    agg[s] = {"ce_gex": 0.0, "pe_gex": 0.0, "net_gex": 0.0,
                              "ce_oi": 0, "pe_oi": 0}
                agg[s]["ce_gex"] += row["ce_gex"]
                agg[s]["pe_gex"] += row["pe_gex"]
                agg[s]["net_gex"] += row["net_gex"]
                agg[s]["ce_oi"] += row["ce_oi"]
                agg[s]["pe_oi"] += row["pe_oi"]

        if not per_expiry:
            # Fallback to cache
            fallback = _get_gex_cache(cache_key)
            if fallback:
                logger.info(
                    f"GEX cumulative [{underlying}] all expiries failed — "
                    "serving last-known data (market-closed / holiday / broker error)"
                )
                fallback["data_mode"] = "last_day_fallback"
                return True, fallback, 200
            return False, {"status": "error", "message": "No valid expiry data found"}, 404

        aggregated_chain = [
            {
                "strike": s,
                "ce_oi": int(v["ce_oi"]),
                "pe_oi": int(v["pe_oi"]),
                "ce_gex": round(v["ce_gex"], 2),
                "pe_gex": round(v["pe_gex"], 2),
                "net_gex": round(v["net_gex"], 2),
            }
            for s, v in sorted(agg.items())
        ]

        aggregated_levels = _derive_gex_levels(aggregated_chain, spot_price)

        response = {
            "status": "success",
            "mode": "cumulative",
            "underlying": underlying.upper(),
            "spot_price": spot_price,
            "expiries_used": [e["expiry_date"] for e in per_expiry],
            "chain": aggregated_chain,
            "levels": aggregated_levels,
            "per_expiry": per_expiry,
            "data_mode": "realtime",
        }
        _set_gex_cache(cache_key, response)

        return (True, response, 200)

    except Exception as exc:
        logger.exception(f"Error in get_gex_levels_cumulative: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500
