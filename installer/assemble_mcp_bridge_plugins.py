"""Build optional local provider bundles at their final local installation path."""
import argparse
import json
from pathlib import Path
import shutil


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--templates',type=Path,default=Path(__file__).with_name('mcp-bridge'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--discovery',type=Path,default=Path.home()/'.engram/overlay-state-api-v1.json')
    args=parser.parse_args()
    if not args.executable.is_file(): raise ValueError('bridge executable missing')
    for provider in ('claude','codex'):
        destination=args.output.resolve()/provider
        if destination.exists(): raise ValueError('output already exists; choose a new bundle directory')
        shutil.copytree(args.templates/provider,destination)
        executable=destination/'engram-mcp-bridge.exe'
        shutil.copy2(args.executable,executable)
        path=destination/'.mcp.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        entry=data['mcpServers']['engram']
        entry['command']=str(executable)
        position=entry['args'].index('--discovery-file')
        entry['args'][position+1]=str(args.discovery.resolve())
        path.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    return 0


if __name__=='__main__': raise SystemExit(main())
