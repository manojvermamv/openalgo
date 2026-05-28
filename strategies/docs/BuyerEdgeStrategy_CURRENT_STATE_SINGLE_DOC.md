# BuyerEdgeStrategy Current State Single Document

Last updated: 2026-05-28

Primary source: `strategies/examples/BuyerEdgeStrategy.py`

Supporting sources:
- `strategies/docs/Logs-27052026.txt`
- `strategies/examples/Logs.txt`
- `strategies/examples/.understand-anything/meta.json`
- `strategies/examples/.understand-anything/knowledge-graph.json`
- `strategies/examples/.understand-anything/intraday-trading-algo-graph.html`

Historical markdown sources consolidated into this document and then removed:
- `BUYEREDGE_AUDIT_PROMPT_DONE.md`
- `BuyerEdge_Autonomous_Audit_Prompt.md`
- `COMPREHENSIVE_AUDIT_FRAMEWORK.md`
- `MONEYNESS_INTEGRATION_COMPLETE.md`
- `PRACTICAL_TESTING_CHECKLIST.md`
- `STRIKE_SELECTION_MONEYNESS_AUDIT.md`

This document is intentionally evidence-bound. It records what is present in the current script and attached docs/logs. It does not treat older audit prompts as automatically true when the live script differs.

Status legend:
- ✅ fixed, verified, or passed
- 🟡 open decision, partial coverage, or needs runtime validation
- 🔴 high-priority blocker before unattended live use
- 🟢 strong/reliable area
- ❌ failed check

## 1. Current Verification Snapshot

| Check | Result | Evidence |
|---|---:|---|
| Strategy file exists | ✅ PASS | `strategies/examples/BuyerEdgeStrategy.py` |
| Current script size | 3967 lines | AST parse of live file |
| Python syntax compile | ✅ PASS | `uv run python -m py_compile strategies/examples/BuyerEdgeStrategy.py` |
| Config field count | 82 fields | AST parse of `BotConfig` |
| Major class count | 16 classes | AST parse of live file |
| Top-level helper count | 2 helpers | `get_ist_now`, `_effective_min_score` |
| understand-anything graph exists | ✅ PASS | `.understand-anything/knowledge-graph.json` |
| understand-anything analyzed files | 2 | `meta.json`: strategy plus `Logs.txt` |

The graph metadata reports:
- `lastAnalyzedAt`: `2026-05-28T09:58:56.754613+00:00`
- `generation`: `manual-understand-schema-fallback`
- `analyzedFiles`: `2`
- graph node count: 123
- graph edge count: 153

Important note: the graph line numbers are older than the current live script in some places. Current line references in this document use the live script AST/line scan from 2026-05-28.

## 2. understand-anything Graph Audit

The `.understand-anything/knowledge-graph.json` file was audited in chunks against the current `BuyerEdgeStrategy.py`. The graph is useful as a conceptual map, but it is not fully current as a line-level source.

Graph structure:

| Item | Count |
|---|---:|
| Total nodes | 123 |
| Total edges | 153 |
| File nodes | 1 |
| Document nodes | 1 |
| Class nodes | 16 |
| Function nodes | 93 |
| Concept nodes | 12 |
| Layers | 8 |
| Tour steps | 6 |

Edge relationship counts:

| Edge type | Count | Audit result |
|---|---:|---|
| `contains` | 109 | Mostly valid by name; many line ranges stale |
| `configures` | 1 | Valid conceptually |
| `depends_on` | 8 | Valid for orchestrator wiring |
| `calls` | 16 | 15 direct calls verified by source scan; 1 conceptual class-level edge |
| `defines_schema` | 3 | Conceptual, valid |
| `validates` | 3 | Conceptual, valid |
| `transforms` | 1 | Conceptual, valid |
| `reads_from` | 4 | Conceptual, valid |
| `triggers` | 2 | Valid |
| `documents` | 3 | Valid for log-derived findings |
| `writes_to` | 1 | Valid |
| `related` | 2 | Valid conceptually |

Graph integrity:
- All edge endpoints resolve to existing graph nodes.
- All layer `nodeIds` resolve to existing graph nodes.
- All tour `nodeIds` resolve to existing graph nodes.
- The graph is internally consistent, even where it is stale versus the current script.

### 2.1 Chunk 1 - File And Document Nodes

The graph contains:
- `file:BuyerEdgeStrategy.py`
- `document:Logs.txt`

Both are valid source artifacts. The file summary is still accurate at a high level: the script remains a single-file OpenAlgo options bot with configuration, data fetch, scoring, strike selection, risk sizing, WebSocket protection, and order management.

The document summary is also valid: `Logs.txt` contains startup, WebSocket smoke test, scan panels, risk sizing blocks, and outside-market-hours loop evidence.

### 2.2 Chunk 2 - Class Nodes

All 16 graph class nodes still exist in the current script.

Line-range audit:

| Status | Count | Meaning |
|---|---:|---|
| Exact line match | 7 | Graph line range still matches current script |
| Stale line range | 9 | Class exists, but live lines shifted |
| Missing class | 0 | No graph class is absent from current script |

Classes with stale graph line ranges:

| Class | Graph lines | Current lines | Shift |
|---|---:|---:|---:|
| `OIFlowAnalyzer` | 593-721 | 634-762 | -41 |
| `SignalEngine` | 728-1175 | 769-1215 | -41 |
| `DataFetcher` | 1182-1607 | 1222-1647 | -40 |
| `EntryStopLossPolicy` | 1614-1712 | 1654-1752 | -40 |
| `StrikeSelector` | 1719-1871 | 1759-1911 | -40 |
| `RiskManager` | 1878-2053 | 1918-2096 | -40 |
| `WebSocketManager` | 2060-2433 | 2103-2498 | -43 |
| `OrderManager` | 2440-3149 | 2505-3214 | -65 |
| `OptionsBuyerEdgeBot` | 3156-3893 | 3221-3967 | -65 |

