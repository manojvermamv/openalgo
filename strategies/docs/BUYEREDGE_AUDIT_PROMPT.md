# BuyerEdgeStrategy - Autonomous Institutional Audit Prompt

Last updated: 2026-05-23
Source of truth: strategies/examples/BuyerEdgeStrategy.py

## Executive Summary
This document is the updated, production-ready audit and implementation record for BuyerEdgeStrategy.

Current status:
- CHUNK 1 (P0/P1 fixes): Completed
- CHUNK 2 (Worst-case scenarios WC-01 to WC-14): Completed (PASS)
- CHUNK 3 (Loop control exit criteria): Completed
- CHUNK 4 (Priority implementation order): Completed
- CHUNK 5/6 (Institutional upgrades): Partially implemented (see status matrix)

Important reality check:
- Core reliability and correctness work is complete.
- Several institutional enhancements are implemented.
- Some optional upgrades remain pending by design.

---

## CHUNK 0 - Current Feature Inventory

### Layer 1 - Technical Signal
- EMA crossover/trend continuation
- RSI momentum
- MACD histogram expansion/contraction
- Spot vs VWAP

Behavior notes:
- Uses closed bars (`iloc[-2]`) for signal stability.
- EMA crossover has minimum-bar guards.
- VWAP is intraday-only (today-filtered) for correctness.

### Layer 2 - OI Flow Intelligence
- PCR OI level
- CE flow classifier
- PE flow classifier
- OI wall positioning

Behavior notes:
- Chain snapshots are smoothed with bounded history (`deque(maxlen=lookback_bars)`).
- Trend direction fields are added per strike.

### Layer 3 - Greeks and Regime
- Delta imbalance: active
- Gamma regime: active when GEX data exists, neutral fallback otherwise
- OI velocity (U3): active, additive component

Behavior notes:
- OI velocity is implemented as an additional signal component.
- Gamma is not treated as dead weight in current code.

### Layer 4 - Straddle and IV
- IV regime (IVR)
- Straddle velocity with ATM-shift guard

Behavior notes:
- On ATM strike shift, previous straddle baseline is reset to avoid false velocity signals.

### Layer 5 - Synthetic Futures Confirmation
- Spot/SF co-movement confirmation
- Spread suppression checks
- Basis/backwardation context handling

### Composite Scoring
Current implementation uses dynamic normalization:

```python
MAX_RAW_SCORE = sum(c.score_max for c in components)
final_score = clamp((raw_score / MAX_RAW_SCORE) * 100, -100, 100)
```

Signal tiers:
- EXECUTE: `abs(score) >= effective_min_score` and `trap_score <= max_trap`
- WATCH: `abs(score) >= 30`
- NO_TRADE: otherwise

Time-of-day weighting (U8) can modify `effective_min_score`.

### Strike Selection and Entry Pipeline
- OTM filter window
- OI/volume minimum thresholds
- Delta target filter (configurable)
- Asymmetry scoring
- Fallback selection when enabled

Added protection:
- Hard spread block at scan stage (U4)
- Same-strike re-entry guard (U5)
- Pre-trade liquidity preflight in order path (U7)

### Position Sizing
- Risk-capped lot sizing
- Adaptive lot multiplier option (U9)
- Qty=0 guard with diagnostic output

### Position Management
- Premium and spot trail logic (tick-driven)
- Breakeven migration
- Broker protection orders (SL-M and LIMIT)
- Pending entry/exit reconciliation loop
- Broker external-fill reconciliation and journaling

### Startup Checks
Current startup behavior includes:
- Strategy registration verification (`_verify_registration`)
- Open-position restore from broker (`_check_open_positions_on_startup`)
- Broker SL/TGT order ID recovery where available
- WS resubscribe for restored option and spot symbols
- WebSocket self-test before run loop

---

## CHUNK 1 - Autonomous Audit Protocol (Status)

### Audit Loop Logic
For each category (ORD, RSK, SIG, WS, THR, DAT, PNL, RECOV, EDGE, PERF):
1. Read relevant implementation lines
2. Mark each item PASS/BUG/OPTIMIZE/UPGRADE
3. Fix required bugs
4. Re-read patched methods
5. Re-run worst-case scenarios

