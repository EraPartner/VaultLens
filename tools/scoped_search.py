#!/usr/bin/env python3
"""Lexical search and stdio MCP over the current run's approved documents only.

The qmd-compatible surface deliberately has no global index, embeddings, model
calls, network access or persistent cache. Every request rereads selected files;
a search covers every approved document, however large the vault.
The whole-process runtime remains responsible for OS access enforcement.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from collections.abc import Callable
from typing import TextIO, cast

from local_access import JsonObject, RunScope, protected_name

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 4096
MAX_RESULTS = 50
DEFAULT_SNIPPET_CHARS = 400
MAX_SNIPPET_CHARS = 1600
MAX_GET_CHARS = 64 * 1024
MAX_MESSAGE_CHARS = 128 * 1024
TOKEN = re.compile(r"\w+", re.UNICODE)


def _items(value: object) -> list[object] | None:
    """Return a JSON array's items, or None for any other value."""
    if not isinstance(value, list):
        return None
    # isinstance narrows to list[Unknown]; callers validate each item.
    return cast(list[object], value)


def _strings(value: object) -> list[str] | None:
    """Return the list only when every item is a string."""
    items = _items(value)
    if items is None:
        return None
    strings = [item for item in items if isinstance(item, str)]
    return strings if len(strings) == len(items) else None


def load_scope(manifest_path: Path) -> RunScope:
    """Load one explicit manifest; never consult the environment or host policy."""
    manifest_path = Path(manifest_path)
    if not manifest_path.is_absolute() or manifest_path.is_symlink():
        raise ValueError("The scope manifest must be an absolute regular file")
    try:
        descriptor = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("The scope manifest must be a regular file")
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("Scope manifest exceeds the size limit")
        parsed: object = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("Scope manifest requires version 1")
        # JSON object keys are strings; the fields are validated individually below.
        data = cast(JsonObject, parsed)
        if data.get("version") != 1:
            raise ValueError("Scope manifest requires version 1")
        root = Path(data["root"])
        if (
            not root.is_absolute()
            or not root.is_dir()
            or root.is_symlink()
            or root != root.resolve()
        ):
            raise ValueError("Scope root must be an absolute real directory")
        groups: list[tuple[Path, ...]] = []
        for key in ("read", "write", "deny_read"):
            values = _strings(data[key])
            if values is None:
                raise ValueError(f"Scope {key} must be a list of paths")
            paths = tuple(Path(value) for value in values)
            if any(
                not path.is_absolute()
                or not path.is_relative_to(root)
                or ".." in path.parts
                for path in paths
            ):
                raise ValueError("Scope selections must remain inside its root")
            groups.append(paths)
        profile = data["profile"]
        reports = data["reports"]
        raw_domains: object = data.get("research_domains", [])
        if (
            not isinstance(profile, str)
            or not isinstance(reports, str)
            or _items(raw_domains) is None
        ):
            raise ValueError("Malformed scope metadata")
        domains = _strings(raw_domains)
        if domains is None:
            raise ValueError("Malformed scope research domains")
        read, write, deny_read = groups
        return RunScope(
            root,
            profile,
            read,
            write,
            deny_read,
            tuple(domains),
            Path(reports),
            data.get("review_queue_metadata", False),
        )
    except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read a valid explicit scope manifest") from exc


