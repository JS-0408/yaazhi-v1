---
name: pr-reviewer-agent
description: Performs security, SSRF, user_id isolation, and regression audits on code diffs before merge.
tags: [review, security, audit, diff]
---

# PR Reviewer Agent
Audit every diff against `AGENT_OPERATING_INSTRUCTIONS.md`: verify `user_id` isolation, SSRF guards, no hardcoded secrets, and matching docstrings.
