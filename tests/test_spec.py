"""TaskSpec models, the 5.1 validation rules (one test each), missing_params, reconcile."""
from __future__ import annotations

import json

import pytest

from revit_bridge.capabilities.store import ToolStore
from revit_bridge.snapshot.project import GridSummary, LevelInfo, ProjectSnapshot, TypeSummary
from revit_bridge.spec.models import (
    Action,
    Interpretation,
    ParamBinding,
    Source,
    TaskSpec,
    WorkflowState,
    canonical_json,
    projection_hash,
)
from revit_bridge.spec.rules import missing_params, reconcile, validate_spec


# -- helpers -----------------------------------------------------------------------------

def binding(name, value, source=Source.designer, evidence="the designer said so", unit=None, display=""):
    return ParamBinding(name=name, value=value, source=source, evidence=evidence, unit=unit, display=display)


def run_tool_spec(tool, *bindings, interpretations=(), task=None, fingerprint="abc", language="zh"):
    return TaskSpec(
        task=task or f"run {tool}",
        action=Action(kind="run_tool", tool=tool),
        parameters=list(bindings),
        interpretations=list(interpretations),
        snapshot_fingerprint=fingerprint,
        language=language,
    )


@pytest.fixture
def store(tmp_path):
    s = ToolStore(user_dir=tmp_path / "user")
    s.solidify(
        name="probe",
        code='return "{level_name}-{type_name}-{x}-{h}";',
        parameters=[
            {"name": "level_name", "type": "string", "source": "tool:levels", "choices_from": "levels",
             "description": "Target level", "required": True},
            {"name": "type_name", "type": "string", "source": "tool:family_types",
             "choices_from": "family_types:OST_Walls", "required": True},
            {"name": "x", "type": "double", "unit": "mm", "source": "designer", "required": True,
             "description": "X (mm)"},
            {"name": "h", "type": "double", "unit": "mm", "source": "default", "default": 3000},
        ],
    )
    return s


@pytest.fixture
def pack(store):
    return store.load("probe")


def snapshot(**overrides) -> ProjectSnapshot:
    base = dict(
        taken_at="2026-09-20T00:00:00Z", duration_ms=1,
        document={"title": "Project1", "revit_version": "2026", "is_workshared": False},
        units={"length": "mm", "raw": "autodesk.unit.unit:millimeters-1.0.1"},
        active_view=None,
        levels=[LevelInfo(id=1, name="L1", elevation_mm=0.0), LevelInfo(id=2, name="L2", elevation_mm=4000.0)],
        grids=GridSummary(count=0, names=[]),
        family_types=[TypeSummary(category="OST_Walls", count=2, names=["Generic - 200mm", "Generic - 300mm"])],
        selection=[], selection_count=0, links=[], phases=[], warnings=[], fingerprint="abc",
    )
    base.update(overrides)
    return ProjectSnapshot(**base)


def good_spec(task=None, language="zh", **changes):
    """A spec that passes every rule against the probe pack."""
    fields = dict(
        level_name=binding("level_name", "L1", Source.tool, "tool:get_tool_choices"),
        type_name=binding("type_name", "Generic - 200mm", Source.answer, "q_type_name"),
        x=binding("x", 1200, Source.designer, "1200 from the left", unit="mm"),
        h=binding("h", 3000, Source.default, "default:probe", unit="mm"),
    )
    fields.update(changes)
    return run_tool_spec("probe", *[b for b in fields.values() if b is not None], task=task, language=language)


def codes(errors):
    return [e.code for e in errors]


# -- models -------------------------------------------------------------------------------

