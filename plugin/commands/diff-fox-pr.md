---
description: Review a GitHub PR by URL or number using 6 specialized review agents
argument-hint: PR number or URL
---

# DiffFox PR Review

Review a specific GitHub Pull Request. The user provides a PR URL or number.

## Step 1: Get PR Information

Parse the user's input to extract the PR:
- If URL like `https://github.com/owner/repo/pull/123` — extract owner/repo and PR number
- If just a number like `123` — use the current repo's origin remote

Fetch the PR diff using the `gh` CLI:
```bash
gh pr diff <number> --repo <owner/repo>
```

If `gh` is not available, ask the user to install it or provide the diff manually.

## Step 2: Follow the Review Process

Follow the exact same review process as the `/review` skill:
1. Load `.diff-fox/config.yml` if it exists
2. Read CLAUDE.md for project context
3. Context enrichment (read changed files, find call sites, analyze impact)
4. Multi-perspective review from 6 agents
5. Self-verification
6. Format and present findings

The only difference from `/review` is the source of the diff (GitHub PR API vs local git diff).
