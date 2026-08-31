# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\main.py
# Role: main.py module

# ==============================================================================
# Path: main.py
# Role: Точка входа в приложение
# ==============================================================================

from __future__ import annotations

import os
from consts import TG_ENABLED
from TG.tg_receiver import TelegramReceiver
import urllib.request
import json
from consts import TG_TOKEN, TG_ALLOWED_USERS
import logging
from c_log import UnifiedLogger
from CORE.bot import BotCore
os.environ["PYDANTIC_DISABLE_MODEL_REBUILD"] = "1"

import asyncio
from typing import *


async def run_app(bot, logger):
    tasks = [bot.start()]
    
    if TG_ENABLED:
        try:
            tg_bot = TelegramReceiver(bot)
            tasks.append(tg_bot.start())
            logger.info("TGReceiver will be started alongside BotCore.")
        except Exception as e:
            logger.error(f"Failed to initialize TGReceiver: {e}")
            
    await asyncio.gather(*tasks)

def send_telegram_fatal(msg: str):
    if not TG_TOKEN or not TG_ALLOWED_USERS:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for user_id in TG_ALLOWED_USERS:
        try:
            data = json.dumps({
                "chat_id": user_id, 
                "text": f"🚨 СТАРТОВАЯ ОШИБКА!\nБот упал при запуске:\n\n{msg}"
            }).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

def main():
    
    logger = UnifiedLogger("App")
    
    try:
        bot = BotCore()
        logger.info("Starting BotCore...")
        print("\n" + "="*50)
        print("🟢 БОТ УСПЕШНО ЗАПУЩЕН!")
        status = "⏸️ ПАУЗА (Ждет старта)" if bot.is_paused else "▶️ АКТИВЕН (Торгует)"
        print(f"Текущий статус: {status}")
        print("👉 ПЕРЕЙДИТЕ В TELEGRAM К ЭЛЕМЕНТАМ УПРАВЛЕНИЯ!")
        print("="*50 + "\n")
        asyncio.run(run_app(bot, logger))
        
    except KeyboardInterrupt:
        logger.info("Ctrl+C detected. Shutting down...")
        try:
            asyncio.run(bot.shutdown())
        except Exception:
            pass
    except ValueError as e:
        logger.error(f"FATAL CONFIG ERROR: {e}")
        send_telegram_fatal(str(e))
        try:
            asyncio.run(bot.shutdown())
        except Exception:
            pass
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        send_telegram_fatal(str(e))
        try:
            asyncio.run(bot.shutdown())
        except Exception:
            pass


if __name__ == "__main__":
    main()



## шпору не трогать!!
# # chmod 600 ssh_key.txt
# # eval "$(ssh-agent -s)" 
# # ssh-add ssh_key.txt
# # git remote set-url origin git@github.com:hotelUpz/uranus_bot.git
# # source .ssh-autostart.sh
# В терминале Git Bash, находясь в папке с проектом:
# source C:/Users/User/Desktop/My_Pro/HP_EliteBook_735_old/WORKSPACE/COMMON/.ssh-autostart.sh

# git push --set-upstream origin master 
# # git config --global push.autoSetupRemote true
# # ssh -T git@github.com 
# # git log -1

# # git add .
# # git commit -m "plh37"
# # git push

# # pip install anthropic
# # npm install -g @anthropic-ai/claude-code

# # export ANTHROPIC_API_KEY=...
# taskkill /F /IM python.exe

# # claude


# Id;Symbol;Side;Open Time;Close Time;PnL (USDT);Balance
# 1;XPLUSDT;SYNC;2026-06-30 03:26:05;2026-06-30 03:26:05;0.0231;84.9831

# Id;Symbol;Side;Open Time;Close Time;PnL (USDT);Balance
# 1;XPLUSDT;SYNC;2026-06-30 03:26:05;2026-06-30 03:26:05;0.0231;298,98


# Slavik
# Id;Symbol;Side;Open Time;Close Time;PnL (USDT);Balance
# 1;SOLUSDT;SHORT;2026-06-26 20:25:01;2026-06-26 20:33:45;1.5663;8999.9317

#   "symbols": [
#     "STABLEUSDT",
#     "DASHUSDT",
#     "SOLUSDT",
#     "BNBUSDT",
#     "XAGUSDT",
#     "JUPUSDT"
#   ],

# # 1. Просто скачиваем информацию об обновлениях с гитхаба (это безопасно, файлы не трогает)
# git fetch origin

# # 2. Вытягиваем из скачанного ТОЛЬКО два конкретных файла, принудительно перезаписывая их локально
# git checkout origin/slavik -- ANALYTICS/analytics.py TG/tg_receiver.py


# Id;Symbol;Side;Open Time;Close Time;PnL (USDT);Balance
# 1;XPLUSDT;SYNC;2026-06-30 03:26:05;2026-06-30 03:26:05;0.0231;298.9831

# git fetch origin
# git checkout origin/slavik -- ANALYTICS/analytics.py TG/tg_receiver.py CORE/bot.py
# git fetch origin
# git checkout origin/slavik -- CORE/_utils.py CORE/bot.py API/BINANCE/price_stream.py CORE/GRID/avg_manager.py POS_FSM/pos_stream_monitor.py


# /quant


#   "symbols": [
#     "STABLEUSDT",
#     "DASHUSDT",
#     "MMTUSDT",
#     "KGENUSDT"
#   ],


    # "auto_closing": {

    #     "negative":  {"threshold": null}, # -float | null. null -- off
    #      "positive":  {"threshold": 50}, # +float | null. null -- off
    # } -- добавшь секцию в конфиги а также внедришь механизм автоматического закрытия позиции. Это закрытие не имеет никакого отношения к остановке бота либо каких то пауз -- есть тригер Net USDT профита -- закрываем позиции как если бы мы нажали кнопку Close All -- бот должен продалжать работу в штатном режиме включая подсос аналитики и так далее.
