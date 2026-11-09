#!/usr/bin/env python3
"""Check a deployed binding with the bot's real router; no NoneBot/QQ/network."""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.story_router import current_story_scope, make_scope, tool_search, hsr_injection

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--persona',default='herta')
parser.add_argument('--query',default='卡芙卡 道别')
args=parser.parse_args()
scope=make_scope(args.persona,'diagnostic')
if scope is None:
    parser.exit(1,'Persona has no story binding.\n')
current_story_scope.set(scope)
result=tool_search(args.query)
injection=hsr_injection(args.persona,args.query,scope) if scope.game=='hsr' else ''
print(json.dumps({'scope':asdict(scope),'tool_result':result,'auto_reference':injection},ensure_ascii=False,indent=2))
sys.exit(0 if not result.startswith('[story_search]') else 1)
