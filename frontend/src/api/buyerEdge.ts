import { webClient } from './client'

export interface MarketState {
  trend: 'Bullish' | 'Bearish' | 'Neutral'
  regime: 'Expansion' | 'Compression' | 'Neutral'
  location: 'Range High' | 'Mid' | 'Range Low' | 'Breakout'
}

export interface OIIntelligence {
  call_wall: number
  put_wall: number
  oi_migrating: boolean
  migration_direction: string
  oi_roll_detected: boolean
  current_pcr_oi?: number | null
  current_pcr_oi_chg?: number | null
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
export type DataMode = 'realtime' | 'live_snapshot' | 'last_day_fallback' | 'spot_only'

export interface ScoreComponent {
  label: string
  score: number
  max: number
  direction: 'bullish' | 'bearish' | 'neutral'
  note: string
}

export interface SignalEngine {
  signal: SignalType
  score: number
  label: string
  reasons: string[]
  data_mode?: DataMode
  components?: ScoreComponent[]
  bias_scores?: {
    market: number
    oi: number
    greeks: number
    straddle: number
  }
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

// ---------------------------------------------------------------------------
// GEX Levels
// ---------------------------------------------------------------------------

export interface GexStrike {
  strike: number
  ce_oi: number
  pe_oi: number
  ce_gex: number
  pe_gex: number
  net_gex: number
}

export interface GexLevels {
  gamma_flip: number | null
  hvl: number | null
  call_gamma_wall: number | null
  put_gamma_wall: number | null
  absolute_wall: number | null
  total_net_gex: number
  zero_gamma: number | null
}

export interface GexPerExpiry {
  expiry_date: string
  chain: GexStrike[]
  levels: GexLevels
}

export interface GexLevelsResponse {
  status: 'success' | 'error'
  message?: string
  mode: 'selected' | 'cumulative'
  underlying: string
  expiry_date?: string
  spot_price: number
  expiries_used: string[]
  chain: GexStrike[]
  levels: GexLevels
  per_expiry?: GexPerExpiry[]
  data_mode?: 'realtime' | 'last_day_fallback'
}

// ---------------------------------------------------------------------------
// PCR Chart
// ---------------------------------------------------------------------------

export interface PcrDataPoint {
  time: number
  spot: number
  atm_strike: number
  pcr_oi: number | null
  pcr_volume: number | null
  synthetic_future: number | null
  advances: number
  declines: number
  neutral: number
  adr: number | null
  ce_oi: number
  pe_oi: number
  atm_ce_ltp: number
  atm_pe_ltp: number
  ce_advances: number
  ce_declines: number
  pe_advances: number
  pe_declines: number
}

export interface StrikeOiChange {
  strike: number
  ce_oi_chg: number
  pe_oi_chg: number
}

export interface PcrChartData {
  underlying: string
  underlying_ltp: number
  expiry_date: string
  interval: string
  live_only: boolean
  current_pcr_oi: number | null
  current_pcr_volume: number | null
  current_adr: number | null
  current_pcr_oi_chg: number | null
  strike_oi_changes?: StrikeOiChange[]
  series: PcrDataPoint[]
}

export interface PcrChartResponse {
  status: 'success' | 'error'
  message?: string
  data?: PcrChartData
}

// ---------------------------------------------------------------------------
// IV Dashboard
// ---------------------------------------------------------------------------

export interface IvExpiryMetrics {
  expiry_date: string
  dte: number
  atm_iv: number | null
  ivx: number | null
  call_iv_otm: number | null
  put_iv_otm: number | null
  vertical_skew: number | null
  vertical_skew_pct: number | null
  horizontal_ivx_skew: number | null
  horizontal_ivx_skew_pct: number | null
  expected_move: number | null
  sigma_1: number | null
}

export interface IvDashboardResponse {
  status: 'success' | 'error'
  message?: string
  underlying?: string
  spot_price?: number
  iv_rank?: number | null
  ivr_available?: boolean
  current_iv?: number | null
  avg_ivx?: number | null
  iv_change_pct?: number | null
  expiries?: IvExpiryMetrics[]
}

// ---------------------------------------------------------------------------
// Spot Candles (for GEX Spot Chart)
// ---------------------------------------------------------------------------

export interface SpotCandle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface SpotCandleResponse {
  status: 'success' | 'error'
  message?: string
  underlying?: string
  exchange?: string
  candles?: SpotCandle[]
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export const buyerEdgeApi = {
  getData: async (params: {
    underlying: string
    exchange: string
    expiry_date: string
    strike_count?: number
    lb_bars?: number
    lb_tf?: string
    atm_mode?: 'auto' | 'manual'
    manual_strike?: number
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

  getGexLevels: async (params: {
    underlying: string
    exchange: string
    mode: 'selected' | 'cumulative'
    expiry_date?: string
    expiry_dates?: string[]
    strike_count?: number
  }): Promise<GexLevelsResponse> => {
    const response = await webClient.post<GexLevelsResponse>('/buyeredge/api/gex_levels', params)
    return response.data
  },

  getPcrChart: async (params: {
    underlying: string
    exchange: string
    expiry_date: string
    interval: string
    days?: number
  }): Promise<PcrChartResponse> => {
    const response = await webClient.post<PcrChartResponse>('/buyeredge/api/pcr_chart', params)
    return response.data
  },

  getIvDashboard: async (params: {
    underlying: string
    exchange: string
    expiry_dates: string[]
    strike_count?: number
  }): Promise<IvDashboardResponse> => {
    const response = await webClient.post<IvDashboardResponse>('/buyeredge/api/iv_dashboard', params)
    return response.data
  },

  getSpotCandles: async (params: {
    underlying: string
    exchange: string
    interval: string
    days?: number
  }): Promise<SpotCandleResponse> => {
    const response = await webClient.post<SpotCandleResponse>('/buyeredge/api/spot_candles', params)
    return response.data
  },

  getUnifiedMonitor: async (params: {
    underlying: string
    exchange: string
    expiry_date: string
    interval: string
    days?: number
    lb_bars?: number
    lb_tf?: string
    atm_mode?: 'auto' | 'manual'
    manual_strike?: number
    pcr_strike_window?: number
    max_snapshot_strikes?: number
  }): Promise<UnifiedMonitorResponse> => {
    const response = await webClient.post<UnifiedMonitorResponse>('/buyeredge/api/unified_monitor', params)
    return response.data
  },
}

export interface UnifiedMonitorResponse {
  status: 'success' | 'error'
  message?: string
  straddle: import('./straddle-chart').StraddleChartResponse
  pcr: PcrChartResponse
  analysis: BuyerEdgeResponse | null
}
