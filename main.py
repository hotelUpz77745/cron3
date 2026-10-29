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

import asyncio
import contextlib
import signal
from typing import Optional, List, Dict, Any

from consts import TG_ENABLED
from TG.tg_receiver import TelegramReceiver
from TG import TelegramAdapter
from c_log import UnifiedLogger
from CORE.bot import BotCore

os.environ["PYDANTIC_DISABLE_MODEL_REBUILD"] = "1"


async def _safe_tg_runner(tg_bot: TelegramReceiver, logger: UnifiedLogger) -> None:
    """Безопасный запуск TG-ресивера: непредвиденные сбои в TG не должны ронять торговое ядро."""
    try:
        await tg_bot.start()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[main] TGReceiver unexpected loop failure: {e}", exc_info=True)


async def run_app(bot: BotCore, logger: UnifiedLogger) -> None:
    tg_bot = None
    tg_task: Optional[asyncio.Task] = None
    if TG_ENABLED:
        try:
            tg_bot = TelegramReceiver(bot)
            logger.info("TGReceiver will be started alongside BotCore.")
        except Exception as e:
            logger.error(f"Failed to initialize TGReceiver: {e}")

    loop = asyncio.get_running_loop()

    def _shutdown_signal_handler(sig_name: str) -> None:
        logger.info(f"Received signal {sig_name}. Initiating graceful shutdown of BotCore...")
        bot.stop()
        if tg_bot:
            asyncio.create_task(tg_bot.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_signal_handler, sig.name)
        except (NotImplementedError, AttributeError):
            pass

    if tg_bot:
        tg_task = asyncio.create_task(_safe_tg_runner(tg_bot, logger), name="TGReceiver")

    try:
        await bot.start()
    finally:
        if tg_task and not tg_task.done():
            tg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tg_task
        if tg_bot:
            with contextlib.suppress(Exception):
                await tg_bot.stop()
        with contextlib.suppress(Exception):
            await bot.shutdown()


def send_telegram_fatal(msg: str):
    TelegramAdapter.send_fatal_alert(msg)


def main():
    logger = UnifiedLogger("App")
    bot: Optional[BotCore] = None
    try:
        bot = BotCore()
        logger.info("Starting BotCore...")
        logger.info("=" * 50)
        logger.info("🟢 БОТ УСПЕШНО ЗАПУЩЕН!")
        status = "⏸️ ПАУЗА (Ждет старта)" if bot.is_paused else "▶️ АКТИВЕН (Торгует)"
        logger.info("Текущий статус: %s", status)
        logger.info("👉 ПЕРЕЙДИТЕ В TELEGRAM К ЭЛЕМЕНТАМ УПРАВЛЕНИЯ!")
        logger.info("=" * 50)
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

