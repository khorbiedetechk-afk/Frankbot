"""
Binance USDT-M Futures — MACD + RSI Combined Strategy
======================================================
LONG  signal: RSI < 40  AND  MACD line crosses ABOVE signal line
SHORT signal: RSI > 60  AND  MACD line crosses BELOW signal line
Both indicators must agree — no trade on mixed signals.
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
import requests  # for catching network errors

# ─── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    # --- Market ---
    "SYMBOL":               "BTCUSDT",
    "INTERVAL":             Client.KLINE_INTERVAL_1MINUTE,
    "LEVERAGE":             2,

    # --- RSI ---
    "RSI_PERIOD":           14,
    "RSI_LONG_THRESHOLD":   40,   # RSI must be BELOW this to consider a LONG
    "RSI_SHORT_THRESHOLD":  60,   # RSI must be ABOVE this to consider a SHORT

    # --- MACD ---
    "MACD_FAST":            12,
    "MACD_SLOW":            26,
    "MACD_SIGNAL":          9,

    # --- Trade management ---
    "TRADE_QUANTITY":       0.001,   # BTC per trade
    "STOP_LOSS_PCT":        2.0,     # % below/above entry
    "TAKE_PROFIT_PCT":      4.0,     # % above/below entry (wider = more aggressive)

    # --- Bot ---
    "LOOP_INTERVAL":        60,      # seconds between checks
    "LOG_FILE":             "macd_rsi_bot.log",

    # --- Retry ---
    "RETRY_MAX_ATTEMPTS":   5,       # max retries on network error
    "RETRY_BASE_DELAY":     5,       # seconds before first retry
    "RETRY_MAX_DELAY":      120,     # cap retry wait at 2 minutes
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


# ─── Retry Helper ──────────────────────────────────────────────────────────────
def retry(func, *args, **kwargs):
    """
    Call func(*args, **kwargs) and retry up to RETRY_MAX_ATTEMPTS times
    on network or API errors, using exponential backoff.
    Raises the final exception if all attempts fail.
    """
    max_attempts = CONFIG["RETRY_MAX_ATTEMPTS"]
    delay        = CONFIG["RETRY_BASE_DELAY"]
    max_delay    = CONFIG["RETRY_MAX_DELAY"]

    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout) as e:
            log.warning(f"🌐 Network error (attempt {attempt}/{max_attempts}): {e}")
        except BinanceAPIException as e:
            # Only retry on server-side / rate-limit errors, not auth errors
            if e.status_code in (429, 500, 502, 503, 504):
                log.warning(f"⚠️  Binance server error {e.status_code} (attempt {attempt}/{max_attempts})")
            else:
                raise  # Auth errors, bad requests etc — don't retry

        if attempt < max_attempts:
            wait = min(delay * (2 ** (attempt - 1)), max_delay)
            log.info(f"⏱️  Retrying in {wait}s...")
            time.sleep(wait)
        else:
            log.error(f"❌ All {max_attempts} attempts failed. Skipping this cycle.")
            raise


# ─── Bot ───────────────────────────────────────────────────────────────────────
class MACDRSIBot:
    def __init__(self, api_key: str, api_secret: str):
        self.client       = Client(api_key, api_secret)
        self.position     = None    # "LONG", "SHORT", or None
        self.entry_price  = None
        self.stop_loss    = None
        self.take_profit  = None
        self.trade_log    = []
        self.prev_macd_cross = None  # track last crossover direction

        log.info("✅ MACD+RSI Bot initialized.")
        self._setup()

    # ── Setup ──────────────────────────────────────────────────────────────────
    def _setup(self):
        try:
            self.client.futures_ping()
            try:
                self.client.futures_change_position_mode(dualSidePosition=False)
            except BinanceAPIException as e:
                if "No need to change" not in str(e):
                    raise

            self.client.futures_change_leverage(
                symbol=CONFIG["SYMBOL"],
                leverage=CONFIG["LEVERAGE"]
            )
            log.info(f"⚙️  {CONFIG['SYMBOL']} | Leverage: {CONFIG['LEVERAGE']}x")

            server_time = self.client.get_server_time()
            log.info(f"🌐 Server time: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
        except BinanceAPIException as e:
            log.error(f"❌ Setup failed: {e}")
            raise

    # ── Data ───────────────────────────────────────────────────────────────────
    def get_balance(self) -> float:
        for b in retry(self.client.futures_account_balance):
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    def get_klines(self, limit=150) -> pd.DataFrame:
        klines = retry(
            self.client.futures_klines,
            symbol=CONFIG["SYMBOL"],
            interval=CONFIG["INTERVAL"],
            limit=limit,
        )
        df = pd.DataFrame(klines, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_volume","trades",
            "taker_base","taker_quote","ignore"
        ])
        df["close"] = pd.to_numeric(df["close"])
        return df

    def get_price(self) -> float:
        return float(retry(self.client.futures_symbol_ticker, symbol=CONFIG["SYMBOL"])["price"])

    # ── Indicators ─────────────────────────────────────────────────────────────
    def calculate_indicators(self, df):
        # RSI
        rsi = ta.momentum.RSIIndicator(
            close=df["close"], window=CONFIG["RSI_PERIOD"]
        ).rsi()

        # MACD
        macd_obj = ta.trend.MACD(
            close=df["close"],
            window_fast=CONFIG["MACD_FAST"],
            window_slow=CONFIG["MACD_SLOW"],
            window_sign=CONFIG["MACD_SIGNAL"],
        )
        macd_line   = macd_obj.macd()
        signal_line = macd_obj.macd_signal()
        histogram   = macd_obj.macd_diff()

        current_rsi  = round(rsi.iloc[-1], 2)
        current_macd = round(macd_line.iloc[-1], 4)
        current_sig  = round(signal_line.iloc[-1], 4)
        current_hist = round(histogram.iloc[-1], 4)

        # Detect crossover on the last two candles
        prev_macd = macd_line.iloc[-2]
        prev_sig  = signal_line.iloc[-2]

        if prev_macd < prev_sig and current_macd > current_sig:
            cross = "BULLISH"   # MACD crossed above signal → bullish
        elif prev_macd > prev_sig and current_macd < current_sig:
            cross = "BEARISH"   # MACD crossed below signal → bearish
        else:
            cross = "NONE"

        return current_rsi, current_macd, current_sig, current_hist, cross

    # ── Signal logic ───────────────────────────────────────────────────────────
    def check_signal(self, rsi, cross):
        """
        LONG:  RSI < RSI_LONG_THRESHOLD  AND  MACD bullish crossover
        SHORT: RSI > RSI_SHORT_THRESHOLD AND  MACD bearish crossover
        """
        if rsi < CONFIG["RSI_LONG_THRESHOLD"] and cross == "BULLISH":
            return "LONG"
        if rsi > CONFIG["RSI_SHORT_THRESHOLD"] and cross == "BEARISH":
            return "SHORT"
        return None

    # ── Orders ─────────────────────────────────────────────────────────────────
    def open_long(self, price: float):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            self.client.futures_create_order(
                symbol=CONFIG["SYMBOL"], side="BUY",
                type="MARKET", quantity=qty,
            )
            self.position    = "LONG"
            self.entry_price = price
            self.stop_loss   = price * (1 - CONFIG["STOP_LOSS_PCT"]   / 100)
            self.take_profit = price * (1 + CONFIG["TAKE_PROFIT_PCT"] / 100)
            self._log_trade("OPEN_LONG", price, qty)
            log.info(f"🟢 LONG  | Entry: ${price:,.2f} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}")
        except BinanceAPIException as e:
            log.error(f"❌ Open LONG failed: {e}")

    def open_short(self, price: float):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            self.client.futures_create_order(
                symbol=CONFIG["SYMBOL"], side="SELL",
                type="MARKET", quantity=qty,
            )
            self.position    = "SHORT"
            self.entry_price = price
            self.stop_loss   = price * (1 + CONFIG["STOP_LOSS_PCT"]   / 100)
            self.take_profit = price * (1 - CONFIG["TAKE_PROFIT_PCT"] / 100)
            self._log_trade("OPEN_SHORT", price, qty)
            log.info(f"🔵 SHORT | Entry: ${price:,.2f} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}")
        except BinanceAPIException as e:
            log.error(f"❌ Open SHORT failed: {e}")

    def close_position(self, price: float, reason: str):
        qty = CONFIG["TRADE_QUANTITY"]
        try:
            side = "SELL" if self.position == "LONG" else "BUY"
            self.client.futures_create_order(
                symbol=CONFIG["SYMBOL"], side=side,
                type="MARKET", quantity=qty, reduceOnly=True,
            )
            if self.position == "LONG":
                pnl = (price - self.entry_price) * qty * CONFIG["LEVERAGE"]
            else:
                pnl = (self.entry_price - price) * qty * CONFIG["LEVERAGE"]

            emoji = "💰" if pnl > 0 else "🔴"
            log.info(f"{emoji} CLOSE {self.position} | {reason} | Price: ${price:,.2f} | PnL: ${pnl:+.4f}")
            self._log_trade(f"CLOSE_{self.position}", price, qty, reason=reason, pnl=round(pnl, 4))

            self.position = self.entry_price = self.stop_loss = self.take_profit = None
        except BinanceAPIException as e:
            log.error(f"❌ Close failed: {e}")

    def _log_trade(self, action, price, qty, reason=None, pnl=None):
        record = {
            "action": action, "price": price,
            "quantity": qty, "leverage": CONFIG["LEVERAGE"],
            "time": datetime.now().isoformat(),
        }
        if reason: record["reason"] = reason
        if pnl is not None: record["pnl_usdt"] = pnl
        self.trade_log.append(record)
        with open("macd_rsi_trade_history.json", "w") as f:
            json.dump(self.trade_log, f, indent=2)

    # ── Sync open position on startup ──────────────────────────────────────────
    def _sync_position(self):
        positions = self.client.futures_position_information(symbol=CONFIG["SYMBOL"])
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                self.position    = "LONG"
                self.entry_price = float(p["entryPrice"])
                self.stop_loss   = self.entry_price * (1 - CONFIG["STOP_LOSS_PCT"] / 100)
                self.take_profit = self.entry_price * (1 + CONFIG["TAKE_PROFIT_PCT"] / 100)
                log.info(f"🔄 Resumed LONG from ${self.entry_price:,.2f}")
            elif amt < 0:
                self.position    = "SHORT"
                self.entry_price = float(p["entryPrice"])
                self.stop_loss   = self.entry_price * (1 + CONFIG["STOP_LOSS_PCT"] / 100)
                self.take_profit = self.entry_price * (1 - CONFIG["TAKE_PROFIT_PCT"] / 100)
                log.info(f"🔄 Resumed SHORT from ${self.entry_price:,.2f}")

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run(self):
        log.info("🚀 MACD+RSI Futures Bot started")
        log.info(f"   Symbol: {CONFIG['SYMBOL']} | Interval: {CONFIG['INTERVAL']} | Leverage: {CONFIG['LEVERAGE']}x")
        log.info(f"   RSI LONG < {CONFIG['RSI_LONG_THRESHOLD']} | RSI SHORT > {CONFIG['RSI_SHORT_THRESHOLD']}")
        log.info(f"   Stop-loss: {CONFIG['STOP_LOSS_PCT']}% | Take-profit: {CONFIG['TAKE_PROFIT_PCT']}%")
        log.info(f"   MACD ({CONFIG['MACD_FAST']}/{CONFIG['MACD_SLOW']}/{CONFIG['MACD_SIGNAL']})")

        self._sync_position()

        while True:
            try:
                df    = self.get_klines()
                price = self.get_price()
                bal   = self.get_balance()
                rsi, macd, sig, hist, cross = self.calculate_indicators(df)

                log.info(
                    f"📊 Price: ${price:,.2f} | RSI: {rsi} | "
                    f"MACD: {macd} | Signal: {sig} | Hist: {hist} | "
                    f"Cross: {cross} | Pos: {self.position or 'NONE'} | Bal: ${bal:.2f}"
                )

                # ── Manage open position ───────────────────────────────────
                if self.position == "LONG":
                    if price <= self.stop_loss:
                        log.warning(f"⚠️  LONG stop-loss! ${price:,.2f} ≤ ${self.stop_loss:,.2f}")
                        self.close_position(price, "STOP_LOSS")
                    elif price >= self.take_profit:
                        log.info(f"🎯 LONG take-profit! ${price:,.2f} ≥ ${self.take_profit:,.2f}")
                        self.close_position(price, "TAKE_PROFIT")
                    elif cross == "BEARISH":
                        log.info("📉 MACD bearish cross → closing LONG early")
                        self.close_position(price, "MACD_REVERSAL")

                elif self.position == "SHORT":
                    if price >= self.stop_loss:
                        log.warning(f"⚠️  SHORT stop-loss! ${price:,.2f} ≥ ${self.stop_loss:,.2f}")
                        self.close_position(price, "STOP_LOSS")
                    elif price <= self.take_profit:
                        log.info(f"🎯 SHORT take-profit! ${price:,.2f} ≤ ${self.take_profit:,.2f}")
                        self.close_position(price, "TAKE_PROFIT")
                    elif cross == "BULLISH":
                        log.info("📈 MACD bullish cross → closing SHORT early")
                        self.close_position(price, "MACD_REVERSAL")

                # ── Look for new entry ─────────────────────────────────────
                else:
                    signal = self.check_signal(rsi, cross)
                    if signal == "LONG":
                        log.info(f"✅ LONG signal | RSI: {rsi} + MACD bullish cross")
                        self.open_long(price)
                    elif signal == "SHORT":
                        log.info(f"✅ SHORT signal | RSI: {rsi} + MACD bearish cross")
                        self.open_short(price)
                    else:
                        reasons = []
                        if cross == "NONE":
                            reasons.append("no MACD cross")
                        if CONFIG["RSI_LONG_THRESHOLD"] <= rsi <= CONFIG["RSI_SHORT_THRESHOLD"]:
                            reasons.append(f"RSI neutral ({rsi})")
                        log.info(f"⏳ No signal — {', '.join(reasons) if reasons else 'waiting'}")

            except BinanceAPIException as e:
                log.error(f"❌ Binance API error: {e}")
                log.info(f"⏱️  Waiting {CONFIG['RETRY_BASE_DELAY']}s before next cycle...")
                time.sleep(CONFIG["RETRY_BASE_DELAY"])
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                log.warning(f"🌐 Network blip: {e}")
                log.info("⏱️  Internet issue — waiting 15s then continuing...")
                time.sleep(15)
            except Exception as e:
                log.error(f"❌ Unexpected error: {e}", exc_info=True)
                log.info("⏱️  Waiting 30s before retrying...")
                time.sleep(30)

            time.sleep(CONFIG["LOOP_INTERVAL"])


# ─── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    API_KEY    = os.environ.get("BINANCE_API_KEY",    "YOUR_API_KEY_HERE")
    API_SECRET = os.environ.get("BINANCE_API_SECRET", "YOUR_API_SECRET_HERE")

    if "YOUR_API" in API_KEY:
        print("⚠️  Set your Binance API keys first:")
        print("   set BINANCE_API_KEY=your_key_here")
        print("   set BINANCE_API_SECRET=your_secret_here")
        exit(1)

    bot = MACDRSIBot(API_KEY, API_SECRET)
    bot.run()
