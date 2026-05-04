"""
Buyer Edge Blueprint

Serves the Options Buyer Edge state engine.

Endpoints:
    POST /buyeredge/api/data
        Computes all 5 modules (Market State, OI Intelligence, Greeks,
        Straddle, Signal) and returns a NO_TRADE / WATCH / EXECUTE signal.

    POST /buyeredge/api/gex_levels
        Advanced GEX Levels: per-strike Net GEX, Gamma Flip / HVL,
        Call/Put Gamma Walls.  Supports single-expiry (mode=selected) and
        cumulative multi-expiry (mode=cumulative) modes.

    POST /buyeredge/api/pcr_chart
        PCR(OI) and PCR(Volume) time series with spot and synthetic futures
        price.  Same interval/days controls as the straddle chart.

    POST /buyeredge/api/iv_dashboard
        IVRank (TastyTrade formula), IVx, Vertical CALL/PUT Skew, Horizontal
        IVx Skew, and per-expiry detail for Calendar/Diagonal analysis.
"""

import re

from flask import Blueprint, jsonify, request, session
from flask_cors import cross_origin

from database.auth_db import get_api_key_for_tradingview
from services.buyer_edge_service import get_buyer_edge_data
from services.buyer_edge_gex_service import get_gex_levels, get_gex_levels_cumulative
from services.buyer_edge_pcr_service import get_pcr_chart_data, get_spot_candles
from services.buyer_edge_ivr_service import get_iv_dashboard
from services.straddle_chart_service import get_straddle_chart_data
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

buyer_edge_bp = Blueprint("buyer_edge_bp", __name__, url_prefix="/")


@buyer_edge_bp.route("/buyeredge/api/data", methods=["POST"])
@cross_origin()
@check_session_validity
def buyer_edge_data():
    """Get Buyer Edge signal for the given underlying and expiry."""
    try:
        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify(
                {
                    "status": "error",
                    "message": "API key not configured. Please generate an API key in /apikey",
                }
            ), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip()[:20]
        exchange = data.get("exchange", "").strip()[:20]
        expiry_date = data.get("expiry_date", "").strip()[:10]
        try:
            strike_count = int(data.get("strike_count", 10))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "strike_count must be a valid integer"}), 400

        try:
            lb_bars = int(data.get("lb_bars", 20))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "lb_bars must be a valid integer"}), 400

        _VALID_LB_TF = {"1m", "3m", "5m", "10m", "15m", "30m", "1h"}
        lb_tf = str(data.get("lb_tf", "3m")).strip()
        if lb_tf not in _VALID_LB_TF:
            return jsonify({"status": "error", "message": f"lb_tf must be one of {sorted(_VALID_LB_TF)}"}), 400

        if not underlying or not exchange or not expiry_date:
            return jsonify(
                {
                    "status": "error",
                    "message": "underlying, exchange, and expiry_date are required",
                }
            ), 400

        if not re.match(r"^[A-Z0-9]+$", underlying.upper()) or not re.match(
            r"^[A-Z0-9_]+$", exchange.upper()
        ):
            return jsonify({"status": "error", "message": "Invalid input format"}), 400

        if not re.match(r"^\d{2}[A-Z]{3}\d{2}$", expiry_date.upper()):
            return jsonify(
                {"status": "error", "message": "Invalid expiry_date format. Expected DDMMMYY"}
            ), 400

        # Clamp strike_count to a reasonable range
        strike_count = max(2, min(strike_count, 30))
        # Clamp lb_bars to a reasonable range
        lb_bars = max(5, min(lb_bars, 100))

        success, response, status_code = get_buyer_edge_data(
            underlying=underlying.upper(),
            exchange=exchange.upper(),
            expiry_date=expiry_date.upper(),
            strike_count=strike_count,
            api_key=api_key,
            lb_bars=lb_bars,
            lb_tf=lb_tf,
        )

        return jsonify(response), status_code

    except Exception as exc:
        logger.exception(f"Error in buyer edge API: {exc}")
        return (
            jsonify({"status": "error", "message": "An error occurred processing your request"}),
            500,
        )


# ---------------------------------------------------------------------------
# /buyeredge/api/gex_levels
# ---------------------------------------------------------------------------
_VALID_GEX_MODES = {"selected", "cumulative"}
_VALID_INTERVALS = {"1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d"}


