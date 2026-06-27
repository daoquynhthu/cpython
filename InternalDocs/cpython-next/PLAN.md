# PLAN.md — CPython 追踪式 GC 改造工程计划

> **本文档地位**：执行计划。高于 AGENT.md 的任务分派，低于 ARCHITECTURE.md 的设计约束。
> **生效规则**：本文档由人类审阅冻结后生效。Agent 可追加已完成条目的状态，不修改架构级决策。
> **文档尊卑链**：`ARCHITECTURE.md > PLAN.md > AGENT.md`

---

## 总体路线图

```
                          ┌─────────────────────────────────────────┐
                          │  Phase 0: Tracing GC 基础设施          │
                          │  (编译开关 + 对象头 + 非移动 Mark-Sweep)│
                          └────────────┬────────────────────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                   ▼
          ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
          │ Phase 1:        │ │ Phase 2:        │ │ (独立)          │
          │ C API 兼容      │ │ 根枚举 + 安全点  │ │                 │
          │ + 分配器三堆分离 │ │ + 保守 C 栈扫描  │ │                 │
          └────────┬────────┘ └────────┬────────┘ │                 │
                   │                   │          │                 │
                   └───────┬───────────┘          │                 │
                           ▼                      ▼                 ▼
                  ┌─────────────────┐ ┌──────────────────────────────┐
                  │ Phase 3:        │ │ Phase 5: JIT-GC 集成        │
                  │ 分代收集        │ │ (OopMap + Safepoint + 根枚举)│
                  │ + Card Table    │ └──────────────────────────────┘
                  │ + 写屏障        │
                  └────────┬────────┘
                           ▼
                  ┌─────────────────┐
                  │ Phase 4:        │
                  │ 可移动 GC       │
                  │ (TraceGC-safe)  │
                  └─────────────────┘
```

### Phase 依赖关系（DAG 边）

| 依赖 | 被依赖 | 含义 |
|------|--------|------|
| Phase 0 | Phase 1, 2, 3, 5 | Phase 0 是所有后续阶段的基础 |
| Phase 1 | Phase 3, 5 | C API 兼容 + 分配器分离后，才能安全做分代 |
| Phase 2 | Phase 3 | 安全点 + 根枚举完整后，分代 GC 的根集合才可用 |
| Phase 3 | Phase 4 | 非移动分代 GC 稳定后，才解锁移动 GC |
| Phase 1 | Phase 4 | TraceGC-safe 类型声明依赖 C API 兼容层就绪 |

---

## Phase 0: Tracing GC 基础设施

**目标**：在 `Py_TRACING_GC` 编译开关下，构建一个可运行的 non-moving mark-sweep 追踪式 GC。
**不包含**：C API 兼容（Phase 1）、分代（Phase 3）、JIT 集成（Phase 5）。
**通过条件**：CPython 能启动并运行 `test_basic.py`（`print("hello")` 级测试），无段错误。

### 任务 DAG（Phase 0 内部）

```
0.0 ──────────────────────────────────────────────┐
   │                                                │
   ├→ 0.1 → 0.2 → 0.3 → 0.4 → 0.5 → 0.6 → 0.7 → 0.8 → 0.9
   │                              ↓
   │                           0.5a (并行)
   └──────────────────────────────────────────────────┘
                    (0.0 桩/构建基线贯穿全部)
```

---

### 0.0 测试桩 + 构建基线 + 回归框架

| 属性 | 值 |
|------|----|
| **架构参考** | §13 条件编译策略（文件结构参考） |
| **说明** | **桩函数**：为 Phase 0 所有 `_PyGC_*` 新 API 创建空桩（stub），返回错误/空值，确保 `#if defined(Py_TRACING_GC)` 分支下 CPython 能链接并启动到 `Py_Initialize()` 入口处。桩包含：`_PyGC_InitHeap`(→返回 -1 标注未实现)、`_PyGC_EdenAlloc`(→返回 NULL 回退 pymalloc)、`_PyGC_MarkRoots`(→空操作)、`_PyGC_ProcessMarkStack`(→空操作)、`_PyGC_SweepOld`(→返回 0)、`_PyGC_CollectYoung/Old`(→返回 0)。<br><br>**构建基线**：编写或复用构建脚本，确保 `Py_TRACING_GC=1 nmake` 从零构建通过。建立两个构建配置的对比 CI：`Release`（引用计数模式，不变）和 `Release-Py_TRACING_GC`（追踪式 GC 模式）。<br><br>**回归框架**：在 `Lib/test/` 下新增 `test_tracegc.py`（初始为空壳），后续逐步添加 GC 行为验证用例。定义 `TESTING.md` 记录所有 Phase 心得的测试执行命令和期望输出。 |
| **边界** | 桩函数不做任何有效工作；仅保证链接和启动不崩溃；回归框架不修改现有测试 |
| **文件** | 新建 `Python/gc_stubs.c`（集中存放所有桩）；新建 `Lib/test/test_tracegc.py`；新建 `InternalDocs/cpython-next/TESTING.md`；修改构建系统文件（§0.1） |
| **门禁** | 桩函数链接正确；Py_TRACING_GC 构建 `_Py_Initialize()` 返回 0 而非崩溃；两个构建配置并行存在 |
| **测试** | 执行 `python -c "print('hello')"` 在 Py_TRACING_GC 下输出 `hello` 并正常退出；`python -m test test_tracegc -v` 至少运行一个空测试 |
| **通过条件** | Py_TRACING_GC + Release 双构建均通过；桩函数全部被链接器解析；`Lib/test/test_tracegc.py` 在 `python -m test` 下可发现并运行 |

