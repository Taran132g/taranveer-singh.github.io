import pandas as pd
import yfinance as yf
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
from datetime import datetime
import os
from typing import List, Dict

# ------------------------------------------------------------------
# CONFIG – CHANGE THESE IF NEEDED
# ------------------------------------------------------------------
CSV_FILE = "nasdaq_2_to_10_stocks_fresh.csv"   # <-- NEW CSV FROM ME
PROXIMITY_THRESHOLD = 0.02               # 2% proximity to ATH/ATL
CHECK_INTERVAL = 300                     # 5 minutes
EMAIL_ENABLED = False                    # Set to True + fill creds below

# Email (optional – leave empty if EMAIL_ENABLED=False)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""          # e.g. "you@gmail.com"
SENDER_PASSWORD = ""       # App password, NOT login password
RECIPIENT_EMAIL = ""       # e.g. "you@gmail.com"

# ------------------------------------------------------------------
class SupportResistanceBot:
    def __init__(self):
        self.csv_file = CSV_FILE
        self.proximity = PROXIMITY_THRESHOLD
        self.interval = CHECK_INTERVAL
        self.email_enabled = EMAIL_ENABLED
        self.smtp_server = SMTP_SERVER
        self.smtp_port = SMTP_PORT
        self.sender = SENDER_EMAIL
        self.password = SENDER_PASSWORD
        self.recipient = RECIPIENT_EMAIL

        self.symbols = self.load_symbols()
        self.alerted = set()  # Prevent spam

    def load_symbols(self) -> List[str]:
        """Load only the 'symbol' column from the CSV I provided."""
        try:
            df = pd.read_csv(self.csv_file)
            symbols = df['symbol'].dropna().str.strip().tolist()
            print(f"Loaded {len(symbols)} symbols from {self.csv_file}")
            return symbols
        except Exception as e:
            print(f"Failed to load CSV: {e}")
            return []

    def get_hist(self, symbol: str, period: str = "2y") -> pd.DataFrame:
        try:
            return yf.Ticker(symbol).history(period=period, auto_adjust=True)
        except:
            return pd.DataFrame()

    def calc_levels(self, hist: pd.DataFrame) -> Dict:
        if hist.empty:
            return {}
        ath = hist['High'].max()
        atl = hist['Low'].min()
        cur = hist['Close'].iloc[-1]
        return {
            'ath': ath,
            'atl': atl,
            'current': cur,
            'ath_pct': abs(cur - ath) / ath,
            'atl_pct': abs(cur - atl) / atl
        }

    def check_alert(self, symbol: str, levels: Dict) -> Dict | None:
        if symbol in self.alerted:
            return None

        cur = levels['current']
        if levels['ath_pct'] <= self.proximity:
            self.alerted.add(symbol)
            return {
                'symbol': symbol,
                'type': 'ATH',
                'current': cur,
                'target': levels['ath'],
                'pct': levels['ath_pct'] * 100
            }
        if levels['atl_pct'] <= self.proximity:
            self.alerted.add(symbol)
            return {
                'symbol': symbol,
                'type': 'ATL',
                'current': cur,
                'target': levels['atl'],
                'pct': levels['atl_pct'] * 100
            }
        return None

    def send_email(self, alert: Dict):
        if not self.email_enabled:
            return
        msg = MIMEMultipart()
        msg['From'] = self.sender
        msg['To'] = self.recipient
        msg['Subject'] = f"{alert['type']} ALERT: {alert['symbol']}"

        body = f"""
{alert['type']} APPROACH ALERT

Symbol     : {alert['symbol']}
Current    : ${alert['current']:.2f}
Target     : ${alert['target']:.2f}
Proximity  : {alert['pct']:.2f}%

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        msg.attach(MIMEText(body, 'plain'))

        try:
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender, self.password)
            server.sendmail(self.sender, self.recipient, msg.as_string())
            server.quit()
            print(f"Email sent for {alert['symbol']}")
        except Exception as e:
            print(f"Email failed: {e}")

    def scan_once(self) -> List[Dict]:
        alerts = []
        print(f"\nScanning {len(self.symbols)} symbols (2% proximity)...")
        for i, sym in enumerate(self.symbols, 1):
            hist = self.get_hist(sym)
            levels = self.calc_levels(hist)
            if not levels:
                continue
            alert = self.check_alert(sym, levels)
            if alert:
                alerts.append(alert)
                print(f"ALERT: {sym} → {alert['type']} (${alert['current']:.2f})")
                self.send_email(alert)
            if i % 10 == 0:
                print(f"  → {i}/{len(self.symbols)} processed")
        return alerts

    def run_forever(self):
        print(f"Starting continuous monitoring every {self.interval}s")
        print("-" * 50)
        while True:
            try:
                alerts = self.scan_once()
                if alerts:
                    print(f"\n{len(alerts)} new alert(s):")
                    for a in alerts:
                        print(f"  • {a['symbol']}: {a['type']} (${a['current']:.2f})")
                print(f"\nNext scan in {self.interval}s... (Ctrl+C to stop)")
                time.sleep(self.interval)
            except KeyboardInterrupt:
                print("\nStopping bot...")
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(60)

# ------------------------------------------------------------------
def main():
    bot = SupportResistanceBot()
    if not bot.symbols:
        print("No symbols loaded. Save the CSV as 'nasdaq_2_to_10_stocks.csv'")
        return

    mode = input("Run once (o) or continuous (c)? [o/c]: ").strip().lower()
    if mode == 'c':
        bot.run_forever()
    else:
        bot.scan_once()

if __name__ == "__main__":
    main()
