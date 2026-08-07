"""
Send a Seasonal Dashboard pipeline notification via Outlook COM.
Usage:
    python notify.py           # success email
    python notify.py --fail    # failure email
"""

import sys
import datetime
import pandas as pd
from pathlib import Path

HERE   = Path(__file__).parent
DB_DIR = HERE.parent / "Database"
PRICES = DB_DIR / "prices.parquet"
TO     = "virat.arya@etgworld.com"
TODAY  = datetime.date.today().strftime("%Y-%m-%d")


def _parquet_stats() -> dict:
    if not PRICES.exists():
        return {"total_rows": "N/A", "tickers": "N/A", "last_date": "N/A"}
    df = pd.read_parquet(PRICES)
    return {
        "total_rows": f"{len(df):,}",
        "tickers":    str(df['ticker'].nunique()),
        "last_date":  str(pd.to_datetime(df['date']).max().date()),
    }


def send(subject: str, body: str):
    import win32com.client
    outlook      = win32com.client.Dispatch("Outlook.Application")
    mail         = outlook.CreateItem(0)
    mail.To      = TO
    mail.Subject = subject
    mail.Body    = body
    mail.Send()


def success():
    s = _parquet_stats()
    subject = f"[OK] Seasonal Dashboard — {TODAY}"
    body = (
        f"Seasonal Dashboard Daily Update\n"
        f"Run time   : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"\n"
        f"Total rows : {s['total_rows']}\n"
        f"Tickers    : {s['tickers']}\n"
        f"Last date  : {s['last_date']}\n"
        f"GitHub     : pushed main\n"
    )
    send(subject, body)
    print(f"Email sent: {subject}")


def failure():
    subject = f"[FAILED] Seasonal Dashboard — {TODAY}"
    body = (
        f"Seasonal Dashboard pipeline FAILED on {TODAY}.\n"
        f"Check log: {HERE / 'logs' / 'automation_log.txt'}\n"
    )
    send(subject, body)
    print(f"Email sent: {subject}")


if __name__ == "__main__":
    if "--fail" in sys.argv:
        failure()
    else:
        success()
