# CORE/SERVICES/__init__.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES
ROLE: Чистый экспорт сервисов, адаптеров и фоновых задач боевого бота
==============================================================================
"""

from CORE.SERVICES.auto_closer import AutoCloser
from CORE.SERVICES.notifier import NotifierManager, Notifier
from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
from CORE.SERVICES.leverage_adapter import LeverageAdapter, LeverageManager

__all__ = [
    "AutoCloser",
    "NotifierManager",
    "Notifier",
    "RuntimeBackupService",
    "LeverageAdapter",
    "LeverageManager",
]
