from __future__ import annotations

from services.openai_backend_api import SEARCH_MODEL
from services.protocol.search_execution import execute_search

MODEL = SEARCH_MODEL


def handle(body: dict[str, object]) -> dict[str, object]:
    result, account = execute_search(str(body["prompt"]))
    result["_account_email"] = str(account.get("email") or "")
    return result
