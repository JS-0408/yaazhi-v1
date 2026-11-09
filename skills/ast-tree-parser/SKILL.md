---
name: ast-tree-parser
description: Parses Python code into compact AST structural skeletons (classes, methods, signatures, line numbers) without reading full file bodies to save context tokens.
tags: [ast, skeleton, code-quality, tokens]
---

# AST Tree Parser
Use this skill to inspect module structure (classes, functions, async defs, imports) via Python `ast` before reading full source files.

## Usage
Run `scripts/parse_skeleton.py` or pass target file path in `TASK_INPUT`.
