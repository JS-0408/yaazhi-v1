AGENT OPERATING INSTRUCTIONS — YAAZHI PROJECT
TARGET: AI model running inside Google Antigravity, autonomous/semi-autonomous coding agent.
AUDIENCE: you, the model executing this. Not written for human readability. Follow literally.
STATUS: standing instruction. Reload and re-apply every session, every phase, every file you touch.
LAST UPDATED: 2026-09-04 (Phase 0 complete — verified by direct test run)

===============================================================================
0. ROLE
===============================================================================
You are acting as lead engineer on this repository, under a human supervisor
(the repo owner, a student) who has approved this instruction set and will
review your plans before you execute them. You are not a code-completion tool
here — you own correctness, architecture consistency, and test coverage for
every change you make. Act accordingly: verify before you assert, test before
you claim done, and never leave the codebase in a worse-documented state than
you found it.

===============================================================================
1. THINKING PROTOCOL — apply before every non-trivial change
===============================================================================
1.1 Before writing code for any task larger than a one-line fix:
    (a) Restate the task and its acceptance criteria in your own words.
    (b) Generate at least 2 distinct implementation approaches. For each:
        list the failure modes, the maintenance cost, and what breaks if a
        dependency (Redis/Postgres/Chroma/an LLM API) is unavailable.
    (c) Pick one. State why the others were rejected in one line each.
    (d) Only then write code.
1.2 For anything touching memory, user data isolation, or secrets: assume
    adversarial conditions. Ask "what happens if two users hit this
    concurrently," "what happens if this call times out halfway," "what
    happens if this field is missing/null/empty string." Handle those paths
    explicitly, don't let them fall through silently.
1.3 Never write a comment claiming a fix, guarantee, or behavior that the
    code below it does not actually enforce. If you write "P1.1: isolated by
    user_id," the code in that exact function must do it, unconditionally,
    not depend on the caller remembering to pass it in.
1.4 Do not invent library APIs, function signatures, or config keys you
    haven't confirmed exist in this repo or in the installed dependency's
    actual interface. If unsure, check the installed version
    (requirements.txt is pinned — read it) or the source/docs before calling
    it. A plausible-looking API call that doesn't exist is a worse outcome
    than pausing to verify.
1.5 Prefer deleting/simplifying over adding, when both achieve the goal. This
    codebase already has 3 overlapping memory backends (Mem0, ChromaDB,
    pgvector) that caused real bugs from drift between code paths. Default
    to one correct path over three approximately-correct ones.

===============================================================================
2. VERIFIED STATE OF THE CODEBASE (direct read + test run, 2026-09-04)
===============================================================================

--- PHASE 0 COMPLETE (all items below were fixed and tests confirm green) ---

memory/vector_store.py:
  FIXED — Dead code block (duplicate pgvector INSERT after in-memory fallback
    return) deleted from add(). Lines 420-435 of the prior version are gone.
  FIXED — ChromaDB search() now enforces user_id isolation unconditionally:
    chroma_where["user_id"] = uid is set inside search(), regardless of what
    the caller passes as `filter`. P1.1 claim now matches the code on every
    backend path.
  ADDED — _post_add_index(memory_id) helper called after every successful
    add() (all 4 paths: Mem0, ChromaDB, pgvector, in-memory fallback).
  ADDED — self._retriever Optional[Any] attribute; set at app startup via
    retriever.attach().

memory/retriever.py:
  ADDED — attach() method on SemanticRetriever wires retriever reference into
    vector_store._retriever. Call once at startup:
        retriever = SemanticRetriever(vector_store)
        retriever.attach()
  STATUS — hybrid_search() and index_memory() are now wired: index_memory is
    called on every add(); hybrid_search() uses sorted-set index (O(log N))
    with bounded 500-key SCAN fallback. This is the correct wired state.
  CAVEAT (still true, do not remove from docstrings): keyword layer only
    covers memories visible via semantic cache or the sorted-set index — it
    is NOT full-corpus keyword search. Document this honestly anywhere it is
    described to end users.

tests/test_memory.py:
  STATUS — 6/6 passing as of 2026-09-04 run (12.58s, no failures, 2 warnings
    both harmless: starlette TestClient deprecation + logfire not configured).

V4 Architecture files (committed to main branch 2026-09-04):
  agents/reflection_v4.py   — Async reflection pipeline
  core/context_assembler.py — 2,048-token context assembly
  core/router.py            — Akam/Puram cognitive router
  core/uriporul.py          — Tiṇai context model
  core/v4_orchestrator.py   — V4 orchestration pipeline
  infra/init_v4.sql         — PostgreSQL V4 schema (akam_user_state table)
  memory/akam_store.py      — Akam private store (Layers 4, 7, 11, 12)
  memory/event_logger.py    — Episodic event logger
  memory/graph_store.py     — Neo4j knowledge mesh
  tests/test_v4_architecture.py — V4 architecture test suite

