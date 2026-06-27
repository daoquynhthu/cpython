# ARCHITECTURE.md — CPython 追踪式 GC 改造架构设计

> 本文档是 CPython 追踪式 GC 改造工程的**宪法**。
> 地位：**本文档高于一切实现代码**。Agent 绝对禁止修改本文档。
> 生效规则：本文档由人类审阅冻结后生效。任何修改必须由人类执行。
> 语言：本文档使用中文编写，关键代码使用 C 语言伪代码。

---

## 目录

1. [设计目标与约束](#1-设计目标与约束)
2. [编译开关体系](#2-编译开关体系)
3. [PyObject 对象模型](#3-pyobject-对象模型)
4. [GC 运行时状态](#4-gc-运行时状态)
5. [分代堆布局](#5-分代堆布局)
6. [三色标记算法](#6-三色标记算法)
7. [写屏障与 Card Table](#7-写屏障与-card-table)
8. [根枚举系统](#8-根枚举系统)
9. [GC 安全点机制](#9-gc-安全点机制)
10. [GC 收集主流程](#10-gc-收集主流程)
11. [C API 兼容层](#11-c-api-兼容层)
12. [分配器适配](#12-分配器适配)
13. [条件编译策略](#13-条件编译策略)
14. [与现有自由线程 GC 的关系](#14-与现有自由线程-gc-的关系)
15. [JIT 子系统概述](#15-jit-子系统概述)
16. [分层编译架构](#16-分层编译架构)
17. [SSA 中间表示](#17-ssa-中间表示)
18. [JIT 编译流水线](#18-jit-编译流水线)
19. [JIT-GC 集成](#19-jit-gc-集成)
20. [类型概要分析](#20-类型概要分析)

---

## 1. 设计目标与约束

### 1.1 设计目标

> **关于兼容目标的说明：**
> 移动式 GC 与裸 `PyObject*` C 扩展 ABI 存在根本冲突（见 §1.2，反射到 `Include/object.h:31-38` 中
> "Objects do not float around in memory; once allocated an object keeps the same size and address"）。
> 因此本设计不承诺"100% 统一兼容"，而是采用**分级兼容策略**（见 §1.4）：
> 第一阶段为 non-moving 全量追踪式 GC，对象地址稳定；移动 GC 仅对显式声明的 `Py_TPFLAGS_TRACEGC_SAFE` 类型开放。

| 目标 | 优先级 | 说明 |
|------|--------|------|
| **消除引用计数运行时开销** | P0 | 移除 `Py_INCREF/Py_DECREF` 的所有原子操作和分支开销 |
| **Non-moving 全量追踪式 GC** | P0 | 第一阶段不移动对象。所有堆对象参与标记，非容器对象标记但不可遍历 |
| **C 扩展兼容（分级）** | P0 | 见 §1.4。Legacy 扩展 pinned + 保守扫描；TraceGC-safe 扩展可用移动 GC |
| **C 扩展 ABI 兼容** | P0 | `ob_type` 偏移量不变，`PyObject` 总大小不变，类型标志位不冲突 |
| **移动式分代收集** | P2 | 仅对 TraceGC-safe 类型开放 |
| **停止时间可预测** | P1 | 增量/并发标记减少单次暂停时间 |
| **内存占用不高于引用计数** | P1 | 对象头大小不变或更小，GC 元数据开销 ≤ 2% |
| **JIT-GC 集成（OopMap + 安全点）** | P0 | JIT 编译代码的 GC 根枚举能力（§19） |
| **分层 JIT 编译** | P1 | 三层编译：Tier 0 → Tier 1（copy-and-patch 增强）→ Tier 2（SSA 优化） |
| **类型概要分析** | P1 | 收集运行时的类型分布，驱动推测性优化 |
| **去优化 / OSR 机制** | P1 | 推测失败回退 + 栈上替换 |
| **Tier 2 SSA 优化编译器** | P2 | Sea-of-Nodes SSA、逃逸分析、GVN、内联（见 §17-18） |

### 1.2 不可妥协的 ABI 约束

以下约束源于现有 CPython 的 C 扩展 ABI。违反任意一条将导致生态断裂：

```
/* ─────────── 约束 1：PyObject 结构体大小 ─────────── */
/* 当前 sizeof(PyObject) = 16 字节（64 位）            */
/* 追踪式 GC 模式下必须保持 sizeof(PyObject) = 16      */
/* 原因：PyListObject 等结构体首字段为 PyObject_VAR_HEAD */
/*   typedef struct {                                  */
/*       PyObject_VAR_HEAD    ← 24 字节 (ob_base + ob_size) */
/*       PyObject **ob_item;  ← 偏移 24                */
/*       Py_ssize_t allocated; ← 偏移 32               */
/*   } PyListObject;                                   */
/* 如果 PyObject 变大，ob_item 偏移量改变                */

/* ─────────── 约束 2：ob_type 偏移量 ─────────── */
/* ob_type 必须在 PyObject 偏移 +8 处 */
/* 原因：所有 C 扩展通过 (op->ob_type) 访问类型，     */
/*       部分扩展假设 ob_type 是第二个 8 字节字段      */

/* ─────────── 约束 3：PyGC_Head / PREHEADER 偏移 ─────────── */
/* MANAGED_DICT_OFFSET  = -24  (非 GIL_DISABLED)    */
/* MANAGED_WEAKREF_OFFSET = -32 (非 GIL_DISABLED)    */
/* 这些偏移量假设 PyGC_Head (16B) 位于 PyObject 之前  */
/* 如果改变 GC 头布局，托管字典和弱引用的访问将崩溃     */
/* 见 Include/internal/pycore_object.h:920-926       */

/* ─────────── 约束 4：tp_dealloc 槽位必须存在 ─────────── */
/* tp_dealloc 在 PyTypeObject 偏移 +48 处             */
/* 语义可从"释放内存"变为"finalizer"，但槽位必须保留    */
```

### 1.3 设计范围

本文档覆盖以下子系统：

```
┌────────────────────────────────────────────────┐
│  Include/ 和 Python/ 中的追踪式 GC + JIT 改造    │
│  涉及：                                          │
│  · PyObject 结构体重构                           │
│  · GC 运行时状态 _(gc_runtime_state)            │
│  · 分代堆布局管理                                │
│  · 三色标记算法实现                              │
│  · 写屏障 (card table)                          │
│  · 根枚举 (root enumeration)                    │
│  · C API 兼容层 (refcount.h 宏重定义)           │
│  · 分配器适配 (obmalloc + GC 集成)              │
│  · 分层 JIT 编译架构 (Tier 0/1/2)               │
│  · SSA 中间表示设计                              │
│  · JIT-GC 集成 (OopMap、安全点、内联分配)       │
│  · 类型概要分析系统                              │
│  · 去优化 (Deoptimization) 框架                 │
├────────────────────────────────────────────────┤
│  不涉及：                                       │
│  · 自由线程 (Py_GIL_DISABLED) 构建的改造       │
│  · Python 层 gc 模块的 API 变更                 │
│  · 对象类型系统 (PyTypeObject 槽位) 的扩展      │
└────────────────────────────────────────────────┘

### 1.4 扩展兼容分级模型

由于追踪式 GC（尤其是移动 GC）与裸 `PyObject*` 扩展 ABI 的根本矛盾，扩展兼容性按使用情境分三级：

| 级别 | 类型 | GC 策略 | 性能代价 | 扩展声明 |
|------|------|---------|---------|---------|
| **Legacy** | 未修改的现有扩展 | pinned（永不移动）+ 保守 C 栈扫描 + 全量老年代扫描代替写屏障 | 高（全量 old 扫描、保守 root、无 bump-pointer） | 无需修改 |
| **Compatible** | 轻微修改的扩展（新增 tp_traverse / tp_clear） | pinned + 精确 tp_traverse 根枚举 + 分配走对象堆 | 中（精确 root 减少误保活） | 声明 `Py_TPFLAGS_COMPATIBLE_GC` |
| **TraceGC-safe** | 为追踪式 GC 重写的扩展 | 可移动 + 精确写屏障 + 快速分配路径 | 低（全速） | 声明 `Py_TPFLAGS_TRACEGC_SAFE` + 通过审核 |

阶段约束：
- **Phase 0（non-moving）**：Legacy + Compatible 两类扩展均可工作。所有对象不移动，无需处理 pinning。
- **Phase 2+（移动 GC）**：仅 TraceGC-safe 扩展可放入可移动区域；Legacy/Compatible 扩展固定在 non-moving 区域。
- **C 栈 root 枚举**：Phase 0 对 Legacy 扩展使用保守扫描（检查 C 栈上所有对齐指针值）；解释器帧使用精确扫描。

---

## 2. 编译开关体系

### 2.1 编译开关定义

引入一个新的编译时预处理器宏：`Py_TRACING_GC`。

```c
/* Include/pyconfig.h 或通过 configure/PCbuild 设置 */

/* #define Py_TRACING_GC  1    ← 取消注释以启用追踪式 GC */
```

所有新代码通过此开关与现有引用计数代码共存：

```c
#if defined(Py_TRACING_GC)
    /* 追踪式 GC 路径 */
#else
    /* 传统引用计数路径（完全不变） */
#endif
```

### 2.2 开关优先级

```
Py_GIL_DISABLED > Py_TRACING_GC > 默认
```

- 如果 `Py_GIL_DISABLED` 已定义，`Py_TRACING_GC` 被强制忽略（自由线程构建有独立的 GC 实现）。
- `Py_TRACING_GC` 仅在默认（带 GIL）构建下有效。

### 2.3 构建配置

```makefile
# Makefile.pre.in
TRACING_GC_CFLAGS = -DPy_TRACING_GC
```

```xml
<!-- PCbuild/pythoncore.vcxproj -->
<ClCompile Include="...">
  <PreprocessorDefinitions Condition="$(UseTracingGC)">Py_TRACING_GC;%(PreprocessorDefinitions)</PreprocessorDefinitions>
</ClCompile>
```

---

## 3. PyObject 对象模型

### 3.1 PyObject 结构体定义（Py_TRACING_GC 模式）

```c
/* ============= Include/object.h (Py_TRACING_GC 分支) ============= */

#if defined(Py_TRACING_GC)

/*
 * 追踪式 GC 模式下的 PyObject 结构体。
 * 关键约束：
 *   1. sizeof(PyObject) == 16（与引用计数模式一致）
 *   2. ob_type 偏移量 == 8（与引用计数模式一致）
 *   3. ob_gc_state 字段包含 GC 所需的所有状态位
 *
 * 相对于引用计数模式的变更：
 *   移除: ob_refcnt (引用计数不再需要)
 *   移除: ob_overflow (溢出计数器不再需要)
 *   新增: ob_gc_state (GC 三色标记状态)
 *   新增: ob_gc_age (分代年龄)
 *   保留: ob_flags (immortal/static 标志位)
 */

struct _object {
    _Py_ANONYMOUS union {
        int64_t _ob_word;                /* 8 字节，与 ob_refcnt_full 同位置同大小 */
        struct {
#if PY_LITTLE_ENDIAN
            /* 低 32 位：GC 状态字 */
            uint32_t ob_gc_state;        /* 偏移 0, 大小 4 — GC 状态 (见 §3.2) */
            /* 高 16 位：GC 分代年龄 */
            uint16_t ob_gc_age;          /* 偏移 4, 大小 2 — GC 年龄计数器   */
            /* 高 16 位（续）：对象标志 */
            uint16_t ob_flags;           /* 偏移 6, 大小 2 — 对象标志位      */
#else
            /* 大端布局（反向顺序） */
            uint16_t ob_flags;           /* 偏移 0 */
            uint16_t ob_gc_age;          /* 偏移 2 */
            uint32_t ob_gc_state;        /* 偏移 4 */
#endif
        };
        _Py_ALIGNED_DEF(_PyObject_MIN_ALIGNMENT, char) _aligner;
    };                                   /* 共计 8 字节 */
    PyTypeObject *ob_type;               /* 偏移 8, 大小 8 — 与引用计数模式完全一致 */
};

_Py_STATIC_ASSERT(sizeof(PyObject) == 16);
_Py_STATIC_ASSERT(offsetof(PyObject, ob_type) == 8);

/*
 * PyObject_VAR_HEAD 宏不变（因为 PyObject_HEAD 不变）：
 */
#define PyObject_HEAD          PyObject ob_base;
#define PyObject_VAR_HEAD      PyVarObject ob_base;
/* PyVarObject 仅在 PyObject 后追加 ob_size，不受影响 */

#endif /* defined(Py_TRACING_GC) */
```

### 3.2 ob_gc_state 位域分配

```c
/* ============= Include/internal/pycore_gc.h (Py_TRACING_GC 分支) ============= */

/*
 * ob_gc_state 是 32 位无符号整数，位域分配如下：
 *
 *  位 0-1: GC_COLOR    (2 位) — 三色标记的颜色
 *  位 2-5: GC_AGE      (4 位) — 分代年龄（与 ob_gc_age 协同）
 *  位 6:   GC_FINALIZED (1 位) — tp_finalize 已调用
 *  位 7:   GC_FROZEN    (1 位) — gc.freeze() 冻结
 *  位 8-31:保留            (24 位) — 预留
 *
 * 使用低 8 位使 bit field 访问效率最优化。
 * 所有未使用的位必须为 0。
 */

/* GC 颜色常量（三色标记，占用 2 位） */
#define _PyGC_COLOR_WHITE   0   /* 00: 未访问（可能死亡） */
#define _PyGC_COLOR_GREY    1   /* 01: 已加入标记栈（待处理） */
#define _PyGC_COLOR_BLACK   2   /* 10: 已标记（存活） */
#define _PyGC_COLOR_FREE    3   /* 11: 对象已释放（空闲） */

/* GC 状态位掩码 */
#define _PyGC_COLOR_MASK    ((uint32_t)0x00000003)       /* 位 0-1 */
#define _PyGC_AGE_MASK      ((uint32_t)0x0000003C)       /* 位 2-5, 移位 2 */
#define _PyGC_FINALIZED_MASK ((uint32_t)0x00000040)      /* 位 6 */
#define _PyGC_FROZEN_MASK   ((uint32_t)0x00000080)       /* 位 7 */

/* 内联访问器 */
static inline uint32_t _PyGC_GetColor(PyObject *op) {
    return op->ob_gc_state & _PyGC_COLOR_MASK;
}
static inline void _PyGC_SetColor(PyObject *op, uint32_t color) {
    uint32_t state = op->ob_gc_state;
    state = (state & ~_PyGC_COLOR_MASK) | (color & _PyGC_COLOR_MASK);
    op->ob_gc_state = state;
}
static inline int _PyGC_IsWhite(PyObject *op) {
    return _PyGC_GetColor(op) == _PyGC_COLOR_WHITE;
}
static inline int _PyGC_IsGrey(PyObject *op) {
    return _PyGC_GetColor(op) == _PyGC_COLOR_GREY;
}
static inline int _PyGC_IsBlack(PyObject *op) {
    return _PyGC_GetColor(op) == _PyGC_COLOR_BLACK;
}

/* GC 年龄访问器 */
static inline uint32_t _PyGC_GetAge(PyObject *op) {
    return (op->ob_gc_state & _PyGC_AGE_MASK) >> 2;
}
static inline void _PyGC_SetAge(PyObject *op, uint32_t age) {
    uint32_t state = op->ob_gc_state;
    state = (state & ~_PyGC_AGE_MASK) | ((age << 2) & _PyGC_AGE_MASK);
    op->ob_gc_state = state;
}
static inline void _PyGC_IncrementAge(PyObject *op) {
    uint32_t age = _PyGC_GetAge(op);
    if (age < 15) {
        _PyGC_SetAge(op, age + 1);
    }
}
```

### 3.3 ob_flags 位域（不变）

在 `Py_TRACING_GC` 模式下，`ob_flags` 的位分配与引用计数模式**完全一致**：

```c
/* ============= Include/object.h (通用，不受 Py_TRACING_GC 影响) ============= */

/* ob_flags 位域定义（不变） */
#define _Py_IMMORTAL_FLAG              0x01   /* 位 0: 对象是不死的 */
#define _Py_LEGACY_ABI_CHECK_FLAG      0x02   /* 位 1: 遗留 ABI 检查 */
#define _Py_STATICALLY_ALLOCATED_FLAG  0x04   /* 位 2: 静态分配（永不释放） */
/* 位 3+ 保留 */
```

`_Py_STATICALLY_ALLOCATED_FLAG` 标记的对象在 GC 中永远不被回收（它们的 `ob_gc_state` 被设置为 `_PyGC_COLOR_BLACK` 且从不被清除）。`_Py_IMMORTAL_FLAG` 标记的对象类似但不完全相同（immortal 对象可能是在堆上分配的但被故意永生）。

### 3.4 PyGC_Head 与 PREHEADER（不变）

在 `Py_TRACING_GC` 模式下，GC 头（`PyGC_Head`）**不再需要**进行 GC 链表管理。但为了保持 ABI 兼容（特别是 `MANAGED_DICT_OFFSET` 和 `MANAGED_WEAKREF_OFFSET`），必须保留 `PyGC_Head` 结构体占位。

```c
/* ============= Include/internal/pycore_interp_structs.h (Py_TRACING_GC 分支) ============= */

/*
 * PyGC_Head 在追踪式 GC 模式下的定义。
 *
 * 存在理由（仅为了 ABI 兼容）：
 *   1. MANAGED_DICT_OFFSET = -24 需要偏移 -24 处有有效存储
 *   2. MANAGED_WEAKREF_OFFSET = -32 需要偏移 -32 处有有效存储
 *
 * 在追踪式 GC 模式下，_gc_next 和 _gc_prev 字段不再用于 GC 链表，
 * 而是由托管字典/弱引用功能重用（与引用计数模式完全相同）。
 * GC 不再维护世代链表，因此这些字段没有 GC 语义。
 */

#if defined(Py_TRACING_GC)

typedef struct {
    _Py_ALIGNED_DEF(_PyObject_MIN_ALIGNMENT, uintptr_t) _gc_next;  /* 偏移 0, 大小 8 — 不再用于 GC */
    uintptr_t _gc_prev;                                              /* 偏移 8, 大小 8 — 不再用于 GC */
} PyGC_Head;

/*
 * 重要说明：
 * _gc_next 和 _gc_prev 在 Py_TRACING_GC 模式下保留其存储，但：
 * - 不被 GC 子系统使用（GC 使用 ob_gc_state 和 ob_gc_age）
 * - 仍被 MANAGED_WEAKREF 和 MANAGED_DICT 机制用作:
 *   _gc_next ↔ 弱引用链表头 (op[-32])
 *   _gc_prev ↔ 托管字典指针 (op[-24])
 * - 当对象被 GC 收集时，这些字段不会被 GC 遍历或修改
 */

/* _PyGC_PREV_MASK_FINALIZED 和 _PyGC_PREV_MASK_COLLECTING 不再被使用 */
/* 它们被 ob_gc_state 中的 _GC_FINALIZED_MASK 替代 */

/* _Py_AS_GC 和 _Py_FROM_GC 的语义不变（仅做指针算术） */
static inline PyGC_Head* _Py_AS_GC(PyObject *op) {
    return (PyGC_Head*)((char*)op - sizeof(PyGC_Head));
}
static inline PyObject* _Py_FROM_GC(PyGC_Head *gc) {
    return (PyObject*)((char*)gc + sizeof(PyGC_Head));
}

#else
/* 引用计数模式的 PyGC_Head 定义不变 */
#endif
```

### 3.5 gc_alloc 分配路径（Py_TRACING_GC 模式）

在追踪式 GC 模式下，对象分配后的 GC 跟踪是**全自动的**——所有分配的对象默认被 GC 追踪，不需要显式的 `_PyObject_GC_TRACK`。

```c
/* ============= Python/gc.c (Py_TRACING_GC 分支) ============= */

/*
 * 追踪式 GC 模式下的对象分配。
 * 
 * 变更要点：
 * 1. 分配后对象颜色设置为 WHITE（初始未标记状态）
 * 2. 分配区域来自 Eden 区（新生代 bump-pointer 分配）
 * 3. 不再维护世代链表（由 ob_gc_state 和堆区域隐式管理）
 * 4. 不再对 gen0 计数（GC 触发由 Eden 区大小决定）
 */

static PyObject *
gc_alloc(PyTypeObject *tp, size_t basicsize, size_t presize)
{
    size_t size = presize + basicsize;
    char *mem;

    /* 尝试从 Eden 区 bump-pointer 分配 */
    mem = _PyGC_EdenAlloc(size);
    if (mem == NULL) {
        /* Eden 区空间不足，触发 minor GC 或从老年代分配 */
        _PyGC_CollectYoung(tstate);
        mem = _PyGC_EdenAlloc(size);
        if (mem == NULL) {
            /* 仍然失败，使用后备分配器 */
            mem = _PyObject_MallocWithType(tp, size);
        }
    }

    PyObject *op = (PyObject *)(mem + presize);

    /* 初始化 GC 状态：白色（未标记），年龄 0 */
    op->ob_gc_state = _PyGC_COLOR_WHITE;
    op->ob_gc_age = 0;
    /* ob_flags 在 _PyObject_Init 中设置 */

    return op;
}

PyObject *
_PyObject_GC_New(PyTypeObject *tp)
{
    size_t presize = _PyType_PreHeaderSize(tp);
    size_t size = _PyObject_SIZE(tp);
    if (_PyType_HasFeature(tp, Py_TPFLAGS_INLINE_VALUES))
        size += _PyInlineValuesSize(tp);
    PyObject *op = gc_alloc(tp, size, presize);
    _PyObject_Init(op, tp);
    return op;
}

/*
 * _PyObject_GC_Track/_PyObject_GC_UnTrack 在追踪式 GC 模式下退化为空操作。
 * 所有对象自动被 GC 追踪，不需要显式跟踪/取消跟踪。
 */
void _PyObject_GC_Track(PyObject *op) {
    /* 空操作：追踪式 GC 自动追踪所有对象 */
}
void _PyObject_GC_UnTrack(PyObject *op) {
    /* 空操作 */
}
int _PyObject_GC_IS_TRACKED(PyObject *op) {
    /* 在追踪式 GC 下，所有 GC 类型对象都是"已追踪"的 */
    return _PyType_IS_GC(Py_TYPE(op)) ? 1 : 0;
}
```

### 3.6 对象内存布局总结（64 位 Py_TRACING_GC）

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. 非 GC 对象（int, float, bool, NoneType 等）                     │
│                                                                    │
│  偏移  大小  字段                                                  │
│  ─────────────────────────────────────                             │
│  0     8     _ob_word (含 ob_gc_state + ob_gc_age + ob_flags)     │
│  8     8     ob_type                                              │
│  ─────────────────────────────────────                             │
│  总计 16 字节                                                      │
│                                                                    │
│  GC 处理：这些类型不包含对其他对象的引用，但**必须被标记为存活**     │
│  （否则被保守 C 栈扫描误保活的 int/float 会被回收）。                │
│  标记访问器对它们只做 `_PyGC_SetColor(BLACK)` ，不调用 tp_traverse。 │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ 2. GC 对象，无需 PREHEADER（如 tuple, frozenset）                   │
│                                                                    │
│  偏移  大小  字段                                                  │
│  ─────────────────────────────────────                             │
│  -16   8     _gc_next (PyGC_Head, 不再用于 GC，仅占位)            │
│  -8    8     _gc_prev (PyGC_Head, 不再用于 GC，仅占位)            │
│  0     8     _ob_word (含 ob_gc_state + ob_gc_age + ob_flags)     │
│  8     8     ob_type                                              │
│  16          实例数据开始                                          │
│  ─────────────────────────────────────                             │
│  预头 16 字节，对象头 16 字节                                       │
│                                                                    │
│  GC 处理：GC 通过 tp_traverse 遍历其子对象。                       │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ 3. GC 对象 + PREHEADER（MANAGED_DICT | MANAGED_WEAKREF）           │
│    如大多数堆类型实例                                              │
│                                                                    │
│  偏移  大小  字段                                                  │
│  ─────────────────────────────────────                             │
│  -32   8     _gc_next / 弱引用链表头 (MANAGED_WEAKREF_OFFSET)     │
│  -24   8     _gc_prev / 托管字典指针 (MANAGED_DICT_OFFSET)        │
│  -16   8    预留 PREHEADER 槽位                                   │
│  -8    8    预留 PREHEADER 槽位                                   │
│  0     8     _ob_word (含 ob_gc_state + ob_gc_age + ob_flags)     │
│  8     8     ob_type                                              │
│  16          实例数据开始                                          │
│  ─────────────────────────────────────                             │
│  预头 32 字节，对象头 16 字节                                       │
│                                                                    │
│  说明：PREHEADER 槽位保留给将来使用。目前与引用计数模式行为一致。   │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ 4. GC 对象 + INLINE_VALUES                                        │
│    如 dict 类型实例                                                │
│                                                                    │
│  布局同情况 2，但实例数据后立即跟随内联值数组。                      │
│  _PyObject_InlineValues(op) 返回偏移 (tp_basicsize) 处。          │
└────────────────────────────────────────────────────────────────────┘
```

---

## 4. GC 运行时状态

### 4.1 _gc_runtime_state 结构体（Py_TRACING_GC 模式）

```c
/* ============= Include/internal/pycore_interp_structs.h (Py_TRACING_GC 分支) ============= */

#if defined(Py_TRACING_GC)

struct _gc_runtime_state {
    /* ── 开关控制 ── */
    int enabled;                              /* GC 是否启用（=1 默认） */
    int debug;                                /* GC 调试标志位 */

    /* ── 分代堆状态 ── */
    struct gc_heap_state {
        char *eden_start;                     /* Eden 区起始地址           */
        char *eden_end;                       /* Eden 区结束地址           */
        char *eden_top;                       /* 当前分配指针（bump）      */
        size_t eden_size;                     /* Eden 区总大小（字节）     */

        char *survivor_start;                 /* Survivor 区起始地址       */
        char *survivor_end;                   /* Survivor 区结束地址       */
        size_t survivor_size;                 /* Survivor 区总大小         */

        char *old_start;                      /* 老年代起始地址            */
        char *old_end;                        /* 老年代结束地址            */
        size_t old_size;                      /* 老年代总大小              */
    } heap;

    /* ── 标记栈 ── */
    struct gc_mark_stack {
        PyObject **stack;                     /* 标记栈（灰色对象列表）    */
        size_t capacity;                      /* 栈容量                   */
        size_t top;                           /* 栈顶指针                  */
    } mark_stack;

    /* ── Card Table（跨代引用追踪） ── */
    struct gc_card_table {
        uint8_t *cards;                       /* card table 起始地址       */
        size_t num_cards;                     /* card 总数                */
        size_t card_size;                     /* card 大小（默认 512B）   */
    } card_table;

    /* ── GC 触发控制 ── */
    Py_ssize_t young_threshold;               /* 新生代触发阈值（默认 16MB） */
    Py_ssize_t old_threshold;                 /* 老年代触发阈值（默认 256MB） */

    /* ── GC 正在进行中 ── */
    int collecting;                           /* 是否正在收集（重入保护） */
    _PyInterpreterFrame *collecting_frame;    /* 触发收集的帧 */

    /* ── GC 统计 ── */
    struct gc_stats {
        PyTime_t last_collection_time;         /* 上次收集时间           */
        double total_pause_time;               /* 累计暂停时间（秒）      */
        Py_ssize_t minor_collections;          /* minor GC 次数           */
        Py_ssize_t major_collections;          /* major GC 次数           */
        Py_ssize_t objects_promoted;           /* 晋升对象数              */
        Py_ssize_t total_freed;                /* 总释放对象数            */
    } stats;

    /* ── 老年代标记状态（用于增量/并发标记） ── */
    int marking_in_progress;                  /* 是否正在增量标记         */
    double marking_deadline;                   /* 增量标记截止时间         */
};

/* 默认值 */
#define _PyGC_DEFAULT_EDEN_SIZE       (16 * 1024 * 1024)   /* 16 MB */
#define _PyGC_DEFAULT_SURVIVOR_SIZE   (4 * 1024 * 1024)    /* 4 MB  */
#define _PyGC_DEFAULT_OLD_SIZE        (256 * 1024 * 1024)  /* 256 MB */
#define _PyGC_CARD_SIZE               512                   /* 512 字节每 card */

#else
/* 引用计数模式的 _gc_runtime_state 定义不变 */
#endif
```

### 4.2 分代参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| Eden 区大小 | 16 MB | 新对象 bump-pointer 分配 |
| Survivor 区大小 | 4 MB | Minor GC 时存活对象复制目标 |
| 老年代初始大小 | 256 MB | Major GC 标记-整理 |
| 新生代触发阈值 | Eden 区满 | Eden 区满时触发 minor GC |
| 老年代触发阈值 | 存活率 > 25% | 存活对象超过阈值时触发 major GC |
| Card 大小 | 512 字节 | Card table 的粒度 |
| 标记栈初始大小 | 64 KB | 可动态扩展 |

---

## 5. 分代堆布局

> **Phase 0 约束：本节的 Eden 复制收集和 Old 标记-整理（对象移动）是 Phase 2+ 目标。**
> Phase 0 实现 non-moving 分代堆：所有对象分配后地址不变。
> 逻辑分代（新生代/老年代）通过分配区域区分，但不做复制或整理。
> 本节的设计保留为 Phase 2+ 参考，Phase 0 实际实现见本节末尾的备注。

### 5.1 物理堆结构

```
低地址
  ┌──────────────────────────────────────┐
  │  Young Generation                    │
  │  ┌────────────────┬────────────────┐ │
  │  │   Eden         │   Survivor     │ │
  │  │   (16 MB)     │   (4 MB)       │ │
  │  │   [bump alloc] │   [minor GC]   │ │
  │  └────────────────┴────────────────┘ │
  ├──────────────────────────────────────┤
  │  Old Generation                      │
  │  ┌──────────────────────────────────┐│
  │  │   Old (256 MB+)                  ││
  │  │   [mark-sweep-compact]          ││
  │  └──────────────────────────────────┘│
  ├──────────────────────────────────────┤
  │  Large Object Space (>256KB)         │
  │  [单独 mmap/malloc 分配]             │
  └──────────────────────────────────────┘
  高地址
```

### 5.2 虚拟内存管理

堆由 `_PyGC_InitHeap()` 在解释器初始化时一次性 `VirtualAlloc`/`mmap` 分配。这样保证堆地址空间连续，为未来指针压缩（compressed oops）做准备。

```c
/* ============= Python/gc_heap.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * 初始化 GC 堆。
 *
 * 策略：
 * 1. 使用 VirtualAlloc (Windows) 或 mmap (POSIX) 分配连续虚拟地址空间
 * 2. 初始分配 YOUNG_SIZE + OLD_SIZE
 * 3. 如果老年代空间不足，扩展老年代（追加新的虚拟内存区域）
 * 4. 大对象（>256KB）仍然使用传统 malloc，不放入分代堆
 */

static int
_PyGC_InitHeap(_gc_runtime_state *gcstate, size_t young_size, size_t old_size)
{
    size_t total_size = young_size + old_size;
    char *base = (char*)_Py_AllocateMemory(total_size,
                                           MEM_LARGE_PAGES | MEM_RESERVE);
    if (base == NULL) {
        return -1;  /* 内存不足 */
    }

    /* Eden: base 到 base + young_size * 4/5 */
    gcstate->heap.eden_start = base;
    gcstate->heap.eden_size = young_size * 4 / 5;
    gcstate->heap.eden_end = base + gcstate->heap.eden_size;
    gcstate->heap.eden_top = base;  /* 初始为空 */

    /* Survivor: Eden 之后 */
    gcstate->heap.survivor_start = gcstate->heap.eden_end;
    gcstate->heap.survivor_size = young_size - gcstate->heap.eden_size;
    gcstate->heap.survivor_end = gcstate->heap.survivor_start
                                 + gcstate->heap.survivor_size;

    /* Old: Survivor 之后 */
    gcstate->heap.old_start = gcstate->heap.survivor_end;
    gcstate->heap.old_size = old_size;
    gcstate->heap.old_end = gcstate->heap.old_start + old_size;

    /* Card Table：覆盖整个堆 */
    size_t total_heap = young_size + old_size;
    size_t num_cards = (total_heap + _PyGC_CARD_SIZE - 1) / _PyGC_CARD_SIZE;
    gcstate->card_table.cards = (uint8_t*)calloc(num_cards, 1);
    gcstate->card_table.num_cards = num_cards;
    gcstate->card_table.card_size = _PyGC_CARD_SIZE;

    return 0;
}
```

### 5.3 Eden 区分配

```c
/* ============= Python/gc_heap.c (Py_TRACING_GC 分支) ============= */

/*
 * Eden 区 bump-pointer 分配。
 * 
 * 快速路径（内联可优化为 3-4 条指令）：
 *   1. new_top = eden_top + size
 *   2. if (new_top <= eden_end) { eden_top = new_top; return old_top; }
 *   3. else → 慢速路径（触发 minor GC）
 *
 * 对齐：始终对齐到 _PyObject_MIN_ALIGNMENT (4 字节)
 */

static inline char*
_PyGC_EdenAlloc(size_t size)
{
    /* 对齐到 4 字节 */
    size = (size + 3) & ~3;

    char *ptr = gcstate->heap.eden_top;
    char *new_top = ptr + size;

    if (_Py_IS_NORMAL_PATH(new_top <= gcstate->heap.eden_end)) {
        gcstate->heap.eden_top = new_top;
        return ptr;
    }

    /* 慢速路径：eden 空间不足 */
    return NULL;  /* 由调用者触发 minor GC */
}
```

### 5.4 Minor GC（新生代收集）

```c
/* ============= Python/gc_collect.c (Py_TRACING_GC 分支) ============= */

/*
 * Minor GC：新生代收集。
 *
 * 触发条件：
 *   1. Eden 区满（分配失败）
 *   2. 显式调用 gc.collect(0)
 *
 * 算法：复制收集（Cheney 算法简化版）
 *   1. 根枚举：扫描所有根，将直接引用的新生代对象标记为 GREY
 *   2. 标记传播：处理标记栈中的灰色对象，将其引用的对象标记
 *   3. 存活对象复制：扫描 Eden 区，将存活对象复制到 Survivor
 *   4. 角色对换：Eden 区清空，Survivor 区变为新的 Eden（如果 survivor 溢出则触发 Major GC）
 *   5. 卡表更新：清除 Eden 区对应的 card
 */

static Py_ssize_t
_PyGC_CollectYoung(PyThreadState *tstate)
{
    _gc_runtime_state *gcstate = &tstate->interp->gc;

    if (gcstate->collecting) return 0;  /* 重入保护 */
    gcstate->collecting = 1;

    /* 1. 根枚举：将根直接引用的新生代对象入栈 */
    _PyGC_MarkRoots(tstate, gcstate, YOUNG_ONLY);

    /* 2. 标记传播 */
    _PyGC_ProcessMarkStack(gcstate);

    /* 3. 复制存活对象到 Survivor */
    size_t promoted = _PyGC_CopyYoungToSurvivor(gcstate);

    /* 4. 清空 Eden */
    gcstate->heap.eden_top = gcstate->heap.eden_start;

    /* 5. 更新卡表（清除 Eden 区域的 card） */
    _PyGC_ClearCards(gcstate,
        (uintptr_t)gcstate->heap.eden_start,
        (uintptr_t)(gcstate->heap.eden_end - gcstate->heap.eden_start));

    /* 6. 如果 Survivor 溢出，触发 Major GC */
    if (gcstate->heap.survivor_top >= gcstate->heap.survivor_end) {
        _PyGC_CollectOld(tstate);
    }

    gcstate->collecting = 0;
    gcstate->stats.minor_collections++;
    return promoted;
}
```

### 5.5 Major GC（老年代收集）

```c
/* ============= Python/gc_collect.c (Py_TRACING_GC 分支) ============= */

/*
 * Major GC：老年代收集。
 *
 * 触发条件：
 *   1. Minor GC 后存活率 > 25%（连续多次 minor GC 都存活对象多）
 *   2. Survivor 区溢出
 *   3. 显式调用 gc.collect(2)
 *
 * 算法：标记-整理（mark-sweep-compact）
 *   1. 暂停所有线程（stop-the-world，或增量标记）
 *   2. 根枚举全堆
 *   3. 标记传播（三色标记，遍历整个存活对象图）
 *   4. 清除阶段：扫描老年代，回收白色对象
 *   5. 整理阶段（可选）：压缩老年代，减少碎片
 *   6. 恢复线程
 *
 * 老年代存活对象晋升年龄计数器（ob_gc_age）递增。
 * 年龄 >= 15 的对象不被整理（固定位置，指针不变）。
 */

static Py_ssize_t
_PyGC_CollectOld(PyThreadState *tstate)
{
    _gc_runtime_state *gcstate = &tstate->interp->gc;

    if (gcstate->collecting) return 0;
    gcstate->collecting = 1;

    /* 1. 全堆根枚举（含老年代和新生代） */
    _PyGC_MarkRoots(tstate, gcstate, ALL_GENERATIONS);

    /* 2. 标记传播 */
    _PyGC_ProcessMarkStack(gcstate);

    /* 3. 清除阶段：扫描老年代，回收白色对象 */
    size_t freed = _PyGC_SweepOld(gcstate);

    /* 4. 整理阶段（可选）：压缩对象 */
    _PyGC_CompactOld(gcstate);

    gcstate->collecting = 0;
    gcstate->stats.major_collections++;
    return freed;
}
```

---

## 6. 三色标记算法

### 6.1 颜色定义

| 颜色 | 含义 | GC 处理 |
|------|------|---------|
| **白色 (0)** | 未被 GC 访问 | 可能死亡；清除阶段回收 |
| **灰色 (1)** | 已发现但子对象未处理完 | 在标记栈中等待处理 |
| **黑色 (2)** | 已标记且子对象已处理 | 存活；清除阶段跳过 |
| **空闲 (3)** | 对象已回收 | 内存可供复用 |

### 6.2 标记传播核心

```c
/* ============= Python/gc_mark.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * 标记传播核心：三色标记算法的标记栈处理。
 *
 * 不变量：
 *   - 白色: 未访问（初始状态）
 *   - 灰色: 在标记栈中，子对象待处理
 *   - 黑色: 处理完成，所有直接引用的对象都已被标记
 *   - 标记栈为空 → 所有可达对象都是黑色
 *
 * 伪代码：
 *   while mark_stack is not empty:
 *       obj = mark_stack.pop()
 *       for each child in obj.tp_traverse():
 *           if child is WHITE:
 *               child = GREY
 *               mark_stack.push(child)
 *       obj = BLACK
 */

static void
_PyGC_ProcessMarkStack(_gc_runtime_state *gcstate)
{
    while (gcstate->mark_stack.top > 0) {
        PyObject *obj = gcstate->mark_stack.stack[--gcstate->mark_stack.top];

        /* 只有在灰色状态才处理 */
        if (_PyGC_GetColor(obj) != _PyGC_COLOR_GREY) {
            continue;
        }

        /* 通过 tp_traverse 遍历所有子对象 */
        PyTypeObject *type = Py_TYPE(obj);
        if (type->tp_traverse) {
            if (type->tp_flags & Py_TPFLAGS_HAVE_GC) {
                /* 为 GC 遍历创建 visitproc 回调 */
                struct _gc_visit_args args;
                args.gcstate = gcstate;
                args.visit = _PyGC_VisitChild;
                if (type->tp_traverse(obj, &_PyGC_VisitChild, &args) < 0) {
                    /* 遍历失败（如内存不足），保守处理：标记为黑色 */
                }
            }
        }

        /* 处理完成，标记为黑色 */
        _PyGC_SetColor(obj, _PyGC_COLOR_BLACK);
    }
}

/*
 * visitproc 回调：在 tp_traverse 遍历中遇到子对象时调用。
 *
 *   - 如果子对象是白色：标记为灰色，压入标记栈
 *   - 如果子对象已是灰色或黑色：跳过
 *   - 如果子对象在卡表中被标记为已修改：需要重新扫描
 */
static int
_PyGC_VisitChild(PyObject *child, void *arg)
{
    struct _gc_visit_args *args = (struct _gc_visit_args*)arg;

    if (child == NULL) {
        return 0;
    }

    if (!_PyType_IS_GC(Py_TYPE(child))) {
        /* 非容器类型（int/float/str/bytes）：只标记为黑色，不入标记栈 */
        uint32_t ccolor = _PyGC_GetColor(child);
        if (ccolor == _PyGC_COLOR_WHITE) {
            _PyGC_SetColor(child, _PyGC_COLOR_BLACK);
        }
        return 0;
    }

    uint32_t color = _PyGC_GetColor(child);

    if (color == _PyGC_COLOR_WHITE) {
        /* 第一次发现：白色 → 灰色，入标记栈 */
        _PyGC_SetColor(child, _PyGC_COLOR_GREY);
        if (_PyGC_MarkStackPush(args->gcstate, child) < 0) {
            /* 标记栈溢出 */
            return -1;
        }
    }
    /* 黑色或灰色：不需要处理 */
    return 0;
}

/*
 * 将对象压入标记栈。
 * 如果栈空间不足，动态扩展（每次 2 倍）。
 */
static int
_PyGC_MarkStackPush(_gc_runtime_state *gcstate, PyObject *obj)
{
    if (gcstate->mark_stack.top >= gcstate->mark_stack.capacity) {
        size_t new_cap = gcstate->mark_stack.capacity * 2;
        PyObject **new_stack = (PyObject**)
            PyMem_RawRealloc(gcstate->mark_stack.stack,
                             new_cap * sizeof(PyObject*));
        if (new_stack == NULL) {
            return -1;  /* 内存不足 */
        }
        gcstate->mark_stack.stack = new_stack;
        gcstate->mark_stack.capacity = new_cap;
    }
    gcstate->mark_stack.stack[gcstate->mark_stack.top++] = obj;
    return 0;
}
```

### 6.3 清除阶段（Sweep）

```c
/* ============= Python/gc_sweep.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * 清除阶段：回收白色对象。
 *
 * 扫描老年代的指定区域，对每个白色对象：
 *   1. 调用 tp_finalize（如果已定义且未调用过）
 *   2. 如果对象在 finalize 中复活（变为黑色），跳过
 *   3. 否则调用 tp_clear 断开引用
 *   4. 调用 tp_free 释放内存
 *   5. 将 ob_gc_state 设置为 _PyGC_COLOR_FREE
 */

static size_t
_PyGC_SweepOld(_gc_runtime_state *gcstate)
{
    size_t freed = 0;
    char *scan = gcstate->heap.old_start;

    while (scan < gcstate->heap.old_end) {
        PyObject *op = (PyObject*)scan;
        size_t obj_size = _PyGC_GetObjectSize(op);

        if (_PyGC_GetColor(op) == _PyGC_COLOR_WHITE) {
            /* ── 回收白色对象 ── */
            PyTypeObject *type = Py_TYPE(op);

            /* 调用 tp_finalize（如果适用） */
            if (type->tp_finalize && !(op->ob_gc_state & _PyGC_FINALIZED_MASK)) {
                op->ob_gc_state |= _PyGC_FINALIZED_MASK;
                PyObject_CallFinalizerFromDealloc(op);
                /* 如果在 finalize 中被复活，颜色变为黑色，跳过 */
                if (_PyGC_GetColor(op) != _PyGC_COLOR_WHITE) {
                    scan += obj_size;
                    continue;
                }
            }

            /* 调用 tp_clear 断开循环引用 */
            if (type->tp_clear) {
                type->tp_clear(op);
            }

            /* 调用 tp_dealloc 作为 finalizer（清理 C 资源） */
            /* 注意：Legacy 扩展的 tp_dealloc 内部可能会调用 tp_free。
             * 此时通过 tp_free_delegated 标志避免重复释放。 */
            int free_was_called = 0;
            if (type->tp_dealloc) {
                type->tp_dealloc(op);
                free_was_called = (type->tp_flags & _Py_TPFLAGS_FREE_DELEGATED);
            }

            /* 调用 tp_free 释放内存（仅当 dealloc 未代为释放时） */
            if (!free_was_called && type->tp_free) {
                type->tp_free(op);
            }

            /* 标记为空闲 */
            op->ob_gc_state = _PyGC_COLOR_FREE;
            freed++;
        }

        scan += obj_size;
    }

    return freed;
}
```

### 6.4 Epoch 标记轮次机制

```
三色标记的颜色在每轮 GC 后不能残留——否则下一轮所有存活（BLACK）对象
会被误认为"已经标记过"而跳过。必须区分"上一轮标记的结果"和"本轮标记的状态"。

解决方案：mark_epoch（标记轮次计数器）。

每轮 GC 开始时递增全局 epoch，每轮 GC 结束时 epoch 不变（存活对象保留标记）。
对象的颜色计算为 (ob_gc_state & COLOR_MASK)，结合 epoch 判断：

  (epoch, color) 含义：
  当前 epoch × 白色：本轮未访问 → 死亡候选
  当前 epoch × 黑色：本轮已标记 → 存活
  旧 epoch × 黑色：等效于白色（上一轮的标记在本轮无效）

简化实现（Phase 0）：
  每轮 major GC 后扫描所有存活对象清除颜色（BLACK→WHITE 复位）。
  代价：O(存活对象数) 扫描，但对 Phase 0（堆不大）可接受。
  后续优化为基于 epoch 的方案，避免复位扫描。
```

---

## 7. 写屏障与 Card Table

### 7.1 Card Table 设计

```c
/* ============= Include/internal/pycore_cardtable.h (新增) ============= */

/*
 * Card Table 用于追踪从老年代到新生代的跨代引用。
 *
 * 原理：
 *   堆被划分为固定大小的 card（默认 512 字节）。
 *   当对老年代对象中的引用字段赋值时，计算目标地址所在的 card，
 *   将该 card 标记为 DIRTY。
 *
 *   Minor GC 时，扫描所有 DIRTY card，从中找出 Old→Young 引用。
 *   这避免了扫描整个老年代来查找跨代引用。
 *
 * 只保护 Old→Young 方向：
 *   - Young→Old：不需要记录（minor GC 不会回收老年代）
 *   - Young→Young：不需要记录（minor GC 扫描整个 Eden）
 *   - Old→Old：不需要记录（在老年代内部，不会影响年轻代回收）
 *
 * 写入点：
 *   - PyList_SetItem (listobject.c)
 *   - PyDict_SetItem (dictobject.c)
 *   - PyObject_SetAttr (object.c)
 *   - 其他所有写入堆对象引用字段的操作
 */

#define _PyGC_CARD_SHIFT   9          /* 2^9 = 512 字节 */
#define _PyGC_CARD_SIZE    (1 << _PyGC_CARD_SHIFT)
#define _PyGC_CARD_MASK    (_PyGC_CARD_SIZE - 1)

/* Card 状态 */
#define _PyGC_CARD_CLEAN   0          /* 未修改 */
#define _PyGC_CARD_DIRTY   1          /* 被修改过 */

/*
 * 计算对象指针所在的 card 索引。
 *
 * 公式：card_index = (addr - heap_base) >> CARD_SHIFT
 */
static inline size_t
_PyGC_GetCardIndex(_gc_runtime_state *gcstate, uintptr_t addr)
{
    uintptr_t offset = addr - (uintptr_t)gcstate->heap.old_start;
    return offset >> _PyGC_CARD_SHIFT;
}

/*
 * 写屏障：在对象引用字段写入后调用。
 *
 * 调用约定：在老年代对象的引用字段被修改后调用此宏。
 *
 * 参数：
 *   gcstate: GC 运行时状态
 *   old_obj: 被修改的老年代对象指针
 *   new_val: 写入的新值（被引用的对象）
 *
 * 行为：
 *   - 如果 old_obj 是老年代对象 且 new_val 是新生代对象
 *     → 标记 old_obj 所在 card 为 DIRTY
 *   - 其他情况 → 无操作
 */
static inline void
_PyGC_WriteBarrier(_gc_runtime_state *gcstate,
                   PyObject *old_obj, PyObject *new_val)
{
    /* 快速检查：只关心 old→young 方向 */
    /* new_val 为 NULL 或非 GC 对象时跳过 */
    if (new_val == NULL) return;
    if (!_PyType_IS_GC(Py_TYPE(new_val))) return;

    /* 检查 old_obj 是否在老年代 */
    char *addr = (char*)old_obj;
    if (addr < gcstate->heap.old_start || addr >= gcstate->heap.old_end) {
        return;  /* 不是老年代对象，不需要屏障 */
    }

    /* 检查 new_val 是否在新生代（Eden + Survivor = 年轻代） */
    char *new_addr = (char*)new_val;
    char *young_start = gcstate->heap.eden_start;  /* Eden 起始 */
    char *young_end = gcstate->heap.survivor_end;  /* Survivor 结束 = 年轻代结束 */
    if (new_addr >= young_start && new_addr < young_end) {
        /* 跨代引用！标记 card 为 DIRTY */
        size_t card_idx = _PyGC_GetCardIndex(gcstate, (uintptr_t)addr);
        gcstate->card_table.cards[card_idx] = _PyGC_CARD_DIRTY;
    }
}

/* 写屏障的快速宏形式（内联展开） */
#define _PyGC_WRITE_BARRIER(gcstate, old, new_val) \
    do { \
        if (new_val != NULL) { \
            if (_PyType_IS_GC(Py_TYPE(new_val))) { \
                _PyGC_WriteBarrier(gcstate, old, new_val); \
            } \
        } \
    } while (0)
```

### 7.2 需要插入写屏障的操作点

以下所有操作在 `Py_TRACING_GC` 模式下必须插入 `_PyGC_WRITE_BARRIER` 调用：

| 文件 | 函数 | 插入位置 |
|------|------|---------|
| `Objects/listobject.c` | `PyList_SetItem` | 在 `FT_ATOMIC_STORE_PTR_RELEASE` 之后 |
| `Objects/listobject.c` | `ins1` (list insert) | 在元素写入之后 |
| `Objects/listobject.c` | `list_ass_slice` | 在切片赋值循环中每次写入后 |
| `Objects/listobject.c` | `list_inplace_repeat` | 在元素复制循环中 |
| `Objects/dictobject.c` | `PyDict_SetItem` | 在 `dict_set_item_by_hash` 返回值后 |
| `Objects/dictobject.c` | `_PyDict_SetItem_Take2` | 同上 |
| `Objects/dictobject.c` | `insertdict` | 在 `DK_UNICODE_ENTRIES` 写入后 |
| `Objects/dictobject.c` | `dict_set_default_ref` | 在设置值后 |
| `Objects/setobject.c` | `set_add_entry` | 在 `set_insert_key` 后 |
| `Objects/setobject.c` | `set_swap_bodies` | 在指针交换后 |
| `Objects/object.c` | `PyObject_SetAttr` | 在调用 `tp_setattro` 之前 |
| `Objects/typeobject.c` | `type_setattro` | 在 `type->tp_dict` 修改后 |
| `Objects/moduleobject.c` | `PyModule_AddObject` | 在 `PyDict_SetItemString` 后 |
| `Objects/funcobject.c` | `func_set_code` | 在设置 `func_code` 后 |
| `Objects/funcobject.c` | `func_set_globals` | 在设置 `func_globals` 后 |
| `Objects/funcobject.c` | `func_set_defaults` | 在设置 `func_defaults` 后 |
| `Objects/cellobject.c` | `cell_ass_sub` | 在 `cell->ob_ref` 赋值后 |
| `Objects/weakrefobject.c` | `PyWeakref_NewRef` | 在建立弱引用后 |

### 7.3 脏卡处理（Minor GC 时）

```c
/* ============= Python/gc_cardtable.c (新增) ============= */

/*
 * Minor GC 时扫描脏卡，找出 Old→Young 引用。
 *
 * 对每个 DIRTY card：
 *   1. 扫描 card 范围内的所有对象（通过对齐扫描）
 *   2. 对每个老年代对象，调用 tp_traverse
 *   3. 如果遍历到新生代对象，确保其被标记
 *   4. 清除 card 状态
 */
static void
_PyGC_ScanDirtyCards(_gc_runtime_state *gcstate)
{
    size_t num_cards = gcstate->card_table.num_cards;

    for (size_t i = 0; i < num_cards; i++) {
        if (gcstate->card_table.cards[i] != _PyGC_CARD_DIRTY) {
            continue;
        }

        /* 计算 card 对应的堆地址范围 */
        uintptr_t card_start = (uintptr_t)gcstate->heap.old_start
                               + (i << _PyGC_CARD_SHIFT);
        uintptr_t card_end = card_start + _PyGC_CARD_SIZE;

        /* 扫描 card 中的每个对象 */
        char *scan = (char*)card_start;
        /* ⚠️ 限制：card_start 可能落在对象中间。必须配合 object-start bitmap
         * 或 line table 找到真正的对象起始地址。简化实现（Phase 0）：
         *   从最近的对齐边界开始，用 allocation bitmap 验证是否为有效对象头。
         * 后续版本需要 object_start_bitmap 或 block_map 辅助定位。
         * 见 §7.4 的 pymalloc 集成说明。 */
        while (scan < (char*)card_end) {
            PyObject *op = (PyObject*)scan;
            uint32_t color = _PyGC_GetColor(op);

            /* 只处理老年代的黑色（存活）对象 */
            if (color == _PyGC_COLOR_BLACK
                && _PyType_IS_GC(Py_TYPE(op))
                && Py_TYPE(op)->tp_traverse) {
                /* 遍历老年代对象，标记其引用的新生代对象 */
                struct _gc_visit_args args;
                args.gcstate = gcstate;
                args.visit = _PyGC_VisitChild;
                Py_TYPE(op)->tp_traverse(op, &_PyGC_VisitChild, &args);
            }

            /* 跳到下一个对象 */
            scan += _PyGC_GetObjectSize(op);
        }

        /* 清除 card 状态 */
        gcstate->card_table.cards[i] = _PyGC_CARD_CLEAN;
    }
}
```

### 7.4 写屏障的限制与回退

```
写屏障的假设：所有 Old→Young 引用写入都经过已知插入点（§7.2 列表）。
这对 CPython 核心代码可以做，但对第三方 C 扩展不成立。

C 扩展中的无保护写入：
  static void set_child(MyObject *self, PyObject *child) {
      self->child = child;   // 直接指针赋值，无写屏障
  }

当 self 在老年代、child 在新生代时，此写入后的 card 未标记 DIRTY，
minor GC 将无法发现此跨代引用，导致 child 被误回收。

解决方案（Phase 0 non-moving 模式不需要分代写屏障）：
  · minor GC 时全量扫描老年代（放弃 card table 性能收益），保证正确性。
  · 对 Legacy 扩展对象做 full old traversal（通过 tp_traverse）。
  · 对 TraceGC-safe 扩展对象使用精确写屏障（§7.2 列表）。

pymalloc 集成与 object-start metadata：
  _PyGC_ScanDirtyCards 需要从 card 范围中找到对象起始地址。
  当前 pymalloc pool 中混合多种 size class 的对象，任意地址强制转为
  PyObject* 会读取到对象中间的数据，导致崩溃。

  需要的数据结构：
    1. object-start bitmap：每对齐单元 1 bit，标记是否对象起始。
    2. line table / block map：记录 pool 内对象边界。
    3. allocation bitmap：pymalloc 的已分配块位图。

  Phase 0 简化：
    · 不做 card table。
    · minor GC 通过全量扫描老年代 tp_traverse 代替 card dirty 检测。
    · 卡表在 Phase 2（分代优化）时引入，配合 object-start metadata。
```

---

## 8. 根枚举系统

### 8.1 根集合定义

```c
/* ============= Python/gc_roots.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * 根枚举：GC 标记的起点。
 *
 * 所有可达对象从以下根集合出发。
 * 如果一个对象不能从任何根到达，它就是垃圾。
 *
 * 根集合（共 11 类，按稳定性排序）：
 *
 *   稳定的根（永不改变）：
 *   ┌────────────────────────────────────────────┐
 *   │ 1. 内置类型对象（PyLong_Type 等）           │
 *   │ 2. 运行时单例（None, True, False, Ellipsis） │
 *   │ 3. 小整数缓存（-5 到 256）                  │
 *   │ 4. 字符串驻留池（interned strings）          │
 *   └────────────────────────────────────────────┘
 *
 *   线程相关的根（每个线程不同）：
 *   ┌────────────────────────────────────────────┐
 *   │ 5. 当前帧的值栈（localsplus 和 stack）      │
 *   │ 6. 异常状态（exc_info, curexc_type 等）     │
 *   │ 7. 线程局部存储（tstate->dict）              │
 *   │ 8. C 栈引用（_PyCStackRef 链表）            │
 *   └────────────────────────────────────────────┘
 *
 *   解释器级别的根：
 *   ┌────────────────────────────────────────────┐
 *   │ 9. 模块 __dict__（sys.modules 等）          │
 *   │ 10. builtins 字典                           │
 *   │ 11. 类型对象的 tp_dict/tp_subclasses        │
 *   └────────────────────────────────────────────┘
 */

typedef struct _gc_root_visitor {
    void (*visit)(PyObject *root, void *arg);   /* 访问器 */
    void *arg;                                    /* 上下文参数 */
    _gc_runtime_state *gcstate;                  /* GC 状态 */
} _gc_root_visitor;
```

### 8.2 根枚举实现

```c
/*
 * 主根枚举函数：遍历所有根，将直接引用的对象标记为灰色。
 *
 * 参数：
 *   mode: YOUNG_ONLY — 只标记新生代对象（用于 minor GC）
 *         ALL_GENERATIONS — 标记所有对象（用于 major GC）
 */

typedef enum {
    YOUNG_ONLY,
    ALL_GENERATIONS
} _PyGC_RootMode;

static void
_PyGC_MarkRoots(PyThreadState *tstate, _gc_runtime_state *gcstate,
                _PyGC_RootMode mode)
{
    _gc_root_visitor visitor;
    visitor.visit = _PyGC_RootMarkVisit;
    visitor.arg = &mode;
    visitor.gcstate = gcstate;

    /* ── 稳定的根 ── */
    _PyGC_VisitRuntimeSingletons(&visitor);     /* None, True, False 等 */
    _PyGC_VisitInternedStrings(&visitor);        /* 字符串驻留池 */
    _PyGC_VisitSmallIntegers(&visitor);          /* 小整数缓存 */
    _PyGC_VisitStaticTypes(&visitor);            /* 内置类型对象 */

    /* ── 线程相关的根 ── */
    _PyGC_VisitThreadStates(&visitor);           /* 遍历所有线程 */

    /* ── 解释器级别的根 ── */
    _PyGC_VisitModules(&visitor);                /* sys.modules */
    _PyGC_VisitBuiltins(&visitor);               /* builtins */
    _PyGC_VisitHeapTypes(&visitor);              /* 堆类型的 tp_dict 等 */
}

/*
 * 根标记访问器：将根对象标记为灰色并压入标记栈。
 * 如果 mode == YOUNG_ONLY，只处理新生代内的对象。
 */
static void
_PyGC_RootMarkVisit(PyObject *root, void *arg)
{
    _PyGC_RootMode *mode = (_PyGC_RootMode*)arg;

    if (root == NULL) return;

    /* 所有对象都必须标记为存活，即使不是容器。
     * 非容器对象（int/float/str）不能作为标记传播的起点（无 tp_traverse），
     * 但必须标记为 BLACK 防误回收。 */
    if (!_PyType_IS_GC(Py_TYPE(root))) {
        _PyGC_SetColor(root, _PyGC_COLOR_BLACK);
        return;
    }

    /* 检查地址范围（如果只处理新生代） */
    char *addr = (char*)root;
    if (*mode == YOUNG_ONLY) {
        /* 跳过老年代对象 */
        if (addr >= gcstate->heap.old_start
            && addr < gcstate->heap.old_end) {
            return;
        }
    }

    /* 标记为灰色并压入标记栈 */
    if (_PyGC_GetColor(root) != _PyGC_COLOR_BLACK) {
        _PyGC_SetColor(root, _PyGC_COLOR_GREY);
        _PyGC_MarkStackPush(gcstate, root);
    }
}
```

### 8.3 每类根的详细访问器

```c
/* 访问运行时单例（None, True, False, Ellipsis, NotImplemented） */
static void
_PyGC_VisitRuntimeSingletons(_gc_root_visitor *visitor)
{
    visitor->visit(_Py_NoneStruct, visitor->arg);
    visitor->visit(_Py_TrueStruct, visitor->arg);
    visitor->visit(_Py_FalseStruct, visitor->arg);
    visitor->visit(_Py_EllipsisObject, visitor->arg);
    visitor->visit(_Py_NotImplementedStruct, visitor->arg);
}

/* 访问字符串驻留池 */
static void
_PyGC_VisitInternedStrings(_gc_root_visitor *visitor)
{
    PyObject *interned = _PyRuntime.cached_objects.interned_strings;
    if (interned) {
        visitor->visit(interned, visitor->arg);
    }
}

/* 访问小整数缓存（-5 到 257） */
static void
_PyGC_VisitSmallIntegers(_gc_root_visitor *visitor)
{
    for (int i = 0; i < _Py_SMALL_INT_COUNT; i++) {
        visitor->visit((PyObject*)&_Py_SmallInts[i], visitor->arg);
    }
}

/* 访问所有线程状态 */
static void
_PyGC_VisitThreadStates(_gc_root_visitor *visitor)
{
    PyThreadState *tstate = NULL;
    while ((tstate = PyInterpreterState_ThreadHead(interp, tstate))) {
        _PyGC_VisitOneThread(tstate, visitor);
    }
}

/* 访问单个线程的根 */
static void
_PyGC_VisitOneThread(PyThreadState *tstate, _gc_root_visitor *visitor)
{
    /* 当前帧的值栈 */
    _PyInterpreterFrame *frame = tstate->current_frame;
    while (frame) {
        _PyGC_VisitFrameStack(frame, visitor);
        frame = frame->previous;
    }

    /* 异常状态 */
    if (tstate->exc_info) {
        visitor->visit(tstate->exc_info->exc_value, visitor->arg);
    }
    visitor->visit(tstate->curexc_type, visitor->arg);
    visitor->visit(tstate->curexc_value, visitor->arg);
    visitor->visit(tstate->curexc_traceback, visitor->arg);

    /* 线程局部存储 */
    visitor->visit(tstate->dict, visitor->arg);
}

/* 访问帧的值栈 */
static void
_PyGC_VisitFrameStack(_PyInterpreterFrame *frame, _gc_root_visitor *visitor)
{
    PyObject **locals = _PyFrame_GetLocalsArray(frame);
    PyObject **stack_pointer = frame->stackpointer;
    for (PyObject **ptr = locals; ptr < stack_pointer; ptr++) {
        visitor->visit(*ptr, visitor->arg);
    }
}

/* 访问所有模块的 __dict__ */
static void
_PyGC_VisitModules(_gc_root_visitor *visitor)
{
    PyObject *modules = PyImport_GetModuleDict();
    if (modules) {
        visitor->visit(modules, visitor->arg);
    }
}

/* 访问 builtins */
static void
_PyGC_VisitBuiltins(_gc_root_visitor *visitor)
{
    PyObject *builtins = PyEval_GetBuiltins();
    if (builtins) {
        visitor->visit(builtins, visitor->arg);
    }
}

/* 访问所有静态内置类型的元数据 */
static void
_PyGC_VisitStaticTypes(_gc_root_visitor *visitor)
{
    for (int i = 0; i < _Py_STATIC_BUILTIN_COUNT; i++) {
        PyTypeObject *type = &_PyStaticBuiltinTypes[i];
        visitor->visit(type->tp_dict, visitor->arg);
        visitor->visit((PyObject*)type->tp_subclasses, visitor->arg);
        visitor->visit(type->tp_weaklist, visitor->arg);
    }
}

/* 访问所有堆类型 */
static void
_PyGC_VisitHeapTypes(_gc_root_visitor *visitor)
{
    /* 通过类型对象链遍历所有已创建的堆类型 */
    PyTypeObject *type = interp->types.head;
    while (type) {
        visitor->visit(type->tp_dict, visitor->arg);
        visitor->visit((PyObject*)type->tp_subclasses, visitor->arg);
        visitor->visit(type->tp_weaklist, visitor->arg);
        type = type->next;
    }
}
```

---

## 9. GC 安全点机制

### 9.1 设计说明

```
安全点（Safepoint）是线程执行中暂停检查的点。GC 需要所有线程在安全点暂停
以确保对象图的一致性。

在带 GIL 的构建中，由于 GIL 已经序列化了所有 Python 线程，GC 在持有 GIL 时
自然获得"停止"效果。但 C 扩展代码可能长时间不释放 GIL，需要在有限时间内
到达安全点。

安全点协议：
  1. GC 线程设置全局 safepoint 请求标志
  2. 其他线程在到达安全点时检查标志
  3. 如果设置了标志，线程进入阻塞等待（或协助 GC）
  4. 所有线程确认到达安全点后，GC 开始工作
  5. GC 完成后，清除标志，唤醒所有线程

╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  安全点的根本限制：C 扩展栈上的 PyObject* 引用在安全点时           ║
║  仍然对 GC 不可见。GIL 只保证并发互斥，不保证根集合完整性。           ║
║                                                                      ║
║  C 扩展在安全点暂停时，其 C 栈帧上可能持有多个 PyObject* 局部变量：   ║
║    PyObject *x = PyObject_Call(...);  // new reference on C stack    ║
║    some_function_that_may_allocate();  // triggers GC, x invisible   ║
║    use(x);                                                            ║
║                                                                      ║
║  解决策略（Phase 0）：                                                ║
║  1. 保守 C 栈扫描：将 C 栈上所有对齐的指针值视为潜在根。              ║
║     误保活增加内存占用但保证正确性。                                   ║
║  2. 扫描范围：从 current_frame 的 C 栈帧往下到线程栈底。              ║
║  3. 精确帧扫描：解释器帧（_PyInterpreterFrame）使用精确的              ║
║     stackpointer 范围（现有 _PyGC_VisitFrameStack 不变）。            ║
║  4. 未来优化：_PyCStackRef（自由线程已有）扩展为通用 C 栈引用注册。   ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 9.2 安全点位置

```c
/*
 * 安全点插入位置：
 *
 * 1. 字节码循环的每次迭代（Python/ceval.c 的 _PyEval_EvalFrameDefault 中）
 * 2. 内存分配请求（PyObject_Malloc 等）
 * 3. 线程进入阻塞状态前（如 I/O 等待）
 * 4. 从 C 扩展返回 Python 解释器时
 * 5. 显式的 _Py_CHECK_PERIODIC 检查点
 *
 * 安全点检查宏（应接近零开销）：
 */

/* ============= Include/internal/pycore_safepoint.h (新增) ============= */

/*
 * 安全点轮询：检查是否需要暂停进行 GC。
 *
 * 正常路径（无 GC 请求时）的开销：
 *   1. 一次内存读取（检查 atomic 标志位）
 *   2. 一个条件分支（几乎永远不进入）
 *
 * 实现：通过为 safepoint 页面设置不可读权限来触发信号（类似 JVM）
 * 或：通过解释器状态字中的位检查
 */

#if defined(Py_TRACING_GC)

/*
 * 简化实现：通过 tstate 中的标志位检查。
 * 后续可优化为基于 page protection 的信号方式。
 */
static inline void
_Py_SafepointCheck(PyThreadState *tstate)
{
    if (_Py_atomic_load_int_relaxed(&tstate->gc_safepoint_requested)) {
        _Py_EnterSafepoint(tstate);
    }
}

/*
 * 进入安全点：暂停当前线程，等待 GC 完成。
 */
static void
_Py_EnterSafepoint(PyThreadState *tstate)
{
    /* 通知 GC 本线程已暂停 */
    _Py_atomic_store_int_relaxed(&tstate->at_safepoint, 1);

    /* 等待 GC 完成 */
    while (_Py_atomic_load_int_relaxed(&tstate->interp->gc_safepoint_active)) {
        _Py_WaitOnAddress(&tstate->interp->gc_safepoint_active, 1);
    }

    /* 清除暂停标志 */
    _Py_atomic_store_int_relaxed(&tstate->gc_safepoint_requested, 0);
    _Py_atomic_store_int_relaxed(&tstate->at_safepoint, 0);
}

/*
 * 请求所有线程进入安全点。
 */
static void
_Py_RequestSafepoint(PyInterpreterState *interp)
{
    /* 设置全局安全点标志 */
    _Py_atomic_store_int_relaxed(&interp->gc_safepoint_active, 1);

    /* 遍历所有线程，设置请求标志 */
    PyThreadState *tstate = NULL;
    while ((tstate = PyInterpreterState_ThreadHead(interp, tstate))) {
        _Py_atomic_store_int_relaxed(&tstate->gc_safepoint_requested, 1);
    }

    /* 等待所有线程到达安全点 */
    while (1) {
        int all_at = 1;
        tstate = NULL;
        while ((tstate = PyInterpreterState_ThreadHead(interp, tstate))) {
            if (!_Py_atomic_load_int_relaxed(&tstate->at_safepoint)) {
                all_at = 0;
                break;
            }
        }
        if (all_at) break;
        _Py_YieldThread();
    }
}

/*
 * 恢复所有线程。
 */
static void
_Py_ResumeFromSafepoint(PyInterpreterState *interp)
{
    _Py_atomic_store_int_relaxed(&interp->gc_safepoint_active, 0);
    /* 等待线程唤醒 */
    _Py_WakeAll(&interp->gc_safepoint_active);
}

#else
/* 传统模式：安全点检查为空操作 */
#define _Py_SafepointCheck(tstate) ((void)0)
#endif
```

---

## 10. GC 收集主流程

### 10.1 整体流程图

```
GC Collect Main Entry
│
├─ _PyGC_MaybeCollect(tstate)          ← 分配时触发，检查阈值
│   │
│   ├─ Eden 区满 → _PyGC_CollectYoung(tstate)
│   │   ├─ _PyGC_MarkRoots(YOUNG_ONLY)
│   │   ├─ _PyGC_ProcessMarkStack()
│   │   ├─ _PyGC_ScanDirtyCards()      ← 扫描脏卡找 Old→Young 引用
│   │   ├─ _PyGC_CopyYoungToSurvivor()
│   │   └─ 重置 Eden 区
│   │
│   ├─ Survivor 溢出 → _PyGC_CollectMajor(tstate)
│   │   ├─ _PyGC_CollectYoung(tstate)   ← 先清理新生代
│   │   ├─ _PyGC_MarkRoots(ALL)
│   │   ├─ _PyGC_ProcessMarkStack()
│   │   ├─ _PyGC_SweepOld()            ← 回收白色对象
│   │   └─ _PyGC_CompactOld()          ← 可选整理
│   │
│   └─ 老年代存活率 > 25% → _PyGC_CollectMajor(tstate)
│
└─ gc.collect(n) 显式调用              ← 入口在 gcmodule.c
```

### 10.2 主收集入口

```c
/* ============= Python/gc_collect.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * 内存分配时触发的 GC 检查。
 * 在 _PyObject_GC_Link (或 gc_alloc) 的末尾被调用。
 *
 * 返回释放的对象数（0 表示未触发 GC）。
 */
Py_ssize_t
_PyGC_MaybeCollect(PyThreadState *tstate)
{
    _gc_runtime_state *gcstate = &tstate->interp->gc;

    if (!gcstate->enabled) return 0;
    if (gcstate->collecting) return 0;   /* 重入保护 */

    Py_ssize_t freed = 0;

    /* 检查 Eden 区是否满 */
    if (gcstate->heap.eden_top >= gcstate->heap.eden_end) {
        freed += _PyGC_CollectYoung(tstate);
    }

    /* 检查老年代触发条件 */
    /* 老年代存活率 = (存活对象数 / 老年代大小) > 25% */
    size_t live_ratio = gcstate->stats.objects_promoted * 100
                        / (gcstate->heap.old_size / sizeof(PyObject));
    if (live_ratio > 25) {
        freed += _PyGC_CollectOld(tstate);
    }

    return freed;
}
```

---

## 11. C API 兼容层

### 11.1 引用计数宏重定义

```c
/* ============= Include/refcount.h (Py_TRACING_GC 分支) ============= */

#if defined(Py_TRACING_GC)

/*
 * 追踪式 GC 模式下，引用计数操作变为空操作或轻量钩子。
 *
 * 原则：
 *   1. Py_INCREF: 空操作（引用由 GC 追踪）
 *   2. Py_DECREF: 空操作（内存由 GC 管理）
 *   3. Py_REFCNT: 返回 1（对象活着）或 0（不可达，但可能未被回收）
 *   4. Py_NewRef: 直接返回输入指针
 *   5. Py_CLEAR: 只设置指针为 NULL（不需要递减）
 *
 * 这些宏必须保持与引用计数模式完全相同的函数签名，
 * 以确保 C 扩展无需修改源码。
 */

/* ── Py_INCREF ── */
static inline void
Py_INCREF(PyObject *op)
{
    (void)op;
    /* 追踪式 GC 下，引用计数操作不需要。对象由 GC 追踪。 */
}

/* ── Py_DECREF ── */
static inline void
Py_DECREF(PyObject *op)
{
    (void)op;
    /* 追踪式 GC 下，对象释放由 GC 在清除阶段统一处理。 */
}

/* ── Py_XINCREF / Py_XDECREF ── */
static inline void
Py_XINCREF(PyObject *op)
{
    (void)op;
}
static inline void
Py_XDECREF(PyObject *op)
{
    (void)op;
}

/* ── Py_NewRef / Py_XNewRef ── */
static inline PyObject*
Py_NewRef(PyObject *op)
{
    return op;  /* 返回同一指针（引用计数不存在了） */
}
static inline PyObject*
Py_XNewRef(PyObject *op)
{
    return op;
}

/* ── Py_CLEAR ── */
static inline void
Py_CLEAR(PyObject *op)
{
    /* 只需要将指针设为 NULL，不需要释放 */
    /* 注意：此宏的参数是左值（如 op->field），不能简单地 (void) */
    _Py_CLEAR_CAST(op) = NULL;
}

/* ── Py_SETREF / Py_XSETREF ── */
#define Py_SETREF(dst, src)                                         \
    do {                                                            \
        PyObject **_dst = (PyObject**)&(dst);                       \
        *_dst = (PyObject*)(src);                                   \
    } while (0)

#define Py_XSETREF(dst, src)                                        \
    do {                                                            \
        PyObject **_dst = (PyObject**)&(dst);                       \
        *_dst = (PyObject*)(src);                                   \
    } while (0)

/* ── Py_REFCNT / Py_SET_REFCNT ── */
/*
 * ⚠️  不能暴露 GC 颜色为 Py_REFCNT。
 * 原因：
 *   新分配对象初始为 WHITE，但它在 C 栈上有引用（尚未完成初始化）。
 *   如果 Py_REFCNT 此时返回 0，C 扩展的 refcount 检查逻辑会认为
 *   对象已死，造成 use-after-free。
 *
 * 改正：
 *   在 Py_TRACING_GC 下，Py_REFCNT 返回稳定哨兵值 1。
 *   - 1 是有效引用计数的最低非零值
 *   - 不暴露 GC 内部状态
 *   - C 扩展依赖的 Py_REFCNT(op) > 0 不变量继续保持
 */
static inline Py_ssize_t
Py_REFCNT(PyObject *op)
{
    (void)op;
    return 1;  /* 稳定哨兵：活着 */
}

static inline void
Py_SET_REFCNT(PyObject *op, Py_ssize_t value)
{
    /* 在 Py_TRACING_GC 下，允许设置标志性值
     * （如强制对象存活/释放），但不支持任意引用计数操作 */
    if (value == 0) {
        /* 请求标记为死亡（GC 可回收） */
        _PyGC_SetColor(op, _PyGC_COLOR_WHITE);
    } else if (value > 1) {
        /* 请求保持存活 */
        _PyGC_SetColor(op, _PyGC_COLOR_BLACK);
    }
    /* value == 1 时无操作（默认存活状态） */
}

/* ── Py_IsImmortal ── */
static inline int
Py_IsImmortal(PyObject *op)
{
    /* 检查 ob_flags 和 ob_gc_state */
    if (op->ob_flags & _Py_STATICALLY_ALLOCATED_FLAG) return 1;
    if (op->ob_flags & _Py_IMMORTAL_FLAG) return 1;
    return 0;
}

#else
/* 传统引用计数的 Py_INCREF/Py_DECREF 定义不变 */
#endif
```

### 11.2 tp_dealloc 语义变更

```
╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  重要限制：简单的包装器无法可靠拦截原始 tp_dealloc 对 tp_free 的调用。 ║
║                                                                      ║
║  当前代码库中，tp_dealloc 调用 tp_free 的位置不统一：                 ║
║    ·  pattern A: Py_TYPE(op)->tp_free(op)    (dict, type, module)    ║
║    ·  pattern B: PyObject_GC_Del(op)          (list, func, cell)     ║
║    ·  pattern C: PyObject_Free(op)            (code, long via freelist)║
║                                                                      ║
║  包装器 _Py_GC_FinalizerWrapper 只能防止 tp_dealloc 被递归调用，      ║
║  不能阻止 original_dealloc 调用 pattern A/B/C。因此：                 ║
║    1. 包装器不是通用解。                                              ║
║    2. 需要按兼容级别采用不同策略（见下文）。                            ║
╚══════════════════════════════════════════════════════════════════════╝
```

追踪式 GC 模式下，`tp_dealloc` 的语义变更按兼容层级不同：

| 模式 | tp_dealloc 角色 | 内存释放者 | tp_free 的处理 |
|------|----------------|-----------|---------------|
| 引用计数（原模式） | 清理 + 释放 | tp_dealloc 内调用 tp_free | 正常行为 |
| Legacy 扩展 (Phase 0) | **不变**——保留完整 dealloc 语义 | tp_dealloc 内 tp_free 照常执行 | 不做拦截 |
| Compatible 扩展 | **finalizer**（清理 C 资源） | GC sweep 阶段调用 tp_free | 包装器替换 tp_free 为 no-op |
| TraceGC-safe 扩展 | **finalizer**（可选） | GC sweep 阶段调用 tp_free | 类型初始化时 tp_free = gc_free |

```c
/* ============= Include/object.h (Py_TRACING_GC 分支) ============= */

/*
 * tp_dealloc 在追踪式 GC 模式下的语义变更。
 *
 * 引用计数模式:
 *   tp_dealloc 负责:
 *     1. 释放持有的子对象引用（Py_DECREF）
 *     2. 释放内部 C 内存（free）
 *     3. 调用 tp_free 释放对象内存
 *   触发时机: Py_DECREF 使引用计数归零时
 *
 * 追踪式 GC 模式:
 *   tp_dealloc 负责:
 *     1. 释放内部 C 内存（free 非 Python 资源）
 *     2. 不再调用 tp_free（由 GC 在清除阶段统一调用）
 *   触发时机: GC 在清除阶段发现对象为白色（不可达）时
 *
 *   注意：Py_DECREF 在追踪式 GC 下是空操作，因此引用计数归零
 *   不会触发 tp_dealloc。但 Legacy 兼容模式保留旧语义。
 */

/*
 * Legacy 扩展的 tp_dealloc：保持不变。
 * 在 Legacy 模式下，Py_DECREF 退化为空操作，
 * 但 tp_dealloc 仍在 sweep 阶段被调用（做资源清理 + 释放内存）。
 * 不包装、不拦截——保持原始行为。
 */

/*
 * Compatible 扩展的 tp_dealloc 处理（Phase 0）：
 *   1. 类型初始化时，GC 系统备份原始 tp_free。
 *   2. 替换 tp_free 为 GC 安全的包装器（只释放 C 资源，不释放内存）。
 *   3. sweep 阶段：
 *      a. 先调用 tp_finalize（如果存在）
 *      b. 调用 tp_clear 断开引用
 *      c. 调用 tp_dealloc（清理 C 资源）
 *      d. 调用原始 tp_free（释放内存）
 *
 * 此方式避免了包装 tp_dealloc 本身，而是从 tp_free 路径拦截。
 */

/*
 * 类型初始化时的兼容设置：
 */
int
_PyTypes_Ready_GCCompatible(PyTypeObject *type)
{
    if (type->tp_flags & Py_TPFLAGS_COMPATIBLE_GC) {
        /* 备份原始 tp_free，替换为 GC 托管版本 */
        type->tp_gc_free_backup = type->tp_free;
        type->tp_free = _PyGC_ManagedFree;
    }
    else if (type->tp_flags & Py_TPFLAGS_TRACEGC_SAFE) {
        /* TraceGC-safe：tp_free 直接设为 GC 释放路径 */
        type->tp_free = _PyGC_TraceGCFree;
    }
    /* Legacy 扩展：tp_free 不变 */
    return 0;
}
```

在 sweep 阶段：

```c
static void
_PyGC_SweepObject(PyObject *op)
{
    PyTypeObject *type = Py_TYPE(op);

    /* 1. 调用 tp_finalize（如果适用） */
    if (type->tp_finalize && !_PyGC_IsFinalized(op)) {
        _PyGC_SetFinalized(op);
        type->tp_finalize(op);
        if (_PyGC_GetColor(op) != _PyGC_COLOR_WHITE) {
            return;  /* 复活 */
        }
    }

    /* 2. 调用 tp_clear 断开引用 */
    if (type->tp_clear) {
        type->tp_clear(op);
    }

    /* 3. 调用 tp_dealloc 作为 finalizer（清理 C 资源） */
    destructor dealloc = type->tp_dealloc;
    if (dealloc) {
        dealloc(op);
    }

    /* 4. 调用 tp_free 释放内存 */
    /* Compatible 模式：type->tp_free == _PyGC_ManagedFree，
     * 实际调用备份的原始 tp_free 释放内存 */
    /* TraceGC-safe 模式：type->tp_free == _PyGC_TraceGCFree */
    /* Legacy 模式：type->tp_free 不变，dealloc 内部已调用过 tp_free，
     * 此处不再重复调用（通过 tp_free_delegated 标志区分） */
    if (type->tp_flags & Py_TPFLAGS_TRACEGC_SAFE
        || type->tp_flags & Py_TPFLAGS_COMPATIBLE_GC) {
        if (type->tp_free) {
            type->tp_free(op);
        }
    }

    /* 5. 标记为空闲 */
    op->ob_gc_state = _PyGC_COLOR_FREE;
}
```
```

### 11.3 PyObject_GC_New / GC_Del 兼容

```c
/* ============= Include/objimpl.h (Py_TRACING_GC 分支) ============= */

#if defined(Py_TRACING_GC)

/*
 * 追踪式 GC 模式下，PyObject_GC_New 简化为普通分配 + 自动追踪。
 * PyObject_GC_UnTrack 变为空操作。
 */

#define PyObject_GC_New(type, typeobj) \
    ((type*)_PyObject_GC_New(typeobj))

#define PyObject_GC_NewVar(type, typeobj, n) \
    ((type*)_PyObject_GC_NewVar((typeobj), (n)))

/*
 * PyObject_GC_Del 在追踪式 GC 模式下不再由引用计数触发。
 * 它保留为公共 API，但在内部被绕过（GC 直接调用 tp_free）。
 * 如果 C 扩展显式调用 PyObject_GC_Del，行为变为"请求提前释放"。
 */
void
PyObject_GC_Del(void *op)
{
    /* 在追踪式 GC 模式下，显式请求释放是合法的但不鼓励。
     * GC 会尊重此请求：将对象标记为白色，等待下一次清除。
     */
    _PyGC_SetColor((PyObject*)op, _PyGC_COLOR_WHITE);
}

/*
 * PyObject_GC_Track / UnTrack 变为空操作
 */
#define PyObject_GC_Track(op)       ((void)0)
#define PyObject_GC_UnTrack(op)     ((void)0)

/*
 * PyObject_IS_GC 行为不变
 */
#define PyType_IS_GC(t)    PyType_HasFeature((t), Py_TPFLAGS_HAVE_GC)
#define PyObject_IS_GC(op) _PyObject_IS_GC(op)

#else
/* 传统模式定义不变 */
#endif
```

### 11.4 tp_traverse / tp_clear 兼容

```c
/*
 * tp_traverse 在追踪式 GC 下功能不变——用于标记传播时的子对象遍历。
 * 实际上，tp_traverse 的正确性在追踪式 GC 下更加重要：
 *   引用计数模式：不完整的 tp_traverse 会导致循环泄漏（内存泄漏）
 *   追踪式 GC 模式：不完整的 tp_traverse 会导致悬挂指针（段错误！）
 *
 * tp_clear 在追踪式 GC 下由清除阶段调用，用于断开循环引用。
 * 如果类型没有 tp_clear，在清除阶段 GC 会直接回收对象。
 *
 * 这两个槽位的签名和约束在两种模式下完全相同。
 */

/* Py_VISIT 宏不变 */
#define Py_VISIT(op)                      \
    do {                                  \
        if (op) {                         \
            int vret = visit(             \
                _PyObject_CAST(op), arg); \
            if (vret) return vret;        \
        }                                 \
    } while (0)
```

### 11.5 兼容性总结

| C API | Legacy 扩展 | Compatible 扩展 | TraceGC-safe 扩展 |
|-------|------------|----------------|-------------------|
| `Py_INCREF(op)` | 空操作 | 空操作 | 空操作 |
| `Py_DECREF(op)` | 空操作 | 空操作 | 空操作 |
| `Py_XINCREF/XDECREF` | 空操作 | 空操作 | 空操作 |
| `Py_NewRef(op)` | 返回 op | 返回 op | 返回 op |
| `Py_CLEAR(op)` | 只设 NULL | 只设 NULL | 只设 NULL |
| `Py_REFCNT(op)` | 返回 1 | 返回 1 | 返回 1 |
| `Py_SET_REFCNT(op, v)` | 请求存活/死亡 | 请求存活/死亡 | 设置存活/死亡 |
| `PyObject_GC_New` | 自动 GC 追踪 | 自动 GC 追踪 | 自动 GC 追踪 |
| `PyObject_GC_Del` | 请求提前释放 | 请求提前释放 | 请求提前释放 |
| `PyObject_GC_Track` | 空操作 | 空操作 | 空操作 |
| `PyObject_GC_UnTrack` | 空操作 | 空操作 | 空操作 |
| `tp_dealloc` | 完整原语义 | finalizer（C 资源清理） | finalizer（可选） |
| `tp_traverse` | 标记遍历（功能增强） | 必须正确实现 | 必须正确实现 |
| `tp_clear` | 清除阶段调用 | 清除阶段调用 | 清除阶段调用 |
| `tp_free` | dealloc 内自动调用 | GC sweep 阶段统一调用 | GC sweep 阶段统一调用 |
| `tp_finalize` | 不变 | 不变 | 不变 |
| 内存释放者 | tp_dealloc 内 dealloc→tp_free | GC sweep 阶段调用 tp_free | GC sweep 阶段 |
| 对象移动 | 否（pinned） | 否（pinned） | 是 |
| 写屏障 | 无（全量 old 遍历） | 可选的精确屏障 | 精确屏障 |
| GC 根枚举 | 保守 C 栈扫描 | 精确 tp_traverse + 保守栈 | 精确根 + OopMap |
| `ob_refcnt` 直接访问 | 4 处核心代码改为 `Py_REFCNT` | 同上 | 同上 |
| `_Py_Dealloc` | 不再由引用计数触发 | 不再由引用计数触发 | 不再由引用计数触发 |

---

## 12. 分配器适配

### 12.1 分配策略

```
对象分配路径（Py_TRACING_GC 模式）：

                      ┌──────────────────┐
                      │  对象分配请求      │
                      │  PyObject_GC_New  │
                      └────────┬─────────┘
                               │
                      ┌────────▼─────────┐
                      │  对象大小 ≤ 512B?  │
                      └──┬──────────┬────┘
                         │          │
                      YES│          │NO
                         │          │
              ┌──────────▼──┐    ┌──▼──────────┐
              │ 对象堆分配   │    │ 系统 malloc  │
              │ bump-pointer│    │  (大对象)    │
              │  (Eden)     │    │             │
              │ succeed?    │    │ 分配到      │
              │ YES→返回    │    │ Large Space  │
              │ NO→minor GC │    │             │
              └─────────────┘    └─────────────┘
```

### 12.2 对象堆与 Raw 内存的分离

```
╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  PyObject_Malloc 不只分配 PyObject，也分配内部 C 结构：         ║
║      dict keys 表、list items 数组、unicode 数据、parser 结构。     ║
║      这些非对象内存不能参与 GC 标记（不是 PyObject 头）。             ║
║                                                                      ║
║  如果 pymalloc 整体从 Eden 获取内存，非对象分配会污染 GC 堆，         ║
║  导致 GC 扫描时将任意字节序列当作 PyObject 头处理，造成崩溃。        ║
╚══════════════════════════════════════════════════════════════════════╝

因此必须分离三条分配路径：

  1. 对象堆 (Object Heap)：
     · 仅存放 PyObject / PyVarObject 实例。
     · 通过 _PyObject_GC_New / _PyObject_GC_NewVar 分配。
     · Phase 0: 从独立对象堆区域 bump-pointer 分配（连续地址空间）。
     · 可被 GC 范围检查识别，参与三色标记。

  2. Raw 堆 (Raw Heap)：
     · 存放非对象内部缓冲区：dict keys、list items、unicode data。
     · 通过 PyMem_Malloc / PyMem_RawMalloc 分配（现有不变）。
     · 不参与 GC 标记，不由 GC 管理生命周期。

  3. 大对象堆 (Large Object Heap)：
     · 存放 > 256KB 的 PyObject 实例。
     · 直接系统 malloc，side metadata 单独记录。
     · 参与 GC 标记但不在分代堆内。

  Phase 0 实现方案：
    · 对象堆从 _PyGC_InitHeap 分配的 GC 堆区域（Eden + Survivor + Old）分配。
    · pymalloc 继续处理 Raw 堆的小块分配（arena 来自传统 mmap/malloc）。
    · 大对象直接系统 malloc。
    · 对象堆的地址范围用于 GC 快速判断：addr in [heap_start, heap_end)。

  PyGIL_DISABLED 构建提供了可参考的分离模式：
    mimalloc 每个线程有三个堆：_Py_MIMALLOC_HEAP_GC（GC 对象）、
    _Py_MIMALLOC_HEAP_GC_PRE（带预头的 GC 对象）、_Py_MIMALLOC_HEAP_OBJECT（非 GC 对象）。
    Py_TRACING_GC 可参考此模式，用 pymalloc 池级别做区分。
```

---

## 13. 条件编译策略

### 13.1 文件结构

```
每个修改的文件使用以下模式：

  /* ============ 文件顶部：公共部分 ============ */
  #include <Python.h>

  /* ============ Py_TRACING_GC 分支 ============ */
  #if defined(Py_TRACING_GC)

  ... 追踪式 GC 的新实现 ...

  /* ============ 传统引用计数分支 ============ */
  #else

  ... 原有代码（完全不变） ...

  #endif
```

### 13.2 条件编译清单

以下文件需要增加 `Py_TRACING_GC` 条件分支：

| 文件 | 修改类型 | 说明 |
|------|---------|------|
| `Include/object.h` | 条件分支 | PyObject 结构体定义 |
| `Include/refcount.h` | 条件分支 | Py_INCREF/DECREF 等宏 |
| `Include/objimpl.h` | 条件分支 | PyObject_GC_New 等 |
| `Include/cpython/object.h` | 条件分支 | PyTypeObject（不变但确认） |
| `Include/internal/pycore_gc.h` | 条件分支 | 新 GC 接口 + 旧接口兼容 |
| `Include/internal/pycore_interp_structs.h` | 条件分支 | _gc_runtime_state |
| `Include/internal/pycore_object.h` | 条件分支 | _PyType_PreHeaderSize 等 |
| `Python/gc.c` | 条件分支 | GC 核心实现（新增路径） |
| `Python/object.c` | 条件分支 | _Py_NewReference 等 |
| `Objects/obmalloc.c` | 条件分支 | 分配器从 Eden 获取 |
| `Modules/gcmodule.c` | 条件分支 | gc 模块适配 |
| `Python/ceval.c` | 插入安全点 | 字节码循环中加 safepoint check |

### 13.3 新增文件清单

| 文件 | 说明 |
|------|------|
| `Python/gc_heap.c` | 分代堆布局管理 |
| `Python/gc_roots.c` | 根枚举系统 |
| `Python/gc_mark.c` | 三色标记传播 |
| `Python/gc_sweep.c` | 清除和整理 |
| `Python/gc_cardtable.c` | Card Table 管理 |
| `Include/internal/pycore_cardtable.h` | Card Table 头文件 |
| `Include/internal/pycore_safepoint.h` | 安全点机制 |
| `InternalDocs/cpython-next/` | 工程设计文档目录 |

---

## 14. 与现有自由线程 GC 的关系

### 14.1 设计差异

| 特性 | 自由线程 GC (Py_GIL_DISABLED) | 追踪式 GC (Py_TRACING_GC) |
|------|------|------|
| 存在的基础 | PEP 703 无 GIL Python | PEP 未定，本设计 |
| 引用计数 | 偏向引用计数 (BRC) | 完全消除 |
| GC 方法 | 非分代堆扫描 | 分代复制 + 标记-整理 |
| 世代 | 无（每次全量扫描） | 年轻代 + 老年代 |
| 对象头 | ob_tid + ob_ref_local + ob_ref_shared | ob_gc_state + ob_gc_age |
| GC 头 (PyGC_Head) | 不使用 | 保留占位 |
| 并发 | stop-the-world | stop-the-world（后续可增量） |
| 写屏障 | 无（BRC 代替） | Card Table 写屏障 |

### 14.2 互斥关系

```
#define Py_GIL_DISABLED 和 Py_TRACING_GC 是互斥的。

理由：
  1. 自由线程构建依赖偏向引用计数 (BRC) 处理多线程竞争
  2. 追踪式 GC 消除引用计数，但需要 stop-the-world 暂停
  3. 两者有不同的对象头布局（不能兼容）
  4. 两套 GC 实现在 gc_free_threading.c 和 gc*.c 中独立

编译时检查：
  #if defined(Py_GIL_DISABLED) && defined(Py_TRACING_GC)
  #error "Py_GIL_DISABLED and Py_TRACING_GC are mutually exclusive"
  #endif
```

### 14.3 未来融合方向

长期目标是使追踪式 GC 支持无 GIL 构建。这需要在追踪式 GC 基础上：
1. 写屏障的并发安全（使用原子操作或锁）
2. 并发标记（GC 线程与应用线程并行）
3. 安全点的精确线程同步

但这些改造属于未来工作，不在本文档范围内。

---

## 15. JIT 子系统概述

### 15.1 当前状态与目标

```
╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  范围说明：Tier 2 SSA 优化编译器（Sea-of-Nodes、GVN、          ║
║  逃逸分析、方法内联）是独立的高复杂度工程。                           ║
║                                                                      ║
║  GC 改造主线（Phase 0-1）的 P0 目标只包含：                          ║
║    · Tier 1 JIT 的 OopMap / 安全点集成（§19）                       ║
║    · JIT 生成代码中的 GC 根枚举（通过 PerCodeMap 或 side table）    ║
║                                                                      ║
║  Tier 2 SSA 优化编译器列为 Phase 2+ 目标（§1.1 优先级 P2）。        ║
║  当前 §15-20 保留设计以备后续参考，但不可作为 Phase 0-1 的实现依据。 ║
╚══════════════════════════════════════════════════════════════════════╝
```

```c
/* CPython 当前 JIT（3.13+）：copy-and-patch JIT           */
/* ┌────────────────────────────────────────────────────┐ */
/* │  Tier 1 (只有一层)                                  │ */
/* │  C 模板 → LLVM 编译 → 机器码拼接                     │ */
/* │  优化器: 常量传播 + 类型特化 + 死代码消除             │ */
/* │  无 SSA IR、无寄存器分配、无内联                      │ */
/* │  无 deopt、无 OSR、无 GC 安全点集成                  │ */
/* └────────────────────────────────────────────────────┘ */
/*                                                        */
/* JVM HotSpot C2 对比差距（15 项主要缺失）：                 */
/* 1. 方法内联           9. 全局值编号 (GVN)               */
/* 2. 逃逸分析           10. 循环向量化                     */
/* 3. 标量替换           11. 代码缓存层级管理               */
/* 4. 寄存器分配         12. 自适应重新编译                  */
/* 5. 去优化 (deopt)     13. OSR                           */
/* 6. 分层编译           14. 写屏障优化                     */
/* 7. 类型剖面驱动优化    15. GC 安全点 / OopMap            */
/* 8. 锁消除                                                 */
/*                                                        */
/* 目标状态：三层编译 + SSA IR + 去优化 + GC 集成          */
```

### 15.2 新 JIT 设计原则

```
原则 1: 渐进式 — 在现有 copy-and-patch JIT 基础上增量构建
原则 2: 分层 — 冷热分离，热度越高优化越多
原则 3: 推测性优化 — 基于 type profile 做大胆假设，错了去优化
原则 4: GC 感知 — 生成的代码必须知晓 GC 安全点和对象引用
原则 5: 向后兼容 — JIT 不影响 C API 和解释器行为
```

---

## 16. 分层编译架构

### 16.1 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│                         Tier 0                              │
│                    线速解释器 (字节码)                        │
│  · 当前 _PyEval_EvalFrameDefault 主循环                      │
│  · 3.11+ 自适应字节码（自适应特化）                           │
│  · 热点检测：方法调用计数 > 1000 时升级到 Tier 1              │
│  · OSR 入口：循环回边计数 > 5000 时升级到 Tier 1 (OSR)       │
└────────────────────────┬────────────────────────────────────┘
                         │  [热点检测]
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                         Tier 1                              │
│                 Copy-and-Patch JIT (冷编译)                  │
│  · 当前实现增强：保留完整                                    │
│  · 优化：常量折叠、类型守卫消除、复制传播                       │
│  · 快速编译：< 1ms 编译时间                                  │
│  · 带轻量 type profile（Top-1 类型信息）                      │
│  · 热度监测：调用计数 + 回边计数 > 10000 时升级到 Tier 2      │
└────────────────────────┬────────────────────────────────────┘
                         │  [深度热点检测]
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                         Tier 2                              │
│                 SSA 优化编译器 (热编译)                       │
│  · SSA 形式中间表示（基于 Sea-of-Nodes 设计）                │
│  · 完整优化：内联、逃逸分析、标量替换、GVN                     │
│  · 寄存器分配：线性扫描 (Linear Scan)                        │
│  · 推测性优化 + 去优化 (Deoptimization)                       │
│  · GC 集成：OopMap、内联分配、写屏障优化                      │
│  · 慢速编译：10-100ms，可后台线程执行                         │
└────────────────────────┬────────────────────────────────────┘
                         │  [去优化失效时回退]
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                     Deoptimization                           │
│  当推测失败时，从 Tier 2 代码回退到 Tier 0/1 的恢复点         │
└─────────────────────────────────────────────────────────────┘
```

### 16.2 热度阈值

```
Tier 0 → Tier 1:
  方法调用计数 ≥ 1,000
  或循环回边计数 ≥ 5,000 (OSR)

Tier 1 → Tier 2:
  方法调用计数 ≥ 10,000
  且 type profile 稳定性 > 90%（同一类型占比）
  编译队列空闲（或后台线程编译）

Tier 2 → Tier 0 (去优化):
  类型 guard 失败
  类层次修改 (class mutation)
  方法覆盖 (method overriding)
  代码版本失效
```

### 16.3 编译调度

```c
/* ============= Python/jit_scheduler.c (新增) ============= */

/*
 * 编译调度器：管理方法从 Tier 0 → 1 → 2 的升级。
 *
 * 三个队列：
 *   1. tier1_queue: 等待 Tier 1 编译的方法（优先级由热度决定）
 *   2. tier2_queue: 等待 Tier 2 编译的方法（需要 type profile 稳定）
 *   3. deopt_list: 已去优化的方法（禁止重复编译）
 */

typedef enum {
    COMPILE_TIER1,
    COMPILE_TIER2,
    COMPILE_OSR,
} CompileKind;

struct _PyJIT_CompileRequest {
    PyCodeObject *code;               /* 代码对象 */
    CompileKind kind;                 /* 编译类型 */
    Py_ssize_t invocation_count;      /* 调用计数 */
    uint32_t type_profile_hash;       /* 类型剖面哈希（Tier 2 用） */
    int osr_bytecode_offset;          /* OSR 入口字节码偏移 */
    struct _PyJIT_CompileRequest *next;
};

/*
 * 安全点检查时调用的热度更新。
 * 在每个安全点，检查当前方法的调用/回边计数器。
 */
static inline void
_PyJIT_UpdateHotness(PyThreadState *tstate, _PyInterpreterFrame *frame)
{
    PyCodeObject *code = frame->f_code;
    code->co_invocation_counter++;

    if (code->co_invocation_counter >= 1000
        && code->co_tier1_entry == NULL) {
        _PyJIT_EnqueueCompile(code, COMPILE_TIER1);
    }
    else if (code->co_invocation_counter >= 10000
             && code->co_tier2_entry == NULL
             && _PyJIT_TypeProfileStable(code)) {
        _PyJIT_EnqueueCompile(code, COMPILE_TIER2);
    }
}
```

---

## 17. SSA 中间表示

### 17.1 IR 设计

```
Python 字节码 → HIR (SSA) → LIR (指令选择) → 机器码
```

**HIR (High-level IR)**:
```
名称: PyIR
形式: SSA (Static Single Assignment)
表示: Sea-of-Nodes 风格（节点 = 操作，边 = 数据依赖）
类型: Python 类型 + 机器类型（int64, float64, PyObject*）
元数据: 字节码位置 (bci)、类型剖面、GC 信息
```

**HIR 节点类型**:

```
IR Node Categories:
  ┌─ 常量节点: Constant(int), Constant(float), Constant(str),
  │             Constant(None), Constant(type)
  │
  ├─ 值节点:   LoadLocal, StoreLocal, LoadField, StoreField,
  │             LoadArray, StoreArray, TypeOf, InstanceOf
  │
  ├─ 运算节点: Add, Sub, Mul, Div, Mod, And, Or, Xor, Shl, Shr,
  │             Neg, Not, Inv, Cmp (Eq/Ne/Lt/Le/Gt/Ge)
  │
  ├─ 控制节点: Start, Return, Branch (if/else), Loop,
  │             Call, CallVirtual, Guard, Deopt
  │
  ├─ 内存节点: Alloc, Load, Store, StoreBarrier (写屏障)
  │
  └─ GC 节点:  Safepoint, OopMap, WriteBarrier
```

### 17.2 HIR 示例

```
Python:  def add(a, b): return a + b
         add(1, 2)

HIR (Sea-of-Nodes):
  ┌──────────────────────────────────────────┐
  │  Start                                   │
  │   │                                      │
  │   ├─── Constant(int, 1)                  │
  │   │       │                              │
  │   ├─── Constant(int, 2)                  │
  │   │       │                              │
  │   └─── GuardType(int, %1)               │
  │           │                              │
  │   └─── GuardType(int, %2)               │
  │           │                              │
  │   └─── Add(int, %1, %2)                 │
  │           │                              │
  │   └─── Return(%3)                       │
  └──────────────────────────────────────────┘
```

### 17.3 Uop → HIR 映射

```c
/* ============= Python/ir_builder.c (新增) ============= */

/*
 * 从现有的 uop 序列构建 HIR。
 *
 * 现有 uop 序列（来自优化器）已经包含：
 *   - 类型守卫 (_GUARD_TYPE)
 *   - 常量信息 (operand0/1 中的缓存值)
 *   - 控制流 (jump_target/error_target)
 *
 * 映射规则：
 *   每个 uop → 一个或多个 HIR 节点
 *   类型守卫 → GuardType 节点 + 类型信息
 *   常量加载 → Constant 节点
 *   运算操作 → 对应运算节点
 */

static int
_PyIR_BuildFromUops(_PyUOpInstruction *uops, int num_uops,
                    PyIRBuilder *builder)
{
    for (int i = 0; i < num_uops; i++) {
        _PyUOpInstruction *uop = &uops[i];
        switch (uop->opcode) {
        case _LOAD_FAST:
            /* LoadLocal 节点 */
            builder->EmitLoadLocal(builder, uop->oparg);
            break;
        case _STORE_FAST:
            /* StoreLocal 节点 + Store 边 */
            builder->EmitStoreLocal(builder, uop->oparg,
                                    builder->PopValue());
            break;
        case _BINARY_OP_ADD_INT:
            /* Add 节点 + GuardType（已在 uop 中验证） */
            PyIRValue *rhs = builder->PopValue();
            PyIRValue *lhs = builder->PopValue();
            builder->EmitAdd(builder, lhs, rhs, PyIRType_Int64);
            break;
        case _GUARD_TYPE:
            /* GuardType 节点 */
            PyIRValue *val = builder->PeekValue(0);
            PyTypeObject *type = (PyTypeObject*)uop->operand0;
            builder->EmitGuardType(builder, val, type);
            break;
        /* ... 其他 uop 映射 */
        }
    }
    return 0;
}
```

### 17.4 SSA 优化 Pass

```c
/*
 * 在 HIR 上运行的优化 pass（按顺序）：
 *
 * Pass 1: 常量折叠 + 传播
 *   · 编译时计算常量表达式
 *   · 对 Python 内置类型 (int/float/str) 做常量折叠
 *
 * Pass 2: 类型特化 + 守卫简化
 *   · 如果类型已由前置守卫保证，消除冗余 GuardType
 *   · 基于 type profile 插入内联缓存
 *
 * Pass 3: 全局值编号 (GVN)
 *   · 对相同值的计算去重
 *   · 消除冗余 LoadField/LoadArray
 *
 * Pass 4: 逃逸分析
 *   · 识别不逃逸出当前方法的对象
 *   · 对不逃逸对象做标量替换（字段展开为局部变量）
 *
 * Pass 5: 方法内联
 *   · 对热点调用点内联（基于 type profile 和 size 启发式）
 *   · 对内置函数调用内联（len(), range(), type() 等）
 *
 * Pass 6: 循环优化
 *   · 循环不变代码外提 (LICM)
 *   · 循环展开（hot loops）
 *   · 循环剥离 (loop peeling)
 *
 * Pass 7: 死代码消除 (DCE)
 *   · 移除没有副作用的未使用节点
 *
 * Pass 8: 代码生成预备
 *   · HIR → LIR 降级
 *   · 插入 GC 安全点
 *   · 分配 OopMap 槽位
 */

/* 优化器接口 */
typedef struct _PyJIT_Optimizer {
    /* IR 图 */
    PyIRGraph *graph;

    /* 分析结果 */
    bool *is_escaped;            /* 逃逸分析结果 */
    uint32_t *value_numbers;     /* GVN 值编号 */
    bool *is_loop_invariant;     /* LICM 分析 */

    /* 编译上下文 */
    PyCodeObject *code;
    _PyJIT_TypeProfile *profile;
} _PyJIT_Optimizer;
```

---

## 18. JIT 编译流水线

### 18.1 Tier 1 编译流水线

```
Tier 1 编译（基于现有 copy-and-patch 增强）：

  [1] 字节码 → uop (现有)
       _PyOptimizer_Optimize → 追踪生成 uop 序列
       ↓
  [2] uop 优化 (现有增强)
       _Py_uop_analyze_and_optimize (现有)
       + 增强常量传播
       + 增强死代码消除
       ↓
  [3] 栈槽分配 (现有)
       stack_allocate → 插入 _SPILL_OR_RELOAD
       ↓
  [4] Copy-and-Patch JIT 编译 (现有)
       _PyJIT_Compile → 从预编译模板生成机器码
       ↓
  [5] 发布
       executor->jit_code = 编译结果
       jit_publish → 注册到 perf/GDB

  编译时间: < 1ms
  代码质量: 中（无寄存器分配，无 SSA 优化）
```

### 18.2 Tier 2 编译流水线

```
Tier 2 编译（新增 SSA 优化编译器）：

  [1] uop → HIR 构建 (新增)
       _PyIR_BuildFromUops → 生成 SSA 图
       ↓
  [2] SSA 优化 (新增)
       _PyJIT_RunOptimizer → 运行所有 pass
       · 常量折叠 + GVN
       · 逃逸分析 + 标量替换
       · 方法内联
       · LICM + 循环展开
       ↓
  [3] 寄存器分配 (新增)
       LinearScanRegisterAllocator
       · 基于区间 (live interval) 的线性扫描
       · 溢出到栈槽位
       ↓
  [4] LIR 生成 (新增)
       HIR → LIR 降级
       · 指令选择 (基于目标架构的模式匹配)
       · x86-64 / AArch64 后端
       ↓
  [5] 机器码发射 (新增)
       _PyJIT_EmitCode → 直接生成机器码
       · 不需要预编译模板
       · 内联 GC 安全点检查
       · 插入 OopMap 记录
       · 写屏障融合优化
       ↓
  [6] 去优化元数据生成 (新增)
       为每个可能失败的安全点生成 DeoptRecord
       ↓
  [7] 发布
       executor->jit_tier2_code = 编译结果
       同时保留 Tier 1 版本（失败回退）

  编译时间: 10-100ms（后台线程）
  代码质量: 高（寄存器分配 + SSA 优化 + 内联）
```

### 18.3 Tier 2 入口和切换

```c
/* ============= Python/jit_tier2.c (新增) ============= */

/*
 * Tier 2 JIT 函数签名（与 Tier 1 兼容）：
 *
 *   _Py_CODEUNIT* (*jit_func)(
 *       _PyExecutorObject *executor,
 *       _PyInterpreterFrame *frame,
 *       _PyStackRef *stack_pointer,
 *       PyThreadState *tstate,
 *       _PyStackRef tos_cache0,
 *       _PyStackRef tos_cache1,
 *       _PyStackRef tos_cache2
 *   );
 */

/*
 * Tier 2 入口包装器。
 * 从 Tier 2 编译的代码进入（或从 Tier 1 升级检测）。
 */
_Py_CODEUNIT*
_PyJIT_Tier2Entry(_PyExecutorObject *executor,
                  _PyInterpreterFrame *frame,
                  _PyStackRef *stack_pointer,
                  PyThreadState *tstate,
                  _PyStackRef tos0, _PyStackRef tos1, _PyStackRef tos2)
{
    /* 检查是否需要去优化（代码版本过期等） */
    if (executor->tier2_invalid) {
        /* 回退到 Tier 1 */
        return _PyJIT_Tier1Entry(executor, frame, stack_pointer,
                                 tstate, tos0, tos1, tos2);
    }

    /* 获取 Tier 2 编译的代码入口 */
    jit_func entry = (jit_func)executor->jit_tier2_code;
    return entry(executor, frame, stack_pointer, tstate,
                 tos0, tos1, tos2);
}
```

### 18.4 去优化框架

```c
/* ============= Include/internal/pycore_deopt.h (新增) ============= */

/*
 * 去优化 (Deoptimization) 框架。
 *
 * 当推测性优化失败时（如类型 guard 失败、内联假设被违反），
 * 必须将执行从优化的 Tier 2 代码"回退"到 Tier 1 或 Tier 0。
 *
 * 去优化记录 (DeoptRecord):
 *   每个可能失败的推测点都有对应的 DeoptRecord，
 *   记录了如何在失败时重构解释器状态。
 */

typedef struct {
    /* 字节码索引（恢复到解释器时从此处继续） */
    int bci;

    /* 局部变量数 */
    int num_locals;

    /* 操作栈深度 */
    int num_stack;

    /* 帧状态快照大小 */
    int snapshot_size;

    /* OopMap：对象引用在寄存器/栈中的位置 */
    struct {
        int num_registers;         /* 包含引用的寄存器数 */
        int register_ids[16];      /* 寄存器编号 */
        int num_stack_slots;       /* 包含引用的栈槽位数 */
        int stack_slots[64];       /* 栈槽位索引 */
    } oop_map;

    /* 注册值与解释器槽位的对应关系 */
    struct {
        int reg;                   /* 寄存器编号 (-1 = 栈槽位) */
        int stack_slot;            /* 解释器栈索引 */
        PyTypeObject *type;        /* 编译时的期望类型 */
    } value_map[64];
} PyDeoptRecord;

/*
 * 去优化入口：当 guard 失败时调用。
 *
 * 参数：
 *   deopt_record: 预先生成的去优化记录
 *   failed_guard: 失败的 guard 索引
 *   frame: 当前帧
 *   reg_values[n]: 当前的寄存器值（n = deopt_record->register_count）
 *
 * 行为：
 *   1. 使用 deopt_record 中的 value_map 重构解释器状态
 *   2. 将当前帧转换为兼容解释器的帧
 *   3. 返回 Tier 0 解释器的继续地址
 */
_Py_CODEUNIT*
_PyJIT_Deopt(PyDeoptRecord *record, int failed_guard,
             _PyInterpreterFrame *frame, PyObject **reg_values)
{
    /* 根据 value_map 重建局部变量 */
    for (int i = 0; i < record->num_locals; i++) {
        int reg = record->value_map[i].reg;
        int slot = record->value_map[i].stack_slot;
        if (reg >= 0) {
            frame->localsplus[slot] = reg_values[reg];
        }
    }

    /* 重建操作栈 */
    for (int i = 0; i < record->num_stack; i++) {
        int reg = record->value_map[record->num_locals + i].reg;
        if (reg >= 0) {
            frame->stackpointer[i] = reg_values[reg];
        }
    }

    /* 设置继续字节码索引 */
    frame->prev_instr = _PyCode_CODE(frame->f_code) + record->bci;

    /* 标记执行器无效（Tier 2 代码不再使用） */
    frame->f_executor = NULL;

    return (_Py_CODEUNIT*)(uintptr_t)(record->bci | 0);
}
```

### 18.5 OSR（栈上替换）

```c
/* ============= Python/jit_osr.c (新增) ============= */

/*
 * 栈上替换 (On-Stack Replacement):
 *   当方法在解释器中执行到长循环时，将正在执行的帧
 *   从解释器模式切换到 JIT 编译模式。
 *
 * OSR 编译：
 *   1. 回边计数超过阈值时触发
 *   2. 编译器从循环头开始编译（不是方法入口）
 *   3. 生成 OSR 入口：接收当前局部变量和栈状态
 *   4. 编译完成后，当前解释器帧被替换为 JIT 帧
 *
 * OSR 入口签名：
 *   _Py_CODEUNIT* osr_entry(
 *       _PyExecutorObject *executor,
 *       _PyInterpreterFrame *frame,
 *       _PyStackRef *osr_locals,    // 循环活跃的局部变量
 *       int num_osr_locals,
 *       PyThreadState *tstate
 *   );
 */

/*
 * OSR 状态快照：在解释器到达 OSR 点时记录。
 */
typedef struct {
    int osr_bci;                    /* OSR 字节码索引 */
    int num_locals;                 /* 活跃局部变量数 */
    _PyStackRef *locals;           /* 局部变量值快照 */
    int num_stack;                  /* 操作栈深度 */
    _PyStackRef *stack;            /* 操作栈快照 */
} _PyOSR_Snapshot;

/*
 * 触发 OSR 编译并切换执行。
 */
_Py_CODEUNIT*
_PyJIT_TriggerOSR(PyThreadState *tstate, _PyInterpreterFrame *frame,
                  int osr_bci)
{
    PyCodeObject *code = frame->f_code;

    /* 检查是否已有 OSR 编译结果 */
    _PyExecutorObject *osr_exec = code->co_osr_executors[osr_bci];
    if (osr_exec == NULL) {
        /* 请求后台 OSR 编译 */
        _PyJIT_EnqueueOSRCompile(code, osr_bci);
        /* 暂时继续解释执行 */
        return (_Py_CODEUNIT*)(uintptr_t)(osr_bci | 0);
    }

    /* 获取 OSR 入口点 */
    jit_osr_func entry = (jit_osr_func)osr_exec->jit_osr_code;

    /* 构建 OSR 快照 */
    _PyStackRef *locals_start = _PyFrame_GetLocalsArray(frame);
    int num_locals = code->co_nlocalsplus;

    /* 调用 OSR 代码（替换当前执行） */
    return entry(osr_exec, frame, locals_start, num_locals, tstate);
}
```

---

## 19. JIT-GC 集成

### 19.1 OopMap 设计

```
OopMap（对象引用映射）：
  JIT 编译的代码中，对象引用可能存储在寄存器和栈上。
  GC 在安全点需要知道所有对象引用的确切位置，
  以便正确地标记存活对象。

  每个安全点（safepoint）有一个 OopMap：
  ┌──────────────────────────────────────────────┐
  │  OopMap for safepoint at offset 0x1234       │
  │  ┌──────────────────────────────────────────┐ │
  │  │  寄存器 RAX:  指向 PyObject*              │ │
  │  │  寄存器 RDX:  指向 PyObject*              │ │
  │  │  寄存器 RCX:  整数 (不是引用)              │ │
  │  │  栈 [+8]:     指向 PyObject*              │ │
  │  │  栈 [+16]:    GC 死亡引用                  │ │
  │  └──────────────────────────────────────────┘ │
  └──────────────────────────────────────────────┘
```

```c
/* ============= Include/internal/pycore_oopmap.h (新增) ============= */

/*
 * OopMap 编码。
 *
 * 为了减少空间占用，OopMap 使用位图编码：
 *   每个可能的寄存器/栈槽位用一个 bit 表示：
 *     1 = 该位置包含对象引用（需要 GC 跟踪）
 *     0 = 该位置不是对象引用
 *
 * 大小：
 *   寄存器 bit: 32 (x86-64) 或 64 (AArch64) bit = 4-8 字节
 *   栈槽位 bit: 每个槽位 1 bit, 最多 256 槽位 = 32 字节
 *   总计: < 40 字节 / 每个安全点
 */

#define OOPMAP_MAX_REGISTERS  64
#define OOPMAP_MAX_STACK_SLOTS 256

typedef struct {
    uint64_t register_bits;          /* 位图：哪些寄存器包含引用 */
    uint32_t stack_bit_count;        /* 栈位图的有效 bit 数 */
    uint8_t stack_bits[32];         /* 位图：栈槽位是否包含引用 */
} _PyOopMap;

/*
 * 安全点描述符：一个安全点 + 其 OopMap。
 */
typedef struct {
    uint32_t code_offset;            /* 安全点在生成代码中的偏移 */
    _PyOopMap oop_map;              /* 该点的 OopMap */
    int stack_depth;                 /* 该点的栈深度 */
} _PySafepointDescriptor;
```

### 19.2 JIT 代码中的 GC 安全点

```
JIT 生成的代码中的安全点位置：

  1. 每次函数调用前
  2. 每个循环回边
  3. 每个可能触发 GC 的操作前（分配、类型守卫失败）
  4. 显式的 _CHECK_PERIODIC 位置

  安全点检查的机器码（x86-64）：
    cmp byte ptr [rsp + SAFEPOINT_OFFSET], 0
    jne slow_path_safepoint
    ; 快速路径（0 开销安全点 — 使用页保护）

  在 Tier 2 编译器中的优化：
    · 消除冗余安全点（相邻的安全点合并）
    · 安全点提升（从循环内提到循环外，仅在回边检查）
```

### 19.3 GC 安全点处理

```c
/* ============= Python/gc_safepoint.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * JIT 代码中的安全点处理。
 *
 * 当 JIT 代码到达安全点时，如果 GC 请求暂停：
 *   1. 当前线程报告自己在安全点
 *   2. 将 OopMap 注册到 GC 子系统
 *   3. 等待 GC 完成
 *   4. 恢复执行
 *
 * 安全点处理的快速/慢速路径：
 */

/* 快速路径：在 Tier 2 编译器中内联（约 3 条指令） */
static inline void
_Py_JIT_SafepointFast(PyThreadState *tstate, _PyOopMap *oopmap)
{
    /* 单条检查指令 */
    if (_Py_atomic_load_int_relaxed(&tstate->gc_safepoint_requested)) {
        _Py_JIT_SafepointSlow(tstate, oopmap);
    }
}

/* 慢速路径：实际挂起当前线程 */
static void
_Py_JIT_SafepointSlow(PyThreadState *tstate, _PyOopMap *oopmap)
{
    /* 注册 OopMap 到 GC */
    tstate->gc_current_oopmap = oopmap;
    tstate->gc_safepoint_pc = _PyJIT_GetCurrentPC();

    /* 进入安全点等待 */
    _Py_EnterSafepoint(tstate);

    /* GC 完成后恢复 */
    tstate->gc_current_oopmap = NULL;
}
```

### 19.4 GC 根枚举中的 OopMap 利用

```c
/* ============= Python/gc_roots.c (Py_TRACING_GC 分支，补充) ============= */

/*
 * 在根枚举时，如果线程正在执行 JIT 代码，使用 OopMap 定位引用。
 *
 * 在安全点，每个线程的 OopMap 告诉你：
 *   1. 哪些寄存器包含对象引用
 *   2. 哪些栈槽位包含对象引用
 *   3. 当前的安全点代码偏移
 *
 * 这使得 GC 可以在不扫描整个 C 栈的情况下找到所有 JIT 代码中的引用。
 */

static void
_PyGC_VisitJITThreadState(PyThreadState *tstate, _gc_root_visitor *visitor)
{
    _PyOopMap *oopmap = tstate->gc_current_oopmap;
    if (oopmap == NULL) {
        /* 线程不在 JIT 代码中（在解释器或 C 代码中） */
        return;
    }

    /* 遍历 OopMap 中的寄存器引用 */
    for (int reg = 0; reg < OOPMAP_MAX_REGISTERS; reg++) {
        if (oopmap->register_bits & (1ULL << reg)) {
            PyObject *obj = _PyJIT_GetRegisterValue(tstate, reg);
            visitor->visit(obj, visitor->arg);
        }
    }

    /* 遍历 OopMap 中的栈槽位引用 */
    for (int i = 0; i < oopmap->stack_bit_count; i++) {
        if (oopmap->stack_bits[i / 8] & (1 << (i % 8))) {
            PyObject *obj = _PyJIT_GetStackValue(tstate,
                                                 tstate->gc_safepoint_pc,
                                                 i);
            visitor->visit(obj, visitor->arg);
        }
    }
}
```

### 19.5 JIT 内联分配

```c
/* ============= Python/jit_alloc.c (新增，Py_TRACING_GC 分支) ============= */

/*
 * JIT 内联对象分配（类似 JVM 的 TLAB 分配）。
 *
 * 在 Tier 2 编译器中，对不逃逸对象做标量替换；
 * 对必须逃逸的对象，生成内联分配代码（bump-pointer）。
 *
 * 内联分配的快速路径（约 5-8 条指令）：
 *   mov  rcx, [EdenTop]          ; 加载当前 Eden 分配指针
 *   lea  rdx, [rcx + size]       ; 计算新指针
 *   cmp  rdx, [EdenEnd]          ; 检查是否超出边界
 *   jg   slow_path               ; 超出 → 触发 minor GC
 *   mov  [EdenTop], rdx          ; 更新分配指针
 *   ; rcx 现在指向新分配的对象
 *   mov  [rcx + 0], gc_state     ; 初始化 ob_gc_state
 *   mov  [rcx + 8], type         ; 初始化 ob_type
 *
 * 慢速路径：调用 _PyGC_EdenAlloc 触发 minor GC
 */

/*
 * 标量替换（Scalar Replacement）：
 *   不逃逸的对象被拆分为多个独立的局部变量。
 *   Python 示例:
 *     def distance(p):
 *         return p.x * p.x + p.y * p.y
 *     # Point(x, y) 如果未逃逸，x 和 y 成为独立局部变量
 *     # 不再需要分配 Point 对象
 */

/* 逃逸分析结果 */
typedef enum {
    ESCAPED_NONE,         /* 未逃逸 — 可标量替换 */
    ESCAPED_ARGUMENT,     /* 逃逸到参数 */
    ESCAPED_GLOBAL,       /* 逃逸到全局变量 */
    ESCAPED_THREAD,       /* 逃逸到其他线程 */
} EscapeState;

/*
 * 标量替换的实现：
 *   在 HIR 中，Alloc 节点如果逃逸分析为 ESCAPED_NONE，
 *   将其字段的 LoadField/StoreField 替换为直接对局部变量的操作。
 */
static int
_PyJIT_ScalarReplace(PyIRGraph *graph, PyIRNode *alloc_node,
                     EscapeState escape)
{
    if (escape != ESCAPED_NONE) {
        return 0;  /* 不能替换 */
    }

    /* 为每个字段创建新的局部变量 */
    int num_fields = _PyIR_GetFieldCount(alloc_node);
    for (int i = 0; i < num_fields; i++) {
        PyIRNode *field_var = graph->NewLocal();
        /* 将对 LoadField[alloc_node, i] 的所有引用替换为 field_var */
        graph->ReplaceUses(LoadField(alloc_node, i), field_var);
        /* 将对 StoreField[alloc_node, i] 的所有引用替换为对 field_var 的赋值 */
    }

    /* 移除 Alloc 节点 */
    graph->RemoveNode(alloc_node);
    return 1;
}
```

---

## 20. 类型概要分析

### 20.1 设计

```
类型概要分析 (Type Profiling) 是推测性优化的基础。
没有准确的类型信息，JIT 编译器无法做去虚拟化、
无法内联、无法特化运算操作。

现有系统（PEP 659）的自适应字节码已经收集了部分类型信息，
但只记录了最后一次的类型（单值缓存），不够全面。

增强目标：
  · 对每个调用点维护 Top-2 类型的分布直方图
  · 类型稳定性检测：如果同一类型占比 > 90%，认为稳定
  · 在 Tier 1 编译中嵌入类型守卫
  · 为 Tier 2 编译提供类型剖面数据
```

### 20.2 类型剖面数据结构

```c
/* ============= Include/internal/pycore_type_profile.h (新增) ============= */

/*
 * 类型剖面条目。
 * 记录一个调用点的类型分布。
 */

#define _Py_PROFILE_MAX_TYPES  2   /* Top-2 类型 */

typedef struct {
    PyTypeObject *types[_Py_PROFILE_MAX_TYPES];  /* Top 类型 */
    uint32_t counts[_Py_PROFILE_MAX_TYPES];      /* 命中次数 */
    uint32_t total;                               /* 总次数 */
    uint8_t num_types;                            /* 已记录的类型数 */
} _PyTypeProfileEntry;

/*
 * 类型剖面表。
 * 每个代码对象有一个剖面表。
 * 索引对应字节码中的调用/属性访问位置。
 */
typedef struct {
    int num_entries;                 /* 条目数 */
    _PyTypeProfileEntry entries[1];  /* 可变长数组 */
} _PyTypeProfileTable;

/*
 * 类型稳定性检查：返回 true 如果 Top-1 类型占比 > 90%。
 */
static inline int
_PyTypeProfile_IsStable(_PyTypeProfileEntry *entry)
{
    if (entry->total == 0) return 0;
    uint32_t top_count = entry->counts[0];
    return (top_count * 100 / entry->total) > 90;
}
```

### 20.3 类型收集时机

```
类型收集插入点：
  · LOAD_ATTR: 记录属性所属的对象类型
  · CALL_FUNCTION: 记录调用接收者的类型
  · BINARY_OP: 记录操作数类型
  · LOAD_GLOBAL: 记录全局变量的类型
  · FOR_ITER: 记录迭代器类型

收集策略：
  · Tier 0 (解释器): 每 N 次执行采样一次（降低开销）
  · Tier 1 (JIT): 插入采样守卫，记录类型
  · Tier 2 (优化 JIT): 不再收集（假设类型稳定）
```

### 20.4 类型剖面驱动优化

```c
/*
 * 基于类型剖面的优化决策：
 *
 * 1. 去虚拟化 (Devirtualization):
 *    如果调用点的 type profile 显示 > 90% 为同一类型，
 *    Tier 2 编译器生成:
 *      guard type == expected_type
 *      call expected_type.method     // 直接调用
 *      deopt if guard fails
 *
 * 2. 内联 (Inlining):
 *    如果去虚拟化成功且被调方法小（< 25 字节码）：
 *      内联被调方法体
 *      guard type + guard code_version
 *      deopt if guard fails
 *
 * 3. 类型特化 (Type Specialization):
 *    如果操作数的类型稳定：
 *      a + b (int + int) → 直接生成整数加法指令
 *      a + b (str + str) → 直接调用字符串拼接
 */

/*
 * 类型剖面在 Tier 2 编译中的使用。
 */
static int
_PyJIT_ApplyProfile(PyIRBuilder *builder,
                    _PyTypeProfileTable *profile,
                    int bytecode_index)
{
    _PyTypeProfileEntry *entry = &profile->entries[bytecode_index];

    if (!_PyTypeProfile_IsStable(entry)) {
        return 0;  /* 类型不稳定，保守处理 */
    }

    PyTypeObject *expected = entry->types[0];

    /* 如果期望类型是内置数值类型 */
    if (expected == &PyLong_Type) {
        builder->SetExpectedType(PyIRType_Int64);
        /* 后续的加法等操作会直接生成整数指令 */
    }
    else if (expected == &PyFloat_Type) {
        builder->SetExpectedType(PyIRType_Float64);
    }
    /* ... 其他类型特化 */

    /* 插入类型守卫 */
    builder->EmitGuardType(builder->PeekValue(0), expected);

    return 1;
}
```

---

## 21. 附录：关键结构体偏移量速查表

### 21.1 PyObject（64 位，Py_TRACING_GC）

```
偏移 大小  字段                   说明
──────────────────────────────────────────
0    4     ob_gc_state          GC 状态字 (颜色/年龄/标志)
4    2     ob_gc_age            分代年龄
6    2     ob_flags             对象标志 (immortal/static)
8    8     ob_type              类型指针
──────────────────────────────────────────
总计 16 字节
```

### 21.2 PyVarObject（64 位，Py_TRACING_GC）

```
偏移 大小  字段
──────────────────────────────────────────
0-15  如上 (PyObject)
16   8     ob_size             可变长度对象的大小
──────────────────────────────────────────
总计 24 字节
```

### 21.3 GC 对象预头（64 位，Py_TRACING_GC，与引用计数模式相同）

```
场景              预头大小  偏移范围
──────────────────────────────────
非 GC 对象         0        —
GC 对象            16       [-16, -1]
GC + PREHEADER    32       [-32, -1]
```

### 21.4 PyTypeObject 关键字段（64 位，不变）

```
偏移  字段
──────────────────
48    tp_dealloc
184   tp_traverse
192   tp_clear
320   tp_free
392   tp_finalize
──────────────────
sizeof(PyTypeObject) = 424 字节
```

---

> **本文档结束。**
> 任何 Agent 如果发现 ARCHITECTURE.md 与源代码不一致，
> 必须记录 ISSUE 并暂停，等待人类决策。
