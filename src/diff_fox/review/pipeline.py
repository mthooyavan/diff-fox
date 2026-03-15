"""Review pipeline orchestrator.

Replaces LangGraph's StateGraph + Send with plain asyncio.gather
for parallel agent fan-out. Same architecture:
  enrich_context → [6x agents in parallel] → aggregate → verify
"""

import asyncio
import logging
import time

import anthropic

from diff_fox.config.loader import filter_files_for_agent
from diff_fox.config.models import ResolvedConfig
from diff_fox.context.enricher import enrich_context
from diff_fox.llm import get_structured_output
from diff_fox.models import EnrichedContext, Finding, ReviewFindings, SymbolContext
from diff_fox.review.prompts.base import (
    AGENT_CONFIGS,
    CONTEXT_INSTRUCTIONS,
    GUIDELINES_HEADER,
    MULTI_LEVEL_THINKING,
)
from diff_fox.scm.base import SCMProvider
from diff_fox.scm.models import DiffFile

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 60_000
MAX_CONTEXT_SYMBOLS = 30


def _get_agent_prompt(agent_name: str) -> str:
    """Import and return the focus prompt for a given agent."""
    if agent_name == "logic":
        from diff_fox.review.prompts.logic import LOGIC_FOCUS_PROMPT
        return LOGIC_FOCUS_PROMPT
    elif agent_name == "security":
        from diff_fox.review.prompts.security import SECURITY_FOCUS_PROMPT
        return SECURITY_FOCUS_PROMPT
    elif agent_name == "architecture":
        from diff_fox.review.prompts.architecture import ARCHITECTURE_FOCUS_PROMPT
        return ARCHITECTURE_FOCUS_PROMPT
    elif agent_name == "performance":
        from diff_fox.review.prompts.performance import PERFORMANCE_FOCUS_PROMPT
        return PERFORMANCE_FOCUS_PROMPT
    elif agent_name == "risk":
        from diff_fox.review.prompts.risk import RISK_FOCUS_PROMPT
        return RISK_FOCUS_PROMPT
    elif agent_name == "cogs":
        from diff_fox.review.prompts.cogs import COGS_FOCUS_PROMPT
        return COGS_FOCUS_PROMPT
    raise ValueError(f"Unknown agent: {agent_name}")


def _relevance_score(symbol: SymbolContext, hints: list[str], ctx: EnrichedContext | None) -> int:
    """Score a symbol's relevance to an agent based on keyword hints."""
    score = 0
    name_lower = symbol.qualified_name.lower()
    path_lower = symbol.file_path.lower()

    for hint in hints:
        h = hint.lower()
        if h in name_lower:
            score += 3
        if h in path_lower:
            score += 2

    if ctx and ctx.callees:
        callees = ctx.callees.get(symbol.qualified_name, [])
        for callee in callees:
            callee_lower = callee.name.lower()
            for hint in hints:
                if hint.lower() in callee_lower:
                    score += 2
                    break

    if ctx and ctx.call_sites:
        site_count = len(ctx.call_sites.get(symbol.qualified_name, []))
        score += min(site_count, 5)

    return score


def _format_diff(diff_files: list[DiffFile]) -> str:
    """Format diff files into a readable string for the LLM."""
    parts: list[str] = []
    total_chars = 0
    for f in diff_files:
        header = f"--- {f.path} ({f.status}) [+{f.additions}/-{f.deletions}]"
        parts.append(header)
        if f.patch:
            remaining = MAX_DIFF_CHARS - total_chars
            if remaining <= 0:
                parts.append("[DIFF TRUNCATED — too large for single review pass]")
                break
            patch_chunk = f.patch[:remaining]
            parts.append(patch_chunk)
            total_chars += len(patch_chunk)
        parts.append("")
    return "\n".join(parts)


