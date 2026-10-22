"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Normalize credential fields supplied by external account import adapters.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

_CREDENTIAL_CONTAINERS = ("credentials", "credential", "tokens", "auth")
_CREDENTIAL_ALIASES = {
    "access_token": ("access_token", "accessToken", "token"),
    "refresh_token": ("refresh_token", "refreshToken"),
    "id_token": ("id_token", "idToken"),
}


def _container(raw: Mapping, key: str) -> Mapping | None:
    value = raw.get(key)
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def extract_import_credentials(raw: object) -> dict[str, str]:
    """Return the supported AT/RT/ID Token fields from an import record.

    External systems expose credentials either at the record root or in a
    nested credential object (possibly JSON-encoded). Only credential fields
    owned by the upstream account are copied.
    """
    if not isinstance(raw, Mapping):
        return {}

    sources: list[Mapping] = []
    for key in _CREDENTIAL_CONTAINERS:
        nested = _container(raw, key)
        if nested is not None:
            sources.append(nested)
    sources.append(raw)

    result: dict[str, str] = {}
    for target, aliases in _CREDENTIAL_ALIASES.items():
        for source in sources:
            value = next(
                (
                    str(source.get(alias) or "").strip()
                    for alias in aliases
                    if str(source.get(alias) or "").strip()
                ),
                "",
            )
            if value:
                result[target] = value
                break
    return result
