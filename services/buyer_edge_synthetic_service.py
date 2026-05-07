"""
Synthetic Future context engine for BuyerEdge.

This module is intentionally kept free of all database, Flask, broker, and
history-service imports so it can be imported cheaply in unit tests and
in other lightweight contexts.

Public API:
    _compute_synthetic_future_context(chain, spot, atm, pcr_series=None) -> dict

Basis interpretation model
--------------------------
Positive basis (SF > spot) is normal cost-of-carry in Indian index options and
must NOT be treated as bearish.  Negative basis is rare backwardation and must
NOT be treated as automatically bullish.  Basis velocity sign alone is equally
ambiguous: negative velocity (SF underperforming spot) can mean options are
not confirming a spot move, which is a trap signal rather than a bullish one.

Directional pressure is therefore derived from **spot-SF co-movement**:
  - spot ↑  AND SF ↑ → bullish  (options pricing confirms the spot rally)
  - spot ↓  AND SF ↓ → bearish  (options pricing confirms the spot decline)
  - spot ↑  AND SF ↓ (or vice versa) → diverging (trap — used by T4)
  - spot flat OR only one moving → neutral

Without an intraday pcr_series, no directional pressure is emitted (neutral).

Basis velocity is still computed and returned as ``basis_velocity`` for
informational display only — it does not drive the pressure signal.

The raw basis level is conveyed via the separate informational field
``basis_state`` ('normal' | 'backwardation' | 'neutral'), which callers can
use for carry / liquidity context without confusing it with direction.
"""

from typing import Any


# Minimum movement required (as a fraction of spot) for both spot and SF to
# register a meaningful co-movement signal or divergence.  0.05% of spot
# (e.g. ≈12 points for Nifty at 24 000).  Keeps micro-ticks from creating
# false confirmations or triggering spurious divergence traps.
_MOVE_THRESHOLD_PCT: float = 0.0005

# Full null-filled template used for both the ATM-not-found early return and
# the completely-invalid data case, so the API shape is always consistent.
_EMPTY_SYNTHETIC: dict[str, Any] = {
    "synthetic_ltp": None,
    "synthetic_mid": None,
    "synthetic_bid": None,
    "synthetic_ask": None,
    "synthetic_spread": None,
    "basis_ltp": None,
    "basis_mid": None,
    "basis_pct": None,
    "spread_pct": None,
    "liquidity_status": "invalid",
    "basis_state": "neutral",
    "pressure": "neutral",
    "confirmation": "unavailable",
    "reason": "",
    "synthetic_velocity": None,
    "basis_velocity": None,
    "basis_session_change": None,
    "ltp_inside_market": None,
}


