# BuyerEdgeStrategy â€” Autonomous Institutional Audit Prompt

> **Engine**: This prompt is designed for a fully autonomous AI coding agent.
> **Loop rule**: After every implementation phase, re-run the worst-case checklist.
> If any item fails â†’ return to PHASE 1 (Audit) for that category only.
> Do NOT exit until every worst-case in PHASE 5 returns âœ… PASS.
>
> **Constraint**: No new classes. No new files. No infra changes.
> All fixes must stay within the existing 9-class architecture.

---

## CHUNK 0 â€” CURRENT FEATURE INVENTORY (Read before touching anything)

### How to use this section
Read this first. Do NOT skip. This is the ground truth of what the strategy already does.
Every bug, optimization, and upgrade in later phases must be evaluated *against* this inventory.
If a feature is already implemented, mark it DONE and do not re-implement it.

---

### Layer 1 â€” Technical Signal (max 4 pts of 15)

| Sub-layer | What it does | Input | Output |
|-----------|-------------|-------|--------|
| EMA Crossover (L1-a) | Detects fresh bull/bear crossover OR trend continuation on EMA(9)/EMA(21) | 15m spot candles | âˆ’1 to +1 pts |
| RSI Momentum (L1-b) | Reads RSI(14): >53 = bullish, <47 = bearish, with 0.5 gradations | 15m spot candles | âˆ’1 to +1 pts |
| MACD Histogram (L1-c) | Expanding positive â†’ +1; expanding negative â†’ âˆ’1; contracting = Â±0.5 | 15m spot candles | âˆ’1 to +1 pts |
| Spot vs VWAP (L1-d) | Spot above/below VWAP using intraday volume-weighted pivot | 15m spot candles with volume | âˆ’1 to +1 pts |

**Notes for trader**: All technicals use `iloc[-2]` (last closed bar, not live bar). EMA crossover uses 3-bar lookback to detect fresh cross vs sustained trend. MACD uses standard 12/26/9. VWAP uses full bar history.

---

### Layer 2 â€” OI Flow Intelligence (max 6 pts of 15)

| Sub-layer | What it does | Edge |
|-----------|-------------|------|
| PCR OI Level (L2-a) | Put/Call OI ratio: <0.6 â†’ bullish (call overload), >1.3 â†’ bearish (put overload) | Crowd positioning |
| Call Flow Classifier (L2-b) | 8-state matrix: Price Ã— Volume Ã— OI trend direction â†’ Call Buying/Writing/Covering/Unwinding | Smart money detection |
| Put Flow Classifier (L2-c) | Same 8-state matrix for PE side | Smart money detection |
| OI Wall Position (L2-d) | Spot relative to max call OI (resistance) and max put OI (support) | Range awareness |

**Chain smoothing**: Last N snapshots (deque `lookback_bars`) are SMA-averaged per strike for OI/Vol/Premium to eliminate single-bar noise. 6 trend-direction fields (`ce_ltp_dir`, `ce_oi_dir`, etc.) appended per row. Single-bar fallback uses 2-factor (OI change + LTP change) classifier.

**Notes for trader**: This layer carries the most weight (6/15). The 8-state matrix catches institutional write/cover cycles that raw PCR misses. OI wall position penalises entries near hard resistance (call wall = overhead supply).

---

### Layer 3 â€” Greeks Engine (1 of 3 pts active; 2 pts DISABLED)

| Sub-layer | Status | What it does |
|-----------|--------|-------------|
| Delta Imbalance (L3-a) | **ACTIVE (max 1 pt)** | ATM CE delta + PE delta sum â†’ net bias. Falls back to CE/PE LTP ratio if delta API unavailable |
| Gamma Regime (L3-b) | **DISABLED (0 pts, max 2)** | GEX-based gamma flip not available; always scores 0, contributes nothing to MAX_RAW_SCORE=15 |

**Notes for trader**: Gamma Regime dead weight is documented but NOT counted in MAX_RAW_SCORE (correctly excluded). Delta proxy via LTP ratio fires when `optiongreeks` API returns nothing.

---

### Layer 4 â€” Straddle & IV (max 3 pts of 15)

| Sub-layer | What it does | Edge |
|-----------|-------------|------|
| IV Regime / IVR (L4-a) | IV percentile rank vs 52-week range (configurable low/high). IVR<20 â†’ full buyer edge (+1), IVR>60 â†’ expensive (âˆ’1) | Structural pricing edge |
| Straddle Velocity (L4-b) | ATM straddle (CE+PE LTP) change % scan-over-scan. Expanding â†’ buyer edge; Contracting â†’ IV crush trap | Volatility regime detection |

**ATM-shift guard**: Straddle velocity is set to `None` if the ATM strike changes between scans. Prevents false velocity signal from moneyness shift. Stored as `{"strike": atm_k, "price": straddle_price}`.

**Entry gate**: `iv_rank_max_entry=40` blocks ALL new entries (at `StrikeSelector.select_best`) if IVR â‰¥ 40, regardless of other scores.

---

### Layer 5 â€” Synthetic Futures Co-movement (max 1 pt of 15)

| Check | Signal |
|-------|--------|
| Spot and SF both move same direction > 0.03% threshold | Co-movement confirms direction (+1 or -1) |
| CE bid-ask spread > 1.5% | Signal suppressed (execution cost too high â†’ 0 pts) |
| SF vs Spot basis in backwardation | Noted in output but no score deduction |

**Fallback**: For equity underlyings without syntheticfuture API, falls back to `{symbol}{expiry}FUT` quote. For indices, uses `client.syntheticfuture()`.

---

### Composite Scoring

```
MAX_RAW_SCORE = 15
raw_score = sum(component.score)
final_score = (raw_score / 15) * 100 â†’ clamped to [âˆ’100, +100]
```

**Signal tiers**:
- `EXECUTE`: `|score| >= min_score` AND `trap_score <= max_trap`
- `WATCH`: `|score| >= 30`
- `NO_TRADE`: everything else, OR trap_score > max_trap

**Trap Score** (0â€“100, gates new entries only â€” does NOT exit running positions):
- Straddle contracting: +25
- IVR > 60: +20
- SF basis divergence > 1.5%: +15
- CE spread > 1.5%: +15
- PE spread > 1.5%: +15
- Extreme PCR (>2.5 or <0.4): +10

---

### Strike Selection

**Pipeline**:
1. Filter by OTM range (CE: spot â†’ spotÃ—1.05; PE: spotÃ—0.95 â†’ spot)
2. Filter by OI â‰¥ `min_oi_filter=50k` and Volume â‰¥ `min_vol_filter=10k`
3. If delta API available: filter by `delta_target_low=0.25 â‰¤ |delta| â‰¤ delta_target_high=0.45`
4. Score each candidate by **4-component asym_score**:
   - `(1 âˆ’ IVR/100) Ã— 0.40` â€” lower IV = better buyer edge
   - `oi_concentration Ã— 0.30` â€” relative OI depth at this strike
   - `vol_concentration Ã— 0.20` â€” intraday activity share
   - `delta_proximity Ã— 0.10` â€” closeness to delta target midpoint
5. Best score â‰¥ `asym_score_threshold=0.35` â†’ selected; else fallback
6. **Fallback**: `simple_otm()` picks `otm_offset` strikes from ATM if `allow_checkpoint_fallback=True`

---

### Position Sizing

```python
fixed_qty = lot_multiplier Ã— lotsize
risk_qty  = floor((available_capital Ã— risk_percent/100) / premium_stop_pts / lotsize) Ã— lotsize
qty       = min(fixed_qty, risk_qty)   # two-way cap
```

`qty=0` is a hard gate â€” produces detailed error message showing required `RISK_PERCENT`.

---

### Position Management (real-time via WebSocket ticks)

**Three independent SL mechanisms** (all active simultaneously):

| Mechanism | Activation | Ratchet |
|-----------|-----------|---------|
| Premium Trail SL | `trail_activate_at_pct`% gain on entry premium | Ratchets `trail_step_rr_pct`% per new high |
| Spot Trail SL | `spot_reward_pct`% spot move in direction | Ratchets `trail_step_rr_pct`% per new spot high/low |
| Breakeven SL | `breakeven_at_gain_pct`% of (tgtâˆ’entry) gain reached | Moves SL to entry cost once, never repeats |

