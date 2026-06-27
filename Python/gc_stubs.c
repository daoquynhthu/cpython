// Stub implementations for Py_TRACING_GC mode.
// These stubs ensure CPython links and reaches Py_Initialize() entry
// without crash. No actual GC work is performed.
//
// Replaced with real implementations incrementally in Phase 0.1-0.8.

#include "Python.h"
#include "pycore_interp_structs.h"
#include "pycore_pystate.h"

#if defined(Py_TRACING_GC)

// Initialize the tracing GC heap.
// Returns -1 to signal "not implemented"; Py_Initialize() must handle this
// gracefully (e.g. fall through to a minimal init path).
int
_PyGC_InitHeap(struct _gc_runtime_state *gcstate, size_t young_size, size_t old_size)
{
    (void)gcstate;
    (void)young_size;
    (void)old_size;
    return -1;
}

// Allocate from the Eden region.
// Returns NULL to force callers to fall back to pymalloc.
char *
_PyGC_EdenAlloc(size_t size)
{
    (void)size;
    return NULL;
}

// Enumerate GC roots: empty stub.
typedef enum { YOUNG_ONLY = 0, ALL_GENERATIONS = 1 } _PyGC_RootMode;

void
_PyGC_MarkRoots(PyThreadState *tstate, struct _gc_runtime_state *gcstate,
                _PyGC_RootMode mode)
{
    (void)tstate;
    (void)gcstate;
    (void)mode;
}

// Process the three-colour mark stack: empty stub.
void
_PyGC_ProcessMarkStack(struct _gc_runtime_state *gcstate)
{
    (void)gcstate;
}

// Sweep unreachable objects in the old generation.
// Returns 0 (no objects collected).
Py_ssize_t
_PyGC_SweepOld(struct _gc_runtime_state *gcstate)
{
    (void)gcstate;
    return 0;
}

// Collect the young generation.
// Returns 0 (no objects collected).
Py_ssize_t
_PyGC_CollectYoung(PyThreadState *tstate)
{
    (void)tstate;
    return 0;
}

// Collect the old generation.
// Returns 0 (no objects collected).
Py_ssize_t
_PyGC_CollectOld(PyThreadState *tstate)
{
    (void)tstate;
    return 0;
}

#endif // defined(Py_TRACING_GC)