def _format_context(ctx: EnrichedContext | None, agent_name: str) -> str:
    """Format enriched context, sorted by agent-specific relevance."""
    if not ctx or not ctx.symbols:
        return "No enriched context available."

    symbols = list(ctx.symbols)
    hints = AGENT_CONFIGS.get(agent_name, {}).get("context_relevance_hints", [])
    if hints:
        symbols = sorted(
            symbols,
            key=lambda s: _relevance_score(s, hints, ctx),
            reverse=True,
        )

    parts: list[str] = []

    for sym in symbols[:MAX_CONTEXT_SYMBOLS]:
        parts.append(f"\n=== Symbol: {sym.qualified_name} ({sym.symbol_type}) ===")
        parts.append(f"File: {sym.file_path}")
        parts.append(f"Change: {sym.change_type}")
        parts.append(f"Signature: {sym.signature}")
        if sym.docstring:
            parts.append(f"Docstring: {sym.docstring}")
        if sym.full_body:
            parts.append(f"Full Body:\n{sym.full_body}")

        sites = ctx.call_sites.get(sym.qualified_name, [])
        if sites:
            parts.append(f"\nCALL SITES ({len(sites)} found):")
            for site in sites[:10]:
                parts.append(
                    f"  - {site.file_path}:{site.line_number}"
                    f" in {site.caller_function or 'module level'}"
                )
                parts.append(f"    {site.call_expression}")

        callees = ctx.callees.get(sym.qualified_name, [])
        if callees:
            parts.append(f"\nCALLEES ({len(callees)} functions called):")
            for callee in callees[:10]:
                parts.append(f"  - {callee.name}")

        impacts = ctx.impact_map.get(sym.qualified_name, [])
        if impacts:
            parts.append(f"\nIMPACT ANALYSIS ({len(impacts)} potential issues):")
            for imp in impacts:
                parts.append(
                    f"  - [{imp.severity.upper()}] {imp.impact_type}: {imp.description}"
                )

    if len(symbols) > MAX_CONTEXT_SYMBOLS:
        parts.append(
            f"\n[CONTEXT TRUNCATED — showing {MAX_CONTEXT_SYMBOLS} "
            f"of {len(symbols)} symbols]"
        )

    return "\n".join(parts)


def build_system_prompt(
    agent_name: str,
    config: ResolvedConfig,
    security_scan_instructions: str | None = None,
) -> str:
    """Build the full system prompt for an agent."""
    focus = _get_agent_prompt(agent_name)
    prompt = focus + MULTI_LEVEL_THINKING + CONTEXT_INSTRUCTIONS

    guidelines = config.guidelines
    if guidelines:
        categories = AGENT_CONFIGS.get(agent_name, {}).get("guideline_categories", [])
        rules_sections: list[str] = []
        for category in categories:
            rules = guidelines.get(category, [])
            if rules:
                section = f"[{category.upper()}]\n" + "\n".join(f"- {r}" for r in rules)
                rules_sections.append(section)
        if rules_sections:
            prompt += GUIDELINES_HEADER + "\n\n".join(rules_sections)

    if agent_name == "security" and security_scan_instructions:
        prompt += (
            "\n\nADDITIONAL ORGANIZATION-SPECIFIC SECURITY CHECKS:\n"
            f"{security_scan_instructions}\n"
        )

    return prompt


def build_user_message(
    diff_files: list[DiffFile],
    ctx: EnrichedContext | None,
    pr_title: str,
    pr_description: str,
    existing_comments: list[dict],
    jira_context_text: str,
    agent_name: str,
) -> str:
    """Build the user message for an agent review call."""
    existing_ctx = ""
    if existing_comments:
        existing_ctx = "<already_flagged>\n"
        existing_ctx += "These issues are ALREADY flagged on this PR. Do NOT repeat them:\n"
        for c in existing_comments[:20]:
            title_line = c["body"].split("\n")[0][:80] if c.get("body") else ""
            existing_ctx += f"  - {c.get('path', '')}:{c.get('line', '')} {title_line}\n"
        existing_ctx += "</already_flagged>\n\n"

    pr_context = ""
    if pr_title or pr_description:
        pr_context = "<pr_context>\n" f"Title: {pr_title}\n"
        if pr_description:
            desc = pr_description[:2000]
            pr_context += f"Description: {desc}\n"
        pr_context += "</pr_context>\n\n"

    jira_block = f"{jira_context_text}\n\n" if jira_context_text else ""

    diff_text = _format_diff(diff_files)
    context_text = _format_context(ctx, agent_name)

    return (
        f"{existing_ctx}"
        f"{pr_context}"
        f"{jira_block}"
        "<pr_diff>\n"
        f"{diff_text}\n"
        "</pr_diff>\n\n"
        "<deep_context>\n"
        f"{context_text}\n"
        "</deep_context>"
    )


