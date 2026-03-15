"""Thin wrapper around Anthropic SDK for structured output via tool use.

Replaces LangChain's ChatAnthropic.with_structured_output() with direct
Anthropic API calls using tool_choice to force structured responses.
"""

import asyncio
import logging

import anthropic
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.01


async def get_structured_output(
    client: anthropic.AsyncAnthropic,
    model: str,
    system_prompt: str,
    user_message: str,
    output_schema: type[BaseModel],
    timeout: float = 120.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[BaseModel, int]:
    """Call Claude with structured output via tool use.

    Converts a Pydantic model to a tool definition, forces the model to call
    that tool, and parses the result back into the Pydantic model.

    Returns (parsed_result, total_tokens).
    """
    tool = {
        "name": "submit_result",
        "description": f"Submit your {output_schema.__name__} result",
        "input_schema": output_schema.model_json_schema(),
    }

    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=DEFAULT_TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_result"},
            ),
            timeout=timeout,
        )

        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        for block in response.content:
            if block.type == "tool_use":
                parsed = output_schema.model_validate(block.input)
                return parsed, total_tokens

        logger.warning("No tool_use block in response for %s", output_schema.__name__)
        return output_schema(), 0

    except asyncio.TimeoutError:
        logger.error("LLM call timed out after %.0fs for %s", timeout, output_schema.__name__)
        return output_schema(), 0
    except anthropic.APIError as exc:
        logger.error("Anthropic API error for %s: %s", output_schema.__name__, exc)
        return output_schema(), 0


def create_client(api_key: str) -> anthropic.AsyncAnthropic:
    """Create an Anthropic async client."""
    return anthropic.AsyncAnthropic(api_key=api_key)