Interpretation:
- The graph is reliable for class identity and rough architecture.
- The graph must not be used for exact current line references after `BotState`.

### 2.3 Chunk 3 - Function Nodes

The graph has 93 function nodes.

Function audit:

| Status | Count | Meaning |
|---|---:|---|
| Exact line match | 8 | Graph function line range still matches current script |
| Stale line range | 84 | Function exists but live lines shifted |
| Missing from current script | 1 | Graph function no longer exists |

Graph function missing from current script:

| Graph node | Graph lines | Audit result |
|---|---:|---|
| `_field_trend` | 583-586 | Not present in current `BuyerEdgeStrategy.py` |

Current script functions missing from graph:

| Current function | Current lines | Meaning |
|---|---:|---|
| `get_ist_now` | 583-595 | Current IST-safe clock helper, absent from graph |
| `_effective_min_score` | 598-626 | Current session-aware score threshold helper, absent from graph |
| `WebSocketManager.set_notify_callback` | 2135-2137 | Current WebSocket notification callback setter, absent from graph |

Interpretation:
- The graph was generated before the current time-helper and score-threshold helper were added or after a different source snapshot.
- Any future audit using the graph should manually include `get_ist_now` and `_effective_min_score`, because both affect timing/session behavior.

### 2.4 Chunk 4 - Layer Model

The graph's 8 layers are conceptually valid:

| Layer | Audit |
|---|---|
| Configuration | Valid |
| State and Models | Valid |
| Market Data and Feature Extraction | Valid |
| Signal Intelligence | Mostly valid, but still references missing `_field_trend` and omits current time helpers |
| Risk, Stops, and Strike Selection | Valid |
| Execution and Protection | Valid, but omits `WebSocketManager.set_notify_callback` |
| Bot Orchestration | Valid |
| Runtime Evidence | Valid for `Logs.txt` |

Recommended use:
- Use layers as a reading order.
- Do not use layer membership as a complete current function inventory.

### 2.5 Chunk 5 - Tour Steps

The graph tour is still a good learning path:

1. Start at the bot: `OptionsBuyerEdgeBot.run`
2. Follow the scan pipeline: `scan_underlying`, data fetch, scoring, strike selection
3. Understand signal construction: five-layer scoring and trap gates
4. Inspect risk and trade blocking: risk sizing and log summary
5. Review live protection: WebSocket and order manager
6. Use logs as feedback: runtime data quality and no-order run evidence

Audit result:
- All tour node IDs resolve in the graph.
- The reading order is valid.
- The tour should be supplemented with the missing current helpers: `get_ist_now` and `_effective_min_score`.

### 2.6 Chunk 6 - Concept Nodes

All 12 concept nodes remain useful:

| Concept | Audit |
|---|---|
| Five-Layer Confirmation | Valid |
| Score and Trap Gates | Valid |
| OI Flow Intelligence | Valid |
| GEX Regime | Valid |
| IV and Straddle Layer | Valid |
| Synthetic Futures Confirmation | Valid |
| Strike Asymmetry Selection | Valid |
| Risk Sizing Block | Valid, supported by logs |
| Runtime Data Quality | Valid, supported by logs |
| WebSocket Protection Loop | Valid |
| Order Reconciliation | Valid |
| Runtime Log Summary | Valid, supported by logs |

Runtime-log concept stat check from `Logs.txt`:

| Stat | Graph value | Recount result | Audit |
|---|---:|---:|---|
| Scan panels | 94 | 94 using header regex `^\s+-- SCAN` equivalent | ✅ PASS |
| Qty-zero blocks | 49 | 49 | ✅ PASS |
| Risk-exceeds-cap blocks | 49 | 49 | ✅ PASS |
| Signal score below 40 | 35 | 35 | ✅ PASS |
| Asymmetry failures | 14 | 14 | ✅ PASS |
| Low VWAP volume | 94 | 94 | ✅ PASS |
| IVR unavailable | 94 | 94 | ✅ PASS |
| Entry orders | 0 | 0 | ✅ PASS |
| Exit orders | 0 | 0 | ✅ PASS |
| Outside-hours skips | 30 | 30 | ✅ PASS |

Note: a naive text count of `SCAN` gives a larger number because it also catches non-header occurrences. The graph's `scan_blocks=94` is correct when counted against actual scan header lines.

### 2.7 Chunk 7 - Call Edges

The graph has 16 call edges.

Direct source scan result:
- 15 call edges were directly found in the current source by checking the source function body for the target call name.
- 1 edge is conceptual/class-level rather than a direct function-body call: `SignalEngine -> OIFlowAnalyzer`.

Verified direct call examples:
- `OptionsBuyerEdgeBot.run -> OptionsBuyerEdgeBot._test_websocket`
- `OptionsBuyerEdgeBot.run -> WebSocketManager.start`
- `OptionsBuyerEdgeBot._strategy_thread -> OptionsBuyerEdgeBot.scan_underlying`
- `OptionsBuyerEdgeBot.scan_underlying -> DataFetcher.fetch_quote`
- `OptionsBuyerEdgeBot.scan_underlying -> DataFetcher.fetch_option_chain`
- `OptionsBuyerEdgeBot.scan_underlying -> OIFlowAnalyzer.smooth_chain_rows`
- `OptionsBuyerEdgeBot.scan_underlying -> SignalEngine.score`
- `OptionsBuyerEdgeBot.scan_underlying -> StrikeSelector.select_best`
- `OptionsBuyerEdgeBot.scan_underlying -> RiskManager.available_capital`
- `OptionsBuyerEdgeBot.scan_underlying -> OrderManager.place_entry`
- `WebSocketManager._on_ws_data -> _check_premium_trail`
- `WebSocketManager._on_ws_data -> _check_spot_trail`
- `OrderManager.place_entry -> register_filled_entry`
- `OrderManager.place_exit -> _write_journal`