**Broker-level protection** (placed immediately after entry fill, if `broker_sl_orders=True`):
- SL-M SELL order at initial `premium_stop_pts` below entry
- LIMIT SELL order at `premium_target_pts` above entry
- Both are cancelled before any software-triggered exit
- Both are modified via `modifyorder` when trail SL ratchets

---

### Risk Gates (session-level, checked before every entry)

| Gate | Default | Scope |
|------|---------|-------|
| Max trades per session | 5 | Per calendar day |
| Max consecutive losses | 3 | Resets on profitable trade |
| Per-symbol entry cooldown | 300s | Per underlying |
| Daily loss â‚¹ | â‚¹2,000 | Absolute daily P&L floor |
| Daily loss % | 0 (disabled) | % of available capital |
| Daily profit target halt | 0 (disabled) | Absolute daily P&L ceiling |
| No new entry after | 13:30 IST (env) | Clock gate |
| EOD force square-off | 15:15 IST | Force-closes all positions |
| Max hold time | 0 (disabled) | Per-position theta guard |

---

### Execution Flow

```
Entry:  MARKET BUY â†’ poll fill (15Ã—2s) â†’ register OptionPosition
            â†’ place broker SL-M + LIMIT target
            â†’ subscribe WS for option symbol + spot symbol

Exit:   cancel_broker_orders() [check pre-filled]
            â†’ MARKET SELL â†’ poll fill (15Ã—2s)
            â†’ record_exit(pnl) â†’ write_journal â†’ unsubscribe WS
```

**Pending reconciliation** (safety nets, run every cycle):
- `check_pending_entries()`: re-polls fill for any submitted BUY that never got fill confirmation
- `check_pending_exits()`: re-polls fill for any submitted SELL that never got confirmation
- `check_broker_order_fills()`: polls broker SL/target order IDs for external fills

---

### Paper Trade Mode

`PAPER_TRADE=true` â†’ simulated fills from `ltp_map` (WS LTP). No real orders. Journal rows labelled `PAPER`. All risk/trail logic identical to live.

---

### Startup Checks

- `_check_open_positions_on_startup()`: reads `positionbook` to log any open positions (prints, does NOT auto-restore)
- `_test_websocket()`: asyncio smoke-test: connect â†’ auth â†’ subscribe Nifty 50 â†’ await ticks â†’ PASS/FAIL with detailed hints

---

## CHUNK 1 â€” AUTONOMOUS AUDIT PROTOCOL

### How to execute this protocol

```
FOR EACH category in [ORD, RSK, SIG, WS, THR, DAT, PNL, RECOV, EDGE, PERF]:
    1. READ every line in BuyerEdgeStrategy.py relevant to that category
    2. CHECK each sub-item against the code â€” confirm present, absent, or broken
    3. RECORD findings: PASS / BUG / OPTIMIZE / UPGRADE
    4. IMPLEMENT all BUG fixes for this category
    5. Re-read the patched code to verify fix is correct
    6. Move to next category
END
THEN run CHUNK 2 (worst-case scenarios)
IF any scenario FAILS â†’ return to the relevant category and fix
REPEAT until all 14 scenarios PASS
```

---

### Category ORD â€” Order Management

**ORD-1**: Entry fill with `average_price=0`
- Check: If `poll_order_status` returns fill but `average_price=0`, fallback reads `price` field. If both zero â†’ abort and log. âœ… Already handled.

**ORD-2**: Partial fills
- Check: `poll_order_status` waits for `status=complete`. A `status=open` with partial qty will keep polling until timeout.
- **BUG**: No partial fill detection. After timeout, `pending_entries` is popped but no position is registered for the partial. Position orphaned.
- **Fix**: In `poll_order_status`, if `status=open` AND `filled_quantity > 0` AND elapsed > 80% of poll window â†’ treat filled qty as the position qty, not the requested qty.

**ORD-3**: EOD square-off race with broker SL
- Check: `place_exit` cancels broker orders before placing market sell. `cancel_broker_orders` checks pre-filled status first.
- **BUG**: If broker SL fills at the exact same moment EOD square-off runs, `cancel_broker_orders` catches it in `broker_filled` and short-circuits the SELL. But `pnl` recorded uses `executed_price` from broker fill; `_write_journal` is called. `record_exit` is called. Position removed. âœ… Correct path.
- **OPTIMIZE**: Add `exit_pending` guard before EOD for-loop (already present via `if pos.exit_pending: continue`). âœ…

**ORD-4**: `modifyorder` on already-filled SL order
- Check: `modify_broker_sl` calls `modifyorder` without pre-checking if the SL is already filled.
- **BUG**: If SL fills at broker side and `_sl_modify_callback` fires from trail at the same instant, `modifyorder` will return an error. Error is caught and printed but not acted on.
- **Fix**: Before calling `modifyorder`, call `orderstatus` for `sl_order_id`. If `status=complete`, call `_trigger_exit` and skip modify.

**ORD-5**: Double exit via broker fill + WS trail
- Check: `check_broker_order_fills` calls `record_exit` and removes position from `state.positions`. If WS trail fires after removal, `_check_premium_trail` iterates `state.positions` which no longer contains the symbol â†’ no double exit. âœ…
- But: `check_broker_order_fills` does NOT set `exit_pending=True` before acting. A WS exit callback might fire between the fill detection and the position removal.
- **Fix**: Set `pos.exit_pending = True` and add to `exit_queue` at the start of `check_broker_order_fills` handling, before removing the position.

**ORD-6**: `place_exit` when order submission fails (resp is None or non-success)
- Check: If `resp` is None or status != success, `order_id = None` â†’ prints log â†’ discards `exit_pending` and retries. âœ… Correct retry path exists.

**ORD-7**: `cancelorder` on stale `sl_order_id` from a previous session
- **UPGRADE**: On strategy startup (after `_check_open_positions_on_startup`), read all open orders from `orderbook` API. For any open SL-M or LIMIT order for the strategy name that does NOT correspond to a current tracked position, cancel it. This prevents ghost orders accumulating across restarts.

---

### Category RSK â€” Risk Management

**RSK-1**: `available_capital()` cached 60s, P&L delta approximation
- Check: Between broker polls, `delta_pnl = _daily_pnl - _pnl_at_last_fetch` adjusts the cached value. This is accurate for closed trades but misses open MTM.
- **OPTIMIZE**: In `available_capital()`, also subtract open position MTM loss from unrealized: for each `pos` in `state.positions`, if `ltp_map` has LTP, add `(ltp - entry_premium) * qty` to delta. This gives a better real-time capital estimate.

**RSK-2**: `check_gates` reads state under lock but not `available_capital`
- Check: `available_capital()` is NOT called under `state_lock`. If called from two threads simultaneously, two calls could both pass the capital check and both attempt entry.
- **BUG**: Multi-underlying scan runs sequentially in a single thread â†’ no race. But `record_entry` is called in `place_entry` (OrderManager thread) AFTER `check_gates` passes (scan loop). A rapid second symbol scan could get through gates before first entry is recorded.
- **Fix**: Add a per-call `entry_in_flight` flag to `BotState` (just a `threading.Event` or a simple counter protected by `state_lock`). `check_gates` checks `entry_in_flight > 0`. `place_entry` sets it before order placement, clears on completion or failure.

**RSK-3**: `_maybe_reset_daily_state` called inside `check_gates` without `state_lock`
- Check: `today != self._session_date` â†’ reset. But `_session_date` is read outside lock.
- **Fix**: Wrap the date check inside `state_lock` as well, or use a dedicated `_reset_lock`.

**RSK-4**: `max_daily_loss_pct` gate reads `available_capital()` every call
- Check: `available_capital()` may trigger a broker API call if cache expired. This adds latency to every `check_gates` call during high scan frequency.
- **OPTIMIZE**: Only recompute `max_loss_amt` when capital is freshly fetched. Cache `_max_loss_amount_cache` alongside `_funds_cache`.

**RSK-5**: Consecutive losses counter never documented to reset at new day
- Check: `_maybe_reset_daily_state` sets `_session_consecutive_losses = 0`. âœ… Correct.

---

### Category SIG â€” Signal Engine

