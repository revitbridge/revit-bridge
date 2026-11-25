"""Run the actual rendered delete_elements C# against a document with independently
specified ownership, dependency queries and deletion cascades. No Revit installation."""
from tests.csharp_simulation import simulate

from revit_bridge.capabilities.store import ToolStore


def test_classification_precedes_the_only_deletion_and_postchecks_precede_success(tmp_path):
    code, errors = ToolStore(user_dir=tmp_path / "user").render("delete_elements", {"element_ids": "10"})
    assert errors == [] and code.count("document.Delete(") == 1
    deletion = code.index("document.Delete(")
    for read in ("GetSubComponentIds(", "GetPanelIds(", "GetMullionIds(", "GetStackedWallMemberIds(",
                 "GetMemberIds(", "root.GetDependentElements(null)", "e.GetType().Name",
                 "if (independent)", "stairs.GetStairsSupports()"):
        assert code.index(read) < deletion
    assert code.count("GetDependentElements(") == 1
    for check in ("!classes.TryGetValue(id", "byClass[className]", "missing.Count > 0", "survivors.Count > 0"):
        assert deletion < code.index(check) < code.index('Status = "Deleted"')
    assert all(term not in code for term in ("FilteredElementCollector", "Settings.Categories", "CategoryType"))


def test_rendered_delete_and_transaction_rollback(tmp_path):
    cases = {name: {"element_ids": ids} for name, ids in (("One", "10"), ("Both", "10,11"), ("Empty", ""))}
    output = simulate(tmp_path, "delete_simulation", "delete_elements", cases)
    reports = [line for line in output.splitlines() if line.startswith("PASS ")]
    assert len(reports) == 70, output
    assert "PASS protect dependent FabricArea" in reports, output
