"""
Custom Straddle Simulation Service
Simulates an intraday short ATM straddle with automated adjustments.

For each trading day:
1. ENTRY at first candle — sell ATM CE + PE
2. ADJUSTMENT when spot moves >= N points from entry strike — exit old, enter new ATM
3. EXIT at last candle — close position

Tracks cumulative PnL across days and returns a time series + trade log.
"""

from collections import defaultdict
from datetime import datetime, timedelta

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
from utils.datetime_utils import IST, to_ist_epoch, get_ist_now

logger = get_logger(__name__)

def _get_quote_exchange(base_symbol: str, underlying_exchange: str) -> str:
    """Determine the exchange to use for fetching underlying quotes."""
    from utils.constants import NSE_INDEX_SYMBOLS, BSE_INDEX_SYMBOLS
    if base_symbol in NSE_INDEX_SYMBOLS: return "NSE_INDEX"
    if base_symbol in BSE_INDEX_SYMBOLS: return "BSE_INDEX"
    if underlying_exchange.upper() in ("NFO", "BFO"):
        return "NSE" if underlying_exchange.upper() == "NFO" else "BSE"
    return underlying_exchange.upper()

def _calculate_days_to_expiry(expiry_date: str) -> int:
    """Calculate days remaining to expiry."""
    try:
        exp_dt = datetime.strptime(expiry_date.upper(), "%d%b%y").replace(hour=15, minute=30)
        exp_dt = IST.localize(exp_dt)
        return max(0, (exp_dt - IST.localize(datetime.now())).days)
    except Exception:
        return 0