def test_projection_canonical_json_and_hash():
    spec = good_spec()
    assert spec.execution_projection() == {
        "kind": "run_tool", "tool": "probe",
        "params": {"level_name": "L1", "type_name": "Generic - 200mm", "x": 1200, "h": 3000},
    }
    text = spec.canonical_json()
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert '"language":"zh"' in text and '{"action":{' in text      # sorted keys, no separators padding
    assert len(spec.spec_hash()) == 64 and spec.spec_hash() == good_spec().spec_hash()
    assert good_spec(x=binding("x", 1300, unit="mm")).spec_hash() != spec.spec_hash()

    # integral floats and ints hash alike; the order of keys does not matter; enums by value
    assert projection_hash({"kind": "run_tool", "params": {"x": 3000.0}}) == \
        projection_hash({"params": {"x": 3000}, "kind": "run_tool"})
    assert projection_hash({"params": {"x": 3000.5}}) != projection_hash({"params": {"x": 3000}})
    assert canonical_json({"s": Source.answer, "t": "墙"}) == '{"s":"answer","t":"墙"}'

    code = TaskSpec(task="c", action=Action(kind="execute_code", code="return 1;", code_parameters=[1]),
                    parameters=[], snapshot_fingerprint=None)
    assert code.execution_projection() == {"kind": "execute_code", "code": "return 1;", "parameters": [1]}


def test_card_shows_every_parameter_with_its_source():
    spec = good_spec()
    spec.interpretations = [Interpretation(param="x", text="x 按 mm 理解", confirmed=True)]
    spec.steps = ["query levels (done)", "run_tool", "count walls after"]
    spec.workflow = WorkflowState(name="walls", stage="confirm")
    card = spec.card()
    lines = card.splitlines()
    assert lines[0] == "Task: run probe" and lines[1] == "Tool: probe (run_tool)"
    assert lines[2] == "Parameters:"
    assert lines[3].startswith('  level_name = "L1"') and lines[3].endswith("source: tool (tool:get_tool_choices)")
    assert "  x          = 1200 mm" in card and "source: designer (1200 from the left)" in card
    assert "  h          = 3000 mm" in card and "source: default (default:probe)" in card
    assert "Interpretations:\n  [x] x 按 mm 理解" in card
    assert "Steps: 1 query levels (done)  2 run_tool  3 count walls after" in card
    assert lines[-1] == "Confirm? (yes / change something)"


# -- 5.1 rules, one test each ------------------------------------------------------------------

def test_rule_missing_param(pack):
    assert validate_spec(good_spec(), pack) == []
    errors = validate_spec(good_spec(level_name=None), pack)
    assert codes(errors) == ["missing_param"] and errors[0].param == "level_name"
    # h has a default: leaving it out is fine
    assert validate_spec(good_spec(h=None), pack) == []
    # run_tool without a pack at all
    errors = validate_spec(run_tool_spec("nope"), None)
    assert codes(errors) == ["missing_param"] and errors[0].param is None and "nope" in errors[0].message


def test_rule_no_evidence(pack):
    errors = validate_spec(good_spec(x=binding("x", 1200, Source.designer, "   ", unit="mm")), pack)
    assert codes(errors) == ["no_evidence"] and errors[0].param == "x"
    with pytest.raises(Exception):
        ParamBinding(name="x", value=1, source=Source.designer)       # evidence is required


def test_rule_unsourced_choice(pack):
    # a choices_from parameter given as the designer's words or a default is a guess
    for src, ev in ((Source.designer, "L1"), (Source.default, "default:probe"), (Source.preference, "preference:lvl")):
        errors = validate_spec(good_spec(level_name=binding("level_name", "L1", src, ev)), pack)
        assert "unsourced_choice" in codes(errors), src
    # tool or answer are the only acceptable sources
    assert validate_spec(good_spec(level_name=binding("level_name", "L1", Source.answer, "q_level_name")), pack) == []


def test_rule_guessed_value(pack):
    errors = validate_spec(good_spec(x=binding("x", 0, Source.tool, "tool:query", unit="mm")), pack)
    assert codes(errors) == ["guessed_value"] and errors[0].param == "x"
    assert "preference" in errors[0].message
    errors = validate_spec(good_spec(x=binding("x", 0, Source.default, "default:probe", unit="mm")), pack)
    assert "guessed_value" in codes(errors)
    # the designer's words, an answer, or a named preference (shown on the card) are all fine
    assert validate_spec(good_spec(x=binding("x", 0, Source.answer, "q_x", unit="mm")), pack) == []
    assert validate_spec(good_spec(x=binding("x", 0, Source.preference, "preference:origin", unit="mm")), pack) == []