### Completed P0 Fixes
1. PNL-2: Journal write for broker-triggered exits
2. WS-2: Watchdog guard on `_last_tick_time`
3. ORD-5: `exit_pending` guard before broker fill handling
4. THR-1: lock-ordering consistency (`state_lock` before `exit_lock`)

### Completed P1 Fixes
1. SIG-6: VWAP today-only calculation
2. SIG-5: EMA insufficient-bar guard
3. ORD-2: Partial-fill handling path
4. ORD-4: SL pre-check before `modifyorder`
5. WS-5: subscription lock around subscribe/unsubscribe
6. RSK-2: `entry_in_flight` race prevention
7. RSK-3: daily reset correctness
8. EDGE-1: market-hours gating for scan/data flow
9. WC-09 cutover logic: post-cutoff pending-entry handling

Open high-risk bugs from CHUNK 1: none.

---

## CHUNK 2 - Worst-Case Scenario Checklist (WC-01 to WC-14)

Result: PASS

Coverage summary:
- Restart/recovery paths: PASS
- WS disconnect/reconnect: PASS
- Entry timeout/pending reconciliation: PASS
- Broker external fills and journaling: PASS
- EOD square-off race handling: PASS
- ATM shift handling: PASS
- Low-capital qty gate behavior: PASS
- Daily halt and next-day reset: PASS
- Post-cutoff entry immediate-exit logic: PASS
- Paper mode behavior parity: PASS
- Expiry-day behavior: PASS
- Trap gating for new entries only: PASS
- Daily profit halt behavior: PASS
- Registration wipe detection path: PASS

---

## CHUNK 3 - Loop Control Exit Condition

Exit condition checklist:
- All WC scenarios PASS: yes
- No `# TODO:` markers in strategy file: yes
- No `# BUG:` markers in strategy file: yes
- P0/P1 implemented: yes
- P2/P3 either implemented or explicitly deferred: yes

Result: DONE

---

## CHUNK 4 - Implementation Priority Order (Current State)

### P0 Critical
- Implemented: PNL-2, WS-2, ORD-5, THR-1

### P1 High
- Implemented: SIG-6, SIG-5, ORD-2, ORD-4, WS-5, RSK-2, WC-09

### P2 Medium
- Implemented: RECOV-1, EDGE-1, DAT-6
- Deferred/partial: EDGE-5, DAT-3

### P3 Optimize
- Implemented/partially addressed: SIG-1 (dynamic score sum)
- Deferred: RSK-1, PNL-1, PERF-1, PNL-3/RECOV-3

---

## CHUNK 5/6 - Institutional Upgrade Implementation Status

### Upgrade Status Matrix

| Upgrade | Title | Status | Notes |
|---|---|---|---|
| U1 | Dynamic ATR-based SL | Implemented | Entry SL policy component wired through scan/order register path |
| U2 | Greeks-aware deep OTM exit | Implemented | Delta cache + threshold-triggered exit |
| U3 | OI velocity signal | Implemented (variant) | Additive component, not a strict gamma replacement |
| U4 | Hard entry spread block | Implemented | Scan-stage spread gate |
| U5 | Duplicate same-strike re-entry guard | Implemented (enhanced) | Count-based daily cap per option+direction |
| U6 | Drawdown-rate risk halt | Implemented | Config + rolling P&L history + velocity gate in risk checks |
| U7 | Pre-trade liquidity preflight | Implemented | Bid/spread check in `place_entry` |
| U8 | Time-of-day score weighting | Implemented | Morning stricter, power-hour relaxed |
| U9 | Adaptive lot sizing | Implemented | Feature-flagged and bounded |
| U10 | Telegram session footer + heartbeat | Pending | Footer/heartbeat path not yet integrated |

### Gamma Clarification for Upgrades
- Earlier docs treated gamma as disabled/dead.
- Actual code now includes gamma-regime scoring logic.
- U3 OI velocity is currently additive, not a one-for-one replacement of gamma.

