import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ChevronUp, ChevronDown, Info } from 'lucide-react'

import { getAdrMeta, getBreadthBias } from './utils'

interface MarketBreadthProps {
  data: any // BuyerEdgeResponse is too complex and inconsistent
  isLoading: boolean
  isOpen: boolean
  onToggle: () => void
  onShowInfo: () => void
}

export function MarketBreadth({
  data,
  isLoading,
  isOpen,
  onToggle,
  onShowInfo
}: MarketBreadthProps) {
  const latestPcr = data?.pcr?.data?.series?.[data?.pcr?.data?.series?.length - 1]
  const adrMeta = getAdrMeta(latestPcr?.adr || 1)
  const breadthBias = getBreadthBias(latestPcr?.ce_advances || 0, latestPcr?.pe_advances || 0)

  return (
    <Card>
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-bold flex items-center gap-2">
              Market Breadth Profile
              <Info className="h-3.5 w-3.5 text-muted-foreground hover:text-primary transition-colors cursor-help"
                onClick={(e) => { e.stopPropagation(); onShowInfo() }} />
            </CardTitle>
          </div>
          {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent>
          {isLoading ? (
            <div className="h-[200px] flex items-center justify-center text-muted-foreground animate-pulse italic">
              Calculating market sentiment...
            </div>
          ) : !data ? (
            <div className="h-[120px] flex items-center justify-center text-sm text-muted-foreground">
              Run an analysis to view breadth metrics
            </div>
          ) : (
            <div className="space-y-6">
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 rounded-xl border border-border/50 bg-muted/20 text-center">
                  <div className="text-[10px] text-muted-foreground uppercase font-black mb-2 tracking-widest">ADR Ratio</div>
                  <div className="text-2xl font-black mb-1" style={{ color: adrMeta.color }}>{latestPcr?.adr?.toFixed(2) || '1.00'}</div>
                  <Badge variant="outline" className="text-[9px] uppercase font-bold" style={{ borderColor: adrMeta.color + '44', color: adrMeta.color }}>
                    {adrMeta.label}
                  </Badge>
                </div>
                <div className="p-4 rounded-xl border border-border/50 bg-muted/20 text-center">
                  <div className="text-[10px] text-muted-foreground uppercase font-black mb-2 tracking-widest">Breadth Bias</div>
                  <div className="text-2xl font-black mb-1" style={{ color: breadthBias?.color || 'inherit' }}>{breadthBias?.label || 'Neutral'}</div>
                  <div className="text-[9px] font-bold text-muted-foreground">Based on CE/PE Advances</div>
                </div>
              </div>

              <div className="space-y-3">
                <div className="flex justify-between text-[10px] font-bold uppercase text-muted-foreground px-1">
                  <span>Advancing Strikes</span>
                  <span>Declining Strikes</span>
                </div>
                <div className="h-3 w-full flex rounded-full overflow-hidden bg-muted/50 border border-border/50">
                  <div className="h-full bg-green-500 transition-all duration-700" style={{ width: `${(latestPcr?.advances || 1) / ((latestPcr?.advances || 1) + (latestPcr?.declines || 1)) * 100}%` }} />
                  <div className="h-full bg-red-500 transition-all duration-700" style={{ width: `${(latestPcr?.declines || 1) / ((latestPcr?.advances || 1) + (latestPcr?.declines || 1)) * 100}%` }} />
                </div>
                <div className="flex justify-between text-xs font-black px-1">
                  <span className="text-green-500">{latestPcr?.advances || 0}</span>
                  <span className="text-red-500">{latestPcr?.declines || 0}</span>
                </div>
              </div>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}
