import { Suspense, lazy, useCallback, useEffect, useState } from 'react'
import { useSupportedExchanges } from '@/hooks/useSupportedExchanges'
import { useThemeStore } from '@/stores/themeStore'
import { useMarketStatus } from '@/hooks/useMarketStatus'
import {
  buyerEdgeApi,
  type GexLevelsResponse,
  type IvDashboardResponse,
  type SpotCandleResponse,
  type UnifiedMonitorResponse,
} from '@/api/buyerEdge'

// Sub-components (Lazy Loaded)
const Controls = lazy(() => import('./buyeredge/Controls').then(m => ({ default: m.Controls })))
const PremiumMonitor = lazy(() => import('./buyeredge/PremiumMonitor').then(m => ({ default: m.PremiumMonitor })))
const IntradayOiChange = lazy(() => import('./buyeredge/IntradayOiChange').then(m => ({ default: m.IntradayOiChange })))
const MarketBreadth = lazy(() => import('./buyeredge/MarketBreadth').then(m => ({ default: m.MarketBreadth })))
const GexLevels = lazy(() => import('./buyeredge/GexLevels').then(m => ({ default: m.GexLevels })))
const GexSpotChart = lazy(() => import('./buyeredge/GexSpotChart').then(m => ({ default: m.GexSpotChart })))
const IvrDashboard = lazy(() => import('./buyeredge/IvrDashboard').then(m => ({ default: m.IvrDashboard })))
const InterpretationGuide = lazy(() => import('./buyeredge/InterpretationGuide').then(m => ({ default: m.InterpretationGuide })))
const ScoreGauge = lazy(() => import('./buyeredge/ScoreGauge').then(m => ({ default: m.ScoreGauge })))
const BEBand = lazy(() => import('./buyeredge/CommonComponents').then(m => ({ default: m.BEBand })))
const DeltaBar = lazy(() => import('./buyeredge/CommonComponents').then(m => ({ default: m.DeltaBar })))

// Logic and Utils
import { AUTO_REFRESH_INTERVAL } from './buyeredge/types'
import { convertExpiryForAPI } from './buyeredge/utils'
import { showToast } from '@/utils/toast'

