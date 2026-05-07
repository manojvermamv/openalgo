import { type StraddleDataPoint } from '@/api/straddle-chart'

export function convertExpiryForAPI(expiry: string): string {
  if (!expiry) return ''
  const parts = expiry.split('-')
  if (parts.length === 3) {
    return `${parts[0]}${parts[1].toUpperCase()}${parts[2].slice(-2)}`
  }
  return expiry.replace(/-/g, '').toUpperCase()
}

export function formatIST(unixSeconds: number): { date: string; time: string } {
  const d = new Date(unixSeconds * 1000)
  const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000)
  const dd = ist.getUTCDate().toString().padStart(2, '0')
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const mo = months[ist.getUTCMonth()]
  const hh = ist.getUTCHours().toString().padStart(2, '0')
  const mm = ist.getUTCMinutes().toString().padStart(2, '0')
  const ampm = ist.getUTCHours() >= 12 ? 'PM' : 'AM'
  return { date: `${dd} ${mo}`, time: `${hh}:${mm} ${ampm}` }
}

export function getAdrMeta(adr: number): { label: string; color: string } {
  if (adr >= 1.5) return { label: 'Strong Bull', color: '#22c55e' }
  if (adr >= 1.0) return { label: 'Bullish', color: '#86efac' }
  if (adr >= 0.9) return { label: 'Sideways', color: '#fbbf24' }
  if (adr >= 0.7) return { label: 'Bearish', color: '#fb923c' }
  return { label: 'Strong Bear', color: '#ef4444' }
}

export function getOiFlowMeta(
  leg: 'CE' | 'PE',
  oiDelta: number,
  premiumDelta: number | null,
): { label: string; color: string; note: string } | null {
  if (oiDelta === 0) return null
  const premUp: boolean | null =
    premiumDelta !== null ? (Math.abs(premiumDelta) >= 0.5 ? premiumDelta > 0 : null) : null

  if (leg === 'CE') {
    if (oiDelta > 0) {
      if (premUp === true) return { label: 'Call Buying', color: '#22c55e', note: 'CE Prem↑ + OI↑ → real call demand (Bullish)' }
      if (premUp === false) return { label: 'Call Writing', color: '#ef4444', note: 'CE Prem↓ + OI↑ → writers adding supply (Bearish ceiling)' }
      return { label: 'CE OI Rising', color: '#86efac', note: 'CE OI↑ — confirm with ATM CE premium direction' }
    }
    if (premUp === true) return { label: 'CE Short Covering', color: '#86efac', note: 'CE Prem↑ + OI↓ → call shorts exiting (Mild Bullish)' }
    if (premUp === false) return { label: 'CE Long Unwinding', color: '#fb923c', note: 'CE Prem↓ + OI↓ → call longs exiting (Bearish)' }
    return { label: 'CE OI Falling', color: '#fb923c', note: 'CE OI↓ — confirm with ATM CE premium direction' }
  }

  if (oiDelta > 0) {
    if (premUp === true) return { label: 'Put Buying', color: '#ef4444', note: 'PE Prem↑ + OI↑ → real put demand (Bearish hedge)' }
    if (premUp === false) return { label: 'Put Writing', color: '#22c55e', note: 'PE Prem↓ + OI↑ → writers building floor (Bullish support)' }
    return { label: 'PE OI Rising', color: '#f59e0b', note: 'PE OI↑ — confirm with ATM PE premium direction' }
  }
  // PE OI falling (OI↓):
  // OI↓ + prem↑ → PE Short Covering: put shorts buying back → mildly bullish
  // OI↓ + prem↓ → PE Long Unwinding: put longs closing positions → mildly bearish
  if (premUp === true) return { label: 'PE Short Covering', color: '#86efac', note: 'PE Prem↑ + OI↓ → put shorts buying back (Mildly Bullish)' }
  if (premUp === false) return { label: 'PE Long Unwinding', color: '#fb923c', note: 'PE Prem↓ + OI↓ → put longs exiting (Mildly Bearish)' }
  return { label: 'PE OI Falling', color: '#86efac', note: 'PE OI↓ — confirm with ATM PE premium direction' }
}

export function getOiRegimeMeta(
  ceDelta: number,
  peDelta: number,
): { label: string; color: string } {
  if (ceDelta > 0 && peDelta > 0) return { label: 'Dual OI Expansion', color: '#f59e0b' }
  if (ceDelta < 0 && peDelta < 0) return { label: 'OI Convergence', color: '#818cf8' }
  if (ceDelta > 0) return { label: 'CE Dominant', color: '#ef4444' }
  return { label: 'PE Dominant', color: '#22c55e' }
}

export function getBreadthBias(
  ceAdvances: number,
  peAdvances: number,
): { bias: number; label: string; color: string } | null {
  if (ceAdvances + peAdvances === 0) return null
  if (ceAdvances === 0) return { bias: 99, label: 'PE Dominated', color: '#ef4444' }
  const bias = peAdvances / ceAdvances
  if (bias >= 1.5) return { bias, label: 'Bearish Breadth', color: '#ef4444' }
  if (bias >= 1.2) return { bias, label: 'Mild Bearish Breadth', color: '#fb923c' }
  if (bias <= 0.67) return { bias, label: 'Bullish Breadth', color: '#22c55e' }
  if (bias <= 0.83) return { bias, label: 'Mild Bullish Breadth', color: '#86efac' }
  return { bias, label: 'Neutral Breadth', color: '#fbbf24' }
}

export function getMoneyness(strike: number, spot: number): string {
  const atmBand = spot * 0.003
  if (Math.abs(strike - spot) <= atmBand) return 'ATM'
  return strike > spot ? 'OTM-CE / ITM-PE' : 'ITM-CE / OTM-PE'
}

export function formatOiLakh(v: number): string {
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)}L`
  return `${sign}${abs.toLocaleString('en-IN')}`
}

export function computeDayAnchoredVWAP(points: StraddleDataPoint[]): { time: number; value: number }[] {
  if (!points.length) return []
  const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000
  let dayKey = ''
  let cumSum = 0
  let cumCount = 0
  return points.map((p) => {
    const istDate = new Date(p.time * 1000 + IST_OFFSET_MS)
    const key = `${istDate.getUTCFullYear()}-${istDate.getUTCMonth()}-${istDate.getUTCDate()}`
    if (key !== dayKey) {
      dayKey = key
      cumSum = 0
      cumCount = 0
    }
    cumSum += p.straddle
    cumCount += 1
    return { time: p.time, value: cumSum / cumCount }
  })
}

export function pillVariant(value: string): 'bullish' | 'bearish' | 'neutral' | 'warning' | 'expansion' {
  const v = value.toLowerCase()
  if (v.includes('bullish') || v.includes('rising') || v.includes('expanding') || v === 'breakout')
    return 'bullish'
  if (v.includes('bearish') || v.includes('falling') || v.includes('contracting') || v.includes('range low'))
    return 'bearish'
  if (v.includes('expansion')) return 'expansion'
  if (v.includes('watch') || v.includes('range high')) return 'warning'
  return 'neutral'
}
