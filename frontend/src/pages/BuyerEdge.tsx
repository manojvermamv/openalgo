import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronsUpDown, RefreshCw, ChevronDown, ChevronUp } from 'lucide-react'
import {
  ColorType,
  CrosshairMode,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from 'lightweight-charts'
import { useSupportedExchanges } from '@/hooks/useSupportedExchanges'
import { useThemeStore } from '@/stores/themeStore'
import {
  buyerEdgeApi,
  type BuyerEdgeResponse,
  type GexLevelsResponse,
  type PcrChartResponse,
  type StrikeOiChange,
  type IvDashboardResponse,
  type SignalType,
} from '@/api/buyerEdge'
import {
  straddleChartApi,
  type StraddleChartData,
  type StraddleDataPoint,
} from '@/api/straddle-chart'
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
const PCR_CHART_HEIGHT = 300
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

/** Return interpretation label + colour for an ADR value. */
function getAdrMeta(adr: number): { label: string; color: string } {
  if (adr >= 1.5) return { label: 'Strong Bull', color: '#22c55e' }
  if (adr >= 1.0) return { label: 'Bullish',     color: '#86efac' }
  if (adr >= 0.9) return { label: 'Sideways',    color: '#fbbf24' }
  if (adr >= 0.7) return { label: 'Bearish',     color: '#fb923c' }
  return                  { label: 'Strong Bear', color: '#ef4444' }
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
          className="bg-red-500 transition-all duration-300"
          style={{ width: `${callPct}%` }}
        />
        <div
          className="bg-green-500 transition-all duration-300"
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
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Straddle Chart state ───────────────────────────────────────
  const [straddleInterval, setStraddleInterval] = useState('1m')
  const [straddleDays, setStraddleDays] = useState('1')
  const [straddleChartData, setStraddleChartData] = useState<StraddleChartData | null>(null)
  const [straddleIntervals, setStraddleIntervals] = useState<string[]>(['1m', '3m', '5m', '10m', '15m', '30m', '1h'])
  const [isChartLoading, setIsChartLoading] = useState(false)
  const [showStraddle, setShowStraddle] = useState(true)
  const [showSpot, setShowSpot] = useState(false)
  const [showSynthetic, setShowSynthetic] = useState(false)
  const [showVwap, setShowVwap] = useState(true)

  // ── Engine lookback settings ───────────────────────────────────
  const [lbBars, setLbBars] = useState('20')
  const [lbTf, setLbTf] = useState('3m')
  const [strikeCount, setStrikeCount] = useState('10')

  // ── Advanced GEX Levels state ──────────────────────────────────
  const [gexData, setGexData] = useState<GexLevelsResponse | null>(null)
  const [isGexLoading, _setIsGexLoading] = useState(false)
  const [gexMode, setGexMode] = useState<'selected' | 'cumulative'>('selected')
  const [gexExpiries, setGexExpiries] = useState<string[]>([])
  const [gexSectionOpen, setGexSectionOpen] = useState(true)
  const [gexViewMode, setGexViewMode] = useState<'line' | 'bar'>('line')
  const [gexLineHover, setGexLineHover] = useState<{ strike: number; net_gex: number } | null>(null)

  // ── PCR Chart state ────────────────────────────────────────────
  const [pcrData, setPcrData] = useState<PcrChartResponse | null>(null)
  const [isPcrLoading, setIsPcrLoading] = useState(false)
  const [pcrInterval, setPcrInterval] = useState('1m')
  const [pcrDays, setPcrDays] = useState('1')
  const [showPcrOi, setShowPcrOi] = useState(true)
  const [showPcrVolume, setShowPcrVolume] = useState(true)
  const [showPcrSpot, setShowPcrSpot] = useState(true)
  const [showPcrSynthetic, setShowPcrSynthetic] = useState(true)
  const [pcrSectionOpen, setPcrSectionOpen] = useState(true)
  const pcrChartContainerRef = useRef<HTMLDivElement>(null)
  const pcrChartRef = useRef<IChartApi | null>(null)
  const pcrOiSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrVolSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrSpotSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrSyntheticSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrTooltipRef = useRef<HTMLDivElement | null>(null)
  const pcrDataMapRef = useRef<Map<number, import('../api/buyerEdge').PcrDataPoint>>(new Map())

  // ── OI Change Chart state ──────────────────────────────────────
  const [oiChgSectionOpen, setOiChgSectionOpen] = useState(true)
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

      // Run all three: state engine + GEX + IVR in parallel
      const [response, gexResp, ivResp] = await Promise.allSettled([
        buyerEdgeApi.getData({
          underlying: selectedUnderlying,
          exchange: selectedExchange,
          expiry_date: expiryForAPI,
          strike_count: parseInt(strikeCount),
          lb_bars: parseInt(lbBars),
          lb_tf: lbTf,
        }),
        buyerEdgeApi.getGexLevels({
          underlying: selectedUnderlying,
          exchange: selectedExchange,
          mode: gexMode,
          expiry_date: gexMode === 'selected' ? expiryForAPI : undefined,
          expiry_dates: gexMode === 'cumulative'
            ? (gexExpiries.length > 0 ? gexExpiries.map(convertExpiryForAPI) : [expiryForAPI])
            : undefined,
          strike_count: 25,
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

      // GEX
      if (gexResp.status === 'fulfilled' && gexResp.value.status === 'success') {
        setGexData(gexResp.value)
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
  }, [selectedUnderlying, selectedExpiry, selectedExchange, strikeCount, lbBars, lbTf, gexMode, gexExpiries, ivExpiries])

  useEffect(() => {
    if (selectedExpiry) fetchData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExpiry])

  // Auto-refresh
  useEffect(() => {
    if (autoRefresh && selectedExpiry) {
      intervalRef.current = setInterval(fetchData, AUTO_REFRESH_INTERVAL)
    }
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [autoRefresh, fetchData, selectedExpiry])

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
      priceLineVisible: true,
      visible: showStraddle,
    })
    const syntheticSeries = chart.addSeries(LineSeries, {
      color: chartColors.synthetic,
      lineWidth: 1,
      lineStyle: 2,
      priceScaleId: 'left',
      title: 'Synthetic Fut',
      lastValueVisible: true,
      priceLineVisible: false,
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

    chartRef.current = chart
    spotSeriesRef.current = spotSeries
    straddleSeriesRef.current = straddleSeries
    syntheticSeriesRef.current = syntheticSeries
    vwapSeriesRef.current = vwapSeries

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
      const cl = chartColorsRef.current
      const { date, time: timeStr } = formatIST(time)
      tt.style.display = 'block'
      tt.style.background = cl.tooltipBg
      tt.style.border = `1px solid ${cl.tooltipBorder}`
      tt.style.color = cl.tooltipText
      tt.innerHTML = `
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.straddle};font-weight:600">Straddle Price</span>
          <span style="color:${cl.straddle};font-weight:600">${point.straddle.toFixed(2)}</span>
        </div>
        ${vwapVal != null ? `<div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.vwap}">VWAP (Day)</span>
          <span style="color:${cl.vwap}">${vwapVal.toFixed(2)}</span>
        </div>` : ''}
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.tooltipMuted}">&bull; ${point.atm_strike} Call:</span>
          <span>${point.ce_price.toFixed(2)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.tooltipMuted}">&bull; ${point.atm_strike} Put:</span>
          <span>${point.pe_price.toFixed(2)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.synthetic}">Synthetic Fut</span>
          <span style="color:${cl.synthetic}">${point.synthetic_future.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:16px">
          <span style="color:${cl.tooltipMuted}">Spot Price</span>
          <span>${point.spot.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        </div>
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
    }).catch(() => {/* keep defaults */})
  }, [])

  // Load straddle chart data
  const loadStraddleData = useCallback(async () => {
    if (!selectedExpiry) return
    setIsChartLoading(true)
    try {
      const res = await straddleChartApi.getStraddleData({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: convertExpiryForAPI(selectedExpiry),
        interval: straddleInterval,
        days: parseInt(straddleDays),
      })
      if (res.status === 'success' && res.data) {
        straddleChartDataRef.current = res.data
        setStraddleChartData(res.data)
        applyDataToChart(res.data)
      } else {
        straddleChartDataRef.current = null
        setStraddleChartData(null)
        seriesDataMapRef.current = new Map()
        vwapDataMapRef.current = new Map()
        spotSeriesRef.current?.setData([])
        straddleSeriesRef.current?.setData([])
        syntheticSeriesRef.current?.setData([])
        vwapSeriesRef.current?.setData([])
        showToast.error(res.message || 'Failed to load straddle data')
      }
    } catch {
      straddleChartDataRef.current = null
      setStraddleChartData(null)
      seriesDataMapRef.current = new Map()
      vwapDataMapRef.current = new Map()
      spotSeriesRef.current?.setData([])
      straddleSeriesRef.current?.setData([])
      syntheticSeriesRef.current?.setData([])
      vwapSeriesRef.current?.setData([])
      showToast.error('Failed to fetch straddle chart data')
    } finally {
      setIsChartLoading(false)
    }
  }, [selectedExpiry, selectedUnderlying, selectedExchange, straddleInterval, straddleDays, applyDataToChart])

  // Auto-load straddle chart when loadStraddleData deps change
  useEffect(() => {
    if (selectedExpiry) loadStraddleData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadStraddleData])

  // ── PCR Chart: load data + chart init ───────────────────────────

  const loadPcrData = useCallback(async () => {
    if (!selectedExpiry) return
    setIsPcrLoading(true)
    try {
      const res = await buyerEdgeApi.getPcrChart({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: convertExpiryForAPI(selectedExpiry),
        interval: pcrInterval,
        days: parseInt(pcrDays),
      })
      if (res.status === 'success') {
        setPcrData(res)
      } else {
        showToast.error(res.message || 'Failed to load PCR data')
      }
    } catch {
      showToast.error('Failed to fetch PCR chart data')
    } finally {
      setIsPcrLoading(false)
    }
  }, [selectedExpiry, selectedUnderlying, selectedExchange, pcrInterval, pcrDays])

  // Auto-load PCR chart when its deps change
  useEffect(() => {
    if (selectedExpiry) loadPcrData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadPcrData])

  // Build / update PCR lightweight-charts
  // PCR chart — always-rendered container (same pattern as straddle chart).
  // The chart container div is never unmounted so offsetWidth is always valid
  // when this effect fires.  Collapse only hides the CardContent via CSS
  // (display:none), not via conditional rendering, which avoids the
  // clientWidth=0 bug that caused lightweight-charts to collapse all series
  // onto the same right price scale.
  useEffect(() => {
    if (!pcrChartContainerRef.current) return
    const container = pcrChartContainerRef.current
    if (!pcrChartRef.current) {
      const c = createChart(container, {
        width: container.offsetWidth || container.clientWidth,
        height: PCR_CHART_HEIGHT,
        layout: {
          background: { type: ColorType.Solid, color: 'transparent' },
          textColor: chartColors.text,
        },
        grid: { vertLines: { color: chartColors.grid }, horzLines: { color: chartColors.grid } },
        crosshair: { mode: CrosshairMode.Normal },
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
            if (parseInt(pcrDays) > 1) {
              const dd = ist.getUTCDate().toString().padStart(2, '0')
              const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
              return `${dd}/${mo} ${hh}:${mm}`
            }
            return `${hh}:${mm}`
          },
        },
      })
      // Add series in same order as straddle chart: left-scale series first, right-scale after
      pcrSpotSeriesRef.current = c.addSeries(LineSeries, {
        color: chartColors.spot, lineWidth: 2, title: 'Spot',
        priceScaleId: 'left', lastValueVisible: true, priceLineVisible: true,
      })
      pcrSyntheticSeriesRef.current = c.addSeries(LineSeries, {
        color: '#a78bfa', lineWidth: 1, lineStyle: 2, title: 'Syn Fut',
        priceScaleId: 'left', lastValueVisible: true, priceLineVisible: false,
      })
      pcrOiSeriesRef.current = c.addSeries(LineSeries, {
        color: '#f59e0b', lineWidth: 2, title: 'PCR(OI)',
        priceScaleId: 'right', lastValueVisible: true, priceLineVisible: true,
      })
      pcrVolSeriesRef.current = c.addSeries(LineSeries, {
        color: '#60a5fa', lineWidth: 2, title: 'PCR(Vol)',
        priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false,
      })

      // Crosshair tooltip div
      if (!pcrTooltipRef.current) {
        const tt = document.createElement('div')
        tt.style.cssText = [
          'position:absolute', 'display:none', 'z-index:100', 'pointer-events:none',
          'padding:8px 10px', 'border-radius:6px', 'font-size:12px', 'line-height:1.6',
          'min-width:180px', 'white-space:nowrap',
        ].join(';')
        pcrTooltipRef.current = tt
        container.appendChild(pcrTooltipRef.current)
      }

      c.subscribeCrosshairMove((param) => {
        const tt = pcrTooltipRef.current
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
        const point = pcrDataMapRef.current.get(time)
        if (!point) {
          tt.style.display = 'none'
          return
        }
        const cl = chartColorsRef.current
        const { date, time: timeStr } = formatIST(time)
        tt.style.display = 'block'
        tt.style.background = cl.tooltipBg
        tt.style.border = `1px solid ${cl.tooltipBorder}`
        tt.style.color = cl.tooltipText
        tt.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:#f59e0b;font-weight:600">PCR(OI)</span>
            <span style="color:#f59e0b;font-weight:600">${point.pcr_oi != null ? point.pcr_oi.toFixed(4) : '—'}</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:#60a5fa">PCR(Vol)</span>
            <span style="color:#60a5fa">${point.pcr_volume != null ? point.pcr_volume.toFixed(4) : '—'}</span>
          </div>
          ${point.synthetic_future != null ? `<div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:#a78bfa">Syn Fut</span>
            <span style="color:#a78bfa">${point.synthetic_future.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          </div>` : ''}
          <div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:${cl.tooltipMuted}">Spot</span>
            <span>${point.spot.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          </div>
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

      pcrChartRef.current = c
    }
    // Apply series data
    const series = pcrData?.data?.series ?? []
    const sorted = [...series].sort((a, b) => a.time - b.time)

    // Build data map for crosshair tooltip lookup
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
    pcrSpotSeriesRef.current?.setData(
      sorted.map((p) => ({ time: p.time as import('lightweight-charts').UTCTimestamp, value: p.spot }))
    )
    pcrSyntheticSeriesRef.current?.setData(
      sorted.filter((p) => p.synthetic_future != null).map((p) => ({
        time: p.time as import('lightweight-charts').UTCTimestamp,
        value: p.synthetic_future as number,
      }))
    )
    pcrOiSeriesRef.current?.applyOptions({ visible: showPcrOi })
    pcrVolSeriesRef.current?.applyOptions({ visible: showPcrVolume })
    pcrSpotSeriesRef.current?.applyOptions({ visible: showPcrSpot })
    pcrSyntheticSeriesRef.current?.applyOptions({ visible: showPcrSynthetic })

    // Update timeScale formatter whenever pcrDays changes (chart is persistent, not rebuilt)
    pcrChartRef.current?.applyOptions({
      timeScale: {
        tickMarkFormatter: (time: number) => {
          const d = new Date(time * 1000)
          const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
          const hh = ist.getUTCHours().toString().padStart(2, '0')
          const mm = ist.getUTCMinutes().toString().padStart(2, '0')
          if (parseInt(pcrDays) > 1) {
            const dd = ist.getUTCDate().toString().padStart(2, '0')
            const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
            return `${dd}/${mo} ${hh}:${mm}`
          }
          return `${hh}:${mm}`
        },
      },
    })

    // Only fit content when at least spot data is present (PCR may be null in live-only mode)
    if (sorted.length > 0) pcrChartRef.current?.timeScale().fitContent()
  }, [pcrData, pcrDays, showPcrOi, showPcrVolume, showPcrSpot, showPcrSynthetic, chartColors])

  // PCR chart resize — set up once on mount (container is always in DOM)
  useEffect(() => {
    if (!pcrChartContainerRef.current) return
    const ro = new ResizeObserver(() => {
      if (pcrChartContainerRef.current && pcrChartRef.current) {
        pcrChartRef.current.applyOptions({ width: pcrChartContainerRef.current.offsetWidth || pcrChartContainerRef.current.clientWidth })
      }
    })
    ro.observe(pcrChartContainerRef.current)
    return () => ro.disconnect()
  }, [])

  // Destroy PCR chart on unmount
  useEffect(() => {
    return () => {
      pcrChartRef.current?.remove()
      pcrChartRef.current = null
      pcrOiSeriesRef.current = null
      pcrVolSeriesRef.current = null
      pcrSpotSeriesRef.current = null
      pcrSyntheticSeriesRef.current = null
    }
  }, [])

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
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: cl.border },
        timeScale: { borderColor: cl.border, timeVisible: true, secondsVisible: false,
          tickMarkFormatter: (t: number) => {
            const d = new Date(t * 1000)
            const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
            const hh = ist.getUTCHours().toString().padStart(2, '0')
            const mm = ist.getUTCMinutes().toString().padStart(2, '0')
            if (parseInt(pcrDays) > 1) {
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
        color: '#ef4444', lineWidth: 2, title: 'Call OI Chg',
        priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false,
      })
      oiChgPeSeriesRef.current = c.addSeries(LineSeries, {
        color: '#22c55e', lineWidth: 2, title: 'Put OI Chg',
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
            <span style="color:#ef4444">Call OI Chg</span>
            <span style="color:#ef4444">${formatOiLakh(pt.ce_oi_chg)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px">
            <span style="color:#22c55e">Put OI Chg</span>
            <span style="color:#22c55e">${formatOiLakh(pt.pe_oi_chg)}</span>
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
          if (parseInt(pcrDays) > 1) {
            const dd = ist.getUTCDate().toString().padStart(2, '0')
            const mo = (ist.getUTCMonth() + 1).toString().padStart(2, '0')
            return `${dd}/${mo} ${hh}:${mm}`
          }
          return `${hh}:${mm}`
        },
      },
    })
    if (sorted.length > 0) oiChgChartRef.current?.timeScale().fitContent()
  }, [pcrData, oiChgViewMode, pcrDays, chartColors])

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

  const signal = data?.signal_engine?.signal ?? null
  const signalCfg = signal ? SIGNAL_CONFIG[signal] : null

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
              onClick={fetchData}
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
                <span className="font-medium">{data.spot?.toFixed(2)}</span>
              </span>
              {data.timestamp && <span>{data.timestamp}</span>}
            </div>
          </CardContent>
        </Card>
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


      {/* Straddle Chart — always rendered so chartContainerRef is always mounted */}
      <Card>
        <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle className="text-base">Straddle Chart</CardTitle>
              <div className="flex flex-wrap items-center gap-2">
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

                {/* Refresh chart */}
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8 text-xs"
                  onClick={loadStraddleData}
                  disabled={isChartLoading}
                >
                  {isChartLoading ? (
                    <RefreshCw className="h-3 w-3 animate-spin mr-1" />
                  ) : (
                    <RefreshCw className="h-3 w-3 mr-1" />
                  )}
                  Refresh
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            {/* Latest info bar */}
            {latestStraddlePoint && (
              <div className="flex flex-wrap items-center gap-x-6 gap-y-1 mb-4 text-sm">
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
              </div>
            )}

            {/* Chart container */}
            <div className="relative">
              <div
                ref={chartContainerRef}
                className="relative w-full rounded-lg border border-border/50"
                style={{ height: STRADDLE_CHART_HEIGHT }}
              />
              {isChartLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-background/60 rounded-lg">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    Loading straddle data…
                  </div>
                </div>
              )}
              {!selectedExpiry && !isChartLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-background/70 rounded-lg">
                  <p className="text-sm text-muted-foreground">Select an underlying and expiry to load the straddle chart</p>
                </div>
              )}
            </div>

            {/* Legend toggles */}
            <div className="flex items-center justify-center gap-4 mt-3">
              <button
                type="button"
                onClick={() => setShowStraddle((v) => !v)}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showStraddle ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
              >
                <span className="inline-block h-0.5 w-5 rounded" style={{ backgroundColor: chartColors.straddle }} />
                Straddle
              </button>
              <button
                type="button"
                onClick={() => setShowVwap((v) => !v)}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showVwap ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
              >
                <span className="inline-block h-0.5 w-5 rounded border-dashed border-t-2" style={{ borderColor: chartColors.vwap, backgroundColor: 'transparent' }} />
                VWAP(D)
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

      {/* Loading state */}
      {isLoading && !data && (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground">
            <RefreshCw className="h-8 w-8 animate-spin mx-auto mb-3" />
            <p>Computing buyer edge signal…</p>
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
                <Badge variant="outline" className="text-xs">
                  {gexData.mode === 'cumulative' ? '∑ Cumulative' : '⊙ Selected'}
                </Badge>
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
                    // ── Vertical bar chart (strikes on X-axis, net_gex up/down from zero) ──
                    const barW = 22
                    const barGap = 6
                    const chartPadTop = 30
                    const chartPadBot = 40
                    const maxBarH = 140
                    const chartW = sorted.length * (barW + barGap) + 60
                    const chartH = chartPadTop + maxBarH + chartPadBot
                    const zeroY = chartPadTop + maxBarH

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
                            <span className="text-muted-foreground">Hover a bar to see strike details</span>
                          )}
                        </div>
                        <svg
                          viewBox={`0 0 ${chartW} ${chartH}`}
                          className="w-full mx-auto"
                          style={{ maxHeight: chartH + 20, minHeight: 200 }}
                          onMouseLeave={() => setGexLineHover(null)}
                        >
                          {/* Zero line */}
                          <line x1={30} y1={zeroY} x2={chartW - 10} y2={zeroY}
                            stroke="currentColor" strokeOpacity={0.2} strokeWidth={1} />

                          {sorted.map((d, i) => {
                            const x = 30 + i * (barW + barGap)
                            const bH = (Math.abs(d.net_gex) / maxAbs) * maxBarH
                            const bY = d.net_gex >= 0 ? zeroY - bH : zeroY
                            const isAtm = spot && Math.abs(d.strike - spot) < STRIKE_PROXIMITY_THRESHOLD
                            const isFlip = gexData.levels.gamma_flip != null &&
                              Math.abs(d.strike - gexData.levels.gamma_flip) < STRIKE_PROXIMITY_THRESHOLD
                            return (
                              <g
                                key={d.strike}
                                onMouseEnter={() => setGexLineHover({ strike: d.strike, net_gex: d.net_gex })}
                                style={{ cursor: 'crosshair' }}
                              >
                                <rect
                                  x={x} y={bY}
                                  width={barW} height={Math.max(bH, 1)}
                                  fill={d.net_gex >= 0 ? '#22c55e' : '#ef4444'}
                                  fillOpacity={0.75}
                                />
                                {/* Strike label */}
                                <text
                                  x={x + barW / 2}
                                  y={chartH - 5}
                                  textAnchor="middle"
                                  fontSize={8}
                                  fill={isAtm ? '#f59e0b' : isFlip ? '#a78bfa' : 'currentColor'}
                                  fontWeight={isAtm || isFlip ? 'bold' : 'normal'}
                                >
                                  {d.strike}
                                </text>
                              </g>
                            )
                          })}

                          {/* Y-axis labels */}
                          <text x={28} y={chartPadTop + 4} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>
                            {formatOiLakh(maxAbs)}
                          </text>
                          <text x={28} y={zeroY + 4} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>0</text>
                          <text x={28} y={zeroY + maxBarH - 2} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>
                            -{formatOiLakh(maxAbs)}
                          </text>

                          {/* Spot vertical marker */}
                          {spot && (() => {
                            const spotIdx = sorted.findIndex((d) => d.strike >= spot)
                            if (spotIdx < 0) return null
                            const sx = 30 + spotIdx * (barW + barGap) - barGap / 2
                            return (
                              <line x1={sx} y1={chartPadTop} x2={sx} y2={zeroY + 10}
                                stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="4,3" />
                            )
                          })()}

                          {/* Gamma flip vertical marker */}
                          {gexData.levels.gamma_flip != null && (() => {
                            const flipIdx = sorted.findIndex((d) =>
                              Math.abs(d.strike - gexData.levels.gamma_flip!) < STRIKE_PROXIMITY_THRESHOLD
                            )
                            if (flipIdx < 0) return null
                            const fx = 30 + flipIdx * (barW + barGap) + barW
                            return (
                              <line x1={fx} y1={chartPadTop} x2={fx} y2={zeroY + 10}
                                stroke="#a78bfa" strokeWidth={1.5} strokeDasharray="4,3" />
                            )
                          })()}
                        </svg>
                        {/* Legend */}
                        <div className="flex flex-wrap justify-center gap-6 mt-1 text-xs text-muted-foreground">
                          <span className="flex items-center gap-1.5">
                            <span className="inline-block h-3 w-3 rounded-sm bg-green-400" />
                            Long Gamma (Dealer)
                          </span>
                          <span className="flex items-center gap-1.5">
                            <span className="inline-block h-3 w-3 rounded-sm bg-red-400" />
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
          Section B — PCR Chart
          ================================================================ */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <CardTitle
                className="text-base cursor-pointer"
                onClick={() => setPcrSectionOpen((v) => !v)}
              >
                PCR Chart
              </CardTitle>
              {pcrData?.data?.live_only && (
                <Badge variant="secondary" className="text-xs">Live PCR only</Badge>
              )}
              {pcrData?.data && (
                <div className="flex gap-3 text-xs text-muted-foreground">
                  <span>PCR(OI): <strong className="text-foreground">{pcrData.data.current_pcr_oi?.toFixed(2) ?? 'N/A'}</strong></span>
                  <span>PCR(Vol): <strong className="text-foreground">{pcrData.data.current_pcr_volume?.toFixed(2) ?? 'N/A'}</strong></span>
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Select value={pcrInterval} onValueChange={setPcrInterval}>
                <SelectTrigger className="w-[80px] h-8 text-xs">
                  <SelectValue placeholder="Interval" />
                </SelectTrigger>
                <SelectContent>
                  {['1m', '3m', '5m', '15m', '30m', '1h', '1d'].map((i) => (
                    <SelectItem key={i} value={i}>{i}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={pcrDays} onValueChange={setPcrDays}>
                <SelectTrigger className="w-[80px] h-8 text-xs">
                  <SelectValue placeholder="Days" />
                </SelectTrigger>
                <SelectContent>
                  {['1', '2', '3', '5'].map((d) => (
                    <SelectItem key={d} value={d}>{d} {d === '1' ? 'Day' : 'Days'}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline" size="sm" className="h-8 text-xs"
                onClick={loadPcrData}
                disabled={isPcrLoading || !selectedExpiry}
              >
                {isPcrLoading ? <RefreshCw className="h-3 w-3 animate-spin mr-1" /> : <RefreshCw className="h-3 w-3 mr-1" />}
                Refresh
              </Button>
              <button
                type="button"
                onClick={() => setPcrSectionOpen((v) => !v)}
                className="text-muted-foreground hover:text-foreground"
              >
                {pcrSectionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
              </button>
            </div>
          </div>
        </CardHeader>
        {/* CardContent is always rendered so the chart container stays in the DOM and
            offsetWidth is valid when the chart is first created. Collapse only
            hides it via CSS (matching the straddle chart pattern). */}
        <CardContent className="pt-0" style={{ display: pcrSectionOpen ? undefined : 'none' }}>
            <div className="relative">
              <div
                ref={pcrChartContainerRef}
                className="relative w-full rounded-lg border border-border/50"
                style={{ height: PCR_CHART_HEIGHT }}
              />
              {isPcrLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-background/60 rounded-lg">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    Loading PCR data…
                  </div>
                </div>
              )}
              {!selectedExpiry && !isPcrLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-background/70 rounded-lg">
                  <p className="text-sm text-muted-foreground">Select an underlying and expiry to load PCR chart</p>
                </div>
              )}
            </div>
            {/* Legend toggles */}
            <div className="flex items-center justify-center gap-4 mt-3 flex-wrap">
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
                onClick={() => setShowPcrSpot((v) => !v)}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showPcrSpot ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
              >
                <span className="inline-block h-0.5 w-5 rounded" style={{ backgroundColor: chartColors.spot }} />
                Spot
              </button>
              <button
                type="button"
                onClick={() => setShowPcrSynthetic((v) => !v)}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-colors ${showPcrSynthetic ? 'bg-muted font-medium' : 'opacity-50 hover:opacity-75'}`}
              >
                <span className="inline-block h-0.5 w-5 rounded border-dashed border-t-2 border-purple-400" style={{ backgroundColor: 'transparent' }} />
                Syn Fut
              </button>
            </div>
            <p className="text-xs text-muted-foreground text-center mt-1">
              PCR &gt; 1.2 = Bearish / PCR &lt; 0.8 = Bullish / PCR ≈ 1.0 = Neutral
            </p>
          </CardContent>
      </Card>

      {/* ================================================================
          Section B.5 — Market Breadth
          ================================================================ */}
      {pcrData?.data && (() => {
        const lastPt = [...(pcrData.data.series ?? [])].reverse().find(
          (p) => p.advances !== undefined
        )
        const adv = lastPt?.advances ?? 0
        const dec = lastPt?.declines ?? 0
        const neu = lastPt?.neutral ?? 0
        const total = adv + dec + neu || 1
        const adr = pcrData.data.current_adr
        const adrMeta = adr != null ? getAdrMeta(adr) : null
        return (
          <Card>
            <CardHeader className="pb-2 pt-4 px-4">
              <div className="flex items-center gap-2">
                <span className="inline-block w-1 h-5 rounded-full bg-pink-500 mr-1" />
                <CardTitle className="text-sm tracking-widest uppercase text-muted-foreground font-semibold">Market Breadth</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="pt-0 px-4 pb-4 space-y-2">
              {/* Advancing */}
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
              {/* Declining */}
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
              {/* Neutral */}
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
              {/* ADR label */}
              <div className="pt-1 text-xs text-muted-foreground">
                ADR:{' '}
                <strong style={{ color: adrMeta?.color ?? 'currentColor' }}>
                  {adr != null ? adr.toFixed(2) : '—'}
                </strong>
                {adrMeta && (
                  <span className="ml-1.5" style={{ color: adrMeta.color }}>
                    • {adrMeta.label}
                  </span>
                )}
              </div>
            </CardContent>
          </Card>
        )
      })()}

      {/* ================================================================
          Section B.6 — OI Change Chart (Line / Bar)
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
                  Total Change in OI — Intraday
                </CardTitle>
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
          </CardHeader>
          <CardContent className="pt-0" style={{ display: oiChgSectionOpen ? undefined : 'none' }}>
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
              const barGroupW = 18
              const barPairGap = 2
              const groupGap = 4
              const chartPadTop = 30
              const chartPadBot = 40
              const maxBarH = 140
              const chartW = sorted.length * (barGroupW * 2 + barPairGap + groupGap) + 60
              const chartH = chartPadTop + maxBarH + chartPadBot
              const zeroY = chartPadTop + maxBarH

              return (
                <div className="overflow-x-auto">
                  {/* Tooltip — always rendered (empty state when nothing is hovered)
                      so screen readers can pick up value changes via aria-live */}
                  <div
                    className="mb-2 text-xs text-center text-foreground min-h-[1.25rem]"
                    aria-live="polite"
                    aria-atomic="true"
                    role="status"
                  >
                    {oiChgBarHover ? (
                      <>
                        <span className="font-bold">Strike: {oiChgBarHover.strike}</span>
                        &nbsp;&nbsp;
                        <span className="text-red-400">Call OI Chg: {formatOiLakh(oiChgBarHover.ce_oi_chg)}</span>
                        &nbsp;&nbsp;
                        <span className="text-green-400">Put OI Chg: {formatOiLakh(oiChgBarHover.pe_oi_chg)}</span>
                      </>
                    ) : (
                      <span className="text-muted-foreground">Hover a bar to see strike details</span>
                    )}
                  </div>
                  <svg
                    viewBox={`0 0 ${chartW} ${chartH}`}
                    className="w-full mx-auto"
                    style={{ maxHeight: chartH + 20, minHeight: 200 }}
                    onMouseLeave={() => setOiChgBarHover(null)}
                  >
                    {/* Zero line */}
                    <line x1={30} y1={zeroY} x2={chartW - 10} y2={zeroY}
                      stroke="currentColor" strokeOpacity={0.2} strokeWidth={1} />

                    {sorted.map((s, i) => {
                      const x = 30 + i * (barGroupW * 2 + barPairGap + groupGap)
                      const ceH = (Math.abs(s.ce_oi_chg) / maxAbsOi) * maxBarH
                      const peH = (Math.abs(s.pe_oi_chg) / maxAbsOi) * maxBarH
                      const ceY = s.ce_oi_chg >= 0 ? zeroY - ceH : zeroY
                      const peY = s.pe_oi_chg >= 0 ? zeroY - peH : zeroY
                      const isAtm = spot && Math.abs(s.strike - spot) < STRIKE_PROXIMITY_THRESHOLD
                      return (
                        <g
                          key={s.strike}
                          onMouseEnter={() => setOiChgBarHover(s)}
                          style={{ cursor: 'crosshair' }}
                        >
                          {/* CE bar (red) */}
                          <rect
                            x={x} y={ceY}
                            width={barGroupW} height={Math.max(ceH, 1)}
                            fill="#ef4444" fillOpacity={0.75}
                          />
                          {/* PE bar (green) */}
                          <rect
                            x={x + barGroupW + barPairGap} y={peY}
                            width={barGroupW} height={Math.max(peH, 1)}
                            fill="#22c55e" fillOpacity={0.75}
                          />
                          {/* Strike label */}
                          <text
                            x={x + barGroupW + barPairGap / 2}
                            y={chartH - 5}
                            textAnchor="middle"
                            fontSize={8}
                            fill={isAtm ? '#f59e0b' : 'currentColor'}
                            fontWeight={isAtm ? 'bold' : 'normal'}
                          >
                            {s.strike}
                          </text>
                        </g>
                      )
                    })}
                    {/* Y-axis labels */}
                    <text x={28} y={chartPadTop + 4} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>
                      {formatOiLakh(maxAbsOi)}
                    </text>
                    <text x={28} y={zeroY + 4} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>0</text>
                    <text x={28} y={zeroY + maxBarH - 2} textAnchor="end" fontSize={9} fill="currentColor" opacity={0.6}>
                      -{formatOiLakh(maxAbsOi)}
                    </text>
                    {/* Spot marker */}
                    {spot && (() => {
                      const spotIdx = sorted.findIndex((s) => s.strike >= spot)
                      if (spotIdx < 0) return null
                      const sx = 30 + spotIdx * (barGroupW * 2 + barPairGap + groupGap) - groupGap / 2
                      return (
                        <line x1={sx} y1={chartPadTop} x2={sx} y2={zeroY + 10}
                          stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="4,3" />
                      )
                    })()}
                  </svg>
                  {/* Legend */}
                  <div className="flex justify-center gap-6 mt-1 text-xs text-muted-foreground">
                    <span className="flex items-center gap-1.5">
                      <span className="inline-block h-0.5 w-5 rounded bg-red-400" />
                      Call OI Change
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="inline-block h-0.5 w-5 rounded bg-green-400" />
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
    </div>
  )
}
