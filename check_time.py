"""
check_time.py - Diagnostika rassinkhrona chasov Railway vs Binance.
Zapustit na Railway: python check_time.py
"""

import asyncio
import time
import os
from datetime import datetime, timezone

try:
    import aiohttp
except ImportError:
    print("aiohttp not installed.")
    exit(1)

BINANCE_TIME_URL = "https://fapi.binance.com/fapi/v1/time"
BINANCE_INCOME_URL = "https://fapi.binance.com/fapi/v1/income"

async def main():
    print("=" * 60)
    print("RAILWAY TIME DRIFT DIAGNOSTIC")
    print("=" * 60)

    local_ts_ms = int(time.time() * 1000)
    local_dt = datetime.fromtimestamp(local_ts_ms / 1000, tz=timezone.utc)
    print(f"\n[LOCAL]  time.time()   -> {local_ts_ms} ms")
    print(f"[LOCAL]  UTC datetime  -> {local_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"[LOCAL]  TZ env var    -> {os.environ.get('TZ', 'Not set')}")
    print(f"[LOCAL]  datetime.now()-> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (naive/local)")

    print("\nFetching Binance server time...")
    before = int(time.time() * 1000)
    async with aiohttp.ClientSession() as s:
        async with s.get(BINANCE_TIME_URL, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
    after = int(time.time() * 1000)

    binance_ts = int(data["serverTime"])
    rtt = after - before
    drift_ms = binance_ts - (before + rtt // 2)

    binance_dt = datetime.fromtimestamp(binance_ts / 1000, tz=timezone.utc)
    print(f"[BINANCE] serverTime  -> {binance_ts} ms")
    print(f"[BINANCE] UTC datetime-> {binance_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"[NET]     Round-trip  -> {rtt} ms")
    print(f"\n{'='*60}")
    print(f"DRIFT = Binance - Local = {drift_ms:+d} ms  ({drift_ms/1000:+.3f} sec)")
    if abs(drift_ms) > 60000:
        print(f"CRITICAL: Drift > 1 minute! Old trades will be pulled!")
    elif abs(drift_ms) > 1000:
        print(f"WARNING:  Drift > 1 second! Signed requests may fail!")
    else:
        print(f"OK: Drift within acceptable range.")
    print(f"{'='*60}")

    api_key = os.environ.get("API_KEY", "")
    api_secret = os.environ.get("API_SECRET", "")

    if not api_key:
        env_file = ".env"
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("API_SECRET="):
                        api_secret = line.split("=", 1)[1].strip().strip('"').strip("'")

    if not api_key or not api_secret:
        print("\n[INCOME] No API keys found, skipping income check.")
        return

    import hmac, hashlib
    start_time = binance_ts - 3 * 60 * 60 * 1000  # 3 hours ago by BINANCE time
    params = {"limit": 20, "startTime": start_time, "timestamp": binance_ts}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    headers = {"X-MBX-APIKEY": api_key}

    print(f"\nChecking income for last 3h by Binance time ({datetime.fromtimestamp(start_time/1000, tz=timezone.utc).strftime('%H:%M:%S')} - {binance_dt.strftime('%H:%M:%S')} UTC):")
    async with aiohttp.ClientSession() as s:
        async with s.get(BINANCE_INCOME_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            income_data = await r.json()

    if isinstance(income_data, list):
        print(f"  Found {len(income_data)} records in last 3h by Binance time:")
        for rec in income_data:
            ts = int(rec.get("time", 0))
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            age_min = (binance_ts - ts) / 60000
            print(f"  [{dt.strftime('%H:%M:%S')} UTC] {rec.get('incomeType','?'):20s} "
                  f"{rec.get('symbol','?'):15s} {float(rec.get('income',0)):+.4f} "
                  f"({age_min:.1f} min ago by Binance)")

    print(f"\n--- Summary ---")
    print(f"  If bot uses local time.time() for first_trade_ts: points to {local_dt.strftime('%H:%M:%S')} UTC")
    print(f"  Binance actual time:                              {binance_dt.strftime('%H:%M:%S')} UTC")
    if drift_ms > 0:
        print(f"  => Bot would query {drift_ms//1000}s EARLIER than intended => old trades pulled!")
    else:
        print(f"  => Bot would query {abs(drift_ms)//1000}s LATER than intended => some new trades missed!")

if __name__ == "__main__":
    asyncio.run(main())
