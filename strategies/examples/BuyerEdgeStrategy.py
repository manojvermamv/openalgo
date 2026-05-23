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

Run:  export OPENALGO_API_KEY="your-key"  &&  python BuyerEdgeStrategy.py
      Inside OpenAlgo /python runner: OPENALGO_API_KEY is injected automatically.

⚠  Long options carry unlimited theta decay — always set PREMIUM_STOP_PTS.
"""

import csv
import math
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
import pandas as pd
from openalgo import api, ta

# Ensure UTF-8 output on Windows (cp1252 console cannot encode ₹ and other Unicode chars).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ===============================================================================
# GLOBAL CONSTANTS
# ===============================================================================

# Market hours (IST): 9:15 AM – 3:30 PM
MARKET_HOURS_START = 915   # 09:15 IST
MARKET_HOURS_END   = 1530  # 15:30 IST


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
    telegram_username: str = ""

    # ── Options Parameters ─────────────────────────────────────────────────────
    dte_min:        int = 7
    dte_max:        int = 30
    otm_offset:     int = 1
    strike_count:   int = 8   # strikes each side fetched from the option chain (STRIKE_COUNT env var)
    lot_multiplier: int = 1

    # ── Signal Thresholds ──────────────────────────────────────────────────────
    min_score: int = 15
    max_trap:  int = 80

    # ── Risk — Fixed ₹ Points ──────────────────────────────────────────────────
    premium_stop_pts:   float = 30.0
    premium_target_pts: float = 50.0

    # ── Session Gates ──────────────────────────────────────────────────────────
    max_trades_per_session: int   = 5
    max_consecutive_losses: int   = 3
    entry_cooldown_secs:    int   = 300
    max_daily_loss_pct:     float = 0.0
    max_daily_loss_amount:  float = 2000.0
    risk_percent:           float = 1.0

    # ── Trailing SL ────────────────────────────────────────────────────────────
    trail_sl_mode:         str   = "premium"
    spot_reward_pct:       float = 1.0
    trail_activate_at_pct: float = 25.0
    trail_step_rr_pct:     float = 10.0

    # ── Mode Flags ─────────────────────────────────────────────────────────────
    long_only_mode:   bool = True
    broker_sl_orders: bool = True

    # ── Technicals ─────────────────────────────────────────────────────────────
    candle_interval: str = "15m"
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

    # ── Paper Trading ──────────────────────────────────────────────────────────
    paper_trade: bool = False       # simulate fills from WS LTP; no real orders sent

    # ── Daily Profit Target ────────────────────────────────────────────────────
    max_daily_profit_amount: float = 0.0    # halt new entries when day P&L hits this; 0=off

    # ── Session Timing ─────────────────────────────────────────────────────────
    no_new_trade_after: str = "15:10"   # no new BUY entries after this IST time (HH:MM)
    square_off_time:    str = "15:15"   # force-exit all positions at this IST time

    # ── Max Hold Time ──────────────────────────────────────────────────────────
    max_hold_minutes: int = 0   # exit positions held > N minutes; 0=disabled

    # ── Breakeven SL ───────────────────────────────────────────────────────────
    breakeven_at_gain_pct: float = 80.0  # move SL to entry cost at X% of target gain; 0=off

    # ── Trade Journal ──────────────────────────────────────────────────────────
    trade_journal_path: str = ""    # CSV path for trade log (timestamp,underlying,entry,exit,pnl,...); ""=off

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Construct a BotConfig from environment variables."""
        underlyings_csv = os.getenv(
            "UNDERLYINGS",
            "NIFTY,BANKNIFTY,FINNIFTY,RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS",
        )
        index_csv = os.getenv(
            "INDEX_UNDERLYINGS",
            "NIFTY,BANKNIFTY,FINNIFTY,MIDCPNIFTY,SENSEX,BANKEX,NIFTYNXT50",
        )
        underlyings = sorted(set(u.strip() for u in underlyings_csv.split(",") if u.strip()))
        index_underlyings: frozenset[str] = frozenset(
            u.strip() for u in index_csv.split(",") if u.strip()
        )
        host_server = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")

        # WebSocket URL: explicit env var → auto-corrected → derived from host.
        _ws_env    = os.getenv("WEBSOCKET_URL", "")
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
            print(
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
            api_key=os.getenv("OPENALGO_API_KEY", "openalgo-apikey"),
            api_host=host_server,
            ws_url=_ws_env,
            strategy_name=os.getenv("STRATEGY_NAME", "OptionsBuyerEdgeBot"),
            underlyings=underlyings,
            index_underlyings=index_underlyings,
            spot_exchange=os.getenv("EXCHANGE", "NSE"),
            fno_exchange=os.getenv("FNO_EXCHANGE", "NFO"),
            index_exchange=os.getenv("INDEX_EXCHANGE", "NSE_INDEX"),
            telegram_username=os.getenv("TELEGRAM_USERNAME", ""),
            dte_min=int(os.getenv("DTE_MIN", "7")),
            dte_max=int(os.getenv("DTE_MAX", "30")),
            otm_offset=int(os.getenv("OTM_OFFSET", "1")),
            strike_count=int(os.getenv("STRIKE_COUNT", "8")),
            lot_multiplier=int(os.getenv("LOT_MULTIPLIER", "1")),
            min_score=int(os.getenv("MIN_SCORE", "15")),
            max_trap=int(os.getenv("MAX_TRAP", "80")),
            premium_stop_pts=float(os.getenv("PREMIUM_STOP_PTS", "30.0")),
            premium_target_pts=float(os.getenv("PREMIUM_TARGET_PTS", "50.0")),
            max_trades_per_session=int(os.getenv("MAX_TRADES_PER_SESSION", "5")),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")),
            entry_cooldown_secs=int(os.getenv("ENTRY_COOLDOWN_SECS", "300")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.0")),
            max_daily_loss_amount=float(os.getenv("MAX_DAILY_LOSS_AMOUNT", "2000.0")),
            risk_percent=float(os.getenv("RISK_PERCENT", "1.0")),
            trail_sl_mode=os.getenv("TRAIL_SL_MODE", "premium"),
            spot_reward_pct=float(os.getenv("SPOT_REWARD_PCT", "1.0")),
            trail_activate_at_pct=float(os.getenv("TRAIL_ACTIVATE_AT_PCT", "25.0")),
            trail_step_rr_pct=float(os.getenv("TRAIL_STEP_RR_PCT", "10.0")),
            long_only_mode=os.getenv("LONG_ONLY_MODE", "true").lower() in ("1", "true", "yes"),
            broker_sl_orders=os.getenv("BROKER_SL_ORDERS", "true").lower() in ("1", "true", "yes"),
            candle_interval=os.getenv("CANDLE_INTERVAL", "15m"),
            lookback_days=int(os.getenv("LOOKBACK_DAYS", "5")),
            fast_ema_period=int(os.getenv("FAST_EMA_PERIOD", "9")),
            slow_ema_period=int(os.getenv("SLOW_EMA_PERIOD", "21")),
            rsi_period=int(os.getenv("RSI_PERIOD", "14")),
            signal_check_interval=int(os.getenv("SIGNAL_CHECK_INTERVAL", "60")),
            lookback_bars=int(os.getenv("LOOKBACK_BARS", "5")),
            iv_rank_max_entry=float(os.getenv("IV_RANK_MAX_ENTRY", "40.0")),
            iv_52w_low=float(os.getenv("IV_52W_LOW", "8.72")),
            iv_52w_high=float(os.getenv("IV_52W_HIGH", "28.91")),
            min_oi_filter=float(os.getenv("MIN_OI_FILTER", "50000")),
            min_vol_filter=float(os.getenv("MIN_VOL_FILTER", "10000")),
            asym_score_threshold=float(os.getenv("ASYM_SCORE_THRESHOLD", "0.35")),
            allow_checkpoint_fallback=os.getenv("ALLOW_CHECKPOINT_FALLBACK", "true").lower() in ("1", "true", "yes"),
            delta_target_low=float(os.getenv("DELTA_TARGET_LOW", "0.25")),
            delta_target_high=float(os.getenv("DELTA_TARGET_HIGH", "0.45")),
            order_status_max_retries=int(os.getenv("ORDER_STATUS_MAX_RETRIES", "15")),
            order_status_poll_interval=float(os.getenv("ORDER_STATUS_POLL_INTERVAL", "2.0")),
            paper_trade=os.getenv("PAPER_TRADE", "false").lower() in ("1", "true", "yes"),
            max_daily_profit_amount=float(os.getenv("MAX_DAILY_PROFIT_AMOUNT", "0.0")),
            no_new_trade_after=os.getenv("NO_NEW_TRADE_AFTER", "13:30"),
            square_off_time=os.getenv("SQUARE_OFF_TIME", "15:15"),
            max_hold_minutes=int(os.getenv("MAX_HOLD_MINUTES", "0")),
            breakeven_at_gain_pct=float(os.getenv("BREAKEVEN_AT_GAIN_PCT", "80.0")),
            trade_journal_path=os.getenv("TRADE_JOURNAL_PATH", ""),
        )
        _known_equity = {"RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS"}
        _unclassified = [
            s for s in cfg.underlyings
            if s not in cfg.index_underlyings and s not in _known_equity
        ]
        if _unclassified:
            print(
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
            errors.append(f"PREMIUM_STOP_PTS={self.premium_stop_pts} must be > 0 (fixed ₹ points)")
        if self.premium_target_pts <= 0:
            errors.append(f"PREMIUM_TARGET_PTS={self.premium_target_pts} must be > 0 (fixed ₹ points)")
        if self.risk_percent <= 0:
            errors.append(f"RISK_PERCENT={self.risk_percent} must be > 0")
        if self.trail_sl_mode not in ("spot", "premium", "both"):
            errors.append(
                f"TRAIL_SL_MODE={self.trail_sl_mode!r} must be one of 'spot', 'premium', 'both'"
            )
        if self.lot_multiplier < 1:
            errors.append(f"LOT_MULTIPLIER={self.lot_multiplier} must be >= 1")
        if self.strike_count < 1:
            errors.append(f"STRIKE_COUNT={self.strike_count} must be >= 1")
        if not (1 <= self.min_score <= 100):
            errors.append(f"MIN_SCORE={self.min_score} must be in range [1, 100]")
        if not (0 <= self.max_trap <= 100):
            errors.append(f"MAX_TRAP={self.max_trap} must be in range [0, 100]")
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
        # Validate time strings (HH:MM 24-hour format)
        _hhmm = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
        for _fname, _val in (
            ("NO_NEW_TRADE_AFTER", self.no_new_trade_after),
            ("SQUARE_OFF_TIME",    self.square_off_time),
        ):
            if _val and not _hhmm.match(_val):
                errors.append(f"{_fname}={_val!r} must be in HH:MM format (e.g. '13:30')")
        if self.max_hold_minutes < 0:
            errors.append(f"MAX_HOLD_MINUTES={self.max_hold_minutes} must be >= 0 (0 = disabled)")
        if self.breakeven_at_gain_pct < 0 or self.breakeven_at_gain_pct > 200:
            errors.append(f"BREAKEVEN_AT_GAIN_PCT={self.breakeven_at_gain_pct} must be in [0, 200]")
        if errors:
            print("[CONFIG] Startup validation failed:")
            for e in errors:
                print(f"  ✗ {e}")
            raise SystemExit(
                "Fix the configuration errors above before running. "
                "See env-var comments at the top of the file."
            )
        print("[CONFIG] All configuration values validated OK")


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
    sl:                   float
    tgt:                  float
    spot_symbol:          str
    spot_entry:           float
    reward_dist:          float
    trail_active:         bool          = False
    trail_peak:           float | None  = None
    trail_sl_spot:        float | None  = None
    premium_trail_active: bool          = False
    premium_trail_peak:   float | None  = None
    premium_trail_sl:     float | None  = None
    sl_order_id:          str | None    = None
    tgt_order_id:         str | None    = None
    broker_protection:    bool          = False
    exit_pending:         bool          = False
    # ── new fields ──────────────────────────────────────────────────────────
    entry_time:           datetime      = field(default_factory=datetime.now)
    breakeven_moved:      bool          = False   # True once SL has been shifted to entry cost


@dataclass
class PendingEntry:
    order_id:   str
    symbol:     str
    qty:        int
    spot:       float
    direction:  str
    created_at: datetime


@dataclass
class PendingExit:
    order_id:   str
    reason:     str
    created_at: datetime


# ===============================================================================
# BOT STATE — shared mutable state passed to all components
# ===============================================================================

class BotState:
    """Thread-safe shared state owned by the orchestrator, passed to all components."""

    def __init__(self, lookback_bars: int = 5):
        self.positions:       dict[str, OptionPosition] = {}
        self.ltp_map:         dict[str, float] = {}
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

    def get_chain_history(self, symbol: str) -> deque:
        if symbol not in self.chain_history:
            self.chain_history[symbol] = deque(maxlen=max(1, self._lookback_bars))
        return self.chain_history[symbol]

    def reset_market_caches(self):
        self.prev_straddle.clear()
        self.prev_spot.clear()
        self.prev_sf.clear()
        self.chain_history.clear()


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
        iv_rank: float | None,
        straddle_price: float | None,
        prev_straddle_price: float | None,
        sf_ltp: float | None,
        ce_bid: float | None,
        ce_ask: float | None,
        pe_bid: float | None,
        pe_ask: float | None,
        ce_delta: float | None = None,
        pe_delta: float | None = None,
        prev_spot: float | None = None,
        prev_sf_ltp: float | None = None,
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
            if len(df_today) >= 5:
                vwap = ta.vwap(df_today["high"], df_today["low"], df_today["close"], df_today["volume"])
                vv = vwap.iloc[-2]
                if spot > vv:
                    s4 = 1;  vwap_note = f"Spot {spot:.1f} above VWAP {vv:.1f}"
                else:
                    s4 = -1; vwap_note = f"Spot {spot:.1f} below VWAP {vv:.1f}"
            else:
                vwap_note = "VWAP insufficient bars for today"
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

        # L3-b: Gamma Regime (disabled — no GEX data)
        _c("Gamma Regime", 0, 2, "neutral", "Gamma flip unavailable (no GEX data)")

        # ── LAYER 4: Straddle & IV ───────────────────────────────────────────
        # L4-a: IV Regime
        s11 = 0
        iv_note = "IVR unavailable"
        if iv_rank is not None:
            if iv_rank < 20:       s11 = 1;    iv_note = f"IVR {iv_rank:.1f}% — structurally cheap, full buyer edge"
            elif iv_rank < 40:     s11 = 0.5;  iv_note = f"IVR {iv_rank:.1f}% — moderate, mild buyer edge"
            elif iv_rank > 60:     s11 = -1;   iv_note = f"IVR {iv_rank:.1f}% — structurally expensive, buyer disadvantage"
            elif iv_rank > 50:     s11 = -0.5; iv_note = f"IVR {iv_rank:.1f}% — elevated, mild seller edge"
            else:                  iv_note = f"IVR {iv_rank:.1f}% — neutral zone (40–50%)"
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
        if iv_rank is not None and iv_rank > 60:
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
        # Active score_max sum: EMA(1)+RSI(1)+MACD(1)+VWAP(1)+PCR(1)+CE-flow(2)+PE-flow(2)
        #   +Wall(1)+Greeks(1)+IV(1)+Straddle(2)+SF(1) = 15  (Gamma=2 disabled → excluded)
        MAX_RAW_SCORE = 15
        raw_score  = sum(c.score for c in components)
        base_score = (raw_score / MAX_RAW_SCORE) * 100
        final_score = int(max(-100, min(100, base_score)))

        for c in components:
            if abs(c.score) >= (c.score_max * 0.5):
                reasons.append(c.note)

        abs_score = abs(final_score)
        if trap_score > cfg.max_trap:
            signal = "NO_TRADE"
            if trap_reasons:
                reasons.insert(0, f"⚠ High trap risk: {trap_reasons[0]}")
        elif abs_score >= cfg.min_score:
            signal = "EXECUTE" if trap_score <= cfg.max_trap else "WATCH"
        elif abs_score >= 30:
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

    def __init__(self, client: api, config: BotConfig):
        self.client = client
        self.config = config

    def underlying_exchange(self, symbol: str) -> str:
        """Return NSE_INDEX/BSE_INDEX for index underlyings, else SPOT_EXCHANGE."""
        return self.config.index_exchange if symbol in self.config.index_underlyings else self.config.spot_exchange

    def fetch_spot_candles(self, symbol: str) -> pd.DataFrame | None:
        try:
            end   = datetime.now()
            start = end - timedelta(days=self.config.lookback_days)
            df = self.client.history(
                symbol=symbol,
                exchange=self.underlying_exchange(symbol),
                interval=self.config.candle_interval,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
            if df is None or len(df) < self.config.slow_ema_period + 5:
                return None
            return df
        except Exception as exc:
            print(f"[DATA] Candle fetch error for {symbol}: {exc}")
            return None

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
            print(f"[DATA] Option chain error for {symbol}: {exc}")
            return [], None

    def fetch_quote(self, symbol: str, exchange: str) -> dict:
        try:
            response = self.client.quotes(symbol=symbol, exchange=exchange) or {}
            if response.get("status") == "success":
                return response.get("data", {})
            print(f"[DEBUG] {symbol}@{exchange}: quotes API error: {response}")
            return {}
        except Exception as e:
            print(f"[DEBUG] {symbol}@{exchange}: quotes API exception: {e}")
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
            except Exception as exc:
                print(f"[DATA] syntheticfuture error for {symbol}: {exc}")
        if expiry:
            fut_symbol = f"{symbol}{expiry}FUT"
            sf_q = self.fetch_quote(fut_symbol, self.config.fno_exchange)
            ltp  = float(sf_q.get("ltp", 0) or 0)
            if ltp:
                return ltp
            print(f"[DATA] syntheticfuture fallback: {fut_symbol} returned no LTP")
        return None

    def fetch_atm_greeks(
        self,
        symbol: str,
        ce_symbol: str | None,
        pe_symbol: str | None,
    ) -> tuple[float | None, float | None]:
        ul_exchange = self.underlying_exchange(symbol)
        ce_delta: float | None = None
        pe_delta: float | None = None
        for opt_sym, key in ((ce_symbol, "ce"), (pe_symbol, "pe")):
            if not opt_sym:
                continue
            try:
                resp = self.client.optiongreeks(
                    symbol=opt_sym,
                    exchange=self.config.fno_exchange,
                    underlying_symbol=symbol,
                    underlying_exchange=ul_exchange,
                )
                if resp and resp.get("status") == "success":
                    delta = resp.get("greeks", {}).get("delta")
                    if delta is not None:
                        if key == "ce":
                            ce_delta = float(delta)
                        else:
                            pe_delta = float(delta)
            except Exception as exc:
                print(f"[DATA] optiongreeks error for {opt_sym}: {exc}")
        return ce_delta, pe_delta

    def fetch_option_delta(self, symbol: str, option_symbol: str | None) -> float | None:
        if not option_symbol:
            return None
        try:
            resp = self.client.optiongreeks(
                symbol=option_symbol,
                exchange=self.config.fno_exchange,
                underlying_symbol=symbol,
                underlying_exchange=self.underlying_exchange(symbol),
            )
            if resp and resp.get("status") == "success":
                delta = resp.get("greeks", {}).get("delta")
                if delta is not None:
                    return abs(float(delta))
        except Exception as exc:
            print(f"[DATA] option delta error for {option_symbol}: {exc}")
        return None

    def fetch_iv_rank(self, spot_quote: dict) -> float | None:
        try:
            atm_iv = spot_quote.get("iv")
            if atm_iv is None:
                return None
            current_iv = float(atm_iv)
            if not math.isfinite(current_iv) or current_iv <= 0:
                return None
            return SignalEngine.iv_rank(current_iv, self.config.iv_52w_low, self.config.iv_52w_high)
        except Exception as exc:
            print(f"[DATA] IV rank error: {exc}")
        return None

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
            print(f"[DATA] expiry fetch error for {symbol}: {exc}")
            return None


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
    ) -> dict | None:
        """
        Select the best entry strike using liquidity + asymmetry score.
        Returns None if no qualifying strike found.
        """
        cfg = self.config
        if not chain_rows or not spot:
            return None
        if iv_rank is not None and iv_rank >= cfg.iv_rank_max_entry:
            print(f"[STRIKE] IVR {iv_rank:.1f}% >= max {cfg.iv_rank_max_entry:.1f}% - buyer edge rejected")
            return None

        if direction == "CE":
            lo, hi = spot, spot * 1.05
        else:
            lo, hi = spot * 0.95, spot

        oi_key  = "ce_oi"     if direction == "CE" else "pe_oi"
        vol_key = "ce_volume" if direction == "CE" else "pe_volume"

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

        opt_key = "ce_symbol" if direction == "CE" else "pe_symbol"
        delta_checked: list[dict] = []
        delta_available = False
        for row in candidates:
            abs_delta = self.fetcher.fetch_option_delta(symbol, row.get(opt_key))
            if abs_delta is None:
                continue
            delta_available = True
            if cfg.delta_target_low <= abs_delta <= cfg.delta_target_high:
                row = dict(row)
                row["_abs_delta"] = abs_delta
                delta_checked.append(row)
        if delta_available:
            if not delta_checked:
                print(
                    f"[STRIKE] No candidate delta in target range "
                    f"{cfg.delta_target_low:.2f}-{cfg.delta_target_high:.2f}"
                )
                return None
            candidates = delta_checked

        ivr        = iv_rank if iv_rank is not None else 50.0
        total_oi   = sum(float(r.get(oi_key,  0) or 0) for r in chain_rows) or 1.0
        total_vol  = sum(float(r.get(vol_key, 0) or 0) for r in chain_rows) or 1.0
        delta_mid  = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
        best_row: dict | None = None
        best_score = -1.0

        for row in candidates:
            strike_oi  = float(row.get(oi_key,  0) or 0)
            strike_vol = float(row.get(vol_key, 0) or 0)
            oi_conc    = min(strike_oi  / total_oi,  1.0)
            vol_conc   = min(strike_vol / total_vol, 1.0)
            abs_delta  = row.get("_abs_delta")
            if abs_delta is not None:
                delta_score = max(0.0, 1.0 - abs(abs_delta - delta_mid) / max(delta_mid, 0.01))
            else:
                delta_score = 0.5   # neutral when no delta data available
            asym_score = (
                (1 - ivr / 100) * 0.40 +   # IV regime: lower IV = better buyer edge
                oi_conc          * 0.30 +   # OI concentration: liquidity depth at strike
                vol_conc         * 0.20 +   # Volume concentration: intraday activity
                delta_score      * 0.10     # Delta proximity to target range centre
            )
            if asym_score > best_score:
                best_score = asym_score
                best_row   = row

        if best_score < cfg.asym_score_threshold:
            print(
                f"[STRIKE] Best asymmetry score {best_score:.3f} < threshold "
                f"{cfg.asym_score_threshold} — no qualifying strike"
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

        self._session_date               = datetime.now().strftime("%Y-%m-%d")
        self._session_trade_count        = 0
        self._session_consecutive_losses = 0
        self._last_entry_times: dict[str, datetime] = {}
        self._daily_pnl                  = 0.0

        self._funds_cache:       float = 0.0   # last broker-reported available capital
        self._funds_cache_time:  float = 0.0
        self._funds_cache_ttl:   float = 60.0  # re-poll interval; between refreshes uses pnl delta
        self._pnl_at_last_fetch: float = 0.0

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
            print(f"[FUNDS] available cash not found in funds() response: {resp}")
        except Exception as exc:
            print(f"[FUNDS] funds() fetch error: {exc}")
            if self._funds_cache_time:
                delta_pnl = self._daily_pnl - self._pnl_at_last_fetch
                return max(0.0, self._funds_cache + delta_pnl)
        return 0.0

    def _maybe_reset_daily_state(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with self._state.state_lock:
            if today != self._session_date:
                print(f"[RISK] New trading day {today} — resetting session state")
                self._session_date               = today
                self._session_trade_count        = 0
                self._session_consecutive_losses = 0
                self._daily_pnl                  = 0.0
                self._last_entry_times.clear()
                self._state.reset_market_caches()

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
                elapsed = (datetime.now() - last_entry_time).total_seconds()
                if elapsed < cfg.entry_cooldown_secs:
                    remaining = int(cfg.entry_cooldown_secs - elapsed)
                    return False, f"Entry cooldown active for {symbol} ({remaining}s remaining)"
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
        if cfg.max_daily_profit_amount > 0 and daily_pnl >= cfg.max_daily_profit_amount:
            return False, (
                f"Daily profit target reached ₹{daily_pnl:.0f} "
                f"(target ₹{cfg.max_daily_profit_amount:.0f}) — locking in gains for the day"
            )
        if cfg.no_new_trade_after:
            now_hm = datetime.now().strftime("%H:%M")
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
            self._last_entry_times[symbol] = datetime.now()

    def record_exit(self, pnl: float):
        """Call after a confirmed exit fill. Updates daily P&L and loss streak."""
        with self._state.state_lock:
            self._daily_pnl += pnl
            if pnl < 0:
                self._session_consecutive_losses += 1
                print(f"[RISK] Loss streak: {self._session_consecutive_losses} | "
                      f"Daily P&L ₹{self._daily_pnl:.0f}")
            else:
                self._session_consecutive_losses = 0

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def halted(self) -> bool:
        """Convenience property — True when check_gates() would block new entries."""
        allowed, _ = self.check_gates()
        return not allowed


# ===============================================================================
# WEBSOCKET MANAGER — real-time LTP + trailing SL engine
# ===============================================================================

class WebSocketManager:
    """
    Manages the WebSocket connection and per-tick trailing SL logic.
    Callbacks for exit and broker SL modification are wired after construction
    to avoid circular dependency with OrderManager.
    """

    def __init__(self, client: api, config: BotConfig, state: BotState):
        self.client = client
        self.config = config
        self._state = state
        self._exit_callback:      Callable[[str, str], None] | None = None
        self._sl_modify_callback: Callable[[str, float], None] | None = None
        self._ws_started     = threading.Event()
        self._subscriptions: set[tuple[str, str]] = set()   # (exchange, symbol) registry for reconnect
        self._subscribe_lock  = threading.Lock()
        self._last_tick_time: float = 0.0                   # updated on every valid tick; used by watchdog

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
            self._subscriptions.add((exchange, symbol))
            try:
                self.client.subscribe_ltp(
                    [{"exchange": exchange, "symbol": symbol}],
                    on_data_received=self._on_ws_data,
                )
                print(f"[WS] Subscribed option {exchange}:{symbol}")
            except Exception as exc:
                print(f"[WS] Subscribe error {exchange}:{symbol}: {exc}")

    def subscribe_spot(self, symbol: str) -> None:
        exch = self.config.index_exchange if symbol in self.config.index_underlyings else self.config.spot_exchange
        with self._subscribe_lock:
            self._subscriptions.add((exch, symbol))
            try:
                self.client.subscribe_ltp(
                    [{"exchange": exch, "symbol": symbol}],
                    on_data_received=self._on_ws_data,
                )
                print(f"[WS] Subscribed spot {exch}:{symbol}")
            except Exception as exc:
                print(f"[WS] Subscribe spot error {symbol}: {exc}")

    def unsubscribe(self, exchange: str, symbol: str) -> None:
        with self._subscribe_lock:
            self._subscriptions.discard((exchange, symbol))
            try:
                self.client.unsubscribe_ltp([{"exchange": exchange, "symbol": symbol}])
            except Exception as exc:
                print(f"[WS] Unsubscribe error {exchange}:{symbol}: {exc}")

    def unsubscribe_spot(self, symbol: str) -> None:
        exch = self.config.index_exchange if symbol in self.config.index_underlyings else self.config.spot_exchange
        with self._subscribe_lock:
            self._subscriptions.discard((exch, symbol))
            try:
                self.client.unsubscribe_ltp([{"exchange": exch, "symbol": symbol}])
            except Exception as exc:
                print(f"[WS] Unsubscribe spot error {symbol}: {exc}")

    def _on_ws_data(self, data: dict) -> None:
        """
        Handles every tick.  Two independent paths:
          Part A — option premium trail (premium trail SL)
          Part B — spot trail (spot-based SL ratchet for indices)
        """
        if not isinstance(data, dict):
            return
        symbol = data.get("symbol", "")
        ltp    = data.get("ltp")
        if ltp is None:
            return
        try:
            ltp = float(ltp)
        except (TypeError, ValueError):
            return

        self._last_tick_time = time.time()    # feed heartbeat for watchdog
        with self._state.state_lock:
            self._state.ltp_map[symbol] = ltp

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
        if ltp <= pos.sl:
            print(f"[WS] PREMIUM SL HIT {underlying}: LTP {ltp:.2f} <= SL {pos.sl:.2f}")
            self._trigger_exit(underlying, "premium_sl_hit")
            return
        if ltp >= pos.tgt:
            print(f"[WS] PREMIUM TARGET HIT {underlying}: LTP {ltp:.2f} >= TGT {pos.tgt:.2f}")
            self._trigger_exit(underlying, "premium_target_hit")
            return

        ep = pos.entry_premium

        if cfg.breakeven_at_gain_pct > 0 and not pos.breakeven_moved:
            target_gain_pts = pos.tgt - ep
            gain_pts        = ltp - ep
            if target_gain_pts > 0 and gain_pts >= (target_gain_pts * cfg.breakeven_at_gain_pct / 100.0):
                new_sl = ep   # SL at entry cost = breakeven
                if new_sl > pos.sl:
                    pos.sl             = new_sl
                    pos.breakeven_moved = True
                    print(
                        f"[WS] BREAKEVEN SL {underlying}: "
                        f"gain {gain_pts:.2f} pts ({gain_pts/target_gain_pts*100:.0f}% of target) "
                        f"→ SL moved to cost ₹{new_sl:.2f}"
                    )
                    if cfg.broker_sl_orders and pos.sl_order_id and self._sl_modify_callback:
                        self._sl_modify_callback(underlying, new_sl)

        if cfg.trail_sl_mode not in ("premium", "both"):
            return
        move = ltp - ep
        activate_pts = ep * (cfg.trail_activate_at_pct / 100.0)
        step_pts     = ep * (cfg.trail_step_rr_pct     / 100.0)

        if not pos.premium_trail_active:
            if move >= activate_pts:
                pos.premium_trail_active = True
                pos.premium_trail_peak   = ltp
                new_sl = ltp - step_pts
                if new_sl > pos.sl:
                    pos.premium_trail_sl = new_sl
                    pos.sl               = new_sl
                    print(f"[WS] Premium trail activated {underlying}: peak {ltp:.2f}, SL → {new_sl:.2f}")
                    if cfg.broker_sl_orders and pos.sl_order_id and self._sl_modify_callback:
                        self._sl_modify_callback(underlying, new_sl)
        else:
            if pos.premium_trail_peak is None or ltp > pos.premium_trail_peak:
                pos.premium_trail_peak = ltp
                new_sl = ltp - step_pts
                if new_sl > pos.sl:
                    pos.premium_trail_sl = new_sl
                    pos.sl               = new_sl
                    print(f"[WS] Premium trail ratchet {underlying}: peak {ltp:.2f}, SL → {new_sl:.2f}")
                    if cfg.broker_sl_orders and pos.sl_order_id and self._sl_modify_callback:
                        self._sl_modify_callback(underlying, new_sl)

    def _check_spot_trail(self, underlying: str, pos: OptionPosition, spot_ltp: float) -> None:
        cfg = self.config
        if cfg.trail_sl_mode not in ("spot", "both"):
            return
        reward_dist  = pos.reward_dist
        activate_pts = reward_dist * (cfg.trail_activate_at_pct / 100.0)
        step_pts     = reward_dist * (cfg.trail_step_rr_pct     / 100.0)

        if pos.option_type == "CE":
            move = spot_ltp - pos.spot_entry
        else:
            move = pos.spot_entry - spot_ltp

        if not pos.trail_active:
            if move >= activate_pts:
                pos.trail_active   = True
                pos.trail_peak     = spot_ltp
                if pos.option_type == "CE":
                    new_sl_spot = spot_ltp - step_pts
                else:
                    new_sl_spot = spot_ltp + step_pts
                pos.trail_sl_spot = new_sl_spot
                print(f"[WS] Spot trail activated {underlying}: peak {spot_ltp:.2f}, SL spot → {new_sl_spot:.2f}")
                # Spot trail activation means we are in a favorable spot position.
                # Raise the premium hard-SL to at least breakeven so IV crush cannot
                # inflict a full stop-loss loss once spot has moved in our favour.
                if not pos.breakeven_moved:
                    new_premium_sl = pos.entry_premium
                    if new_premium_sl > pos.sl:
                        pos.sl             = new_premium_sl
                        pos.breakeven_moved = True
                        print(
                            f"[WS] Spot-trail → breakeven SL {underlying}: "
                            f"pos.sl raised to cost ₹{new_premium_sl:.2f}"
                        )
                        if cfg.broker_sl_orders and pos.sl_order_id and self._sl_modify_callback:
                            self._sl_modify_callback(underlying, new_premium_sl)
        else:
            if pos.option_type == "CE":
                if pos.trail_peak is None or spot_ltp > pos.trail_peak:
                    pos.trail_peak = spot_ltp
                    new_sl_spot    = spot_ltp - step_pts
                    if pos.trail_sl_spot is None or new_sl_spot > pos.trail_sl_spot:
                        pos.trail_sl_spot = new_sl_spot
                        print(f"[WS] Spot trail ratchet {underlying}: peak {spot_ltp:.2f}, SL spot → {new_sl_spot:.2f}")
                if pos.trail_sl_spot is not None and spot_ltp <= pos.trail_sl_spot:
                    print(f"[WS] SPOT TRAIL SL HIT {underlying}: spot {spot_ltp:.2f} <= trail_sl_spot {pos.trail_sl_spot:.2f}")
                    self._trigger_exit(underlying, "spot_trail_sl_hit")
            else:
                if pos.trail_peak is None or spot_ltp < pos.trail_peak:
                    pos.trail_peak = spot_ltp
                    new_sl_spot    = spot_ltp + step_pts
                    if pos.trail_sl_spot is None or new_sl_spot < pos.trail_sl_spot:
                        pos.trail_sl_spot = new_sl_spot
                        print(f"[WS] Spot trail ratchet {underlying}: peak {spot_ltp:.2f}, SL spot → {new_sl_spot:.2f}")
                if pos.trail_sl_spot is not None and spot_ltp >= pos.trail_sl_spot:
                    print(f"[WS] SPOT TRAIL SL HIT {underlying}: spot {spot_ltp:.2f} >= trail_sl_spot {pos.trail_sl_spot:.2f}")
                    self._trigger_exit(underlying, "spot_trail_sl_hit")

    def _trigger_exit(self, underlying: str, reason: str) -> None:
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
                args=(underlying, reason),
                name=f"exit-{underlying}",
                daemon=True,
            ).start()

    def _ws_thread(self) -> None:
        client = self.client
        print("[WS] WebSocket thread starting...")
        self._ws_started.set()
        ws_url = self.config.ws_url or "(SDK default)"
        while True:
            try:
                ok = client.connect()
                _actual_url = getattr(client, 'ws_url', ws_url)
                print(f"[WS] `client.connect()` using {_actual_url} (expected {ws_url})")
                if ok:
                    print(f"[WS] Connected to {_actual_url} — SDK managing reconnects automatically")
                    with self._subscribe_lock:
                        subs = list(self._subscriptions)   # replay all subscriptions (handles reconnects)
                    if subs:
                        print(f"[WS] Replaying {len(subs)} subscription(s)...")
                        for (exch, sym) in subs:
                            try:
                                with self._subscribe_lock:
                                    client.subscribe_ltp(
                                        [{"exchange": exch, "symbol": sym}],
                                        on_data_received=self._on_ws_data,
                                    )
                            except Exception as _re_exc:
                                print(f"[WS] Re-subscribe error {exch}:{sym}: {_re_exc}")
                    while True:  # watchdog: force reconnect if feed silent > 120s in market hours
                        time.sleep(60)
                        elapsed = time.time() - self._last_tick_time
                        hm = int(datetime.now().strftime("%H%M"))
                        if self._last_tick_time and MARKET_HOURS_START <= hm <= MARKET_HOURS_END and elapsed > 120:
                            print(
                                f"[WS] Feed silent {int(elapsed)}s during market hours — "
                                f"forcing hard reconnect..."
                            )
                            break   # exit watchdog → outer loop reconnects immediately
                    continue        # skip time.sleep(5) — reconnect without delay
                print(f"[WS] Connection failed ({ws_url}), retrying in 5s...")
                print("[WS] Check: WEBSOCKET_URL correct? OPENALGO_API_KEY matches dashboard?")
            except Exception as exc:
                _emsg = str(exc)
                print(f"[WS] Connection error: {exc}. Retrying in 5s...")
                if "Invalid API key" in _emsg or "AUTHENTICATION_ERROR" in _emsg:
                    print("[WS] HINT: Check OPENALGO_API_KEY — copy the key from OpenAlgo dashboard \u2192 API Key page")
                elif "InvalidStatus" in type(exc).__name__ or "HTTP 200" in _emsg:
                    print("[WS] HINT: Reverse proxy (/ws) not routing to port 8765 — fix Caddyfile or use ws://127.0.0.1:8765")
            time.sleep(5)


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
        notify:  Callable[[str, int], None],
    ):
        self.client = client
        self.config = config
        self._state = state
        self._risk  = risk
        self._ws    = ws
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
                    print(f"[ORDER] Order {order_id} {order_status}")
                    return None
                # ORD-2: detect partial fill near end of retry window
                filled_qty = int(data.get("filled_quantity", 0) or 0)
                if filled_qty > 0 and attempt >= int(max_r * 0.8):
                    print(
                        f"[ORDER] Partial fill detected: {filled_qty} units "
                        f"for {order_id} (attempt {attempt+1}/{max_r}) — treating as fill"
                    )
                    return resp
            except Exception as exc:
                print(f"[ORDER] orderstatus error (attempt {attempt+1}): {exc}")
            time.sleep(slp)
        print(f"[ORDER] Timed out polling order {order_id} after {max_r} attempts")
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
                        print(f"[ORDER] Broker {attr_name} already filled: {oid}")
            except Exception as exc:
                print(f"[ORDER] pre-check fill error {oid}: {exc}")

        for attr_name, oid in (("sl_order_id", sl_id), ("tgt_order_id", tgt_id)):
            if not oid or attr_name in broker_filled:
                continue
            try:
                resp = self.client.cancelorder(order_id=oid, strategy=self.config.strategy_name)
                if isinstance(resp, dict) and resp.get("status") in ("success", "cancelled"):
                    print(f"[ORDER] Cancelled broker {attr_name} {oid}")
                else:
                    print(f"[ORDER] Cancel resp for {oid}: {resp}")
            except Exception as exc:
                print(f"[ORDER] Cancel error {oid}: {exc}")

        for attr_name, oid in (("sl_order_id", sl_id), ("tgt_order_id", tgt_id)):
            if not oid:
                continue
            try:
                resp = self.client.orderstatus(order_id=oid, strategy=self.config.strategy_name)
                if isinstance(resp, dict) and resp.get("status") == "success":
                    data = resp.get("data") or resp
                    broker_stat = str(data.get("order_status", "")).lower()
                    print(f"[ORDER] Post-cancel status {oid}: {broker_stat}")
            except Exception as exc:
                print(f"[ORDER] Post-cancel check error {oid}: {exc}")
        pos.sl_order_id  = None
        pos.tgt_order_id = None
        pos.broker_protection = False
        return broker_filled

    def modify_broker_sl(self, underlying: str, new_trigger: float) -> None:
        if self.config.paper_trade:
            return   # no-op in paper trade mode
        pos = self._state.positions.get(underlying)
        if not pos or not pos.sl_order_id:
            return
        # ORD-4: pre-check if broker SL already filled before sending modifyorder
        try:
            pre = self.client.orderstatus(
                order_id=pos.sl_order_id, strategy=self.config.strategy_name
            )
            if isinstance(pre, dict) and pre.get("status") == "success":
                _data = pre.get("data") or pre
                if str(_data.get("order_status", "")).lower() in ("complete", "filled", "executed"):
                    print(f"[ORDER] SL already filled for {underlying} — skipping modify, triggering exit")
                    self._trigger_exit(underlying, "broker_sl_filled_on_modify")
                    return
        except Exception as _pre_exc:
            print(f"[ORDER] modify_broker_sl pre-check error for {underlying}: {_pre_exc}")
        try:
            resp = self.client.modifyorder(
                order_id=pos.sl_order_id,
                strategy=self.config.strategy_name,
                action="SELL",
                quantity=pos.qty,
                order_type="SL-M",
                product="NRML",
                price=0,
                trigger_price=new_trigger,
            )
            if isinstance(resp, dict) and resp.get("status") == "success":
                print(f"[ORDER] Broker SL modified for {underlying} → trigger ₹{new_trigger:.2f}")
            else:
                print(f"[ORDER] modifyorder resp for {underlying}: {resp}")
        except Exception as exc:
            print(f"[ORDER] modify_broker_sl error for {underlying}: {exc}")

    def check_broker_order_fills(self) -> None:
        """Periodic poll: if broker SL or target order was filled, trigger exit."""
        for underlying, pos in list(self._state.positions.items()):
            if pos.exit_pending:
                continue
            for attr_name, reason in (
                ("sl_order_id",  "broker_sl_filled"),
                ("tgt_order_id", "broker_target_filled"),
            ):
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
                        print(f"[ORDER] Broker {attr_name} filled for {underlying} ({oid})")
                        executed_price = float(data.get("average_price", 0) or 0)
                        pnl = 0.0
                        if executed_price > 0:
                            pnl = (executed_price - pos.entry_premium) * pos.qty
                            self._risk.record_exit(pnl)
                            self._notify(
                                f"🏦 Broker order filled for {underlying}\n"
                                f"Option: {pos.symbol}\nExecuted: ₹{executed_price:.2f}\n"
                                f"P&L: ₹{pnl:.0f}\nReason: {reason}",
                                2,
                            )
                        # PNL-2: write journal entry for broker-triggered exits
                        self._write_journal(underlying, pos, executed_price, pnl, reason)
                        self._ws.unsubscribe(self.config.fno_exchange, pos.symbol)
                        self._ws.unsubscribe_spot(pos.spot_symbol)
                        with self._state.state_lock:
                            self._state.positions.pop(underlying, None)
                        with self._state.exit_lock:
                            self._state.exit_queue.discard(underlying)
                except Exception as exc:
                    print(f"[ORDER] check_broker_order_fills error ({underlying}, {oid}): {exc}")

    def register_filled_entry(
        self,
        underlying: str,
        option_symbol: str,
        qty: int,
        spot: float,
        direction: str,
        executed: float,
    ) -> None:
        cfg = self.config
        sl  = executed - cfg.premium_stop_pts
        tgt = executed + cfg.premium_target_pts
        reward_dist = spot * (cfg.spot_reward_pct / 100.0)

        pos = OptionPosition(
            underlying=underlying,
            symbol=option_symbol,
            entry_premium=executed,
            qty=qty,
            option_type=direction,
            sl=sl,
            tgt=tgt,
            spot_symbol=underlying,
            spot_entry=spot,
            reward_dist=reward_dist,
        )
        with self._state.state_lock:
            self._state.positions[underlying] = pos

        self._ws.subscribe(cfg.fno_exchange, option_symbol)
        self._ws.subscribe_spot(underlying)

        if cfg.broker_sl_orders and not cfg.paper_trade:
            try:
                sl_resp = self.client.placeorder(
                    strategy=cfg.strategy_name,
                    symbol=option_symbol,
                    action="SELL",
                    exchange=cfg.fno_exchange,
                    price_type="SL-M",
                    product="NRML",
                    quantity=qty,
                    price=0,
                    trigger_price=sl,
                )
                if isinstance(sl_resp, dict) and sl_resp.get("status") == "success":
                    pos.sl_order_id = sl_resp.get("orderid")
                    print(f"[ORDER] Broker SL-M placed for {underlying}: trigger ₹{sl:.2f} (id:{pos.sl_order_id})")
            except Exception as exc:
                print(f"[ORDER] Broker SL-M error for {underlying}: {exc}")
            try:
                tgt_resp = self.client.placeorder(
                    strategy=cfg.strategy_name,
                    symbol=option_symbol,
                    action="SELL",
                    exchange=cfg.fno_exchange,
                    price_type="LIMIT",
                    product="NRML",
                    quantity=qty,
                    price=tgt,
                )
                if isinstance(tgt_resp, dict) and tgt_resp.get("status") == "success":
                    pos.tgt_order_id = tgt_resp.get("orderid")
                    print(f"[ORDER] Broker LIMIT placed for {underlying}: ₹{tgt:.2f} (id:{pos.tgt_order_id})")
            except Exception as exc:
                print(f"[ORDER] Broker LIMIT target error for {underlying}: {exc}")
            if pos.sl_order_id or pos.tgt_order_id:
                pos.broker_protection = True

        print(
            f"[ORDER] Position registered for {underlying}: {option_symbol} "
            f"QTY={qty} ENTRY=₹{executed:.2f} SL=₹{sl:.2f} TGT=₹{tgt:.2f}"
        )

    # ── Trade Journal ──────────────────────────────────────────────────────────

    def _write_journal(
        self,
        underlying: str,
        pos: OptionPosition,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        """Append one row to the CSV trade journal (if enabled)."""
        path = self.config.trade_journal_path
        if not path:
            return
        header = [
            "timestamp", "underlying", "option_symbol", "direction", "qty",
            "entry", "exit", "pnl", "reason", "mode",
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
        ]
        write_header = not os.path.exists(path)
        try:
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(header)
                w.writerow(row)
        except OSError as exc:
            print(f"[JOURNAL] Write error: {exc}")

    def place_entry(
        self,
        underlying: str,
        option_symbol: str,
        qty: int,
        spot: float,
        direction: str,
    ) -> bool:
        """Place a market BUY order, poll for fill, then register the position."""
        cfg = self.config
        if underlying in self._state.positions:
            print(f"[ORDER] {underlying} already has an open position — skip entry")
            return False

        if cfg.paper_trade:
            executed = self._state.ltp_map.get(option_symbol) or spot * 0.01
            print(f"[PAPER] Simulated BUY {qty}x {option_symbol} @ ₹{executed:.2f}")
            self._risk.record_entry(underlying)
            self.register_filled_entry(underlying, option_symbol, qty, spot, direction, executed)
            self._notify(
                f"📄 PAPER Entry: {underlying}\n"
                f"Option: {option_symbol} x{qty}\n"
                f"Sim fill: ₹{executed:.2f}\n"
                f"SL: ₹{executed - cfg.premium_stop_pts:.2f} | "
                f"TGT: ₹{executed + cfg.premium_target_pts:.2f}",
                2,
            )
            return True

        with self._state.state_lock:
            self._state.entry_in_flight += 1
        try:
            resp = self.client.placeorder(
                strategy=cfg.strategy_name,
                symbol=option_symbol,
                action="BUY",
                exchange=cfg.fno_exchange,
                price_type="MARKET",
                product="NRML",
                quantity=qty,
            )
            if not isinstance(resp, dict) or resp.get("status") != "success":
                print(f"[ORDER] Entry order rejected for {underlying}: {resp}")
                return False
            order_id = resp.get("orderid")
            print(f"[ORDER] Entry order {order_id} placed for {underlying} ({option_symbol} x{qty})")

            # Add to pending entries for reconciliation
            with self._state.state_lock:
                self._state.pending_entries[underlying] = PendingEntry(
                    order_id=order_id,
                    symbol=option_symbol,
                    qty=qty,
                    spot=spot,
                    direction=direction,
                    created_at=datetime.now(),
                )

            filled = self.poll_order_status(order_id)
            with self._state.state_lock:
                self._state.pending_entries.pop(underlying, None)
            if not filled:
                print(f"[ORDER] Entry order {order_id} not filled within poll window — abandoning")
                return False

            data       = filled.get("data") or filled
            executed   = float(data.get("average_price", 0) or 0)
            if not executed:
                executed = float(data.get("price", 0) or 0)
            if not executed:
                print(f"[ORDER] Executed price is zero for {order_id} — cannot register position")
                return False

            filled_qty = int(data.get("filled_quantity", 0) or data.get("filled_qty", 0) or 0)
            if filled_qty > 0 and filled_qty != qty:
                print(
                    f"[ORDER] Partial fill accepted for {order_id}: requested {qty}, "
                    f"filled {filled_qty}"
                )
                qty = filled_qty

            self._risk.record_entry(underlying)
            self.register_filled_entry(underlying, option_symbol, qty, spot, direction, executed)
            self._notify(
                f"✅ Entry executed: {underlying}\n"
                f"Option: {option_symbol}\nQty: {qty}\nExecuted: ₹{executed:.2f}\n"
                f"SL: ₹{executed - cfg.premium_stop_pts:.2f} | "
                f"TGT: ₹{executed + cfg.premium_target_pts:.2f}",
                2,
            )
            return True
        except Exception as exc:
            print(f"[ORDER] placeorder error for {underlying}: {exc}")
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
        print(f"[ORDER] Exiting {underlying} — reason: {reason}")

        if cfg.paper_trade:
            executed_price = self._state.ltp_map.get(pos.symbol) or pos.entry_premium
            pnl = (executed_price - pos.entry_premium) * pos.qty
            print(f"[PAPER] Simulated SELL {pos.qty}x {pos.symbol} @ ₹{executed_price:.2f} | P&L ₹{pnl:.2f}")
            self._risk.record_exit(pnl)
            self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
            self._ws.unsubscribe_spot(pos.spot_symbol)
            self._write_journal(underlying, pos, executed_price, pnl, reason)
            with self._state.state_lock:
                self._state.positions.pop(underlying, None)
            with self._state.exit_lock:
                self._state.exit_queue.discard(underlying)
            emoji = "✅" if pnl >= 0 else "❌"
            self._notify(
                f"{emoji} PAPER Exit: {underlying} | {reason}\n"
                f"Option: {pos.symbol}\n"
                f"Entry ₹{pos.entry_premium:.2f} → Exit ₹{executed_price:.2f}\n"
                f"P&L: ₹{pnl:.0f} | Daily ₹{self._risk.daily_pnl:.0f}",
                2,
            )
            return

        broker_filled = {}
        if cfg.broker_sl_orders:
            broker_filled = self.cancel_broker_orders(underlying)

        for attr_name, info in broker_filled.items():
            if isinstance(info, dict) and info.get("order_status") in ("complete", "filled", "executed"):
                executed_price = info.get("executed", 0)
                print(f"[ORDER] Broker {attr_name} already filled at ₹{executed_price:.2f} — skipping SELL")
                pnl = (float(executed_price) - pos.entry_premium) * pos.qty
                self._risk.record_exit(pnl)
                self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
                self._ws.unsubscribe_spot(pos.spot_symbol)
                self._write_journal(underlying, pos, float(executed_price), pnl, reason)
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
                product="NRML",
                quantity=pos.qty,
            )
            if isinstance(resp, dict) and resp.get("status") == "success":
                order_id = resp.get("orderid")
                print(f"[ORDER] Exit order {order_id} placed for {underlying}")
            else:
                print(f"[ORDER] Exit order response: {resp}")
        except Exception as exc:
            print(f"[ORDER] place_exit error for {underlying}: {exc}")

        if order_id is None:
            # Order was not submitted — safe to release exit lock so the next SL
            # trigger from the WS trail can retry the exit on the next tick.
            print(f"[ORDER] Exit order not submitted for {underlying} — releasing for retry")
            with self._state.exit_lock:
                self._state.exit_queue.discard(underlying)
            pos.exit_pending = False
            return

        with self._state.state_lock:
            self._state.pending_exits[underlying] = PendingExit(
                order_id=order_id,
                reason=reason,
                created_at=datetime.now(),
            )
        filled = self.poll_order_status(order_id)
        if not filled:
            # Order submitted but fill could not be confirmed within the poll window.
            # Leave pending_exits intact so check_pending_exits() reconciles on the
            # next strategy cycle; position and exit_pending stay as-is.
            print(
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
        self._write_journal(underlying, pos, executed_price, pnl, reason)
        self._ws.unsubscribe(cfg.fno_exchange, pos.symbol)
        self._ws.unsubscribe_spot(pos.spot_symbol)
        with self._state.state_lock:
            self._state.positions.pop(underlying, None)
        with self._state.exit_lock:
            self._state.exit_queue.discard(underlying)

        emoji = "✅" if pnl >= 0 else "❌"
        self._notify(
            f"{emoji} Exit: {underlying} | {reason}\n"
            f"Option: {pos.symbol}\n"
            f"Entry ₹{pos.entry_premium:.2f} → Exit ₹{executed_price:.2f}\n"
            f"P&L: ₹{pnl:.0f} | Daily ₹{self._risk.daily_pnl:.0f}",
            2,
        )

    def check_pending_entries(self) -> None:
        """Reconcile stale pending entry orders. WC-09: post-cutoff entries queue immediate exit."""
        with self._state.state_lock:
            pending = list(self._state.pending_entries.items())
        now_hm = datetime.now().strftime("%H:%M")
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
                    print(f"[PENDING] BUY {order_id} filled for {underlying} @ ₹{price:.2f}; activating protection")
                    self.register_filled_entry(
                        underlying, pending_entry.symbol, pending_entry.qty,
                        pending_entry.spot, pending_entry.direction, price,
                    )
                    # WC-09: If filled after square_off_time, queue immediate exit
                    if square_off_hm and now_hm >= square_off_hm:
                        print(f"[PENDING] Entry {order_id} filled AFTER cutoff ({now_hm} >= {square_off_hm}) — queuing exit")
                        with self._state.state_lock:
                            pos = self._state.positions.get(underlying)
                            if pos:
                                pos.exit_pending = True
                                with self._state.exit_lock:
                                    self._state.exit_queue.add(underlying)
                        self.orders.place_exit(underlying, "PostCutoffEntry")
                    self._notify(
                        f"✅ {self.config.strategy_name}: pending BUY {order_id} reconciled "
                        f"for {underlying} @ ₹{price:.2f} (fill detected outside normal path)",
                        5,
                    )
                elif status in ("rejected", "cancelled", "canceled"):
                    with self._state.state_lock:
                        self._state.pending_entries.pop(underlying, None)
                    print(f"[PENDING] BUY {order_id} {status}; removed from pending entries")
            elif square_off_hm and now_hm >= square_off_hm:
                # WC-09: Cancel unfilled pending entry after square_off_time cutoff
                try:
                    cancel_resp = self.client.cancelorder(order_id=order_id, strategy=self.config.strategy_name)
                    cancel_status = cancel_resp.get("status") if isinstance(cancel_resp, dict) else None
                    if cancel_status == "success" or "cancel" in str(cancel_resp).lower():
                        with self._state.state_lock:
                            self._state.pending_entries.pop(underlying, None)
                        print(f"[PENDING] Cancelled unfilled entry {order_id} after {now_hm} cutoff")
                except Exception as _exc:
                    print(f"[PENDING] Cancel error for {order_id}: {_exc}")

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
                    self._risk.record_exit(pnl)
                    self._write_journal(underlying, pos, executed_price, pnl, pending_exit.reason)
                    self._ws.unsubscribe(self.config.fno_exchange, opt_sym)
                    self._ws.unsubscribe_spot(pos.spot_symbol)
                    with self._state.state_lock:
                        self._state.positions.pop(underlying, None)
                        self._state.pending_exits.pop(underlying, None)
                    with self._state.exit_lock:
                        self._state.exit_queue.discard(underlying)
                    print(f"[PENDING] EXIT {order_id} complete for {underlying} @ ₹{executed_price:.2f} | P&L ₹{pnl:.2f}")
                    self._notify(
                        f"{pnl_sign} {self.config.strategy_name} EXIT confirmed\n"
                        f"{underlying} {pos.option_type} | {opt_sym}\n"
                        f"Exit ₹{executed_price:.2f} | Entry ₹{pos.entry_premium:.2f} | P&L ₹{pnl:.2f}\n"
                        f"Daily P&L ₹{self._risk.daily_pnl:.0f}",
                        8 if pnl < 0 else 6,
                    )
                elif status in ("rejected", "cancelled", "canceled"):
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
        api_kwargs: dict = dict(api_key=config.api_key, host=config.api_host)
        if config.ws_url:
            api_kwargs["ws_url"] = config.ws_url   # explicit override; otherwise SDK derives from host
        self.client = api(**api_kwargs)
        self.state   = BotState(lookback_bars=config.lookback_bars)
        self.risk    = RiskManager(self.client, config, self.state)
        self.fetcher = DataFetcher(self.client, config)
        self.scorer  = SignalEngine(config)
        self.strikes = StrikeSelector(self.fetcher, config)
        self.ws      = WebSocketManager(self.client, config, self.state)
        self.orders  = OrderManager(
            self.client, config, self.state, self.risk, self.ws, self._send_telegram
        )
        # Wire callbacks to break circular dependency
        self.ws.set_exit_callback(self.orders.place_exit)
        self.ws.set_sl_modify_callback(self.orders.modify_broker_sl)

    def _send_telegram(self, message: str, priority: int = 1) -> None:
        if not self.config.telegram_username:
            return
        try:
            self.client.telegram(
                username=self.config.telegram_username,
                strategy=self.config.strategy_name,
                message=message,
            )
        except Exception as exc:
            print(f"[TELEGRAM] Send error: {exc}")

    def _verify_registration(self) -> None:
        """WC-14: Verify strategy is registered in broker's strategy configs."""
        cfg = self.config
        try:
            resp = self.client.orderbook(strategy=cfg.strategy_name)
            if isinstance(resp, dict) and resp.get("status") == "success":
                print(f"[STARTUP] ✓ Strategy '{cfg.strategy_name}' registered OK")
                return
        except Exception:
            pass
        print(f"[STARTUP] ⚠️  Strategy '{cfg.strategy_name}' not found in strategy configs.")
        print(f"[STARTUP]    Run: python3 /app/strategies/register_strategy.py")
        print(f"[STARTUP]    Then restart this script.")
        print(f"[STARTUP] Continuing anyway (may cause runtime errors)...\n")

    def _check_open_positions_on_startup(self) -> None:
        """WC-01: Restore broker positions + resubscribe WS + query SL orders."""
        try:
            resp = self.client.positionbook(strategy=self.config.strategy_name)
            if not isinstance(resp, dict) or resp.get("status") != "success":
                return
            positions = resp.get("data", []) or []
            if not positions:
                print("[STARTUP] No open positions found in broker position book")
                return
            print(f"[STARTUP] Found {len(positions)} broker position(s). Restoring...")
            cfg = self.config
            # Fetch orderbook to find SL/TGT orders
            orderbook_resp = self.client.orderbook(strategy=cfg.strategy_name)
            open_orders = orderbook_resp.get("data", []) if isinstance(orderbook_resp, dict) else []
            for p in positions:
                sym      = p.get("symbol", "")
                qty      = int(p.get("netqty", 0) or 0)
                entry_px = float(p.get("average_price", 0) or 0)
                if not sym or qty == 0 or entry_px <= 0:
                    continue
                # Extract underlying (e.g., BANKNIFTY from BANKNIFTY24APR24CE)
                base = sym.replace("FUT", "").replace("CE", "").replace("PE", "")
                underlying = base.split("24")[0] if "24" in base else base
                opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                if not opt_type or underlying in self.state.positions:
                    continue
                # Create position with conservative SL/TGT estimates
                pos = OptionPosition(
                    underlying=underlying,
                    symbol=sym,
                    entry_premium=entry_px,
                    qty=qty,
                    option_type=opt_type,
                    sl=entry_px - cfg.premium_stop_pts,
                    tgt=entry_px + cfg.premium_target_pts,
                    spot_symbol=underlying,
                    spot_entry=entry_px,
                    reward_dist=0.0,
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
                # Register + resubscribe WS
                with self.state.state_lock:
                    self.state.positions[underlying] = pos
                self.ws.subscribe(cfg.fno_exchange, sym)
                self.ws.subscribe_spot(underlying)
                print(f"[STARTUP] ✓ Restored {underlying}: {sym} x{qty} @ ₹{entry_px:.2f}")
        except Exception as exc:
            print(f"[STARTUP] positionbook error: {exc}")

    def _check_max_hold(self) -> None:
        """Exit positions held > max_hold_minutes (theta decay guard). 0=disabled."""
        cfg = self.config
        if cfg.max_hold_minutes <= 0:
            return
        now = datetime.now()
        with self.state.state_lock:
            positions = list(self.state.positions.items())
        for ul, pos in positions:
            if pos.exit_pending:
                continue
            held_minutes = (now - pos.entry_time).total_seconds() / 60.0
            if held_minutes >= cfg.max_hold_minutes:
                print(
                    f"[TIME-EXIT] {ul}: held {held_minutes:.0f}m "
                    f">= max {cfg.max_hold_minutes}m — exiting (theta guard)"
                )
                with self.state.exit_lock:
                    if pos.exit_pending:
                        continue
                    pos.exit_pending = True
                self.orders.place_exit(ul, f"MaxHoldTime({cfg.max_hold_minutes}m)")

    def _is_market_hours(self) -> bool:
        hm = int(datetime.now().strftime("%H%M"))
        return MARKET_HOURS_START <= hm <= MARKET_HOURS_END

    def _print_startup_info(self) -> None:
        cfg = self.config
        print("=" * 70)
        print(f"  {cfg.strategy_name}{'  [PAPER TRADE]' if cfg.paper_trade else ''}")
        print("=" * 70)
        print(f"  API Host        : {cfg.api_host}")
        print(f"  WebSocket URL   : {cfg.ws_url if cfg.ws_url else '(SDK auto-derive from host)'}")
        print(f"  Underlyings     : {', '.join(cfg.underlyings)}")
        print(f"  FNO Exchange    : {cfg.fno_exchange}")
        print(f"  Min Score       : {cfg.min_score} | Max Trap: {cfg.max_trap}")
        print(f"  SL Points       : {cfg.premium_stop_pts} | TGT Points: {cfg.premium_target_pts}")
        print(f"  Trail Mode      : {cfg.trail_sl_mode}")
        print(f"  Breakeven SL    : {'disabled' if cfg.breakeven_at_gain_pct <= 0 else f'{cfg.breakeven_at_gain_pct:.0f}% of target gain'}")
        print(f"  Long Only Mode  : {cfg.long_only_mode}")
        print(f"  Broker SL Orders: {cfg.broker_sl_orders}")
        print(f"  DTE Range       : {cfg.dte_min} – {cfg.dte_max} days")
        print(f"  Candle Interval : {cfg.candle_interval}")
        print(f"  Check Interval  : {cfg.signal_check_interval}s")
        print("-" * 70)
        print(f"  [RISK GATES]")
        print(f"  Max Trades/Day  : {cfg.max_trades_per_session or 'unlimited'}")
        print(f"  Max Consec Loss : {cfg.max_consecutive_losses}")
        print(f"  Daily Loss Limit: ₹{cfg.max_daily_loss_amount:.0f}"
              + (f" | {cfg.max_daily_loss_pct:.1f}%" if cfg.max_daily_loss_pct > 0 else ""))
        print(f"  Daily Profit Tgt: {'disabled' if cfg.max_daily_profit_amount <= 0 else f'₹{cfg.max_daily_profit_amount:.0f}'}")
        print(f"  Entry Cooldown  : {cfg.entry_cooldown_secs}s per underlying")
        print(f"  [TIMING]")
        print(f"  No New Entries  : after {cfg.no_new_trade_after} IST")
        print(f"  EOD Square-Off  : {cfg.square_off_time} IST")
        print(f"  Max Hold Time   : {'disabled' if cfg.max_hold_minutes <= 0 else f'{cfg.max_hold_minutes}m per trade'}")
        if cfg.trade_journal_path:
            print(f"  Trade Journal   : {cfg.trade_journal_path}")
        if cfg.paper_trade:
            print(f"\n  *** PAPER TRADE MODE — no real orders will be sent ***")
        print("=" * 70)

    def scan_underlying(self, symbol: str) -> None:
        """Full scan pipeline for one underlying.  Called from strategy thread."""
        cfg    = self.config
        state  = self.state
        orders = self.orders

        if symbol in state.positions:
            return

        allowed, gate_reason = self.risk.check_gates(symbol)
        if not allowed:
            print(f"[SCAN] {symbol} blocked by risk gate: {gate_reason}")
            return

        spot_q = self.fetcher.fetch_quote(symbol, self.fetcher.underlying_exchange(symbol))
        spot   = float(spot_q.get("ltp", 0) or 0)
        if not spot:
            print(f"[SCAN] {symbol}: no spot LTP")
            return

        expiry = self.fetcher.fetch_target_expiry(symbol)
        if not expiry and not cfg.allow_checkpoint_fallback:
            print(f"[SCAN] {symbol}: no expiry in DTE range {cfg.dte_min}–{cfg.dte_max} — skip")
            return

        # Fetch option chain
        chain_rows, expiry_used = self.fetcher.fetch_option_chain(symbol, expiry)
        if not chain_rows:
            print(f"[SCAN] {symbol}: empty option chain")
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

        ce_bid = float(atm_row.get("ce_bid", 0) or 0) or None
        ce_ask = float(atm_row.get("ce_ask", 0) or 0) or None
        pe_bid = float(atm_row.get("pe_bid", 0) or 0) or None
        pe_ask = float(atm_row.get("pe_ask", 0) or 0) or None

        # IV rank
        iv_rank_val = self.fetcher.fetch_iv_rank(spot_q)
        if iv_rank_val is None:
            iv_rank_val = self.scorer.iv_rank(
                float(spot_q.get("iv", 0) or 0) or None,
                cfg.iv_52w_low,
                cfg.iv_52w_high,
            )

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
            prev_spot=prev_spot_ltp,
            prev_sf_ltp=prev_sf_ltp,
        )

        # ── Formatted scoring panel ──────────────────────────────────────────
        _s        = result.score
        _trap     = result.trap_score
        _signal   = result.signal
        _dir_ico  = "▲" if _s > 0 else ("▼" if _s < 0 else "◆")
        _sig_ico  = "✔" if _signal == "EXECUTE" else ("⚡" if _signal == "WATCH" else "✘")
        _nfill    = int(abs(_s) / 100 * 16)
        _score_bar = "█" * _nfill + "░" * (16 - _nfill)
        _sep      = "─" * 79
        print(f"  ── SCAN · {symbol}  {'─' * max(1, 68 - len(symbol))}")
        print(f"      {_dir_ico} {result.label:<10}  score {_s:+d}/100  {_score_bar}  trap {_trap}/100   {_sig_ico} {_signal}")
        print(f"  {_sep}")
        _cbar_w = 8
        for c in result.components:
            _cfill = int(abs(c.score) / max(c.score_max, 0.01) * _cbar_w)
            _cbar  = "█" * _cfill + "░" * (_cbar_w - _cfill)
            print(f"     {c.score:+.0f}/{c.score_max:.0f}  {_cbar}  {c.label:<20} {c.note}")
        print(f"  {_sep}")
        if result.trap_reasons:
            print(f"  ⚠ TRAP {_trap}  ·  {'  ·  '.join(result.trap_reasons)}")
        if _signal != "EXECUTE":
            print(f"  {_sig_ico} {_signal}  —  not executing  (score {abs(_s)}/100, min {cfg.min_score})")
            print()
            return
        print(f"  ✔ EXECUTE  {_dir_ico}  {result.direction}")
        print()
        direction = result.direction
        if cfg.long_only_mode and direction not in ("CE", "PE"):
            return
        if direction is None:
            return

        best = self.strikes.select_best(symbol, smoothed, spot, direction, iv_rank_val)
        if best is None:
            if cfg.allow_checkpoint_fallback:
                best = StrikeSelector.simple_otm(smoothed, spot, direction, cfg.otm_offset)
                if best:
                    print(f"[SCAN] {symbol}: using simple OTM fallback strike {best.get('strike')}")
            if best is None:
                print(f"[SCAN] {symbol}: no qualifying strike found — skip")
                return

        opt_key    = "ce_symbol" if direction == "CE" else "pe_symbol"
        opt_symbol = best.get(opt_key)
        if not opt_symbol:
            print(f"[SCAN] {symbol}: strike {best.get('strike')} has no {direction} symbol — skip")
            return

        lotsize = int(best.get("lotsize", 1) or 1)
        fixed_qty = max(1, cfg.lot_multiplier) * lotsize

        available  = self.risk.available_capital()
        risk_cap   = available * (cfg.risk_percent / 100.0)
        risk_per_unit = cfg.premium_stop_pts
        risk_qty   = int(risk_cap / risk_per_unit) if risk_per_unit > 0 else 0
        # Round down to lot size boundary
        risk_qty   = (risk_qty // lotsize) * lotsize if lotsize > 0 else risk_qty
        qty = min(fixed_qty, risk_qty) if risk_qty > 0 else 0
        if qty <= 0:
            est_premium = float(best.get("ce_ltp" if direction == "CE" else "pe_ltp", 0) or 0)
            min_risk_pct = (cfg.premium_stop_pts * lotsize / available * 100) if available > 0 else 0.0
            print(
                f"[SCAN] {symbol}: qty=0 — 1 lot risk exceeds cap "
                f"(stop ₹{cfg.premium_stop_pts} pts × {lotsize} units = ₹{cfg.premium_stop_pts*lotsize:.0f}/lot, "
                f"risk cap ₹{risk_cap:.0f} @ {cfg.risk_percent}% of ₹{available:.0f} available; "
                f"need RISK_PERCENT≥{min_risk_pct:.1f}%)"
            )
            return

        print(
            f"[SCAN] {symbol}: placing {direction} entry | strike {best.get('strike')} "
            f"| {opt_symbol} x{qty}"
        )
        self._send_telegram(
            f"🔍 Signal: {symbol} {direction}\n"
            f"Score: {result.score:+d} | Trap: {result.trap_score}\n"
            f"Strike: {best.get('strike')} | {opt_symbol} x{qty}\n"
            f"Reasons: {'; '.join(result.reasons[:3])}",
            1,
        )
        orders.place_entry(symbol, opt_symbol, qty, spot, direction)

    def _strategy_thread(self) -> None:
        """Clock-anchored strategy scan loop."""
        cfg = self.config
        print("[STRATEGY] Strategy scan thread started")
        while True:
            try:
                self.orders.check_pending_entries()
                self.orders.check_pending_exits()
                if cfg.broker_sl_orders and not cfg.paper_trade:
                    self.orders.check_broker_order_fills()

                if cfg.square_off_time:
                    now_hm = datetime.now().strftime("%H:%M")
                    if now_hm >= cfg.square_off_time:
                        with self.state.state_lock:
                            open_positions = list(self.state.positions.keys())
                        if open_positions:
                            print(
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
                if self._is_market_hours():
                    for symbol in cfg.underlyings:
                        self.scan_underlying(symbol)
                else:
                    print("[STRATEGY] Outside market hours — skipping signal scan")

            except Exception as exc:
                print(f"[STRATEGY ERROR] {exc}")

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
            print("[WS-TEST] SKIP — 'websockets' package not installed")
            return

        cfg   = self.config
        ws_url = cfg.ws_url
        if not ws_url:
            print("[WS-TEST] SKIP — ws_url not configured (set WEBSOCKET_URL)")
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
                print(f"[WS-TEST] REST API key OK (orderbook: {_n} order(s))")
            else:
                _rest_msg = _rest_data.get("message", str(_rest_data))
                print(f"[WS-TEST] WARN: REST API key check failed: {_rest_msg}")
                print(f"[WS-TEST]       If REST also returns 'Invalid API key', the key in OPENALGO_API_KEY is wrong.")
                print(f"[WS-TEST]       Get the correct key from: {cfg.api_host}/apikey")
        except Exception as _rest_exc:
            print(f"[WS-TEST] REST check skipped: {_rest_exc}")

        print(f"[WS-TEST] Testing {ws_url} ...")

        async def _run() -> None:
            try:
                async with _websockets.connect(ws_url, open_timeout=10) as ws:
                    print("[WS-TEST] Transport OK — WebSocket handshake succeeded")

                    await ws.send(_json.dumps({
                        "action": "authenticate",
                        "api_key": cfg.api_key,
                    }))
                    raw = await _aio.wait_for(ws.recv(), timeout=10)
                    resp = _json.loads(raw)
                    status = resp.get("status") or resp.get("type", "")
                    if status not in ("success", "authenticated"):
                        code = resp.get("code", "")
                        print(f"[WS-TEST] FAIL — auth rejected: {resp}")
                        if code == "AUTHENTICATION_ERROR" or "Invalid API key" in resp.get("message", ""):
                            print(
                                f"[WS-TEST] HINT: The API key in OPENALGO_API_KEY does not match"
                                f" any key stored in the OpenAlgo database."
                                f"\n[WS-TEST]       1. Log in to your OpenAlgo dashboard"
                                f"\n[WS-TEST]       2. Go to API Key page (Account → API Key)"
                                f"\n[WS-TEST]       3. Copy the key and set OPENALGO_API_KEY=<copied-key> in your .env"
                            )
                        return
                    print(f"[WS-TEST] Auth OK")

                    await ws.send(_json.dumps({
                        "action": "subscribe",
                        "symbols": [TEST_SYMBOL],
                        "mode": "ltp",
                    }))
                    print(f"[WS-TEST] Subscribed {TEST_SYMBOL['exchange']}:{TEST_SYMBOL['symbol']}")

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
                            print(f"[WS-TEST] Tick #{tick_count} — ltp={ltp}")
                            if tick_count >= 3:
                                break
                        except _aio.TimeoutError:
                            print(f"[WS-TEST] (no tick yet, {remaining:.0f}s remaining...)")

                    if tick_count == 0:
                        print(
                            f"[WS-TEST] WARNING — connected & authenticated but 0 ticks "
                            f"in {TICK_WAIT}s. Market may be closed or WS server has no feed."
                        )
                    else:
                        print(f"[WS-TEST] PASS — received {tick_count} tick(s) ✓")

            except OSError as exc:
                print(f"[WS-TEST] FAIL — cannot reach {ws_url}: {exc}")
                print("[WS-TEST] Check: Is the WebSocket server running? Is /ws proxied to port 8765 in Caddy/nginx?")
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
                print(f"[WS-TEST] FAIL — {_exc_type}: {exc}{_hint}")

        try:
            _aio.run(_run())
        except RuntimeError:
            # Already inside a running event loop (e.g. eventlet) — skip test
            print("[WS-TEST] SKIP — cannot run async test inside existing event loop")

    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start WebSocket + strategy threads, run until KeyboardInterrupt."""
        cfg = self.config
        self._verify_registration()  # WC-14: check strategy config first
        self._print_startup_info()
        self._check_open_positions_on_startup()  # WC-01: restore broker positions

        self._send_telegram(
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

        print(f"[BOT] {cfg.strategy_name} running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Stopping bot...")
            for ul in list(self.state.positions.keys()):
                print(f"[SHUTDOWN] Closing {ul} position...")
                self.orders.place_exit(ul, "Bot Shutdown")
        finally:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self._send_telegram(f"🛑 {cfg.strategy_name} stopped", 1)
            print("[BOT] Shutdown complete")


# ===============================================================================
# ENTRY POINT
# ===============================================================================

if __name__ == "__main__":
    config = BotConfig.from_env()
    config.validate()

    if not config.api_key or config.api_key == "openalgo-apikey":
        print(
            "[WARNING] OPENALGO_API_KEY is not set in environment.\n"
            "          Export it before running: export OPENALGO_API_KEY=your-key"
        )

    bot = OptionsBuyerEdgeBot(config)
    bot.run()
