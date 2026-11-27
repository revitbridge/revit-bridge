"""A parameter value written into C# cannot form code, whatever context it lands in.

The context check (tests/test_template.py) decides where a placeholder may sit. This file
covers the second line of defence: the text a value is rendered as, and the two template
shapes that slipped past the context check in review.
"""
import re

import pytest

from revit_bridge.capabilities.store import ToolStore, escape_param_value
from revit_bridge.capabilities.template import template_slots

from tests.conftest import legacy_escape_param_value

UNICODE_ESCAPE = re.compile(r"\\u([0-9a-f]{4})")
SIMPLE = {"\\\\": "\\", '\\"': '"', "\\r": "\r", "\\n": "\n", "\\0": "\x00"}

HOSTILE = [
    "", " ", "Level 1", "WALL_USER_HEIGHT_PARAM", 'a "b"', "back\\slash", "tail\\",
    "+Call()+", "{document.Delete(new ElementId(1L))}", "x; y();", "a.b", "a,b", "(a)", "[a]", "{a}",
    "// comment", "/* block */", "*/", "line\nbreak", "cr\rlf\r\n", "nul\x00", "nel\x85", "ls ps ",
    "'c'", "@verbatim", "$interp", "#if X", "tab\there", "nbsp\xa0", "bom﻿", "sub\x1a",
    "中文 标高", "emoji \U0001F600", "lone \ud800 surrogate", '"""', "'''", "=>", "?:", "::", "`", "~!%^&*|<>=",
]


def decode_units(text: str) -> list[int]:
    """What the C# compiler reads from the escaped text, as UTF-16 code units."""
    units, pos = [], 0
    while pos < len(text):
        match = UNICODE_ESCAPE.match(text, pos)
        if match:
            units.append(int(match[1], 16))
            pos = match.end()
        else:
            units.append(ord(text[pos]))
            pos += 1
    return units


def utf16_units(text: str) -> list[int]:
    data = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def legacy_units(text: str) -> list[int]:
    """The same for the 0.11.1 spelling (backslash escapes and unicode escapes)."""
    pattern = re.compile(r'\\\\|\\"|\\r|\\n|\\0|\\u[0-9a-f]{4}')
    out, pos = [], 0
    for match in pattern.finditer(text):
        out += utf16_units(text[pos:match.start()])
        token = match.group()
        out.append(ord(SIMPLE[token]) if token in SIMPLE else int(token[2:], 16))
        pos = match.end()
    return out + utf16_units(text[pos:])


@pytest.mark.parametrize("value", HOSTILE)
def test_escaped_text_holds_nothing_that_can_form_code(value):
    text = escape_param_value(value)
    assert re.fullmatch(r"(?:[A-Za-z0-9_ ]|\\u[0-9a-f]{4})*", text), text
    # No quote, bracket, operator, dot, semicolon, comment marker or line end survives.
    assert not re.search(r"[^A-Za-z0-9_ \\]", text)


@pytest.mark.parametrize("value", HOSTILE)
def test_the_compiler_reads_the_original_text_back(value):
    assert decode_units(escape_param_value(value)) == utf16_units(value)


@pytest.mark.parametrize("value", HOSTILE)
def test_it_denotes_the_same_string_as_the_0_11_1_spelling(value):
    assert decode_units(escape_param_value(value)) == legacy_units(legacy_escape_param_value(value))


@pytest.mark.parametrize("value,expected", [(True, "True"), (False, "False"), (3000, "3000"),
                                            (-2, "\\u002d2"), (2.5, "2\\u002e5"), (1e-8, "1e\\u002d08")])
def test_a_scalar_keeps_its_0_11_1_spelling_before_escaping(value, expected):
    assert escape_param_value(value) == expected
    assert decode_units(expected) == utf16_units(legacy_escape_param_value(value))


@pytest.mark.parametrize("value", [None, [1], {"a": 1}, b"bytes"])
def test_only_single_values_are_rendered(value):
    with pytest.raises(TypeError):
        escape_param_value(value)


