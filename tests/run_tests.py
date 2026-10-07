# ==============================================================================
# Path: tests/run_tests.py
# Role: Безопасный запуск тестов: все изменяемые данные (CFG/CACHE/ANALYTICS/logs)
#       берутся из временной копии (CRON3_SANDBOX_DIR), боевые файлы не затрагиваются.
# Usage: python tests/run_tests.py [-v] [pattern]
# ==============================================================================

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="cron3_test_sandbox_"))
    os.environ["CRON3_SANDBOX_DIR"] = str(sandbox)
    os.chdir(ROOT)
    for p in (str(ROOT), str(ROOT.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)

    verbosity = 2 if "-v" in sys.argv else 1
    args = [a for a in sys.argv[1:] if a != "-v"]
    pattern = args[0] if args else "test*.py"

    suite = unittest.defaultTestLoader.discover(str(TESTS_DIR), pattern=pattern, top_level_dir=str(TESTS_DIR))
    try:
        result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
