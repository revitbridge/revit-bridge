"""Differential lexical checks against the SDK's Roslyn, without executing template code."""
import json
import random

from revit_bridge.capabilities.store import ToolStore
from tests.csharp_simulation import run_csharp_project


def test_seeded_renderings_preserve_roslyn_tokens_and_literal_values(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user", builtin_dir=tmp_path / "builtin")
    rng = random.Random(0x4B39)
    params = [{"name": "name"}, {"name": "n", "type": "double"}, {"name": "b", "type": "bool"}]
    alphabet = ['"', "\\", "{", "}", "{n}", "{name}", "\r\n", "\x85", "\u2028", "\u2029",
                "\x00", "\ud800", "\udfff", "💡", "/*", "//", "$", "@", "#endif", '"""', "abc"]
    samples = []
    for i in range(256):
        opening, closing, left, right = rng.choice([
            ('$"', '"', "{", "}"), ('$@"', '"', "{", "}"), ('@$"', '"', "{", "}"),
            ('$$"""', '"""', "{{", "}}"), ('$$$""""', '""""', "{{{", "}}}"),
        ])
        expr = rng.choice(["local", "(ok ? x : y)", 'F("}", new[] { x, y })', 'F($"{local}")',
                           'F("""(}""")', "(arr[i] /* } */ + 2)"])
        hole = left + expr + rng.choice(["", ",10", ",-8"]) + rng.choice(["", ":N2", "::(", ":000"]) + right
        prefix = "var label = " + opening + "prefix " + hole + " tail" + closing + ";\n"
        literal = rng.choice(['"{name}"', r'"\"{name}\\{name}"', '"left{name}{name}right"'])
        scalar_sites = rng.choice(["F({n}, {b});\n", "bool Flag() { return{b}; } F({n}.ToString());\n"])
        statements = [prefix, "var text = " + literal + ";\n", scalar_sites,
                      '/* quotes " and braces } */ var keep = "constant";\n']
        rng.shuffle(statements)
        code = rng.choice(["", "#pragma warning disable 618\n", "#nullable enable\n"]) + "".join(statements)
        store.solidify("sample", code, parameters=params)
        value = alphabet[i] if i < len(alphabet) else "".join(rng.choices(alphabet, k=rng.randrange(25)))
        number, csharp = rng.choice([(" +00019 ", "19"), ("-0.75", "-0.75"), (1e-8, "0.00000001"),
                                     ("9007199254740993", "9007199254740993")])
        flag, keyword = rng.choice([(True, "true"), ("FaLsE", "false")])
        rendered, errors = store.render("sample", {"name": value, "n": number, "b": flag})
        assert not errors, (i, errors)
        # A bare {n} is not a C# operand. Supply independent reference operands
        # and leave the string slots in the template for Roslyn to locate.
        reference = code.replace("return{b}", "return {b}").replace("{n}.", "{n} .")
        reference = reference.replace("{n}", csharp).replace("{b}", keyword)
        units = value.encode("utf-16-le", "surrogatepass")
        samples.append({"Reference": reference, "Rendered": rendered, "ValueUnits": [
            int.from_bytes(units[j:j + 2], "little") for j in range(0, len(units), 2)]})
    (tmp_path / "samples.json").write_text(json.dumps(samples), encoding="utf-8")
    assert run_csharp_project(tmp_path, "roslyn_template", roslyn=True).strip() == str(len(samples))
