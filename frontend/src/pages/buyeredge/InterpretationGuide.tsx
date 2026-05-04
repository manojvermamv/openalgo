import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Info, ChevronUp, ChevronDown, Calculator, ShieldAlert, Target } from 'lucide-react'

export function InterpretationGuide({ isOpen, onToggle }: { isOpen: boolean; onToggle: () => void }) {
  return (
    <Card className="border-2 border-primary/20 shadow-lg bg-primary/5">
      <CardHeader className="pb-3 cursor-pointer" onClick={onToggle}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-md bg-primary/10">
              <Info className="h-4 w-4 text-primary" />
            </div>
            <div>
              <CardTitle className="text-base font-bold">Buyer Edge — Sentiment & Calculation Methodology</CardTitle>
              <p className="text-xs text-muted-foreground mt-0.5">How to interpret OI Change, Breadth, and Market Flow</p>
            </div>
          </div>
          {isOpen ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {isOpen && (
        <CardContent className="pt-0 animate-in fade-in slide-in-from-top-1 duration-300">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 pt-2 pb-2">

            {/* 1. OI Change Indicators */}
            <div className="space-y-3">
              <h4 className="text-sm font-bold flex items-center gap-2 text-primary">
                <Target className="h-4 w-4" />
                OI Change Indicators
              </h4>
              <div className="space-y-3">
                <div className="p-3 rounded-lg bg-background/50 border border-border/50">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2.5 h-2.5 rounded-full bg-green-500" />
                    <span className="text-xs font-bold text-green-600 dark:text-green-400">Call OI (Green)</span>
                  </div>
                  <p className="text-[10px] leading-relaxed text-muted-foreground">
                    Rising Call OI indicates <span className="font-semibold text-foreground italic">Upside Participation</span>.
                    If price is also rising, it confirms aggressive Call Buying.
                  </p>
                </div>
                <div className="p-3 rounded-lg bg-background/50 border border-border/50">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2.5 h-2.5 rounded-full bg-red-500" />
                    <span className="text-xs font-bold text-red-600 dark:text-red-400">Put OI (Red)</span>
                  </div>
                  <p className="text-[10px] leading-relaxed text-muted-foreground">
                    Rising Put OI indicates <span className="font-semibold text-foreground italic">Downside Protection</span>.
                    If price is falling, it confirms aggressive Put Buying.
                  </p>
                </div>
              </div>
            </div>

            {/* 2. Breadth & Sentiment */}
            <div className="space-y-3">
              <h4 className="text-sm font-bold flex items-center gap-2 text-primary">
                <ShieldAlert className="h-4 w-4" />
                Breadth & PCR
              </h4>
              <div className="space-y-3">
                <div className="p-3 rounded-lg bg-background/50 border border-border/50">
                  <span className="text-[10px] font-bold block mb-1">PCR (OI CHG)</span>
                  <ul className="text-[10px] space-y-1.5 text-muted-foreground list-disc pl-3">
                    <li><span className="text-foreground font-semibold">{'>'} 1.0:</span> Bearish Bias (Puts dominating)</li>
                    <li><span className="text-foreground font-semibold">{'<'} 1.0:</span> Bullish Bias (Calls dominating)</li>
                  </ul>
                </div>
                <div className="p-3 rounded-lg bg-background/50 border border-border/50">
                  <span className="text-[10px] font-bold block mb-1">ADR (Strike Breadth)</span>
                  <ul className="text-[10px] space-y-1.5 text-muted-foreground list-disc pl-3">
                    <li><span className="text-foreground font-semibold">{'>'} 1.2:</span> Healthy Trend Participation</li>
                    <li><span className="text-foreground font-semibold">{'<'} 0.8:</span> Trend Exhaustion / Narrow Market / Trap Zone</li>
                  </ul>
                </div>
              </div>
            </div>

            {/* 3. Methodology: Matrix Logic */}
            <div className="space-y-3">
              <h4 className="text-sm font-bold flex items-center gap-2 text-primary">
                <Calculator className="h-4 w-4" />
                Calculation Methodology
              </h4>
              <div className="p-3 rounded-lg bg-background/50 border border-border/50 space-y-2.5">
                <div className="border-b border-border/30 pb-2">
                  <span className="text-[10px] font-black uppercase text-primary mb-1 block underline underline-offset-4">Buyer Perspective (Flow)</span>
                  <p className="text-[9px] text-muted-foreground leading-tight">
                    We track <span className="text-foreground font-bold italic">Price + OI Correlation</span>.
                    If Price rises while OI rises, it's <span className="text-green-500 font-bold">Long Buildup</span>.
                    If Price falls while OI rises, it's <span className="text-red-500 font-bold">Short Buildup</span>.
                  </p>
                </div>
                <div>
                  <span className="text-[10px] font-black uppercase text-amber-500 mb-1 block underline underline-offset-4">Seller Perspective (Walls)</span>
                  <p className="text-[9px] text-muted-foreground leading-tight">
                    Large OI at specific strikes acts as <span className="text-foreground font-bold">Resistances (Call Wall)</span>
                    or <span className="text-foreground font-bold">Supports (Put Wall)</span> where sellers are defending premiums.
                  </p>
                </div>
              </div>
            </div>

            {/* 4. Strategic Cues */}
            <div className="space-y-3">
              <h4 className="text-sm font-bold flex items-center gap-2 text-primary">
                <Info className="h-4 w-4" />
                Strategic Cues
              </h4>
              <div className="p-3 rounded-lg bg-primary/10 border border-primary/20 h-[calc(100%-2.5rem)]">
                <ul className="text-[10px] space-y-2 text-muted-foreground">
                  <li className="flex gap-2">
                    <span className="text-primary font-bold">1.</span>
                    <span><span className="text-foreground font-semibold">Crossover:</span> Green crosses Red from below confirms <span className="text-green-500 font-bold">Bullish Dominance</span>.</span>
                  </li>
                  <li className="flex gap-2">
                    <span className="text-primary font-bold">2.</span>
                    <span><span className="text-foreground font-semibold">ADR Expansion:</span> Broad strike participation confirms the <span className="text-foreground font-bold underline">Strength</span> of the current move.</span>
                  </li>
                  <li className="flex gap-2">
                    <span className="text-primary font-bold">3.</span>
                    <span><span className="text-foreground font-semibold">PCR Divergence:</span> If Spot rises but PCR rises too, beware of <span className="text-amber-500 font-bold">Hidden Weakness</span>.</span>
                  </li>
                </ul>
              </div>
            </div>

          </div>
        </CardContent>
      )}
    </Card>
  )
}
