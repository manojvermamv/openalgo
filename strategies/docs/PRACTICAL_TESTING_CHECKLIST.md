# Practical Testing Execution Checklist

**Purpose**: Step-by-step guide to run real-world scenario tests and capture metrics  
**Output**: Collect data for comparison before/after fixes

---

## Test Execution Schedule

### Test 1: Full Morning Session (Market Open)
**When**: Next trading day, 09:14 IST  
**Duration**: 09:15–11:00 IST (105 minutes, 105 scans)

#### Pre-Test Setup
- [ ] Clear previous logs: `rm -f logs/strategy.log`
- [ ] Ensure API key configured
- [ ] Verify WebSocket connection works
- [ ] Start fresh Python process

#### Test 1 Execution
```bash
# 1. Start exactly at 09:14:30 IST (before market open)
python3 BuyerEdgeStrategy.py | tee test1_morning_open.log &
TEST1_PID=$!

# 2. Wait for 105 minutes (09:15–11:00)
# Monitor in parallel terminal:
tail -f test1_morning_open.log | grep -E "\[SCAN\]|\[SCORE\]"

# 3. At 11:00 IST, stop script
kill $TEST1_PID

# 4. Extract metrics
```

#### Metrics to Capture
```python
# Run this script against test1_morning_open.log:

import re
import pandas as pd
from datetime import datetime, timedelta

log_file = "test1_morning_open.log"
data = []

with open(log_file) as f:
    for line in f:
        if "[SCAN]" in line:
            # Extract time, symbol, score, components
            match = re.search(r'\[(\d+:\d+:\d+)\].*Score: ([\d\-]+)', line)
            if match:
                time_str, score = match.groups()
                data.append({
                    'time': time_str,
                    'score': int(score),
                    'scan_num': len(data) + 1
                })

df = pd.DataFrame(data)

print(f"=== TEST 1: MORNING OPEN (09:15–11:00) ===")
print(f"Total Scans: {len(df)}")
print(f"Score Stats:")
print(f"  Min:  {df['score'].min()}")
print(f"  Max:  {df['score'].max()}")
print(f"  Mean: {df['score'].mean():.1f}")
print(f"  Std:  {df['score'].std():.1f}")
print(f"  Median: {df['score'].median():.1f}")

# Count phase transitions
scans_blocked = df[df['score'] < 15].shape[0]
scans_watchable = df[(df['score'] >= 15) & (df['score'] < 30)].shape[0]
scans_tradeable = df[df['score'] >= 30].shape[0]

print(f"\nScore Distribution:")
print(f"  < 15 (blocked):     {scans_blocked} scans ({100*scans_blocked/len(df):.0f}%)")
print(f"  15-30 (watchable):  {scans_watchable} scans ({100*scans_watchable/len(df):.0f}%)")
print(f"  >= 30 (tradeable):  {scans_tradeable} scans ({100*scans_tradeable/len(df):.0f}%)")

# Identify when VWAP unlocks (score jumps)
df['score_delta'] = df['score'].diff()
unlocks = df[df['score_delta'] > 5]
if len(unlocks) > 0:
    print(f"\nVWAP Unlock Event:")
    print(f"  At scan: {unlocks.iloc[0]['scan_num']}")
    print(f"  Time: {unlocks.iloc[0]['time']}")
    print(f"  Score jumped from {unlocks.iloc[0]['score'] - 5} to {unlocks.iloc[0]['score']}")
```

#### Expected Results (Test 1)
```
=== TEST 1: MORNING OPEN (09:15–11:00) ===
Total Scans: 105

Score Stats:
  Min:  5
  Max:  38
  Mean: 19.3
  Std:  11.2
  Median: 16

Score Distribution:
  < 15 (blocked):     48 scans (46%)  ← Phase 1: Market open
  15-30 (watchable):  42 scans (40%)  ← Phase 2: Transition
  >= 30 (tradeable):  15 scans (14%)  ← Phase 3: Stabilized

VWAP Unlock Event:
  At scan: ~34
  Time: 10:30  (calculated: 60 mins from 09:15 + 15 min candle lag)
  Score jumped from 15 to 21
```

---

### Test 2: Midday Session (Stable Conditions)
**When**: Same day, 10:30–11:30 IST (60 scans)

#### Execution
```bash
# Start at 10:30 IST (after VWAP unlock)
python3 BuyerEdgeStrategy.py | tee test2_midday_stable.log &
TEST2_PID=$!

# Run for 60 minutes
# At 11:30 IST, stop
kill $TEST2_PID
```

