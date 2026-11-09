---
name: git-auto-committer
description: Inspects `git status` and `git diff` to stage verified, test-passing changes with structured conventional commit messages.
tags: [git, commit, version-control]
---

# Git Auto-Committer
Run `py -m pytest` before committing, verify no secrets or `.env` files are staged, and format commit messages as `feat|fix|test(scope): summary`.
