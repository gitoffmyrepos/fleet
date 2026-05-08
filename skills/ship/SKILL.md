---
name: fleet-ship
description: Use when ready to release. Drives gsd:ship via Fleet — runs final verification, prepares PR/merge.
---

# Fleet Ship

Call `mcp__fleet__ship`. Internally maps to `dispatch_phase(stage="ship")` which invokes `/gsd:ship`.

Always check the returned `verdict` before announcing release. If FAIL, surface the gating issues to the operator.
