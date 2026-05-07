"""
Buyer Edge Blueprint
Serves the Options Buyer Edge state engine.
"""

import re
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request, session
from flask_cors import cross_origin

from database.auth_db import get_api_key_for_tradingview
from extensions import socketio
from services.buyer_edge_service import get_buyer_edge_data
from services.buyer_edge_gex_service import get_gex_levels, get_gex_levels_cumulative
from services.buyer_edge_pcr_service import get_pcr_chart_data, get_spot_candles
from services.buyer_edge_ivr_service import get_iv_dashboard
from services.straddle_chart_service import get_straddle_chart_data
from utils.logging import get_logger
from utils.session import check_session_validity
from limiter import limiter

logger = get_logger(__name__)

buyer_edge_bp = Blueprint("buyer_edge_bp", __name__, url_prefix="/")

_VALID_LB_TF = {"1m", "3m", "5m", "10m", "15m", "30m", "1h"}
_VALID_INTERVALS = {"1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d"}

def _normalize_expiry(expiry_raw: str) -> str:
    """Normalize '06-FEB-26' -> '06FEB26' and validate."""
    clean = str(expiry_raw or "").strip().upper()
    if re.match(r"^\d{2}-[A-Z]{3}-\d{2}$", clean):
        result = clean.replace("-", "")
        logger.debug(f"Expiry normalized (dashed): '{clean}' → '{result}'")
        return result
    if re.match(r"^\d{2}[A-Z]{3}\d{2}$", clean):
        logger.debug(f"Expiry already in canonical form: '{clean}'")
        return clean
    logger.warning(f"Expiry normalization failed — unrecognized format: '{expiry_raw}'")
    return ""

def _get_api_key():
    username = session.get("user")
    if not username: return None
    return get_api_key_for_tradingview(username)

