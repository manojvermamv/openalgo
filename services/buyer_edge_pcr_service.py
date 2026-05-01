"""
Buyer Edge — PCR Time Series Service

Builds an intraday Put/Call Ratio time series using the same approach as
straddle_chart_service.py:

  1. Fetch underlying history for the requested interval/window.
  2. For each candle, determine the ATM strike from the underlying close.
  3. Fetch CE and PE option history for every unique ATM strike that appears.
  4. For each timestamp, sum CE OI/volume and PE OI/volume across an n-strike
     window around ATM and compute:
       PCR(OI)     = total_pe_oi    / total_ce_oi      (or null if unavailable)
       PCR(Volume) = total_pe_volume / total_ce_volume  (or null if unavailable)
  5. Compute day-anchored VWAP of PCR(OI) for reference.

Graceful degradation: if option OI history is unavailable (broker returns 0
for all OI), the series uses the live-snapshot PCR from get_option_chain() as
a static reference value, and the response includes "live_only": True.

Returns:
    series: [{time, pcr_oi, pcr_volume, spot, synthetic_future}]
    current_pcr_oi, current_pcr_volume
    live_only (bool)
"""

from datetime import datetime

import pandas as pd
import pytz

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

# How many strikes on each side of ATM to sum OI/volume for PCR
_PCR_STRIKE_WINDOW = 5
# Decimal precision for PCR values
_PCR_DECIMAL_PRECISION = 4

NSE_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "NIFTYIT", "NIFTYPHARMA", "NIFTYBANK",
}
BSE_INDEX_SYMBOLS = {"SENSEX", "BANKEX", "SENSEX50"}


def _get_quote_exchange(base_symbol: str, exchange: str) -> str:
    if base_symbol in NSE_INDEX_SYMBOLS:
        return "NSE_INDEX"
    if base_symbol in BSE_INDEX_SYMBOLS:
        return "BSE_INDEX"
    if exchange.upper() in ("NFO", "BFO"):
        return "NSE" if exchange.upper() == "NFO" else "BSE"
    if exchange.upper() in CRYPTO_EXCHANGES:
        return exchange.upper()
    return exchange.upper()


