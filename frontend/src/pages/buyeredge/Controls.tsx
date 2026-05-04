import { Check, ChevronsUpDown, RefreshCw, Wifi, WifiOff } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
} from '@/components/ui/command'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

interface ControlsProps {
  fnoExchanges: any[] // Matches what comes from useSupportedExchanges
  selectedExchange: string
  onExchangeChange: (ex: string) => void
  underlyings: string[]
  selectedUnderlying: string
  onUnderlyingChange: (val: string) => void
  underlyingOpen: boolean
  setUnderlyingOpen: (open: boolean) => void
  expiries: string[]
  selectedExpiry: string
  onExpiryChange: (val: string) => void
  lbBars: string
  setLbBars: (val: string) => void
  lbTf: string
  setLbTf: (val: string) => void
  strikeCount: string
  setStrikeCount: (val: string) => void
  isLoading: boolean
  onRefresh: () => void
  autoRefresh: boolean
  setAutoRefresh: (val: boolean) => void
  isMarketOpen: boolean
}

export function Controls({
  fnoExchanges,
  selectedExchange,
  onExchangeChange,
  underlyings,
  selectedUnderlying,
  onUnderlyingChange,
  underlyingOpen,
  setUnderlyingOpen,
  expiries,
  selectedExpiry,
  onExpiryChange,
  lbBars,
  setLbBars,
  lbTf,
  setLbTf,
  strikeCount,
  setStrikeCount,
  isLoading,
  onRefresh,
  autoRefresh,
  setAutoRefresh,
  isMarketOpen,
}: ControlsProps) {
  return (
    <Card className="border-none shadow-none bg-transparent">
      <CardContent className="p-0">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 bg-muted/30 p-4 rounded-2xl border border-border/50">
          <div className="flex flex-wrap items-center gap-3">
            {/* Exchange Selection */}
            <Select value={selectedExchange} onValueChange={onExchangeChange}>
              <SelectTrigger className="w-[100px] h-10 bg-background font-bold">
                <SelectValue placeholder="Exchange" />
              </SelectTrigger>
              <SelectContent>
                {fnoExchanges.map((ex) => (
                  <SelectItem key={ex.value} value={ex.value}>
                    {ex.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* Underlying Search */}
            <Popover open={underlyingOpen} onOpenChange={setUnderlyingOpen}>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  role="combobox"
                  aria-expanded={underlyingOpen}
                  className="w-[200px] justify-between h-10 bg-background font-bold"
                >
                  {selectedUnderlying || "Select Underlying..."}
                  <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-[200px] p-0">
                <Command>
                  <CommandInput placeholder="Search underlying..." />
                  <CommandEmpty>No underlying found.</CommandEmpty>
                  <CommandGroup className="max-h-[300px] overflow-y-auto">
                    {underlyings.map((u) => (
                      <CommandItem
                        key={u}
                        value={u}
                        onSelect={(currentValue) => {
                          onUnderlyingChange(currentValue)
                          setUnderlyingOpen(false)
                        }}
                      >
                        <Check
                          className={cn(
                            "mr-2 h-4 w-4",
                            selectedUnderlying === u ? "opacity-100" : "opacity-0"
                          )}
                        />
                        {u}
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </Command>
              </PopoverContent>
            </Popover>

            {/* Expiry Selection */}
            <Select value={selectedExpiry} onValueChange={onExpiryChange}>
              <SelectTrigger className="w-[140px] h-10 bg-background font-bold">
                <SelectValue placeholder="Expiry" />
              </SelectTrigger>
              <SelectContent>
                {expiries.map((exp) => (
                  <SelectItem key={exp} value={exp}>
                    {exp}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* Lookback Settings */}
            <div className="flex items-center gap-1 bg-background rounded-lg border border-input h-10 px-2">
              <Input
                type="number"
                value={lbBars}
                onChange={(e) => setLbBars(e.target.value)}
                className="w-12 h-7 border-none bg-transparent p-1 text-center text-xs font-bold focus-visible:ring-0"
              />
              <span className="text-[10px] text-muted-foreground font-bold">D /</span>
              <Select value={lbTf} onValueChange={setLbTf}>
                <SelectTrigger className="w-[65px] h-7 border-none bg-transparent text-xs font-bold focus:ring-0">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['1m', '3m', '5m', '15m', '30m', '1h', '1d'].map(tf => (
                    <SelectItem key={tf} value={tf}>{tf}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Strike Count */}
            <div className="flex items-center gap-2 bg-background rounded-lg border border-input h-10 px-3">
              <Label className="text-[10px] uppercase font-black text-muted-foreground">Strikes</Label>
              <Input
                type="number"
                value={strikeCount}
                onChange={(e) => setStrikeCount(e.target.value)}
                className="w-12 h-7 border-none bg-transparent p-0 text-center text-xs font-bold focus-visible:ring-0"
              />
            </div>
          </div>

          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <Switch id="auto-refresh" checked={autoRefresh} onCheckedChange={setAutoRefresh} />
              <Label htmlFor="auto-refresh" className="text-[10px] font-black uppercase tracking-widest text-muted-foreground cursor-pointer">Auto</Label>
            </div>

            <Button 
              onClick={onRefresh} 
              disabled={isLoading || !selectedUnderlying}
              className="h-10 px-6 font-bold shadow-lg shadow-primary/20"
            >
              {isLoading ? (
                <RefreshCw className="h-4 w-4 animate-spin mr-2" />
              ) : null}
              {isLoading ? 'ANALYZING...' : 'ANALYZE'}
            </Button>

            <div className="flex flex-col items-end">
              {isMarketOpen ? (
                <Badge variant="outline" className="bg-green-500/5 text-green-600 border-green-500/20 px-1 py-0 h-4 text-[9px] font-black uppercase flex items-center gap-1">
                  <Wifi className="h-2 w-2" /> Live
                </Badge>
              ) : (
                <Badge variant="outline" className="bg-amber-500/5 text-amber-600 border-amber-500/20 px-1 py-0 h-4 text-[9px] font-black uppercase flex items-center gap-1">
                  <WifiOff className="h-2 w-2" /> Closed
                </Badge>
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
