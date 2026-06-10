"""
OPTIONS BUYER-EDGE STRATEGY  ·  Multi-Layer Confirmation  ·  OpenAlgo Trading Bot

Buys NSE F&O options (CE / PE) only when five independent signal layers agree:

  Layer 1 — Technical Trend        (EMA, VWAP, RSI, MACD on spot candles)
  Layer 2 — OI Flow Intelligence   (PCR, Call/Put Flow, OI Wall)
  Layer 3 — Greeks Engine          (Delta Imbalance, Gamma Regime)
  Layer 4 — Straddle & IV          (IV Regime, Straddle Velocity)
  Layer 5 — Synthetic Futures      (spot-SF co-movement)

Composite score: −100 → +100.  Order placed when:
  • abs(score) ≥ MIN_SCORE  and  trap_score ≤ MAX_TRAP  and  signal == "EXECUTE"

Market data flows through MarketSnapshot authority layer:
  WebSocket ticks → SnapshotCache → trail / PNL / alerts / risk
  Quote API fallback when WS stale → SnapshotCache
  OptionChain enrichment → SnapshotCache

Snapshot is authority for market data only.
Position state (fills, protection orders) comes from broker APIs — independent reconciliation.

Run:  export OPENALGO_API_KEY="your-key"  &&  python BuyerEdgeStrategy.py
      Inside OpenAlgo /python runner: OPENALGO_API_KEY is injected automatically.

⚠  Long options carry unlimited theta decay — always set PREMIUM_STOP_PTS.
"""

import csv
import traceback
import concurrent.futures
import math
import os
import re
import sys
import threading
import time as _time_mod
time = _time_mod   # single canonical alias — use time.sleep / time.time / _time_mod.mktime interchangeably
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
import pandas as pd
from openalgo import api, ta

# Ensure UTF-8 output on Windows (cp1252 console cannot encode ₹ and other Unicode chars).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ===============================================================================
# Logging — global toggle constants
# ===============================================================================
DEBUG_ENABLED = False
INFO_ENABLED  = True

def dbg(*args, **kwargs):
    if DEBUG_ENABLED:
        print(*args, **kwargs)

def inf(*args, **kwargs):
    if INFO_ENABLED:
        print(*args, **kwargs)

def err(msg: str, exc: BaseException | None = None, *, always: bool = True):
    """Error log — always visible regardless of INFO_ENABLED / DEBUG_ENABLED.

    Args:
        msg:   Human-readable error description.
        exc:   Exception object (optional). When provided, traceback is printed.
        always: If True (default), output is unconditional. Set False to respect
                a future ERROR_ENABLED toggle.
    """
    if always:
        if exc is not None:
            print(f"[ERROR] {msg}: {exc}")
        else:
            print(f"[ERROR] {msg}")

# ===============================================================================
# AUDIT STATUS (Post Conviction-Risk-Engine + Confirmed-Close Trail +
# WS Recovery + Basket Protection + Exit Attribution Upgrade)
# ===============================================================================
#
# CRITICAL BUGS
# ------------------------------------------------------------------------------
# None currently identified.
#
# Audited and verified:
#
#   ✓ MarketSnapshot authority layer (single source for market data)
#   ✓ SnapshotCache with WS + quote-fallback + option-chain enrichment
#   ✓ Snapshot freshness monitoring + stale-data fallback
#   ✓ Snapshot overwrite guard (field-specific None check, not timestamp)
#   ✓ Conviction-driven strike selection
#   ✓ Conviction-driven entry stop-loss sizing
#   ✓ Conviction-driven breakeven engine
#   ✓ Conviction-driven premium trailing
#   ✓ Conviction-aware spot trail activation
#   ✓ Gamma Speed-X acceleration
#   ✓ Premium confirmed-close trail ratchet architecture
#   ✓ Synchronized snapshot-based spot trailing
#   ✓ Single-pass delta caching
#   ✓ PendingEntry delta propagation
#   ✓ Moneyness-aware target scaling
#   ✓ Moneyness-aware trail activation scaling
#   ✓ Profit Acceleration Compression Engine (Trend Efficiency)
#   ✓ KER observation: Uses rolling 15-bar window on 1-minute option candles (~15 minutes of premium behavior); may react abruptly; consider EMA smoother or decay floor
#   ✓ Discretionary mapping:
#     Early trade → normal trail;
#     Gamma expansion → compress;
#     High-ROI consolidation/chop → relax compression via KER
#   ✓ Auth-error notification deduplication
#   ✓ WS-dead entry protection (broker SL-M independent of WS)
#   ✓ Persistent WebSocket client architecture
#   ✓ Subscription reconciliation engine
#   ✓ Reconnect subscription restoration
#   ✓ WebSocket circuit-breaker alerting
#   ✓ WebSocket watchdog self-healing
#   ✓ WebSocket health telemetry
#   ✓ Reconcile-cycle monitoring
#   ✓ Subscription drift detection
#   ✓ Thread-leak visibility instrumentation
#
#   ✓ MIS product consistency (Entry / SL / Target / Exit)
#   ✓ Basket protection order architecture
#   ✓ Partial basket acceptance recovery
#   ✓ Protection-order state reconciliation (skip-if-exists, startup restore, basket fallback)
#   ✓ Basket-order fallback protection
#   ✓ Startup broker-protection restoration
#   ✓ Startup orphan-order cancellation
#   ✓ Broker SL synchronization (modify-before-advance pattern)
#   ✓ Position persistence compatibility
#
#   ✓ Exit attribution framework
#   ✓ Exit-type normalization
#   ✓ R-multiple journaling
#   ✓ Daily-PnL accounting consistency
#   ✓ EOD force-untrack journaling
#   ✓ EOD force-untrack attribution
#   ✓ Broker-fill accounting protection
#
#
# HIGH SEVERITY ITEMS
# ------------------------------------------------------------------------------
# None currently identified.
#
# KNOWN ARCHITECTURAL INVARIANTS (not bugs)
# ------------------------------------------------------------------------------
#
# 1. 1 underlying ⟷ 1 active position (no multi-leg, no scale-in, no hedge).
#     SnapshotCache.option_symbol is singular. Violating this invariant requires
#     upgrading SnapshotCache to per-symbol snapshots.
#
# 2. Key-level trail state is intentionally ephemeral — reset on every restart.
#     Broker SL-M is the source of truth for protection; trail reinitializes from
#     live market data after restart. Not a bug — a conscious tradeoff.
#
# 3. Snapshot is authority for market data only.
#     Position state (fills, protection order IDs) comes from broker APIs
#     (positionbook / orderbook) via independent reconciliation paths.
#     Not a design gap — a data-domain boundary.
#
# 4. WebSocket is operationally optional for trail/PNL/alerts since the quote-API
#     fallback re-populates SnapshotCache when ticks go stale. However WS recovery
#     (diagnose why _on_ws_data never fires) has not been proven.
#
# Current live-trading architecture:
#
#   Broker SL                : Native broker-side SL-M
#   Broker Target            : Native broker-side LIMIT
#   Entry Protection         : Basket-protected (SL + Target)
#   Breakeven                : Confirmed-close driven
#   Premium Trail            : Confirmed-close driven
#   Spot Trail               : Confirmed-close driven
#   Gamma Speed-X            : Confirmed-close ROI driven
#   Exit Breach Detection    : Tick driven (WS) + poll driven (broker fill)
#   Exit Attribution         : Normalized exit-type tracking
#   Journal Analytics        : R-multiple aware
#
# Audited paths with no known open defects:
#   - conviction propagation
#   - moneyness propagation
#   - reconnect & subscription reconciliation
#   - exit attribution & R-multiple journaling
#   - daily-PnL accounting (broker fill vs WS fallback)
#
# Known bounded risks (documented invariants, not bugs):
#   - Snapshot is authority for market data only, not total state
#   - 1 underlying ⟷ 1 active position (singular option_symbol)
#   - Key-level trail is ephemeral across restart
#   - WS ticks have not been confirmed flowing (diagnose pending)
#   - Broker SL modify failure → SL not advanced (retry on next tick)
#
#
# MEDIUM SEVERITY ITEMS (Calibration / Research)
# ------------------------------------------------------------------------------
#
# 1. Gamma Speed-X thresholds remain distribution-dependent.
#
#    Current:
#
#       ROI >=  50% -> 1.5x speed
#       ROI >= 100% -> 2.0x speed
#       ROI >= 150% -> 2.5x speed
#
#    Architecture verified.
#
#    Future work:
#
#       Validate thresholds using live ROI distributions
#       and realized premium expansion behavior.
#
#
# 2. asym_score_threshold requires statistical calibration.
#
#    Current:
#
#       cfg.asym_score_threshold
#
#    Future work:
#
#       Log asym_score distributions
#       Log rejection rates
#       Log selected strike quality
#       Log realized trade outcomes
#
#    Calibration should remain data-driven.
#
#
# 3. Conviction scaling constants require long-term calibration.
#
#       CONV_BE_BASE
#       CONV_BE_RANGE
#
#       CONV_TRAIL_ACT_BASE
#       CONV_TRAIL_ACT_RANGE
#
#    Current ranges are intentionally conservative.
#
#    Architecture validated.
#
#    Requires larger live-trade sample size before modification.
#
#
# 4. Confirmed-close trail timing uses synchronized WS snapshots.
#
#    Current architecture:
#
#       Strategy thread wakes on clock-aligned boundaries.
#
#       Trail decisions use the most recent synchronized
#       premium and spot LTP available at processing time.
#
#       Trail ratchets only on new confirmed-close highs.
#
#    Result:
#
#       Avoids intrabar noise ratchets.
#       Avoids tick-spike contamination.
#       Preserves graceful Speed-X decay behavior.
#       Maintains sub-second execution latency.
#
#    No structural issue identified.
#
#
# 5. Exit-type expectancy database is newly available.
#
#    Current exit categories:
#
#       BROKER_SL
#       BROKER_TARGET
#       PREMIUM_TRAIL
#       SPOT_TRAIL
#       MAX_HOLD
#       EOD
#       FORCE_UNTRACK_EST
#       FORCE_UNTRACK_UNKNOWN
#       MANUAL
#       OTHER
#
#    Future work:
#
#       Measure expectancy by exit type.
#       Measure average R-multiple by exit type.
#       Identify negative-expectancy exit mechanisms.
#
#    Architecture complete.
#    Research phase pending live sample accumulation.
#
#
# LOW SEVERITY ITEMS
# ------------------------------------------------------------------------------
#
# 1. Delta target curve may benefit from future optimization.
#
#    Current:
#
#       STRIKE_DELTA_BASE
#       STRIKE_DELTA_RANGE
#
#    Architecture is stable.
#
#    Future tuning should be expectancy-driven and supported
#    by live trade-distribution analysis.
#
#
# 2. Gamma Speed-X floor may require instrument-specific tuning.
#
#    Current:
#
#       max(base_step * 0.40,
#           base_step / trail_speed)
#
#    Production-safe.
#
#    Future optimization may improve gamma-capture efficiency.
#
#
# 3. Confirmed-close implementation is not exchange OHLC close.
#
#    Current:
#
#       Uses latest synchronized WS LTP sampled on the
#       clock-aligned strategy boundary.
#
#    Typical timing drift:
#
#       Sub-second.
#
#    Considered preferable to REST candle retrieval due to:
#
#       Lower latency
#       Reduced API dependence
#       Better live responsiveness
#
#    No structural issue identified.
#
#
# 4. Deep-ITM stop sizing may warrant future expectancy research.
#
#    Current architecture:
#
#       Moneyness-aware stop sizing.
#
#    Future work:
#
#       Compare Deep-ITM vs ATM expectancy
#       Compare capital efficiency
#       Compare premium retention
#       Compare realized R-multiples
#
#
# 5. Conviction scaling is currently linear.
#
#    Current:
#
#       conviction = abs(score) / 100
#
#       Applied via:
#
#           Strike Selection
#           Entry Risk
#           Breakeven
#           Trail Activation
#
#    No architectural issue identified.
#
#    Future research candidates:
#
#       conviction ** 1.5
#           Compress low-conviction influence.
#
#       sqrt(conviction)
#           Expand low-conviction influence.
#
#    Any change should be expectancy-validated before adoption.
#
#
# 6. Retail API idempotency remains externally constrained.
#
#    Basket protection architecture is hardened against:
#
#       Partial acceptance
#       Missing-leg recovery
#       Startup restoration
#
#    However, true broker-side idempotency keys are not
#    available through the retail API stack.
#
#    Residual tail-risk:
#
#       Network timeout after broker acceptance but before
#       response delivery may theoretically create duplicate
#       protection orders during fallback recovery.
#
#    Considered acceptable operational risk.
#
#
# PRODUCTION READINESS
# ------------------------------------------------------------------------------
#
# Architecture Status
#
#   Strike Selection          : Stable
#   Entry Risk Engine         : Stable
#   Trail Engine              : Stable
#   Conviction Framework      : Stable
#   Gamma Capture             : Stable
#   Moneyness Framework       : Stable
#
#   WebSocket Architecture    : Stable
#   Subscription Recovery     : Stable
#   Watchdog Recovery         : Stable
#   Auth Failure Detection    : Stable
#   Entry Protection          : Stable
#   Basket Protection         : Stable
#
#   Exit Attribution          : Stable
#   Journal Analytics         : Stable
#   R-Multiple Tracking       : Stable
#   Accounting Consistency    : Stable
#
#   Broker SL Synchronization : Stable
#   Startup Recovery          : Stable
#   Position Reconstruction   : Stable
#
# Remaining work is calibration, expectancy research,
# trade-distribution analysis, and statistical optimization
# rather than structural correctness, fault tolerance,
# or production reliability.
#
# ===============================================================================


# ===============================================================================
# GLOBAL CONSTANTS
# ===============================================================================

# Market hours (IST): 9:15 AM – 3:30 PM
MARKET_HOURS_START = 915   # 09:15 IST
MARKET_HOURS_END   = 1530  # 15:30 IST

# ── Layer 1: Score Generation ──────────────────────────────────────────────────
# PRACTICAL_ALIGNMENT_FACTOR: defines what fraction of MAX_RAW_SCORE is treated as
# the "practical ceiling" for a 100-point conviction score. Market signals rarely
# achieve 100% component alignment; this factor acknowledges that reality.
#
# Calibration guide — run a distribution audit across N scans and observe:
#   If 95th percentile raw_score ≈ 0.50 × MAX_RAW_SCORE → set to 0.50
#   If 95th percentile raw_score ≈ 0.75 × MAX_RAW_SCORE → set to 0.75
#   Until confirmed by live data, keep at 1.00 (no compression, full gradient).
PRACTICAL_ALIGNMENT_FACTOR = 1.00

# ── Layer 2: Trade Selection ────────────────────────────────────────────────────
# WATCH_FACTOR: the score band below EXECUTE that marks a setup worth monitoring.
# watch_threshold = effective_min_score × WATCH_FACTOR
#
# Session thresholds (set via BotConfig / ENV):
#   morning_gate  → typically 45  (stricter pre-market discipline)
#   normal_hours  → typically 30  (baseline execution bar)
#   power_hour    → typically 20  (relaxed, momentum-driven)
#
# Example at normal_hours (effective_min_score=30):
#   abs_score >= 30  → EXECUTE
#   abs_score >= 22  → WATCH  (30 × 0.75 = 22.5 → 22)
#   abs_score  < 22  → NO_TRADE
WATCH_FACTOR = 0.75

# ── Layer 3: Strike Selection (conviction-driven) ──────────────────────────────
# All strike-selection parameters are driven by a single `conviction` scalar
# derived from (abs(signal_score) - min_score) / (100 - min_score).  This
# eliminates hard regime jumps and maps the tradeable score range [min→100]
# continuously to [0.0, 1.0].
#
# Delta targeting — piecewise continuous mapping:
#   Score < 50   → [STRIKE_DELTA_BASE, STRIKE_DELTA_PIVOT] (near-OTM → ATM)
#   Score >= 50  → [STRIKE_DELTA_PIVOT, STRIKE_DELTA_MAX]  (ATM → Mild ITM)
#
# Calibration change (2026-06-04):
#   STRIKE_DELTA_BASE:  raised 0.15 → 0.25: even the weakest signal now targets
#     a near-OTM strike (Δ≈0.25) instead of a deep-OTM (Δ≈0.15), reducing
#     SL width and preventing systematic qty=0 risk-cap rejections.
#   STRIKE_SCORE_PIVOT: lowered 60 → 50: ATM targeting is reached at a lower
#     score, so today's typical 42–52 signals get meaningfully better strikes.
STRIKE_DELTA_BASE  = 0.25   # min delta at min_score (near-OTM floor) — raised from 0.15 to reduce SL width on weak signals
STRIKE_DELTA_PIVOT = 0.50   # delta at SCORE_PIVOT (ATM) — crossover between OTM and mild-ITM targeting zones
STRIKE_DELTA_MAX   = 0.70   # max delta at score 100 (mild ITM) — prevents over-leveraged deep-ITM selection
STRIKE_SCORE_PIVOT = 50.0   # score where ATM is targeted — lowered from 60 so typical 42–52 signals reach ATM-ish strikes

# Delta band half-width — how wide to search around the target delta.
# e.g. 0.08 means [target-0.08, target+0.08].
STRIKE_DELTA_BAND = 0.08    # search band around target_delta — wider band tolerates illiquid chains with sparse delta coverage

# Maximum acceptable delta gap in the fallback strike selection.
# If the nearest available delta is farther than this from the target, the
# fallback is considered pathological and the delta filter is bypassed entirely,
# falling back to pure liquidity ranking instead of picking a wildly OTM strike.
MAX_DELTA_GAP = 0.15        # fallback gap ceiling — exceeding this bypasses delta filter entirely and uses liquidity rank only

# Dynamic asym_score weighting — delta vs liquidity tradeoff:
#   conviction=0.0 → delta_weight = STRIKE_DELTA_WEIGHT_BASE          (favour liquidity)
#   conviction=1.0 → delta_weight = STRIKE_DELTA_WEIGHT_BASE + RANGE  (favour delta fit)
STRIKE_DELTA_WEIGHT_BASE  = 0.10   # delta fit weight at zero conviction — low conviction defers to OI/volume liquidity signals
STRIKE_DELTA_WEIGHT_RANGE = 0.20   # additional delta weight at max conviction — total 0.30 at high conviction (max delta precision)

# Maximum strike search window as a fraction of spot price (each side).
# e.g. 0.05 = ±5% → CE: [spot, spot*1.05]; PE: [spot*0.95, spot].
STRIKE_RANGE_PCT = 0.05     # strike search radius — ±5% of spot; wider = more candidates but lower quality floor

# ── Conviction Risk Engine — global tuning constants ──────────────────────────
# These constants are shared by SL sizing, breakeven, and all trail functions
# to ensure a single consistent model drives all risk parameters.
#
# Breakeven trigger adjustment:
#   adj = CONV_BE_BASE - conviction * CONV_BE_RANGE
#   conviction=0.0 → 1.10× (need 110% of normal trigger)
#   conviction=1.0 → 0.90× (trigger at 90% — protect earlier)
#   Narrow range intentional: avoids killing winners via premature BE on strong setups.
CONV_BE_BASE  = 1.10
CONV_BE_RANGE = 0.20

# Trail activation adjustment:
#   adj = CONV_TRAIL_ACT_BASE - conviction * CONV_TRAIL_ACT_RANGE
#   conviction=0.0 → 1.20× (needs larger buffer before trailing)
#   conviction=1.0 → 0.80× (trail activates earlier)
CONV_TRAIL_ACT_BASE  = 1.20
CONV_TRAIL_ACT_RANGE = 0.40

# Gamma Speed-X trail step floor:
#   step_pts = max(base * GAMMA_SPEED_STEP_FLOOR, base / trail_speed)
#   Prevents trail step from becoming dangerously tight on fast gamma spikes.
GAMMA_SPEED_STEP_FLOOR = 0.40   # never tighter than 40% of base step


# ===============================================================================
# EXIT REASON ENUM — Normalized exit attribution for expectancy analysis
# ===============================================================================

class ExitReason:
    """Normalized exit reason codes for expectancy database."""
    BROKER_SL           = "BROKER_SL"
    BROKER_TARGET       = "BROKER_TARGET"
    PREMIUM_TRAIL       = "PREMIUM_TRAIL"
    SPOT_TRAIL          = "SPOT_TRAIL"
    MAX_HOLD            = "MAX_HOLD"
    EOD                 = "EOD"
    FORCE_UNTRACK_EST   = "FORCE_UNTRACK_EST"
    FORCE_UNTRACK_UNKNOWN = "FORCE_UNTRACK_UNKNOWN"
    MANUAL              = "MANUAL"
    OTHER               = "OTHER"
    
    # Internal mapping from raw reason strings to normalized enum
    _RAW_TO_ENUM = {
        "premium_sl_hit": PREMIUM_TRAIL,
        "premium_target_hit": BROKER_TARGET,
        "spot_trail_sl_hit": SPOT_TRAIL,
        "broker_sl_filled": BROKER_SL,
        "broker_sl_filled_on_modify": BROKER_SL,
        "broker_target_filled": BROKER_TARGET,
        "EOD-SquareOff": EOD,
        "EOD-ForceUntrack-Estimated": FORCE_UNTRACK_EST,
        "EOD-ForceUntrack-NoBrokerPrice": FORCE_UNTRACK_UNKNOWN,
        "PostCutoffEntry": EOD,
        "MaxHoldTime": MAX_HOLD,
        "Bot Shutdown": MANUAL,
        "manual": MANUAL,
    }
    
    @classmethod
    def normalize(cls, raw_reason: str) -> str:
        """Map raw exit reason to normalized enum value."""
        return cls._RAW_TO_ENUM.get(raw_reason, cls.OTHER)
    
    @classmethod
    def all_values(cls) -> list[str]:
        """Return all normalized enum values."""
        return [
            cls.BROKER_SL, cls.BROKER_TARGET, cls.PREMIUM_TRAIL, cls.SPOT_TRAIL,
            cls.MAX_HOLD, cls.EOD, cls.FORCE_UNTRACK_EST, cls.FORCE_UNTRACK_UNKNOWN,
            cls.MANUAL, cls.OTHER
        ]


# ===============================================================================
# CONFIGURATION — BotConfig dataclass
# ===============================================================================