@buyer_edge_bp.route("/buyeredge/api/data", methods=["POST"])
@cross_origin()
@limiter.limit("15 per minute")
@check_session_validity
def buyer_edge_data():
    """Get Buyer Edge signal."""
    try:
        api_key = _get_api_key()
        if not api_key: return jsonify({"status": "error", "message": "API key required"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip().upper()
        exchange = data.get("exchange", "").strip().upper()
        expiry_date = _normalize_expiry(data.get("expiry_date", ""))

        if not underlying or not exchange or not expiry_date:
            return jsonify({"status": "error", "message": "Missing or invalid parameters"}), 400

        lb_bars = max(5, min(int(data.get("lb_bars", 20)), 100))
        lb_tf = str(data.get("lb_tf", "3m")).strip()
        if lb_tf not in _VALID_LB_TF: lb_tf = "3m"

        atm_mode = str(data.get("atm_mode", "auto")).strip().lower()
        manual_strike = data.get("manual_strike")
        if manual_strike is not None: manual_strike = float(manual_strike)

        success, response, status = get_buyer_edge_data(
            underlying, exchange, expiry_date, 10, api_key, lb_bars, lb_tf, atm_mode, manual_strike
        )
        return jsonify(response), status
    except Exception as e:
        logger.exception(f"Error in buyer_edge_data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@buyer_edge_bp.route("/buyeredge/api/pcr_chart", methods=["POST"])
@cross_origin()
@limiter.limit("15 per minute")
@check_session_validity
def buyer_edge_pcr_chart():
    """PCR chart data."""
    try:
        api_key = _get_api_key()
        if not api_key: return jsonify({"status": "error", "message": "API key required"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip().upper()
        exchange = data.get("exchange", "").strip().upper()
        expiry_date = _normalize_expiry(data.get("expiry_date", ""))
        interval = str(data.get("interval", "1m")).strip()
        days = max(1, min(int(data.get("days", 1)), 5))

        if not underlying or not exchange or not expiry_date:
            return jsonify({"status": "error", "message": "Missing or invalid parameters"}), 400

        success, response, status = get_pcr_chart_data(underlying, exchange, expiry_date, interval, api_key, days)
        return jsonify(response), status
    except Exception as e:
        logger.exception(f"Error in buyer_edge_pcr_chart: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@buyer_edge_bp.route("/buyeredge/api/unified_monitor", methods=["POST"])
@cross_origin()
@limiter.limit("20 per minute")
@check_session_validity
def buyer_edge_unified_monitor():
    """Parallel PCR, Straddle, GEX, IVR, and Analysis fetching."""
    try:
        api_key = _get_api_key()
        if not api_key: return jsonify({"status": "error", "message": "API key required"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip().upper()
        exchange = data.get("exchange", "").strip().upper()
        expiry_date = _normalize_expiry(data.get("expiry_date", ""))
        interval = str(data.get("interval", "1m")).strip()
        days = max(1, min(int(data.get("days", 1)), 5))

        atm_mode = str(data.get("atm_mode", "auto")).strip().lower()
        manual_strike = data.get("manual_strike")
        if manual_strike is not None: manual_strike = float(manual_strike)

        # Forwarded params (previously hardcoded in the service call)
        lb_bars = max(5, min(int(data.get("lb_bars", 20)), 100))
        lb_tf = str(data.get("lb_tf", "3m")).strip()
        if lb_tf not in _VALID_LB_TF: lb_tf = "3m"
        pcr_strike_window = max(1, min(int(data.get("pcr_strike_window", 10)), 20))
        max_snapshot_strikes = max(5, min(int(data.get("max_snapshot_strikes", 20)), 100))

        if not underlying or not exchange or not expiry_date:
            return jsonify({"status": "error", "message": "Missing or invalid parameters"}), 400

        with ThreadPoolExecutor(max_workers=4) as executor:
            f_straddle = executor.submit(get_straddle_chart_data, underlying, exchange, expiry_date, interval, api_key, days)
            f_pcr = executor.submit(get_pcr_chart_data, underlying, exchange, expiry_date, interval, api_key, days, pcr_strike_window, max_snapshot_strikes)
            f_gex = executor.submit(get_gex_levels, underlying, exchange, expiry_date, 20, api_key)
            f_ivr = executor.submit(get_iv_dashboard, underlying, exchange, [expiry_date], 10, api_key)

            res_s = f_straddle.result()
            res_p = f_pcr.result()
            res_gex = f_gex.result()
            res_ivr = f_ivr.result()
            
        pcr_series = []
        if res_p[0] and "data" in res_p[1] and "series" in res_p[1]["data"]:
            pcr_series = res_p[1]["data"]["series"]

        # Extract GEX levels and IVR data for signal engine enrichment
        gex_levels = res_gex[1].get("levels") if res_gex[0] else None
        ivr_data = res_ivr[1] if res_ivr[0] else None

        res_a = get_buyer_edge_data(
            underlying, exchange, expiry_date, max_snapshot_strikes, api_key,
            lb_bars, lb_tf, atm_mode, manual_strike,
            pcr_series=pcr_series, gex_levels=gex_levels, ivr_data=ivr_data,
        )

        payload = {
            "status": "success",
            "straddle": res_s[1],
            "pcr": res_p[1],
            "analysis": res_a[1] if res_a[0] else {"status": "error", "message": res_a[1].get("message", "Analysis failed")},
            # Include GEX and IVR data in the unified response so the frontend
            # can use them for display (GexLevels, IvrDashboard) without making
            # separate duplicate API calls on every refresh.
            "gex": res_gex[1] if res_gex[0] else None,
            "ivr": res_ivr[1] if res_ivr[0] else None,
        }

        logger.debug(
            f"unified_monitor [{underlying}|{exchange}|{expiry_date}]: "
            f"straddle={'ok' if res_s[0] else 'err'}, "
            f"pcr={'ok' if res_p[0] else 'err'}, "
            f"gex={'ok' if res_gex[0] else 'err'}, "
            f"ivr={'ok' if res_ivr[0] else 'err'}, "
            f"analysis={'ok' if res_a[0] else 'err'}, "
            f"pcr_series_bars={len(pcr_series)}"
        )

        # Broadcast result as a SocketIO event so the frontend can receive
        # live updates without polling once it subscribes to this channel.
        # OpenAlgo is a single-user-per-deployment platform, so broadcasting
        # to all clients on the default namespace is intentional — there is
        # only one authenticated session that could be connected at any time.
        socketio.emit("buyer_edge_update", {
            "underlying": underlying,
            "exchange": exchange,
            "expiry_date": expiry_date,
            "straddle_ok": res_s[0],
            "pcr_ok": res_p[0],
            "analysis_ok": res_a[0],
        })

        return jsonify(payload), 200
    except Exception as e:
        logger.exception(f"Error in unified_monitor: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@buyer_edge_bp.route("/buyeredge/api/gex_levels", methods=["POST"])
@cross_origin()
@limiter.limit("15 per minute")
@check_session_validity
def buyer_edge_gex_levels():
    """GEX levels."""
    try:
        api_key = _get_api_key()
        if not api_key: return jsonify({"status": "error", "message": "API key required"}), 401
        data = request.get_json(silent=True) or {}
        underlying, exchange = data.get("underlying", "").upper(), data.get("exchange", "").upper()
        mode = str(data.get("mode", "selected")).lower()

        if mode == "selected":
            expiry = _normalize_expiry(data.get("expiry_date", ""))
            success, resp, status = get_gex_levels(underlying, exchange, expiry, 20, api_key)
        else:
            expiries = [_normalize_expiry(e) for e in data.get("expiry_dates", []) if _normalize_expiry(e)][:4]
            success, resp, status = get_gex_levels_cumulative(underlying, exchange, expiries, 20, api_key)
        
        return jsonify(resp), status
    except Exception as e:
        logger.exception(f"Error in gex_levels: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@buyer_edge_bp.route("/buyeredge/api/iv_dashboard", methods=["POST"])
@cross_origin()
@limiter.limit("15 per minute")
@check_session_validity
def buyer_edge_iv_dashboard():
    """IV dashboard."""
    try:
        api_key = _get_api_key()
        if not api_key: return jsonify({"status": "error", "message": "API key required"}), 401
        data = request.get_json(silent=True) or {}
        underlying, exchange = data.get("underlying", "").upper(), data.get("exchange", "").upper()
        expiries = [_normalize_expiry(e) for e in data.get("expiry_dates", []) if _normalize_expiry(e)][:4]
        
        success, resp, status = get_iv_dashboard(underlying, exchange, expiries, 15, api_key)
        return jsonify(resp), status
    except Exception as e:
        logger.exception(f"Error in iv_dashboard: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@buyer_edge_bp.route("/buyeredge/api/spot_candles", methods=["POST"])
@cross_origin()
@limiter.limit("20 per minute")
@check_session_validity
def buyer_edge_spot_candles():
    """Spot candles."""
    try:
        api_key = _get_api_key()
        if not api_key: return jsonify({"status": "error", "message": "API key required"}), 401
        data = request.get_json(silent=True) or {}
        underlying, exchange = data.get("underlying", "").upper(), data.get("exchange", "").upper()
        interval = str(data.get("interval", "15m")).strip()
        days = max(1, min(int(data.get("days", 5)), 30))

        success, resp, status = get_spot_candles(underlying, exchange, interval, api_key, days)
        return jsonify(resp), status
    except Exception as e:
        logger.exception(f"Error in spot_candles: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