@buyer_edge_bp.route("/buyeredge/api/gex_levels", methods=["POST"])
@cross_origin()
@check_session_validity
def buyer_edge_gex_levels():
    """Advanced GEX Levels — single or cumulative multi-expiry mode."""
    try:
        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify({"status": "error", "message": "API key not configured"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip()[:20]
        exchange = data.get("exchange", "").strip()[:20]
        mode = str(data.get("mode", "selected")).strip().lower()

        if not underlying or not exchange:
            return jsonify({"status": "error", "message": "underlying and exchange are required"}), 400

        if not re.match(r"^[A-Z0-9]+$", underlying.upper()) or not re.match(
            r"^[A-Z0-9_]+$", exchange.upper()
        ):
            return jsonify({"status": "error", "message": "Invalid input format"}), 400

        if mode not in _VALID_GEX_MODES:
            return jsonify({"status": "error", "message": f"mode must be one of {sorted(_VALID_GEX_MODES)}"}), 400

        try:
            strike_count = max(2, min(int(data.get("strike_count", 20)), 45))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "strike_count must be an integer"}), 400

        if mode == "selected":
            expiry_date = data.get("expiry_date", "").strip()[:10]
            if not re.match(r"^\d{2}[A-Z]{3}\d{2}$", expiry_date.upper()):
                return jsonify({"status": "error", "message": "Invalid expiry_date format. Expected DDMMMYY"}), 400

            success, response, status_code = get_gex_levels(
                underlying=underlying.upper(),
                exchange=exchange.upper(),
                expiry_date=expiry_date.upper(),
                strike_count=strike_count,
                api_key=api_key,
            )
        else:
            # cumulative mode
            expiry_dates_raw = data.get("expiry_dates", [])
            if not isinstance(expiry_dates_raw, list):
                return jsonify({"status": "error", "message": "expiry_dates must be an array"}), 400

            expiry_dates = []
            for e in expiry_dates_raw[:4]:
                e_clean = str(e).strip().upper()[:10]
                if re.match(r"^\d{2}[A-Z]{3}\d{2}$", e_clean):
                    expiry_dates.append(e_clean)

            if not expiry_dates:
                return jsonify({"status": "error", "message": "At least one valid expiry_date required"}), 400

            success, response, status_code = get_gex_levels_cumulative(
                underlying=underlying.upper(),
                exchange=exchange.upper(),
                expiry_dates=expiry_dates,
                strike_count=strike_count,
                api_key=api_key,
            )

        return jsonify(response), status_code

    except Exception as exc:
        logger.exception(f"Error in buyer_edge_gex_levels: {exc}")
        return jsonify({"status": "error", "message": "An error occurred processing your request"}), 500


# ---------------------------------------------------------------------------
# /buyeredge/api/pcr_chart
# ---------------------------------------------------------------------------

@buyer_edge_bp.route("/buyeredge/api/pcr_chart", methods=["POST"])
@cross_origin()
@check_session_validity
def buyer_edge_pcr_chart():
    """PCR(OI) and PCR(Volume) time series chart data."""
    try:
        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify({"status": "error", "message": "API key not configured"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip()[:20]
        exchange = data.get("exchange", "").strip()[:20]
        expiry_date = data.get("expiry_date", "").strip()[:10]
        interval = str(data.get("interval", "1d")).strip()

        if not underlying or not exchange or not expiry_date:
            return jsonify({"status": "error", "message": "underlying, exchange, and expiry_date are required"}), 400

        if not re.match(r"^[A-Z0-9]+$", underlying.upper()) or not re.match(
            r"^[A-Z0-9_]+$", exchange.upper()
        ):
            return jsonify({"status": "error", "message": "Invalid input format"}), 400

        if not re.match(r"^\d{2}[A-Z]{3}\d{2}$", expiry_date.upper()):
            return jsonify({"status": "error", "message": "Invalid expiry_date format. Expected DDMMMYY"}), 400

        if interval not in _VALID_INTERVALS:
            return jsonify({"status": "error", "message": f"interval must be one of {sorted(_VALID_INTERVALS)}"}), 400

        try:
            days = max(1, min(int(data.get("days", 1)), 5))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "days must be an integer 1–5"}), 400

        try:
            pcr_strike_window = max(1, min(int(data.get("pcr_strike_window", 10)), 15))
            max_snapshot_strikes = max(5, min(int(data.get("max_snapshot_strikes", 40)), 100))
        except (TypeError, ValueError):
            pcr_strike_window = 10
            max_snapshot_strikes = 40

        success, response, status_code = get_pcr_chart_data(
            underlying=underlying.upper(),
            exchange=exchange.upper(),
            expiry_date=expiry_date.upper(),
            interval=interval,
            api_key=api_key,
            days=days,
            pcr_strike_window=pcr_strike_window,
            max_snapshot_strikes=max_snapshot_strikes,
        )
        return jsonify(response), status_code

    except Exception as exc:
        logger.exception(f"Error in buyer_edge_pcr_chart: {exc}")
        return jsonify({"status": "error", "message": "An error occurred processing your request"}), 500


# ---------------------------------------------------------------------------
# /buyeredge/api/unified_monitor (Request Batching)
# ---------------------------------------------------------------------------