### 0.1 编译开关定义

| 属性 | 值 |
|------|----|
| **架构参考** | §2.1–§2.3 编译开关体系 |
| **说明** | 在构建系统中定义 `Py_TRACING_GC` 宏，与 `Py_GIL_DISABLED` 互斥检查 |
| **边界** | 仅 PCbuild + Makefile；不改 Include/ 下的头文件逻辑 |
| **文件** | `PCbuild/pythoncore.vcxproj`, `Makefile.pre.in`, `Include/pyport.h`（互斥守卫） |
| **门禁** | 构建后 `grep Py_TRACING_GC pyconfig.h` 可见定义；`#if defined(Py_GIL_DISABLED) && defined(Py_TRACING_GC)` 触发编译错误 |
| **测试** | 空构建：`nmake` / MSBuild 成功 |
| **通过条件** | 构建产物中包含 `-DPy_TRACING_GC` 编译标志 |

### 0.2 PyObject 结构体改造

| 属性 | 值 |
|------|----|
| **架构参考** | §3.1–§3.3 PyObject 对象模型、§21.1 偏移量速查 |
| **说明** | 在 `#if defined(Py_TRACING_GC)` 分支下重定义 `struct _object`，加入 `ob_gc_state`（32位）、`ob_gc_age`（16位），移除 `ob_refcnt`，保持 `ob_type` 偏移 +8、`sizeof(PyObject)=16` |
| **边界** | 不改 `PyVarObject`；不改 `ob_flags` 位分配；不加新字段到 `_object` 联合体之外；`PyGC_Head` 保留占位（§3.4） |
| **文件** | `Include/object.h`（`struct _object`）、`Include/cpython/object.h`（确认 `PyTypeObject` 不变） |
| **门禁** | `static_assert(sizeof(PyObject) == 16)`、`static_assert(offsetof(PyObject, ob_type) == 8)` |
| **测试** | 编译期断言通过；Python 启动后 `sizeof(int.__basicsize__)` 等同 |
| **通过条件** | 两个 static_assert 编译通过；Py_TRACING_GC 构建启动到 import 阶段不崩溃 |

### 0.3 GC 运行时状态

| 属性 | 值 |
|------|----|
| **架构参考** | §4.1–§4.2 GC 运行时状态 |
| **说明** | 在 `#if defined(Py_TRACING_GC)` 分支下定义新 `struct _gc_runtime_state`，含 `gc_heap_state`（eden/survivor/old 起始地址）、`gc_mark_stack`、标记栈、`enabled/collecting` 等基础字段 |
| **边界** | 不取代现有 `_gc_runtime_state`；保留引用计数模式的世代链表字段（`generations[3]`）；新结构只在 `Py_TRACING_GC` 分支生效 |
| **文件** | `Include/internal/pycore_interp_structs.h` |
| **门禁** | 新旧结构体不冲突；`sizeof` 不超出预期 |
| **测试** | 编译通过，启动后 `gcstate->enabled == 1` |
| **通过条件** | Py_TRACING_GC 构建下 `_gc_runtime_state` 内存布局断言通过 |

### 0.4 对象堆初始化

| 属性 | 值 |
|------|----|
| **架构参考** | §5.1–§5.3 分代堆布局（Phase 0 non-moving 子集）、§4.2 参数表 |
| **说明** | 实现 `_PyGC_InitHeap()`，使用 `VirtualAlloc`/`mmap` 分配连续地址空间作为对象堆；初始大小 16MB（eden）+ 4MB（survivor 保留）+ 256MB（old）；初始化 card table 数组但不启用 |
| **边界** | 只分配地址空间（不提交物理页全部）；不改造 pymalloc；大对象（>256KB）不走对象堆 |
| **文件** | 新建 `Python/gc_heap.c`、`Include/internal/pycore_gc.h`（函数声明） |
| **门禁** | heap 地址连续；eden_start < eden_end < survivor_start < survivor_end < old_start < old_end |
| **测试** | 解释器初始化后 `heap.old_end - heap.eden_start == 276MB` |
| **通过条件** | `_PyGC_InitHeap` 返回 0；三次 `_PyGC_EdenAlloc(64)` 返回不同地址且都在 eden 范围内 |

### 0.5 基础对象分配器

| 属性 | 值 |
|------|----|
| **架构参考** | §3.5 gc_alloc 分配路径、§5.3 Eden 区分配 |
| **说明** | 实现 `gc_alloc()` + `_PyGC_EdenAlloc()` bump-pointer 分配；Eden 满时触发的 slow path 先 `_PyGC_CollectYoung()` 再重试；重试失败走 `_PyObject_MallocWithType()` 后备 |
| **边界** | Phase 0 的 `_PyGC_CollectYoung` 退化为全量非移动 mark-sweep（不做复制）；`_PyObject_GC_Track/UnTrack` 为空操作（§3.5） |
| **文件** | 新建 `Python/gc_alloc.c`；修改 `Include/objimpl.h`（`PyObject_GC_New` 条件分支） |
| **门禁** | 分配对齐到 4 字节；`ob_gc_state` 初始化为 WHITE |
| **测试** | 连续分配 1000 个 64 字节块无重叠；Eden 满时触发 GC 并成功分配 |
| **通过条件** | `_PyObject_GC_New(PyLong_Type)` 返回非 NULL 且 `_PyGC_IsWhite(op) == 1` |

### 0.6 根枚举框架

