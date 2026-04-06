"""
Binance USDT-M Futures — MACD + RSI Strategy
============================================
✅ Dynamic position sizing: Trade Quantity = Balance / Entry Price
✅ Telegram bot control: start, stop, status, history, change settings
✅ Retry logic on network errors
"""

import os
import time
import logging
import json
import math
import threading
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import ta
import requests

# ─── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    # --- Market ---
    "SYMBOL":               "BTCUSDT",
    "INTERVAL":             Client.KLINE_INTERVAL_15MINUTE,
    "LEVERAGE":             2,

    # --- RSI ---
    "RSI_PERIOD":           14,
    "RSI_LONG_THRESHOLD":   40,
    "RSI_SHORT_THRESHOLD":  60,

    # --- MACD ---
    "MACD_FAST":            12,
    "MACD_SLOW":            26,
    "MACD_SIGNAL":          9,

    # --- Trade management ---
    "STOP_LOSS_PCT":        2.0,
    "TAKE_PROFIT_PCT":      4.0,

    # --- Bot ---
    "LOOP_INTERVAL":        60,
    "LOG_FILE":             "macd_rsi_bot.log",

    # --- Retry ---
    "RETRY_MAX_ATTEMPTS":   5,
    "RETRY_BASE_DELAY":     5,
    "RETRY_MAX_DELAY":      120,
}

# Bot state — controlled via Telegram
BOT_RUNNING = True

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
            if e.status_code in (429, 500, 502, 503, 504):
                log.warning(f"⚠️  Binance server error {e.status_code} (attempt {attempt}/{max_attempts})")
            else:
                raise

        if attempt < max_attempts:
            wait = min(delay * (2 ** (attempt - 1)), max_delay)
            log.info(f"⏱️  Retrying in {wait}s...")
            time.sleep(wait)
        else:
            log.error(f"❌ All {max_attempts} attempts failed.")
            raise


# ─── Telegram Helper ───────────────────────────────────────────────────────────
class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.base    = f"https://api.telegram.org/bot{token}"
        self.last_update_id = None

    def send(self, msg: str):
        try:
            requests.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")

    def get_updates(self):
        try:
            params = {"timeout": 10, "allowed_updates": ["message"]}
            if self.last_update_id:
                params["offset"] = self.last_update_id + 1
            r = requests.get(f"{self.base}/getUpdates", params=params, timeout=15)
            return r.json().get("result", [])
        except Exception:
            return []


