#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: clear_cache.py
ROLE: Алиас сервиса очистки кэша (clean_cache.py) для удобства вызова.
==============================================================================
"""

import sys
from clean_cache import main

if __name__ == "__main__":
    sys.exit(main())