export default function BuyerEdge() {
  const { fnoExchanges, defaultFnoExchange, defaultUnderlyings } = useSupportedExchanges()
  const { mode } = useThemeStore()
  const { isMarketOpen } = useMarketStatus()
  const isDarkMode = mode === 'dark'

  const [selectedExchange, setSelectedExchange] = useState(defaultFnoExchange)
  const [underlyings, setUnderlyings] = useState<string[]>(
    (defaultUnderlyings[defaultFnoExchange] as any) || []
  )
  const [underlyingOpen, setUnderlyingOpen] = useState(false)
  const [selectedUnderlying, setSelectedUnderlying] = useState(
    (defaultUnderlyings[defaultFnoExchange]?.[0] as string) || ''
  )
  const [expiries, setExpiries] = useState<string[]>([])
  const [selectedExpiry, setSelectedExpiry] = useState('')
  const [data, setData] = useState<UnifiedMonitorResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(false)

  // ── Premium Monitor (Straddle + PCR) state ─────────────────────
  const [straddleInterval, setStraddleInterval] = useState('1m')
  const [straddleDays, setStraddleDays] = useState('1')
  const [showStraddle, setShowStraddle] = useState(false)
  const [showSpot, setShowSpot] = useState(false)
  const [showSynthetic, setShowSynthetic] = useState(false)
  const [showVwap, setShowVwap] = useState(false)
  const [showPcrOi, setShowPcrOi] = useState(true)
  const [showPcrVol, setShowPcrVol] = useState(false)
  const [straddleSectionOpen, setStraddleSectionOpen] = useState(true)
  const [showPremiumInfo, setShowPremiumInfo] = useState(false)

  // ── Engine lookback settings ───────────────────────────────────
  const [lbBars, setLbBars] = useState('20')
  const [lbTf, setLbTf] = useState('3m')
  const [strikeCount, setStrikeCount] = useState('10')
  const [atmMode, setAtmMode] = useState<'auto' | 'manual'>('auto')
  const [manualStrike, setManualStrike] = useState('')

  // ── GEX Levels state ──────────────────────────────────────────
  const [gexData, setGexData] = useState<GexLevelsResponse | null>(null)
  const [isGexLoading, setIsGexLoading] = useState(false)
  const [gexMode, setGexMode] = useState<'selected' | 'cumulative'>('selected')
  const [gexSectionOpen, setGexSectionOpen] = useState(true)

  // ── OI Change state ───────────────────────────────────────────
  const [oiChgSectionOpen, setOiChgSectionOpen] = useState(true)
  const [showOiChgInfo, setShowOiChgInfo] = useState(false)
  const [oiChgViewMode, setOiChgViewMode] = useState<'line' | 'bar'>('line')

  // ── Breadth state ─────────────────────────────────────────────
  const [breadthSectionOpen, setBreadthSectionOpen] = useState(true)
  const [showBreadthInfo, setShowBreadthInfo] = useState(false)

  // ── IVR Dashboard state ───────────────────────────────────────
  const [ivData, setIvData] = useState<IvDashboardResponse | null>(null)
  const [ivSectionOpen, setIvSectionOpen] = useState(true)

  // ── GEX Spot Chart state ──────────────────────────────────────
  const [spotCandleData, setSpotCandleData] = useState<SpotCandleResponse | null>(null)
  const [spotChartInterval, setSpotChartInterval] = useState('15m')
  const [spotChartDays, setSpotChartDays] = useState('5')
  const [isSpotCandleLoading, setIsSpotCandleLoading] = useState(false)
  const [spotChartSectionOpen, setSpotChartSectionOpen] = useState(true)

  // ── Interpretation state ──────────────────────────────────────
  const [showInterpretation, setShowInterpretation] = useState(false)

  // ── Data Refresh Logic ────────────────────────────────────────
  useEffect(() => {
    if (!selectedUnderlying || !selectedExchange) return

    const loadExpiries = async () => {
      try {
        const res = await buyerEdgeApi.getExpiries(selectedExchange, selectedUnderlying)
        if (res.status === 'success' && res.expiries.length > 0) {
          setExpiries(res.expiries)
          if (!selectedExpiry || !res.expiries.includes(selectedExpiry)) {
            setSelectedExpiry(res.expiries[0])
          }
        }
      } catch (err) {
        console.error('Failed to load expiries:', err)
      }
    }

    loadExpiries()
  }, [selectedUnderlying, selectedExchange])

  // ── Data Loaders ──────────────────────────────────────────────

  const loadData = useCallback(async () => {
    if (!selectedUnderlying || !selectedExpiry) return
    setIsLoading(true)
    try {
      const parsedManualStrike = (atmMode === 'manual' && manualStrike.trim() !== '')
        ? parseFloat(manualStrike)
        : undefined

      const res = await buyerEdgeApi.getUnifiedMonitor({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: convertExpiryForAPI(selectedExpiry),
        interval: straddleInterval,
        days: parseInt(straddleDays),
        lb_bars: parseInt(lbBars),
        lb_tf: lbTf,
        pcr_strike_window: 10,
        max_snapshot_strikes: parseInt(strikeCount),
        atm_mode: atmMode,
        manual_strike: isNaN(parsedManualStrike as any) ? undefined : parsedManualStrike,
      })
      if (res.status === 'success') {
        setData(res)
        // Reuse GEX and IVR data fetched in parallel by unified_monitor so the
        // display components (GexLevels, IvrDashboard) don't need a separate call
        // on every auto-refresh.  The separate loadGexData / loadIvDashboardData
        // callbacks are still used when the user explicitly changes mode or
        // triggers a manual refresh.
        if (res.gex?.status === 'success' && gexMode === 'selected') {
          setGexData(res.gex)
        }
        if (res.ivr?.status === 'success') {
          setIvData(res.ivr)
        }
      } else {
        showToast.error(res.message || 'Failed to load data')
      }
    } catch {
      showToast.error('Failed to load data')
    } finally {
      setIsLoading(false)
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange, straddleInterval, straddleDays, lbBars, lbTf, strikeCount, atmMode, manualStrike, gexMode])



  const loadGexData = useCallback(async () => {
    if (!selectedUnderlying) return
    setIsGexLoading(true)
    try {
      const expiryForAPI = convertExpiryForAPI(selectedExpiry)
      const res = await buyerEdgeApi.getGexLevels({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        mode: gexMode,
        expiry_date: gexMode === 'selected' ? expiryForAPI : undefined,
        expiry_dates: gexMode === 'cumulative' ? expiries.map(convertExpiryForAPI) : undefined,
        strike_count: 20,
      })
      if (res.status === 'success') {
        setGexData(res)
      } else {
        showToast.error(res.message || 'Failed to load GEX data')
      }
    } catch {
      showToast.error('Failed to load GEX data')
    } finally {
      setIsGexLoading(false)
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange, gexMode, expiries])

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
        showToast.error(res.message || 'Failed to load spot candle data')
      }
    } catch {
      showToast.error('Failed to load spot candle data')
    } finally {
      setIsSpotCandleLoading(false)
    }
  }, [selectedUnderlying, selectedExchange, spotChartInterval, spotChartDays])

  const refreshAll = useCallback(() => {
    // loadData fetches GEX (selected mode) and IVR in parallel via unified_monitor
    // and populates gexData / ivData from the response — no separate call needed
    // unless the user is in cumulative GEX mode.
    loadData()
    if (gexMode === 'cumulative') {
      loadGexData()
    }
    loadSpotCandleData()
  }, [loadData, loadGexData, loadSpotCandleData, gexMode])

  // Initial load
  useEffect(() => {
    if (selectedUnderlying && selectedExpiry) {
      refreshAll()
    }
  }, [selectedUnderlying, selectedExpiry])

  useEffect(() => {
    if (!autoRefresh || !isMarketOpen(selectedExchange)) return
    const timer = setInterval(refreshAll, AUTO_REFRESH_INTERVAL)
    return () => clearInterval(timer)
  }, [autoRefresh, selectedExchange, isMarketOpen, refreshAll])


  return (
    <div className="container mx-auto py-6 space-y-6 max-w-[1600px] animate-in fade-in duration-700">
      <Suspense fallback={<div className="h-20 flex items-center justify-center italic text-muted-foreground">Loading controls...</div>}>
        <Controls
          fnoExchanges={fnoExchanges}
          selectedExchange={selectedExchange}
          onExchangeChange={(ex) => {
            setSelectedExchange(ex)
            setUnderlyings(defaultUnderlyings[ex] || [])
            setSelectedUnderlying(defaultUnderlyings[ex]?.[0] || '')
          }}
          underlyings={underlyings}
          selectedUnderlying={selectedUnderlying}
          onUnderlyingChange={setSelectedUnderlying}
          underlyingOpen={underlyingOpen}
          setUnderlyingOpen={setUnderlyingOpen}
          expiries={expiries}
          selectedExpiry={selectedExpiry}
          onExpiryChange={setSelectedExpiry}
          lbBars={lbBars}
          setLbBars={setLbBars}
          lbTf={lbTf}
          setLbTf={setLbTf}
          strikeCount={strikeCount}
          setStrikeCount={setStrikeCount}
          atmMode={atmMode}
          setAtmMode={setAtmMode}
          manualStrike={manualStrike}
          setManualStrike={setManualStrike}
          isLoading={isLoading}
          onRefresh={refreshAll}
          autoRefresh={autoRefresh}
          setAutoRefresh={setAutoRefresh}
          isMarketOpen={isMarketOpen(selectedExchange)}
        />
      </Suspense>

      <Suspense fallback={null}>
        <InterpretationGuide
          isOpen={showInterpretation}
          onToggle={() => setShowInterpretation(!showInterpretation)}
        />
      </Suspense>

      <Suspense fallback={null}>
        <div className="space-y-6">
          <ScoreGauge
            signal={(data?.analysis as any)?.status === 'success' ? (data?.analysis as any).signal_engine : null}
            market={(data?.analysis as any)?.status === 'success' ? (data?.analysis as any).market_state : null}
            synthetic={(data?.analysis as any)?.status === 'success' ? (data?.analysis as any).synthetic_engine : null}
          />
          {(data?.analysis as any)?.straddle_engine && (
            <div className="space-y-4 animate-in fade-in slide-in-from-right-4 duration-500">
              <div className="p-4 rounded-xl border border-border/50 bg-muted/20 space-y-4">
                <BEBand
                  spot={(data?.analysis as any).straddle_engine.atm_strike || 0}
                  lower={(data?.analysis as any).straddle_engine.lower_be || 0}
                  upper={(data?.analysis as any).straddle_engine.upper_be || 0}
                />
                <DeltaBar
                  callDelta={(data?.analysis as any).greeks_engine?.total_call_delta || 0}
                  putDelta={(data?.analysis as any).greeks_engine?.total_put_delta || 0}
                />
              </div>
            </div>
          )}
        </div>
      </Suspense>

      <div className="grid grid-cols-1 gap-6">
        <Suspense fallback={<div className="h-[400px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading Premium Monitor...</div>}>
          <PremiumMonitor
            pcrData={data?.pcr || null}
            straddleData={data?.straddle?.data || null}
            isPcrLoading={isLoading}
            isChartLoading={isLoading}
            interval={straddleInterval}
            setInterval={setStraddleInterval}
            days={straddleDays}
            setDays={setStraddleDays}
            showStraddle={showStraddle}
            setShowStraddle={setShowStraddle}
            showSpot={showSpot}
            setShowSpot={setShowSpot}
            showSynthetic={showSynthetic}
            setShowSynthetic={setShowSynthetic}
            showVwap={showVwap}
            setShowVwap={setShowVwap}
            showPcrOi={showPcrOi}
            setShowPcrOi={setShowPcrOi}
            showPcrVol={showPcrVol}
            setShowPcrVol={setShowPcrVol}
            isOpen={straddleSectionOpen}
            onToggle={() => setStraddleSectionOpen(!straddleSectionOpen)}
            onShowInfo={() => setShowPremiumInfo(!showPremiumInfo)}
            isDarkMode={isDarkMode}
          />
        </Suspense>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <Suspense fallback={<div className="h-[300px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading OI Change...</div>}>
            <IntradayOiChange
              data={data}
              isLoading={isLoading}
              isOpen={oiChgSectionOpen}
              onToggle={() => setOiChgSectionOpen(!oiChgSectionOpen)}
              onShowInfo={() => setShowOiChgInfo(!showOiChgInfo)}
              viewMode={oiChgViewMode}
              setViewMode={setOiChgViewMode}
              isDarkMode={isDarkMode}
            />
          </Suspense>

          <Suspense fallback={<div className="h-[300px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading Breadth...</div>}>
            <MarketBreadth
              data={data}
              isLoading={isLoading}
              isOpen={breadthSectionOpen}
              onToggle={() => setBreadthSectionOpen(!breadthSectionOpen)}
              onShowInfo={() => setShowBreadthInfo(!showBreadthInfo)}
            />
          </Suspense>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <Suspense fallback={<div className="h-[300px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading GEX...</div>}>
            <GexLevels
              gexData={gexData}
              isLoading={isGexLoading}
              mode={gexMode}
              setMode={setGexMode}
              isOpen={gexSectionOpen}
              onToggle={() => setGexSectionOpen(!gexSectionOpen)}
            />
          </Suspense>

          <Suspense fallback={<div className="h-[300px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading IVR...</div>}>
            <IvrDashboard
              ivData={ivData}
              isLoading={isLoading}
              isOpen={ivSectionOpen}
              onToggle={() => setIvSectionOpen(!ivSectionOpen)}
            />
          </Suspense>
        </div>

        <Suspense fallback={<div className="h-[400px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading GEX Spot Chart...</div>}>
          <GexSpotChart
            gexData={gexData}
            spotCandleData={spotCandleData}
            isLoading={isSpotCandleLoading}
            interval={spotChartInterval}
            setInterval={setSpotChartInterval}
            days={spotChartDays}
            setDays={setSpotChartDays}
            isOpen={spotChartSectionOpen}
            onToggle={() => setSpotChartSectionOpen(!spotChartSectionOpen)}
            isDarkMode={isDarkMode}
          />
        </Suspense>
      </div>
    </div>
  )
}