| 属性 | 值 |
|------|----|
| **架构参考** | §8.1–§8.3 根枚举系统、§8.2 YOUNG_ONLY/ALL_GENERATIONS 模式 |
| **说明** | 实现 `_PyGC_MarkRoots()` 及其子访问器：运行时单例（None/True/False）、小整数缓存、字符串驻留池、内置类型对象、线程帧栈（`_PyGC_VisitFrameStack`）、模块 dict、builtins |
| **边界** | Phase 0 不做 C 栈保守扫描（Phase 2）；不做 OopMap（Phase 5）；仅精确枚举解释器帧根 |
| **文件** | 新建 `Python/gc_roots.c`、`Include/internal/pycore_gc.h` |
| **门禁** | 每个访问器不崩溃；帧栈扫描覆盖从 `localsplus` 到 `stackpointer` 范围内所有 `PyObject*` |
| **测试** | 在启动后调用 `_PyGC_MarkRoots` 并验证 None/True/False 被标记为 GREY |
| **通过条件** | `_PyGC_MarkRoots(YOUNG_ONLY)` 将所有已知根标记为 `_PyGC_COLOR_GREY` 并入栈 |

### 0.7 三色标记算法

| 属性 | 值 |
|------|----|
| **架构参考** | §6.1–§6.4 三色标记算法、§6.2 标记传播核心、§6.4 Epoch 机制 |
| **说明** | 实现 `_PyGC_ProcessMarkStack()` 循环处理标记栈；`_PyGC_VisitChild()` 回调：非容器类型直接标记 BLACK 不入栈（§3.6 修正）、容器类型 WHITE→GREY 入栈；实现 Epoch 轮次：`mark_stack_push` 时记录当前 epoch，颜色校验时匹配 epoch |
| **边界** | Phase 0 不处理卡表；不处理写屏障；仅全量标记（Eden 满时触发全量 sweep） |
| **文件** | 新建 `Python/gc_mark.c` |
| **门禁** | 不变量：栈空后所有可达对象为 BLACK；`_PyGC_IsWhite` 存活对象返回 0；循环图正确处理无栈溢出 |
| **测试** | 创建简单引用链 chain(A→B→C)，GC 标记后 A/B/C 都是 BLACK；创建循环 A→B→C→A，均标记为 BLACK |
| **通过条件** | 标记后 `_PyGC_GetColor(root)==BLACK && _PyGC_GetColor(unreachable)==WHITE` |

### 0.8 清除阶段 + Epoch 复位

| 属性 | 值 |
|------|----|
| **架构参考** | §6.3 清除阶段、§6.4 Epoch 复位机制、§11.2 tp_dealloc 分级策略（Legacy 模式） |
| **说明** | 实现 `_PyGC_SweepOld()`：扫描对象堆所有存活对象；白色对象先 `tp_finalize` → `tp_clear` → `tp_dealloc`（清理 C 资源）→ 可选 `tp_free` → 标记 FREE；epoch 复位：sweep 后将所有 BLACK 对象颜色复位为 WHITE（为下一轮准备）；`_PyGC_Collect` 主流程编排函数 |
| **边界** | Phase 0 对所有对象使用 Legacy 模式（保留完整 dealloc 语义）；不处理 `tp_free` 替换 |
| **文件** | 新建 `Python/gc_sweep.c`、`Python/gc_collect.c`（`_PyGC_MaybeCollect` + `_PyGC_CollectOld`） |
| **门禁** | 不 double-free；复活对象不被回收；epoch 复位后所有存活对象颜色正确 |
| **测试** | 创建对象并在 GC 后验证存活；强制 GC 后已死对象 ob_gc_state==FREE；复活对象在 finalize 后不被收集 |
| **通过条件** | 启动后调用 `_PyGC_CollectOld(tstate)` 返回 ≥0，不崩溃；所有对象颜色正确复位 |

### 0.9 集成冒烟测试

| 属性 | 值 |
|------|----|
| **架构参考** | §10 GC 收集主流程（整体编排） |
| **说明** | 构建端到端冒烟测试，验证 Phase 0 各组件（编译开关 → 对象头 → 堆初始化 → 分配 → 根枚举 → 标记 → 清除）集成工作的正确性。测试场景：(1) `python -c "print('hello')"` 正常退出。(2) `python -c "x = [1,2,3]; print(len(x))"` 列表正常分配。(3) 显式 `gc.collect()` 调用返回正常。(4) 循环垃圾结构自动回收（创建循环引用 → 手动触发全部收集 → 验证白色对象被释放）。(5) `Lib/test/test_tracegc.py` 内建的单元测试全部通过。 |
| **边界** | 不覆盖 C 扩展兼容（Phase 1）；不覆盖分代（Phase 3）；不覆盖复杂 Python 程序。仅验证追踪式 GC 在纯 Python 字节码下的基础正确性 |
| **文件** | `Lib/test/test_tracegc.py`（补充已有）+ `Python/gc_collect.c`（`_PyGC_MaybeCollect` 入口正确性） |
| **门禁** | 测试 (1)(2)(3)(4)(5) 全部通过，无 segfault、无 `SystemError`、无 `Fatal Python error` |
| **测试** | `python -m pytest Lib/test/test_tracegc.py -v`（或用 `python -m test test_tracegc -v`）每项 500 次迭代 |
| **通过条件** | 5 个冒烟场景全部通过；`test_tracegc.py` 覆盖率报告显示 Phase 0 各组件路径被覆盖 > 80%；连续 3 次无失败运行 |

---

## Phase 1: C API 兼容 + 分配器三堆分离

**目标**：所有 C 扩展无需修改即可在 Py_TRACING_GC 下编译运行（Legacy 兼容模式）。
**依赖**：Phase 0 完成且通过门禁。
**通过条件**：`_csv`、`_socket`、`_json` 等内置 C 扩展模块在 Py_TRACING_GC 构建下正常 import。

