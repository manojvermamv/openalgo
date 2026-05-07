"""
Buyer Edge — PCR Time Series Service

Builds an intraday Put/Call Ratio time series using the same approach as
straddle_chart_service.py:

  1. Fetch underlying history for the requested interval/window.
  2. For each candle, determine the ATM strike from the underlying close.
  3. Batch fetch CE and PE option history for every unique ATM strike that appears.
  4. For each timestamp, sum CE OI/volume and PE OI/volume across an n-strike
     window around ATM and compute:
       PCR(OI)     = total_pe_oi    / total_ce_oi
       PCR(Volume) = total_pe_volume / total_ce_volume
  5. Compute day-anchored VWAP of PCR(OI) for reference.

Redesigned to follow the OpenAlgo Timeseries infrastructure:
  - Batch-fetching of symbol history to respect broker rate limits.
  - Aligned time grid for all symbols.
  - IST-centralised datetime logic from utils/datetime_utils.
"""

import time
from datetime import datetime
from typing import Any

import pandas as pd

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
from utils.datetime_utils import IST, to_ist_epoch, get_ist_now

logger = get_logger(__name__)

# Decimal precision for PCR values
_PCR_DECIMAL_PRECISION = 4


def _fetch_history_batch(
    symbols: list[dict],
    interval: str,
    start_date: str,
    end_date: str,
    api_key: str,
) -> dict[str, list[dict]]:
    """
    Fetch history for multiple symbols sequentially with rate limiting.
    Mirrors the logic in timeseries_service.py for consistency.
    """
    results = {}
    BATCH_SIZE = 5
    INDIVIDUAL_DELAY = 0.5
    BATCH_DELAY = 1.0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        for item in batch:
            sym = item["symbol"]
            exch = item["exchange"]
            success, resp, _ = get_history(
                symbol=sym,
                exchange=exch,
                interval=interval,
                start_date=start_date,
                end_date=end_date,
                api_key=api_key,
            )
            if success and resp.get("data"):
                results[f"{sym}:{exch}"] = resp["data"]
            else:
                results[f"{sym}:{exch}"] = []
            time.sleep(INDIVIDUAL_DELAY)

        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_DELAY)

    return results


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
    Compute intraday PCR(OI) and PCR(Volume) time series using batch fetching.
    """
    try:
        start_date_str, end_date_str = _resolve_trading_window(days, IST)

        base_symbol = underlying.upper()
        quote_exchange = get_buyer_edge_quote_exchange(base_symbol, exchange)
        options_exchange = get_option_exchange(quote_exchange)

        # Resolve underlying symbol (handle crypto perps)
        underlying_quote_symbol = base_symbol
        if exchange.upper() in CRYPTO_EXCHANGES:
            _perp = fno_search_symbols(
                query=f"{base_symbol}USDFUT",
                exchange=exchange,
                instrumenttype=INSTRUMENT_PERPFUT,
                limit=1,
            )
            if _perp:
                underlying_quote_symbol = _perp[0]["symbol"]

        # 1. Fetch underlying history
        success_u, resp_u, _ = get_history(
            symbol=underlying_quote_symbol,
            exchange=quote_exchange,
            interval=interval,
            start_date=start_date_str,
            end_date=end_date_str,
            api_key=api_key,
        )
        if not success_u or not resp_u.get("data"):
            logger.warning(
                f"PCR [{underlying}|{exchange}]: underlying history empty "
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

        # 3. Batch fetch all needed option symbols
        symbols_to_fetch = []
        all_window_strikes = set()
        _build_sym = construct_crypto_option_symbol if exchange.upper() in CRYPTO_EXCHANGES else construct_option_symbol

        for atm in unique_atm_strikes:
            try:
                idx = available_strikes.index(atm)
                window = available_strikes[max(0, idx - pcr_strike_window) : min(len(available_strikes), idx + pcr_strike_window + 1)]
                all_window_strikes.update(window)
            except ValueError:
                continue

        for strike in sorted(all_window_strikes):
            symbols_to_fetch.append({"symbol": _build_sym(base_symbol, expiry_date.upper(), strike, "CE"), "exchange": options_exchange})
            symbols_to_fetch.append({"symbol": _build_sym(base_symbol, expiry_date.upper(), strike, "PE"), "exchange": options_exchange})

        logger.debug(
            f"PCR [{underlying}|{expiry_date}]: {len(df_u)} underlying bars, "
            f"{len(unique_atm_strikes)} unique ATM strikes, "
            f"{len(symbols_to_fetch)} option symbols to batch-fetch"
        )

        # Batch fetch option history
        history_map = _fetch_history_batch(symbols_to_fetch, interval, start_date_str, end_date_str, api_key)

        # Build lookup table: {strike: {side: {ts: {oi, volume, close}}}}
        lookup = {}
        for strike in all_window_strikes:
            lookup[strike] = {"CE": {}, "PE": {}}
            for side in ["CE", "PE"]:
                sym = _build_sym(base_symbol, expiry_date.upper(), strike, side)
                candles = history_map.get(f"{sym}:{options_exchange}", [])
                for c in candles:
                    ts = to_ist_epoch(c["timestamp"])
                    lookup[strike][side][ts] = {
                        "oi": float(c.get("oi", 0) or 0),
                        "volume": float(c.get("volume", 0) or 0),
                        "close": float(c.get("close", 0) or 0),
                    }

        # Pre-compute per-strike intraday OI/LTP change (last candle − first candle).
        # This powers the IntradayOiChange chart on the BuyerEdge dashboard.
        strike_oi_changes = []
        for strike in sorted(all_window_strikes):
            ce_ts_map = lookup[strike]["CE"]
            pe_ts_map = lookup[strike]["PE"]
            ce_ts_sorted = sorted(ce_ts_map.keys())
            pe_ts_sorted = sorted(pe_ts_map.keys())
            if not ce_ts_sorted or not pe_ts_sorted:
                missing = ("CE" if not ce_ts_sorted else "") + ("PE" if not pe_ts_sorted else "")
                logger.debug(
                    f"PCR [{underlying}|{expiry_date}]: strike {strike} skipped — "
                    f"no candle data for {missing.strip()}"
                )
                continue
            ce_oi_first = ce_ts_map[ce_ts_sorted[0]]["oi"]
            ce_oi_last = ce_ts_map[ce_ts_sorted[-1]]["oi"]
            pe_oi_first = pe_ts_map[pe_ts_sorted[0]]["oi"]
            pe_oi_last = pe_ts_map[pe_ts_sorted[-1]]["oi"]
            ce_ltp_first = ce_ts_map[ce_ts_sorted[0]]["close"]
            ce_ltp_last = ce_ts_map[ce_ts_sorted[-1]]["close"]
            pe_ltp_first = pe_ts_map[pe_ts_sorted[0]]["close"]
            pe_ltp_last = pe_ts_map[pe_ts_sorted[-1]]["close"]
            strike_oi_changes.append({
                "strike": strike,
                "ce_oi_chg": round(ce_oi_last - ce_oi_first),
                "pe_oi_chg": round(pe_oi_last - pe_oi_first),
                "ce_ltp_chg": round(ce_ltp_last - ce_ltp_first, 2),
                "pe_ltp_chg": round(pe_ltp_last - pe_ltp_first, 2),
            })

        logger.debug(
            f"PCR [{underlying}|{expiry_date}]: strike_oi_changes={len(strike_oi_changes)}/{len(all_window_strikes)} strikes computed"
        )

        # 4. Align and compute PCR series
        series = []
        prev_strike_oi = {}
        first_candle = True

        # Use the underlying's timestamps as the master grid
        for ts in sorted(df_u.index):
            row_u = df_u.loc[ts]
            spot = float(row_u["close"])
            atm = atm_per_ts.get(ts)
            if not atm:
                continue

            try:
                idx = available_strikes.index(atm)
                window_strikes = available_strikes[max(0, idx - pcr_strike_window) : min(len(available_strikes), idx + pcr_strike_window + 1)]
            except ValueError:
                continue

            t_ce_oi, t_pe_oi, t_ce_vol, t_pe_vol = 0.0, 0.0, 0.0, 0.0
            adv, dec, ce_adv, ce_dec, pe_adv, pe_dec = 0, 0, 0, 0, 0, 0
            atm_ce_ltp, atm_pe_ltp = 0.0, 0.0

            for strike in window_strikes:
                ce_d = lookup[strike]["CE"].get(ts, {})
                pe_d = lookup[strike]["PE"].get(ts, {})
                
                ce_oi = ce_d.get("oi", 0)
                pe_oi = pe_d.get("oi", 0)
                t_ce_oi += ce_oi
                t_pe_oi += pe_oi
                t_ce_vol += ce_d.get("volume", 0)
                t_pe_vol += pe_d.get("volume", 0)

                if strike == atm:
                    atm_ce_ltp = ce_d.get("close", 0)
                    atm_pe_ltp = pe_d.get("close", 0)

                # Breadth / ADR logic
                curr_oi = ce_oi + pe_oi
                if not first_candle:
                    prev_oi = prev_strike_oi.get(strike, (0, 0, 0))
                    if curr_oi > prev_oi[0]: adv += 1
                    elif curr_oi < prev_oi[0]: dec += 1
                    
                    if ce_oi > prev_oi[1]: ce_adv += 1
                    elif ce_oi < prev_oi[1]: ce_dec += 1
                    
                    if pe_oi > prev_oi[2]: pe_adv += 1
                    elif pe_oi < prev_oi[2]: pe_dec += 1
                
                prev_strike_oi[strike] = (curr_oi, ce_oi, pe_oi)

            pcr_oi = round(t_pe_oi / t_ce_oi, _PCR_DECIMAL_PRECISION) if t_ce_oi > 0 else None
            pcr_vol = round(t_pe_vol / t_ce_vol, _PCR_DECIMAL_PRECISION) if t_ce_vol > 0 else None
            adr = round(adv / dec, _PCR_DECIMAL_PRECISION) if dec > 0 else (10.0 if adv > 0 else None)
            
            series.append({
                "time": int(ts),
                "spot": round(spot, 2),
                "atm_strike": atm,
                "pcr_oi": pcr_oi,
                "pcr_volume": pcr_vol,
                "ce_oi": round(t_ce_oi),
                "pe_oi": round(t_pe_oi),
                "advances": adv,
                "declines": dec,
                "adr": adr,
                "ce_advances": ce_adv,
                "ce_declines": ce_dec,
                "pe_advances": pe_adv,
                "pe_declines": pe_dec,
                "atm_ce_ltp": round(atm_ce_ltp, 2),
                "atm_pe_ltp": round(atm_pe_ltp, 2),
                "synthetic_future": round(atm + atm_ce_ltp - atm_pe_ltp, 2) if atm_ce_ltp and atm_pe_ltp else None,
                # Velocity fields populated in a second pass below
                "ce_oi_velocity": 0,
                "pe_oi_velocity": 0,
                "pcr_oi_velocity": 0.0,
            })
            first_candle = False

        # Second pass: compute bar-by-bar OI velocity (first bar stays at 0)
        for i in range(1, len(series)):
            prev = series[i - 1]
            curr = series[i]
            curr["ce_oi_velocity"] = round(curr["ce_oi"] - prev["ce_oi"])
            curr["pe_oi_velocity"] = round(curr["pe_oi"] - prev["pe_oi"])
            curr_pcr = curr.get("pcr_oi")
            prev_pcr = prev.get("pcr_oi")
            if curr_pcr is not None and prev_pcr is not None:
                curr["pcr_oi_velocity"] = round(curr_pcr - prev_pcr, _PCR_DECIMAL_PRECISION)
            else:
                curr["pcr_oi_velocity"] = None

        # 5. Live Snapshot
        live_pcr_oi, live_pcr_vol = None, None
        success_oc, oc_resp, _ = get_option_chain(base_symbol, exchange, expiry_date, max_snapshot_strikes, api_key)
        if success_oc:
            ce_oi_t = sum((i.get("ce") or {}).get("oi", 0) or 0 for i in oc_resp.get("chain", []))
            pe_oi_t = sum((i.get("pe") or {}).get("oi", 0) or 0 for i in oc_resp.get("chain", []))
            ce_v_t = sum((i.get("ce") or {}).get("volume", 0) or 0 for i in oc_resp.get("chain", []))
            pe_v_t = sum((i.get("pe") or {}).get("volume", 0) or 0 for i in oc_resp.get("chain", []))
            if ce_oi_t > 0: live_pcr_oi = round(pe_oi_t / ce_oi_t, _PCR_DECIMAL_PRECISION)
            if ce_v_t > 0: live_pcr_vol = round(pe_v_t / ce_v_t, _PCR_DECIMAL_PRECISION)
        else:
            logger.warning(
                f"PCR [{underlying}|{expiry_date}]: live option chain snapshot failed — "
                "market may be closed; live_pcr_oi/vol will be None"
            )

        logger.debug(
            f"PCR [{underlying}|{expiry_date}]: series={len(series)} bars, "
            f"live_pcr_oi={live_pcr_oi}, live_pcr_vol={live_pcr_vol}"
        )

        # 6. Current Stats
        sq_ok, sq_resp, _ = get_quotes(underlying_quote_symbol, quote_exchange, api_key)
        ltp = sq_resp.get("data", {}).get("ltp", 0) if sq_ok else spot

        return (
            True,
            {
                "status": "success",
                "data": {
                    "underlying": base_symbol,
                    "underlying_ltp": ltp,
                    "expiry_date": expiry_date.upper(),
                    "interval": interval,
                    "current_pcr_oi": live_pcr_oi,
                    "current_pcr_volume": live_pcr_vol,
                    "strike_oi_changes": strike_oi_changes,
                    "series": _cap_last_n_trading_dates(series, days, IST),
                },
            },
            200,
        )

    except Exception as exc:
        logger.exception(f"Error in get_pcr_chart_data: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500


def get_spot_candles(underlying: str, exchange: str, interval: str, api_key: str, days: int = 5) -> tuple[bool, dict, int]:
    """Fetch OHLCV history for underlying spot."""
    try:
        start_date_str, end_date_str = _resolve_trading_window(days, IST)
        base_symbol = underlying.upper()
        quote_exchange = get_buyer_edge_quote_exchange(base_symbol, exchange)

        success, resp, _ = get_history(base_symbol, quote_exchange, interval, start_date_str, end_date_str, api_key)
        if not success:
            return False, {"status": "error", "message": "Failed to fetch spot history"}, 502

        candles = []
        for r in resp.get("data", []):
            candles.append({
                "time": int(to_ist_epoch(r["timestamp"])),
                "open": round(float(r.get("open", 0) or 0), 2),
                "high": round(float(r.get("high", 0) or 0), 2),
                "low": round(float(r.get("low", 0) or 0), 2),
                "close": round(float(r.get("close", 0) or 0), 2),
                "volume": int(r.get("volume", 0) or 0),
            })
        return True, {"status": "success", "underlying": base_symbol, "candles": candles}, 200
    except Exception as exc:
        logger.exception(f"Error in get_spot_candles: {exc}")
        return False, {"status": "error", "message": str(exc)}, 500
