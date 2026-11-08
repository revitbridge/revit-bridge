"""`revit-bridge skills list|install`: the bundled skills are listed and copied whole."""
from __future__ import annotations

from pathlib import Path

import pytest

import revit_bridge.mcp_server as server
from revit_bridge import skills_cli
from revit_bridge.paths import skills_dir

NAMES = ["revit-addin-dev", "revit-bridge", "revit-core", "revit-task-brief"]


def run(capsys, *argv: str) -> tuple[int, str]:
    code = server.main(["skills", *argv])
    return code, capsys.readouterr().out


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def test_list_names_every_bundled_skill_with_its_description(capsys):
    code, out = run(capsys, "list")
    assert code == 0
    lines = out.strip().splitlines()
    assert [line.split()[0] for line in lines] == NAMES
    assert all(len(line.split(None, 1)) == 2 for line in lines)          # a description on every line
    assert "Revit" in out


def test_install_copies_every_skill_byte_for_byte(tmp_path, capsys):
    code, out = run(capsys, "install", "--to", str(tmp_path / ".claude" / "skills"))
    assert code == 0
    for name in NAMES:
        dest = tmp_path / ".claude" / "skills" / name
        assert tree(dest) == tree(skills_dir() / name), name
        assert f"{name}: " in out and str(dest) in out
    assert not list((tmp_path / ".claude" / "skills").rglob("__pycache__"))


def test_install_one_skill_keeps_the_rest_of_the_directory(tmp_path, capsys):
    target = tmp_path / ".agents" / "skills"
    (target / "mine").mkdir(parents=True)
    (target / "mine" / "SKILL.md").write_text("theirs", encoding="utf-8")
    code, _ = run(capsys, "install", "revit-addin-dev", "--to", str(target))
    assert code == 0
    assert sorted(p.name for p in target.iterdir()) == ["mine", "revit-addin-dev"]
    assert (target / "mine" / "SKILL.md").read_text(encoding="utf-8") == "theirs"
    assert (target / "revit-addin-dev" / "scripts" / "new_addin.py").is_file()


def test_install_refuses_an_existing_skill_unless_forced(tmp_path, capsys):
    target = tmp_path / "skills"
    dest = target / "revit-core"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("edited locally", encoding="utf-8")
    code, out = run(capsys, "install", "revit-core", "--to", str(target))
    assert code == 1 and "--force" in out
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "edited locally"
    code, _ = run(capsys, "install", "revit-core", "--to", str(target), "--force")
    assert code == 0
    assert tree(dest) == tree(skills_dir() / "revit-core")


def test_unknown_name_is_an_error_and_copies_nothing(tmp_path, capsys):
    code, out = run(capsys, "install", "revit-nope", "--to", str(tmp_path / "skills"))
    assert code == 2 and "revit-nope" in out and "revit-addin-dev" in out
    assert not (tmp_path / "skills").exists() or not list((tmp_path / "skills").iterdir())


def test_available_matches_the_plugin_skills_directory():
    assert list(skills_cli.available()) == NAMES
    for folder in skills_cli.available().values():
        assert skills_cli.describe(folder)


def test_install_needs_a_destination(capsys):
    with pytest.raises(SystemExit):
        server.main(["skills", "install"])
