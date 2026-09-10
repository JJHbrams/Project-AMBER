"""Public MCP schema/logging checks import the server only in an isolated profile."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StateToolSchemaTests(unittest.TestCase):
    def test_identity_free_strict_schema_and_transport_log_filters(self):
        with tempfile.TemporaryDirectory(prefix="engram-state-tool-") as directory:
            profile = Path(directory)
            environment = dict(
                os.environ,
                HOME=str(profile),
                USERPROFILE=str(profile),
                APPDATA=str(profile / "AppData"),
                ENGRAM_SMOKE_DB_DIR=str(profile / "db"),
                PYTHONUTF8="1",
            )
            code = """
import asyncio
import logging
from unittest.mock import Mock
from pydantic import ValidationError
import mcp_server as server
assert 'engram_report_session_title' in server.engramMCP.instructions
tool = server.engramMCP._tool_manager.get_tool('engram_report_session_state')
assert set(tool.parameters['properties']) == {'state','label','subagent_count'}
assert tool.parameters['additionalProperties'] is False
for args in ({'state':'working','session_id':'MCP_PRIVATE_CANARY'}, {'state':'working','subagent_count':True}):
    try:
        tool.fn_metadata.arg_model.model_validate(args)
    except ValidationError as error:
        assert 'MCP_PRIVATE_CANARY' not in str(error)
    else:
        raise AssertionError('unexpected argument accepted')
server._mcp_presence.report_context = Mock()
server._mcp_presence.report_state = Mock(return_value={'accepted':True,'reason':'delivered'})
server.engramMCP.get_context = Mock(return_value=object())
asyncio.run(server.engramMCP.call_tool('engram_report_session_state', {'state':'working','label':'safe fixture'}))
assert server._call_log._buf[-1]['kwargs'] == {}
codex_tool = server.engramMCP._tool_manager.get_tool('engram_report_codex_event')
assert set(codex_tool.parameters['properties']) == {'event','turn_id','tool_name','tool_use_id','native_session_id','agent_id'}
assert codex_tool.parameters['additionalProperties'] is False
try:
    codex_tool.fn_metadata.arg_model.model_validate({'event':'Stop','turn_id':'1','session_id':'MCP_PRIVATE_CANARY'})
except ValidationError as error:
    assert 'MCP_PRIVATE_CANARY' not in str(error)
else:
    raise AssertionError('Codex hook accepted raw session identity')
server._mcp_presence.report_context.reset_mock()
server._mcp_presence.report_codex_event = Mock(return_value={'accepted':True,'reason':'delivered'})
asyncio.run(server.engramMCP.call_tool('engram_report_codex_event', {'event':'PreToolUse','turn_id':'PRIVATE_TURN','tool_name':'Bash'}))
assert server._call_log._buf[-1]['kwargs'] == {}
server._mcp_presence.report_context.assert_not_called()
hook_result = asyncio.run(server.engram_report_codex_event('PreToolUse','PRIVATE_TURN','Bash'))
assert hook_result.content[0].text == '{}'
title_tool = server.engramMCP._tool_manager.get_tool('engram_report_session_title')
assert set(title_tool.parameters['properties']) == {'title'}
assert title_tool.parameters['additionalProperties'] is False
try:
    title_tool.fn_metadata.arg_model.model_validate({'title':'Safe generated title','session_id':'MCP_PRIVATE_CANARY'})
except ValidationError as error:
    assert 'MCP_PRIVATE_CANARY' not in str(error)
else:
    raise AssertionError('title identity unexpectedly accepted')
server._mcp_presence.report_title = Mock(return_value={'accepted':True,'reason':'delivered'})
asyncio.run(server.engramMCP.call_tool('engram_report_session_title', {'title':'Safe generated title'}))
assert server._call_log._buf[-1]['kwargs'] == {}
for name, message in [('mcp.server.streamable_http_manager','Created session %s'),
                      ('mcp.server.streamable_http','Terminating session %s')]:
    record = logging.LogRecord(name, logging.INFO, '', 1, message, ('a'*32,), None)
    logging.getLogger(name).filter(record)
    assert 'a'*32 not in record.getMessage()
print('schema and privacy PASS')
"""
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2500:])
            self.assertIn("schema and privacy PASS", result.stdout)
