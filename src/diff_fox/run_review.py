"""End-to-end review orchestrator.

Connects all pipeline components into a single `run_review` function.
Used by both the GitHub Action and the CLI.
"""

import logging
import time

import anthropic

from diff_fox.config.loader import load_config_from_repo, should_skip_file
from diff_fox.integrations.jira import extract_ticket_numbers, fetch_jira_context, format_jira_context
from diff_fox.output.github_poster import post_review_to_pr
from diff_fox.review.dedup import semantic_dedup
from diff_fox.review.jira_alignment import check_jira_alignment
from diff_fox.review.pipeline import run_pipeline
from diff_fox.review.processor import process_findings
from diff_fox.review.security_filter import filter_security_findings
from diff_fox.review.validator import (
    filter_already_posted,
    llm_filter_already_posted,
    validate_findings_for_posting,
)
from diff_fox.review.verification import verify_findings
from diff_fox.scm.base import SCMProvider

logger = logging.getLogger(__name__)


async def run_review(
    repo: str,
    pr_number: int,
    scm: SCMProvider,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6-20250514",
    post_comments: bool = True,
    jira_enabled: bool = False,
    jira_mcp_url: str = "",
    jira_ticket_pattern: str | None = None,
) -> dict:
    """Run a complete code review for a single PR.

    Flow:
    1. Fetch PR metadata + diff
    2. Load .diff-fox/config.yml
    3. Filter skipped files
    4. Run pipeline (enrich → 6 parallel agents → aggregate)
    5. Verify findings
    6. Hard security filter + semantic dedup + validation
    7. Post comments to GitHub (optional)
    """
    try:
        # 1. Fetch PR metadata
        pr = await scm.get_pull_request(repo, pr_number)
        diff_files = await scm.get_diff(repo, pr_number)

        total_additions = sum(f.additions for f in diff_files)
        total_deletions = sum(f.deletions for f in diff_files)

        logger.info(
            "Starting review for %s PR #%d (%d files, +%d/-%d)",
            repo, pr_number, len(diff_files), total_additions, total_deletions,
        )

        # 2. Fetch existing comments (for dedup)
        try:
            existing_comments = await scm.get_review_comments(repo, pr_number)
            if existing_comments:
                logger.info("Found %d existing comments on PR", len(existing_comments))
        except Exception:
            existing_comments = []

        # 3. Load config
        changed_paths = [f.path for f in diff_files]
        config = await load_config_from_repo(repo, pr.head_sha, scm, changed_paths)

        # 4. Filter skipped files
        if config.skip:
            original_count = len(diff_files)
            diff_files = [
                f for f in diff_files
                if not should_skip_file(f.path, config.skip)
            ]
            skipped = original_count - len(diff_files)
            if skipped:
                logger.info("Skipped %d files matching skip patterns", skipped)

        if not diff_files:
            logger.info("No reviewable files after filtering — skipping review")
            return {"status": "skipped", "reason": "no reviewable files"}

        # 5. Fetch Jira context (if enabled)
        jira_context = None
        jira_context_text = ""
        jira_active = jira_enabled
        if config.jira_enabled is not None:
            jira_active = config.jira_enabled

        if jira_active and jira_mcp_url:
            ticket_numbers = extract_ticket_numbers(
                pr.title, pr.body or "", pattern=jira_ticket_pattern,
            )
            if ticket_numbers:
                try:
                    jira_context = await fetch_jira_context(ticket_numbers, jira_mcp_url)
                    jira_context_text = format_jira_context(jira_context)
                    if jira_context.tickets:
                        logger.info(
                            "Fetched Jira context: %s",
                            ", ".join(t.key for t in jira_context.tickets),
                        )
                except Exception:
                    logger.warning("Jira context fetch failed, continuing without")

        # 6. Run pipeline (enrich → agents → aggregate)
        t0 = time.monotonic()
        raw_findings, enriched_ctx, enrichment_failed, pipeline_metrics = await run_pipeline(
            diff_files=diff_files,
            repo=repo,
            head_sha=pr.head_sha,
            pr_title=pr.title,
            pr_description=pr.body or "",
            config=config,
            scm=scm,
            client=client,
            model=model,
            existing_comments=existing_comments,
            jira_context_text=jira_context_text,
        )
        pipeline_ms = (time.monotonic() - t0) * 1000

        # 7. Verify findings
        verified = await verify_findings(
            raw_findings, diff_files, enriched_ctx, client, model,
        )

        logger.info(
            "Pipeline completed in %.0fms: %d raw → %d verified findings",
            pipeline_ms, len(raw_findings), len(verified),
        )

        # 8. Hard exclusion filter for security findings
        verified, hard_excluded = filter_security_findings(verified)
        if hard_excluded:
            logger.info("Hard exclusion filtered %d security findings", len(hard_excluded))

        # 9. Semantic dedup (LLM-based cross-agent merge)
        merged = await semantic_dedup(verified, client, model)
        logger.info("Semantic dedup: %d → %d findings", len(verified), len(merged))

        # 10. Validate against diff lines + suppress filters
        suppress = config.suppress_filters
        validated, rejected = validate_findings_for_posting(
            merged, diff_files, suppress_filters=suppress,
        )

        # 11. Filter already-posted findings
        if existing_comments:
            validated, already_posted = filter_already_posted(validated, existing_comments)
            if validated:
                validated, already_posted_llm = await llm_filter_already_posted(
                    validated, existing_comments, client, model,
                )

        # 12. Jira alignment check
        alignment = None
        if jira_context and jira_context.tickets:
            alignment = await check_jira_alignment(
                jira_context, validated, diff_files, client, model,
            )

        # 13. Process validated findings (rank, format)
        ranked, comments, summary = process_findings(
            validated, repo, pr_number, enrichment_failed, alignment=alignment,
        )

        # 14. Log findings in dry-run mode
        if not post_comments and ranked:
            logger.info("=== DRY RUN: %d findings ===", len(ranked))
            for i, f in enumerate(ranked, 1):
                logger.info(
                    "  [%d] %s %s | %s:%d-%d | %s",
                    i, f.severity.upper(), f.category, f.file_path,
                    f.line_start, f.line_end, f.title,
                )

        # 15. Post comments
        post_stats = {"inline_posted": 0, "inline_failed": 0, "summary_posted": False}
        if post_comments and ranked:
            post_stats = await post_review_to_pr(
                ranked, repo, pr_number, pr.head_sha, scm,
                enrichment_failed=enrichment_failed,
                pre_formatted_comments=comments,
                pre_formatted_summary=summary,
            )
        elif post_comments and not ranked:
            await scm.post_pr_comment(repo, pr_number, summary)
            post_stats["summary_posted"] = True

        return {
            "status": "completed",
            "repo": repo,
            "pr_number": pr_number,
            "findings_count": len(ranked),
            "posted": post_stats["inline_posted"],
            "failed": post_stats["inline_failed"],
            "summary_posted": post_stats["summary_posted"],
            "pipeline_ms": pipeline_ms,
            "enrichment_failed": enrichment_failed,
        }

    except Exception as exc:
        logger.exception("Review failed for %s PR #%d", repo, pr_number)
        return {
            "status": "failed",
            "repo": repo,
            "pr_number": pr_number,
            "error": str(exc),
        }