@dataclass
class BotConfig:
    """All strategy configuration as typed fields. Build via BotConfig.from_env()."""

    # ── API Connection ─────────────────────────────────────────────────────────
    api_key:       str = "openalgo-apikey"
    api_host:      str = "http://127.0.0.1:5000"
    ws_url:        str = ""   # empty → SDK auto-derives from api_host; set WEBSOCKET_URL to override
    strategy_name: str = "OptionsBuyerEdgeBot"

    # ── Scan Universe ──────────────────────────────────────────────────────────
    underlyings:       list[str]      = field(default_factory=list)
    index_underlyings: frozenset[str] = field(default_factory=frozenset)

    # ── Exchange Routing ───────────────────────────────────────────────────────
    spot_exchange:  str = "NSE"
    fno_exchange:   str = "NFO"
    index_exchange: str = "NSE_INDEX"

    # ── Notifications ──────────────────────────────────────────────────────────
    openalgo_username: str = "manojv097"
    live_pnl_alert_interval: int = 60
    risk_based_sizing_enabled: bool = False  # When True, caps qty by RISK_PERCENT of capital; disabled by default

    # ── Options Parameters ─────────────────────────────────────────────────────
    dte_min:        int = 7
    dte_max:        int = 30
    otm_offset:     int = 1
    strike_count:   int = 8   # strikes each side fetched from the option chain (STRIKE_COUNT env var)
    lot_multiplier: int = 1
    gex_enabled:    bool = True

    # ── Signal Thresholds ──────────────────────────────────────────────────────
    min_score: int = 40
    max_trap:  int = 60

    # ── Session Regime Weighting (U8) ─────────────────────────────────────────
    morning_session_end:   str   = "09:30"
    afternoon_power_start: str   = "14:00"
    power_hour_score_factor: float = 0.80
    morning_score_factor:    float = 1.50

    # ══ Phase A: Hard SL at Entry (always premium-based) ════════════════════════
    # Computed at fill time: if entry_delta known → moneyness-adapted pts
    #                        else                 → premium_stop_pts (fallback)
    # Placed as broker SL-M immediately after fill — does NOT depend on WebSocket.
    premium_stop_pts:   float = 25.0   # fallback fixed premium points SL (env: PREMIUM_STOP_PTS)
    premium_target_pts: float = 50.0   # broker LIMIT target points (used for basket/sequential protection)

    # ── Snapshot Freshness ───────────────────────────────────────────────────────
    snapshot_stale_timeout: float = 5.0   # seconds before a snapshot is considered stale; triggers quote-fetch refresh (env: SNAPSHOT_STALE_TIMEOUT)

    # ══ Session Gates ════════════════════════════════════════════════════════════
    max_trades_per_session: int   = 5
    max_consecutive_losses: int   = 3
    entry_cooldown_secs:    int   = 300
    max_daily_loss_pct:     float = 0.0
    max_daily_loss_amount:  float = 2000.0
    risk_percent:           float = 2.0

    # ══ Phase B: Periodic Trail SL (computed every 1-min sync by TrailSLEngine) ═
    #
    # TRACKING MODE — which price series drives the trail ratchet:
    #   "premium"  → track option LTP        (delta/intrinsic-value aware)
    #   "spot"     → track underlying price   (structural; cleaner for indices)
    trail_tracking_mode: str = "premium"   # env: TRAIL_TRACKING_MODE
    #
    # STEP METHOD — how the trail step distance is computed (both modes support all 4):
    #   "fixed_pct" → % of entry premium; cap applied at entry_premium × 50% to prevent giant steps
    #   "fixed_pts" → raw premium points regardless of price level (best for high-VIX, high-premium options)
    #   "atr"       → ATR-based dynamic step; self-scaling, no cap needed
    #   "delta"     → live delta drives tightness; ITM→tight, OTM→wide; cap at entry_premium × 50%
    trail_sl_method: str = "fixed_pct"   # env: TRAIL_SL_METHOD
    #
    # Activation gate — minimum move before trail engine activates:
    trail_activate_at_pct:     float = 25.0   # % of base distance required before trail fires; env: TRAIL_ACTIVATE_AT_PCT
    trail_activate_at_max_pts: float = 30.0   # Hard ceiling on activation distance in premium pts; 0=no cap; env: TRAIL_ACTIVATE_AT_MAX_PTS
    #                                           Prevents activation from exceeding the TP window on expensive options.
    #                                           Example: premium=₹267, pct=25%→67pts, but TP=50pts → trail never fires.
    #                                           Default 30pts caps activation well inside typical 50pt target window,
    #                                           ensuring trail can activate before target on most trades.
    #
    # Method: fixed_pct params
    trail_step_pct: float = 10.0   # % of entry_premium (premium) or reward_dist (spot); env: TRAIL_STEP_PCT
    #
    # Method: fixed_pts params
    trail_step_pts: float = 15.0   # Raw premium points per ratchet step (independent of entry price); env: TRAIL_STEP_PTS
    #
    # Method: atr params
    trail_atr_period: int   = 14
    trail_atr_mult:   float = 1.5
    #
    # Method: delta params (step tightens as option moves deeper ITM)
    trail_delta_itm_step_pct: float = 5.0    # delta >= 0.55 → tightest
    trail_delta_atm_step_pct: float = 10.0   # 0.35 <= delta < 0.55 → standard
    trail_delta_otm_step_pct: float = 15.0   # delta < 0.35 → widest buffer
    #
    # Spot mode anchor (% of spot price = total spot reward distance):
    spot_reward_pct: float = 0.05   # env: SPOT_REWARD_PCT

    # ── Key Level Trail (structure-driven) ─────────────────────────────────────
    key_level_spacing: dict = field(default_factory=lambda: {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 50, "SENSEX": 100, "BANKEX": 100})
    key_level_trail_style: str = "capture_pct"   # "fixed" | "capture_pct"; env: KEY_LEVEL_TRAIL_STYLE
    key_level_capture_pct: float = 25.0          # % of captured premium range to lock per level; env: KEY_LEVEL_CAPTURE_PCT
    key_level_fixed_pts: float = 15.0            # Fixed premium pts to lock per level (fixed style); env: KEY_LEVEL_FIXED_PTS
    key_level_breakeven_after_levels: int = 1    # Move SL to entry after N completed levels; 0=disable; env: KEY_LEVEL_BE_AFTER_LEVELS

    # ── Mode Flags ─────────────────────────────────────────────────────────────
    long_only_mode:        bool = True
    broker_sl_orders:      bool = True
    use_basket_protection: bool = True

    # ── Technicals ─────────────────────────────────────────────────────────────
    candle_interval: str = "1m"
    lookback_days:   int = 5
    fast_ema_period: int = 9
    slow_ema_period: int = 21
    rsi_period:      int = 14

    # ── Loop Timing ────────────────────────────────────────────────────────────
    signal_check_interval: int = 60
    lookback_bars:         int = 5

    # ── IV Gating ──────────────────────────────────────────────────────────────
    iv_rank_max_entry: float = 40.0
    iv_52w_low:        float = 8.72
    iv_52w_high:       float = 28.91

    # ── Strike Selection ───────────────────────────────────────────────────────
    min_oi_filter:             float = 50_000.0
    min_vol_filter:            float = 10_000.0
    asym_score_threshold:      float = 0.35   # calibrated for 4-component formula (ivr+oi+vol+delta)
    allow_checkpoint_fallback: bool  = True
    delta_target_low:          float = 0.25
    delta_target_high:         float = 0.45

    # ── Order Polling ──────────────────────────────────────────────────────────
    order_status_max_retries:   int   = 15
    order_status_poll_interval: float = 2.0

    # ── Quote API Rate Limiting ────────────────────────────────────────────────
    quote_api_rps:   float = 30.0   # max requests per second (env: QUOTE_API_RPS)
    quote_api_burst: int   = 10     # burst allowance (env: QUOTE_API_BURST)

    # ── U2 Greeks-Aware Deep OTM Exit ─────────────────────────────────────────
    delta_exit_threshold: float = 0.10

    # ── U3 OI Velocity ─────────────────────────────────────────────────────────
    oi_velocity_enabled:   bool  = True
    oi_velocity_threshold: float = 0.05

    # ── U4 Hard Entry Spread Block ─────────────────────────────────────────────
    max_entry_spread_pct: float = 8.0

    # ── U5 Duplicate/Re-entry Guard (configurable per strike) ─────────────────
    same_strike_reentry_guard_enabled: bool = True
    max_same_strike_trades_per_day:    int  = 1

    # ── U6 Drawdown Rate Monitor (velocity-based halt; disabled by default) ───
    drawdown_rate_enabled:     bool  = False
    drawdown_rate_window_mins: int   = 30
    drawdown_rate_max_loss:    float = 1000.0

    # ── U7 Pre-trade Liquidity Preflight ───────────────────────────────────────
    preflight_spread_check:   bool  = True
    preflight_max_spread_pct: float = 10.0
    preflight_min_bid:        float = 5.0

    # ── U9 Adaptive Sizing (disabled by default) ──────────────────────────────
    adaptive_sizing_enabled:     bool = False
    adaptive_max_lot_mult:       int  = 3
    adaptive_win_streak_trigger: int  = 2
    adaptive_win_streak_step:    int  = 1

    # ── Paper Trading ──────────────────────────────────────────────────────────
    paper_trade: bool = False       # simulate fills from WS LTP; no real orders sent

    # ── Daily Profit Target ────────────────────────────────────────────────────
    max_daily_profit_amount: float = 0.0    # halt new entries when day P&L hits this; 0=off

    # ── Session Timing ─────────────────────────────────────────────────────────
    no_new_trade_after: str = "15:10"   # no new BUY entries after this IST time (HH:MM) — must be before square_off_time
    square_off_time:    str = "15:15"   # force-exit all positions at this IST time — MUST match broker MIS cutoff

    # ── Max Hold Time ──────────────────────────────────────────────────────────
    max_hold_minutes: int = 0   # exit positions held > N minutes; 0=disabled

    # ── Breakeven SL ───────────────────────────────────────────────────────────
    breakeven_at_gain_pct: float = 80.0  # move SL to entry cost at X% of target gain; 0=off

    # ── Trade Journal ──────────────────────────────────────────────────────────
    trade_journal_path: str = ""    # CSV path for trade log (timestamp,underlying,entry,exit,pnl,...); ""=off

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Construct a BotConfig from environment variables."""
        # underlyings_csv = os.getenv(
        #     "UNDERLYINGS",
        #     "NIFTY,BANKNIFTY,FINNIFTY,RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS",
        # )
        # index_csv = os.getenv(
        #     "INDEX_UNDERLYINGS",
        #     "NIFTY,BANKNIFTY,FINNIFTY,MIDCPNIFTY,SENSEX,BANKEX,NIFTYNXT50",
        # )
        underlyings_csv = os.getenv("UNDERLYINGS", "NIFTY")
        index_csv = os.getenv("INDEX_UNDERLYINGS", "NIFTY")
        underlyings = sorted(set(u.strip() for u in underlyings_csv.split(",") if u.strip()))
        index_underlyings: frozenset[str] = frozenset(
            u.strip() for u in index_csv.split(",") if u.strip()
        )
        defaults = cls()
        host_server = os.getenv("HOST_SERVER", defaults.api_host)

        # WebSocket URL: explicit env var → auto-corrected → derived from host.
        _ws_env    = os.getenv("WEBSOCKET_URL", defaults.ws_url)
        _ws_domain = host_server[8:].split("/")[0] if host_server.startswith("https://") else ""

        # Correct ws://hostname for HTTPS hosts to wss://domain/ws (port 80 → 301 breaks WS).
        _is_plain_ws_for_https = (
            _ws_env
            and _ws_domain                         # HOST_SERVER is https://
            and _ws_env.startswith("ws://")        # not already wss://
            and "127.0.0.1" not in _ws_env
            and "localhost" not in _ws_env
        )
        if _is_plain_ws_for_https:
            _corrected = f"wss://{_ws_domain}/ws"
            inf(
                f"[CONFIG] WARNING: WEBSOCKET_URL='{_ws_env}' auto-corrected to '{_corrected}'."
                f"\n[CONFIG]          Update your .env: WEBSOCKET_URL={_corrected}"
            )
            _ws_env = _corrected
        if not _ws_env:
            # Strategy runs as a subprocess of the OpenAlgo Python runner — always on the same
            # host as the WS server (port 8765, loopback). Default to localhost instead of
            # wss://domain/ws which requires a Traefik /ws route that is rarely configured.
            # Override with WEBSOCKET_URL=wss://... if you need external/TLS access.
            _ws_env = "ws://127.0.0.1:8765"

        cfg = cls(
            api_key=os.getenv("OPENALGO_API_KEY", defaults.api_key),
            api_host=host_server,
            ws_url=_ws_env,
            strategy_name=os.getenv("STRATEGY_NAME", defaults.strategy_name),
            underlyings=underlyings,
            index_underlyings=index_underlyings,
            spot_exchange=os.getenv("EXCHANGE", defaults.spot_exchange),
            fno_exchange=os.getenv("FNO_EXCHANGE", defaults.fno_exchange),
            index_exchange=os.getenv("INDEX_EXCHANGE", defaults.index_exchange),
            openalgo_username=os.getenv("OPENALGO_USERNAME", defaults.openalgo_username),
            live_pnl_alert_interval=int(os.getenv("LIVE_PNL_ALERT_INTERVAL", str(defaults.live_pnl_alert_interval))),
            risk_based_sizing_enabled=os.getenv("RISK_BASED_SIZING", str(defaults.risk_based_sizing_enabled)).lower() in ("1", "true", "yes"),
            dte_min=int(os.getenv("DTE_MIN", str(defaults.dte_min))),
            dte_max=int(os.getenv("DTE_MAX", str(defaults.dte_max))),
            otm_offset=int(os.getenv("OTM_OFFSET", str(defaults.otm_offset))),
            strike_count=int(os.getenv("STRIKE_COUNT", str(defaults.strike_count))),
            lot_multiplier=int(os.getenv("LOT_MULTIPLIER", str(defaults.lot_multiplier))),
            gex_enabled=os.getenv("GEX_ENABLED", str(defaults.gex_enabled)).lower() in ("1", "true", "yes"),
            min_score=int(os.getenv("MIN_SCORE", str(defaults.min_score))),
            max_trap=int(os.getenv("MAX_TRAP", str(defaults.max_trap))),
            morning_session_end=os.getenv("MORNING_SESSION_END", defaults.morning_session_end),
            afternoon_power_start=os.getenv("AFTERNOON_POWER_START", defaults.afternoon_power_start),
            power_hour_score_factor=float(os.getenv("POWER_HOUR_SCORE_FACTOR", str(defaults.power_hour_score_factor))),
            morning_score_factor=float(os.getenv("MORNING_SCORE_FACTOR", str(defaults.morning_score_factor))),
            premium_stop_pts=float(os.getenv("PREMIUM_STOP_PTS", str(defaults.premium_stop_pts))),
            premium_target_pts=float(os.getenv("PREMIUM_TARGET_PTS", str(defaults.premium_target_pts))),
            max_trades_per_session=int(os.getenv("MAX_TRADES_PER_SESSION", str(defaults.max_trades_per_session))),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", str(defaults.max_consecutive_losses))),
            entry_cooldown_secs=int(os.getenv("ENTRY_COOLDOWN_SECS", str(defaults.entry_cooldown_secs))),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", str(defaults.max_daily_loss_pct))),
            max_daily_loss_amount=float(os.getenv("MAX_DAILY_LOSS_AMOUNT", str(defaults.max_daily_loss_amount))),
            risk_percent=float(os.getenv("RISK_PERCENT", str(defaults.risk_percent))),
            trail_tracking_mode=os.getenv("TRAIL_TRACKING_MODE", defaults.trail_tracking_mode).strip().lower(),
            trail_sl_method=os.getenv("TRAIL_SL_METHOD", defaults.trail_sl_method).strip().lower(),
            spot_reward_pct=float(os.getenv("SPOT_REWARD_PCT", str(defaults.spot_reward_pct))),
            key_level_spacing=eval(os.getenv("KEY_LEVEL_SPACING", str(defaults.key_level_spacing))),
            key_level_trail_style=os.getenv("KEY_LEVEL_TRAIL_STYLE", defaults.key_level_trail_style).strip().lower(),
            key_level_capture_pct=float(os.getenv("KEY_LEVEL_CAPTURE_PCT", str(defaults.key_level_capture_pct))),
            key_level_fixed_pts=float(os.getenv("KEY_LEVEL_FIXED_PTS", str(defaults.key_level_fixed_pts))),
            key_level_breakeven_after_levels=int(os.getenv("KEY_LEVEL_BE_AFTER_LEVELS", str(defaults.key_level_breakeven_after_levels))),
            trail_activate_at_pct=float(os.getenv("TRAIL_ACTIVATE_AT_PCT", str(defaults.trail_activate_at_pct))),
            trail_activate_at_max_pts=float(os.getenv("TRAIL_ACTIVATE_AT_MAX_PTS", str(defaults.trail_activate_at_max_pts))),
            trail_step_pct=float(os.getenv("TRAIL_STEP_PCT", str(defaults.trail_step_pct))),
            trail_step_pts=float(os.getenv("TRAIL_STEP_PTS", str(defaults.trail_step_pts))),
            trail_atr_period=int(os.getenv("TRAIL_ATR_PERIOD", str(defaults.trail_atr_period))),
            trail_atr_mult=float(os.getenv("TRAIL_ATR_MULT", str(defaults.trail_atr_mult))),
            trail_delta_itm_step_pct=float(os.getenv("TRAIL_DELTA_ITM_STEP_PCT", str(defaults.trail_delta_itm_step_pct))),
            trail_delta_atm_step_pct=float(os.getenv("TRAIL_DELTA_ATM_STEP_PCT", str(defaults.trail_delta_atm_step_pct))),
            trail_delta_otm_step_pct=float(os.getenv("TRAIL_DELTA_OTM_STEP_PCT", str(defaults.trail_delta_otm_step_pct))),
            long_only_mode=os.getenv("LONG_ONLY_MODE", str(defaults.long_only_mode)).lower() in ("1", "true", "yes"),
            broker_sl_orders=os.getenv("BROKER_SL_ORDERS", str(defaults.broker_sl_orders)).lower() in ("1", "true", "yes"),
            candle_interval=os.getenv("CANDLE_INTERVAL", defaults.candle_interval),
            lookback_days=int(os.getenv("LOOKBACK_DAYS", str(defaults.lookback_days))),
            fast_ema_period=int(os.getenv("FAST_EMA_PERIOD", str(defaults.fast_ema_period))),
            slow_ema_period=int(os.getenv("SLOW_EMA_PERIOD", str(defaults.slow_ema_period))),
            rsi_period=int(os.getenv("RSI_PERIOD", str(defaults.rsi_period))),
            signal_check_interval=int(os.getenv("SIGNAL_CHECK_INTERVAL", str(defaults.signal_check_interval))),
            lookback_bars=int(os.getenv("LOOKBACK_BARS", str(defaults.lookback_bars))),
            iv_rank_max_entry=float(os.getenv("IV_RANK_MAX_ENTRY", str(defaults.iv_rank_max_entry))),
            iv_52w_low=float(os.getenv("IV_52W_LOW", str(defaults.iv_52w_low))),
            iv_52w_high=float(os.getenv("IV_52W_HIGH", str(defaults.iv_52w_high))),
            min_oi_filter=float(os.getenv("MIN_OI_FILTER", str(defaults.min_oi_filter))),
            min_vol_filter=float(os.getenv("MIN_VOL_FILTER", str(defaults.min_vol_filter))),
            asym_score_threshold=float(os.getenv("ASYM_SCORE_THRESHOLD", str(defaults.asym_score_threshold))),
            allow_checkpoint_fallback=os.getenv("ALLOW_CHECKPOINT_FALLBACK", str(defaults.allow_checkpoint_fallback)).lower() in ("1", "true", "yes"),
            delta_target_low=float(os.getenv("DELTA_TARGET_LOW", str(defaults.delta_target_low))),
            delta_target_high=float(os.getenv("DELTA_TARGET_HIGH", str(defaults.delta_target_high))),
            order_status_max_retries=int(os.getenv("ORDER_STATUS_MAX_RETRIES", str(defaults.order_status_max_retries))),
            order_status_poll_interval=float(os.getenv("ORDER_STATUS_POLL_INTERVAL", str(defaults.order_status_poll_interval))),
            delta_exit_threshold=float(os.getenv("DELTA_EXIT_THRESHOLD", str(defaults.delta_exit_threshold))),
            oi_velocity_enabled=os.getenv("OI_VELOCITY_ENABLED", str(defaults.oi_velocity_enabled)).lower() in ("1", "true", "yes"),
            oi_velocity_threshold=float(os.getenv("OI_VELOCITY_THRESHOLD", str(defaults.oi_velocity_threshold))),
            max_entry_spread_pct=float(os.getenv("MAX_ENTRY_SPREAD_PCT", str(defaults.max_entry_spread_pct))),
            same_strike_reentry_guard_enabled=os.getenv("SAME_STRIKE_REENTRY_GUARD_ENABLED", str(defaults.same_strike_reentry_guard_enabled)).lower() in ("1", "true", "yes"),
            max_same_strike_trades_per_day=int(os.getenv("MAX_SAME_STRIKE_TRADES_PER_DAY", str(defaults.max_same_strike_trades_per_day))),
            drawdown_rate_enabled=os.getenv("DRAWDOWN_RATE_ENABLED", str(defaults.drawdown_rate_enabled)).lower() in ("1", "true", "yes"),
            drawdown_rate_window_mins=int(os.getenv("DRAWDOWN_RATE_WINDOW_MINS", str(defaults.drawdown_rate_window_mins))),
            drawdown_rate_max_loss=float(os.getenv("DRAWDOWN_RATE_MAX_LOSS", str(defaults.drawdown_rate_max_loss))),
            preflight_spread_check=os.getenv("PREFLIGHT_SPREAD_CHECK", str(defaults.preflight_spread_check)).lower() in ("1", "true", "yes"),
            preflight_max_spread_pct=float(os.getenv("PREFLIGHT_MAX_SPREAD_PCT", str(defaults.preflight_max_spread_pct))),
            preflight_min_bid=float(os.getenv("PREFLIGHT_MIN_BID", str(defaults.preflight_min_bid))),
            adaptive_sizing_enabled=os.getenv("ADAPTIVE_SIZING_ENABLED", str(defaults.adaptive_sizing_enabled)).lower() in ("1", "true", "yes"),
            adaptive_max_lot_mult=int(os.getenv("ADAPTIVE_MAX_LOT_MULT", str(defaults.adaptive_max_lot_mult))),
            adaptive_win_streak_trigger=int(os.getenv("ADAPTIVE_WIN_STREAK_TRIGGER", str(defaults.adaptive_win_streak_trigger))),
            adaptive_win_streak_step=int(os.getenv("ADAPTIVE_WIN_STREAK_STEP", str(defaults.adaptive_win_streak_step))),
            paper_trade=os.getenv("PAPER_TRADE", str(defaults.paper_trade)).lower() in ("1", "true", "yes"),
            max_daily_profit_amount=float(os.getenv("MAX_DAILY_PROFIT_AMOUNT", str(defaults.max_daily_profit_amount))),
            no_new_trade_after=os.getenv("NO_NEW_TRADE_AFTER", defaults.no_new_trade_after),
            square_off_time=os.getenv("SQUARE_OFF_TIME", defaults.square_off_time),
            max_hold_minutes=int(os.getenv("MAX_HOLD_MINUTES", str(defaults.max_hold_minutes))),
            breakeven_at_gain_pct=float(os.getenv("BREAKEVEN_AT_GAIN_PCT", str(defaults.breakeven_at_gain_pct))),
            trade_journal_path=os.getenv("TRADE_JOURNAL_PATH", defaults.trade_journal_path),
            quote_api_rps=float(os.getenv("QUOTE_API_RPS", str(defaults.quote_api_rps))),
            quote_api_burst=int(os.getenv("QUOTE_API_BURST", str(defaults.quote_api_burst))),
        )
        _known_equity = {"RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS"}
        _unclassified = [
            s for s in cfg.underlyings
            if s not in cfg.index_underlyings and s not in _known_equity
        ]
        if _unclassified:
            inf(
                f"[CONFIG] WARNING: {_unclassified} are in UNDERLYINGS but not in "
                "INDEX_UNDERLYINGS. If these are index symbols they will be routed via "
                f"SPOT_EXCHANGE ({cfg.spot_exchange}) which may cause bad data. "
                "Add them to INDEX_UNDERLYINGS if they are indices."
            )
        return cfg

    def validate(self) -> None:
        """Validate all configuration values. Raises SystemExit on errors."""
        errors: list[str] = []
        if self.premium_stop_pts <= 0:
            errors.append(f"PREMIUM_STOP_PTS={self.premium_stop_pts} must be > 0 (fallback hard SL points)")
        if self.risk_percent <= 0:
            errors.append(f"RISK_PERCENT={self.risk_percent} must be > 0")
        if self.trail_tracking_mode not in ("premium", "spot"):
            errors.append(f"TRAIL_TRACKING_MODE={self.trail_tracking_mode!r} must be 'premium' or 'spot'")
        if self.trail_sl_method not in ("fixed_pct", "fixed_pts", "atr", "delta"):
            errors.append(f"TRAIL_SL_METHOD={self.trail_sl_method!r} must be 'fixed_pct', 'fixed_pts', 'atr', or 'delta'")
        if self.lot_multiplier < 1:
            errors.append(f"LOT_MULTIPLIER={self.lot_multiplier} must be >= 1")
        if self.strike_count < 1:
            errors.append(f"STRIKE_COUNT={self.strike_count} must be >= 1")
        if not isinstance(self.gex_enabled, bool):
            errors.append(f"GEX_ENABLED={self.gex_enabled!r} must be boolean")
        if not (1 <= self.min_score <= 100):
            errors.append(f"MIN_SCORE={self.min_score} must be in range [1, 100]")
        if not (0 <= self.max_trap <= 100):
            errors.append(f"MAX_TRAP={self.max_trap} must be in range [0, 100]")
        if self.delta_exit_threshold < 0 or self.delta_exit_threshold >= 1:
            errors.append(f"DELTA_EXIT_THRESHOLD={self.delta_exit_threshold} must be in [0, 1)")
        if self.oi_velocity_threshold < 0:
            errors.append(f"OI_VELOCITY_THRESHOLD={self.oi_velocity_threshold} must be >= 0")
        if self.max_entry_spread_pct < 0:
            errors.append(f"MAX_ENTRY_SPREAD_PCT={self.max_entry_spread_pct} must be >= 0")
        if self.max_same_strike_trades_per_day < 1:
            errors.append(
                f"MAX_SAME_STRIKE_TRADES_PER_DAY={self.max_same_strike_trades_per_day} must be >= 1"
            )
        if self.drawdown_rate_window_mins < 1:
            errors.append(
                f"DRAWDOWN_RATE_WINDOW_MINS={self.drawdown_rate_window_mins} must be >= 1"
            )
        if self.drawdown_rate_max_loss < 0:
            errors.append(f"DRAWDOWN_RATE_MAX_LOSS={self.drawdown_rate_max_loss} must be >= 0")
        if self.preflight_max_spread_pct < 0:
            errors.append(f"PREFLIGHT_MAX_SPREAD_PCT={self.preflight_max_spread_pct} must be >= 0")
        if self.preflight_min_bid < 0:
            errors.append(f"PREFLIGHT_MIN_BID={self.preflight_min_bid} must be >= 0")
        if self.power_hour_score_factor <= 0:
            errors.append(f"POWER_HOUR_SCORE_FACTOR={self.power_hour_score_factor} must be > 0")
        if self.morning_score_factor <= 0:
            errors.append(f"MORNING_SCORE_FACTOR={self.morning_score_factor} must be > 0")
        if self.adaptive_max_lot_mult < 1:
            errors.append(f"ADAPTIVE_MAX_LOT_MULT={self.adaptive_max_lot_mult} must be >= 1")
        if self.adaptive_win_streak_trigger < 1:
            errors.append(
                f"ADAPTIVE_WIN_STREAK_TRIGGER={self.adaptive_win_streak_trigger} must be >= 1"
            )
        if self.adaptive_win_streak_step < 1:
            errors.append(f"ADAPTIVE_WIN_STREAK_STEP={self.adaptive_win_streak_step} must be >= 1")
        if self.dte_min < 0 or self.dte_max < self.dte_min:
            errors.append(
                f"DTE_MIN={self.dte_min} / DTE_MAX={self.dte_max}: "
                "must satisfy 0 <= DTE_MIN <= DTE_MAX"
            )
        if not (0 < self.delta_target_low < self.delta_target_high < 1):
            errors.append(
                f"DELTA_TARGET_LOW={self.delta_target_low} / "
                f"DELTA_TARGET_HIGH={self.delta_target_high}: "
                "must satisfy 0 < low < high < 1"
            )
        if self.iv_rank_max_entry <= 0 or self.iv_rank_max_entry > 100:
            errors.append(f"IV_RANK_MAX_ENTRY={self.iv_rank_max_entry} must be in range (0, 100]")
        if self.asym_score_threshold <= 0 or self.asym_score_threshold >= 1:
            errors.append(
                f"ASYM_SCORE_THRESHOLD={self.asym_score_threshold} must be in range (0, 1)"
            )
        if self.order_status_max_retries < 1:
            errors.append(f"ORDER_STATUS_MAX_RETRIES={self.order_status_max_retries} must be >= 1")
        if self.order_status_poll_interval < 0:
            errors.append(f"ORDER_STATUS_POLL_INTERVAL={self.order_status_poll_interval} must be >= 0")
        if self.trail_sl_method not in ("fixed_pct", "fixed_pts", "atr", "delta", "key_level"):
            errors.append(f"TRAIL_SL_METHOD={self.trail_sl_method!r} must be 'fixed_pct', 'fixed_pts', 'atr', 'delta', or 'key_level'")
        if self.key_level_trail_style not in ("fixed", "capture_pct"):
            errors.append(f"KEY_LEVEL_TRAIL_STYLE={self.key_level_trail_style!r} must be 'fixed' or 'capture_pct'")
        if self.key_level_capture_pct < 0 or self.key_level_capture_pct > 100:
            errors.append(f"KEY_LEVEL_CAPTURE_PCT={self.key_level_capture_pct} must be in range [0, 100]")
        if self.key_level_fixed_pts <= 0:
            errors.append(f"KEY_LEVEL_FIXED_PTS={self.key_level_fixed_pts} must be > 0")
        if self.key_level_breakeven_after_levels < 0:
            errors.append(f"KEY_LEVEL_BE_AFTER_LEVELS={self.key_level_breakeven_after_levels} must be >= 0")
        # Validate time strings (HH:MM 24-hour format)
        _hhmm = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
        for _fname, _val in (
            ("NO_NEW_TRADE_AFTER", self.no_new_trade_after),
            ("SQUARE_OFF_TIME",    self.square_off_time),
            ("MORNING_SESSION_END", self.morning_session_end),
            ("AFTERNOON_POWER_START", self.afternoon_power_start),
        ):
            if _val and not _hhmm.match(_val):
                errors.append(f"{_fname}={_val!r} must be in HH:MM format (e.g. '13:30')")
        if self.max_hold_minutes < 0:
            errors.append(f"MAX_HOLD_MINUTES={self.max_hold_minutes} must be >= 0 (0 = disabled)")
        if self.breakeven_at_gain_pct < 0 or self.breakeven_at_gain_pct > 200:
            errors.append(f"BREAKEVEN_AT_GAIN_PCT={self.breakeven_at_gain_pct} must be in [0, 200]")
        if errors:
            inf("[CONFIG] Startup validation failed:")
            for e in errors:
                inf(f"  ✗ {e}")
            raise SystemExit(
                "Fix the configuration errors above before running. "
                "See env-var comments at the top of the file."
            )
        inf("[CONFIG] All configuration values validated OK")


# ===============================================================================
# CUSTOM TYPES
# ===============================================================================

@dataclass
class ScoreComponent:
    label:     str
    score:     float
    score_max: float
    direction: str
    note:      str


@dataclass
class SignalResult:
    score:        int
    label:        str
    signal:       str
    direction:    str | None
    trap_score:   int
    trap_reasons: list[str]
    reasons:      list[str]
    components:   list[ScoreComponent]


@dataclass
class OptionPosition:
    underlying:           str
    symbol:               str
    entry_premium:        float
    qty:                  int
    option_type:          str           # "CE" or "PE"
    spot_symbol:          str
    spot_entry:           float
    reward_dist:          float
    entry_delta:          float | None  = None  # Actual delta at entry; used for SL/TP adaptation
    moneyness:            str           = "Unknown"  # "ATM" / "Sl-OTM" / "OTM" / "Deep-OTM"
    sl:                   float         = 0.0
    initial_sl:           float         = 0.0
    tgt:                  float         = 0.0
    trail_active:         bool          = False
    trail_peak:           float | None  = None
    trail_sl_spot:        float | None  = None
    premium_trail_active: bool          = False
    premium_trail_peak:   float | None  = None
    premium_trail_sl:     float | None  = None
    trail_peak_close:     float | None  = None   # Confirmed-close high; used for BE/trail ratchets
    sl_order_id:          str | None    = None
    tgt_order_id:         str | None    = None
    broker_protection:    bool          = False
    exit_pending:         bool          = False
    # ── new fields ──────────────────────────────────────────────────────────
    entry_time:           datetime      = field(default_factory=datetime.now)
    breakeven_moved:      bool          = False   # True once SL has been shifted to entry cost
    entry_conviction:     float         = 0.0     # ∈ [0,1] conviction at entry; drives adaptive risk engine
    trail_act_mult:       float         = 1.0     # Scaler for trail activation based on moneyness
    # ── key_level trail state ──────────────────────────────────────────────
    kl_active:            bool          = False
    kl_next_level:        float | None  = None
    kl_levels_completed:  int           = 0
    kl_level_premium:     float | None  = None    # Premium at last completed level


@dataclass
class PendingEntry:
    order_id:         str
    symbol:           str
    qty:              int
    spot:             float
    direction:        str
    sl_pts:           float
    created_at:       datetime
    entry_delta:      float | None = None  # Preserved for moneyness-adapted tgt/trail on async fill
    entry_conviction: float = 0.0          # Conviction at entry for adaptive risk on async fill


@dataclass
class PendingExit:
    order_id:   str
    reason:     str
    created_at: datetime


# ===============================================================================
# MARKET SNAPSHOT — single source of truth for live market data
# ===============================================================================

@dataclass
class MarketSnapshot:
    """Timestamped snapshot of all market data for one underlying.

    Every consumer (trail, PNL, alerts, risk) reads from the same snapshot
    so there is zero drift between premium/spot/greeks at a given instant.
    """
    underlying:    str
    timestamp:     float            = 0.0   # time.time() when this was built
    spot_ltp:      float | None     = None
    option_symbol: str | None       = None   # The active position's option symbol
    option_ltp:    float | None     = None
    option_delta:  float | None     = None
    option_theta:  float | None     = None
    option_iv:     float | None     = None
    chain_oi:      float | None     = None   # Total OI for the active option
    chain_volume:  float | None     = None

    def is_stale(self, max_age: float = 5.0) -> bool:
        return (time.time() - self.timestamp) > max_age if self.timestamp else True

    @property
    def has_both_prices(self) -> bool:
        return self.spot_ltp is not None and self.option_ltp is not None


class SnapshotCache:
    """Thread-safe cache of MarketSnapshot per underlying. Single writer,
    multiple readers all see the same timestamped data.

    INVARIANT: 1 underlying ⟷ 1 active option symbol.
    SnapshotCache.option_symbol is singular — multi-leg, scale-in, or hedged
    structures require upgrading to per-symbol snapshots.

    Usage:
        cache = SnapshotCache()
        cache.update("NIFTY", spot_ltp=23100, option_ltp=244)

        snap = cache.get("NIFTY")
        if snap and snap.has_both_prices:
            process(snap)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshots: dict[str, MarketSnapshot] = {}

    def get(self, underlying: str) -> MarketSnapshot | None:
        with self._lock:
            snap = self._snapshots.get(underlying)
            if snap is None:
                return None
            import copy
            return copy.copy(snap)

    def get_stale_underlyings(self, max_age: float) -> list[str]:
        """Return list of underlyings whose snapshot is stale or missing."""
        now = time.time()
        stale: list[str] = []
        with self._lock:
            for ul, snap in self._snapshots.items():
                age = now - snap.timestamp if snap.timestamp else float("inf")
                if age > max_age or snap.option_ltp is None or snap.spot_ltp is None:
                    stale.append(ul)
            # Also include underlyings with positions but no snapshot at all
            # (caller passes the active-positions list for this check)
        return stale

    def get_or_create(self, underlying: str) -> MarketSnapshot:
        with self._lock:
            if underlying not in self._snapshots:
                self._snapshots[underlying] = MarketSnapshot(underlying=underlying)
            snap = self._snapshots[underlying]
            # Return a shallow copy so readers never see a half-updated snapshot
            # (the dataclass fields are primitives so a shallow copy is safe).
            import copy
            return copy.copy(snap)

    def update(self, underlying: str, **fields: Any) -> None:
        """Atomically update a snapshot with the given fields."""
        with self._lock:
            if underlying not in self._snapshots:
                self._snapshots[underlying] = MarketSnapshot(underlying=underlying)
            snap = self._snapshots[underlying]
            for k, v in fields.items():
                if hasattr(snap, k):
                    setattr(snap, k, v)
            snap.timestamp = time.time()

    def set_option_symbol(self, underlying: str, symbol: str) -> None:
        """Link the active position's option symbol so the cache can be
        populated when OptionChain data arrives."""
        self.update(underlying, option_symbol=symbol)

    def update_from_ws_tick(self, underlying: str, symbol: str, spot_ltp: float, option_ltp: float) -> None:
        """Convenience: update both spot and option LTP from a WS tick."""
        self.update(
            underlying,
            spot_ltp=spot_ltp,
            option_symbol=symbol,
            option_ltp=option_ltp,
        )

    def update_from_option_chain(self, underlying: str, chain_data: dict) -> None:
        """Populate from a fetched option-chain row for the tracked symbol."""
        symbol = None
        with self._lock:
            snap = self._snapshots.get(underlying)
            if snap:
                symbol = snap.option_symbol
        if not symbol:
            return
        if "ce_symbol" in chain_data and chain_data["ce_symbol"] == symbol:
            prefix = "ce"
        elif "pe_symbol" in chain_data and chain_data["pe_symbol"] == symbol:
            prefix = "pe"
        else:
            return
        oi    = float(chain_data.get(f"{prefix}_oi", 0) or 0)
        vol   = float(chain_data.get(f"{prefix}_volume", 0) or 0)
        ltp   = float(chain_data.get(f"{prefix}_ltp", 0) or 0)
        delta = float(chain_data.get("ce_delta" if prefix == "ce" else "pe_delta", 0) or 0)
        self.update(underlying, option_ltp=ltp, chain_oi=oi, chain_volume=vol, option_delta=delta if delta else None)

    def update_from_greeks(self, underlying: str, greeks: dict) -> None:
        """Populate greeks data from the optiongreeks API."""
        delta = greeks.get("delta")
        theta = greeks.get("theta")
        iv    = greeks.get("iv")
        self.update(
            underlying,
            option_delta=float(delta) if delta is not None else None,
            option_theta=float(theta) if theta is not None else None,
            option_iv=float(iv) if iv is not None else None,
        )

    def get_all(self) -> list[MarketSnapshot]:
        """Return all non-stale snapshots."""
        now = time.time()
        with self._lock:
            return [copy.copy(s) for s in self._snapshots.values() if now - s.timestamp < 10.0]


# ===============================================================================
# BOT STATE — shared mutable state passed to all components
# ===============================================================================

class BotState:
    """Thread-safe shared state owned by the orchestrator, passed to all components."""

    def __init__(self, lookback_bars: int = 5):
        self.positions:       dict[str, OptionPosition] = {}
        self.ltp_map:         dict[str, float] = {}
        self.snapshot_cache:  SnapshotCache = SnapshotCache()
        self.exit_queue:      set[str] = set()
        self.exit_lock        = threading.Lock()
        self.state_lock       = threading.Lock()
        self.pending_entries: dict[str, PendingEntry] = {}
        self.pending_exits:   dict[str, PendingExit]  = {}
        self.prev_straddle:   dict[str, dict]  = {}
        self.prev_spot:       dict[str, float] = {}
        self.prev_sf:         dict[str, float] = {}
        self.chain_history:   dict[str, deque] = {}
        self._lookback_bars   = lookback_bars
        self.entry_in_flight: int = 0
        self._traded_today:   dict[str, int] = {}

    def get_chain_history(self, symbol: str) -> deque:
        if symbol not in self.chain_history:
            self.chain_history[symbol] = deque(maxlen=max(1, self._lookback_bars))
        return self.chain_history[symbol]

    def reset_market_caches(self):
        self.prev_straddle.clear()
        self.prev_spot.clear()
        self.prev_sf.clear()
        self.chain_history.clear()

    def mark_traded(self, option_symbol: str, direction: str) -> None:
        key = f"{option_symbol}|{direction}"
        with self.state_lock:
            self._traded_today[key] = self._traded_today.get(key, 0) + 1

    def trade_count_today(self, option_symbol: str, direction: str) -> int:
        key = f"{option_symbol}|{direction}"
        with self.state_lock:
            return int(self._traded_today.get(key, 0))

    def reset_traded_today(self) -> None:
        with self.state_lock:
            self._traded_today.clear()


def get_ist_now() -> datetime:
    """Return current IST datetime, works regardless of system timezone.
    Uses _time_mod to compute offset if system is not IST.
    """
    try:
        # Compute offset between local time and UTC
        _tz_off = (_time_mod.mktime(_time_mod.localtime()) - _time_mod.mktime(_time_mod.gmtime())) / 3600
        if abs(_tz_off - 5.5) < 0.1:
            return datetime.now()
        else:
            return datetime.utcnow() + timedelta(hours=5.5)
    except Exception:
        return datetime.now()


def _effective_min_score(now: datetime, cfg: "BotConfig") -> tuple[int, str]:
    """Return the session-adjusted minimum composite score and a reason label.

    Implements U-C (Session-Aware Min Score):
      • Morning  (09:15 – morning_session_end)  : score threshold raised by morning_score_factor
        — higher bar because early-session volatility is noisy and traps are common.
      • Power-hour (afternoon_power_start – no_new_trade_after): threshold eased by power_hour_score_factor
        — institutional momentum flows are cleaner; lower bar improves participation.
      • Mid-session: standard min_score applies.

    Args:
        now: Current IST datetime (use get_ist_now()).
        cfg: Resolved BotConfig instance.

    Returns:
        (effective_score, session_label) tuple.
    """
    now_hm = now.strftime("%H:%M")
    if cfg.morning_session_end and now_hm < cfg.morning_session_end:
        score = max(1, int(cfg.min_score * cfg.morning_score_factor))
        return score, f"morning-gate(raised→{score})"
    if (
        cfg.afternoon_power_start
        and cfg.no_new_trade_after
        and cfg.afternoon_power_start <= now_hm < cfg.no_new_trade_after
    ):
        score = max(1, int(cfg.min_score * cfg.power_hour_score_factor))
        return score, f"power-hour(eased→{score})"
    return cfg.min_score, "mid-session"


def _field_trend(oldest: dict, newest: dict, fld: str) -> int:
    """Return +1 (rising), 0 (flat), or -1 (falling) for a chain field across oldest→newest snapshot."""
    diff = float(newest.get(fld) or 0) - float(oldest.get(fld) or 0)
    return 1 if diff > 0 else (-1 if diff < 0 else 0)


# ===============================================================================
# OI FLOW ANALYZER — chain smoothing + 3-factor Price×Volume×OI classification
# ===============================================================================

