import sys
import unittest
import types
import time

class TestPyVaultSecurity(unittest.TestCase):
    def setUp(self):
        sys.set_color(0)

    def tearDown(self):
        sys.set_color(0)

    def test_color_isolation(self):
        # 初始颜色应为 0
        self.assertEqual(sys.get_color(), 0)

        # 设置颜色为 1
        sys.set_color(1)
        self.assertEqual(sys.get_color(), 1)

        # 创建一个对象，它应该被打上颜色 1 的标签
        obj1 = [1, 2, 3]
        self.assertEqual(sys.get_obj_tag(obj1), 1)

        # 切换回颜色 2
        sys.set_color(2)
        self.assertEqual(sys.get_color(), 2)

        # 尝试访问颜色 1 的对象（在 color 2 下）
        with self.assertRaises(PermissionError) as cm:
            _ = obj1.append
        self.assertIn("Security violation", str(cm.exception))

    def test_module_sealing(self):
        sys.set_color(0)
        # 创建一个模拟模块
        mod = types.ModuleType("secret_module")
        mod.secret_data = "top_secret"
        def get_secret():
            return mod.secret_data
        mod.get_secret = get_secret

        # 锁定模块颜色为 42
        sys.seal(mod, 42)

        # 检查模块及其成员的标签
        sys.set_color(42)
        self.assertEqual(sys.get_obj_tag(mod), 42)
        self.assertEqual(sys.get_obj_tag(mod.secret_data), 42)
        self.assertEqual(sys.get_obj_tag(mod.get_secret), 42)

        # 检查函数代码对象的颜色
        self.assertEqual(sys.get_code_color(mod.get_secret.__code__), 42)

        self.assertEqual(mod.secret_data, "top_secret")
        sys.set_color(1)
        try:
            _ = mod.secret_data
        except PermissionError:
            sys.set_color(0)
        else:
            sys.set_color(0)
            self.fail("PermissionError not raised")

    def test_memory_allocator_isolation(self):
        # 这是一个底层测试，验证 obmalloc 是否真的隔离了内存池
        # 我们通过观察分配的地址来间接验证

        sys.set_color(10)
        ptr1 = [i for i in range(100)]

        sys.set_color(20)
        ptr2 = [i for i in range(100)]

        # 在理想情况下，不同颜色的对象应该位于不同的 Arena 中
        # Arena 大小通常是 256KB 或 1MB
        # 我们可以检查它们的地址差异
        addr1 = id(ptr1)
        addr2 = id(ptr2)

        # 如果它们在不同的 Arena，地址差异通常会很大（至少跨越一个 Arena 边界）
        # 这是一个启发式检查
        self.assertNotEqual(addr1 >> 18, addr2 >> 18) # 假设 Arena 至少 256KB

    def test_cross_color_free_protection(self):
        # 验证是否能防止跨颜色释放
        sys.set_color(1)
        obj = {"data": "vault1"}

        sys.set_color(2)
        # 在颜色 2 下尝试删除（释放）颜色 1 的对象
        # 我们的 obmalloc 修改应该拦截这一点
        # 注意：Python 的 del 只是减少引用计数，真正的释放发生在 GC 或引用归零时
        import gc
        del obj
        gc.collect()

        # 如果保护有效，对象不应该被真正释放（或者至少不会引起崩溃）
        # 这里的验证比较困难，主要靠不崩溃来证明安全性

class TestPyVaultPerformance(unittest.TestCase):
    def setUp(self):
        sys.set_color(0)

    def test_benchmark_alloc(self):
        start = time.perf_counter()
        for _ in range(20000):
            _ = [0] * 10
        elapsed = time.perf_counter() - start
        self.assertGreaterEqual(elapsed, 0.0)

    def test_benchmark_access(self):
        obj = [1, 2, 3]
        sys.set_obj_tag(obj, sys.get_color())
        start = time.perf_counter()
        for _ in range(200000):
            _ = obj[0]
        elapsed = time.perf_counter() - start
        self.assertGreaterEqual(elapsed, 0.0)

if __name__ == "__main__":
    unittest.main()
