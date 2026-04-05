"""
Binance USDT-M Futures RSI Trading Bot
Strategy:
  - RSI < 30 → Open LONG  (price expected to rise)
  - RSI > 70 → Open SHORT (price expected to fall)
  - Take-profit and stop-loss on every trade
  - Leverage: 2x
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

# ─── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    "SYMBOL":           "BTCUSDT",                      # Futures trading pair
    "INTERVAL":         Client.KLINE_INTERVAL_1MINUTE, # Candle interval
    "LEVERAGE":         2,                              # 2x leverage
    "RSI_PERIOD":       14,                             # RSI lookback period
    "RSI_OVERSOLD":     30,                             # LONG trigger
    "RSI_OVERBOUGHT":   70,                             # SHORT trigger
    "TRADE_QUANTITY":   0.002,                          # BTC per trade
    "STOP_LOSS_PCT":    2.0,                            # Stop-loss %
    "TAKE_PROFIT_PCT":  3.0,                            # Take-profit %
    "LOOP_INTERVAL":    60,                             # Seconds between checks
    "LOG_FILE":         "futures_trades.log",
}

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Bot ───────────────────────────────────────────────────────────────────────
class FuturesRSIBot:
    def __init__(self, api_key: str, api_secret: str):
        self.client = Client(api_key, api_secret)
        self.position      = None   # "LONG", "SHORT", or None
        self.entry_price   = None
        self.stop_loss     = None
        self.take_profit   = None
        self.trade_log     = []

        log.info("✅ Futures Bot initialized.")
        self._setup()

    def _setup(self):
        """Ping, set hedge-off mode, and apply leverage."""
        try:
            self.client.futures_ping()
            # Set to one-way mode (not hedge mode)
            try:
                self.client.futures_change_position_mode(dualSidePosition=False)
            except BinanceAPIException as e:
                if "No need to change" not in str(e):
                    raise

            # Set leverage
            self.client.futures_change_leverage(
                symbol=CONFIG["SYMBOL"],
                leverage=CONFIG["LEVERAGE"]
            )
            log.info(f"⚙️  Leverage set to {CONFIG['LEVERAGE']}x on {CONFIG['SYMBOL']}")

            server_time = self.client.get_server_time()
            log.info(f"🌐 Server time: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
        except BinanceAPIException as e:
            log.error(f"❌ Setup failed: {e}")
            raise

    def get_balance(self) -> float:
        """Get available USDT in futures wallet."""
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    def get_klines(self, limit=100) -> pd.DataFrame:
        klines = self.client.futures_klines(
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
        return df

    def calculate_rsi(self, df) -> float:
        rsi = ta.momentum.RSIIndicator(
            close=df["close"], window=CONFIG["RSI_PERIOD"]
        ).rsi()
        return round(rsi.iloc[-1], 2)

    def get_price(self) -> float:
        ticker = self.client.futures_symbol_ticker(symbol=CONFIG["SYMBOL"])
        return float(ticker["price"])

    def get_open_position(self):
        """Check if there's an open position on Binance side."""
        positions = self.client.futures_position_information(symbol=CONFIG["SYMBOL"])
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                return "LONG", float(p["entryPrice"])
            elif amt < 0:
                return "SHORT", float(p["entryPrice"])
        return None, None

    def open_long(self, price: float):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            self.client.futures_create_order(
                symbol=CONFIG["SYMBOL"],
                side="BUY",
                type="MARKET",
                quantity=qty,
            )
            self.position    = "LONG"
            self.entry_price = price
            self.stop_loss   = price * (1 - CONFIG["STOP_LOSS_PCT"] / 100)
            self.take_profit = price * (1 + CONFIG["TAKE_PROFIT_PCT"] / 100)

            self._log_trade("OPEN_LONG", price, qty)
            log.info(f"🟢 LONG  opened | Entry: ${price:,.2f} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}")
        except BinanceAPIException as e:
            log.error(f"❌ Open LONG failed: {e}")

    def open_short(self, price: float):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            self.client.futures_create_order(
                symbol=CONFIG["SYMBOL"],
                side="SELL",
                type="MARKET",
                quantity=qty,
            )
            self.position    = "SHORT"
            self.entry_price = price
            self.stop_loss   = price * (1 + CONFIG["STOP_LOSS_PCT"] / 100)
            self.take_profit = price * (1 - CONFIG["TAKE_PROFIT_PCT"] / 100)

            self._log_trade("OPEN_SHORT", price, qty)
            log.info(f"🔵 SHORT opened | Entry: ${price:,.2f} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}")
        except BinanceAPIException as e:
            log.error(f"❌ Open SHORT failed: {e}")

    def close_position(self, price: float, reason: str):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            if self.position == "LONG":
                self.client.futures_create_order(
                    symbol=CONFIG["SYMBOL"],
                    side="SELL",
                    type="MARKET",
                    quantity=qty,
                    reduceOnly=True,
                )
                pnl = (price - self.entry_price) * qty * CONFIG["LEVERAGE"]
            else:  # SHORT
                self.client.futures_create_order(
                    symbol=CONFIG["SYMBOL"],
                    side="BUY",
                    type="MARKET",
                    quantity=qty,
                    reduceOnly=True,
                )
                pnl = (self.entry_price - price) * qty * CONFIG["LEVERAGE"]

            emoji = "💰" if pnl > 0 else "🔴"
            log.info(f"{emoji} CLOSE {self.position} | Reason: {reason} | Price: ${price:,.2f} | PnL: ${pnl:+.4f}")
            self._log_trade(f"CLOSE_{self.position}", price, qty, reason=reason, pnl=round(pnl, 4))

            self.position    = None
            self.entry_price = None
            self.stop_loss   = None
            self.take_profit = None

        except BinanceAPIException as e:
            log.error(f"❌ Close position failed: {e}")

    def _log_trade(self, action, price, qty, reason=None, pnl=None):
        record = {
            "action": action,
            "price": price,
            "quantity": qty,
            "leverage": CONFIG["LEVERAGE"],
            "time": datetime.now().isoformat(),
        }
        if reason: record["reason"] = reason
        if pnl is not None: record["pnl_usdt"] = pnl
        self.trade_log.append(record)
        with open("futures_trade_history.json", "w") as f:
            json.dump(self.trade_log, f, indent=2)

    def run(self):
        log.info(f"🚀 Futures Bot started | {CONFIG['SYMBOL']} | {CONFIG['LEVERAGE']}x leverage")
        log.info(f"   RSI LONG < {CONFIG['RSI_OVERSOLD']} | RSI SHORT > {CONFIG['RSI_OVERBOUGHT']}")
        log.info(f"   Stop-loss: {CONFIG['STOP_LOSS_PCT']}% | Take-profit: {CONFIG['TAKE_PROFIT_PCT']}%")

        # Sync position state with Binance on startup
        pos, entry = self.get_open_position()
        if pos:
            self.position = pos
            self.entry_price = entry
            self.stop_loss = entry * (1 - CONFIG["STOP_LOSS_PCT"] / 100) if pos == "LONG" else entry * (1 + CONFIG["STOP_LOSS_PCT"] / 100)
            self.take_profit = entry * (1 + CONFIG["TAKE_PROFIT_PCT"] / 100) if pos == "LONG" else entry * (1 - CONFIG["TAKE_PROFIT_PCT"] / 100)
            log.info(f"🔄 Resumed existing {pos} position from entry ${entry:,.2f}")

        while True:
            try:
                df    = self.get_klines()
                rsi   = self.calculate_rsi(df)
                price = self.get_price()
                bal   = self.get_balance()

                log.info(f"📊 {CONFIG['SYMBOL']} | Price: ${price:,.2f} | RSI: {rsi} | "
                         f"Balance: ${bal:.2f} USDT | Position: {self.position or 'NONE'}")

                if self.position == "LONG":
                    if price <= self.stop_loss:
                        log.warning(f"⚠️  LONG stop-loss hit! ${price:,.2f} ≤ ${self.stop_loss:,.2f}")
                        self.close_position(price, "STOP_LOSS")
                    elif price >= self.take_profit:
                        log.info(f"🎯 LONG take-profit hit! ${price:,.2f} ≥ ${self.take_profit:,.2f}")
                        self.close_position(price, "TAKE_PROFIT")
                    elif rsi >= CONFIG["RSI_OVERBOUGHT"]:
                        log.info(f"📈 RSI overbought ({rsi}) → closing LONG")
                        self.close_position(price, "RSI_EXIT")

                elif self.position == "SHORT":
                    if price >= self.stop_loss:
                        log.warning(f"⚠️  SHORT stop-loss hit! ${price:,.2f} ≥ ${self.stop_loss:,.2f}")
                        self.close_position(price, "STOP_LOSS")
                    elif price <= self.take_profit:
                        log.info(f"🎯 SHORT take-profit hit! ${price:,.2f} ≤ ${self.take_profit:,.2f}")
                        self.close_position(price, "TAKE_PROFIT")
                    elif rsi <= CONFIG["RSI_OVERSOLD"]:
                        log.info(f"📉 RSI oversold ({rsi}) → closing SHORT")
                        self.close_position(price, "RSI_EXIT")

                else:  # No position — look for entry
                    if rsi <= CONFIG["RSI_OVERSOLD"]:
                        log.info(f"📉 RSI oversold ({rsi}) → opening LONG")
                        self.open_long(price)
                    elif rsi >= CONFIG["RSI_OVERBOUGHT"]:
                        log.info(f"📈 RSI overbought ({rsi}) → opening SHORT")
                        self.open_short(price)
                    else:
                        log.info(f"⏳ No signal. Waiting... (RSI: {rsi})")

            except BinanceAPIException as e:
                log.error(f"❌ Binance API error: {e}")
            except Exception as e:
                log.error(f"❌ Unexpected error: {e}", exc_info=True)

            time.sleep(CONFIG["LOOP_INTERVAL"])


# ─── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    API_KEY    = os.environ.get("BINANCE_API_KEY",    "YOUR_API_KEY_HERE")
    API_SECRET = os.environ.get("BINANCE_API_SECRET", "YOUR_API_SECRET_HERE")

    if "YOUR_API" in API_KEY:
        print("⚠️  Set your Binance API keys as environment variables:")
        print("   set BINANCE_API_KEY=your_key_here")
        print("   set BINANCE_API_SECRET=your_secret_here")
        exit(1)

    bot = FuturesRSIBot(API_KEY, API_SECRET)
    bot.run()
