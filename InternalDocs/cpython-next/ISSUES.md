# ISSUES.md — 追踪式 GC 改造工程已知问题

> 由架构审计（2026-06-26，10 项）发现的全部设计问题已在 ARCHITECTURE.md 中针对性修正。
> 本章只记录**修正后仍遗留的、架构层面无法覆盖的具体实现盲区**。

---

## 开放问题

### I-001: `_PyGC_GetObjectSize` 在现有源码中不直接存在

| 属性 | 值 |
|------|----|
| **发现阶段** | 架构编写（2026-06-26） |
| **严重度** | 阻塞 |
| **状态** | ⬜ 待解决 |
| **架构参考** | ARCHITECTURE.md §5.4（扫表循环）、§6.3（sweep 循环）多处依赖此函数 |
| **描述** | 架构伪代码中多处使用 `_PyGC_GetObjectSize(op)` 扫表遍历堆，但当前 CPython 没有通用函数可以从任意 PyObject* 获取其分配时的大小。`tp_basicsize + ob_size * tp_itemsize` 仅对已知类型有效，且不反映 preheader/对齐填充。需要在分配器侧（Phase 0.4/0.5）同步设计 size 元数据的存储方案。 |
| **候选方案** | (A) 对象堆使用 segregated-fit（按 size class 分块），size class 隐含大小；(B) side table（parallel array）；(C) 嵌入 object-start bitmap 的 size 区域。 |

---

## 已解决问题

### I-002: Phase 0.0 桩函数签名与 ARCHITECTURE.md 不匹配

| 属性 | 值 |
|------|----|
| **发现阶段** | STEP 4 审查 (Task-0.0) |
| **严重度** | 阻塞 |
| **状态** | ✅ RESOLVED (2026-06-27) |
| **架构参考** | ARCHITECTURE.md §5.4 `_PyGC_InitHeap`、§5.5 `_PyGC_EdenAlloc`、§8.2 `_PyGC_MarkRoots` |
| **描述** | 三个桩函数签名与 ARCHITECTURE.md 不一致：(1) `_PyGC_InitHeap(void)` 缺少 `(gcstate, young_size, old_size)` 参数；(2) `_PyGC_EdenAlloc(type, size)` 返回 `PyObject*` 应为 `(size_t)` 返回 `char*`；(3) `_PyGC_MarkRoots(tstate)` 缺少 `gcstate` 和 `mode` 参数。一旦 Phase 0.1+ 的调用者写入，将导致链接失败。 |
| **修复** | 已对齐三个函数的签名、参数类型和返回值。 |

### I-003: 构建文件新增条目排序错误

| 属性 | 值 |
|------|----|
| **发现阶段** | STEP 4 审查 (Task-0.0) |
| **严重度** | 低 |
| **状态** | ✅ RESOLVED (2026-06-27) |
| **架构参考** | 无（代码风格惯例） |
| **描述** | `PCbuild/pythoncore.vcxproj` 和 `Makefile.pre.in` 中 `gc_stubs` 条目被错误地放在 `gc_free_threading` 之前（应在其之后，因按字母序 `gc_s` > `gc_f`）。 |
| **修复** | 将 `gc_stubs` 移至 `gc_gil` 之后、`getargs` 之前。 |

### I-004: `_PyGC_EdenAlloc` 缺少未使用参数抑制

| 属性 | 值 |
|------|----|
| **发现阶段** | STEP 4 审查 (Task-0.0) |
| **严重度** | 低 |
| **状态** | ✅ RESOLVED (2026-06-27) |
| **架构参考** | ARCHITECTURE.md §5.5 |
| **描述** | `_PyGC_EdenAlloc` 的 `(size_t size)` 参数未使用且没有 `(void)size;` 抑制，可能触发编译器警告。 |
| **修复** | 添加 `(void)size;`。 |

### I-005: PROGRESS.md Phase 0 表格缺少 0.0 行

| 属性 | 值 |
|------|----|
| **发现阶段** | STEP 4 审查 (Task-0.0) |
| **严重度** | 低 |
| **状态** | ✅ RESOLVED (2026-06-27) |
| **架构参考** | PLAN.md §0.0 |
| **描述** | PROGRESS.md Phase 0 表格从 0.1 开始，缺少 0.0 行。Agent 无法在 PROGRESS.md 中标记 0.0 的完成状态。 |
| **修复** | 在表格中补充 0.0 行，标记为已完成。 |

---

> 最后更新：2026-06-27
