"""Local operator interface. Never registered as an agent tool; no global publishing."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'QQBot'))
from agent.cognitive_state import CognitiveState
from agent.experience_service import ExperienceService


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('list','review','retire','feedback'):
        p=sub.add_parser(command);p.add_argument('--owner',required=True);p.add_argument('--scope-file',required=True)
        if command!='list':p.add_argument('--case',required=True)
        if command=='review':
            p.add_argument('--revision',required=True,type=int);p.add_argument('--review-file',required=True);p.add_argument('--evidence-file',required=True)
        if command=='retire':p.add_argument('--quarantine',action='store_true')
        if command=='feedback':p.add_argument('--task',required=True);p.add_argument('--outcome',choices=['confirmed_success','counterexample','unknown'],required=True)
    p=sub.add_parser('publish');p.add_argument('--selection-file',required=True,help='JSON [[case_id, reviewed_revision], ...]')
    p=sub.add_parser('rollback');p.add_argument('--release',required=True)
    p=sub.add_parser('drain');p.add_argument('--limit',type=int,default=20)
    sub.add_parser('gc')
    p=sub.add_parser('mode');p.add_argument('value',choices=['off','shadow','inject'])
    args=parser.parse_args()
    service=ExperienceService(CognitiveState(str(Path(args.data_root)/'_cognition')),mode='shadow')
    store=service.store
    scope=''
    if hasattr(args,'scope_file'):
        value=json.loads(Path(args.scope_file).read_text())
        if not isinstance(value,list) or len(value)!=4 or not all(isinstance(v,str) for v in value):raise ValueError('Scope must be the four-element runtime scope array')
        scope=json.dumps(value,ensure_ascii=False)
    if args.command=='mode':store.set_mode(args.value);result={'mode':args.value}
    elif args.command=='list':result=store.list_cases(args.owner,scope)
    elif args.command=='drain':result={'completed':service.drain(max(0,min(args.limit,1000)))}
    elif args.command=='gc':result={'purged':store.purge_expired()}
    elif args.command=='review':
        review=json.loads(Path(args.review_file).read_text());review['evidence_sha256']=hashlib.sha256(Path(args.evidence_file).read_bytes()).hexdigest()
        result={'revision':store.review(args.owner,scope,args.case,args.revision,review)}
    elif args.command=='publish':result={'release':store.release(json.loads(Path(args.selection_file).read_text()))}
    elif args.command=='rollback':store.rollback(args.release);result={'active_release':args.release}
    elif args.command=='retire':store.retire(args.owner,scope,args.case,args.quarantine);result={'retired':args.case}
    else:store.feedback(args.owner,scope,args.task,args.case,args.outcome);result={'feedback_recorded':True}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
