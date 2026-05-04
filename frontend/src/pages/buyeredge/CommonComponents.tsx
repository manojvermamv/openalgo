export function Pill({
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

export function BEBand({
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
        <div className="absolute inset-y-0 left-0 w-[10%] bg-red-400/40 rounded-l-full" />
        <div className="absolute inset-y-0 right-0 w-[10%] bg-red-400/40 rounded-r-full" />
        <div className="absolute inset-y-0 left-[10%] right-[10%] bg-green-400/20" />
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

export function DeltaBar({
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
          className="bg-green-500 transition-all duration-300"
          style={{ width: `${callPct}%` }}
        />
        <div
          className="bg-red-500 transition-all duration-300"
          style={{ width: `${putPct}%` }}
        />
      </div>
    </div>
  )
}
