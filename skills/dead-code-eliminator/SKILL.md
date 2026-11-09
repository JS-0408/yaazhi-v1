---
name: dead-code-eliminator
description: Scans Python ASTs to detect unreachable statements after return/raise and unused private helper functions.
tags: [dead-code, ast, cleanup]
---

# Dead Code Eliminator
Scan AST function bodies for statements following unconditional `return`/`raise` and remove orphaned definitions.