Interpretation:
- The graph call map is useful for high-level flow.
- It is not exhaustive. For example, current code also has startup registration checks, position recovery, pending reconciliation, square-off, and max-hold calls that are not fully represented as call edges.

### 2.8 Graph Reliability Rating

| Area | Reliability | Use for |
|---|---|---|
| Conceptual architecture | 🟢 High | Understanding layers and reading path |
| Class/function inventory by name | 🟡 Medium-high | Most names valid, but a few misses |
| Exact line numbers | 🔴 Low after line 580 | Use current script instead |
| Runtime log stats | 🟢 High | Supported by recount from `Logs.txt` |
| Call graph completeness | 🟡 Medium | Good core path, not exhaustive |
| Current timing/session logic | 🟡 Medium-low | Missing `get_ist_now` and `_effective_min_score` in graph artifact |

Bottom line: keep the graph as an orientation map, but cross-check every line number and every risk/timing claim against the live `BuyerEdgeStrategy.py`.

## 3. What The Script Is

`BuyerEdgeStrategy.py` is a single-file OpenAlgo options buying bot for intraday NSE/NFO trading. It is designed to run from OpenAlgo's built-in Python automated execution system or as a standalone script with OpenAlgo SDK access.

The script wires:
- environment-driven configuration
- OpenAlgo REST SDK client
- OpenAlgo WebSocket feed
- five-layer signal scoring
- option-chain smoothing and OI flow logic
- Greeks, IV, GEX, synthetic futures context
- strike selection
- fixed or ATR-derived entry stop policy
- risk-gated position sizing
- real or paper order placement
- broker SL/target protection orders
- pending order reconciliation
- live trailing exits
- optional Telegram notifications
- startup position recovery
- WebSocket smoke testing

The script is not a Flask app module. It is a strategy process that talks back to an OpenAlgo server through SDK calls and WebSocket data.

## 4. Main Code Map

| Component | Current lines | Responsibility |
|---|---:|---|
| `BotConfig` | 58-459 | All runtime configuration, `from_env`, and validation |
| `ScoreComponent` | 467-472 | One score row in terminal component output |
| `SignalResult` | 476-484 | Final signal, direction, score, trap, reasons |
| `OptionPosition` | 488-513 | Active position model, including entry delta and moneyness |
| `PendingEntry` | 517-524 | Pending BUY reconciliation state |
| `PendingExit` | 528-531 | Pending SELL reconciliation state |
| `BotState` | 538-580 | Shared mutable state, locks, caches, traded-strike guard |
| `get_ist_now` | 583-595 | IST-safe clock helper |
| `_effective_min_score` | 598-626 | Time-of-day score threshold adjustment |
| `OIFlowAnalyzer` | 634-762 | Chain smoothing, PCR, walls, CE/PE flow classification |
| `SignalEngine` | 769-1215 | Five-layer scoring and trap decision |
| `DataFetcher` | 1222-1647 | OpenAlgo data adapter, chain, candles, quotes, Greeks, GEX, IVR |
| `EntryStopLossPolicy` | 1654-1752 | Fixed, strike ATR, spot ATR, and delta-aware SL resolution |
| `StrikeSelector` | 1759-1911 | Delta/liquidity/asymmetry-based option strike selection |
| `RiskManager` | 1918-2096 | Session gates, capital, cooldown, daily PnL, adaptive sizing |
| `WebSocketManager` | 2103-2498 | Live feed, LTP map, premium/spot trailing exits |
| `OrderManager` | 2505-3214 | Entry/exit orders, broker SL/TGT, pending reconciliation, journal |
| `OptionsBuyerEdgeBot` | 3221-3967 | Orchestration, startup checks, scan loop, WebSocket test, run loop |

## 5. Complete Current Inventories

This section exists so the document can be cross-checked from the live script outward.

### 5.1 BotConfig Fields

Current `BotConfig` has 82 fields:

