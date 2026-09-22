"""The evidence ledger: one JSONL line per execution with every spec-9 field."""
from __future__ import annotations

import json

from revit_bridge.evidence.ledger import RECORD_FIELDS, Ledger, code_fields, new_id, summarize_result


def test_append_writes_every_field_and_get_finds_it(tmp_path):
    ledger = Ledger(tmp_path / "evidence")
    record_id = ledger.append({
        "host": "mcp", "action": "run_tool", "tool": "create_wall", "tool_version": "1.0.0",
        "spec_hash": "s" * 64, "projection_hash": "p" * 64, "token_prefix": "abc123",
        "confirmed_by": "designer", "channel": "chat", "params": {"level_name": "L1", "height": 3000},
        "code_sha256": None, "code_head": None,
        "document": {"title": "Project1", "revit_version": "2026"},
        "success": True, "error": None, "result_summary": {"ids": [1663902], "status": "Created"},
        "validation": {"validator": "count_delta", "passed": True, "checks": [], "before": {}, "after": {}},
        "duration_ms": 1234, "preconditions_failed": [], "warnings": [],
    })
    assert record_id.startswith("ev_") and len(record_id) == len("ev_20260920T071440_ab12cd")
    files = list((tmp_path / "evidence").glob("*.jsonl"))
    assert len(files) == 1 and files[0].name == f"{record_id[3:7]}-{record_id[7:9]}.jsonl"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert list(line) == list(RECORD_FIELDS)                       # every field, in spec order
    assert line["id"] == record_id and line["ts"].endswith("Z")
    assert line["tool"] == "create_wall" and line["params"]["height"] == 3000
    assert line["validation"]["passed"] is True and line["result_summary"]["ids"] == [1663902]
    assert ledger.get(record_id) == line
    assert ledger.get("ev_20000101T000000_ffffff") is None and ledger.get("nope") is None


def test_recent_is_newest_first_with_limit_and_tool_filter(tmp_path):
    ledger = Ledger(tmp_path / "evidence")
    ids = [ledger.append({"action": "run_tool", "tool": "create_wall" if i % 2 else "query_levels",
                          "id": f"ev_20260920T07{i:02d}00_{i:06d}"}) for i in range(5)]
    recent = ledger.recent(3)
    assert [r["id"] for r in recent] == list(reversed(ids))[:3]
    walls = ledger.recent(10, tool="create_wall")
    assert [r["id"] for r in walls] == [ids[3], ids[1]]
    assert ledger.recent(10, tool="nope") == []
    # a second month file sorts before the first one; corrupt lines are skipped
    older = ledger.append({"action": "execute_code", "id": "ev_20260819T120000_aaaaaa"})
    assert (tmp_path / "evidence" / "2026-08.jsonl").exists()
    (tmp_path / "evidence" / "2026-08.jsonl").open("a", encoding="utf-8").write("not json\n")
    assert [r["id"] for r in ledger.recent(10)][-1] == older
    assert Ledger(tmp_path / "missing").recent() == []
    # defaults fill the fields a partial record leaves out
    partial = ledger.get(ids[0])
    assert partial["warnings"] == [] and partial["preconditions_failed"] == [] and partial["params"] == {}
    assert partial["duration_ms"] == 0 and partial["validation"] is None


def test_summaries_and_code_fields():
    assert summarize_result({"ElementId": 5, "Status": "Created", "Message": "m" * 300}) == {
        "ids": [5], "status": "Created", "message": "m" * 200}
    assert summarize_result({"Status": "Deleted", "Count": 3}) == {"ids": [], "status": "Deleted", "count": 3}
    assert summarize_result([{"Id": 1}, {"Id": 2}]) == {"ids": [], "items": 2}
    assert summarize_result("Project1") == {"ids": [], "value": "Project1"}
    assert summarize_result(None) == {"ids": []}
    assert code_fields(None) == {"code_sha256": None, "code_head": None}
    fields = code_fields("x" * 300)
    assert len(fields["code_sha256"]) == 64 and fields["code_head"] == "x" * 200
    assert new_id() != new_id()


def test_scope_is_written_filtered_and_defaulted(tmp_path):
    """Phase 7: every line carries `scope`; lines from 0.2 read as `local`."""
    ledger = Ledger(tmp_path / "evidence")
    assert "scope" in RECORD_FIELDS and RECORD_FIELDS.index("scope") == RECORD_FIELDS.index("host") + 1
    local = ledger.append({"action": "run_tool", "tool": "query_levels", "id": "ev_20260922T090000_000001"})
    dev_a = ledger.append({"action": "execute_code", "scope": "dev_aaaaaaaaaaaa", "id": "ev_20260922T090100_000002"})
    dev_b = ledger.append({"action": "run_tool", "tool": "query_levels", "scope": "dev_bbbbbbbbbbbb",
                           "id": "ev_20260922T090200_000003"})
    dev_a2 = ledger.append({"action": "run_tool", "tool": "create_wall", "scope": "dev_aaaaaaaaaaaa",
                            "id": "ev_20260922T090300_000004"})
    lines = [json.loads(l) for l in (tmp_path / "evidence" / "2026-09.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["scope"] for l in lines] == ["local", "dev_aaaaaaaaaaaa", "dev_bbbbbbbbbbbb", "dev_aaaaaaaaaaaa"]
    assert all(list(l) == list(RECORD_FIELDS) for l in lines)
    assert ledger.get(local)["scope"] == "local" and ledger.get(dev_a)["scope"] == "dev_aaaaaaaaaaaa"

    assert [r["id"] for r in ledger.recent(10)] == [dev_a2, dev_b, dev_a, local]              # no filter: all
    assert [r["id"] for r in ledger.recent(10, scope="dev_aaaaaaaaaaaa")] == [dev_a2, dev_a]
    assert [r["id"] for r in ledger.recent(10, scope="local")] == [local]
    assert [r["id"] for r in ledger.recent(10, tool="query_levels", scope="dev_bbbbbbbbbbbb")] == [dev_b]
    assert [r["id"] for r in ledger.recent(1, scope="dev_aaaaaaaaaaaa")] == [dev_a2]
    assert ledger.recent(10, scope="dev_cccccccccccc") == []

    # a 0.2 line has no scope field at all: it belongs to the local add-in
    with (tmp_path / "evidence" / "2026-08.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "ev_20260819T120000_aaaaaa", "action": "run_tool", "tool": "query_levels",
                                 "host": "mcp"}) + "\n")
    old = ledger.get("ev_20260819T120000_aaaaaa")
    assert old["scope"] == "local" and "scope" not in json.loads(
        (tmp_path / "evidence" / "2026-08.jsonl").read_text(encoding="utf-8"))
    assert [r["id"] for r in ledger.recent(10, scope="local")] == [local, "ev_20260819T120000_aaaaaa"]
    assert ledger.recent(10, scope="dev_aaaaaaaaaaaa")[-1]["id"] == dev_a
