"""User-editable YAML workflows beside the supplied tool store."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from revit_bridge.capabilities.store import ToolStore
from .schema import valid_name, validate_workflow


class WorkflowError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


class WorkflowStore:
    def __init__(self, tool_store: ToolStore):
        self.tool_store = tool_store
        self.directory = tool_store.user_dir.parent / "workflows"
        if self.directory.resolve() in (tool_store.user_dir.resolve(), tool_store.builtin_dir.resolve()):
            raise ValueError("the workflows directory must be separate from both tool directories")

    def _path(self, name: str) -> Path:
        if not valid_name(name):
            raise WorkflowError(["name: expected a valid tool identifier"])
        return self.directory / f"{name}.yaml"

    def load(self, name: str) -> dict | None:
        """Read and validate against today's catalog. A missing file returns None."""
        data, _ = self._read(name)
        return data

    def read_yaml(self, name: str) -> str | None:
        """The same validated file, including the author's layout and comments."""
        _, source = self._read(name)
        return source

    def _read(self, name: str) -> tuple[dict | None, str | None]:
        path = self._path(name)
        try:
            source = path.read_bytes().decode("utf-8-sig")
            data = yaml.safe_load(source)
        except FileNotFoundError:
            return None, None
        except (ValueError, yaml.YAMLError) as exc:
            raise WorkflowError([f"{path.name}: invalid YAML: {exc}"]) from exc
        errors = validate_workflow(data, self.tool_store)
        if isinstance(data, dict) and data.get("name") != name:
            errors.append(f"name: must match filename {path.name!r}")
        if errors:
            raise WorkflowError(errors)
        return data, source

    def list_workflows(self) -> list[dict]:
        """Valid workflows in name order. Invalid entries remain editable on disk."""
        workflows = []
        for path in sorted(self.directory.glob("*.yaml"), key=lambda p: p.name):
            if not path.is_file():
                continue
            try:
                data = self.load(path.stem)
            except (OSError, WorkflowError) as exc:
                logging.getLogger(__name__).warning("Skipping workflow %s: %s", path.name, exc)
                continue
            if data is not None:
                workflows.append(data)
        return workflows

    def save(self, data: dict, *, overwrite: bool = False) -> Path:
        """Publish a complete YAML file. A concurrent create cannot clobber the winner."""
        errors = validate_workflow(data, self.tool_store)
        if errors:
            raise WorkflowError(errors)
        path = self._path(data["name"])
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = NamedTemporaryFile(mode="w", dir=self.directory, suffix=".tmp", encoding="utf-8",
                                    newline="\n", delete=False)
        tmp = Path(handle.name)
        try:
            with handle as output:
                yaml.safe_dump(data, output, allow_unicode=True, sort_keys=False)
                output.flush()
                os.fsync(output.fileno())
            if overwrite:
                os.replace(tmp, path)
            elif os.name == "nt":
                os.rename(tmp, path)  # atomic, raises FileExistsError on Windows
            else:
                os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    def delete(self, name: str) -> bool:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            return False
        return True