| Group | Fields |
|---|---|
| API and identity | `api_key`, `api_host`, `ws_url`, `strategy_name` |
| Universe | `underlyings`, `index_underlyings` |
| Exchange routing | `spot_exchange`, `fno_exchange`, `index_exchange` |
| Notifications | `telegram_username` |
| Options parameters | `dte_min`, `dte_max`, `otm_offset`, `strike_count`, `lot_multiplier`, `gex_enabled` |
| Signal thresholds | `min_score`, `max_trap` |
| Session score weighting | `morning_session_end`, `afternoon_power_start`, `power_hour_score_factor`, `morning_score_factor` |
| Fixed premium risk | `premium_stop_pts`, `premium_target_pts` |
| Entry SL policy | `entry_sl_mode`, `dynamic_sl_atr_period`, `dynamic_sl_atr_mult`, `dynamic_sl_min_pts`, `dynamic_sl_max_pts` |
| Session risk gates | `max_trades_per_session`, `max_consecutive_losses`, `entry_cooldown_secs`, `max_daily_loss_pct`, `max_daily_loss_amount`, `risk_percent` |
| Trailing SL | `trail_sl_mode`, `spot_reward_pct`, `trail_activate_at_pct`, `trail_step_rr_pct` |
| Mode flags | `long_only_mode`, `broker_sl_orders` |
| Technicals | `candle_interval`, `lookback_days`, `fast_ema_period`, `slow_ema_period`, `rsi_period` |
| Loop timing | `signal_check_interval`, `lookback_bars` |
| IV gating | `iv_rank_max_entry`, `iv_52w_low`, `iv_52w_high` |
| Strike selection | `min_oi_filter`, `min_vol_filter`, `asym_score_threshold`, `allow_checkpoint_fallback`, `delta_target_low`, `delta_target_high` |
| Order polling | `order_status_max_retries`, `order_status_poll_interval` |
| U2 deep OTM exit | `delta_exit_threshold` |
| U3 OI velocity | `oi_velocity_enabled`, `oi_velocity_threshold` |
| U4 spread block | `max_entry_spread_pct` |
| U5 same-strike guard | `same_strike_reentry_guard_enabled`, `max_same_strike_trades_per_day` |
| U6 drawdown-rate monitor | `drawdown_rate_enabled`, `drawdown_rate_window_mins`, `drawdown_rate_max_loss` |
| U7 preflight liquidity | `preflight_spread_check`, `preflight_max_spread_pct`, `preflight_min_bid` |
| U9 adaptive sizing | `adaptive_sizing_enabled`, `adaptive_max_lot_mult`, `adaptive_win_streak_trigger`, `adaptive_win_streak_step` |
| Paper mode | `paper_trade` |
| Daily profit target | `max_daily_profit_amount` |
| Session timing | `no_new_trade_after`, `square_off_time` |
| Hold-time exit | `max_hold_minutes` |
| Breakeven SL | `breakeven_at_gain_pct` |
| Journal | `trade_journal_path` |

### 5.2 Current Method Inventory

Current script method/function inventory from AST:

| Component | Methods |
|---|---|
| Top-level helpers | `get_ist_now`, `_effective_min_score` |
| `BotConfig` | `from_env`, `validate` |
| `BotState` | `__init__`, `get_chain_history`, `reset_market_caches`, `mark_traded`, `trade_count_today`, `reset_traded_today` |
| `OIFlowAnalyzer` | `smooth_chain_rows`, `compute_pcr`, `call_wall`, `put_wall`, `classify_ce_flow`, `classify_pe_flow` |
| `SignalEngine` | `__init__`, `iv_rank`, `score` |
| `DataFetcher` | `__init__`, `clear_greeks_cache`, `_fetch_option_greeks_cached`, `greeks_perf_snapshot`, `batch_prefetch_option_greeks`, `underlying_exchange`, `fetch_candles`, `fetch_spot_candles`, `fetch_option_candles`, `fetch_option_chain`, `fetch_quote`, `fetch_synthetic_future`, `fetch_atm_greeks`, `fetch_option_delta`, `fetch_option_gamma`, `derive_gex_levels`, `fetch_gex_levels`, `fetch_atm_iv_ranks`, `fetch_target_expiry` |
| `EntryStopLossPolicy` | `__init__`, `_atr_stop_pts`, `_classify_moneyness`, `_sl_pts_by_delta`, `resolve_entry_sl_points` |
| `StrikeSelector` | `__init__`, `simple_otm`, `select_best` |
| `RiskManager` | `__init__`, `available_capital`, `_maybe_reset_daily_state`, `check_gates`, `record_entry`, `record_exit`, `effective_lot_multiplier`, `consecutive_wins`, `daily_pnl`, `halted` |
| `WebSocketManager` | `__init__`, `set_fetcher`, `set_notify_callback`, `_get_cached_delta`, `_fetch_and_cache_delta`, `set_exit_callback`, `set_sl_modify_callback`, `start`, `subscribe`, `subscribe_spot`, `unsubscribe`, `unsubscribe_spot`, `_on_ws_data`, `_check_premium_trail`, `_check_spot_trail`, `_trigger_exit`, `_ws_thread` |
| `OrderManager` | `__init__`, `poll_order_status`, `cancel_broker_orders`, `modify_broker_sl`, `check_broker_order_fills`, `register_filled_entry`, `_write_journal`, `place_entry`, `place_exit`, `check_pending_entries`, `check_pending_exits` |
| `OptionsBuyerEdgeBot` | `__init__`, `_send_telegram`, `_verify_registration`, `_check_open_positions_on_startup`, `_check_max_hold`, `_is_market_hours`, `_print_startup_info`, `scan_underlying`, `_strategy_thread`, `_test_websocket`, `run` |

Nested helper functions are intentionally not listed in the main map unless they affect external behavior. Current nested helpers include `_agg` inside both OI classifiers, `_dir` and `_c` inside `SignalEngine.score`, `_log_greeks_perf` inside `scan_underlying`, and async `_run` inside `_test_websocket`.

## 6. OpenAlgo Built-In Python Runner Fit

The current script explicitly supports OpenAlgo's local strategy runner model:
- `OPENALGO_API_KEY` is read from the environment in `BotConfig.from_env`.
- `HOST_SERVER` feeds `api_host`; default is `http://127.0.0.1:5000`.
- If `WEBSOCKET_URL` is not set, the script defaults to `ws://127.0.0.1:8765`.
- The inline comment at lines 241-244 states the strategy runs as a subprocess of the OpenAlgo Python runner and should generally talk to the same host WebSocket server.
- `OptionsBuyerEdgeBot.__init__` builds `api(api_key=config.api_key, host=config.api_host, ws_url=config.ws_url)` when a WebSocket URL is configured.
- The script verifies strategy registration through `/api/v1/strategy`.
- The startup flow tests REST orderbook auth, tests WebSocket auth/subscription, starts the WebSocket thread, then starts the strategy scan thread.