def _convert_timestamp_to_ist(df: pd.DataFrame) -> pd.DataFrame | None:
    ist = pytz.timezone("Asia/Kolkata")
    try:
        if "timestamp" not in df.columns:
            return None
        try:
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df["datetime"] = df["datetime"].dt.tz_convert(ist)
        except Exception:
            try:
                df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df["datetime"] = df["datetime"].dt.tz_convert(ist)
            except Exception:
                df["datetime"] = pd.to_datetime(df["timestamp"])
                if df["datetime"].dt.tz is None:
                    df["datetime"] = df["datetime"].dt.tz_localize("UTC").dt.tz_convert(ist)
                else:
                    df["datetime"] = df["datetime"].dt.tz_convert(ist)
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
) -> tuple[bool, dict, int]:
    """
    Compute intraday PCR(OI) and PCR(Volume) time series.

    Args:
        underlying:  Underlying symbol (e.g., NIFTY)
        exchange:    Exchange (NSE_INDEX, BSE_INDEX, NFO, BFO, …)
        expiry_date: Expiry in DDMMMYY format
        interval:    Candle interval (1m, 3m, 5m, 15m, 30m, 1h, 1d)
        api_key:     OpenAlgo API key
        days:        Number of trading days of history (1–5)

    Returns:
        (success, response_dict, status_code)
    """
    try:
        ist = pytz.timezone("Asia/Kolkata")
        start_date_str, end_date_str = _resolve_trading_window(days, ist)

        base_symbol = underlying.upper()
        quote_exchange = _get_quote_exchange(base_symbol, exchange)
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

        # Step 2: Fetch underlying history
        success_u, resp_u, _ = get_history(
            symbol=underlying_quote_symbol,
            exchange=quote_exchange,
            interval=interval,
            start_date=start_date_str,
            end_date=end_date_str,
            api_key=api_key,
        )
        if not success_u:
            return False, {
                "status": "error",
                "message": f"Failed to fetch underlying history: {resp_u.get('message', '')}",
            }, 400

        df_underlying = pd.DataFrame(resp_u.get("data", []))
        if df_underlying.empty:
            return False, {"status": "error", "message": "No underlying history data"}, 404

        df_underlying = _convert_timestamp_to_ist(df_underlying)
        if df_underlying is None:
            return False, {"status": "error", "message": "Failed to parse underlying timestamps"}, 500

        # Step 3: Compute ATM per candle
        atm_per_row = [
            find_atm_strike_from_actual(float(row["close"]), available_strikes)
            for _, row in df_underlying.iterrows()
        ]
        df_underlying["atm_strike"] = atm_per_row
        unique_strikes = sorted(s for s in set(atm_per_row) if s is not None)

        if not unique_strikes:
            return False, {"status": "error", "message": "Could not determine ATM strikes"}, 400

        # Step 4: For each unique ATM, fetch CE+PE history (close, oi, volume)
        # The PCR window is: ATM ± _PCR_STRIKE_WINDOW strikes from available_strikes
        strike_history: dict[float, dict] = {}  # strike -> {ts: {ce_oi, ce_vol, pe_oi, pe_vol}}

        for atm in unique_strikes:
            atm_idx = available_strikes.index(atm) if atm in available_strikes else None
            if atm_idx is None:
                continue

            window_strikes = available_strikes[
                max(0, atm_idx - _PCR_STRIKE_WINDOW):
                min(len(available_strikes), atm_idx + _PCR_STRIKE_WINDOW + 1)
            ]

            # We store OI/volume data keyed by (atm, ts)
            for k_strike in window_strikes:
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

                if k_strike not in strike_history:
                    strike_history[k_strike] = {"ce": {}, "pe": {}}
                strike_history[k_strike]["ce"].update(ce_rows)
                strike_history[k_strike]["pe"].update(pe_rows)

        # Step 5: Build PCR series per candle
        series = []
        has_oi_data = False

        for ts, row in df_underlying.iterrows():
            spot = float(row["close"])
            atm = row["atm_strike"]
            if atm is None:
                continue

            atm_idx = available_strikes.index(atm) if atm in available_strikes else None
            if atm_idx is None:
                continue

            window_strikes = available_strikes[
                max(0, atm_idx - _PCR_STRIKE_WINDOW):
                min(len(available_strikes), atm_idx + _PCR_STRIKE_WINDOW + 1)
            ]

            total_ce_oi = 0.0
            total_pe_oi = 0.0
            total_ce_vol = 0.0
            total_pe_vol = 0.0
            atm_ce_close = 0.0
            atm_pe_close = 0.0

            for k_strike in window_strikes:
                sh = strike_history.get(k_strike, {"ce": {}, "pe": {}})
                ce_data = sh["ce"].get(ts, {})
                pe_data = sh["pe"].get(ts, {})
                total_ce_oi += ce_data.get("oi", 0)
                total_pe_oi += pe_data.get("oi", 0)
                total_ce_vol += ce_data.get("volume", 0)
                total_pe_vol += pe_data.get("volume", 0)
                if k_strike == atm:
                    atm_ce_close = ce_data.get("close", 0)
                    atm_pe_close = pe_data.get("close", 0)

            pcr_oi = round(total_pe_oi / total_ce_oi, _PCR_DECIMAL_PRECISION) if total_ce_oi > 0 else None
            pcr_volume = round(total_pe_vol / total_ce_vol, _PCR_DECIMAL_PRECISION) if total_ce_vol > 0 else None
            synthetic_future = (
                round(atm + atm_ce_close - atm_pe_close, 2)
                if atm_ce_close and atm_pe_close
                else None
            )

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
                }
            )

        # Trim to last N trading days
        series = _cap_last_n_trading_dates(series, days, ist)

        # Step 6: Live snapshot PCR fallback
        live_pcr_oi = None
        live_pcr_volume = None
        success_oc, oc_resp, _ = get_option_chain(
            underlying=base_symbol,
            exchange=exchange,
            expiry_date=expiry_date,
            strike_count=15,
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

        # If no OI in history, build a flat series from live snapshot
        live_only = not has_oi_data
        if live_only and live_pcr_oi is not None:
            for pt in series:
                pt["pcr_oi"] = live_pcr_oi
                pt["pcr_volume"] = live_pcr_volume

        if not series:
            return False, {"status": "error", "message": "No PCR data available"}, 404

        # Current LTP
        success_q, quote_resp, _ = get_quotes(
            symbol=underlying_quote_symbol, exchange=quote_exchange, api_key=api_key
        )
        underlying_ltp = quote_resp.get("data", {}).get("ltp", 0) if success_q else 0

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
                    "series": series,
                },
            },
            200,
        )

    except Exception as exc:
        logger.exception(f"Error in get_pcr_chart_data: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500
