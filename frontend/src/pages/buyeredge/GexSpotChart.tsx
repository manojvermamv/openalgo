import { useEffect, useRef } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ChevronUp, ChevronDown, RefreshCw } from 'lucide-react'
import { createChart, ColorType, CrosshairMode, CandlestickSeries, LineSeries } from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import type { GexLevelsResponse, SpotCandleResponse } from '@/api/buyerEdge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SPOT_CHART_HEIGHT } from './types'

interface GexSpotChartProps {
  gexData: GexLevelsResponse | null
  spotCandleData: SpotCandleResponse | null
  isLoading: boolean
  interval: string
  setInterval: (v: string) => void
  days: string
  setDays: (v: string) => void
  isOpen: boolean
  onToggle: () => void
  isDarkMode: boolean
}

export function GexSpotChart({
  gexData,
  spotCandleData,
  isLoading,
  interval,
  setInterval,
  days,
  setDays,
  isOpen,
  onToggle,
  isDarkMode
}: GexSpotChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  
  // GEX Level series
  const gammaFlipRef = useRef<ISeriesApi<'Line'> | null>(null)
  const callWallRef = useRef<ISeriesApi<'Line'> | null>(null)
  const putWallRef = useRef<ISeriesApi<'Line'> | null>(null)

  useEffect(() => {
    if (!isOpen || !chartContainerRef.current) return

    const chart = createChart(chartContainerRef.current, {
      height: SPOT_CHART_HEIGHT,
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
      timeScale: {
        borderVisible: false,
        timeVisible: true,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
    })

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    })

    gammaFlipRef.current = chart.addSeries(LineSeries, {
      color: isDarkMode ? '#818cf8' : '#6366f1',
      lineWidth: 2,
      lineStyle: 2, // Dashed
      title: 'Gamma Flip',
      lastValueVisible: true,
      priceLineVisible: true,
    })

    callWallRef.current = chart.addSeries(LineSeries, {
      color: '#22c55e',
      lineWidth: 1,
      lineStyle: 3, // Dotted
      title: 'Call Wall',
    })

    putWallRef.current = chart.addSeries(LineSeries, {
      color: '#ef4444',
      lineWidth: 1,
      lineStyle: 3, // Dotted
      title: 'Put Wall',
    })

    chartRef.current = chart
    candleSeriesRef.current = candleSeries

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

  // Sync Data
  useEffect(() => {
    if (!spotCandleData?.candles || !candleSeriesRef.current) return
    const formatted = spotCandleData.candles.map((c: any) => ({
      time: c.time as any,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    })).sort((a: any, b: any) => (a.time as any) - (b.time as any))
    
    candleSeriesRef.current.setData(formatted)
    
    // Sync GEX Levels as horizontal lines across the chart
    if (gexData?.levels && formatted.length > 0) {
      const firstTime = formatted[0].time
      const lastTime = formatted[formatted.length - 1].time
      
      const setLevel = (series: ISeriesApi<'Line'> | null, price: number | null) => {
        if (!series || price === null) {
          series?.setData([])
          return
        }
        series.setData([
          { time: firstTime, value: price },
          { time: lastTime, value: price },
        ])
      }

      setLevel(gammaFlipRef.current, gexData.levels.gamma_flip)
      setLevel(callWallRef.current, gexData.levels.call_gamma_wall)
      setLevel(putWallRef.current, gexData.levels.put_gamma_wall)
    }

    chartRef.current?.timeScale().fitContent()
  }, [spotCandleData, gexData])

  return (
    <Card>
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-bold flex items-center gap-2">
              Spot Price & GEX Walls
            </CardTitle>
            <div className="flex items-center gap-1">
              <Select value={interval} onValueChange={setInterval}>
                <SelectTrigger className="h-7 w-[70px] text-[10px] font-bold uppercase">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['1m', '3m', '5m', '15m', '30m', '1h'].map(v => (
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
          </div>
          {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent>
          <div className="relative">
            {isLoading && (
              <div className="absolute inset-0 z-10 bg-background/50 backdrop-blur-[1px] flex items-center justify-center">
                <RefreshCw className="h-6 w-6 animate-spin text-primary" />
              </div>
            )}
            <div ref={chartContainerRef} />
            <div className="absolute top-2 left-2 flex gap-4 text-[9px] font-bold uppercase tracking-wider pointer-events-none">
              <div className="flex items-center gap-1.5">
                <div className="w-3 h-0.5 border-t-2 border-dashed border-indigo-500" />
                <span className="text-indigo-500">Gamma Flip</span>
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-3 h-0.5 border-t-2 border-dotted border-green-500" />
                <span className="text-green-500">Call Wall</span>
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-3 h-0.5 border-t-2 border-dotted border-red-500" />
                <span className="text-red-500">Put Wall</span>
              </div>
            </div>
          </div>
        </CardContent>
      )}
    </Card>
  )
}