#### Metrics to Capture
```python
# Same extraction as Test 1, but compare results

print(f"=== TEST 2: MIDDAY STABLE (10:30–11:30) ===")
print(f"[COMPARE TO TEST 1]")
print(f"Score Improvement: {df_test2['score'].mean():.1f} vs {df_test1['score'].mean():.1f}")
print(f"Available Scans: {(df_test2['score'] >= 30).sum()} vs {(df_test1['score'] >= 30).sum()}")
```

#### Expected Results (Test 2)
```
=== TEST 2: MIDDAY STABLE (10:30–11:30) ===
Score Stats:
  Min:  12
  Max:  52
  Mean: 32.1  ← 67% higher than morning!
  Std:  14.5
  Median: 31

Score Distribution:
  < 15 (blocked):     3 scans (5%)    ← Almost none
  15-30 (watchable):  25 scans (42%)
  >= 30 (tradeable):  32 scans (53%)  ← Majority tradeable!
```

---

### Test 3: EOD Compression (Risk Gates Active)
**When**: Same day, 14:00–15:30 IST (90 scans, but entries blocked after 13:30)

#### Execution
```bash
# Note: Entries will be BLOCKED after 13:30 (expected behavior)
python3 BuyerEdgeStrategy.py | tee test3_eod_compression.log &
TEST3_PID=$!

# At 15:30, stop
kill $TEST3_PID
```

#### Metrics to Capture
```python
# Parse for "blocked by risk gate" messages
print(f"=== TEST 3: EOD COMPRESSION (14:00–15:30) ===")

# Count entry blocks
with open("test3_eod_compression.log") as f:
    blocked_count = len([l for l in f if "blocked by risk gate" in l])

print(f"Entry Blocks after 13:30: {blocked_count}")
print(f"  (Expected: ~50-60 scans, all blocked by 'No new entries after 13:30')")

# Still measure score (for diagnostics)
print(f"Score Range: {df_test3['score'].min()}–{df_test3['score'].max()}")
print(f"Avg Score: {df_test3['score'].mean():.1f}")
print(f"  (Typically elevated due to time decay and volatility)")
```

#### Expected Results (Test 3)
```
=== TEST 3: EOD COMPRESSION (14:00–15:30) ===

Entry Blocks after 13:30: 55  ← Expected behavior, not a bug

Score Stats:
  Min:  18
  Max:  58
  Mean: 38.2  ← Highest of all tests (time decay effect)
  Std:  15.1

Status: ALL ENTRIES BLOCKED (as configured) ✓
```

---

## Component-Level Audit Tests

### Component Test A: VWAP Availability Timeline

```bash
# Extract all VWAP messages from logs
grep "Spot vs VWAP" test1_morning_open.log | head -50

# Expected output:
# Scan 1:  [SCAN 09:15] Spot vs VWAP   VWAP insufficient bars (need 5)
# Scan 2:  [SCAN 09:16] Spot vs VWAP   VWAP insufficient bars (need 5)
# ...
# Scan 34: [SCAN 10:30] Spot vs VWAP   Spot 19245.5 above VWAP 19240.2 (rolling_5bar, 5 valid bars)  ← UNLOCK!
# ...

# Count availability
available=$(grep "above\|below VWAP" test1_morning_open.log | wc -l)
total=$(grep "Spot vs VWAP" test1_morning_open.log | wc -l)

echo "VWAP Availability: $available / $total scans ($(( 100 * available / total ))%)"
```

#### Expected Results
```
VWAP Availability Timeline:
  Scans 1-20:  0/20 available (0%)
  Scans 21-33: 0/13 available (0%)
  Scans 34+:   (34-last)/scans available (95-100%)

Total: ~50/105 scans (48%) for 105-minute run starting at market open
```

---

### Component Test B: OI Velocity Pattern

```bash
# Extract OI velocity messages
grep "OI Velocity" test1_morning_open.log | head -20

# Expected output:
# Scan 1:  [SCAN 09:15] OI Velocity  OI velocity below threshold (CE +0.00%, PE +0.00%)
# Scan 2:  [SCAN 09:16] OI Velocity  OI velocity below threshold (CE +0.12%, PE -0.05%)
# ...
# Scan 15: [SCAN 09:29] OI Velocity  CE OI building +1.2% + call buying ← Score +1!

# Calculate percentage scoring (non-zero)
non_threshold=$(grep "above threshold\|building\|unwinding" test1_morning_open.log | wc -l)
below_threshold=$(grep "below threshold" test1_morning_open.log | wc -l)

echo "OI Velocity Scoring: $non_threshold / $((non_threshold + below_threshold)) scans ($(( 100 * non_threshold / (non_threshold + below_threshold) ))%)"
```

#### Expected Results
```
OI Velocity Scoring Rate:
  Early (scans 1-20): 5% (broker lag on OI changes)
  Mid (scans 21-60): 30% (building up)
  Late (scans 61+): 60-70% (OI moving)
```

---

### Component Test C: IV Rank Availability

