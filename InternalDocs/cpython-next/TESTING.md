# TESTING.md — 追踪式 GC 改造工程测试手册

> 本文档记录每个 Phase 的测试执行命令、期望输出、回归基线。
> 所有测试在 `Py_TRACING_GC = 1` 构建和 `Release`（引用计数）双模式下
> 执行，以验证两套路径互不影响。

---

## 全局测试命令

```powershell
# ── 构建 ──
# Release（引用计数模式，不变）
& "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" PCbuild\pcbuild.proj /p:Configuration=Release

# Py_TRACING_GC 模式
& "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" PCbuild\pcbuild.proj /p:Configuration=Release /p:ExtraDefines=Py_TRACING_GC
```

```bash
# POSIX
make -j$(nproc)            # Release
make -j$(nproc) TRACING_GC=1  # Py_TRACING_GC
```

```powershell
# ── 运行测试 ──
python -m test test_tracegc -v
python -m test test_builtin -v    # 内置类型基础功能
python -m test test_gc -v         # 现有 GC 测试（确认未破坏引用计数模式）
```

---

## Phase 0 测试

### 桩验证
```powershell
# 确认桩函数链接正确，启动不崩溃
python -c "print('hello world')"
# 期望: hello world
```

### 编译开关验证
```powershell
# 确认 Py_TRACING_GC 被正确定义
python -c "import sysconfig; print(sysconfig.get_config_var('Py_TRACING_GC'))"
# 期望: 1
```

### 对象头验证
```powershell
python -c "
import ctypes, sys
ob = object()
# 验证 sizeof(PyObject) == 16
print(sys.getsizeof(ob))
# 验证 ob_type 偏移正确
addr = id(ob)
type_ptr = ctypes.c_void_p.from_address(addr + 8)
print(hex(type_ptr.value))
"
# 期望: 32 (含 ob_refcnt 保留位) 或 16 (干净对象)
# 期望: type_ptr 指向 &PyBaseObject_Type
```

### GC 分配验证
```powershell
python -c "
import gc
# 创建对象，检查 GC 是否跟踪
x = [1, 2, 3]; y = {'a': x}
gc.collect()
print('GC collect OK')
"
# 期望: GC collect OK
```

### 标记验证
```powershell
python -c "
import gc, sys
gc.disable()  # 手动控制
x = []
gc.collect()  # x 存活，不应被回收
print('manual collect OK')
"
# 期望: manual collect OK
```

---

## Phase 1 测试

### C API 兼容性
```powershell
# 内置 C 扩展 import 测试
python -c "
import _csv
import _socket
import _json
import _datetime
import _struct
print('All C extensions imported OK')
"
# 期望: All C extensions imported OK
```

### Py_INCREF/DECREF 宏兼容
```c
// test_incref_compat.c (C 测试文件)
#include <Python.h>
void test(void) {
    PyObject *obj = PyLong_FromLong(42);
    Py_INCREF(obj);         // 追踪式 GC 下为空操作
    Py_ssize_t ref = Py_REFCNT(obj);  // 应返回 1
    assert(ref == 1);
    Py_DECREF(obj);         // 追踪式 GC 下为空操作
    // 不调用 Py_DECREF 也不会泄漏（GC 管理）
}
```

---

## Phase 2 测试

### 安全点检查
```powershell
# 长循环，GC 请求安全点，应正常到达
python -c "
import gc, threading, time
def busy():
    for i in range(1000000): pass
t = threading.Thread(target=busy)
t.start()
time.sleep(0.1)
gc.collect()  # 请求安全点
t.join()
print('safepoint OK')
"
# 期望: safepoint OK
```

---

## Phase 3 测试

### Card Table 写屏障
```powershell
python -c "
import gc
# 大量对象分配，触发分代 GC
for _ in range(10000):
    x = [object() for _ in range(10)]
gc.collect()
print('generational GC OK')
"
# 期望: generational GC OK
```

---

## 回归基线（跨 Phase 不变）

| 测试 | 命令 | 期望 |
|------|------|------|
| 空启动 | `python -c ""` | 退出码 0，无 stderr |
| Hello World | `python -c "print('hello')"` | 输出 `hello` |
| 算术 | `python -c "print(2+2)"` | 输出 `4` |
| 列表操作 | `python -c "x=[1,2,3];print(len(x))"` | 输出 `3` |
| 字典操作 | `python -c "x={'a':1};print(x['a'])"` | 输出 `1` |
| 异常处理 | `python -c "try:1/0\nexcept:print('ok')"` | 输出 `ok` |
| 内置 C 扩展 | `python -c "import _csv; import _socket; import _json"` | 无 ImportError |
| 大对象 | `python -c "x=bytearray(10**7);print(len(x))"` | 输出 `10000000` |

---

> 最后更新：2026-06-27
