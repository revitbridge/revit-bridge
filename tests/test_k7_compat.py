"""Byte snapshots taken on 5ad24d9, the parent of the K7 change."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from itertools import product

from mcp.client.session import LATEST_HANDSHAKE_VERSION

from revit_bridge import host_instructions
from revit_bridge import mcp_server as server
from revit_bridge.capabilities.store import ToolStore


def digest(texts):
    return sha256(b"\0".join(s.encode("utf-8") for s in texts)).hexdigest()


def test_instruction_variants_match_parent():
    texts = [server.mcp.instructions]
    for role, code, mentions, label in product(
        (None, "planner", "analyst", "supervisor"), (True, False), (True, False), (None, "Run approved task")
    ):
        texts.append(host_instructions(role, code, mentions, confirm_button_label=label))
    assert digest(texts) == "61bded0e5c35d8a87bd4ee903962cc41e9a456b0695dc2e332658cafe05d4572"


def stdio_output(cwd, data_dir):
    def read_catalog(process):
        def send(method, **fields):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, **fields}) + "\n")
            process.stdin.flush()

        def request(i, method, **fields):
            send(method, id=i, **fields)
            reply = json.loads(process.stdout.readline())
            assert reply["id"] == i, reply
            return reply["result"]

        init = request(1, "initialize", params={"protocolVersion": LATEST_HANDSHAKE_VERSION, "capabilities": {},
                                               "clientInfo": {"name": "example", "version": "1"}})
        send("notifications/initialized")
        items = request(2, "tools/list")["tools"]
        items = [item for item in items if item["name"] not in {"list_workflows", "get_workflow"}]
        items.extend(request(3, "resources/list")["resources"])
        items.extend(r for r in request(4, "resources/templates/list")["resourceTemplates"]
                     if r["uriTemplate"] != "revit://workflows/{name}")
        items.extend(request(5, "prompts/list")["prompts"])
        process.stdin.close()
        return [init["instructions"], *(json.dumps(i, ensure_ascii=False, separators=(",", ":")) for i in items)]

    # Anonymous stdio pipes work on Windows accounts that cannot open named pipes.
    with ThreadPoolExecutor(max_workers=1) as reader, subprocess.Popen(
        [sys.executable, "-m", "revit_bridge.mcp_server"], cwd=cwd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8",
        env=dict(os.environ, REVIT_BRIDGE_DATA_DIR=str(data_dir)),
    ) as process:
        try:
            output = reader.submit(read_catalog, process).result(timeout=30)
            assert process.wait(timeout=10) == 0
            return output
        finally:
            if process.poll() is None:
                process.kill()


def test_existing_stdio_descriptors_match_parent(isolated_data_dir):
    assert digest(stdio_output(None, isolated_data_dir)) == "eeb5ec78c292f4c054f34ddd98d40eea5e5f2cb2c9bd0b1f98b5a429225cfedd"


def test_list_tools_reply_matches_parent(tmp_path, monkeypatch):
    store = ToolStore(tmp_path / "user", tmp_path / "builtin")
    store.solidify("sample", "return 1;", "Example pack")
    monkeypatch.setattr(server, "_tool_store", store)
    assert digest([server.list_tools(), server.list_tools(True)]) == "bcd6db305d28129e836f6b60fa4b243a9933cfb1a8a7f9a317cd4b39c707b40f"
