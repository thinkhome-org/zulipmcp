"""Static route evidence extraction; never imports upstream source files."""

import ast
import hashlib
from pathlib import Path
from typing import Any

from thinkhome_zulip_mcp.catalog import HTTP_METHODS

ROUTE_FUNCTIONS = {"path", "re_path", "rest_path", "remote_server_path"}


def extract_routes(source: str, source_path: str) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=source_path)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    imports = {
        alias.asname or alias.name: f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    records = []
    for node in sorted(ast.walk(tree), key=lambda item: getattr(item, "lineno", 0)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ROUTE_FUNCTIONS or not node.args:
            continue
        conditions, group = [], None
        child: ast.AST = node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.If):
                condition = ast.unparse(parent.test)
                if child in parent.orelse:
                    condition = f"not ({condition})"
                conditions.append(condition)
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                target = (
                    parent.targets[0]
                    if isinstance(parent, ast.Assign)
                    else parent.target
                )
                group = ast.unparse(target)
            child = parent
        pattern = node.args[0]
        base = {
            "source_file": source_path,
            "source_line": node.lineno,
            "constructor": node.func.id,
            "group": group,
            "conditions": list(reversed(conditions)),
            "pattern": pattern.value
            if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
            else None,
            "pattern_expression": ast.unparse(pattern),
            "review_status": "pending",
        }
        methods: list[tuple[str | None, ast.expr]] = [
            (kw.arg, kw.value)
            for kw in node.keywords
            if kw.arg and kw.arg.lower() in HTTP_METHODS
        ]
        if not methods:
            methods = [
                (None, node.args[1] if len(node.args) > 1 else ast.Constant(None))
            ]
        for method, handler in methods:
            flags = []
            if isinstance(handler, ast.Tuple) and len(handler.elts) == 2:
                flag_node = handler.elts[1]
                flags = sorted(ast.literal_eval(flag_node))
                handler = handler.elts[0]
            handler_name = ast.unparse(handler)
            handler_name = imports.get(handler_name, handler_name)
            is_include = (
                isinstance(handler, ast.Call) and ast.unparse(handler.func) == "include"
            )
            records.append(
                {
                    **base,
                    "method": method,
                    "handler": handler_name,
                    "flags": flags,
                    "kind": "include" if is_include else "route",
                }
            )
    return records


def build_inventory(source_root: Path, source_paths: list[str]) -> dict[str, Any]:
    source_files = {}
    routes = []
    for source_path in sorted(source_paths):
        content = (source_root / source_path).read_bytes()
        source_files[source_path] = {"sha256": hashlib.sha256(content).hexdigest()}
        routes.extend(extract_routes(content.decode("utf-8"), source_path))
    handlers = {}
    for handler in sorted({route["handler"] for route in routes}):
        module, _, function_name = handler.rpartition(".")
        if not module or not all(part.isidentifier() for part in handler.split(".")):
            continue
        source_path = module.replace(".", "/") + ".py"
        handler_file = source_root / source_path
        if not handler_file.is_file():
            continue
        content = handler_file.read_bytes()
        tree = ast.parse(content, filename=source_path)
        function = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ),
            None,
        )
        if function is None:
            continue
        source_files[source_path] = {"sha256": hashlib.sha256(content).hexdigest()}
        handlers[handler] = {
            "source_file": source_path,
            "source_line": function.lineno,
            "source_end_line": function.end_lineno,
            "decorators": [ast.unparse(d) for d in function.decorator_list],
            "parameters": extract_parameters(function.args),
            "review_status": "pending",
        }
    return {"source_files": source_files, "routes": routes, "handlers": handlers}


def extract_parameters(arguments: ast.arguments) -> list[dict[str, Any]]:
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(arguments.defaults)
    ) + list(arguments.defaults)
    parameters = []
    for arg, default, kind in [
        *(
            (arg, default, "positional")
            for arg, default in zip(positional, defaults, strict=True)
        ),
        *(
            (arg, default, "keyword_only")
            for arg, default in zip(
                arguments.kwonlyargs, arguments.kw_defaults, strict=True
            )
        ),
        *(
            (arg, None, kind)
            for arg, kind in [
                (arguments.vararg, "var_positional"),
                (arguments.kwarg, "var_keyword"),
            ]
            if arg is not None
        ),
    ]:
        parameters.append(
            {
                "name": arg.arg,
                "kind": kind,
                "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
                "default": ast.unparse(default) if default is not None else None,
            }
        )
    return parameters