### 任务 DAG

```
1.1 ──→ 1.2 → 1.3 → 1.4 → 1.5
        ↑                      │
        └── 1.2a (并行) ───────┘
```

---

### 1.1 引用计数宏重定义

| 属性 | 值 |
|------|----|
| **架构参考** | §11.1 引用计数宏重定义、§11.5 兼容性总结（Legacy 列） |
| **说明** | 在 `#if defined(Py_TRACING_GC)` 下重定义：`Py_INCREF/DECREF` 为空操作、`Py_XINCREF/XDECREF` 为空操作、`Py_NewRef/XNewRef` 返回原指针、`Py_CLEAR` 只设 NULL、`Py_SETREF/XSETREF` 只做指针赋值 |
| **边界** | 不改变函数签名；可用性断言（`Py_IsImmortal` 等）保留；`_Py_DECREF_SPECIALIZED` 也需条件编译 |
| **文件** | `Include/refcount.h`（条件分支）、`Include/internal/pycore_object.h`（`_Py_DECREF_SPECIALIZED`） |
| **门禁** | 每个宏的签名与引用计数模式完全一致；C 编译器无类型警告 |
| **测试** | 在 C 扩展测试代码中调用所有宏，编译无错误 |
| **通过条件** | `Py_INCREF(NULL)` 编译通过且不产生代码；`Py_DECREF(NULL)` 同 |

### 1.2 Py_REFCNT 稳定哨兵值

| 属性 | 值 |
|------|----|
| **架构参考** | §11.1 Py_REFCNT / Py_SET_REFCNT（修正后） |
| **说明** | `Py_REFCNT(op)` 返回稳定值 1（不论 GC 颜色）；`Py_SET_REFCNT(op, v)`：v==0 设 WHITE（请求释放），v>1 设 BLACK（请求存活），v==1 无操作 |
| **边界** | 不暴露 GC 颜色状态；`Py_REFCNT` 返回值在 Stable ABI 中 |
| **文件** | `Include/refcount.h` |
| **门禁** | `Py_REFCNT(some_white_object) == 1` |
| **测试** | 创建对象调用 `Py_REFCNT` 返回值始终 1；调用 `Py_SET_REFCNT(op, 0)` 后 `_PyGC_IsWhite(op) == 1` |
| **通过条件** | 两者都通过 |

### 1.2a 核心代码中 ob_refcnt 直接访问点修改

| 属性 | 值 |
|------|----|
| **架构参考** | §11.5 表格 `ob_refcnt` 直接访问行 |
| **说明** | 在 CPython 核心代码中搜索 `op->ob_refcnt` 直接访问（约 4 处），改为 `Py_REFCNT(op)` / `_PyGC_GetColor(op)` / 条件分支 |
| **边界** | 仅限于 Include/ 和 Python/ 目录下的核心代码；不改 C 扩展接口 |
| **文件** | `Objects/object.c`（`_Py_NewReference`、`_Py_Dealloc` 等）、`Python/ceval.c` |
| **门禁** | `Py_REFCNT` 宏在任何 `PyObject*` 上可用 |
| **测试** | 构建通过；`_Py_NewReference` 在 Py_TRACING_GC 下不访问 ob_refcnt |
| **通过条件** | grep `op->ob_refcnt` 在 Include/ 和 Python/ 中无残留 |

### 1.3 tp_dealloc 分级兼容

| 属性 | 值 |
|------|----|
| **架构参考** | §11.2 tp_dealloc 语义变更（分级策略）、§11.5 兼容性总结 |
| **说明** | Legacy 模式：`tp_dealloc` 保持不变（完整语义），通过 `Py_TPFLAGS_FREE_DELEGATED` 标志标记内部已调用 tp_free 的类型；实现 `_PyTypes_Ready_GCCompatible()` 在类型初始化时设置兼容标志 |
| **边界** | Phase 1 只实现 Legacy 模式；Compatible/TraceGC-safe 模式在 Phase 3 实现 |
| **文件** | `Objects/typeobject.c`（`type_ready` 流程中插入 `_PyTypes_Ready_GCCompatible`）、`Include/object.h`（`Py_TPFLAGS_FREE_DELEGATED` 标志） |
| **门禁** | Legacy 模式无 double-free；sweep 阶段通过 `tp_free_delegated` 标志跳过重复释放 |
| **测试** | 内置类型（dict, list, tuple, float）在 Legacy 模式下正确 finalize + free；无 double-free segfault |
| **通过条件** | 所有内置类型在 Py_TRACING_GC 下创建 + 销毁 10000 次无泄漏/崩溃 |

### 1.4 PyObject_GC_New/Del/Track/UnTrack 适配

| 属性 | 值 |
|------|----|
| **架构参考** | §11.3 PyObject_GC_New / GC_Del 兼容、§3.5 gc_alloc 分配路径 |
| **说明** | `PyObject_GC_New/NewVar` 走 `gc_alloc` 自动追踪；`PyObject_GC_Del` 将对象标记为 WHITE 请求回收；`PyObject_GC_Track/UnTrack` 为空操作；`_PyObject_GC_IS_TRACKED` 对所有对象返回 true |
| **边界** | `PyObject_GC_Del` 不立即释放内存（仅请求回收）；`PyObject_GC_Track` 不影响 GC 根枚举 |
| **文件** | `Include/objimpl.h`、`Python/gc_alloc.c` |
| **门禁** | `PyObject_GC_New` 返回的对象 `_PyGC_GetColor() == WHITE` |
| **测试** | 调用 `PyObject_GC_New(PyDict_Type)` → `PyObject_GC_Del(op)` → GC 后对象 FREE |
| **通过条件** | 所有 GC API 在 Py_TRACING_GC 下编译且 `gc_alloc` 路径正常工作 |

