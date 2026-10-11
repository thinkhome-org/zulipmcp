"""Caller-visible user matching, without shared directory or credential caches."""

from difflib import SequenceMatcher
from typing import Any


def resolve_users(
    users: object,
    query: str,
    *,
    mode: str = "fuzzy",
    threshold: float = 0.6,
    limit: int = 20,
    include_bots: bool = False,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return candidates; even a truncated ambiguous list stays ambiguous."""
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 256
        or mode not in {"fuzzy", "substring"}
        or not 0 <= threshold <= 1
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Invalid user resolution request")
    if not isinstance(users, list):
        raise ValueError("Invalid upstream user response")
    seen: set[int] = set()
    eligible = []
    for user in users:
        if (
            not isinstance(user, dict)
            or type(user.get("user_id")) is not int
            or user["user_id"] <= 0
            or user["user_id"] in seen
            or not isinstance(user.get("full_name"), str)
            or not isinstance(user.get("email"), str)
            or type(user.get("is_bot", False)) is not bool
            or type(user.get("is_active", True)) is not bool
            or (
                user.get("delivery_email") is not None
                and not isinstance(user["delivery_email"], str)
            )
        ):
            raise ValueError("Invalid upstream user response")
        seen.add(user["user_id"])
        if (include_bots or not user.get("is_bot", False)) and (
            include_inactive or user.get("is_active", True)
        ):
            eligible.append(user)
    q = query.strip().casefold()
    candidates: list[tuple[dict[str, Any], float]] = []
    if mode == "fuzzy":
        for user in eligible:
            name = user["full_name"].casefold().strip()
            keys = {name, user["email"].casefold()}
            if name:
                keys.add(name.split()[0])
            if user.get("delivery_email"):
                keys.add(user["delivery_email"].casefold())
            if q in keys:
                candidates.append((user, 1.0))
    if not candidates:
        candidates = [
            (user, 1.0) for user in eligible if q in user["full_name"].casefold()
        ]
    if not candidates and mode == "fuzzy":
        for user in eligible:
            name = user["full_name"].casefold().strip()
            names = [name, name.split()[0]] if name else []
            score = max(
                (
                    SequenceMatcher(None, q, key, autojunk=False).ratio()
                    for key in names
                ),
                default=0.0,
            )
            if score >= threshold:
                candidates.append((user, score))
    candidates.sort(
        key=lambda item: (-item[1], item[0]["full_name"].casefold(), item[0]["user_id"])
    )
    total = len(candidates)
    return {
        "status": "not_found"
        if not total
        else "resolved"
        if total == 1
        else "ambiguous",
        "query": query,
        "matches": [
            {
                "user_id": user["user_id"],
                "full_name": user["full_name"],
                "email": user["email"],
                "confidence": round(score, 4),
            }
            for user, score in candidates[:limit]
        ],
        "total": total,
        "truncated": total > limit,
    }
