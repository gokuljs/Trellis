# Defense-in-Depth Validation

## Overview

One check may not cover every path through an application. Validate important assumptions at the boundary where data enters and at the operation where an invalid value could cause harm.

## Validation Layers

1. **Entry point:** Reject malformed or incomplete external input.
2. **Business logic:** Check invariants required by the operation.
3. **Operation boundary:** Guard destructive or context-sensitive actions.
4. **Diagnostics:** Record enough context to investigate unexpected failures.

For example, validate that a project directory is non-empty and exists when it enters the application, check the directory again before initializing a workspace, and confirm the resolved path before invoking a filesystem command.

## Applying the Pattern

1. Trace the data flow from input to use.
2. Identify assumptions that must remain true at each boundary.
3. Add the smallest useful validation at those boundaries.
4. Confirm that invalid input produces a clear error and cannot reach a dangerous operation.

Multiple checks should protect distinct boundaries. Avoid repeating the same validation without a clear reason.
