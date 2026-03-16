---
description: Review all committed changes on the current branch vs the base branch using local git diff
argument-hint: Optional base branch name (defaults to main/master)
---

# DiffFox Code Review

You are running a LOCAL code review using 6 specialized perspectives. Follow these steps exactly.

**DO NOT use GitHub API, `gh` CLI, or fetch any PRs. This is a LOCAL-ONLY review using `git diff`.**

## Step 1: Get the Local Git Diff (DO THIS FIRST)

This is the most important step. You MUST get the diff using local git commands, NOT from GitHub.

1. Detect the base branch by running this command:
   ```bash
   git branch -l main master 2>/dev/null
   ```
   - If `main` exists, use `main` as the base
   - Else if `master` exists, use `master` as the base
   - If neither exists, ask the user for the base branch name

2. Get the full branch diff using this exact command:
   ```bash
   git diff <base>...HEAD
   ```
   Replace `<base>` with the detected base branch name. This shows ALL committed changes since the current branch diverged from the base.

3. If the diff is empty, tell the user "No changes found on this branch compared to <base>."
4. If the diff is too large, use `git diff <base>...HEAD --stat` first to see which files changed, then read the most important ones.

## Step 2: Load Configuration

1. Check if `.diff-fox/config.yml` exists — if so, read it
2. The config controls:
   - Which agents are enabled (logic, security, architecture, performance, risk, cogs)
   - Per-agent file include/skip patterns
   - Custom guidelines per category
   - Global file skip patterns
   - Suppress filters (suppress findings matching title patterns)

If no config exists, all 6 agents are enabled with defaults.

## Step 3: Read CLAUDE.md

If a CLAUDE.md file exists in the project root, it's already in your context. Use it to understand project conventions, patterns, and requirements. Apply these as context during your review.

## Step 4: Context Enrichment

For each changed file in the diff:

1. **Read the full file** to understand the complete context around changes
2. **Identify changed symbols** (functions, classes, methods) from the diff hunks
3. **Find call sites**: For each changed function/class, use Grep to search the codebase:
   ```
   Grep for: function_name\s*\(
   ```
   Read the surrounding code at each call site to understand how the changed code is used.
4. **Analyze impact**: Check if:
   - Return types changed (now returning None/Optional where it didn't before)
   - Parameter count changed (callers may pass wrong number of args)
   - New exceptions raised (callers may not handle them)

## Step 5: Multi-Perspective Review

Review the changes from ALL 6 perspectives below. For each perspective, think deeply about the code, apply the exclusion rules, and only report findings you are genuinely confident about.

**CRITICAL RULES FOR ALL PERSPECTIVES:**
- ONLY flag issues in lines that were ADDED or MODIFIED in the diff
- Do NOT review or flag issues in unchanged/existing code
- ZERO FINDINGS IS A VALID OUTCOME — quality over quantity
- Be concise: title under 10 words, description 1-2 sentences max
- Only flag what you would flag if your name was on the review

### Perspective 1: LOGIC ERRORS
Focus: bugs at runtime — incorrect conditions, null handling, off-by-one errors, unhandled error paths, edge cases, type mismatches, state mutations.
DO NOT report: defensive null checks, missing error handling when caught by parent, impossible edge cases, style preferences.

### Perspective 2: SECURITY VULNERABILITIES
Focus: injection (SQL, command, template, XXE), auth bypass, privilege escalation, secrets in code, XSS, data exposure, SSRF (server-side only), path traversal.
DO NOT report: DOS/resource exhaustion, rate limiting, memory safety in non-C/C++, log spoofing, open redirects, SSRF in client-side code, test file issues.
Confidence must be >80%. Must provide exploit scenario for each finding.

### Perspective 3: ARCHITECTURE & MAINTAINABILITY
Focus: design pattern violations, wrong layer for logic, DRY violations (>3 lines), breaking API contracts, leaky abstractions, tight coupling.
DO NOT report: 2-3 similar lines (premature abstraction), naming preferences, missing docs, TODO/FIXME comments, import ordering.

### Perspective 4: PERFORMANCE & SCALABILITY
Focus: O(n^2) or worse algorithms, N+1 queries, blocking I/O in async code, unbounded caches, missing connection pooling.
DO NOT report: micro-optimizations, small-N collections (<100), missing cache for cheap operations, startup performance.

### Perspective 5: RISK & DEPLOYMENT SAFETY
Focus: high blast radius changes, breaking backwards compatibility, unsafe migrations, missing rollback path, data integrity risks.
DO NOT report: additive changes (new endpoints/functions), missing feature flags for every change, test-only changes.

### Perspective 6: COST (COGS)
Focus: unbounded queries, LLM calls in loops, missing rate limits on external APIs, auto-scaling without caps, logging without volume limits.
DO NOT report: test/staging concerns, cheap operations (<10ms), one-time migration costs.

## Step 6: Self-Verification

For each finding, ask yourself:
1. Is this genuinely a bug/issue, or a style preference?
2. Does the code I'm referencing actually exist in the diff?
3. Is the issue already handled elsewhere in the code?
4. Would this cause a production incident?

Remove any finding where the answer to questions 1-3 is "no" or question 4 is unlikely.

## Step 7: Format Output

Present findings organized by severity:

For each finding, use this format:
```
[SEVERITY] Title (under 10 words)
File: path/to/file.py:line_start-line_end
Category: logic_error | security | architecture | performance | risk | cost

Description (1-2 sentences)

Suggested fix: (if applicable)
```

Severity levels:
- 🔴 **Critical** — Bug that should block merge
- 🟡 **Warning** — Issue worth fixing but not blocking
- 🔵 **Nit** — Minor improvement
- 🟣 **Pre-existing** — Bug not introduced by this PR

End with a summary: total findings by severity, files reviewed.