**SIG-1**: `MAX_RAW_SCORE = 15` hardcoded in `score()` method
- Check: If any component's `score_max` changes (e.g., Gamma Regime gets enabled), MAX_RAW_SCORE must be manually updated. A mismatch silently miscalibrates ALL scores.
- **OPTIMIZE**: Derive `MAX_RAW_SCORE` dynamically: `max(1, sum(c.score_max for c in components))`. This prevents miscalibration when a disabled layer gets activated.

**SIG-2**: Signal label logic after trap check
- Check: `signal = "NO_TRADE"` when `trap_score > cfg.max_trap`. Then `elif abs_score >= cfg.min_score` â†’ EXECUTE. But the `elif` is never reached when trap kills it. âœ… Correct.

**SIG-3**: `reasons` list deduplication via `dict.fromkeys(reasons)` 
- Check: Already implemented. âœ…

**SIG-4**: Gamma Regime "dead weight" documentation
- **UPGRADE**: Add a config field `gamma_regime_enabled: bool = False` and a comment block explaining how to enable it when/if a GEX endpoint becomes available. Right now there's no path to activation. Make the dead weight intentional and visible.

**SIG-5**: EMA uses `iloc[-2]` and `iloc[-3]` for crossover detection
- **EDGE CASE**: If `len(df) == slow_ema_period + 2`, then `iloc[-3]` is valid. But `ta.ema` needs at least `slow_ema_period` bars. If `len(df) < slow_ema_period + 3`, `iloc[-3]` references before the EMA is stabilized (NaN zone).
- **Fix**: Guard with `len(df) >= slow_ema_period + 3` for crossover detection; use `>= slow_ema_period + 2` only for trend continuation (fast > slow at [-2]).

**SIG-6**: VWAP `ta.vwap` called with full history (multi-day if `lookback_days > 1`)
- **BUG**: VWAP should be reset daily. Multi-day VWAP is not standard. Using 5 days of 15m bars gives a meaningless VWAP anchor.
- **Fix**: Filter `df_spot` to today's date before computing VWAP: `df_today = df_spot[df_spot.index.date == today]`. If `df_today` has < 5 bars, skip VWAP score.

---

### Category WS â€” WebSocket

**WS-1**: `_subscriptions` registry can grow unbounded
- Check: `subscribe` adds, `unsubscribe` discards. If `place_exit` fails before `unsubscribe`, the symbol stays in registry.
- **OPTIMIZE**: On each reconnect, cross-check `_subscriptions` against `state.positions`. Remove any subscription not tied to an active position or underlying.

**WS-2**: Watchdog timer uses wall-clock `time.time()` comparison
- Check: `elapsed = time.time() - self._last_tick_time`. If `_last_tick_time = 0.0` (no tick ever received), `elapsed` is huge â†’ immediately triggers reconnect on first watchdog cycle.
- **BUG**: On first connect before any tick arrives, watchdog at 60s fires: `elapsed > 120` is False (only 60s elapsed), so no false reconnect. But if market is open and still no tick at 180s, reconnect fires. This is correct.
- Actually: `_last_tick_time = 0.0` and `elapsed = time.time() - 0.0` â‰ˆ 1.7 billion seconds â†’ `elapsed > 120` is always True on FIRST watchdog check if no tick ever received.
- **BUG CONFIRMED**: First watchdog check (at ~60s after connect) will always force reconnect if `_last_tick_time` was never set, creating a reconnect loop.
- **Fix**: Change guard to `if self._last_tick_time > 0 and elapsed > 120:` (only trigger if at least one tick was ever received).

**WS-3**: `_on_ws_data` iterates `state.positions` without lock
- Check: `list(self._state.positions.items())` is called without `state_lock`. Dict can change size during iteration (position added by strategy thread).
- **BUG**: In Python 3.12, iterating a dict snapshot via `list(d.items())` is safe from `RuntimeError: dictionary changed size` since we took a snapshot. But the snapshot may include a position whose `pos` object is being modified concurrently.
- **OPTIMIZE**: The `pos` attributes being read (`exit_pending`, `symbol`, `spot_symbol`) are primitive booleans and strings â€” reads are atomic in CPython. Acceptable risk. Add a comment explaining the GIL-safe read pattern.

**WS-4**: Subscription replay uses `list(self._subscriptions)` (set snapshot)
- Check: âœ… Correct â€” snapshot avoids mutation-during-iteration.

**WS-5**: `subscribe_ltp` called from strategy thread AND from reconnect thread
- **BUG**: Both `_ws_thread` (replaying subscriptions) and `scan_underlying` (new subscription via `orders.place_entry â†’ register_filled_entry â†’ ws.subscribe`) can call `client.subscribe_ltp` concurrently.
- **Fix**: Add `_subscribe_lock = threading.Lock()` to `WebSocketManager`. Wrap all `subscribe_ltp`/`unsubscribe_ltp` calls in this lock.

---

### Category THR â€” Threading & Locking

**THR-1**: Lock ordering â€” `exit_lock` and `state_lock` 
- Acquire order in codebase:
  - `_trigger_exit`: acquires `exit_lock` â†’ then reads `state.positions` (no `state_lock`)
  - `place_exit`: acquires `state_lock` then `exit_lock` (via `state_queue.discard`)
  - `check_broker_order_fills`: acquires `state_lock` then operates
- **BUG**: Inconsistent lock ordering between `exit_lock` and `state_lock`. If `_trigger_exit` (holding `exit_lock`) and `check_gates` (acquiring `state_lock`) run concurrently while `place_exit` holds `state_lock` then acquires `exit_lock` â†’ deadlock possible.
- **Fix**: Establish a strict lock order: always acquire `state_lock` BEFORE `exit_lock`. Audit every acquisition site and enforce this order.

**THR-2**: `_check_max_hold` acquires `state_lock` then calls `place_exit` (which also acquires `state_lock`)
- **BUG**: `_check_max_hold` acquires `state_lock` to get positions list, releases it, then calls `place_exit`. `place_exit` acquires `state_lock` again. This is correct (not nested). âœ… No deadlock.
- But: Between the lock release and `place_exit` call, the position could be removed by WS exit. `place_exit` handles missing position with early return. âœ…

**THR-3**: `record_entry` and `record_exit` both use `state_lock`
- Check: âœ… Correct.

---

### Category DAT â€” Data Fetching

**DAT-1**: `fetch_option_chain` â€” lotsize defaults to 1 if missing
- Check: `lotsize = ce.get("lotsize") or pe.get("lotsize") or 1`. If broker returns lot size of 0, this also defaults to 1. âœ… Safe.

**DAT-2**: `fetch_target_expiry` tries multiple date formats
- Check: Tries `%d%b%y`, `%d-%b-%y`, `%d%b%Y`, `%d-%b-%Y`. âœ… Robust.

**DAT-3**: `fetch_synthetic_future` â€” equity fallback uses futures quote
- **EDGE**: Equity stocks may not have active futures. `{symbol}{expiry}FUT` may return 0 LTP on expiry day or for illiquid stocks.
- **Fix**: Add `if ltp < spot * 0.8 or ltp > spot * 1.2: ltp = None` sanity check (futures price > 20% away from spot is clearly bad data).

**DAT-4**: `fetch_iv_rank` reads `spot_q.get("iv")` â€” broker may return IV as string
- Check: `float(atm_iv)` call. If `atm_iv = "N/A"` or `""`, `float()` raises `ValueError`. Caught by outer `except Exception`. âœ… Safe but silently returns None.

**DAT-5**: `fetch_atm_greeks` makes 2 API calls per scan per underlying
- **OPTIMIZE**: Cache greeks per (symbol, expiry, strike) with a 30s TTL. For 9 underlyings Ã— 2 calls = 18 API calls per 60s cycle. Caching halves this.

**DAT-6**: `fetch_option_chain` with `strike_count=8` may miss the optimal OTM strike
- **EDGE**: For high-volatility days, ATM could be at the edge of the 8-strike window. OTM+1 might be outside the fetched chain.
- **Fix**: Request `strike_count = config.strike_count + config.otm_offset + 2` to always include `otm_offset` strikes beyond ATM on both sides.

---

### Category PNL â€” P&L Accuracy

