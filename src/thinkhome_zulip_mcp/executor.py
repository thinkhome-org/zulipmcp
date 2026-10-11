"""Validated catalog requests under an immutable, provisioned Zulip identity."""

import asyncio
import json
import math
import os
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import httpx
from jsonschema import Draft7Validator
from referencing import Registry

from thinkhome_zulip_mcp.catalog import (
    Operation,
    load_catalog,
    load_document,
    resolve_reference,
)
from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.files import FileAccessError, FileStore


@dataclass(frozen=True)
class Principal:
    identity_id: str
    realm_url: str
    email: str
    api_key: str = field(repr=False)
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if any(ord(char) < 33 for char in self.realm_url) or "\\" in self.realm_url:
            raise ValueError("Invalid provisioned Zulip URL")
        parsed = urlsplit(self.realm_url)
        _ = parsed.port
        if (
            parsed.scheme
            not in ({"https", "http"} if self.allow_insecure_http else {"https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not self.identity_id
            or not self.email
            or not self.api_key
        ):
            raise ValueError("Invalid provisioned Zulip identity or URL")


@dataclass(frozen=True)
class OperationResult:
    request_id: str
    data: Any = None
    error: dict[str, Any] | None = None
    status_code: int | None = None
    uncertain_write: bool = False
    retry_after: str | None = None


def _normalize_schema(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "$ref" in value and not str(value["$ref"]).startswith("#/"):
        raise ValueError("External schema references are not allowed")
    normalized = deepcopy(value)
    nullable = normalized.pop("nullable", False)
    for key in ("properties", "patternProperties", "definitions", "$defs"):
        if key in normalized:
            normalized[key] = {
                name: _normalize_schema(v) for name, v in normalized[key].items()
            }
    for key in ("allOf", "anyOf", "oneOf"):
        if key in normalized:
            normalized[key] = [_normalize_schema(v) for v in normalized[key]]
    for key in (
        "items",
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "contains",
    ):
        if key in normalized:
            v = normalized[key]
            normalized[key] = (
                [_normalize_schema(item) for item in v]
                if isinstance(v, list)
                else _normalize_schema(v)
            )
    if nullable:
        if "type" in normalized:
            normalized["type"] = [normalized["type"], "null"]
        else:
            normalized = {"anyOf": [normalized, {"type": "null"}]}
    for name, bound in [
        ("exclusiveMinimum", "minimum"),
        ("exclusiveMaximum", "maximum"),
    ]:
        if isinstance(normalized.get(name), bool):
            exclusive = normalized.pop(name)
            if exclusive and bound in normalized:
                normalized[name] = normalized.pop(bound)
    return normalized


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _wire_value(value: Any, json_encoded: bool) -> str:
    if json_encoded:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or isinstance(value, float) and math.isfinite(value):
        return str(value)
    raise ValueError("Input requires a declared JSON encoding")


class Executor:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = 8 * 1024 * 1024,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        files: FileStore | None = None,
        read_only: bool | None = None,
        channel_policy: ChannelPolicy | None = None,
    ) -> None:
        if read_only is None:
            setting = os.environ.get("ZULIPCHAT_READ_ONLY", "false").lower()
            if setting not in {"true", "false", "1", "0"}:
                raise ValueError("ZULIPCHAT_READ_ONLY must be true, false, 1 or 0")
            read_only = setting in {"true", "1"}
        self.read_only = read_only
        self.channel_policy = channel_policy or ChannelPolicy.from_environment(
            os.environ
        )
        if max_response_bytes <= 0:
            raise ValueError("Response limit must be positive")
        self.document = load_document()
        self.catalog = load_catalog()
        self.components = deepcopy(self.document["components"])
        self.components["schemas"] = {
            name: _normalize_schema(schema)
            for name, schema in self.components["schemas"].items()
        }
        self.files = files
        self.sleep = sleep
        self.transport = transport
        self.max_response_bytes = max_response_bytes

    def _validate(self, schema: dict[str, Any], value: Any) -> None:
        full_schema = {**_normalize_schema(schema), "components": self.components}
        errors = Draft7Validator(full_schema, registry=Registry()).iter_errors(value)
        error = next(errors, None)
        if error:
            location = ".".join(str(part) for part in error.absolute_path) or "input"
            raise ValueError(f"Invalid value at {location} ({error.validator})")

    def _prepare(
        self, operation: Operation, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, str], dict[str, str], Any, bool, dict[str, str]]:
        json.dumps(arguments, allow_nan=False)
        definition = operation.definition
        path = operation.path
        parameters = {}
        for raw in operation.path_parameters + definition.get("parameters", []):
            parameter = resolve_reference(self.document, raw)
            parameters[(parameter["in"], parameter["name"])] = parameter
        body = resolve_reference(self.document, definition.get("requestBody", {}))
        content_types = body.get("content", {})
        multipart = content_types.get("multipart/form-data", {})
        multipart_properties = resolve_reference(
            self.document, multipart.get("schema", {})
        ).get("properties", {})
        binary_fields = {
            name
            for name, schema in multipart_properties.items()
            if resolve_reference(self.document, schema).get("format") == "binary"
        }
        prefer_multipart = bool(binary_fields & arguments.keys())
        media_order = ["application/x-www-form-urlencoded", "application/json"]
        media_order.insert(
            0 if prefer_multipart else len(media_order), "multipart/form-data"
        )
        media = next(
            (m for m in media_order if m in content_types),
            None,
        )
        if content_types and media is None:
            raise NotImplementedError(
                "This operation requires a binary transfer adapter"
            )
        content = content_types[media] if media else {}
        body_schema = resolve_reference(self.document, content.get("schema", {}))
        properties = body_schema.get("properties", {})
        allowed = set(properties) | {p["name"] for p in parameters.values()}
        if set(arguments) - allowed:
            raise ValueError("Unknown operation arguments")
        query = {}
        for parameter in parameters.values():
            name = parameter["name"]
            if name not in arguments:
                if parameter.get("required"):
                    raise ValueError(f"Missing required argument: {name}")
                continue
            value = arguments[name]
            encoded = "application/json" in parameter.get("content", {})
            schema = (
                parameter["content"]["application/json"]["schema"]
                if encoded
                else parameter["schema"]
            )
            self._validate(schema, value)
            if parameter["in"] == "path":
                # These routes use <path:filename> in Zulip 12.3 zproject/urls.py.
                is_file_path = (
                    operation.operation_id
                    in {"get-file-temporary-url", "check-thumbnail-status"}
                    and name == "filename"
                )
                segments = str(value).split("/") if is_file_path else [str(value)]
                for segment in segments:
                    decoded = segment
                    for _ in range(4):
                        decoded = unquote(decoded)
                    if (
                        not decoded
                        or decoded in {".", ".."}
                        or any(c in decoded for c in "/\\\x00")
                    ):
                        raise ValueError("Unsafe path parameter")
                encoded_path = "/".join(quote(part, safe="") for part in segments)
                path = path.replace("{" + name + "}", encoded_path)
            elif parameter["in"] == "query":
                query[name] = _wire_value(value, encoded)
            else:
                raise NotImplementedError("Unsupported parameter location")
        if "{" in path or "}" in path:
            raise ValueError("Missing path parameter")
        values = {key: value for key, value in arguments.items() if key in properties}
        form = {}
        file_handles = {}
        json_body = None
        if content:
            self._validate(body_schema, values)
            if media == "multipart/form-data":
                if self.files is None:
                    raise NotImplementedError("File staging is not configured")
                file_handles = {
                    name: values.pop(name) for name in binary_fields if name in values
                }
                if not file_handles:
                    raise ValueError("At least one staged file is required")
            if media == "application/json":
                json_body = values
            else:
                encoding = content.get("encoding", {})
                form = {
                    key: _wire_value(
                        value,
                        encoding.get(key, {}).get("contentType") == "application/json",
                    )
                    for key, value in values.items()
                }
        authenticated = (
            definition.get("security", self.document.get("security", [])) != []
        )
        return path, query, form, json_body, authenticated, file_handles

    async def _channel_visible(
        self, principal: Principal, stream_id: int
    ) -> bool | None:
        lookup = await self._execute(
            principal, "get-stream-by-id", {"stream_id": stream_id}
        )
        stream = lookup.data.get("stream") if isinstance(lookup.data, dict) else None
        if (
            lookup.error
            or not isinstance(stream, dict)
            or type(stream.get("stream_id")) is not int
            or stream["stream_id"] != stream_id
            or not isinstance(stream.get("name"), str)
            or type(stream.get("invite_only")) is not bool
        ):
            return None
        return self.channel_policy.allows_channel(
            stream["name"], private=stream["invite_only"]
        )

    async def execute(
        self, principal: Principal, operation_id: str, arguments: dict[str, Any]
    ) -> OperationResult:
        if not self.channel_policy.active:
            return await self._execute(principal, operation_id, arguments)
        if operation_id == "delete-queue":
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                expired = (
                    isinstance(result.data, dict)
                    and result.data.get("code") == "BAD_EVENT_QUEUE_ID"
                )
                return replace(
                    result, data={"code": "BAD_EVENT_QUEUE_ID"} if expired else None
                )
            return replace(result, data={"result": "success"})
        if operation_id == "register-queue":
            if self.read_only:
                return await self._execute(principal, operation_id, arguments)
            requested = arguments.get("event_types")
            initial = arguments.get("fetch_event_types", requested)
            if any(
                not isinstance(types, list) or any(kind != "message" for kind in types)
                for types in (requested, initial)
            ):
                return OperationResult(
                    str(uuid4()), error={"code": "channel_policy_not_supported"}
                )
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                return replace(result, data=None)
            data = result.data
            queue_id = data.get("queue_id") if isinstance(data, dict) else None
            cursor = data.get("last_event_id") if isinstance(data, dict) else None
            if (
                not isinstance(queue_id, str)
                or not queue_id
                or type(cursor) is not int
                or cursor < -1
            ):
                uncertain = True
                if isinstance(queue_id, str) and queue_id:
                    cleanup = await self._execute(
                        principal, "delete-queue", {"queue_id": queue_id}
                    )
                    uncertain = cleanup.error is not None
                return replace(
                    result,
                    data=None,
                    error={"code": "invalid_queue_response"},
                    uncertain_write=uncertain,
                )
            metadata_types = {
                "queue_id": str,
                "last_event_id": int,
                "max_message_id": int,
                "zulip_version": str,
                "zulip_feature_level": int,
                "zulip_merge_base": str,
                "idle_queue_timeout_secs": int,
            }
            return replace(
                result,
                data={
                    "result": "success",
                    **{
                        key: data[key]
                        for key, expected in metadata_types.items()
                        if key in data and type(data[key]) is expected
                    },
                },
            )
        if operation_id == "get-events":
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                expired = (
                    isinstance(result.data, dict)
                    and result.data.get("code") == "BAD_EVENT_QUEUE_ID"
                )
                return replace(
                    result, data={"code": "BAD_EVENT_QUEUE_ID"} if expired else None
                )
            events = (
                result.data.get("events") if isinstance(result.data, dict) else None
            )
            if not isinstance(events, list):
                return replace(
                    result, data=None, error={"code": "invalid_events_response"}
                )
            cursor = arguments.get("last_event_id", -1)
            visible = []
            event_channels: dict[int, bool] = {}
            try:
                async with asyncio.timeout(30):
                    for event in events:
                        if (
                            not isinstance(event, dict)
                            or type(event.get("id")) is not int
                            or event["id"] < 0
                            or event["id"] <= cursor
                        ):
                            return replace(
                                result,
                                data=None,
                                error={"code": "invalid_events_response"},
                            )
                        cursor = event["id"]
                        if event.get("type") == "heartbeat":
                            visible.append({"id": cursor, "type": "heartbeat"})
                            continue
                        if event.get("type") != "message":
                            return replace(
                                result,
                                data=None,
                                error={"code": "channel_policy_event_not_supported"},
                            )
                        message = event.get("message")
                        if (
                            not isinstance(message, dict)
                            or type(message.get("id")) is not int
                            or message["id"] <= 0
                        ):
                            return replace(
                                result,
                                data=None,
                                error={"code": "invalid_events_response"},
                            )
                        allowed = False
                        if message.get("type") in {"private", "direct"}:
                            allowed = self.channel_policy.allows_dm()
                        elif message.get("type") == "stream":
                            stream_id = message.get("stream_id")
                            if type(stream_id) is not int or stream_id <= 0:
                                return replace(
                                    result,
                                    data=None,
                                    error={"code": "invalid_events_response"},
                                )
                            if stream_id not in event_channels:
                                decision = await self._channel_visible(
                                    principal, stream_id
                                )
                                if decision is None:
                                    return replace(
                                        result,
                                        data=None,
                                        error={"code": "channel_policy_lookup_failed"},
                                    )
                                event_channels[stream_id] = decision
                            allowed = event_channels[stream_id]
                        if allowed:
                            visible.append(event)
            except TimeoutError:
                return replace(
                    result, data=None, error={"code": "channel_policy_lookup_timeout"}
                )
            return replace(
                result,
                data={
                    "result": "success",
                    "events": visible,
                    "policy_event_cursor": cursor,
                },
            )
        if operation_id == "get-messages":
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                return replace(result, data=None)
            data = result.data
            messages = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(messages, list) or any(
                not isinstance(message, dict)
                or type(message.get("id")) is not int
                or message["id"] <= 0
                for message in messages
            ):
                return replace(
                    result, data=None, error={"code": "invalid_upstream_response"}
                )
            ids = [message["id"] for message in messages]
            if any(a >= b for a, b in zip(ids, ids[1:], strict=False)):
                return replace(
                    result, data=None, error={"code": "invalid_upstream_response"}
                )
            channels: dict[int, bool] = {}
            visible = []
            try:
                async with asyncio.timeout(30):
                    for message in messages:
                        if message.get("type") in {"direct", "private"}:
                            if self.channel_policy.allows_dm():
                                visible.append(message)
                        elif message.get("type") == "stream":
                            stream_id = message.get("stream_id")
                            if type(stream_id) is not int or stream_id <= 0:
                                return replace(
                                    result,
                                    data=None,
                                    error={"code": "invalid_upstream_response"},
                                )
                            if stream_id not in channels:
                                decision = await self._channel_visible(
                                    principal, stream_id
                                )
                                if decision is None:
                                    return replace(
                                        result,
                                        data=None,
                                        error={"code": "channel_policy_lookup_failed"},
                                    )
                                channels[stream_id] = decision
                            if channels[stream_id]:
                                visible.append(message)
            except TimeoutError:
                return replace(
                    result, data=None, error={"code": "channel_policy_lookup_timeout"}
                )
            metadata = {
                key: data[key]
                for key in (
                    "found_oldest",
                    "found_newest",
                    "found_anchor",
                    "history_limited",
                )
                if key in data and type(data[key]) is bool
            }
            return replace(
                result,
                data={
                    "result": "success",
                    "messages": visible,
                    **metadata,
                    "policy_page": {
                        "first_id": ids[0] if ids else None,
                        "last_id": ids[-1] if ids else None,
                        "count": len(ids),
                    },
                },
            )
        if operation_id in {
            "update-message",
            "delete-message",
            "add-reaction",
            "remove-reaction",
        }:
            if self.read_only:
                return await self._execute(principal, operation_id, arguments)
            try:
                self._prepare(self.catalog[operation_id], arguments)
            except (ValueError, TypeError) as exc:
                return OperationResult(
                    str(uuid4()),
                    error={"code": "invalid_arguments", "message": str(exc)},
                )
            source = await self.execute(
                principal, "get-message", {"message_id": arguments["message_id"]}
            )
            if source.error:
                return source
            message = source.data["message"]
            stream_ids = set()
            if message["type"] == "stream":
                stream_ids.add(message["stream_id"])
            if "stream_id" in arguments:
                stream_ids.add(arguments["stream_id"])
            for stream_id in stream_ids:
                channel = await self.execute(
                    principal, "get-stream-by-id", {"stream_id": stream_id}
                )
                if channel.error:
                    return channel
                stream = channel.data["stream"]
                if not self.channel_policy.allows_channel(
                    stream["name"], private=stream["invite_only"], write=True
                ):
                    return OperationResult(
                        str(uuid4()), error={"code": "channel_policy_denied"}
                    )
            return await self._execute(principal, operation_id, arguments)
        if operation_id == "get-message":
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                return replace(result, data=None)
            message = (
                result.data.get("message") if isinstance(result.data, dict) else None
            )
            if (
                not isinstance(message, dict)
                or type(message.get("id")) is not int
                or message["id"] != arguments["message_id"]
            ):
                return replace(
                    result, data=None, error={"code": "invalid_upstream_response"}
                )
            allowed = False
            if message.get("type") in {"private", "direct"}:
                allowed = self.channel_policy.allows_dm()
            elif message.get("type") == "stream":
                stream_id = message.get("stream_id")
                if type(stream_id) is int and stream_id > 0:
                    channel = await self.execute(
                        principal, "get-stream-by-id", {"stream_id": stream_id}
                    )
                    allowed = channel.error is None
            if not allowed:
                return replace(
                    result, data=None, error={"code": "channel_policy_denied"}
                )
            return replace(result, data={"result": "success", "message": message})
        if operation_id == "get-user-channels":
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                return replace(result, data=None)
            channel_ids = (
                result.data.get("subscribed_channel_ids")
                if isinstance(result.data, dict)
                else None
            )
            if (
                not isinstance(channel_ids, list)
                or any(type(value) is not int or value <= 0 for value in channel_ids)
                or len(set(channel_ids)) != len(channel_ids)
            ):
                return replace(
                    result, data=None, error={"code": "invalid_upstream_response"}
                )
            allowed_ids = []
            try:
                async with asyncio.timeout(30):
                    for stream_id in channel_ids:
                        decision = await self._channel_visible(principal, stream_id)
                        if decision is None:
                            return replace(
                                result,
                                data=None,
                                error={"code": "channel_policy_lookup_failed"},
                            )
                        if decision:
                            allowed_ids.append(stream_id)
            except TimeoutError:
                return replace(
                    result, data=None, error={"code": "channel_policy_lookup_timeout"}
                )
            return replace(
                result,
                data={"result": "success", "subscribed_channel_ids": allowed_ids},
            )
        if operation_id in {"get-streams", "get-subscriptions"}:
            collection = "streams" if operation_id == "get-streams" else "subscriptions"
            result = await self._execute(principal, operation_id, arguments)
            if result.error:
                return replace(result, data=None)
            streams = (
                result.data.get(collection) if isinstance(result.data, dict) else None
            )
            if not isinstance(streams, list) or any(
                not isinstance(stream, dict)
                or type(stream.get("stream_id")) is not int
                or stream["stream_id"] <= 0
                or not isinstance(stream.get("name"), str)
                or type(stream.get("invite_only")) is not bool
                for stream in streams
            ):
                return replace(
                    result, data=None, error={"code": "invalid_upstream_response"}
                )
            return replace(
                result,
                data={
                    "result": "success",
                    collection: [
                        stream
                        for stream in streams
                        if self.channel_policy.allows_channel(
                            stream["name"], private=stream["invite_only"]
                        )
                    ],
                },
            )
        if operation_id not in {
            "send-message",
            "get-stream-id",
            "get-stream-by-id",
            "get-stream-topics",
            "get-subscribers",
            "get-subscription-status",
        }:
            # shortcut: deny other paths until their channel guards are implemented.
            return OperationResult(
                str(uuid4()), error={"code": "channel_policy_not_supported"}
            )
        if self.read_only and operation_id == "send-message":
            return await self._execute(principal, operation_id, arguments)
        try:
            self._prepare(self.catalog[operation_id], arguments)
        except (ValueError, TypeError) as exc:
            return OperationResult(
                str(uuid4()), error={"code": "invalid_arguments", "message": str(exc)}
            )
        denied = OperationResult(str(uuid4()), error={"code": "channel_policy_denied"})
        if operation_id == "send-message" and arguments["type"] in {
            "direct",
            "private",
        }:
            if not self.channel_policy.allows_dm():
                return denied
            return await self._execute(principal, operation_id, arguments)
        target = arguments[
            "to"
            if operation_id == "send-message"
            else "stream"
            if operation_id == "get-stream-id"
            else "stream_id"
        ]
        if isinstance(target, list):
            if len(target) != 1:
                return denied
            target = target[0]
        if isinstance(target, str):
            lookup = await self._execute(principal, "get-stream-id", {"stream": target})
            if lookup.error or not isinstance(lookup.data, dict):
                return denied
            target = lookup.data.get("stream_id")
        if type(target) is not int or target <= 0:
            return denied
        lookup = await self._execute(
            principal, "get-stream-by-id", {"stream_id": target}
        )
        if lookup.error or not isinstance(lookup.data, dict):
            return denied
        stream = lookup.data.get("stream")
        if (
            not isinstance(stream, dict)
            or type(stream.get("stream_id")) is not int
            or stream["stream_id"] != target
            or not isinstance(stream.get("name"), str)
            or type(stream.get("invite_only")) is not bool
        ):
            return denied
        if not self.channel_policy.allows_channel(
            stream["name"],
            private=stream["invite_only"],
            write=operation_id == "send-message",
        ):
            return denied
        if operation_id == "get-stream-id":
            return replace(lookup, data={"result": "success", "stream_id": target})
        if operation_id == "get-stream-by-id":
            return replace(lookup, data={"result": "success", "stream": stream})
        if operation_id != "send-message":
            return await self._execute(principal, operation_id, arguments)
        # Bind the send to the checked ID, even if a name is renamed/reassigned.
        return await self._execute(principal, operation_id, {**arguments, "to": target})

    async def _execute(
        self, principal: Principal, operation_id: str, arguments: dict[str, Any]
    ) -> OperationResult:
        request_id = str(uuid4())
        operation = self.catalog.get(operation_id)
        if operation is None:
            return OperationResult(request_id, error={"code": "unknown_operation"})
        if self.read_only and (
            operation.method not in {"GET", "HEAD", "OPTIONS"}
            or operation.definition.get("x-thinkhome", {}).get("safe_read") is False
            or operation_id
            in {
                "get-events",
                "get-stream-email-address",
                "create-big-blue-button-video-call",
                "summarize-messages",
            }
        ):
            return OperationResult(request_id, error={"code": "read_only_policy"})
        if operation.path.startswith(("/dev_", "/remotes/")) or operation_id in {
            "fetch-api-key",
            "jwt-fetch-api-key",
        }:
            return OperationResult(
                request_id, error={"code": "requires_separate_authentication_adapter"}
            )
        try:
            path, query, form, json_body, authenticated, file_handles = self._prepare(
                operation, arguments
            )
        except (ValueError, TypeError) as exc:
            return OperationResult(
                request_id, error={"code": "invalid_arguments", "message": str(exc)}
            )
        except NotImplementedError as exc:
            return OperationResult(
                request_id, error={"code": "unsupported_operation", "message": str(exc)}
            )
        safe_read = operation.definition.get("x-thinkhome", {}).get("safe_read") is True
        for attempt in range(3):
            result = await self._send(
                request_id,
                principal,
                operation,
                path,
                query,
                form,
                json_body,
                authenticated,
                file_handles,
            )
            retryable = result.status_code in {429, 502, 503, 504} or (
                result.error is not None
                and result.error["code"] == "upstream_transport_error"
            )
            if not safe_read or not retryable or attempt == 2:
                return result
            delay = float(2**attempt)
            if result.retry_after is not None:
                try:
                    delay = float(result.retry_after)
                except ValueError:
                    try:
                        date = parsedate_to_datetime(result.retry_after)
                        delay = (date - datetime.now(UTC)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        return result
                if not math.isfinite(delay) or delay > 30:
                    return result
            await self.sleep(max(0.0, delay))
        raise AssertionError("Retry loop exhausted without a result")

    async def _send(
        self,
        request_id: str,
        principal: Principal,
        operation: Operation,
        path: str,
        query: dict[str, str],
        form: dict[str, str],
        json_body: Any,
        authenticated: bool,
        file_handles: dict[str, str],
    ) -> OperationResult:
        url = principal.realm_url.rstrip("/") + "/api/v1" + path
        is_write = (
            operation.method not in {"GET", "HEAD", "OPTIONS"}
            or operation.definition.get("x-thinkhome", {}).get("safe_read") is False
        )
        try:
            with ExitStack() as stack:
                uploads = {}
                for field_name, handle in file_handles.items():
                    if self.files is None:
                        raise FileAccessError("File staging is not configured")
                    info, stream = stack.enter_context(
                        self.files.open(principal, handle)
                    )
                    uploads[field_name] = (info.filename, stream, info.content_type)
                # A client belongs to one call; cookies never cross principals.
                async with httpx.AsyncClient(
                    transport=self.transport,
                    follow_redirects=False,
                    timeout=httpx.Timeout(30),
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        operation.method,
                        url,
                        params=query,
                        data=form or None,
                        json=json_body,
                        files=uploads or None,
                        auth=httpx.BasicAuth(principal.email, principal.api_key)
                        if authenticated
                        else None,
                    ) as response:
                        if 300 <= response.status_code < 400:
                            return OperationResult(
                                request_id,
                                data={"location": response.headers.get("location")},
                                status_code=response.status_code,
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(body) + len(chunk) > self.max_response_bytes:
                                return OperationResult(
                                    request_id,
                                    error={"code": "response_too_large"},
                                    status_code=response.status_code,
                                    retry_after=response.headers.get("retry-after"),
                                    uncertain_write=is_write,
                                )
                            body.extend(chunk)
                        try:
                            data = (
                                json.loads(
                                    body,
                                    parse_float=_finite_float,
                                    parse_constant=_finite_float,
                                )
                                if body
                                else None
                            )
                        except (ValueError, UnicodeError):
                            return OperationResult(
                                request_id,
                                error={"code": "invalid_upstream_response"},
                                status_code=response.status_code,
                                retry_after=response.headers.get("retry-after"),
                                uncertain_write=is_write,
                            )
                        failed = (
                            response.is_error
                            or isinstance(data, dict)
                            and data.get("result") == "error"
                        )
                        return OperationResult(
                            request_id,
                            data=data,
                            error={"code": "upstream_error"} if failed else None,
                            uncertain_write=is_write and failed,
                            status_code=response.status_code,
                            retry_after=response.headers.get("retry-after"),
                        )
        except FileAccessError:
            return OperationResult(request_id, error={"code": "invalid_file_handle"})
        except OSError:
            return OperationResult(
                request_id, error={"code": "file_io_error"}, uncertain_write=is_write
            )
        except httpx.TransportError:
            return OperationResult(
                request_id,
                error={"code": "upstream_transport_error"},
                uncertain_write=is_write,
            )
