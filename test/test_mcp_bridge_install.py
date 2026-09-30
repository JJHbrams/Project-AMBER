import json,tempfile,tomllib
from pathlib import Path
import pytest
from core.install.mcp_bridge_config import merge_claude,merge_codex
def test_claude_preserves_hooks_and_is_idempotent():
 with tempfile.TemporaryDirectory() as d:
  p=Path(d)/'c.json';p.write_text(json.dumps({'hooks':{'x':[1]},'mcpServers':{'other':{'url':'x'}}}))
  assert merge_claude(p,'py',['-u','b']);before=p.read_bytes();assert not merge_claude(p,'py',['-u','b']);assert p.read_bytes()==before
  x=json.loads(before);assert x['hooks']=={'x':[1]} and x['mcpServers']['other']=={'url':'x'};assert len(list(p.parent.glob('c.json.engram-bridge-backup-*')))==1
def test_codex_preserves_unrelated_bytes_and_rejects_inline():
 with tempfile.TemporaryDirectory() as d:
  p=Path(d)/'config.toml';p.write_text('[x]\na = 1\n\n[mcp_servers.other]\nurl = "x"\n')
  assert merge_codex(p,'py',['-u','b']);s=p.read_text();parsed=tomllib.loads(s);assert parsed['x']['a']==1 and parsed['mcp_servers']['other']['url']=='x' and parsed['mcp_servers']['engram']['startup_timeout_sec']==120
  bad=Path(d)/'bad.toml';bad.write_text('mcp_servers.engram = { command = "x" }\n')
  with pytest.raises(ValueError):merge_codex(bad,'py',[])

def test_malformed_inputs_leave_files_and_create_no_backup():
 with tempfile.TemporaryDirectory() as d:
  for name,func in [('bad.json',merge_claude),('bad.toml',merge_codex)]:
   p=Path(d)/name;p.write_text('{ bad' if name.endswith('json') else '[bad')
   before=p.read_bytes()
   with pytest.raises(ValueError):func(p,'py',[])
   assert p.read_bytes()==before and not list(p.parent.glob(p.name+'.engram-bridge-backup-*'))
