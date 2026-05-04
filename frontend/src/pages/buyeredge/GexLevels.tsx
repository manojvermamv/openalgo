import { useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ChevronUp, ChevronDown } from 'lucide-react'
import type { GexLevelsResponse } from '@/api/buyerEdge'
import { formatOiLakh } from './utils'

interface GexLevelsProps {
  gexData: GexLevelsResponse | null
  isLoading: boolean
  mode: 'selected' | 'cumulative'
  setMode: (m: 'selected' | 'cumulative') => void
  isOpen: boolean
  onToggle: () => void
}

export function GexLevels({
  gexData,
  isLoading,
  mode,
  setMode,
  isOpen,
  onToggle,
}: GexLevelsProps) {
  const [lineHover, setLineHover] = useState<{ strike: number; net_gex: number } | null>(null)
  return (
    <Card>
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-bold text-primary">Advanced GEX Levels</CardTitle>
            {gexData && (
              <div className="flex items-center gap-1">
                <Badge variant="outline" className="text-[10px] uppercase font-bold h-5">
                  {mode === 'cumulative' ? '∑ Cumulative' : '⊙ Selected'}
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
            <div className="flex rounded-md overflow-hidden border border-border text-[10px] font-bold h-7">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setMode('selected') }}
                className={`px-3 transition-colors ${mode === 'selected' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
              >
                Selected
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setMode('cumulative') }}
                className={`px-3 transition-colors ${mode === 'cumulative' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
              >
                Cumulative
              </button>
            </div>
            {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
          </div>
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent>
          {isLoading ? (
            <div className="h-[200px] flex items-center justify-center text-muted-foreground animate-pulse italic text-sm">
              Analyzing gamma exposure profile...
            </div>
          ) : !gexData ? (
            <div className="h-[120px] flex items-center justify-center text-sm text-muted-foreground">
              Run an analysis to view GEX distribution
            </div>
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                <div className="p-3 rounded-lg border border-border/50 bg-muted/20">
                  <div className="text-[9px] text-muted-foreground uppercase font-black mb-1">Gamma Flip</div>
                  <div className="text-sm font-black text-indigo-500">{gexData.levels.gamma_flip?.toFixed(0) || 'N/A'}</div>
                </div>
                <div className="p-3 rounded-lg border border-border/50 bg-muted/20">
                  <div className="text-[9px] text-muted-foreground uppercase font-black mb-1">Call Wall</div>
                  <div className="text-sm font-black text-green-500">{gexData.levels.call_gamma_wall?.toFixed(0) || 'N/A'}</div>
                </div>
                <div className="p-3 rounded-lg border border-border/50 bg-muted/20">
                  <div className="text-[9px] text-muted-foreground uppercase font-black mb-1">Put Wall</div>
                  <div className="text-sm font-black text-red-500">{gexData.levels.put_gamma_wall?.toFixed(0) || 'N/A'}</div>
                </div>
                <div className="p-3 rounded-lg border border-border/50 bg-muted/20">
                  <div className="text-[9px] text-muted-foreground uppercase font-black mb-1">Total Net GEX</div>
                  <div className={`text-sm font-black ${gexData.levels.total_net_gex > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatOiLakh(gexData.levels.total_net_gex)}
                  </div>
                </div>
              </div>

              {/* GEX Distribution Chart (CSS Bars) */}
              <div className="h-[200px] flex items-end gap-1 px-1 pb-6 relative group border-b border-border/20">
                {gexData.chain.map((item) => {
                  const maxGex = Math.max(...gexData.chain.map(i => Math.abs(i.net_gex)))
                  const h = (Math.abs(item.net_gex) / maxGex) * 100
                  const isPositive = item.net_gex >= 0
                  const isHovered = lineHover?.strike === item.strike

                  return (
                    <div
                      key={item.strike}
                      className="flex-1 flex flex-col justify-end h-full relative"
                      onMouseEnter={() => setLineHover({ strike: item.strike, net_gex: item.net_gex })}
                      onMouseLeave={() => setLineHover(null)}
                    >
                      <div
                        className={`w-full rounded-t-sm transition-all duration-300 ${isPositive ? 'bg-green-500' : 'bg-red-500'} ${isHovered ? 'opacity-100 ring-2 ring-primary ring-offset-1 ring-offset-background z-10' : 'opacity-60 hover:opacity-100'}`}
                        style={{ height: `${h}%` }}
                      />
                      {isHovered && (
                        <div className="absolute -top-12 left-1/2 -translate-x-1/2 z-50 bg-background border border-border p-1.5 rounded shadow-xl whitespace-nowrap text-[10px] animate-in fade-in zoom-in-95 duration-200">
                          <div className="font-bold border-b border-border/50 pb-1 mb-1">{item.strike}</div>
                          <div className={`font-bold ${isPositive ? 'text-green-500' : 'text-red-500'}`}>
                            {formatOiLakh(item.net_gex)}
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
                <div className="absolute bottom-2 left-0 right-0 flex justify-between px-2 text-[9px] font-black text-muted-foreground/30 uppercase tracking-widest pointer-events-none">
                  <span>{gexData.chain[0]?.strike}</span>
                  <span>Strike Distribution</span>
                  <span>{gexData.chain[gexData.chain.length - 1]?.strike}</span>
                </div>
              </div>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}
