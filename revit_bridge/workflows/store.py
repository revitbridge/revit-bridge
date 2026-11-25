"""User-editable YAML workflows beside the supplied tool store."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from revit_bridge.capabilities.store import ToolStore
from revit_bridge.jsonfile import LockedJsonFile
from .schema import MAX_ERRORS, valid_name, validate_workflow

_MAX_BYTES = 256 * 1024
_MAX_DEPTH = 32
_SIZE_ERROR = "workflow YAML exceeds 256 KiB"


class WorkflowError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems[:MAX_ERRORS]
        super().__init__("\n".join(self.problems))


class _WorkflowLoader(yaml.SafeLoader):
    depth = 0

    def compose_node(self, parent, index):
        if self.peek_event().anchor is not None:
            raise WorkflowError(["workflow YAML aliases and anchors are not allowed"])
        if self.depth >= _MAX_DEPTH:
            raise WorkflowError([f"workflow YAML exceeds maximum depth of {_MAX_DEPTH}"])
        self.depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


class _WorkflowDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        # Validated v1 fields have fixed depth. Repeat a shared Python value
        # in the output so every file we save can be imported again.
        return True


def parse_workflow_yaml(text: str) -> dict:
    """Read one UTF-8 workflow mapping, up to 256 KiB and 32 levels, without YAML anchors."""
    if not isinstance(text, str):
        raise WorkflowError(["workflow YAML must be text"])
    try:
        if len(text) > _MAX_BYTES or len(text.encode("utf-8")) > _MAX_BYTES:
            raise WorkflowError([_SIZE_ERROR])
        data = yaml.load(text, Loader=_WorkflowLoader)
    except WorkflowError:
        raise
    except (yaml.YAMLError, ValueError, TypeError, OverflowError, LookupError, AttributeError) as exc:
        # Constructor errors can include arbitrary input. Report position only.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise WorkflowError(["invalid workflow YAML" + where]) from None
    if not isinstance(data, dict):
        raise WorkflowError(["workflow: expected a mapping"])
    return data


def _publish_new(tmp: Path, path: Path) -> None:
    if os.name == "nt":
        os.rename(tmp, path)  # atomic, exclusive on Windows
        return
    try:
        os.link(tmp, path)
        return
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        pass  # Creating a file can still work when the filesystem refuses hard links.
    # Reserve the destination exclusively, then publish the fully synced file.
    # A reader encountering the empty reservation waits for the publisher.
    path.open("xb").close()
    try:
        os.replace(tmp, path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


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
            with path.open("rb") as handle:
                raw = handle.read(_MAX_BYTES + 1)
            if not raw:
                with self._lock(name).locked(), path.open("rb") as handle:
                    raw = handle.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return None, None
        if len(raw) > _MAX_BYTES:
            raise WorkflowError([_SIZE_ERROR])
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise WorkflowError(["invalid workflow YAML: expected UTF-8"]) from None
        data = parse_workflow_yaml(source)
        errors = validate_workflow(data, self.tool_store)
        if data.get("name") != name:
            errors.append(f"name: must match filename {name}.yaml")
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
                reason = exc if isinstance(exc, WorkflowError) else type(exc).__name__
                logging.getLogger(__name__).warning("Skipping workflow %.105s: %s", path.name, reason)
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
                try:
                    yaml.dump(data, output, Dumper=_WorkflowDumper, allow_unicode=True, sort_keys=False)
                except (yaml.YAMLError, ValueError):
                    raise WorkflowError(["workflow cannot be represented as UTF-8 YAML"]) from None
                output.flush()
                if output.tell() > _MAX_BYTES:
                    raise WorkflowError([_SIZE_ERROR])
                os.fsync(output.fileno())
            with self._lock(data["name"]).locked(required=True):
                if any(p.name != path.name and p.name.casefold() == path.name.casefold()
                       for p in self.directory.glob("*.yaml")):
                    raise WorkflowError(["name: a workflow with different case already exists"])
                if overwrite:
                    os.replace(tmp, path)
                else:
                    _publish_new(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    def _lock(self, name: str) -> LockedJsonFile:
        return LockedJsonFile(self.directory / (name.casefold() + ".yaml"))

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        try:
            with self._lock(name).locked(required=True):
                path.unlink()
        except FileNotFoundError:
            return False
        return True