### 1.5 分配器三堆分离

| 属性 | 值 |
|------|----|
| **架构参考** | §12.1–§12.2 分配策略、对象堆与 Raw 内存的分离 |
| **说明** | 实现三条分配路径：**对象堆**（Object Heap）从 Phase 0 的 `_PyGC_InitHeap` 区域 bump-pointer 分配，仅用于 `PyObject_GC_New`；**Raw 堆**（Raw Heap）保持现有 pymalloc 不变（arena 来自传统 mmap/malloc），用于 `PyMem_Malloc`/`PyMem_RawMalloc` 和内部 buffer；**大对象堆**（Large Object Heap）直接系统 malloc，side metadata 单独记录。地址范围检查实现：`_PyObject_IsOnObjectHeap(addr)` 通过 `[heap_start, heap_end)` 判断 |
| **边界** | 不改现有 `PyMem_Malloc`/`PyMem_RawMalloc` 行为；pymalloc 的 `obmalloc.c` 只增加 `#ifndef Py_TRACING_GC` 守卫；不修改 `_PyObject_MallocWithType` |
| **文件** | `Objects/obmalloc.c`（条件编译守卫）、`Python/gc_heap.c`（`_PyObject_IsOnObjectHeap`）、`Include/internal/pycore_gc.h`（声明）、新建 `Python/gc_large.c`（大对象 side metadata） |
| **门禁** | `_PyObject_IsOnObjectHeap(op)` 当 op 在对象堆内返回 1 否则 0 |
| **测试** | `PyObject_GC_New(PyLong_Type)` 返回的地址在对象堆范围内；`PyMem_Malloc(100)` 返回的地址不在对象堆范围内 |
| **通过条件** | 三堆分配不交叉；混合分配 10000 次后对象堆地址范围不变；pymalloc 从原始 arena 分配 |

---

## Phase 2: 根枚举完整化 + 安全点 + 保守 C 栈扫描

**目标**：C 扩展栈上的 `PyObject*` 局部变量被 GC 正确识别为根，不会误回收。
**依赖**：Phase 0 完成。
**通过条件**：在 C 扩展中分配对象、调用会触发 GC 的函数、继续使用该对象——不 segfault。

### 任务 DAG

```
2.1 → 2.2 → 2.3
```

---

### 2.1 C 栈保守扫描

| 属性 | 值 |
|------|----|
| **架构参考** | §9 安全点设计（修改后的保守 C 栈扫描框）、§8.3 线程状态访问器 |
| **说明** | 实现 `_PyGC_ConservativeScanCStack()`：从 `tstate->current_frame` 的 C 栈帧到线程栈底，扫描所有对齐的指针值；对每个看起来在对象堆地址范围内的值，调用 `_PyGC_VisitChild` 标记为存活；伪根标记不推入标记栈（仅标记 BLACK）以避免误保活放大 |
| **边界** | 保守扫描只针对 Legacy 扩展的 C 栈；解释器帧仍使用精确扫描；误保活（false positive）可接受（增加存活集但保证正确性） |
| **文件** | 新建 `Python/gc_cstack.c`、`Include/internal/pycore_gc.h` |
| **门禁** | 扫描不访问非法内存地址；对所有假指针值不做 `tp_traverse` |
| **测试** | 创建 C 扩展调用链 A→B→C，在 C 函数中触发 GC，A/B/C 均存活；故意构造 false positive 指针值，GC 不崩溃 |
| **通过条件** | Python 解释器在 Py_TRACING_GC 下运行 1000 次简单 C API 调用序列无 crash |

### 2.2 安全点基础框架

| 属性 | 值 |
|------|----|
| **架构参考** | §9.1–§9.2 安全点设计（`_Py_SafepointCheck`、`_Py_EnterSafepoint`、`_Py_RequestSafepoint`） |
| **说明** | 实现 `_Py_SafepointCheck` 宏（检查 `tstate->gc_safepoint_requested` 原子标志）；在字节码循环每次迭代开头插入 `_Py_SafepointCheck(tstate)`；在 `_PyGC_CollectOld` 前调用 `_Py_RequestSafepoint` |
| **边界** | Phase 2不实现 page-fault 信号式安全点（后续优化）；不等待其他线程（单线程 GIL 构建） |
| **文件** | 新建 `Include/internal/pycore_safepoint.h`、修改 `Python/ceval.c`（字节码循环插入）、修改 `Python/gc_collect.c`（收集前调用安全点） |
| **门禁** | 安全点检查不影响正常执行路径的性能（单次 atomic load + branch） |
| **测试** | 执行 `while True: pass` 循环，从另一个线程请求安全点，循环在有限时间内到达安全点 |
| **通过条件** | `_Py_SafepointCheck` 正常路径不改变解释器状态；GC 时所有线程报告 at_safepoint |

### 2.3 根枚举集成安全点 + 保守栈

