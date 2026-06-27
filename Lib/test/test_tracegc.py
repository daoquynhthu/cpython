"""Shell test for tracing GC infrastructure.

Filled incrementally as Phase 0 sub-tasks land.
"""

import unittest
from test import support


class TestTraceGCBasic(unittest.TestCase):
    """Smoke tests for tracing GC build."""

    def test_import_gc(self):
        import gc
        self.assertTrue(hasattr(gc, 'collect'))

    def test_manual_collect(self):
        import gc
        gc.collect()

    def test_basic_create(self):
        x = [1, 2, 3]
        self.assertEqual(len(x), 3)


if __name__ == '__main__':
    unittest.main()
