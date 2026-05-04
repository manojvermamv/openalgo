import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronsUpDown, RefreshCw, ChevronDown, ChevronUp, Wifi, WifiOff, Info } from 'lucide-react'
import {
  ColorType,
  CrosshairMode,
  LineSeries,
  CandlestickSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from 'lightweight-charts'
import { useSupportedExchanges } from '@/hooks/useSupportedExchanges'
import { useThemeStore } from '@/stores/themeStore'
import { useAuthStore } from '@/stores/authStore'
import { useOptionChainLive } from '@/hooks/useOptionChainLive'
import { useMarketStatus } from '@/hooks/useMarketStatus'
import {
  buyerEdgeApi,
  type BuyerEdgeResponse,
  type GexLevelsResponse,
  type PcrChartResponse,
  type StrikeOiChange,
  type IvDashboardResponse,
  type SignalType,
  type SpotCandleResponse,
} from '@/api/buyerEdge'
import {
  straddleChartApi,
  type StraddleChartData,
  type StraddleDataPoint,
} from '@/api/straddle-chart'
import { ivChartApi, type IVChartData } from '@/api/iv-chart'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { showToast } from '@/utils/toast'

const AUTO_REFRESH_INTERVAL = 30000
const STRADDLE_CHART_HEIGHT = 400
const OI_CHANGE_CHART_HEIGHT = 300
// Threshold (in price points) for proximity annotations in GEX/IVx charts
const STRIKE_PROXIMITY_THRESHOLD = 50
// IVx expiry bar chart layout
const IVX_BAR_WIDTH = 40
const IVX_BAR_GAP = 16
const IVX_CHART_HEIGHT = 120

function convertExpiryForAPI(expiry: string): string {
  if (!expiry) return ''
  const parts = expiry.split('-')
  if (parts.length === 3) {
    return `${parts[0]}${parts[1].toUpperCase()}${parts[2].slice(-2)}`
  }
  return expiry.replace(/-/g, '').toUpperCase()
}

function formatIST(unixSeconds: number): { date: string; time: string } {
  const d = new Date(unixSeconds * 1000)
  const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
  const dd = ist.getUTCDate().toString().padStart(2, '0')
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const mo = months[ist.getUTCMonth()]
  const hh = ist.getUTCHours().toString().padStart(2, '0')
  const mm = ist.getUTCMinutes().toString().padStart(2, '0')
  const ampm = ist.getUTCHours() >= 12 ? 'PM' : 'AM'
  return { date: `${dd} ${mo}`, time: `${hh}:${mm} ${ampm}` }
}

/** Return interpretation label + colour for an ADR value (combined strike OI breadth). */
function getAdrMeta(adr: number): { label: string; color: string } {
  if (adr >= 1.5) return { label: 'Strong Bull', color: '#22c55e' }
  if (adr >= 1.0) return { label: 'Bullish', color: '#86efac' }
  if (adr >= 0.9) return { label: 'Sideways', color: '#fbbf24' }
  if (adr >= 0.7) return { label: 'Bearish', color: '#fb923c' }
  return { label: 'Strong Bear', color: '#ef4444' }
}

/**
 * Premium + OI matrix for an individual options leg.
 * Uses ATM option premium direction (CE or PE LTP change from session open).
 *
 * CE leg — directional interpretation:
 *   OI↑ + Prem↑ = Call Buying   (bullish demand — real call buyers)
 *   OI↑ + Prem↓ = Call Writing  (bearish ceiling — sellers writing OTM calls)
 *   OI↓ + Prem↑ = CE Short Covering (call shorts closing — mild bullish)
 *   OI↓ + Prem↓ = CE Long Unwinding (call longs exiting — bearish)
 *
 * PE leg — directional interpretation:
 *   OI↑ + Prem↑ = Put Buying   (bearish demand — real put buyers / hedgers)
 *   OI↑ + Prem↓ = Put Writing  (bullish floor — sellers writing OTM puts)
 *   OI↓ + Prem↑ = PE Short Covering (put shorts closing — mildly bearish)
 *   OI↓ + Prem↓ = PE Long Unwinding (put longs exiting — bullish)
 */
function getOiFlowMeta(
  leg: 'CE' | 'PE',
  oiDelta: number,
  /** Session-start to session-current ATM CE or PE LTP change. null = no premium data. */
  premiumDelta: number | null,
): { label: string; color: string; note: string } | null {
  if (oiDelta === 0) return null
  // Treat tiny premium moves (< ₹0.50) as noise — don't classify direction
  const premUp: boolean | null =
    premiumDelta !== null ? (Math.abs(premiumDelta) >= 0.5 ? premiumDelta > 0 : null) : null

  if (leg === 'CE') {
    if (oiDelta > 0) {
      if (premUp === true) return { label: 'Call Buying', color: '#22c55e', note: 'CE Prem↑ + OI↑ → real call demand (Bullish)' }
      if (premUp === false) return { label: 'Call Writing', color: '#ef4444', note: 'CE Prem↓ + OI↑ → writers adding supply (Bearish ceiling)' }
      return { label: 'CE OI Rising', color: '#86efac', note: 'CE OI↑ — confirm with ATM CE premium direction' }
    }
    if (premUp === true) return { label: 'CE Short Covering', color: '#86efac', note: 'CE Prem↑ + OI↓ → call shorts exiting (Mild Bullish)' }
    if (premUp === false) return { label: 'CE Long Unwinding', color: '#fb923c', note: 'CE Prem↓ + OI↓ → call longs exiting (Bearish)' }
    return { label: 'CE OI Falling', color: '#fb923c', note: 'CE OI↓ — confirm with ATM CE premium direction' }
  }

  // PE leg
  if (oiDelta > 0) {
    if (premUp === true) return { label: 'Put Buying', color: '#ef4444', note: 'PE Prem↑ + OI↑ → real put demand (Bearish hedge)' }
    if (premUp === false) return { label: 'Put Writing', color: '#22c55e', note: 'PE Prem↓ + OI↑ → writers building floor (Bullish support)' }
    return { label: 'PE OI Rising', color: '#f59e0b', note: 'PE OI↑ — confirm with ATM PE premium direction' }
  }
  if (premUp === true) return { label: 'PE Short Covering', color: '#f59e0b', note: 'PE Prem↑ + OI↓ → put shorts uncertain (Mildly Bearish)' }
  if (premUp === false) return { label: 'PE Long Unwinding', color: '#86efac', note: 'PE Prem↓ + OI↓ → hedgers exiting (Bullish)' }
  return { label: 'PE OI Falling', color: '#86efac', note: 'PE OI↓ — confirm with ATM PE premium direction' }
}

/**
 * OI regime when CE and PE legs are considered together.
 * "Dual OI Expansion" is deliberately ambiguous — add ATM premium direction
 * to distinguish straddle buying (OI↑ + Prem↑) from straddle writing (OI↑ + Prem↓).
 */
function getOiRegimeMeta(
  ceDelta: number,
  peDelta: number,
): { label: string; color: string } {
  if (ceDelta > 0 && peDelta > 0) return { label: 'Dual OI Expansion', color: '#f59e0b' }
  if (ceDelta < 0 && peDelta < 0) return { label: 'OI Convergence', color: '#818cf8' }
  if (ceDelta > 0) return { label: 'CE Dominant', color: '#ef4444' }
  return { label: 'PE Dominant', color: '#22c55e' }
}

/**
 * Directional Breadth Bias from per-leg strike advancers.
 * Breadth Bias = PE Advancers / CE Advancers.
 * > 1.2 → more puts advancing = Bearish breadth.
 * < 0.83 → more calls advancing = Bullish breadth.
 */
function getBreadthBias(
  ceAdvances: number,
  peAdvances: number,
): { bias: number; label: string; color: string } | null {
  if (ceAdvances + peAdvances === 0) return null
  if (ceAdvances === 0) return { bias: 99, label: 'PE Dominated', color: '#ef4444' }
  const bias = peAdvances / ceAdvances
  if (bias >= 1.5) return { bias, label: 'Bearish Breadth', color: '#ef4444' }
  if (bias >= 1.2) return { bias, label: 'Mild Bearish Breadth', color: '#fb923c' }
  if (bias <= 0.67) return { bias, label: 'Bullish Breadth', color: '#22c55e' }
  if (bias <= 0.83) return { bias, label: 'Mild Bullish Breadth', color: '#86efac' }
  return { bias, label: 'Neutral Breadth', color: '#fbbf24' }
}

// ---------------------------------------------------------------------------
// Directional Conviction Score
// ---------------------------------------------------------------------------

interface ScoreComponent {
  label: string
  score: number   // raw contribution (may be fractional)
  max: number     // maximum absolute contribution for this component
  note: string    // human-readable signal value
  direction: 'bullish' | 'bearish' | 'neutral'
}

interface DirectionalScoreResult {
  /** Normalised score: -100 to +100 */
  pct: number
  /** Raw sum */
  raw: number
  /** Maximum possible absolute raw sum */
  maxRaw: number
  label: 'Strong Bullish' | 'Mild Bullish' | 'Neutral' | 'Mild Bearish' | 'Strong Bearish'
  color: string
  components: ScoreComponent[]
}

/**
 * Aggregate up to 10 signal dimensions into a single directional conviction score.
 *
 * Scoring weights:
 *  1. Spot short-term (vs prev PCR bar)            ±1
 *  2. Spot session anchor (vs first PCR bar)       ±1
 *  3. Spot vs session spot-VWAP (mean of PCR spots)±1
 *  4. PCR OI level                                 ±1
 *  5. PCR OI change direction                      ±1
 *  6. CE Flow (premium+OI matrix)                  ±2
 *  7. PE Flow (premium+OI matrix)                  ±2
 *  8. Breadth Bias (PE/CE advancers ratio)         ±1
 *  9. Delta Imbalance (greeks engine)              ±1
 * 10. Market State Trend                           ±1
 *                                            Max = 12
 */
function computeDirectionalScore(params: {
  pcrSeries: import('../api/buyerEdge').PcrDataPoint[]
  pcrOi: number | null
  pcrOiChg: number | null
  deltaImbalance: number | null
  trend: string | null
}): DirectionalScoreResult {
  const { pcrSeries, pcrOi, pcrOiChg, deltaImbalance, trend } = params
  const components: ScoreComponent[] = []
  let raw = 0
  const MAX_RAW = 12

  // ── helpers ──────────────────────────────────────────────────────────────
  function push(c: ScoreComponent) {
    components.push(c)
    raw += c.score
  }
  function dir(s: number): 'bullish' | 'bearish' | 'neutral' {
    return s > 0 ? 'bullish' : s < 0 ? 'bearish' : 'neutral'
  }

  const hasSeries = pcrSeries.length >= 2
  const latest = hasSeries ? pcrSeries[pcrSeries.length - 1] : null
  const prevBar = hasSeries ? pcrSeries[pcrSeries.length - 2] : null
  const first = hasSeries ? pcrSeries[0] : null

  // ── 1. Spot short-term (vs previous bar) ─────────────────────────────────
  {
    const s =
      latest && prevBar
        ? latest.spot > prevBar.spot
          ? 1
          : latest.spot < prevBar.spot
            ? -1
            : 0
        : 0
    push({ label: 'Spot (short-term)', score: s, max: 1, note: s > 0 ? 'Spot ↑ vs prev bar' : s < 0 ? 'Spot ↓ vs prev bar' : 'Spot flat', direction: dir(s) })
  }

  // ── 2. Spot session anchor (vs first bar) ─────────────────────────────────
  {
    const s =
      latest && first
        ? latest.spot > first.spot
          ? 1
          : latest.spot < first.spot
            ? -1
            : 0
        : 0
    push({ label: 'Spot (session)', score: s, max: 1, note: s > 0 ? 'Spot ↑ vs open' : s < 0 ? 'Spot ↓ vs open' : 'Spot at open', direction: dir(s) })
  }

  // ── 3. Spot vs session spot-VWAP ─────────────────────────────────────────
  {
    let spotVwap: number | null = null
    if (hasSeries && latest) {
      const sum = pcrSeries.reduce((acc, p) => acc + p.spot, 0)
      spotVwap = sum / pcrSeries.length
    }
    const s =
      latest && spotVwap !== null
        ? latest.spot > spotVwap
          ? 1
          : latest.spot < spotVwap
            ? -1
            : 0
        : 0
    push({ label: 'Spot vs VWAP', score: s, max: 1, note: spotVwap !== null ? `Spot ${s > 0 ? '↑' : s < 0 ? '↓' : '='} VWAP ${spotVwap.toFixed(1)}` : '—', direction: dir(s) })
  }

  // ── 4. PCR OI Level ───────────────────────────────────────────────────────
  {
    let s = 0
    let note = '—'
    if (pcrOi !== null) {
      if (pcrOi >= 1.2) { s = 1; note = `PCR OI ${pcrOi.toFixed(2)} (Strong put writing)` }
      else if (pcrOi >= 1.0) { s = 0.5; note = `PCR OI ${pcrOi.toFixed(2)} (Moderate bullish)` }
      else if (pcrOi <= 0.6) { s = -1; note = `PCR OI ${pcrOi.toFixed(2)} (Strong call writing)` }
      else if (pcrOi <= 0.8) { s = -0.5; note = `PCR OI ${pcrOi.toFixed(2)} (Moderate bearish)` }
      else { s = 0; note = `PCR OI ${pcrOi.toFixed(2)} (Neutral)` }
    }
    push({ label: 'PCR OI Level', score: s, max: 1, note, direction: dir(s) })
  }

  // ── 5. PCR OI Change direction ────────────────────────────────────────────
  {
    let s = 0
    let note = '—'
    if (pcrOiChg !== null) {
      if (pcrOiChg >= 1.2) { s = 1; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Intraday put build-up)` }
      else if (pcrOiChg >= 0.9) { s = 0.5; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Moderate build-up)` }
      else if (pcrOiChg <= 0.5) { s = -1; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Intraday call build-up)` }
      else if (pcrOiChg <= 0.7) { s = -0.5; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Moderate call build-up)` }
      else { s = 0; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Neutral intraday)` }
    }
    push({ label: 'PCR OI Change', score: s, max: 1, note, direction: dir(s) })
  }

  // ── 6. CE Flow (premium + OI matrix) ─────────────────────────────────────
  // CE scoring: Call Buying = bullish demand = +2, Call Writing = bearish ceiling = -2
  {
    let s = 0
    let note = '—'
    if (latest && first) {
      const ceOiDelta = latest.ce_oi - first.ce_oi
      const cePremDelta = latest.atm_ce_ltp - first.atm_ce_ltp
      const meta = getOiFlowMeta('CE', ceOiDelta, cePremDelta)
      if (meta) {
        note = meta.label
        switch (meta.label) {
          case 'Call Buying': s = 2; break
          case 'CE Short Covering': s = 1; break
          case 'CE Long Unwinding': s = -1; break
          case 'Call Writing': s = -2; break
          default: s = 0
        }
      }
    }
    push({ label: 'CE Flow', score: s, max: 2, note, direction: dir(s) })
  }

  // ── 7. PE Flow (premium + OI matrix) ─────────────────────────────────────
  {
    let s = 0
    let note = '—'
    if (latest && first) {
      const peOiDelta = latest.pe_oi - first.pe_oi
      const pePremDelta = latest.atm_pe_ltp - first.atm_pe_ltp
      const meta = getOiFlowMeta('PE', peOiDelta, pePremDelta)
      if (meta) {
        note = meta.label
        // PE scoring:
        // Put Writing = bullish support floor = +2
        // PE Long Unwinding = hedgers exiting = +1 (bullish)
        // PE Short Covering = mildly bearish = -1
        // Put Buying = bearish demand = -2
        switch (meta.label) {
          case 'Put Writing': s = 2; break
          case 'PE Long Unwinding': s = 1; break
          case 'PE Short Covering': s = -1; break
          case 'Put Buying': s = -2; break
          default: s = 0
        }
      }
    }
    push({ label: 'PE Flow', score: s, max: 2, note, direction: dir(s) })
  }

  // ── 8. Breadth Bias ───────────────────────────────────────────────────────
  {
    let s = 0
    let note = '—'
    if (latest) {
      const bb = getBreadthBias(latest.ce_advances, latest.pe_advances)
      if (bb) {
        note = bb.label
        switch (bb.label) {
          case 'Bullish Breadth': s = 1; break
          case 'Mild Bullish Breadth': s = 0.5; break
          case 'Neutral Breadth': s = 0; break
          case 'Mild Bearish Breadth': s = -0.5; break
          case 'Bearish Breadth':
          case 'PE Dominated': s = -1; break
        }
      }
    }
    push({ label: 'Breadth Bias', score: s, max: 1, note, direction: dir(s) })
  }

  // ── 9. Delta Imbalance ────────────────────────────────────────────────────
  {
    let s = 0
    let note = '—'
    if (deltaImbalance !== null) {
      if (deltaImbalance >= 0.1) { s = 1; note = `Δ imbalance +${deltaImbalance.toFixed(3)} (Call delta dominant)` }
      else if (deltaImbalance >= 0.05) { s = 0.5; note = `Δ imbalance +${deltaImbalance.toFixed(3)} (Mild call bias)` }
      else if (deltaImbalance <= -0.1) { s = -1; note = `Δ imbalance ${deltaImbalance.toFixed(3)} (Put delta dominant)` }
      else if (deltaImbalance <= -0.05) { s = -0.5; note = `Δ imbalance ${deltaImbalance.toFixed(3)} (Mild put bias)` }
      else { s = 0; note = `Δ imbalance ${deltaImbalance.toFixed(3)} (Balanced)` }
    }
    push({ label: 'Delta Imbalance', score: s, max: 1, note, direction: dir(s) })
  }

  // ── 10. Market State Trend ────────────────────────────────────────────────
  {
    let s = 0
    let note = trend ?? '—'
    if (trend === 'Bullish') s = 1
    if (trend === 'Bearish') s = -1
    push({ label: 'Market Trend', score: s, max: 1, note, direction: dir(s) })
  }

  // ── Normalise ─────────────────────────────────────────────────────────────
  const pct = Math.round((raw / MAX_RAW) * 100)
  let label: DirectionalScoreResult['label']
  let color: string
  if (pct >= 50) { label = 'Strong Bullish'; color = '#22c55e' }
  else if (pct >= 20) { label = 'Mild Bullish'; color = '#86efac' }
  else if (pct <= -50) { label = 'Strong Bearish'; color = '#ef4444' }
  else if (pct <= -20) { label = 'Mild Bearish'; color = '#fb923c' }
  else { label = 'Neutral'; color = '#fbbf24' }

  return { pct, raw, maxRaw: MAX_RAW, label, color, components }
}

// ---------------------------------------------------------------------------
// Directional Score Gauge component
// ---------------------------------------------------------------------------
function DirectionalScoreGauge({ result }: { result: DirectionalScoreResult }) {
  // Position of the needle: map -100..+100 to 0..100%
  const needlePct = Math.min(100, Math.max(0, ((result.pct + 100) / 200) * 100))
  return (
    <Card className="border-2" style={{ borderColor: result.color + '55' }}>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Directional Conviction Score</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Score display */}
        <div className="flex items-center gap-4">
          <span className="text-4xl font-black" style={{ color: result.color }}>
            {result.pct > 0 ? '+' : ''}{result.pct}
          </span>
          <div className="flex flex-col gap-1">
            <span className="font-semibold text-sm" style={{ color: result.color }}>{result.label}</span>
            <span className="text-xs text-muted-foreground">
              Raw {result.raw.toFixed(1)} / max ±{result.maxRaw} · {result.components.length} signals
            </span>
          </div>
        </div>

        {/* Thermometer gauge */}
        <div className="space-y-1">
          <div className="relative h-4 rounded-full overflow-hidden"
            style={{ background: 'linear-gradient(to right, #ef4444 0%, #fb923c 25%, #fbbf24 50%, #86efac 75%, #22c55e 100%)' }}>
            {/* Center line */}
            <div className="absolute top-0 bottom-0 w-0.5 bg-background/70 left-1/2" />
            {/* Needle */}
            <div
              className="absolute top-0.5 bottom-0.5 w-2.5 rounded-full bg-white shadow-md border border-background/60 transition-all duration-500"
              style={{ left: `calc(${needlePct}% - 5px)` }}
            />
          </div>
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>Strong Bear -100</span>
            <span>Neutral 0</span>
            <span>+100 Strong Bull</span>
          </div>
        </div>

        {/* Per-signal breakdown */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 pt-1">
          {result.components.map((c) => {
            const barPct = Math.round((Math.abs(c.score) / c.max) * 100)
            const barColor = c.direction === 'bullish' ? '#22c55e' : c.direction === 'bearish' ? '#ef4444' : '#fbbf24'
            const scoreStr = c.score > 0 ? `+${c.score}` : c.score === 0 ? '0' : `${c.score}`
            return (
              <div key={c.label} className="flex items-center gap-2 py-0.5">
                <span className="text-xs text-muted-foreground w-[120px] shrink-0 truncate" title={c.label}>{c.label}</span>
                <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full rounded-full transition-all duration-300"
                    style={{
                      width: `${barPct}%`,
                      background: barColor,
                      marginLeft: c.score < 0 ? 'auto' : undefined,
                    }}
                  />
                </div>
                <span
                  className="text-xs font-bold w-8 text-right shrink-0"
                  style={{ color: barColor }}
                >
                  {scoreStr}
                </span>
                <span className="text-xs text-muted-foreground max-w-[140px] truncate hidden sm:block" title={c.note}>{c.note}</span>
              </div>
            )
          })}
        </div>
      </CardContent>
    </Card>
  )
}

/** Return moneyness label for a strike relative to current spot. */
function getMoneyness(strike: number, spot: number): string {
  // Use 0.3% of spot as ATM band — scales with the underlying level
  const atmBand = spot * 0.003
  if (Math.abs(strike - spot) <= atmBand) return 'ATM'
  return strike > spot ? 'OTM-CE / ITM-PE' : 'ITM-CE / OTM-PE'
}

/** Format an OI value as Indian Lakh (1L = 100,000) or Crore. */
function formatOiLakh(v: number): string {
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)}L`
  return `${sign}${abs.toLocaleString('en-IN')}`
}

/**
 * Compute 1-Day Anchored VWAP on straddle price.
 * Since straddle data carries no exchange volume, each bar is treated with equal
 * weight (weight = 1), making this a cumulative arithmetic mean of the straddle
 * price that resets at the start of each trading day (9:15 AM IST).
 */
function computeDayAnchoredVWAP(points: StraddleDataPoint[]): { time: number; value: number }[] {
  if (!points.length) return []
  const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000
  let dayKey = ''
  let cumSum = 0
  let cumCount = 0
  return points.map((p) => {
    const istDate = new Date(p.time * 1000 + IST_OFFSET_MS)
    const key = `${istDate.getUTCFullYear()}-${istDate.getUTCMonth()}-${istDate.getUTCDate()}`
    if (key !== dayKey) {
      dayKey = key
      cumSum = 0
      cumCount = 0
    }
    cumSum += p.straddle
    cumCount += 1
    return { time: p.time, value: cumSum / cumCount }
  })
}

// ---------------------------------------------------------------------------
// Signal colours
// ---------------------------------------------------------------------------
const SIGNAL_CONFIG: Record<
  SignalType,
  { label: string; bg: string; text: string; border: string; badgeClass: string }
> = {
  EXECUTE: {
    label: 'EXECUTE',
    bg: 'bg-green-500/10',
    text: 'text-green-600 dark:text-green-400',
    border: 'border-green-500/30',
    badgeClass: 'bg-green-500 text-white',
  },
  WATCH: {
    label: 'WATCH',
    bg: 'bg-yellow-500/10',
    text: 'text-yellow-600 dark:text-yellow-400',
    border: 'border-yellow-500/30',
    badgeClass: 'bg-yellow-500 text-black',
  },
  NO_TRADE: {
    label: 'NO TRADE',
    bg: 'bg-red-500/10',
    text: 'text-red-600 dark:text-red-400',
    border: 'border-red-500/30',
    badgeClass: 'bg-red-500 text-white',
  },
}

// ---------------------------------------------------------------------------
// Pill helper
// ---------------------------------------------------------------------------
function Pill({
  label,
  value,
  variant = 'neutral',
}: {
  label: string
  value: string
  variant?: 'bullish' | 'bearish' | 'neutral' | 'warning' | 'expansion'
}) {
  const color = {
    bullish: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
    bearish: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
    neutral: 'bg-muted text-muted-foreground',
    warning: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
    expansion: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  }[variant]

  return (
    <div className="flex flex-col items-center gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${color}`}>{value}</span>
    </div>
  )
}

function pillVariant(value: string): 'bullish' | 'bearish' | 'neutral' | 'warning' | 'expansion' {
  const v = value.toLowerCase()
  if (v.includes('bullish') || v.includes('rising') || v.includes('expanding') || v === 'breakout')
    return 'bullish'
  if (
    v.includes('bearish') ||
    v.includes('falling') ||
    v.includes('contracting') ||
    v.includes('range low')
  )
    return 'bearish'
  if (v.includes('expansion')) return 'expansion'
  if (v.includes('watch') || v.includes('range high')) return 'warning'
  return 'neutral'
}

// ---------------------------------------------------------------------------
// BE band visualisation
// ---------------------------------------------------------------------------
function BEBand({
  spot,
  lower,
  upper,
}: {
  spot: number
  lower: number
  upper: number
}) {
  if (!spot || !lower || !upper || upper <= lower) return null
  const span = upper - lower
  const pct = Math.min(100, Math.max(0, ((spot - lower) / span) * 100))

  return (
    <div className="mt-2 space-y-1">
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>BE Low {lower.toFixed(0)}</span>
        <span>BE High {upper.toFixed(0)}</span>
      </div>
      <div className="relative h-3 rounded-full bg-muted overflow-hidden">
        {/* red zone at edges */}
        <div className="absolute inset-y-0 left-0 w-[10%] bg-red-400/40 rounded-l-full" />
        <div className="absolute inset-y-0 right-0 w-[10%] bg-red-400/40 rounded-r-full" />
        {/* green center safe zone */}
        <div className="absolute inset-y-0 left-[10%] right-[10%] bg-green-400/20" />
        {/* spot indicator */}
        <div
          className="absolute top-0 bottom-0 w-1 bg-blue-500 rounded-full"
          style={{ left: `calc(${pct}% - 2px)` }}
        />
      </div>
      <div className="text-center text-xs text-muted-foreground">
        Spot {spot.toFixed(0)}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Delta bar
// ---------------------------------------------------------------------------
function DeltaBar({
  callDelta,
  putDelta,
}: {
  callDelta: number
  putDelta: number
}) {
  const total = Math.abs(callDelta) + Math.abs(putDelta)
  if (total === 0) return null
  const callPct = Math.round((Math.abs(callDelta) / total) * 100)
  const putPct = 100 - callPct

  return (
    <div className="mt-2 space-y-1">
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>CE Delta {callDelta.toFixed(2)}</span>
        <span>PE Delta {putDelta.toFixed(2)}</span>
      </div>
      <div className="flex h-3 rounded-full overflow-hidden">
        <div
          className="bg-green-500 transition-all duration-300"
          style={{ width: `${callPct}%` }}
        />
        <div
          className="bg-red-500 transition-all duration-300"
          style={{ width: `${putPct}%` }}
        />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function BuyerEdge() {
  const { fnoExchanges, defaultFnoExchange, defaultUnderlyings } = useSupportedExchanges()
  const { mode, appMode } = useThemeStore()
  const { apiKey } = useAuthStore()
  const { isMarketOpen } = useMarketStatus()
  const isDarkMode = mode === 'dark'
  const isAnalyzer = appMode === 'analyzer'

  const [selectedExchange, setSelectedExchange] = useState(defaultFnoExchange)
  const [underlyings, setUnderlyings] = useState<string[]>(
    defaultUnderlyings[defaultFnoExchange] || []
  )
  const [underlyingOpen, setUnderlyingOpen] = useState(false)
  const [selectedUnderlying, setSelectedUnderlying] = useState(
    defaultUnderlyings[defaultFnoExchange]?.[0] || ''
  )
  const [expiries, setExpiries] = useState<string[]>([])
  const [selectedExpiry, setSelectedExpiry] = useState('')
  const [data, setData] = useState<BuyerEdgeResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const requestIdRef = useRef(0)

  // ── Straddle Chart state ───────────────────────────────────────
  const [straddleInterval, setStraddleInterval] = useState('1m')
  const [straddleDays, setStraddleDays] = useState('1')
  const [straddleChartData, setStraddleChartData] = useState<StraddleChartData | null>(null)
  const [straddleIntervals, setStraddleIntervals] = useState<string[]>(['1m', '3m', '5m', '10m', '15m', '30m', '1h'])
  const [isChartLoading, setIsChartLoading] = useState(false)
  const [showStraddle, setShowStraddle] = useState(false)    // Straddle line default OFF (PCR lines show by default)
  const [showSpot, setShowSpot] = useState(false)
  const [showSynthetic, setShowSynthetic] = useState(false)
  const [showVwap, setShowVwap] = useState(false)            // always toggled together with showStraddle
  const [straddleSectionOpen, setStraddleSectionOpen] = useState(true)
  const [showStraddleInfo, setShowStraddleInfo] = useState(false)

  // ── Engine lookback settings ───────────────────────────────────
  const [lbBars, setLbBars] = useState('20')
  const [lbTf, setLbTf] = useState('3m')
  const [strikeCount, setStrikeCount] = useState('10')

  // ── Advanced GEX Levels state ──────────────────────────────────
  const [gexData, setGexData] = useState<GexLevelsResponse | null>(null)
  const [isGexLoading, setIsGexLoading] = useState(false)
  const [gexMode, setGexMode] = useState<'selected' | 'cumulative'>('selected')
  const [gexExpiries, setGexExpiries] = useState<string[]>([])
  const [gexSectionOpen, setGexSectionOpen] = useState(true)
  const [gexViewMode, setGexViewMode] = useState<'line' | 'bar'>('line')
  const [gexLineHover, setGexLineHover] = useState<{ strike: number; net_gex: number } | null>(null)

  // ── PCR state (unified with straddle) ──────────────────────────
  const [pcrData, setPcrData] = useState<PcrChartResponse | null>(null)
  const [isPcrLoading, setIsPcrLoading] = useState(false)
  const [pcrStrikeWindow, setPcrStrikeWindow] = useState('10')
  const [maxSnapshotStrikes, setMaxSnapshotStrikes] = useState('40')
  const [showPcrOi, setShowPcrOi] = useState(true)
  const [showPcrVolume, setShowPcrVolume] = useState(true)
  // PCR series are hosted inside the Options Premium Monitor (straddle chart)
  const pcrOiSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrVolSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrDataMapRef = useRef<Map<number, import('../api/buyerEdge').PcrDataPoint>>(new Map())
  const pcrSortedDataRef = useRef<import('../api/buyerEdge').PcrDataPoint[]>([])

  // ── OI Change Chart state ──────────────────────────────────────
  const [oiChgSectionOpen, setOiChgSectionOpen] = useState(true)
  const [showOiChgInfo, setShowOiChgInfo] = useState(false)
  const [oiChgViewMode, setOiChgViewMode] = useState<'line' | 'bar'>('line')
  const [oiChgBarHover, setOiChgBarHover] = useState<StrikeOiChange | null>(null)
  const oiChgChartContainerRef = useRef<HTMLDivElement>(null)
  const oiChgChartRef = useRef<IChartApi | null>(null)
  const oiChgCeSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const oiChgPeSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const oiChgTooltipRef = useRef<HTMLDivElement | null>(null)
  const oiChgDataMapRef = useRef<Map<number, { ce_oi_chg: number; pe_oi_chg: number }>>(new Map())

  // ── IVR Dashboard state ────────────────────────────────────────
  const [ivData, setIvData] = useState<IvDashboardResponse | null>(null)
  const [isIvLoading, _setIsIvLoading] = useState(false)
  const [ivExpiries, setIvExpiries] = useState<string[]>([])
  const [ivSectionOpen, setIvSectionOpen] = useState(true)

  // ── IV Chart (intraday ATM IV series via ivChartApi) ────────────
  const [ivChartData, setIvChartData] = useState<IVChartData | null>(null)
  const [ivChartInterval, setIvChartInterval] = useState('5m')
  const [ivChartDays, setIvChartDays] = useState('1')
  const [ivChartIntervals, setIvChartIntervals] = useState<string[]>(['1m', '3m', '5m', '10m', '15m', '30m', '1h'])
  const [isIvChartLoading, setIsIvChartLoading] = useState(false)
  const [ivChartSectionOpen, setIvChartSectionOpen] = useState(true)

  // ── GEX Spot Chart state ───────────────────────────────────────
  const [spotCandleData, setSpotCandleData] = useState<SpotCandleResponse | null>(null)
  const [spotChartInterval, setSpotChartInterval] = useState('15m')
  const [spotChartDays, setSpotChartDays] = useState('5')
  const [isSpotCandleLoading, setIsSpotCandleLoading] = useState(false)
  const [spotChartSectionOpen, setSpotChartSectionOpen] = useState(true)

  // Refs for the GEX spot chart
  const spotChartContainerRef = useRef<HTMLDivElement>(null)
  const spotChartRef = useRef<IChartApi | null>(null)
  const spotCandleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const spotPriceLinesRef = useRef<import('lightweight-charts').IPriceLine[]>([])

  // Chart DOM refs
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const spotSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const straddleSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const syntheticSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const vwapSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const tooltipRef = useRef<HTMLDivElement | null>(null)
  const watermarkRef = useRef<HTMLDivElement | null>(null)
  const straddleChartDataRef = useRef<StraddleChartData | null>(null)
  const seriesDataMapRef = useRef<Map<number, StraddleDataPoint>>(new Map())
  const vwapDataMapRef = useRef<Map<number, number>>(new Map())

  // Theme colors for the embedded straddle chart
  const chartColors = useMemo(() => {
    if (isAnalyzer) {
      return {
        text: '#d4bfff',
        grid: 'rgba(139, 92, 246, 0.1)',
        border: 'rgba(139, 92, 246, 0.2)',
        crosshair: 'rgba(139, 92, 246, 0.5)',
        crosshairLabel: '#4c1d95',
        spot: '#e2e8f0',
        straddle: '#a78bfa',
        synthetic: '#60a5fa',
        vwap: '#fbbf24',
        watermark: 'rgba(139, 92, 246, 0.12)',
        tooltipBg: 'rgba(30, 15, 60, 0.92)',
        tooltipBorder: 'rgba(139, 92, 246, 0.3)',
        tooltipText: '#d4bfff',
        tooltipMuted: '#a78bfa',
      }
    }
    if (isDarkMode) {
      return {
        text: '#a6adbb',
        grid: 'rgba(166, 173, 187, 0.1)',
        border: 'rgba(166, 173, 187, 0.2)',
        crosshair: 'rgba(166, 173, 187, 0.5)',
        crosshairLabel: '#1f2937',
        spot: '#e2e8f0',
        straddle: '#4ade80',
        synthetic: '#60a5fa',
        vwap: '#f59e0b',
        watermark: 'rgba(166, 173, 187, 0.12)',
        tooltipBg: 'rgba(17, 24, 39, 0.92)',
        tooltipBorder: 'rgba(166, 173, 187, 0.2)',
        tooltipText: '#e2e8f0',
        tooltipMuted: '#9ca3af',
      }
    }
    return {
      text: '#333',
      grid: 'rgba(0, 0, 0, 0.1)',
      border: 'rgba(0, 0, 0, 0.2)',
      crosshair: 'rgba(0, 0, 0, 0.3)',
      crosshairLabel: '#2563eb',
      spot: '#1e293b',
      straddle: '#16a34a',
      synthetic: '#2563eb',
      vwap: '#d97706',
      watermark: 'rgba(0, 0, 0, 0.06)',
      tooltipBg: 'rgba(255, 255, 255, 0.95)',
      tooltipBorder: 'rgba(0, 0, 0, 0.15)',
      tooltipText: '#1e293b',
      tooltipMuted: '#6b7280',
    }
  }, [isDarkMode, isAnalyzer])

  // Keep a stable ref to colors for the crosshair callback
  const chartColorsRef = useRef(chartColors)
  chartColorsRef.current = chartColors

  // Stable ref to toggle states — read inside crosshair callbacks without re-init
  const straddleShowsRef = useRef({ showStraddle, showVwap, showSpot, showSynthetic, showPcrOi, showPcrVolume })
  straddleShowsRef.current = { showStraddle, showVwap, showSpot, showSynthetic, showPcrOi, showPcrVolume }

  // Sync exchange when broker caps load
  useEffect(() => {
    setSelectedExchange((prev) =>
      prev && fnoExchanges.some((ex) => ex.value === prev) ? prev : defaultFnoExchange
    )
  }, [defaultFnoExchange, fnoExchanges])

  // Fetch underlyings when exchange changes
  useEffect(() => {
    const defaults = defaultUnderlyings[selectedExchange] || []
    setUnderlyings(defaults)
    setSelectedUnderlying(defaults[0] || '')
    setExpiries([])
    setSelectedExpiry('')
    setData(null)
    setStraddleChartData(null)
    straddleChartDataRef.current = null

    let cancelled = false
    const fetch = async () => {
      try {
        const response = await buyerEdgeApi.getUnderlyings(selectedExchange)
        if (cancelled) return
        if (response.status === 'success' && response.underlyings.length > 0) {
          setUnderlyings(response.underlyings)
          if (!response.underlyings.includes(defaults[0])) {
            setSelectedUnderlying(response.underlyings[0])
          }
        }
      } catch {
        // keep defaults
      }
    }
    fetch()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExchange])

  // Fetch expiries when underlying changes
  useEffect(() => {
    if (!selectedUnderlying) return
    setExpiries([])
    setSelectedExpiry('')
    setData(null)
    setStraddleChartData(null)
    straddleChartDataRef.current = null

    let cancelled = false
    const fetch = async () => {
      try {
        const response = await buyerEdgeApi.getExpiries(selectedExchange, selectedUnderlying)
        if (cancelled) return
        if (response.status === 'success' && response.expiries.length > 0) {
          setExpiries(response.expiries)
          setSelectedExpiry(response.expiries[0])
        }
      } catch {
        if (cancelled) return
        setExpiries([])
        setSelectedExpiry('')
      }
    }
    fetch()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedUnderlying])

  // Fetch buyer edge data
  const fetchData = useCallback(async () => {
    if (!selectedExpiry) return
    const requestId = ++requestIdRef.current
    setIsLoading(true)
    try {
      const expiryForAPI = convertExpiryForAPI(selectedExpiry)

      // Run state engine + IVR in parallel (GEX is loaded separately via loadGexData)
      const [response, ivResp] = await Promise.allSettled([
        buyerEdgeApi.getData({
          underlying: selectedUnderlying,
          exchange: selectedExchange,
          expiry_date: expiryForAPI,
          strike_count: parseInt(strikeCount),
          lb_bars: parseInt(lbBars),
          lb_tf: lbTf,
        }),
        buyerEdgeApi.getIvDashboard({
          underlying: selectedUnderlying,
          exchange: selectedExchange,
          expiry_dates: ivExpiries.length > 0
            ? ivExpiries.map(convertExpiryForAPI)
            : [expiryForAPI],
          strike_count: 15,
        }),
      ])

      if (requestIdRef.current !== requestId) return

      // State engine
      if (response.status === 'fulfilled') {
        if (response.value.status === 'success') {
          setData(response.value)
        } else {
          showToast.error(response.value.message || 'Failed to fetch data')
        }
      } else {
        showToast.error('Failed to fetch buyer edge data')
      }

      // IV Dashboard
      if (ivResp.status === 'fulfilled' && ivResp.value.status === 'success') {
        setIvData(ivResp.value)
      }

    } catch {
      if (requestIdRef.current !== requestId) return
      showToast.error('Failed to fetch buyer edge data')
    } finally {
      if (requestIdRef.current === requestId) setIsLoading(false)
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange, strikeCount, lbBars, lbTf, ivExpiries])

  useEffect(() => {
    if (selectedExpiry) fetchData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExpiry])

  // ── Dedicated GEX loader — runs independently on mode/expiry/expiries changes ─────
  const loadGexData = useCallback(async () => {
    if (!selectedExpiry) return
    setIsGexLoading(true)
    try {
      const expiryForAPI = convertExpiryForAPI(selectedExpiry)
      const res = await buyerEdgeApi.getGexLevels({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        mode: gexMode,
        expiry_date: gexMode === 'selected' ? expiryForAPI : undefined,
        expiry_dates: gexMode === 'cumulative'
          ? (gexExpiries.length > 0 ? gexExpiries.map(convertExpiryForAPI) : [expiryForAPI])
          : undefined,
        strike_count: 25,
      })
      if (res.status === 'success') {
        setGexData(res)
      }
    } catch {
      // silent — GEX is supplementary; avoid noisy toasts
    } finally {
      setIsGexLoading(false)
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange, gexMode, gexExpiries])

  useEffect(() => {
    if (selectedExpiry) loadGexData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadGexData])

  // ── useOptionChainLive: real-time option chain data ─────────────
  // When Auto ON + market open → WebSocket delivers live LTP/OI updates.
  // When Auto OFF or market closed → hook stays disabled; manual Analyse
  // button or Refresh button triggers a one-shot REST fetch instead.
  const expiryForLive = selectedExpiry ? convertExpiryForAPI(selectedExpiry) : ''
  const marketOpen = isMarketOpen(selectedExchange)
  const liveEnabled = autoRefresh && !!expiryForLive && marketOpen

  const {
    data: liveChainData,
    isStreaming,
    isConnected: isLiveConnected,
    isPaused: isLivePaused,
    lastUpdate: liveLastUpdate,
    streamingSymbols,
    refetch: refetchLive,
  } = useOptionChainLive(
    apiKey,
    selectedUnderlying,
    selectedExchange,
    selectedExchange,
    expiryForLive,
    parseInt(strikeCount),
    { enabled: liveEnabled, oiRefreshInterval: AUTO_REFRESH_INTERVAL, pauseWhenHidden: true }
  )

  // When Auto ON and market closed, fall back to a simple setInterval for
  // the backend engine calls (same cadence as the live OI polling interval).
  const autoRefreshFallbackRef = useRef<ReturnType<typeof setInterval> | null>(null)
  useEffect(() => {
    if (autoRefresh && selectedExpiry && !marketOpen) {
      autoRefreshFallbackRef.current = setInterval(fetchData, AUTO_REFRESH_INTERVAL)
    }
    return () => {
      if (autoRefreshFallbackRef.current) {
        clearInterval(autoRefreshFallbackRef.current)
        autoRefreshFallbackRef.current = null
      }
    }
  }, [autoRefresh, fetchData, selectedExpiry, marketOpen])

  // When live chain OI polling fires (liveLastUpdate changes) re-run the
  // backend engines so signal/Greeks stay in sync with the fresh OI data.
  const prevLiveLastUpdateRef = useRef<Date | null>(null)
  useEffect(() => {
    if (!liveEnabled || !liveLastUpdate) return
    if (prevLiveLastUpdateRef.current === liveLastUpdate) return
    prevLiveLastUpdateRef.current = liveLastUpdate
    fetchData()
  }, [liveEnabled, liveLastUpdate, fetchData])

  // ── IV Chart: intraday ATM IV series via ivChartApi ─────────────
  const loadIvChartData = useCallback(async () => {
    if (!selectedExpiry || !selectedUnderlying) return
    setIsIvChartLoading(true)
    try {
      const res = await ivChartApi.getIVData({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: convertExpiryForAPI(selectedExpiry),
        interval: ivChartInterval,
        days: parseInt(ivChartDays),
      })
      if (res.status === 'success' && res.data) {
        setIvChartData(res.data)
      } else {
        setIvChartData(null)
      }
    } catch {
      setIvChartData(null)
    } finally {
      setIsIvChartLoading(false)
    }
  }, [selectedExpiry, selectedUnderlying, selectedExchange, ivChartInterval, ivChartDays])

  // Auto-load IV chart when its deps change
  useEffect(() => {
    if (selectedExpiry) loadIvChartData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadIvChartData])

  // Fetch available IV chart intervals once
  useEffect(() => {
    ivChartApi.getIntervals().then((res) => {
      if (res.status === 'success' && res.data) {
        const all = [...(res.data.seconds || []), ...(res.data.minutes || []), ...(res.data.hours || [])]
        if (all.length > 0) setIvChartIntervals(all)
      }
    }).catch(() => {/* keep defaults */ })
  }, [])

  // ── GEX Spot Chart: data loader ────────────────────────────────
  const loadSpotCandleData = useCallback(async () => {
    if (!selectedUnderlying) return
    setIsSpotCandleLoading(true)
    try {
      const res = await buyerEdgeApi.getSpotCandles({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        interval: spotChartInterval,
        days: parseInt(spotChartDays),
      })
      if (res.status === 'success') {
        setSpotCandleData(res)
      } else {
        setSpotCandleData(null)
      }
    } catch {
      setSpotCandleData(null)
    } finally {
      setIsSpotCandleLoading(false)
    }
  }, [selectedUnderlying, selectedExchange, spotChartInterval, spotChartDays])

  // Auto-load spot candles when deps change (including after expiry is set)
  useEffect(() => {
    if (selectedUnderlying) loadSpotCandleData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadSpotCandleData])

  // ── GEX Spot Chart: lightweight-charts init/destroy ────────────
  const initSpotChart = useCallback(() => {
    const container = spotChartContainerRef.current
    if (!container) return

    if (spotChartRef.current) {
      spotChartRef.current.remove()
      spotChartRef.current = null
    }
    container.innerHTML = ''

    const chart = createChart(container, {
      width: container.offsetWidth,
      height: 380,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: chartColors.text,
      },
      grid: {
        vertLines: { color: chartColors.grid, style: 1 as const, visible: true },
        horzLines: { color: chartColors.grid, style: 1 as const, visible: true },
      },
      rightPriceScale: {
        visible: true,
        borderColor: chartColors.border,
        scaleMargins: { top: 0.08, bottom: 0.08 },
      },
      timeScale: {
        borderColor: chartColors.border,
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time: number) => {
          const d = new Date(time * 1000)
          const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
          const hh = ist.getUTCHours().toString().padStart(2, '0')
          const mm = ist.getUTCMinutes().toString().padStart(2, '0')
          if (parseInt(spotChartDays) > 1) {
            const dd = ist.getUTCDate().toString().padStart(2, '0')
            const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
            return `${dd}/${mo} ${hh}:${mm}`
          }
          return `${hh}:${mm}`
        },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { width: 1 as const, color: chartColors.crosshair, style: 2 as const, labelVisible: true },
        horzLine: { width: 1 as const, color: chartColors.crosshair, style: 2 as const, labelBackgroundColor: chartColors.crosshairLabel },
      },
    })

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      borderVisible: false,
      priceScaleId: 'right',
    })

    spotChartRef.current = chart
    spotCandleSeriesRef.current = candleSeries

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: container.offsetWidth })
    })
    ro.observe(container)

    return () => {
      ro.disconnect()
      chart.remove()
      spotChartRef.current = null
      spotCandleSeriesRef.current = null
    }
    // chartColors deliberately omitted — re-init is driven by the caller
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spotChartDays])

  // Re-init spot chart when section becomes visible or interval/days change
  useEffect(() => {
    if (!spotChartSectionOpen) return
    const cleanup = initSpotChart()
    return () => { cleanup?.() }
  }, [spotChartSectionOpen, initSpotChart])

  // Push candle data + GEX price lines into the chart whenever either changes
  useEffect(() => {
    const series = spotCandleSeriesRef.current
    const chart = spotChartRef.current
    if (!series || !chart) return

    const candles = spotCandleData?.candles ?? []
    series.setData(
      candles.map((c) => ({
        time: c.time as import('lightweight-charts').UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
    )
    if (candles.length > 0) chart.timeScale().fitContent()

    // ── Overlay GEX key levels as horizontal price lines ─────────
    // Remove previously created price lines before adding fresh ones so that
    // repeated effect runs (e.g. on auto-refresh) do not accumulate duplicates.
    // series is already validated non-null above; suppress only stale-line errors
    // that occur when the chart was re-initialised between renders.
    for (const pl of spotPriceLinesRef.current) {
      try { series.removePriceLine(pl) } catch { /* price line already removed on chart re-init */ }
    }
    spotPriceLinesRef.current = []

    if (!gexData?.levels) return

    const gexLevels = gexData.levels
    const newPriceLines: import('lightweight-charts').IPriceLine[] = []

    const keyLevels: Array<{ price: number | null; color: string; title: string; lineStyle?: number }> = [
      { price: gexLevels.call_gamma_wall, color: '#22c55e', title: 'Call Wall', lineStyle: 0 },
      { price: gexLevels.put_gamma_wall, color: '#ef4444', title: 'Put Wall', lineStyle: 0 },
      { price: gexLevels.gamma_flip, color: '#f59e0b', title: 'HVL / Γ Flip', lineStyle: 2 },
      { price: gexLevels.absolute_wall, color: '#a78bfa', title: 'Abs Wall', lineStyle: 1 },
    ]

    for (const lvl of keyLevels) {
      if (lvl.price == null || lvl.price === 0) continue
      newPriceLines.push(series.createPriceLine({
        price: lvl.price,
        color: lvl.color,
        lineWidth: 2,
        lineStyle: (lvl.lineStyle ?? 0) as 0 | 1 | 2 | 3 | 4,
        axisLabelVisible: true,
        title: lvl.title,
      }))
    }

    // Per-expiry walls when in cumulative mode (weekly vs monthly overlay)
    if (gexData.per_expiry && gexData.per_expiry.length > 1) {
      const perExpiryColors = ['#6ee7b7', '#fca5a5', '#93c5fd', '#fde68a', '#d8b4fe']
      gexData.per_expiry.forEach((exp, idx) => {
        if (!exp.levels) return
        const c = perExpiryColors[idx % perExpiryColors.length]
        const label = exp.expiry_date.slice(-5)
        if (exp.levels.call_gamma_wall != null && exp.levels.call_gamma_wall !== 0) {
          newPriceLines.push(series.createPriceLine({ price: exp.levels.call_gamma_wall, color: c, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: `${label} CW` }))
        }
        if (exp.levels.put_gamma_wall != null && exp.levels.put_gamma_wall !== 0) {
          newPriceLines.push(series.createPriceLine({ price: exp.levels.put_gamma_wall, color: c, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: `${label} PW` }))
        }
      })
    }

    spotPriceLinesRef.current = newPriceLines
  }, [spotCandleData, gexData])

  // ── Straddle Chart: init & destroy ───────────────────────────

  const applyDataToChart = useCallback((chartData: StraddleChartData) => {
    if (!chartData.series || chartData.series.length === 0) return
    const sorted = [...chartData.series].sort((a, b) => a.time - b.time)
    const map = new Map<number, StraddleDataPoint>()
    for (const p of sorted) map.set(p.time, p)
    seriesDataMapRef.current = map

    // Compute anchored VWAP and store for tooltip
    const vwapPoints = computeDayAnchoredVWAP(sorted)
    const vwapMap = new Map<number, number>()
    for (const v of vwapPoints) vwapMap.set(v.time, v.value)
    vwapDataMapRef.current = vwapMap

    spotSeriesRef.current?.setData(
      sorted.map((p) => ({
        time: p.time as import('lightweight-charts').UTCTimestamp,
        value: p.spot,
      }))
    )
    straddleSeriesRef.current?.setData(
      sorted.map((p) => ({
        time: p.time as import('lightweight-charts').UTCTimestamp,
        value: p.straddle,
      }))
    )
    syntheticSeriesRef.current?.setData(
      sorted.map((p) => ({
        time: p.time as import('lightweight-charts').UTCTimestamp,
        value: p.synthetic_future,
      }))
    )
    vwapSeriesRef.current?.setData(
      vwapPoints.map((v) => ({
        time: v.time as import('lightweight-charts').UTCTimestamp,
        value: v.value,
      }))
    )
    chartRef.current?.timeScale().fitContent()
  }, [])

  const initChart = useCallback(() => {
    if (!chartContainerRef.current) return

    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }

    const container = chartContainerRef.current
    const tooltip = tooltipRef.current
    container.innerHTML = ''
    if (tooltip) container.appendChild(tooltip)

    const chart = createChart(container, {
      width: container.offsetWidth,
      height: STRADDLE_CHART_HEIGHT,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: chartColors.text,
      },
      grid: {
        vertLines: { color: chartColors.grid, style: 1 as const, visible: true },
        horzLines: { color: chartColors.grid, style: 1 as const, visible: true },
      },
      leftPriceScale: {
        visible: true,
        borderColor: chartColors.border,
        scaleMargins: { top: 0.05, bottom: 0.05 },
      },
      rightPriceScale: {
        visible: true,
        borderColor: chartColors.border,
        scaleMargins: { top: 0.05, bottom: 0.05 },
      },
      timeScale: {
        borderColor: chartColors.border,
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time: number) => {
          const d = new Date(time * 1000)
          const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
          const hh = ist.getUTCHours().toString().padStart(2, '0')
          const mm = ist.getUTCMinutes().toString().padStart(2, '0')
          if (parseInt(straddleDays) > 1) {
            const dd = ist.getUTCDate().toString().padStart(2, '0')
            const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
            return `${dd}/${mo} ${hh}:${mm}`
          }
          return `${hh}:${mm}`
        },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { width: 1 as const, color: chartColors.crosshair, style: 2 as const, labelVisible: false },
        horzLine: { width: 1 as const, color: chartColors.crosshair, style: 2 as const, labelBackgroundColor: chartColors.crosshairLabel },
      },
    })

    // Watermark
    const watermark = document.createElement('div')
    watermark.style.cssText = `position:absolute;z-index:2;font-family:Arial,sans-serif;font-size:48px;font-weight:bold;user-select:none;pointer-events:none;color:${chartColors.watermark}`
    watermark.textContent = 'OpenAlgo'
    container.appendChild(watermark)
    watermarkRef.current = watermark
    setTimeout(() => {
      watermark.style.left = `${container.offsetWidth / 2 - watermark.offsetWidth / 2}px`
      watermark.style.top = `${container.offsetHeight / 2 - watermark.offsetHeight / 2}px`
    }, 0)

    // Tooltip element
    if (!tooltipRef.current) {
      const tt = document.createElement('div')
      tt.style.cssText =
        'position:absolute;z-index:10;pointer-events:none;display:none;border-radius:6px;padding:8px 12px;font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;line-height:1.6;white-space:nowrap;'
      container.appendChild(tt)
      tooltipRef.current = tt
    } else {
      container.appendChild(tooltipRef.current)
    }

    // Series
    const spotSeries = chart.addSeries(LineSeries, {
      color: chartColors.spot,
      lineWidth: 2,
      priceScaleId: 'left',
      title: 'Spot',
      lastValueVisible: true,
      priceLineVisible: true,
      visible: showSpot,
    })
    const straddleSeries = chart.addSeries(LineSeries, {
      color: chartColors.straddle,
      lineWidth: 2,
      priceScaleId: 'right',
      title: 'Straddle',
      lastValueVisible: true,
      priceLineVisible: false,
      visible: showStraddle,
    })
    const syntheticSeries = chart.addSeries(LineSeries, {
      color: chartColors.synthetic,
      lineWidth: 1,
      lineStyle: 2,
      priceScaleId: 'left',
      title: 'Synthetic Fut',
      lastValueVisible: true,
      priceLineVisible: true,
      visible: showSynthetic,
    })
    const vwapSeries = chart.addSeries(LineSeries, {
      color: chartColors.vwap,
      lineWidth: 1,
      lineStyle: 3,
      priceScaleId: 'right',
      title: 'VWAP(D)',
      lastValueVisible: true,
      priceLineVisible: false,
      visible: showVwap,
    })
    // PCR(OI) and PCR(Vol) — overlay scale so they don't clash with straddle/spot scales
    const pcrOiSeries = chart.addSeries(LineSeries, {
      color: '#f59e0b',
      lineWidth: 2,
      priceScaleId: 'right',
      title: 'PCR(OI)',
      lastValueVisible: true,
      priceLineVisible: false,
      visible: showPcrOi,
    })
    const pcrVolSeries = chart.addSeries(LineSeries, {
      color: '#60a5fa',
      lineWidth: 2,
      priceScaleId: 'right',
      title: 'PCR(Vol)',
      lastValueVisible: true,
      priceLineVisible: false,
      visible: showPcrVolume,
    })

    chartRef.current = chart
    spotSeriesRef.current = spotSeries
    straddleSeriesRef.current = straddleSeries
    syntheticSeriesRef.current = syntheticSeries
    vwapSeriesRef.current = vwapSeries
    pcrOiSeriesRef.current = pcrOiSeries
    pcrVolSeriesRef.current = pcrVolSeries

    // Re-apply existing PCR data so chart reinit doesn't lose already-loaded data
    const existingPcr = pcrSortedDataRef.current
    if (existingPcr.length > 0) {
      pcrOiSeries.setData(
        existingPcr.filter((p) => p.pcr_oi != null).map((p) => ({
          time: p.time as import('lightweight-charts').UTCTimestamp,
          value: p.pcr_oi as number,
        }))
      )
      pcrVolSeries.setData(
        existingPcr.filter((p) => p.pcr_volume != null).map((p) => ({
          time: p.time as import('lightweight-charts').UTCTimestamp,
          value: p.pcr_volume as number,
        }))
      )
    }

    // Crosshair tooltip
    chart.subscribeCrosshairMove((param) => {
      const tt = tooltipRef.current
      if (!tt || !container) return
      if (
        !param.time ||
        !param.point ||
        param.point.x < 0 ||
        param.point.y < 0 ||
        param.point.x > container.offsetWidth ||
        param.point.y > container.offsetHeight
      ) {
        tt.style.display = 'none'
        return
      }
      const time = param.time as number
      const point = seriesDataMapRef.current.get(time)
      if (!point) {
        tt.style.display = 'none'
        return
      }

      const vwapVal = vwapDataMapRef.current.get(time)
      const pcrPoint = pcrDataMapRef.current.get(time)
      const cl = chartColorsRef.current
      const shows = straddleShowsRef.current
      const { date, time: timeStr } = formatIST(time)
      tt.style.display = 'block'
      tt.style.background = cl.tooltipBg
      tt.style.border = `1px solid ${cl.tooltipBorder}`
      tt.style.color = cl.tooltipText
      tt.innerHTML = `
        ${shows.showStraddle ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.straddle};font-weight:600">Straddle Price</span>
          <span style="color:${cl.straddle};font-weight:600">${point.straddle.toFixed(2)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.tooltipMuted}">&bull; ${point.atm_strike} Call:</span>
          <span>${point.ce_price.toFixed(2)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.tooltipMuted}">&bull; ${point.atm_strike} Put:</span>
          <span>${point.pe_price.toFixed(2)}</span>
        </div>` : ''}
        ${shows.showVwap && vwapVal != null ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.vwap}">VWAP (Day)</span>
          <span style="color:${cl.vwap}">${vwapVal.toFixed(2)}</span>
        </div>` : ''}
        ${shows.showSynthetic ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.synthetic}">Synthetic Fut</span>
          <span style="color:${cl.synthetic}">${point.synthetic_future.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        </div>` : ''}
        ${shows.showSpot ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.tooltipMuted}">Spot Price</span>
          <span>${point.spot.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        </div>` : ''}
        ${shows.showPcrOi && pcrPoint?.pcr_oi != null ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:#f59e0b">PCR(OI)</span>
          <span style="color:#f59e0b">${pcrPoint.pcr_oi.toFixed(4)}</span>
        </div>` : ''}
        ${shows.showPcrVolume && pcrPoint?.pcr_volume != null ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:#60a5fa">PCR(Vol)</span>
          <span style="color:#60a5fa">${pcrPoint.pcr_volume.toFixed(4)}</span>
        </div>` : ''}
        <div style="display:flex;justify-content:space-between;gap:16px;margin-top:4px;border-top:1px solid ${cl.tooltipBorder};padding-top:4px">
          <span style="color:${cl.tooltipMuted}">${date}</span>
          <span style="color:${cl.tooltipMuted}">${timeStr}</span>
        </div>
      `

      const tooltipW = tt.offsetWidth
      const tooltipH = tt.offsetHeight
      const x = param.point.x
      const y = param.point.y
      const margin = 16
      let left = x + margin
      if (left + tooltipW > container.offsetWidth) left = x - tooltipW - margin
      let top = y - tooltipH / 2
      if (top < 0) top = 0
      if (top + tooltipH > container.offsetHeight) top = container.offsetHeight - tooltipH
      tt.style.left = `${left}px`
      tt.style.top = `${top}px`
    })

    // Re-apply data if available
    if (straddleChartDataRef.current) applyDataToChart(straddleChartDataRef.current)

    // Resize handler
    const handleResize = () => {
      if (chartRef.current && container) {
        chartRef.current.applyOptions({ width: container.offsetWidth })
        if (watermarkRef.current) {
          watermarkRef.current.style.left = `${container.offsetWidth / 2 - watermarkRef.current.offsetWidth / 2}px`
          watermarkRef.current.style.top = `${container.offsetHeight / 2 - watermarkRef.current.offsetHeight / 2}px`
        }
      }
    }
    window.addEventListener('resize', handleResize)
    return () => { window.removeEventListener('resize', handleResize) }
    // `applyDataToChart` is intentionally omitted from deps: it is defined with
    // useCallback([], []) so it never changes — omitting it matches the standalone
    // StraddleChart.tsx pattern and avoids a spurious chart re-init.
  }, [chartColors, straddleDays, showStraddle, showSpot, showSynthetic, showVwap])

  // Chart lifecycle — the chart card is always rendered so chartContainerRef is
  // always valid.  Simple [initChart] dep mirrors the standalone StraddleChart page.
  useEffect(() => {
    const cleanup = initChart()
    return () => {
      cleanup?.()
      if (chartRef.current) { chartRef.current.remove(); chartRef.current = null }
    }
  }, [initChart])

  // Series visibility toggles
  useEffect(() => { spotSeriesRef.current?.applyOptions({ visible: showSpot }) }, [showSpot])
  useEffect(() => { straddleSeriesRef.current?.applyOptions({ visible: showStraddle }) }, [showStraddle])
  useEffect(() => { syntheticSeriesRef.current?.applyOptions({ visible: showSynthetic }) }, [showSynthetic])
  useEffect(() => { vwapSeriesRef.current?.applyOptions({ visible: showVwap }) }, [showVwap])

  // Fetch available intervals once
  useEffect(() => {
    straddleChartApi.getIntervals().then((res) => {
      if (res.status === 'success' && res.data) {
        const all = [...(res.data.seconds || []), ...(res.data.minutes || []), ...(res.data.hours || [])]
        if (all.length > 0) setStraddleIntervals(all)
      }
    }).catch(() => {/* keep defaults */ })
  }, [])

  // Unified load for straddle + PCR data
  const refreshMonitorData = useCallback(async () => {
    if (!selectedExpiry) return
    setIsChartLoading(true)
    setIsPcrLoading(true)
    try {
      const res = await buyerEdgeApi.getUnifiedMonitor({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: convertExpiryForAPI(selectedExpiry),
        interval: straddleInterval,
        days: parseInt(straddleDays),
        pcr_strike_window: parseInt(pcrStrikeWindow),
        max_snapshot_strikes: parseInt(maxSnapshotStrikes),
      })
      if (res.status === 'success') {
        // Handle Straddle data
        if (res.straddle?.status === 'success' && res.straddle.data) {
          straddleChartDataRef.current = res.straddle.data
          setStraddleChartData(res.straddle.data)
          applyDataToChart(res.straddle.data)
        } else if (res.straddle?.status === 'error') {
          straddleChartDataRef.current = null
          setStraddleChartData(null)
          seriesDataMapRef.current = new Map()
          vwapDataMapRef.current = new Map()
          spotSeriesRef.current?.setData([])
          straddleSeriesRef.current?.setData([])
          syntheticSeriesRef.current?.setData([])
          vwapSeriesRef.current?.setData([])
          showToast.error(res.straddle.message || 'Failed to load straddle data')
        }

        // Handle PCR data
        if (res.pcr?.status === 'success') {
          setPcrData(res.pcr)
        } else if (res.pcr?.status === 'error') {
          showToast.error(res.pcr.message || 'Failed to load PCR data')
        }
      } else {
        showToast.error(res.message || 'Failed to load monitor data')
      }
    } catch {
      showToast.error('Failed to fetch monitor data')
    } finally {
      setIsChartLoading(false)
      setIsPcrLoading(false)
    }
  }, [selectedExpiry, selectedUnderlying, selectedExchange, straddleInterval, straddleDays, pcrStrikeWindow, maxSnapshotStrikes, applyDataToChart])

  // Auto-load monitor chart when its deps change
  useEffect(() => {
    if (selectedExpiry) refreshMonitorData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshMonitorData])

  // ── PCR data → push to Options Premium Monitor straddle chart ────
  // When PCR data loads/refreshes, populate the PCR series that live
  // inside the straddle (Options Premium Monitor) chart.
  useEffect(() => {
    const sorted = [...(pcrData?.data?.series ?? [])].sort((a, b) => a.time - b.time)
    pcrSortedDataRef.current = sorted
    const dataMap = new Map<number, import('../api/buyerEdge').PcrDataPoint>()
    for (const p of sorted) dataMap.set(p.time, p)
    pcrDataMapRef.current = dataMap
    pcrOiSeriesRef.current?.setData(
      sorted.filter((p) => p.pcr_oi != null).map((p) => ({
        time: p.time as import('lightweight-charts').UTCTimestamp,
        value: p.pcr_oi as number,
      }))
    )
    pcrVolSeriesRef.current?.setData(
      sorted.filter((p) => p.pcr_volume != null).map((p) => ({
        time: p.time as import('lightweight-charts').UTCTimestamp,
        value: p.pcr_volume as number,
      }))
    )
  }, [pcrData])

  useEffect(() => { pcrOiSeriesRef.current?.applyOptions({ visible: showPcrOi }) }, [showPcrOi])
  useEffect(() => { pcrVolSeriesRef.current?.applyOptions({ visible: showPcrVolume }) }, [showPcrVolume])

  // ── OI Change line chart ───────────────────────────────────────
  useEffect(() => {
    if (oiChgViewMode !== 'line') return
    const series = pcrData?.data?.series ?? []
    if (!series.length || !oiChgChartContainerRef.current) return

    const container = oiChgChartContainerRef.current
    const cl = chartColorsRef.current

    if (!oiChgChartRef.current) {
      const c = createChart(container, {
        layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: cl.text },
        grid: { vertLines: { color: cl.grid }, horzLines: { color: cl.grid } },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { width: 1 as const, color: chartColors.crosshair, style: 2 as const, labelVisible: false },
          horzLine: { width: 1 as const, color: chartColors.crosshair, style: 2 as const, labelBackgroundColor: chartColors.crosshairLabel },
        },
        rightPriceScale: { borderColor: cl.border },
        timeScale: {
          borderColor: cl.border, timeVisible: true, secondsVisible: false,
          tickMarkFormatter: (t: number) => {
            const d = new Date(t * 1000)
            const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
            const hh = ist.getUTCHours().toString().padStart(2, '0')
            const mm = ist.getUTCMinutes().toString().padStart(2, '0')
            if (parseInt(straddleDays) > 1) {
              const dd = ist.getUTCDate().toString().padStart(2, '0')
              const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
              return `${dd}/${mo} ${hh}:${mm}`
            }
            return `${hh}:${mm}`
          },
        },
        width: container.offsetWidth || container.clientWidth,
        height: OI_CHANGE_CHART_HEIGHT,
      })

      oiChgCeSeriesRef.current = c.addSeries(LineSeries, {
        color: '#22c55e', lineWidth: 2, title: 'Call OI Chg',
        priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false,
      })
      oiChgPeSeriesRef.current = c.addSeries(LineSeries, {
        color: '#ef4444', lineWidth: 2, title: 'Put OI Chg',
        priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false,
      })

      // Crosshair tooltip
      if (!oiChgTooltipRef.current) {
        const tt = document.createElement('div')
        tt.style.cssText = [
          'position:absolute', 'display:none', 'z-index:100', 'pointer-events:none',
          'padding:8px 10px', 'border-radius:6px', 'font-size:12px', 'line-height:1.6',
          'min-width:180px', 'white-space:nowrap',
        ].join(';')
        oiChgTooltipRef.current = tt
        container.appendChild(tt)
      }

      c.subscribeCrosshairMove((param) => {
        const tt = oiChgTooltipRef.current
        if (!tt || !container) return
        if (!param.time || !param.point ||
          param.point.x < 0 || param.point.y < 0 ||
          param.point.x > container.offsetWidth || param.point.y > container.offsetHeight) {
          tt.style.display = 'none'; return
        }
        const pt = oiChgDataMapRef.current.get(param.time as number)
        if (!pt) { tt.style.display = 'none'; return }
        const { date, time: timeStr } = formatIST(param.time as number)
        tt.style.display = 'block'
        tt.style.background = cl.tooltipBg
        tt.style.border = `1px solid ${cl.tooltipBorder}`
        tt.style.color = cl.tooltipText
        tt.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:#22c55e">Call OI Chg</span>
            <span style="color:#22c55e">${formatOiLakh(pt.ce_oi_chg)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:#ef4444">Put OI Chg</span>
            <span style="color:#ef4444">${formatOiLakh(pt.pe_oi_chg)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px;margin-top:4px;border-top:1px solid ${cl.tooltipBorder};padding-top:4px">
            <span style="color:${cl.tooltipMuted}">${date}</span>
            <span style="color:${cl.tooltipMuted}">${timeStr}</span>
          </div>
        `
        const tw = tt.offsetWidth; const th = tt.offsetHeight
        const margin = 16; let left = param.point.x + margin
        if (left + tw > container.offsetWidth) left = param.point.x - tw - margin
        let top = param.point.y - th / 2
        if (top < 0) top = 0
        if (top + th > container.offsetHeight) top = container.offsetHeight - th
        tt.style.left = `${left}px`; tt.style.top = `${top}px`
      })

      oiChgChartRef.current = c
    }

    // Build OI change series anchored to first point
    const sorted = [...series].sort((a, b) => a.time - b.time)
    const baseCe = sorted[0]?.ce_oi ?? 0
    const basePe = sorted[0]?.pe_oi ?? 0
    const dataMap = new Map<number, { ce_oi_chg: number; pe_oi_chg: number }>()
    const ceData: { time: import('lightweight-charts').UTCTimestamp; value: number }[] = []
    const peData: { time: import('lightweight-charts').UTCTimestamp; value: number }[] = []
    for (const p of sorted) {
      const ceChg = (p.ce_oi ?? 0) - baseCe
      const peChg = (p.pe_oi ?? 0) - basePe
      dataMap.set(p.time, { ce_oi_chg: ceChg, pe_oi_chg: peChg })
      ceData.push({ time: p.time as import('lightweight-charts').UTCTimestamp, value: ceChg })
      peData.push({ time: p.time as import('lightweight-charts').UTCTimestamp, value: peChg })
    }
    oiChgDataMapRef.current = dataMap
    oiChgCeSeriesRef.current?.setData(ceData)
    oiChgPeSeriesRef.current?.setData(peData)
    oiChgChartRef.current?.applyOptions({
      timeScale: {
        tickMarkFormatter: (t: number) => {
          const d = new Date(t * 1000)
          const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
          const hh = ist.getUTCHours().toString().padStart(2, '0')
          const mm = ist.getUTCMinutes().toString().padStart(2, '0')
          if (parseInt(straddleDays) > 1) {
            const dd = ist.getUTCDate().toString().padStart(2, '0')
            const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
            return `${dd}/${mo} ${hh}:${mm}`
          }
          return `${hh}:${mm}`
        },
      },
    })
    if (sorted.length > 0) oiChgChartRef.current?.timeScale().fitContent()
  }, [pcrData, oiChgViewMode, straddleDays, chartColors])

  // OI Change chart resize
  useEffect(() => {
    if (!oiChgChartContainerRef.current) return
    const ro = new ResizeObserver(() => {
      if (oiChgChartContainerRef.current && oiChgChartRef.current) {
        oiChgChartRef.current.applyOptions({ width: oiChgChartContainerRef.current.offsetWidth || oiChgChartContainerRef.current.clientWidth })
      }
    })
    ro.observe(oiChgChartContainerRef.current)
    return () => ro.disconnect()
  }, [])

  // Destroy OI Change chart on unmount
  useEffect(() => {
    return () => {
      oiChgChartRef.current?.remove()
      oiChgChartRef.current = null
      oiChgCeSeriesRef.current = null
      oiChgPeSeriesRef.current = null
    }
  }, [])

  // Latest straddle data point and its VWAP for info bar
  const latestStraddlePoint = useMemo(() => {
    if (!straddleChartData?.series?.length) return null
    return straddleChartData.series[straddleChartData.series.length - 1]
  }, [straddleChartData])

  const latestVwap = useMemo(() => {
    if (!latestStraddlePoint) return null
    return vwapDataMapRef.current.get(latestStraddlePoint.time) ?? null
  }, [latestStraddlePoint])

  // Live spot price: prefer WebSocket-delivered underlying LTP, fall back to engine result
  const liveSpot = liveChainData?.underlying_ltp ?? data?.spot ?? null

  const signal = data?.signal_engine?.signal ?? null
  const signalCfg = signal ? SIGNAL_CONFIG[signal] : null

  // ── Directional Conviction Score ────────────────────────────────────────
  const directionalScore = useMemo<DirectionalScoreResult | null>(() => {
    // Require at least the PCR series to have 2+ points for meaningful signals
    const series = pcrSortedDataRef.current.length >= 2 ? pcrSortedDataRef.current : null
    if (!series) return null
    return computeDirectionalScore({
      pcrSeries: series,
      pcrOi: pcrData?.data?.current_pcr_oi ?? null,
      pcrOiChg: pcrData?.data?.current_pcr_oi_chg ?? null,
      deltaImbalance: data?.greeks_engine?.delta_imbalance ?? null,
      trend: data?.market_state?.trend ?? null,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pcrData, data])

  return (
    <div className="py-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold">Buyer Edge</h1>
        <p className="text-muted-foreground mt-1">
          State engine: Are sellers still in control, or are they being forced to reprice?
        </p>
      </div>

      {/* Controls */}
      <Card>
        <CardContent className="pt-4">
          <div className="flex flex-wrap gap-3 items-end">
            {/* Exchange */}
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Exchange</label>
              <Select value={selectedExchange} onValueChange={setSelectedExchange}>
                <SelectTrigger className="w-36 h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {fnoExchanges.map((ex) => (
                    <SelectItem key={ex.value} value={ex.value}>
                      {ex.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Underlying */}
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Underlying</label>
              <Popover open={underlyingOpen} onOpenChange={setUnderlyingOpen}>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    role="combobox"
                    aria-expanded={underlyingOpen}
                    className="w-44 h-9 justify-between font-normal"
                  >
                    {selectedUnderlying || 'Select…'}
                    <ChevronsUpDown className="ml-2 h-4 w-4 opacity-50 shrink-0" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-44 p-0">
                  <Command>
                    <CommandInput placeholder="Search…" />
                    <CommandList>
                      <CommandEmpty>No results.</CommandEmpty>
                      <CommandGroup>
                        {underlyings.map((u) => (
                          <CommandItem
                            key={u}
                            value={u}
                            onSelect={() => {
                              setSelectedUnderlying(u)
                              setUnderlyingOpen(false)
                            }}
                          >
                            <Check
                              className={`mr-2 h-4 w-4 ${selectedUnderlying === u ? 'opacity-100' : 'opacity-0'}`}
                            />
                            {u}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
            </div>

            {/* Expiry */}
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Expiry</label>
              <Select value={selectedExpiry} onValueChange={setSelectedExpiry}>
                <SelectTrigger className="w-36 h-9">
                  <SelectValue placeholder="Select expiry" />
                </SelectTrigger>
                <SelectContent>
                  {expiries.map((e) => (
                    <SelectItem key={e} value={e}>
                      {e}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Engine: Timeframe */}
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Timeframe</label>
              <Select value={lbTf} onValueChange={setLbTf}>
                <SelectTrigger className="w-24 h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['1m', '3m', '5m', '10m', '15m', '30m', '1h'].map((tf) => (
                    <SelectItem key={tf} value={tf}>{tf}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Engine: Lookback bars */}
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Bars</label>
              <Select value={lbBars} onValueChange={setLbBars}>
                <SelectTrigger className="w-24 h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['10', '20', '30', '50', '75', '100'].map((b) => (
                    <SelectItem key={b} value={b}>{b} bars</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Engine: Strike range (CE/PE offset for Call & Put Wall) */}
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Strikes</label>
              <Select value={strikeCount} onValueChange={setStrikeCount}>
                <SelectTrigger className="w-24 h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['2', '3', '5', '10', '15', '20', '25', '30'].map((s) => (
                    <SelectItem key={s} value={s}>±{s}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Fetch button */}
            <Button
              onClick={() => { fetchData(); loadGexData(); if (liveEnabled) refetchLive() }}
              disabled={isLoading || !selectedExpiry}
              className="h-9"
            >
              {isLoading ? (
                <RefreshCw className="h-4 w-4 animate-spin mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              Analyse
            </Button>

            {/* Auto-refresh toggle */}
            <Button
              variant={autoRefresh ? 'default' : 'outline'}
              onClick={() => setAutoRefresh((v) => !v)}
              className="h-9"
            >
              Auto {autoRefresh ? 'ON' : 'OFF'}
            </Button>

            {/* Market + streaming status */}
            <div className="flex items-center gap-2 ml-auto">
              <Badge variant={marketOpen ? 'default' : 'secondary'} className="text-xs">
                {marketOpen ? '● Market Open' : '○ Market Closed'}
              </Badge>
              {autoRefresh && (
                <div className="flex items-center gap-1.5">
                  {isStreaming ? (
                    <Wifi className="h-4 w-4 text-green-500" />
                  ) : (
                    <WifiOff className="h-4 w-4 text-muted-foreground" />
                  )}
                  <Badge variant={isStreaming ? 'default' : isLiveConnected ? 'secondary' : 'outline'} className="text-xs">
                    {isLivePaused ? 'Paused' : isStreaming ? `Live · ${streamingSymbols}` : marketOpen ? 'Polling' : 'Offline'}
                  </Badge>
                  {liveLastUpdate && (
                    <span className="text-xs text-muted-foreground hidden sm:inline">
                      {liveLastUpdate.toLocaleTimeString()}
                    </span>
                  )}
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Signal Banner */}
      {signalCfg && data?.signal_engine && (
        <Card className={`border-2 ${signalCfg.border} ${signalCfg.bg}`}>
          <CardContent className="pt-5">
            <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
              <div className="flex items-center gap-3">
                <span
                  className={`text-4xl font-black tracking-wide ${signalCfg.text}`}
                >
                  {signalCfg.label}
                </span>
                <Badge className={signalCfg.badgeClass}>
                  {data.signal_engine.confidence}/4
                </Badge>
                {data.signal_engine.data_mode && data.signal_engine.data_mode !== 'realtime' && (
                  <Badge
                    variant="outline"
                    className={
                      data.signal_engine.data_mode === 'last_day_fallback'
                        ? 'border-amber-500 text-amber-600 dark:text-amber-400 text-xs'
                        : data.signal_engine.data_mode === 'spot_only'
                          ? 'border-red-500 text-red-600 dark:text-red-400 text-xs'
                          : 'border-blue-500 text-blue-600 dark:text-blue-400 text-xs'
                    }
                    title={
                      data.signal_engine.data_mode === 'last_day_fallback'
                        ? 'Market State computed from last session data (market closed or broker unavailable)'
                        : data.signal_engine.data_mode === 'spot_only'
                          ? 'No history available — structure analysis degraded (spot-only fallback)'
                          : 'Using live snapshot data'
                    }
                  >
                    {data.signal_engine.data_mode === 'last_day_fallback'
                      ? '⚠ Last Session'
                      : data.signal_engine.data_mode === 'spot_only'
                        ? '⚠ Spot Only'
                        : '◉ Snapshot'}
                  </Badge>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                {data.signal_engine.reasons.map((r, i) => (
                  <span
                    key={i}
                    className="text-xs px-2 py-0.5 rounded-full bg-background/60 border text-muted-foreground"
                  >
                    {r}
                  </span>
                ))}
              </div>
            </div>
            <div className="mt-2 flex flex-wrap gap-4 text-sm text-muted-foreground">
              <span>
                <span className="font-medium">{data.underlying}</span> · Spot{' '}
                <span className="font-medium">{liveSpot != null ? liveSpot.toFixed(2) : (data.spot?.toFixed(2) ?? 'N/A')}</span>
                {liveChainData?.underlying_ltp && isStreaming && (
                  <span className="ml-1 text-xs text-green-500">● live</span>
                )}
              </span>
              {data.timestamp && <span>{data.timestamp}</span>}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Directional Conviction Score */}
      {directionalScore && (
        <DirectionalScoreGauge result={directionalScore} />
      )}

      {/* Module cards */}
      {data?.market_state && data?.oi_intelligence && data?.greeks_engine && data?.straddle_engine && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {/* Module 1 — Market State */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Module 1 — Market State</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-6 justify-around py-2">
                <Pill
                  label="Trend"
                  value={data.market_state.trend}
                  variant={pillVariant(data.market_state.trend)}
                />
                <Pill
                  label="Regime"
                  value={data.market_state.regime}
                  variant={pillVariant(data.market_state.regime)}
                />
                <Pill
                  label="Location"
                  value={data.market_state.location}
                  variant={pillVariant(data.market_state.location)}
                />
              </div>
              <p className="text-xs text-muted-foreground mt-3 text-center">
                {data.market_state.location === 'Breakout'
                  ? '⚡ Structure break — primary signal active'
                  : data.market_state.regime === 'Compression'
                    ? '🔒 Sellers in control — compression phase'
                    : data.market_state.regime === 'Neutral'
                      ? '⚖ Transition zone — ambiguous regime, watch for direction'
                      : '👁 Expansion detected — watch for transitions'}
              </p>
            </CardContent>
          </Card>

          {/* Module 2 — OI Intelligence */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Module 2 — OI Intelligence</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">Call Wall (Supply Zone)</p>
                  <p className="font-bold text-red-500" aria-label={`Call wall strike ${data.oi_intelligence.call_wall}`}>
                    ↑ {data.oi_intelligence.call_wall || '—'}
                  </p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">Put Wall (Demand Zone)</p>
                  <p className="font-bold text-green-500" aria-label={`Put wall strike ${data.oi_intelligence.put_wall}`}>
                    ↓ {data.oi_intelligence.put_wall || '—'}
                  </p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">OI Migration</p>
                  <Badge
                    variant={data.oi_intelligence.oi_migrating ? 'default' : 'secondary'}
                  >
                    {data.oi_intelligence.oi_migrating
                      ? data.oi_intelligence.migration_direction
                      : 'Stable'}
                  </Badge>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">OI Roll</p>
                  <Badge
                    variant={data.oi_intelligence.oi_roll_detected ? 'default' : 'secondary'}
                  >
                    {data.oi_intelligence.oi_roll_detected ? '⚡ Detected' : 'None'}
                  </Badge>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Module 3 — Greeks Engine */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Module 3 — Greeks Engine</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-6 justify-around py-1">
                <Pill
                  label="Delta Velocity"
                  value={data.greeks_engine.delta_velocity}
                  variant={pillVariant(data.greeks_engine.delta_velocity)}
                />
                <Pill
                  label="Gamma Regime"
                  value={data.greeks_engine.gamma_regime}
                  variant={pillVariant(data.greeks_engine.gamma_regime)}
                />
                <Pill
                  label="Delta Imbalance"
                  value={data.greeks_engine.delta_imbalance.toFixed(3)}
                  variant={
                    data.greeks_engine.delta_imbalance > 0.1
                      ? 'bullish'
                      : data.greeks_engine.delta_imbalance < -0.1
                        ? 'bearish'
                        : 'neutral'
                  }
                />
              </div>
              <DeltaBar
                callDelta={data.greeks_engine.total_call_delta}
                putDelta={data.greeks_engine.total_put_delta}
              />
              <p className="text-xs text-muted-foreground mt-2 text-center">
                {data.greeks_engine.gamma_regime === 'Expansion'
                  ? '📈 Negative gamma — dealers amplify moves'
                  : '↔ Positive gamma — mean-reversion likely'}
              </p>
            </CardContent>
          </Card>

          {/* Module 4 — Straddle Engine */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Module 4 — Straddle Engine</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">ATM Strike</p>
                  <p className="font-bold">{data.straddle_engine.atm_strike}</p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">Straddle Price</p>
                  <p className="font-bold">{data.straddle_engine.straddle_price.toFixed(2)}</p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">Premium</p>
                  <Badge
                    variant={
                      data.straddle_engine.straddle_velocity === 'Expanding'
                        ? 'default'
                        : 'secondary'
                    }
                  >
                    {data.straddle_engine.straddle_velocity}
                  </Badge>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">BE Distance</p>
                  <p
                    className={`font-bold ${data.straddle_engine.be_distance_pct < 0.5 ? 'text-yellow-600 dark:text-yellow-400' : ''}`}
                  >
                    {data.straddle_engine.be_distance_pct.toFixed(2)}%
                  </p>
                </div>
              </div>
              <BEBand
                spot={data.spot ?? 0}
                lower={data.straddle_engine.lower_be}
                upper={data.straddle_engine.upper_be}
              />
            </CardContent>
          </Card>
        </div>
      )}

      {/* Empty state */}
      {!data && !isLoading && (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground">
            <p className="text-lg">Select an underlying and expiry to run the state engine</p>
            <p className="text-sm mt-2">
              The engine checks: Structure → OI Walls → Delta Pressure → Straddle Stress
            </p>
          </CardContent>
        </Card>
      )}

      {/* Loading state */}
      {isLoading && !data && (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground">
            <RefreshCw className="h-8 w-8 animate-spin mx-auto mb-3" />
            <p>Computing buyer edge signal…</p>
          </CardContent>
        </Card>
      )}

      {/* Options Premium Monitor — always rendered so chartContainerRef is always mounted */}
      <Card>
        <CardHeader className="pb-3 cursor-pointer" onClick={() => setStraddleSectionOpen((v) => !v)}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">Options Premium Monitor</CardTitle>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setShowStraddleInfo((v) => !v) }}
                className="text-muted-foreground hover:text-foreground transition-colors"
                title="How to interpret"
              >
                <Info className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2" onClick={(e) => e.stopPropagation()}>
              {/* Interval selector */}
              <Select value={straddleInterval} onValueChange={setStraddleInterval}>
                <SelectTrigger className="w-[90px] h-8 text-xs">
                  <SelectValue placeholder="Interval" />
                </SelectTrigger>
                <SelectContent>
                  {straddleIntervals.map((intv) => (
                    <SelectItem key={intv} value={intv}>
                      {intv}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Days selector */}
              <Select value={straddleDays} onValueChange={setStraddleDays}>
                <SelectTrigger className="w-[90px] h-8 text-xs">
                  <SelectValue placeholder="Days" />
                </SelectTrigger>
                <SelectContent>
                  {['1', '3', '5'].map((d) => (
                    <SelectItem key={d} value={d}>
                      {d} {d === '1' ? 'Day' : 'Days'}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* PCR Window (dynamic) */}
              <Select value={pcrStrikeWindow} onValueChange={setPcrStrikeWindow}>
                <SelectTrigger className="w-[100px] h-8 text-xs">
                  <SelectValue placeholder="PCR Window" />
                </SelectTrigger>
                <SelectContent>
                  {['1', '3', '5', '10', '15', '20'].map((w) => (
                    <SelectItem key={w} value={w}>±{w} PCR Window</SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* PCR Range (max snapshots) */}
              <Select value={maxSnapshotStrikes} onValueChange={setMaxSnapshotStrikes}>
                <SelectTrigger className="w-[110px] h-8 text-xs">
                  <SelectValue placeholder="PCR Range" />
                </SelectTrigger>
                <SelectContent>
                  {['20', '30', '40', '50', '100', '150'].map((r) => (
                    <SelectItem key={r} value={r}>{r} PCR Strikes</SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Refresh chart */}
              <Button
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                onClick={refreshMonitorData}
                disabled={isChartLoading || isPcrLoading}
              >
                {(isChartLoading || isPcrLoading) ? (
                  <RefreshCw className="h-3 w-3 animate-spin mr-1" />
                ) : (
                  <RefreshCw className="h-3 w-3 mr-1" />
                )}
                Refresh
              </Button>
              {straddleSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </div>
          </div>

          {/* Interpretation guide — toggles on info icon click */}
          {showStraddleInfo && (
            <div className="mt-3 p-3 rounded-lg bg-muted/50 border border-border/50 text-xs space-y-2" onClick={(e) => e.stopPropagation()}>
              <p className="font-semibold uppercase tracking-wide text-muted-foreground text-[10px]">How to Interpret — Options Premium Monitor</p>
              <p className="text-muted-foreground leading-relaxed">
                The <strong className="text-foreground">Straddle Price</strong> (ATM CE + ATM PE) measures total premium at the closest strike to spot.
                A <strong className="text-foreground">rising straddle</strong> indicates premium <em>expansion</em> — market is paying up for hedges and expects a large move.
                A <strong className="text-foreground">falling straddle</strong> indicates premium <em>compression</em> — sellers dominate and range-bound conditions are expected.
              </p>
              <p className="text-muted-foreground leading-relaxed">
                <strong className="text-foreground">VWAP(D)</strong> is the intraday volume-weighted average of the straddle price.
                Straddle trading <em>above</em> VWAP → buyers expanding premium; trading <em>below</em> VWAP → sellers compressing it.
              </p>
              <div className="grid grid-cols-2 gap-2 mt-1">
                <div className="p-2 rounded bg-green-500/10 border border-green-500/20">
                  <p className="font-medium text-green-600 dark:text-green-400">PCR &gt; 1.2 — Bullish</p>
                  <p className="text-muted-foreground mt-0.5">More open puts than calls. Put writers (sellers) are creating a floor — market expects support.</p>
                </div>
                <div className="p-2 rounded bg-red-500/10 border border-red-500/20">
                  <p className="font-medium text-red-600 dark:text-red-400">PCR &lt; 0.8 — Bearish</p>
                  <p className="text-muted-foreground mt-0.5">More open calls than puts. Call writers (sellers) are creating a ceiling — market expects resistance.</p>
                </div>
                <div className="p-2 rounded bg-blue-500/10 border border-blue-500/20">
                  <p className="font-medium text-blue-500">Straddle ↑ + PCR ↑ — Buy premium</p>
                  <p className="text-muted-foreground mt-0.5">Premium expanding while puts build up → directional move expected (long straddle setup).</p>
                </div>
                <div className="p-2 rounded bg-amber-500/10 border border-amber-500/20">
                  <p className="font-medium text-amber-500">Straddle ↓ + PCR ≈ 1.0 — Sell premium</p>
                  <p className="text-muted-foreground mt-0.5">Premium compressing in balanced conditions → range-bound day (short straddle / iron condor setup).</p>
                </div>
              </div>
            </div>
          )}
        </CardHeader>
        <CardContent className="pt-0" style={{ display: straddleSectionOpen ? undefined : 'none' }}>
          {/* Latest info bar */}
          {(latestStraddlePoint || pcrData?.data) && (
            <div className="flex flex-wrap items-center gap-x-6 gap-y-1 mb-4 text-sm">
              {latestStraddlePoint && (
                <>
                  <div>
                    <span className="text-muted-foreground">Straddle </span>
                    <span className="font-semibold" style={{ color: chartColors.straddle }}>
                      {latestStraddlePoint.straddle.toFixed(2)}
                    </span>
                  </div>
                  {latestVwap != null && (
                    <div>
                      <span className="text-muted-foreground">VWAP(D) </span>
                      <span className="font-semibold" style={{ color: chartColors.vwap }}>
                        {latestVwap.toFixed(2)}
                      </span>
                    </div>
                  )}
                  <div>
                    <span className="text-muted-foreground">Spot </span>
                    <span className="font-medium">{latestStraddlePoint.spot.toFixed(2)}</span>
                  </div>
                </>
              )}
              {pcrData?.data && (
                <>
                  <div>
                    <span className="text-muted-foreground">PCR(OI) </span>
                    <span className="font-semibold text-amber-500">
                      {pcrData.data.current_pcr_oi?.toFixed(2) ?? 'N/A'}
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">PCR(Vol) </span>
                    <span className="font-semibold text-blue-500">
                      {pcrData.data.current_pcr_volume?.toFixed(2) ?? 'N/A'}
                    </span>
                  </div>
                </>
              )}
              {latestStraddlePoint && (
                <>
                  <div>
                    <span className="text-muted-foreground">Strike </span>
                    <span className="font-medium">{latestStraddlePoint.atm_strike}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">{latestStraddlePoint.atm_strike} CE </span>
                    <span className="font-medium">{latestStraddlePoint.ce_price.toFixed(2)}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">{latestStraddlePoint.atm_strike} PE </span>
                    <span className="font-medium">{latestStraddlePoint.pe_price.toFixed(2)}</span>
                  </div>
                </>
              )}
            </div>
          )}

          {/* Chart container */}
          <div className="relative">
            <div
              ref={chartContainerRef}
              className="relative w-full rounded-lg border border-border/50"
              style={{ height: STRADDLE_CHART_HEIGHT }}
            />
            {(isChartLoading || isPcrLoading) && (
              <div className="absolute inset-0 flex items-center justify-center bg-background/60 rounded-lg">
                <div className="flex flex-col items-center gap-2 text-sm text-muted-foreground">
                  <div className="h-5 w-5 animate-spin rounded-full border-2 border-current border-t-transparent" />
                  <div className="flex flex-col items-center">
                    {(showStraddle || showSpot || showSynthetic) && isChartLoading && <span>Loading Straddle & Spot data…</span>}
                    {(showPcrOi || showPcrVolume) && isPcrLoading && <span>Loading PCR data…</span>}
                    {(!showStraddle && !showSpot && !showSynthetic && !showPcrOi && !showPcrVolume) && <span>Loading Monitor data…</span>}
                  </div>
                </div>
              </div>
            )}
            {!selectedExpiry && !isChartLoading && (
              <div className="absolute inset-0 flex items-center justify-center bg-background/70 rounded-lg">
                <p className="text-sm text-muted-foreground">Select an underlying and expiry to load the straddle chart</p>
              </div>
            )}
          </div>

          {/* Legend toggles — Straddle controls VWAP(D) together; PCR lines on by default */}
          <div className="flex items-center justify-center gap-4 mt-3 flex-wrap">
            <button
              type="button"
              onClick={() => { const next = !showStraddle; setShowStraddle(next); setShowVwap(next) }}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showStraddle ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
            >
              <span className="inline-block h-0.5 w-5 rounded" style={{ backgroundColor: chartColors.straddle }} />
              Straddle + VWAP(D)
            </button>
            <button
              type="button"
              onClick={() => setShowPcrOi((v) => !v)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showPcrOi ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
            >
              <span className="inline-block h-0.5 w-5 rounded bg-amber-400" />
              PCR(OI)
            </button>
            <button
              type="button"
              onClick={() => setShowPcrVolume((v) => !v)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showPcrVolume ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
            >
              <span className="inline-block h-0.5 w-5 rounded bg-blue-400" />
              PCR(Vol)
            </button>
            <button
              type="button"
              onClick={() => setShowSpot((v) => !v)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showSpot ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
            >
              <span className="inline-block h-0.5 w-5 rounded" style={{ backgroundColor: chartColors.spot }} />
              Spot
            </button>
            <button
              type="button"
              onClick={() => setShowSynthetic((v) => !v)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showSynthetic ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
            >
              <span className="inline-block h-0.5 w-5 rounded border-dashed border-t-2" style={{ borderColor: chartColors.synthetic, backgroundColor: 'transparent' }} />
              Synthetic Fut
            </button>
          </div>
        </CardContent>
      </Card>

      {/* ================================================================
          Section B.6 — Intraday OI Change & Market Breadth
          ================================================================ */}
      {pcrData?.data && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <span className="inline-block w-1 h-5 rounded-full bg-amber-400 mr-1" />
                <CardTitle
                  className="text-base cursor-pointer"
                  onClick={() => setOiChgSectionOpen((v) => !v)}
                >
                  Intraday OI Change &amp; Market Breadth
                </CardTitle>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); setShowOiChgInfo((v) => !v) }}
                  className="text-muted-foreground hover:text-foreground transition-colors"
                  title="How to interpret"
                >
                  <Info className="h-3.5 w-3.5" />
                </button>
                {pcrData.data.current_pcr_oi_chg != null && (
                  <Badge
                    variant="outline"
                    className="text-xs font-bold"
                    style={{
                      color: (pcrData.data.current_pcr_oi_chg ?? 0) >= 0 ? '#22c55e' : '#ef4444',
                      borderColor: (pcrData.data.current_pcr_oi_chg ?? 0) >= 0 ? '#22c55e' : '#ef4444',
                    }}
                  >
                    PCR (OI CHG):&nbsp;
                    {pcrData.data.current_pcr_oi_chg >= 0 ? '+' : ''}
                    {pcrData.data.current_pcr_oi_chg.toFixed(2)}
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-2">
                {/* Line / Bar toggle */}
                <div className="flex rounded-md overflow-hidden border border-border text-xs">
                  <button
                    type="button"
                    onClick={() => setOiChgViewMode('line')}
                    className={`px-3 py-1 transition-colors ${oiChgViewMode === 'line' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                  >
                    Line
                  </button>
                  <button
                    type="button"
                    onClick={() => setOiChgViewMode('bar')}
                    className={`px-3 py-1 transition-colors border-l border-border ${oiChgViewMode === 'bar' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                  >
                    Bar
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => setOiChgSectionOpen((v) => !v)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  {oiChgSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                </button>
              </div>
            </div>

            {/* Interpretation guide for OI Change & Breadth — Premium + OI matrix (elite classification) */}
            {showOiChgInfo && (
              <div className="mt-3 p-3 rounded-lg bg-muted/50 border border-border/50 text-xs space-y-2.5">
                <p className="font-semibold uppercase tracking-wide text-muted-foreground text-[10px]">Premium + OI Matrix — Correct Flow Classification</p>
                {/* CE matrix */}
                <p className="text-[10px] font-semibold text-foreground/70">CE Leg (Calls)</p>
                <div className="grid grid-cols-3 gap-px bg-border/50 rounded overflow-hidden text-[10px]">
                  <div className="bg-muted/80 px-2 py-1 font-semibold text-muted-foreground">ATM CE Prem</div>
                  <div className="bg-muted/80 px-2 py-1 font-semibold text-muted-foreground">CE OI</div>
                  <div className="bg-muted/80 px-2 py-1 font-semibold text-muted-foreground">Classification</div>
                  <div className="bg-background/60 px-2 py-1 text-green-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-green-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-green-400 font-medium">Call Buying (Bullish demand)</div>
                  <div className="bg-background/60 px-2 py-1 text-red-500">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-red-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-red-400 font-medium">Call Writing (Bearish ceiling)</div>
                  <div className="bg-background/60 px-2 py-1 text-green-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-lime-400 font-medium">CE Short Covering (Mild Bullish)</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400 font-medium">CE Long Unwinding (Bearish)</div>
                </div>
                {/* PE matrix */}
                <p className="text-[10px] font-semibold text-foreground/70 pt-1">PE Leg (Puts)</p>
                <div className="grid grid-cols-3 gap-px bg-border/50 rounded overflow-hidden text-[10px]">
                  <div className="bg-muted/80 px-2 py-1 font-semibold text-muted-foreground">ATM PE Prem</div>
                  <div className="bg-muted/80 px-2 py-1 font-semibold text-muted-foreground">PE OI</div>
                  <div className="bg-muted/80 px-2 py-1 font-semibold text-muted-foreground">Classification</div>
                  <div className="bg-background/60 px-2 py-1 text-red-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-red-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-red-400 font-medium">Put Buying (Bearish hedge)</div>
                  <div className="bg-background/60 px-2 py-1 text-green-500">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-green-500">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-green-400 font-medium">Put Writing (Bullish support floor)</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400">↑ Rising</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-orange-400 font-medium">PE Short Covering (Mildly Bearish)</div>
                  <div className="bg-background/60 px-2 py-1 text-lime-400">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-lime-400">↓ Falling</div>
                  <div className="bg-background/60 px-2 py-1 text-lime-400 font-medium">PE Long Unwinding (Bullish)</div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
                  <div className="space-y-1.5">
                    <p className="text-muted-foreground leading-relaxed">
                      <strong className="text-foreground">Premium direction is more accurate than spot:</strong> Spot↑ but CE Prem↓ (IV crush) → writers dominate. Always cross-check ATM CE/PE premium direction with OI for the correct classification.
                    </p>
                    <p className="text-muted-foreground leading-relaxed">
                      <strong className="text-foreground">PCR(OI CHG) &gt; 1 ≠ auto-Bullish:</strong> Put OI↑ from Put Writing = bullish floor. Put OI↑ from Put Buying = bearish hedge. Check PE Premium direction — PE Prem↓ + PE OI↑ = Put Writing (bullish).
                    </p>
                  </div>
                  <div className="space-y-1.5">
                    <p className="text-muted-foreground leading-relaxed">
                      <strong className="text-foreground">Breadth Bias = PE Advancers / CE Advancers:</strong> Splits advancing strikes by leg. &gt;1.2 → more PE strikes adding OI (bearish pressure). &lt;0.83 → more CE strikes adding OI (bullish pressure). Neutral near 1.0.
                    </p>
                    <p className="text-muted-foreground leading-relaxed">
                      <strong className="text-foreground">OI Velocity (5-bar avg):</strong> Contracts added per bar over the last 5 candles. Accelerating CE OI↑ → call building pace increasing. Positive for directional conviction, not just cumulative snapshot.
                    </p>
                  </div>
                </div>
                <p className="text-[10px] text-muted-foreground border-t border-border/50 pt-1.5">
                  <strong className="text-foreground">Dual OI Expansion</strong> (CE↑ + PE↑): Both legs adding OI — ambiguous: could be straddle writing (IV compression) or event-driven buying. Add ATM premium direction to disambiguate. &nbsp;
                  <strong className="text-foreground">OI Convergence</strong> (CE↓ + PE↓): Positions unwinding on both sides — short covering, expiry liquidation, or volatility collapse.
                </p>
              </div>
            )}
          </CardHeader>
          <CardContent className="pt-0" style={{ display: oiChgSectionOpen ? undefined : 'none' }}>
            {(() => {
              const lastPt = [...(pcrData.data.series ?? [])].reverse().find(
                (p) => p.advances !== undefined
              )
              const adv = lastPt?.advances ?? 0
              const dec = lastPt?.declines ?? 0
              const neu = lastPt?.neutral ?? 0
              const total = adv + dec + neu || 1
              const adr = pcrData.data.current_adr
              const adrMeta = adr != null ? getAdrMeta(adr) : null

              // ── Smart Flow Classification (Premium + OI matrix) ─────────────
              const seriesSorted = [...(pcrData.data.series ?? [])].sort((a, b) => a.time - b.time)
              const firstSeriesPt = seriesSorted[0]
              const lastSeriesPt = seriesSorted[seriesSorted.length - 1]
              // Require at least 2 data points to compute a meaningful delta
              const hasEnoughData = seriesSorted.length >= 2
              const spotNow = pcrData.data.underlying_ltp
              const rawPriceChg = hasEnoughData && firstSeriesPt?.spot
                ? ((spotNow - firstSeriesPt.spot) / firstSeriesPt.spot) * 100
                : null
              const ceDelta = hasEnoughData
                ? (lastSeriesPt?.ce_oi ?? 0) - (firstSeriesPt?.ce_oi ?? 0)
                : 0
              const peDelta = hasEnoughData
                ? (lastSeriesPt?.pe_oi ?? 0) - (firstSeriesPt?.pe_oi ?? 0)
                : 0
              // ATM premium deltas (session open → now): primary basis for flow classification
              const hasPremiumData = hasEnoughData
                && (firstSeriesPt?.atm_ce_ltp ?? 0) > 0
                && (firstSeriesPt?.atm_pe_ltp ?? 0) > 0
              const cePremiumDelta = hasPremiumData
                ? (lastSeriesPt?.atm_ce_ltp ?? 0) - (firstSeriesPt?.atm_ce_ltp ?? 0)
                : null
              const pePremiumDelta = hasPremiumData
                ? (lastSeriesPt?.atm_pe_ltp ?? 0) - (firstSeriesPt?.atm_pe_ltp ?? 0)
                : null
              const ceFlowMeta = getOiFlowMeta('CE', ceDelta, cePremiumDelta)
              const peFlowMeta = getOiFlowMeta('PE', peDelta, pePremiumDelta)
              const oiRegimeMeta = getOiRegimeMeta(ceDelta, peDelta)
              // 5-bar OI velocity (contracts/bar) for acceleration context
              const last6 = seriesSorted.slice(-6)
              const vel5BarCe = last6.length >= 2
                ? ((last6[last6.length - 1]?.ce_oi ?? 0) - (last6[0]?.ce_oi ?? 0)) / (last6.length - 1)
                : null
              const vel5BarPe = last6.length >= 2
                ? ((last6[last6.length - 1]?.pe_oi ?? 0) - (last6[0]?.pe_oi ?? 0)) / (last6.length - 1)
                : null
              // Directional Breadth Bias from per-leg strike advancers
              const lastBreadthPt = [...seriesSorted].reverse().find(
                (p) => (p.ce_advances ?? 0) + (p.pe_advances ?? 0) > 0
              )
              const breadthBiasMeta = lastBreadthPt
                ? getBreadthBias(lastBreadthPt.ce_advances ?? 0, lastBreadthPt.pe_advances ?? 0)
                : null

              return (
                <div className="mb-4 space-y-2">
                  {/* Smart Flow Classification */}
                  {(ceDelta !== 0 || peDelta !== 0) && (
                    <div className="flex flex-wrap items-center gap-2 pb-2 border-b border-border/40 text-xs">
                      {/* Spot direction (context) */}
                      {rawPriceChg != null && (
                        <span
                          className="font-semibold"
                          style={{ color: rawPriceChg > 0.05 ? '#22c55e' : rawPriceChg < -0.05 ? '#ef4444' : '#fbbf24' }}
                        >
                          {rawPriceChg > 0.05 ? '↑' : rawPriceChg < -0.05 ? '↓' : '→'} Spot{' '}
                          {rawPriceChg >= 0 ? '+' : ''}{rawPriceChg.toFixed(2)}%
                        </span>
                      )}
                      {/* ATM CE premium direction */}
                      {hasPremiumData && cePremiumDelta !== null && Math.abs(cePremiumDelta) >= 0.5 && (
                        <span className="text-[10px] text-muted-foreground">
                          ATM CE:{' '}
                          <span style={{ color: cePremiumDelta > 0 ? '#22c55e' : '#ef4444' }}>
                            {cePremiumDelta > 0 ? '↑' : '↓'} ₹{Math.abs(cePremiumDelta).toFixed(1)}
                          </span>
                        </span>
                      )}
                      {/* ATM PE premium direction */}
                      {hasPremiumData && pePremiumDelta !== null && Math.abs(pePremiumDelta) >= 0.5 && (
                        <span className="text-[10px] text-muted-foreground">
                          ATM PE:{' '}
                          <span style={{ color: pePremiumDelta > 0 ? '#ef4444' : '#22c55e' }}>
                            {pePremiumDelta > 0 ? '↑' : '↓'} ₹{Math.abs(pePremiumDelta).toFixed(1)}
                          </span>
                        </span>
                      )}
                      {/* CE Flow (premium-classified) */}
                      {ceFlowMeta && (
                        <span
                          className="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                          style={{ color: ceFlowMeta.color, borderColor: ceFlowMeta.color + '60' }}
                          title={ceFlowMeta.note}
                        >
                          CE: {ceFlowMeta.label}
                        </span>
                      )}
                      {/* PE Flow (premium-classified) */}
                      {peFlowMeta && (
                        <span
                          className="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                          style={{ color: peFlowMeta.color, borderColor: peFlowMeta.color + '60' }}
                          title={peFlowMeta.note}
                        >
                          PE: {peFlowMeta.label}
                        </span>
                      )}
                      {/* OI Regime */}
                      <span
                        className="px-1.5 py-0.5 rounded text-[10px] font-medium border ml-auto"
                        style={{ color: oiRegimeMeta.color, borderColor: oiRegimeMeta.color + '60' }}
                      >
                        {oiRegimeMeta.label}
                      </span>
                    </div>
                  )}
                  {/* 5-bar OI velocity row */}
                  {(vel5BarCe !== null || vel5BarPe !== null) && (vel5BarCe !== 0 || vel5BarPe !== 0) && (
                    <div className="flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground pb-1.5 border-b border-border/30">
                      <span className="font-medium">OI Velocity (5‑bar avg):</span>
                      {vel5BarCe !== null && vel5BarCe !== 0 && (
                        <span style={{ color: vel5BarCe > 0 ? '#ef4444' : '#22c55e' }}>
                          CE {vel5BarCe > 0 ? '+' : ''}{Math.round(vel5BarCe).toLocaleString('en-IN')}/bar
                        </span>
                      )}
                      {vel5BarPe !== null && vel5BarPe !== 0 && (
                        <span style={{ color: vel5BarPe > 0 ? '#22c55e' : '#ef4444' }}>
                          PE {vel5BarPe > 0 ? '+' : ''}{Math.round(vel5BarPe).toLocaleString('en-IN')}/bar
                        </span>
                      )}
                    </div>
                  )}

                  {/* Advancing / Declining / Neutral bars */}
                  <div className="flex items-center gap-3">
                    <span className="h-2.5 w-2.5 rounded-full bg-emerald-500 shrink-0" />
                    <span className="text-sm text-muted-foreground w-20 shrink-0">Advancing</span>
                    <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                      <div
                        className="h-full rounded-full bg-emerald-500 transition-all duration-500"
                        style={{ width: `${(adv / total) * 100}%` }}
                      />
                    </div>
                    <span className="text-sm font-semibold text-emerald-500 w-8 text-right">{adv}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="h-2.5 w-2.5 rounded-full bg-red-500 shrink-0" />
                    <span className="text-sm text-muted-foreground w-20 shrink-0">Declining</span>
                    <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                      <div
                        className="h-full rounded-full bg-red-500 transition-all duration-500"
                        style={{ width: `${(dec / total) * 100}%` }}
                      />
                    </div>
                    <span className="text-sm font-semibold text-red-500 w-8 text-right">{dec}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="h-2.5 w-2.5 rounded-full bg-muted-foreground shrink-0" />
                    <span className="text-sm text-muted-foreground w-20 shrink-0">Neutral</span>
                    <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                      <div
                        className="h-full rounded-full bg-muted-foreground transition-all duration-500"
                        style={{ width: `${(neu / total) * 100}%` }}
                      />
                    </div>
                    <span className="text-sm font-semibold text-muted-foreground w-8 text-right">{neu}</span>
                  </div>
                  {/* ADR + Directional Breadth Bias row */}
                  <div className="pt-1 text-xs text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span>
                      ADR:{' '}
                      <strong style={{ color: adrMeta?.color ?? 'currentColor' }}>
                        {adr != null ? adr.toFixed(2) : '—'}
                      </strong>
                      {adrMeta && (
                        <span className="ml-1.5" style={{ color: adrMeta.color }}>
                          • {adrMeta.label}
                        </span>
                      )}
                    </span>
                    {breadthBiasMeta && (
                      <span
                        className="px-1.5 py-0.5 rounded text-[10px] font-medium border"
                        style={{ color: breadthBiasMeta.color, borderColor: breadthBiasMeta.color + '50' }}
                        title={`Directional Breadth Bias = PE Advancers / CE Advancers = ${breadthBiasMeta.bias >= 99 ? 'n/a' : breadthBiasMeta.bias.toFixed(2)}`}
                      >
                        {breadthBiasMeta.label}
                      </span>
                    )}
                    <span className="text-[10px] opacity-60">(combined strike OI — deep OTM noise may apply)</span>
                  </div>
                </div>
              )
            })()}
            {/* ── LINE MODE ─────────────────────────────────── */}
            <div
              ref={oiChgChartContainerRef}
              className="relative w-full"
              style={{ display: oiChgViewMode === 'line' ? 'block' : 'none', height: OI_CHANGE_CHART_HEIGHT }}
            />
            {/* ── BAR MODE ─────────────────────────────────── */}
            {oiChgViewMode === 'bar' && (() => {
              const strikes = pcrData.data.strike_oi_changes ?? []
              if (!strikes.length) {
                return (
                  <p className="text-center text-sm text-muted-foreground py-8">
                    No strike OI data available
                  </p>
                )
              }
              const sorted = [...strikes].sort((a, b) => a.strike - b.strike)
              const maxAbsOi = Math.max(
                ...sorted.map((s) => Math.max(Math.abs(s.ce_oi_chg), Math.abs(s.pe_oi_chg))),
                1,
              )
              const spot = pcrData.data.underlying_ltp
              // ── Butterfly horizontal bar chart: CE bars ← left, PE bars → right ──
              const rowH = 20
              const rowGap = 4
              const strikeColW = 60   // center column width for strike labels
              const maxBarW = 220     // max bar width on each side
              const padTop = 8
              const padBot = 20       // room for axis labels
              const totalW = maxBarW + strikeColW + maxBarW
              const centerX = maxBarW + strikeColW / 2
              const chartH = padTop + sorted.length * (rowH + rowGap) + padBot

              return (
                <div className="overflow-x-auto">
                  {/* Tooltip */}
                  <div
                    className="mb-2 text-xs text-center text-foreground min-h-[1.25rem]"
                    aria-live="polite"
                    aria-atomic="true"
                    role="status"
                  >
                    {oiChgBarHover ? (
                      <>
                        <span className="font-bold">Strike: {oiChgBarHover.strike}</span>
                        &nbsp;
                        <span className="text-amber-400 text-[10px]">
                          ({getMoneyness(oiChgBarHover.strike, pcrData.data.underlying_ltp)})
                        </span>
                        &nbsp;&nbsp;
                        <span className="text-red-400">Call OI Chg: {formatOiLakh(oiChgBarHover.ce_oi_chg)}</span>
                        &nbsp;&nbsp;
                        <span className="text-green-400">Put OI Chg: {formatOiLakh(oiChgBarHover.pe_oi_chg)}</span>
                      </>
                    ) : (
                      <span className="text-muted-foreground">Hover a row to see strike details</span>
                    )}
                  </div>
                  <svg
                    viewBox={`0 0 ${totalW} ${chartH}`}
                    className="w-full mx-auto"
                    style={{ maxHeight: chartH + 10, minHeight: 200 }}
                    onMouseLeave={() => setOiChgBarHover(null)}
                  >
                    {/* Center column dividers */}
                    <line x1={maxBarW} y1={0} x2={maxBarW} y2={chartH - padBot}
                      stroke="currentColor" strokeOpacity={0.15} strokeWidth={1} />
                    <line x1={maxBarW + strikeColW} y1={0} x2={maxBarW + strikeColW} y2={chartH - padBot}
                      stroke="currentColor" strokeOpacity={0.15} strokeWidth={1} />

                    {/* X-axis scale labels at bottom */}
                    <text x={4} y={chartH - 4} textAnchor="start" fontSize={8} fill="#ef4444" opacity={0.7}>
                      CE ←{formatOiLakh(maxAbsOi)}
                    </text>
                    <text x={centerX} y={chartH - 4} textAnchor="middle" fontSize={8} fill="currentColor" opacity={0.5}>
                      0
                    </text>
                    <text x={totalW - 4} y={chartH - 4} textAnchor="end" fontSize={8} fill="#22c55e" opacity={0.7}>
                      PE {formatOiLakh(maxAbsOi)}→
                    </text>

                    {/* Spot horizontal marker */}
                    {spot && (() => {
                      const spotIdx = sorted.findIndex((s) => s.strike >= spot)
                      if (spotIdx < 0) return null
                      const sy = padTop + spotIdx * (rowH + rowGap) + rowH / 2
                      return (
                        <line x1={0} y1={sy} x2={totalW} y2={sy}
                          stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="4,3" />
                      )
                    })()}

                    {sorted.map((s, i) => {
                      const y = padTop + i * (rowH + rowGap)
                      const ceW = (Math.abs(s.ce_oi_chg) / maxAbsOi) * maxBarW
                      const peW = (Math.abs(s.pe_oi_chg) / maxAbsOi) * maxBarW
                      // CE bar: extends LEFT from left edge of strike col
                      const ceX = maxBarW - ceW
                      // PE bar: extends RIGHT from right edge of strike col
                      const peX = maxBarW + strikeColW
                      const isAtm = spot && Math.abs(s.strike - spot) < STRIKE_PROXIMITY_THRESHOLD
                      return (
                        <g
                          key={s.strike}
                          onMouseEnter={() => setOiChgBarHover(s)}
                          style={{ cursor: 'crosshair' }}
                        >
                          {/* CE bar (red) — extends left */}
                          <rect
                            x={ceX} y={y + 2}
                            width={Math.max(ceW, 1)} height={rowH - 4}
                            fill="#ef4444"
                            fillOpacity={s.ce_oi_chg >= 0 ? 0.75 : 0.35}
                            rx={2}
                          />
                          {/* PE bar (green) — extends right */}
                          <rect
                            x={peX} y={y + 2}
                            width={Math.max(peW, 1)} height={rowH - 4}
                            fill="#22c55e"
                            fillOpacity={s.pe_oi_chg >= 0 ? 0.75 : 0.35}
                            rx={2}
                          />
                          {/* Strike label (centered) */}
                          <text
                            x={centerX} y={y + rowH / 2 + 4}
                            textAnchor="middle"
                            fontSize={9}
                            fill={isAtm ? '#f59e0b' : 'currentColor'}
                            fontWeight={isAtm ? 'bold' : 'normal'}
                          >
                            {s.strike}
                          </text>
                        </g>
                      )
                    })}
                  </svg>
                  {/* Legend */}
                  <div className="flex justify-center gap-6 mt-1 text-xs text-muted-foreground">
                    <span className="flex items-center gap-1.5">
                      <span className="inline-block h-3 w-8 rounded-sm bg-red-400 opacity-75" />
                      Call OI Change (→ right = adding, ← shorter = removing)
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="inline-block h-3 w-8 rounded-sm bg-green-400 opacity-75" />
                      Put OI Change
                    </span>
                  </div>
                </div>
              )
            })()}
            {/* Line chart legend */}
            {oiChgViewMode === 'line' && (
              <div className="flex justify-center gap-6 mt-2 text-xs text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-0.5 w-5 rounded bg-red-400" />
                  Call OI Change
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-0.5 w-5 rounded bg-green-400" />
                  Put OI Change
                </span>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ================================================================
          Section A — Advanced GEX Levels
          ================================================================ */}
      <Card>
        <CardHeader className="pb-3 cursor-pointer" onClick={() => setGexSectionOpen((v) => !v)}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">Advanced GEX Levels</CardTitle>
              {gexData?.levels && (
                <div className="flex items-center gap-1">
                  <Badge variant="outline" className="text-xs">
                    {gexData.mode === 'cumulative' ? '∑ Cumulative' : '⊙ Selected'}
                  </Badge>
                  {gexData.data_mode === 'last_day_fallback' && (
                    <Badge variant="secondary" className="text-[10px] bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 border-amber-200 dark:border-amber-800">
                      Last Day Fallback
                    </Badge>
                  )}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              {/* Selected / Cumulative mode toggle */}
              <div className="flex rounded-md overflow-hidden border border-border text-xs">
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); setGexMode('selected') }}
                  className={`px-2 py-1 ${gexMode === 'selected' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                >
                  ⊙ Selected
                </button>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); setGexMode('cumulative') }}
                  className={`px-2 py-1 ${gexMode === 'cumulative' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                >
                  ∑ Cumulative
                </button>
              </div>
              {/* Line / Bar view toggle */}
              <div className="flex rounded-md overflow-hidden border border-border text-xs">
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); setGexViewMode('line') }}
                  className={`px-3 py-1 transition-colors ${gexViewMode === 'line' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                >
                  Line
                </button>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); setGexViewMode('bar') }}
                  className={`px-3 py-1 transition-colors border-l border-border ${gexViewMode === 'bar' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                >
                  Bar
                </button>
              </div>
              {gexSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </div>
          </div>
          {/* Multi-expiry selector for cumulative mode */}
          {gexMode === 'cumulative' && gexSectionOpen && expiries.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-2" onClick={(e) => e.stopPropagation()}>
              <span className="text-xs text-muted-foreground self-center">Select expiries:</span>
              {expiries.slice(0, 4).map((exp) => {
                const isSelected = gexExpiries.includes(exp)
                return (
                  <button
                    key={exp}
                    type="button"
                    onClick={() =>
                      setGexExpiries((prev) =>
                        isSelected ? prev.filter((e) => e !== exp) : [...prev, exp].slice(0, 4)
                      )
                    }
                    className={`text-xs px-2 py-0.5 rounded-full border ${isSelected ? 'bg-primary text-primary-foreground border-primary' : 'border-border hover:bg-muted'}`}
                  >
                    {exp}
                  </button>
                )
              })}
            </div>
          )}
        </CardHeader>
        {gexSectionOpen && (
          <CardContent className="pt-0">
            {isGexLoading && (
              <div className="py-8 text-center text-muted-foreground">
                <RefreshCw className="h-5 w-5 animate-spin mx-auto mb-2" />
                <p className="text-sm">Loading GEX data…</p>
              </div>
            )}
            {!isGexLoading && gexData?.levels && (
              <>
                {/* Key levels pills */}
                <div className="flex flex-wrap gap-4 justify-around py-2 mb-4">
                  <Pill
                    label="Gamma Flip / HVL"
                    value={gexData.levels.gamma_flip != null ? gexData.levels.gamma_flip.toFixed(0) : 'N/A'}
                    variant="warning"
                  />
                  <Pill
                    label="Call Gamma Wall"
                    value={gexData.levels.call_gamma_wall != null ? gexData.levels.call_gamma_wall.toFixed(0) : 'N/A'}
                    variant="bearish"
                  />
                  <Pill
                    label="Put Gamma Wall"
                    value={gexData.levels.put_gamma_wall != null ? gexData.levels.put_gamma_wall.toFixed(0) : 'N/A'}
                    variant="bullish"
                  />
                  <Pill
                    label="Abs GEX Wall"
                    value={gexData.levels.absolute_wall != null ? gexData.levels.absolute_wall.toFixed(0) : 'N/A'}
                    variant="expansion"
                  />
                  <Pill
                    label="Total Net GEX"
                    value={gexData.levels.total_net_gex.toFixed(0)}
                    variant={gexData.levels.total_net_gex >= 0 ? 'bullish' : 'bearish'}
                  />
                </div>

                {/* GEX chart — Line or Bar mode */}
                {gexData.chain.length > 0 && (() => {
                  const sorted = [...gexData.chain].sort((a, b) => a.strike - b.strike)
                  const maxAbs = Math.max(...sorted.map((d) => Math.abs(d.net_gex)), 1)
                  const spot = gexData.spot_price

                  if (gexViewMode === 'bar') {
                    // ── Horizontal bar chart: strikes on Y-axis, bars extend left/right ──
                    const rowH = 22
                    const rowGap = 4
                    const labelW = 60   // width reserved for strike labels on the left
                    const maxBarW = 260  // max bar half-width (positive or negative)
                    const padTop = 10
                    const padBot = 10
                    const chartW = labelW + maxBarW * 2 + 10
                    const chartH = padTop + sorted.length * (rowH + rowGap) + padBot
                    const centerX = labelW + maxBarW  // center zero line

                    return (
                      <div className="overflow-x-auto">
                        {/* Hover tooltip */}
                        <div
                          className="mb-2 text-xs text-center text-foreground min-h-[1.25rem]"
                          aria-live="polite"
                          aria-atomic="true"
                          role="status"
                        >
                          {gexLineHover ? (
                            <>
                              <span className="font-bold">Strike: {gexLineHover.strike}</span>
                              &nbsp;&nbsp;
                              <span style={{ color: gexLineHover.net_gex >= 0 ? '#22c55e' : '#ef4444' }}>
                                Net GEX: {formatOiLakh(gexLineHover.net_gex)}
                              </span>
                            </>
                          ) : (
                            <span className="text-muted-foreground">Hover a bar to see strike details</span>
                          )}
                        </div>
                        <svg
                          viewBox={`0 0 ${chartW} ${chartH}`}
                          className="w-full mx-auto"
                          style={{ maxHeight: chartH + 10, minHeight: 180 }}
                          onMouseLeave={() => setGexLineHover(null)}
                        >
                          {/* Center zero line */}
                          <line
                            x1={centerX} y1={0}
                            x2={centerX} y2={chartH}
                            stroke="currentColor" strokeOpacity={0.2} strokeWidth={1}
                          />
                          {/* X-axis scale labels */}
                          <text x={centerX} y={chartH - 2} textAnchor="middle" fontSize={8} fill="currentColor" opacity={0.5}>0</text>
                          <text x={labelW + 4} y={chartH - 2} textAnchor="start" fontSize={8} fill="#ef4444" opacity={0.7}>
                            –{formatOiLakh(maxAbs)}
                          </text>
                          <text x={chartW - 4} y={chartH - 2} textAnchor="end" fontSize={8} fill="#22c55e" opacity={0.7}>
                            +{formatOiLakh(maxAbs)}
                          </text>

                          {/* Spot horizontal marker */}
                          {spot && (() => {
                            const spotIdx = sorted.findIndex((d) => d.strike >= spot)
                            if (spotIdx < 0) return null
                            const sy = padTop + spotIdx * (rowH + rowGap) + rowH / 2
                            return (
                              <line x1={labelW} y1={sy} x2={chartW - 4} y2={sy}
                                stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="4,3" />
                            )
                          })()}

                          {/* Gamma flip horizontal marker */}
                          {gexData.levels.gamma_flip != null && (() => {
                            const flipIdx = sorted.findIndex((d) =>
                              Math.abs(d.strike - gexData.levels.gamma_flip!) < STRIKE_PROXIMITY_THRESHOLD
                            )
                            if (flipIdx < 0) return null
                            const fy = padTop + flipIdx * (rowH + rowGap) + rowH / 2
                            return (
                              <line x1={labelW} y1={fy} x2={chartW - 4} y2={fy}
                                stroke="#a78bfa" strokeWidth={1.5} strokeDasharray="4,3" />
                            )
                          })()}

                          {sorted.map((d, i) => {
                            const y = padTop + i * (rowH + rowGap)
                            const bW = (Math.abs(d.net_gex) / maxAbs) * maxBarW
                            const bX = d.net_gex >= 0 ? centerX : centerX - bW
                            const isAtm = spot && Math.abs(d.strike - spot) < STRIKE_PROXIMITY_THRESHOLD
                            const isFlip = gexData.levels.gamma_flip != null &&
                              Math.abs(d.strike - gexData.levels.gamma_flip) < STRIKE_PROXIMITY_THRESHOLD
                            return (
                              <g
                                key={d.strike}
                                onMouseEnter={() => setGexLineHover({ strike: d.strike, net_gex: d.net_gex })}
                                style={{ cursor: 'crosshair' }}
                              >
                                {/* Strike label */}
                                <text
                                  x={labelW - 6} y={y + rowH / 2 + 4}
                                  textAnchor="end"
                                  fontSize={9}
                                  fill={isAtm ? '#f59e0b' : isFlip ? '#a78bfa' : 'currentColor'}
                                  fontWeight={isAtm || isFlip ? 'bold' : 'normal'}
                                >
                                  {d.strike}
                                </text>
                                {/* Bar */}
                                <rect
                                  x={bX} y={y + 2}
                                  width={Math.max(bW, 1)} height={rowH - 4}
                                  fill={d.net_gex >= 0 ? '#22c55e' : '#ef4444'}
                                  fillOpacity={0.75}
                                  rx={2}
                                />
                              </g>
                            )
                          })}
                        </svg>
                        {/* Legend */}
                        <div className="flex flex-wrap justify-center gap-6 mt-1 text-xs text-muted-foreground">
                          <span className="flex items-center gap-1.5">
                            <span className="inline-block h-3 w-8 rounded-sm bg-green-400 opacity-75" />
                            Long Gamma (Dealer)
                          </span>
                          <span className="flex items-center gap-1.5">
                            <span className="inline-block h-3 w-8 rounded-sm bg-red-400 opacity-75" />
                            Short Gamma (Dealer)
                          </span>
                          {spot && (
                            <span style={{ color: '#f59e0b' }}>── Spot: {spot.toFixed(0)}</span>
                          )}
                          {gexData.levels.gamma_flip != null && (
                            <span style={{ color: '#a78bfa' }}>── Γ Flip: {gexData.levels.gamma_flip.toFixed(0)}</span>
                          )}
                        </div>
                      </div>
                    )
                  }

                  // ── Line chart: strike on X-axis, net_gex on Y-axis ───────────────
                  const perStrikeW = 30
                  const padLeft = 65
                  const padRight = 20
                  const padTop = 20
                  const padBot = 45
                  const halfH = 110
                  const svgW = padLeft + sorted.length * perStrikeW + padRight
                  const svgH = padTop + halfH * 2 + padBot
                  const zeroY = padTop + halfH

                  const xs = sorted.map((_, i) => padLeft + i * perStrikeW + perStrikeW / 2)
                  const ys = sorted.map((d) => zeroY - (d.net_gex / maxAbs) * halfH)

                  const polylinePts = xs.map((x, i) => `${x},${ys[i]}`).join(' ')

                  // Positive fill: area above the zero line
                  const posPolyPts = [
                    `${xs[0]},${zeroY}`,
                    ...xs.map((x, i) => `${x},${Math.min(ys[i], zeroY)}`),
                    `${xs[xs.length - 1]},${zeroY}`,
                  ].join(' ')

                  // Negative fill: area below the zero line
                  const negPolyPts = [
                    `${xs[0]},${zeroY}`,
                    ...xs.map((x, i) => `${x},${Math.max(ys[i], zeroY)}`),
                    `${xs[xs.length - 1]},${zeroY}`,
                  ].join(' ')

                  return (
                    <div className="overflow-x-auto">
                      {/* Hover status */}
                      <div
                        className="mb-2 text-xs text-center text-foreground min-h-[1.25rem]"
                        aria-live="polite"
                        aria-atomic="true"
                        role="status"
                      >
                        {gexLineHover ? (
                          <>
                            <span className="font-bold">Strike: {gexLineHover.strike}</span>
                            &nbsp;&nbsp;
                            <span style={{ color: gexLineHover.net_gex >= 0 ? '#22c55e' : '#ef4444' }}>
                              Net GEX: {formatOiLakh(gexLineHover.net_gex)}
                            </span>
                          </>
                        ) : (
                          <span className="text-muted-foreground">Hover a strike to see details</span>
                        )}
                      </div>
                      <svg
                        viewBox={`0 0 ${svgW} ${svgH}`}
                        className="w-full mx-auto"
                        style={{ maxHeight: svgH + 20, minHeight: 200 }}
                        onMouseLeave={() => setGexLineHover(null)}
                      >
                        {/* Positive area fill (green) */}
                        <polygon points={posPolyPts} fill="#22c55e" fillOpacity={0.15} />
                        {/* Negative area fill (red) */}
                        <polygon points={negPolyPts} fill="#ef4444" fillOpacity={0.15} />
                        {/* Zero line */}
                        <line
                          x1={padLeft} y1={zeroY} x2={svgW - padRight} y2={zeroY}
                          stroke="currentColor" strokeOpacity={0.25} strokeWidth={1}
                        />
                        {/* Net GEX polyline */}
                        <polyline
                          points={polylinePts}
                          fill="none"
                          stroke="#10b981"
                          strokeWidth={2}
                          strokeLinejoin="round"
                        />
                        {/* Per-strike dots coloured by sign */}
                        {sorted.map((d, i) => (
                          <circle
                            key={d.strike}
                            cx={xs[i]} cy={ys[i]} r={3}
                            fill={d.net_gex >= 0 ? '#22c55e' : '#ef4444'}
                          />
                        ))}
                        {/* Spot vertical marker */}
                        {spot && (() => {
                          const spotIdx = sorted.findIndex((s) => s.strike >= spot)
                          if (spotIdx < 0) return null
                          return (
                            <line
                              x1={xs[spotIdx]} y1={padTop} x2={xs[spotIdx]} y2={zeroY + halfH}
                              stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="4,3"
                            />
                          )
                        })()}
                        {/* Gamma flip vertical marker */}
                        {gexData.levels.gamma_flip != null && (() => {
                          const flipIdx = sorted.findIndex((d) =>
                            Math.abs(d.strike - gexData.levels.gamma_flip!) < STRIKE_PROXIMITY_THRESHOLD
                          )
                          if (flipIdx < 0) return null
                          return (
                            <line
                              x1={xs[flipIdx]} y1={padTop} x2={xs[flipIdx]} y2={zeroY + halfH}
                              stroke="#a78bfa" strokeWidth={1.5} strokeDasharray="4,3"
                            />
                          )
                        })()}
                        {/* Y-axis labels */}
                        <text x={padLeft - 4} y={padTop + 4} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>
                          {formatOiLakh(maxAbs)}
                        </text>
                        <text x={padLeft - 4} y={zeroY + 4} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>0</text>
                        <text x={padLeft - 4} y={padTop + halfH * 2 - 2} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>
                          -{formatOiLakh(maxAbs)}
                        </text>
                        {/* X-axis strike labels */}
                        {sorted.map((d, i) => {
                          const isAtm = spot && Math.abs(d.strike - spot) < STRIKE_PROXIMITY_THRESHOLD
                          const isFlip = gexData.levels.gamma_flip != null &&
                            Math.abs(d.strike - gexData.levels.gamma_flip) < STRIKE_PROXIMITY_THRESHOLD
                          return (
                            <text
                              key={d.strike}
                              x={xs[i]} y={svgH - 5}
                              textAnchor="middle" fontSize={8}
                              fill={isAtm ? '#f59e0b' : isFlip ? '#a78bfa' : 'currentColor'}
                              fontWeight={isAtm || isFlip ? 'bold' : 'normal'}
                            >
                              {d.strike}
                            </text>
                          )
                        })}
                        {/* Invisible hit areas per strike for hover */}
                        {sorted.map((d, i) => (
                          <rect
                            key={d.strike}
                            x={xs[i] - perStrikeW / 2} y={padTop}
                            width={perStrikeW} height={halfH * 2}
                            fill="transparent"
                            style={{ cursor: 'crosshair' }}
                            onMouseEnter={() => setGexLineHover({ strike: d.strike, net_gex: d.net_gex })}
                          />
                        ))}
                      </svg>
                      {/* Legend */}
                      <div className="flex flex-wrap justify-center gap-6 mt-1 text-xs text-muted-foreground">
                        <span className="flex items-center gap-1.5">
                          <span className="inline-block h-0.5 w-5 rounded bg-green-400" />
                          Long Gamma (Dealer)
                        </span>
                        <span className="flex items-center gap-1.5">
                          <span className="inline-block h-0.5 w-5 rounded bg-red-400" />
                          Short Gamma (Dealer)
                        </span>
                        {spot && (
                          <span style={{ color: '#f59e0b' }}>── Spot: {spot.toFixed(0)}</span>
                        )}
                        {gexData.levels.gamma_flip != null && (
                          <span style={{ color: '#a78bfa' }}>── Γ Flip: {gexData.levels.gamma_flip.toFixed(0)}</span>
                        )}
                      </div>
                    </div>
                  )
                })()}

                {/* Expiries used */}
                <div className="mt-2 text-xs text-muted-foreground text-center">
                  Expiries: {gexData.expiries_used.join(', ')}
                </div>
              </>
            )}
            {!isGexLoading && !gexData && selectedExpiry && (
              <p className="text-center text-sm text-muted-foreground py-6">
                Click Analyse to load GEX data
              </p>
            )}
            {!selectedExpiry && (
              <p className="text-center text-sm text-muted-foreground py-6">
                Select an underlying and expiry first
              </p>
            )}
          </CardContent>
        )}
      </Card>

      {/* ================================================================
          Section A.1 — GEX Levels Spot Chart
          Multi-Expiry Gamma Exposure overlaid on OHLCV candle chart.
          ================================================================ */}
      <Card>
        <CardHeader
          className="pb-3 cursor-pointer"
          onClick={() => setSpotChartSectionOpen((v) => !v)}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">GEX Levels — Spot Chart</CardTitle>
              {gexData?.levels && (
                <Badge variant="outline" className="text-xs">
                  {gexData.mode === 'cumulative' ? '∑ Multi-Expiry' : '⊙ Single Expiry'}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
              {/* Interval selector */}
              <Select value={spotChartInterval} onValueChange={setSpotChartInterval}>
                <SelectTrigger className="w-[82px] h-8 text-xs">
                  <SelectValue placeholder="Interval" />
                </SelectTrigger>
                <SelectContent>
                  {['1m', '3m', '5m', '10m', '15m', '30m', '1h', '1d'].map((intv) => (
                    <SelectItem key={intv} value={intv}>{intv}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {/* Days selector */}
              <Select value={spotChartDays} onValueChange={setSpotChartDays}>
                <SelectTrigger className="w-[82px] h-8 text-xs">
                  <SelectValue placeholder="Days" />
                </SelectTrigger>
                <SelectContent>
                  {[['1', '1 Day'], ['3', '3 Days'], ['5', '5 Days'], ['10', '10 Days'], ['20', '20 Days'], ['30', '30 Days']].map(([v, l]) => (
                    <SelectItem key={v} value={v}>{l}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                onClick={() => loadSpotCandleData()}
                disabled={isSpotCandleLoading || !selectedUnderlying}
              >
                {isSpotCandleLoading
                  ? <RefreshCw className="h-3 w-3 animate-spin mr-1" />
                  : <RefreshCw className="h-3 w-3 mr-1" />}
                Refresh
              </Button>
              {spotChartSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </div>
          </div>
        </CardHeader>
        {spotChartSectionOpen && (
          <CardContent className="pt-0">
            <div className="relative">
              {isSpotCandleLoading && (
                <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-background/70 rounded-lg">
                  <RefreshCw className="h-5 w-5 animate-spin mx-auto mb-2" />
                  <p className="text-sm text-muted-foreground">Loading spot candles…</p>
                </div>
              )}
              <div
                ref={spotChartContainerRef}
                className="relative w-full rounded-lg border border-border/50"
                style={{ height: 380 }}
              />
              {(!spotCandleData?.candles?.length) && !isSpotCandleLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-background/70 rounded-lg">
                  <p className="text-sm text-muted-foreground">
                    {selectedUnderlying ? 'No candle data available' : 'Select an underlying first'}
                  </p>
                </div>
              )}
            </div>
            {/* GEX Level Legend */}
            {gexData?.levels && (
              <div className="flex flex-wrap justify-center gap-x-6 gap-y-1 mt-3 text-xs text-muted-foreground">
                {gexData.levels.call_gamma_wall != null && (
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-0.5 w-5 rounded bg-green-500" />
                    Call Wall {gexData.levels.call_gamma_wall.toFixed(0)}
                  </span>
                )}
                {gexData.levels.put_gamma_wall != null && (
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-0.5 w-5 rounded bg-red-500" />
                    Put Wall {gexData.levels.put_gamma_wall.toFixed(0)}
                  </span>
                )}
                {gexData.levels.gamma_flip != null && (
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-0.5 w-5 rounded border-dashed border-t-2 border-amber-400" style={{ backgroundColor: 'transparent' }} />
                    HVL / Γ Flip {gexData.levels.gamma_flip.toFixed(0)}
                  </span>
                )}
                {gexData.levels.absolute_wall != null && (
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-0.5 w-5 rounded bg-violet-400" />
                    Abs Wall {gexData.levels.absolute_wall.toFixed(0)}
                  </span>
                )}
                {gexData.per_expiry && gexData.per_expiry.length > 1 && (
                  <span className="text-muted-foreground italic">
                    + {gexData.per_expiry.length} expiry overlays (dashed)
                  </span>
                )}
              </div>
            )}
          </CardContent>
        )}
      </Card>

      {/* ================================================================
          Section C — IVR & Skew Dashboard
          ================================================================ */}
      <Card>
        <CardHeader className="pb-3 cursor-pointer" onClick={() => setIvSectionOpen((v) => !v)}>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">IVR &amp; Skew Dashboard</CardTitle>
            <div className="flex items-center gap-2">
              {/* Expiry multi-selector */}
              {expiries.length > 0 && ivSectionOpen && (
                <div className="flex gap-1 flex-wrap" onClick={(e) => e.stopPropagation()}>
                  {expiries.slice(0, 4).map((exp) => {
                    const isSelected = ivExpiries.includes(exp)
                    return (
                      <button
                        key={exp}
                        type="button"
                        onClick={() =>
                          setIvExpiries((prev) =>
                            isSelected ? prev.filter((e) => e !== exp) : [...prev, exp].slice(0, 4)
                          )
                        }
                        className={`text-xs px-2 py-0.5 rounded-full border ${isSelected ? 'bg-primary text-primary-foreground border-primary' : 'border-border hover:bg-muted'}`}
                      >
                        {exp}
                      </button>
                    )
                  })}
                </div>
              )}
              {ivSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </div>
          </div>
        </CardHeader>
        {ivSectionOpen && (
          <CardContent className="pt-0">
            {isIvLoading && (
              <div className="py-8 text-center text-muted-foreground">
                <RefreshCw className="h-5 w-5 animate-spin mx-auto mb-2" />
                <p className="text-sm">Loading IV data…</p>
              </div>
            )}
            {!isIvLoading && ivData && (
              <>
                {/* Summary pills */}
                <div className="flex flex-wrap gap-4 justify-around py-2 mb-4">
                  <Pill
                    label="IV Rank"
                    value={
                      ivData.ivr_available && ivData.iv_rank != null
                        ? `${ivData.iv_rank.toFixed(1)}%`
                        : 'N/A'
                    }
                    variant={
                      ivData.iv_rank != null && ivData.iv_rank > 50
                        ? 'bearish'
                        : ivData.iv_rank != null && ivData.iv_rank < 30
                          ? 'bullish'
                          : 'neutral'
                    }
                  />
                  <Pill
                    label="Current IV"
                    value={ivData.current_iv != null ? `${ivData.current_iv.toFixed(1)}%` : 'N/A'}
                    variant="neutral"
                  />
                  <Pill
                    label="Avg IVx"
                    value={ivData.avg_ivx != null ? `${ivData.avg_ivx.toFixed(1)}%` : 'N/A'}
                    variant="neutral"
                  />
                  <Pill
                    label="IV Change"
                    value={ivData.iv_change_pct != null ? `${ivData.iv_change_pct.toFixed(1)}%` : 'N/A'}
                    variant={
                      ivData.iv_change_pct != null && ivData.iv_change_pct > 0 ? 'bearish' : 'bullish'
                    }
                  />
                </div>
                {!ivData.ivr_available && (
                  <p className="text-xs text-muted-foreground text-center mb-3 italic">
                    IVRank unavailable — broker does not provide 1-year option history
                  </p>
                )}

                {/* Per-expiry table */}
                {ivData.expiries && ivData.expiries.length > 0 && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs text-left">
                      <thead>
                        <tr className="text-muted-foreground border-b border-border">
                          <th className="py-1.5 pr-3">Expiry</th>
                          <th className="py-1.5 pr-3">DTE</th>
                          <th className="py-1.5 pr-3">IVx</th>
                          <th className="py-1.5 pr-3">V.Skew%</th>
                          <th className="py-1.5 pr-3">H.IVx Skew%</th>
                          <th className="py-1.5 pr-3">Exp.Move</th>
                          <th className="py-1.5">Calendar?</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ivData.expiries.map((exp) => {
                          const hSkew = exp.horizontal_ivx_skew_pct
                          const isCalendarOpportunity = hSkew != null && hSkew < 0
                          return (
                            <tr key={exp.expiry_date} className="border-b border-border/40 hover:bg-muted/30">
                              <td className="py-1.5 pr-3 font-medium">{exp.expiry_date}</td>
                              <td className="py-1.5 pr-3">{exp.dte.toFixed(0)}d</td>
                              <td className="py-1.5 pr-3">
                                {exp.ivx != null ? `${exp.ivx.toFixed(1)}%` : '—'}
                              </td>
                              <td className={`py-1.5 pr-3 ${exp.vertical_skew_pct != null && exp.vertical_skew_pct > 0 ? 'text-red-500' : exp.vertical_skew_pct != null ? 'text-green-500' : ''}`}>
                                {exp.vertical_skew_pct != null ? `${exp.vertical_skew_pct > 0 ? '+' : ''}${exp.vertical_skew_pct.toFixed(1)}%` : '—'}
                              </td>
                              <td className={`py-1.5 pr-3 ${isCalendarOpportunity ? 'text-amber-500 font-semibold' : ''}`}>
                                {hSkew != null ? `${hSkew > 0 ? '+' : ''}${hSkew.toFixed(1)}%` : '—'}
                              </td>
                              <td className="py-1.5 pr-3">
                                {exp.expected_move != null ? `±${exp.expected_move.toFixed(0)}` : '—'}
                              </td>
                              <td className="py-1.5">
                                {isCalendarOpportunity ? (
                                  <Badge className="text-xs bg-amber-500/20 text-amber-600 dark:text-amber-400 border-amber-500/30">
                                    📅 Cal/Diag
                                  </Badge>
                                ) : '—'}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {/* IVx per-expiry bar chart */}
                {ivData.expiries && ivData.expiries.length > 1 && (() => {
                  const sorted = [...ivData.expiries].sort((a, b) => a.dte - b.dte)
                  const maxIvx = Math.max(...sorted.map((e) => e.ivx ?? 0), 1)
                  const barW = IVX_BAR_WIDTH
                  const gap = IVX_BAR_GAP
                  const chartW = sorted.length * (barW + gap) + 40
                  const chartH = IVX_CHART_HEIGHT

                  return (
                    <div className="mt-4 overflow-x-auto">
                      <div className="text-xs text-muted-foreground mb-1 text-center">IVx by Expiry</div>
                      <svg viewBox={`0 0 ${chartW} ${chartH + 40}`} className="w-full max-w-md mx-auto">
                        {sorted.map((exp, i) => {
                          const ivx = exp.ivx ?? 0
                          const h = (ivx / maxIvx) * chartH
                          const x = i * (barW + gap) + 20
                          const isInverted = exp.horizontal_ivx_skew != null && exp.horizontal_ivx_skew < 0
                          return (
                            <g key={exp.expiry_date}>
                              <rect
                                x={x} y={chartH - h}
                                width={barW} height={h}
                                fill={isInverted ? '#f59e0b' : '#60a5fa'}
                                fillOpacity={0.8}
                              />
                              <text x={x + barW / 2} y={chartH - h - 4} textAnchor="middle" fontSize={9} fill="currentColor">
                                {ivx.toFixed(1)}%
                              </text>
                              <text x={x + barW / 2} y={chartH + 12} textAnchor="middle" fontSize={8} fill="currentColor">
                                {exp.expiry_date.slice(0, 5)}
                              </text>
                              <text x={x + barW / 2} y={chartH + 22} textAnchor="middle" fontSize={8} fill="currentColor" opacity={0.7}>
                                {exp.dte.toFixed(0)}d
                              </text>
                            </g>
                          )
                        })}
                        <line x1={20} y1={0} x2={20} y2={chartH} stroke="currentColor" strokeOpacity={0.15} />
                        <line x1={20} y1={chartH} x2={chartW - 20} y2={chartH} stroke="currentColor" strokeOpacity={0.15} />
                      </svg>
                      <p className="text-xs text-center text-amber-500 mt-1">
                        🟡 Amber = Inverted IVx Skew → Calendar/Diagonal opportunity
                      </p>
                    </div>
                  )
                })()}
              </>
            )}
            {!isIvLoading && !ivData && selectedExpiry && (
              <p className="text-center text-sm text-muted-foreground py-6">
                Click Analyse to load IV dashboard
              </p>
            )}
            {!selectedExpiry && (
              <p className="text-center text-sm text-muted-foreground py-6">
                Select an underlying and expiry first
              </p>
            )}
          </CardContent>
        )}
      </Card>

      {/* ================================================================
          Section D — Intraday ATM IV Chart (ivChartApi)
          ================================================================ */}
      <Card>
        <CardHeader
          className="pb-3 cursor-pointer"
          onClick={() => setIvChartSectionOpen((v) => !v)}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">Intraday ATM IV Chart</CardTitle>
              {ivChartData && (
                <Badge variant="outline" className="text-xs">
                  ATM {ivChartData.atm_strike}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
              {/* Interval selector */}
              <Select value={ivChartInterval} onValueChange={setIvChartInterval}>
                <SelectTrigger className="w-[82px] h-8 text-xs">
                  <SelectValue placeholder="Interval" />
                </SelectTrigger>
                <SelectContent>
                  {ivChartIntervals.map((intv) => (
                    <SelectItem key={intv} value={intv}>{intv}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {/* Days selector */}
              <Select value={ivChartDays} onValueChange={setIvChartDays}>
                <SelectTrigger className="w-[82px] h-8 text-xs">
                  <SelectValue placeholder="Days" />
                </SelectTrigger>
                <SelectContent>
                  {['1', '3', '5'].map((d) => (
                    <SelectItem key={d} value={d}>{d} {d === '1' ? 'Day' : 'Days'}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                onClick={() => loadIvChartData()}
                disabled={isIvChartLoading || !selectedExpiry}
              >
                {isIvChartLoading ? (
                  <RefreshCw className="h-3 w-3 animate-spin mr-1" />
                ) : (
                  <RefreshCw className="h-3 w-3 mr-1" />
                )}
                Refresh
              </Button>
              {ivChartSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </div>
          </div>
        </CardHeader>
        {ivChartSectionOpen && (
          <CardContent className="pt-0">
            {isIvChartLoading && (
              <div className="py-8 text-center text-muted-foreground">
                <RefreshCw className="h-5 w-5 animate-spin mx-auto mb-2" />
                <p className="text-sm">Loading IV chart data…</p>
              </div>
            )}
            {!isIvChartLoading && ivChartData && ivChartData.series.length > 0 && (() => {
              // Show CE IV and PE IV as a simple SVG sparkline for each series
              const ceIvSeries = ivChartData.series.find((s) => s.option_type === 'CE')
              const peIvSeries = ivChartData.series.find((s) => s.option_type === 'PE')

              const allIvPoints = [
                ...(ceIvSeries?.iv_data.filter((p) => p.iv != null) ?? []),
                ...(peIvSeries?.iv_data.filter((p) => p.iv != null) ?? []),
              ]
              if (!allIvPoints.length) return (
                <p className="text-center text-sm text-muted-foreground py-6">No IV data available</p>
              )

              const minIv = Math.min(...allIvPoints.map((p) => p.iv as number))
              const maxIv = Math.max(...allIvPoints.map((p) => p.iv as number))
              const ivRange = maxIv - minIv || 1

              const svgH = 140
              const padT = 12; const padB = 24; const padL = 44; const padR = 12
              const plotH = svgH - padT - padB

              const buildPoints = (pts: typeof allIvPoints) => {
                if (!pts.length) return null
                const sorted = [...pts].sort((a, b) => a.time - b.time)
                const minT = sorted[0].time
                const maxT = sorted[sorted.length - 1].time
                const tRange = maxT - minT || 1
                const plotW = 600 - padL - padR

                const coords = sorted.map((p) => {
                  const x = padL + ((p.time - minT) / tRange) * plotW
                  const y = padT + plotH - ((((p.iv as number) - minIv) / ivRange) * plotH)
                  return `${x.toFixed(1)},${y.toFixed(1)}`
                })

                // Time axis tick labels (4 evenly spaced)
                const ticks = [0, 1, 2, 3].map((i) => {
                  const t = sorted[Math.round((i / 3) * (sorted.length - 1))]
                  const x = padL + ((t.time - minT) / tRange) * plotW
                  const ist = new Date(t.time * 1000 + 5.5 * 3600000)
                  const hh = ist.getUTCHours().toString().padStart(2, '0')
                  const mm = ist.getUTCMinutes().toString().padStart(2, '0')
                  return { x, label: `${hh}:${mm}` }
                })

                return { coords, ticks, plotW }
              }

              const ceBuilt = ceIvSeries ? buildPoints(ceIvSeries.iv_data.filter((p) => p.iv != null)) : null
              const peBuilt = peIvSeries ? buildPoints(peIvSeries.iv_data.filter((p) => p.iv != null)) : null
              const ticks = (ceBuilt ?? peBuilt)?.ticks ?? []

              return (
                <div>
                  {/* Legend + meta */}
                  <div className="flex flex-wrap items-center gap-4 mb-3 text-xs text-muted-foreground">
                    {ceIvSeries && (
                      <span className="flex items-center gap-1">
                        <span className="inline-block h-0.5 w-5 rounded bg-green-500" />
                        CE IV ({ceIvSeries.symbol})
                      </span>
                    )}
                    {peIvSeries && (
                      <span className="flex items-center gap-1">
                        <span className="inline-block h-0.5 w-5 rounded bg-red-500" />
                        PE IV ({peIvSeries.symbol})
                      </span>
                    )}
                    <span>ATM {ivChartData.atm_strike}</span>
                    <span>Spot {ivChartData.underlying_ltp.toFixed(2)}</span>
                  </div>
                  <svg
                    viewBox={`0 0 600 ${svgH}`}
                    className="w-full"
                    style={{ minHeight: svgH }}
                    preserveAspectRatio="none"
                  >
                    {/* Y-axis labels */}
                    {[0, 0.5, 1].map((frac) => {
                      const iv = minIv + frac * ivRange
                      const y = padT + plotH - frac * plotH
                      return (
                        <text key={frac} x={padL - 4} y={y + 4} textAnchor="end" fontSize={8} fill="currentColor" opacity={0.55}>
                          {iv.toFixed(1)}%
                        </text>
                      )
                    })}
                    {/* Grid lines */}
                    {[0, 0.5, 1].map((frac) => {
                      const y = padT + plotH - frac * plotH
                      return (
                        <line key={frac} x1={padL} y1={y} x2={600 - padR} y2={y}
                          stroke="currentColor" strokeOpacity={0.1} strokeWidth={1} />
                      )
                    })}
                    {/* CE IV polyline */}
                    {ceBuilt && (
                      <polyline points={ceBuilt.coords.join(' ')} fill="none" stroke="#22c55e" strokeWidth={1.5} />
                    )}
                    {/* PE IV polyline */}
                    {peBuilt && (
                      <polyline points={peBuilt.coords.join(' ')} fill="none" stroke="#ef4444" strokeWidth={1.5} />
                    )}
                    {/* X-axis time labels */}
                    {ticks.map((t, i) => (
                      <text key={i} x={t.x} y={svgH - 6} textAnchor="middle" fontSize={8} fill="currentColor" opacity={0.55}>
                        {t.label}
                      </text>
                    ))}
                  </svg>
                </div>
              )
            })()}
            {!isIvChartLoading && !ivChartData && selectedExpiry && (
              <p className="text-center text-sm text-muted-foreground py-6">
                Click Refresh to load the intraday ATM IV chart
              </p>
            )}
            {!selectedExpiry && (
              <p className="text-center text-sm text-muted-foreground py-6">
                Select an underlying and expiry first
              </p>
            )}
          </CardContent>
        )}
      </Card>
    </div>
  )
}
