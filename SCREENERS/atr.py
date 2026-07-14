# ==============================================================================
# Path: SCREENERS/atr.py
# Role: Alias / Entry point for the main ATR Volatility Screener
# Note: This is a wrapper around the core module `CORE.ADVANCED.volatility_scanner`.
# ==============================================================================

import sys
import asyncio
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    from CORE.ADVANCED.volatility_scanner import main
    asyncio.run(main())