Operational requirement:
- The strategy still needs a valid OpenAlgo API key and a running OpenAlgo server.
- The logs show one run where strategy registration was missing: `Strategy 'BuyerEdgeStrategyBot' not found in strategy configs`; the script continued anyway. That is runtime evidence, not a compile failure.

## 7. Configuration State

The current `BotConfig` has 82 fields. Most environment fallbacks use `defaults.<field>` or `str(defaults.<field>)`, which keeps dataclass defaults as the source of truth.

Known special cases:
- `UNDERLYINGS` default is a hardcoded CSV in `from_env`, not `defaults.underlyings`, because the dataclass default is an empty list.
- `INDEX_UNDERLYINGS` default is a hardcoded CSV in `from_env`, not `defaults.index_underlyings`, because the dataclass default is an empty frozenset.
- `WEBSOCKET_URL` starts from `defaults.ws_url`, but if empty the script derives `ws://127.0.0.1:8765`.
- `HOST_SERVER` maps to `api_host`.
- The live logs show `No New Entries: after 13:30 IST` and `EOD Square-Off: 15:15 IST`; the current dataclass defaults are `15:25` and `15:30`. This means that log run used environment overrides. It is not evidence that the defaults changed.

Validation currently covers:
- premium SL/target positive values
- entry SL mode enum: `fixed`, `strike_atr`, `spot_atr`
- ATR period/mult/min/max
- risk percent positive
- trailing mode enum: `spot`, `premium`, `both`
- lot multiplier, strike count
- GEX bool type
- score/trap ranges
- delta exit threshold
- OI velocity threshold
- spread thresholds
- same-strike count
- drawdown window/loss
- preflight thresholds
- session score factors
- adaptive sizing parameters
- DTE range
- delta target range
- IV rank max range
- asymmetry score range
- order poll settings
- HH:MM time formats
- max hold time
- breakeven gain range

## 8. Runtime Flow

Startup:
1. Build config from environment.
2. Validate config.
3. Verify strategy registration.
4. Print resolved runtime values.
5. Restore open broker positions when possible.
6. Send Telegram startup message when configured.
7. Smoke-test WebSocket auth and tick flow.
8. Start WebSocket manager thread.
9. Start strategy scan thread.

Per scan:
1. Clear scan-scoped Greeks cache.
2. Check market data and risk gates.
3. Fetch candles, option chain, synthetic futures, Greeks/GEX/IVR.
4. Smooth chain rows.
5. Compute effective minimum score using time-of-day weighting.
6. Score five signal layers.
7. Print a self-contained scan panel.
8. If executable, select a strike.
9. Apply same-strike guard and spread block.
10. Resolve entry stop points.
11. Size quantity from available capital and risk percent.
12. Place order only if quantity is positive and all guards pass.

Background loop:
1. Reconcile pending entries.
2. Reconcile pending exits.
3. Check broker-side SL/TGT fills.
4. Force square-off after configured time.
5. Check max-hold exits.
6. Scan underlyings if inside market hours.

## 9. Signal Model

The current scoring engine is five-layer.

Layer 1: Technical trend
- EMA trend
- RSI momentum
- MACD histogram
- Spot versus VWAP
- Code uses closed-bar logic for EMA/MACD sections.
- VWAP uses valid-volume filtering and logs low-volume conditions.

Layer 2: OI flow
- PCR OI level
- CE flow classifier
- PE flow classifier
- OI wall positioning
- Smoothing uses bounded chain history in `BotState`.

Layer 3: Greeks and regime
- Delta imbalance
- Gamma regime when GEX data is available
- OI velocity as an additive component
- This aligns with the consolidated historical audit record, which stated gamma is active and OI velocity is additive, not a replacement.

Layer 4: Straddle and IV
- Separate CE/PE IV rank fetch path exists.
- Best-fit IVR favors cheaper IV for option buying.
- Straddle velocity resets when ATM strike changes, avoiding false velocity across strike shifts.

Layer 5: Synthetic futures
- Tracks previous spot and synthetic futures values in state.
- Scores co-movement only when movement is meaningful enough.

Composite score:
- The current code uses dynamic normalization from the component max scores.
- The docs state:
  `MAX_RAW_SCORE = sum(c.score_max for c in components)`
  and final score is clamped to `[-100, +100]`.

Signal states:
- `EXECUTE`: absolute score meets effective minimum and trap is within max.
- `WATCH`: intermediate state.
- `NO_TRADE`: insufficient edge.

## 10. Strike Selection And Moneyness Alignment

The consolidated historical strike/moneyness audit identified a problem: fixed point SL/TP across ATM, OTM, and deep OTM strikes caused unrealistic risk behavior.

The current script and consolidated moneyness integration notes show that this has been integrated:
- `OptionPosition` stores `entry_delta` and `moneyness`.
- `EntryStopLossPolicy._classify_moneyness` maps delta into ATM, Sl-OTM, OTM, Deep-OTM, or Unknown.
- `EntryStopLossPolicy._sl_pts_by_delta` adapts stop width based on entry delta.
- `StrikeSelector.select_best` receives `signal_score` and maps signal strength to target delta ranges.
- `OrderManager.place_entry` accepts `entry_delta`.
- `OrderManager.register_filled_entry` persists moneyness and delta in active position state.

Current signal-to-delta behavior in code:
- Very high conviction: ATM target range.
- High conviction: ATM-leaning target range.
- Medium conviction: slight OTM target range.
- Low conviction below the selector threshold returns no qualifying strike.