| 属性 | 值 |
|------|----|
| **架构参考** | §8.2 `_PyGC_MarkRoots`、§8.3 `_PyGC_VisitOneThread` |
| **说明** | 在 `_PyGC_MarkRoots` 中集成：先请求安全点暂停线程（`_Py_RequestSafepoint`），然后对每个线程调用 `_PyGC_ConservativeScanCStack()` 扫描 C 栈 + 现有的 `_PyGC_VisitOneThread` 扫描解释器帧 |
| **边界** | 安全点 + 保守扫描确保根完整性；保留现有的精确帧扫描 |
| **文件** | `Python/gc_roots.c`（修改 `_PyGC_MarkRoots`）、`Python/gc_cstack.c` |
| **门禁** | 安全点后根枚举覆盖解释器帧 + C 栈 |
| **测试** | 创建跨 C 函数的引用链，GC 完整遍历 |
| **通过条件** | `_PyGC_MarkRoots` 在安全点保护下的根枚举覆盖所有已知根 |

---

## Phase 3: 分代收集 + Card Table + 写屏障

**目标**：在 non-moving 前提下引入分代 GC，新生代不复制（仅逻辑分代，通过分配区域和年龄区分），搭配 card table 优化 minor GC 的根扫描。
**依赖**：Phase 1（分配器分离）、Phase 2（安全点 + 根枚举完整）。
**通过条件**：分代 GC 正确收集循环垃圾，不做对象移动，card table 在核心类型写操作中正确记录脏卡。

### 任务 DAG

```
3.1 → 3.2 → 3.3 → 3.4
        ↗
  3.1a (并行)
```

---

### 3.1 Object-Start Metadata

| 属性 | 值 |
|------|----|
| **架构参考** | §7.4（object-start metadata 需求） |
| **说明** | 实现 object-start bitmap：每 16 字节对齐单元对应 1 bit，标记是否为对象起始地址；在对象堆分配时设置对应 bit，释放时清除；为 `_PyGC_FindObjectStart(addr)` 提供 O(1) 查找 |
| **边界** | 只覆盖对象堆（Large Object Heap 使用 side hash table）；不覆盖 Raw 堆 |
| **文件** | 新建 `Python/gc_objmap.c`、`Include/internal/pycore_objmap.h`；修改 `Python/gc_heap.c`（分配时维护 bitmap） |
| **门禁** | `_PyGC_FindObjectStart(addr)` 返回的地址一定是有效的 `PyObject*` |
| **测试** | 随机地址测试：bitmap 中 1 bit 对应的地址通过 `_PyGC_GetObjectSize` 返回正数 |
| **通过条件** | 混合大小对象分配后 bitmap 覆盖所有对象起始地址 |

### 3.1a Card Table 数据结构

| 属性 | 值 |
|------|----|
| **架构参考** | §7.1 Card Table 设计（修正后）、§7.4 |
| **说明** | 实现 card table 初始化（`_PyGC_InitCardTable`）、card dirty 操作（`_PyGC_MarkCardDirty`）、card clean 操作（`_PyGC_ClearCards`）；card size 512 字节，覆盖堆全部地址空间 |
| **边界** | Phase 3 只在对象堆上启用 card table；不覆盖 Raw 堆；系统初始化的 card table 数据已在 §0.4 中分配 |
| **文件** | 新建 `Python/gc_cardtable.c`、`Include/internal/pycore_cardtable.h` |
| **门禁** | `_PyGC_GetCardIndex(heap_start) = 0`；每个 card 对应 512 字节 |
| **测试** | 设置 D→C→B→A 四张脏卡，检查 card table 位图正确 |
| **通过条件** | 连续范围 `_PyGC_ClearCards` 清除所有覆盖的 card |

### 3.2 写屏障插入（核心类型）

| 属性 | 值 |
|------|----|
| **架构参考** | §7.2 需要插入写屏障的操作点 |
| **说明** | 在 `PyList_SetItem`、`dict_set_item_by_hash`、`insertdict`、`set_add_entry`、`PyObject_SetAttr`、`cell_ass_sub` 等核心写入点后插入 `_PyGC_WRITE_BARRIER` 宏；在老年代对象写入新生代引用时标记 card dirty |
| **边界** | 仅在核心类型写入点插入；不覆盖第三方 C 扩展（Legacy 模式使用全量 old 遍历） |
| **文件** | `Objects/listobject.c`、`Objects/dictobject.c`、`Objects/setobject.c`、`Objects/object.c`、`Objects/cellobject.c`、`Objects/funcobject.c` |
| **门禁** | 写屏障后 card table 中对应 card 状态为 DIRTY |
| **测试** | 创建 old→young 引用后检查 card 状态；创建 old→old 引用后 card 不变 |
| **通过条件** | 写屏障覆盖所有 §7.2 列出的操作点；全冒烟测试（Py_TRACING_GC 构建运行测试套件子集） |

### 3.3 Minor GC（非移动，全量标记 + 逻辑分代）

| 属性 | 值 |
|------|----|
| **架构参考** | §5.4 Minor GC（Phase 0 non-moving 版本）、§7.3 脏卡处理 |
| **说明** | 实现 `_PyGC_CollectYoung`：Eden 满时触发，不做复制；根枚举 → 标记传播 → 扫描脏卡找 old→young 引用 → 全量 sweep 对象堆；promotion 逻辑：存活年龄递增，超过阈值进入老年代标记（逻辑区分，无物理移动） |
| **边界** | 不复制对象；不整理；分代仅通过 `ob_gc_age` 字段逻辑区分 |
| **文件** | `Python/gc_collect.c`（`_PyGC_CollectYoung`） |
| **门禁** | Minor GC 后 Eden 被清空但对象不移动；存活对象 age++ |
| **测试** | 触发 minor GC：新分配对象在 eden 内被扫描，存活但 age 不变（第一次 minor），第二次存活后 age++ |
| **通过条件** | Minor GC 释放白色对象内存；存活对象 age 正确递增 |

### 3.4 Major GC（全量标记-清除）

