# tests/test_grid_volume_calculator.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_grid_volume_calculator.py
Role: Институциональный юнит-тест калькулятора сетки объемов (GridVolumeCalculator)
==============================================================================
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from CORE.SERVICES.TOOLS.grid_volume_calculator import (
    calculate_grid_volumes,
    print_grid_table,
    patch_universe_file,
)


class TestGridVolumeCalculator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_grid_vol_"))

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_calculate_grid_volumes_equal_multiplier(self):
        """Multiplier 1.0 дает равномерное распределение объема."""
        vols, v0 = calculate_grid_volumes(levels=4, multiplier=1.0, total_pct=100.0)
        self.assertEqual(len(vols), 4)
        self.assertEqual(vols, [25.0, 25.0, 25.0, 25.0])
        self.assertEqual(sum(vols), 100.0)
        self.assertEqual(v0, 25.0)

    def test_calculate_grid_volumes_geometric_progression(self):
        """Geometric multiplier 1.1 дает возрастающие объемы с точной суммой 100.0%."""
        vols, v0 = calculate_grid_volumes(levels=6, multiplier=1.1, total_pct=100.0)
        self.assertEqual(len(vols), 6)
        # Проверяем строгое возрастание
        for i in range(len(vols) - 1):
            self.assertLess(vols[i], vols[i + 1])
        # Точная сумма 100.00%
        self.assertEqual(round(sum(vols), 2), 100.0)

    def test_calculate_grid_volumes_single_level(self):
        """Сетка из одного уровня занимает ровно 100%."""
        vols, v0 = calculate_grid_volumes(levels=1, multiplier=1.2, total_pct=100.0)
        self.assertEqual(vols, [100.0])
        self.assertEqual(v0, 100.0)

    def test_calculate_grid_volumes_invalid_levels(self):
        """Невалидное количество уровней (<= 0) вызывает ValueError."""
        with self.assertRaises(ValueError):
            calculate_grid_volumes(levels=0, multiplier=1.1)
        with self.assertRaises(ValueError):
            calculate_grid_volumes(levels=-5, multiplier=1.1)

    def test_print_grid_table_runs_cleanly(self):
        """print_grid_table печатает таблицу без исключений."""
        vols, _ = calculate_grid_volumes(levels=4, multiplier=1.1)
        # Без invest_size
        print_grid_table(levels=4, multiplier=1.1, volumes=vols)
        # С invest_size
        print_grid_table(levels=4, multiplier=1.1, volumes=vols, invest_size=400.0)

    def test_patch_universe_file(self):
        """patch_universe_file корректно обновляет volumes в JSON шаблоне вселенной."""
        u_file = self.temp_dir / "test_universe.json"
        initial_cfg = {
            "LONG": {
                "grid": {
                    "0": {"indent": 0.0, "volume": 50.0},
                    "1": {"indent": -5.0, "volume": 50.0},
                }
            },
            "SHORT": {
                "grid": {
                    "0": {"indent": 0.0, "volume": 50.0},
                    "1": {"indent": 5.0, "volume": 50.0},
                }
            },
        }
        u_file.write_text(json.dumps(initial_cfg, indent=2), encoding="utf-8")

        # Патчим LONG сторону
        patch_universe_file(u_file, levels=2, multiplier=1.5, target_side="LONG")

        updated = json.loads(u_file.read_text(encoding="utf-8"))
        long_vols = [updated["LONG"]["grid"][k]["volume"] for k in ("0", "1")]
        self.assertEqual(round(sum(long_vols), 2), 100.0)
        self.assertLess(long_vols[0], long_vols[1])

        # SHORT остался нетронутым
        short_vols = [updated["SHORT"]["grid"][k]["volume"] for k in ("0", "1")]
        self.assertEqual(short_vols, [50.0, 50.0])


if __name__ == "__main__":
    unittest.main()
