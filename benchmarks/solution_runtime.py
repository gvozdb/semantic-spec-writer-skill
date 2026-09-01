"""Import benchmark solution entrypoints without flattening Python packages."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_entrypoint(workspace: Path, entrypoint: str) -> tuple[Path, tuple[str, ...]]:
    workspace = workspace.resolve()
    candidate = Path(entrypoint)
    if (
        not entrypoint
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.suffix != ".py"
    ):
        raise ValueError(f"invalid benchmark entrypoint: {entrypoint}")
    path = (workspace / candidate).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"benchmark entrypoint escapes workspace: {entrypoint}") from exc
    if not path.is_file():
        raise ValueError(f"benchmark entrypoint does not exist: {entrypoint}")
    module_parts = candidate.with_suffix("").parts
    if module_parts[-1] == "__init__":
        module_parts = module_parts[:-1]
    if not module_parts or any(not part.isidentifier() for part in module_parts):
        raise ValueError(f"entrypoint is not an importable Python module: {entrypoint}")
    return path, module_parts


def import_entrypoint(workspace: Path, entrypoint: str) -> ModuleType:
    workspace = workspace.resolve()
    workspace_text = str(workspace)
    if workspace_text not in sys.path:
        sys.path.insert(0, workspace_text)
    path, module_parts = resolve_entrypoint(workspace, entrypoint)
    if len(module_parts) == 1 and path.name != "__init__.py":
        module_name = f"benchmark_{module_parts[0]}_{abs(hash(entrypoint))}"
        return load_module(path, module_name)

    package = module_parts[0]
    existing = sys.modules.get(package)
    existing_file = Path(getattr(existing, "__file__", "")).resolve() if existing else None
    if existing_file is not None and workspace not in existing_file.parents:
        for name in list(sys.modules):
            if name == package or name.startswith(f"{package}."):
                del sys.modules[name]
    return importlib.import_module(".".join(module_parts))
