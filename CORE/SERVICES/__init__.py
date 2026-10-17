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
from CORE.SERVICES.emergency_closer import EmergencyCloserService
from CORE.SERVICES.symbol_lifecycle_manager import SymbolLifecycleManager
from CORE.SERVICES.bot_bootstrap import BotBootstrapService

__all__ = [
    "AutoCloser",
    "NotifierManager",
    "Notifier",
    "RuntimeBackupService",
    "LeverageAdapter",
    "LeverageManager",
    "EmergencyCloserService",
    "SymbolLifecycleManager",
    "BotBootstrapService",
]

