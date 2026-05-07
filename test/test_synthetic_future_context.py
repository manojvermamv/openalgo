"""
Unit tests for _compute_synthetic_future_context (buyer_edge_synthetic_service).

Basis interpretation model (tested here):
  - Positive basis = normal cost-of-carry, NOT bearish.
  - Negative basis = rare backwardation, NOT automatically bullish.
  - Directional pressure comes from spot-SF CO-MOVEMENT, NOT basis level or
    basis velocity sign.
  - Without pcr_series → pressure=neutral, confirmation=unavailable always.

Covers:
  - valid bid/ask formulas
  - LTP-only fallback
  - missing bid/ask (None values)
  - both LTP and bid/ask missing → invalid
  - wide spread
  - LTP outside bid/ask range
  - bid/ask primary when LTP missing
  - ATM strike not in chain → full null-filled shape
  - basis_state: normal (positive basis), backwardation (negative basis)
  - pressure neutral without pcr_series regardless of basis level
  - co-movement bullish pressure (both spot and SF rising)
  - co-movement bearish pressure (both spot and SF falling)
  - co-movement neutral (spot flat, SF-only movement → not actionable)
  - tiny SF tick does not confirm large spot move (neutral)
  - tiny SF tick does not trigger divergence trap (neutral)
  - divergence trap detection via pcr_series
  - confirming bullish via pcr_series (spot & SF both rising, no divergence)
  - confirming bearish via pcr_series (spot & SF both falling, no divergence)
  - basis_pct and spread_pct formulas
  - velocity fields populated from pcr_series (informational only)
  - no series → velocity fields are None

Usage:
    uv run pytest test/test_synthetic_future_context.py -v
"""

import os
import sys

