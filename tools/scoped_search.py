#!/usr/bin/env python3
"""Lexical search and stdio MCP over the current run's approved documents only.

The qmd-compatible surface deliberately has no global index, embeddings, model
calls, network access or persistent cache. Every request rereads selected files.
The whole-process runtime remains responsible for OS access enforcement.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import TextIO

from local_access import PROTECTED_NAMES, RunScope

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_CORPUS_BYTES = 32 * 1024 * 1024
MAX_DOCUMENTS = 4096
MAX_QUERY_CHARS = 4096
MAX_RESULTS = 50
MAX_GET_CHARS = 64 * 1024
MAX_MESSAGE_CHARS = 128 * 1024
TOKEN = re.compile(r"\w+", re.UNICODE)


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
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Scope manifest requires version 1")
        root = Path(data["root"])
        if (
            not root.is_absolute()
            or not root.is_dir()
            or root.is_symlink()
            or root != root.resolve()
        ):
            raise ValueError("Scope root must be an absolute real directory")
        groups = []
        for key in ("read", "write", "deny_read"):
            values = data[key]
            if not isinstance(values, list) or any(
                not isinstance(v, str) for v in values
            ):
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
        domains = data.get("research_domains", [])
        if (
            not isinstance(profile, str)
            or not isinstance(reports, str)
            or not isinstance(domains, list)
        ):
            raise ValueError("Malformed scope metadata")
        if any(not isinstance(domain, str) for domain in domains):
            raise ValueError("Malformed scope research domains")
        return RunScope(
            root,
            profile,
            *groups,
            tuple(domains),
            Path(reports),
            data.get("review_queue_metadata", False),
        )
    except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read a valid explicit scope manifest") from exc


def _read_document(scope: RunScope, path: Path) -> str:
    """Walk directory descriptors without following links, including race swaps."""
    if (
        path.suffix not in {".md", ".txt"}
        or path.name in PROTECTED_NAMES
        or not scope.readable(path)
    ):
        raise ValueError("Document is unavailable in this access profile")
    relative = path.relative_to(scope.root)
    descriptors = []
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
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT_BYTES:
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


def _queries(arguments: dict) -> list[str]:
    query = arguments.get("query")
    searches = arguments.get("searches")
    if query is not None:
        values = [query]
    elif isinstance(searches, list) and 1 <= len(searches) <= 10:
        values = [
            item.get("query") if isinstance(item, dict) else item for item in searches
        ]
    else:
        raise ValueError("Provide query or a nonempty list of searches")
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARS
        for value in values
    ):
        raise ValueError("Queries must be nonempty strings within the size limit")
    return values


def _title(text: str, path: Path) -> str:
    for line in text.splitlines()[:100]:
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return path.stem


class ScopedSearch:
    def __init__(self, scope: RunScope):
        self.scope = scope

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

    def search(self, arguments: dict) -> dict:
        queries = _queries(arguments)
        terms = list(
            dict.fromkeys(
                token.casefold() for query in queries for token in TOKEN.findall(query)
            )
        )
        if not terms:
            raise ValueError("Queries must contain searchable words")
        limit = _bounded_int(arguments.get("limit"), default=10, maximum=MAX_RESULTS)
        paths = self.scope.document_paths()
        results = []
        consumed = 0
        truncated = len(paths) > MAX_DOCUMENTS
        skipped = 0
        for path in paths[:MAX_DOCUMENTS]:
            try:
                text = _read_document(self.scope, path)
            except ValueError:
                skipped += 1
                continue
            consumed += len(text.encode("utf-8"))
            if consumed > MAX_CORPUS_BYTES:
                truncated = True
                break
            relative = path.relative_to(self.scope.root).as_posix()
            lowered = text.casefold()
            title = _title(text, path)
            corpus = f"{relative}\n{title}\n{lowered}".casefold()
            counts = [corpus.count(term) for term in terms]
            matches = sum(count > 0 for count in counts)
            if not matches:
                continue
            position = min(
                (lowered.find(term) for term in terms if term in lowered), default=0
            )
            start = max(0, position - 200)
            score = matches / len(terms) + min(sum(counts), 100) / 1000
            results.append(
                {
                    "file": relative,
                    "path": relative,
                    "docid": relative,
                    "title": title,
                    "score": round(score, 4),
                    "snippet": text[start : start + 1600],
                }
            )
        results.sort(key=lambda result: (-result["score"], result["file"]))
        return {
            "mode": "lexical",
            "results": results[:limit],
            "truncated": truncated or len(results) > limit,
            "skipped_documents": skipped,
        }

    def get(self, arguments: dict) -> dict:
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

    def multi_get(self, arguments: dict) -> dict:
        paths = arguments.get("paths", arguments.get("files", arguments.get("docids")))
        if isinstance(paths, str):
            paths = [value.strip() for value in paths.split(",")]
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
            raise ValueError("Provide between 1 and 10 approved document paths")
        # Reject the whole request on any excluded path; no partial disclosure.
        max_chars = _bounded_int(
            arguments.get("max_chars"), default=8192, maximum=MAX_GET_CHARS
        )
        documents = [
            self.get({"path": path, "max_chars": min(8192, max_chars)})
            for path in paths
        ]
        return {"documents": documents, "mode": "lexical"}

    def status(self, arguments: dict | None = None) -> dict:
        paths = self.scope.document_paths()
        return {
            "mode": "lexical",
            "profile": self.scope.name,
            "documents": min(len(paths), MAX_DOCUMENTS),
            "truncated": len(paths) > MAX_DOCUMENTS,
            "embeddings": False,
            "persistent_index": False,
        }

    def call(self, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        operations = {
            "search": self.search,
            "query": self.search,
            "get": self.get,
            "multi_get": self.multi_get,
            "status": self.status,
        }
        try:
            operation = operations[name]
        except KeyError as exc:
            raise ValueError("Unknown scoped search tool") from exc
        return operation(arguments)


def _tools() -> list[dict]:
    query_schema = {
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
        },
    }
    definitions = [
        (
            "search",
            "Lexical search over approved current Markdown/text documents.",
            query_schema,
        ),
        (
            "query",
            "Lexical query; accepts query or searches. No semantic models or embeddings.",
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
            "Read up to 10 approved document paths.",
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
    def send(payload: dict) -> None:
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
            message = json.loads(line)
        except json.JSONDecodeError:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Invalid JSON"},
                }
            )
            continue
        if (
            not isinstance(message, dict)
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
        parameters = message.get("params", {})
        try:
            if not isinstance(parameters, dict):
                raise ValueError("Parameters must be an object")
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
