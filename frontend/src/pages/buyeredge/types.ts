import type { SignalType } from '@/api/buyerEdge'

export interface ScoreComponent {
  label: string
  score: number   // raw contribution (may be fractional)
  max: number     // maximum absolute contribution for this component
  note: string    // human-readable signal value
  direction: 'bullish' | 'bearish' | 'neutral'
}

export interface DirectionalScoreResult {
  /** Normalised score: -100 to +100 */
  pct: number
  /** Raw sum */
  raw: number
  /** Maximum possible absolute raw sum */
  maxRaw: number
  label: 'Strong Bullish' | 'Mild Bullish' | 'Neutral' | 'Mild Bearish' | 'Strong Bearish'
  color: string
  components: ScoreComponent[]
}

export interface StrikeOiChange {
  strike: number
  ce_oi_chg: number
  pe_oi_chg: number
}

export const AUTO_REFRESH_INTERVAL = 30000
export const STRADDLE_CHART_HEIGHT = 450
export const OI_CHANGE_CHART_HEIGHT = 300
export const SPOT_CHART_HEIGHT = 400
export const STRIKE_PROXIMITY_THRESHOLD = 50
export const IVX_BAR_WIDTH = 40
export const IVX_BAR_GAP = 16
export const IVX_CHART_HEIGHT = 200

export const SIGNAL_CONFIG: Record<
  SignalType,
  { label: string; bg: string; text: string; border: string; badgeClass: string }
> = {
  EXECUTE: {
    label: 'EXECUTE',
    bg: 'bg-green-500/10',
    text: 'text-green-600 dark:text-green-400',
    border: 'border-green-500/30',
    badgeClass: 'bg-green-500 text-white',
  },
  WATCH: {
    label: 'WATCH',
    bg: 'bg-yellow-500/10',
    text: 'text-yellow-600 dark:text-yellow-400',
    border: 'border-yellow-500/30',
    badgeClass: 'bg-yellow-500 text-black',
  },
  NO_TRADE: {
    label: 'NO TRADE',
    bg: 'bg-red-500/10',
    text: 'text-red-600 dark:text-red-400',
    border: 'border-red-500/30',
    badgeClass: 'bg-red-500 text-white',
  },
}
