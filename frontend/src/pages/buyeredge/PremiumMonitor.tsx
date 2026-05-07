import { useEffect, useRef } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ChevronUp, ChevronDown, Info, RefreshCw } from 'lucide-react'
import { createChart, ColorType, CrosshairMode, LineSeries } from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import type { PcrChartResponse } from '@/api/buyerEdge'
import type { StraddleChartData } from '@/api/straddle-chart'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { computeDayAnchoredVWAP } from './utils'
import { STRADDLE_CHART_HEIGHT } from './types'

interface PremiumMonitorProps {
  pcrData: PcrChartResponse | null
  straddleData: StraddleChartData | null
  isPcrLoading: boolean
  isChartLoading: boolean
  interval: string
  setInterval: (v: string) => void
  days: string
  setDays: (v: string) => void
  showStraddle: boolean
  setShowStraddle: (v: boolean) => void
  showSpot: boolean
  setShowSpot: (v: boolean) => void
  showSynthetic: boolean
  setShowSynthetic: (v: boolean) => void
  showVwap: boolean
  setShowVwap: (v: boolean) => void
  showPcrOi: boolean
  setShowPcrOi: (v: boolean) => void
  showPcrVol: boolean
  setShowPcrVol: (v: boolean) => void
  isOpen: boolean
  onToggle: () => void
  onShowInfo: () => void
  isDarkMode: boolean
}

