from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolResult:
    """Tool body plus an optional error so MCP can set isError.

    Keyword-fallback text stays in ``text`` when the MiniLM folder is missing.
    """

    text: str
    error: str | None = None

    def __str__(self) -> str:
        return self.text


class ToolErrorStatus(Exception):
    """Raised after wrapping so FastMCP marks the tool call as ``isError``."""


def keyword_fallback_result(text: str, error: Exception | None, *, notice: str | None = None):
    """Keep keyword hits, but mark the reply as an error when MiniLM is missing."""
    if error is None:
        return text
    body = f"{notice}\n\n{text}" if notice else text
    return ToolResult(text=body, error=str(error))


def finalize_tool_text(result, *, redact=None, wrap=None) -> str:
    """Turn a tool body into text, raising ``ToolErrorStatus`` when needed."""
    error = None
    if isinstance(result, ToolResult):
        error = result.error
        text = result.text
    elif result is None:
        text = ""
    else:
        text = result if isinstance(result, str) else str(result)
    if redact is not None:
        text = redact(text)
    if wrap is not None:
        text = wrap(text)
    if error:
        raise ToolErrorStatus(text)
    return text
