# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\main.py
# Role: main.py module

# ==============================================================================
# Path: main.py
# Role: Точка входа в приложение
# ==============================================================================

from __future__ import annotations

import os
import sys
from pathlib import Path

_CUR_DIR = Path(__file__).resolve().parent
if str(_CUR_DIR) not in sys.path:
    sys.path.insert(0, str(_CUR_DIR))

from consts import TG_ENABLED
from TG.tg_receiver import TelegramReceiver
import urllib.request
import json
from consts import TG_TOKEN, TG_ALLOWED_USERS
from c_log import UnifiedLogger
from CORE.bot import BotCore
os.environ["PYDANTIC_DISABLE_MODEL_REBUILD"] = "1"

import asyncio
from typing import Optional, List, Dict, Any


async def run_app(bot, logger):
    tg_bot = None
    if TG_ENABLED:
        try:
            tg_bot = TelegramReceiver(bot)
            logger.info("TGReceiver will be started alongside BotCore.")
        except Exception as e:
            logger.error(f"Failed to initialize TGReceiver: {e}")

    tasks = [bot.start()]
    if tg_bot:
        tasks.append(tg_bot.start())

    try:
        await asyncio.gather(*tasks)
    finally:
        if tg_bot:
            try:
                await tg_bot.stop()
            except Exception:
                pass

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
    bot: Optional[BotCore] = None
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
        if bot:
            try:
                asyncio.run(bot.shutdown())
            except Exception:
                pass
    except ValueError as e:
        logger.error(f"FATAL CONFIG ERROR: {e}")
        send_telegram_fatal(str(e))
        if bot:
            try:
                asyncio.run(bot.shutdown())
            except Exception:
                pass
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        send_telegram_fatal(str(e))
        if bot:
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