**PNL-1**: P&L calculated as `(exit - entry) * qty` â€” ignores brokerage/STT
- **OPTIMIZE**: Add a `brokerage_per_lot: float = 0.0` config field. Deduct `brokerage_per_lot * (qty // lotsize)` from P&L in `_write_journal` and `record_exit`. Keeps risk tracking realistic.

**PNL-2**: `check_broker_order_fills` calls `record_exit(pnl)` but does NOT call `_write_journal`
- **BUG CONFIRMED**: When broker SL or target fills externally (detected via polling), `record_exit(pnl)` is called and position is removed. But `_write_journal` is never called â†’ missing entry in CSV trade log.
- **Fix**: Call `self._write_journal(underlying, pos, executed_price, pnl, reason)` before removing the position in `check_broker_order_fills`.

**PNL-3**: `_daily_pnl` not persisted across restarts
- Check: On restart, `_daily_pnl = 0.0`. If strategy crashed mid-session after 3 trades, daily loss limit resets. Existing positions are not restored either.
- **UPGRADE**: Write a `_state_checkpoint.json` file on each `record_exit` with `{date, daily_pnl, trade_count, consecutive_losses}`. On startup, read and restore if same date.

---

### Category RECOV â€” Crash Recovery & Restart

**RECOV-1**: `_check_open_positions_on_startup` only PRINTS positions â€” does not restore
- **UPGRADE**: After printing, for each open position found in `positionbook`:
  1. Try to match symbol to a known underlying
  2. If matched, reconstruct an `OptionPosition` with `entry_premium = average_price`, `qty = netqty`, default SL/TGT from config
  3. Add to `state.positions`
  4. Subscribe WS for the symbol and its underlying
  5. Restore broker SL/target orders if `broker_sl_orders=True` (query `orderbook` for open orders matching symbol+SELL)
- This prevents orphaned positions from running unprotected after a crash.

**RECOV-2**: Strategy registration in `strategy_configs.json` is ephemeral (server wipe)
- Check: Documented in CLAUDE.md. No persistent volume on Coolify.
- **UPGRADE**: Add a `deploy_check()` call at startup that verifies the strategy is registered. If not, print a prominent warning with the exact `register_strategy.py` command to run.

**RECOV-3**: `pending_entries` and `pending_exits` not persisted
- **UPGRADE** (low priority): On each modification of `pending_entries`/`pending_exits`, append to the same `_state_checkpoint.json`. On startup, poll each pending order ID immediately to resolve it before starting the scan loop.

---

### Category EDGE â€” Market Edge Cases

**EDGE-1**: Pre-market / post-market API calls
- Check: `fetch_option_chain` and `fetch_quote` will return empty or stale data outside 9:15â€“15:30 IST. Strategy has no explicit market-hours gate.
- **FIX**: Add `_is_market_hours()` helper: `hhmm = int(datetime.now().strftime("%H%M")); return 915 <= hhmm <= 1530`. Skip full scan if not market hours. Already partially handled by `no_new_trade_after` but not for data fetch calls.

**EDGE-2**: Index holiday / exchange halt
- **UPGRADE**: If `fetch_quote` returns `ltp=0` for 3 consecutive cycles for ALL underlyings â†’ print `[SCAN] All quotes zero â€” possible market holiday or exchange halt. Sleeping 5 minutes.` and sleep.

**EDGE-3**: Lot size change mid-session (NSE circular)
- Check: `lotsize` is read from chain row on each scan. If NSE changes lot size intraday (rare but happens on expiry month rollover), the next scan picks up the new size automatically. âœ… Safe.

**EDGE-4**: Expiry-day entry
- Check: `dte_min` config gate. DTE is calculated as `(exp_date - now.date()).days`. On expiry day, `dte = 0 < dte_min` â†’ correctly blocked. âœ…

**EDGE-5**: Circuit-hit option (frozen LTP, zero bid-ask)
- **UPGRADE**: In `StrikeSelector.select_best`, add check: if `ce_bid == 0 and ce_ask == 0` for a CE candidate â†’ skip (frozen/circuit). Add same for PE.

**EDGE-6**: Weekend / public holiday WS reconnect loop
- Check: WS watchdog only triggers during `900 <= hm <= 1535`. Outside these hours it keeps the connection but does NOT force reconnect on silence. âœ… No weekend reconnect storm.

---

### Category PERF â€” Performance

**PERF-1**: `scan_underlying` makes 6â€“8 API calls per underlying per cycle
- Calls: `fetch_quote`, `fetch_target_expiry`, `fetch_option_chain`, `fetch_spot_candles`, `fetch_synthetic_future`, `fetch_atm_greeks` (Ã—2), `fetch_option_delta` (Ã—N candidates in strike selection)
- **OPTIMIZE**: Parallelize independent calls (fetch_spot_candles, fetch_option_chain, fetch_synthetic_future) using `concurrent.futures.ThreadPoolExecutor` within `scan_underlying`. Keep within 2â€“3 worker threads.

**PERF-2**: `chain_history` deque entries are full list copies
- Check: `chain_hist.append(chain_rows)` appends a full list of dicts on every scan. For 9 underlyings Ã— `lookback_bars=5` Ã— ~20 strikes Ã— 15 fields = ~13,500 dict entries in memory.
- **OPTIMIZE**: Store only the fields needed for smoothing (`SMOOTH_FIELDS`) in the history deque, not full chain rows. Reduces memory by ~40%.

**PERF-3**: `_on_ws_data` loops ALL positions for EVERY tick
- Check: For each tick, iterates all `state.positions` twice (option trail + spot trail). With 5 positions Ã— 2 loops = 10 iterations per tick. At 10 ticks/second this is negligible.
- âœ… No action needed unless positions scale beyond 10.

**PERF-4**: `fetch_option_delta` called once per candidate in strike selection
- For 5 candidates, this is 5 serial API calls inside `select_best`. Combined with the rest of `scan_underlying`, this adds 10s+ per underlying.
- **OPTIMIZE**: Set `delta_target_low=0` and `delta_target_high=1` to disable delta filtering (use OI/Vol/IV asym only) when low latency is critical. Document this as a config trade-off.

---

## CHUNK 2 â€” WORST-CASE SCENARIO CHECKLIST

Run this checklist after completing all CHUNK 1 fixes. Each scenario must produce the stated PASS outcome with NO exceptions, NO orphaned positions, NO missed exits.

---

**WC-01**: Server restart mid-trade (position open, no fills pending)

*Trigger*: Strategy process killed with open `RELIANCE CE` position.
*Expected PASS*:
- `_check_open_positions_on_startup()` detects the position in `positionbook`
- Position is restored to `state.positions` with entry price from broker
- WS subscriptions re-established for option symbol and spot symbol
- Broker SL order queried from `orderbook`; if still open, `sl_order_id` restored
- Trail SL resumes from current LTP (no pre-crash peak information â†’ `trail_active=False`, will activate fresh)
- No Telegram alert saying "orphaned position"

---

**WC-02**: WS disconnects with open position

*Trigger*: Network drop. `_ws_thread` catches exception. `_last_tick_time` is 200s old at next watchdog check.
*Expected PASS*:
- `_last_tick_time > 0` guard prevents false reconnect before first tick âœ… (after WS-2 fix)
- Exception catches â†’ `time.sleep(5)` â†’ `client.connect()` retried
- On reconnect: `_subscriptions` replayed for all active position symbols + spots
- Trail SL logic is tick-driven â†’ resumes from next received tick
- `_last_tick_time` reset on first tick of new connection
- No duplicate subscriptions (subscription registry uses `set`)

---

**WC-03**: Entry order placed, fill never received (broker timeout / exchange reject after submission)

*Trigger*: `place_entry` submits order, `poll_order_status` returns `None` after 15Ã—2s=30s.
*Expected PASS*:
- `pending_entries` entry was added before polling
- After `poll_order_status` returns None â†’ `pending_entries.pop(underlying)`
- `place_entry` returns `False` â€” no position registered
- `check_pending_entries` on next cycle polls the order ID again (1 retry)
- If still incomplete and order was actually filled (race) â†’ position registered from pending reconciliation
- If rejected: `pending_entries` cleaned up, no orphan

---

**WC-04**: Broker SL fills externally BEFORE strategy polls it

