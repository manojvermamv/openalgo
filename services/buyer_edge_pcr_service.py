"""
Buyer Edge — PCR Time Series Service

Builds an intraday Put/Call Ratio time series using the same approach as
straddle_chart_service.py:

  1. Fetch underlying history for the requested interval/window (best-effort;
     empty results on market holidays are handled gracefully).
  2. For each candle, determine the ATM strike from the underlying close.
  3. Fetch CE and PE option history for every unique ATM strike that appears.
  4. For each timestamp, sum CE OI/volume and PE OI/volume across an n-strike
     window around ATM and compute:
       PCR(OI)     = total_pe_oi    / total_ce_oi      (or null if unavailable)
       PCR(Volume) = total_pe_volume / total_ce_volume  (or null if unavailable)
  5. Compute day-anchored VWAP of PCR(OI) for reference.

Graceful degradation (three-level fallback):
  Level 1 — Historical option OI in the time-series bars:
    If all option history bars carry non-zero OI, a true intraday PCR series
    is produced ("live_only": False).
  Level 2 — Live option-chain snapshot ("combined Options Strike Range"):
    If option history OI is unavailable, the current option chain is fetched
    using ALL available strikes (strike_count ≈ half of available strikes,
    capped at 50 each side) so the PCR matches the NSE-published figure.
    A flat constant PCR is applied to every series point; response includes
    "live_only": True.
  Level 3 — Latest historical OI fallback (market holidays):
    If the live option chain also returns 0 OI (e.g. broker serves no live
    data on holidays), the most-recent OI values from the already-fetched ATM±
    window historical bars are aggregated and used as the snapshot PCR.

If underlying history is completely empty (very first day or broker outage)
but a PCR value is available from levels 2/3, a single synthetic data point
is synthesised at the current timestamp so the chart always renders.

Returns:
    series: [{time, pcr_oi, pcr_volume, spot, synthetic_future}]
    current_pcr_oi, current_pcr_volume
    live_only (bool)
"""

from datetime import datetime

import pandas as pd
import pytz

from services.buyer_edge_utils import get_buyer_edge_quote_exchange
from services.history_service import get_history
from services.option_chain_service import get_option_chain
from services.option_symbol_service import (
    construct_crypto_option_symbol,
    construct_option_symbol,
    find_atm_strike_from_actual,
    get_available_strikes,
    get_option_exchange,
)
from services.quotes_service import get_quotes
from services.strategy_chart_service import (
    _cap_last_n_trading_dates,
    _resolve_trading_window,
)
from database.token_db_enhanced import fno_search_symbols
from utils.constants import CRYPTO_EXCHANGES, INSTRUMENT_PERPFUT
from utils.logging import get_logger

logger = get_logger(__name__)

# Shared IST timezone object — created once at module level to avoid repeated construction
_IST = pytz.timezone("Asia/Kolkata")
# Decimal precision for PCR values
_PCR_DECIMAL_PRECISION = 4


def _convert_timestamp_to_ist(df: pd.DataFrame) -> pd.DataFrame | None:
    try:
        if "timestamp" not in df.columns:
            return None
        try:
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df["datetime"] = df["datetime"].dt.tz_convert(_IST)
        except Exception:
            try:
                df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df["datetime"] = df["datetime"].dt.tz_convert(_IST)
            except Exception:
                df["datetime"] = pd.to_datetime(df["timestamp"])
                if df["datetime"].dt.tz is None:
                    df["datetime"] = df["datetime"].dt.tz_localize("UTC").dt.tz_convert(_IST)
                else:
                    df["datetime"] = df["datetime"].dt.tz_convert(_IST)
        df.set_index("datetime", inplace=True)
        return df.sort_index()
    except Exception as e:
        logger.warning(f"PCR timestamp conversion error: {e}")
        return None


