import { webClient } from './client'

export interface MarketState {
  trend: 'Bullish' | 'Bearish' | 'Neutral'
  regime: 'Expansion' | 'Compression'
  location: 'Range High' | 'Mid' | 'Range Low' | 'Breakout'
}

export interface OIIntelligence {
  call_wall: number
  put_wall: number
  oi_migrating: boolean
  migration_direction: string
  oi_roll_detected: boolean
}

export interface GreeksEngine {
  total_call_delta: number
  total_put_delta: number
  delta_imbalance: number
  delta_velocity: 'Rising' | 'Falling' | 'Stable'
  gamma_regime: 'Mean-Reversion' | 'Expansion'
  net_gamma: number
}

export interface StraddleEngine {
  atm_strike: number
  atm_ce_ltp: number
  atm_pe_ltp: number
  straddle_price: number
  straddle_velocity: 'Expanding' | 'Contracting' | 'Flat'
  upper_be: number
  lower_be: number
  be_distance_pct: number
  oi_roll_detected: boolean
}

export type SignalType = 'NO_TRADE' | 'WATCH' | 'EXECUTE'

export interface SignalEngine {
  signal: SignalType
  confidence: number
  reasons: string[]
}

export interface BuyerEdgeResponse {
  status: 'success' | 'error'
  message?: string
  underlying?: string
  expiry_date?: string
  spot?: number
  timestamp?: string
  market_state?: MarketState
  oi_intelligence?: OIIntelligence
  greeks_engine?: GreeksEngine
  straddle_engine?: StraddleEngine
  signal_engine?: SignalEngine
}

export interface UnderlyingsResponse {
  status: 'success' | 'error'
  underlyings: string[]
}

export interface ExpiriesResponse {
  status: 'success' | 'error'
  expiries: string[]
}

export const buyerEdgeApi = {
  getData: async (params: {
    underlying: string
    exchange: string
    expiry_date: string
    strike_count?: number
    lb_bars?: number
    lb_tf?: string
  }): Promise<BuyerEdgeResponse> => {
    const response = await webClient.post<BuyerEdgeResponse>('/buyeredge/api/data', params)
    return response.data
  },

  getUnderlyings: async (exchange: string): Promise<UnderlyingsResponse> => {
    const response = await webClient.get<UnderlyingsResponse>(
      `/search/api/underlyings?exchange=${exchange}`
    )
    return response.data
  },

  getExpiries: async (exchange: string, underlying: string): Promise<ExpiriesResponse> => {
    const response = await webClient.get<ExpiriesResponse>(
      `/search/api/expiries?exchange=${exchange}&underlying=${underlying}`
    )
    return response.data
  },
}