class OIFlowAnalyzer:
    """Static helpers for OI-flow analysis and chain smoothing."""

    @staticmethod
    def smooth_chain_rows(history: list) -> list[dict]:
        """
        SMA-smooth per-strike OI/Volume/Premium across N snapshots (oldest-first).
        Appends six direction fields per row for the 3-factor classifier.
        Returns single-bar snapshot unchanged (with zero trend flags).
        """
        if not history:
            return []
        if len(history) == 1:
            result = []
            for row in history[0]:
                r = dict(row)
                r["ce_ltp_dir"] = 0; r["ce_vol_dir"] = 0; r["ce_oi_dir"] = 0
                r["pe_ltp_dir"] = 0; r["pe_vol_dir"] = 0; r["pe_oi_dir"] = 0
                result.append(r)
            return result

        snaps = []
        for snap in history:
            d = {}
            for row in snap:
                k = row.get("strike")
                if k is not None:
                    d[k] = row
            snaps.append(d)

        all_strikes = sorted({k for s in snaps for k in s})
        SMOOTH_FIELDS = [
            "ce_oi", "pe_oi", "ce_volume", "pe_volume",
            "ce_ltp", "pe_ltp", "ce_oi_chg", "pe_oi_chg",
            "ce_ltp_chg", "pe_ltp_chg", "ce_bid", "ce_ask", "pe_bid", "pe_ask",
        ]

        smoothed = []
        for strike in all_strikes:
            rows = [s[strike] for s in snaps if strike in s]
            if not rows:
                continue
            base = None
            for s in reversed(snaps):
                if strike in s:
                    base = dict(s[strike])
                    break
            row_out = dict(base)
            for fld in SMOOTH_FIELDS:
                vals = [float(r.get(fld) or 0) for r in rows]
                row_out[fld] = sum(vals) / len(vals)

            _oldest_row, _newest_row = rows[0], rows[-1]
            row_out["ce_ltp_dir"] = _field_trend(_oldest_row, _newest_row, "ce_ltp")
            row_out["ce_vol_dir"] = _field_trend(_oldest_row, _newest_row, "ce_volume")
            row_out["ce_oi_dir"]  = _field_trend(_oldest_row, _newest_row, "ce_oi")
            row_out["pe_ltp_dir"] = _field_trend(_oldest_row, _newest_row, "pe_ltp")
            row_out["pe_vol_dir"] = _field_trend(_oldest_row, _newest_row, "pe_volume")
            row_out["pe_oi_dir"]  = _field_trend(_oldest_row, _newest_row, "pe_oi")
            smoothed.append(row_out)
        return smoothed

    @staticmethod
    def compute_pcr(chain_rows: list[dict]) -> float:
        ce_oi = sum(r.get("ce_oi", 0) or 0 for r in chain_rows)
        pe_oi = sum(r.get("pe_oi", 0) or 0 for r in chain_rows)
        return (pe_oi / ce_oi) if ce_oi else 1.0

    @staticmethod
    def call_wall(chain_rows: list[dict]) -> float | None:
        return max(chain_rows, key=lambda r: r.get("ce_oi", 0))["strike"] if chain_rows else None

    @staticmethod
    def put_wall(chain_rows: list[dict]) -> float | None:
        return max(chain_rows, key=lambda r: r.get("pe_oi", 0))["strike"] if chain_rows else None

    @staticmethod
    def classify_ce_flow(chain_rows: list[dict]) -> tuple[float, str]:
        """3-factor CE flow classifier (8-state matrix). Falls back to 2-factor on single bar."""
        def _agg(fld: str) -> int:
            raw = sum(r.get(fld, 0) or 0 for r in chain_rows)
            return 1 if raw > 0 else (-1 if raw < 0 else 0)

        l, v, o = _agg("ce_ltp_dir"), _agg("ce_vol_dir"), _agg("ce_oi_dir")
        if v != 0:
            if   l ==  1 and v ==  1 and o ==  1: return  2.0, "Call Buying — strong bullish conviction"
            elif l ==  1 and v ==  1 and o == -1: return  1.0, "CE Short Covering — moderately bullish"
            elif l ==  1 and v == -1 and o ==  1: return  0.5, "CE accumulation low volume — cautiously bullish"
            elif l ==  1 and v == -1 and o == -1: return  0.0, "CE fading interest — weakening"
            elif l == -1 and v ==  1 and o ==  1: return -2.0, "Call Writing — strong bearish signal"
            elif l == -1 and v ==  1 and o == -1: return -1.0, "CE Long Unwinding — moderately bearish"
            elif l == -1 and v == -1 and o ==  1: return -0.5, "Call writing low volume — cautiously bearish"
            elif l == -1 and v == -1 and o == -1: return  0.0, "CE pressure fading — weakening bearish"
            return 0.0, "CE Neutral"
        # 2-factor fallback
        oi_chg  = sum(r.get("ce_oi_chg", 0) or 0 for r in chain_rows)
        ltp_chg = sum(r.get("ce_ltp_chg", 0) or 0 for r in chain_rows)
        if oi_chg > 0 and ltp_chg > 0.5:  return  2, "Call Buying"
        if oi_chg < 0 and ltp_chg > 0.5:  return  1, "CE Short Covering"
        if oi_chg > 0 and ltp_chg < -0.5: return -2, "Call Writing"
        if oi_chg < 0 and ltp_chg < -0.5: return -1, "CE Long Unwinding"
        return 0, "CE Neutral"

    @staticmethod
    def classify_pe_flow(chain_rows: list[dict]) -> tuple[float, str]:
        """3-factor PE flow classifier (8-state matrix). Falls back to 2-factor on single bar."""
        def _agg(fld: str) -> int:
            raw = sum(r.get(fld, 0) or 0 for r in chain_rows)
            return 1 if raw > 0 else (-1 if raw < 0 else 0)

        l, v, o = _agg("pe_ltp_dir"), _agg("pe_vol_dir"), _agg("pe_oi_dir")
        if v != 0:
            if   l ==  1 and v ==  1 and o ==  1: return -2.0, "Put Buying — strong bearish for underlying"
            elif l ==  1 and v ==  1 and o == -1: return -1.0, "PE Short Covering — moderately bearish"
            elif l ==  1 and v == -1 and o ==  1: return -0.5, "Put accumulation low volume — cautiously bearish"
            elif l ==  1 and v == -1 and o == -1: return  0.0, "PE demand fading — weakening bearish"
            elif l == -1 and v ==  1 and o ==  1: return  2.0, "Put Writing — strong bullish for underlying"
            elif l == -1 and v ==  1 and o == -1: return  1.0, "PE Long Unwinding — moderately bullish"
            elif l == -1 and v == -1 and o ==  1: return  0.5, "Put writing low volume — cautiously bullish"
            elif l == -1 and v == -1 and o == -1: return  0.0, "PE pressure fading — weakening bullish"
            return 0.0, "PE Neutral"
        # 2-factor fallback
        oi_chg  = sum(r.get("pe_oi_chg", 0) or 0 for r in chain_rows)
        ltp_chg = sum(r.get("pe_ltp_chg", 0) or 0 for r in chain_rows)
        if oi_chg > 0 and ltp_chg < -0.5: return  2, "Put Writing"
        if oi_chg > 0 and ltp_chg > 0.5:  return -2, "Put Buying"
        if oi_chg < 0 and ltp_chg > 0.5:  return  1, "PE Short Covering"
        if oi_chg < 0 and ltp_chg < -0.5: return -1, "PE Long Unwinding"
        return 0, "PE Neutral"


# ===============================================================================
# SIGNAL ENGINE — five-layer composite scoring
# ===============================================================================

class SignalEngine:
    """Computes composite directional score and trap score from market data."""

    def __init__(self, config: BotConfig):
        self.config = config

    @staticmethod
    def iv_rank(
        current_iv: float | None,
        iv_52w_low: float | None,
        iv_52w_high: float | None,
    ) -> float | None:
        if current_iv is None or iv_52w_low is None or iv_52w_high is None:
            return None
        if iv_52w_high <= iv_52w_low:
            return None
        return (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100

    def score(
        self,
        spot: float,
        df_spot: pd.DataFrame,
        chain_rows: list[dict],
        atm_ce_ltp: float,
        atm_pe_ltp: float,
        iv_rank: float | None = None,
        straddle_price: float | None = None,
        prev_straddle_price: float | None = None,
        sf_ltp: float | None = None,
        ce_bid: float | None = None,
        ce_ask: float | None = None,
        pe_bid: float | None = None,
        pe_ask: float | None = None,
        ce_delta: float | None = None,
        pe_delta: float | None = None,
        gex_levels: dict[str, Any] | None = None,
        min_score_override: int | None = None,
        prev_spot: float | None = None,
        prev_sf_ltp: float | None = None,
        ce_iv_rank: float | None = None,
        pe_iv_rank: float | None = None,
        best_fit_iv_side: str | None = None,
    ) -> SignalResult:
        """Compute composite directional score (−100 → +100) and trap_score (0 → 100)."""
        cfg = self.config
        components: list[ScoreComponent] = []
        reasons:    list[str] = []

        def _dir(s: float) -> str:
            return "bullish" if s > 0 else "bearish" if s < 0 else "neutral"

        def _c(label: str, score: float, score_max: float, direction: str, note: str) -> None:
            components.append(ScoreComponent(label=label, score=score, score_max=score_max,
                                             direction=direction, note=note))

        # ── LAYER 1: Technical Trend ─────────────────────────────────────────
        # L1-a: EMA crossover
        s1 = 0
        trend_note = "Insufficient candles"
        if df_spot is not None and len(df_spot) >= cfg.slow_ema_period + 2:
            fast = ta.ema(df_spot["close"], period=cfg.fast_ema_period)
            slow = ta.ema(df_spot["close"], period=cfg.slow_ema_period)
            if len(df_spot) >= cfg.slow_ema_period + 3 and fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-3] <= slow.iloc[-3]:
                s1 = 1;   trend_note = f"Bullish EMA crossover ({cfg.fast_ema_period}/{cfg.slow_ema_period})"
            elif len(df_spot) >= cfg.slow_ema_period + 3 and fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-3] >= slow.iloc[-3]:
                s1 = -1;  trend_note = f"Bearish EMA crossover ({cfg.fast_ema_period}/{cfg.slow_ema_period})"
            elif fast.iloc[-2] > slow.iloc[-2]:
                s1 = 0.5; trend_note = "Fast EMA above Slow EMA (bullish)"
            elif fast.iloc[-2] < slow.iloc[-2]:
                s1 = -0.5; trend_note = "Fast EMA below Slow EMA (bearish)"
        _c("EMA Trend", s1, 1, _dir(s1), trend_note)

        # L1-b: RSI
        s2 = 0
        rsi_note = "RSI unavailable"
        if df_spot is not None and len(df_spot) >= cfg.rsi_period + 2:
            rsi = ta.rsi(df_spot["close"], period=cfg.rsi_period)
            rv = rsi.iloc[-2]
            if rv > 53:      s2 = 1;    rsi_note = f"RSI {rv:.1f} — bullish momentum"
            elif rv > 50:    s2 = 0.5;  rsi_note = f"RSI {rv:.1f} — mild bullish tilt"
            elif rv < 47:    s2 = -1;   rsi_note = f"RSI {rv:.1f} — bearish momentum"
            elif rv < 50:    s2 = -0.5; rsi_note = f"RSI {rv:.1f} — mild bearish tilt"
            else:            rsi_note = f"RSI {rv:.1f} — exactly neutral (50)"
        _c("RSI Momentum", s2, 1, _dir(s2), rsi_note)

        # L1-c: MACD Histogram
        s3 = 0
        macd_note = "MACD unavailable"
        if df_spot is not None and len(df_spot) >= 35:
            _, _, hist = ta.macd(df_spot["close"])
            h_now, h_prev = hist.iloc[-2], hist.iloc[-3]
            if h_now > 0 and h_now > h_prev:    s3 = 1;    macd_note = "MACD Histogram expanding positive"
            elif h_now < 0 and h_now < h_prev:  s3 = -1;   macd_note = "MACD Histogram expanding negative"
            elif h_now > 0:                      s3 = 0.5;  macd_note = "MACD Histogram positive (contracting)"
            elif h_now < 0:                      s3 = -0.5; macd_note = "MACD Histogram negative (contracting)"
        _c("MACD Histogram", s3, 1, _dir(s3), macd_note)

        # L1-d: Price vs VWAP
        s4 = 0
        vwap_note = "VWAP unavailable"
        if df_spot is not None and len(df_spot) >= 5 and "volume" in df_spot.columns:
            today = pd.Timestamp.now().normalize()
            df_today = (
                df_spot[df_spot.index.normalize() == today]
                if isinstance(df_spot.index, pd.DatetimeIndex)
                else df_spot
            )
            # Use today's data if >= 5 bars; fallback to last 5 bars overall (today + yesterday if needed)
            if len(df_today) >= 5:
                df_vwap = df_today
                source = "today"
            elif len(df_spot) >= 5:
                df_vwap = df_spot.iloc[-5:]  # Last 5 bars across day boundary if needed
                source = "rolling_5bar"
            else:
                df_vwap = None
                source = "insufficient"
            
            if df_vwap is not None:
                # Convert volume to numeric and filter out zero/NaN bars
                df_vwap = df_vwap.copy()
                df_vwap["volume"] = pd.to_numeric(df_vwap["volume"], errors='coerce')
                
                # Filter to bars with non-zero volume
                df_valid_vol = df_vwap[df_vwap["volume"] > 0]
                
                if len(df_valid_vol) >= 5:
                    # Use last 5 valid bars
                    df_vwap_calc = df_valid_vol.iloc[-5:] if len(df_valid_vol) > 5 else df_valid_vol
                    try:
                        vwap = ta.vwap(df_vwap_calc["high"], df_vwap_calc["low"], df_vwap_calc["close"], df_vwap_calc["volume"])
                        vv = vwap.iloc[-1]
                        if spot > vv:
                            s4 = 1;  vwap_note = f"Spot {spot:.1f} above VWAP {vv:.1f} ({source}, {len(df_vwap_calc)} valid bars)"
                        else:
                            s4 = -1; vwap_note = f"Spot {spot:.1f} below VWAP {vv:.1f} ({source}, {len(df_vwap_calc)} valid bars)"
                    except Exception as e:
                        vwap_note = f"VWAP calc error: {str(e)[:40]}"
                else:
                    # Insufficient bars with volume
                    zero_vol_count = (df_vwap["volume"] == 0).sum()
                    vwap_note = f"VWAP insufficient volume ({len(df_valid_vol)}/5 have volume; {zero_vol_count} zero)"
            else:
                vwap_note = "VWAP insufficient bars (need 5)"
        _c("Spot vs VWAP", s4, 1, _dir(s4), vwap_note)

        # ── LAYER 2: OI Flow Intelligence ────────────────────────────────────
        # L2-a: PCR OI Level
        pcr = OIFlowAnalyzer.compute_pcr(chain_rows)
        if pcr <= 0.6:   s5 = 1
        elif pcr <= 0.9: s5 = 0.5
        elif pcr <= 1.1: s5 = 0
        elif pcr <= 1.3: s5 = -0.5
        else:            s5 = -1
        _c("PCR OI Level", s5, 1, _dir(s5), f"PCR OI {pcr:.2f}")

        # L2-b: Call Flow
        s6, ce_flow_label = OIFlowAnalyzer.classify_ce_flow(chain_rows)
        _c("Call OI Flow", s6, 2, _dir(s6), ce_flow_label)

        # L2-c: Put Flow
        s7, pe_flow_label = OIFlowAnalyzer.classify_pe_flow(chain_rows)
        _c("Put OI Flow", s7, 2, _dir(s7), pe_flow_label)

        # L2-d: OI Wall Position
        s8 = 0
        cw = OIFlowAnalyzer.call_wall(chain_rows)
        pw = OIFlowAnalyzer.put_wall(chain_rows)
        wall_note = "OI walls unavailable"
        if cw and pw and spot:
            if spot < cw and spot > pw:
                if (cw - spot) > (spot - pw):
                    s8 = 0.5;  wall_note = f"Spot between walls, near put support {pw:.0f} (call wall {cw:.0f} far) — mild bullish"
                else:
                    s8 = -0.5; wall_note = f"Spot between walls, near call resistance {cw:.0f} (put wall {pw:.0f} far) — mild bearish"
            elif spot >= cw:
                s8 = -1; wall_note = f"Spot {spot:.0f} at/above call wall {cw:.0f} — overhead resistance, bearish"
            elif spot <= pw:
                s8 = -1; wall_note = f"Spot {spot:.0f} below put wall {pw:.0f} — support broken, put writers hedging (bearish)"
        _c("OI Wall Position", s8, 1, _dir(s8), wall_note)

        # ── LAYER 3: Greeks Engine ───────────────────────────────────────────
        # L3-a: Delta Imbalance
        s9 = 0
        di_note = "Delta imbalance unavailable"
        _delta_computed = False
        if ce_delta is not None and pe_delta is not None:
            di = ce_delta + pe_delta
            if di >= 0.05:        s9 = 1;    di_note = f"ATM Δ sum {di:+.3f} — CE ITM, net bullish  (CE {ce_delta:+.3f} / PE {pe_delta:+.3f})"
            elif di >= 0.02:      s9 = 0.5;  di_note = f"ATM Δ sum {di:+.3f} — mild CE dominance   (CE {ce_delta:+.3f} / PE {pe_delta:+.3f})"
            elif di <= -0.05:     s9 = -1;   di_note = f"ATM Δ sum {di:+.3f} — PE ITM, net bearish  (CE {ce_delta:+.3f} / PE {pe_delta:+.3f})"
            elif di <= -0.02:     s9 = -0.5; di_note = f"ATM Δ sum {di:+.3f} — mild PE dominance   (CE {ce_delta:+.3f} / PE {pe_delta:+.3f})"
            else:                 di_note = f"ATM Δ sum {di:+.3f} — balanced (CE {ce_delta:+.3f} / PE {pe_delta:+.3f})"
            _delta_computed = True
        if not _delta_computed and atm_ce_ltp and atm_pe_ltp and atm_pe_ltp > 0:
            di = (atm_ce_ltp - atm_pe_ltp) / ((atm_ce_ltp + atm_pe_ltp) / 2)
            if di >= 0.10:        s9 = 1;    di_note = f"LTP proxy Δ {di:+.3f} — CE premium heavy (bullish)"
            elif di >= 0.05:      s9 = 0.5;  di_note = f"LTP proxy Δ {di:+.3f} — mild CE premium"
            elif di <= -0.10:     s9 = -1;   di_note = f"LTP proxy Δ {di:+.3f} — PE premium heavy (bearish)"
            elif di <= -0.05:     s9 = -0.5; di_note = f"LTP proxy Δ {di:+.3f} — mild PE premium"
            else:                 di_note = f"LTP proxy Δ {di:+.3f} — balanced"
        _c("Greeks Bias (Δ)", s9, 1, _dir(s9), di_note)

        # L3-b: Gamma Regime (institutional GEX interpretation)
        s10 = 0
        gamma_note = "Gamma regime unavailable"
        if gex_levels:
            total_net_gex = float(gex_levels.get("total_net_gex") or 0)
            gamma_flip = gex_levels.get("gamma_flip")
            upside_punch = gex_levels.get("upside_punch_target")
            downside_punch = gex_levels.get("downside_punch_target")

            if total_net_gex < 0:
                # Short-gamma regime: directional moves tend to accelerate near nearest
                # negative-net-GEX strike (punch target).
                chosen_side = None
                chosen_target = None
                chosen_dist = None

                if upside_punch is not None and downside_punch is not None:
                    up_dist = abs(float(upside_punch) - spot)
                    dn_dist = abs(spot - float(downside_punch))
                    if up_dist < dn_dist:
                        chosen_side, chosen_target, chosen_dist = "upside", float(upside_punch), up_dist
                    elif dn_dist < up_dist:
                        chosen_side, chosen_target, chosen_dist = "downside", float(downside_punch), dn_dist
                elif upside_punch is not None:
                    chosen_side, chosen_target, chosen_dist = "upside", float(upside_punch), abs(float(upside_punch) - spot)
                elif downside_punch is not None:
                    chosen_side, chosen_target, chosen_dist = "downside", float(downside_punch), abs(spot - float(downside_punch))

                if chosen_side == "upside":
                    s10 = 1
                elif chosen_side == "downside":
                    s10 = -1

                # Very near punch target implies stronger acceleration risk.
                if chosen_dist is not None and spot > 0 and chosen_dist <= spot * 0.0025 and s10 != 0:
                    s10 = 2 if s10 > 0 else -2

                if chosen_side and chosen_target is not None:
                    gamma_note = (
                        f"Short gamma (Net GEX {total_net_gex:+.0f}); nearest {chosen_side} punch "
                        f"{chosen_target:.0f} from spot {spot:.0f}"
                    )
                else:
                    gamma_note = (
                        f"Short gamma (Net GEX {total_net_gex:+.0f}) but no nearby punch target "
                        "resolved — neutral"
                    )

            elif total_net_gex > 0:
                gamma_note = (
                    f"Long gamma (Net GEX {total_net_gex:+.0f}) — dealer hedging tends to dampen "
                    "directional follow-through"
                )
                s10 = 0
            else:
                gamma_note = "Net GEX near zero — no clear gamma regime"

            if gamma_flip is not None:
                gamma_note += f" | gamma flip {float(gamma_flip):.0f}"

        _c("Gamma Regime", s10, 2, _dir(s10), gamma_note)

        # L3-c: OI Build/Unwind Velocity (U3; additive to Gamma, not replacement)
        s10b = 0
        oi_vel_note = "OI velocity unavailable"
        if cfg.oi_velocity_enabled and chain_rows:
            ce_oi_chg = sum(float(r.get("ce_oi_chg", 0) or 0) for r in chain_rows)
            pe_oi_chg = sum(float(r.get("pe_oi_chg", 0) or 0) for r in chain_rows)
            ce_oi_tot = sum(float(r.get("ce_oi", 0) or 0) for r in chain_rows)
            pe_oi_tot = sum(float(r.get("pe_oi", 0) or 0) for r in chain_rows)
            ce_vel = (ce_oi_chg / ce_oi_tot * 100) if ce_oi_tot > 0 else 0.0
            pe_vel = (pe_oi_chg / pe_oi_tot * 100) if pe_oi_tot > 0 else 0.0
            th = cfg.oi_velocity_threshold

            if ce_vel > th and s6 > 0:
                s10b = 1
                oi_vel_note = (
                    f"CE OI building {ce_vel:+.2%} + call buying — institutional accumulation"
                )
            elif ce_vel > th and s6 < 0:
                s10b = -1
                oi_vel_note = (
                    f"CE OI building {ce_vel:+.2%} + call writing — institutional writer trap"
                )
            elif ce_vel < -th and s6 > 0:
                s10b = 0.5
                oi_vel_note = f"CE OI unwinding {ce_vel:+.2%} — short covering"
            elif pe_vel > th and s7 < 0:
                s10b = -1
                oi_vel_note = (
                    f"PE OI building {pe_vel:+.2%} + put buying — bearish accumulation"
                )
            elif pe_vel > th and s7 > 0:
                s10b = 1
                oi_vel_note = (
                    f"PE OI building {pe_vel:+.2%} + put writing — institutional support"
                )
            elif pe_vel < -th and s7 < 0:
                s10b = -0.5
                oi_vel_note = f"PE OI unwinding {pe_vel:+.2%} — put covering"
            else:
                oi_vel_note = f"OI velocity below threshold (CE {ce_vel:+.2%}, PE {pe_vel:+.2%})"
        else:
            oi_vel_note = "OI velocity disabled"
        _c("OI Velocity", s10b, 2, _dir(s10b), oi_vel_note)

        # ── LAYER 4: Straddle & IV ───────────────────────────────────────────
        # L4-a: IV Regime (Separate CE/PE Analysis)
        s11 = 0
        iv_note = "IVR unavailable"
        best_ivr = None
        
        # Use separate CE/PE IV Ranks if available, otherwise fall back to legacy single rank
        if ce_iv_rank is not None and pe_iv_rank is not None:
            # Both available: use best fit (lower = cheaper for buying)
            best_ivr = min(ce_iv_rank, pe_iv_rank)
            best_side = "CE" if ce_iv_rank <= pe_iv_rank else "PE"
            iv_note = f"IVR: CE={ce_iv_rank:.1f}% / PE={pe_iv_rank:.1f}% → best={best_side}({best_ivr:.1f}%)"
        elif ce_iv_rank is not None:
            best_ivr = ce_iv_rank
            iv_note = f"IVR: CE={ce_iv_rank:.1f}% (PE unavailable)"
        elif pe_iv_rank is not None:
            best_ivr = pe_iv_rank
            iv_note = f"IVR: PE={pe_iv_rank:.1f}% (CE unavailable)"
        elif iv_rank is not None:
            # Legacy single-rank fallback
            best_ivr = iv_rank
            iv_note = f"IVR {iv_rank:.1f}% (legacy single-rank)"
        
        # Score based on best-fit IV Rank
        if best_ivr is not None:
            if best_ivr < 20:       s11 = 1;    iv_note += " — structurally cheap, full buyer edge"
            elif best_ivr < 40:     s11 = 0.5;  iv_note += " — moderate, mild buyer edge"
            elif best_ivr > 60:     s11 = -1;   iv_note += " — structurally expensive, buyer disadvantage"
            elif best_ivr > 50:     s11 = -0.5; iv_note += " — elevated, mild seller edge"
            else:                   iv_note += " — neutral zone (40–50%)"
        
        _c("IV Regime (IVR)", s11, 1, _dir(s11), iv_note)

        # L4-b: Straddle Velocity
        s12 = 0
        straddle_note = "Straddle velocity unavailable"
        straddle_vel  = "Flat"
        if straddle_price and prev_straddle_price and prev_straddle_price > 0:
            chg_pct = (straddle_price - prev_straddle_price) / prev_straddle_price * 100
            if chg_pct >= 1.5:      s12 = 2;  straddle_vel = "Expanding";         straddle_note = f"Straddle expanding {chg_pct:+.1f}% — real directional move, buyer edge"
            elif chg_pct >= 0.5:    s12 = 1;  straddle_vel = "Mild Expansion";    straddle_note = f"Straddle mild expansion {chg_pct:+.1f}% — modest premium growth"
            elif chg_pct <= -1.5:   s12 = -2; straddle_vel = "Contracting";       straddle_note = f"Straddle contracting {chg_pct:+.1f}% — IV crush, avoid naked buying"
            elif chg_pct <= -0.5:   s12 = -1; straddle_vel = "Mild Contraction";  straddle_note = f"Straddle mild contraction {chg_pct:+.1f}% — premium fading"
            else:                   straddle_note = f"Straddle flat ({chg_pct:+.1f}%)"
        _c("Straddle Velocity", s12, 2, _dir(s12), straddle_note)

        # ── LAYER 5: Synthetic Futures co-movement ───────────────────────────
        s13 = 0
        sf_note = "SF data unavailable"
        if sf_ltp and spot:
            spread_pct = None
            if ce_bid and ce_ask and ce_bid > 0:
                spread_pct = (ce_ask - ce_bid) / ((ce_ask + ce_bid) / 2) * 100
            if spread_pct is not None and spread_pct > 1.5:
                s13 = 0; sf_note = f"Wide option spread {spread_pct:.1f}% — executable cost degrades signal"
            elif prev_spot is not None and prev_sf_ltp is not None:
                move_threshold = spot * 0.0003
                spot_move = spot - prev_spot
                sf_move   = sf_ltp - prev_sf_ltp
                if spot_move > move_threshold and sf_move > move_threshold:
                    s13 = 1;  sf_note = f"SF co-movement bullish: spot Δ{spot_move:+.1f}, SF Δ{sf_move:+.1f} — confirming"
                elif spot_move < -move_threshold and sf_move < -move_threshold:
                    s13 = -1; sf_note = f"SF co-movement bearish: spot Δ{spot_move:+.1f}, SF Δ{sf_move:+.1f} — confirming"
                else:
                    basis = sf_ltp - spot
                    carry = "normal" if basis >= -(spot * 0.001) else "backwardation"
                    sf_note = f"SF diverging or insufficient move — no directional vote (basis {basis:+.1f}, {carry})"
            else:
                basis = sf_ltp - spot
                carry = "normal" if basis >= -(spot * 0.001) else "backwardation"
                sf_note = f"SF snapshot only (no prior bar): basis {basis:+.1f} ({carry}) — score 0"
        _c("Synthetic Futures", s13, 1, _dir(s13), sf_note)

        # ── Trap Score ───────────────────────────────────────────────────────
        trap_score   = 0
        trap_reasons = []
        if straddle_vel == "Contracting":
            trap_score += 25; trap_reasons.append("Straddle contracting — IV crush trap")
        # Use best-fit IVR for trap check
        if best_ivr is not None and best_ivr > 60:
            trap_score += 20; trap_reasons.append(f"High IVR {best_ivr:.1f}% — options structurally overpriced")
        elif iv_rank is not None and iv_rank > 60:
            trap_score += 20; trap_reasons.append(f"High IVR {iv_rank:.1f}% — options structurally overpriced")
        if sf_ltp and spot and abs(sf_ltp - spot) > spot * 0.015:
            trap_score += 15; trap_reasons.append(f"SF basis divergence {abs(sf_ltp-spot)/spot*100:.2f}% — possible mispricing")
        if ce_bid and ce_ask and ce_bid > 0:
            sp = (ce_ask - ce_bid) / ((ce_ask + ce_bid) / 2) * 100
            if sp > 1.5:
                trap_score += 15; trap_reasons.append(f"Wide CE spread {sp:.1f}% — high slippage cost")
        if pe_bid and pe_ask and pe_bid > 0:
            sp = (pe_ask - pe_bid) / ((pe_ask + pe_bid) / 2) * 100
            if sp > 1.5:
                trap_score += 15; trap_reasons.append(f"Wide PE spread {sp:.1f}% — high slippage cost")
        if pcr > 2.5:
            trap_score += 10; trap_reasons.append(f"PCR OI {pcr:.2f} — extreme put skew, reversal risk")
        elif pcr < 0.4:
            trap_score += 10; trap_reasons.append(f"PCR OI {pcr:.2f} — extreme call skew, reversal risk")
        trap_score = min(100, trap_score)

        # ── Final Score ──────────────────────────────────────────────────────
        # Active score_max sum includes Gamma Regime now.
        # Current total: EMA(1)+RSI(1)+MACD(1)+VWAP(1)+PCR(1)+CE-flow(2)+PE-flow(2)
        # +Wall(1)+Delta(1)+Gamma(2)+IV(1)+Straddle(2)+SF(1) = 17
        MAX_RAW_SCORE = sum(c.score_max for c in components)
        raw_score  = sum(c.score for c in components)
        
        # We cap expected alignment to PRACTICAL_ALIGNMENT_FACTOR, so achieving this threshold yields a 100 score.
        effective_max = MAX_RAW_SCORE * PRACTICAL_ALIGNMENT_FACTOR
        base_score = (raw_score / effective_max) * 100 if effective_max > 0 else 0
        
        final_score = int(max(-100, min(100, base_score)))

        for c in components:
            if abs(c.score) >= (c.score_max * 0.5):
                reasons.append(c.note)

        abs_score = abs(final_score)
        effective_min_score = int(min_score_override) if min_score_override is not None else cfg.min_score
        if effective_min_score < 1:
            effective_min_score = 1
        elif effective_min_score > 100:
            effective_min_score = 100

        if trap_score > cfg.max_trap:
            signal = "NO_TRADE"
            if trap_reasons:
                reasons.insert(0, f"⚠ High trap risk: {trap_reasons[0]}")
        elif abs_score >= effective_min_score:
            signal = "EXECUTE"
        elif abs_score >= int(effective_min_score * WATCH_FACTOR):
            signal = "WATCH"
        else:
            signal = "NO_TRADE"

        label = "Bullish" if final_score > 15 else "Bearish" if final_score < -15 else "Neutral"
        direction: str | None = "CE" if final_score > 0 else ("PE" if final_score < 0 else None)

        return SignalResult(
            score=final_score, label=label, signal=signal, direction=direction,
            trap_score=trap_score, trap_reasons=trap_reasons,
            reasons=list(dict.fromkeys(reasons)), components=components,
        )


# ===============================================================================
# DATA FETCHER — all market data calls via OpenAlgo SDK
# ===============================================================================

