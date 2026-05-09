from openalgo import api, ta

# 1. Initialize Client
# The host 127.0.0.1 works if the script runs on the same machine as OpenAlgo
client = api(api_key='108acd4d1aa4fd7535a27ae62033bde891ba6a7a38f0147d90a18987650cdc11', host='http://127.0.0.1:5000')

# 2. Settings
# These should match your "Current Symbol Mappings"
SYMBOLS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", 
    "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS", "TATAMOTORS"
]
EXCHANGE = "NSE"
INTERVAL = "5m"
STRATEGY_ID = "1"  # ID for 'python_breakoutv0'

def execute_strategy():
    for symbol in SYMBOLS:
        try:
            # Fetch OHLCV Data
            df = client.history(symbol=symbol, exchange=EXCHANGE, interval=INTERVAL)
            if df.empty or len(df) < 100: 
                continue

            # --- Technical Indicators ---
            df['ema100'] = ta.ema(df['close'], period=100)
            df['rsi'] = ta.rsi(df['close'], period=14)
            df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
            
            # Bollinger Bands & MACD
            upper, mid, lower = ta.bbands(df['close'], period=20, std_dev=2.0)
            macd_line, sig_line, macd_hist = ta.macd(df['close'])

            # Current and Previous Data Points
            curr = df.iloc[-1]
            prev = df.iloc[-2]

            # --- Strategy Conditions ---
            # 1. Price Crossover 100 EMA
            # 2. Price > VWAP
            # 3. RSI > 50
            # 4. MACD Histogram > 0
            buy_signal = (
                prev['close'] < prev['ema100'] and curr['close'] > curr['ema100'] and
                curr['close'] > curr['vwap'] and
                curr['rsi'] > 50 and
                macd_hist.iloc[-1] > 0
            )

            if buy_signal:
                print(f"Signal Detected: Buying {symbol}")
                # This triggers the order based on your dashboard's quantity/product settings
                client.strategyorder(symbol=symbol, action="BUY", strategy_id=STRATEGY_ID)

        except Exception as e:
            print(f"Error on {symbol}: {e}")

if __name__ == "__main__":
    execute_strategy()
