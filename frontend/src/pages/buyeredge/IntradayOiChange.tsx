import { useEffect, useRef, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ChevronUp, ChevronDown, Info } from 'lucide-react'
import { createChart, ColorType, CrosshairMode, LineSeries } from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import type { StrikeOiChange } from '@/api/buyerEdge'
import { OI_CHANGE_CHART_HEIGHT } from './types'
import { formatOiLakh, getOiFlowMeta } from './utils'

interface IntradayOiChangeProps {
  data: any // BuyerEdgeResponse is too complex and inconsistent
  isLoading: boolean
  isOpen: boolean
  onToggle: () => void
  onShowInfo: () => void
  viewMode: 'line' | 'bar'
  setViewMode: (v: 'line' | 'bar') => void
  isDarkMode: boolean
}

export function IntradayOiChange({
  data,
  isLoading,
  isOpen,
  onToggle,
  onShowInfo,
  viewMode,
  setViewMode,
  isDarkMode
}: IntradayOiChangeProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const ceSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const peSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const tooltipRef = useRef<HTMLDivElement | null>(null)
  const [barHover, setBarHover] = useState<StrikeOiChange | null>(null)

  useEffect(() => {
    if (!isOpen || !chartContainerRef.current) return

    const chart = createChart(chartContainerRef.current, {
      height: OI_CHANGE_CHART_HEIGHT,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDarkMode ? '#94a3b8' : '#64748b',
        fontSize: 10,
      },
      grid: {
        vertLines: { color: isDarkMode ? '#33415544' : '#e2e8f044' },
        horzLines: { color: isDarkMode ? '#33415544' : '#e2e8f044' },
      },
      rightPriceScale: {
        borderVisible: false,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderVisible: false,
        visible: true,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { labelVisible: false },
      },
      handleScale: false,
      handleScroll: false,
    })

    const ceSeries = chart.addSeries(LineSeries, {
      color: '#22c55e',
      lineWidth: 2,
      title: 'Call OI Δ',
    })

    const peSeries = chart.addSeries(LineSeries, {
      color: '#ef4444',
      lineWidth: 2,
      title: 'Put OI Δ',
    })

    chartRef.current = chart
    ceSeriesRef.current = ceSeries
    peSeriesRef.current = peSeries

    // Handle tooltip/hover for line mode
    chart.subscribeCrosshairMove((param) => {
      if (viewMode === 'bar') return
      if (!param.point || !param.time || !tooltipRef.current) {
        if (tooltipRef.current) tooltipRef.current.style.display = 'none'
        return
      }

      const ceData = param.seriesData.get(ceSeries) as { value: number } | undefined
      const peData = param.seriesData.get(peSeries) as { value: number } | undefined

      if (ceData || peData) {
        tooltipRef.current.style.display = 'block'
        tooltipRef.current.style.left = param.point.x + 15 + 'px'
        tooltipRef.current.style.top = param.point.y + 15 + 'px'
        tooltipRef.current.innerHTML = `
          <div class="space-y-1">
            <div class="font-bold border-b border-border/50 pb-1 mb-1">${param.time}</div>
            <div class="flex items-center justify-between gap-4">
              <span class="text-green-500 font-bold">Call Δ:</span>
              <span>${formatOiLakh(ceData?.value || 0)}</span>
            </div>
            <div class="flex items-center justify-between gap-4">
              <span class="text-red-500 font-bold">Put Δ:</span>
              <span>${formatOiLakh(peData?.value || 0)}</span>
            </div>
          </div>
        `
      }
    })

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
  }, [isOpen, isDarkMode, viewMode])

  useEffect(() => {
    if (!data?.pcr?.data?.strike_oi_changes || !ceSeriesRef.current || !peSeriesRef.current || viewMode === 'bar') return

    const ceData = data.pcr.data.strike_oi_changes.map((s: any) => ({
      time: s.strike,
      value: s.ce_oi_chg
    })).sort((a: any, b: any) => a.time - b.time)

    const peData = data.pcr.data.strike_oi_changes.map((s: any) => ({
      time: s.strike,
      value: s.pe_oi_chg
    })).sort((a: any, b: any) => a.time - b.time)

    ceSeriesRef.current.setData(ceData)
    peSeriesRef.current.setData(peData)
    chartRef.current?.timeScale().fitContent()
  }, [data, viewMode])

  return (
    <Card>
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-bold flex items-center gap-2">
              Intraday OI Change (per Strike)
              <Info className="h-3.5 w-3.5 text-muted-foreground hover:text-primary transition-colors cursor-help"
                onClick={(e) => { e.stopPropagation(); onShowInfo() }} />
            </CardTitle>
            <div className="flex rounded-md overflow-hidden border border-border text-xs">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setViewMode('line') }}
                className={`px-3 py-1 transition-colors ${viewMode === 'line' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
              >
                Line
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setViewMode('bar') }}
                className={`px-3 py-1 transition-colors ${viewMode === 'bar' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
              >
                Bar
              </button>
            </div>
          </div>
          {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent>
          {isLoading ? (
            <div className="h-[300px] flex items-center justify-center text-muted-foreground animate-pulse italic">
              Building OI change profile...
            </div>
          ) : !data ? (
            <div className="h-[120px] flex items-center justify-center text-sm text-muted-foreground">
              Run an analysis to view OI distribution
            </div>
          ) : viewMode === 'line' ? (
            <div className="relative">
              <div ref={chartContainerRef} />
              <div
                ref={tooltipRef}
                className="absolute hidden z-50 bg-background/95 backdrop-blur-sm border border-border p-2 rounded-lg shadow-xl text-[11px] pointer-events-none min-w-[140px]"
              />
            </div>
          ) : (
            <div className="h-[300px] flex items-end gap-1 px-2 pb-8 pt-4 relative group">
              {data.pcr?.data?.strike_oi_changes && [...data.pcr.data.strike_oi_changes].sort((a: any, b: any) => a.strike - b.strike).map((s: any) => {
                const maxVal = Math.max(...data.pcr.data.strike_oi_changes.map((x: any) => Math.max(Math.abs(x.ce_oi_chg), Math.abs(x.pe_oi_chg))))
                const ceH = (Math.abs(s.ce_oi_chg) / maxVal) * 100
                const peH = (Math.abs(s.pe_oi_chg) / maxVal) * 100
                return (
                  <div
                    key={s.strike}
                    className="flex-1 flex flex-col justify-end gap-0.5 h-full relative"
                    onMouseEnter={() => setBarHover(s)}
                    onMouseLeave={() => setBarHover(null)}
                  >
                    <div className="flex-1 flex flex-col justify-end">
                      <div className="w-full bg-green-500/60 hover:bg-green-500 rounded-t-sm transition-all" style={{ height: `${ceH}%` }} />
                    </div>
                    <div className="flex-1 flex flex-col justify-start">
                      <div className="w-full bg-red-500/60 hover:bg-red-500 rounded-b-sm transition-all" style={{ height: `${peH}%` }} />
                    </div>
                    {barHover?.strike === s.strike && (() => {
                      const ceMeta = getOiFlowMeta('CE', s.ce_oi_chg, s.ce_ltp_chg)
                      const peMeta = getOiFlowMeta('PE', s.pe_oi_chg, s.pe_ltp_chg)
                      return (
                        <div className="absolute -top-24 left-1/2 -translate-x-1/2 z-50 bg-background border-2 border-primary/20 p-2 rounded-xl shadow-2xl whitespace-nowrap text-[10px] animate-in fade-in zoom-in-95 duration-200 min-w-[160px]">
                          <div className="font-black border-b border-border/50 pb-1 mb-2 text-center text-xs tracking-tighter">{s.strike}</div>
                          <div className="space-y-2">
                            <div className="flex flex-col gap-0.5">
                              <div className="flex justify-between items-center gap-4">
                                <span className="text-green-500 font-bold">Call Δ:</span>
                                <span className="font-mono font-bold">{formatOiLakh(s.ce_oi_chg)}</span>
                              </div>
                              {ceMeta && (
                                <div className="text-[9px] font-black uppercase px-1.5 py-0.5 rounded bg-muted/50 w-fit" style={{ color: ceMeta.color }}>
                                  {ceMeta.label}
                                </div>
                              )}
                            </div>
                            <div className="flex flex-col gap-0.5 border-t border-border/20 pt-1.5">
                              <div className="flex justify-between items-center gap-4">
                                <span className="text-red-500 font-bold">Put Δ:</span>
                                <span className="font-mono font-bold">{formatOiLakh(s.pe_oi_chg)}</span>
                              </div>
                              {peMeta && (
                                <div className="text-[9px] font-black uppercase px-1.5 py-0.5 rounded bg-muted/50 w-fit" style={{ color: peMeta.color }}>
                                  {peMeta.label}
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })()}
                  </div>
                )
              })}
              <div className="absolute bottom-2 left-0 right-0 flex justify-between px-2 text-[10px] font-bold text-muted-foreground/40 pointer-events-none">
                <span>{data.pcr?.data?.strike_oi_changes ? Math.min(...data.pcr.data.strike_oi_changes.map((s: any) => s.strike)) : 0}</span>
                <span>Strike Price</span>
                <span>{data.pcr?.data?.strike_oi_changes ? Math.max(...data.pcr.data.strike_oi_changes.map((s: any) => s.strike)) : 0}</span>
              </div>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}
