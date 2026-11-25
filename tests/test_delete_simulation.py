"""Run the actual rendered delete_elements C# against a document with independently
specified ownership, dependency queries and deletion cascades. No Revit installation."""
from tests.csharp_simulation import simulate

from revit_bridge.capabilities.store import ToolStore


def test_classification_precedes_the_only_deletion_and_postchecks_precede_success(tmp_path):
    code, errors = ToolStore(user_dir=tmp_path / "user").render("delete_elements", {"element_ids": "10"})
    assert errors == [] and code.count("document.Delete(") == 1
    deletion = code.index("document.Delete(")
    for read in ("GetSubComponentIds(", "GetPanelIds(", "GetMullionIds(", "GetStackedWallMemberIds(",
                 "GetMemberIds(", "GetDependentElements(", "dependent.Category"):
        assert code.index(read) < deletion
    for check in ("modelDependents.Contains(id)", "!allowedExtras.Contains(id)", "missing.Count > 0", "survivors.Count > 0"):
        assert deletion < code.index(check) < code.index('Status = "Deleted"')
    assert "FilteredElementCollector" not in code and "Settings.Categories" not in code


def test_rendered_delete_and_transaction_rollback(tmp_path):
    cases = {name: {"element_ids": ids} for name, ids in (("One", "10"), ("Both", "10,11"), ("Empty", ""))}
    output = simulate(tmp_path, "delete_simulation", "delete_elements", cases)
    assert len([line for line in output.splitlines() if line.startswith("PASS ")]) == 28, output