*Trigger*: Market gaps down hard. Broker SL-M fires immediately. Strategy is sleeping in `time.sleep(sleep_secs)`.
*Expected PASS*:
- On next cycle, `check_broker_order_fills` detects `order_status=complete` for `sl_order_id`
- `exit_pending` set to `True` BEFORE position removal (after ORD-5 fix)
- `record_exit(pnl)` called
- `_write_journal` called (after PNL-2 fix)
- WS unsubscribed for option and spot
- Position removed from `state.positions`
- `exit_queue` cleared
- No double-exit: WS trail cannot fire after `exit_pending=True`

---

**WC-05**: EOD square-off with existing broker SL active

*Trigger*: 15:15 IST reached. Position has `sl_order_id` and `tgt_order_id` both open.
*Expected PASS*:
- `_strategy_thread` detects `now_hm >= square_off_time`
- `pos.exit_pending = True` set under `exit_lock` before calling `place_exit`
- `place_exit("EOD-SquareOff")` calls `cancel_broker_orders()`
- `cancel_broker_orders` checks pre-fill status: if SL already filled â†’ short-circuits, uses broker fill price
- If not filled: cancels SL-M and LIMIT orders, confirms cancel
- Places MARKET SELL for remaining qty
- No double-sell: `exit_pending` flag prevents WS trail from firing simultaneously

---

**WC-06**: ATM strike changes between two consecutive scans

*Trigger*: NIFTY moves 200 points between scan cycles. ATM shifts from 24500 to 24600.
*Expected PASS*:
- `prev_straddle = state.prev_straddle.get(symbol)` returns `{"strike": 24500, "price": 350}`
- New `atm_k = 24600` â‰  `prev_str["strike"]` â†’ `prev_straddle_price = None`
- `straddle_price` set to new ATM straddle
- `state.prev_straddle[symbol] = {"strike": 24600, "price": new_price}`
- L4-b Straddle Velocity scores 0 (unavailable) â€” no false expansion/contraction signal
- Next scan can correctly compute velocity from the new ATM anchor

---

**WC-07**: Capital falls below minimum lot cost

*Trigger*: Capital = â‚¹5,000. BANKNIFTY lotsize=15. `premium_stop_pts=30`. 1 lot risk = â‚¹450. `risk_percent=1%` â†’ risk cap = â‚¹50. `risk_qty = floor(50/30)=1 â†’ (1//15)Ã—15 = 0`.
*Expected PASS*:
- `qty = min(fixed_qty=15, risk_qty=0) = 0`
- Detailed diagnostic: `"qty=0 â€” 1 lot risk â‚¹450/lot, risk cap â‚¹50 @ 1% of â‚¹5000; need RISK_PERCENTâ‰¥9.0%"`
- No order placed, no exception
- Strategy continues scanning other underlyings

---

**WC-08**: Max consecutive losses halt â†’ resumes next day

*Trigger*: 3rd consecutive loss recorded. `check_gates` returns `(False, "Loss streak reached")`.
*Expected PASS*:
- All scan entries blocked for rest of session
- Open positions continue (gates block only new entries, not exits)
- Next calendar day: `_maybe_reset_daily_state()` detects new date
- `_session_consecutive_losses = 0`, all counters reset
- First `check_gates` of new day returns `(True, "")`

---

**WC-09**: Multiple underlyings â€” pending entry exactly at EOD cutoff

*Trigger*: NIFTY entry order submitted, still polling fill. BANKNIFTY has open position. 15:15 hits.
*Expected PASS*:
- EOD loop: BANKNIFTY in `state.positions` â†’ `place_exit("EOD-SquareOff")`
- NIFTY NOT in `state.positions` â†’ not touched by EOD loop
- `check_pending_entries`: polls NIFTY order â†’ if filled after 15:15 â†’ register fill â†’ immediately queue exit with reason `"PostCutoffEntry"`
- If order not yet filled â†’ cancel via `cancelorder` API

---

**WC-10**: Full paper trade session

*Trigger*: `PAPER_TRADE=true`. NIFTY CE signal fires.
*Expected PASS*:
- `place_entry` simulates fill from `ltp_map` (or `spot Ã— 0.01` fallback)
- Zero real orders sent (`placeorder` never called)
- No broker SL/target orders placed
- Trail SL logic identical to live mode
- `_write_journal` writes row with `mode=PAPER`
- `place_exit` uses `ltp_map.get(pos.symbol)` as simulated exit price
- Telegram alerts prefixed "ðŸ“„ PAPER"
- `record_exit` tracks daily simulated P&L correctly

---

**WC-11**: Expiry day (DTE=0)

*Trigger*: Today is NIFTY weekly expiry. Only available expiry has `dte=0`.
*Expected PASS*:
- `fetch_target_expiry`: `dte = 0 < dte_min=7` â†’ not selected â†’ returns `None`
- `scan_underlying` with `allow_checkpoint_fallback=False` â†’ skip with log
- With `allow_checkpoint_fallback=True` â†’ chain fetched without explicit expiry (broker default)
- Broker may return the expiring-day chain â€” near-expiry risk logged as warning

---

**WC-12**: IVR spikes while holding a position (post-entry IV crush)

*Trigger*: IVR was 25 at entry. Jumps to 80 mid-hold. Straddle contracting. Trap score = 45.
*Expected PASS*:
- Trap score is computed in `scan_underlying` â†’ gates NEW entries only
- Running `OptionPosition` is NOT exited by trap score
- Premium trail SL fires as option premium erodes
- `scan_underlying` returns `signal="NO_TRADE"` â†’ no new entry for this underlying
- Breakeven SL (if previously activated) prevents full premium loss

---

**WC-13**: `max_daily_profit_amount` hit mid-session

*Trigger*: `max_daily_profit_amount=3000`. After 2 wins, `_daily_pnl = â‚¹3,200`.
*Expected PASS*:
- `check_gates`: `3200 >= 3000` â†’ `(False, "Daily profit target reached â‚¹3200...")`
- No new entries for rest of session
- Existing open positions continue under WS trail SL
- EOD square-off still fires at configured time

---

**WC-14**: Strategy restart with wiped `strategy_configs.json`

*Trigger*: Coolify redeploys. `/app/strategies/strategy_configs.json` gone. Python runner does not launch bot.
*Expected PASS* (with RECOV-2 upgrade):
- Startup `_verify_registration()` detects missing config entry
- Prints: `[STARTUP] âš  Strategy 'OptionsBuyerEdgeBot' not found in strategy_configs.json.`
- Prints: `[STARTUP]   Run: python3 /app/strategies/register_strategy.py`
- If launched manually â†’ runs but warns prominently
- No silent failure

---

## CHUNK 3 â€” LOOP CONTROL

```
LOOP:
    implemented_categories = set()
    for scenario in [WC-01 .. WC-14]:
        result = trace_code_path(scenario)
        if result == FAIL:
            cat = map_to_category(scenario)
            if cat not in implemented_categories:
                implement_all_fixes(cat)
                implemented_categories.add(cat)

    failures = [s for s in [WC-01..WC-14] if trace_code_path(s) == FAIL]
    if not failures:
        DONE  â† exit loop
    else:
        continue LOOP
```

**Exit condition**: All 14 WC = PASS. No `# TODO:` or `# BUG:` markers in code. All P0/P1 items implemented. P2/P3 either implemented or explicitly deferred with inline comment.

---

## CHUNK 4 â€” IMPLEMENTATION PRIORITY ORDER

### P0 â€” Critical (silent data loss / crash risk)
1. **PNL-2** â€” `_write_journal` missing in `check_broker_order_fills`
2. **WS-2** â€” Watchdog false reconnect on `_last_tick_time=0`
3. **ORD-5** â€” Missing `exit_pending` guard in `check_broker_order_fills`
4. **THR-1** â€” Lock ordering deadlock audit (`exit_lock` vs `state_lock`)

### P1 â€” High (correctness)
5. **SIG-6** â€” VWAP multi-day data bug (filter to today only)
6. **SIG-5** â€” EMA `iloc[-3]` on insufficient bars
7. **ORD-2** â€” Partial fill â†’ orphaned entry
8. **ORD-4** â€” `modifyorder` on already-filled SL
9. **WS-5** â€” Concurrent `subscribe_ltp` (add `_subscribe_lock`)
10. **RSK-2** â€” Entry-in-flight race (add `_entry_in_flight` counter)
11. **WC-09 fix** â€” Post-cutoff pending entry â†’ immediate exit queue

