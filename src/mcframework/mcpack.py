"""Parser and line-preserving editor for the Mcpack text metadata format."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import keyword
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import MetadataError

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")
_PROVIDERS = {"modrinth", "curseforge", "local", "direct_url"}


@dataclass(frozen=True)
class McpackLine:
    line_id: str
    line_number: int
    expression: str
    expression_start: int
    expression_end: int
    record: dict[str, Any] | None
    section: dict[str, Any] | None = None


@dataclass(frozen=True)
class McpackDocument:
    source: str
    text: str
    lines: tuple[McpackLine, ...]

    @classmethod
    def parse(cls, content: bytes | str, source: str | Path = "<memory>") -> McpackDocument:
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MetadataError(f"{source}: Mcpack file is not UTF-8") from exc
        else:
            text = content
        source = str(source)
        parsed: list[McpackLine] = []
        sections: set[str] = set()
        current_section: str | None = None
        section_order = 0
        for number, physical in enumerate(text.splitlines(keepends=True), 1):
            body = physical.rstrip("\r\n")
            start = len(body) - len(body.lstrip())
            meaningful = _without_comment(body[start:]).rstrip()
            end = start + len(meaningful)
            expr = body[start:end]
            record = None
            section = None
            if expr.startswith("#"):
                section_order += 1
                section = _section(expr[1:], source, number, section_order)
                current_section = section["id"]
                if current_section in sections:
                    raise MetadataError(f"{source}:{number}: duplicate section {current_section!r}")
                sections.add(current_section)
                record = {"type": "section", **section}
            elif expr:
                record = _operation(expr, source, number)
                if record.get("type") == "package" and current_section is not None:
                    record["section"] = current_section
            if record is not None:
                record["_line_id"] = digest_for(source, number)
                record["_line_number"] = number
                record["_expression"] = expr
            digest = digest_for(source, number)
            parsed.append(McpackLine(digest, number, expr, start, end, record, section))
        return cls(str(source), text, tuple(parsed))

    def replace_expression(self, line_id: str, expected_expression: str, replacement: str) -> bytes:
        line = self._line(line_id)
        if line.expression != expected_expression:
            raise MetadataError(
                f"{self.source}:{line.line_number}: expression changed since it was read"
            )
        if "\n" in replacement or "\r" in replacement:
            raise MetadataError("replacement must fit on one physical line")
        physical = self.text.splitlines(keepends=True)
        current = physical[line.line_number - 1]
        physical[line.line_number - 1] = (
            current[: line.expression_start] + replacement + current[line.expression_end :]
        )
        return "".join(physical).encode("utf-8")

    def replace_section(self, line_id: str, expected_expression: str, replacement: str) -> bytes:
        line = self._line(line_id)
        if line.section is None:
            raise MetadataError(f"{self.source}:{line.line_number}: line is not a section header")
        return self.replace_expression(line_id, expected_expression, replacement)

    def _line(self, line_id: str) -> McpackLine:
        for line in self.lines:
            if line.line_id == line_id:
                return line
        raise MetadataError(f"{self.source}: line {line_id!r} no longer exists")


@dataclass(frozen=True)
class _McpackExpression:
    text: str


def _without_comment(text: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif text.startswith("//", index):
            return text[:index]
    return text


def digest_for(source: str | Path, number: int) -> str:
    return hashlib.sha1(f"{source}\0{number}".encode()).hexdigest()[:12]


def _split_args(text: str, source: str, line: int) -> list[str]:
    values: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                raise MetadataError(f"{source}:{line}: mismatched delimiter")
        elif char == "," and not stack:
            values.append(text[start:index].strip())
            start = index + 1
    if quote or stack:
        raise MetadataError(f"{source}:{line}: unclosed string or delimiter")
    tail = text[start:].strip()
    if tail:
        values.append(tail)
    return values


def _arguments(parts: list[str], source: str, line: int) -> tuple[list[Any], dict[str, Any]]:
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    named = False
    for part in parts:
        key, sep, value = part.partition("=")
        if sep and _IDENTIFIER.fullmatch(key.strip()):
            named = True
            key = key.strip()
            if key in keywords:
                raise MetadataError(f"{source}:{line}: duplicate argument {key!r}")
            keywords[key] = _value(value.strip(), source, line)
        else:
            if named:
                raise MetadataError(
                    f"{source}:{line}: positional arguments must precede named arguments"
                )
            positional.append(_value(part, source, line))
    return positional, keywords


def _value(text: str, source: str, line: int) -> Any:
    if _IDENTIFIER.fullmatch(text) and text not in {"True", "False", "None"}:
        return {"_reference": text}
    provider = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", text)
    if provider and provider.group(1) in _PROVIDERS:
        name, positional, keywords = _call(text, source, line)
        return {"_provider": _package(name, positional, keywords, None, False, source, line)}
    try:
        node = ast.parse(text, mode="eval").body
        if node is None:
            raise ValueError("expected an expression")
        value = _literal_node(node)
    except (SyntaxError, ValueError, TypeError) as exc:
        raise MetadataError(f"{source}:{line}: expected a literal value, got {text!r}") from exc
    if not _allowed(value):
        raise MetadataError(f"{source}:{line}: unsupported literal value")
    return value


def _literal_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool, type(None))
    ):
        return node.value
    if isinstance(node, ast.Name):
        return {"_reference": node.id}
    if isinstance(node, ast.List):
        return [_literal_node(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        result = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise ValueError("dictionary unpacking is not supported")
            key = _literal_node(key_node)
            if not isinstance(key, str):
                raise ValueError("dictionary keys must be strings")
            result[key] = _literal_node(value_node)
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _literal_node(node.operand)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("signs only apply to numbers")
        return -value if isinstance(node.op, ast.USub) else value
    raise ValueError("unsupported expression")


def _allowed(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_allowed(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _allowed(item) for key, item in value.items())
    return False


def _call(text: str, source: str, line: int) -> tuple[str, list[Any], dict[str, Any]]:
    match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*\((.*)\)\s*", text)
    if not match:
        raise MetadataError(f"{source}:{line}: expected one operation call")
    positional, keywords = _arguments(_split_args(match.group(2), source, line), source, line)
    return match.group(1), positional, keywords


def _bind(
    name: str,
    positional: list[Any],
    keywords: dict[str, Any],
    signature: tuple[str, ...],
    source: str,
    line: int,
) -> dict[str, Any]:
    if len(positional) > len(signature):
        raise MetadataError(f"{source}:{line}: too many positional arguments for {name}()")
    result = dict(zip(signature[: len(positional)], positional, strict=True))
    for key, value in keywords.items():
        if key not in signature:
            raise MetadataError(f"{source}:{line}: unexpected argument {key!r} for {name}()")
        if key in result:
            raise MetadataError(f"{source}:{line}: argument {key!r} specified twice")
        result[key] = value
    return result


def _section(text: str, source: str, line: int, order: int) -> dict[str, Any]:
    if not _is_legacy_section(text, source, line):
        title = text.strip()
        if not title:
            raise MetadataError(f"{source}:{line}: section ID cannot be empty")
        return {"id": title, "title": title, "members": [], "order": order}
    opening = text.find("(")
    identity = text[:opening].strip()
    if not identity:
        raise MetadataError(f"{source}:{line}: section ID cannot be empty")
    name, positional, keywords = _call("section(" + text[opening + 1 :], source, line)
    values = _bind(name, positional, keywords, ("title", "order", "members"), source, line)
    title = values.get("title", identity)
    if not isinstance(title, str):
        raise MetadataError(f"{source}:{line}: section title must be a string")
    members = values.get("members", [])
    if not isinstance(members, list):
        raise MetadataError(f"{source}:{line}: section members must be a list")
    return {
        "id": identity,
        "title": title,
        "members": [_ref(member) for member in members],
        "order": order,
    }


def _is_legacy_section(text: str, source: str, line: int) -> bool:
    opening = text.find("(")
    if opening <= 0 or text[-1:] != ")" or text[opening - 1].isspace():
        return False
    arguments = text[opening + 1 : -1]
    try:
        parts = _split_args(arguments, source, line)
    except MetadataError:
        return False
    legacy_keys = {"title", "order", "members"}
    for part in parts:
        key, separator, _value_text = part.partition("=")
        if separator and _IDENTIFIER.fullmatch(key.strip()) and key.strip() in legacy_keys:
            return True
    # Earlier Mcpack drafts accepted positional section metadata. Keep reading it
    # when its first argument is unambiguously a quoted title.
    return bool(parts and parts[0].lstrip().startswith(("'", '"')))


def _operation(text: str, source: str, line: int) -> dict[str, Any]:
    alias: str | None = None
    body = text
    if "=" in text and not text.startswith(("==",)):
        lhs, rhs = text.split("=", 1)
        if _IDENTIFIER.fullmatch(lhs.strip()):
            alias, body = lhs.strip(), rhs.strip()
    declared_only = body.startswith("*")
    if declared_only:
        body = body[1:].lstrip()
    call = re.match(r"([A-Za-z_]\w*)\s*\(", body)
    if not call:
        raise MetadataError(f"{source}:{line}: expected an operation call")
    name, positional, keywords = _call(body, source, line)
    if name in _PROVIDERS:
        return _package(name, positional, keywords, alias, declared_only, source, line)
    if alias is not None or declared_only:
        raise MetadataError(
            f"{source}:{line}: aliases and '*' are only valid for package declarations"
        )
    sig = {
        "layer": ("id", "minecraft", "loader"),
        "parent": ("path", "parameters"),
        "add": ("target",),
        "disable": ("target",),
        "remove": ("target",),
        "dependency": ("source", "target"),
        "put": ("path", "content", "content_base64"),
        "remove_paths": ("glob",),
        "patch_properties": ("path", "set", "remove"),
        "group": ("id", "name", "description", "state"),
        "prune": ("glob",),
        "ignore": ("path", "kind"),
        "route": ("id", "when", "file", "section", "priority", "path_mode"),
    }
    if name not in sig:
        raise MetadataError(f"{source}:{line}: unknown operation {name!r}")
    values = _bind(name, positional, keywords, sig[name], source, line)
    if name == "layer":
        if "id" in values:
            values["id"] = _ref(values["id"])
        return {"type": "layer", **values}
    if name == "parent":
        return {"type": "parent", **values}
    if name in ("add", "disable", "remove"):
        target = values.get("target")
        package_selector = False
        if isinstance(target, dict) and "_provider" in target:
            target = target["_provider"]["id"]
            package_selector = True
        elif isinstance(target, dict) and "_reference" in target:
            target = target["_reference"]
            package_selector = True
        if name == "remove" and package_selector:
            return {"type": "activation", "id": target, "state": "disabled"}
        return (
            {
                "type": "activation",
                "id": target,
                "state": "enabled" if name == "add" else "disabled",
            }
            if name != "remove"
            else {"type": "remove", "path": target}
        )
    if name == "dependency":
        return {"type": name, **{key: _ref(value) for key, value in values.items()}}
    if name == "group":
        values["activation"] = values.pop("state", "available")
        if "id" in values:
            values["id"] = _ref(values["id"])
        return {"type": name, **values}
    if name == "route":
        return {
            "type": "routing_rule",
            "id": _ref(values.get("id")),
            "when": values.get("when", {}),
            "write": {
                "file": {
                    "path": values.get("file", "local-edits.mcpack"),
                    "path_mode": values.get("path_mode", "local"),
                },
                "section": values.get("section"),
            },
            "priority": values.get("priority", 0),
        }
    if name == "ignore":
        kind = values.get("kind", "glob")
        return {"type": name, "selector": {"kind": kind, "path": values.get("path")}}
    if name in ("put", "remove_paths", "patch_properties", "prune"):
        return {"type": name, **values}
    raise MetadataError(f"{source}:{line}: unsupported operation {name!r}")


def _package(
    name: str,
    positional: list[Any],
    keywords: dict[str, Any],
    alias: str | None,
    declared_only: bool,
    source: str,
    line: int,
) -> dict[str, Any]:
    signatures = {
        "modrinth": ("project", "version"),
        "curseforge": ("project", "file"),
        "local": ("path",),
        "direct_url": ("url", "sha256"),
    }
    common = ("kind", "destination", "logical_id", "mod_id_alias", "update", "auto_requires")
    values = _bind(name, positional, keywords, signatures[name] + common, source, line)
    key = signatures[name][0]
    project = values.get(key)
    if not isinstance(project, str) or not project:
        raise MetadataError(f"{source}:{line}: {name}() requires a non-empty {key}")
    identity = alias or f"{name}:{project}"
    selector = values.get(signatures[name][1]) if name in ("modrinth", "curseforge") else None
    if isinstance(selector, dict):
        selector = None
    version = str(selector) if selector is not None else None
    location = project
    sha = values.get("sha256", "")
    if name == "local":
        location = project
    if name == "direct_url":
        location = project
    result = {
        "type": "package",
        "id": identity,
        "kind": values.get("kind", "mod"),
        "source": {"provider": name, "location": location, "sha256": sha},
        "activation": "available" if declared_only else "enabled",
        "update_policy": values.get("update", "fixed"),
    }
    if "logical_id" in values:
        result["logical_id"] = values["logical_id"]
    if "auto_requires" in values:
        result["automatic_requires"] = values["auto_requires"]
    if values.get("mod_id_alias") is not None:
        result["alias"] = values["mod_id_alias"]
    if selector is not None:
        result["version"] = version
    if values.get("destination") is not None:
        result["destination"] = values["destination"]
    return result


def _ref(value: Any) -> Any:
    if isinstance(value, dict) and "_reference" in value:
        return value["_reference"]
    if isinstance(value, dict) and "_provider" in value:
        return value["_provider"]["id"]
    return value


class McpackParser:
    extensions = (".mcpack",)

    def parse(self, content: bytes | str, source: str | Path = "<memory>") -> McpackDocument:
        return McpackDocument.parse(content, source)


def format_operation(record: Mapping[str, Any]) -> str:
    """Format one normalized operation as a single Mcpack expression."""
    kind = record.get("type")
    if kind == "package":
        source = record.get("source")
        if not isinstance(source, Mapping):
            raise MetadataError("package source must be a mapping")
        provider = source.get("provider")
        location = source.get("location")
        if provider not in _PROVIDERS or not isinstance(location, str):
            raise MetadataError("package has an unsupported provider or location")
        activation = record.get("activation", "enabled")
        if activation == "disabled":
            raise MetadataError(
                "disabled packages must be formatted as a separate disable() operation"
            )
        star = "*" if activation in {"available", "declared"} else ""
        positional: list[Any] = [location]
        if provider in {"modrinth", "curseforge"} and "version" in record:
            selector: Any = record["version"]
            if provider == "curseforge" and isinstance(selector, str) and selector.isdecimal():
                selector = int(selector)
            positional.append(selector)
        names = (
            ("kind", "kind"),
            ("destination", "destination"),
            ("logical_id", "logical_id"),
            ("mod_id_alias", "alias"),
            ("update", "update_policy"),
            ("auto_requires", "automatic_requires"),
        )
        kwargs = {
            argument: record[field]
            for argument, field in names
            if field in record and record[field] is not None
        }
        if kwargs.get("kind") == "mod":
            kwargs.pop("kind")
        if kwargs.get("update") == "fixed":
            kwargs.pop("update")
        if kwargs.get("auto_requires") == []:
            kwargs.pop("auto_requires")
        if provider == "direct_url":
            digest = source.get("sha256")
            if not isinstance(digest, str) or not digest:
                raise MetadataError("direct_url packages require sha256")
            positional.append(digest)
        expression = _format_call(str(provider), positional, kwargs)
        identity = record.get("id")
        alias = f"{identity} = " if isinstance(identity, str) and _bare_reference(identity) else ""
        return f"{alias}{star}{expression}"
    if kind == "activation":
        identity = record.get("id")
        state = record.get("state")
        if state == "enabled":
            return _format_call("add", [_format_ref(identity)], {})
        if state == "disabled":
            return _format_call("disable", [_format_ref(identity)], {})
        raise MetadataError(f"cannot format activation state {state!r} as an operation")
    if kind == "dependency":
        return _format_call(
            "dependency", [_format_ref(record.get("source")), _format_ref(record.get("target"))], {}
        )
    if kind == "group":
        identity = record.get("id")
        kwargs = {}
        for output, field in (
            ("name", "name"),
            ("description", "description"),
            ("state", "activation"),
        ):
            if field in record and record[field] is not None:
                kwargs[output] = record[field]
        return _format_call("group", [_format_ref(identity)], kwargs)
    if kind == "section":
        return format_section(record)
    if kind == "routing_rule":
        write = record.get("write", {})
        if not isinstance(write, Mapping):
            raise MetadataError("route write must be a mapping")
        file_spec = write.get("file", "local-edits.mcpack")
        if isinstance(file_spec, Mapping):
            path = file_spec.get("path", "local-edits.mcpack")
            path_mode = file_spec.get("path_mode", "local")
        else:
            path = file_spec
            path_mode = write.get("path_mode", "local")
        kwargs = {"when": record.get("when", {}), "file": path}
        if "section" in write and write["section"] is not None:
            kwargs["section"] = write["section"]
        kwargs["priority"] = record.get("priority", 0)
        kwargs["path_mode"] = path_mode
        return _format_call("route", [_format_ref(record.get("id"))], kwargs)
    if kind in {"layer", "parent"}:
        keys = ("id", "minecraft", "loader") if kind == "layer" else ("path", "parameters")
        positional_key = keys[0]
        positional = (
            [record[positional_key]]
            if positional_key in record and record[positional_key] is not None
            else []
        )
        kwargs = {key: record[key] for key in keys[1:] if key in record and record[key] is not None}
        return _format_call(str(kind), positional, kwargs)
    if kind == "put":
        if "content_base64" in record:
            kwargs = {"content_base64": record["content_base64"]}
        elif isinstance(record.get("content"), bytes):
            kwargs = {"content_base64": base64.b64encode(record["content"]).decode("ascii")}
        else:
            kwargs = {"content": record.get("content")}
        return _format_call("put", [record.get("path")], kwargs)
    if kind == "remove":
        return _format_call("remove", [record.get("path")], {})
    if kind == "remove_paths":
        return _format_call("remove_paths", [record.get("glob")], {})
    if kind == "patch_properties":
        kwargs = {}
        if "set" in record:
            kwargs["set"] = record["set"]
        if "remove" in record:
            kwargs["remove"] = record["remove"]
        return _format_call("patch_properties", [record.get("path")], kwargs)
    if kind == "prune":
        return _format_call("prune", [record.get("glob")], {})
    if kind == "ignore":
        selector = record.get("selector")
        if isinstance(selector, Mapping):
            return _format_call(
                "ignore", [selector.get("path")], {"kind": selector.get("kind", "glob")}
            )
        return _format_call("ignore", [record.get("glob")], {})
    raise MetadataError(f"unsupported Mcpack operation type: {kind!r}")


def format_section(record: Mapping[str, Any]) -> str:
    """Format a section record as a Mcpack section header."""
    identity = record.get("id")
    if not isinstance(identity, str) or not identity or "\n" in identity or "\r" in identity:
        raise MetadataError("section id must be non-empty and cannot contain newlines")
    return f"#{identity}"


def _format_call(name: str, positional: list[Any], kwargs: Mapping[str, Any]) -> str:
    return f"{name}({_format_call_arguments(positional, kwargs)})"


def _format_call_arguments(positional: list[Any], kwargs: Mapping[str, Any]) -> str:
    args = [_format_value(value) for value in positional]
    args.extend(f"{key}={_format_value(value)}" for key, value in kwargs.items())
    return ", ".join(args)


def _format_ref(value: Any) -> _McpackExpression:
    if not isinstance(value, str):
        raise MetadataError("operation reference must be a string")
    return _McpackExpression(value if _bare_reference(value) else _format_value(value))


def _bare_reference(value: str) -> bool:
    return (
        bool(_IDENTIFIER.fullmatch(value))
        and not keyword.iskeyword(value)
        and value not in {"True", "False", "None"}
    )


def _format_value(value: Any) -> str:
    if isinstance(value, _McpackExpression):
        return value.text
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise MetadataError("non-finite floats are not Mcpack literals")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if isinstance(value, tuple):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise MetadataError("Mcpack dictionary keys must be strings")
        return (
            "{"
            + ", ".join(
                f"{_format_value(key)}: {_format_value(item)}" for key, item in value.items()
            )
            + "}"
        )
    raise MetadataError(f"unsupported Mcpack literal: {value!r}")


def pin_provider_selector(expression: str, selector: str) -> str:
    """Replace or add a Modrinth version / CurseForge file selector in one expression."""
    match = re.search(r"\b(modrinth|curseforge)\s*\(", expression)
    if match is None:
        raise MetadataError("expression has no Modrinth or CurseForge provider call")
    provider = match.group(1)
    open_at = expression.find("(", match.start())
    close_at = _matching_close(expression, open_at)
    if close_at is None:
        raise MetadataError("provider call has an unclosed argument list")
    args_start = open_at + 1
    spans = _argument_spans(expression, args_start, close_at)
    if not spans:
        raise MetadataError("provider call requires a project argument")
    selector_name = "version" if provider == "modrinth" else "file"
    named_selector: tuple[int, int] | None = None
    positional: list[tuple[int, int]] = []
    any_keyword = False
    for start, end in spans:
        raw = expression[start:end]
        eq = _top_level_equal(raw)
        if eq is not None and _IDENTIFIER.fullmatch(raw[:eq].strip()):
            any_keyword = True
            if raw[:eq].strip() == selector_name:
                named_selector = (start + eq + 1, end)
        else:
            positional.append((start, end))
    quoted = (
        selector
        if provider == "curseforge" and selector.isdecimal()
        else json.dumps(selector, ensure_ascii=False)
    )
    target: tuple[int, int] | None = named_selector
    if target is None and len(positional) >= 2:
        target = positional[1]
    if target is not None:
        start, end = target
        while start < end and expression[start].isspace():
            start += 1
        while end > start and expression[end - 1].isspace():
            end -= 1
        return expression[:start] + quoted + expression[end:]
    first_start, first_end = spans[0]
    insertion = f", {selector_name}={quoted}" if any_keyword else f", {quoted}"
    return expression[:first_end] + insertion + expression[first_end:]


def _matching_close(text: str, opening: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _argument_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    item_start = start
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for index in range(start, end):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                raise MetadataError("provider call has mismatched delimiters")
        elif char == "," and not stack:
            left, right = item_start, index
            while left < right and text[left].isspace():
                left += 1
            while right > left and text[right - 1].isspace():
                right -= 1
            if right > left:
                spans.append((left, right))
            item_start = index + 1
    left, right = item_start, end
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    if right > left:
        spans.append((left, right))
    return spans


def _top_level_equal(text: str) -> int | None:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif char == "=" and not stack:
            return index
    return None
