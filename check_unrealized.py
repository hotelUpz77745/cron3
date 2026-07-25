import asyncio
import os
import time

try:
    import aiohttp
except ImportError:
    exit(1)

import hmac, hashlib

async def main():
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

    print("Monitoring /fapi/v2/account for 5 seconds to see if Unrealized PnL moves...")
    
    for i in range(5):
        params = {"recvWindow": 20000, "timestamp": int(time.time() * 1000)}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        
        async with aiohttp.ClientSession() as s:
            async with s.get("https://fapi.binance.com/fapi/v2/account", params=params, headers={"X-MBX-APIKEY": api_key}) as r:
                data = await r.json()
                
        positions = data.get("positions", [])
        print(f"\n--- TICK {i+1} ---")
        total_unrealized = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                unrealized = float(p.get("unrealizedProfit", 0))
                sym = p.get("symbol")
                side = p.get("positionSide")
                print(f"  {sym} {side:5s} | Amt: {amt:8.3f} | Unrealized: {unrealized:+.8f}")
                total_unrealized += unrealized
                
        print(f"  => TOTAL SUM OF UNREALIZED: {total_unrealized:+.8f}")
        await asyncio.sleep(1.0)

if __name__ == "__main__":
    asyncio.run(main())
