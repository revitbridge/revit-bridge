import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from revit_bridge.revit.client import RevitResponse
from revit_bridge.snapshot.query import RevitQueryExecutor


ROW = {"Id": 28, "Name": "Panel", "Category": "Walls"}


def test_trigger_selection_accepts_only_self():
    assert list(inspect.signature(RevitQueryExecutor.trigger_selection).parameters) == ["self"]


@pytest.mark.parametrize("reply", [ROW, [ROW], {"elements": [ROW]}])
def test_pick_returns_original_rows_without_discovery(reply):
    client = AsyncMock()
    client.send_code.return_value = RevitResponse(success=True, result=reply)
    result = asyncio.run(RevitQueryExecutor(client).trigger_selection())
    assert result == (reply if isinstance(reply, list) else [reply])
    snippet, = client.send_code.await_args.args
    assert "uidoc.Selection.PickObject(" in snippet
    assert 'Please select a host element (wall/floor)' in snippet
    client.send_command.assert_not_awaited()


@pytest.mark.parametrize("ok,reply,expected", [
    (True, ROW, [ROW]), (True, [ROW], [ROW]), (True, [], []), (False, None, []),
])
def test_failed_pick_uses_current_selection_once(ok, reply, expected):
    client = AsyncMock()
    client.send_code.return_value = RevitResponse(success=False, error="Pick ended")
    client.send_command.return_value = RevitResponse(success=ok, result=reply)
    assert asyncio.run(RevitQueryExecutor(client).trigger_selection()) == expected
    client.send_command.assert_awaited_once_with("get_selected_elements", {})
