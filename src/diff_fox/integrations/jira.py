"""Jira context fetcher.

Extracts Jira ticket numbers from PR metadata, fetches ticket details,
and returns structured context for agent prompts.

Jira context is ADDITIVE — it should never block or delay a review.
All failures are graceful: log and continue without Jira context.
"""

import json
import logging
import re

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_TICKET_PATTERN = r"\b[A-Z][A-Z0-9]+-\d+\b"


class JiraTicket(BaseModel):
    key: str = Field(description="Ticket key, e.g., PROJ-123")
    summary: str = ""
    status: str = ""
    priority: str = ""
    issue_type: str = ""
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    acceptance_criteria: str = ""


class JiraContext(BaseModel):
    tickets: list[JiraTicket] = Field(default_factory=list)


def extract_ticket_numbers(
    pr_title: str,
    pr_description: str,
    pattern: str | None = None,
) -> list[str]:
    """Extract unique Jira ticket numbers from PR title and description."""
    ticket_re = re.compile(pattern or DEFAULT_TICKET_PATTERN, re.IGNORECASE)
    combined = f"{pr_title} {pr_description}"
    matches = ticket_re.findall(combined)
    if not matches:
        return []

    normalized = set()
    for m in matches:
        key = m.upper().replace(" ", "-")
        normalized.add(key)

    result = sorted(normalized)
    if result:
        logger.info("Extracted Jira tickets: %s", ", ".join(result))
    return result


async def fetch_jira_context(
    ticket_numbers: list[str],
    mcp_url: str,
) -> JiraContext:
    """Fetch Jira ticket details via MCP Atlassian server."""
    if not ticket_numbers or not mcp_url:
        return JiraContext()

    tickets: list[JiraTicket] = []

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            for key in ticket_numbers:
                try:
                    resp = await client.post(
                        mcp_url,
                        json={
                            "method": "tools/call",
                            "params": {
                                "name": "jira_get_issue",
                                "arguments": {"issueIdOrKey": key},
                            },
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        ticket = _parse_mcp_response(key, data)
                        if ticket:
                            tickets.append(ticket)
                            logger.info("Fetched Jira ticket: %s — %s", key, ticket.summary)
                except Exception:
                    logger.warning("Failed to fetch Jira ticket %s", key, exc_info=True)
                    continue

    except Exception:
        logger.warning("Jira MCP connection failed", exc_info=True)

    return JiraContext(tickets=tickets)


def _parse_mcp_response(key: str, data: dict) -> JiraTicket | None:
    """Parse MCP tool response into a JiraTicket."""
    try:
        result = data.get("result", data)
        content = result.get("content", []) if isinstance(result, dict) else []

        text = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")

        if not text:
            return JiraTicket(key=key)

        try:
            parsed = json.loads(text)
            return _extract_ticket_fields(key, parsed)
        except json.JSONDecodeError:
            return JiraTicket(key=key, description=text[:3000])

    except Exception:
        logger.warning("Failed to parse Jira response for %s", key, exc_info=True)
        return JiraTicket(key=key)


def _extract_ticket_fields(key: str, data: dict) -> JiraTicket:
    fields = data.get("fields", data)

    description = fields.get("description", "") or ""
    if isinstance(description, dict):
        description = _flatten_adf(description)

    acceptance = ""
    for marker in ["acceptance criteria", "ac:", "acceptance:"]:
        idx = description.lower().find(marker)
        if idx != -1:
            acceptance = description[idx:idx + 1000].strip()
            break

    return JiraTicket(
        key=key,
        summary=fields.get("summary", "") or "",
        status=_nested_get(fields, "status", "name") or "",
        priority=_nested_get(fields, "priority", "name") or "",
        issue_type=_nested_get(fields, "issuetype", "name") or "",
        description=description[:3000],
        labels=fields.get("labels", []) or [],
        acceptance_criteria=acceptance,
    )


def _nested_get(data: dict, *keys: str) -> str:
    current = data
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k, "")
        else:
            return ""
    return str(current) if current else ""


def _flatten_adf(doc: dict) -> str:
    """Flatten Atlassian Document Format to plain text."""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return "\n".join(parts)


def format_jira_context(jira_context: JiraContext) -> str:
    """Format Jira context as a string block for agent prompts."""
    if not jira_context.tickets:
        return ""

    parts = ["<jira_context>"]
    for ticket in jira_context.tickets:
        parts.append(f"\n## {ticket.key}: {ticket.summary}")
        parts.append(
            f"Status: {ticket.status} | Priority: {ticket.priority} | "
            f"Type: {ticket.issue_type}"
        )
        if ticket.description:
            parts.append(f"Description: {ticket.description[:2000]}")
        if ticket.acceptance_criteria:
            parts.append(f"Acceptance Criteria: {ticket.acceptance_criteria}")
        if ticket.labels:
            parts.append(f"Labels: {', '.join(ticket.labels)}")
        parts.append("")
    parts.append("</jira_context>")
    return "\n".join(parts)
