"""Mask secrets that appear in URL query strings of uvicorn access logs."""

from __future__ import annotations

import logging
import re
from typing import Any

_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:key|token|ticket|access_token|auth_key|api_key|authorization)=)[^&#\s\"']*",
    re.IGNORECASE,
)


def redact_query_secrets(text: str) -> str:
    return _SENSITIVE_QUERY_RE.sub(r"\1***", text)


class AccessLogRedactionFilter(logging.Filter):
    """Rewrite ``uvicorn.access`` record args so query secrets never reach handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        args: Any = record.args
        if isinstance(args, tuple):
            record.args = tuple(redact_query_secrets(a) if isinstance(a, str) else a for a in args)
        if isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        return True


def install_access_log_redaction(logger_names: tuple[str, ...] = ("uvicorn.access",)) -> None:
    for name in logger_names:
        target = logging.getLogger(name)
        if not any(isinstance(f, AccessLogRedactionFilter) for f in target.filters):
            target.addFilter(AccessLogRedactionFilter())