### P2 â€” Medium (robustness)
12. **RECOV-1** â€” Restore open positions on startup
13. **EDGE-1** â€” Market-hours gate for data fetches
14. **EDGE-5** â€” Circuit-hit option filter (zero bid-ask)
15. **DAT-3** â€” SF price sanity (Â±20% from spot)
16. **DAT-6** â€” `strike_count + otm_offset + 2` in chain fetch

### P3 â€” Optimize (quality of life)
17. **SIG-1** â€” Dynamic MAX_RAW_SCORE from component sum
18. **RSK-1** â€” Open MTM in capital estimate
19. **PNL-1** â€” Brokerage deduction in P&L
20. **PERF-1** â€” Parallel data fetches per underlying
21. **PNL-3 / RECOV-3** â€” State checkpoint JSON for crash restart

---

## CHUNK 5 â€” IMPLEMENTATION TEMPLATE

For each fix, use this exact template. Always re-read the patched method in full after applying. Never apply two fixes to the same method simultaneously.

```
## FIX: <CATEGORY-N> â€” <Short title>
### Location: <ClassName> â†’ <method_name>() â†’ ~line N
### Root cause:
<One sentence describing the exact failure>
### Code before:
```python
<exact current code, 3â€“5 lines of context>
```
### Code after:
```python
<fixed code>
```
### Verification: Run WC-NN â†’ should PASS
```

---

## CHUNK 6 â€” INSTITUTIONAL UPGRADES

> **Scope**: These upgrades elevate the strategy to institutional-grade standards.
> All stay within the 9-class architecture â€” no new classes, no new files.
> Each upgrade is tagged with the class it touches and the trader benefit.
> Implement only after all P0/P1 bugs from CHUNK 1 are resolved.

---

### UPGRADE-1 â€” Dynamic SL Based on Realized Volatility (ATR-Scaled Stop)

**Class**: `BotConfig` + `scan_underlying` + `OrderManager.register_filled_entry`
**Problem**: Fixed `premium_stop_pts=30` is blind to current volatility. On high-vol sessions (NIFTY ATR=150), â‚¹30 SL is noise â€” guaranteed stop-out within minutes. On quiet sessions (ATR=40), â‚¹30 is too wide for the option's expected range.
**Trader benefit**: SL automatically widens on volatile sessions (fewer false stop-outs) and tightens on quiet sessions (faster damage control).

**Implementation**:
```python
# BotConfig â€” add:
dynamic_sl_enabled:     bool  = False
dynamic_sl_atr_period:  int   = 14       # ATR lookback period (bars)
dynamic_sl_atr_mult:    float = 1.5      # SL = ATR Ã— this multiplier
dynamic_sl_min_pts:     float = 15.0     # floor â€” never tighter than this
dynamic_sl_max_pts:     float = 80.0     # ceiling â€” never wider than this

# In scan_underlying(), after df_spot is fetched:
if cfg.dynamic_sl_enabled and df_spot is not None and len(df_spot) >= cfg.dynamic_sl_atr_period + 2:
    atr_series = ta.atr(df_spot["high"], df_spot["low"], df_spot["close"],
                        period=cfg.dynamic_sl_atr_period)
    atr_val = float(atr_series.iloc[-2])
    dynamic_sl = max(cfg.dynamic_sl_min_pts,
                     min(cfg.dynamic_sl_max_pts, atr_val * cfg.dynamic_sl_atr_mult))
    print(f"[SCAN] {symbol}: ATR={atr_val:.1f} â†’ dynamic SL = â‚¹{dynamic_sl:.1f}")
else:
    dynamic_sl = cfg.premium_stop_pts

# Pass dynamic_sl into place_entry():
orders.place_entry(symbol, opt_symbol, qty, spot, direction, sl_pts=dynamic_sl)

# OrderManager.register_filled_entry() â€” add sl_pts parameter:
def register_filled_entry(self, ..., sl_pts: float | None = None):
    sl_pts = sl_pts if sl_pts is not None else self.config.premium_stop_pts
    sl  = executed - sl_pts
    tgt = executed + self.config.premium_target_pts
```

---

### UPGRADE-2 â€” Greeks-Aware Exit (Deep OTM Auto-Bail)

**Class**: `WebSocketManager` + `BotConfig`
**Problem**: A position entered at 0.35 delta can drift to 0.05 delta (near-zero probability) after an adverse spot move. The premium trail SL may not fire fast enough as the option bleeds slowly from â‚¹85 â†’ â‚¹30 â†’ â‚¹15 over multiple candles. The buyer holds an expensive lottery ticket.
**Trader benefit**: Exit automatically when the option has lost most of its directional probability â€” stop paying theta on a broken trade.

**Implementation**:
```python
# BotConfig â€” add:
delta_exit_threshold: float = 0.10   # exit if live |delta| < this; 0 = disabled

# WebSocketManager â€” add delta cache:
self._delta_cache: dict[str, tuple[float, float]] = {}   # symbol â†’ (delta, timestamp)

def _get_cached_delta(self, option_symbol: str, ttl: float = 30.0) -> float | None:
    cached = self._delta_cache.get(option_symbol)
    if cached and (time.time() - cached[1]) < ttl:
        return cached[0]
    # Non-blocking background fetch â€” does not stall WS handler
    threading.Thread(
        target=self._fetch_and_cache_delta,
        args=(option_symbol,), daemon=True,
    ).start()
    return cached[0] if cached else None   # return stale value while refresh is in-flight

def _fetch_and_cache_delta(self, option_symbol: str) -> None:
    # Find underlying for this option symbol from active positions
    underlying = next(
        (ul for ul, pos in self._state.positions.items() if pos.symbol == option_symbol), None
    )
    if not underlying:
        return
    try:
        resp = self.client.optiongreeks(
            symbol=option_symbol,
            exchange=self.config.fno_exchange,
            underlying_symbol=underlying,
            underlying_exchange=(self.config.index_exchange
                                 if underlying in self.config.index_underlyings
                                 else self.config.spot_exchange),
        )
        if resp and resp.get("status") == "success":
            delta = resp.get("greeks", {}).get("delta")
            if delta is not None:
                self._delta_cache[option_symbol] = (abs(float(delta)), time.time())
    except Exception:
        pass

# In _check_premium_trail(), at the top before SL checks:
if self.config.delta_exit_threshold > 0 and not pos.exit_pending:
    live_delta = self._get_cached_delta(pos.symbol)
    if live_delta is not None and live_delta < self.config.delta_exit_threshold:
        print(f"[WS] DEEP OTM EXIT {underlying}: delta {live_delta:.3f} < "
              f"threshold {self.config.delta_exit_threshold}")
        self._trigger_exit(underlying, f"DeepOTM_delta_{live_delta:.3f}")
        return
```

---

### UPGRADE-3 â€” OI Build/Unwind Velocity (Replace Dead Gamma Regime)

**Class**: `SignalEngine.score` + `BotConfig` + `OIFlowAnalyzer`
**Problem**: Gamma Regime (L3-b) contributes 0 pts but holds 2 `score_max` weight slots. The dead slot reduces effective score resolution. Replace with OI velocity: detects whether institutions are *accumulating or distributing* positions â€” velocity-based vs. the level-based PCR in L2-a.
**Trader benefit**: Catches early institutional accumulation (OI building + price rising = conviction buying) or distribution (OI building + price falling = writer trap). Different signal from PCR because it's a rate-of-change measure.

