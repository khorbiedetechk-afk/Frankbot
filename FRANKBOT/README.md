# Binance RSI Trading Bot

A live crypto trading bot for Binance using the RSI (Relative Strength Index) strategy.

## Strategy
- **BUY** when RSI drops below **30** (oversold)
- **SELL** when RSI rises above **70** (overbought)
- **STOP-LOSS** triggered if price drops 2% below buy price

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get Binance API keys
1. Log in to [Binance](https://www.binance.com)
2. Go to **Account → API Management**
3. Create a new API key — enable **Spot Trading**, disable withdrawals
4. Copy your API Key and Secret

### 3. Set environment variables
```bash
export BINANCE_API_KEY="your_api_key_here"
export BINANCE_API_SECRET="your_api_secret_here"
```

### 4. Configure the bot (optional)
Edit `bot.py` → `CONFIG` section:

| Parameter | Default | Description |
|---|---|---|
| `SYMBOL` | `BTCUSDT` | Trading pair |
| `INTERVAL` | `15m` | Candle interval |
| `RSI_PERIOD` | `14` | RSI lookback period |
| `RSI_OVERSOLD` | `30` | Buy trigger |
| `RSI_OVERBOUGHT` | `70` | Sell trigger |
| `TRADE_QUANTITY` | `0.001` | BTC per trade (~$60–$90) |
| `STOP_LOSS_PCT` | `2.0` | Stop-loss % below buy price |
| `LOOP_INTERVAL` | `60` | Seconds between checks |

### 5. Run the bot
```bash
python bot.py
```

## Output
- **Console**: Real-time logs with price, RSI, and trade signals
- **trades.log**: Full log file
- **trade_history.json**: JSON record of all executed trades

## ⚠️ Disclaimer
This bot trades with **real money**. Use at your own risk. Start with small trade quantities. Past performance does not guarantee future results. Always test strategies before using significant capital.
