"""Shared review prompts and context formatting utilities."""

MULTI_LEVEL_THINKING = """
Review this code thinking at EVERY engineering level:

AS A SENIOR ENGINEER: Check for correctness, edge cases, null handling,
incorrect conditions, unhandled error paths, off-by-one errors.

AS A LEAD ENGINEER: Does this follow established codebase patterns?
Is it maintainable? DRY? Consistent with conventions?

AS A STAFF ENGINEER: Does this scale? Performance under load?
Concurrent access issues? Race conditions?

AS A PRINCIPAL ENGINEER: Does this introduce tech debt? Architectural
consistency? Will this age well? System-wide implications?

AS A SECURITY ARCHITECT: Could this create a security bypass?
Data exposure? Input validation gaps? Secrets in code?

AS AN ENGINEERING MANAGER: Blast radius if this fails in production?
Critical path? Needs staged rollout? Extra test coverage?
"""

CONTEXT_INSTRUCTIONS = """
You have been given:
1. The PR diff (what changed)
2. Deep context for each changed symbol: full signatures, call sites
   (where this code is used), callees (what it calls), and impact analysis

CRITICAL — ONLY REVIEW THE CHANGED CODE:
- ONLY flag issues in lines that were ADDED or MODIFIED in this PR diff
- Do NOT review or flag issues in unchanged/existing code
- The context (call sites, callees) is for UNDERSTANDING, not for flagging
- If existing code has a bug that this PR didn't introduce, do NOT flag it
- Your line_start and line_end MUST be within the diff's changed lines

USE THE CONTEXT to verify your findings are real:
- Check call sites to confirm the issue has real downstream impact
- Check callees to confirm functions behave as expected
- Reference specific files and line numbers in your findings
- Only flag issues you are genuinely confident about

ZERO FINDINGS IS A VALID OUTCOME:
- You are NOT expected or required to find issues in every PR.
- Returning an empty findings list is perfectly fine and encouraged when
  the code is correct. This is not a race to find problems.
- Quality over quantity. One real finding is worth more than ten questionable ones.
- Only flag what you would flag if your name was on the review.

BE CONCISE:
- Title: under 10 words (e.g., "Missing null check on getUser return")
- Description: 1-2 sentences max. No filler, no restating the obvious.
- suggested_code: ONLY when you can write exact replacement code for the lines.
  This will render as a GitHub "Apply suggestion" button.
- suggested_fix: plain text explanation when the fix is conceptual or multi-step.
  Do NOT put prose in suggested_code or code in suggested_fix.

JIRA CONTEXT (when provided):
- Use it to UNDERSTAND the intent behind the code changes
- It is INFORMATIONAL ONLY — do NOT suppress or change findings based on Jira
- A bug is a bug regardless of what the Jira ticket says
- Do NOT add findings that only exist because of Jira mismatch
"""

GUIDELINES_HEADER = "\n\nMANDATORY RULES (from team config — ALWAYS enforce):\n"

# Agent metadata: guideline_categories and context_relevance_hints per agent
AGENT_CONFIGS = {
    "logic": {
        "guideline_categories": ["logic", "general"],
        "context_relevance_hints": [
            "optional",
            "none",
            "null",
            "error",
            "exception",
            "raise",
            "try",
            "return",
            "assert",
            "validate",
            "check",
            "parse",
            "convert",
        ],
    },
    "security": {
        "guideline_categories": ["security", "api"],
        "context_relevance_hints": [
            "auth",
            "login",
            "session",
            "token",
            "password",
            "secret",
            "key",
            "sanitize",
            "validate",
            "escape",
            "middleware",
            "handler",
            "route",
            "encrypt",
            "decrypt",
            "hash",
            "permission",
            "role",
            "acl",
        ],
    },
    "architecture": {
        "guideline_categories": ["architecture", "api", "lint"],
        "context_relevance_hints": [
            "interface",
            "abstract",
            "base",
            "factory",
            "service",
            "repository",
            "controller",
            "model",
            "schema",
            "config",
            "init",
            "setup",
        ],
    },
    "performance": {
        "guideline_categories": ["performance"],
        "context_relevance_hints": [
            "query",
            "database",
            "db",
            "cache",
            "redis",
            "loop",
            "iterate",
            "request",
            "response",
            "handler",
            "async",
            "await",
            "pool",
            "batch",
        ],
    },
    "risk": {
        "guideline_categories": ["security", "general"],
        "context_relevance_hints": [
            "migration",
            "deploy",
            "config",
            "flag",
            "rollback",
            "auth",
            "payment",
            "critical",
            "database",
            "schema",
            "version",
            "release",
        ],
    },
    "cogs": {
        "guideline_categories": ["performance", "general"],
        "context_relevance_hints": [
            "api",
            "http",
            "request",
            "query",
            "database",
            "llm",
            "openai",
            "anthropic",
            "s3",
            "redis",
            "cache",
            "lambda",
            "metric",
            "log",
        ],
    },
}
