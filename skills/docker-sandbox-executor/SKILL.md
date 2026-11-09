---
name: docker-sandbox-executor
description: Executes untrusted or generated Python snippets inside Yaazhi CoderAgent network-blocked, AST-audited sandbox.
tags: [sandbox, security, isolation, docker]
---

# Sandbox Executor
All generated and external open-source Python code must pass `scan_python_ast()` and run inside `CoderAgent.execute_code()` with socket networking disabled.