def string_params(*names):
    return {name: {"name": name, "type": "string"} for name in names}


@pytest.mark.parametrize("line", [
    '#pragma warning disable """"',
    "#pragma warning disable 'x'",
    '#pragma checksum "file.cs" "{406EA660-64CF-4C82-B6F0-42D48172A799}" "ab"',
    '#pragma """"',
    '#pragma warning restore """',
    "#pragma warning disable 618 /* block */",
    "#pragma warning suppress 618",
    '#nullable enable "x"',
    "#nullable maybe",
])
def test_an_allowed_directive_line_has_one_fixed_shape(line):
    """Roslyn lexes a run of quotes on a directive line as a raw string that swallows the
    following lines, so the line may hold nothing but the directive itself."""
    template = line + '\n@"\n""""\nvar t = " + "{name}" + "//";\n;\nreturn t;'
    slots, errors = template_slots(template, string_params("name"))
    assert not slots and errors and "#pragma" in errors[0]


@pytest.mark.parametrize("line", [
    "#pragma warning disable 618", "#pragma warning restore 618", "#pragma warning disable CS0618, CS0612",
    "#pragma warning disable", "  #pragma warning restore 618 // back on", "#nullable disable",
    "#nullable enable annotations", "#nullable restore warnings",
])
def test_the_ordinary_directive_lines_stay_valid(line):
    slots, errors = template_slots(line + '\nstring x = "{name}";\nreturn x;', string_params("name"))
    assert not errors and [slot.name for slot in slots] == ["name"]


@pytest.mark.parametrize("template", [
    'var x = "{a}"""""\n  hello\n  """";\nvar q = @"\n"""""";\nvar t = " + "{b}" + "//";\n;\nreturn t;',
    'var x = "{a}""b";',
    'var x = "{a}"@"b";',
    'var x = "{a}"$"b";',
    'var x = @"a""{b}";',
    'var x = "{a}"$@"b";',
])
def test_a_literal_that_touches_another_literal_is_refused(template):
    """An empty value would let the quotes merge into a longer, raw delimiter."""
    slots, errors = template_slots(template, string_params("a", "b"))
    assert not slots and errors


def test_literals_separated_by_an_operator_stay_valid():
    slots, errors = template_slots('var x = "{a}" + "b" + @"c" + $"{1}";', string_params("a"))
    assert not errors and len(slots) == 1


@pytest.mark.parametrize("value", [v for v in HOSTILE if "\ud800" not in v])
def test_a_rendered_pack_carries_the_value_only_as_plain_text(tmp_path, value):
    store = ToolStore(user_dir=tmp_path / "packs")
    store.solidify("echo_value", 'string text = "{text}";\nreturn text;', "Echo a value.",
                   parameters=[{"name": "text", "type": "string", "description": "Text.",
                                "source": "designer", "required": True}])
    code, errors = store.render("echo_value", {"text": value})
    assert not errors
    assert code == f'string text = "{escape_param_value(value)}";\nreturn text;'
    assert code.count('"') == 2 and code.count(";") == 2 and "\n" in code and code.count("\n") == 1


def test_the_code_review_reads_text_as_the_compiler_does():
    """A unicode escape or a format character inside a blocked name does not hide it, and a
    blocked name in a rendered (escaped) value is still seen."""
    from revit_bridge.revit.sandbox import review

    backslash = chr(92)
    for code in ("dynamic x = 1;",
                 backslash + "u0064ynamic x = 1;",
                 "typeof(object)." + backslash + "u0041ssembly",
                 "System." + backslash + "u0049O.File.Exists(path)",
                 "Environment." + backslash + "u0045xit(0)",
                 "Regis" + chr(0xAD) + "try.CurrentUser",
                 'var name = "' + escape_param_value("System.IO.File") + '";'):
        assert review(code)[0] is False, code
    assert review('var name = "' + escape_param_value("Level 1 - core") + '"; return name;') == (True, [])
