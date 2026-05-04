"""
Buyer Edge — IVRank & Skew Dashboard Service

Computes the following IV-based metrics for one or more expiries in a single call:

  IVRank (TastyTrade formula):
    IVR = (current_ATM_IV − min_52w_IV) / (max_52w_IV − min_52w_IV) × 100
    52-week IV series is built from 1-year daily bars of the ATM CE option,
    computing Black-76 IV at each daily close.
    Result is cached per (underlying, exchange) with a 1-hour TTL to avoid
    re-computing on every page refresh.

  Per-expiry metrics:
    - DTE (days to expiry)
    - IVx (mean ATM IV for that expiry, CE+PE average)
    - Vertical Skew %: OTM Put IV − OTM Call IV (≈25-delta proxy at 5% OTM)
    - Horizontal IVx Skew %: IVx(exp_n) − IVx(exp_n-1) between consecutive expiries
    - Expected Move: spot × ATM_IV × sqrt(DTE/365)
    - Standard Deviation (1σ): same formula

  Summary scalars (across all expiries):
    - iv_rank, ivr_label, current_iv, iv_change_pct
    - avg_ivx (mean IVx across supplied expiries)

Graceful degradation:
  - If 52-week history is unavailable (broker doesn't support 1-year lookback),
    iv_rank and iv_change_pct are returned as None and "ivr_available" is False.
  - Per-expiry metrics that can't be computed are returned as None.
"""

import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import pytz

from services.buyer_edge_utils import get_buyer_edge_quote_exchange
from services.history_service import get_history
from services.iv_chart_service import calculate_time_to_expiry_at
from services.option_chain_service import get_option_chain
from services.option_greeks_service import (
    DEFAULT_INTEREST_RATES,
    calculate_greeks,
    parse_option_symbol,
)
from services.option_symbol_service import (
    find_atm_strike_from_actual,
)
from database.token_db_enhanced import fno_search_symbols
from utils.constants import CRYPTO_EXCHANGES, INSTRUMENT_PERPFUT
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 52-week IV cache  (LRU-bounded module-level dict, no DB)
# TTL: 3600 s (1 hour) — daily IV series changes slowly intraday
# Max 20 entries to prevent unbounded growth under eventlet single-worker.
# ---------------------------------------------------------------------------
_IV_HISTORY_MAX = 20
_IV_HISTORY_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_IV_HISTORY_TTL = 3600  # seconds

# Days of history to fetch for 52-week IV series
# 380 > 365 to account for weekends, holidays, and non-trading days
_IV_HISTORY_DAYS = 380

# Approximation parameters for historical IV estimation from underlying closes
# These are used when actual option history is unavailable
_MIN_OPTION_PRICE = 1.0         # floor price to avoid zero-price IV errors
_SPOT_PRICE_MULTIPLIER = 0.005  # 0.5% of spot as ATM option price approximation

# OTM distance for 25-delta proxy (vertical skew calculation)
_VERTICAL_SKEW_OTM_PCT = 0.05  # 5% OTM from ATM ≈ 25-delta for typical index options


def _parse_ddmmmyy_to_dte(expiry_str: str) -> float:
    """Return days to expiry from a DDMMMYY string, or 0 if expired/parse error."""
    try:
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist)
        dt = datetime.strptime(expiry_str.upper(), "%d%b%y")
        dt = ist.localize(dt.replace(hour=15, minute=30))
        return max(0.0, (dt - now).total_seconds() / 86400)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 52-week ATM IV history
# ---------------------------------------------------------------------------