### Upgrade Priority View (Remaining - Low)
- U-P3 (U10) remaining: Telegram session footer and heartbeat

---

## Architecture and Workflow Impact (Updated)

### Entry Flow (Updated)
1. Risk gates and session timing checks
2. Time-of-day score weighting computes `effective_min_score`
3. Signal score + trap gating
4. Strike selection and hard spread block (U4)
5. Same-strike re-entry guard (U5)
6. Adaptive lot multiplier (U9, if enabled)
7. Pre-trade liquidity preflight (U7)
8. Order placement and reconciliation

### Exit Flow (Updated)
1. Tick/broker/EOD trigger
2. `exit_pending` lock-protected guard
3. Broker protection cancel with pre-filled detection
4. Exit fill reconciliation and journal write
5. WS unsubscribe cleanup

No class additions, no file additions, architecture remains within existing 9-class design.

---

## Environment and Setup Updates

### New/Updated Environment Variables
Implemented feature flags and settings include:
- `ENTRY_SL_MODE`, `DYNAMIC_SL_ATR_PERIOD`, `DYNAMIC_SL_ATR_MULT`, `DYNAMIC_SL_MIN_PTS`, `DYNAMIC_SL_MAX_PTS` (U1)
- `DELTA_EXIT_THRESHOLD` (U2)
- `OI_VELOCITY_ENABLED`, `OI_VELOCITY_THRESHOLD` (U3)
- `MAX_ENTRY_SPREAD_PCT` (U4)
- `SAME_STRIKE_REENTRY_GUARD_ENABLED`, `MAX_SAME_STRIKE_TRADES_PER_DAY` (U5)
- `DRAWDOWN_RATE_ENABLED`, `DRAWDOWN_RATE_WINDOW_MINS`, `DRAWDOWN_RATE_MAX_LOSS` (U6)
- `PREFLIGHT_SPREAD_CHECK`, `PREFLIGHT_MAX_SPREAD_PCT`, `PREFLIGHT_MIN_BID` (U7)
- `MORNING_SESSION_END`, `AFTERNOON_POWER_START`, `POWER_HOUR_SCORE_FACTOR`, `MORNING_SCORE_FACTOR` (U8)
- `ADAPTIVE_SIZING_ENABLED`, `ADAPTIVE_MAX_LOT_MULT`, `ADAPTIVE_WIN_STREAK_TRIGGER`, `ADAPTIVE_WIN_STREAK_STEP` (U9)

Pending (not yet in code):
- U10 notification footer/heartbeat variables

### Dependency and Package Notes
- No dependency/package version upgrades were required for current implemented fixes/upgrades.
- Existing `openalgo` and pandas/TA usage remains unchanged.

---

## Migration Notes (Behavioral Changes)

These are operationally important behavior changes:
1. Score normalization is dynamic by active component weights.
2. Same-strike re-entry can now be blocked by default configuration.
3. Preflight checks can reject entries that previously would have gone through.
4. Time-of-day weighting can alter entry frequency by session regime.
5. Adaptive sizing can increase lot multiplier when enabled.

Recommended rollout:
1. Paper mode burn-in with current env values
2. Validate preflight reject rates and re-entry guard behavior
3. Enable adaptive sizing only after stable baseline performance

---

## Changelog / Release Notes

### 2026-05-23
- Completed CHUNK 1-4 reliability and worst-case remediation program.
- Implemented U1, U2, U3 (variant), U4, U5 (enhanced), U6, U7, U8, U9.
- Added registration verification and startup position restore workflow.
- Added post-cutoff pending-entry immediate-exit logic.
- Added race protection (`entry_in_flight`, subscription lock, exit guards).
- Added broker-fill journaling and reconciliation hardening.
- Updated documentation to reflect implemented vs pending upgrades accurately.

---

## Final Readiness Statement

Production core: READY

Institutional enhancement backlog remaining:
- U10 Telegram session-context footer

This document intentionally distinguishes completed production reliability work from optional enhancement backlog.