Important runtime evidence:
- Logs show repeated `Signal score < 40 - insufficient edge to trade`, followed by `using simple OTM fallback strike ...`.
- That behavior is controlled by `allow_checkpoint_fallback=True`.
- If the intended policy is "low conviction must never trade fallback OTM", this config/logic path needs a future decision. The logs only show quantity zero, so no actual order was sent in those runs.

## 11. Risk And Execution Guards

Current implemented guards include:
- global `entry_in_flight` block
- max trades per session
- max consecutive losses
- per-underlying cooldown using `time.monotonic`
- max daily loss percent
- max daily loss fixed amount
- drawdown-rate halt using a deque-backed PnL history pruned by the configured time window
- max daily profit target
- no-new-entry time gate using `get_ist_now`
- same-strike re-entry guard
- hard spread block before order placement
- preflight bid/spread check immediately before live order placement
- quantity zero block
- paper-trade path that does not call `client.placeorder`

Quantity sizing:
- `available = risk.available_capital()`
- `risk_cap = available * (risk_percent / 100)`
- `risk_qty = int(risk_cap / entry_sl_pts)`
- quantity is rounded down to lot size
- final quantity is `min(fixed_qty, risk_qty)`
- if final quantity is zero, no order is sent

Current log evidence:
- Repeated `qty=0` blocks show stop risk per lot exceeded configured risk cap.
- Example from logs: stop 30 points x 65 units = Rs 1950 per lot, risk cap Rs 1000 at 1.0 percent of Rs 100000 available.
- This proves the quantity guard blocked order placement in the observed run.

## 12. Order Management

Entry path:
- Paper mode simulates a fill and registers the position.
- Live mode increments `entry_in_flight`, performs preflight quote checks, submits MARKET BUY, records pending entry state, polls for fill, accepts partial fills, records risk entry after confirmed fill, and registers the position.
- `entry_in_flight` is decremented in `finally`.

Exit path:
- Paper mode simulates SELL, records PnL, unsubscribes, writes journal, and removes position.
- Live mode cancels broker protection orders first.
- If broker protection already filled, it records that fill and skips duplicate SELL.
- Otherwise it submits MARKET SELL, records pending exit, polls for fill, writes journal when confirmed, unsubscribes, and clears position state.

Pending reconciliation:
- Pending BUY fills can activate protection even if detected outside the normal path.
- If pending BUY fills after square-off time, the code queues immediate exit.
- Unfilled pending entries are cancelled after square-off cutoff.
- Pending exits are polled and reconciled every strategy loop.

## 13. WebSocket And Exit Management

The WebSocket manager:
- starts a daemon thread
- accepts fetcher, notify, exit, and SL-modify callbacks
- subscribes/unsubscribes option and spot symbols under a lock
- maintains `ltp_map`
- handles premium trailing stop checks
- handles spot trailing stop checks
- can trigger exits through the order manager callback
- uses a bounded ThreadPoolExecutor for delta fetches
- has reconnect/backoff behavior in the WebSocket thread

Position protection:
- Premium trail can ratchet option SL.
- Spot trail can track underlying movement.
- `both` mode chooses a tighter effective SL path when bridging spot movement into premium SL.
- Deep OTM delta exit is supported through cached delta checks.
- Breakeven migration is guarded by `breakeven_moved`.

## 14. Observability And Logs

The current scan panel includes:
- symbol
- spot price
- IST timestamp
- score
- label
- trap score
- signal state
- component-by-component score rows
- trap reasons when present
- final verdict
- Greeks cache performance metrics

The 2026-05-27 logs prove:
- config validation passed
- REST API key worked for orderbook
- WebSocket handshake succeeded
- WebSocket auth succeeded
- WebSocket subscription succeeded
- at least one tick was received in smoke test
- scan blocks printed component breakdowns
- repeated qty-zero blocks prevented orders
- later loop emitted outside-market-hours skip messages

Observed data-quality issues in logs:
- VWAP repeatedly reported low volume: valid 0, zero 5, total 5.
- IVR was often unavailable.
- OI velocity was often below threshold.
- Greeks cache hit rates varied widely, including low-hit-rate windows.

These are runtime market-data observations, not syntax errors.

## 15. Alignment With Consolidated Historical Docs

The removed historical markdown docs were consolidated into this single document. The closest current-state historical record said:
- P0/P1 reliability fixes completed.
- WC-01 to WC-14 passed.
- U1 through U9 implemented or partly implemented.
- U10 Telegram session footer + heartbeat pending.
- gamma is active.
- OI velocity is additive.

Current script agrees with many of those statements:
- adaptive SL policy exists
- delta-aware deep OTM exit exists
- OI velocity exists
- hard spread block exists
- same-strike guard exists
- drawdown-rate gate exists
- preflight liquidity check exists
- time-of-day score weighting exists
- adaptive sizing exists
- Telegram footer/heartbeat is not visible as an integrated feature

The autonomous audit prompt was a task prompt, not a completed-result file. Its useful phase-loop process is now refined in Section 17. Some requested items are implemented, some remain future/audit checks, and some conflict with current code.

The comprehensive audit framework and practical testing checklist were testing protocols and expectations. Their useful loop/checklist structure is now consolidated into Section 17. They should be treated as test plans, not proof that those scenarios were actually run.

The strike-selection/moneyness audit was historical diagnosis. The moneyness integration notes recorded the integration that addressed that diagnosis.

## 16. Known Current-State Gaps Or Items Requiring Decision

These are not guessed bugs; they are differences or risks observed from the live script and docs.

✅ Fixed in the current script:
- ✅ Market-hours, square-off, daily reset, max-hold checks, and registered/restored position entry times now use `get_ist_now()` consistently for IST-sensitive strategy behavior.
- ✅ Drawdown-rate history now uses a deque and prunes old entries with `popleft()`.