| 属性 | 值 |
|------|----|
| **架构参考** | §5.5 Major GC（Phase 0 non-moving 版本）、§6.3–§6.4 |
| **说明** | 实现 `_PyGC_CollectOld`（重命名自 Phase 0 的 `_PyGC_CollectOld`）：全量根枚举 → 全量标记传播 → 全量 sweep；epoch 复位：sweep 后所有 BLACK→WHITE；触发条件：Minor GC 后存活率 > 25% |
| **边界** | 不做整理；不做对象移动；全量暂停（不增量） |
| **文件** | `Python/gc_collect.c`（`_PyGC_CollectOld`）、`Python/gc_mark.c`、`Python/gc_sweep.c` |
| **门禁** | Major GC 后存活集合与根集合一致；所有白色对象被回收 |
| **测试** | 创建大对象图 → 断开引用 → 触发 major GC → 对象被回收；复杂循环引用结构正确回收 |
| **通过条件** | 在 CPython 测试套件子集（`test_dict.py`, `test_list.py`, `test_set.py`, `test_gc.py`）上正确运行 |

---

## Phase 4: TraceGC-safe 扩展 + 可移动 GC（实验性）

**目标**：对声明 `Py_TPFLAGS_TRACEGC_SAFE` 的扩展类型启用 Eden 复制收集和老年代整理。
**依赖**：Phase 3（分代 + card table 稳定）、Phase 1（C API 兼容层）。
**通过条件**：声明了 TraceGC-safe 的内置类型（如 `list`）在 minor GC 中被复制到 survivor 区，地址发生变化后仍被正确追踪。

### 任务 DAG

```
4.1 → 4.2 → 4.3
  ↘ ↗
   4.2a
```

---

### 4.1 TraceGC-safe 类型声明系统

| 属性 | 值 |
|------|----|
| **架构参考** | §1.4 扩展兼容分级模型（TraceGC-safe 行）、§11.5 兼容性总结（TraceGC-safe 列） |
| **说明** | 定义 `Py_TPFLAGS_TRACEGC_SAFE` 类型标志；实现类型审核流程（静态检查：tp_traverse 完整、tp_dealloc 不依赖地址稳定性、无 direct tp_free 调用）；TraceGC-safe 类型在初始化时走 `_PyGC_TraceGCFree` 释放路径 |
| **边界** | Phase 4 类型审核只检查静态属性；不执行运行时验证 |
| **文件** | `Include/object.h`（标志定义）、`Objects/typeobject.c`（`type_ready` 中审核） |
| **门禁** | `Py_TPFLAGS_TRACEGC_SAFE` 标志不与 `Py_TPFLAGS_HAVE_GC` 冲突 |
| **测试** | 将内置 `list` 类型声明为 TraceGC-safe，审核通过 |
| **通过条件** | 类型初始化时 `_PyTypes_Ready_GCCompatible` 为 TraceGC-safe 类型设置正确的 `tp_free` |

### 4.2 Eden 复制收集（TraceGC-safe 类型）

| 属性 | 值 |
|------|----|
| **架构参考** | §5.4 Minor GC（复制收集原设计） |
| **说明** | 在 `_PyGC_CollectYoung` 中对 TraceGC-safe 的存活对象启用复制到 Survivor 区；复制前更新所有指向旧地址的引用（通过 card table 和根枚举找到所有引用点）；复制后旧地址标记 FREE；非 TraceGC-safe 类型保持不移动 |
| **边界** | 只复制 TraceGC-safe 对象；Legacy/Compatible 对象保持 pinned；Survivor 溢出时触发 major GC |
| **文件** | `Python/gc_collect.c`（修改 `_PyGC_CopyYoungToSurvivor`） |
| **门禁** | 复制后对象地址变化；所有引用指向新地址 |
| **测试** | list 对象在 minor GC 后地址改变；通过旧指针访问不崩溃（old 指针已更新或防止使用）；old→young card 引用正确追踪新地址 |
| **通过条件** | 复制后的 TraceGC-safe 对象在后续访问中状态正确 |

### 4.2a Pinned 区域管理

| 属性 | 值 |
|------|----|
| **架构参考** | §1.4（Phase 2+ 移动 GC 下 Legacy 固定） |
| **说明** | 将 Legacy/Compatible 扩展分配的对象放置在 pinned 区域（对象堆内的一个子范围，永不整理/复制）；pinned 区域内的对象地址稳定 |
| **边界** | pinned 区域不参与 Eden/Survivor 复制和整理 |
| **文件** | `Python/gc_heap.c`（初始化时划分 pinned 区域） |
| **门禁** | Pinned 区域内对象 `_PyGC_IsPinned(op) == 1` |
| **测试** | non-TraceGC-safe 类型对象分配在 pinned 区域；复制 GC 后 pinned 对象地址不变 |
| **通过条件** | Legacy 扩展类型地址在多次 GC 后保持不变 |

### 4.3 老年代整理（TraceGC-safe）

| 属性 | 值 |
|------|----|
| **架构参考** | §5.5 Major GC（标记-整理原设计） |
| **说明** | 在 major GC 中对 TraceGC-safe 对象启用整理（compact）：存活对象向老年代前端滑动，减少碎片；需配合 pinned 区域边界；整理后更新 card table 引用 |
| **边界** | 仅整理 TraceGC-safe 对象；Legacy/Compatible 对象跳过；大对象不做整理 |
| **文件** | `Python/gc_sweep.c`（`_PyGC_CompactOld`） |
| **门禁** | 整理后存活对象连续排列；card table 引用正确更新 |
| **测试** | 交替分配/释放大量 TraceGC-safe 对象，整理后老年代碎片率 < 10% |
| **通过条件** | 整理后对象堆空间利用率 > 90% |