def _compute_synthetic_future_context(
    chain: list[dict],
    spot: float,
    atm: float,
    pcr_series: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Compute a bid/ask-aware synthetic future context from the live option-chain
    snapshot already fetched by get_buyer_edge_data().  No extra API call needed.

    Bid/ask data is PRIMARY.  LTP is a fallback when bid/ask is unavailable.

    Returns a structured dict with:
      atm_strike, synthetic_ltp, synthetic_mid, synthetic_bid, synthetic_ask,
      synthetic_spread, basis_ltp, basis_mid, basis_pct, spread_pct,
      liquidity_status ('good' | 'wide' | 'ltp_only' | 'invalid'),
      basis_state ('normal' | 'backwardation' | 'neutral'),
      pressure ('bullish' | 'bearish' | 'neutral'),
      confirmation ('confirming' | 'diverging' | 'unavailable'),
      reason, synthetic_velocity, basis_velocity, basis_session_change,
      ltp_inside_market.

    The dict shape is always complete (all keys present, None for missing values)
    so downstream consumers can rely on the schema regardless of data availability.

    Pressure model
    --------------
    Positive synthetic basis is normal cost-of-carry; negative basis is rare
    backwardation.  Neither raw basis sign nor basis velocity sign is a reliable
    directional indicator on its own.

    Directional ``pressure`` is derived from SPOT-SF CO-MOVEMENT using the
    last two bars of ``pcr_series``:

      spot ↑  AND SF ↑  → 'bullish'   (options pricing confirms spot rally)
      spot ↓  AND SF ↓  → 'bearish'   (options pricing confirms spot decline)
      spot ↑  AND SF ↓  → diverging trap (confirmation = 'diverging')
      spot flat or only SF moving → 'neutral' (not actionable)

    ``basis_velocity`` is computed from the series and returned for informational
    display, but it does NOT drive the pressure signal.

    Without a pcr_series, ``pressure`` is always 'neutral' and
    ``confirmation`` is always 'unavailable'.
    """
    atm_row = next((i for i in chain if i["strike"] == atm), None)
    if not atm_row:
        return {
            "atm_strike": atm,
            **_EMPTY_SYNTHETIC,
            "reason": "ATM strike not found in chain",
        }

    ce = atm_row.get("ce") or {}
    pe = atm_row.get("pe") or {}

    ce_ltp = ce.get("ltp") or 0
    pe_ltp = pe.get("ltp") or 0
    ce_bid = ce.get("bid") or 0
    ce_ask = ce.get("ask") or 0
    pe_bid = pe.get("bid") or 0
    pe_ask = pe.get("ask") or 0

    ba_valid = (
        ce_bid > 0 and ce_ask >= ce_bid
        and pe_bid > 0 and pe_ask >= pe_bid
    )

    # LTP synthetic (fallback when bid/ask is absent)
    synthetic_ltp: float | None = (
        round(atm + ce_ltp - pe_ltp, 2) if ce_ltp > 0 and pe_ltp > 0 else None
    )

    # Bid/ask-aware values (primary path)
    synthetic_mid = synthetic_bid = synthetic_ask = synthetic_spread = None
    spread_pct: float | None = None
    ltp_inside_market: bool | None = None

    if ba_valid:
        ce_mid = (ce_bid + ce_ask) / 2
        pe_mid = (pe_bid + pe_ask) / 2
        synthetic_mid = round(atm + ce_mid - pe_mid, 2)
        # Synthetic bid (what you receive selling it): CE bid − PE ask
        synthetic_bid = round(atm + ce_bid - pe_ask, 2)
        # Synthetic ask (what you pay buying it): CE ask − PE bid
        synthetic_ask = round(atm + ce_ask - pe_bid, 2)
        synthetic_spread = round(synthetic_ask - synthetic_bid, 2)
        spread_pct = round((synthetic_spread / spot) * 100, 3) if spot > 0 else None
        ltp_inside_market = bool(
            ce_ltp > 0 and pe_ltp > 0
            and ce_bid <= ce_ltp <= ce_ask
            and pe_bid <= pe_ltp <= pe_ask
        )

    # Liquidity status — bid/ask is primary; LTP is fallback
    has_ltp = synthetic_ltp is not None
    if not has_ltp and not ba_valid:
        liquidity_status = "invalid"
    elif ba_valid and spread_pct is not None and spread_pct > 0.5:
        liquidity_status = "wide"
    elif ba_valid:
        liquidity_status = "good"
    else:
        # has_ltp=True, ba_valid=False
        liquidity_status = "ltp_only"

    # Basis values
    basis_ltp = round(synthetic_ltp - spot, 2) if synthetic_ltp is not None and spot else None
    basis_mid = round(synthetic_mid - spot, 2) if synthetic_mid is not None and spot else None
    basis_pct = round((basis_mid / spot) * 100, 3) if basis_mid is not None and spot > 0 else None

    # basis_state — informational carry context, NOT directional
    # Positive basis is normal cost-of-carry; negative is rare backwardation.
    basis_ref = basis_mid if basis_mid is not None else basis_ltp
    if basis_ref is None:
        basis_state = "neutral"
    elif basis_ref < -(spot * 0.001 if spot > 0 else 0):
        basis_state = "backwardation"
    elif basis_ref >= 0:
        basis_state = "normal"
    else:
        basis_state = "neutral"

    # Time-series velocity from pcr_series
    synthetic_velocity: float | None = None
    basis_velocity: float | None = None
    basis_session_change: float | None = None
    if pcr_series and len(pcr_series) >= 2:
        sf_vals = [p.get("synthetic_future") for p in pcr_series if p.get("synthetic_future") is not None]
        if len(sf_vals) >= 2:
            synthetic_velocity = round(sf_vals[-1] - sf_vals[-2], 2)
            sp_aligned = [
                p.get("spot", 0) for p in pcr_series
                if p.get("synthetic_future") is not None
            ]
            basis_vals = [sf - sp for sf, sp in zip(sf_vals, sp_aligned) if sp]
            if len(basis_vals) >= 2:
                basis_velocity = round(basis_vals[-1] - basis_vals[-2], 2)
                basis_session_change = round(basis_vals[-1] - basis_vals[0], 2)

    # Pressure and confirmation model
    # Direction is derived from SPOT-SF CO-MOVEMENT (both rising or both falling),
    # NOT from raw basis level or basis velocity sign.
    # Basis velocity is surfaced as an informational field only.
    if basis_ref is None or liquidity_status == "invalid":
        pressure = "neutral"
        confirmation = "unavailable"
        reason = "Insufficient option data for synthetic computation"
    elif not (pcr_series and len(pcr_series) >= 2):
        # No time-series: raw basis level alone cannot determine direction.
        carry_str = f"{'+' if basis_ref >= 0 else ''}{round(basis_ref, 1)}"
        pressure = "neutral"
        confirmation = "unavailable"
        reason = f"SF carry {carry_str} ({basis_state}) — no intraday series"
    else:
        latest_sf = pcr_series[-1].get("synthetic_future")
        prev_sf = pcr_series[-2].get("synthetic_future")
        carry_str = f"{'+' if basis_ref >= 0 else ''}{round(basis_ref, 1)}"
        vel_str = f"{'+' if (basis_velocity or 0) >= 0 else ''}{round(basis_velocity or 0, 1)}"
        if latest_sf is not None and prev_sf is not None:
            spot_delta = (
                (pcr_series[-1].get("spot") or spot)
                - (pcr_series[-2].get("spot") or spot)
            )
            sf_delta = latest_sf - prev_sf
            # Both spot AND SF must move at least 0.05% of spot to register a signal.
            # Applying the threshold to both sides prevents a tiny SF tick from
            # falsely confirming or diverging against a real spot move.
            move_threshold = spot * _MOVE_THRESHOLD_PCT if spot > 0 else 0.01
            # Divergence: spot and SF both moving significantly but in opposite directions
            if (
                abs(spot_delta) > move_threshold
                and abs(sf_delta) > move_threshold
                and (
                    (spot_delta > 0 and sf_delta < 0)
                    or (spot_delta < 0 and sf_delta > 0)
                )
            ):
                pressure = "neutral"
                confirmation = "diverging"
                reason = (
                    f"Spot {'rising' if spot_delta > 0 else 'falling'} but SF "
                    f"{'declining' if sf_delta < 0 else 'rising'} — divergence trap"
                )
            # Co-movement bullish: both spot and SF rising meaningfully
            elif spot_delta > move_threshold and sf_delta > move_threshold:
                pressure = "bullish"
                confirmation = "confirming"
                reason = (
                    f"Spot & SF both rising (basis {carry_str}, vel {vel_str})"
                    f" — options confirming spot bull"
                )
            # Co-movement bearish: both spot and SF falling meaningfully
            elif spot_delta < -move_threshold and sf_delta < -move_threshold:
                pressure = "bearish"
                confirmation = "confirming"
                reason = (
                    f"Spot & SF both falling (basis {carry_str}, vel {vel_str})"
                    f" — options confirming spot bear"
                )
            else:
                # Spot flat, SF-only movement, or both below move threshold
                pressure = "neutral"
                confirmation = "unavailable"
                reason = f"SF carry {carry_str} ({basis_state}), basis vel {vel_str} — no co-movement"
        else:
            # pcr_series present but SF values missing in last two bars
            pressure = "neutral"
            confirmation = "unavailable"
            reason = f"SF carry {carry_str} ({basis_state}) — series present but SF values incomplete"

    return {
        "atm_strike": atm,
        "synthetic_ltp": synthetic_ltp,
        "synthetic_mid": synthetic_mid,
        "synthetic_bid": synthetic_bid,
        "synthetic_ask": synthetic_ask,
        "synthetic_spread": synthetic_spread,
        "basis_ltp": basis_ltp,
        "basis_mid": basis_mid,
        "basis_pct": basis_pct,
        "spread_pct": spread_pct,
        "liquidity_status": liquidity_status,
        "basis_state": basis_state,
        "pressure": pressure,
        "confirmation": confirmation,
        "reason": reason,
        "synthetic_velocity": synthetic_velocity,
        "basis_velocity": basis_velocity,
        "basis_session_change": basis_session_change,
        "ltp_inside_market": ltp_inside_market,
    }