```bash
# Extract IV Rank messages
grep "IV Regime" test*.log

# Count availability
available=$(grep "IVR [0-9]" test*.log | wc -l)
unavailable=$(grep "IVR unavailable" test*.log | wc -l)

echo "IV Rank Availability:"
echo "  Available: $available scans"
echo "  Unavailable: $unavailable scans"
echo "  Rate: $((100 * available / (available + unavailable)))%"
```

#### Expected Results
```
IV Rank Availability:
  Available:    10-30 scans out of 255 total
  Unavailable:  225-245 scans
  Rate:         5-15%  ← Broker API limitation, not code issue
```

---

## Data Quality Matrix

### Create Scorecard
```yaml
TEST DAY: 2026-05-26
SESSION:  09:15–15:30 (full day)

COMPONENT SCORECARD:
├─ VWAP
│  ├─ Availability: 48%  (blocked first 45 min)
│  ├─ Unlock Time: 10:30 (scan #34)
│  ├─ Reason: Need 5 complete 15m candles
│  └─ Status: EXPECTED BEHAVIOR ✓
│
├─ OI Velocity
│  ├─ Availability: 35%
│  ├─ Reason: Broker returns 0 or 'oi_change' missing
│  └─ Status: NEEDS FALLBACK (snapshot-based calculation)
│
├─ IV Rank
│  ├─ Availability: 8%
│  ├─ Reason: Quote missing 'iv' field
│  └─ Status: NEEDS FALLBACK (use ATM greeks.iv)
│
├─ Straddle Velocity
│  ├─ Availability: 20%
│  ├─ Reason: Threshold ±0.5% filters noise
│  └─ Status: WORKING AS DESIGNED ✓
│
└─ Synthetic Futures
   ├─ Availability: 98%
   ├─ Reason: Works after scan #2
   └─ Status: WORKING AS DESIGNED ✓

OVERALL SIGNAL QUALITY:
  Average Composite Score: 19.2/100
  Score Range: 5–58
  Std Deviation: 12.3
  
  Tradeable Scans (>=40): 12% of session
  Expected Impact: 1-3 trades per full day
```

---

## Comparison Template (Before/After Fixes)

Run this template 3 times:
1. **Before**: Current code (baseline)
2. **After Fix #1**: OI Velocity snapshot fallback
3. **After Fix #2**: IV Rank from ATM greeks

```yaml
BASELINE (Current):
  Date: 2026-05-26
  Avg Score: 19.2
  VWAP Availability: 48%
  OI Velocity Availability: 35%
  IV Rank Availability: 8%
  Total Component Points Available: 43% of maximum

AFTER FIX #1 (OI Velocity Fallback):
  Date: 2026-05-27
  Avg Score: 21.5  (↑ +2.3 points)
  OI Velocity Availability: 65%  (↑ +30%)
  Expected Trades: +1 per day

AFTER FIX #2 (IV Rank Fallback):
  Date: 2026-05-28
  Avg Score: 23.1  (↑ +1.6 more points)
  IV Rank Availability: 45%  (↑ +37%)
  Expected Trades: +1 more per day

CUMULATIVE IMPACT:
  Score Improvement: 19.2 → 23.1 (+20%)
  Trades/Day: 1-2 → 3-4 (+100%)
```

---

## Red Flags Checklist

If you observe ANY of these, escalate immediately:

- [ ] VWAP still "insufficient bars" at 11:00 AM → API lookback_days too short
- [ ] OI velocity exactly 0.00% all day → Broker not returning oi_change field
- [ ] Score never exceeds 20 → Multiple components permanently broken
- [ ] Strategy crashes with "No volume provided" → Use fixed VWAP filter code
- [ ] Composite score negative (-10 to -50) → Check if weights inverted
- [ ] IV Rank available but always > 80% → Broker IV data stale
- [ ] WebSocket disconnects mid-session → Network/firewall issue
- [ ] Fewer than 30 scans completed in 60 minutes → Performance bottleneck

---

## Summary: What to Expect

### Morning Session (09:15–11:00)
- **First 30 min**: Components blocked, score 5-15
- **Next 30 min**: Transition, score 15-28
- **Last 45 min**: Stabilized, score 25-40

### Midday (10:30–15:30)
- **Stable**: Score 25-50, components 80%+ available
- **Volatile events**: Score spikes to 50-60, components 85-90% available

### EOD (14:00–15:30)
- **Score elevated**: 35-50 (time decay effect)
- **Entry blocks**: ALL entries blocked after 13:30 (expected) ✓

### Overall Expected Improvement
- **Before fixes**: 19.2 avg score, 1-2 trades/day
- **After fixes**: 23-25 avg score, 3-4 trades/day
- **Effort**: 2-3 days of testing + 4-8 hours of implementation
