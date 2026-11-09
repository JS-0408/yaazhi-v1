---
name: auto-debugger-loop
description: Captures pytest/runtime stderr tracebacks, isolates the root cause line and signature mismatch, applies minimal fixes, and re-runs until green.
tags: [debugging, traceback, self-healing]
---

# Auto-Debugger Loop
1. Capture exact exception type, file, and line number.
2. Inspect the callee signature and caller arguments.
3. Apply minimal fix and re-run test to confirm 0 errors.
