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
from services.option_greeks_service import calculate_greeks
from utils.logging import get_logger

logger = get_logger(__name__)

_MAX_EXPIRIES = 4  # cap broker calls in cumulative mode; balances analysis depth vs API rate limits


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

    Returns a list of dicts:
        strike, ce_oi, pe_oi, ce_gex, pe_gex, net_gex
    """
    result = []
    for item in chain:
        strike = item["strike"]
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}

        ce_oi = ce.get("oi", 0) or 0
        pe_oi = pe.get("oi", 0) or 0
        ce_ltp = ce.get("ltp", 0) or 0
        pe_ltp = pe.get("ltp", 0) or 0
        lot_size = ce.get("lotsize") or pe.get("lotsize") or 1

        ce_gamma = 0.0
        pe_gamma = 0.0

        if ce.get("symbol") and ce_ltp > 0 and ce_oi > 0:
            try:
                ok, resp, _ = calculate_greeks(
                    option_symbol=ce["symbol"],
                    exchange=options_exchange,
                    spot_price=spot_price,
                    option_price=ce_ltp,
                )
                if ok and resp.get("status") == "success":
                    ce_gamma = resp.get("greeks", {}).get("gamma", 0) or 0
            except Exception as exc:
                logger.debug(f"GEX CE gamma {ce.get('symbol')}: {exc}")

        if pe.get("symbol") and pe_ltp > 0 and pe_oi > 0:
            try:
                ok, resp, _ = calculate_greeks(
                    option_symbol=pe["symbol"],
                    exchange=options_exchange,
                    spot_price=spot_price,
                    option_price=pe_ltp,
                )
                if ok and resp.get("status") == "success":
                    pe_gamma = resp.get("greeks", {}).get("gamma", 0) or 0
            except Exception as exc:
                logger.debug(f"GEX PE gamma {pe.get('symbol')}: {exc}")

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

    return {
        "gamma_flip": gamma_flip,
        "hvl": hvl,
        "call_gamma_wall": call_gamma_wall,
        "put_gamma_wall": put_gamma_wall,
        "absolute_wall": absolute_wall,
        "total_net_gex": round(total_net_gex, 2),
        "zero_gamma": gamma_flip,  # alias used by some charting libs
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
            return False, chain_resp, sc

        chain = chain_resp.get("chain", [])
        spot_price = chain_resp.get("underlying_ltp", 0) or 0

        if not spot_price:
            return False, {"status": "error", "message": "Could not determine spot price"}, 500

        gex_chain = _compute_gex_from_chain(chain, spot_price, options_exchange)
        levels = _derive_gex_levels(gex_chain, spot_price)

        return (
            True,
            {
                "status": "success",
                "mode": "selected",
                "underlying": underlying.upper(),
                "expiry_date": expiry_date.upper(),
                "spot_price": spot_price,
                "expiries_used": [expiry_date.upper()],
                "chain": gex_chain,
                "levels": levels,
            },
            200,
        )

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

        return (
            True,
            {
                "status": "success",
                "mode": "cumulative",
                "underlying": underlying.upper(),
                "spot_price": spot_price,
                "expiries_used": [e["expiry_date"] for e in per_expiry],
                "chain": aggregated_chain,
                "levels": aggregated_levels,
                "per_expiry": per_expiry,
            },
            200,
        )

    except Exception as exc:
        logger.exception(f"Error in get_gex_levels_cumulative: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500
