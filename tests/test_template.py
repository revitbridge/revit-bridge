"""C# template boundaries shared by pack validation, listing and rendering."""
import json
import random
import re

import pytest
import yaml

from revit_bridge import mcp_server
from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.capabilities.template import CODE, STRING, Lexer


def pack(code, ptype="string"):
    return {"schema_version": 1, "version": "1.0.0", "name": "probe", "code_template": code,
            "parameters": [{"name": "name", "type": ptype, "source": "answer", "required": True}]}


@pytest.mark.parametrize("ptype", ["string", "int", "double", "boolean"])
@pytest.mark.parametrize("source,context", [
    ('$"Level {name}"', "interpolated string literal"),
    ('$"escaped {{name}}"', "interpolated string literal"),
    (r'$"\" {name}"', "interpolated string literal"),
    ('$@"one ""two"" {name}"', "interpolated verbatim string literal"),
    ('@$"one ""two"" {name}"', "interpolated verbatim string literal"),
    ('@"one ""two"" {name}"', "verbatim string literal"),
    ('"""Level "{name}" """', "raw string literal"),
    ('""""embedded """ {name} """"', "raw string literal"),
    ('$$""""{{F("""}", new[] { 1 })}} "{name}" """"', "interpolated raw string literal"),
    ('$$"""{{name}}"""', "interpolated raw string literal"),
    ('$"{new[] { F("}", /* } */ $"{G("{name}")}") }}"', "interpolated string literal"),
    ('$@"{n:0""mm""} {name}"', "interpolated verbatim string literal"),
    ('// " {name}', "line comment"),
    ('/* " {name} */', "block comment"),
    (r"'\'' + '{name}'", "char literal"),
    (r'"\{name}"', "escape sequence in an ordinary string literal"),
])
def test_location_and_repair_for_each_forbidden_context(source, context, ptype):
    code = "int n = 1;\r\n  " + source
    [error] = validate_pack(pack(code, ptype))
    assert "{name}" in error and f"line 2, column {3 + source.index('{name}')}" in error
    assert f"({context})" in error and 'string x = "{name}";' in error


@pytest.mark.parametrize("source", ['return $"Level {name}";', 'return @"{name}";',
                                    'return """"{name}"""";', 'return {name};'])
def test_reviewer_shapes_cannot_be_saved_or_rendered_from_a_draft_file(tmp_path, source):
    store = ToolStore(user_dir=tmp_path, builtin_dir=tmp_path / "builtin")
    data = pack(source)
    with pytest.raises(ValueError, match="placeholder {name}"):
        store.solidify("probe", source, parameters=data["parameters"])
    assert not store.path_of("probe")
    (tmp_path / "probe.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    for value in ('{document.Delete(new ElementId(1L))}', '"', '""""'):
        assert store.render("probe", {"name": value}) == (None, validate_pack(data))


def test_bad_user_override_stays_listed_and_can_be_repaired(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    good = pack('return "{name}";')
    (builtin / "probe.yaml").write_text(yaml.safe_dump(good), encoding="utf-8")
    store = ToolStore(user_dir=tmp_path, builtin_dir=builtin)
    bad = pack("return {name};")
    user_file = tmp_path / "probe.yaml"
    user_file.write_text(yaml.safe_dump(bad), encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_tool_store", store)
    [listed] = json.loads(mcp_server.list_tools())
    assert listed["validation_errors"] == validate_pack(bad)
    assert store.health_check("probe")["status"] == "invalid"
    assert store.render("probe", {"name": "ok"}) == (None, listed["validation_errors"])
    with pytest.raises(ValueError, match="line 1, column"):
        store.update("probe", {"description": "still unsafe"})
    assert store.update("probe", {"code_template": good["code_template"]}).validation_errors == []
    assert "validation_errors" not in yaml.safe_load(user_file.read_text(encoding="utf-8"))
    assert store.render("probe", {"name": "ok"}) == ('return "ok";', [])


@pytest.mark.parametrize("opaque", [
    r'"escaped \" quotes and \\"', '@"a ""quote"""', '"""""three """ and four """" here"""""',
    r"'\''", '/* " */', '// "\u2028', '""""\n#endif\nquote """\n""""',
    '$"{{literal text}} {F(new[] { "}" }, $"{levelName}")}"',
    '$@"{F(@"""}""")} {levelName}"', '@$"{levelName}"', '$""',
    '$$""""single { brace and {{levelName}} and {{F("""}""")}} """"',
    '$"{n:\\"0\\"} {levelName}"',
])
def test_lexer_returns_to_code_after_literal_or_comment(tmp_path, opaque):
    code = opaque + ';\nstring levelName = "{name}";'
    store = ToolStore(user_dir=tmp_path)
    store.solidify("probe", code, parameters=pack(code)["parameters"])
    assert store.render("probe", {"name": "a", "levelName": "unbound"}) == (
        opaque + ';\nstring levelName = "a";', [])