# Pre-set env vars in case the broader test runner also imports buyer_edge_service
# (which chains through database/auth_db and requires API_KEY_PEPPER).
os.environ.setdefault("API_KEY_PEPPER", "a" * 64)
os.environ.setdefault("APP_KEY", "a" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test_openalgo.db")
os.environ.setdefault("BROKER_API_KEY", "test")
os.environ.setdefault("BROKER_API_SECRET", "test")
os.environ.setdefault("VALID_BROKERS", "zerodha")

# Ensure repo root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

# Import from the lightweight pure module — no DB, no Flask, no broker deps.
from services.buyer_edge_synthetic_service import _compute_synthetic_future_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ATM = 24000.0
SPOT = 23990.0


def _chain(ce_ltp=200.0, pe_ltp=180.0,
           ce_bid=199.0, ce_ask=201.0,
           pe_bid=179.0, pe_ask=181.0,
           atm=ATM):
    """Build a minimal single-strike chain list."""
    return [{
        "strike": atm,
        "ce": {"ltp": ce_ltp, "bid": ce_bid, "ask": ce_ask},
        "pe": {"ltp": pe_ltp, "bid": pe_bid, "ask": pe_ask},
    }]


# ---------------------------------------------------------------------------
# 1. Valid bid/ask formulas
# ---------------------------------------------------------------------------

def test_valid_bidask_formulas():
    chain = _chain()
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    # synthetic_ltp = ATM + ce_ltp - pe_ltp
    assert result["synthetic_ltp"] == pytest.approx(ATM + 200.0 - 180.0, abs=0.01)

    # synthetic_mid = ATM + (ce_bid+ce_ask)/2 - (pe_bid+pe_ask)/2
    ce_mid = (199.0 + 201.0) / 2  # 200.0
    pe_mid = (179.0 + 181.0) / 2  # 180.0
    assert result["synthetic_mid"] == pytest.approx(ATM + ce_mid - pe_mid, abs=0.01)

    # synthetic_bid = ATM + ce_bid - pe_ask
    assert result["synthetic_bid"] == pytest.approx(ATM + 199.0 - 181.0, abs=0.01)

    # synthetic_ask = ATM + ce_ask - pe_bid
    assert result["synthetic_ask"] == pytest.approx(ATM + 201.0 - 179.0, abs=0.01)

    # synthetic_spread = synthetic_ask - synthetic_bid
    expected_spread = (ATM + 201.0 - 179.0) - (ATM + 199.0 - 181.0)
    assert result["synthetic_spread"] == pytest.approx(expected_spread, abs=0.01)

    # basis_mid = synthetic_mid - spot
    expected_basis_mid = (ATM + ce_mid - pe_mid) - SPOT
    assert result["basis_mid"] == pytest.approx(expected_basis_mid, abs=0.01)

    # basis_ltp = synthetic_ltp - spot
    expected_basis_ltp = (ATM + 200.0 - 180.0) - SPOT
    assert result["basis_ltp"] == pytest.approx(expected_basis_ltp, abs=0.01)

    # Liquidity status should be 'good' (spread is tiny)
    assert result["liquidity_status"] == "good"

    # LTP inside market (200.0 is between 199 and 201, 180 is between 179 and 181)
    assert result["ltp_inside_market"] is True


# ---------------------------------------------------------------------------
# 2. LTP-only fallback (bid/ask zero)
# ---------------------------------------------------------------------------

def test_ltp_only_fallback():
    chain = _chain(ce_bid=0, ce_ask=0, pe_bid=0, pe_ask=0)
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    assert result["synthetic_ltp"] is not None
    assert result["synthetic_mid"] is None
    assert result["synthetic_bid"] is None
    assert result["synthetic_ask"] is None
    assert result["synthetic_spread"] is None
    assert result["liquidity_status"] == "ltp_only"
    assert result["ltp_inside_market"] is None


# ---------------------------------------------------------------------------
# 3. Missing bid/ask (None values)
# ---------------------------------------------------------------------------

def test_missing_bidask_none_values():
    chain = [{
        "strike": ATM,
        "ce": {"ltp": 200.0, "bid": None, "ask": None},
        "pe": {"ltp": 180.0, "bid": None, "ask": None},
    }]
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    assert result["synthetic_ltp"] is not None  # LTP available
    assert result["synthetic_mid"] is None
    assert result["liquidity_status"] == "ltp_only"


# ---------------------------------------------------------------------------
# 4. Both LTP and bid/ask missing → invalid
# ---------------------------------------------------------------------------

def test_completely_missing_data():
    chain = [{
        "strike": ATM,
        "ce": {"ltp": 0, "bid": 0, "ask": 0},
        "pe": {"ltp": 0, "bid": 0, "ask": 0},
    }]
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    assert result["synthetic_ltp"] is None
    assert result["liquidity_status"] == "invalid"
    assert result["confirmation"] == "unavailable"


# ---------------------------------------------------------------------------
# 5. Wide spread → liquidity_status == 'wide'
# ---------------------------------------------------------------------------

def test_wide_spread():
    # Force a spread > 0.5% of spot.  Spot=23990, 0.5% = ~120.
    # synthetic_spread = (ce_ask - pe_bid) - (ce_bid - pe_ask)
    #                  = (ce_ask - ce_bid) + (pe_ask - pe_bid)
    # Make ce spread = 100 and pe spread = 100 → synthetic spread = 200
    chain = _chain(ce_bid=100.0, ce_ask=200.0, pe_bid=100.0, pe_ask=200.0)
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    assert result["liquidity_status"] == "wide"
    assert result["spread_pct"] is not None
    assert result["spread_pct"] > 0.5


# ---------------------------------------------------------------------------
# 6. LTP outside bid/ask range
# ---------------------------------------------------------------------------

def test_ltp_outside_bidask():
    # CE ltp = 250 but bid/ask = 199-201 → ltp outside market
    chain = _chain(ce_ltp=250.0, ce_bid=199.0, ce_ask=201.0,
                   pe_ltp=180.0, pe_bid=179.0, pe_ask=181.0)
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    assert result["ltp_inside_market"] is False


# ---------------------------------------------------------------------------
# 7. ATM not in chain → full null-filled shape
# ---------------------------------------------------------------------------

def test_atm_not_in_chain():
    chain = [{"strike": 23500.0, "ce": {"ltp": 200.0, "bid": 199.0, "ask": 201.0},
              "pe": {"ltp": 180.0, "bid": 179.0, "ask": 181.0}}]
    result = _compute_synthetic_future_context(chain, SPOT, ATM)  # ATM=24000 missing

    assert result["liquidity_status"] == "invalid"
    assert result["confirmation"] == "unavailable"
    # All nullable fields must be present (full shape, not partial dict)
    NULLABLE_FIELDS = [
        "synthetic_ltp", "synthetic_mid", "synthetic_bid", "synthetic_ask",
        "synthetic_spread", "basis_ltp", "basis_mid", "basis_pct", "spread_pct",
        "synthetic_velocity", "basis_velocity", "basis_session_change", "ltp_inside_market",
    ]
    for field in NULLABLE_FIELDS:
        assert field in result, f"Missing field: {field}"
        assert result[field] is None, f"Expected None for {field}, got {result[field]}"
    assert "basis_state" in result


# ---------------------------------------------------------------------------
# 7b. bid/ask primary — valid bid/ask but LTP=0 → 'good', not 'invalid'
# ---------------------------------------------------------------------------

def test_bidask_primary_when_ltp_missing():
    """When bid/ask is valid but LTP is 0, liquidity_status should be 'good'."""
    chain = _chain(ce_ltp=0, pe_ltp=0,
                   ce_bid=199.0, ce_ask=201.0,
                   pe_bid=179.0, pe_ask=181.0)
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    assert result["synthetic_ltp"] is None  # LTP correctly absent
    assert result["synthetic_mid"] is not None  # bid/ask mid available
    assert result["synthetic_bid"] is not None
    assert result["synthetic_ask"] is not None
    assert result["liquidity_status"] == "good"  # bid/ask is primary, not LTP


# ---------------------------------------------------------------------------
# 8. basis_state: normal (positive basis) and backwardation (negative basis)
# ---------------------------------------------------------------------------

def test_basis_state_normal():
    """Positive basis (cost-of-carry) → basis_state='normal'."""
    # CE premium >> PE premium → synthetic > spot → positive basis
    chain = _chain(ce_ltp=300.0, pe_ltp=50.0,
                   ce_bid=299.0, ce_ask=301.0, pe_bid=49.0, pe_ask=51.0)
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    # basis_mid = (ATM + ce_mid - pe_mid) - SPOT = (24000+300-50) - 23990 = +260
    assert result["basis_mid"] is not None
    assert result["basis_mid"] > 0
    assert result["basis_state"] == "normal"


def test_basis_state_backwardation():
    """Negative basis beyond threshold → basis_state='backwardation'."""
    # CE premium << PE premium → synthetic < spot → negative basis
    chain = _chain(ce_ltp=50.0, pe_ltp=200.0,
                   ce_bid=49.0, ce_ask=51.0, pe_bid=199.0, pe_ask=201.0)
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    # basis_mid = (ATM + 50 - 200) - SPOT = 23850 - 23990 = -140
    assert result["basis_mid"] is not None
    assert result["basis_mid"] < 0
    assert result["basis_state"] == "backwardation"


# ---------------------------------------------------------------------------
# 9. Pressure is NEUTRAL without pcr_series regardless of basis level
# ---------------------------------------------------------------------------

def test_pressure_neutral_when_no_series_any_basis():
    """Raw basis level must never determine directional pressure."""
    # Large negative basis (old logic: bullish) — now neutral without series
    chain_neg = _chain(ce_ltp=50.0, pe_ltp=200.0,
                       ce_bid=49.0, ce_ask=51.0, pe_bid=199.0, pe_ask=201.0)
    r1 = _compute_synthetic_future_context(chain_neg, SPOT, ATM, pcr_series=None)
    assert r1["pressure"] == "neutral"
    assert r1["confirmation"] == "unavailable"

    # Large positive basis (old logic: bearish) — now neutral without series
    chain_pos = _chain(ce_ltp=300.0, pe_ltp=50.0,
                       ce_bid=299.0, ce_ask=301.0, pe_bid=49.0, pe_ask=51.0)
    r2 = _compute_synthetic_future_context(chain_pos, SPOT, ATM, pcr_series=None)
    assert r2["pressure"] == "neutral"
    assert r2["confirmation"] == "unavailable"


# ---------------------------------------------------------------------------
# 10. Co-movement bullish pressure (both spot and SF rising)
# ---------------------------------------------------------------------------

def test_pressure_bullish_co_movement():
    """Both spot and SF rising intraday → bullish pressure + confirming."""
    chain = _chain()
    # Both spot and SF rising: co-movement model signals bullish.
    # Note: basis_velocity is informational only — not used for direction.
    pcr_series = [
        {"spot": 23900.0, "synthetic_future": 23950.0},
        {"spot": 23950.0, "synthetic_future": 24000.0},  # spot +50, SF +50
        {"spot": 24050.0, "synthetic_future": 24100.0},  # spot +100, SF +100
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["pressure"] == "bullish"
    assert result["confirmation"] == "confirming"
    # Basis velocity is still populated for display even though it does not
    # drive the directional signal.
    assert result["basis_velocity"] is not None


# ---------------------------------------------------------------------------
# 11. Co-movement bearish pressure (both spot and SF falling)
# ---------------------------------------------------------------------------

def test_pressure_bearish_co_movement():
    """Both spot and SF falling intraday → bearish pressure + confirming."""
    chain = _chain()
    # Both spot and SF falling: co-movement model signals bearish.
    pcr_series = [
        {"spot": 24100.0, "synthetic_future": 24200.0},
        {"spot": 24050.0, "synthetic_future": 24130.0},  # spot -50, SF -70
        {"spot": 23980.0, "synthetic_future": 24060.0},  # spot -70, SF -70
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["pressure"] == "bearish"
    assert result["confirmation"] == "confirming"
    assert result["basis_velocity"] is not None


# ---------------------------------------------------------------------------
# 12. Co-movement neutral: spot flat (or SF-only movement) → neutral
# ---------------------------------------------------------------------------

def test_pressure_neutral_spot_flat():
    """Spot flat with any SF movement → neutral (not actionable co-movement)."""
    chain = _chain()
    # Case A: both spot and SF flat
    pcr_flat = [
        {"spot": 24000.0, "synthetic_future": 24010.0},
        {"spot": 24000.0, "synthetic_future": 24010.0},
        {"spot": 24000.0, "synthetic_future": 24010.0},
    ]
    r1 = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_flat)
    assert r1["pressure"] == "neutral"
    assert r1["confirmation"] == "unavailable"

    # Case B: spot flat, SF falling — was previously "bullish via velocity",
    # now correctly neutral (SF lagging alone is not a directional signal)
    pcr_sf_only = [
        {"spot": 24000.0, "synthetic_future": 23900.0},
        {"spot": 24000.0, "synthetic_future": 23870.0},  # spot flat, SF falling
        {"spot": 24000.0, "synthetic_future": 23830.0},
    ]
    r2 = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_sf_only)
    assert r2["pressure"] == "neutral"
    assert r2["confirmation"] == "unavailable"
    # Basis velocity is still populated for informational display
    assert r2["basis_velocity"] is not None

    # Case C: spot flat, SF rising — was previously "bearish via velocity",
    # now correctly neutral (SF richening alone is not a directional signal)
    pcr_sf_rich = [
        {"spot": 24000.0, "synthetic_future": 24150.0},
        {"spot": 24000.0, "synthetic_future": 24180.0},  # spot flat, SF rising
        {"spot": 24000.0, "synthetic_future": 24230.0},
    ]
    r3 = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_sf_rich)
    assert r3["pressure"] == "neutral"
    assert r3["confirmation"] == "unavailable"