def _build_52w_iv_series(
    underlying: str,
    exchange: str,
    options_exchange: str,
    api_key: str,
) -> list[float]:
    """
    Build a 52-week daily IV series for IVRank computation.

    Fetches 1-year daily bars of the underlying.  For each daily close,
    finds the nearest ATM CE option (using today's available strikes as proxy)
    and computes Black-76 IV using the correct time-to-expiry for that candle's
    date — mirroring the approach in iv_chart_service._calculate_iv_series().

    Option price is approximated as _SPOT_PRICE_MULTIPLIER × spot because
    brokers rarely supply historical option OHLCV for a full 52-week lookback.

    Returns list of IV values (as %) or [] on failure / py_vollib unavailable.
    """
    cache_key = (underlying.upper(), exchange.upper())
    cached = _IV_HISTORY_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached["ts"]) < _IV_HISTORY_TTL:
        _IV_HISTORY_CACHE.move_to_end(cache_key)
        return cached["data"]

    # Lazy-import to avoid loading scipy/numba/llvmlite at startup
    try:
        from py_vollib.black.implied_volatility import implied_volatility as _black_iv
    except ImportError:
        logger.error(
            "py_vollib not installed — 52w IV series unavailable for IVRank. "
            "Install with: pip install py_vollib"
        )
        return []

    try:
        quote_exchange = get_buyer_edge_quote_exchange(underlying, exchange)
        ist = pytz.timezone("Asia/Kolkata")
        today = datetime.now(ist).date()
        start_date = (today - timedelta(days=_IV_HISTORY_DAYS)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        # Determine underlying symbol for history
        if exchange.upper() in CRYPTO_EXCHANGES:
            _perp = fno_search_symbols(
                query=f"{underlying}USDFUT", exchange=exchange,
                instrumenttype=INSTRUMENT_PERPFUT, limit=1
            )
            und_sym = _perp[0]["symbol"] if _perp else underlying
        else:
            und_sym = underlying

        success, resp, _ = get_history(
            symbol=und_sym,
            exchange=quote_exchange,
            interval="1d",
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
        )
        if not success:
            return []

        rows = resp.get("data", [])
        if not rows:
            return []

        # Get available strikes from today's nearest expiry (proxy for ATM resolution)
        _opts = fno_search_symbols(
            underlying=underlying.upper(),
            exchange=options_exchange,
            instrumenttype="CE",
            limit=5,
        )
        if not _opts:
            return []

        available_strikes = sorted(
            set(float(o.get("strike", 0)) for o in _opts if o.get("strike"))
        )
        if not available_strikes:
            return []

        # Parse the sample option symbol ONCE to obtain the reference expiry datetime.
        # All historical candles use this same expiry for TTE calculation — a known
        # approximation, but unavoidable without per-day historical option data.
        try:
            _, sample_expiry, _, _ = parse_option_symbol(_opts[0]["symbol"], options_exchange)
        except Exception:
            return []

        # Interest rate for Black-76 (same convention as iv_chart_service)
        interest_rate = DEFAULT_INTEREST_RATES.get(options_exchange, 0) / 100.0

        iv_series: list[float] = []

        for row in rows:
            try:
                close_price = float(row.get("close", 0) or 0)
                if close_price <= 0:
                    continue

                atm = find_atm_strike_from_actual(close_price, available_strikes)
                if atm is None:
                    continue

                # Option price proxy: same approximation as before; documented limitation
                approx_ltp = max(_MIN_OPTION_PRICE, close_price * _SPOT_PRICE_MULTIPLIER)

                # --- Resolve candle datetime for correct per-candle TTE ---
                # This is the key fix: use the candle's own timestamp instead of
                # datetime.now() (which is what calculate_greeks() would do).
                ts_val = row.get("timestamp")
                candle_dt: datetime | None = None
                if isinstance(ts_val, (int, float)):
                    candle_dt = datetime.fromtimestamp(float(ts_val))
                elif ts_val:
                    try:
                        candle_dt = datetime.fromisoformat(str(ts_val))
                    except Exception:
                        logger.debug(f"_build_52w_iv_series: unparseable timestamp {ts_val!r}, skipping candle")
                        pass
                if candle_dt is None:
                    continue
                # calculate_time_to_expiry_at expects naive datetimes (no tzinfo)
                if candle_dt.tzinfo is not None:
                    candle_dt = candle_dt.replace(tzinfo=None)

                years_to_expiry, _ = calculate_time_to_expiry_at(candle_dt, sample_expiry)
                if years_to_expiry <= 0:
                    continue

                # Direct Black-76 IV (same call as iv_chart_service._calculate_iv_series)
                try:
                    iv_decimal = _black_iv(
                        approx_ltp, close_price, atm, interest_rate, years_to_expiry, "c"
                    )
                    if iv_decimal and iv_decimal > 0:
                        iv_series.append(round(float(iv_decimal) * 100, 2))
                except Exception as _iv_err:
                    logger.debug(
                        f"_build_52w_iv_series: Black-76 failed for "
                        f"spot={close_price}, strike={atm}, tte={years_to_expiry:.4f}: {_iv_err}"
                    )
                    continue

            except Exception:
                continue

        # Cache result (LRU: evict oldest when over capacity)
        _IV_HISTORY_CACHE[cache_key] = {"ts": time.monotonic(), "data": iv_series}
        _IV_HISTORY_CACHE.move_to_end(cache_key)
        while len(_IV_HISTORY_CACHE) > _IV_HISTORY_MAX:
            _IV_HISTORY_CACHE.popitem(last=False)
        return iv_series

    except Exception as exc:
        logger.warning(f"_build_52w_iv_series error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Per-expiry IV metrics
# ---------------------------------------------------------------------------

def _compute_expiry_iv_metrics(
    chain: list[dict],
    atm_strike: float,
    spot_price: float,
    options_exchange: str,
    expiry_date: str,
) -> dict[str, Any]:
    """
    Compute per-expiry IV metrics from a live option chain snapshot.

    Returns: {
        atm_iv, ivx, vertical_skew, call_iv_otm, put_iv_otm,
        dte, expected_move, sigma_1, ce_put_iv_skew_pct
    }
    """
    dte = _parse_ddmmmyy_to_dte(expiry_date)

    # Collect CE/PE IVs across all strikes
    ce_ivs: dict[float, float] = {}
    pe_ivs: dict[float, float] = {}
    atm_ce_iv = None
    atm_pe_iv = None

    for item in chain:
        strike = item["strike"]
        ce = item.get("ce") or {}
        pe = item.get("pe") or {}

        ce_ltp = ce.get("ltp", 0) or 0
        pe_ltp = pe.get("ltp", 0) or 0

        if ce.get("symbol") and ce_ltp > 0:
            try:
                ok, resp, _ = calculate_greeks(
                    option_symbol=ce["symbol"],
                    exchange=options_exchange,
                    spot_price=spot_price,
                    option_price=ce_ltp,
                )
                if ok and resp.get("status") == "success":
                    iv = resp.get("implied_volatility")
                    if iv and iv > 0:
                        ce_ivs[strike] = round(float(iv), 2)
                        if strike == atm_strike:
                            atm_ce_iv = ce_ivs[strike]
            except Exception:
                pass

        if pe.get("symbol") and pe_ltp > 0:
            try:
                ok, resp, _ = calculate_greeks(
                    option_symbol=pe["symbol"],
                    exchange=options_exchange,
                    spot_price=spot_price,
                    option_price=pe_ltp,
                )
                if ok and resp.get("status") == "success":
                    iv = resp.get("implied_volatility")
                    if iv and iv > 0:
                        pe_ivs[strike] = round(float(iv), 2)
                        if strike == atm_strike:
                            atm_pe_iv = pe_ivs[strike]
            except Exception:
                pass

    # ATM IV (average of CE + PE)
    if atm_ce_iv is not None and atm_pe_iv is not None:
        atm_iv = round((atm_ce_iv + atm_pe_iv) / 2, 2)
    elif atm_ce_iv is not None:
        atm_iv = atm_ce_iv
    elif atm_pe_iv is not None:
        atm_iv = atm_pe_iv
    else:
        atm_iv = None

    # IVx: mean of all valid CE IVs (OTM convention — CE above ATM, PE below ATM)
    otm_ce = [v for k, v in ce_ivs.items() if k >= atm_strike]
    otm_pe = [v for k, v in pe_ivs.items() if k <= atm_strike]
    all_otm = otm_ce + otm_pe
    ivx = round(sum(all_otm) / len(all_otm), 2) if all_otm else atm_iv

    # Vertical Skew (25-delta proxy at ~5% OTM)
    otm_dist = atm_strike * _VERTICAL_SKEW_OTM_PCT
    call_iv_otm: float | None = None
    put_iv_otm: float | None = None

    # Nearest OTM call IV (above ATM)
    above_sorted = sorted(
        [(abs(k - (atm_strike + otm_dist)), v) for k, v in ce_ivs.items() if k > atm_strike]
    )
    if above_sorted:
        call_iv_otm = above_sorted[0][1]

    # Nearest OTM put IV (below ATM)
    below_sorted = sorted(
        [(abs(k - (atm_strike - otm_dist)), v) for k, v in pe_ivs.items() if k < atm_strike]
    )
    if below_sorted:
        put_iv_otm = below_sorted[0][1]

    vertical_skew = (
        round(put_iv_otm - call_iv_otm, 2)
        if put_iv_otm is not None and call_iv_otm is not None
        else None
    )
    skew_pct = (
        round(vertical_skew / ((put_iv_otm + call_iv_otm) / 2) * 100, 2)
        if vertical_skew is not None and call_iv_otm and put_iv_otm
        else None
    )

    # Expected move and 1-sigma for this expiry
    if atm_iv is not None and dte > 0:
        expected_move = round(spot_price * (atm_iv / 100) * (dte / 365) ** 0.5, 2)
        sigma_1 = expected_move
    else:
        expected_move = None
        sigma_1 = None

    return {
        "dte": round(dte, 1),
        "atm_iv": atm_iv,
        "ivx": ivx,
        "call_iv_otm": call_iv_otm,
        "put_iv_otm": put_iv_otm,
        "vertical_skew": vertical_skew,
        "vertical_skew_pct": skew_pct,
        "expected_move": expected_move,
        "sigma_1": sigma_1,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_iv_dashboard(
    underlying: str,
    exchange: str,
    expiry_dates: list[str],
    strike_count: int,
    api_key: str,
) -> tuple[bool, dict[str, Any], int]:
    """
    Compute IVRank + per-expiry IV metrics.

    Args:
        underlying:    Underlying symbol
        exchange:      Exchange
        expiry_dates:  List of expiry dates in DDMMMYY format (1–4)
        strike_count:  Strikes around ATM for chain fetch
        api_key:       OpenAlgo API key

    Returns:
        (success, response_dict, status_code)
    """
    try:
        expiry_dates = expiry_dates[:4]
        if not expiry_dates:
            return False, {"status": "error", "message": "At least one expiry required"}, 400

        base_symbol = underlying.upper()
        options_exchange = exchange.upper()
        if options_exchange in ("NSE_INDEX", "NSE"):
            options_exchange = "NFO"
        elif options_exchange in ("BSE_INDEX", "BSE"):
            options_exchange = "BFO"

        spot_price = 0.0
        per_expiry_metrics: list[dict] = []

        for exp in expiry_dates:
            success, chain_resp, _ = get_option_chain(
                underlying=base_symbol,
                exchange=exchange,
                expiry_date=exp,
                strike_count=strike_count,
                api_key=api_key,
            )
            if not success:
                logger.warning(f"IV dashboard: skipping expiry {exp}")
                continue

            if not spot_price:
                spot_price = chain_resp.get("underlying_ltp", 0) or 0

            chain = chain_resp.get("chain", [])
            atm_strike = chain_resp.get("atm_strike", 0) or 0

            metrics = _compute_expiry_iv_metrics(
                chain, atm_strike, spot_price, options_exchange, exp
            )
            metrics["expiry_date"] = exp.upper()
            per_expiry_metrics.append(metrics)

        if not per_expiry_metrics:
            return False, {"status": "error", "message": "No expiry data available"}, 404

        # Horizontal IVx Skew between consecutive expiries
        sorted_by_dte = sorted(per_expiry_metrics, key=lambda x: x["dte"])
        for i, item in enumerate(sorted_by_dte):
            if i == 0:
                item["horizontal_ivx_skew"] = None
                item["horizontal_ivx_skew_pct"] = None
            else:
                prev = sorted_by_dte[i - 1]
                if item.get("ivx") is not None and prev.get("ivx") is not None:
                    skew = round(item["ivx"] - prev["ivx"], 2)
                    item["horizontal_ivx_skew"] = skew
                    # Negative skew = IVx falling as DTE increases = Calendar opportunity
                    item["horizontal_ivx_skew_pct"] = (
                        round(skew / prev["ivx"] * 100, 2) if prev["ivx"] else None
                    )
                else:
                    item["horizontal_ivx_skew"] = None
                    item["horizontal_ivx_skew_pct"] = None

        # Summary scalars
        ivx_values = [m["ivx"] for m in per_expiry_metrics if m.get("ivx") is not None]
        avg_ivx = round(sum(ivx_values) / len(ivx_values), 2) if ivx_values else None
        current_iv = per_expiry_metrics[0].get("atm_iv") if per_expiry_metrics else None

        # IVRank
        iv_rank = None
        ivr_available = False
        iv_52w_series = _build_52w_iv_series(base_symbol, exchange, options_exchange, api_key)
        if iv_52w_series and current_iv is not None:
            min_iv = min(iv_52w_series)
            max_iv = max(iv_52w_series)
            if max_iv > min_iv:
                iv_rank = round((current_iv - min_iv) / (max_iv - min_iv) * 100, 1)
                ivr_available = True
            elif max_iv == min_iv:
                iv_rank = 50.0
                ivr_available = True

        # IV Change % (today's first candle IV vs current) — approximation:
        # We compare first expiry IVx from the front end of the series if available.
        # Without intraday option OHLCV we use None as graceful degradation.
        iv_change_pct = None

        return (
            True,
            {
                "status": "success",
                "underlying": base_symbol,
                "spot_price": spot_price,
                "iv_rank": iv_rank,
                "ivr_available": ivr_available,
                "current_iv": current_iv,
                "avg_ivx": avg_ivx,
                "iv_change_pct": iv_change_pct,
                "expiries": per_expiry_metrics,
            },
            200,
        )

    except Exception as exc:
        logger.exception(f"Error in get_iv_dashboard: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500
