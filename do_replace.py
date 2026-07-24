import sys

with open("ANALYTICS/analytics.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

with open(r"C:\Users\User\.gemini\antigravity-ide\brain\8d5e2802-c838-4097-9387-ad9011af0b86\scratch\unified_drawdowns.py", "r", encoding="utf-8") as f:
    unified = f.read()
    
# Fix the recovery_factor line just in case
unified = unified.replace(
    'data["recovery_factor"] = round(data["net_profit_usdt"] / abs(data["max_drawdown_usdt"]), 2)',
    'data["recovery_factor"] = round(bot_gross_profit / abs(data["max_drawdown_usdt"]), 2)'
)

# Find start and end indices
start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if line.startswith("    async def _update_drawdowns(self, client, data: dict):"):
        start_idx = i
    if line.startswith("    async def _realtime_tracker_loop(self, client):"):
        end_idx = i
        break

if start_idx != -1 and end_idx != -1:
    new_lines = lines[:start_idx] + [unified, "\n"] + lines[end_idx:]
    with open("ANALYTICS/analytics.py", "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"Successfully replaced from line {start_idx+1} to {end_idx+1}")
else:
    print("Failed to find start or end index.")