def get_custom_straddle_simulation(
    underlying, exchange, expiry_date, interval, api_key,
    days=1, adjustment_points=50, lot_size=65, lots=1,
):
    """Simulate an intraday short ATM straddle with N-point adjustments."""
    try:
        start_date_str, end_date_str = _resolve_trading_window(days, IST)
        quantity = lot_size * lots
        base_symbol = underlying.upper()
        quote_exchange = _get_quote_exchange(base_symbol, exchange)
        options_exchange = get_option_exchange(quote_exchange)

        # Handle crypto perpetual symbol lookup
        underlying_quote_symbol = base_symbol
        if exchange.upper() in CRYPTO_EXCHANGES:
            _perp = fno_search_symbols(query=f"{base_symbol}USDFUT", exchange=exchange, instrumenttype=INSTRUMENT_PERPFUT, limit=1)
            if not _perp: return False, {"status": "error", "message": "No perpetual futures found"}, 404
            underlying_quote_symbol = _perp[0]["symbol"]

        # Available strikes
        available_strikes = get_available_strikes(base_symbol, expiry_date.upper(), "CE", options_exchange)
        if not available_strikes: return False, {"status": "error", "message": "No strikes found"}, 404

        # Underlying history
        success_u, resp_u, _ = get_history(underlying_quote_symbol, quote_exchange, interval, start_date_str, end_date_str, api_key)
        if not success_u or not resp_u.get("data"):
            return False, {"status": "error", "message": "No underlying history"}, 404

        df_u = pd.DataFrame(resp_u["data"])
        df_u["timestamp_ist"] = df_u["timestamp"].apply(lambda x: IST.localize(datetime.fromtimestamp(to_ist_epoch(x))))
        
        # Determine ATM per row
        df_u["atm_strike"] = df_u["close"].apply(lambda x: find_atm_strike_from_actual(float(x), available_strikes))
        unique_strikes = set(df_u["atm_strike"].dropna().unique())

        # Fetch option history for all unique ATM strikes
        _build_sym = construct_crypto_option_symbol if exchange.upper() in CRYPTO_EXCHANGES else construct_option_symbol
        strike_data = {}
        for strike in sorted(unique_strikes):
            ce_sym = _build_sym(base_symbol, expiry_date.upper(), strike, "CE")
            pe_sym = _build_sym(base_symbol, expiry_date.upper(), strike, "PE")
            
            ce_map, pe_map = {}, {}
            for sym, storage in [(ce_sym, ce_map), (pe_sym, pe_map)]:
                s_l, r_l, _ = get_history(sym, options_exchange, interval, start_date_str, end_date_str, api_key)
                if s_l:
                    for c in r_l.get("data", []):
                        storage[to_ist_epoch(c["timestamp"])] = float(c["close"])
            strike_data[strike] = {"ce": ce_map, "pe": pe_map}

        # Simulation
        daily_candles = defaultdict(list)
        for _, row in df_u.iterrows():
            ts_epoch = int(row["timestamp_ist"].timestamp())
            daily_candles[row["timestamp_ist"].date()].append((ts_epoch, row))

        cumulative_realized, total_adjustments = 0.0, 0
        pnl_series, trades = [], []
        
        active_days = sorted(daily_candles.keys())[-max(1, days):]
        for day in active_days:
            candles = daily_candles[day]
            e_strike, e_ce, e_pe = None, None, None
            day_realized, day_adjustments, last_unrealized = 0.0, 0, 0.0

            for i, (ts, row) in enumerate(candles):
                spot, atm = float(row["close"]), row["atm_strike"]
                if atm is None or atm not in strike_data: continue
                
                is_last = i == len(candles) - 1

                # ENTRY
                if e_strike is None:
                    c_atm = strike_data[atm]["ce"].get(ts)
                    p_atm = strike_data[atm]["pe"].get(ts)
                    if c_atm is None or p_atm is None: continue
                    e_strike, e_ce, e_pe = atm, c_atm, p_atm
                    trades.append({"time": ts, "type": "ENTRY", "strike": atm, "ce_price": round(c_atm, 2), "pe_price": round(p_atm, 2), "spot": round(spot, 2), "cumulative_pnl": round(cumulative_realized, 2)})
                else:
                    # ADJUSTMENT
                    if abs(atm - e_strike) >= adjustment_points:
                        old_c = strike_data[e_strike]["ce"].get(ts)
                        old_p = strike_data[e_strike]["pe"].get(ts)
                        new_c = strike_data[atm]["ce"].get(ts)
                        new_p = strike_data[atm]["pe"].get(ts)
                        if all(v is not None for v in [old_c, old_p, new_c, new_p]):
                            leg_pnl = ((e_ce - old_c) + (e_pe - old_p)) * quantity
                            day_realized += leg_pnl
                            day_adjustments += 1
                            trades.append({"time": ts, "type": "ADJUSTMENT", "old_strike": e_strike, "strike": atm, "exit_ce": round(old_c, 2), "exit_pe": round(old_p, 2), "ce_price": round(new_c, 2), "pe_price": round(new_p, 2), "spot": round(spot, 2), "leg_pnl": round(leg_pnl, 2), "cumulative_pnl": round(cumulative_realized + day_realized, 2)})
                            e_strike, e_ce, e_pe = atm, new_c, new_p

                # Current PnL
                cur_c = strike_data[e_strike]["ce"].get(ts)
                cur_p = strike_data[e_strike]["pe"].get(ts)
                unrealized = ((e_ce - cur_c) + (e_pe - cur_p)) * quantity if cur_c is not None and cur_p is not None else last_unrealized
                last_unrealized = unrealized
                total_pnl = cumulative_realized + day_realized + unrealized

                atm_ce = strike_data[atm]["ce"].get(ts, 0) or 0
                atm_pe = strike_data[atm]["pe"].get(ts, 0) or 0
                pnl_series.append({
                    "time": ts, "pnl": round(total_pnl, 2), "spot": round(spot, 2), "atm_strike": atm,
                    "ce_price": round(atm_ce, 2), "pe_price": round(atm_pe, 2), "straddle": round(atm_ce + atm_pe, 2),
                    "adjustments": total_adjustments + day_adjustments
                })

                # EXIT
                if is_last and e_strike is not None:
                    ex_c, ex_p = strike_data[e_strike]["ce"].get(ts), strike_data[e_strike]["pe"].get(ts)
                    leg_pnl = ((e_ce - ex_c) + (e_pe - ex_p)) * quantity if ex_c is not None and ex_p is not None else last_unrealized
                    trades.append({"time": ts, "type": "EXIT", "strike": e_strike, "ce_price": round(ex_c or 0, 2), "pe_price": round(ex_p or 0, 2), "spot": round(spot, 2), "leg_pnl": round(leg_pnl, 2), "cumulative_pnl": round(cumulative_realized + day_realized + leg_pnl, 2)})
                    cumulative_realized += day_realized + leg_pnl

            total_adjustments += day_adjustments

        if not pnl_series: return False, {"status": "error", "message": "No simulation data"}, 404

        success_q, q_resp, _ = get_quotes(underlying_quote_symbol, quote_exchange, api_key)
        ltp = q_resp.get("data", {}).get("ltp", 0) if success_q else 0

        return True, {
            "status": "success",
            "data": {
                "underlying": base_symbol, "underlying_ltp": ltp, "expiry_date": expiry_date.upper(),
                "interval": interval, "days_to_expiry": _calculate_days_to_expiry(expiry_date),
                "pnl_series": pnl_series, "trades": trades,
                "summary": {"total_pnl": round(cumulative_realized, 2), "total_adjustments": total_adjustments}
            }
        }, 200

    except Exception as e:
        logger.exception(f"Error in custom straddle simulation: {e}")
        return False, {"status": "error", "message": str(e)}, 500
