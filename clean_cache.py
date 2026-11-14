#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: clean_cache.py
ROLE: Сервис глубокой очистки кэша, рантайм-состояний, аналитических логов
      и технических слепков перед боевым запуском бота или новой сессией.

ОЧИЩАЕМЫЕ ЦЕЛЕВЫЕ ЗОНЫ:
  1. CFG/runtime/            - рантайм-конфигурации позиций и FSM монеты
  2. CACHE/                  - кэш автозакрывателя, эквити, итераций и плеч
  3. ANALYTICS/LEDGERS/      - журналы сделок, пилинга и рециркуляции
  4. ANALYTICS/STATS/        - персистентные JSON-снимки аналитики куполов
  5. ANALYTICS/images/       - сохраненные графики баланса и эквити
  6. ANALYTICS/leaderboard.txt - текстовый лидерборд стратегий
  7. logs/                   - файлы логов (.log, ротированные хвосты)

ИСПОЛЬЗОВАНИЕ:
  python clean_cache.py                 # Полная очистка всех кэшей
  python clean_cache.py --keep-specs    # Очистить всё, сохранив specifications.json
  python clean_cache.py --dry-run       # Показать, что будет удалено, без удаления
==============================================================================
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


class CacheCleaner:
    """Сервис инспекции и очистки кэшей и временных рантайм-файлов проекта."""

    def __init__(
        self,
        project_root: Optional[Path] = None,
        keep_specs: bool = False,
        dry_run: bool = False,
    ):
        self.root: Path = Path(project_root or Path(__file__).resolve().parent)
        self.keep_specs: bool = bool(keep_specs)
        self.dry_run: bool = bool(dry_run)

        # Список очищаемых директорий (очищается содержимое, папка сохраняется пустой)
        self.target_dirs: List[Path] = [
            self.root / "CFG" / "runtime",
            self.root / "CACHE",
            self.root / "ANALYTICS" / "LEDGERS",
            self.root / "ANALYTICS" / "STATS",
            self.root / "ANALYTICS" / "images",
            self.root / "logs",
        ]

        # Список очищаемых отдельных файлов
        self.target_files: List[Path] = [
            self.root / "ANALYTICS" / "leaderboard.txt",
        ]

    def _should_skip_file(self, file_path: Path) -> bool:
        """Проверяет, нужно ли пропустить файл (например, сохраненный specifications.json)."""
        if self.keep_specs and file_path.name.lower() == "specifications.json":
            return True
        return False

    def clean_all(self) -> Dict[str, Any]:
        """
        Выполняет очистку всех целевых кэшей.
        Возвращает детальную статистику:
          {
            "files_removed": int,
            "dirs_removed": int,
            "bytes_freed": int,
            "errors": List[str],
            "details": List[str],
          }
        """
        stats = {
            "files_removed": 0,
            "dirs_removed": 0,
            "bytes_freed": 0,
            "errors": [],
            "details": [],
        }

        # 1. Очистка целевых директорий
        for d in self.target_dirs:
            if not d.exists():
                if not self.dry_run:
                    d.mkdir(parents=True, exist_ok=True)
                continue

            for item in d.iterdir():
                if self._should_skip_file(item):
                    stats["details"].append(f"[SKIP] {item.relative_to(self.root)} (сохранен по --keep-specs)")
                    continue

                try:
                    if item.is_file() or item.is_symlink():
                        size = item.stat().st_size
                        if not self.dry_run:
                            item.unlink(missing_ok=True)
                        stats["files_removed"] += 1
                        stats["bytes_freed"] += size
                        stats["details"].append(f"[DEL FILE] {item.relative_to(self.root)} ({size} B)")
                    elif item.is_dir():
                        dir_size = sum(f.stat().st_size for f in item.glob("**/*") if f.is_file())
                        dir_files = sum(1 for f in item.glob("**/*") if f.is_file())
                        if not self.dry_run:
                            shutil.rmtree(item, ignore_errors=True)
                        stats["dirs_removed"] += 1
                        stats["files_removed"] += dir_files
                        stats["bytes_freed"] += dir_size
                        stats["details"].append(f"[DEL DIR]  {item.relative_to(self.root)}/ ({dir_size} B, {dir_files} files)")
                except Exception as e:
                    stats["errors"].append(f"Ошибка при удалении {item.relative_to(self.root)}: {e}")

            # Гарантируем наличие пустой корневой директории
            if not self.dry_run:
                d.mkdir(parents=True, exist_ok=True)

        # 2. Очистка целевых отдельных файлов
        for f in self.target_files:
            if f.exists() and f.is_file():
                try:
                    size = f.stat().st_size
                    if not self.dry_run:
                        f.unlink(missing_ok=True)
                    stats["files_removed"] += 1
                    stats["bytes_freed"] += size
                    stats["details"].append(f"[DEL FILE] {f.relative_to(self.root)} ({size} B)")
                except Exception as e:
                    stats["errors"].append(f"Ошибка при удалении {f.relative_to(self.root)}: {e}")

        return stats


def format_bytes(num_bytes: int) -> str:
    """Форматирует байты в удобные единицы (B, KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Сервис глубокой очистки кэша и временных файлов HRON_3 / cron3."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать список удаляемых файлов без фактического удаления.",
    )
    parser.add_argument(
        "--keep-specs",
        action="store_true",
        help="Сохранить CACHE/specifications.json (не скачивать заново спецификации Binance).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Выводить только итоговую сводку без подробного списка файлов.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Не запрашивать интерактивное подтверждение.",
    )

    args = parser.parse_args()

    # Интерактивное подтверждение (только если запущен вручную в TTY без -y и без --dry-run)
    if not args.yes and not args.dry_run and sys.stdin.isatty():
        prompt = "⚠️ ВНИМАНИЕ: Будет очищен весь кэш (CFG/runtime, CACHE, ANALYTICS/STATS, LEDGERS, logs). Продолжить? [y/N]: "
        try:
            choice = input(prompt).strip().lower()
            if choice not in ("y", "yes"):
                print("❌ Операция отменена пользователем.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\n❌ Операция отменена пользователем.")
            return 1

    cleaner = CacheCleaner(keep_specs=args.keep_specs, dry_run=args.dry_run)
    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(f"\n🧹 {prefix}Запуск очистки кэшей cron3...")

    stats = cleaner.clean_all()

    if not args.quiet:
        for detail in stats["details"]:
            print(f"  {detail}")

    print("\n" + "=" * 60)
    print(f"📊 {prefix}ИТОГИ ОЧИСТКИ КЭША:")
    print(f"  ├ Удалено файлов:      {stats['files_removed']}")
    print(f"  ├ Удалено директорий:  {stats['dirs_removed']}")
    print(f"  ├ Освобождено места:   {format_bytes(stats['bytes_freed'])}")
    if stats["errors"]:
        print(f"  └ ⚠️ Ошибок:           {len(stats['errors'])}")
        for err in stats["errors"]:
            print(f"     - {err}")
    else:
        print("  └ 🟢 Статус:           УСПЕШНО (Все целевые папки чисты)")
    print("=" * 60 + "\n")

    return 0 if not stats["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())



# python clean_cache.py