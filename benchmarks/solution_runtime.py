"""Import benchmark solution entrypoints without flattening Python packages."""

from __future__ import annotations

import importlib
import importlib.machinery
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
    package_root = workspace / package

    def belongs_to_workspace(module: ModuleType) -> bool:
        candidates: list[str] = []
        module_file = getattr(module, "__file__", None)
        if isinstance(module_file, str) and module_file:
            candidates.append(module_file)
        module_paths = getattr(module, "__path__", ())
        candidates.extend(str(item) for item in module_paths)
        for candidate in candidates:
            try:
                Path(candidate).resolve().relative_to(workspace)
                return True
            except ValueError:
                continue
        return False

    collision = existing is not None and not belongs_to_workspace(existing)
    if not (package_root / "__init__.py").is_file() and collision:
        synthetic_package = (
            f"_benchmark_namespace_{package}_{abs(hash(str(workspace)))}"
        )
        namespace = ModuleType(synthetic_package)
        namespace.__package__ = synthetic_package
        namespace.__path__ = [str(package_root)]
        namespace_spec = importlib.machinery.ModuleSpec(
            synthetic_package,
            loader=None,
            is_package=True,
        )
        namespace_spec.submodule_search_locations = [str(package_root)]
        namespace.__spec__ = namespace_spec
        namespace.__loader__ = None
        sys.modules[synthetic_package] = namespace
        importlib.invalidate_caches()
        return importlib.import_module(
            ".".join((synthetic_package, *module_parts[1:]))
        )

    if collision:
        for name in list(sys.modules):
            if name == package or name.startswith(f"{package}."):
                del sys.modules[name]

    if not (package_root / "__init__.py").is_file():
        namespace = ModuleType(package)
        namespace.__package__ = package
        namespace.__path__ = [str(package_root)]
        namespace_spec = importlib.machinery.ModuleSpec(
            package,
            loader=None,
            is_package=True,
        )
        namespace_spec.submodule_search_locations = [str(package_root)]
        namespace.__spec__ = namespace_spec
        namespace.__loader__ = None
        sys.modules[package] = namespace
    importlib.invalidate_caches()
    return importlib.import_module(".".join(module_parts))
