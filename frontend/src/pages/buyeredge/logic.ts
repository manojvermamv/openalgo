import type { PcrDataPoint } from '@/api/buyerEdge'
import type { DirectionalScoreResult, ScoreComponent } from './types'
import { getOiFlowMeta, getBreadthBias } from './utils'

export function computeDirectionalScore(params: {
  pcrSeries: PcrDataPoint[]
  pcrOi: number | null
  pcrOiChg: number | null
  deltaImbalance: number | null
  trend: string | null
}): DirectionalScoreResult {
  const { pcrSeries, pcrOi, pcrOiChg, deltaImbalance, trend } = params
  const components: ScoreComponent[] = []
  let raw = 0
  const MAX_RAW = 12

  function push(c: ScoreComponent) {
    components.push(c)
    raw += c.score
  }
  function dir(s: number): 'bullish' | 'bearish' | 'neutral' {
    return s > 0 ? 'bullish' : s < 0 ? 'bearish' : 'neutral'
  }

  const hasSeries = pcrSeries.length >= 2
  const latest = hasSeries ? pcrSeries[pcrSeries.length - 1] : null
  const prevBar = hasSeries ? pcrSeries[pcrSeries.length - 2] : null
  const first = hasSeries ? pcrSeries[0] : null

  // 1. Spot short-term
  {
    const s = latest && prevBar ? (latest.spot > prevBar.spot ? 1 : latest.spot < prevBar.spot ? -1 : 0) : 0
    push({ label: 'Spot (short-term)', score: s, max: 1, note: s > 0 ? 'Spot ↑ vs prev bar' : s < 0 ? 'Spot ↓ vs prev bar' : 'Spot flat', direction: dir(s) })
  }

  // 2. Spot session
  {
    const s = latest && first ? (latest.spot > first.spot ? 1 : latest.spot < first.spot ? -1 : 0) : 0
    push({ label: 'Spot (session)', score: s, max: 1, note: s > 0 ? 'Spot ↑ vs open' : s < 0 ? 'Spot ↓ vs open' : 'Spot at open', direction: dir(s) })
  }

  // 3. Spot vs VWAP
  {
    let spotVwap: number | null = null
    if (hasSeries && latest) {
      const sum = pcrSeries.reduce((acc, p) => acc + p.spot, 0)
      spotVwap = sum / pcrSeries.length
    }
    const s = latest && spotVwap !== null ? (latest.spot > spotVwap ? 1 : latest.spot < spotVwap ? -1 : 0) : 0
    push({ label: 'Spot vs VWAP', score: s, max: 1, note: spotVwap !== null ? `Spot ${s > 0 ? '↑' : s < 0 ? '↓' : '='} VWAP ${spotVwap.toFixed(1)}` : '—', direction: dir(s) })
  }

  // 4. PCR OI Level
  {
    let s = 0, note = '—'
    if (pcrOi !== null) {
      if (pcrOi >= 1.2) { s = 1; note = `PCR OI ${pcrOi.toFixed(2)} (Strong put writing)` }
      else if (pcrOi >= 1.0) { s = 0.5; note = `PCR OI ${pcrOi.toFixed(2)} (Moderate bullish)` }
      else if (pcrOi <= 0.6) { s = -1; note = `PCR OI ${pcrOi.toFixed(2)} (Strong call writing)` }
      else if (pcrOi <= 0.8) { s = -0.5; note = `PCR OI ${pcrOi.toFixed(2)} (Moderate bearish)` }
      else { s = 0; note = `PCR OI ${pcrOi.toFixed(2)} (Neutral)` }
    }
    push({ label: 'PCR OI Level', score: s, max: 1, note, direction: dir(s) })
  }

  // 5. PCR OI Change
  {
    let s = 0, note = '—'
    if (pcrOiChg !== null) {
      if (pcrOiChg >= 1.2) { s = 1; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Intraday put build-up)` }
      else if (pcrOiChg >= 0.9) { s = 0.5; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Moderate build-up)` }
      else if (pcrOiChg <= 0.5) { s = -1; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Intraday call build-up)` }
      else if (pcrOiChg <= 0.7) { s = -0.5; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Moderate call build-up)` }
      else { s = 0; note = `PCR Δ ${pcrOiChg.toFixed(2)} (Neutral intraday)` }
    }
    push({ label: 'PCR OI Change', score: s, max: 1, note, direction: dir(s) })
  }

  // 6. CE Flow
  {
    let s = 0, note = '—'
    if (latest && first) {
      const ceOiDelta = latest.ce_oi - first.ce_oi
      const cePremDelta = latest.atm_ce_ltp - first.atm_ce_ltp
      const meta = getOiFlowMeta('CE', ceOiDelta, cePremDelta)
      if (meta) {
        note = meta.label
        switch (meta.label) {
          case 'Call Buying': s = 2; break
          case 'CE Short Covering': s = 1; break
          case 'CE Long Unwinding': s = -1; break
          case 'Call Writing': s = -2; break
        }
      }
    }
    push({ label: 'CE Flow', score: s, max: 2, note, direction: dir(s) })
  }

  // 7. PE Flow
  {
    let s = 0, note = '—'
    if (latest && first) {
      const peOiDelta = latest.pe_oi - first.pe_oi
      const pePremDelta = latest.atm_pe_ltp - first.atm_pe_ltp
      const meta = getOiFlowMeta('PE', peOiDelta, pePremDelta)
      if (meta) {
        note = meta.label
        switch (meta.label) {
          case 'Put Writing': s = 2; break
          case 'PE Long Unwinding': s = 1; break
          case 'PE Short Covering': s = -1; break
          case 'Put Buying': s = -2; break
        }
      }
    }
    push({ label: 'PE Flow', score: s, max: 2, note, direction: dir(s) })
  }

  // 8. Breadth Bias
  {
    let s = 0, note = '—'
    if (latest) {
      const bb = getBreadthBias(latest.ce_advances, latest.pe_advances)
      if (bb) {
        note = bb.label
        switch (bb.label) {
          case 'Bullish Breadth': s = 1; break
          case 'Mild Bullish Breadth': s = 0.5; break
          case 'Neutral Breadth': s = 0; break
          case 'Mild Bearish Breadth': s = -0.5; break
          case 'Bearish Breadth':
          case 'PE Dominated': s = -1; break
        }
      }
    }
    push({ label: 'Breadth Bias', score: s, max: 1, note, direction: dir(s) })
  }

  // 9. Delta Imbalance
  {
    let s = 0, note = '—'
    if (deltaImbalance !== null) {
      if (deltaImbalance >= 0.1) { s = 1; note = `Δ imbalance +${deltaImbalance.toFixed(3)} (Call delta dominant)` }
      else if (deltaImbalance >= 0.05) { s = 0.5; note = `Δ imbalance +${deltaImbalance.toFixed(3)} (Mild call bias)` }
      else if (deltaImbalance <= -0.1) { s = -1; note = `Δ imbalance ${deltaImbalance.toFixed(3)} (Put delta dominant)` }
      else if (deltaImbalance <= -0.05) { s = -0.5; note = `Δ imbalance ${deltaImbalance.toFixed(3)} (Mild put bias)` }
      else { s = 0; note = `Δ imbalance ${deltaImbalance.toFixed(3)} (Balanced)` }
    }
    push({ label: 'Delta Imbalance', score: s, max: 1, note, direction: dir(s) })
  }

  // 10. Market Trend
  {
    let s = 0, note = trend ?? '—'
    if (trend === 'Bullish') s = 1
    if (trend === 'Bearish') s = -1
    push({ label: 'Market Trend', score: s, max: 1, note, direction: dir(s) })
  }

  const pct = Math.round((raw / MAX_RAW) * 100)
  let label: DirectionalScoreResult['label'], color: string
  if (pct >= 50) { label = 'Strong Bullish'; color = '#22c55e' }
  else if (pct >= 20) { label = 'Mild Bullish'; color = '#86efac' }
  else if (pct <= -50) { label = 'Strong Bearish'; color = '#ef4444' }
  else if (pct <= -20) { label = 'Mild Bearish'; color = '#fb923c' }
  else { label = 'Neutral'; color = '#fbbf24' }

  return { pct, raw, maxRaw: MAX_RAW, label, color, components }
}