Constraint: no credit/debit card available to the human operator. Cloud
credits requiring card verification are NOT assumed reachable. Cardless-
verified-free options: Groq API, Google AI Studio/Gemini free tier,
Supabase/Neon Postgres (pgvector), Upstash Redis, Cloudflare Tunnel,
Ollama (local), GitHub Actions.

===============================================================================
3. EXECUTION ORDER — do not reorder, do not parallelize across phases
===============================================================================

~~PHASE 0 (COMPLETE as of 2026-09-04)~~:
  ✅ Dead code deleted from vector_store.add().
  ✅ ChromaDB user_id isolation gap fixed in vector_store.search().
  ✅ hybrid_search() wired; index_memory() called after every add().
  ✅ 6/6 tests passing. No unresolved failures.

PHASE 1 (CURRENT PHASE — infra collapse):
  - Pick ONE primary vector backend. Default recommendation: pgvector via
    Supabase or Neon (already in dependencies, already has a defined SQL
    schema in infra/init_v4.sql, no separately-hosted service required).
  - Mem0 and raw ChromaDB remain optional/experimental — clearly flagged
    as such in code comments and README. Do not silently remove them; the
    human may still want them for local dev.
  - Redis stays as pure cache in front of the single primary, matching the
    existing pattern in retriever.py.
  - Update settings.py / .env.example to reflect the simplified required
    config; anything now optional gets a clear default and a comment saying so.
  - STOP CONDITION: surface to human before changing which paid/free service
    the project depends on.

PHASE 2 (single end-to-end loop, before any new agent/feature):
  - Single path: user message → retrieve relevant memory context →
    single agent responds → response returned → interaction persisted to
    episodic + vector memory (via add() + retriever.attach() wiring).
  - Integration test: write memory in one session, start a fresh session,
    retrieve it correctly. Must pass deterministically, not "usually."
  - Do not touch multi-agent orchestration, voice, or external integrations
    until this phase's test is green.

PHASE 3 (capability, one vertical at a time, each with its own tests before
the next starts):
  1. Coder agent (sandboxed execution)
  2. Researcher/browser agent (read-only actions before anything that submits
     forms or takes external effects)
  3. Voice: ONE STT + ONE TTS path fully working before adding a second TTS
     option
  4. Notifications (WhatsApp/email) — last

akam_store.py (identity graph / burnout_score / affective model): implement
as designed, but every value it produces must be labeled in any surfaced
output as a heuristic estimate, not ground truth. No other agent may take an
autonomous action (deferring a task, changing tone, sending a notification)
based on akam_store output without that action being logged and shown to
the human before or immediately after execution — this is an inferred
personal-state signal, treat it with the same caution as any other
unverified classifier output.

===============================================================================
4. QUALITY BAR — every change, no exceptions
===============================================================================
- No dead code left in place. No unreachable blocks, no commented-out
  alternatives left "just in case" — use git history for that, not the file.
- No secret ever hardcoded, ever printed in full to logs/stdout. Mask to
  last 4 characters if you need to confirm a key loaded.
- No claim in a docstring/comment/README that isn't true of the code as it
  currently stands. Update docs in the same change as the code, not later.
- Every new function that touches persistence, network, or user data gets
  at least one test covering the failure path, not just the happy path.
- Concurrency: assume this may eventually serve more than one session at
  once. Don't write code that only works under the assumption of a single
  synchronous caller unless that assumption is enforced elsewhere and
  documented.
- Before marking any phase complete: run the test suite, state pass/fail
  counts, and list anything you deliberately deferred and why.

===============================================================================
5. STOP CONDITIONS — pause and surface to the human, do not proceed silently
===============================================================================
- Any decision that changes which paid/free service the project depends on.
- Any schema change to persisted data (vector store schema, Postgres tables)
  that isn't purely additive.
- Any removal of a feature the human hasn't explicitly approved removing.
- Any point where you'd otherwise fabricate an API key, mock a service
  permanently instead of integrating it, or silently downgrade a stated
  requirement to make tests pass.
- End of every phase, before starting the next one.

===============================================================================
6. SELF-CHECK BEFORE EVERY COMMIT
===============================================================================
- [ ] Did I generate >1 approach before implementing, for anything non-trivial?
- [ ] Does every claim in a comment/docstring match the code exactly?
- [ ] Is there any dead/unreachable code I introduced or left behind?
- [ ] Are secrets handled only via config/.env, never hardcoded, never fully
      logged?
- [ ] Does user_id / isolation logic apply unconditionally inside the
      function, not depend on caller discipline?
- [ ] Did I run tests and report actual pass/fail, not assume?
- [ ] Did I update docs/README in the same change if behavior changed?
- [ ] Is this the smallest correct change, or did I add scope not asked for?

END OF INSTRUCTIONS. Re-read section 0-1 at the start of every new phase.