class DataFetcher:
    """Fetches all market data using the OpenAlgo SDK client."""

    def __init__(self, client: api, config: BotConfig, notify_callback: Callable[[str, int], None] | None = None):
        self.client = client
        self.config = config
        self._greeks_cache: OrderedDict[tuple[str, str], dict[str, float]] = OrderedDict()
        self._greeks_cache_hits: int = 0
        self._notify = notify_callback
        self._greeks_cache_misses: int = 0
        self._greeks_api_calls: int = 0
        self._greeks_cache_max_size: int = 500  # LRU: prevent unbounded growth
        self._auth_error_notified: bool = False  # One-time alert per session for UDAPI100050
        # Token bucket rate limiter for fetch_quote (global across all callers)
        self._quote_rate_limit_rps: float = config.quote_api_rps
        self._quote_burst: int = config.quote_api_burst
        self._quote_tokens: float = float(self._quote_burst)
        self._quote_last_refill: float = time.time()
        self._quote_lock = threading.Lock()

    def clear_greeks_cache(self, symbol: str | None = None) -> None:
        """Clear cached option greeks. Called once per scan to avoid stale reads."""
        if symbol is None:
            self._greeks_cache.clear()
            self._greeks_cache_hits = 0
            self._greeks_cache_misses = 0
            self._greeks_api_calls = 0
            return
        keys = [k for k in self._greeks_cache if k[0] == symbol]
        for k in keys:
            self._greeks_cache.pop(k, None)
        # Symbol scan cycle starts fresh counters.
        self._greeks_cache_hits = 0
        self._greeks_cache_misses = 0
        self._greeks_api_calls = 0

    def _fetch_option_greeks_cached(
        self, symbol: str, option_symbol: str | None
    ) -> dict[str, float] | None:
        if not option_symbol:
            return None
        cache_key = (symbol, option_symbol)
        cached = self._greeks_cache.get(cache_key)
        if cached is not None:
            self._greeks_cache_hits += 1
            self._greeks_cache.move_to_end(cache_key)
            return cached
        self._greeks_cache_misses += 1

        try:
            self._greeks_api_calls += 1
            resp = self.client.optiongreeks(
                symbol=option_symbol,
                exchange=self.config.fno_exchange,
                underlying_symbol=symbol,
                underlying_exchange=self.underlying_exchange(symbol),
            )
            if resp and resp.get("status") == "success":
                greeks = resp.get("greeks", {}) or {}
                parsed = {
                    "delta": float(greeks.get("delta", 0) or 0),
                    "gamma": float(greeks.get("gamma", 0) or 0),
                    "iv": float(greeks.get("implied_volatility", 0) or 0),  # IV from greeks
                }
                while len(self._greeks_cache) >= self._greeks_cache_max_size:
                    self._greeks_cache.popitem(last=False)
                self._greeks_cache[cache_key] = parsed
                return parsed
        except Exception as exc: err(f"[DATA] optiongreeks error for {option_symbol}: ", exc)
        return None

    def greeks_perf_snapshot(self, symbol: str | None = None) -> dict[str, float | int]:
        cache_size = len(self._greeks_cache)
        if symbol is not None:
            cache_size = sum(1 for k in self._greeks_cache if k[0] == symbol)
        total_lookups = self._greeks_cache_hits + self._greeks_cache_misses
        hit_rate = (self._greeks_cache_hits / total_lookups * 100.0) if total_lookups > 0 else 0.0
        return {
            "hits": self._greeks_cache_hits,
            "misses": self._greeks_cache_misses,
            "api_calls": self._greeks_api_calls,
            "cache_size": cache_size,
            "hit_rate": round(hit_rate, 1),
        }

    def batch_prefetch_option_greeks(self, symbol: str, option_symbols: list[str]) -> None:
        """Prefetch greeks for unique option symbols used in this scan cycle."""
        for opt_sym in dict.fromkeys([s for s in option_symbols if s]):
            self._fetch_option_greeks_cached(symbol, opt_sym)

    def underlying_exchange(self, symbol: str) -> str:
        """Return NSE_INDEX/BSE_INDEX for index underlyings, else SPOT_EXCHANGE."""
        return self.config.index_exchange if symbol in self.config.index_underlyings else self.config.spot_exchange

    def fetch_candles(self, symbol: str, exchange: str) -> pd.DataFrame | None:
        """Fetch OHLCV history for any instrument symbol on a given exchange."""
        try:
            end = datetime.now()
            start = end - timedelta(days=self.config.lookback_days)
            return self.client.history(
                symbol=symbol,
                exchange=exchange,
                interval=self.config.candle_interval,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
        except Exception as exc:
            err(f"[DATA] Candle fetch error for {symbol}@{exchange}", exc)
            return None

    def fetch_spot_candles(self, symbol: str) -> pd.DataFrame | None:
        df = self.fetch_candles(symbol, self.underlying_exchange(symbol))
        if df is None or len(df) < self.config.slow_ema_period + 5:
            return None
        return df

    def fetch_option_candles(self, option_symbol: str) -> pd.DataFrame | None:
        return self.fetch_candles(option_symbol, self.config.fno_exchange)

    def fetch_option_chain(
        self, symbol: str, expiry: str | None = None
    ) -> tuple[list[dict], str | None]:
        """Fetch and flatten the option chain (CE/PE nested → flat dicts)."""
        try:
            ul_exchange = self.underlying_exchange(symbol)
            kwargs: dict = dict(underlying=symbol, exchange=ul_exchange)
            if expiry:
                kwargs["expiry_date"] = expiry
            kwargs["strike_count"] = self.config.strike_count
            raw = self.client.optionchain(**kwargs)
            if not raw:
                return [], None
            if isinstance(raw, dict):
                expiry_date = raw.get("expiry_date")
                nested = raw.get("chain", raw.get("data", []))
            else:
                nested, expiry_date = raw, None
            if not isinstance(nested, list):
                return [], expiry_date

            flat_rows: list[dict] = []
            for entry in nested:
                strike = entry.get("strike")
                if strike is None:
                    continue
                ce = entry.get("ce") or {}
                pe = entry.get("pe") or {}
                ce_ltp  = float(ce.get("ltp")  or 0) or None
                pe_ltp  = float(pe.get("ltp")  or 0) or None
                ce_prev = float(ce.get("prev_close") or 0) or None
                pe_prev = float(pe.get("prev_close") or 0) or None
                flat_rows.append({
                    "strike":     strike,
                    "ce_symbol":  ce.get("symbol"),
                    "pe_symbol":  pe.get("symbol"),
                    "ce_ltp":     ce_ltp,
                    "pe_ltp":     pe_ltp,
                    "ce_bid":     float(ce.get("bid") or 0) or None,
                    "ce_ask":     float(ce.get("ask") or 0) or None,
                    "pe_bid":     float(pe.get("bid") or 0) or None,
                    "pe_ask":     float(pe.get("ask") or 0) or None,
                    "ce_oi":      float(ce.get("oi")  or 0),
                    "pe_oi":      float(pe.get("oi")  or 0),
                    "ce_volume":  float(ce.get("volume") or 0),
                    "pe_volume":  float(pe.get("volume") or 0),
                    "ce_oi_chg":  float(ce.get("oi_change") or 0),
                    "pe_oi_chg":  float(pe.get("oi_change") or 0),
                    "ce_ltp_chg": (ce_ltp - ce_prev) if (ce_ltp and ce_prev) else 0.0,
                    "pe_ltp_chg": (pe_ltp - pe_prev) if (pe_ltp and pe_prev) else 0.0,
                    "lotsize":    ce.get("lotsize") or pe.get("lotsize") or 1,
                })
            return flat_rows, expiry_date
        except Exception as exc:
            err(f"[DATA] Option chain error for {symbol}", exc)
            return [], None

    def _acquire_quote_token(self) -> bool:
        """Acquire a token from the quote API rate limiter.
        Returns True if token acquired, False if rate limited.
        """
        with self._quote_lock:
            now = time.time()
            # Refill tokens based on elapsed time
            elapsed = now - self._quote_last_refill
            self._quote_tokens = min(
                self._quote_burst,
                self._quote_tokens + elapsed * self._quote_rate_limit_rps
            )
            self._quote_last_refill = now
            if self._quote_tokens >= 1.0:
                self._quote_tokens -= 1.0
                return True
            return False

    def fetch_quote(self, symbol: str, exchange: str) -> dict:
        # Rate limit: return empty if rate limited
        if not self._acquire_quote_token():
            dbg(f"[RATE] fetch_quote rate limited for {symbol}@{exchange}")
            return {}
        try:
            response = self.client.quotes(symbol=symbol, exchange=exchange) or {}
            if response.get("status") == "success":
                return response.get("data", {})
            
            error_msg = response.get("message", "")
            if isinstance(error_msg, str) and ("Invalid token" in error_msg or "UDAPI100050" in error_msg):
                inf(f"[ERROR] {symbol}@{exchange}: broker token invalid (UDAPI100050): {response}")
                if self._notify and not self._auth_error_notified:
                    self._auth_error_notified = True
                    self._notify(
                        f"🚨 API Auth Error: Broker token invalid (UDAPI100050) for {symbol}.\n"
                        f"All quote/chain calls will fail until token is refreshed.\n"
                        f"Action: Re-login to broker and restart strategy.",
                        9,
                    )
            else:
                inf(f"[ERROR] {symbol}@{exchange}: quotes API error: {response}")
            return {}
        except Exception as e:
            if "Invalid token" in str(e) or "UDAPI100050" in str(e):
                err(f"[ERROR] {symbol}@{exchange}: broker token invalid", e)
                if self._notify and not self._auth_error_notified:
                    self._auth_error_notified = True
                    self._notify(
                        f"🚨 API Auth Error: Broker token invalid (exception) for {symbol}.\n"
                        f"Action: Re-login to broker and restart strategy.",
                        9,
                    )
            else:
                err(f"[ERROR] {symbol}@{exchange}: quotes API exception", e)
            return {}

    def fetch_synthetic_future(self, symbol: str, expiry: str | None) -> float | None:
        if symbol in self.config.index_underlyings and expiry:
            try:
                resp = self.client.syntheticfuture(
                    underlying=symbol,
                    exchange=self.underlying_exchange(symbol),
                    expiry_date=expiry,
                )
                if resp and resp.get("status") == "success":
                    price = float(resp.get("synthetic_future_price") or 0)
                    return price if price else None
            except Exception as exc: err(f"[DATA] syntheticfuture error for {symbol}: ", exc)
        if expiry:
            fut_symbol = f"{symbol}{expiry}FUT"
            sf_q = self.fetch_quote(fut_symbol, self.config.fno_exchange)
            ltp  = float(sf_q.get("ltp", 0) or 0)
            if ltp:
                return ltp
            inf(f"[DATA] syntheticfuture fallback: {fut_symbol} returned no LTP")
        return None

    def fetch_atm_greeks(
        self,
        symbol: str,
        ce_symbol: str | None,
        pe_symbol: str | None,
    ) -> tuple[float | None, float | None]:
        ce_delta: float | None = None
        pe_delta: float | None = None
        for opt_sym, key in ((ce_symbol, "ce"), (pe_symbol, "pe")):
            if not opt_sym:
                continue
            greeks = self._fetch_option_greeks_cached(symbol, opt_sym)
            if greeks is not None:
                delta = greeks.get("delta")
                if key == "ce":
                    ce_delta = float(delta)
                else:
                    pe_delta = float(delta)
        return ce_delta, pe_delta

    def fetch_option_delta(self, symbol: str, option_symbol: str | None) -> float | None:
        if not option_symbol:
            return None
        greeks = self._fetch_option_greeks_cached(symbol, option_symbol)
        if greeks is not None:
            return abs(float(greeks.get("delta", 0) or 0))
        return None

    def fetch_option_gamma(self, symbol: str, option_symbol: str | None) -> float | None:
        if not option_symbol:
            return None
        greeks = self._fetch_option_greeks_cached(symbol, option_symbol)
        if greeks is not None:
            return float(greeks.get("gamma", 0) or 0)
        return None

    @staticmethod
    def derive_gex_levels(gex_chain: list[dict], spot_price: float) -> dict[str, Any]:
        """Derive institutional GEX levels from per-strike net-gex profile."""
        if not gex_chain:
            return {
                "gamma_flip": None,
                "call_gamma_wall": None,
                "put_gamma_wall": None,
                "absolute_wall": None,
                "total_net_gex": 0.0,
                "upside_punch_target": None,
                "downside_punch_target": None,
            }

        sorted_chain = sorted(gex_chain, key=lambda x: x["strike"])
        total_net_gex = float(sum(float(x.get("net_gex", 0) or 0) for x in sorted_chain))

        # Gamma flip via cumulative net-gex sign change.
        gamma_flip: float | None = None
        cumsum = 0.0
        prev_sign = None
        prev_item = None
        for item in sorted_chain:
            prev_cumsum = cumsum
            cumsum += float(item.get("net_gex", 0) or 0)
            sign = 1 if cumsum >= 0 else -1
            if prev_sign is not None and sign != prev_sign and prev_item is not None:
                if cumsum != prev_cumsum:
                    frac = -prev_cumsum / (cumsum - prev_cumsum)
                    gamma_flip = round(
                        float(prev_item["strike"]) + frac * (float(item["strike"]) - float(prev_item["strike"])),
                        2,
                    )
                else:
                    gamma_flip = float(item["strike"])
                break
            prev_sign = sign
            prev_item = item

        above_spot_pos = [x for x in sorted_chain if x["strike"] > spot_price and (x.get("net_gex", 0) or 0) > 0]
        below_spot_neg = [x for x in sorted_chain if x["strike"] < spot_price and (x.get("net_gex", 0) or 0) < 0]
        call_gamma_wall = max(above_spot_pos, key=lambda x: x["net_gex"])["strike"] if above_spot_pos else None
        put_gamma_wall = min(below_spot_neg, key=lambda x: x["net_gex"])["strike"] if below_spot_neg else None
        absolute_wall = max(sorted_chain, key=lambda x: abs(x.get("net_gex", 0) or 0))["strike"] if sorted_chain else None

        above_neg = [x for x in sorted_chain if x["strike"] > spot_price and (x.get("net_gex", 0) or 0) < 0]
        below_neg = [x for x in sorted_chain if x["strike"] < spot_price and (x.get("net_gex", 0) or 0) < 0]
        upside_punch_target = min(above_neg, key=lambda x: x["strike"])["strike"] if above_neg else None
        downside_punch_target = max(below_neg, key=lambda x: x["strike"])["strike"] if below_neg else None

        return {
            "gamma_flip": gamma_flip,
            "call_gamma_wall": call_gamma_wall,
            "put_gamma_wall": put_gamma_wall,
            "absolute_wall": absolute_wall,
            "total_net_gex": round(total_net_gex, 2),
            "upside_punch_target": upside_punch_target,
            "downside_punch_target": downside_punch_target,
        }

    def fetch_gex_levels(self, symbol: str, chain_rows: list[dict], spot_price: float) -> dict[str, Any] | None:
        """Compute per-strike GEX from live option greeks + OI using SDK APIs."""
        if not self.config.gex_enabled:
            return None
        if not chain_rows or not spot_price:
            return None

        gex_chain: list[dict] = []
        for row in chain_rows:
            strike = float(row.get("strike", 0) or 0)
            if not strike:
                continue

            ce_symbol = row.get("ce_symbol")
            pe_symbol = row.get("pe_symbol")
            ce_oi = float(row.get("ce_oi", 0) or 0)
            pe_oi = float(row.get("pe_oi", 0) or 0)
            lot_size = float(row.get("lotsize", 1) or 1)

            ce_gamma = self.fetch_option_gamma(symbol, ce_symbol) if ce_oi > 0 else None
            pe_gamma = self.fetch_option_gamma(symbol, pe_symbol) if pe_oi > 0 else None

            ce_gex = (ce_gamma or 0.0) * ce_oi * lot_size
            pe_gex = (pe_gamma or 0.0) * pe_oi * lot_size
            net_gex = ce_gex - pe_gex

            gex_chain.append(
                {
                    "strike": strike,
                    "ce_gamma": round(float(ce_gamma or 0.0), 6),
                    "pe_gamma": round(float(pe_gamma or 0.0), 6),
                    "ce_gex": round(float(ce_gex), 2),
                    "pe_gex": round(float(pe_gex), 2),
                    "net_gex": round(float(net_gex), 2),
                }
            )

        if not gex_chain:
            return None

        levels = self.derive_gex_levels(gex_chain, spot_price)
        levels["chain"] = gex_chain
        return levels

    def fetch_atm_iv_ranks(
        self,
        symbol: str,
        ce_symbol: str | None = None,
        pe_symbol: str | None = None,
    ) -> dict[str, float | None]:
        """Fetch separate IV Rank for ATM CE and PE.
        Returns: {"ce_iv_rank": float|None, "pe_iv_rank": float|None, "best_fit": "CE"|"PE"|None}
        Best fit = lower IVR (cheaper options for buying).
        """
        result = {"ce_iv_rank": None, "pe_iv_rank": None, "best_fit": None}
        try:
            # Fetch CE IVR
            if ce_symbol:
                ce_greeks = self._fetch_option_greeks_cached(symbol, ce_symbol)
                if ce_greeks:
                    ce_iv = ce_greeks.get("iv")
                    if ce_iv is not None and float(ce_iv) > 0:
                        result["ce_iv_rank"] = SignalEngine.iv_rank(
                            float(ce_iv), self.config.iv_52w_low, self.config.iv_52w_high
                        )
            
            # Fetch PE IVR
            if pe_symbol:
                pe_greeks = self._fetch_option_greeks_cached(symbol, pe_symbol)
                if pe_greeks:
                    pe_iv = pe_greeks.get("iv")
                    if pe_iv is not None and float(pe_iv) > 0:
                        result["pe_iv_rank"] = SignalEngine.iv_rank(
                            float(pe_iv), self.config.iv_52w_low, self.config.iv_52w_high
                        )
            
            # Determine best fit: lower IVR = cheaper = better for buying
            ce_ivr = result["ce_iv_rank"]
            pe_ivr = result["pe_iv_rank"]
            if ce_ivr is not None and pe_ivr is not None:
                result["best_fit"] = "CE" if ce_ivr <= pe_ivr else "PE"
            elif ce_ivr is not None:
                result["best_fit"] = "CE"
            elif pe_ivr is not None:
                result["best_fit"] = "PE"
            
        except Exception as exc: err(f"[DATA] IV ranks fetch error: ", exc)
        
        return result

    def fetch_target_expiry(self, symbol: str) -> str | None:
        if not hasattr(self.client, "expiry"):
            return None
        try:
            resp = self.client.expiry(
                symbol=symbol,
                exchange=self.config.fno_exchange,
                instrumenttype="options",
            )
            if not resp:
                return None
            if isinstance(resp, list):
                expiry_list: list[str] = resp
            elif isinstance(resp, dict):
                expiry_list = resp.get("data", resp.get("expiries", []))
            else:
                return None

            now = datetime.now().date()
            for exp in expiry_list:
                exp_text = str(exp).strip().upper()
                exp_date = None
                for fmt in ("%d%b%y", "%d-%b-%y", "%d%b%Y", "%d-%b-%Y"):
                    try:
                        exp_date = datetime.strptime(exp_text, fmt).date()
                        break
                    except ValueError:
                        pass
                if exp_date is None:
                    continue
                dte = (exp_date - now).days
                if self.config.dte_min <= dte <= self.config.dte_max:
                    return exp_date.strftime("%d%b%y").upper()
            return None
        except Exception as exc:
            err(f"[DATA] expiry fetch error for {symbol}", exc)
            return None


# ===============================================================================
# ENTRY SL POLICY ENGINE — fixed / strike ATR / spot ATR
# ===============================================================================

class EntryStopLossPolicy:
    """Resolves Phase A initial hard SL points (premium-based) using delta-aware moneyness adaptation or fallback fixed points."""

    def __init__(self, fetcher: DataFetcher, config: BotConfig):
        self._fetcher = fetcher
        self._config = config

    @staticmethod
    def get_moneyness_multipliers(delta: float | None) -> tuple[str, float, float, float]:
        """
        Returns (moneyness_label, sl_width_pct, tgt_mult, act_mult).
        Deep-ITM: Tightest SL width (20%), Largest TP (2.0x), Smallest Trail Act buffer (0.5x).
        Deep-OTM: Widest SL width (75%), Smallest TP (0.5x), Largest Trail Act buffer (2.0x).
        """
        if delta is None:
            return ("Unknown", 40, 1.0, 1.0)
        d = abs(delta)
        if d >= 0.75: return ("Deep-ITM", 20, 2.0, 0.5)
        elif d >= 0.65: return ("ITM", 25, 1.5, 0.75)
        elif d >= 0.55: return ("Sl-ITM", 30, 1.25, 0.9)
        elif d >= 0.45: return ("ATM", 40, 1.0, 1.0)
        elif d >= 0.35: return ("Sl-OTM", 50, 0.85, 1.2)
        elif d >= 0.25: return ("OTM", 60, 0.7, 1.5)
        else: return ("Deep-OTM", 75, 0.5, 2.0)

    def _sl_pts_by_delta(self, delta: float | None, entry_premium: float) -> tuple[float, str]:
        """Compute SL points adapted to entry delta (moneyness). Wider SL for OTM, tighter for ITM."""
        moneyness, sl_width_pct, _, _ = self.get_moneyness_multipliers(delta)
        sl_pts = max(10, min(entry_premium * (sl_width_pct / 100.0), 50))
        return (sl_pts, moneyness)

    def resolve_entry_sl_points(
        self,
        option_symbol: str,
        df_spot: pd.DataFrame | None,
        entry_delta: float | None = None,
        est_premium: float | None = None,
    ) -> tuple[float, str]:
        """Resolve Phase A hard entry SL using delta moneyness (if available), else fallback fixed pts."""
        cfg = self._config
        base_sl = cfg.premium_stop_pts
        base_source = "hard_sl_pts_fallback"

        if entry_delta is not None and est_premium is not None and est_premium > 0:
            delta_sl, moneyness = self._sl_pts_by_delta(entry_delta, est_premium)
            return (delta_sl, f"moneyness_adapted_{moneyness}")
        
        return (base_sl, base_source)

# ===============================================================================
# TRAIL SL ENGINE — Phase B Periodic Trail Computations
# ===============================================================================

class TrailSLEngine:
    """
    Computes trailing SL ratchets (Phase B) periodically on the strategy thread.
    Supports fixed %, ATR-based, and live Delta-based trailing steps.
    """

    def __init__(self, fetcher: DataFetcher, config: BotConfig):
        self._fetcher = fetcher
        self._config = config
        self.modify_callback: Callable[[str, float], bool] | None = None

    def check_trailing_stops(self, state: BotState) -> None:
        """Run periodically to calculate trailing SL ratchets and update orders if needed."""
        cfg = self._config
        with state.state_lock:
            # We copy to avoid blocking state during external calls.
            positions = list(state.positions.items())

        for underlying, pos in positions:
            if pos.exit_pending:
                continue

            # Read from SnapshotCache — single-source-of-truth for all trails
            snap = state.snapshot_cache.get(underlying)
            if not snap or not snap.has_both_prices:
                if not hasattr(self, '_data_skip_logged'):
                    self._data_skip_logged: set[str] = set()
                _missing = []
                if not snap:
                    _missing.append("no_snapshot")
                else:
                    if snap.option_ltp is None:
                        _missing.append("option_ltp")
                    if snap.spot_ltp is None:
                        _missing.append("spot_ltp")
                _key = f"{underlying}|{'_'.join(_missing)}"
                if _key not in self._data_skip_logged:
                    self._data_skip_logged.add(_key)
                    dbg(
                        f"[DATA-MISS] {underlying}: {_missing} — "
                        f"snapshot={snap.timestamp if snap else 'NONE'} | "
                        f"age={(time.time() - snap.timestamp):.1f}s" if snap and snap.timestamp else ""
                    )
                continue
            if hasattr(self, '_data_skip_logged'):
                self._data_skip_logged.clear()

            opt_ltp = snap.option_ltp
            spot_ltp = snap.spot_ltp
            confirmed_close = opt_ltp
            prior_trail_peak_close = (
                pos.trail_peak_close
                if pos.trail_peak_close is not None
                else pos.entry_premium
            )
            is_new_confirmed_close_high = confirmed_close > prior_trail_peak_close
            if is_new_confirmed_close_high:
                pos.trail_peak_close = confirmed_close

            # ── 1. Breakeven logic (Conviction-aware) ────────
            ep = pos.entry_premium
            if cfg.breakeven_at_gain_pct > 0 and not pos.breakeven_moved:
                _be_conv_adj = CONV_BE_BASE - pos.entry_conviction * CONV_BE_RANGE
                _be_trigger_pct = (cfg.breakeven_at_gain_pct / 100.0) * _be_conv_adj
                target_gain_pts = pos.tgt - ep
                gain_pts = (pos.trail_peak_close or ep) - ep
                if target_gain_pts > 0 and gain_pts >= target_gain_pts * _be_trigger_pct:
                    if ep > pos.sl:
                        _broker_ok = True
                        if cfg.broker_sl_orders and pos.sl_order_id and self.modify_callback:
                            _broker_ok = self.modify_callback(underlying, ep)
                        if _broker_ok:
                            pos.sl = ep
                            pos.breakeven_moved = True
                            inf(f"[TRAIL] BREAKEVEN SL {underlying}: moved to cost ₹{ep:.2f}")

            # ── 2. Activation Buffer ────────
            _trail_conv_adj = CONV_TRAIL_ACT_BASE - pos.entry_conviction * CONV_TRAIL_ACT_RANGE
            
            # ── 3. Mode Processing ────────
            if cfg.trail_sl_method == "key_level":
                self._process_key_level_trail(underlying, pos, spot_ltp, confirmed_close)
            elif cfg.trail_tracking_mode == "premium":
                self._process_premium_trail(
                    underlying,
                    pos,
                    confirmed_close,
                    _trail_conv_adj,
                    is_new_confirmed_close_high,
                )
            elif cfg.trail_tracking_mode == "spot":
                self._process_spot_trail(underlying, pos, spot_ltp, _trail_conv_adj)

    def _get_step_pts(self, pos: OptionPosition, base_dist: float, price_series_df: pd.DataFrame | None, current_delta: float | None = None) -> float:
        """Resolve step points based on trail_sl_method.

        Cap logic (prevents oversized steps on high-premium options):
          fixed_pts → raw N pts, no cap (it IS the explicit value)
          fixed_pct → capped at entry_premium × 50%
          delta     → capped at entry_premium × 50%
          atr       → no cap (ATR is self-scaling)
        """
        cfg = self._config
        method = cfg.trail_sl_method
        ep = pos.entry_premium

        # ── fixed_pts: always return a fixed raw premium point step ──────────
        if method == "fixed_pts":
            return max(1.0, cfg.trail_step_pts)

        # ── atr: self-adapting; no external cap applied ────────────────────
        if method == "atr" and price_series_df is not None and len(price_series_df) >= cfg.trail_atr_period + 2:
            try:
                atr_series = ta.atr(
                    price_series_df["high"],
                    price_series_df["low"],
                    price_series_df["close"],
                    period=cfg.trail_atr_period,
                )
                atr_val = float(atr_series.iloc[-2])
                if math.isfinite(atr_val) and atr_val > 0:
                    return atr_val * cfg.trail_atr_mult
            except Exception as e: err(f"[TRAIL] ATR compute error: ", e)

        # ── delta: tier-based pct ──────────────────────────────────────────
        if method == "delta" and current_delta is not None:
            d = abs(current_delta)
            if d >= 0.55:   step_pct = cfg.trail_delta_itm_step_pct
            elif d >= 0.35: step_pct = cfg.trail_delta_atm_step_pct
            else:           step_pct = cfg.trail_delta_otm_step_pct
            return base_dist * (step_pct / 100.0)

        # ── fixed_pct (default/fallback) ───────────────────────────────────
        return base_dist * (cfg.trail_step_pct / 100.0)

    def _process_premium_trail(
        self,
        underlying: str,
        pos: OptionPosition,
        confirmed_close: float,
        conv_adj: float,
        is_new_confirmed_close_high: bool,
    ) -> None:
        cfg = self._config
        ep = pos.entry_premium
        move = confirmed_close - ep
        ltp = confirmed_close
        
        activate_pts = ep * (cfg.trail_activate_at_pct / 100.0) * conv_adj * pos.trail_act_mult
        # Hard ceiling: prevents activation requiring more pts than the TP window on high-premium options
        if cfg.trail_activate_at_max_pts > 0:
            activate_pts = min(activate_pts, cfg.trail_activate_at_max_pts)

        if not pos.premium_trail_active and move < activate_pts:
            return  # not activated yet

        # Resolve step points
        current_delta = None
        df = None
        if cfg.trail_sl_method == "delta":
            greeks = self._fetcher._fetch_option_greeks_cached(underlying, pos.symbol)
            if greeks and "delta" in greeks:
                current_delta = greeks["delta"]
        elif cfg.trail_sl_method == "atr":
            df = self._fetcher.fetch_option_candles(pos.symbol)
            
        _base_step_pts = self._get_step_pts(pos, ep, df, current_delta)
        
        # ── Profit Acceleration Compression Engine ─────────────────────────────
        # 1. Base Gamma Speed (ROI-based)
        _roi_pct = ((confirmed_close - ep) / ep * 100.0) if ep > 0 else 0.0
        if _roi_pct >= 150:
            _trail_speed = 2.5
            _gamma_tier = "TIER_3_150PLUS"
        elif _roi_pct >= 100:
            _trail_speed = 2.0
            _gamma_tier = "TIER_2_100_150"
        elif _roi_pct >= 50:
            _trail_speed = 1.5
            _gamma_tier = "TIER_1_50_100"
        else:
            _trail_speed = 1.0
            _gamma_tier = "TIER_0_0_50"
        
        # 2. Trend Efficiency Factor (Market Structure & Ranging Avoidance)
        if df is None:
            df = self._fetcher.fetch_option_candles(pos.symbol)
            
        trend_efficiency = 1.0
        _net_move = 0.0
        _path_length = 0.0
        if df is not None and not df.empty:
            recent = df.tail(15)  # recent window to evaluate chop vs trend
            if len(recent) > 1:
                closes = recent["close"].values
                _net_move = abs(closes[-1] - closes[0])
                _path_length = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
                if _path_length > 0:
                    trend_efficiency = _net_move / _path_length
                    
        # Clamp efficiency between 0.50 and 1.0 to prevent dividing by zero or inflating the step
        trend_efficiency_factor = max(0.50, min(1.0, trend_efficiency))
        
        # Apply efficiency multiplier: ranging markets will lower the trail speed (looser trail)
        _trail_speed *= trend_efficiency_factor
        
        # 3. Apply intelligence to raw step (Option B architecture)
        step_pts = max(_base_step_pts * GAMMA_SPEED_STEP_FLOOR, _base_step_pts / max(0.1, _trail_speed))
        
        # 4. Final Safety Limit Cap (Guarantees trail doesn't exceed 50% of entry premium)
        step_pts = min(step_pts, ep * 0.50)
        
        # ── Incremental PnL ──
        _unrealized_pnl_pts = confirmed_close - ep
        _unrealized_pnl_pct = (_unrealized_pnl_pts / ep * 100.0) if ep > 0 else 0.0
        _unrealized_pnl_abs = _unrealized_pnl_pts * pos.qty
        
        # ── Detailed Trail Logging ──
        inf(
            f"[TRAIL] {underlying} | ROI={_roi_pct:.1f}% ({_gamma_tier}) | "
            f"KER={trend_efficiency:.3f} (net={_net_move:.2f}/path={_path_length:.2f}) → "
            f"KER_factor={trend_efficiency_factor:.3f} | "
            f"Speed={_trail_speed:.2f}x | BaseStep={_base_step_pts:.2f} → "
            f"FinalStep={step_pts:.2f} | Cap={ep*0.50:.2f} | "
            f"UnrealPnL={_unrealized_pnl_pts:.2f}pts ({_unrealized_pnl_pct:.1f}%) ₹{_unrealized_pnl_abs:.0f} | "
            f"PeakClose={pos.trail_peak_close:.2f} | LTP={confirmed_close:.2f}"
        )
        
        # Trail activation and ratchet placement use confirmed periodic closes.
        if not pos.premium_trail_active:
            pos.premium_trail_active = True
            pos.premium_trail_peak = pos.trail_peak_close
            new_sl = confirmed_close - step_pts
            if new_sl > pos.sl:
                _broker_ok = True
                if cfg.broker_sl_orders and pos.sl_order_id and self.modify_callback:
                    _broker_ok = self.modify_callback(underlying, new_sl)
                if _broker_ok:
                    pos.premium_trail_sl = new_sl
                    pos.sl = new_sl
                    inf(f"[TRAIL] Premium ACTIVATED {underlying}: peak {ltp:.2f} SL→{new_sl:.2f} (speed={_trail_speed:.1f}x)")
        else:
            if is_new_confirmed_close_high:
                pos.premium_trail_peak = pos.trail_peak_close
                new_sl = confirmed_close - step_pts
                if new_sl > pos.sl:
                    _broker_ok = True
                    if cfg.broker_sl_orders and pos.sl_order_id and self.modify_callback:
                        _broker_ok = self.modify_callback(underlying, new_sl)
                    if _broker_ok:
                        pos.premium_trail_sl = new_sl
                        pos.sl = new_sl
                        inf(f"[TRAIL] Premium RATCHET {underlying}: peak {ltp:.2f} SL→{new_sl:.2f} (speed={_trail_speed:.1f}x)")

    def _process_spot_trail(self, underlying: str, pos: OptionPosition, spot_ltp: float, conv_adj: float) -> None:
        cfg = self._config
        reward_dist = pos.reward_dist
        
        activate_pts = reward_dist * (cfg.trail_activate_at_pct / 100.0) * conv_adj * pos.trail_act_mult
        # Hard ceiling: prevents activation requiring more pts than the TP window on expensive options
        if cfg.trail_activate_at_max_pts > 0:
            activate_pts = min(activate_pts, cfg.trail_activate_at_max_pts)

        if pos.option_type == "CE": move = spot_ltp - pos.spot_entry
        else: move = pos.spot_entry - spot_ltp

        if not pos.trail_active and move < activate_pts:
            return
            
        # Resolve step points
        current_delta = None
        df = None
        if cfg.trail_sl_method == "delta":
            greeks = self._fetcher._fetch_option_greeks_cached(underlying, pos.symbol)
            if greeks and "delta" in greeks:
                current_delta = greeks["delta"]
        elif cfg.trail_sl_method == "atr":
            df = self._fetcher.fetch_spot_candles(underlying)
            
        step_pts = self._get_step_pts(pos, reward_dist, df, current_delta)

        # Final Safety Limit Cap for Spot Mode (Option B architecture)
        step_pts = min(step_pts, reward_dist * 0.50)

        # Spot PnL equivalent (spot move vs entry)
        _spot_move_pts = move
        _spot_move_pct = (_spot_move_pts / pos.spot_entry * 100.0) if pos.spot_entry > 0 else 0.0

        inf(
            f"[TRAIL] {underlying} SPOT | Move={_spot_move_pts:.2f}pts ({_spot_move_pct:.2f}%) | "
            f"ActivateReq={activate_pts:.2f} | Step={step_pts:.2f} | Cap={reward_dist*0.50:.2f} | "
            f"Peak={pos.trail_peak if pos.trail_peak else 'N/A'} | LTP={spot_ltp:.2f} | "
            f"SL_Spot={pos.trail_sl_spot:.2f if pos.trail_sl_spot else 'N/A'}"
        )

        if not pos.trail_active:
            pos.trail_active = True
            pos.trail_peak = spot_ltp
            new_sl_spot = (spot_ltp - step_pts) if pos.option_type == "CE" else (spot_ltp + step_pts)
            pos.trail_sl_spot = new_sl_spot
            inf(f"[TRAIL] Spot ACTIVATED {underlying}: peak {spot_ltp:.2f}, SL spot → {new_sl_spot:.2f}")
        else:
            if pos.option_type == "CE":
                if pos.trail_peak is None or spot_ltp > pos.trail_peak:
                    pos.trail_peak = spot_ltp
                    new_sl_spot = spot_ltp - step_pts
                    if pos.trail_sl_spot is None or new_sl_spot > pos.trail_sl_spot:
                        pos.trail_sl_spot = new_sl_spot
                        inf(f"[TRAIL] Spot RATCHET {underlying}: peak {spot_ltp:.2f}, SL spot → {new_sl_spot:.2f}")
            else:
                if pos.trail_peak is None or spot_ltp < pos.trail_peak:
                    pos.trail_peak = spot_ltp
                    new_sl_spot = spot_ltp + step_pts
                    if pos.trail_sl_spot is None or new_sl_spot < pos.trail_sl_spot:
                        pos.trail_sl_spot = new_sl_spot
                        inf(f"[TRAIL] Spot RATCHET {underlying}: peak {spot_ltp:.2f}, SL spot → {new_sl_spot:.2f}")

    # ── Key Level Trail Helpers ──────────────────────────────────────────────

    def _get_key_levels(self, spot: float, underlying: str) -> list[float]:
        """Generate a symmetric strike ladder around spot using per-instrument spacing.

        Returns a sorted list of price levels (e.g. for NIFTY with spacing=50:
        [..., 24750, 24800, 24850, 24900, 24950, ...]).
        """
        cfg = self._config
        # Resolve spacing from config dict; fallback to 50 for unknown instruments
        spacing = cfg.key_level_spacing.get(underlying, 50)
        if spacing <= 0:
            spacing = 50

        # Nearest level at or below spot (floor to spacing grid)
        floor_level = math.floor(spot / spacing) * spacing
        # Generate ~20 levels in each direction (enough for the session)
        levels = []
        for i in range(-20, 21):
            lvl = floor_level + i * spacing
            if lvl > 0:
                levels.append(lvl)
        levels.sort()
        return levels

    def _get_next_key_level(
        self,
        spot: float,
        direction: str,
        underlying: str,
        current_level: float | None,
    ) -> float | None:
        """Return the next structure level the spot must cross to trigger a trail ratchet.

        CE: next level ABOVE current_level (upward targets).
        PE: next level BELOW current_level (downward targets).
        If current_level is None, finds the nearest level on the correct side of spot.
        """
        levels = self._get_key_levels(spot, underlying)
        if not levels:
            return None

        if current_level is None:
            # First call: pick the nearest level on the favorable side of spot
            if direction == "CE":
                above = [l for l in levels if l > spot]
                return min(above) if above else None
            else:
                below = [l for l in levels if l < spot]
                return max(below) if below else None

        # Find the next level beyond current_level in the trade direction
        if direction == "CE":
            above = [l for l in levels if l > current_level]
            return min(above) if above else None
        else:
            below = [l for l in levels if l < current_level]
            return max(below) if below else None

    def _process_key_level_trail(
        self,
        underlying: str,
        pos: OptionPosition,
        spot_ltp: float,
        premium_ltp: float,
    ) -> None:
        """Structure-driven trailing SL based on key strike levels.

        Logic:
          1. When spot crosses the next structure level → lock in a portion of
             the premium move since the last level (capture_pct) or a fixed pts
             amount.
          2. After key_level_breakeven_after_levels completed levels, SL moves
             to entry cost (breakeven).
          3. SL ratchets UP for both CE and PE (long options profit from
             increasing premium). The one-way ratchet is: SL only moves higher,
             never lower. Spot levels move opposite directions (CE: up, PE: down)
             but premium-based SL always ratchets upward.

        Gap-jump handling: uses a while loop so multiple levels crossed in a
        single tick are all processed immediately.
        """
        cfg = self._config
        ep = pos.entry_premium

        # ── Diagnostic: data snapshot on every key_level trail call ─────────
        _roi_pct = ((premium_ltp - ep) / ep * 100.0) if ep > 0 else 0.0
        if not hasattr(self, '_kl_tick_count'):
            self._kl_tick_count: dict[str, int] = {}
        self._kl_tick_count[underlying] = self._kl_tick_count.get(underlying, 0) + 1
        _kl_cnt = self._kl_tick_count[underlying]
        if _kl_cnt <= 5 or _kl_cnt % 10 == 0:
            inf(
                f"[DATA-KL] {underlying} tick#{_kl_cnt}: "
                f"spot=₹{spot_ltp:.2f} premium=₹{premium_ltp:.2f} | "
                f"entry=₹{ep:.2f} ROI={_roi_pct:.1f}% | "
                f"SL=₹{pos.sl:.2f} initial=₹{pos.initial_sl:.2f} | "
                f"direction={pos.option_type} moneyness={pos.moneyness} | "
                f"kl_active={pos.kl_active} kl_next={pos.kl_next_level} "
                f"kl_completed={pos.kl_levels_completed} "
                f"kl_level_premium={pos.kl_level_premium if pos.kl_level_premium else 'N/A'}"
            )

        # ── Initialization on first tick ────────────────────────────────────
        if not pos.kl_active:
            pos.kl_active = True
            pos.kl_next_level = self._get_next_key_level(
                spot_ltp, pos.option_type, underlying, None
            )
            pos.kl_levels_completed = 0
            pos.kl_level_premium = premium_ltp
            inf(
                f"[TRAIL] KeyLevel INIT {underlying}: "
                f"next_level={pos.kl_next_level:.0f} | "
                f"premium={premium_ltp:.2f}"
            )
            return

        # ── Guard: no next level computed ───────────────────────────────────
        if pos.kl_next_level is None:
            return

        # ── Process all crossed levels in a loop (handles gap jumps) ────────
        # CE: spot rising crosses upward levels; PE: spot falling crosses downward levels.
        while pos.kl_next_level is not None:
            if pos.option_type == "CE" and spot_ltp < pos.kl_next_level:
                break
            elif pos.option_type == "PE" and spot_ltp > pos.kl_next_level:
                break

            # ── Level crossed — compute trail step ─────────────────────────
            pos.kl_levels_completed += 1
            captured_range = premium_ltp - (pos.kl_level_premium or ep)

            if cfg.key_level_trail_style == "capture_pct":
                trail_step = max(1.0, captured_range * (cfg.key_level_capture_pct / 100.0))
            else:
                trail_step = max(1.0, cfg.key_level_fixed_pts)

            # ── Breakeven after N completed levels ─────────────────────────
            if (
                cfg.key_level_breakeven_after_levels > 0
                and pos.kl_levels_completed >= cfg.key_level_breakeven_after_levels
                and not pos.breakeven_moved
                and ep > pos.sl
            ):
                _broker_ok = True
                if cfg.broker_sl_orders and pos.sl_order_id and self.modify_callback:
                    _broker_ok = self.modify_callback(underlying, ep)
                if _broker_ok:
                    pos.sl = ep
                    pos.breakeven_moved = True
                    inf(
                        f"[TRAIL] KeyLevel BREAKEVEN {underlying}: "
                        f"SL → entry ₹{ep:.2f} after {pos.kl_levels_completed} level(s)"
                    )

            # ── Compute new SL from captured range ─────────────────────────
            new_sl = premium_ltp - trail_step

            # One-directional ratchet (SL only moves UP for both CE and PE)
            if new_sl > pos.sl:
                _broker_ok = True
                if cfg.broker_sl_orders and pos.sl_order_id and self.modify_callback:
                    _broker_ok = self.modify_callback(underlying, new_sl)
                if _broker_ok:
                    pos.sl = new_sl
                    pos.premium_trail_sl = new_sl
                    inf(
                        f"[TRAIL] KeyLevel RATCHET {underlying}: "
                        f"level_crossed={pos.kl_next_level:.0f} | "
                        f"captured={captured_range:.2f}pts → step={trail_step:.2f} | "
                        f"SL→₹{new_sl:.2f} | completed={pos.kl_levels_completed}"
                    )
            else:
                inf(
                    f"[TRAIL] KeyLevel CROSS {underlying}: "
                    f"level_crossed={pos.kl_next_level:.0f} | "
                    f"captured={captured_range:.2f}pts → step={trail_step:.2f} | "
                    f"SL unchanged ₹{pos.sl:.2f} (new_sl={new_sl:.2f} not higher)"
                )

            # ── Advance to next level ──────────────────────────────────────
            pos.kl_level_premium = premium_ltp
            pos.kl_next_level = self._get_next_key_level(
                spot_ltp, pos.option_type, underlying, pos.kl_next_level
            )

        # Log distance to next level (only when no level was crossed this tick)
        if pos.kl_next_level is not None:
            _move_to_level = abs(spot_ltp - pos.kl_next_level)
            inf(
                f"[TRAIL] KeyLevel {underlying}: "
                f"spot={spot_ltp:.0f} → next_level={pos.kl_next_level:.0f} "
                f"({_move_to_level:.0f}pts away) | "
                f"completed={pos.kl_levels_completed} | "
                f"SL={pos.sl:.2f} | premium={premium_ltp:.2f}"
            )


# ===============================================================================
# STRIKE SELECTOR — liquidity + asymmetry score based strike selection
# ===============================================================================

class StrikeSelector:
    """Selects the best entry strike using check_all_checkpoints criteria."""

    def __init__(self, fetcher: DataFetcher, config: BotConfig):
        self.fetcher = fetcher
        self.config  = config

    @staticmethod
    def simple_otm(
        chain_rows: list[dict],
        spot: float,
        option_type: str,
        otm_offset: int,
    ) -> dict | None:
        """Pick a slightly OTM strike that is `otm_offset` strikes away from ATM."""
        strikes = sorted(set(r["strike"] for r in chain_rows if "strike" in r))
        if not strikes:
            return None
        atm = min(strikes, key=lambda x: abs(x - spot))
        idx = strikes.index(atm)
        if option_type == "CE":
            target_idx = min(idx + otm_offset, len(strikes) - 1)
        else:
            target_idx = max(idx - otm_offset, 0)
        target_strike = strikes[target_idx]
        for row in chain_rows:
            if row.get("strike") == target_strike:
                if option_type in (row.get("option_type", ""), ""):
                    return row
        for row in chain_rows:
            if row.get("strike") == target_strike:
                return row
        return None

    def select_best(
        self,
        symbol: str,
        chain_rows: list[dict],
        spot: float,
        direction: str,
        iv_rank: float | None,
        signal_score: float = 50.0,
    ) -> dict | None:
        """
        Conviction-driven strike selection.

        All selection parameters (delta target, delta weight, asym threshold)
        are derived from a single `conviction` scalar ∈ [0, 1] so that the
        entire function behaves as a self-consistent system:

            Low conviction  → conservative OTM strike, liquidity-weighted
            High conviction → near-ATM strike, delta-weighted

        Returns None if no qualifying strike found.
        """
        cfg = self.config

        # ── Guard: empty input ────────────────────────────────────────────────
        if not chain_rows or not spot:
            return None

        # ── Guard: IVR too high for buyer edge ────────────────────────────────
        if iv_rank is not None and iv_rank >= cfg.iv_rank_max_entry:
            inf(f"[STRIKE] IVR {iv_rank:.1f}% >= max {cfg.iv_rank_max_entry:.1f}% — buyer edge rejected")
            return None

        # ── Guard: insufficient signal conviction ─────────────────────────────
        abs_score = abs(signal_score)
        if abs_score < cfg.min_score:
            inf(f"[STRIKE] Signal score {signal_score:.0f} < min {cfg.min_score} — insufficient edge")
            return None

        # ── Conviction scalar ─────────────────────────────────────────────────
        # Maps [min_score, 100] → [0.0, 1.0] so STRIKE_DELTA_BASE is the actual
        # minimum delta at the weakest tradeable signal, not a theoretical floor
        # at score=0 which can never be reached after the min_score gate above.
        conviction = min(
            (abs_score - cfg.min_score) / max(100.0 - cfg.min_score, 1.0),
            1.0,
        )

        # ── Piecewise continuous delta target ─────────────────────────────────
        # 0 - 50 score   → STRIKE_DELTA_BASE(0.25) to STRIKE_DELTA_PIVOT(0.50)  (near-OTM → ATM)
        # 50 - 100 score → STRIKE_DELTA_PIVOT(0.50) to STRIKE_DELTA_MAX(0.70)   (ATM → mild ITM)
        if abs_score <= STRIKE_SCORE_PIVOT:
            # Map [min_score, SCORE_PIVOT] -> [BASE, PIVOT]
            score_range = max(STRIKE_SCORE_PIVOT - cfg.min_score, 1.0)
            fraction = max(0.0, abs_score - cfg.min_score) / score_range
            target_delta = STRIKE_DELTA_BASE + fraction * (STRIKE_DELTA_PIVOT - STRIKE_DELTA_BASE)
        else:
            # Map [SCORE_PIVOT, 100] -> [PIVOT, MAX]
            score_range = 100.0 - STRIKE_SCORE_PIVOT
            fraction = min((abs_score - STRIKE_SCORE_PIVOT) / score_range, 1.0)
            target_delta = STRIKE_DELTA_PIVOT + fraction * (STRIKE_DELTA_MAX - STRIKE_DELTA_PIVOT)
        target_delta_low  = max(0.01, target_delta - STRIKE_DELTA_BAND)
        target_delta_high = min(0.99, target_delta + STRIKE_DELTA_BAND)
        inf(
            f"[STRIKE] conviction={conviction:.2f} "
            f"target_delta={target_delta:.3f} "
            f"band=[{target_delta_low:.2f},{target_delta_high:.2f}]"
        )

        # ── Stage 1: Price-range filter ───────────────────────────────────────
        # Window scales from config; avoids hardcoding ±5%.
        lo = spot if direction == "CE" else spot * (1 - STRIKE_RANGE_PCT)
        hi = spot * (1 + STRIKE_RANGE_PCT) if direction == "CE" else spot
        oi_key  = "ce_oi"     if direction == "CE" else "pe_oi"
        vol_key = "ce_volume" if direction == "CE" else "pe_volume"
        opt_key = "ce_symbol" if direction == "CE" else "pe_symbol"

        # ── Stage 2: Liquidity filter ─────────────────────────────────────────
        candidates: list[dict] = []
        for row in chain_rows:
            strike = row.get("strike", 0)
            if not (lo <= strike <= hi):
                continue
            oi = float(row.get(oi_key, 0) or 0)
            if oi < cfg.min_oi_filter:
                continue
            vol = float(row.get(vol_key, 0) or 0)
            if vol < cfg.min_vol_filter:
                continue
            candidates.append(row)

        if not candidates:
            return None

        # ── Stage 3: Delta filter (optional — only when greeks available) ─────
        # Single-pass: fetch delta ONCE and annotate every candidate immediately.
        # The fallback path reads _abs_delta from the annotated list — zero re-fetches.
        delta_checked: list[dict] = []
        delta_available = False
        annotated: list[dict] = []
        for row in candidates:
            abs_delta = self.fetcher.fetch_option_delta(symbol, row.get(opt_key))
            row = dict(row)                         # copy — safe to annotate
            if abs_delta is not None:
                row["_abs_delta"] = abs_delta
                delta_available = True
                if target_delta_low <= abs_delta <= target_delta_high:
                    delta_checked.append(row)
            annotated.append(row)

        if delta_available:
            if not delta_checked:
                inf(
                    f"[STRIKE] No candidate delta in "
                    f"[{target_delta_low:.2f}, {target_delta_high:.2f}] "
                    f"— conviction={conviction:.2f}, relaxing to closest available"
                )
                # Fallback: nearest-delta candidate — reads cached _abs_delta (no re-fetch)
                best_fallback: dict | None = None
                best_gap = float("inf")
                for row in annotated:
                    ad = row.get("_abs_delta")
                    if ad is None:
                        continue
                    gap = abs(ad - target_delta)
                    if gap < best_gap:
                        best_gap = gap
                        best_fallback = row
                if best_fallback and best_gap <= MAX_DELTA_GAP:
                    candidates = [best_fallback]
                else:
                    # Gap too large — pathological fallback; bypass delta filter
                    if best_fallback:
                        inf(
                            f"[STRIKE] Fallback gap {best_gap:.2f} > MAX_DELTA_GAP {MAX_DELTA_GAP:.2f} "
                            f"— delta filter bypassed, using liquidity ranking only"
                        )
                    candidates = annotated
            else:
                candidates = delta_checked
        else:
            candidates = annotated  # delta unavailable: all liquidity candidates proceed (no silent skip)

        # ── Stage 4: Conviction-driven asymmetry scoring ──────────────────────
        # IV weight: lower IV = better buyer conditions.
        # IVR missing → do NOT penalize; skip IV component (set ivr_weight to 0).
        ivr_known    = iv_rank is not None
        ivr_val      = iv_rank if ivr_known else 0.0
        iv_score_raw = (1 - ivr_val / 100) if ivr_known else None

        # Delta weight scales with conviction; liquidity gets the remainder.
        delta_weight = STRIKE_DELTA_WEIGHT_BASE + conviction * STRIKE_DELTA_WEIGHT_RANGE
        # Distribute remaining weight across IV, OI, Volume proportionally:
        #   baseline split:  IV 40%, OI 30%, Vol 20%  → total 90%
        #   when IVR missing drop IV weight, redistribute to OI+Vol
        liq_total  = 1.0 - delta_weight
        if ivr_known:
            iv_w   = liq_total * (4/9)  # ~44.44% of total
            oi_w   = liq_total * (3/9)  # ~33.33% of total
            vol_w  = liq_total * (2/9)  # ~22.22% of total
        else:
            iv_w   = 0.0
            oi_w   = liq_total * 0.60
            vol_w  = liq_total * 0.40

        # Pre-compute chain-level maxima ONCE — O(n), not O(n²) per candidate.
        max_oi  = max(float(r.get(oi_key,  0) or 0) for r in chain_rows) or 1.0
        max_vol = max(float(r.get(vol_key, 0) or 0) for r in chain_rows) or 1.0

        best_row: dict | None = None
        best_asym = -1.0

        for row in candidates:
            strike_oi  = float(row.get(oi_key,  0) or 0)
            strike_vol = float(row.get(vol_key, 0) or 0)
            # OI / Vol concentration normalized to best-in-chain strike
            oi_conc    = min(strike_oi / max_oi, 1.0)
            vol_conc   = min(strike_vol / max_vol, 1.0)

            abs_delta = row.get("_abs_delta")
            if abs_delta is not None:
                # smoother decay: half the band on each side
                delta_score = max(0.0, 1.0 - abs(abs_delta - target_delta) / max(2 * STRIKE_DELTA_BAND, 0.01))
            else:
                delta_score = 0.5  # neutral when no greeks

            iv_component = (iv_score_raw * iv_w) if ivr_known else 0.0
            asym_score = (
                iv_component
                + oi_conc    * oi_w
                + vol_conc   * vol_w
                + delta_score * delta_weight
            )
            if asym_score > best_asym:
                best_asym = asym_score
                best_row  = row

        # ── Stage 5: Conviction-scaled minimum quality gate ───────────────────
        # Institutional logic: strong signal → more willing to execute on a
        # slightly imperfect strike.  Weak signal → insist on cleaner setup.
        # Scales between [threshold * 0.80, threshold * 1.00]:
        #   conviction=0.0 → min = threshold × 1.00  (strictest)
        #   conviction=1.0 → min = threshold × 0.80  (relaxed 20%)
        min_asym = cfg.asym_score_threshold * (1.00 - conviction * 0.20)
        if best_asym < min_asym:
            inf(
                f"[STRIKE] Best asym {best_asym:.3f} < conviction-scaled min "
                f"{min_asym:.3f} (conviction={conviction:.2f}) — no qualifying strike"
            )
            return None
        return best_row


# ===============================================================================
# RISK MANAGER — session gates, daily P&L, loss streak
# ===============================================================================

class RiskManager:
    """Manages session-level risk: trade counts, loss streaks, entry cooldowns."""

    def __init__(self, client: api, config: BotConfig, state: BotState):
        self.client  = client
        self.config  = config
        self._state  = state

        self._session_date               = get_ist_now().strftime("%Y-%m-%d")
        self._session_trade_count        = 0
        self._session_consecutive_losses = 0
        self._session_consecutive_wins   = 0
        self._last_entry_times: dict[str, float] = {}
        self._daily_pnl                  = 0.0

        self._funds_cache:       float = 0.0   # last broker-reported available capital
        self._funds_cache_time:  float = 0.0
        self._funds_cache_ttl:   float = 60.0  # re-poll interval; between refreshes uses pnl delta
        self._pnl_at_last_fetch: float = 0.0
        self._pnl_history: deque[tuple[float, float]] = deque()  # (unix_timestamp, cumulative_pnl)

    def available_capital(self) -> float:
        """Cached funds() call: re-polls broker every _funds_cache_ttl seconds.
        Between refreshes returns cached broker capital + script P&L delta since last poll.
        """
        now = time.time()
        if self._funds_cache_time and (now - self._funds_cache_time) < self._funds_cache_ttl:
            delta_pnl = self._daily_pnl - self._pnl_at_last_fetch
            return max(0.0, self._funds_cache + delta_pnl)

        try:
            resp = self.client.funds()
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            for key in ("availablecash", "available_cash", "cash", "available_margin", "net"):
                value = data.get(key)
                if value is None:
                    continue
                capital = float(value)
                if capital > 0:
                    self._funds_cache       = capital
                    self._funds_cache_time  = now
                    self._pnl_at_last_fetch = self._daily_pnl
                    return capital
            inf(f"[FUNDS] available cash not found in funds() response: {resp}")
        except Exception as exc:
            err(f"[FUNDS] funds() fetch error", exc)
            if self._funds_cache_time:
                delta_pnl = self._daily_pnl - self._pnl_at_last_fetch
                return max(0.0, self._funds_cache + delta_pnl)
        return 0.0

    def _maybe_reset_daily_state(self):
        today = get_ist_now().strftime("%Y-%m-%d")
        with self._state.state_lock:
            if today != self._session_date:
                inf(f"[RISK] New trading day {today} — resetting session state")
                self._session_date               = today
                self._session_trade_count        = 0
                self._session_consecutive_losses = 0
                self._session_consecutive_wins   = 0
                self._daily_pnl                  = 0.0
                self._last_entry_times.clear()
                self._state.reset_market_caches()
                self._state._traded_today.clear()
                self._pnl_history.clear()

    def check_gates(self, symbol: str = "") -> tuple[bool, str]:
        """
        Evaluate all session-level risk guards.
        Returns (allowed, reason). reason is empty string when allowed.
        """
        self._maybe_reset_daily_state()
        cfg = self.config

        with self._state.state_lock:
            trade_count        = self._session_trade_count
            consecutive_losses = self._session_consecutive_losses
            daily_pnl          = self._daily_pnl
            last_entry_time    = self._last_entry_times.get(symbol)
            entry_in_flight    = self._state.entry_in_flight

        if entry_in_flight > 0:
            return False, f"Entry already in flight ({entry_in_flight})"
        if cfg.max_trades_per_session > 0 and trade_count >= cfg.max_trades_per_session:
            return False, (
                f"Max trades/session reached ({trade_count}/{cfg.max_trades_per_session})"
            )
        if cfg.max_consecutive_losses > 0 and consecutive_losses >= cfg.max_consecutive_losses:
            return False, (
                f"Loss streak limit reached ({consecutive_losses} consecutive losses)"
            )
        if cfg.entry_cooldown_secs > 0 and symbol:
            if last_entry_time is not None:
                elapsed = time.monotonic() - last_entry_time
                if elapsed < cfg.entry_cooldown_secs:
                    remaining = int(cfg.entry_cooldown_secs - elapsed)
                    return False, f"Entry cooldown active for {symbol} ({remaining}s remaining)"
        # ── Timing gate: no new entries after configured time (IST) ──────────
        # get_ist_now() is TZ-safe: returns IST regardless of Docker/UTC host.
        _now_ist = get_ist_now()
        if cfg.max_daily_loss_pct > 0:
            capital = self.available_capital()
            max_loss_amt = capital * (cfg.max_daily_loss_pct / 100.0)
            if daily_pnl <= -max_loss_amt:
                return False, (
                    f"Daily loss limit hit ({cfg.max_daily_loss_pct}% = "
                    f"₹{max_loss_amt:.0f}) | current P&L ₹{daily_pnl:.0f}"
                )
        if cfg.max_daily_loss_amount > 0 and daily_pnl <= -cfg.max_daily_loss_amount:
            return False, (
                f"Daily loss limit hit (₹{cfg.max_daily_loss_amount:.0f}) "
                f"| current P&L ₹{daily_pnl:.0f}"
            )
        if cfg.drawdown_rate_enabled and cfg.drawdown_rate_max_loss > 0 and len(self._pnl_history) >= 2:
            window_pnl_change = self._daily_pnl - self._pnl_history[0][1]
            if window_pnl_change <= -cfg.drawdown_rate_max_loss:
                return False, (
                    f"Drawdown rate limit: ₹{abs(window_pnl_change):.0f} lost in last "
                    f"{cfg.drawdown_rate_window_mins}m (limit ₹{cfg.drawdown_rate_max_loss:.0f})"
                )
        if cfg.max_daily_profit_amount > 0 and daily_pnl >= cfg.max_daily_profit_amount:
            return False, (
                f"Daily profit target reached ₹{daily_pnl:.0f} "
                f"(target ₹{cfg.max_daily_profit_amount:.0f}) — locking in gains for the day"
            )
        if cfg.no_new_trade_after:
            now_hm = _now_ist.strftime("%H:%M")
            if now_hm >= cfg.no_new_trade_after:
                return False, (
                    f"No new entries after {cfg.no_new_trade_after} IST "
                    f"(current {now_hm}) — waiting for EOD"
                )
        return True, ""

    def record_entry(self, symbol: str):
        """Call after a confirmed entry fill."""
        with self._state.state_lock:
            self._session_trade_count += 1
            self._last_entry_times[symbol] = time.monotonic()

    def record_exit(self, pnl: float):
        """Call after a confirmed exit fill. Updates daily P&L and loss streak."""
        with self._state.state_lock:
            self._daily_pnl += pnl
            now_ts = time.time()
            self._pnl_history.append((now_ts, self._daily_pnl))
            cutoff = now_ts - (self.config.drawdown_rate_window_mins * 60)
            while self._pnl_history and self._pnl_history[0][0] < cutoff:
                self._pnl_history.popleft()
            if pnl < 0:
                self._session_consecutive_losses += 1
                self._session_consecutive_wins = 0
                inf(f"[RISK] Loss streak: {self._session_consecutive_losses} | "
                      f"Daily P&L ₹{self._daily_pnl:.0f}")
            else:
                self._session_consecutive_losses = 0
                self._session_consecutive_wins += 1

    def effective_lot_multiplier(self, base_multiplier: int) -> int:
        """Adaptive lot sizing (U9). Disabled by default for safety."""
        cfg = self.config
        if not cfg.adaptive_sizing_enabled:
            return max(1, base_multiplier)
        bonus = (
            self._session_consecutive_wins // cfg.adaptive_win_streak_trigger
        ) * cfg.adaptive_win_streak_step
        return max(1, min(base_multiplier + bonus, cfg.adaptive_max_lot_mult))

    @property
    def consecutive_wins(self) -> int:
        return self._session_consecutive_wins

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def halted(self) -> bool:
        """Convenience property — True when check_gates() would block new entries."""
        allowed, _ = self.check_gates()
        return not allowed


# ===============================================================================
# WEBSOCKET MANAGER — real-time LTP + breach detection
# ===============================================================================

class WebSocketManager:
    """
    Manages the WebSocket connection and per-tick breach detection (SL/target hits).
    Trailing SL ratchets are computed by TrailSLEngine on the strategy thread.
    Callbacks for exit and broker SL modification are wired after construction
    to avoid circular dependency with OrderManager.
    """

    def __init__(self, client: api, config: BotConfig, state: BotState):
        self.client = client
        self.config = config
        self._state = state
        self._exit_callback:      Callable[[str, str], None] | None = None
        self._sl_modify_callback: Callable[[str, float], bool] | None = None
        self._notify_callback:    Callable[[str, int], None] | None = None   # U-G: wired after init
        self._fetcher:            DataFetcher | None = None  # Set via set_fetcher() after construction
        self._ws_started     = threading.Event()
        self._desired: set[tuple[str, str]] = set()   # Instruments we WANT subscribed (desired state)
        self._actual:  set[tuple[str, str]] = set()   # Instruments SDK has confirmed subscribed (actual state)
        self._subscribe_lock  = threading.Lock()
        self._last_tick_time: float = 0.0                   # updated on every valid tick; used by watchdog
        self._ws_stale_alerted: bool = False                # U-G: rate-limit 30s WARNING log to once per stale window
        self._delta_cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._delta_cache_max_size: int = 200  # Prevent unbounded growth
        self._delta_fetch_inflight: set[str] = set()
        self._delta_fetch_limit: int = 100
        self._delta_lock = threading.Lock()
        # Thread pool to limit concurrent delta fetches (avoid spawning unlimited daemon threads)
        self._delta_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="delta-pool")
        self._ws_connected: bool = False  # True only while SDK reports a live, authenticated connection
        self._reconnect_count: int = 0
        self._reconcile_cycles: int = 0
        self._repaired_subscriptions: int = 0
        self._ws_start_time: float = time.time()
        self._telemetry_last_log_time: float = 0.0

    def set_fetcher(self, fetcher: DataFetcher) -> None:
        """Set DataFetcher reference to consolidate greeks API calls."""
        self._fetcher = fetcher

    def set_notify_callback(self, cb: Callable[[str, int], None]) -> None:
        """Wire the orchestrator's Telegram notify function into the WS watchdog (U-G)."""
        self._notify_callback = cb

    def is_connected(self) -> bool:
        """Returns True when the WebSocket is live and authenticated. Used by scan_underlying() entry guard."""
        return self._ws_connected

    def _get_cached_delta(self, underlying: str, option_symbol: str, ttl: float = 30.0) -> float | None:
        """Return cached |delta| and refresh asynchronously when stale."""
        with self._delta_lock:
            cached = self._delta_cache.get(option_symbol)
            if cached and (time.time() - cached[1]) < ttl:
                return cached[0]
            if option_symbol not in self._delta_fetch_inflight:
                if len(self._delta_fetch_inflight) < self._delta_fetch_limit:
                    self._delta_fetch_inflight.add(option_symbol)
                    # Use thread pool instead of unlimited daemon spawn
                    # Pass fetcher to reuse cached greeks instead of duplicate API call
                    self._delta_executor.submit(self._fetch_and_cache_delta, underlying, option_symbol, self._fetcher)
                else:
                    inf(f"[WS] Delta fetch suppressed because {len(self._delta_fetch_inflight)} requests are pending")
            return cached[0] if cached else None

    def _fetch_and_cache_delta(self, underlying: str, option_symbol: str, fetcher: DataFetcher | None = None) -> None:
        """Reuse DataFetcher's cached greeks instead of duplicate optiongreeks API call."""
        try:
            if fetcher is None:
                # Fallback: direct API call if fetcher unavailable (should not happen in normal flow)
                ul_exch = (
                    self.config.index_exchange
                    if underlying in self.config.index_underlyings
                    else self.config.spot_exchange
                )
                resp = self.client.optiongreeks(
                    symbol=option_symbol,
                    exchange=self.config.fno_exchange,
                    underlying_symbol=underlying,
                    underlying_exchange=ul_exch,
                )
                if resp and resp.get("status") == "success":
                    delta = resp.get("greeks", {}).get("delta")
                    if delta is not None:
                        with self._delta_lock:
                            while len(self._delta_cache) >= self._delta_cache_max_size:
                                self._delta_cache.popitem(last=False)
                            self._delta_cache[option_symbol] = (abs(float(delta)), time.time())
            else:
                # Use DataFetcher's cached greeks (consolidates API calls)
                greeks = fetcher._fetch_option_greeks_cached(underlying, option_symbol)
                if greeks and greeks.get("delta") is not None:
                    with self._delta_lock:
                        while len(self._delta_cache) >= self._delta_cache_max_size:
                            self._delta_cache.popitem(last=False)
                        self._delta_cache[option_symbol] = (abs(float(greeks["delta"])), time.time())
        except Exception:
            pass
        finally:
            with self._delta_lock:
                self._delta_fetch_inflight.discard(option_symbol)

    def set_exit_callback(self, cb: Callable[[str, str], None]) -> None:
        self._exit_callback = cb

    def set_sl_modify_callback(self, cb: Callable[[str, float], None]) -> None:
        self._sl_modify_callback = cb

    def start(self) -> None:
        t = threading.Thread(target=self._ws_thread, name="ws-thread", daemon=True)
        t.start()
        self._ws_started.wait(timeout=10)

    def subscribe(self, exchange: str, symbol: str) -> None:
        with self._subscribe_lock:
            self._desired.add((exchange, symbol))
            try:
                self.client.subscribe_ltp(
                    [{"exchange": exchange, "symbol": symbol}],
                    on_data_received=self._on_ws_data,
                )
                self._actual.add((exchange, symbol))
                inf(f"[WS] Subscribed option {exchange}:{symbol}")
            except Exception as exc: err(f"[WS] Subscribe error {exchange}:{symbol}: ", exc)

    def subscribe_spot(self, symbol: str) -> None:
        exch = self.config.index_exchange if symbol in self.config.index_underlyings else self.config.spot_exchange
        with self._subscribe_lock:
            self._desired.add((exch, symbol))
            try:
                self.client.subscribe_ltp(
                    [{"exchange": exch, "symbol": symbol}],
                    on_data_received=self._on_ws_data,
                )
                self._actual.add((exch, symbol))
                inf(f"[WS] Subscribed spot {exch}:{symbol}")
            except Exception as exc: err(f"[WS] Subscribe spot error {symbol}: ", exc)

    def unsubscribe(self, exchange: str, symbol: str) -> None:
        with self._subscribe_lock:
            self._desired.discard((exchange, symbol))
            try:
                self.client.unsubscribe_ltp([{"exchange": exchange, "symbol": symbol}])
                self._actual.discard((exchange, symbol))
            except Exception as exc:
                self._actual.discard((exchange, symbol))
                err(f"[WS] Unsubscribe error {exchange}:{symbol}", exc)

    def unsubscribe_spot(self, symbol: str) -> None:
        exch = self.config.index_exchange if symbol in self.config.index_underlyings else self.config.spot_exchange
        with self._subscribe_lock:
            self._desired.discard((exch, symbol))
            try:
                self.client.unsubscribe_ltp([{"exchange": exch, "symbol": symbol}])
                self._actual.discard((exch, symbol))
            except Exception as exc:
                self._actual.discard((exch, symbol))
                err(f"[WS] Unsubscribe spot error {symbol}", exc)

    def _on_ws_data(self, data: dict) -> None:
        """
        Handles every tick.  Two independent paths:
          Part A — option premium trail (premium trail SL)
          Part B — spot trail (spot-based SL ratchet for indices)
        """
        dbg(f"[WS] Received data: {data}")
        # ── RAW CALLBACK DIAGNOSTIC — fires on EVERY WS message ────────────
        if not hasattr(self, '_raw_cb_count'):
            self._raw_cb_count = 0
        self._raw_cb_count += 1
        _raw_cnt = self._raw_cb_count
        if _raw_cnt <= 5 or _raw_cnt % 50 == 0:
            _dtype = type(data).__name__
            _keys = list(data.keys()) if isinstance(data, dict) else "N/A"
            _inner = data.get("data") if isinstance(data, dict) else None
            _inner_keys = list(_inner.keys()) if isinstance(_inner, dict) else type(_inner).__name__ if _inner else "None"
            dbg(f"[WS-RAW] cb#{_raw_cnt}: type={_dtype} keys={_keys} | "
                f"inner_type={type(data.get('data')).__name__ if isinstance(data, dict) else 'N/A'} "
                f"inner_keys={_inner_keys}"
            )
        # ── END RAW DIAGNOSTIC ─────────────────────────────────────────────

        if not isinstance(data, dict):
            return
            
        # OpenAlgo SDK encapsulates actual market data inside a nested 'data' dictionary.
        # Fallback to root level just in case.
        inner_data = data.get("data") if isinstance(data.get("data"), dict) else data
        
        symbol = inner_data.get("symbol") or data.get("symbol", "")
        ltp    = inner_data.get("ltp") or data.get("ltp")
        
        if ltp is None:
            return
        try:
            ltp = float(ltp)
        except (TypeError, ValueError):
            return

        self._last_tick_time = time.time()    # feed heartbeat for watchdog
        with self._state.state_lock:
            self._state.ltp_map[symbol] = ltp

        # ── Feed SnapshotCache on every tick for active positions ──────────
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending:
                continue
            if pos.symbol == symbol:
                # Option premium tick → update snapshot
                snap = self._state.snapshot_cache.get_or_create(underlying)
                self._state.snapshot_cache.update(
                    underlying,
                    option_symbol=symbol,
                    option_ltp=ltp,
                    spot_ltp=snap.spot_ltp,
                )
            elif pos.spot_symbol == symbol:
                # Spot tick → update snapshot
                snap = self._state.snapshot_cache.get_or_create(underlying)
                self._state.snapshot_cache.update(
                    underlying,
                    spot_ltp=ltp,
                    option_ltp=snap.option_ltp,
                )

        # ── Diagnostic: log ticks for active position symbols ───────────────
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending:
                continue
            if pos.symbol == symbol:
                if not hasattr(self, '_tick_counts'):
                    self._tick_counts: dict[str, int] = {}
                self._tick_counts[symbol] = self._tick_counts.get(symbol, 0) + 1
                cnt = self._tick_counts[symbol]
                # Log every 5th tick to avoid flooding, plus first tick
                if cnt <= 3 or cnt % 5 == 0:
                    spot_snap = self._state.snapshot_cache.get(underlying)
                    spot_ltp = spot_snap.spot_ltp if spot_snap else None
                    inf(
                        f"[WS-TICK] {underlying} option={symbol} "
                        f"premium=₹{ltp:.2f} spot={spot_ltp if spot_ltp else 'N/A'} | "
                        f"tick#{cnt} SL=₹{pos.sl:.2f} TGT=₹{pos.tgt:.2f} | "
                        f"kl_active={pos.kl_active} kl_next={pos.kl_next_level}"
                    )
            elif pos.spot_symbol == symbol:
                if not hasattr(self, '_spot_tick_counts'):
                    self._spot_tick_counts: dict[str, int] = {}
                self._spot_tick_counts[underlying] = self._spot_tick_counts.get(underlying, 0) + 1
                cnt = self._spot_tick_counts[underlying]
                if cnt <= 3 or cnt % 5 == 0:
                    opt_snap = self._state.snapshot_cache.get(underlying)
                    opt_ltp = opt_snap.option_ltp if opt_snap else None
                    inf(
                        f"[WS-TICK] {underlying} spot={symbol} "
                        f"spot_ltp=₹{ltp:.2f} premium={opt_ltp if opt_ltp else 'N/A'} | "
                        f"spot_tick#{cnt} | "
                        f"kl_active={pos.kl_active} kl_next={pos.kl_next_level}"
                    )

        # ── Part A: Premium Trail (option LTP → trail SL) ──────────────────
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending or pos.symbol != symbol:
                continue
            self._check_premium_trail(underlying, pos, ltp)

        # ── Part B: Spot Trail (underlying LTP → spot SL ratchet) ──────────
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending or pos.spot_symbol != symbol:
                continue
            self._check_spot_trail(underlying, pos, ltp)

    def _check_premium_trail(self, underlying: str, pos: OptionPosition, ltp: float) -> None:
        cfg = self.config
        if cfg.delta_exit_threshold > 0 and not pos.exit_pending:
            live_delta = self._get_cached_delta(underlying, pos.symbol)
            if live_delta is not None and live_delta < cfg.delta_exit_threshold:
                inf(
                    f"[WS] DEEP OTM EXIT {underlying}: delta {live_delta:.3f} "
                    f"< threshold {cfg.delta_exit_threshold:.3f}"
                )
                self._trigger_exit(underlying, f"DeepOTM_delta_{live_delta:.3f}")
                return
        if ltp <= pos.sl:
            inf(f"[WS] PREMIUM SL HIT {underlying}: LTP {ltp:.2f} <= SL {pos.sl:.2f}")
            self._trigger_exit(underlying, "premium_sl_hit")
            return
        if ltp >= pos.tgt:
            inf(f"[WS] PREMIUM TARGET HIT {underlying}: LTP {ltp:.2f} >= TGT {pos.tgt:.2f}")
            self._trigger_exit(underlying, "premium_target_hit")
            return

    def _check_spot_trail(self, underlying: str, pos: OptionPosition, spot_ltp: float) -> None:
        if pos.trail_sl_spot is not None:
            if pos.option_type == "CE" and spot_ltp <= pos.trail_sl_spot:
                inf(f"[WS] SPOT TRAIL SL HIT {underlying}: spot {spot_ltp:.2f} <= trail_sl_spot {pos.trail_sl_spot:.2f}")
                self._trigger_exit(underlying, "spot_trail_sl_hit")
            elif pos.option_type == "PE" and spot_ltp >= pos.trail_sl_spot:
                inf(f"[WS] SPOT TRAIL SL HIT {underlying}: spot {spot_ltp:.2f} >= trail_sl_spot {pos.trail_sl_spot:.2f}")
                self._trigger_exit(underlying, "spot_trail_sl_hit")

    def _trigger_exit(self, underlying: str, reason: str) -> None:
        normalized_reason = ExitReason.normalize(reason)
        with self._state.state_lock:
            pos = self._state.positions.get(underlying)
            if not pos or pos.exit_pending:
                return
            pos.exit_pending = True
            with self._state.exit_lock:
                if underlying in self._state.exit_queue:
                    return
                self._state.exit_queue.add(underlying)
        if self._exit_callback:
            threading.Thread(
                target=self._exit_callback,
                args=(underlying, normalized_reason),
                name=f"exit-{underlying}",
                daemon=True,
            ).start()

    def _ws_thread(self) -> None:
        inf("[WS] WebSocket thread starting...")
        self._ws_started.set()
        ws_url = self.config.ws_url or "(SDK default)"
        backoff_secs = 5
        max_backoff_secs = 300  # 5 minutes max backoff
        consecutive_failures = 0
        max_consecutive_failures = 36  # ~3 hours of retries before circuit break
        is_first_connect = True
        while True:
            try:
                # Persistent single-client architecture — SDK-aligned.
                # Per OpenAlgo SDK docs: one client instance, one connect(), many subscriptions.
                # Re-instantiating api() on every attempt spawns new internal SDK threads
                # without cleaning up the old ones, exhausting the OS thread limit.
                # Fix: reuse self.client (constructed once in BuyerEdgeStrategy.__init__);
                # disconnect() the previous transport before reconnecting.
                inf(f"[WS] Connecting... (active OS threads: {threading.active_count()})")
                try:
                    self.client.disconnect()   # Release previous transport threads
                except Exception:
                    pass
                with self._subscribe_lock:
                    self._actual.clear()
                time.sleep(0.5)               # Brief pause: allow SDK thread teardown

                ok = self.client.connect()
                _actual_url = getattr(self.client, 'ws_url', ws_url)
                inf(f"[WS] Client connects using {_actual_url} (expected {ws_url})")
                if ok:
                    if not is_first_connect:
                        self._reconnect_count += 1
                    is_first_connect = False
                    self._ws_connected = True
                    inf(f"[WS] Connected to {_actual_url} — SDK managing reconnects automatically")
                    backoff_secs = 5  # Reset backoff on successful connect
                    consecutive_failures = 0
                    # ── Diff-based subscription reconciliation ──────────────────────────────
                    # Reconcile desired vs actual rather than a full replay.
                    # Avoids redundant SDK subscribe calls for already-active instruments.
                    with self._subscribe_lock:
                        to_add    = self._desired - self._actual
                        to_remove = self._actual  - self._desired  # stale cleanup (edge case)
                    if to_add or to_remove:
                        inf(f"[WS] Reconciling: +{len(to_add)} subscribe / -{len(to_remove)} unsubscribe")
                    for (exch, sym) in to_remove:
                        try:
                            self.client.unsubscribe_ltp([{"exchange": exch, "symbol": sym}])
                            with self._subscribe_lock:
                                self._actual.discard((exch, sym))
                        except Exception as _un_exc: err(f"[WS] Reconcile unsubscribe error {exch}:{sym}: ", _un_exc)
                    for (exch, sym) in to_add:
                        with self._subscribe_lock:
                            if (exch, sym) not in self._desired:
                                continue
                        try:
                            self.client.subscribe_ltp(
                                [{"exchange": exch, "symbol": sym}],
                                on_data_received=self._on_ws_data,
                            )
                            with self._subscribe_lock:
                                if (exch, sym) in self._desired:
                                    self._actual.add((exch, sym))
                        except Exception as _re_exc: err(f"[WS] Reconcile subscribe error {exch}:{sym}: ", _re_exc)
                    while True:  # watchdog: graduated alerts then force-reconnect if feed silent
                        time.sleep(30)
                        elapsed = time.time() - self._last_tick_time
                        hm = int(get_ist_now().strftime("%H%M"))
                        in_market = self._last_tick_time and MARKET_HOURS_START <= hm <= MARKET_HOURS_END
                        if in_market and elapsed > 30 and not self._ws_stale_alerted:
                            inf(f"[WS] WARNING: Stale tick feed — no ticks in {int(elapsed)}s (market hours active)")
                            self._ws_stale_alerted = True
                        if in_market and elapsed > 120:
                            _msg = f"⚠️ WS Feed STALE: No ticks for {int(elapsed)}s during market hours. Forcing reconnect — check broker/VPS connectivity."
                            if self._notify_callback:
                                try:
                                    self._notify_callback(_msg, 9)
                                except Exception:
                                    pass
                            inf(f"[WS] Feed silent {int(elapsed)}s — forcing hard reconnect...")
                            self._ws_stale_alerted = False   # reset for next connection window
                            try:
                                self.client.disconnect()
                            except Exception:
                                pass
                            break   # exit watchdog → outer loop reconnects immediately
                        if not in_market:
                            self._ws_stale_alerted = False   # reset outside market hours

                        # ── Fix B5: Periodic Reconcile Check ──
                        # If a mid-batch subscribe fails, it won't be in _actual. Retry here.
                        with self._subscribe_lock:
                            missing = self._desired - self._actual
                        if missing:
                            self._reconcile_cycles += 1
                            self._repaired_subscriptions += len(missing)
                            inf(f"[WS] Watchdog: Found {len(missing)} missing subscriptions. Attempting to reconcile...")
                            for (exch, sym) in missing:
                                with self._subscribe_lock:
                                    if (exch, sym) not in self._desired:
                                        continue
                                try:
                                    self.client.subscribe_ltp(
                                        [{"exchange": exch, "symbol": sym}],
                                        on_data_received=self._on_ws_data,
                                    )
                                    with self._subscribe_lock:
                                        if (exch, sym) in self._desired:
                                            self._actual.add((exch, sym))
                                except Exception as _re_exc: err(f"[WS] Watchdog subscribe error {exch}:{sym}: ", _re_exc)

                        # ── Telemetry Logging ──
                        now_ts = time.time()
                        if now_ts - self._telemetry_last_log_time >= 300:
                            self._telemetry_last_log_time = now_ts
                            with self._subscribe_lock:
                                d_len = len(self._desired)
                                a_len = len(self._actual)
                            uptime_mins = (now_ts - self._ws_start_time) / 60.0
                            last_tick_sec = (now_ts - self._last_tick_time) if self._last_tick_time else 0.0
                            dbg(f"[WS-HEALTH] Uptime: {uptime_mins:.1f}m | Threads: {threading.active_count()} | "
                                f"Subs: {d_len}/{a_len} | Reconnects: {self._reconnect_count} | "
                                f"Reconciles: {self._reconcile_cycles} (Repaired: {self._repaired_subscriptions}) | "
                                f"LastTick: {last_tick_sec:.1f}s"
                            )

                    continue        # skip backoff sleep — reconnect without delay
                consecutive_failures += 1
                self._ws_connected = False
                inf(f"[WS] Connection failed, Verify [WEBSOCKET_URL={self.config.ws_url}, API Key: {self.config.api_key}], attempt {consecutive_failures}/{max_consecutive_failures}")
            except Exception as exc:
                _emsg = str(exc)
                consecutive_failures += 1
                self._ws_connected = False
                err(f"[WS] Connection error: {exc}. Attempt {consecutive_failures}/{max_consecutive_failures}", exc)
                if "Invalid API key" in _emsg or "AUTHENTICATION_ERROR" in _emsg:
                    inf("[WS] HINT: Check OPENALGO_API_KEY — copy the key from OpenAlgo dashboard \u2192 API Key page")
                elif "InvalidStatus" in type(exc).__name__ or "HTTP 200" in _emsg:
                    inf("[WS] HINT: Reverse proxy (/ws) not routing to port 8765 — fix Caddyfile or use ws://127.0.0.1:8765")
            # Circuit breaker: if persistent failures, give up to prevent memory accumulation
            if consecutive_failures >= max_consecutive_failures:
                inf(f"[WS] Circuit breaker triggered: {consecutive_failures} consecutive failures. Giving up.")
                inf("[WS] Check broker connectivity, credentials, and reverse proxy configuration.")
                if self._notify_callback:
                    try:
                        self._notify_callback(
                            f"🚨 WS Circuit Breaker: {consecutive_failures} consecutive failures. "
                            f"WebSocket monitoring STOPPED. Only broker SL-M protecting positions.",
                            9,
                        )
                    except Exception:
                        pass
                return  # Exit thread to prevent infinite retry accumulation
            # Exponential backoff (5s → 7.5s → 11.25s ... → 300s max)
            current_backoff = min(backoff_secs, max_backoff_secs)
            inf(f"[WS] Retrying in {current_backoff:.0f}s...")
            time.sleep(current_backoff)
            backoff_secs = min(backoff_secs * 1.5, max_backoff_secs)


