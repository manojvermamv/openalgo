"""
Buyer Edge Blueprint

Serves the Options Buyer Edge state engine.

Endpoint:
    POST /buyeredge/api/data
        Computes all 5 modules (Market State, OI Intelligence, Greeks,
        Straddle, Signal) and returns a NO_TRADE / WATCH / EXECUTE signal.
"""

import re

from flask import Blueprint, jsonify, request, session
from flask_cors import cross_origin

from database.auth_db import get_api_key_for_tradingview
from services.buyer_edge_service import get_buyer_edge_data
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
        strike_count = int(data.get("strike_count", 10))

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
        strike_count = max(5, min(strike_count, 30))

        success, response, status_code = get_buyer_edge_data(
            underlying=underlying.upper(),
            exchange=exchange.upper(),
            expiry_date=expiry_date.upper(),
            strike_count=strike_count,
            api_key=api_key,
        )

        return jsonify(response), status_code

    except Exception as exc:
        logger.exception(f"Error in buyer edge API: {exc}")
        return (
            jsonify({"status": "error", "message": "An error occurred processing your request"}),
            500,
        )
