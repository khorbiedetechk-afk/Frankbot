"""
Binance RSI Trading Bot
Strategy: Buy when RSI < 30 (oversold), Sell when RSI > 70 (overbought)
"""

import os
import time
import logging
import json
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import ta

# ─── Configuration ────────────────────────────────────────────────────────────
CONFIG = {
    "SYMBOL": "BTCUSDT",           # Trading pair
    "INTERVAL": Client.KLINE_INTERVAL_15MINUTE,  # Candle interval
    "RSI_PERIOD": 14,              # RSI calculation period
    "RSI_OVERSOLD": 30,            # Buy threshold
    "RSI_OVERBOUGHT": 70,          # Sell threshold
    "TRADE_QUANTITY": 0.001,       # BTC amount per trade (adjust to your budget)
    "STOP_LOSS_PCT": 2.0,          # Stop-loss % below buy price
    "TAKE_PROFIT_PCT": 3.0,        # Take-profit % above buy price
    "LOOP_INTERVAL": 60,           # Seconds between checks
    "LOG_FILE": "trades.log",
}

# ─── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Bot Class ─────────────────────────────────────────────────────────────────
class RSIBot:
    def __init__(self, api_key: str, api_secret: str):
        self.client = Client(api_key, api_secret)
        self.in_position = False
        self.buy_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        self.trade_log = []

        log.info("✅ Bot initialized. Connected to Binance.")
        self._check_connectivity()

    def _check_connectivity(self):
        try:
            self.client.ping()
            server_time = self.client.get_server_time()
            log.info(f"🌐 Server time: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
        except BinanceAPIException as e:
            log.error(f"❌ Binance connection failed: {e}")
            raise

    def get_account_balance(self, asset="USDT"):
        balance = self.client.get_asset_balance(asset=asset)
        return float(balance["free"]) if balance else 0.0

    def get_klines(self, limit=100):
        """Fetch recent candlestick data."""
        klines = self.client.get_klines(
            symbol=CONFIG["SYMBOL"],
            interval=CONFIG["INTERVAL"],
            limit=limit,
        )
        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_base", "taker_quote", "ignore"
        ])
        df["close"] = pd.to_numeric(df["close"])
        df["high"] = pd.to_numeric(df["high"])
        df["low"] = pd.to_numeric(df["low"])
        return df

    def calculate_rsi(self, df) -> float:
        """Calculate RSI using the 'ta' library."""
        rsi_series = ta.momentum.RSIIndicator(
            close=df["close"], window=CONFIG["RSI_PERIOD"]
        ).rsi()
        return round(rsi_series.iloc[-1], 2)

    def get_current_price(self) -> float:
        ticker = self.client.get_symbol_ticker(symbol=CONFIG["SYMBOL"])
        return float(ticker["price"])

    def place_buy_order(self, price: float):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            order = self.client.order_market_buy(
                symbol=CONFIG["SYMBOL"],
                quantity=qty,
            )
            self.buy_price = price
            self.stop_loss_price = price * (1 - CONFIG["STOP_LOSS_PCT"] / 100)
            self.take_profit_price = price * (1 + CONFIG["TAKE_PROFIT_PCT"] / 100)
            self.in_position = True

            record = {
                "action": "BUY",
                "price": price,
                "quantity": qty,
                "stop_loss": self.stop_loss_price,
                "take_profit": self.take_profit_price,
                "time": datetime.now().isoformat(),
                "order_id": order.get("orderId"),
            }
            self.trade_log.append(record)
            self._save_trade_log()

            log.info(f"🟢 BUY  | Price: ${price:,.2f} | Qty: {qty} | Stop-loss: ${self.stop_loss_price:,.2f} | Take-profit: ${self.take_profit_price:,.2f}")
        except BinanceAPIException as e:
            log.error(f"❌ Buy order failed: {e}")

    def place_sell_order(self, price: float, reason="RSI_OVERBOUGHT"):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            order = self.client.order_market_sell(
                symbol=CONFIG["SYMBOL"],
                quantity=qty,
            )
            pnl = (price - self.buy_price) * qty if self.buy_price else 0

            record = {
                "action": "SELL",
                "reason": reason,
                "price": price,
                "quantity": qty,
                "buy_price": self.buy_price,
                "pnl_usdt": round(pnl, 4),
                "time": datetime.now().isoformat(),
                "order_id": order.get("orderId"),
            }
            self.trade_log.append(record)
            self._save_trade_log()

            emoji = "🔴" if pnl < 0 else "💰"
            log.info(f"{emoji} SELL | Reason: {reason} | Price: ${price:,.2f} | PnL: ${pnl:+.4f}")

            self.in_position = False
            self.buy_price = None
            self.stop_loss_price = None
            self.take_profit_price = None
        except BinanceAPIException as e:
            log.error(f"❌ Sell order failed: {e}")

    def _save_trade_log(self):
        with open("trade_history.json", "w") as f:
            json.dump(self.trade_log, f, indent=2)

    def run(self):
        log.info(f"🚀 Starting RSI Bot | Symbol: {CONFIG['SYMBOL']} | Interval: {CONFIG['INTERVAL']}")
        log.info(f"   RSI Buy  < {CONFIG['RSI_OVERSOLD']} | RSI Sell > {CONFIG['RSI_OVERBOUGHT']} | Stop-loss: {CONFIG['STOP_LOSS_PCT']}% | Take-profit: {CONFIG['TAKE_PROFIT_PCT']}%")

        while True:
            try:
                df = self.get_klines()
                rsi = self.calculate_rsi(df)
                price = self.get_current_price()
                usdt_balance = self.get_account_balance("USDT")

                log.info(f"📊 {CONFIG['SYMBOL']} | Price: ${price:,.2f} | RSI: {rsi} | "
                         f"Balance: ${usdt_balance:.2f} | Position: {'YES' if self.in_position else 'NO'}")

                # ── Stop-loss check ──────────────────────────────────────────
                if self.in_position and price <= self.stop_loss_price:
                    log.warning(f"⚠️  Stop-loss triggered! Price ${price:,.2f} ≤ ${self.stop_loss_price:,.2f}")
                    self.place_sell_order(price, reason="STOP_LOSS")

                # ── Take-profit check ────────────────────────────────────────
                elif self.in_position and price >= self.take_profit_price:
                    log.info(f"🎯 Take-profit triggered! Price ${price:,.2f} ≥ ${self.take_profit_price:,.2f}")
                    self.place_sell_order(price, reason="TAKE_PROFIT")

                # ── Sell signal ──────────────────────────────────────────────
                elif self.in_position and rsi >= CONFIG["RSI_OVERBOUGHT"]:
                    log.info(f"📈 RSI overbought ({rsi}) → SELL signal")
                    self.place_sell_order(price, reason="RSI_OVERBOUGHT")

                # ── Buy signal ───────────────────────────────────────────────
                elif not self.in_position and rsi <= CONFIG["RSI_OVERSOLD"]:
                    required_usdt = price * CONFIG["TRADE_QUANTITY"]
                    if usdt_balance >= required_usdt:
                        log.info(f"📉 RSI oversold ({rsi}) → BUY signal")
                        self.place_buy_order(price)
                    else:
                        log.warning(f"⚠️  Insufficient USDT balance: ${usdt_balance:.2f} (need ${required_usdt:.2f})")

                else:
                    log.info(f"⏳ Waiting... (RSI: {rsi} | Need {'<' + str(CONFIG['RSI_OVERSOLD']) if not self.in_position else '>' + str(CONFIG['RSI_OVERBOUGHT'])})")

            except BinanceAPIException as e:
                log.error(f"❌ Binance API error: {e}")
            except Exception as e:
                log.error(f"❌ Unexpected error: {e}", exc_info=True)

            time.sleep(CONFIG["LOOP_INTERVAL"])


# ─── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    API_KEY = os.environ.get("BINANCE_API_KEY", "YOUR_API_KEY_HERE")
    API_SECRET = os.environ.get("BINANCE_API_SECRET", "YOUR_API_SECRET_HERE")

    if "YOUR_API" in API_KEY:
        print("⚠️  Set your API keys via environment variables:")
        print("   export BINANCE_API_KEY='your_key'")
        print("   export BINANCE_API_SECRET='your_secret'")
        exit(1)

    bot = RSIBot(API_KEY, API_SECRET)
    bot.run()