**Implementation**:
```python
# BotConfig â€” add:
oi_velocity_enabled:    bool  = True
oi_velocity_threshold:  float = 0.05   # minimum OI change rate to register as signal

# SignalEngine.score() â€” replace the Gamma Regime block (L3-b):
s10 = 0
oi_vel_note = "OI velocity unavailable"
if cfg.oi_velocity_enabled and chain_rows:
    ce_oi_chg = sum(r.get("ce_oi_chg", 0) or 0 for r in chain_rows)
    pe_oi_chg = sum(r.get("pe_oi_chg", 0) or 0 for r in chain_rows)
    ce_oi_tot = sum(r.get("ce_oi", 1) or 1 for r in chain_rows)
    pe_oi_tot = sum(r.get("pe_oi", 1) or 1 for r in chain_rows)
    ce_vel = ce_oi_chg / ce_oi_tot if ce_oi_tot else 0.0
    pe_vel = pe_oi_chg / pe_oi_tot if pe_oi_tot else 0.0
    th = cfg.oi_velocity_threshold
    # Use s6 (CE flow score) and s7 (PE flow score) as context
    if   ce_vel >  th and s6 > 0:  s10 =  1; oi_vel_note = f"CE OI building {ce_vel:+.2%} + call buying â€” institutional accumulation"
    elif ce_vel >  th and s6 < 0:  s10 = -1; oi_vel_note = f"CE OI building {ce_vel:+.2%} + call writing â€” institutional writer trap"
    elif ce_vel < -th and s6 > 0:  s10 =  0.5; oi_vel_note = f"CE OI unwinding {ce_vel:+.2%} â€” short covering"
    elif pe_vel >  th and s7 < 0:  s10 = -1; oi_vel_note = f"PE OI building {pe_vel:+.2%} + put buying â€” bearish accumulation"
    elif pe_vel >  th and s7 > 0:  s10 =  1; oi_vel_note = f"PE OI building {pe_vel:+.2%} + put writing â€” institutional support"
    elif pe_vel < -th and s7 < 0:  s10 = -0.5; oi_vel_note = f"PE OI unwinding {pe_vel:+.2%} â€” put covering"
    else: oi_vel_note = f"OI velocity below threshold (CE {ce_vel:+.2%}, PE {pe_vel:+.2%})"
else:
    oi_vel_note = "OI velocity disabled"
_c("OI Velocity", s10, 2, _dir(s10), oi_vel_note)
# NOTE: MAX_RAW_SCORE stays at 15 (OI Velocity replaces Gamma's 2 slots, net neutral)
# Apply SIG-1 dynamic MAX_RAW_SCORE fix to auto-handle future slot changes
```

---

### UPGRADE-4 â€” Hard Bid-Ask Spread Block at Entry (Not Just Trap Score)

**Class**: `scan_underlying` in `OptionsBuyerEdgeBot`
**Problem**: Trap score penalises wide spreads but does NOT prevent the entry. A 25% spread means the buyer pays â‚¹12.50 in slippage on a â‚¹100 option â€” consuming 25% of a â‚¹50 target before the trade even starts. Real desks have a hard pre-trade spread limit.
**Trader benefit**: Hard no-go if execution cost exceeds a configurable fraction of the expected reward.

**Implementation**:
```python
# BotConfig â€” add:
max_entry_spread_pct: float = 8.0    # refuse entry if bid-ask spread > this %; 0 = off

# In scan_underlying(), after best strike is selected, before place_entry():
if cfg.max_entry_spread_pct > 0:
    bid_key = "ce_bid" if direction == "CE" else "pe_bid"
    ask_key = "ce_ask" if direction == "CE" else "pe_ask"
    bid = float(best.get(bid_key, 0) or 0)
    ask = float(best.get(ask_key, 0) or 0)
    mid = (bid + ask) / 2 if (bid and ask) else 0.0
    if mid > 0:
        live_spread_pct = (ask - bid) / mid * 100
        if live_spread_pct > cfg.max_entry_spread_pct:
            print(
                f"[SCAN] {symbol}: entry blocked â€” spread {live_spread_pct:.1f}% "
                f"> max {cfg.max_entry_spread_pct:.1f}% (bid={bid}, ask={ask})"
            )
            return
```

---

### UPGRADE-5 â€” Duplicate Trade Prevention (Same-Strike Re-Entry Guard)

**Class**: `BotState` + `RiskManager._maybe_reset_daily_state` + `scan_underlying`
**Problem**: After a stop-out on NIFTY 24500CE, the next scan can fire a fresh entry on the exact same strike. The algorithm re-enters the position that just proved to be wrong â€” algorithmic revenge-trading.
**Trader benefit**: Once a specific option instrument is traded (win or loss), it is blocked for the rest of the session. Re-entry allowed only at a different strike or next day.

**Implementation**:
```python
# BotState â€” add:
self._traded_today: set[str] = set()   # "option_symbol|direction" traded this session

def mark_traded(self, option_symbol: str, direction: str) -> None:
    with self.state_lock:
        self._traded_today.add(f"{option_symbol}|{direction}")

def was_traded_today(self, option_symbol: str, direction: str) -> bool:
    with self.state_lock:
        return f"{option_symbol}|{direction}" in self._traded_today

def reset_traded_today(self) -> None:
    with self.state_lock:
        self._traded_today.clear()

# RiskManager._maybe_reset_daily_state() â€” add at end of reset block:
    self._state.reset_traded_today()

# scan_underlying(), after opt_symbol resolved, before place_entry():
if state.was_traded_today(opt_symbol, direction):
    print(f"[SCAN] {symbol}: {opt_symbol} {direction} already traded today â€” skip (re-entry guard)")
    return

# OrderManager.place_entry(), after confirmed fill (before return True):
self._state.mark_traded(option_symbol, direction)
```

---

### UPGRADE-6 â€” P&L Drawdown Rate Monitor (Velocity-Based Session Halt)

**Class**: `RiskManager`
**Problem**: Daily loss limit (â‚¹2,000) fires only after the damage is done. An institutional risk desk also watches the *speed* of drawdown â€” losing â‚¹1,500 in 20 minutes is a crisis signal even if the daily limit is â‚¹5,000.
**Trader benefit**: Halt the session early if P&L deteriorates faster than a configurable â‚¹/minute rate, preventing compounding losses from a malfunctioning signal.

**Implementation**:
```python
# BotConfig â€” add:
drawdown_rate_enabled:     bool  = False
drawdown_rate_window_mins: int   = 30       # sliding window size
drawdown_rate_max_loss:    float = 1000.0   # max loss allowed within that window; 0 = off

# RiskManager â€” add:
self._pnl_history: list[tuple[float, float]] = []   # (unix_timestamp, cumulative_pnl)

# In record_exit(), after updating _daily_pnl:
self._pnl_history.append((time.time(), self._daily_pnl))
cutoff = time.time() - (self.config.drawdown_rate_window_mins * 60)
self._pnl_history = [(t, p) for t, p in self._pnl_history if t >= cutoff]

# In check_gates(), after daily loss checks:
if cfg.drawdown_rate_enabled and cfg.drawdown_rate_max_loss > 0 and len(self._pnl_history) >= 2:
    window_pnl_change = self._daily_pnl - self._pnl_history[0][1]
    if window_pnl_change <= -cfg.drawdown_rate_max_loss:
        return False, (
            f"Drawdown rate limit: â‚¹{abs(window_pnl_change):.0f} lost in last "
            f"{cfg.drawdown_rate_window_mins}m (limit â‚¹{cfg.drawdown_rate_max_loss:.0f})"
        )
```

---

### UPGRADE-7 â€” Pre-Trade Liquidity Preflight (Execution-Time Spread Check)

**Class**: `OrderManager.place_entry` + `BotConfig`
**Problem**: Strike selector uses chain snapshot (up to 60s old). By the time the order is placed, a large buyer may have lifted the ask, blowing out the spread. Entering a MARKET order into a 30% spread destroys the R:R on that trade.
**Trader benefit**: Final millisecond check before committing capital â€” reject the trade if the live spread has deteriorated since the signal was generated.