def get_pcr_chart_data(
    underlying: str,
    exchange: str,
    expiry_date: str,
    interval: str,
    api_key: str,
    days: int = 1,
    pcr_strike_window: int = 5,
    max_snapshot_strikes: int = 50,
) -> tuple[bool, dict, int]:
    """
    Compute intraday PCR(OI) and PCR(Volume) time series.

    Args:
        underlying:           Underlying symbol (e.g., NIFTY)
        exchange:             Exchange (NSE_INDEX, BSE_INDEX, NFO, BFO, …)
        expiry_date:          Expiry in DDMMMYY format
        interval:             Candle interval (1m, 3m, 5m, 15m, 30m, 1h, 1d)
        api_key:              OpenAlgo API key
        days:                 Number of trading days of history (1–5)
        pcr_strike_window:    Strikes on each side of ATM for intraday PCR sum
        max_snapshot_strikes: Max strikes per side for the live total PCR snapshot

    Returns:
        (success, response_dict, status_code)
    """
    try:
        start_date_str, end_date_str = _resolve_trading_window(days, _IST)

        base_symbol = underlying.upper()
        quote_exchange = get_buyer_edge_quote_exchange(base_symbol, exchange)
        options_exchange = get_option_exchange(quote_exchange)

        # CRYPTO: resolve perpetual symbol
        if exchange.upper() in CRYPTO_EXCHANGES:
            _perp = fno_search_symbols(
                query=f"{base_symbol}USDFUT",
                exchange=exchange,
                instrumenttype=INSTRUMENT_PERPFUT,
                limit=1,
            )
            if not _perp:
                return False, {"status": "error", "message": f"No perpetual futures found for {base_symbol}"}, 404
            underlying_quote_symbol = _perp[0]["symbol"]
        else:
            underlying_quote_symbol = base_symbol

        _build_sym = (
            construct_crypto_option_symbol
            if exchange.upper() in CRYPTO_EXCHANGES
            else construct_option_symbol
        )

        # Step 1: Get available strikes for this expiry
        available_strikes = get_available_strikes(
            base_symbol, expiry_date.upper(), "CE", options_exchange
        )
        if not available_strikes:
            return False, {
                "status": "error",
                "message": f"No strikes found for {base_symbol} {expiry_date}",
            }, 404

        # Step 2: Fetch underlying history (best-effort; holidays return empty)
        success_u, resp_u, _ = get_history(
            symbol=underlying_quote_symbol,
            exchange=quote_exchange,
            interval=interval,
            start_date=start_date_str,
            end_date=end_date_str,
            api_key=api_key,
        )
        df_underlying = None
        if success_u:
            _raw = pd.DataFrame(resp_u.get("data", []))
            if not _raw.empty:
                df_underlying = _convert_timestamp_to_ist(_raw)

        # Step 3: Compute ATM per candle (skip when no history — holiday / broker down)
        unique_strikes: list[float] = []
        if df_underlying is not None:
            atm_per_row = [
                find_atm_strike_from_actual(float(row["close"]), available_strikes)
                for _, row in df_underlying.iterrows()
            ]
            df_underlying["atm_strike"] = atm_per_row
            unique_strikes = sorted(s for s in set(atm_per_row) if s is not None)

        # Step 4: Collect ALL unique window strikes across every unique ATM first, then
        # fetch one CE history call and one PE history call per unique strike.
        strike_history: dict[float, dict] = {}  # strike -> {ce: {ts: {oi, volume, close}}, pe: {...}}

        all_window_strikes: set[float] = set()
        for atm in unique_strikes:
            atm_idx = available_strikes.index(atm) if atm in available_strikes else None
            if atm_idx is None:
                continue
            window = available_strikes[
                max(0, atm_idx - pcr_strike_window):
                min(len(available_strikes), atm_idx + pcr_strike_window + 1)
            ]
            all_window_strikes.update(window)

        for k_strike in sorted(all_window_strikes):
            ce_sym = _build_sym(base_symbol, expiry_date.upper(), k_strike, "CE")
            pe_sym = _build_sym(base_symbol, expiry_date.upper(), k_strike, "PE")

            ce_ok, resp_ce, _ = get_history(
                symbol=ce_sym,
                exchange=options_exchange,
                interval=interval,
                start_date=start_date_str,
                end_date=end_date_str,
                api_key=api_key,
            )
            pe_ok, resp_pe, _ = get_history(
                symbol=pe_sym,
                exchange=options_exchange,
                interval=interval,
                start_date=start_date_str,
                end_date=end_date_str,
                api_key=api_key,
            )

            ce_rows: dict = {}
            pe_rows: dict = {}

            if ce_ok:
                df_ce = pd.DataFrame(resp_ce.get("data", []))
                if not df_ce.empty:
                    df_ce = _convert_timestamp_to_ist(df_ce)
                    if df_ce is not None:
                        for ts, row in df_ce.iterrows():
                            ce_rows[ts] = {
                                "oi": float(row.get("oi", 0) or 0),
                                "volume": float(row.get("volume", 0) or 0),
                                "close": float(row.get("close", 0) or 0),
                            }

            if pe_ok:
                df_pe = pd.DataFrame(resp_pe.get("data", []))
                if not df_pe.empty:
                    df_pe = _convert_timestamp_to_ist(df_pe)
                    if df_pe is not None:
                        for ts, row in df_pe.iterrows():
                            pe_rows[ts] = {
                                "oi": float(row.get("oi", 0) or 0),
                                "volume": float(row.get("volume", 0) or 0),
                                "close": float(row.get("close", 0) or 0),
                            }

            strike_history[k_strike] = {"ce": ce_rows, "pe": pe_rows}

        # Step 5: Build PCR series per candle (only when underlying history is available)
        series = []
        has_oi_data = False
        # ADR: track previous total OI (CE+PE) per strike to detect advances/declines
        prev_strike_oi: dict[float, float] = {}
        # Split breadth: track previous CE OI and PE OI independently per strike
        prev_ce_oi_per_strike: dict[float, float] = {}
        prev_pe_oi_per_strike: dict[float, float] = {}
        first_candle = True

        for ts, row in (df_underlying.iterrows() if df_underlying is not None else []):
            spot = float(row["close"])
            atm = row["atm_strike"]
            if atm is None:
                continue

            atm_idx = available_strikes.index(atm) if atm in available_strikes else None
            if atm_idx is None:
                continue

            window_strikes = available_strikes[
                max(0, atm_idx - pcr_strike_window):
                min(len(available_strikes), atm_idx + pcr_strike_window + 1)
            ]

            total_ce_oi = 0.0
            total_pe_oi = 0.0
            total_ce_vol = 0.0
            total_pe_vol = 0.0
            atm_ce_close = 0.0
            atm_pe_close = 0.0

            # ADR accumulators for this candle
            advances = 0
            declines = 0
            neutral_count = 0
            # Split CE/PE breadth accumulators
            ce_advances = 0
            ce_declines = 0
            pe_advances = 0
            pe_declines = 0

            for k_strike in window_strikes:
                sh = strike_history.get(k_strike, {"ce": {}, "pe": {}})
                ce_data = sh["ce"].get(ts, {})
                pe_data = sh["pe"].get(ts, {})
                ce_oi = ce_data.get("oi", 0)
                pe_oi = pe_data.get("oi", 0)
                total_ce_oi += ce_oi
                total_pe_oi += pe_oi
                total_ce_vol += ce_data.get("volume", 0)
                total_pe_vol += pe_data.get("volume", 0)
                if k_strike == atm:
                    atm_ce_close = ce_data.get("close", 0)
                    atm_pe_close = pe_data.get("close", 0)

                # ADR: compare combined CE+PE OI to the previous candle.
                curr_total_oi = ce_oi + pe_oi
                if not first_candle:
                    prev_total_oi = prev_strike_oi.get(k_strike, curr_total_oi)
                    if curr_total_oi > prev_total_oi:
                        advances += 1
                    elif curr_total_oi < prev_total_oi:
                        declines += 1
                    else:
                        neutral_count += 1

                    # Split CE/PE breadth: track each leg's OI independently
                    prev_ce = prev_ce_oi_per_strike.get(k_strike, ce_oi)
                    prev_pe = prev_pe_oi_per_strike.get(k_strike, pe_oi)
                    if ce_oi > prev_ce:
                        ce_advances += 1
                    elif ce_oi < prev_ce:
                        ce_declines += 1
                    if pe_oi > prev_pe:
                        pe_advances += 1
                    elif pe_oi < prev_pe:
                        pe_declines += 1
                else:
                    neutral_count += 1
                prev_strike_oi[k_strike] = curr_total_oi
                prev_ce_oi_per_strike[k_strike] = ce_oi
                prev_pe_oi_per_strike[k_strike] = pe_oi

            pcr_oi = round(total_pe_oi / total_ce_oi, _PCR_DECIMAL_PRECISION) if total_ce_oi > 0 else None
            pcr_volume = round(total_pe_vol / total_ce_vol, _PCR_DECIMAL_PRECISION) if total_ce_vol > 0 else None
            synthetic_future = (
                round(atm + atm_ce_close - atm_pe_close, 2)
                if atm_ce_close and atm_pe_close
                else None
            )

            # ADR = advances / declines
            if first_candle:
                adr = None
            elif declines > 0:
                adr = round(min(advances / declines, 10.0), _PCR_DECIMAL_PRECISION)
            elif advances > 0:
                adr = 10.0
            else:
                adr = 0.0

            first_candle = False

            if pcr_oi is not None:
                has_oi_data = True

            series.append(
                {
                    "time": int(ts.timestamp()),
                    "spot": round(spot, 2),
                    "atm_strike": atm,
                    "pcr_oi": pcr_oi,
                    "pcr_volume": pcr_volume,
                    "synthetic_future": synthetic_future,
                    "advances": advances,
                    "declines": declines,
                    "neutral": neutral_count,
                    "adr": adr,
                    "ce_oi": round(total_ce_oi),
                    "pe_oi": round(total_pe_oi),
                    "atm_ce_ltp": round(atm_ce_close, 2),
                    "atm_pe_ltp": round(atm_pe_close, 2),
                    "ce_advances": ce_advances,
                    "ce_declines": ce_declines,
                    "pe_advances": pe_advances,
                    "pe_declines": pe_declines,
                }
            )

        # Trim to last N trading days
        series = _cap_last_n_trading_dates(series, days, _IST)

        # Step 6: Live snapshot PCR
        live_pcr_oi = None
        live_pcr_volume = None
        snapshot_strike_count = min(len(available_strikes) // 2 + 1, max_snapshot_strikes)
        success_oc, oc_resp, _ = get_option_chain(
            underlying=base_symbol,
            exchange=exchange,
            expiry_date=expiry_date,
            strike_count=snapshot_strike_count,
            api_key=api_key,
        )
        if success_oc:
            ce_oi_t = sum(
                (item.get("ce") or {}).get("oi", 0) or 0
                for item in oc_resp.get("chain", [])
            )
            pe_oi_t = sum(
                (item.get("pe") or {}).get("oi", 0) or 0
                for item in oc_resp.get("chain", [])
            )
            ce_vol_t = sum(
                (item.get("ce") or {}).get("volume", 0) or 0
                for item in oc_resp.get("chain", [])
            )
            pe_vol_t = sum(
                (item.get("pe") or {}).get("volume", 0) or 0
                for item in oc_resp.get("chain", [])
            )
            live_pcr_oi = round(pe_oi_t / ce_oi_t, _PCR_DECIMAL_PRECISION) if ce_oi_t > 0 else None
            live_pcr_volume = round(pe_vol_t / ce_vol_t, _PCR_DECIMAL_PRECISION) if ce_vol_t > 0 else None

        # Fallback: if live option chain returned 0 OI (market closed / broker returns no
        # live quotes on a holiday), try to use the most-recent historical OI values that
        # were already fetched for the ATM±window strikes during Step 4.
        if live_pcr_oi is None and strike_history:
            all_hist_ts: set = set()
            for sh in strike_history.values():
                all_hist_ts.update(sh["ce"].keys())
                all_hist_ts.update(sh["pe"].keys())
            if all_hist_ts:
                latest_ts = max(all_hist_ts)
                hist_ce_oi = sum(
                    sh["ce"].get(latest_ts, {}).get("oi", 0) or 0
                    for sh in strike_history.values()
                )
                hist_pe_oi = sum(
                    sh["pe"].get(latest_ts, {}).get("oi", 0) or 0
                    for sh in strike_history.values()
                )
                hist_ce_vol = sum(
                    sh["ce"].get(latest_ts, {}).get("volume", 0) or 0
                    for sh in strike_history.values()
                )
                hist_pe_vol = sum(
                    sh["pe"].get(latest_ts, {}).get("volume", 0) or 0
                    for sh in strike_history.values()
                )
                if hist_ce_oi > 0:
                    live_pcr_oi = round(hist_pe_oi / hist_ce_oi, _PCR_DECIMAL_PRECISION)
                if hist_ce_vol > 0:
                    live_pcr_volume = round(hist_pe_vol / hist_ce_vol, _PCR_DECIMAL_PRECISION)

        # If no OI in history, build a flat series from the snapshot PCR
        live_only = not has_oi_data
        if live_only and live_pcr_oi is not None:
            for pt in series:
                pt["pcr_oi"] = live_pcr_oi
                pt["pcr_volume"] = live_pcr_volume

        # If underlying history was empty (e.g. first day, broker down, market holiday
        # with no prior session) but we have a live PCR, synthesise a single snapshot
        # point so the chart always renders something meaningful.
        if not series and live_pcr_oi is not None:
            # Try to get a spot price from live quotes; fall back to 0
            _sq_ok, _sq_resp, _ = get_quotes(
                symbol=underlying_quote_symbol, exchange=quote_exchange, api_key=api_key
            )
            _spot = _sq_resp.get("data", {}).get("ltp", 0) if _sq_ok else 0
            series = [
                {
                    "time": int(datetime.now(_IST).timestamp()),
                    "spot": float(_spot or 0),
                    "atm_strike": None,
                    "pcr_oi": live_pcr_oi,
                    "pcr_volume": live_pcr_volume,
                    "synthetic_future": None,
                    "advances": 0,
                    "declines": 0,
                    "neutral": 0,
                    "adr": None,
                    "ce_oi": 0,
                    "pe_oi": 0,
                    "atm_ce_ltp": 0.0,
                    "atm_pe_ltp": 0.0,
                    "ce_advances": 0,
                    "ce_declines": 0,
                    "pe_advances": 0,
                    "pe_declines": 0,
                }
            ]
            live_only = True

        if not series:
            return False, {"status": "error", "message": "No PCR data available"}, 404

        # Current LTP
        success_q, quote_resp, _ = get_quotes(
            symbol=underlying_quote_symbol, exchange=quote_exchange, api_key=api_key
        )
        underlying_ltp = quote_resp.get("data", {}).get("ltp", 0) if success_q else 0

        # Derive current ADR from the last non-null adr value in the series
        current_adr = next(
            (pt["adr"] for pt in reversed(series) if pt.get("adr") is not None),
            None,
        )

        # Per-strike OI change snapshot (first candle vs last candle in the window).
        # Uses strike_history already fetched in Step 4 so no additional API calls.
        strike_oi_changes: list[dict] = []
        if strike_history and series:
            all_hist_ts_set: set = set()
            for _sh in strike_history.values():
                all_hist_ts_set.update(_sh["ce"].keys())
                all_hist_ts_set.update(_sh["pe"].keys())
            if all_hist_ts_set:
                sorted_ts = sorted(all_hist_ts_set)
                first_ts = sorted_ts[0]
                last_ts = sorted_ts[-1]
                for k_strike in sorted(strike_history.keys()):
                    _sh = strike_history[k_strike]
                    first_ce = _sh["ce"].get(first_ts, {}).get("oi", 0) or 0
                    first_pe = _sh["pe"].get(first_ts, {}).get("oi", 0) or 0
                    last_ce = _sh["ce"].get(last_ts, {}).get("oi", 0) or 0
                    last_pe = _sh["pe"].get(last_ts, {}).get("oi", 0) or 0
                    strike_oi_changes.append({
                        "strike": k_strike,
                        "ce_oi_chg": round(last_ce - first_ce),
                        "pe_oi_chg": round(last_pe - first_pe),
                    })

        # PCR based on OI change (delta-PCR): pe_oi_change_total / ce_oi_change_total
        current_pcr_oi_chg = None
        if series:
            first_ce_oi = series[0].get("ce_oi", 0) or 0
            first_pe_oi = series[0].get("pe_oi", 0) or 0
            last_ce_oi = series[-1].get("ce_oi", 0) or 0
            last_pe_oi = series[-1].get("pe_oi", 0) or 0
            ce_chg = last_ce_oi - first_ce_oi
            pe_chg = last_pe_oi - first_pe_oi
            if ce_chg != 0:
                current_pcr_oi_chg = round(pe_chg / ce_chg, _PCR_DECIMAL_PRECISION)

        return (
            True,
            {
                "status": "success",
                "data": {
                    "underlying": base_symbol,
                    "underlying_ltp": underlying_ltp,
                    "expiry_date": expiry_date.upper(),
                    "interval": interval,
                    "live_only": live_only,
                    "current_pcr_oi": live_pcr_oi,
                    "current_pcr_volume": live_pcr_volume,
                    "current_adr": current_adr,
                    "current_pcr_oi_chg": current_pcr_oi_chg,
                    "strike_oi_changes": strike_oi_changes,
                    "series": series,
                },
            },
            200,
        )

    except Exception as exc:
        logger.exception(f"Error in get_pcr_chart_data: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500


# ---------------------------------------------------------------------------
# get_spot_candles — lightweight OHLCV history for the GEX spot chart
# ---------------------------------------------------------------------------

def get_spot_candles(
    underlying: str,
    exchange: str,
    interval: str,
    api_key: str,
    days: int = 5,
) -> tuple[bool, dict, int]:
    """
    Fetch OHLCV candle history for the underlying spot symbol.

    Args:
        underlying:  Base symbol (e.g. NIFTY, BANKNIFTY)
        exchange:    Option exchange (NFO, BFO, …); used only to derive the
                     quote exchange via _get_quote_exchange
        interval:    Candle interval (1m, 3m, 5m, 15m, 30m, 1h, 1d)
        api_key:     OpenAlgo API key
        days:        Trading days of history to return (1–30)

    Returns:
        (success, response_dict, status_code)
        response_dict: {status, underlying, exchange, candles:[{time,open,high,low,close,volume}]}
    """
    try:
        start_date_str, end_date_str = _resolve_trading_window(days, _IST)

        base_symbol = underlying.upper()
        quote_exchange = get_buyer_edge_quote_exchange(base_symbol, exchange)

        if exchange.upper() in CRYPTO_EXCHANGES:
            _perp = fno_search_symbols(
                query=f"{base_symbol}USDFUT",
                exchange=exchange,
                instrumenttype=INSTRUMENT_PERPFUT,
                limit=1,
            )
            if not _perp:
                return False, {
                    "status": "error",
                    "message": f"No perpetual futures found for {base_symbol}",
                }, 404
            underlying_quote_symbol = _perp[0]["symbol"]
        else:
            underlying_quote_symbol = base_symbol

        success_u, resp_u, _ = get_history(
            symbol=underlying_quote_symbol,
            exchange=quote_exchange,
            interval=interval,
            start_date=start_date_str,
            end_date=end_date_str,
            api_key=api_key,
        )
        if not success_u:
            return False, {"status": "error", "message": "Failed to fetch spot history"}, 502

        raw = resp_u.get("data", [])
        if not raw:
            return True, {
                "status": "success",
                "underlying": base_symbol,
                "exchange": quote_exchange,
                "candles": [],
            }, 200

        df = pd.DataFrame(raw)
        df = _convert_timestamp_to_ist(df)
        if df is None:
            return False, {"status": "error", "message": "Failed to parse candle timestamps"}, 500

        candles = []
        for ts, row in df.iterrows():
            candles.append({
                "time": int(ts.timestamp()),
                "open": round(float(row.get("open", 0) or 0), 2),
                "high": round(float(row.get("high", 0) or 0), 2),
                "low": round(float(row.get("low", 0) or 0), 2),
                "close": round(float(row.get("close", 0) or 0), 2),
                "volume": int(row.get("volume", 0) or 0),
            })

        return True, {
            "status": "success",
            "underlying": base_symbol,
            "exchange": quote_exchange,
            "candles": candles,
        }, 200

    except Exception as exc:
        logger.exception(f"Error in get_spot_candles: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500
