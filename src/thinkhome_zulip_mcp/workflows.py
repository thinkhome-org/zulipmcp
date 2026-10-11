"""Bounded workflows using the shared executor and current caller permissions."""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.store import AuthenticationError


async def execute_batch(
    executor: Executor,
    current_principal: Callable[[], Principal],
    operations: list[dict[str, Any]],
) -> OperationResult:
    request_id = str(uuid4())
    if not 1 <= len(operations) <= 50 or any(
        not isinstance(item, dict)
        or set(item) != {"operation_id", "arguments"}
        or not isinstance(item["operation_id"], str)
        or not item["operation_id"]
        or not isinstance(item["arguments"], dict)
        for item in operations
    ):
        return OperationResult(request_id, error={"code": "invalid_batch"})
    results: list[dict[str, Any]] = []
    completed = 0
    error = None
    uncertain = False
    for index, item in enumerate(operations):
        try:
            principal = current_principal()
        except AuthenticationError:
            error = {"code": "authentication_required"}
            break
        result = await executor.execute(
            principal, item["operation_id"], item["arguments"]
        )
        results.append({"index": index, "result": asdict(result)})
        uncertain = uncertain or result.uncertain_write
        if result.error is not None:
            error = {"code": "batch_stopped"}
            break
        completed += 1
    return OperationResult(
        request_id,
        data={
            "results": results,
            "completed": completed,
            "not_attempted": list(range(len(results), len(operations))),
        },
        error=error,
        uncertain_write=uncertain,
    )


async def history_page(
    executor: Executor,
    principal: Principal,
    narrow: list[Any],
    *,
    cursor: int | None = None,
    direction: str = "older",
    limit: int = 100,
) -> OperationResult:
    if (
        direction not in {"older", "newer"}
        or type(limit) is not int
        or not 1 <= limit <= 200
        or (cursor is not None and (type(cursor) is not int or cursor < 1))
    ):
        return OperationResult(str(uuid4()), error={"code": "invalid_history_request"})
    older = direction == "older"
    result = await executor.execute(
        principal,
        "get-messages",
        {
            "anchor": str(cursor)
            if cursor is not None
            else "newest"
            if older
            else "oldest",
            "include_anchor": False,
            "num_before": limit if older else 0,
            "num_after": 0 if older else limit,
            "narrow": narrow,
        },
    )
    if result.error:
        return result
    data = result.data
    messages = data.get("messages") if isinstance(data, dict) else None
    exhausted = (
        data.get("found_oldest" if older else "found_newest")
        if isinstance(data, dict)
        else None
    )
    valid = isinstance(messages, list) and len(messages) <= limit
    ids: list[int] = []
    if isinstance(messages, list):
        for message in messages:
            mid = message.get("id") if isinstance(message, dict) else None
            if type(mid) is not int or mid <= 0:
                valid = False
                break
            ids.append(mid)
    if valid:
        valid = all(left < right for left, right in zip(ids, ids[1:], strict=False))
        if cursor is not None:
            valid = valid and all(
                mid < cursor if older else mid > cursor for mid in ids
            )
    traversal = ids
    if executor.channel_policy.active:
        page = data.get("policy_page") if isinstance(data, dict) else None
        if not isinstance(page, dict) or type(page.get("count")) is not int:
            valid = False
        else:
            count, first, last = (
                page["count"],
                page.get("first_id"),
                page.get("last_id"),
            )
            if count == 0:
                valid = valid and not ids and first is None and last is None
                traversal = []
            elif (
                not len(ids) <= count <= limit
                or type(first) is not int
                or type(last) is not int
                or first < 1
                or last < first
                or (count == 1) != (first == last)
            ):
                valid = False
            else:
                traversal = [first, last]
                valid = valid and all(first <= mid <= last for mid in ids)
                if cursor is not None:
                    valid = valid and all(
                        mid < cursor if older else mid > cursor for mid in traversal
                    )
    if not valid or type(exhausted) is not bool or (not traversal and not exhausted):
        return OperationResult(
            result.request_id, error={"code": "invalid_history_response"}
        )
    return OperationResult(
        result.request_id,
        data={
            **data,
            "next_cursor": (traversal[0] if older else traversal[-1])
            if traversal
            else cursor,
            "exhausted": exhausted,
            "direction": direction,
        },
        status_code=result.status_code,
    )