def _read_document(scope: RunScope, path: Path, *, approved: bool = False) -> str:
    """Walk directory descriptors without following links, including race swaps.

    `approved` skips the path-level access check for a path this run already
    approved. The descriptor walk and hard-link check below still run on every
    read, so a later symlink or hard-link swap is refused either way.
    """
    if (
        path.suffix not in {".md", ".txt"}
        or protected_name(path.name)
        or not (approved or scope.readable(path))
    ):
        raise ValueError("Document is unavailable in this access profile")
    relative = path.relative_to(scope.root)
    descriptors: list[int] = []
    try:
        # Ancestors need path traversal, not directory listings. Linux O_PATH
        # and macOS O_SEARCH keep descriptor walks valid under narrow read
        # grants without disclosing the surrounding directory's contents.
        traversal = getattr(os, "O_PATH", getattr(os, "O_SEARCH", None))
        if traversal is None:
            raise ValueError("Search-only directory descriptors are unavailable")
        directory_flags = traversal | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(scope.root, directory_flags)
        descriptors.append(parent)
        for part in relative.parts[:-1]:
            parent = os.open(part, directory_flags, dir_fd=parent)
            descriptors.append(parent)
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_DOCUMENT_BYTES
        ):
            raise ValueError("Document is not a regular file within the size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise ValueError("Document exceeds the size limit")
        return payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Document is unavailable in this access profile") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _bounded_int(value: object, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"Expected an integer between 1 and {maximum}")
    return value


def _queries(arguments: JsonObject) -> list[str]:
    query = arguments.get("query")
    searches = arguments.get("searches")
    if query is not None:
        values: list[object] = [query]
    elif (items := _items(searches)) is not None and 1 <= len(items) <= 10:
        values = [
            cast(JsonObject, item).get("query") if isinstance(item, dict) else item
            for item in items
        ]
    else:
        raise ValueError("Provide query or a nonempty list of searches")
    strings = [value for value in values if isinstance(value, str)]
    if len(strings) != len(values) or any(
        not value.strip() or len(value) > MAX_QUERY_CHARS for value in strings
    ):
        raise ValueError("Queries must be nonempty strings within the size limit")
    return strings


def _title(text: str, path: Path) -> str:
    for line in text.splitlines()[:100]:
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return path.stem


class ScopedSearch:
    scope: RunScope
    _approved: set[Path]

    def __init__(self, scope: RunScope) -> None:
        self.scope = scope
        self._approved = set()

    def _documents(self) -> list[Path]:
        """List approved documents, rewalking the grants so new files appear.

        The path-level access check is the slow part of a large search, so a
        path approved once stays approved for this run; refusals are rechecked.
        """
        paths: list[Path] = []
        for path in sorted(self.scope.document_candidates()):
            if protected_name(path.name):
                continue
            if path not in self._approved:
                if not self.scope.readable(path):
                    continue
                self._approved.add(path)
            paths.append(path)
        return paths

    def _path(self, identifier: object) -> Path:
        if (
            not isinstance(identifier, str)
            or not identifier
            or "\0" in identifier
            or "\\" in identifier
        ):
            raise ValueError("Provide an approved document path")
        if identifier.startswith("qmd://scope/"):
            identifier = identifier[len("qmd://scope/") :]
        requested = Path(identifier)
        if ".." in requested.parts:
            raise ValueError("Document path traversal is forbidden")
        path = requested if requested.is_absolute() else self.scope.root / requested
        if not self.scope.readable(path):
            raise ValueError("Document is unavailable in this access profile")
        return path

    def search(self, arguments: JsonObject) -> JsonObject:
        queries = _queries(arguments)
        terms = list(
            dict.fromkeys(
                token.casefold() for query in queries for token in TOKEN.findall(query)
            )
        )
        if not terms:
            raise ValueError("Queries must contain searchable words")
        limit = _bounded_int(arguments.get("limit"), default=10, maximum=MAX_RESULTS)
        snippet_chars = _bounded_int(
            arguments.get("snippet_chars"),
            default=DEFAULT_SNIPPET_CHARS,
            maximum=MAX_SNIPPET_CHARS,
        )
        # Keep one candidate snippet per matched term, not whole documents, so
        # memory stays bounded when a common term matches most of a large vault.
        matched: list[tuple[str, str, dict[int, str], str, list[int]]] = []
        document_frequency = [0] * len(terms)
        scanned = 0
        skipped = 0
        for path in self._documents():
            try:
                text = _read_document(self.scope, path, approved=True)
            except ValueError:
                skipped += 1
                continue
            scanned += 1
            relative = path.relative_to(self.scope.root).as_posix()
            title = _title(text, path)
            corpus = f"{relative}\n{title}\n{text}".casefold()
            counts = [corpus.count(term) for term in terms]
            for index, count in enumerate(counts):
                document_frequency[index] += count > 0
            if any(counts):
                lowered = text.casefold()
                snippets: dict[int, str] = {}
                for index, count in enumerate(counts):
                    position = lowered.find(terms[index]) if count else -1
                    if position >= 0:
                        start = max(0, position - snippet_chars // 8)
                        snippets[index] = text[start : start + snippet_chars]
                # Terms found only in the path or title fall back to the start.
                lead = "" if snippets else text[:snippet_chars]
                matched.append((relative, title, snippets, lead, counts))
        # Weight terms by rarity so common words in a natural-language query
        # cannot outrank the one page that has the distinctive terms.
        weights = [
            math.log(1 + scanned / max(frequency, 1)) for frequency in document_frequency
        ]
        total_weight = sum(weights) or 1.0
        results: list[JsonObject] = []
        for relative, title, snippets, lead, counts in matched:
            present = [index for index, count in enumerate(counts) if count]
            score = sum(weights[index] for index in present) / total_weight + min(
                sum(counts), 100
            ) / 1000
            # Anchor the snippet on the rarest term found in the document body.
            anchor = max(snippets, key=lambda index: weights[index], default=None)
            results.append(
                {
                    "file": relative,
                    "path": relative,
                    "docid": relative,
                    "title": title,
                    "score": round(score, 4),
                    "snippet": lead if anchor is None else snippets[anchor],
                }
            )
        results.sort(key=lambda result: (-result["score"], result["file"]))
        return {
            "mode": "lexical",
            "results": results[:limit],
            "truncated": len(results) > limit,
            "skipped_documents": skipped,
        }

    def get(self, arguments: JsonObject) -> JsonObject:
        path = self._path(
            arguments.get("path", arguments.get("file", arguments.get("docid")))
        )
        text = _read_document(self.scope, path)
        relative = path.relative_to(self.scope.root).as_posix()
        max_chars = _bounded_int(
            arguments.get("max_chars"), default=MAX_GET_CHARS, maximum=MAX_GET_CHARS
        )
        return {
            "file": relative,
            "path": relative,
            "docid": relative,
            "title": _title(text, path),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def multi_get(self, arguments: JsonObject) -> JsonObject:
        requested: object = arguments.get(
            "paths", arguments.get("files", arguments.get("docids"))
        )
        if isinstance(requested, str):
            paths: list[object] = [value.strip() for value in requested.split(",")]
        elif isinstance(requested, list):
            # isinstance narrows to list[Unknown]; each path is validated by get().
            paths = list(cast(list[object], requested))
        else:
            raise ValueError("Provide between 1 and 10 approved document paths")
        if not 1 <= len(paths) <= 10:
            raise ValueError("Provide between 1 and 10 approved document paths")
        # Reject the whole request on any excluded path; no partial disclosure.
        max_chars = _bounded_int(
            arguments.get("max_chars"), default=8192, maximum=MAX_GET_CHARS
        )
        documents: list[JsonObject] = []
        for path in paths:
            try:
                documents.append(
                    self.get({"path": path, "max_chars": min(8192, max_chars)})
                )
            except ValueError as exc:
                # Name the caller's own path so it can drop it and retry.
                raise ValueError(f"{exc}: {str(path)[:200]!r}") from exc
        return {"documents": documents, "mode": "lexical"}

    def status(self, _arguments: JsonObject | None = None) -> JsonObject:
        return {
            "mode": "lexical",
            "profile": self.scope.name,
            "documents": len(self._documents()),
            "embeddings": False,
            "persistent_index": False,
        }

    def call(self, name: object, arguments: object) -> JsonObject:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        # JSON object keys are strings; isinstance only narrows to dict[Unknown, Unknown].
        tool_arguments = cast(JsonObject, arguments)
        operations: dict[str, Callable[[JsonObject], JsonObject]] = {
            "search": self.search,
            "query": self.search,
            "get": self.get,
            "multi_get": self.multi_get,
            "status": self.status,
        }
        if not isinstance(name, str) or name not in operations:
            raise ValueError("Unknown scoped search tool")
        operation = operations[name]
        return operation(tool_arguments)


def _tools() -> list[JsonObject]:
    query_schema: JsonObject = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "searches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "type": {"type": "string"},
                    },
                },
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            "snippet_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SNIPPET_CHARS,
            },
        },
    }
    definitions: list[tuple[str, str, JsonObject]] = [
        (
            "search",
            "Lexical search over approved current Markdown/text documents. Matches words, not meaning: pass distinctive key terms and retry with synonyms. Snippets default to 400 characters; set snippet_chars (up to 1600) for more, or use get.",
            query_schema,
        ),
        (
            "query",
            "Lexical query; accepts query or searches. No semantic models or embeddings, so pass key terms rather than a full sentence.",
            query_schema,
        ),
        (
            "get",
            "Read an approved document returned by search, within output limits.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "file": {"type": "string"},
                    "docid": {"type": "string"},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_GET_CHARS,
                    },
                },
            },
        ),
        (
            "multi_get",
            "Read up to 10 approved document paths. One unavailable path fails the whole call and is named in the error.",
            {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                    }
                },
                "required": ["paths"],
            },
        ),
        (
            "status",
            "Report the current selected document scope; embeddings are disabled.",
            {"type": "object", "properties": {}},
        ),
    ]
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        }
        for name, description, schema in definitions
    ]


