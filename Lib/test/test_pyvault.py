import sys
import unittest
import types

class PyVaultTest(unittest.TestCase):
    def setUp(self):
        # Reset color to 0 before each test
        if hasattr(sys, 'set_color'):
            sys.set_color(0)

    def tearDown(self):
        if hasattr(sys, 'set_color'):
            sys.set_color(0)

    def _make_cycles(self, count, color):
        import weakref

        class Node:
            __slots__ = ("other", "__weakref__")

        refs = []
        sys.set_color(color)
        try:
            for _ in range(count):
                a = Node()
                b = Node()
                a.other = b
                b.other = a
                refs.append(weakref.ref(a))
                refs.append(weakref.ref(b))
            return refs
        finally:
            sys.set_color(0)

    def test_thread_color(self):
        """Test setting and getting thread security color."""
        if not hasattr(sys, 'set_color'):
            self.skipTest("PyVault APIs not available")

        sys.set_color(100)
        self.assertEqual(sys.get_color(), 100)

        sys.set_color(0)
        self.assertEqual(sys.get_color(), 0)

        with self.assertRaises(ValueError):
            sys.set_color(-1)
        with self.assertRaises(ValueError):
            sys.set_color(256)

    def test_object_tagging(self):
        """Test tagging objects and retrieving tags."""
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        obj = [1, 2, 3]
        sys.set_obj_tag(obj, 42)
        self.assertEqual(sys.get_obj_tag(obj), 42)

        # New objects should inherit current thread color by default
        sys.set_color(0)
        new_obj = {}
        self.assertEqual(sys.get_obj_tag(new_obj), 0)

        sys.set_color(7)
        colored_obj = {}
        self.assertEqual(sys.get_obj_tag(colored_obj), 7)

    def test_access_control(self):
        """Test that access is denied when colors don't match."""
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        # Create a "secret" object and tag it with color 1
        secret = type('Secret', (), {'data': 'sensitive info'})()
        sys.set_obj_tag(secret, 1)

        sys.set_color(0)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            _ = secret.data

        # Accessing with matching color 1 should be fine
        sys.set_color(1)
        self.assertEqual(secret.data, 'sensitive info')

        # Accessing with wrong color 2 should raise PermissionError
        sys.set_color(2)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            _ = secret.data

    def test_public_object_access(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        obj = type('Public', (), {'data': 'public info'})()
        sys.set_obj_tag(obj, 0)

        for color in (0, 1, 2, 7):
            sys.set_color(color)
            self.assertEqual(obj.data, 'public info')

    def test_access_control_buffer(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        data = bytearray(b"abc")
        sys.set_obj_tag(data, 3)

        sys.set_color(4)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            memoryview(data)

        sys.set_color(3)
        self.assertEqual(memoryview(data).tobytes(), b"abc")

    def test_access_control_capi(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import ctypes

        secret = type('Secret', (), {'data': 'sensitive info'})()
        sys.set_obj_tag(secret, 1)

        sys.set_color(2)
        pythonapi = ctypes.pythonapi
        pythonapi.PyObject_GetAttrString.restype = ctypes.py_object
        pythonapi.PyObject_GetAttrString.argtypes = [ctypes.py_object, ctypes.c_char_p]

        with self.assertRaisesRegex((PermissionError, ctypes.ArgumentError), "PyVault: Security violation"):
            pythonapi.PyObject_GetAttrString(secret, b"data")

    def test_ctypes_string_at_blocked(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import ctypes

        buf = ctypes.create_string_buffer(b"vault")
        ptr = ctypes.addressof(buf)

        sys.set_color(1)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            ctypes.string_at(ptr, 5)

        sys.set_color(0)
        self.assertEqual(ctypes.string_at(ptr, 5), b"vault")

    def test_ctypes_wstring_at_blocked(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import ctypes

        buf = ctypes.create_unicode_buffer("vault")
        ptr = ctypes.addressof(buf)

        sys.set_color(1)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            ctypes.wstring_at(ptr)

        sys.set_color(0)
        self.assertEqual(ctypes.wstring_at(ptr), "vault")

    def test_ctypes_memoryview_at_blocked(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import ctypes

        buf = ctypes.create_string_buffer(b"vault")
        ptr = ctypes.addressof(buf)

        sys.set_color(1)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            ctypes.memoryview_at(ptr, 5)

        sys.set_color(0)
        self.assertEqual(ctypes.memoryview_at(ptr, 5).tobytes(), b"vault")

    def test_import_module_blocked(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import importlib

        mod = types.ModuleType("pyvault_test_module")
        sys.set_obj_tag(mod, 1)

        sys.modules["pyvault_test_module"] = mod
        try:
            sys.set_color(2)
            with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
                importlib.import_module("pyvault_test_module")
        finally:
            sys.modules.pop("pyvault_test_module", None)

    def test_parallel_cross_color_access(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import threading

        secret = type('Secret', (), {'data': 'sensitive info'})()
        sys.set_obj_tag(secret, 1)

        results = []

        def worker():
            try:
                sys.set_color(2)
                _ = secret.data
            except PermissionError:
                results.append(True)
            except Exception as exc:
                results.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        self.assertEqual(results, [True])

    def test_gc_pressure_cross_color(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import gc

        sys.set_color(3)
        objs = [type('Secret', (), {'data': i})() for i in range(2000)]
        for obj in objs:
            sys.set_obj_tag(obj, 3)

        sys.set_color(4)
        gc.collect()
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            _ = objs[0].data

        sys.set_color(3)
        self.assertEqual(objs[0].data, 0)

    def test_gc_color_isolation_collect(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import gc

        refs = self._make_cycles(200, 1)
        sys.set_color(2)
        try:
            for _ in range(5):
                gc.collect(0)
            alive = any(r() is not None for r in refs)
        finally:
            sys.set_color(0)
        self.assertTrue(alive)

        sys.set_color(1)
        try:
            for _ in range(20):
                gc.collect(2)
                if all(r() is None for r in refs):
                    break
        finally:
            sys.set_color(0)
        self.assertTrue(all(r() is None for r in refs))

    def test_gc_pressure_tail_latency(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        import gc

        refs = self._make_cycles(1000, 5)
        sys.set_color(5)
        try:
            completed = False
            for _ in range(50):
                gc.collect(2)
                if all(r() is None for r in refs):
                    completed = True
                    break
        finally:
            sys.set_color(0)
        self.assertTrue(completed)

    def test_access_control_super(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        class Base:
            value = 10

        class Derived(Base):
            def get_value(self):
                return super().value

        obj = Derived()
        sys.set_obj_tag(obj, 1)

        sys.set_color(1)
        self.assertEqual(obj.get_value(), 10)

        sys.set_color(2)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            obj.get_value()

    def test_access_control_delete_attr(self):
        if not hasattr(sys, 'set_obj_tag'):
            self.skipTest("PyVault APIs not available")

        class Holder:
            pass

        obj = Holder()
        obj.value = 123
        sys.set_obj_tag(obj, 3)

        sys.set_color(3)
        del obj.value
        obj.value = 456

        sys.set_color(4)
        with self.assertRaisesRegex(PermissionError, "PyVault: Security violation"):
            del obj.value

    def test_code_color_auto_switch(self):
        """Test that code objects automatically switch thread color."""
        if not hasattr(sys, 'set_code_color'):
            self.skipTest("PyVault APIs not available")

        def secure_func():
            return sys.get_color()

        # Initially, color is 0
        sys.set_color(0)
        self.assertEqual(secure_func(), 0)

        # Set code color to 5
        sys.set_code_color(secure_func.__code__, 5)

        # Calling the function should switch to color 5 and then back to 0
        self.assertEqual(secure_func(), 5)
        self.assertEqual(sys.get_color(), 0)

    def test_nested_color_switch(self):
        """Test nested function calls with different colors."""
        if not hasattr(sys, 'set_code_color'):
            self.skipTest("PyVault APIs not available")

        def inner():
            return sys.get_color()

        def outer():
            c1 = sys.get_color()
            c2 = inner()
            c3 = sys.get_color()
            return c1, c2, c3

        sys.set_code_color(outer.__code__, 10)
        sys.set_code_color(inner.__code__, 20)

        # outer (10) -> inner (20) -> outer (10) -> main (0)
        self.assertEqual(outer(), (10, 20, 10))
        self.assertEqual(sys.get_color(), 0)

    def test_module_sealing(self):
        """Test sealing a module and checking its member tags."""
        if not hasattr(sys, 'seal'):
            self.skipTest("PyVault seal API not available")

        import math
        sys.seal(math, 99)

        # Check module object tag
        self.assertEqual(sys.get_obj_tag(math), 99)

        # Check a function in the module
        sys.set_color(99)
        self.assertEqual(sys.get_obj_tag(math.sqrt), 99)
        sys.set_color(0)

        # Check code object tag
        # Note: math.sqrt is usually a built-in function, so it won't have a co_vault_color
        # Let's use a user-defined module
        mod_name = 'test_mod'
        mod = types.ModuleType(mod_name)
        def test_func(): return 1
        mod.test_func = test_func
        sys.seal(mod, 77)

        self.assertEqual(sys.get_obj_tag(mod), 77)
        sys.set_color(77)
        self.assertEqual(sys.get_obj_tag(mod.test_func), 77)
        self.assertEqual(sys.get_color(), 77)
        sys.set_color(0)

        sys.set_color(77)
        self.assertEqual(mod.test_func(), 1)

        # Test access violation on sealed module
        sys.set_color(88)
        with self.assertRaises(PermissionError):
            _ = mod.test_func

        sys.set_color(0)
        with self.assertRaises(PermissionError):
            _ = mod.test_func

if __name__ == '__main__':
    unittest.main()