**Implementation**:
```python
# BotConfig â€” add:
preflight_spread_check:   bool  = True
preflight_max_spread_pct: float = 10.0   # reject if spread > this % at execution; 0 = off
preflight_min_bid:        float = 5.0    # reject if best bid < â‚¹5 (near-zero liquidity)

# OrderManager â€” store fetcher reference in __init__:
#   Add parameter: fetcher: "DataFetcher"
#   self._fetcher = fetcher
# Update OptionsBuyerEdgeBot.__init__: pass fetcher=self.fetcher to OrderManager()

# In place_entry(), after opening guards, before placeorder call:
if cfg.preflight_spread_check and not cfg.paper_trade:
    live_q = self._fetcher.fetch_quote(option_symbol, cfg.fno_exchange)
    if live_q:
        bid = float(live_q.get("bid", 0) or 0)
        ask = float(live_q.get("ask", 0) or 0)
        ltp = float(live_q.get("ltp", 0) or 0)
        mid = (bid + ask) / 2 if (bid and ask) else ltp
        if cfg.preflight_min_bid > 0 and bid < cfg.preflight_min_bid:
            print(f"[ORDER] Pre-flight FAIL {option_symbol}: bid â‚¹{bid:.2f} < min â‚¹{cfg.preflight_min_bid:.2f}")
            return False
        if mid > 0 and ask > bid:
            spread_pct = (ask - bid) / mid * 100
            if spread_pct > cfg.preflight_max_spread_pct:
                print(f"[ORDER] Pre-flight FAIL {option_symbol}: spread {spread_pct:.1f}% > max {cfg.preflight_max_spread_pct:.1f}%")
                return False
```

---

### UPGRADE-8 â€” Time-of-Day Score Weighting (Session Regime Awareness)

**Class**: `scan_underlying` in `OptionsBuyerEdgeBot` + `BotConfig`
**Problem**: Signal reliability is not uniform across the trading day. First 30 minutes (9:15â€“9:45 IST) have extreme volatility, gappy option prices, and unreliable OI data â€” entries here are low-probability. The power hour (14:00â€“15:10 IST) historically has the best directional follow-through for options buyers. Using identical score thresholds all day ignores this market structure reality.
**Trader benefit**: Automatically stricter in the volatile morning; more aggressive in the high-probability afternoon power hour. Zero code change during normal hours.

**Implementation**:
```python
# BotConfig â€” add:
morning_session_end:         str   = "09:45"   # skip new entries until after this time
afternoon_power_start:       str   = "14:00"   # power-hour starts here
power_hour_score_factor:     float = 0.80      # multiply min_score by this in power hour
morning_score_factor:        float = 1.50      # multiply min_score by this in morning session

# scan_underlying(), after check_gates, compute effective_min_score:
now_hm = datetime.now().strftime("%H:%M")
effective_min_score = cfg.min_score

if cfg.morning_session_end and now_hm < cfg.morning_session_end:
    effective_min_score = int(cfg.min_score * cfg.morning_score_factor)
    print(f"[SCAN] {symbol}: morning volatility gate â€” min_score raised to {effective_min_score}")
elif (cfg.afternoon_power_start
      and cfg.afternoon_power_start <= now_hm < cfg.no_new_trade_after):
    effective_min_score = max(1, int(cfg.min_score * cfg.power_hour_score_factor))
    print(f"[SCAN] {symbol}: power hour â€” min_score eased to {effective_min_score}")

# Replace hard cfg.min_score comparison in signal evaluation:
if abs_score < effective_min_score:
    signal = "WATCH" if abs_score >= 30 else "NO_TRADE"
```

---

### UPGRADE-9 â€” Adaptive Lot Sizing on Winning Streaks

**Class**: `RiskManager` + `BotConfig` + `scan_underlying`
**Problem**: Fixed `lot_multiplier` ignores current session performance edge. Institutional sizing uses a Kelly-fraction approach: when the strategy is in a confirmed profitable run, scale up modestly within safety limits; when cold, shrink back. The loss-streak gate already handles the downside â€” this adds the upside counterpart.
**Trader benefit**: Compound modestly during high-conviction sessions; natural position reduction is already handled by the loss-streak gate.

**Implementation**:
```python
# BotConfig â€” add:
adaptive_sizing_enabled:     bool = False
adaptive_max_lot_mult:       int  = 3      # hard ceiling â€” never exceed this multiplier
adaptive_win_streak_trigger: int  = 2      # consecutive wins needed to step up
adaptive_win_streak_step:    int  = 1      # lots added per trigger threshold

# RiskManager â€” add:
self._session_consecutive_wins: int = 0

# record_exit() â€” add:
if pnl >= 0:
    self._session_consecutive_wins += 1
else:
    self._session_consecutive_wins = 0   # reset on any loss

# RiskManager â€” add method:
def effective_lot_multiplier(self, base_multiplier: int) -> int:
    if not self.config.adaptive_sizing_enabled:
        return base_multiplier
    bonus = (self._session_consecutive_wins // self.config.adaptive_win_streak_trigger) \
            * self.config.adaptive_win_streak_step
    return min(base_multiplier + bonus, self.config.adaptive_max_lot_mult)

# scan_underlying(), replace fixed_qty computation:
effective_mult = self.risk.effective_lot_multiplier(cfg.lot_multiplier)
fixed_qty = effective_mult * lotsize
print(f"[SCAN] {symbol}: lot_mult={effective_mult} (base={cfg.lot_multiplier}, "
      f"wins={self.risk._session_consecutive_wins})")
```

---

### UPGRADE-10 â€” Enriched Telegram Alerts with Session Context Footer

**Class**: `OptionsBuyerEdgeBot` + all `_notify` call sites
**Problem**: All Telegram alerts carry identical formatting. A â‚¹âˆ’1,800 loss (near daily limit) looks identical to a â‚¹+500 target hit in the notification. The operator has to log in to understand session health.
**Trader benefit**: Every alert automatically shows session state: daily P&L, trade count, open positions, loss streak. Operator can assess session health from the notification alone without logging in.

**Implementation**:
```python
# OptionsBuyerEdgeBot â€” add method:
def _session_footer(self) -> str:
    """One-line session summary appended to every Telegram alert."""
    pnl       = self.risk.daily_pnl
    n_trades  = self.risk._session_trade_count
    n_open    = len(self.state.positions)
    streak    = self.risk._session_consecutive_losses
    pnl_icon  = "ðŸ“ˆ" if pnl >= 0 else "ðŸ“‰"
    return (
        f"\nâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€\n"
        f"{pnl_icon} Session â‚¹{pnl:+.0f} | {n_trades} trades | "
        f"{n_open} open | streak {streak}"
    )

# Modify _send_telegram to accept optional suffix, or patch each callsite to append:
#   + self._session_footer()
# Example in place_exit notification:
#   f"{emoji} Exit: {underlying} | {reason}\n..."
#   + self._session_footer()

# Add periodic heartbeat (optional, low-priority):
# In _strategy_thread, track last_hb_time. If (time.time() - last_hb_time) > heartbeat_interval_secs:
#     self._send_telegram(f"ðŸ’“ {cfg.strategy_name} alive{self._session_footer()}", 0)
#     last_hb_time = time.time()
```

---

## CHUNK 7 â€” UPGRADE PRIORITY ORDER

### U-P0 â€” Apply alongside P0/P1 bugs (correctness-linked)
- **UPGRADE-3** â€” OI Velocity replaces dead Gamma (also resolves SIG-1 dynamic score issue)
- **UPGRADE-4** â€” Hard bid-ask block at entry (critical for live capital protection, no API calls needed)

### U-P1 â€” High value, low risk (implement before going live with real capital)
- **UPGRADE-1** â€” Dynamic SL via ATR (biggest single P&L improvement for options buyers)
- **UPGRADE-5** â€” Duplicate trade / re-entry guard (prevents algorithmic revenge-trading)
- **UPGRADE-7** â€” Pre-trade liquidity preflight (last-mile capital protection before order submission)

### U-P2 â€” Medium value (add after a stable live run of 20+ trades)
- **UPGRADE-2** â€” Greeks-aware deep OTM exit (reduces theta bleed on broken trades)
- **UPGRADE-6** â€” Drawdown rate monitor (institutional velocity-based risk metric)
- **UPGRADE-8** â€” Time-of-day score weighting (session-regime awareness)

### U-P3 â€” Nice to have (implement after validating edge over 50+ live trades)
- **UPGRADE-9** â€” Adaptive lot sizing on winning streaks (only after win rate is validated)
- **UPGRADE-10** â€” Telegram session context footer (operator quality of life)

---

*End of document.*
*Source: `strategies/examples/BuyerEdgeStrategy.py` (~2,750 lines, 9 classes)*
*Audit scope: Full code read â€” all methods, all thread paths, all lock acquisition sites*
*Generated: May 2026*
