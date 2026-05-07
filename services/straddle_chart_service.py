"""
Straddle Chart Service
Computes Dynamic ATM Straddle time series from historical candle data.

For each candle timestamp, determines the ATM strike from the underlying close,
then batch-fetches corresponding CE and PE option prices to compute:
- Straddle value = CE + PE
- Synthetic Future = Strike + CE - PE

Redesigned to follow the OpenAlgo Timeseries infrastructure:
- Batch-fetching of symbol history to respect broker rate limits.
- IST-centralised datetime logic from utils/datetime_utils.
"""

import time
from datetime import datetime
from typing import Any

import pandas as pd

from services.history_service import get_history
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
from utils.datetime_utils import IST, to_ist_epoch

logger = get_logger(__name__)

# Index symbols that need NSE_INDEX/BSE_INDEX for quotes
NSE_INDEX_SYMBOLS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "NIFTYIT",
    "NIFTYPHARMA",
    "NIFTYBANK",
}

BSE_INDEX_SYMBOLS = {"SENSEX", "BANKEX", "SENSEX50"}

def _get_quote_exchange(base_symbol: str, underlying_exchange: str) -> str:
    """Determine the exchange to use for fetching underlying quotes."""
    if base_symbol in NSE_INDEX_SYMBOLS:
        return "NSE_INDEX"
    if base_symbol in BSE_INDEX_SYMBOLS:
        return "BSE_INDEX"
    if underlying_exchange.upper() in ("NFO", "BFO"):
        return "NSE" if underlying_exchange.upper() == "NFO" else "BSE"
    return underlying_exchange.upper()


def _fetch_history_batch(
    symbols: list[dict],
    interval: str,
    start_date: str,
    end_date: str,
    api_key: str,
) -> dict[str, list[dict]]:
    """Fetch history for multiple symbols with rate limiting."""
    results = {}
    BATCH_SIZE = 5
    INDIVIDUAL_DELAY = 0.5
    BATCH_DELAY = 1.0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        for item in batch:
            sym, exch = item["symbol"], item["exchange"]
            success, resp, _ = get_history(sym, exch, interval, start_date, end_date, api_key)
            results[f"{sym}:{exch}"] = resp.get("data", []) if success else []
            time.sleep(INDIVIDUAL_DELAY)
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_DELAY)
    return results


