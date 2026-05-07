import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { TrendingUp, TrendingDown, Minus, Activity, Target, Compass, Info, AlertTriangle } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { SignalEngine, MarketState, SyntheticEngine } from '@/api/buyerEdge'
import { SIGNAL_CONFIG } from './types'
import { cn } from '@/lib/utils'

interface ScoreGaugeProps {
  signal: SignalEngine | null
  market: MarketState | null
  synthetic?: SyntheticEngine | null
}

export function ScoreGauge({ signal, market, synthetic }: ScoreGaugeProps) {
  if (!signal || !market) {
    return (
      <Card className="border-primary/10 bg-muted/5 shadow-none overflow-hidden h-full flex flex-col justify-center items-center text-center p-6 border-dashed">
        <Compass className="h-10 w-10 text-muted-foreground/20 mb-3 animate-pulse" />
        <div className="text-xs font-bold text-muted-foreground uppercase tracking-widest">Waiting for Signal</div>
        <div className="text-[10px] text-muted-foreground/60 mt-1">Calculating institutional convictions...</div>
      </Card>
    )
  }

  let score = signal.score
  if (!Number.isFinite(score)) score = 0

  const needlePct = Math.min(100, Math.max(0, ((score + 100) / 200) * 100))
  const config = SIGNAL_CONFIG[signal.signal]

  const getScoreColor = (val: number) => {
    if (val > 30) return '#22c55e'
    if (val < -30) return '#ef4444'
    return '#fbbf24'
  }

  const scoreColor = getScoreColor(score)

  return (
    <Card className={cn("border-2 transition-all duration-500 overflow-hidden", config.bg, config.border)}>
      <CardContent className="p-6">
        <div className="flex flex-col md:flex-row gap-8 items-center">

          {/* Signal Indicator */}
          <div className="flex flex-col items-center gap-2 shrink-0">
            <span className="text-[10px] font-black uppercase tracking-[0.2em] text-muted-foreground">Action Signal</span>
            <div className={cn("px-8 py-3 rounded-2xl border-2 font-black text-2xl tracking-tighter shadow-xl transition-all duration-500",
              signal.signal === 'EXECUTE' ? "bg-green-500 border-green-400 text-white shadow-green-500/20" :
                signal.signal === 'WATCH' ? "bg-yellow-500 border-yellow-400 text-black shadow-yellow-500/20" :
                  "bg-red-500 border-red-400 text-white shadow-red-500/20"
            )}>
              {signal.signal}
            </div>
            <div className="flex items-center gap-1.5 mt-1 relative group">
              <span className="text-xs font-bold" style={{ color: scoreColor }}>{signal.label}</span>
              <span className="text-[10px] text-muted-foreground uppercase font-bold tracking-wider opacity-60">Conviction</span>

              <Popover>
                <PopoverTrigger asChild>
                  <button className="p-1 hover:bg-muted rounded-full transition-colors">
                    <Info className="h-3 w-3 text-muted-foreground" />
                  </button>
                </PopoverTrigger>
                <PopoverContent className="w-80 p-4 bg-background/95 backdrop-blur-md border-border/50 shadow-2xl">
                  <div className="space-y-3">
                    <div className="flex justify-between items-center border-b border-border/20 pb-2 mb-2">
                      <span className="text-xs font-black uppercase tracking-widest">Intelligence Bias Breakdown</span>
                      <Badge variant="outline" className="text-[9px]">{signal.label}</Badge>
                    </div>

                    <div className="space-y-2.5 max-h-[300px] overflow-y-auto pr-1">
                      {(signal.components || []).map((c) => {
                        const barPct = Math.round((Math.abs(c.score) / c.max) * 100)
                        const barColor = c.direction === 'bullish' ? '#22c55e' : c.direction === 'bearish' ? '#ef4444' : '#fbbf24'
                        const scoreStr = c.score > 0 ? `+${c.score}` : c.score === 0 ? '0' : `${c.score}`

                        return (
                          <div key={c.label} className="space-y-1">
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex flex-col">
                                <span className="text-[10px] font-bold text-foreground leading-none">{c.label}</span>
                                <span className="text-[8px] text-muted-foreground mt-0.5 tracking-tighter uppercase">{c.note}</span>
                              </div>
                              <span className="text-[10px] font-black" style={{ color: barColor }}>{scoreStr}</span>
                            </div>
                            <div className="h-1.5 w-full rounded-full bg-muted/50 overflow-hidden border border-border/10">
                              <div
                                className="h-full rounded-full transition-all duration-700 ease-out shadow-[0_0_8px_rgba(0,0,0,0.1)]"
                                style={{
                                  width: `${barPct}%`,
                                  background: barColor,
                                  marginLeft: c.score < 0 ? 'auto' : undefined,
                                }}
                              />
                            </div>
                          </div>
                        )
                      })}
                    </div>
                    <div className="pt-2 mt-2 border-t border-border/20 flex justify-between items-center">
                      <span className="text-[9px] font-bold text-muted-foreground uppercase">Net Calculated Score</span>
                      <span className="text-sm font-black" style={{ color: scoreColor }}>{score > 0 ? '+' : ''}{score}</span>
                    </div>
                  </div>
                </PopoverContent>
              </Popover>
            </div>

            {/* Trap Score Warning Badge */}
            {signal.trap_score !== undefined && signal.trap_score > 0 && (
              <Popover>
                <PopoverTrigger asChild>
                  <button
                    className={cn(
                      "flex items-center gap-1.5 px-3 py-1.5 rounded-xl border font-bold text-[11px] transition-all duration-300 cursor-pointer",
                      signal.trap_score > 75
                        ? "bg-red-500/15 border-red-500/40 text-red-500 shadow-red-500/10 shadow-md"
                        : signal.trap_score > 50
                        ? "bg-orange-500/15 border-orange-500/40 text-orange-500 shadow-orange-500/10 shadow-sm"
                        : "bg-yellow-500/10 border-yellow-500/30 text-yellow-600 dark:text-yellow-400"
                    )}
                  >
                    <AlertTriangle className="h-3 w-3" />
                    <span>Trap Risk {signal.trap_score}</span>
                  </button>
                </PopoverTrigger>
                <PopoverContent className="w-72 p-4 bg-background/95 backdrop-blur-md border-border/50 shadow-2xl">
                  <div className="space-y-3">
                    <div className="flex items-center gap-2 border-b border-border/20 pb-2">
                      <AlertTriangle className={cn("h-4 w-4",
                        signal.trap_score > 75 ? "text-red-500" :
                        signal.trap_score > 50 ? "text-orange-500" : "text-yellow-500"
                      )} />
                      <span className="text-xs font-black uppercase tracking-widest">
                        {signal.trap_score > 75 ? 'High' : signal.trap_score > 50 ? 'Elevated' : 'Moderate'} Trap Risk
                      </span>
                      <Badge variant="outline" className={cn("ml-auto text-[9px]",
                        signal.trap_score > 75 ? "border-red-500/50 text-red-500" :
                        signal.trap_score > 50 ? "border-orange-500/50 text-orange-500" : ""
                      )}>
                        {signal.trap_score}/100
                      </Badge>
                    </div>
                    <div className="space-y-2">
                      {(signal.trap_reasons || []).length > 0
                        ? signal.trap_reasons!.map((r, i) => (
                            <div key={i} className="flex items-start gap-2 text-[11px] text-muted-foreground">
                              <span className="mt-0.5 shrink-0">•</span>
                              <span>{r}</span>
                            </div>
                          ))
                        : <p className="text-[11px] text-muted-foreground">Active trap conditions detected.</p>
                      }
                    </div>
                    <p className="text-[10px] text-muted-foreground/60 border-t border-border/10 pt-2">
                      {signal.trap_score > 75
                        ? 'Consider avoiding naked options. Use spreads or stand aside.'
                        : signal.trap_score > 50
                        ? 'Exercise caution. Consider reduced size or debit spreads.'
                        : 'Monitor conditions. Keep stops tight.'}
                    </p>
                  </div>
                </PopoverContent>
              </Popover>
            )}
          </div>

          {/* Gauge and Market State */}
          <div className="flex-1 w-full space-y-6">

            {/* Market State Badges */}
            <div className="flex flex-wrap gap-3">
              <div className="flex flex-col gap-1">
                <span className="text-[9px] font-black uppercase tracking-widest text-muted-foreground px-1">Market Trend</span>
                <Badge variant="outline" className="bg-background/50 border-border/50 backdrop-blur-sm px-3 py-1.5 flex items-center gap-2">
                  {market.trend === 'Bullish' ? <TrendingUp className="h-3 w-3 text-green-500" /> :
                    market.trend === 'Bearish' ? <TrendingDown className="h-3 w-3 text-red-500" /> :
                      <Minus className="h-3 w-3 text-yellow-500" />}
                  <span className="font-bold text-xs">{market.trend}</span>
                </Badge>
              </div>

              <div className="flex flex-col gap-1">
                <span className="text-[9px] font-black uppercase tracking-widest text-muted-foreground px-1">Regime</span>
                <Badge variant="outline" className="bg-background/50 border-border/50 backdrop-blur-sm px-3 py-1.5 flex items-center gap-2">
                  <Activity className="h-3 w-3 text-blue-500" />
                  <span className="font-bold text-xs">{market.regime}</span>
                </Badge>
              </div>

              <div className="flex flex-col gap-1">
                <span className="text-[9px] font-black uppercase tracking-widest text-muted-foreground px-1">Structure</span>
                <Badge variant="outline" className="bg-background/50 border-border/50 backdrop-blur-sm px-3 py-1.5 flex items-center gap-2">
                  <Target className="h-3 w-3 text-purple-500" />
                  <span className="font-bold text-xs">{market.location}</span>
                </Badge>
              </div>
            </div>

            {/* The Gauge */}
            <div className="space-y-3">
              <div className="flex justify-between items-end">
                <div className="flex items-baseline gap-2">
                  <span className="text-4xl font-black tracking-tighter" style={{ color: scoreColor }}>
                    {score > 0 ? '+' : ''}{score}
                  </span>
                  <span className="text-[10px] font-black uppercase tracking-widest text-muted-foreground">Points</span>
                </div>
                <div className="flex gap-4 text-[10px] font-bold uppercase tracking-widest opacity-40">
                  <span>-100 BEAR</span>
                  <span>NEUTRAL</span>
                  <span>BULL +100</span>
                </div>
              </div>

              <div className="relative h-4 rounded-full overflow-hidden bg-muted/20 border border-border/20 shadow-inner">
                <div className="absolute inset-0 bg-gradient-to-r from-[#ef4444] via-[#fbbf24] to-[#22c55e] opacity-80" />
                <div className="absolute top-0 bottom-0 w-0.5 bg-white/40 left-1/2" />

                {/* Needle */}
                <div
                  className="absolute top-[-2px] bottom-[-2px] w-1.5 bg-white shadow-[0_0_15px_rgba(255,255,255,0.8)] border border-black/10 z-10 rounded-full transition-all duration-700 ease-out"
                  style={{ left: `calc(${needlePct}% - 3px)` }}
                />
              </div>
            </div>

            {/* Modular Bias Scores */}
            {signal.bias_scores && (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-4 border-t border-border/10">
                {[
                  { label: 'Trend', val: signal.bias_scores.market },
                  { label: 'OI Flow', val: signal.bias_scores.oi },
                  { label: 'Greeks', val: signal.bias_scores.greeks },
                  { label: 'Premium', val: signal.bias_scores.straddle },
                ].map((b) => (
                  <div key={b.label} className="space-y-1">
                    <div className="flex justify-between items-center px-0.5">
                      <span className="text-[8px] font-black uppercase text-muted-foreground/60 tracking-widest">{b.label}</span>
                    </div>
                    <div className={cn(
                      "text-[11px] font-black px-2 py-1 rounded-lg border flex items-center justify-between shadow-sm transition-all duration-300",
                      b.val > 0 ? "bg-green-500/5 border-green-500/20 text-green-500" :
                        b.val < 0 ? "bg-red-500/5 border-red-500/20 text-red-500" :
                          "bg-muted/30 border-border/50 text-muted-foreground"
                    )}>
                      <span>{b.val > 0 ? '+' : ''}{b.val}</span>
                      <div className={cn("h-1 w-1 rounded-full shadow-[0_0_5px_currentColor]",
                        b.val > 0 ? "bg-green-500" : b.val < 0 ? "bg-red-500" : "bg-muted-foreground/40")}
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Reasons / Insights */}
            <div className="flex flex-wrap gap-2 pt-2">
              <span className="text-[9px] font-black uppercase tracking-widest text-muted-foreground mr-1 flex items-center gap-1">
                <Compass className="h-3 w-3" /> Insights:
              </span>
              {signal.reasons.map((r, i) => (
                <span key={i} className="text-[10px] font-bold bg-background/40 px-2 py-0.5 rounded-md border border-border/20 text-foreground/80 hover:bg-background/60 transition-colors">
                  {r}
                </span>
              ))}
            </div>

            {/* Compact Synthetic Future Block */}
            {synthetic && synthetic.liquidity_status !== 'invalid' && (
              <div className="pt-3 mt-1 border-t border-border/10">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[9px] font-black uppercase tracking-widest text-muted-foreground">
                    Synthetic Future
                  </span>
                  <Badge
                    variant="outline"
                    className={cn(
                      "text-[9px] font-bold px-2 py-0.5",
                      synthetic.liquidity_status === 'good'
                        ? "border-green-500/30 text-green-500"
                        : synthetic.liquidity_status === 'wide'
                        ? "border-orange-500/30 text-orange-500"
                        : "border-muted-foreground/30 text-muted-foreground"
                    )}
                  >
                    {synthetic.liquidity_status === 'good' ? '● Liquid'
                      : synthetic.liquidity_status === 'wide' ? '⚠ Wide Spread'
                      : synthetic.liquidity_status === 'ltp_only' ? '~ LTP only'
                      : 'Invalid'}
                  </Badge>
                </div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[10px]">
                  {synthetic.synthetic_mid != null && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Mid</span>
                      <span className="font-bold">{synthetic.synthetic_mid.toFixed(1)}</span>
                    </div>
                  )}
                  {synthetic.basis_mid != null ? (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Basis</span>
                      <span className="flex items-center gap-1">
                        <span className={cn("font-bold",
                          synthetic.basis_mid > 0 ? "text-foreground/80" : synthetic.basis_mid < 0 ? "text-amber-400" : "text-muted-foreground"
                        )}>
                          {synthetic.basis_mid > 0 ? '+' : ''}{synthetic.basis_mid.toFixed(1)}
                        </span>
                        {synthetic.basis_state && (
                          <span className={cn("text-[8px] px-1 rounded",
                            synthetic.basis_state === 'backwardation'
                              ? "bg-amber-500/10 text-amber-400"
                              : "bg-muted/30 text-muted-foreground"
                          )}>
                            {synthetic.basis_state}
                          </span>
                        )}
                      </span>
                    </div>
                  ) : synthetic.basis_ltp != null && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Basis(LTP)</span>
                      <span className="flex items-center gap-1">
                        <span className={cn("font-bold",
                          synthetic.basis_ltp > 0 ? "text-foreground/80" : synthetic.basis_ltp < 0 ? "text-amber-400" : "text-muted-foreground"
                        )}>
                          {synthetic.basis_ltp > 0 ? '+' : ''}{synthetic.basis_ltp.toFixed(1)}
                        </span>
                        {synthetic.basis_state && (
                          <span className={cn("text-[8px] px-1 rounded",
                            synthetic.basis_state === 'backwardation'
                              ? "bg-amber-500/10 text-amber-400"
                              : "bg-muted/30 text-muted-foreground"
                          )}>
                            {synthetic.basis_state}
                          </span>
                        )}
                      </span>
                    </div>
                  )}
                  {synthetic.spread_pct != null && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Spread</span>
                      <span className={cn("font-bold",
                        synthetic.spread_pct > 0.5 ? "text-orange-400" : "text-foreground/70"
                      )}>
                        {synthetic.spread_pct.toFixed(2)}%
                      </span>
                    </div>
                  )}
                  {synthetic.ltp_inside_market != null && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">LTP in Mkt</span>
                      <span className={cn("font-bold text-[9px]",
                        synthetic.ltp_inside_market ? "text-green-400" : "text-orange-400"
                      )}>
                        {synthetic.ltp_inside_market ? '✓ Yes' : '⚠ Outside'}
                      </span>
                    </div>
                  )}
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Pressure</span>
                    <span className={cn("font-bold capitalize",
                      synthetic.pressure === 'bullish' ? "text-green-400"
                      : synthetic.pressure === 'bearish' ? "text-red-400"
                      : "text-muted-foreground"
                    )}>
                      {synthetic.pressure}
                    </span>
                  </div>
                </div>
                <div className={cn(
                  "mt-2 text-[9px] px-2 py-1 rounded-md border",
                  synthetic.confirmation === 'confirming'
                    ? "bg-green-500/5 border-green-500/20 text-green-400"
                    : synthetic.confirmation === 'diverging'
                    ? "bg-red-500/5 border-red-500/20 text-red-400"
                    : "bg-muted/20 border-border/20 text-muted-foreground"
                )}>
                  {synthetic.confirmation === 'confirming'
                    ? `✓ Confirming ${synthetic.pressure}`
                    : synthetic.confirmation === 'diverging'
                    ? `⚠ ${synthetic.reason}`
                    : synthetic.reason}
                </div>
              </div>
            )}
          </div>

        </div>
      </CardContent>
    </Card>
  )
}
