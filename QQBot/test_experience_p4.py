import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent.cognitive_state import CognitiveState,scope_key,current_scope
from agent.experience_service import ExperienceService,extract_candidate,tool_versions,current_run
from agent.experience_store import fingerprint
from agent.tool_registry import ToolRegistry


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cognition=CognitiveState(self.root/'_cognition')
        self.scope=scope_key('qq_official:bot','','rubi')
        self.generation=self.cognition.generation('u',self.scope)
        self.service=ExperienceService(self.cognition,mode='shadow')
        self.store=self.service.store
        self.registry=ToolRegistry()
        self.registry.register('check_document',lambda path='': 'ok','检查文档',{'type':'object','properties':{'path':{'type':'string'}}})
        self.versions=tool_versions(self.registry)
    def tearDown(self):self.tmp.cleanup()
    def record(self,event='event1',goal='检查文档格式',result='200 OK',finish=True):
        run=self.service.start('u',self.scope,self.generation,event,goal,self.registry)
        run.observe('check_document',{'path':'/private/secret.txt'},result)
        if finish:run.finish()
        return run
    def candidate(self,event='event1'):
        self.record(event)
        return next(r for r in self.store.list_cases('u',self.scope) if r['payload']['source_event_id']==event)
    def validate(self,row,lesson='先检查格式再读取结果',outcome='success',conditions=None):
        review={'reviewer':'offline-operator','evidence_sha256':'a'*64,'observation_id':row['payload']['evidence_refs'][0],
            'lesson':lesson,'applicability':'文档格式检查，使用当前输入','steps':['检查格式','读取检查结果'],
            'outcome':outcome,'checked':True,'conditions':conditions or {}}
        rev=self.store.review('u',self.scope,row['id'],row['revision'],review)
        return row['id'],rev
    def retrieve(self,**kwargs):
        return self.store.retrieve('u',self.scope,self.generation,'检查文档',self.versions,'p4-v1',**kwargs)

    def test_shadow_capture_no_success_inference_or_argument_values(self):
        run=self.record(result='{"success":true,"status":200}')
        rows=self.store.list_cases('u',self.scope)
        self.assertEqual(1,len(rows));self.assertEqual('unknown',rows[0]['payload']['outcome'])
        self.assertIsNone(rows[0]['payload']['verification'])
        self.assertNotIn('/private/secret.txt',json.dumps(rows))
        self.assertEqual([],run.blocks());self.assertEqual([],self.retrieve()[1])
    def test_duplicate_event_is_exactly_once(self):
        self.record();self.record()
        self.assertEqual(1,len(self.store.list_cases('u',self.scope)))
        with self.store.transaction() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM experience.observations').fetchone()[0])
    def test_review_requires_observation_and_success_claim_cannot_publish(self):
        row=self.candidate()
        with self.assertRaises(ValueError):self.store.release([(row['id'],1)])
        with self.assertRaises(ValueError):self.store.review('u',self.scope,row['id'],1,{'checked':True,'outcome':'success'})
        wrong={'reviewer':'op','evidence_sha256':'a'*64,'observation_id':'invented','lesson':'检查', 'applicability':'文档','steps':['检查'],'outcome':'success','checked':True}
        with self.assertRaisesRegex(ValueError,'observed'):self.store.review('u',self.scope,row['id'],1,wrong)
    def test_private_scope_permission_version_and_environment_filters(self):
        selected=self.validate(self.candidate());self.store.release([selected])
        self.assertEqual(1,len(self.retrieve()[1]))
        for owner,scope in [('other',self.scope),('u',scope_key('qq_official:bot','group','rubi')),('u',scope_key('qq_official:bot','','other')),('u',scope_key('onebot:bot','','rubi'))]:
            gen=self.cognition.generation(owner,scope)
            self.assertEqual([],self.store.retrieve(owner,scope,gen,'检查文档',self.versions,'p4-v1')[1])
        self.assertEqual([],self.store.retrieve('u',self.scope,0,'检查文档',{},'p4-v1')[1])
        self.assertEqual([],self.store.retrieve('u',self.scope,0,'检查文档',self.versions,'other-env')[1])
    def test_conditions_and_no_low_relevance_fallback(self):
        pair=self.validate(self.candidate(),conditions={'session_kind':'named'});self.store.release([pair])
        self.assertEqual([],self.retrieve(context={'session_kind':'temporary'})[1])
        self.assertEqual(1,len(self.retrieve(context={'session_kind':'named'})[1]))
        self.assertEqual([],self.store.retrieve('u',self.scope,0,'天气如何',self.versions,'p4-v1',context={'session_kind':'named'})[1])
    def test_release_atomicity_rollback_and_retirement(self):
        pair=self.validate(self.candidate());rid=self.store.release([pair])
        with self.assertRaises(ValueError):self.store.release([pair,('missing',1)])
        self.assertEqual(rid,self.retrieve()[0])
        empty=self.store.release([]);self.assertEqual([],self.retrieve()[1])
        self.store.rollback(rid);self.assertEqual(1,len(self.retrieve()[1]))
        self.store.retire('u',self.scope,pair[0]);self.store.rollback(empty);self.store.rollback(rid)
        self.assertEqual([],self.retrieve()[1])
    def test_revision_conflict_and_frozen_release(self):
        row=self.candidate();pair=self.validate(row);rid=self.store.release([pair])
        with self.assertRaises(ValueError):self.validate(row)
        row=self.store.list_cases('u',self.scope)[0]
        revised=self.validate(row,lesson='新的复核建议');self.store.release([revised])
        self.store.rollback(rid)
        self.assertEqual('先检查格式再读取结果',self.retrieve()[1][0]['lesson'])
    def test_forget_deletes_candidate_job_versions_and_fences_late_worker(self):
        run=self.record(finish=False)
        self.store.submit('u',self.scope,0,run.task_id,{'goal':'检查文档','environment':'p4-v1','ts':time.time()})
        job=self.store.claim();case=extract_candidate(job)
        self.cognition.forget('u',self.scope)
        self.assertFalse(self.store.finish(job,case));self.assertEqual([],self.store.list_cases('u',self.scope))
        self.assertFalse(self.store.observe('u',self.scope,0,'late',1,{}))
        with self.store.transaction() as db:
            for table in ('cases','jobs','versions','observations'):
                self.assertEqual(0,db.execute('SELECT count(*) FROM experience.'+table).fetchone()[0])
    def test_forget_failure_rolls_back_cognitive_and_experience_mutations(self):
        self.cognition.ingest('u',self.scope,0,'batch',[{'content':'喜欢登山'}],['event'])
        self.candidate()
        from agent.experience_store import invalidate_attached
        def fail(*args):
            invalidate_attached(*args)
            raise RuntimeError('injected failure')
        with patch('agent.experience_store.invalidate_attached',side_effect=fail):
            with self.assertRaises(RuntimeError):self.cognition.forget('u',self.scope)
        self.assertEqual(0,self.cognition.generation('u',self.scope))
        self.assertEqual(1,len(self.cognition.list('u',self.scope)))
        self.assertEqual(1,len(self.store.list_cases('u',self.scope)))
    def test_lease_recovery_stale_worker_cannot_commit(self):
        run=self.record(finish=False)
        self.store.submit('u',self.scope,0,run.task_id,{'goal':'检查文档','environment':'p4-v1','ts':time.time()})
        first=self.store.claim(now=time.time());second=self.store.claim(now=time.time()+61)
        self.assertNotEqual(first['lease'],second['lease'])
        self.assertFalse(self.store.finish(first,extract_candidate(first)))
        self.assertTrue(self.store.finish(second,extract_candidate(second)))
    def test_extractor_failure_is_retry_not_negative_case(self):
        run=self.record(finish=False)
        self.store.submit('u',self.scope,0,run.task_id,{'goal':'检查文档','environment':'p4-v1','ts':time.time()})
        with patch('agent.experience_service.extract_candidate',side_effect=TimeoutError('test')), self.assertLogs('agent.experience_service',level='ERROR'):
            self.service.drain()
        self.assertEqual([],self.store.list_cases('u',self.scope))
        with self.store.transaction() as db:self.assertEqual('retry_wait',db.execute('SELECT status FROM experience.jobs').fetchone()[0])
    def test_counterexample_quarantines_without_promoting_usage(self):
        pair=self.validate(self.candidate());rid=self.store.release([pair]);cases=self.retrieve()[1]
        self.store.application('u',self.scope,'new-task',rid,cases,'injected')
        with self.assertRaises(ValueError):self.store.feedback('other',self.scope,'new-task',pair[0],'counterexample')
        self.store.feedback('u',self.scope,'new-task',pair[0],'confirmed_success')
        self.assertEqual(2,self.store.list_cases('u',self.scope)[0]['revision'])
        self.store.feedback('u',self.scope,'new-task',pair[0],'counterexample')
        self.assertEqual([],self.retrieve()[1]);self.store.rollback(rid);self.assertEqual([],self.retrieve()[1])
    def test_shadow_inject_budget_and_hot_off(self):
        self.store.release([self.validate(self.candidate())])
        run=self.service.start('u',self.scope,0,'next','检查文档',self.registry)
        self.assertEqual([],run.blocks())
        self.service.configured_mode='inject';blocks=run.blocks();self.assertEqual(1,len(blocks));self.assertLessEqual(len(blocks[0]),800)
        run.applied('');run.applied(blocks[0]);run.applied(blocks[0])
        with self.store.transaction() as db:self.assertEqual(1,db.execute("SELECT count(*) FROM experience.applications WHERE mode='injected'").fetchone()[0])
        self.service.configured_mode='off';self.assertEqual([],run.blocks());self.assertIsNone(self.service.start('u',self.scope,0,'off','检查文档',self.registry))
    def test_expiration_and_pausing_invalidate_recall(self):
        self.store.release([self.validate(self.candidate())]);self.store.purge_expired(time.time()+91*86400)
        self.assertEqual([],self.store.list_cases('u',self.scope));self.assertEqual([],self.retrieve()[1])
        self.store.release([self.validate(self.candidate('second'))]);self.cognition.pause('u',self.scope,True)
        self.assertEqual([],self.retrieve()[1]);self.cognition.pause('u',self.scope,False)
        self.assertEqual([],self.store.list_cases('u',self.scope))
    def test_worker_restart_recovers_and_stops(self):
        run=self.record(finish=False)
        self.store.submit('u',self.scope,0,run.task_id,{'goal':'检查文档','environment':'p4-v1','ts':time.time()})
        async def work():
            other=ExperienceService(self.cognition,mode='shadow')
            await other.start_recovery();await asyncio.sleep(0.02);await other.stop_recovery()
        asyncio.run(work());self.assertEqual(1,len(self.store.list_cases('u',self.scope)))
    def test_task_record_links_to_observed_run(self):
        from agent.task_record import build_record
        run=self.record(finish=False);token=current_run.set(run)
        try:
            record=build_record('检查','模型声称完成',task_id='parent-task')
            self.assertEqual('parent-task',record['task_id']);self.assertEqual(run.task_id,record['experience_task_id'])
            self.assertEqual('unknown',record['verification']['outcome'])
        finally:current_run.reset(token)
    def test_agent_integration_shadow_and_isolated_run(self):
        from agent.agent import Agent
        from agent.profile import ProfileManager
        from agent.session import SessionManager
        from agent.context import _current_evidence_id
        class Client:
            def __init__(self):self.calls=[]
            async def chat_completion_with_tools(self,messages,tools,**kwargs):
                self.calls.append(messages)
                if len(self.calls)%2:
                    return {'content':'','tool_calls':[{'id':'call1','type':'function','function':{'name':'check_document','arguments':'{}'}}]}
                return {'content':'检查完成','tool_calls':None}
        async def work():
            profiles=ProfileManager(str(self.root));client=Client()
            agent=Agent(client,self.registry,str(self.root/'configs'),session_manager=SessionManager(),profile_manager=profiles,experience_service=self.service)
            agent._audit_dir=str(self.root/'audit')
            token=_current_evidence_id.set(json.dumps(['qq_official','bot','','u','message1']))
            scope_token=current_scope.set(self.scope)
            try:
                with patch('agent.character_registry.get_character_registry') as registry:
                    registry.return_value.resolve_effective_personality.return_value='rubi';registry.return_value.load.return_value='';registry.return_value.get_short_name.return_value='rubi'
                    await agent.run('检查文档',user_id='u')
                    self.assertEqual(1,len(self.store.list_cases('u',self.scope)))
                    self.assertNotIn('历史任务经验',json.dumps(client.calls,ensure_ascii=False))
                    await agent.run('检查文档',user_id='u',persist=False)
                    self.assertEqual(1,len(self.store.list_cases('u',self.scope)))
                    self.assertIsNone(current_run.get())
            finally:
                _current_evidence_id.reset(token);current_scope.reset(scope_token)
        asyncio.run(work())

    def test_persisted_kill_switch_without_restart(self):
        service=ExperienceService(self.cognition)
        with patch.dict(os.environ,{'EXPERIENCE_MODE':'shadow'}):
            self.assertEqual('shadow',service.mode)
            self.store.set_mode('off');self.assertEqual('off',service.mode)
            self.assertIsNone(service.start('u',self.scope,0,'x','检查文档',self.registry))
            self.store.set_mode('shadow');self.assertEqual('shadow',service.mode)
    def test_permissions_are_observed_without_enabling_tool_reuse(self):
        run=self.service.start('u',self.scope,0,'blocked','检查文档',self.registry,allowed=set())
        run.observe('check_document',{},'[Permission] blocked','permission');run.finish()
        row=self.store.list_cases('u',self.scope)[0]
        self.assertEqual(['permission'],row['payload']['failure_type'])
        self.store.release([self.validate(row,outcome='failure')])
        self.service.configured_mode='inject'
        self.assertEqual([],run.blocks())
    def test_same_method_dedup_and_oversize_whole_case_skipped(self):
        pairs=[self.validate(self.candidate('a')),self.validate(self.candidate('b'))]
        self.store.release(pairs);self.assertEqual(1,len(self.retrieve()[1]))
        row=self.store.list_cases('u',self.scope)[0]
        pair=self.validate(row,lesson='长内容'*500);self.store.release([pair])
        self.service.configured_mode='inject'
        run=self.service.start('u',self.scope,0,'new','检查文档',self.registry)
        self.assertEqual([],run.blocks())

    def test_wal_mode_rejected_for_atomic_forget_guarantee(self):
        import sqlite3
        self.candidate()
        with sqlite3.connect(self.store.path) as db:db.execute('PRAGMA journal_mode=WAL')
        with self.assertRaisesRegex(ValueError,'rollback journals'):self.store.list_cases('u',self.scope)
        with self.assertRaisesRegex(ValueError,'rollback journals'):self.cognition.forget('u',self.scope)
        self.assertEqual(0,self.cognition.generation('u',self.scope))
    def test_admin_cli_review_publish_and_disable(self):
        import subprocess,sys
        row=self.candidate();scopefile=self.root/'scope.json';scopefile.write_text(self.scope)
        artifact=self.root/'evidence.txt';artifact.write_text('Local fixture checked')
        reviewfile=self.root/'review.json';reviewfile.write_text(json.dumps({'reviewer':'test-operator','checked':True,
            'observation_id':row['payload']['evidence_refs'][0],'lesson':'检查格式','steps':['检查格式'],
            'applicability':'离线文档测试','outcome':'success'}))
        cli=Path(__file__).resolve().parents[1]/'scripts/experience_admin.py'
        command=[sys.executable,str(cli),'--data-root',str(self.root)]
        result=subprocess.run(command+['review','--owner','u','--scope-file',str(scopefile),'--case',row['id'],
            '--revision','1','--review-file',str(reviewfile),'--evidence-file',str(artifact)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertEqual(2,json.loads(result.stdout)['revision'])
        selection=self.root/'selection.json';selection.write_text(json.dumps([[row['id'],2]]))
        result=subprocess.run(command+['publish','--selection-file',str(selection)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertTrue(json.loads(result.stdout)['release'])
        result=subprocess.run(command+['mode','off'],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('off',ExperienceService(self.cognition).mode)
    def test_shadow_hits_cannot_be_labelled_as_applied_success(self):
        pair=self.validate(self.candidate());rid=self.store.release([pair])
        self.store.application('u',self.scope,'shadow-task',rid,self.retrieve()[1],'shadow')
        with self.assertRaises(ValueError):self.store.feedback('u',self.scope,'shadow-task',pair[0],'confirmed_success')

    def test_crash_before_finalization_recovers_as_unknown(self):
        self.record(finish=False)
        self.assertIsNone(self.store.claim(now=time.time()))
        job=self.store.claim(now=time.time()+1801)
        self.assertIsNotNone(job)
        self.assertTrue(self.store.finish(job,extract_candidate(job)))
        self.assertEqual('unknown',self.store.list_cases('u',self.scope)[0]['payload']['outcome'])
    def test_published_experience_reaches_agent_only_in_inject_mode(self):
        from agent.agent import Agent
        from agent.profile import ProfileManager
        from agent.session import SessionManager
        from agent.context import _current_evidence_id
        self.store.release([self.validate(self.candidate())]);self.service.configured_mode='inject'
        class Client:
            async def chat_completion_with_tools(self,messages,tools,**kwargs):
                self.messages=messages
                return {'content':'请提供文档','tool_calls':None}
        async def run():
            client=Client();profiles=ProfileManager(str(self.root))
            agent=Agent(client,self.registry,str(self.root/'configs'),session_manager=SessionManager(),profile_manager=profiles,experience_service=self.service)
            token=_current_evidence_id.set(json.dumps(['qq_official','bot','','u','live-inject']))
            try:
                with patch('agent.character_registry.get_character_registry') as registry:
                    registry.return_value.resolve_effective_personality.return_value='rubi';registry.return_value.load.return_value='';registry.return_value.get_short_name.return_value='rubi'
                    await agent.run('检查文档',user_id='u')
                    self.assertIn('历史任务经验',json.dumps(client.messages,ensure_ascii=False))
                    self.service.configured_mode='off'
                    await agent.run('检查文档',user_id='u')
                    self.assertNotIn('历史任务经验',json.dumps(client.messages,ensure_ascii=False))
            finally:_current_evidence_id.reset(token)
        asyncio.run(run())

if __name__=='__main__':unittest.main()