async def _run_single_agent(
    agent_name: str,
    diff_files: list[DiffFile],
    ctx: EnrichedContext | None,
    config: ResolvedConfig,
    client: anthropic.AsyncAnthropic,
    model: str,
    pr_title: str,
    pr_description: str,
    existing_comments: list[dict],
    jira_context_text: str,
) -> tuple[list[Finding], dict]:
    """Run a single review agent and return its findings + metrics."""
    t0 = time.monotonic()

    system_prompt = build_system_prompt(
        agent_name, config,
        security_scan_instructions=config.security_scan_instructions,
    )
    user_message = build_user_message(
        diff_files, ctx, pr_title, pr_description,
        existing_comments, jira_context_text, agent_name,
    )

    try:
        result, token_count = await get_structured_output(
            client, model, system_prompt, user_message, ReviewFindings,
        )
        duration_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Agent %s: %d findings, %d tokens, %.0fms",
            agent_name, len(result.findings), token_count, duration_ms,
        )
        metrics = {
            f"agent_{agent_name}_tokens": token_count,
            f"agent_{agent_name}_duration_ms": duration_ms,
            f"agent_{agent_name}_findings": len(result.findings),
        }
        return result.findings, metrics
    except Exception:
        logger.exception("Agent %s failed during review", agent_name)
        duration_ms = (time.monotonic() - t0) * 1000
        return [], {f"agent_{agent_name}_duration_ms": duration_ms}


async def run_pipeline(
    diff_files: list[DiffFile],
    repo: str,
    head_sha: str,
    pr_title: str,
    pr_description: str,
    config: ResolvedConfig,
    scm: SCMProvider,
    client: anthropic.AsyncAnthropic,
    model: str,
    existing_comments: list[dict],
    jira_context_text: str = "",
) -> tuple[list[Finding], EnrichedContext | None, bool, dict]:
    """Run the full review pipeline: enrich → agents → aggregate.

    Returns (findings, enriched_context, enrichment_failed, metrics).
    Verification is done by the caller (run_review) after this.
    """
    metrics: dict = {}

    # Step 1: Enrich context (deterministic)
    t0 = time.monotonic()
    enrichment_failed = False
    ctx: EnrichedContext | None = None
    try:
        ctx = await enrich_context(
            diff_files=diff_files,
            repo=repo,
            head_ref=head_sha,
            scm=scm,
        )
    except Exception:
        logger.exception("Context enrichment failed for %s", repo)
        ctx = EnrichedContext()
        enrichment_failed = True
    metrics["enrichment_duration_ms"] = (time.monotonic() - t0) * 1000

    # Step 2: Determine enabled agents and filter files per agent
    enabled = [name for name, cfg in config.agents.items() if cfg.enabled]
    if not enabled:
        logger.info("No agents enabled — skipping review")
        return [], ctx, enrichment_failed, metrics

    logger.info("Dispatching %d agents: %s", len(enabled), enabled)

    # Step 3: Run all enabled agents in parallel
    agent_tasks = []
    for name in enabled:
        agent_cfg = config.agents.get(name)
        agent_include = agent_cfg.include if agent_cfg else []
        agent_skip = agent_cfg.skip if agent_cfg else []

        agent_files = filter_files_for_agent(
            diff_files, config.include, config.skip, agent_include, agent_skip,
        )

        agent_tasks.append(
            _run_single_agent(
                name, agent_files, ctx, config, client, model,
                pr_title, pr_description, existing_comments, jira_context_text,
            )
        )

    results = await asyncio.gather(*agent_tasks, return_exceptions=True)

    # Step 4: Aggregate findings from all agents
    all_findings: list[Finding] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Agent task failed: %s", result)
            continue
        findings, agent_metrics = result
        all_findings.extend(findings)
        metrics.update(agent_metrics)

    logger.info("Aggregated %d raw findings from %d agents", len(all_findings), len(enabled))

    return all_findings, ctx, enrichment_failed, metrics