export function PremiumMonitor({
  pcrData,
  straddleData,
  isPcrLoading,
  isChartLoading,
  interval,
  setInterval,
  days,
  setDays,
  showStraddle,
  setShowStraddle,
  showSpot,
  setShowSpot,
  showSynthetic,
  setShowSynthetic,
  showVwap,
  setShowVwap,
  showPcrOi,
  setShowPcrOi,
  showPcrVol,
  setShowPcrVol,
  isOpen,
  onToggle,
  onShowInfo,
  isDarkMode
}: PremiumMonitorProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)

  // Series refs
  const straddleSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const spotSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const syntheticSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const vwapSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrOiSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const pcrVolSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)

  useEffect(() => {
    if (!isOpen || !chartContainerRef.current) return

    const chart = createChart(chartContainerRef.current, {
      height: STRADDLE_CHART_HEIGHT,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDarkMode ? '#94a3b8' : '#64748b',
        fontSize: 10,
      },
      grid: {
        vertLines: { color: isDarkMode ? '#33415522' : '#e2e8f022' },
        horzLines: { color: isDarkMode ? '#33415522' : '#e2e8f022' },
      },
      rightPriceScale: {
        borderVisible: false,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      leftPriceScale: {
        visible: true,
        borderVisible: false,
        scaleMargins: { top: 0.2, bottom: 0.2 },
      },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
    })

    // Straddle / Price Series (Right Axis)
    straddleSeriesRef.current = chart.addSeries(LineSeries, {
      color: '#3b82f6',
      lineWidth: 2,
      priceScaleId: 'right',
      title: 'Straddle',
    })

    spotSeriesRef.current = chart.addSeries(LineSeries, {
      color: isDarkMode ? '#94a3b8' : '#64748b',
      lineWidth: 1,
      lineStyle: 2,
      priceScaleId: 'right',
      title: 'Spot',
    })

    syntheticSeriesRef.current = chart.addSeries(LineSeries, {
      color: '#f59e0b',
      lineWidth: 1,
      priceScaleId: 'right',
      title: 'Synthetic',
    })

    vwapSeriesRef.current = chart.addSeries(LineSeries, {
      color: '#8b5cf6',
      lineWidth: 1,
      lineStyle: 1,
      priceScaleId: 'right',
      title: 'VWAP(D)',
    })

    // PCR Series (Left Axis)
    pcrOiSeriesRef.current = chart.addSeries(LineSeries, {
      color: '#10b981',
      lineWidth: 2,
      priceScaleId: 'left',
      title: 'PCR(OI)',
    })

    pcrVolSeriesRef.current = chart.addSeries(LineSeries, {
      color: '#06b6d4',
      lineWidth: 2,
      lineStyle: 2,
      priceScaleId: 'left',
      title: 'PCR(Vol)',
    })

    chartRef.current = chart

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth })
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
    }
  }, [isOpen, isDarkMode])

  // Sync Visibilities
  useEffect(() => {
    straddleSeriesRef.current?.applyOptions({ visible: showStraddle })
    spotSeriesRef.current?.applyOptions({ visible: showSpot })
    syntheticSeriesRef.current?.applyOptions({ visible: showSynthetic })
    vwapSeriesRef.current?.applyOptions({ visible: showVwap })
    pcrOiSeriesRef.current?.applyOptions({ visible: showPcrOi })
    pcrVolSeriesRef.current?.applyOptions({ visible: showPcrVol })
  }, [showStraddle, showSpot, showSynthetic, showVwap, showPcrOi, showPcrVol])

  // Sync Data
  useEffect(() => {
    if (!straddleData?.series || !straddleSeriesRef.current) return

    const points = [...straddleData.series].sort((a, b) => a.time - b.time)
    straddleSeriesRef.current.setData(points.map(p => ({ time: p.time as any, value: p.straddle })))
    spotSeriesRef.current?.setData(points.map(p => ({ time: p.time as any, value: p.spot })))

    // Compute and set VWAP
    const vwapPoints = computeDayAnchoredVWAP(points)
    vwapSeriesRef.current?.setData(vwapPoints.map(p => ({ time: p.time as any, value: p.value })))

    chartRef.current?.timeScale().fitContent()
  }, [straddleData])

  useEffect(() => {
    if (!pcrData?.data?.series || !pcrOiSeriesRef.current || !pcrVolSeriesRef.current) return

    const series = [...pcrData.data.series].sort((a, b) => a.time - b.time)
    pcrOiSeriesRef.current.setData(series.map(p => ({ time: p.time as any, value: p.pcr_oi || 0 })))
    pcrVolSeriesRef.current.setData(series.map(p => ({ time: p.time as any, value: p.pcr_volume || 0 })))
    syntheticSeriesRef.current?.setData(series.map(p => ({ time: p.time as any, value: p.synthetic_future || 0 })))

    chartRef.current?.timeScale().fitContent()
  }, [pcrData])

  const latestPcr = pcrData?.data?.series?.[pcrData.data.series.length - 1]

  return (
    <Card>
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-bold flex items-center gap-2">
              Options Premium Monitor
              <Info className="h-3.5 w-3.5 text-muted-foreground hover:text-primary transition-colors cursor-help"
                onClick={(e) => { e.stopPropagation(); onShowInfo() }} />
            </CardTitle>
            <div className="flex items-center gap-1">
              <Select value={interval} onValueChange={setInterval}>
                <SelectTrigger className="h-7 w-[70px] text-[10px] font-bold uppercase">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['1m', '3m', '5m', '10m', '15m', '30m', '1h'].map(v => (
                    <SelectItem key={v} value={v}>{v}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={days} onValueChange={setDays}>
                <SelectTrigger className="h-7 w-[60px] text-[10px] font-bold uppercase">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['1', '3', '5'].map(v => (
                    <SelectItem key={v} value={v}>{v}D</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {latestPcr && (
              <Badge variant="outline" className="text-[10px] ml-2">
                PCR(OI): {latestPcr.pcr_oi?.toFixed(2)} ·
                PCR(Vol): {latestPcr.pcr_volume?.toFixed(2)}
              </Badge>
            )}
          </div>
          {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent>
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3 mb-2">
                <button
                  onClick={() => setShowStraddle(!showStraddle)}
                  className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${showStraddle ? 'bg-blue-500/10 border-blue-500 text-blue-500' : 'bg-muted border-border text-muted-foreground'}`}
                >
                  STRADDLE
                </button>
                <button
                  onClick={() => setShowSpot(!showSpot)}
                  className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${showSpot ? 'bg-slate-500/10 border-slate-500 text-slate-500' : 'bg-muted border-border text-muted-foreground'}`}
                >
                  SPOT
                </button>
                <button
                  onClick={() => setShowSynthetic(!showSynthetic)}
                  className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${showSynthetic ? 'bg-amber-500/10 border-amber-500 text-amber-500' : 'bg-muted border-border text-muted-foreground'}`}
                >
                  SYNTHETIC
                </button>
                <button
                  onClick={() => setShowVwap(!showVwap)}
                  className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${showVwap ? 'bg-violet-500/10 border-violet-500 text-violet-500' : 'bg-muted border-border text-muted-foreground'}`}
                >
                  VWAP(D)
                </button>
                <button
                  onClick={() => setShowPcrOi(!showPcrOi)}
                  className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${showPcrOi ? 'bg-emerald-500/10 border-emerald-500 text-emerald-500' : 'bg-muted border-border text-muted-foreground'}`}
                >
                  PCR(OI)
                </button>
                <button
                  onClick={() => setShowPcrVol(!showPcrVol)}
                  className={`px-2 py-1 rounded text-[10px] font-bold border transition-colors ${showPcrVol ? 'bg-cyan-500/10 border-cyan-500 text-cyan-500' : 'bg-muted border-border text-muted-foreground'}`}
                >
                  PCR(VOL)
                </button>
              </div>

              <div className="relative">
                {(isChartLoading || isPcrLoading) && (
                  <div className="absolute inset-0 z-10 bg-background/50 backdrop-blur-[1px] flex items-center justify-center">
                    <RefreshCw className="h-6 w-6 animate-spin text-primary" />
                  </div>
                )}
                <div ref={chartContainerRef} />
                <div className="absolute top-2 left-2 text-[9px] font-bold text-muted-foreground/40 uppercase tracking-widest pointer-events-none">
                  Left Axis: PCR · Right Axis: Premium/Price
                </div>
              </div>
            </div>
          </CardContent>
      )}
    </Card>
  )
}
