"""Channel rules shared by API guards; metadata must come from the caller's realm."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass


def _boolean(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{key} must be true, false, 1 or 0")
    return value in {"true", "1"}


def _areas(value: str) -> frozenset[int]:
    areas: set[int] = set()
    for part in value.split(","):
        if not part.strip():
            continue
        match = re.fullmatch(r"\s*([0-9]{1,2})(?:\s*-\s*([0-9]{1,2}))?\s*", part)
        if match is None:
            raise ValueError("JD areas must be numbers or ranges between 0 and 99")
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if last < first:
            raise ValueError("JD area range must be ascending")
        areas.update(range(first, last + 1))
    return frozenset(areas)


def _names(value: str) -> frozenset[str]:
    return frozenset(name.strip() for name in value.split(",") if name.strip())


def _allowlist(value: str, *, empty_all: bool) -> frozenset[str] | None:
    value = value.strip()
    if not value:
        return None if empty_all else frozenset()
    if value.startswith(("[", '"', "{")):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError("Invalid channel allowlist JSON") from exc
        if isinstance(parsed, str):
            parsed = [parsed]
        if not isinstance(parsed, list) or any(not isinstance(n, str) for n in parsed):
            raise ValueError("Channel allowlist must contain strings")
        names = frozenset(n.strip().lower() for n in parsed if n.strip())
    else:
        names = frozenset(n.lower() for n in _names(value))
    return None if "__all__" in names else names


@dataclass(frozen=True)
class ChannelPolicy:
    jd_enabled: bool = False
    allow_areas: frozenset[int] = frozenset()
    deny_areas: frozenset[int] = frozenset()
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    exclude_non_jd: bool = True
    exclude_dms: bool = True
    exclude_private: bool = True
    private_allowlist: frozenset[str] | None = None
    write_allowlist: frozenset[str] | None = None

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "ChannelPolicy":
        return cls(
            jd_enabled=_boolean(env, "ZULIPCHAT_CHANNEL_FILTER_ENABLED", False),
            allow_areas=_areas(env.get("ZULIPCHAT_JD_ALLOW_AREAS", "")),
            deny_areas=_areas(env.get("ZULIPCHAT_JD_DENY_AREAS", "")),
            include=_names(env.get("ZULIPCHAT_CHANNEL_INCLUDE", "")),
            exclude=_names(env.get("ZULIPCHAT_CHANNEL_EXCLUDE", "")),
            exclude_non_jd=_boolean(env, "ZULIPCHAT_EXCLUDE_NON_JD", True),
            exclude_dms=_boolean(env, "ZULIPCHAT_EXCLUDE_DMS", True),
            exclude_private=_boolean(env, "ZULIPCHAT_EXCLUDE_PRIVATE", True),
            private_allowlist=(
                _allowlist(env["BOT_ALLOWED_PRIVATE_STREAMS"], empty_all=False)
                if "BOT_ALLOWED_PRIVATE_STREAMS" in env
                else None
            ),
            write_allowlist=_allowlist(
                env.get("BOT_ALLOWED_WRITE_STREAMS", ""), empty_all=True
            ),
        )

    @property
    def active(self) -> bool:
        return (
            self.jd_enabled
            or self.private_allowlist is not None
            or self.write_allowlist is not None
        )

    def allows_dm(self) -> bool:
        return not (self.jd_enabled and self.exclude_dms)

    def allows_channel(self, name: str, *, private: bool, write: bool = False) -> bool:
        if private and self.private_allowlist is not None:
            if name.lower() not in self.private_allowlist:
                return False
        if write and self.write_allowlist is not None:
            if name.lower() not in self.write_allowlist:
                return False
        if not self.jd_enabled:
            return True
        if private and self.exclude_private or name in self.exclude:
            return False
        if name in self.include:
            return True
        match = re.match(r"^([0-9]{2})(?:\.[0-9]{2})?\s", name)
        if match is None:
            return not self.exclude_non_jd
        area = int(match[1])
        return area not in self.deny_areas and (
            not self.allow_areas or area in self.allow_areas
        )
