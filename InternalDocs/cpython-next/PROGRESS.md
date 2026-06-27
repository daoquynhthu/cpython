# PROGRESS.md — CPython 追踪式 GC 改造工程进度

> **规则**：只追加不修改。已完成条目追加在对应 Phase 下，注明完成日期和 Agent 会话 ID。
> 格式：`- [x] 任务编号 任务名  @YYYY-MM-DD  Agent会话摘要`

---

## Phase 0: Tracing GC 基础设施（入门）

| 任务 | 状态 | 完成日期 | 备注 |
|------|------|---------|------|
| 0.0 测试桩+构建基线+回归框架 | ✅ 已完成 | 2026-06-27 | 7 桩函数，双模式编译通过 |
| 0.1 编译开关定义 | ✅ 已完成 | 2026-06-27 | PCbuild + pyport.h + Makefile |
| 0.2 PyObject 结构体改造 | ✅ 已完成 | 2026-06-27 | object.h struct _object 重定义 |
| 0.3 GC 运行时状态 | ✅ 已完成 | 2026-06-27 | struct _gc_runtime_state 重定义（3 分支），GC 颜色常量+访问器，13 新增桩函数，gcmodule.c 5函数守卫，refcount.h/pycore_object.h 顶层守卫，_PyGC_DEFAULT_* 常量 |
| 0.4 对象堆初始化 | ⬜ 待开始 | — | — |
| 0.5 基础对象分配器 | ⬜ 待开始 | — | — |
| 0.6 根枚举框架 | ⬜ 待开始 | — | — |
| 0.7 三色标记算法 | ⬜ 待开始 | — | — |
| 0.8 清除阶段 + Epoch 复位 | ⬜ 待开始 | — | — |

## Phase 1: C API 兼容 + 分配器三堆分离

| 任务 | 状态 | 完成日期 | 备注 |
|------|------|---------|------|
| 1.1 引用计数宏重定义 | ⬜ 待开始 | — | — |
| 1.2 Py_REFCNT 稳定哨兵值 | ⬜ 待开始 | — | — |
| 1.2a ob_refcnt 直接访问点修改 | ⬜ 待开始 | — | — |
| 1.3 tp_dealloc 分级兼容 | ⬜ 待开始 | — | — |
| 1.4 PyObject_GC_New/Del/Track 适配 | ⬜ 待开始 | — | — |
| 1.5 分配器三堆分离 | ⬜ 待开始 | — | — |

## Phase 2: 根枚举完整化 + 安全点 + 保守 C 栈扫描

| 任务 | 状态 | 完成日期 | 备注 |
|------|------|---------|------|
| 2.1 C 栈保守扫描 | ⬜ 待开始 | — | — |
| 2.2 安全点基础框架 | ⬜ 待开始 | — | — |
| 2.3 根枚举集成安全点 + 保守栈 | ⬜ 待开始 | — | — |

## Phase 3: 分代收集 + Card Table + 写屏障

| 任务 | 状态 | 完成日期 | 备注 |
|------|------|---------|------|
| 3.1 Object-Start Metadata | ⬜ 待开始 | — | — |
| 3.1a Card Table 数据结构 | ⬜ 待开始 | — | — |
| 3.2 写屏障插入（核心类型） | ⬜ 待开始 | — | — |
| 3.3 Minor GC（非移动逻辑分代） | ⬜ 待开始 | — | — |
| 3.4 Major GC（全量标记-清除） | ⬜ 待开始 | — | — |

## Phase 4: TraceGC-safe 扩展 + 可移动 GC（实验性）

| 任务 | 状态 | 完成日期 | 备注 |
|------|------|---------|------|
| 4.1 TraceGC-safe 类型声明系统 | ⬜ 待开始 | — | — |
| 4.2 Eden 复制收集 | ⬜ 待开始 | — | — |
| 4.2a Pinned 区域管理 | ⬜ 待开始 | — | — |
| 4.3 老年代整理 | ⬜ 待开始 | — | — |

## Phase 5: JIT-GC 集成

| 任务 | 状态 | 完成日期 | 备注 |
|------|------|---------|------|
| 5.1 OopMap 数据结构 | ⬜ 待开始 | — | — |
| 5.2 Tier 1 安全点插入 | ⬜ 待开始 | — | — |
| 5.3 JIT 代码根枚举 | ⬜ 待开始 | — | — |

---

> **正在进行的任务**：（无）
> **最近完成的任务**：
> - [x] [Task-0.3] GC 运行时状态  @2026-06-27  struct _gc_runtime_state 重定义（Py_TRACING_GC 分支含 heap/mark_stack/card_table/switch/stats/incremental 13 字段），_PyGC_COLOR_* 常量+5 访问器，_PyGC_DEFAULT_* 常量，rcount.h/pycore_object.h/pycore_gc.h/pycore_runtime_init.h 顶层守卫，gcmodule.c 5函数 #if 守卫，gc.c 双重守卫，13 新增桩函数，Py_CLEAR 宏 BUG 修复
> - [x] [Task-0.2] PyObject 结构体改造  @2026-06-27  struct _object 重定义（3 分支），PyObject_HEAD_INIT 适配，端序守卫，static_assert sizeof+offsetof
> - [x] [Task-0.1] 编译开关定义  @2026-06-27  PCbuild/pyproject.props + pyport.h 互斥守卫 + Makefile.pre.in
> - [x] [Task-0.0] 测试桩+构建基线+回归框架  @2026-06-27  gc_stubs.c(7 桩) + test_tracegc.py + build diff