def test_rule_default_not_declared(pack):
    errors = validate_spec(good_spec(type_name=binding("type_name", "Generic - 200mm", Source.default, "default:probe")), pack)
    assert "default_not_declared" in codes(errors)
    # a default binding for a parameter the pack does not know at all
    errors = validate_spec(good_spec(extra=binding("extra", 1, Source.default, "default:probe")), pack)
    assert codes(errors) == ["default_not_declared"] and errors[0].param == "extra"


def test_rule_bad_preference_ref(pack):
    errors = validate_spec(good_spec(h=binding("h", 3600, Source.preference, "my usual height", unit="mm")), pack)
    assert codes(errors) == ["bad_preference_ref"] and errors[0].param == "h"
    assert validate_spec(good_spec(h=binding("h", 3600, Source.preference, "preference:default_wall_height", unit="mm")), pack) == []


def test_rule_unit_interpretation(pack):
    # a numeric value without a unit where the pack declares one: an interpretation, not an error
    spec = good_spec(x=binding("x", 1200, Source.designer, "1200 from the left"))
    errors = validate_spec(spec, pack)
    assert codes(errors) == ["unconfirmed_interpretation"]
    assert errors[0].param == "x" and errors[0].message == "x 未标单位，按 mm 理解，请确认"
    spec.interpretations = [Interpretation(param="x", text="x 未标单位，按 mm 理解，请确认", confirmed=True)]
    assert validate_spec(spec, pack) == []
    # english template
    en = good_spec(x=binding("x", 1200, Source.designer, "1200 from the left"))
    en.language = "en"
    assert validate_spec(en, pack)[0].message == "x has no unit; reading it as mm, please confirm"


def test_rule_unconfirmed_interpretation(pack):
    spec = good_spec()
    spec.interpretations = [Interpretation(param=None, text="'F2 上' 理解为底部约束为 F2", confirmed=False)]
    errors = validate_spec(spec, pack)
    assert codes(errors) == ["unconfirmed_interpretation"] and errors[0].param is None
    spec.interpretations[0].confirmed = True
    assert validate_spec(spec, pack) == []


def test_rule_blocked_code():
    def code_spec(code):
        return TaskSpec(task="c", action=Action(kind="execute_code", code=code), parameters=[], snapshot_fingerprint=None)

    errors = validate_spec(code_spec('System.IO.File.Delete("x");'), None)
    assert codes(errors) == ["blocked_code"] and "System.IO" in errors[0].message
    assert codes(validate_spec(code_spec(""), None)) == ["blocked_code"]
    assert validate_spec(code_spec("return document.Title;"), None) == []


def test_rules_report_every_problem_at_once(pack):
    spec = good_spec(
        level_name=binding("level_name", "L1", Source.designer, ""),
        x=binding("x", 0, Source.tool, "tool:query"),
        h=binding("h", 3600, Source.preference, "usual"),
    )
    # x and h both lack a unit the pack declares: two interpretations to confirm
    assert sorted(codes(validate_spec(spec, pack))) == sorted([
        "no_evidence", "unsourced_choice", "guessed_value", "bad_preference_ref",
        "unconfirmed_interpretation", "unconfirmed_interpretation",
    ])


# -- 5.2 missing parameters --------------------------------------------------------------------

def test_missing_params_asks_for_required_unbound_parameters_with_real_options(pack):
    questions = missing_params(pack, {"x": 100}, snapshot())
    assert [q.param for q in questions] == ["level_name", "type_name"]
    q = questions[0]
    assert q.id == "q_level_name" and q.allow_other is True
    assert q.text == "请选择 level_name（Target level）："
    assert q.why == "probe 的必填参数，来源须为 tool:levels。"
    assert q.options == [
        {"label": "L1 (0.0mm)", "value": "L1", "source": "tool:levels"},
        {"label": "L2 (4000.0mm)", "value": "L2", "source": "tool:levels"},
    ]
    assert questions[1].options == [
        {"label": "Generic - 200mm", "value": "Generic - 200mm", "source": "tool:family_types"},
        {"label": "Generic - 300mm", "value": "Generic - 300mm", "source": "tool:family_types"},
    ]
    # without a snapshot: same questions, no options, "ask" wording; english templates
    questions = missing_params(pack, {}, None, language="en")
    assert [q.param for q in questions] == ["level_name", "type_name", "x"]
    assert questions[0].options == [] and questions[0].text == "Please provide level_name (Target level)."
    assert questions[2].why == "Required by probe; its source must be designer."
    assert missing_params(pack, {"level_name": "L1", "type_name": "t", "x": 0}, None) == []


