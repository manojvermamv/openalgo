import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, ChevronsUpDown, RefreshCw } from 'lucide-react'
import { useSupportedExchanges } from '@/hooks/useSupportedExchanges'
import {
  buyerEdgeApi,
  type BuyerEdgeResponse,
  type SignalType,
} from '@/api/buyerEdge'
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

function convertExpiryForAPI(expiry: string): string {
  if (!expiry) return ''
  const parts = expiry.split('-')
  if (parts.length === 3) {
    return `${parts[0]}${parts[1].toUpperCase()}${parts[2].slice(-2)}`
  }
  return expiry.replace(/-/g, '').toUpperCase()
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
        <span>CE Δ {callDelta.toFixed(2)}</span>
        <span>PE Δ {putDelta.toFixed(2)}</span>
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
      const response = await buyerEdgeApi.getData({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: expiryForAPI,
      })
      if (requestIdRef.current !== requestId) return
      if (response.status === 'success') {
        setData(response)
      } else {
        showToast.error(response.message || 'Failed to fetch data')
      }
    } catch {
      if (requestIdRef.current !== requestId) return
      showToast.error('Failed to fetch buyer edge data')
    } finally {
      if (requestIdRef.current === requestId) setIsLoading(false)
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange])

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
                  <p className="text-xs text-muted-foreground">Call Wall (Supply)</p>
                  <p className="font-bold text-red-500">
                    {data.oi_intelligence.call_wall || '—'}
                  </p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-xs text-muted-foreground">Put Wall (Demand)</p>
                  <p className="font-bold text-green-500">
                    {data.oi_intelligence.put_wall || '—'}
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
                  label="Δ Imbalance"
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
    </div>
  )
}