---

## Phase 5: JIT-GC 集成

**目标**：Tier 1 JIT 生成的代码在安全点报告 OopMap，使 GC 能精确枚举 JIT 代码中的对象引用。
**依赖**：Phase 1（C API 兼容）、Phase 2（安全点框架）。
**不包含**：Tier 2 SSA 优化编译器（此为 Phase 2+ 目标，见 §1.1 优先级 P2）。
**通过条件**：在 `Py_TRACING_GC` + `Py_JIT` 双开关下，JIT 编译的代码中的对象引用被 GC 正确枚举。

### 任务 DAG

```
5.1 → 5.2 → 5.3
```

---

### 5.1 OopMap 数据结构

| 属性 | 值 |
|------|----|
| **架构参考** | §19.1 OopMap 设计、§19.2 JIT 代码中的 GC 安全点 |
| **说明** | 实现 `_PyOopMap` 位图结构（64 位寄存位图 + 256 槽栈位图）；`_PySafepointDescriptor` 关联代码偏移与 OopMap；实现 `_PyJIT_AllocateOopMap` / `_PyJIT_RegisterOopMap` |
| **边界** | 不涉及编译器代码生成；仅数据结构和注册 API |
| **文件** | 新建 `Include/internal/pycore_oopmap.h`、`Python/jit_oopmap.c` |
| **门禁** | OopMap 位图大小固定（寄存器位图 8 字节，栈位图 32 字节） |
| **测试** | 创建 OopMap 并注册、查询；位图正确编码/解码 |
| **通过条件** | OopMap 编码/解码 roundtrip 测试通过 |

### 5.2 Tier 1 安全点插入

| 属性 | 值 |
|------|----|
| **架构参考** | §19.3 GC 安全点处理、§18.1 Tier 1 编译流水线 |
| **说明** | 在 Tier 1 copy-and-patch 编译中，在方法入口、回边、分配点前插入安全点检查代码（cmp + jne）；安全点调用 `_Py_JIT_SafepointFast` 传入对应 OopMap |
| **边界** | 只修改 Tier 1 JIT（现有 copy-and-patch）；不改 Tier 0 解释器（已有字节码安全点） |
| **文件** | `Python/jit.c`（copy-and-patch 编译时插入安全点）、`Python/gc_safepoint.c` |
| **门禁** | JIT 生成代码的安全点检查在无 GC 时快速路径为 3 指令 |
| **测试** | JIT 编译的函数到达安全点时不崩溃 |
| **通过条件** | 安全点插入后 Tier 1 编译的函数仍正确执行 |

### 5.3 JIT 代码根枚举

| 属性 | 值 |
|------|----|
| **架构参考** | §19.4 GC 根枚举中的 OopMap 利用 |
| **说明** | 在 `_PyGC_MarkRoots` 中集成 JIT OopMap 根枚举：`_PyGC_VisitJITThreadState` 检查 `tstate->gc_current_oopmap`，遍历寄存器/栈位图标记引用 |
| **边界** | 仅在安全点有效；线程不在 JIT 代码中时跳过 |
| **文件** | `Python/gc_roots.c`（`_PyGC_VisitJITThreadState`）、`Python/gc_safepoint.c` |
| **门禁** | OopMap 根枚举不遗漏 JIT 代码中的引用 |
| **测试** | JIT 编译函数中引用对象，在 GC 安全点后被正确保留 |
| **通过条件** | JIT 编译函数 + GC 混合测试 10000 次无 use-after-free |

---

## 附录 A：关键性能门禁

| 门禁指标 | Phase | 阈值 | 测量方法 |
|---------|-------|------|---------|
| 追踪式 GC 进程启动时间 | 0 | < 引用计数模式的 120% | `time python -c ""` |
| `Py_INCREF` 空操作开销 | 1 | 0 条指令（与引用计数模式同位置空操作） | 反汇编验证 |
| Minor GC 暂停时间 | 3 | < 5ms (100MB 堆) | `gc.set_debug` 时间戳 |
| Major GC 暂停时间 | 3 | < 50ms (500MB 堆) | `gc.set_debug` 时间戳 |
| 写屏障 CPI 开销 | 3 | < 1% 总执行时间 | perf stat |
| 保守扫描误保活率 | 2 | < 5% 的堆大小 | 统计确认 |
| JIT 安全点检查开销 | 5 | < 0.5% 总执行时间 | perf stat |

---

## 附录 B：风险登记

| 风险 | 影响 | 缓解 | 触发时行动 |
|------|------|------|-----------|
| 保守 C 栈扫描误保活过高 | 内存使用 > 2x | Phase 2 降级：全量精确根枚举 | 放慢到 Phase 2a 做 conservative 扫描细化 |
| tp_dealloc Legacy 模式 double-free | 段错误 | §6.3 的 `free_was_called` 标志 | 增加 Legacy 类型白名单逐个验证 |
| JIT copy-and-patch 安全点插入破坏模板 | 编译后段错误 | §5.2 为每个模板单独验证 | 逐个禁用到模板修复 |
| 分代收集后 card table 脏卡漏扫 | 新生代对象悬空指针（use-after-free） | §3.3 Minor GC 先全量 old traverse 再启用 card table | 全量 old 遍历作为永久安全模式 |

---

> **本文档结束。**
> `ARCHITECTURE.md` 设计冻结后执行 `PLAN.md`。Agent 依 `AGENT.md` 8 步循环每次完成一个子任务。
