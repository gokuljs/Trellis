# Root Cause Tracing

## Overview

Bugs can appear far from the code that supplied the invalid value. Trace backward through the call chain until you find where that value first became incorrect, then fix it at its source.

## When to Use

- An error occurs deep in execution.
- A stack trace shows a long call chain.
- You do not know where invalid data originated.
- A fix at the visible failure point would only mask the symptom.

## The Tracing Process

1. **Observe the symptom.** Record the error, relevant values, and where it occurs.
2. **Find the immediate cause.** Identify the operation that directly produced the error.
3. **Trace its inputs.** Follow each relevant value to the caller that supplied it.
4. **Continue until the source is clear.** Check configuration, request data, and earlier transformations.
5. **Fix the source.** Add validation where the invalid value enters or changes meaning.

For example, if a filesystem operation receives an empty project path, trace the path through its callers until you find the configuration or input that supplied the empty value. Validate it at that boundary and retain a guard near the filesystem operation.

## Instrumentation

When static tracing is not enough, add temporary diagnostics immediately before the failing operation:

```typescript
async function initializeWorkspace(directory: string) {
  logger.debug('Initializing workspace', {
    directory,
    cwd: process.cwd(),
    stack: new Error().stack,
  });
  // ... proceed
}
```

Capture the values that affect the operation, the current environment, and a call stack. Remove temporary diagnostics after the source is understood, or replace them with appropriate structured logging.

## Key Principle

Keep tracing until you can explain how the bad value was produced. A guard at the failure point may prevent damage, while validation at the source prevents the invalid value from spreading.