# ─── Main Bot ──────────────────────────────────────────────────────────────────
class MACDRSIBot:
    def __init__(self, api_key: str, api_secret: str, telegram: Telegram):
        self.client      = Client(api_key, api_secret)
        self.tg          = telegram
        self.position    = None
        self.entry_price = None
        self.stop_loss   = None
        self.take_profit = None
        self.trade_log   = []

        log.info("✅ MACD+RSI Bot initialized.")
        self.tg.send("✅ <b>FrankBot started!</b>\nConnecting to Binance...")
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
                symbol=CONFIG["SYMBOL"], leverage=CONFIG["LEVERAGE"]
            )
            msg = (
                f"⚙️ <b>Bot Ready</b>\n"
                f"Symbol: {CONFIG['SYMBOL']} | Leverage: {CONFIG['LEVERAGE']}x\n"
                f"SL: {CONFIG['STOP_LOSS_PCT']}% | TP: {CONFIG['TAKE_PROFIT_PCT']}%\n"
                f"RSI Long &lt;{CONFIG['RSI_LONG_THRESHOLD']} | Short &gt;{CONFIG['RSI_SHORT_THRESHOLD']}"
            )
            self.tg.send(msg)
            log.info(f"⚙️  {CONFIG['SYMBOL']} | Leverage: {CONFIG['LEVERAGE']}x")
        except BinanceAPIException as e:
            self.tg.send(f"❌ Setup failed: {e}")
            log.error(f"❌ Setup failed: {e}")
            raise

    # ── Dynamic Position Size ──────────────────────────────────────────────────
    def calculate_quantity(self, price: float) -> float:
        """
        Trade Quantity = Current Balance / Entry Price
        Rounded down to 3 decimal places (Binance BTC step size)
        """
        balance = self.get_balance()
        raw_qty = balance / price
        qty = math.floor(raw_qty * 1000) / 1000  # round down to 3dp
        return max(qty, 0.001)  # minimum 0.001

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
        rsi = ta.momentum.RSIIndicator(
            close=df["close"], window=CONFIG["RSI_PERIOD"]
        ).rsi()

        macd_obj    = ta.trend.MACD(
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

        prev_macd = macd_line.iloc[-2]
        prev_sig  = signal_line.iloc[-2]

        if prev_macd < prev_sig and current_macd > current_sig:
            cross = "BULLISH"
        elif prev_macd > prev_sig and current_macd < current_sig:
            cross = "BEARISH"
        else:
            cross = "NONE"

        return current_rsi, current_macd, current_sig, current_hist, cross

    def check_signal(self, rsi, cross):
        if rsi < CONFIG["RSI_LONG_THRESHOLD"] and cross == "BULLISH":
            return "LONG"
        if rsi > CONFIG["RSI_SHORT_THRESHOLD"] and cross == "BEARISH":
            return "SHORT"
        return None

    # ── Orders ─────────────────────────────────────────────────────────────────
    def open_long(self, price: float):
        qty = self.calculate_quantity(price)
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

            msg = (
                f"🟢 <b>LONG Opened</b>\n"
                f"Entry: <b>${price:,.2f}</b>\n"
                f"Qty: {qty} BTC (${price*qty:,.2f})\n"
                f"Stop-loss: ${self.stop_loss:,.2f}\n"
                f"Take-profit: ${self.take_profit:,.2f}"
            )
            log.info(f"🟢 LONG | Entry: ${price:,.2f} | Qty: {qty} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}")
            self.tg.send(msg)
        except BinanceAPIException as e:
            log.error(f"❌ Open LONG failed: {e}")
            self.tg.send(f"❌ Open LONG failed: {e}")

    def open_short(self, price: float):
        qty = self.calculate_quantity(price)
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

            msg = (
                f"🔵 <b>SHORT Opened</b>\n"
                f"Entry: <b>${price:,.2f}</b>\n"
                f"Qty: {qty} BTC (${price*qty:,.2f})\n"
                f"Stop-loss: ${self.stop_loss:,.2f}\n"
                f"Take-profit: ${self.take_profit:,.2f}"
            )
            log.info(f"🔵 SHORT | Entry: ${price:,.2f} | Qty: {qty} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}")
            self.tg.send(msg)
        except BinanceAPIException as e:
            log.error(f"❌ Open SHORT failed: {e}")
            self.tg.send(f"❌ Open SHORT failed: {e}")

    def close_position(self, price: float, reason: str):
        qty = abs(float(self.client.futures_position_information(symbol=CONFIG["SYMBOL"])[0]["positionAmt"]))
        if qty == 0:
            qty = self.calculate_quantity(self.entry_price)
        try:
            side = "SELL" if self.position == "LONG" else "BUY"
            self.client.futures_create_order(
                symbol=CONFIG["SYMBOL"], side=side,
                type="MARKET", quantity=qty, reduceOnly=True,
            )
            pnl = ((price - self.entry_price) if self.position == "LONG"
                   else (self.entry_price - price)) * qty * CONFIG["LEVERAGE"]

            emoji = "💰" if pnl > 0 else "🔴"
            msg = (
                f"{emoji} <b>Position Closed</b>\n"
                f"Side: {self.position} | Reason: {reason}\n"
                f"Exit: <b>${price:,.2f}</b>\n"
                f"PnL: <b>${pnl:+.4f} USDT</b>"
            )
            log.info(f"{emoji} CLOSE {self.position} | {reason} | ${price:,.2f} | PnL: ${pnl:+.4f}")
            self.tg.send(msg)
            self._log_trade(f"CLOSE_{self.position}", price, qty, reason=reason, pnl=round(pnl, 4))
            self.position = self.entry_price = self.stop_loss = self.take_profit = None
        except BinanceAPIException as e:
            log.error(f"❌ Close failed: {e}")
            self.tg.send(f"❌ Close position failed: {e}")

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
                self.tg.send(f"🔄 Resumed existing LONG from ${self.entry_price:,.2f}")
            elif amt < 0:
                self.position    = "SHORT"
                self.entry_price = float(p["entryPrice"])
                self.stop_loss   = self.entry_price * (1 + CONFIG["STOP_LOSS_PCT"] / 100)
                self.take_profit = self.entry_price * (1 - CONFIG["TAKE_PROFIT_PCT"] / 100)
                log.info(f"🔄 Resumed SHORT from ${self.entry_price:,.2f}")
                self.tg.send(f"🔄 Resumed existing SHORT from ${self.entry_price:,.2f}")

    # ── Telegram Commands ──────────────────────────────────────────────────────
    def handle_command(self, text: str):
        global BOT_RUNNING
        text = text.strip().lower()
        parts = text.split()
        cmd = parts[0] if parts else ""

        # /start
        if cmd == "/start":
            BOT_RUNNING = True
            self.tg.send("▶️ <b>Bot started!</b> Trading is now active.")

        # /stop
        elif cmd == "/stop":
            BOT_RUNNING = False
            self.tg.send("⏹ <b>Bot stopped.</b> No new trades will be opened.\nUse /start to resume.")

        # /status
        elif cmd == "/status":
            price = self.get_price()
            bal   = self.get_balance()
            qty   = self.calculate_quantity(price)
            if self.position:
                pnl_est = ((price - self.entry_price) if self.position == "LONG"
                           else (self.entry_price - price)) * qty * CONFIG["LEVERAGE"]
                pos_info = (
                    f"\n📌 Position: <b>{self.position}</b>\n"
                    f"Entry: ${self.entry_price:,.2f}\n"
                    f"Current: ${price:,.2f}\n"
                    f"Unrealised PnL: ${pnl_est:+.4f}\n"
                    f"Stop-loss: ${self.stop_loss:,.2f}\n"
                    f"Take-profit: ${self.take_profit:,.2f}"
                )
            else:
                pos_info = "\n📌 No open position"

            msg = (
                f"📊 <b>Bot Status</b>\n"
                f"Running: {'✅ Yes' if BOT_RUNNING else '⏹ Stopped'}\n"
                f"Symbol: {CONFIG['SYMBOL']} | {CONFIG['LEVERAGE']}x\n"
                f"Balance: <b>${bal:.2f} USDT</b>\n"
                f"Next trade qty: {qty} BTC\n"
                f"SL: {CONFIG['STOP_LOSS_PCT']}% | TP: {CONFIG['TAKE_PROFIT_PCT']}%\n"
                f"Interval: {CONFIG['INTERVAL']}"
                f"{pos_info}"
            )
            self.tg.send(msg)

        # /history
        elif cmd == "/history":
            if not self.trade_log:
                self.tg.send("📋 No trades yet.")
                return
            last5 = self.trade_log[-5:]
            lines = ["📋 <b>Last 5 Trades:</b>"]
            for t in last5:
                pnl_str = f" | PnL: ${t['pnl_usdt']:+.4f}" if "pnl_usdt" in t else ""
                lines.append(
                    f"• {t['action']} @ ${t['price']:,.2f}{pnl_str}\n"
                    f"  {t['time'][:16]}"
                )
            self.tg.send("\n".join(lines))

        # /set sl <value>
        elif cmd == "/set" and len(parts) >= 3 and parts[1] == "sl":
            try:
                val = float(parts[2])
                CONFIG["STOP_LOSS_PCT"] = val
                self.tg.send(f"✅ Stop-loss updated to <b>{val}%</b>")
            except ValueError:
                self.tg.send("❌ Usage: /set sl 2.0")

        # /set tp <value>
        elif cmd == "/set" and len(parts) >= 3 and parts[1] == "tp":
            try:
                val = float(parts[2])
                CONFIG["TAKE_PROFIT_PCT"] = val
                self.tg.send(f"✅ Take-profit updated to <b>{val}%</b>")
            except ValueError:
                self.tg.send("❌ Usage: /set tp 4.0")

        # /set interval <value>
        elif cmd == "/set" and len(parts) >= 3 and parts[1] == "interval":
            intervals = {
                "1m":  Client.KLINE_INTERVAL_1MINUTE,
                "5m":  Client.KLINE_INTERVAL_5MINUTE,
                "15m": Client.KLINE_INTERVAL_15MINUTE,
                "1h":  Client.KLINE_INTERVAL_1HOUR,
                "4h":  Client.KLINE_INTERVAL_4HOUR,
            }
            val = parts[2].lower()
            if val in intervals:
                CONFIG["INTERVAL"] = intervals[val]
                self.tg.send(f"✅ Interval updated to <b>{val}</b>")
            else:
                self.tg.send("❌ Valid intervals: 1m, 5m, 15m, 1h, 4h")

        # /set rsi_long <value>
        elif cmd == "/set" and len(parts) >= 3 and parts[1] == "rsi_long":
            try:
                val = float(parts[2])
                CONFIG["RSI_LONG_THRESHOLD"] = val
                self.tg.send(f"✅ RSI Long threshold updated to <b>{val}</b>")
            except ValueError:
                self.tg.send("❌ Usage: /set rsi_long 40")

        # /set rsi_short <value>
        elif cmd == "/set" and len(parts) >= 3 and parts[1] == "rsi_short":
            try:
                val = float(parts[2])
                CONFIG["RSI_SHORT_THRESHOLD"] = val
                self.tg.send(f"✅ RSI Short threshold updated to <b>{val}</b>")
            except ValueError:
                self.tg.send("❌ Usage: /set rsi_short 60")

        # /help
        elif cmd == "/help":
            self.tg.send(
                "📖 <b>FrankBot Commands</b>\n\n"
                "/start — Resume trading\n"
                "/stop — Pause trading\n"
                "/status — View balance, position, settings\n"
                "/history — Last 5 trades\n\n"
                "<b>Change settings:</b>\n"
                "/set sl 2.0 — Stop-loss %\n"
                "/set tp 4.0 — Take-profit %\n"
                "/set interval 15m — Candle interval\n"
                "/set rsi_long 40 — RSI long threshold\n"
                "/set rsi_short 60 — RSI short threshold\n"
            )
        else:
            self.tg.send("❓ Unknown command. Type /help for the full list.")

    # ── Telegram Polling Thread ────────────────────────────────────────────────
    def poll_telegram(self):
        log.info("📱 Telegram polling started.")
        while True:
            updates = self.tg.get_updates()
            for update in updates:
                self.tg.last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "")
                if text.startswith("/"):
                    log.info(f"📱 Telegram command: {text}")
                    self.handle_command(text)
            time.sleep(2)

    # ── Main Loop ──────────────────────────────────────────────────────────────
    def run(self):
        global BOT_RUNNING
        log.info("🚀 MACD+RSI Futures Bot started")

        self._sync_position()

        # Start Telegram polling in background thread
        tg_thread = threading.Thread(target=self.poll_telegram, daemon=True)
        tg_thread.start()

        self.tg.send(
            f"🚀 <b>FrankBot is live!</b>\n"
            f"Symbol: {CONFIG['SYMBOL']} | {CONFIG['LEVERAGE']}x leverage\n"
            f"Type /help to see all commands."
        )

        while True:
            try:
                if not BOT_RUNNING:
                    log.info("⏹ Bot paused via Telegram.")
                    time.sleep(CONFIG["LOOP_INTERVAL"])
                    continue

                df    = self.get_klines()
                price = self.get_price()
                bal   = self.get_balance()
                rsi, macd, sig, hist, cross = self.calculate_indicators(df)

                log.info(
                    f"📊 Price: ${price:,.2f} | RSI: {rsi} | MACD: {macd} | "
                    f"Cross: {cross} | Pos: {self.position or 'NONE'} | Bal: ${bal:.2f}"
                )

                # ── Manage open position ───────────────────────────────────
                if self.position == "LONG":
                    if price <= self.stop_loss:
                        log.warning(f"⚠️  LONG stop-loss hit!")
                        self.close_position(price, "STOP_LOSS")
                    elif price >= self.take_profit:
                        log.info(f"🎯 LONG take-profit hit!")
                        self.close_position(price, "TAKE_PROFIT")
                    elif cross == "BEARISH":
                        log.info("📉 MACD bearish cross → closing LONG")
                        self.close_position(price, "MACD_REVERSAL")

                elif self.position == "SHORT":
                    if price >= self.stop_loss:
                        log.warning(f"⚠️  SHORT stop-loss hit!")
                        self.close_position(price, "STOP_LOSS")
                    elif price <= self.take_profit:
                        log.info(f"🎯 SHORT take-profit hit!")
                        self.close_position(price, "TAKE_PROFIT")
                    elif cross == "BULLISH":
                        log.info("📈 MACD bullish cross → closing SHORT")
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
                        log.info(f"⏳ No signal | RSI: {rsi} | Cross: {cross}")

            except BinanceAPIException as e:
                log.error(f"❌ Binance API error: {e}")
                self.tg.send(f"⚠️ Binance API error: {e}")
                time.sleep(CONFIG["RETRY_BASE_DELAY"])
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                log.warning(f"🌐 Network blip: {e}")
                time.sleep(15)
            except Exception as e:
                log.error(f"❌ Unexpected error: {e}", exc_info=True)
                self.tg.send(f"❌ Unexpected error: {e}")
                time.sleep(30)

            time.sleep(CONFIG["LOOP_INTERVAL"])


# ─── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    API_KEY        = os.environ.get("BINANCE_API_KEY",    "")
    API_SECRET     = os.environ.get("BINANCE_API_SECRET", "")
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN",     "")
    TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "")

    missing = []
    if not API_KEY:        missing.append("BINANCE_API_KEY")
    if not API_SECRET:     missing.append("BINANCE_API_SECRET")
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT:  missing.append("TELEGRAM_CHAT_ID")

    if missing:
        print(f"⚠️  Missing environment variables: {', '.join(missing)}")
        print("Set them with:")
        for m in missing:
            print(f"   set {m}=your_value_here")
        exit(1)

    tg  = Telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT)
    bot = MACDRSIBot(API_KEY, API_SECRET, tg)
    bot.run()