def serve_mcp(
    search: ScopedSearch, input_stream: TextIO, output_stream: TextIO
) -> None:
    def send(payload: JsonObject) -> None:
        output_stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        output_stream.flush()

    while line := input_stream.readline(MAX_MESSAGE_CHARS + 1):
        if len(line) > MAX_MESSAGE_CHARS:
            # Drain an oversized line without retaining its full body.
            while not line.endswith("\n"):
                line = input_stream.readline(MAX_MESSAGE_CHARS + 1)
                if not line:
                    break
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Message exceeds the size limit",
                    },
                }
            )
            continue
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Invalid JSON"},
                }
            )
            continue
        # JSON object keys are strings; isinstance only narrows to dict[Unknown, Unknown].
        message = cast(JsonObject, decoded) if isinstance(decoded, dict) else None
        if (
            message is None
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid request"},
                }
            )
            continue
        if "id" not in message:
            continue
        identifier = message["id"]
        method = message["method"]
        raw_parameters: object = message.get("params", {})
        try:
            if not isinstance(raw_parameters, dict):
                raise ValueError("Parameters must be an object")
            parameters = cast(JsonObject, raw_parameters)  # keys are JSON strings
            result: JsonObject
            if method == "initialize":
                result = {
                    "protocolVersion": parameters.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "qmd", "version": "1.0"},
                    "instructions": "Search is lexical and restricted to current run selections. Never use a global qmd index.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": _tools()}
            elif method == "tools/call":
                try:
                    payload = search.call(
                        parameters.get("name"), parameters.get("arguments", {})
                    )
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(payload, ensure_ascii=False),
                            }
                        ],
                        "structuredContent": payload,
                        "isError": False,
                    }
                except (ValueError, TypeError) as exc:
                    result = {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    }
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                continue
            send({"jsonrpc": "2.0", "id": identifier, "result": result})
        except (ValueError, TypeError):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32602, "message": "Invalid parameters"},
                }
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "operation",
        choices=("mcp", "search", "query", "get", "multi-get", "multi_get", "status"),
    )
    parser.add_argument("values", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("-n", "--limit", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        search = ScopedSearch(load_scope(args.manifest))
        if args.operation == "mcp":
            serve_mcp(search, sys.stdin, sys.stdout)
            return 0
        if args.operation in {"search", "query"}:
            payload = search.search(
                {"query": " ".join(args.values), "limit": args.limit}
            )
        elif args.operation == "get":
            if len(args.values) != 1:
                raise ValueError("get requires one approved document path")
            payload = search.get({"path": args.values[0]})
        elif args.operation in {"multi-get", "multi_get"}:
            payload = search.multi_get({"paths": args.values})
        else:
            payload = search.status()
        if args.json or args.format == "json":
            print(json.dumps(payload, ensure_ascii=False))
        elif "results" in payload:
            for result in payload["results"]:
                print(
                    f"{result['file']} ({result['score']:.3f})\n{result['snippet']}\n"
                )
        elif "text" in payload:
            print(payload["text"])
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(f"Scoped search error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