# -- 5.2 reconciliation ------------------------------------------------------------------------

def test_reconcile_ready_when_everything_matches(pack):
    result = reconcile(good_spec(), snapshot(), pack)
    assert result.model_dump() == {"conflicts": [], "questions": [], "interpretations_required": [], "ready": True}


def test_reconcile_flags_values_that_do_not_exist(pack):
    result = reconcile(good_spec(level_name=binding("level_name", "L3", Source.answer, "q")), snapshot(), pack)
    assert [c.kind for c in result.conflicts] == ["not_found"]
    c = result.conflicts[0]
    assert c.param == "level_name" and c.claimed == "L3" and c.available == ["L1", "L2"]
    assert c.message == "level_name = 'L3' 在快照中不存在" and result.ready is False

    # case / whitespace differences only produce a hint, never an automatic fix
    result = reconcile(good_spec(level_name=binding("level_name", " l1", Source.answer, "q")), snapshot(), pack)
    assert result.conflicts[0].kind == "not_found" and result.conflicts[0].available[0] == "L1"
    assert "是否指 'L1'" in result.conflicts[0].message

    result = reconcile(good_spec(type_name=binding("type_name", "Generic 200mm", Source.answer, "q")), snapshot(), pack)
    assert result.conflicts[0].param == "type_name" and result.conflicts[0].available == ["Generic - 200mm", "Generic - 300mm"]

    # a category the snapshot did not cover cannot be checked
    result = reconcile(good_spec(), snapshot(family_types=[]), pack)
    assert result.conflicts == [] and result.ready is True


def test_reconcile_never_says_not_found_against_a_truncated_list(pack):
    """Review C-1: TypeSummary.names holds at most 50 of count; absence proves nothing."""
    snap = snapshot(family_types=[TypeSummary(category="OST_Walls", count=80,
                                              names=[f"Type {i:02d}" for i in range(50)])])
    result = reconcile(good_spec(type_name=binding("type_name", "Type 77", Source.answer, "q")), snap, pack)
    assert [c.kind for c in result.conflicts] == ["ambiguous"]
    c = result.conflicts[0]
    assert c.param == "type_name" and c.claimed == "Type 77" and len(c.available) == 50
    assert c.message == ("type_name = 'Type 77' 不在快照列出的前 50 个名字里（该类别共 80 个，名单被截断）；"
                         '请用 query("family_types", {"categories": ["OST_Walls"]}) 核对')
    assert result.ready is False
    # a value that is in the partial list is fine; a complete list still yields not_found
    assert reconcile(good_spec(type_name=binding("type_name", "Type 07", Source.answer, "q")), snap, pack).ready is True
    complete = snapshot(family_types=[TypeSummary(category="OST_Walls", count=50,
                                                  names=[f"Type {i:02d}" for i in range(50)])])
    result = reconcile(good_spec(type_name=binding("type_name", "Type 77", Source.answer, "q")), complete, pack)
    assert [c.kind for c in result.conflicts] == ["not_found"]
    en = good_spec(type_name=binding("type_name", "Type 77", Source.answer, "q"), language="en")
    assert reconcile(en, snap, pack).conflicts[0].message.endswith(
        'verify with query("family_types", {"categories": ["OST_Walls"]})')


