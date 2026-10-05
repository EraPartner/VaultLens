"""Typed loader for test modules that cannot be imported by name.

Several modules live in hyphenated or non-package paths (``agents/wiki-agent.py``,
``schedule/dispatch.py``), so tests load them from a file location. ``module_from_spec``
and ``spec.loader`` are Optional in typeshed, which leaves every call site with strict
errors; this helper narrows them once.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Protocol, cast


class LoadedModule(Protocol):
    """A dynamically loaded module: any attribute can be read and tests may rebind them."""

    def __getattr__(self, name: str) -> Any: ...  # noqa: ANN401  # pyright: ignore[reportExplicitAny] - contents are unknowable statically

    def __setattr__(self, name: str, value: object) -> None: ...


def load_module(name: str, path: Path, *, register: bool = False) -> LoadedModule:
    """Execute the Python file at ``path`` as module ``name`` and return it.

    ``register=True`` adds the module to ``sys.modules`` before execution, which
    ``dataclasses`` and ``typing.get_type_hints`` need to resolve string annotations.
    The caller owns removing the entry afterwards (for example with
    ``patch.dict(sys.modules)``); the default leaves ``sys.modules`` untouched.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = module
    spec.loader.exec_module(module)
    # ModuleType rejects attribute assignment under strict typing; LoadedModule models what
    # tests actually do with these modules (read and rebind arbitrary attributes). The
    # intermediate object cast is needed because the two types do not overlap structurally.
    return cast(LoadedModule, cast(object, module))
