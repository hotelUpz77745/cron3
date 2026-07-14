# ==============================================================================
# Path: main.py
# Role: Точка входа в приложение
# ==============================================================================

from __future__ import annotations

import os
os.environ["PYDANTIC_DISABLE_MODEL_REBUILD"] = "1"

import asyncio
from typing import *


async def run_app(bot, logger):
    from consts import TG_ENABLED
    tasks = [bot.start()]
    
    if TG_ENABLED:
        try:
            from TG.tg_receiver import TelegramReceiver
            tg_bot = TelegramReceiver(bot)
            tasks.append(tg_bot.start())
            logger.info("TGReceiver will be started alongside BotCore.")
        except Exception as e:
            logger.error(f"Failed to initialize TGReceiver: {e}")
            
    await asyncio.gather(*tasks)

def send_telegram_fatal(msg: str):
    import requests
    from consts import TG_TOKEN, TG_ALLOWED_USERS
    if not TG_TOKEN or not TG_ALLOWED_USERS:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for user_id in TG_ALLOWED_USERS:
        try:
            requests.post(url, json={"chat_id": user_id, "text": f"🚨 СТАРТОВАЯ ОШИБКА!\nБот упал при запуске:\n\n{msg}"}, timeout=5)
        except Exception:
            pass

def main():
    import logging
    from c_log import UnifiedLogger
    from CORE.bot import BotCore
    
    logger = UnifiedLogger("App")
    
    try:
        bot = BotCore()
        logger.info("Starting BotCore...")
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
# source C:/Users/User/Desktop/HP_EliteBook_735/PROGECTS/WORKSPACE/TRADING_SYSTEM/COMMON/.ssh-autostart.sh

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
