import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { DirectionalScoreResult } from './types'

export function ScoreGauge({ result }: { result: DirectionalScoreResult }) {
  const needlePct = Math.min(100, Math.max(0, ((result.pct + 100) / 200) * 100))
  return (
    <Card className="border-2" style={{ borderColor: result.color + '55' }}>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Directional Conviction Score</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
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

        <div className="space-y-1">
          <div className="relative h-4 rounded-full overflow-hidden"
            style={{ background: 'linear-gradient(to right, #ef4444 0%, #fb923c 25%, #fbbf24 50%, #86efac 75%, #22c55e 100%)' }}>
            <div className="absolute top-0 bottom-0 w-0.5 bg-background/70 left-1/2" />
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

        <div className="grid grid-cols-1 2xl:grid-cols-2 gap-x-8 gap-y-1.5 pt-1">
          {result.components.map((c) => {
            const barPct = Math.round((Math.abs(c.score) / c.max) * 100)
            const barColor = c.direction === 'bullish' ? '#22c55e' : c.direction === 'bearish' ? '#ef4444' : '#fbbf24'
            const scoreStr = c.score > 0 ? `+${c.score}` : c.score === 0 ? '0' : `${c.score}`
            return (
              <div key={c.label} className="flex items-center gap-2 py-0.5 min-w-0">
                <span className="text-[10px] text-muted-foreground w-[90px] shrink-0 truncate" title={c.label}>{c.label}</span>
                <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden min-w-[30px]">
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
                  className="text-[10px] font-bold w-7 text-right shrink-0"
                  style={{ color: barColor }}
                >
                  {scoreStr}
                </span>
                <span className="text-[10px] text-muted-foreground max-w-[100px] truncate hidden xl:block" title={c.note}>{c.note}</span>
              </div>
            )
          })}
        </div>
      </CardContent>
    </Card>
  )
}