# ---------------------------------------------------------------------------
# 12b. SF-side threshold: tiny SF tick must not confirm large spot move
# ---------------------------------------------------------------------------

def test_tiny_sf_tick_does_not_confirm_bullish():
    """Large spot move with SF barely ticking → neutral, not bullish."""
    chain = _chain()
    # Spot moves significantly (+100) but SF only ticks +1 (below 0.05% threshold).
    # move_threshold ≈ 24000 * 0.0005 = 12.  SF delta = 1 < 12 → neutral.
    pcr_series = [
        {"spot": 23900.0, "synthetic_future": 23950.0},
        {"spot": 23950.0, "synthetic_future": 23951.0},   # spot +50, SF +1 (noise)
        {"spot": 24050.0, "synthetic_future": 23952.0},   # spot +100, SF +1 (noise)
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["pressure"] == "neutral"
    assert result["confirmation"] == "unavailable"


def test_tiny_sf_tick_does_not_trigger_divergence():
    """Large spot move with SF ticking by only a trivial amount in opposite direction
    → neutral, not diverging.  Divergence requires SF to also move meaningfully.
    """
    chain = _chain()
    # Spot rises +100 but SF only drops -1 (noise below 0.05% threshold).
    # Divergence must NOT be declared when SF barely moves.
    pcr_series = [
        {"spot": 23900.0, "synthetic_future": 23950.0},
        {"spot": 23950.0, "synthetic_future": 23949.0},   # spot +50, SF -1 (noise)
        {"spot": 24050.0, "synthetic_future": 23948.0},   # spot +100, SF -1 (noise)
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["confirmation"] == "unavailable"
    assert result["pressure"] == "neutral"


# ---------------------------------------------------------------------------
# 13. Confirmation: divergence trap via pcr_series
# ---------------------------------------------------------------------------

def test_confirmation_diverging_trap():
    """Spot rising but synthetic declining → divergence trap."""
    chain = _chain()
    pcr_series = [
        {"spot": 23900.0, "synthetic_future": 23950.0},  # open
        {"spot": 23950.0, "synthetic_future": 23940.0},  # prev bar: SF dips
        {"spot": 24050.0, "synthetic_future": 23920.0},  # latest: spot up, SF down
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["confirmation"] == "diverging"


# ---------------------------------------------------------------------------
# 14. Confirmation: confirming bullish via pcr_series
# ---------------------------------------------------------------------------

def test_confirmation_confirming_bullish():
    """Spot and SF both rising (no divergence) → confirming bullish."""
    chain = _chain(ce_ltp=50.0, pe_ltp=200.0,
                   ce_bid=49.0, ce_ask=51.0, pe_bid=199.0, pe_ask=201.0)
    # Both spot and SF rise in the last two bars — co-movement bullish signal.
    # Note: basis widens negatively here (not "compressing"); direction comes
    # from the co-movement, not from basis behaviour.
    pcr_series = [
        {"spot": 23900.0, "synthetic_future": 23800.0},  # basis = -100
        {"spot": 23950.0, "synthetic_future": 23820.0},  # spot +50, SF +20
        {"spot": 24020.0, "synthetic_future": 23845.0},  # spot +70, SF +25
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["pressure"] == "bullish"
    assert result["confirmation"] == "confirming"


# ---------------------------------------------------------------------------
# 15. Confirmation: confirming bearish via pcr_series
# ---------------------------------------------------------------------------

def test_confirmation_confirming_bearish():
    """Spot and SF both falling (no divergence) → confirming bearish."""
    chain = _chain(ce_ltp=300.0, pe_ltp=50.0,
                   ce_bid=299.0, ce_ask=301.0, pe_bid=49.0, pe_ask=51.0)
    # Both spot and SF fall in the last two bars — co-movement bearish signal.
    # Note: basis widens positively here; direction comes from the co-movement.
    pcr_series = [
        {"spot": 24100.0, "synthetic_future": 24250.0},  # basis = +150
        {"spot": 24050.0, "synthetic_future": 24220.0},  # spot -50, SF -30
        {"spot": 23980.0, "synthetic_future": 24200.0},  # spot -70, SF -20
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["pressure"] == "bearish"
    assert result["confirmation"] == "confirming"


# ---------------------------------------------------------------------------
# 16. basis_pct and spread_pct are computed correctly
# ---------------------------------------------------------------------------

def test_basis_pct_and_spread_pct():
    chain = _chain()
    result = _compute_synthetic_future_context(chain, SPOT, ATM)

    if result["basis_mid"] is not None:
        expected_pct = round((result["basis_mid"] / SPOT) * 100, 3)
        assert result["basis_pct"] == pytest.approx(expected_pct, abs=0.001)

    if result["spread_pct"] is not None and result["synthetic_spread"] is not None:
        expected_sp = round((result["synthetic_spread"] / SPOT) * 100, 3)
        assert result["spread_pct"] == pytest.approx(expected_sp, abs=0.001)


# ---------------------------------------------------------------------------
# 17. Velocity fields populated from pcr_series
# ---------------------------------------------------------------------------

def test_velocity_fields_with_series():
    chain = _chain()
    pcr_series = [
        {"spot": 23900.0, "synthetic_future": 23850.0},
        {"spot": 23950.0, "synthetic_future": 23870.0},  # sf up 20
        {"spot": 24000.0, "synthetic_future": 23900.0},  # sf up 30
    ]
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series)

    assert result["synthetic_velocity"] == pytest.approx(23900.0 - 23870.0, abs=0.01)
    assert result["basis_velocity"] is not None
    assert result["basis_session_change"] is not None


# ---------------------------------------------------------------------------
# 18. No series → velocity fields are None
# ---------------------------------------------------------------------------

def test_velocity_fields_no_series():
    chain = _chain()
    result = _compute_synthetic_future_context(chain, SPOT, ATM, pcr_series=None)

    assert result["synthetic_velocity"] is None
    assert result["basis_velocity"] is None
    assert result["basis_session_change"] is None