🟡 Remaining gaps and decisions:

1. 🟡 Max-hold exit is scan-loop driven, not WebSocket tick-callback driven.
   - The audit prompt asked to verify max-hold exits via WebSocket tick callback.
   - Current code calls `_check_max_hold()` from `_strategy_thread`.
   - This is deterministic, but the doc/prompt expectation differs from implementation.

2. 🟡 Low-score strike fallback can still choose an OTM strike after selector rejection.
   - `StrikeSelector.select_best` can reject scores below 40.
   - `scan_underlying` can still use `StrikeSelector.simple_otm` when `allow_checkpoint_fallback=True`.
   - Quantity/risk may still block the trade, as logs show, but the policy should be clarified.

3. 🟡 `UNDERLYINGS` and `INDEX_UNDERLYINGS` are not pure dataclass-default fallbacks.
   - This is probably intentional because the dataclass defaults are empty containers.
   - If strict zero-drift config doctrine is required, those defaults should be moved into default factories or constants.

4. 🟡 WebSocket smoke test uses `asyncio.run`.
   - The method catches `RuntimeError` and skips if an event loop is already active.
   - Since this script is intended as a strategy subprocess, this may be acceptable.
   - If the script is ever imported/run directly under eventlet monkey-patching, this deserves another production compatibility pass.

5. 🟡 The docs contain mojibake in several files.
   - This document uses ASCII-only text to avoid adding encoding noise.
   - Existing docs/logs include rendered symbols that appear corrupted in PowerShell output.

6. 🔴 Runtime logs show missing strategy registration.
   - Startup continued anyway.
   - For live automation, the strategy should be registered before enabling unattended execution.

## 17. Future Revalidation Prompt

Use this refined prompt when a future agent must re-audit or change `BuyerEdgeStrategy.py`. It preserves the original loop process, but updates it to the current script and this single-doc workflow.

### Mission

You are auditing and improving `strategies/examples/BuyerEdgeStrategy.py`, a single-file OpenAlgo NSE/NFO options buyer strategy. Work from the current script first, then cross-check this document, `Logs-27052026.txt`, `strategies/examples/Logs.txt`, and `.understand-anything/knowledge-graph.json`.

Do not assume the graph or old line numbers are current. Treat this document as the living map and the script as the source of truth.

### Mandatory Loop Rule

Work phase by phase. After every phase, run the complete worst-case checklist for that phase and write a structured result table:

| Check | Status | Evidence | Fix if failed |
|---|---|---|---|
| Example check | ✅ PASS / ❌ FAIL / 🟡 GAP | File/line/log evidence | Patch or reason |

If any required check fails:
1. patch the code or doc,
2. rerun the check,
3. do not move to the next phase until required items pass or are explicitly recorded as an accepted current-state gap.

### Phase 0 - Source Refresh

1. Parse the current script with AST.
2. Recount classes, methods, config fields, and top-level helpers.
3. Cross-check Section 5 inventories in this document.
4. Re-audit `.understand-anything/knowledge-graph.json` for stale/missing nodes.
5. Update this document before making behavioral claims.

Worst-case checks:
- ✅ all current classes are listed in this document
- ✅ all current methods/functions are listed or intentionally summarized as nested helpers
- ✅ all current `BotConfig` fields are listed
- ✅ graph nodes missing from script are recorded
- ✅ script nodes missing from graph are recorded

### Phase 1 - Configuration Integrity

Objective: keep `BotConfig` as the configuration source of truth.

Tasks:
1. Map every `BotConfig` field to its `from_env()` source.
2. Verify fallbacks use dataclass defaults where practical.
3. Record intentional exceptions such as CSV defaults for `UNDERLYINGS` and `INDEX_UNDERLYINGS`.
4. Verify `validate()` covers constrained ranges, enums, bools, and HH:MM strings.
5. Verify the startup banner prints resolved values from `cfg`.

Worst-case checks:
- ✅ `RISK_PERCENT=0` fails at startup validation
- ✅ unset `ENTRY_SL_MODE` resolves to `fixed`
- ✅ unset `NO_NEW_TRADE_AFTER` resolves to the dataclass default
- ✅ invalid `TRAIL_SL_MODE` fails validation
- ✅ current `.env` overrides are not mistaken for dataclass defaults

### Phase 2 - Time And Risk Gates

Objective: all session gates must be deterministic and non-bypassable.

Tasks:
1. Verify no-new-entry and square-off boundary semantics.
2. Verify all time gates use IST-safe logic or record host-time assumptions.
3. Verify daily loss, daily profit, consecutive loss, cooldown, and max-trade gates.
4. Verify `entry_in_flight` blocks concurrent sizing.
5. Verify drawdown-rate behavior. Current code uses a deque-backed PnL history pruned after exits.
6. Verify qty-zero never sends orders and closes scan logs cleanly.

Worst-case checks:
- ✅ exact `no_new_trade_after` minute blocks new entries
- ✅ exact `square_off_time` minute exits open positions
- ✅ two simultaneous underlyings cannot double-size through `entry_in_flight`
- ✅ qty-zero path does not call `place_entry`
- 🟡 paper and live exits both update daily PnL/loss streak behavior

### Phase 3 - Signal Engine

Objective: five-layer scoring must be reproducible and gracefully degrade when data is missing.

Tasks:
1. Verify technical layer closed-bar behavior for EMA/MACD.
2. Verify VWAP low-volume fallback and no stray debug print.
3. Verify OI smoothing, PCR, CE/PE flow classifiers, OI wall, and OI velocity behavior.
4. Verify Greeks cache, delta convention, GEX fallback, and IVR fallback.
5. Verify straddle velocity reset on ATM shift.
6. Verify synthetic futures state persistence.
7. Verify dynamic score normalization from component `score_max`.