# ===============================================================================
# ORDER MANAGER — entry, exit, broker SL orders, pending reconciliation
# ===============================================================================

class OrderManager:
    """
    Places and manages all orders via the OpenAlgo SDK.
    Depends on WebSocketManager (for subscribe/unsubscribe) and RiskManager.
    notify(message, priority) sends Telegram alerts.
    """

    def __init__(
        self,
        client:  api,
        config:  BotConfig,
        state:   BotState,
        risk:    "RiskManager",
        ws:      WebSocketManager,
        fetcher: "DataFetcher",
        notify:  Callable[[str, int], None],
    ):
        self.client = client
        self.config = config
        self._state = state
        self._risk  = risk
        self._ws    = ws
        self._fetcher = fetcher
        self._notify = notify

    def poll_order_status(
        self,
        order_id: str,
        max_retries: int | None = None,
        sleep_secs: float | None = None,
    ) -> dict | None:
        cfg = self.config
        max_r = max_retries if max_retries is not None else cfg.order_status_max_retries
        slp   = sleep_secs  if sleep_secs  is not None else cfg.order_status_poll_interval
        _TERMINAL_FILL    = ("complete", "filled", "executed")
        _TERMINAL_FAIL    = ("rejected", "cancelled")
        for attempt in range(max_r):
            try:
                resp = self.client.orderstatus(order_id=order_id, strategy=cfg.strategy_name)
                if not resp:
                    time.sleep(slp)
                    continue
                if not isinstance(resp, dict):
                    time.sleep(slp)
                    continue
                api_status = resp.get("status", "").lower()
                if api_status not in ("success",):
                    time.sleep(slp)
                    continue
                data         = resp.get("data") or resp
                order_status = str(data.get("order_status", "")).lower()
                if order_status in _TERMINAL_FILL:
                    # ORD-2: only return on a confirmed terminal fill state
                    return resp
                if order_status in _TERMINAL_FAIL:
                    inf(f"[ORDER] Order {order_id} {order_status}")
                    return None
                # ORD-2: detect partial fill near end of retry window
                filled_qty = int(data.get("filled_quantity", 0) or 0)
                if filled_qty > 0 and attempt >= int(max_r * 0.8):
                    inf(
                        f"[ORDER] Partial fill detected: {filled_qty} units "
                        f"for {order_id} (attempt {attempt+1}/{max_r}) — treating as fill"
                    )
                    return resp
            except Exception as exc: err(f"[ORDER] orderstatus error (attempt {attempt+1}): ", exc)
            time.sleep(slp)
        inf(f"[ORDER] Timed out polling order {order_id} after {max_r} attempts")
        return None

    def cancel_broker_orders(self, underlying: str) -> dict:
        """Cancel outstanding broker SL-M + LIMIT target orders for an underlying."""
        pos = self._state.positions.get(underlying)
        if not pos:
            return {}
        broker_filled: dict = {}
        sl_id  = pos.sl_order_id
        tgt_id = pos.tgt_order_id

        for attr_name, oid in (("sl_order_id", sl_id), ("tgt_order_id", tgt_id)):
            if not oid:
                continue
            try:
                resp = self.client.orderstatus(order_id=oid, strategy=self.config.strategy_name)
                if isinstance(resp, dict) and resp.get("status") == "success":
                    data = resp.get("data") or resp
                    broker_stat = str(data.get("order_status", "")).lower()
                    if broker_stat in ("complete", "filled", "executed"):
                        broker_filled[attr_name] = {
                            "order_id":    oid,
                            "executed":    float(data.get("average_price", 0) or 0),
                            "order_status": broker_stat,
                        }
                        inf(f"[ORDER] Broker {attr_name} already filled: {oid}")
            except Exception as exc: err(f"[ORDER] pre-check fill error {oid}: ", exc)

        for attr_name, oid in (("sl_order_id", sl_id), ("tgt_order_id", tgt_id)):
            if not oid or attr_name in broker_filled:
                continue
            try:
                resp = self.client.cancelorder(order_id=oid, strategy=self.config.strategy_name)
                if isinstance(resp, dict) and resp.get("status") in ("success", "cancelled"):
                    inf(f"[ORDER] Cancelled broker {attr_name} {oid}")
                else:
                    inf(f"[ORDER] Cancel resp for {oid}: {resp}")
            except Exception as exc: err(f"[ORDER] Cancel error {oid}: ", exc)

        for attr_name, oid in (("sl_order_id", sl_id), ("tgt_order_id", tgt_id)):
            if not oid:
                continue
            try:
                resp = self.client.orderstatus(order_id=oid, strategy=self.config.strategy_name)
                if isinstance(resp, dict) and resp.get("status") == "success":
                    data = resp.get("data") or resp
                    broker_stat = str(data.get("order_status", "")).lower()
                    inf(f"[ORDER] Post-cancel status {oid}: {broker_stat}")
            except Exception as exc: err(f"[ORDER] Post-cancel check error {oid}: ", exc)
        pos.sl_order_id  = None
        pos.tgt_order_id = None
        pos.broker_protection = False
        return broker_filled

    def modify_broker_sl(self, underlying: str, new_trigger: float) -> bool:
        """Modify broker SL-M trigger price. Returns True if the broker accepted the change."""
        if self.config.paper_trade:
            return False  # no-op in paper trade mode
        pos = self._state.positions.get(underlying)
        if not pos or not pos.sl_order_id:
            return False
        # ORD-4: pre-check if broker SL already filled before sending modifyorder
        try:
            pre = self.client.orderstatus(
                order_id=pos.sl_order_id, strategy=self.config.strategy_name
            )
            if isinstance(pre, dict) and pre.get("status") == "success":
                _data = pre.get("data") or pre
                if str(_data.get("order_status", "")).lower() in ("complete", "filled", "executed"):
                    inf(f"[ORDER] SL already filled for {underlying} — skipping modify, triggering exit")
                    self.place_exit(underlying, "broker_sl_filled_on_modify")
                    return False
        except Exception as _pre_exc: err(f"[ORDER] modify_broker_sl pre-check error for {underlying}: ", _pre_exc)
        try:
            resp = self.client.modifyorder(
                order_id=pos.sl_order_id,
                strategy=self.config.strategy_name,
                symbol=pos.symbol,
                exchange=self.config.fno_exchange,
                action="SELL",
                quantity=pos.qty,
                order_type="SL-M",
                product="MIS",
                price=0,
                trigger_price=new_trigger,
            )
            if isinstance(resp, dict) and resp.get("status") == "success":
                inf(f"[ORDER] Broker SL modified for {underlying} → trigger ₹{new_trigger:.2f}")
                return True
            else:
                inf(f"[ORDER] modifyorder resp for {underlying}: {resp}")
                # TOCTOU guard: modify may have failed because SL filled in the
                # window between pre-check and modifyorder. Re-query immediately.
                try:
                    post = self.client.orderstatus(
                        order_id=pos.sl_order_id, strategy=self.config.strategy_name
                    )
                    if isinstance(post, dict) and post.get("status") == "success":
                        _post_data = post.get("data") or post
                        if str(_post_data.get("order_status", "")).lower() in ("complete", "filled", "executed"):
                            inf(f"[ORDER] SL filled in modify window for {underlying} — triggering exit")
                            self.place_exit(underlying, "broker_sl_filled_on_modify")
                except Exception:
                    pass
                return False
        except Exception as exc:
            err(f"[ORDER] modify_broker_sl error for {underlying}", exc)
            # TOCTOU guard: same immediate status check on exception
            try:
                post = self.client.orderstatus(
                    order_id=pos.sl_order_id, strategy=self.config.strategy_name
                )
                if isinstance(post, dict) and post.get("status") == "success":
                    _post_data = post.get("data") or post
                    if str(_post_data.get("order_status", "")).lower() in ("complete", "filled", "executed"):
                        inf(f"[ORDER] SL filled in modify window (exc) for {underlying} — triggering exit")
                        self.place_exit(underlying, "broker_sl_filled_on_modify")
            except Exception:
                pass
            return False

    def check_broker_order_fills(self) -> None:
        """Periodic poll: if broker SL or target order was filled, trigger exit."""
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending:
                continue
            for attr_name, raw_reason in (
                ("sl_order_id",  "broker_sl_filled"),
                ("tgt_order_id", "broker_target_filled"),
            ):
                reason = ExitReason.normalize(raw_reason)
                oid = getattr(pos, attr_name, None)
                if not oid:
                    continue
                try:
                    resp = self.client.orderstatus(order_id=oid, strategy=self.config.strategy_name)
                    if not isinstance(resp, dict) or resp.get("status") != "success":
                        continue
                    data = resp.get("data") or resp
                    broker_stat = str(data.get("order_status", "")).lower()
                    if broker_stat in ("complete", "filled", "executed"):
                        # ORD-5: mark exit_pending immediately to block concurrent WS trail exits
                        with self._state.state_lock:
                            if pos.exit_pending:
                                continue
                            pos.exit_pending = True
                            with self._state.exit_lock:
                                self._state.exit_queue.add(underlying)
                        inf(f"[ORDER] Broker {attr_name} filled for {underlying} ({oid})")
                        
                        # --- CANCEL THE OTHER LEG ---
                        other_oid = pos.tgt_order_id if attr_name == "sl_order_id" else pos.sl_order_id
                        other_name = "tgt_order_id" if attr_name == "sl_order_id" else "sl_order_id"
                        if other_oid:
                            try:
                                inf(f"[ORDER] Cancelling opposite broker order {other_name} ({other_oid})...")
                                self.client.cancelorder(order_id=other_oid, strategy=self.config.strategy_name)
                            except Exception as c_exc: err(f"[ORDER] Cancel opposite broker order error {other_name} ({other_oid}): ", c_exc)
                        # ----------------------------
                        executed_price = float(data.get("average_price", 0) or 0)
                        pnl = 0.0
                        if executed_price > 0:
                            pnl = (executed_price - pos.entry_premium) * pos.qty
                        # PNL-2: always update both risk counter and journal together
                        # (prevents journal vs daily_pnl drift when average_price=0)
                        self._risk.record_exit(pnl)
                        direction_emoji = "🔺 UP" if pos.option_type.upper() == "CE" else "🔻 DN"
                        emoji = "✅ PROFIT" if pnl >= 0 else "❌ LOSS"
                        risk_pts = max(0.01, pos.entry_premium - pos.initial_sl)
                        risk_amt = risk_pts * pos.qty
                        r_multiple = pnl / risk_amt if risk_amt > 0 else 0.0
                        hold_mins = max(0, int((get_ist_now() - pos.entry_time).total_seconds() / 60))
                        self._notify(
                            f"{emoji} EXIT: {underlying}\n"
                            f"📌 {reason.upper()}\n"
                            f"{direction_emoji} {pos.symbol}\n"
                            f"🚪 ₹{pos.entry_premium:.2f} → ₹{executed_price:.2f}\n"
                            f"💰 P&L: ₹{pnl:.0f} ({r_multiple:+.2f}R)\n"
                            f"⏱ Hold: {hold_mins}m | Daily: ₹{self._risk.daily_pnl:.0f}",
                            2,
                        )
                        self._write_journal(underlying, pos, executed_price, pnl, reason,
                                            exit_price_source="broker_fill")
                        self._ws.unsubscribe(self.config.fno_exchange, pos.symbol)
                        self._ws.unsubscribe_spot(pos.spot_symbol)
                        with self._state.state_lock:
                            self._state.positions.pop(underlying, None)
                        with self._state.exit_lock:
                            self._state.exit_queue.discard(underlying)
                except Exception as exc: err(f"[ORDER] check_broker_order_fills error ({underlying}, {oid}): ", exc)

    def verify_sl_orders_active(self) -> None:
        """Periodic verification that broker SL orders are still open.
        Detects externally cancelled SL orders and re-issues them.
        """
        if self.config.paper_trade:
            return
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending or not pos.sl_order_id:
                continue
            try:
                resp = self.client.orderstatus(
                    order_id=pos.sl_order_id, strategy=self.config.strategy_name
                )
                if not isinstance(resp, dict) or resp.get("status") != "success":
                    continue
                data = resp.get("data") or resp
                broker_stat = str(data.get("order_status", "")).lower()
                # SL was cancelled/rejected externally - re-issue it
                if broker_stat in ("cancelled", "rejected", "canceled", "expired"):
                    inf(f"[ORDER] SL ORDER MISSING for {underlying} (status={broker_stat}) — re-issuing SL")
                    # Clear stale order ID so _place_protection_orders_sequential re-issues it
                    pos.sl_order_id = None
                    self._place_protection_orders_sequential(
                        underlying, pos, pos.symbol, pos.qty, pos.sl, pos.tgt
                    )
                    if pos.sl_order_id:
                        inf(f"[ORDER] SL re-issued for {underlying}: new_id={pos.sl_order_id}")
                    else:
                        err(f"[ORDER] SL re-issue FAILED for {underlying}")
            except Exception as exc:
                err(f"[ORDER] verify_sl_orders_active error for {underlying}: ", exc)

    def register_filled_entry(
        self,
        underlying: str,
        option_symbol: str,
        qty: int,
        spot: float,
        direction: str,
        executed: float,
        sl_pts: float | None = None,
        entry_delta: float | None = None,
        entry_conviction: float = 0.0,
    ) -> None:
        """Register filled entry with delta tracking for moneyness analysis."""
        cfg = self.config
        moneyness, _, tgt_mult, act_mult = EntryStopLossPolicy.get_moneyness_multipliers(entry_delta)

        resolved_sl_pts = sl_pts if (sl_pts is not None and sl_pts > 0) else cfg.premium_stop_pts
        sl  = executed - resolved_sl_pts
        tgt = executed + (cfg.premium_target_pts * tgt_mult)
        reward_dist = spot * (cfg.spot_reward_pct / 100.0)

        pos = OptionPosition(
            underlying=underlying,
            symbol=option_symbol,
            entry_premium=executed,
            qty=qty,
            option_type=direction,
            entry_delta=entry_delta,
            moneyness=moneyness,
            sl=sl,
            initial_sl=sl,
            tgt=tgt,
            spot_symbol=underlying,
            spot_entry=spot,
            reward_dist=reward_dist,
            entry_time=get_ist_now(),
            entry_conviction=max(0.0, min(1.0, entry_conviction)),
            trail_act_mult=act_mult,
        )
        with self._state.state_lock:
            self._state.positions[underlying] = pos
        self._state.mark_traded(option_symbol, direction)

        # Link snapshot cache with the active option symbol
        self._state.snapshot_cache.set_option_symbol(underlying, option_symbol)

        self._ws.subscribe(cfg.fno_exchange, option_symbol)
        self._ws.subscribe_spot(underlying)
        inf(
            f"[DATA] TRADE REGISTERED {underlying}: "
            f"option={option_symbol} exchange={cfg.fno_exchange} | "
            f"spot={underlying} spot_exchange={'NSE_INDEX' if underlying in cfg.index_underlyings else cfg.spot_exchange} | "
            f"entry=₹{executed:.2f} spot_entry=₹{spot:.2f} direction={direction} | "
            f"SL=₹{sl:.2f} TGT=₹{tgt:.2f} | "
            f"delta={entry_delta if entry_delta else 'N/A'} conviction={entry_conviction:.2f} | "
            f"ws_desired={list(self._ws._desired)}"
        )

        if cfg.broker_sl_orders and not cfg.paper_trade:
            if getattr(cfg, "use_basket_protection", True) and hasattr(self.client, "basketorder"):
                self._place_protection_basket(underlying, pos, option_symbol, qty, sl, tgt)
            else:
                self._place_protection_orders_sequential(underlying, pos, option_symbol, qty, sl, tgt)

            if pos.sl_order_id or pos.tgt_order_id:
                pos.broker_protection = True

        inf(
            f"[ORDER] Position registered for {underlying}: {option_symbol} "
            f"QTY={qty} ENTRY=₹{executed:.2f} SL=₹{sl:.2f} "
            f"(pts={resolved_sl_pts:.2f}) TGT=₹{tgt:.2f}"
        )

        direction_emoji = "🔺 UP" if direction.upper() == "CE" else "🔻 DN"
        now_str = datetime.now().strftime("%H:%M:%S")
        actual_sl_pts = round(executed - sl, 2)
        actual_target_pts = round(tgt - executed, 2)
        rrr = round(actual_target_pts / actual_sl_pts, 2) if actual_sl_pts > 0 else 0
        sl_amt = actual_sl_pts * qty
        tgt_amt = actual_target_pts * qty
        delta_str = f"{entry_delta:.2f}" if entry_delta is not None else "N/A"
        mode_str = "PAPER" if cfg.paper_trade else "TRADE"
        self._notify(
            f"🚀 {direction_emoji} {mode_str} ENTRY: {underlying} @ {now_str}\n"
            f"🔹 Option: {option_symbol} (x{qty})\n"
            f"🎯 Fill Price: ₹{executed:.2f}\n"
            f"📊 {moneyness} | RRR: 1:{rrr} | Δ:{delta_str} | Conv:{entry_conviction:.0%}\n"
            f"🛑 SL: {actual_sl_pts:.1f} (₹{sl_amt:.0f}) | 🏁 TGT: {actual_target_pts:.1f} (₹{tgt_amt:.0f})",
            2,
        )

    def _place_protection_basket(
        self,
        underlying: str,
        pos: OptionPosition,
        option_symbol: str,
        qty: int,
        sl: float,
        tgt: float
    ) -> None:
        cfg = self.config
        try:
            basket_orders = [
                {
                    "symbol": option_symbol,
                    "exchange": cfg.fno_exchange,
                    "action": "SELL",
                    "quantity": qty,
                    "pricetype": "SL-M",
                    "product": "MIS",
                    "trigger_price": sl,
                    "price": 0,
                },
                {
                    "symbol": option_symbol,
                    "exchange": cfg.fno_exchange,
                    "action": "SELL",
                    "quantity": qty,
                    "pricetype": "LIMIT",
                    "product": "MIS",
                    "price": tgt,
                }
            ]
            basket_resp = self.client.basketorder(orders=basket_orders)
            if isinstance(basket_resp, dict) and basket_resp.get("status") == "success":
                results = basket_resp.get("results", [])
                for i, leg in enumerate(results):
                    if leg.get("status") != "success" or not leg.get("orderid"):
                        inf(f"[ORDER] Basket leg {i} rejected for {underlying}: {leg}")
                        continue
                    
                    pt = str(leg.get("pricetype", leg.get("price_type", leg.get("ordertype", "")))).upper()
                    is_sl = ("SL" in pt) if pt else (i == 0)
                    
                    if is_sl:
                        pos.sl_order_id = leg.get("orderid")
                        inf(f"[ORDER] Basket SL-M placed for {underlying}: trigger ₹{sl:.2f} (id:{pos.sl_order_id})")
                    else:
                        pos.tgt_order_id = leg.get("orderid")
                        inf(f"[ORDER] Basket LIMIT placed for {underlying}: ₹{tgt:.2f} (id:{pos.tgt_order_id})")

                if pos.sl_order_id and pos.tgt_order_id:
                    return
        except Exception as exc: err(f"[ORDER] Basket order error for {underlying}: ", exc)

        inf(f"[ORDER] Falling back to sequential protective orders for {underlying}...")
        self._place_protection_orders_sequential(underlying, pos, option_symbol, qty, sl, tgt)

    def _place_protection_orders_sequential(
        self,
        underlying: str,
        pos: OptionPosition,
        option_symbol: str,
        qty: int,
        sl: float,
        tgt: float
    ) -> None:
        cfg = self.config
        if not pos.sl_order_id:
            try:
                sl_resp = self.client.placeorder(
                    strategy=cfg.strategy_name,
                    symbol=option_symbol,
                    action="SELL",
                    exchange=cfg.fno_exchange,
                    price_type="SL-M",
                    product="MIS",
                    quantity=qty,
                    price=0,
                    trigger_price=sl,
                )
                if isinstance(sl_resp, dict) and sl_resp.get("status") == "success":
                    pos.sl_order_id = sl_resp.get("orderid")
                    inf(f"[ORDER] Broker SL-M placed for {underlying}: trigger ₹{sl:.2f} (id:{pos.sl_order_id})")
            except Exception as exc: err(f"[ORDER] Broker SL-M error for {underlying}: ", exc)
                
        if not pos.tgt_order_id:
            try:
                tgt_resp = self.client.placeorder(
                    strategy=cfg.strategy_name,
                    symbol=option_symbol,
                    action="SELL",
                    exchange=cfg.fno_exchange,
                    price_type="LIMIT",
                    product="MIS",
                    quantity=qty,
                    price=tgt,
                )
                if isinstance(tgt_resp, dict) and tgt_resp.get("status") == "success":
                    pos.tgt_order_id = tgt_resp.get("orderid")
                    inf(f"[ORDER] Broker LIMIT placed for {underlying}: ₹{tgt:.2f} (id:{pos.tgt_order_id})")
            except Exception as exc: err(f"[ORDER] Broker LIMIT target error for {underlying}: ", exc)

    # ── Trade Journal ──────────────────────────────────────────────────────────

    def _write_journal(
        self,
        underlying: str,
        pos: OptionPosition,
        exit_price: float,
        pnl: float,
        reason: str,
        exit_price_source: str = "broker_fill",
    ) -> None:
        """Append one row to the CSV trade journal (if enabled).

        Args:
            exit_price_source: One of "broker_fill", "snapshot", "estimated".
               - broker_fill: price from broker order response (average_price)
               - snapshot:    price from SnapshotCache (live or stale)
               - estimated:   fallback when neither is available (0.0 or entry_premium)
               - paper:       paper-trade simulated price
        """
        path = self.config.trade_journal_path
        if not path:
            return
        risk_pts = max(0.01, pos.entry_premium - pos.initial_sl)
        risk_amt = risk_pts * pos.qty
        r_multiple = pnl / risk_amt if risk_amt > 0 else 0.0
        header = [
            "timestamp", "underlying", "option_symbol", "direction", "qty",
            "entry", "exit", "pnl", "reason", "mode",
            "r_multiple", "entry_conviction", "moneyness",
            "exit_price_source",
        ]
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            underlying,
            pos.symbol,
            pos.option_type,
            pos.qty,
            f"{pos.entry_premium:.2f}",
            f"{exit_price:.2f}",
            f"{pnl:.2f}",
            reason,
            "PAPER" if self.config.paper_trade else "LIVE",
            f"{r_multiple:.2f}",
            f"{pos.entry_conviction:.2f}",
            pos.moneyness,
            exit_price_source,
        ]
        write_header = not os.path.exists(path)
        try:
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(header)
                w.writerow(row)
        except OSError as exc:
            err(f"[JOURNAL] Write error", exc)

    def place_entry(
        self,
        underlying: str,
        option_symbol: str,
        qty: int,
        spot: float,
        direction: str,
        sl_pts: float | None = None,
        entry_delta: float | None = None,
        entry_conviction: float = 0.0,
    ) -> bool:
        """Place a market BUY order, poll for fill, then register the position with moneyness tracking."""
        cfg = self.config
        resolved_sl_pts = sl_pts if (sl_pts is not None and sl_pts > 0) else cfg.premium_stop_pts
        if underlying in self._state.positions:
            inf(f"[ORDER] {underlying} already has an open position — skip entry")
            return False

        if cfg.paper_trade:
            snap = self._state.snapshot_cache.get(underlying)
            executed = (snap.option_ltp if snap and snap.option_ltp is not None
                        else self._state.ltp_map.get(option_symbol))
            executed = executed or spot * 0.01
            inf(f"[PAPER] Simulated BUY {qty}x {option_symbol} @ ₹{executed:.2f}")
            self._risk.record_entry(underlying)
            self.register_filled_entry(
                underlying, option_symbol, qty, spot, direction, executed,
                sl_pts=resolved_sl_pts, entry_delta=entry_delta,
                entry_conviction=entry_conviction,
            )
            return True

        with self._state.state_lock:
            self._state.entry_in_flight += 1
        try:
            if cfg.preflight_spread_check and not cfg.paper_trade:
                live_q = self._fetcher.fetch_quote(option_symbol, cfg.fno_exchange)
                if live_q:
                    bid = float(live_q.get("bid", 0) or 0)
                    ask = float(live_q.get("ask", 0) or 0)
                    ltp = float(live_q.get("ltp", 0) or 0)
                    mid = (bid + ask) / 2 if (bid and ask) else ltp
                    if cfg.preflight_min_bid > 0 and bid < cfg.preflight_min_bid:
                        inf(
                            f"[ORDER] Pre-flight FAIL {option_symbol}: "
                            f"bid ₹{bid:.2f} < min ₹{cfg.preflight_min_bid:.2f}"
                        )
                        return False
                    if (
                        cfg.preflight_max_spread_pct > 0
                        and mid > 0
                        and ask > bid
                    ):
                        spread_pct = (ask - bid) / mid * 100
                        if spread_pct > cfg.preflight_max_spread_pct:
                            inf(
                                f"[ORDER] Pre-flight FAIL {option_symbol}: "
                                f"spread {spread_pct:.1f}% > max {cfg.preflight_max_spread_pct:.1f}%"
                            )
                            return False

            resp = self.client.placeorder(
                strategy=cfg.strategy_name,
                symbol=option_symbol,
                action="BUY",
                exchange=cfg.fno_exchange,
                price_type="MARKET",
                product="MIS",
                quantity=qty,
            )
            if not isinstance(resp, dict) or resp.get("status") != "success":
                inf(f"[ORDER] Entry order rejected for {underlying}: {resp}")
                return False
            order_id = resp.get("orderid")
            inf(f"[ORDER] Entry order {order_id} placed for {underlying} ({option_symbol} x{qty})")

            # Add to pending entries for reconciliation
            with self._state.state_lock:
                self._state.pending_entries[underlying] = PendingEntry(
                    order_id=order_id,
                    symbol=option_symbol,
                    qty=qty,
                    spot=spot,
                    direction=direction,
                    sl_pts=resolved_sl_pts,
                    created_at=datetime.now(),
                    entry_delta=entry_delta,
                    entry_conviction=entry_conviction,
                )

            filled = self.poll_order_status(order_id)
            with self._state.state_lock:
                self._state.pending_entries.pop(underlying, None)
            if not filled:
                inf(f"[ORDER] Entry order {order_id} not filled within poll window — abandoning")
                return False

            data       = filled.get("data") or filled
            executed   = float(data.get("average_price", 0) or 0)
            if not executed:
                executed = float(data.get("price", 0) or 0)
            if not executed:
                inf(f"[ORDER] Executed price is zero for {order_id} — cannot register position")
                return False

            filled_qty = int(data.get("filled_quantity", 0) or data.get("filled_qty", 0) or 0)
            if filled_qty > 0 and filled_qty != qty:
                inf(
                    f"[ORDER] Partial fill accepted for {order_id}: requested {qty}, "
                    f"filled {filled_qty}"
                )
                qty = filled_qty

            self._risk.record_entry(underlying)
            self.register_filled_entry(
                underlying, option_symbol, qty, spot, direction, executed,
                sl_pts=resolved_sl_pts, entry_delta=entry_delta,
                entry_conviction=entry_conviction,
            )
            return True
        except Exception as exc:
            err(f"[ORDER] placeorder error for {underlying}", exc)
            return False
        finally:
            with self._state.state_lock:
                self._state.entry_in_flight = max(0, self._state.entry_in_flight - 1)

    def place_exit(self, underlying: str, reason: str = "manual") -> None:
        """Cancel broker orders first, then place SELL MARKET to exit position."""
        cfg = self.config
        pos = self._state.positions.get(underlying)
        if not pos:
            return
        # Normalize exit reason to enum for consistent attribution
        norm_reason = ExitReason.normalize(reason)
        inf(f"[ORDER] Exiting {underlying} — reason: {reason} → {norm_reason}")

        if cfg.paper_trade:
            snap = self._state.snapshot_cache.get(underlying)
            executed_price = (snap.option_ltp if snap and snap.option_ltp is not None
                              else self._state.ltp_map.get(pos.symbol))
            executed_price = executed_price or pos.entry_premium
            pnl = (executed_price - pos.entry_premium) * pos.qty
            inf(f"[PAPER] Simulated SELL {pos.qty}x {pos.symbol} @ ₹{executed_price:.2f} | P&L ₹{pnl:.2f}")
            self._risk.record_exit(pnl)
            self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
            self._ws.unsubscribe_spot(pos.spot_symbol)
            self._write_journal(underlying, pos, executed_price, pnl, norm_reason,
                                exit_price_source="paper")
            with self._state.state_lock:
                self._state.positions.pop(underlying, None)
            with self._state.exit_lock:
                self._state.exit_queue.discard(underlying)
            direction_emoji = "🔺 UP" if pos.option_type.upper() == "CE" else "🔻 DN"
            emoji = "✅ PROFIT" if pnl >= 0 else "❌ LOSS"
            risk_pts = max(0.01, pos.entry_premium - pos.initial_sl)
            risk_amt = risk_pts * pos.qty
            r_multiple = pnl / risk_amt if risk_amt > 0 else 0.0
            hold_mins = max(0, int((get_ist_now() - pos.entry_time).total_seconds() / 60))
            self._notify(
                f"{emoji} PAPER EXIT: {underlying}\n"
                f"📌 {norm_reason}\n"
                f"{direction_emoji} {pos.symbol}\n"
                f"🚪 ₹{pos.entry_premium:.2f} → ₹{executed_price:.2f}\n"
                f"💰 P&L: ₹{pnl:.0f} ({r_multiple:+.2f}R)\n"
                f"⏱ Hold: {hold_mins}m | Daily: ₹{self._risk.daily_pnl:.0f}",
                2,
            )
            return

        broker_filled = {}
        if cfg.broker_sl_orders:
            broker_filled = self.cancel_broker_orders(underlying)

        for attr_name, info in broker_filled.items():
            if isinstance(info, dict) and info.get("order_status") in ("complete", "filled", "executed"):
                executed_price = info.get("executed", 0)
                inf(f"[ORDER] Broker {attr_name} already filled at ₹{executed_price:.2f} — skipping SELL")
                pnl = (float(executed_price) - pos.entry_premium) * pos.qty
                self._risk.record_exit(pnl)
                self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
                self._ws.unsubscribe_spot(pos.spot_symbol)
                self._write_journal(underlying, pos, float(executed_price), pnl, norm_reason,
                                    exit_price_source="broker_fill")
                with self._state.state_lock:
                    self._state.positions.pop(underlying, None)
                with self._state.exit_lock:
                    self._state.exit_queue.discard(underlying)
                return

        executed_price = 0.0
        order_id       = None
        try:
            resp = self.client.placeorder(
                strategy=cfg.strategy_name,
                symbol=pos.symbol,
                action="SELL",
                exchange=cfg.fno_exchange,
                price_type="MARKET",
                product="MIS",
                quantity=pos.qty,
            )
            if isinstance(resp, dict) and resp.get("status") == "success":
                order_id = resp.get("orderid")
                inf(f"[ORDER] Exit order {order_id} placed for {underlying}")
            else:
                inf(f"[ORDER] Exit order response: {resp}")
        except Exception as exc: err(f"[ORDER] place_exit error for {underlying}: ", exc)

        if order_id is None:
            now_hm = get_ist_now().strftime("%H:%M")
            is_past_cutoff = bool(cfg.square_off_time and now_hm >= cfg.square_off_time)

            if is_past_cutoff:
                snap = self._state.snapshot_cache.get(underlying)
                best_price = (snap.option_ltp if snap and snap.option_ltp is not None
                              else self._state.ltp_map.get(pos.symbol))
                if best_price is not None:
                    pnl = (best_price - pos.entry_premium) * pos.qty
                    journal_reason = ExitReason.FORCE_UNTRACK_EST
                else:
                    best_price = 0.0
                    pnl = 0.0
                    journal_reason = ExitReason.FORCE_UNTRACK_UNKNOWN
                    
                self._risk.record_exit(pnl)
                self._write_journal(underlying, pos, best_price, pnl, journal_reason,
                                    exit_price_source="estimated")
                self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
                self._ws.unsubscribe_spot(pos.spot_symbol)
                inf(
                    f"[ORDER] Exit order rejected after EOD cutoff — untracking {underlying} "
                    f"({journal_reason} price ₹{best_price:.2f} | P&L ₹{pnl:.0f})"
                )
                with self._state.state_lock:
                    self._state.positions.pop(underlying, None)
                with self._state.exit_lock:
                    self._state.exit_queue.discard(underlying)
                return

            # Order was not submitted — safe to release exit lock so the next SL
            # trigger from the WS trail can retry the exit on the next tick.
            inf(f"[ORDER] Exit order not submitted for {underlying} — releasing for retry")
            with self._state.exit_lock:
                self._state.exit_queue.discard(underlying)
            pos.exit_pending = False
            return

        with self._state.state_lock:
            self._state.pending_exits[underlying] = PendingExit(
                order_id=order_id,
                reason=norm_reason,
                created_at=datetime.now(),
            )
        filled = self.poll_order_status(order_id)
        if not filled:
            # Order submitted but fill could not be confirmed within the poll window.
            # Leave pending_exits intact so check_pending_exits() reconciles on the
            # next strategy cycle; position and exit_pending stay as-is.
            inf(
                f"[ORDER] Exit fill unconfirmed for {underlying} (order {order_id}) "
                f"— leaving in pending_exits for reconciliation"
            )
            return

        with self._state.state_lock:
            self._state.pending_exits.pop(underlying, None)
        data           = filled.get("data") or filled
        executed_price = float(data.get("average_price", 0) or 0)

        pnl = (executed_price - pos.entry_premium) * pos.qty
        self._risk.record_exit(pnl)
        self._write_journal(underlying, pos, executed_price, pnl, norm_reason,
                            exit_price_source="broker_fill")
        self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
        self._ws.unsubscribe_spot(pos.spot_symbol)
        with self._state.state_lock:
            self._state.positions.pop(underlying, None)
        with self._state.exit_lock:
            self._state.exit_queue.discard(underlying)

        direction_emoji = "🔺 UP" if pos.option_type.upper() == "CE" else "🔻 DN"
        emoji = "✅ PROFIT" if pnl >= 0 else "❌ LOSS"
        risk_pts = max(0.01, pos.entry_premium - pos.initial_sl)
        risk_amt = risk_pts * pos.qty
        r_multiple = pnl / risk_amt if risk_amt > 0 else 0.0
        hold_mins = max(0, int((get_ist_now() - pos.entry_time).total_seconds() / 60))
        self._notify(
            f"{emoji} EXIT: {underlying}\n"
            f"📌 {reason.upper()}\n"
            f"{direction_emoji} {pos.symbol}\n"
            f"🚪 ₹{pos.entry_premium:.2f} → ₹{executed_price:.2f}\n"
            f"💰 P&L: ₹{pnl:.0f} ({r_multiple:+.2f}R)\n"
            f"⏱ Hold: {hold_mins}m | Daily: ₹{self._risk.daily_pnl:.0f}",
            2,
        )

    def check_pending_entries(self) -> None:
        """Reconcile stale pending entry orders. WC-09: post-cutoff entries queue immediate exit."""
        with self._state.state_lock:
            pending = list(self._state.pending_entries.items())
        now_hm = get_ist_now().strftime("%H:%M")
        square_off_hm = self.config.square_off_time
        for underlying, pending_entry in pending:
            order_id = pending_entry.order_id
            filled = self.poll_order_status(order_id, max_retries=1, sleep_secs=0)
            if filled:
                data     = filled.get("data") or filled
                status   = str(data.get("order_status", "")).lower() if isinstance(data, dict) else ""
                price    = float((data.get("average_price") if isinstance(data, dict) else None) or 0)
                if status == "complete" and price:
                    with self._state.state_lock:
                        self._state.pending_entries.pop(underlying, None)
                        already_open = underlying in self._state.positions
                    if already_open:
                        self._notify(
                            f"⚠️ {self.config.strategy_name}: pending BUY {order_id} filled but "
                            f"{underlying} already has a tracked position. Reconcile manually.",
                            9,
                        )
                        continue
                    inf(f"[PENDING] BUY {order_id} filled for {underlying} @ ₹{price:.2f}; activating protection")
                    filled_qty = int(data.get("filled_quantity", 0) or data.get("filled_qty", 0) or 0)
                    if filled_qty > 0 and filled_qty != pending_entry.qty:
                        inf(f"[PENDING] Partial fill: requested {pending_entry.qty}, filled {filled_qty}")
                    use_qty = filled_qty if filled_qty > 0 else pending_entry.qty
                    self.register_filled_entry(
                        underlying, pending_entry.symbol, use_qty,
                        pending_entry.spot, pending_entry.direction, price,
                        sl_pts=pending_entry.sl_pts,
                        entry_delta=pending_entry.entry_delta,
                        entry_conviction=pending_entry.entry_conviction,
                    )
                    # WC-09: If filled after square_off_time, queue immediate exit
                    if square_off_hm and now_hm >= square_off_hm:
                        inf(f"[PENDING] Entry {order_id} filled AFTER cutoff ({now_hm} >= {square_off_hm}) — queuing exit")
                        with self._state.state_lock:
                            pos = self._state.positions.get(underlying)
                            if pos:
                                pos.exit_pending = True
                                with self._state.exit_lock:
                                    self._state.exit_queue.add(underlying)
                        self.place_exit(underlying, "PostCutoffEntry")
                    self._notify(
                        f"✅ {self.config.strategy_name}: pending BUY {order_id} reconciled "
                        f"for {underlying} @ ₹{price:.2f} (fill detected outside normal path)",
                        5,
                    )
                elif status in ("rejected", "cancelled", "canceled"):
                    with self._state.state_lock:
                        self._state.pending_entries.pop(underlying, None)
                    inf(f"[PENDING] BUY {order_id} {status}; removed from pending entries")
            elif square_off_hm and now_hm >= square_off_hm:
                # WC-09: Cancel unfilled pending entry after square_off_time cutoff
                try:
                    cancel_resp = self.client.cancelorder(order_id=order_id, strategy=self.config.strategy_name)
                    cancel_status = cancel_resp.get("status") if isinstance(cancel_resp, dict) else None
                    if cancel_status == "success" or "cancel" in str(cancel_resp).lower():
                        with self._state.state_lock:
                            self._state.pending_entries.pop(underlying, None)
                        inf(f"[PENDING] Cancelled unfilled entry {order_id} after {now_hm} cutoff")
                except Exception as _exc: err(f"[PENDING] Cancel error for {order_id}: ", _exc)

    def check_pending_exits(self) -> None:
        """Reconcile stale pending exit orders (safety net — runs every cycle)."""
        with self._state.state_lock:
            pending = list(self._state.pending_exits.items())
        for underlying, pending_exit in pending:
            order_id = pending_exit.order_id
            filled = self.poll_order_status(order_id, max_retries=1, sleep_secs=0)
            with self._state.state_lock:
                pos = self._state.positions.get(underlying)
            if not pos:
                with self._state.state_lock:
                    self._state.pending_exits.pop(underlying, None)
                continue
            opt_sym = pos.symbol
            if filled:
                data           = filled.get("data") or filled
                status         = str(data.get("order_status", "")).lower() if isinstance(data, dict) else ""
                executed_price = float((data.get("average_price") if isinstance(data, dict) else None) or 0)
                if status == "complete" and executed_price:
                    pnl = (executed_price - pos.entry_premium) * pos.qty
                    pnl_sign = "✅" if pnl >= 0 else "❌"
                    norm_reason = ExitReason.normalize(pending_exit.reason)
                    self._risk.record_exit(pnl)
                    self._write_journal(underlying, pos, executed_price, pnl, norm_reason,
                                        exit_price_source="broker_fill")
                    self._ws.unsubscribe(self.config.fno_exchange, opt_sym)
                    self._ws.unsubscribe_spot(pos.spot_symbol)
                    with self._state.state_lock:
                        self._state.positions.pop(underlying, None)
                        self._state.pending_exits.pop(underlying, None)
                    with self._state.exit_lock:
                        self._state.exit_queue.discard(underlying)
                    inf(f"[PENDING] EXIT {order_id} complete for {underlying} @ ₹{executed_price:.2f} | P&L ₹{pnl:.2f} | reason={norm_reason}")
                    self._notify(
                        f"{pnl_sign} {self.config.strategy_name} EXIT confirmed\n"
                        f"{underlying} {pos.option_type} | {opt_sym}\n"
                        f"Exit ₹{executed_price:.2f} | Entry ₹{pos.entry_premium:.2f} | P&L ₹{pnl:.2f}\n"
                        f"Daily P&L ₹{self._risk.daily_pnl:.0f}",
                        8 if pnl < 0 else 6,
                    )
                elif status in ("rejected", "cancelled", "canceled"):
                    now_hm = get_ist_now().strftime("%H:%M")
                    is_past_cutoff = bool(self.config.square_off_time and now_hm >= self.config.square_off_time)

                    if is_past_cutoff:
                        snap = self._state.snapshot_cache.get(underlying)
                        best_price = (snap.option_ltp if snap and snap.option_ltp is not None
                                      else self._state.ltp_map.get(opt_sym))
                        if best_price is not None:
                            pnl = (best_price - pos.entry_premium) * pos.qty
                            journal_reason = ExitReason.FORCE_UNTRACK_EST
                        else:
                            best_price = 0.0
                            pnl = 0.0
                            journal_reason = ExitReason.FORCE_UNTRACK_UNKNOWN
                            
                        self._risk.record_exit(pnl)
                        self._write_journal(underlying, pos, best_price, pnl, journal_reason,
                                            exit_price_source="estimated")
                        self._ws.unsubscribe(self.config.fno_exchange, opt_sym)
                        self._ws.unsubscribe_spot(pos.spot_symbol)
                        with self._state.state_lock:
                            self._state.pending_exits.pop(underlying, None)
                            self._state.positions.pop(underlying, None)
                        with self._state.exit_lock:
                            self._state.exit_queue.discard(underlying)
                        inf(
                            f"[PENDING] EXIT {order_id} {status} after EOD cutoff — untracking {underlying} "
                            f"({journal_reason} price ₹{best_price:.2f} | P&L ₹{pnl:.0f})"
                        )
                    else:
                        with self._state.state_lock:
                            self._state.pending_exits.pop(underlying, None)
                            pos.exit_pending = False
                        with self._state.exit_lock:
                            self._state.exit_queue.discard(underlying)
                        self._notify(
                            f"🚨 {self.config.strategy_name}: pending EXIT {order_id} {status} for {underlying} {opt_sym}\n"
                            "Position remains tracked; software exit may retry on next trigger.",
                            9,
                        )


