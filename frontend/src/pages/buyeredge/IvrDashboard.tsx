import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ChevronUp, ChevronDown } from 'lucide-react'
import type { IvDashboardResponse } from '@/api/buyerEdge'

interface IvrDashboardProps {
  ivData: IvDashboardResponse | null
  isLoading: boolean
  isOpen: boolean
  onToggle: () => void
}

export function IvrDashboard({
  ivData,
  isLoading,
  isOpen,
  onToggle
}: IvrDashboardProps) {
  return (
    <Card>
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-bold">IV Rank & Skew Profiler</CardTitle>
            {ivData && (
              <Badge variant={ivData.iv_rank !== undefined && ivData.iv_rank !== null && ivData.iv_rank > 50 ? 'destructive' : 'secondary'}>
                IVR: {ivData.iv_rank !== undefined && ivData.iv_rank !== null ? ivData.iv_rank.toFixed(1) : 'N/A'}
              </Badge>
            )}
          </div>
          {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent>
          {isLoading ? (
            <div className="h-[200px] flex items-center justify-center text-muted-foreground animate-pulse italic">
              Analyzing IV term structure...
            </div>
          ) : !ivData ? (
            <div className="h-[120px] flex items-center justify-center text-sm text-muted-foreground">
              Run an analysis to view IV metrics
            </div>
          ) : (
            <div className="space-y-6">
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                <div className="p-3 rounded-lg bg-muted/30 border border-border/50">
                  <div className="text-[10px] text-muted-foreground uppercase font-bold mb-1">IV Rank</div>
                  <span className={`text-xl font-black ${ivData.iv_rank !== undefined && ivData.iv_rank !== null && ivData.iv_rank > 50 ? 'text-red-500' : 'text-primary'}`}>
                    {ivData.iv_rank !== undefined && ivData.iv_rank !== null ? ivData.iv_rank.toFixed(1) : '—'}
                  </span>
                </div>
                <div className="p-3 rounded-lg bg-muted/30 border border-border/50">
                  <div className="text-[10px] text-muted-foreground uppercase font-bold mb-1">Avg IVx</div>
                  <span className="text-xl font-black">
                    {ivData.avg_ivx !== undefined && ivData.avg_ivx !== null ? `${ivData.avg_ivx.toFixed(1)}%` : '—'}
                  </span>
                </div>
                <div className="p-3 rounded-lg bg-muted/30 border border-border/50">
                  <div className="text-[10px] text-muted-foreground uppercase font-bold mb-1">Current IV</div>
                  <span className="text-xl font-black">
                    {ivData.current_iv !== undefined && ivData.current_iv !== null ? `${ivData.current_iv.toFixed(1)}%` : '—'}
                  </span>
                </div>
                <div className="p-3 rounded-lg bg-muted/30 border border-border/50">
                  <div className="text-[10px] text-muted-foreground uppercase font-bold mb-1">Pricing</div>
                  <span className={`text-sm font-bold ${ivData.iv_rank !== undefined && ivData.iv_rank !== null && ivData.iv_rank > 50 ? 'text-red-500' : 'text-green-500'}`}>
                    {ivData.iv_rank !== undefined && ivData.iv_rank !== null ? (ivData.iv_rank > 50 ? 'EXPENSIVE' : 'CHEAP') : '—'}
                  </span>
                </div>
              </div>

              {/* Simple Horizontal Term Structure */}
              <div className="space-y-2">
                <div className="text-[10px] uppercase font-bold text-muted-foreground mb-2">Term Structure (IVx)</div>
                <div className="space-y-3">
                  {ivData.expiries && ivData.expiries.map((exp) => {
                    const maxIvx = Math.max(...(ivData.expiries?.map(e => e.ivx || 0) || [1]))
                    const w = ((exp.ivx || 0) / maxIvx) * 100
                    return (
                      <div key={exp.expiry_date} className="space-y-1">
                        <div className="flex justify-between text-[10px] font-bold">
                          <span>{exp.expiry_date}</span>
                          <span>{exp.ivx?.toFixed(1)}%</span>
                        </div>
                        <div className="h-1.5 w-full bg-muted rounded-full overflow-hidden">
                          <div className="h-full bg-primary transition-all duration-500" style={{ width: `${w}%` }} />
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}
