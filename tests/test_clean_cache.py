# tests/test_clean_cache.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_clean_cache
ROLE: Юнит-тесты сервиса очистки кэша (clean_cache.py).
==============================================================================
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from clean_cache import CacheCleaner, format_bytes


class TestCacheCleaner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_clean_cache_"))

        # Создаем макет структуры проекта
        self.runtime_dir = self.temp_dir / "CFG" / "runtime" / "u96_test"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "litusdt.json").write_text('{"mock": 1}', encoding="utf-8")

        self.cache_dir = self.temp_dir / "CACHE"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "auto_closer_state.json").write_text("{}", encoding="utf-8")
        (self.cache_dir / "specifications.json").write_text('{"symbols": []}', encoding="utf-8")

        self.ledgers_dir = self.temp_dir / "ANALYTICS" / "LEDGERS"
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)
        (self.ledgers_dir / "trades_ledger.txt").write_text("trade 1\n", encoding="utf-8")

        self.stats_dir = self.temp_dir / "ANALYTICS" / "STATS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        (self.stats_dir / "analytics.json").write_text("{}", encoding="utf-8")

        self.images_dir = self.temp_dir / "ANALYTICS" / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        (self.images_dir / "balance_curve.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        self.leaderboard_file = self.temp_dir / "ANALYTICS" / "leaderboard.txt"
        self.leaderboard_file.write_text("LEADERBOARD", encoding="utf-8")

        self.logs_dir = self.temp_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "all.log").write_text("log line 1\n", encoding="utf-8")
        (self.logs_dir / "tests.log").write_text("test log\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dry_run_does_not_delete_anything(self):
        cleaner = CacheCleaner(project_root=self.temp_dir, dry_run=True)
        stats = cleaner.clean_all()

        self.assertGreater(stats["files_removed"], 0)
        self.assertTrue((self.cache_dir / "auto_closer_state.json").exists())
        self.assertTrue(self.leaderboard_file.exists())
        self.assertTrue((self.logs_dir / "all.log").exists())

    def test_clean_all_deletes_all_targets_and_preserves_empty_dirs(self):
        cleaner = CacheCleaner(project_root=self.temp_dir, dry_run=False, keep_specs=False)
        stats = cleaner.clean_all()

        self.assertEqual(len(stats["errors"]), 0)
        self.assertFalse(self.leaderboard_file.exists())
        self.assertFalse((self.cache_dir / "auto_closer_state.json").exists())
        self.assertFalse((self.cache_dir / "specifications.json").exists())
        self.assertFalse((self.runtime_dir / "litusdt.json").exists())
        self.assertFalse((self.logs_dir / "all.log").exists())

        # Корневые папки должны остаться существующими и пустыми
        self.assertTrue((self.temp_dir / "CFG" / "runtime").exists())
        self.assertEqual(list((self.temp_dir / "CFG" / "runtime").iterdir()), [])
        self.assertTrue(self.cache_dir.exists())
        self.assertEqual(list(self.cache_dir.iterdir()), [])
        self.assertTrue(self.ledgers_dir.exists())
        self.assertEqual(list(self.ledgers_dir.iterdir()), [])
        self.assertTrue(self.stats_dir.exists())
        self.assertEqual(list(self.stats_dir.iterdir()), [])
        self.assertTrue(self.images_dir.exists())
        self.assertEqual(list(self.images_dir.iterdir()), [])
        self.assertTrue(self.logs_dir.exists())
        self.assertEqual(list(self.logs_dir.iterdir()), [])

    def test_keep_specs_preserves_specifications_json(self):
        cleaner = CacheCleaner(project_root=self.temp_dir, dry_run=False, keep_specs=True)
        stats = cleaner.clean_all()

        self.assertEqual(len(stats["errors"]), 0)
        self.assertTrue((self.cache_dir / "specifications.json").exists())
        self.assertFalse((self.cache_dir / "auto_closer_state.json").exists())

    def test_handles_nonexistent_directories_gracefully(self):
        empty_root = self.temp_dir / "nonexistent"
        cleaner = CacheCleaner(project_root=empty_root, dry_run=False)
        stats = cleaner.clean_all()

        self.assertEqual(len(stats["errors"]), 0)
        self.assertEqual(stats["files_removed"], 0)
        # Папки должны быть созданы пустыми
        self.assertTrue((empty_root / "CACHE").exists())
        self.assertTrue((empty_root / "logs").exists())

    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), "500.0 B")
        self.assertIn("KB", format_bytes(2048))
        self.assertIn("MB", format_bytes(1048576 * 5))


if __name__ == "__main__":
    unittest.main()