@buyer_edge_bp.route("/buyeredge/api/unified_monitor", methods=["POST"])
@cross_origin()
@check_session_validity
def buyer_edge_unified_monitor():
    """Unified endpoint for parallel Straddle and PCR data loading."""
    try:
        from concurrent.futures import ThreadPoolExecutor

        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify({"status": "error", "message": "API key not configured"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip().upper()[:20]
        exchange = data.get("exchange", "").strip().upper()[:20]
        expiry_date = data.get("expiry_date", "").strip().upper()[:10]
        interval = str(data.get("interval", "1m")).strip()
        days = max(1, min(int(data.get("days", 1)), 5))
        pcr_strike_window = max(1, min(int(data.get("pcr_strike_window", 10)), 15))
        max_snapshot_strikes = max(5, min(int(data.get("max_snapshot_strikes", 40)), 100))

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_straddle = executor.submit(
                get_straddle_chart_data,
                underlying=underlying,
                exchange=exchange,
                expiry_date=expiry_date,
                interval=interval,
                api_key=api_key,
                days=days,
            )
            future_pcr = executor.submit(
                get_pcr_chart_data,
                underlying=underlying,
                exchange=exchange,
                expiry_date=expiry_date,
                interval=interval,
                api_key=api_key,
                days=days,
                pcr_strike_window=pcr_strike_window,
                max_snapshot_strikes=max_snapshot_strikes,
            )

            res_straddle = future_straddle.result()
            res_pcr = future_pcr.result()

        return jsonify({
            "status": "success",
            "straddle": res_straddle[1],
            "pcr": res_pcr[1],
        }), 200

    except Exception as exc:
        logger.exception(f"Error in buyer_edge_unified_monitor: {exc}")
        return jsonify({"status": "error", "message": "An error occurred processing your request"}), 500


# ---------------------------------------------------------------------------
# /buyeredge/api/iv_dashboard
# ---------------------------------------------------------------------------

@buyer_edge_bp.route("/buyeredge/api/iv_dashboard", methods=["POST"])
@cross_origin()
@check_session_validity
def buyer_edge_iv_dashboard():
    """IVRank, IVx, Vertical/Horizontal Skew dashboard for 1–4 expiries."""
    try:
        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify({"status": "error", "message": "API key not configured"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip()[:20]
        exchange = data.get("exchange", "").strip()[:20]

        if not underlying or not exchange:
            return jsonify({"status": "error", "message": "underlying and exchange are required"}), 400

        if not re.match(r"^[A-Z0-9]+$", underlying.upper()) or not re.match(
            r"^[A-Z0-9_]+$", exchange.upper()
        ):
            return jsonify({"status": "error", "message": "Invalid input format"}), 400

        expiry_dates_raw = data.get("expiry_dates", [])
        if not isinstance(expiry_dates_raw, list):
            return jsonify({"status": "error", "message": "expiry_dates must be an array"}), 400

        expiry_dates = []
        for e in expiry_dates_raw[:4]:
            e_clean = str(e).strip().upper()[:10]
            if re.match(r"^\d{2}[A-Z]{3}\d{2}$", e_clean):
                expiry_dates.append(e_clean)

        if not expiry_dates:
            return jsonify({"status": "error", "message": "At least one valid expiry_date required in expiry_dates"}), 400

        try:
            strike_count = max(2, min(int(data.get("strike_count", 15)), 30))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "strike_count must be an integer"}), 400

        success, response, status_code = get_iv_dashboard(
            underlying=underlying.upper(),
            exchange=exchange.upper(),
            expiry_dates=expiry_dates,
            strike_count=strike_count,
            api_key=api_key,
        )
        return jsonify(response), status_code

    except Exception as exc:
        logger.exception(f"Error in buyer_edge_iv_dashboard: {exc}")
        return jsonify({"status": "error", "message": "An error occurred processing your request"}), 500


# ---------------------------------------------------------------------------
# /buyeredge/api/spot_candles
# ---------------------------------------------------------------------------

_VALID_SPOT_INTERVALS = {"1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d"}


@buyer_edge_bp.route("/buyeredge/api/spot_candles", methods=["POST"])
@cross_origin()
@check_session_validity
def buyer_edge_spot_candles():
    """OHLCV candle history for the underlying — used by the GEX spot chart."""
    try:
        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify({"status": "error", "message": "API key not configured"}), 401

        data = request.get_json(silent=True) or {}
        underlying = data.get("underlying", "").strip()[:20]
        exchange = data.get("exchange", "").strip()[:20]
        interval = str(data.get("interval", "15m")).strip()

        if not underlying or not exchange:
            return jsonify({"status": "error", "message": "underlying and exchange are required"}), 400

        if not re.match(r"^[A-Z0-9]+$", underlying.upper()) or not re.match(
            r"^[A-Z0-9_]+$", exchange.upper()
        ):
            return jsonify({"status": "error", "message": "Invalid input format"}), 400

        if interval not in _VALID_SPOT_INTERVALS:
            return jsonify({"status": "error", "message": f"interval must be one of {sorted(_VALID_SPOT_INTERVALS)}"}), 400

        try:
            days = max(1, min(int(data.get("days", 5)), 30))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "days must be an integer 1–30"}), 400

        success, response, status_code = get_spot_candles(
            underlying=underlying.upper(),
            exchange=exchange.upper(),
            interval=interval,
            api_key=api_key,
            days=days,
        )
        return jsonify(response), status_code

    except Exception as exc:
        logger.exception(f"Error in buyer_edge_spot_candles: {exc}")
        return jsonify({"status": "error", "message": "An error occurred processing your request"}), 500