Worst-case checks:
- ✅ empty option chain does not crash
- ✅ GEX unavailable scores neutral, no crash
- ✅ IVR unavailable scores neutral, no crash
- ✅ all-zero components produce `NO_TRADE`
- 🟡 max-aligned components can produce `EXECUTE`

### Phase 4 - Strike, Stops, And Sizing

Objective: selected strike and SL must match signal strength and risk budget.

Tasks:
1. Verify signal-score-to-delta mapping in `StrikeSelector.select_best`.
2. Verify same-strike daily guard.
3. Verify hard spread block and preflight bid/spread checks.
4. Verify `EntryStopLossPolicy` modes: `fixed`, `strike_atr`, `spot_atr`.
5. Verify moneyness-aware SL adaptation from entry delta.
6. Verify sizing formula and lot-size rounding.
7. Decide whether `allow_checkpoint_fallback=True` should remain enabled for low-score selector rejection.

Worst-case checks:
- 🟡 low score below selector threshold cannot silently become a live trade without explicit fallback policy
- ✅ one-lot risk above cap yields qty zero
- ✅ missing option symbol blocks entry
- ✅ bad bid/ask spread blocks entry
- ✅ same strike over daily cap blocks entry

### Phase 5 - Execution And Reconciliation

Objective: order state must remain idempotent and recoverable.

Tasks:
1. Verify paper mode never sends `client.placeorder`.
2. Verify live entry order polling and partial-fill handling.
3. Verify pending entry reconciliation, including post-cutoff fills.
4. Verify broker SL/TGT order placement, modify, cancel, and external-fill handling.
5. Verify pending exit reconciliation and retry release.
6. Verify trade journal append behavior and fields.
7. Verify startup position recovery and WS resubscribe.

Worst-case checks:
- ✅ entry placed but not filled remains pending or is reconciled/cancelled correctly
- ✅ exit submitted but not confirmed remains pending
- ✅ broker SL already filled prevents duplicate SELL
- ✅ restart with open broker position restores tracking conservatively
- ✅ journal is append-only

### Phase 6 - WebSocket And Live Protection

Objective: live ticks must protect positions without duplicate exits.

Tasks:
1. Verify subscribe/unsubscribe locking.
2. Verify reconnect/backoff and resubscribe behavior.
3. Verify premium trail, target, breakeven, spot trail, and deep OTM delta exit.
4. Verify `exit_pending` and `exit_queue` guard duplicate exits.
5. Verify watchdog/notify callback wiring.
6. Verify `_test_websocket()` behavior under normal subprocess execution and under an existing event loop.

Worst-case checks:
- ✅ stale feed warning/reconnect path is visible
- ✅ reconnect replays subscriptions
- ✅ premium and spot trail do not loosen SL
- ✅ breakeven moves once
- ✅ duplicate ticks do not trigger duplicate exits

### Phase 7 - Observability And Docs

Objective: every run should explain itself.

Tasks:
1. Verify scan header has symbol, spot, and IST timestamp.
2. Verify every scan return path closes with a separator.
3. Verify `[PERF]` Greeks cache logs are inside/outside the scan block as designed.
4. Recount runtime log stats after any new test run.
5. Update this single doc and remove any newly superseded markdown docs.

Worst-case checks:
- ✅ no early return leaves scan block open
- ✅ no debug noise leaks into production logs
- ✅ qty-zero and no-execute paths include performance logs
- ✅ updated docs do not reference deleted files as active sources

### Final Deliverables

After all phases:
1. Patch summary by phase.
2. Test commands and results.
3. Updated current-state gaps.
4. Updated graph-audit delta if graph was regenerated.
5. Recommendation for live or paper-only operation.

Human checkpoint:
- Pause before live deployment changes.
- Report unresolved FAIL items and accepted gaps.
- Ask for confirmation before enabling live unattended execution.

## 18. Testing Baseline

Already performed for this documentation pass:

```powershell
uv run python -m py_compile strategies\examples\BuyerEdgeStrategy.py
```

Result: ✅ PASS.

Recommended next tests before live capital:
- 🔴 register `BuyerEdgeStrategyBot`
- 🟡 run OpenAlgo local server
- 🟡 run one paper-trade session from the OpenAlgo Python runner
- 🟡 verify `OPENALGO_API_KEY`, `HOST_SERVER`, and `WEBSOCKET_URL` injection
- ✅ confirm host timezone or patch all time gates to use `get_ist_now`
- 🟡 replay morning/midday/EOD protocols using the consolidated revalidation prompt in Section 17
- 🟡 confirm VWAP volume data availability for the selected broker/feed
- 🟡 confirm IVR fields are available or accepted as optional
- 🟡 confirm fallback strike policy is desired

## 19. Current Conclusion

The current `BuyerEdgeStrategy.py` is a sophisticated single-file OpenAlgo options buyer strategy with extensive risk controls, signal scoring, execution reconciliation, and WebSocket-driven protection. It is structured to run inside OpenAlgo's built-in Python automation environment and compiles successfully.

The current state is strongest in:
- configuration validation
- risk-based quantity blocking
- order reconciliation
- scan observability
- Greeks/GEX-aware scoring
- moneyness-aware strike and SL improvements

The main items needing a deliberate next decision are:
- unify all runtime clocks to IST-safe helpers if the live server may run in UTC
- decide whether low-score fallback strike selection should remain enabled
- register the strategy in OpenAlgo before unattended live use
- run a fresh paper-trade session with current code and current server settings