def test_classifier_fails_closed_at_ambiguous_conditional_branch():
    code = '#if OFF\n/*\n#endif\n// */\nreturn "{name}";'
    assert "ambiguous preprocessor" in validate_pack(pack(code))[0]


@pytest.mark.parametrize("ptype,value,text", [
    ("float", 1e-8, "0.00000001"), ("double", 1e18, "1000000000000000000.0"),
    ("number", -1.25, "-1.25"), ("int", -2, "-2"), ("integer", 2**60 + 1, str(2**60 + 1)),
    ("bool", True, "true"), ("boolean", False, "false"),
])
def test_scalar_spelling_in_both_permitted_contexts(tmp_path, ptype, value, text):
    code = 'return F({name}, "{name}");'
    store = ToolStore(user_dir=tmp_path)
    store.solidify("probe", code, parameters=pack(code, ptype)["parameters"])
    assert store.render("probe", {"name": value}) == (f'return F({text}, "{text}");', [])
    assert store.render_code("probe", {"name": text}) is None


def test_invalid_defaults_and_fractional_ids_never_render(tmp_path):
    store = ToolStore(user_dir=tmp_path)
    for ptype, default in (("integer", 1.5), ("bool", "true"), ("double", "1"), ("string", 1)):
        code = 'return "{name}";'
        store.solidify("probe", code, parameters=[{"name": "name", "type": ptype, "default": default}])
        for params in ({}, {"name": default}):
            rendered, errors = store.render("probe", params)
            assert rendered is None and f"expects {ptype}" in errors[0]
    with pytest.raises(ValueError, match="empty default in bare code"):
        store.solidify("probe", "return {name};", parameters=[{"name": "name", "type": "int", "default": ""}])


def test_negative_bare_value_after_minus_preserves_the_subtraction(tmp_path):
    store = ToolStore(user_dir=tmp_path)
    code = 'return F(1-{n}, "-{n}");'
    store.solidify("probe", code, parameters=[{"name": "n", "type": "int"}])
    assert store.render("probe", {"n": -1}) == ('return F(1- -1, "--1");', [])


def _structure(code):
    """Keep all surrounding code, normalizing only literal bodies and typed scalar operands."""
    shape = []
    for token in Lexer(code).tokens():
        text = code[token.start:token.end]
        if token.context == STRING:
            text = '""'
        elif token.context == CODE:
            text = re.sub(r"\{(?:n|b)\}|-?[0-9]+(?:\.[0-9]+)?|\b(?:true|false)\b", "<scalar>", text)
        shape.append((token.context, text))
    return shape


def test_adversarial_values_preserve_template_token_structure(tmp_path):
    store = ToolStore(user_dir=tmp_path)
    # Both allowed contexts, repeated/adjacent slots, an already-escaped quote
    # and backslash, and real interpolation with an unbound C# local.
    code = r'var x = "\"\\{name}{name}/{n}/{b}"; return F({n}, {b}, $"{local}", x);'
    store.solidify("probe", code, parameters=[{"name": "name"}, {"name": "n", "type": "double"},
                                              {"name": "b", "type": "bool"}])
    rng = random.Random(918)
    alphabet = ['"', "\\", "{", "}", "{n}", "{name}", "\r\n", "\x85", "\u2028", "\u2029",
                "\ud800", "\udfff", "\x00", "/*", "//", "$", "@", "abc"]
    values = ["", *alphabet, *("".join(rng.choices(alphabet, k=rng.randrange(1, 25))) for _ in range(120))]
    shape = _structure(code)
    for value in values:
        number = rng.choice([1e-307, -1e22, -0.0, 3, 9.375])
        flag = rng.choice([True, False])
        rendered, problems = store.render("probe", {"name": value, "n": number, "b": flag})
        assert not problems and _structure(rendered) == shape
        # JSON-compatible spelling of ordinary C# escapes (except C#'s NUL)
        # independently checks that every inserted character stayed data.
        first = next(t for t in Lexer(rendered).tokens() if t.context == STRING)
        literal = rendered[first.start:first.end]
        as_json = re.sub(r'\\(?:\\|"|0)', lambda m: r"\u0000" if m[0] == r"\0" else m[0], literal)
        # A JSON decoder can combine adjacent high/low surrogates. Compare
        # the actual UTF-16 code units that a C# string stores.
        prefix = ('"\\' + value * 2 + "/").encode("utf-16-le", "surrogatepass")
        assert json.loads(as_json).encode("utf-16-le", "surrogatepass").startswith(prefix)
        for name in ("n", "b"):
            assert store.render_code("probe", {"name": "", "n": 1, "b": True, name: value}) is None
