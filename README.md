# DiffFox

AI-powered code review with 6 specialized agents. Runs as a **GitHub Action** on PRs or as a **Claude Code plugin** locally.

## What It Does

DiffFox analyzes code changes from 6 engineering perspectives simultaneously:

| Agent | Focus |
|-------|-------|
| **Logic** | Bugs at runtime — null handling, off-by-one, incorrect conditions, edge cases |
| **Security** | Injection, auth bypass, secrets, XSS, SSRF, data exposure |
| **Architecture** | Design violations, DRY, API contracts, coupling, tech debt |
| **Performance** | N+1 queries, O(n^2) algorithms, blocking I/O, resource leaks |
| **Risk** | Blast radius, backwards compatibility, migration safety, rollback |
| **COGS** | Unbounded queries, LLM calls in loops, missing rate limits, cost spikes |

Each agent has its own exclusion rules and precedent rules to minimize false positives. Findings are verified by a second-opinion LLM pass, deduplicated across agents, and validated against diff lines before posting.

## Quick Start

### GitHub Action

```yaml
# .github/workflows/diff-fox.yml
name: DiffFox Review
on:
  pull_request:
    types: [opened, synchronize, ready_for_review]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: read
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - uses: mthooyavan/diff-fox@main
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

> **Dependabot PRs:** If you use Dependabot, add `ANTHROPIC_API_KEY` under **Settings > Secrets > Dependabot** separately — Dependabot can't access regular repo secrets. Alternatively, skip DiffFox for Dependabot by adding `if: github.actor != 'dependabot[bot]'` to the job.

### Claude Code Plugin

**Install from GitHub** (inside any Claude Code session):
```
/plugin marketplace add mthooyavan/diff-fox
/plugin install diff-fox@diff-fox-marketplace
```

**Or install from local clone:**
```bash
git clone https://github.com/mthooyavan/diff-fox.git ~/diff-fox
```
Then in Claude Code:
```
/plugin marketplace add ~/diff-fox
/plugin install diff-fox@diff-fox-marketplace
```

**Usage** (start a new Claude Code session after install):
```
/diff-fox              # Review all changes on current branch vs main/master
/diff-fox-pr 123       # Review a specific GitHub PR
```

No Python dependencies — uses Claude Code's built-in Read, Grep, Glob, and Bash tools.

> **Note:** If you also have the `code-review` plugin installed, use `/diff-fox` (not `/review`) to avoid name collision.

## Configuration

Create `.diff-fox/config.yml` in your repo root:

```yaml
# Enable/disable agents
agents:
  logic: true
  security: true
  architecture: true
  performance: true
  risk: true
  cogs: true

  # Per-agent file filtering
  security:
    enabled: true
    include: ["src/api/**"]
    skip: ["src/api/tests/**"]

# Custom guidelines injected into agent prompts
guidelines:
  security:
    - "All API endpoints must validate JWT tokens"
  architecture:
    - "Database access only through repository pattern"

# Global file filtering
skip:
  - "src/generated/**"
  - "**/*.min.js"
  - "vendor/**"

# Suppress findings matching these title patterns
suppress_filters:
  - "commented-out code"

# Optional: Jira integration
jira:
  enabled: true
```

The config supports hierarchical merging — repo-level config is merged with project-level config found by walking up from the primary changed directory.

## Action Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `anthropic-api-key` | Yes | — | Anthropic API key |
| `model` | No | `claude-sonnet-4-6-20250514` | Claude model to use |
| `config-path` | No | `.diff-fox/config.yml` | Config file path |
| `jira-mcp-url` | No | — | Jira MCP server URL |
| `jira-enabled` | No | `false` | Enable Jira context |
| `post-comments` | No | `true` | Post comments to PR |

## How It Works

```
PR opened
  → Fetch diff + existing comments
  → Load .diff-fox/config.yml
  → Context enrichment (symbol extraction, call graphs, impact analysis)
  → 6 agents review in parallel
  → Verification (second-opinion LLM pass)
  → Hard security exclusion filter (regex)
  → Semantic dedup (LLM merges cross-agent duplicates)
  → Validate against diff lines
  → Filter already-posted comments
  → Jira alignment check (optional)
  → Post inline comments + summary
```

### Deterministic vs AI-Powered

**Deterministic (no LLM):**
- Diff parsing, symbol extraction (Python AST + regex for other languages)
- Call site search, callee extraction, impact analysis
- Security hard exclusion filter (compiled regex patterns)
- Diff line validation, heuristic dedup, severity ranking, formatting

**AI-Powered (Claude):**
- 6 parallel agent reviews with specialized prompts
- Finding verification (second opinion)
- Semantic dedup (cross-agent/cross-file merge)
- Jira alignment check

## Architecture

```
diff-fox/
├── action/              # GitHub Action (Docker-based)
│   ├── action.yml
│   ├── Dockerfile
│   └── entrypoint.py
├── plugin/              # Claude Code Plugin
│   ├── plugin.json
│   ├── agents/reviewer.md
│   └── skills/
├── src/diff_fox/        # Core library
│   ├── config/          # .diff-fox/config.yml loader
│   ├── context/         # Symbol extraction, call graphs, impact
│   ├── review/          # Pipeline, verification, dedup, agents
│   ├── scm/             # GitHub API client
│   ├── integrations/    # Jira MCP
│   ├── output/          # GitHub poster, text formatter
│   ├── llm.py           # Anthropic SDK wrapper
│   └── run_review.py    # Main orchestrator
└── pyproject.toml
```

## Dependencies

- `anthropic` — Claude API
- `httpx` — GitHub API client
- `pyyaml` — Config parsing
- `pydantic` — Data models

No LangChain. No LangGraph. No FastAPI. No Temporal. No database.

## License

MIT
