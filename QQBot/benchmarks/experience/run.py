"""Deterministic P4 correctness and retrieval ablation; no LLM quality claims."""
import hashlib
import io
import json
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root))
from agent.cognitive_state import CognitiveState,scope_key
from agent.experience_service import ExperienceService,tool_versions
from agent.tool_registry import ToolRegistry


def ablation():
    with tempfile.TemporaryDirectory() as tmp:
        cognition=CognitiveState(Path(tmp)/'_cognition');scope=scope_key('fixture:bot','','assistant')
        generation=cognition.generation('fixture-user',scope)
        service=ExperienceService(cognition,mode='shadow');store=service.store;registry=ToolRegistry()
        registry.register('check_document',lambda: 'fixture checked','检查文档',{'type':'object','properties':{}})
        versions=tool_versions(registry);pairs=[]
        for event,goal,outcome,lesson in [
            ('train-format','检查文档格式，确认字符编码','success','先核对文件编码再解析格式'),
            ('train-required','检查文档字段缺失问题','failure','缺少必填内容时向用户索取，不填造数据')]:
            run=service.start('fixture-user',scope,generation,event,goal,registry)
            run.observe('check_document',{},json.dumps({'fixture':event,'outcome':outcome}));run.finish()
            row=next(r for r in store.list_cases('fixture-user',scope) if r['payload']['source_event_id']==event)
            revision=store.review('fixture-user',scope,row['id'],1,{
                'reviewer':'offline-fixture','checked':True,'evidence_sha256':hashlib.sha256(event.encode()).hexdigest(),
                'observation_id':row['payload']['evidence_refs'][0],'lesson':lesson,'steps':[lesson],
                'applicability':'仅离线文档检查示例','outcome':outcome})
            pairs.append((row['id'],revision))
        results={}
        queries=['检查文档字符编码与字段要求','今天的天气如何']
        for label,selection in [('none',[]),('success_only',pairs[:1]),('success_and_failure',pairs)]:
            release=store.release(selection)
            counts=[];chars=[]
            for i,query in enumerate(queries):
                run=service.start('fixture-user',scope,generation,label+str(i),query,registry)
                service.configured_mode='inject';blocks=run.blocks();service.configured_mode='shadow'
                counts.append(len(blocks));chars.append(len('\n\n'.join(blocks)))
            _,foreign=store.retrieve('other-user',scope,generation,queries[0],versions,'p4-v1',release_id=release)
            results[label]={'matched_blocks':counts,'injected_characters':chars,'cross_owner_matches':len(foreign)}
        expected={'none':[0,0],'success_only':[1,0],'success_and_failure':[2,0]}
        assert all(results[k]['matched_blocks']==v for k,v in expected.items())
        assert all(max(r['injected_characters'])<=800 and r['cross_owner_matches']==0 for r in results.values())
        return results


def main():
    suite=unittest.defaultTestLoader.loadTestsFromName('test_experience_p4')
    output=io.StringIO();start=time.perf_counter()
    result=unittest.TextTestRunner(stream=output,verbosity=1).run(suite)
    if not result.wasSuccessful():print(output.getvalue(),file=sys.stderr)
    fixtures=ablation()
    paths=[root/'agent'/name for name in ('experience_store.py','experience_service.py','cognitive_state.py','agent.py','task_record.py','turn.py')]
    paths.extend([root/'test_experience_p4.py',root.parent/'scripts/experience_admin.py',Path(__file__)])
    report={'schema_version':1,'kind':'deterministic_offline_experience_correctness',
        'date':time.strftime('%Y-%m-%d'),'python':platform.python_version(),
        'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'source_sha256':{str(p.relative_to(root.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        'tests':{'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'elapsed_seconds':round(time.perf_counter()-start,3)},
        'retrieval_ablation':fixtures,
        'limitations':['Synthetic retrieval fixtures, not model task-success evaluation','No provider requests or production user data','Human review is a trusted operator assertion, not automatic proof of general usefulness','No real power-loss or multi-host filesystem test']}
    destination=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).with_name('baseline_p4.json')
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'tests':report['tests'],'retrieval_ablation':fixtures},ensure_ascii=False,indent=2))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
