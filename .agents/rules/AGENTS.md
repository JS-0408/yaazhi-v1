# Autonomous Vibe-Coding Guardrails

1. **Self-Execution Verification**: Never mark a feature complete until you execute the code, capture stdout/stderr, and confirm zero errors.
2. **Context Token Optimization**: Use AST/structural summaries for context evaluation instead of re-reading raw source files repeatedly.
3. **Sandbox Safety**: Execute all untrusted external open-source snippets in an isolated container/environment (`CoderAgent` sandbox / `core/skill_manager.py` AST audit).
4. **Self-Improvement Protocol**: When missing a feature, search local skills first -> search external ecosystem -> draft a new `SKILL.md` -> save to `.agents/plugins/vibe-coding-engine/skills/` and `skills/`.
5. **Modularization**: Keep modules focused and cohesive; if any new code file exceeds 300–400 lines, refactor it into discrete sub-modules.
6. **Yaazhi Operating Discipline**: Honour `AGENT_OPERATING_INSTRUCTIONS.md` at all times — verify before asserting, test before claiming done, enforce `user_id` isolation unconditionally, and never hardcode secrets.

