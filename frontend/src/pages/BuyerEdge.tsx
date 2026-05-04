import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react'
import { useSupportedExchanges } from '@/hooks/useSupportedExchanges'
import { useThemeStore } from '@/stores/themeStore'
import { useAuthStore } from '@/stores/authStore'
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

// Logic and Utils
import { AUTO_REFRESH_INTERVAL } from './buyeredge/types'
import { convertExpiryForAPI } from './buyeredge/utils'
import { computeDirectionalScore } from './buyeredge/logic'

export default function BuyerEdge() {
  const { fnoExchanges, defaultFnoExchange, defaultUnderlyings } = useSupportedExchanges()
  const { mode } = useThemeStore()
  const { apiKey } = useAuthStore()
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
  const [isIvLoading, setIsIvLoading] = useState(false)
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

  const convictionScore = useMemo(() => {
    return computeDirectionalScore({
      pcrSeries: data?.pcr?.data?.series || [],
      pcrOi: data?.analysis?.oi_intelligence?.current_pcr_oi || data?.pcr?.data?.current_pcr_oi || null,
      pcrOiChg: data?.analysis?.oi_intelligence?.current_pcr_oi_chg || data?.pcr?.data?.current_pcr_oi_chg || null,
      deltaImbalance: data?.analysis?.greeks_engine?.delta_imbalance || null,
      trend: data?.analysis?.market_state?.trend || null,
    })
  }, [data])

  // ── Data Loaders ──────────────────────────────────────────────

  const loadData = useCallback(async () => {
    if (!selectedUnderlying || !selectedExpiry || !apiKey) return
    setIsLoading(true)
    try {
      const res = await buyerEdgeApi.getUnifiedMonitor({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_date: selectedExpiry,
        interval: straddleInterval,
        days: parseInt(straddleDays),
        lb_bars: parseInt(lbBars),
        lb_tf: lbTf,
        pcr_strike_window: 10,
        max_snapshot_strikes: parseInt(strikeCount),
      })
      if (res.status === 'success') {
        setData(res)
      }
    } catch (err: any) {
      console.error('PCR error:', err)
    } finally {
      setIsLoading(false) // shared loading for unified monitor
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange, straddleInterval, straddleDays, lbBars, lbTf, strikeCount, apiKey])



  const loadGexData = useCallback(async () => {
    if (!selectedUnderlying || !apiKey) return
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
      }
    } catch (err: any) {
      console.error('GEX error:', err)
    } finally {
      setIsGexLoading(false)
    }
  }, [selectedUnderlying, selectedExpiry, selectedExchange, gexMode, expiries, apiKey])

  const loadIvDashboardData = useCallback(async () => {
    if (!selectedUnderlying || !apiKey || expiries.length === 0) return
    setIsIvLoading(true)
    try {
      const res = await buyerEdgeApi.getIvDashboard({
        underlying: selectedUnderlying,
        exchange: selectedExchange,
        expiry_dates: expiries.slice(0, 4).map(convertExpiryForAPI),
        strike_count: 10,
      })
      if (res.status === 'success') {
        setIvData(res)
      }
    } catch (err: any) {
      console.error('IVR error:', err)
    } finally {
      setIsIvLoading(false)
    }
  }, [selectedUnderlying, selectedExchange, expiries, apiKey])

  const loadSpotCandleData = useCallback(async () => {
    if (!selectedUnderlying || !apiKey) return
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
      }
    } catch (err: any) {
      console.error('Spot candle error:', err)
    } finally {
      setIsSpotCandleLoading(false)
    }
  }, [selectedUnderlying, selectedExchange, spotChartInterval, spotChartDays, apiKey])

  const refreshAll = useCallback(() => {
    loadData()
    loadGexData()
    loadIvDashboardData()
    loadSpotCandleData()
  }, [loadData, loadGexData, loadIvDashboardData, loadSpotCandleData])

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

      <div className="grid grid-cols-1 gap-6">
        <Suspense fallback={<div className="h-[400px] flex items-center justify-center border rounded-lg animate-pulse bg-muted/20">Loading Premium Monitor...</div>}>
          <PremiumMonitor
            data={data?.analysis}
            pcrData={data?.pcr || null}
            straddleData={data?.straddle?.data || null}
            convictionScore={convictionScore}
            isLoading={isLoading}
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
              isLoading={isIvLoading}
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