def get_straddle_chart_data(
    underlying: str,
    exchange: str,
    expiry_date: str,
    interval: str,
    api_key: str,
    days: int = 5,
) -> tuple[bool, dict, int]:
    """Compute Dynamic ATM Straddle time series using batch fetching."""
    try:
        start_date_str, end_date_str = _resolve_trading_window(days, IST)

        base_symbol = underlying.upper()
        quote_exchange = _get_quote_exchange(base_symbol, exchange)
        options_exchange = get_option_exchange(quote_exchange)

        # Resolve underlying symbol
        underlying_quote_symbol = base_symbol
        if exchange.upper() in CRYPTO_EXCHANGES:
            _perp = fno_search_symbols(query=f"{base_symbol}USDFUT", exchange=exchange, instrumenttype=INSTRUMENT_PERPFUT, limit=1)
            if _perp:
                underlying_quote_symbol = _perp[0]["symbol"]

        # 1. Fetch underlying history
        success_u, resp_u, _ = get_history(underlying_quote_symbol, quote_exchange, interval, start_date_str, end_date_str, api_key)
        if not success_u or not resp_u.get("data"):
            logger.warning(
                f"Straddle [{underlying}|{exchange}]: underlying history empty "
                f"(window: {start_date_str}→{end_date_str}) — market may be closed"
            )
            return False, {"status": "error", "message": "Failed to fetch underlying history"}, 400

        df_u = pd.DataFrame(resp_u["data"])
        df_u["timestamp"] = df_u["timestamp"].apply(to_ist_epoch)
        df_u.set_index("timestamp", inplace=True)

        # 2. Identify unique ATM strikes
        available_strikes = get_available_strikes(base_symbol, expiry_date.upper(), "CE", options_exchange)
        if not available_strikes:
            return False, {"status": "error", "message": "No strikes found"}, 404

        atm_per_ts = {}
        unique_atm_strikes = set()
        for ts, row in df_u.iterrows():
            atm = find_atm_strike_from_actual(float(row["close"]), available_strikes)
            if atm:
                atm_per_ts[ts] = atm
                unique_atm_strikes.add(atm)

        # 3. Batch fetch needed option symbols
        symbols_to_fetch = []
        _build_sym = construct_crypto_option_symbol if exchange.upper() in CRYPTO_EXCHANGES else construct_option_symbol
        for strike in sorted(unique_atm_strikes):
            symbols_to_fetch.append({"symbol": _build_sym(base_symbol, expiry_date.upper(), strike, "CE"), "exchange": options_exchange})
            symbols_to_fetch.append({"symbol": _build_sym(base_symbol, expiry_date.upper(), strike, "PE"), "exchange": options_exchange})

        logger.debug(
            f"Straddle [{underlying}|{expiry_date}]: {len(df_u)} underlying bars, "
            f"{len(unique_atm_strikes)} unique ATM strikes, "
            f"{len(symbols_to_fetch)} option symbols to batch-fetch"
        )

        history_map = _fetch_history_batch(symbols_to_fetch, interval, start_date_str, end_date_str, api_key)

        # Build lookup table: {strike: {side: {ts: close}}}
        lookup = {}
        for strike in unique_atm_strikes:
            lookup[strike] = {"CE": {}, "PE": {}}
            for side in ["CE", "PE"]:
                sym = _build_sym(base_symbol, expiry_date.upper(), strike, side)
                for c in history_map.get(f"{sym}:{options_exchange}", []):
                    lookup[strike][side][to_ist_epoch(c["timestamp"])] = float(c.get("close", 0) or 0)

        # 4. Merge series
        series = []
        for ts in sorted(df_u.index):
            spot = float(df_u.loc[ts, "close"])
            atm = atm_per_ts.get(ts)
            if not atm or atm not in lookup:
                continue

            ce_ltp = lookup[atm]["CE"].get(ts)
            pe_ltp = lookup[atm]["PE"].get(ts)
            if ce_ltp is None or pe_ltp is None:
                continue

            series.append({
                "time": int(ts),
                "spot": round(spot, 2),
                "atm_strike": atm,
                "ce_price": round(ce_ltp, 2),
                "pe_price": round(pe_ltp, 2),
                "straddle": round(ce_ltp + pe_ltp, 2),
                "synthetic_future": round(atm + ce_ltp - pe_ltp, 2)
            })

        if not series:
            logger.warning(
                f"Straddle [{underlying}|{expiry_date}]: empty series after merge — "
                f"underlying bars={len(df_u)}, unique_atm_strikes={len(unique_atm_strikes)}, "
                "CE/PE lookups may be missing; market may be closed or strikes unavailable"
            )
            return False, {"status": "error", "message": "No straddle data available"}, 404

        # 5. Metadata
        sq_ok, sq_resp, _ = get_quotes(underlying_quote_symbol, quote_exchange, api_key)
        ltp = sq_resp.get("data", {}).get("ltp", 0) if sq_ok else spot
        
        # Days to expiry
        dte = 0
        try:
            exp_dt = datetime.strptime(expiry_date.upper(), "%d%b%y").replace(hour=15, minute=30)
            exp_dt = IST.localize(exp_dt)
            dte = max(0, (exp_dt - IST.localize(datetime.now())).days)
        except Exception: pass

        logger.debug(
            f"Straddle [{underlying}|{expiry_date}]: series={len(series)} bars, DTE={dte}"
        )

        return (
            True,
            {
                "status": "success",
                "data": {
                    "underlying": base_symbol,
                    "underlying_ltp": ltp,
                    "expiry_date": expiry_date.upper(),
                    "interval": interval,
                    "days_to_expiry": dte,
                    "series": _cap_last_n_trading_dates(series, days, IST),
                },
            },
            200,
        )

    except Exception as e:
        logger.exception(f"Error in get_straddle_chart_data: {e}")
        return False, {"status": "error", "message": str(e)}, 500