def test_reconcile_flags_ambiguous_fuzzy_matches(pack):
    snap = snapshot(levels=[LevelInfo(id=1, name="L1", elevation_mm=0.0), LevelInfo(id=2, name="l 1", elevation_mm=100.0)])
    result = reconcile(good_spec(level_name=binding("level_name", "l1", Source.answer, "q")), snap, pack)
    assert result.conflicts[0].kind == "ambiguous" and result.conflicts[0].available == ["L1", "l 1"]


def test_reconcile_unit_missing_becomes_a_conflict_and_a_required_interpretation(pack):
    spec = good_spec(x=binding("x", 1200, Source.designer, "1200 from the left"))
    result = reconcile(spec, snapshot(), pack)
    assert [c.kind for c in result.conflicts] == ["unit_missing"]
    assert result.conflicts[0].available == ["mm"] and result.conflicts[0].message == "x = 1200 未标单位；probe 按 mm 计"
    assert [it.model_dump() for it in result.interpretations_required] == [
        {"param": "x", "text": "x 未标单位，按 mm 理解，请确认", "confirmed": False}]
    assert result.ready is False
    spec.interpretations = [Interpretation(param="x", text="x 未标单位，按 mm 理解，请确认", confirmed=True)]
    result = reconcile(spec, snapshot(), pack)
    assert result.conflicts == [] and result.interpretations_required == [] and result.ready is True


def test_reconcile_detects_a_stale_snapshot(pack):
    result = reconcile(good_spec(), snapshot(fingerprint="other"), pack)
    assert [c.kind for c in result.conflicts] == ["stale_snapshot"]
    assert result.conflicts[0].claimed == "abc" and result.conflicts[0].available == ["other"]
    # a draft that names no snapshot cannot be stale
    spec = good_spec()
    spec.snapshot_fingerprint = None
    assert reconcile(spec, snapshot(fingerprint="other"), pack).ready is True


def test_reconcile_asks_for_missing_parameters(pack):
    result = reconcile(good_spec(type_name=None), snapshot(), pack)
    assert [q.param for q in result.questions] == ["type_name"]
    assert result.questions[0].options[0]["value"] == "Generic - 200mm" and result.ready is False


def test_reconcile_requires_range_words_to_be_interpreted(pack):
    spec = good_spec(task="在 L2 上建三面墙")           # L2 scoped by a range word, bound nowhere
    result = reconcile(spec, snapshot(), pack)
    assert [it.model_dump() for it in result.interpretations_required] == [
        {"param": None, "text": "'在 L2 上' 理解为标高 L2（在该层上操作），请确认", "confirmed": False}]
    assert result.ready is False
    spec.interpretations = [Interpretation(param=None, text="'在 L2 上' 理解为标高 L2（在该层上操作），请确认", confirmed=True)]
    assert reconcile(spec, snapshot(), pack).ready is True

    # a level that is bound needs no interpretation; a bare level name without a range word neither
    assert reconcile(good_spec(task="在 L1 上建墙"), snapshot(), pack).interpretations_required == []
    assert reconcile(good_spec(task="L2 附近的墙"), snapshot(), pack).interpretations_required == []
    for phrase in ("整层 L2", "所有 L2 的墙", "L2 整层"):
        assert len(reconcile(good_spec(task=phrase), snapshot(), pack).interpretations_required) == 1, phrase
    en = good_spec(task="所有 L2 walls", language="en")
    assert reconcile(en, snapshot(), pack).interpretations_required[0].text == \
        "'所有 L2' is read as level L2 (work on that level); please confirm"


def test_reconcile_without_a_pack_or_with_unconfirmed_interpretations(pack):
    result = reconcile(run_tool_spec("nope"), snapshot(), None)
    assert result.conflicts[0].kind == "not_found" and result.conflicts[0].param == "tool"
    assert result.questions == [] and result.ready is False

    spec = good_spec()
    spec.interpretations = [Interpretation(param=None, text="x", confirmed=False)]
    assert reconcile(spec, snapshot(), pack).ready is False

    code = TaskSpec(task="c", action=Action(kind="execute_code", code="return 1;"), parameters=[], snapshot_fingerprint="abc")
    assert reconcile(code, snapshot(), None).ready is True