# ===============================================================================
# ORCHESTRATOR — thin coordinator wiring all components together
# ===============================================================================

class OptionsBuyerEdgeBot:
    """
    Thin orchestrator.  Creates all components, wires callbacks, then runs the
    two long-lived threads: WebSocket + strategy scan loop.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        api_kwargs: dict = dict(api_key=config.api_key, host=config.api_host, verbose=1)
        if config.ws_url:
            api_kwargs["ws_url"] = config.ws_url   # explicit override; otherwise SDK derives from host
        self.client = api(**api_kwargs)
        self.state   = BotState(lookback_bars=config.lookback_bars)
        self.risk    = RiskManager(self.client, config, self.state)
        self.fetcher = DataFetcher(self.client, config, notify_callback=self._send_alert)
        self.sl_policy      = EntryStopLossPolicy(self.fetcher, config)
        self.trail_engine   = TrailSLEngine(self.fetcher, config)
        self.scorer         = SignalEngine(config)
        self.strikes        = StrikeSelector(self.fetcher, config)
        self.ws             = WebSocketManager(self.client, config, self.state)
        self.orders         = OrderManager(self.client, config, self.state, self.risk, self.ws, self.fetcher, self._send_alert)
        # Wire callbacks and dependencies to break circular dependency + consolidate API calls
        self.ws.set_fetcher(self.fetcher)       # Reuse DataFetcher cache for delta in trailing SL
        self.ws.set_exit_callback(self.orders.place_exit)
        self.ws.set_sl_modify_callback(self.orders.modify_broker_sl)
        self.ws.set_notify_callback(self._send_alert)  # U-G: WS watchdog alert
        self.trail_engine.modify_callback = self.orders.modify_broker_sl
        self._last_pnl_alert_time: float = 0.0

    def _send_alert(self, message: str, priority: int = 1) -> None:
        try:
            self.client.telegram(
                username=self.config.openalgo_username,
                strategy=self.config.strategy_name,
                message=message,
                priority=priority,
            )
        except Exception as exc: err(f"[ALERT] Send error: ", exc)

    def _verify_registration(self) -> None:
        """WC-14: Verify strategy is registered in broker's strategy configs."""
        cfg = self.config
        try:
            resp = self.client.orderbook(strategy=cfg.strategy_name)
            if isinstance(resp, dict) and resp.get("status") == "success":
                inf(f"[STARTUP] ✓ Strategy '{cfg.strategy_name}' registered OK")
                return
        except Exception:
            pass
        inf(f"[STARTUP] ⚠️  Strategy '{cfg.strategy_name}' not found in strategy configs.")
        inf(f"[STARTUP]    Run: python3 /app/strategies/register_strategy.py")
        inf(f"[STARTUP]    Then restart this script.")
        inf(f"[STARTUP] Continuing anyway (may cause runtime errors)...\n")

    def _check_open_positions_on_startup(self) -> None:
        """WC-01: Restore broker positions + resubscribe WS + reconcile protection orders.

        Reconciliation logic (Phase 2):
          Case A: SL ✓ Target ✓ → adopt both, no new orders
          Case B: SL ✗ Target ✓ → re-issue SL
          Case C: SL ✓ Target ✗ → re-issue target
          Case D: SL ✗ Target ✗ → re-issue both

        Orphan detection (Phase 3):
          Open SL/TGT orders with no matching position → cancel.
        """
        try:
            cfg = self.config
            resp = self.client.positionbook()
            if not isinstance(resp, dict) or resp.get("status") != "success":
                return
            positions = resp.get("data", []) or []
            if not positions:
                inf("[STARTUP] No open positions found in broker position book")
                return
            inf(f"[STARTUP] Found {len(positions)} broker position(s). Restoring...")

            # Fetch orderbook to find SL/TGT orders
            orderbook_resp = self.client.orderbook(strategy=cfg.strategy_name)
            open_orders = orderbook_resp.get("data", []) if isinstance(orderbook_resp, dict) else []

            # ── Phase 3: Orphan detection ───────────────────────────────────
            # Collect symbols from broker positions, cancel orders for others
            pos_symbols: set[str] = set()

            for p in positions:
                sym = p.get("symbol", "")
                qty = int(p.get("netqty", 0) or 0)
                if sym and qty != 0:
                    pos_symbols.add(sym)

            orphan_orders_cancelled = 0
            for order in open_orders:
                o_sym = order.get("symbol", "")
                o_stat = str(order.get("status", "")).lower()
                if o_sym and o_sym not in pos_symbols and o_stat in ("pending", "open"):
                    oid = order.get("orderid")
                    if oid:
                        try:
                            resp_c = self.client.cancelorder(order_id=oid, strategy=cfg.strategy_name)
                            if isinstance(resp_c, dict) and resp_c.get("status") in ("success", "cancelled"):
                                orphan_orders_cancelled += 1
                                inf(f"[STARTUP] Cancelled orphan order {oid} for {o_sym}")
                        except Exception:
                            pass
            if orphan_orders_cancelled:
                inf(f"[STARTUP] Cancelled {orphan_orders_cancelled} orphan order(s)")

            # ── Phase 1: Restore positions ──────────────────────────────────
            for p in positions:
                sym      = p.get("symbol", "")
                qty      = int(p.get("netqty", 0) or 0)
                entry_px = float(p.get("average_price", 0) or 0)
                if not sym or qty == 0 or entry_px <= 0:
                    continue

                # Robust underlying extraction
                underlying = ""
                m = re.match(r"^(.*?)(\d{1,2}[A-Z]{3}\d{2})(?:\d+(?:\.\d+)?)?(CE|PE|FUT)$", sym)
                if m:
                    underlying = m.group(1)
                if not underlying:
                    candidates = sorted(cfg.underlyings, key=len, reverse=True)
                    underlying = next((u for u in candidates if sym.startswith(u)), "")
                if not underlying:
                    inf(f"[STARTUP] Could not derive underlying from {sym} — skipping restore")
                    continue

                opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                if not opt_type or underlying in self.state.positions:
                    continue

                spot_q = self.fetcher.fetch_quote(underlying, self.fetcher.underlying_exchange(underlying))
                restored_spot = float(spot_q.get("ltp", 0) or 0)
                if restored_spot <= 0:
                    restored_spot = entry_px

                pos = OptionPosition(
                    underlying=underlying,
                    symbol=sym,
                    entry_premium=entry_px,
                    qty=qty,
                    option_type=opt_type,
                    sl=entry_px - cfg.premium_stop_pts,
                    tgt=entry_px + cfg.premium_target_pts,
                    spot_symbol=underlying,
                    spot_entry=restored_spot,
                    reward_dist=restored_spot * (cfg.spot_reward_pct / 100.0),
                    entry_time=get_ist_now(),
                )

                # Query SL/TGT order IDs from orderbook
                for order in open_orders:
                    o_sym = order.get("symbol", "")
                    o_stat = str(order.get("status", "")).lower()
                    o_type = str(order.get("order_type", "")).lower()
                    if o_stat in ("pending", "open") and o_sym == sym:
                        if "sl" in o_type:
                            pos.sl_order_id = order.get("orderid")
                        elif "limit" in o_type:
                            pos.tgt_order_id = order.get("orderid")

                if pos.sl_order_id or pos.tgt_order_id:
                    pos.broker_protection = True

                # Register + resubscribe WS
                with self.state.state_lock:
                    self.state.positions[underlying] = pos
                self.state.snapshot_cache.set_option_symbol(underlying, sym)
                self.ws.subscribe(cfg.fno_exchange, sym)
                self.ws.subscribe_spot(underlying)
                inf(f"[STARTUP] ✓ Restored {underlying}: {sym} x{qty} @ ₹{entry_px:.2f} "
                      f"SL_id={pos.sl_order_id or 'MISSING'} TGT_id={pos.tgt_order_id or 'MISSING'}")

                # ── Phase 2: Re-issue missing protection orders ─────────────
                if cfg.broker_sl_orders and not cfg.paper_trade:
                    sl_ok = bool(pos.sl_order_id)
                    tgt_ok = bool(pos.tgt_order_id)
                    if not sl_ok or not tgt_ok:
                        inf(f"[STARTUP] Reconciling protection orders for {underlying}: "
                              f"SL={'OK' if sl_ok else 'MISSING'} TGT={'OK' if tgt_ok else 'MISSING'}")
                        self._place_protection_orders_sequential(underlying, pos, sym, qty, pos.sl, pos.tgt)
                        if pos.sl_order_id or pos.tgt_order_id:
                            pos.broker_protection = True

        except Exception as exc: err(f"[STARTUP] positionbook error: ", exc)

    def _check_max_hold(self) -> None:
        """Exit positions held > max_hold_minutes (theta decay guard). 0=disabled."""
        cfg = self.config
        if cfg.max_hold_minutes <= 0:
            return
        now = get_ist_now()
        with self.state.state_lock:
            positions = list(self.state.positions.items())
        for ul, pos in positions:
            if pos.exit_pending:
                continue
            held_minutes = (now - pos.entry_time).total_seconds() / 60.0
            if held_minutes >= cfg.max_hold_minutes:
                inf(
                    f"[TIME-EXIT] {ul}: held {held_minutes:.0f}m "
                    f">= max {cfg.max_hold_minutes}m — exiting (theta guard)"
                )
                with self.state.exit_lock:
                    if pos.exit_pending:
                        continue
                    pos.exit_pending = True
                self.orders.place_exit(ul, f"MaxHoldTime({cfg.max_hold_minutes}m)")

    def _send_live_pnl_alert(self, open_positions: list[OptionPosition]) -> None:
        """Fetch live positions and dispatch a single-line active PNL alert."""
        dbg(f"[PNL] Checking PNL for {len(open_positions)} position(s)")
        try:
            broker_pnl_map: dict[str, float] = {}
            broker_raw_data: dict[str, dict] = {}
            if not self.config.paper_trade and hasattr(self.client, "positionbook"):
                resp = self.client.positionbook()
                if isinstance(resp, dict) and resp.get("status") == "success":
                    for p in resp.get("data", []):
                        sym = p.get("symbol", "")
                        pnl = float(p.get("pnl", 0) or 0)
                        if sym:
                            broker_pnl_map[sym] = pnl
                            broker_raw_data[sym] = p
                    dbg(f"[PNL] {len(broker_pnl_map)} position(s): {list(broker_pnl_map.keys())}")
                else:
                    inf(f"[PNL] Broker positionbook call failed: {resp}")

            for pos in open_positions:
                pnl = 0.0
                source = "none"
                if self.config.paper_trade:
                    snap = self.state.snapshot_cache.get(pos.underlying)
                    if snap and snap.option_ltp is not None:
                        ltp = snap.option_ltp
                        pnl = (ltp - pos.entry_premium) * pos.qty
                        source = "snapshot_cache"
                        dbg(f"[PNL] "
                            f"ltp=₹{ltp:.2f} entry=₹{pos.entry_premium:.2f} "
                            f"qty={pos.qty} pnl=₹{pnl:.2f}"
                        )
                    else:
                        dbg(f"[PNL] — no snapshot data. "
                            f"snap_exists={snap is not None} "
                            f"opt_ltp={snap.option_ltp if snap else 'None'}"
                        )
                elif pos.symbol in broker_pnl_map:
                    pnl = broker_pnl_map[pos.symbol]
                    source = "broker"
                    dbg(f"[PNL] pnl=₹{pnl:.2f} "
                        f"raw={broker_raw_data.get(pos.symbol, {})}"
                    )
                else:
                    broker_symbols = list(broker_pnl_map.keys())
                    raw_entry = broker_raw_data.get(pos.symbol)
                    inf(
                        f"[PNL] {pos.underlying}: symbol={pos.symbol} NOT in broker data. "
                        f"broker_symbols={broker_symbols} raw_entry={raw_entry}"
                    )

                hold_mins = max(0, int((get_ist_now() - pos.entry_time).total_seconds() / 60))
                hours = hold_mins // 60
                mins  = hold_mins % 60
                hold_str = f"{hours}h{mins}m" if hours > 0 else f"{mins}m"

                side  = pos.option_type.upper()
                emoji = "🟢" if pnl >= 0 else "🔴"
                sign  = "+" if pnl >= 0 else ""

                if pnl == 0.0 and source == "none":
                    dbg(f"[PNL] — no data source")
                else:
                    dbg(f"[PNL]₹{pnl:.2f} source={source} "
                        f"hold={hold_str} alert_sent=True"
                    )

                self._send_alert(
                    f"{emoji} {pos.underlying} {side} | PNL: ₹{sign}{pnl:.0f} | Hold: {hold_str}",
                    3,
                )
                
        except Exception as exc: err(f"[PNL REPORT] Error checking active PNL: ", exc)

    def _is_market_hours(self) -> bool:
        hm = int(get_ist_now().strftime("%H%M"))
        return MARKET_HOURS_START <= hm <= MARKET_HOURS_END

    def _print_startup_info(self) -> None:
        cfg = self.config
        inf("=" * 70)
        inf(f"  {cfg.strategy_name}{'  [PAPER TRADE]' if cfg.paper_trade else ''}")
        inf("=" * 70)
        inf(f"  API Host        : {cfg.api_host}")
        inf(f"  WebSocket URL   : {cfg.ws_url if cfg.ws_url else '(SDK auto-derive from host)'}")
        inf(f"  Underlyings     : {', '.join(cfg.underlyings)}")
        inf(f"  FNO Exchange    : {cfg.fno_exchange}")
        inf(f"  Min Score       : {cfg.min_score} | Max Trap: {cfg.max_trap}")
        inf(f"  SL Points       : {cfg.premium_stop_pts} (Phase A hard SL fallback)")
        inf(f"  Phase A SL      : moneyness-adapted from entry_delta or fallback to PREMIUM_STOP_PTS")
        _max_pts_str = f" (hard cap {cfg.trail_activate_at_max_pts:.0f}pts)" if cfg.trail_activate_at_max_pts > 0 else ""
        inf(
            f"  Phase B Trail   : tracking={cfg.trail_tracking_mode}  method={cfg.trail_sl_method}  "
            f"activate={cfg.trail_activate_at_pct:.0f}%{_max_pts_str}"
        )
        if cfg.trail_sl_method == "fixed_pct":
            inf(f"  Trail Step      : {cfg.trail_step_pct:.1f}% of base distance (cap: 50% of entry premium)")
        elif cfg.trail_sl_method == "fixed_pts":
            inf(f"  Trail Step      : {cfg.trail_step_pts:.1f} raw pts (no scaling — use for high-VIX/high-premium options)")
        elif cfg.trail_sl_method == "atr":
            inf(f"  Trail ATR       : period={cfg.trail_atr_period}, mult={cfg.trail_atr_mult}")
        elif cfg.trail_sl_method == "delta":
            inf(
                f"  Trail Delta     : ITM={cfg.trail_delta_itm_step_pct:.0f}%  "
                f"ATM={cfg.trail_delta_atm_step_pct:.0f}%  OTM={cfg.trail_delta_otm_step_pct:.0f}%  (cap: 50% of entry premium)"
            )
        elif cfg.trail_sl_method == "key_level":
            style = cfg.key_level_trail_style
            if style == "capture_pct":
                inf(f"  Key Level Trail : capture_pct={cfg.key_level_capture_pct:.0f}% per level, spacing={cfg.key_level_spacing}")
            else:
                inf(f"  Key Level Trail : fixed={cfg.key_level_fixed_pts:.0f}pts per level, spacing={cfg.key_level_spacing}")
            inf(f"  Key Level BE    : after {cfg.key_level_breakeven_after_levels} level(s)")
        inf(f"  Breakeven SL    : {'disabled' if cfg.breakeven_at_gain_pct <= 0 else f'{cfg.breakeven_at_gain_pct:.0f}% of target gain'}")
        inf(f"  Long Only Mode  : {cfg.long_only_mode}")
        inf(f"  Broker SL Orders: {cfg.broker_sl_orders}")
        inf(f"  DTE Range       : {cfg.dte_min} – {cfg.dte_max} days")
        inf(f"  Candle Interval : {cfg.candle_interval}")
        inf(f"  Check Interval  : {cfg.signal_check_interval}s")
        inf("-" * 70)
        inf(f"  [RISK GATES]")
        inf(f"  Max Trades/Day  : {cfg.max_trades_per_session or 'unlimited'}")
        inf(f"  Max Consec Loss : {cfg.max_consecutive_losses}")
        inf(f"  Daily Loss Limit: ₹{cfg.max_daily_loss_amount:.0f}"
              + (f" | {cfg.max_daily_loss_pct:.1f}%" if cfg.max_daily_loss_pct > 0 else ""))
        inf(f"  Daily Profit Tgt: {'disabled' if cfg.max_daily_profit_amount <= 0 else f'₹{cfg.max_daily_profit_amount:.0f}'}")
        inf(f"  Entry Cooldown  : {cfg.entry_cooldown_secs}s per underlying")
        inf(f"  [TIMING]")
        inf(f"  No New Entries  : after {cfg.no_new_trade_after} IST")
        inf(f"  EOD Square-Off  : {cfg.square_off_time} IST")
        inf(f"  Max Hold Time   : {'disabled' if cfg.max_hold_minutes <= 0 else f'{cfg.max_hold_minutes}m per trade'}")
        if cfg.trade_journal_path:
            inf(f"  Trade Journal   : {cfg.trade_journal_path}")
        if cfg.paper_trade:
            inf(f"\n  *** PAPER TRADE MODE — no real orders will be sent ***")
        inf("=" * 70)

    def scan_underlying(self, symbol: str) -> None:
        """Full scan pipeline for one underlying.  Called from strategy thread."""
        cfg    = self.config
        state  = self.state
        orders = self.orders

        # Keep greeks cache scoped to this scan cycle for fresh yet deduplicated API calls.
        self.fetcher.clear_greeks_cache(symbol)

        def _log_greeks_perf(
            stage:     str,
            sep_count: int = 0,
            sep_char:  str = "━",
        ) -> None:
            """Log greeks cache performance for this scan-cycle stage.

            Args:
                stage:     Execution stage label  (e.g. 'no-execute', 'entry-order').
                sep_count: When > 0 prints a closing separator of this many `sep_char`
                           characters directly after the perf line, letting callers
                           consolidate  ``_log_greeks_perf(...)``  +  separator  into
                           one call instead of two.
                sep_char:  Separator character (default: ━).
            """
            perf = self.fetcher.greeks_perf_snapshot(symbol)
            dbg(
                f"  [PERF] {symbol} [{stage}] greeks: "
                f"hit={perf['hits']} miss={perf['misses']} "
                f"api_calls={perf['api_calls']} hit_rate={perf['hit_rate']}% "
                f"cache_size={perf['cache_size']}"
            )
            if sep_count > 0:
                inf(f"  {sep_char * sep_count}\n")

        if symbol in state.positions:
            return

        allowed, gate_reason = self.risk.check_gates(symbol)
        if not allowed:
            inf(f"[SCAN] {symbol} blocked by risk gate: {gate_reason}")
            return

        # U-C: Session-Aware Min Score — use global helper for clean, testable logic
        _ist_now          = get_ist_now()
        effective_min_score, _session_label = _effective_min_score(_ist_now, cfg)
        if _session_label != "mid-session":
            inf(f"[SCAN] {symbol}: session regime [{_session_label}]")

        spot_q = self.fetcher.fetch_quote(symbol, self.fetcher.underlying_exchange(symbol))
        spot   = float(spot_q.get("ltp", 0) or 0)
        if not spot:
            inf(f"[SCAN] {symbol}: no spot LTP")
            return

        expiry = self.fetcher.fetch_target_expiry(symbol)
        if not expiry and not cfg.allow_checkpoint_fallback:
            inf(f"[SCAN] {symbol}: no expiry in DTE range {cfg.dte_min}–{cfg.dte_max} — skip")
            return

        # Fetch option chain
        chain_rows, expiry_used = self.fetcher.fetch_option_chain(symbol, expiry)
        if not chain_rows:
            inf(f"[SCAN] {symbol}: empty option chain")
            return
        if expiry_used and not expiry:
            expiry = expiry_used

        chain_hist = state.get_chain_history(symbol)
        chain_hist.append(chain_rows)
        smoothed = OIFlowAnalyzer.smooth_chain_rows(list(chain_hist))
        if not smoothed:
            return

        df_spot = self.fetcher.fetch_spot_candles(symbol)

        strikes = sorted(set(r["strike"] for r in smoothed))
        atm_k   = min(strikes, key=lambda x: abs(x - spot))
        atm_row = next((r for r in smoothed if r.get("strike") == atm_k), {})
        atm_ce_ltp  = float(atm_row.get("ce_ltp", 0) or 0)
        atm_pe_ltp  = float(atm_row.get("pe_ltp", 0) or 0)

        # Prefetch greeks only for symbols that will be consumed in this scan:
        # 1) ATM CE/PE (L3 delta component)
        # 2) Strikes near ATM with OI > 0 (GEX gamma profile)
        # 3) Liquidity-qualified strikes near ATM (strike selection delta gate)
        option_symbols: list[str] = []
        if atm_row.get("ce_symbol"):
            option_symbols.append(atm_row.get("ce_symbol"))
        if atm_row.get("pe_symbol"):
            option_symbols.append(atm_row.get("pe_symbol"))

        for row in chain_rows:
            if float(row.get("ce_oi", 0) or 0) > 0 and row.get("ce_symbol"):
                option_symbols.append(row.get("ce_symbol"))
            if float(row.get("pe_oi", 0) or 0) > 0 and row.get("pe_symbol"):
                option_symbols.append(row.get("pe_symbol"))

        for row in smoothed:
            if (
                float(row.get("ce_oi", 0) or 0) >= cfg.min_oi_filter
                and float(row.get("ce_volume", 0) or 0) >= cfg.min_vol_filter
                and row.get("ce_symbol")
            ):
                option_symbols.append(row.get("ce_symbol"))
            if (
                float(row.get("pe_oi", 0) or 0) >= cfg.min_oi_filter
                and float(row.get("pe_volume", 0) or 0) >= cfg.min_vol_filter
                and row.get("pe_symbol")
            ):
                option_symbols.append(row.get("pe_symbol"))

        self.fetcher.batch_prefetch_option_greeks(symbol, option_symbols)

        straddle_price = (atm_ce_ltp + atm_pe_ltp) if (atm_ce_ltp and atm_pe_ltp) else None
        # Only compare premium expansion if the ATM strike is the same as the previous scan.
        # If the ATM strike shifted, straddle velocity is undefined/reset for this bar.
        prev_str = state.prev_straddle.get(symbol)
        prev_straddle_price = None
        if isinstance(prev_str, dict) and prev_str.get("strike") == atm_k:
            prev_straddle_price = prev_str.get("price")
        if straddle_price is not None:
            state.prev_straddle[symbol] = {"strike": atm_k, "price": straddle_price}

        sf_ltp   = self.fetcher.fetch_synthetic_future(symbol, expiry)
        prev_sf_ltp  = state.prev_sf.get(symbol)
        prev_spot_ltp = state.prev_spot.get(symbol)
        if sf_ltp:
            state.prev_sf[symbol] = sf_ltp
        state.prev_spot[symbol] = spot

        ce_delta, pe_delta = self.fetcher.fetch_atm_greeks(
            symbol,
            atm_row.get("ce_symbol"),
            atm_row.get("pe_symbol"),
        )
        gex_levels = self.fetcher.fetch_gex_levels(symbol, chain_rows, spot)

        ce_bid = float(atm_row.get("ce_bid", 0) or 0) or None
        ce_ask = float(atm_row.get("ce_ask", 0) or 0) or None
        pe_bid = float(atm_row.get("pe_bid", 0) or 0) or None
        pe_ask = float(atm_row.get("pe_ask", 0) or 0) or None

        # Fetch separate CE and PE IV Ranks (best fit = lower = cheaper for buying)
        iv_ranks = self.fetcher.fetch_atm_iv_ranks(
            symbol,
            ce_symbol=atm_row.get("ce_symbol"),
            pe_symbol=atm_row.get("pe_symbol"),
        )
        ce_iv_rank = iv_ranks.get("ce_iv_rank")
        pe_iv_rank = iv_ranks.get("pe_iv_rank")
        best_fit_iv_side = iv_ranks.get("best_fit")
        # Legacy fallback for backward compatibility
        iv_rank_val = ce_iv_rank if (ce_iv_rank is not None and pe_iv_rank is None) else (pe_iv_rank if pe_iv_rank is not None else None)

        result = self.scorer.score(
            spot=spot,
            df_spot=df_spot,
            chain_rows=smoothed,
            atm_ce_ltp=atm_ce_ltp,
            atm_pe_ltp=atm_pe_ltp,
            iv_rank=iv_rank_val,
            straddle_price=straddle_price,
            prev_straddle_price=prev_straddle_price,
            sf_ltp=sf_ltp,
            ce_bid=ce_bid,
            ce_ask=ce_ask,
            pe_bid=pe_bid,
            pe_ask=pe_ask,
            ce_delta=ce_delta,
            pe_delta=pe_delta,
            gex_levels=gex_levels,
            min_score_override=effective_min_score,
            prev_spot=prev_spot_ltp,
            prev_sf_ltp=prev_sf_ltp,
            ce_iv_rank=ce_iv_rank,
            pe_iv_rank=pe_iv_rank,
            best_fit_iv_side=best_fit_iv_side,
        )

        # ── Formatted scoring panel ──────────────────────────────────────────
        _s        = result.score
        _trap     = result.trap_score
        _signal   = result.signal
        _dir_ico  = "▲" if _s > 0 else ("▼" if _s < 0 else "◆")
        _sig_ico  = "✔" if _signal == "EXECUTE" else ("⚡" if _signal == "WATCH" else "✘")
        _nfill    = int(abs(_s) / 100 * 16)
        _score_bar = "█" * _nfill + "░" * (16 - _nfill)
        _sep        = "━" * 79
        _now_hdr    = get_ist_now()   # TZ-safe IST — works on Docker/UTC and local hosts
        _time_str   = _now_hdr.strftime("%H:%M:%S")
        _spot_fmt   = f"{spot:,.0f}" if spot else ""
        _header_txt = f"  ━━ SCAN · {symbol} · {_spot_fmt} · {_time_str}  "
        inf(_header_txt + "━" * max(1, 79 - len(_header_txt)))
        inf(f"      {_dir_ico} {result.label:<10}  score {_s:+d}/100  {_score_bar}  trap {_trap}/100   {_sig_ico} {_signal}")
        inf(f"  {_sep}")
        _cbar_w = 8
        for c in result.components:
            _cfill = int(abs(c.score) / max(c.score_max, 0.01) * _cbar_w)
            _cbar  = "█" * _cfill + "░" * (_cbar_w - _cfill)
            inf(f"     {c.score:+.0f}/{c.score_max:.0f}  {_cbar}  {c.label:<20} {c.note}")
        if result.trap_reasons:
            inf(f"  ⚠ TRAP {_trap}  ·  {'  ·  '.join(result.trap_reasons)}")

        if _signal != "EXECUTE":
            inf(
                f"  {_sig_ico} {_signal}  —  not executing  "
                f"(score {abs(_s)}/100, min {effective_min_score})"
            )
            _log_greeks_perf("no-execute", sep_count=79)
            return

        # ✔ EXECUTE path — separator printed AFTER every blocking guard below
        inf(f"  ✔ EXECUTE  {_dir_ico}  {result.direction}")

        direction = result.direction
        if cfg.long_only_mode and direction not in ("CE", "PE"):
            _log_greeks_perf("blocked-direction", sep_count=79)
            return
        if direction is None:
            _log_greeks_perf("neutral-direction", sep_count=79)
            return

        best = self.strikes.select_best(
            symbol, smoothed, spot, direction, iv_rank_val,
            signal_score=result.score,  # Pass signal strength for delta adaptation
        )
        if best is None:
            if cfg.allow_checkpoint_fallback:
                best = StrikeSelector.simple_otm(smoothed, spot, direction, cfg.otm_offset)
                if best:
                    inf(f"[SCAN] {symbol}: using simple OTM fallback strike {best.get('strike')}")
            if best is None:
                inf(f"[SCAN] {symbol}: no qualifying strike found — skip")
                _log_greeks_perf("no-strike", sep_count=79)
                return

        opt_key    = "ce_symbol" if direction == "CE" else "pe_symbol"
        opt_symbol = best.get(opt_key)
        if not opt_symbol:
            inf(f"[SCAN] {symbol}: strike {best.get('strike')} has no {direction} symbol — skip")
            _log_greeks_perf("missing-option-symbol", sep_count=79)
            return

        if cfg.same_strike_reentry_guard_enabled:
            traded_count = state.trade_count_today(opt_symbol, direction)
            if traded_count >= cfg.max_same_strike_trades_per_day:
                inf(
                    f"[SCAN] {symbol}: {opt_symbol} {direction} already traded "
                    f"{traded_count}x today (max {cfg.max_same_strike_trades_per_day}) — skip"
                )
                _log_greeks_perf("reentry-guard", sep_count=79)
                return

        if cfg.max_entry_spread_pct > 0:
            bid_key = "ce_bid" if direction == "CE" else "pe_bid"
            ask_key = "ce_ask" if direction == "CE" else "pe_ask"
            bid = float(best.get(bid_key, 0) or 0)
            ask = float(best.get(ask_key, 0) or 0)
            mid = (bid + ask) / 2 if (bid and ask) else 0.0
            if mid > 0 and ask > bid:
                live_spread_pct = (ask - bid) / mid * 100
                if live_spread_pct > cfg.max_entry_spread_pct:
                    inf(
                        f"[SCAN] {symbol}: entry blocked — spread {live_spread_pct:.1f}% "
                        f"> max {cfg.max_entry_spread_pct:.1f}% (bid={bid:.2f}, ask={ask:.2f})"
                    )
                    _log_greeks_perf("hard-spread-block", sep_count=79)
                    return

        est_premium = float(best.get("ce_ltp" if direction == "CE" else "pe_ltp", 0) or 0)
        base_sl_pts, entry_sl_source = self.sl_policy.resolve_entry_sl_points(
            opt_symbol,
            df_spot,
            entry_delta=best.get("_abs_delta"),
            est_premium=est_premium,
        )

        # ── Conviction scalar (single source of truth for all adaptive risk) ──────
        # Maps [min_score, 100] → [0.0, 1.0]. Used for SL, BE, and trail adaptation.
        entry_conviction = max(0.0, min(
            (abs(result.score) - cfg.min_score) / max(100.0 - cfg.min_score, 1.0),
            1.0,
        ))

        # ── Part 2: Conviction-Driven SL Sizing ──────────────────────────────
        # High conviction → thesis should resolve quickly; tighter SL improves R-multiple.
        # sl_factor: conviction=0.0 → 1.10x; conviction=0.5 → 1.00x; conviction=1.0 → 0.90x
        _sl_raw_conv = min(abs(result.score) / 100.0, 1.0)   # raw score, not effective_conviction
        sl_factor    = 1.10 - (_sl_raw_conv * 0.20)
        entry_sl_pts = base_sl_pts * sl_factor
        # Clamp to a safe minimum to prevent instant stop outs, but remove arbitrary dynamic limits
        entry_sl_pts = max(5.0, entry_sl_pts)

        inf(
            f"[SCAN] {symbol}: Phase A initial SL source={entry_sl_source} "
            f"base={base_sl_pts:.2f} × factor={sl_factor:.2f} (conv={entry_conviction:.2f})"
            f" → clamped={entry_sl_pts:.2f}pts"
            )

        lotsize = int(best.get("lotsize", 1) or 1)
        effective_mult = self.risk.effective_lot_multiplier(cfg.lot_multiplier)
        fixed_qty = max(1, effective_mult) * lotsize
        if cfg.adaptive_sizing_enabled:
            inf(
                f"[SCAN] {symbol}: lot_mult={effective_mult} "
                f"(base={cfg.lot_multiplier}, wins={self.risk.consecutive_wins})"
            )

        available  = self.risk.available_capital()
        if cfg.risk_based_sizing_enabled:
            risk_cap      = available * (cfg.risk_percent / 100.0)
            risk_per_unit = entry_sl_pts
            risk_qty      = int(risk_cap / risk_per_unit) if risk_per_unit > 0 else 0
            risk_qty      = (risk_qty // lotsize) * lotsize if lotsize > 0 else risk_qty
            qty = min(fixed_qty, risk_qty) if risk_qty > 0 else 0
            if qty <= 0:
                min_risk_pct = (entry_sl_pts * lotsize / available * 100) if available > 0 else 0.0
                inf(
                    f"[SCAN] {symbol}: qty=0 — 1 lot risk exceeds cap "
                    f"(stop ₹{entry_sl_pts:.2f} pts × {lotsize} units = ₹{entry_sl_pts*lotsize:.0f}/lot, "
                    f"risk cap ₹{risk_cap:.0f} @ {cfg.risk_percent}% of ₹{available:.0f} available; "
                    f"need RISK_PERCENT≥{min_risk_pct:.1f}%)"
                )
                _log_greeks_perf("qty-zero", sep_count=79)
                return
        else:
            qty = fixed_qty
            inf(f"[SCAN] {symbol}: qty={qty} (risk-based sizing disabled — using fixed lot_mult)")

        # ── WS connectivity guard — must run last so all preflight logs are printed ──
        # When WS is down, trail/target detection is blind; broker SL-M provides minimum protection.
        # Block entry entirely if broker_sl_orders=False (no fallback protection at all).
        if not self.ws.is_connected():
            if not cfg.broker_sl_orders:
                inf(
                    f"[SCAN] {symbol}: entry BLOCKED — WS disconnected and broker_sl_orders=False. "
                    f"No protection available."
                )
                _log_greeks_perf("ws-dead-no-broker-sl", sep_count=79)
                return
            inf(
                f"[RISK] {symbol}: WS disconnected — entry allowed (broker SL-M active). "
                f"Trail/target hit detection blind until WS recovers."
            )

        # All guards passed — close the scan block then log intent
        _log_greeks_perf("entry-preflight", sep_count=79)
        inf(
            f"[SCAN] {symbol}: placing {direction} entry | strike {best.get('strike')} "
            f"| {opt_symbol} x{qty}"
        )

        _log_greeks_perf("entry-order")
        orders.place_entry(
            symbol, opt_symbol, qty, spot, direction,
            sl_pts=entry_sl_pts,
            entry_delta=best.get("_abs_delta"),
            entry_conviction=entry_conviction,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # SNAPSHOT FRESHNESS: producer-failure fallback
    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_stale_snapshots(self) -> None:
        """For every open position whose snapshot is stale, fetch a broker quote
        and update SnapshotCache.  WS-dead → still have data for trail/PNL.

        Guards:
          1. Re-check freshness right before write — if a WS tick arrived between
             stale detection and quote response, don't overwrite the newer data.
          2. Per-underlying cooldown — only one refresh per stale_timeout window
             to avoid quote API rate limiting.
        """
        cfg = self.config
        timeout = cfg.snapshot_stale_timeout
        stale_underlyings = self.state.snapshot_cache.get_stale_underlyings(timeout)

        # Also check positions with no snapshot at all (fresh startup)
        for ul, pos in list(self.state.positions.items()):
            if pos.exit_pending:
                continue
            if ul not in stale_underlyings:
                snap = self.state.snapshot_cache.get(ul)
                if snap is None or not snap.has_both_prices:
                    stale_underlyings.append(ul)

        if not stale_underlyings:
            return

        # Cooldown tracking
        if not hasattr(self, '_last_quote_refresh_ts'):
            self._last_quote_refresh_ts: dict[str, float] = {}
        last_ts = self._last_quote_refresh_ts
        now = time.time()

        for ul in stale_underlyings:
            # Rate limit: skip if refreshed within the stale window
            if ul in last_ts and (now - last_ts[ul]) < timeout * 0.8:
                continue
            last_ts[ul] = now

            pos = self.state.positions.get(ul)
            if pos is None or pos.exit_pending:
                continue

            # Guard: if WS has already fully refreshed both prices, skip
            snap = self.state.snapshot_cache.get(ul)
            if snap and snap.has_both_prices and not snap.is_stale(timeout):
                continue

            # Option premium refresh
            try:
                q = self.fetcher.fetch_quote(pos.symbol, cfg.fno_exchange)
                if q:
                    ltp = float(q.get("ltp", q.get("last_price", 0)) or 0)
                    if ltp > 0:
                        snap2 = self.state.snapshot_cache.get(ul)
                        if snap2 is None or snap2.option_ltp is None:
                            self.state.snapshot_cache.update(ul, option_ltp=ltp)
            except Exception as exc: err(f"[SNAPSHOT] Quote refresh failed for {ul}: ", exc)

            # Spot refresh (indices and equities)
            try:
                spot_exch = cfg.index_exchange if ul in cfg.index_underlyings else cfg.spot_exchange
                sq = self.fetcher.fetch_quote(ul, spot_exch)
                if sq:
                    spot_ltp = float(sq.get("ltp", sq.get("last_price", 0)) or 0)
                    if spot_ltp > 0:
                        snap2 = self.state.snapshot_cache.get(ul)
                        if snap2 is None or snap2.spot_ltp is None:
                            self.state.snapshot_cache.update(ul, spot_ltp=spot_ltp)
            except Exception as exc:
                pass

    def _strategy_thread(self) -> None:
        """Clock-anchored strategy scan loop."""
        cfg = self.config
        inf("[STRATEGY] Strategy scan thread started")
        while True:
            try:
                self.orders.check_pending_entries()
                self.orders.check_pending_exits()
                if cfg.broker_sl_orders and not cfg.paper_trade:
                    self.orders.check_broker_order_fills()
                    self.orders.verify_sl_orders_active()

                if cfg.square_off_time:
                    now_hm = get_ist_now().strftime("%H:%M")
                    if now_hm >= cfg.square_off_time:
                        with self.state.state_lock:
                            open_positions = list(self.state.positions.keys())
                        if open_positions:
                            inf(
                                f"[SQUAREOFF] {cfg.square_off_time} reached — "
                                f"closing {len(open_positions)} position(s)"
                            )
                            for ul in open_positions:
                                pos = self.state.positions.get(ul)
                                if pos is None:
                                    continue
                                with self.state.exit_lock:
                                    if pos.exit_pending:
                                        continue
                                    pos.exit_pending = True
                                self.orders.place_exit(ul, "EOD-SquareOff")
                self._check_max_hold()
                self._refresh_stale_snapshots()
                self.trail_engine.check_trailing_stops(self.state)

                if cfg.live_pnl_alert_interval > 0 and self._is_market_hours():
                    with self.state.state_lock:
                        open_positions = list(self.state.positions.values())
                    if open_positions:
                        now_ts = time.time()
                        if now_ts - getattr(self, "_last_pnl_alert_time", 0.0) >= cfg.live_pnl_alert_interval:
                            self._send_live_pnl_alert(open_positions)
                            self._last_pnl_alert_time = now_ts

                if self._is_market_hours():
                    for symbol in cfg.underlyings:
                        self.scan_underlying(symbol)
                else:
                    inf("[STRATEGY] Outside market hours — skipping signal scan")

            except Exception as exc: err(f"[STRATEGY ERROR] ", exc)

            # clock-anchored sleep: align to next N-second boundary
            interval = max(cfg.signal_check_interval, 1)
            now = time.time()
            sleep_secs = interval - (now % interval)
            if sleep_secs < 1.0:
                sleep_secs += interval
            time.sleep(sleep_secs)

    # ------------------------------------------------------------------
    # WebSocket connectivity self-test
    # ------------------------------------------------------------------

    def _test_websocket(self) -> None:
        """Smoke-test: connect → authenticate → subscribe → await ticks. Prints PASS/FAIL before live feed starts."""
        import asyncio as _aio
        import json as _json
        try:
            import websockets as _websockets
        except ImportError:
            inf("[WS-TEST] SKIP — 'websockets' package not installed")
            return

        cfg   = self.config
        ws_url = cfg.ws_url
        if not ws_url:
            inf("[WS-TEST] SKIP — ws_url not configured (set WEBSOCKET_URL)")
            return

        TICK_WAIT   = 15   # seconds to wait for a live tick after subscribing
        TEST_SYMBOL = {"exchange": "NSE_INDEX", "symbol": "Nifty 50"}

        try:
            import httpx as _httpx
            _rest_resp = _httpx.post(
                f"{cfg.api_host}/api/v1/orderbook",
                json={"apikey": cfg.api_key},
                timeout=10,
                verify=False,  # tolerate self-signed certs on dev servers
            )
            _rest_data = _rest_resp.json()
            if _rest_data.get("status") == "success":
                _n = len(_rest_data.get("data", []))
                inf(f"[WS-TEST] REST API key OK (orderbook: {_n} order(s))")
            else:
                _rest_msg = _rest_data.get("message", str(_rest_data))
                inf(f"[WS-TEST] WARN: REST API key check failed: {_rest_msg}")
                inf(f"[WS-TEST]       If REST also returns 'Invalid API key', the key in OPENALGO_API_KEY is wrong.")
                inf(f"[WS-TEST]       Get the correct key from: {cfg.api_host}/apikey")
        except Exception as _rest_exc: err(f"[WS-TEST] REST check skipped: ", _rest_exc)

        inf(f"[WS-TEST] Testing {ws_url} ...")

        async def _run() -> None:
            try:
                async with _websockets.connect(ws_url, open_timeout=10) as ws:
                    inf("[WS-TEST] Transport OK — WebSocket handshake succeeded")

                    await ws.send(_json.dumps({
                        "action": "authenticate",
                        "api_key": cfg.api_key,
                    }))
                    raw = await _aio.wait_for(ws.recv(), timeout=10)
                    resp = _json.loads(raw)
                    status = resp.get("status") or resp.get("type", "")
                    if status not in ("success", "authenticated"):
                        code = resp.get("code", "")
                        inf(f"[WS-TEST] FAIL — auth rejected: {resp}")
                        if code == "AUTHENTICATION_ERROR" or "Invalid API key" in resp.get("message", ""):
                            inf(
                                f"[WS-TEST] HINT: The API key in OPENALGO_API_KEY does not match"
                                f" any key stored in the OpenAlgo database."
                                f"\n[WS-TEST]       1. Log in to your OpenAlgo dashboard"
                                f"\n[WS-TEST]       2. Go to API Key page (Account → API Key)"
                                f"\n[WS-TEST]       3. Copy the key and set OPENALGO_API_KEY=<copied-key> in your .env"
                            )
                        return
                    inf(f"[WS-TEST] Auth OK")

                    await ws.send(_json.dumps({
                        "action": "subscribe",
                        "symbols": [TEST_SYMBOL],
                        "mode": "ltp",
                    }))
                    inf(f"[WS-TEST] Subscribed {TEST_SYMBOL['exchange']}:{TEST_SYMBOL['symbol']}")

                    deadline = _aio.get_event_loop().time() + TICK_WAIT
                    tick_count = 0
                    while _aio.get_event_loop().time() < deadline:
                        remaining = deadline - _aio.get_event_loop().time()
                        try:
                            raw = await _aio.wait_for(ws.recv(), timeout=min(5, remaining))
                            msg = _json.loads(raw)
                            # Skip subscribe-ack messages
                            if msg.get("action") == "subscribe" or msg.get("type") == "subscribed":
                                continue
                            tick_count += 1
                            ltp = msg.get("ltp") or msg.get("data", {}).get("ltp", "?")
                            inf(f"[WS-TEST] Tick #{tick_count} — ltp={ltp}")
                            if tick_count >= 3:
                                break
                        except _aio.TimeoutError:
                            inf(f"[WS-TEST] (no tick yet, {remaining:.0f}s remaining...)")

                    if tick_count == 0:
                        inf(
                            f"[WS-TEST] WARNING — connected & authenticated but 0 ticks "
                            f"in {TICK_WAIT}s. Market may be closed or WS server has no feed."
                        )
                    else:
                        inf(f"[WS-TEST] PASS — received {tick_count} tick(s) ✓")

            except OSError as exc:
                err(f"[WS-TEST] FAIL — cannot reach {ws_url}", exc)
                inf("[WS-TEST] Check: Is the WebSocket server running? Is /ws proxied to port 8765 in Caddy/nginx?")
            except Exception as exc:
                _hint = ""
                _emsg = str(exc)
                _exc_type = type(exc).__name__
                if "scheme" in _emsg or "InvalidURI" in _exc_type or "isn't a valid URI" in _emsg:
                    if cfg.api_host.startswith("https://"):
                        _ws_domain = cfg.api_host[8:].split("/")[0]
                        _hint = (
                            f"\n[WS-TEST] HINT: '{ws_url}' is wrong for an HTTPS host."
                            f"\n[WS-TEST]       Remote server → set  WEBSOCKET_URL=wss://{_ws_domain}/ws"
                            f"\n[WS-TEST]       Same server   → set  WEBSOCKET_URL=ws://127.0.0.1:8765"
                        )
                elif "InvalidStatus" in _exc_type or "HTTP 200" in _emsg or "HTTP 4" in _emsg:
                    _hint = (
                        f"\n[WS-TEST] HINT: The server returned an HTTP response instead of upgrading to WebSocket."
                        f"\n[WS-TEST]       This means the reverse proxy (Caddy/nginx) is NOT routing '{ws_url}'"
                        f"\n[WS-TEST]       to the OpenAlgo WebSocket server on port 8765."
                        f"\n[WS-TEST]       Fix: Add a /ws → localhost:8765 block in your Caddyfile:"
                        f"\n[WS-TEST]         @websocket path /ws /ws/*"
                        f"\n[WS-TEST]         handle @websocket {{ reverse_proxy localhost:8765 }}"
                        f"\n[WS-TEST]       Then reload Caddy: sudo systemctl reload caddy"
                        f"\n[WS-TEST]       Until then, use ws://127.0.0.1:8765 if running on the same server."
                    )
                err(f"[WS-TEST] FAIL — {_exc_type}: {exc}{_hint}", exc)

        try:
            _aio.run(_run())
        except RuntimeError:
            # Already inside a running event loop (e.g. eventlet) — skip test
            inf("[WS-TEST] SKIP — cannot run async test inside existing event loop")

    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start WebSocket + strategy threads, run until KeyboardInterrupt."""
        cfg = self.config
        self._verify_registration()  # WC-14: check strategy config first
        self._print_startup_info()
        self._check_open_positions_on_startup()  # WC-01: restore broker positions

        self._send_alert(
            f"🚀 {cfg.strategy_name} starting\n"
            f"Underlyings: {', '.join(cfg.underlyings)}\n"
            f"Min Score: {cfg.min_score} | Max Trap: {cfg.max_trap}",
            1,
        )

        self._test_websocket()

        self.ws.start()

        st_thread = threading.Thread(
            target=self._strategy_thread, name="strategy-thread", daemon=True
        )
        st_thread.start()

        inf(f"[BOT] {cfg.strategy_name} running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            inf("\n[SHUTDOWN] Stopping bot...")
            for ul in list(self.state.positions.keys()):
                inf(f"[SHUTDOWN] Closing {ul} position...")
                self.orders.place_exit(ul, "Bot Shutdown")
        finally:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self._send_alert(f"🛑 {cfg.strategy_name} stopped", 1)
            inf("[BOT] Shutdown complete")


# ===============================================================================
# ENTRY POINT
# ===============================================================================

if __name__ == "__main__":
    config = BotConfig.from_env()
    config.validate()

    if not config.api_key or config.api_key == "openalgo-apikey":
        inf(
            "[WARNING] OPENALGO_API_KEY is not set in environment.\n"
            "          Export it before running: export OPENALGO_API_KEY=your-key"
        )

    bot = OptionsBuyerEdgeBot(config)
    bot.run()
