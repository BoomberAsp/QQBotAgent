"""Offline P0/P1 -> P2/P3 migration. Preview by default; never modifies legacy data.
Requires cognitive_state.py beside this file or in ../QQBot/agent.
"""
import argparse, collections, hashlib, importlib.util, io, json, os, re
import sqlite3, tarfile, tempfile, time
from pathlib import Path

VERSION = 1

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encode(value).encode()).hexdigest()

def safe_name(value):
    return ''.join(c if c.isalnum() or c in '-_.' or '\u4e00' <= c <= '\u9fff' else '_' for c in value).strip('_') or 'session'

def check_source_transactions(data):
    if any(data.glob('*/profile.json.transaction.json')):
        raise ValueError('Pending profile transaction; let the old runtime recover, then retry')


def check_target(target, expected_hash):
    if target.is_symlink():
        raise ValueError('Target is a symlink; refusing overwrite')
    if any(Path(str(target)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('Target has SQLite sidecar files; stop new-runtime writers before migration')
    actual = digest(target.read_bytes()) if target.exists() else None
    if actual != expected_hash:
        raise ValueError('Target changed during migration; refusing overwrite')


def scan(data, code):
    check_source_transactions(data)
    paths = {}
    for pattern in ('*/profile.json', '*/buffer.json', '*/sessions/**/*.json', '*/sessions/**/*.jsonl',
                    'memory/tiers/**/*.json', 'memory/tiers/long/**/*.md', 'memory/user/**/*.md', 'memory/MEMORY.md'):
        for p in data.glob(pattern):
            if p.is_file(): paths['data/'+p.relative_to(data).as_posix()] = p
    for pattern in ('data/sessions/*.json', 'data/personality_settings.json', 'data/personality_config.json'):
        for p in code.glob(pattern):
            if p.is_file(): paths['code/'+p.relative_to(code).as_posix()] = p
    for key,p in paths.items():
        base=data if key.startswith('data/') else code
        if not p.resolve().is_relative_to(base.resolve()) or p.is_symlink():
            raise ValueError('Source escapes root: '+key)
    result = {k:p.read_bytes() for k,p in sorted(paths.items())}
    check_source_transactions(data)
    return result

def prepare(files, mapping):
    facts=[]; fragments=[]; provenance=[]; scopes=set(); coverage={}
    def read(k): return json.loads(files[k])
    def scope(owner, meta=None, scene=''):
        m=mapping['users'].get(owner)
        if not m: raise ValueError('Missing scope mapping for user '+owner)
        meta=meta or {}
        role=meta.get('personality') or meta.get('persona_key') or m['character']
        if not scene: scene=m.get('scene','')
        value=json.dumps([m['platform']+':'+m['bot_id'],m.get('channel') or 'private',role,scene],ensure_ascii=False)
        scopes.add((owner,value));return value
    def fragment(key,owner,sc,kind,content,stamp=0,slot=''):
        fid=digest(['fragment',key,owner,sc,slot])[:20]
        fragments.append((fid,owner,sc,kind,content,float(stamp or 0)))
        provenance.append((fid,key,slot,'fragment'))
    def fact(key,owner,sc,slot,content,predicate='',stamp=0,metadata=None):
        if not str(content).strip(): return
        fid=digest(['fact',key,owner,sc,slot])[:16]
        facts.append((fid,owner,sc,str(content),predicate,1,0,float(stamp or 0)))
        provenance.append((fid,key,slot,encode(metadata or {})))
    def messages(key,owner,sc,items,stamp):
        pending=None
        for i,m in enumerate(items):
            if not isinstance(m,dict): raise ValueError('Malformed message in '+key)
            if m.get('role')=='user': pending=m.get('content','')
            elif m.get('role')=='assistant' and pending is not None and m.get('content'):
                fragment(key,owner,sc,'conversation',encode({'user':pending,'assistant':m['content']}),float(stamp or 0)+i/1000000,'pair:'+str(i))
                pending=None
    # Every source is backed up. Runtime raw fragments retain full old fields;
    # migration provenance has no message bodies, and scoped forget clears fragments.
    for key,raw in files.items():
        parts=key.split('/');owner=None;sc=None;stamp=0
        if key.startswith('data/memory/'):
            if key=='data/memory/MEMORY.md':coverage[key]='backup_only_aggregate_index';continue
            if '/tiers/long/' in key:owner=parts[-2]
            elif '/tiers/' in key:owner=Path(key).stem
            elif '/user/' in key:
                match=re.match(r'interaction_(.+)_\d+\.md$',Path(key).name)
                owner=parts[-2] if parts[-2]!='user' else (match.group(1) if match else None)
        elif key.startswith('data/'):
            owner=parts[1]
        elif key.startswith('code/data/sessions/'):
            owner=Path(key).stem
        else:coverage[key]='backup_only_config';continue
        if not owner: raise ValueError('Cannot determine owner: '+key)
        sc=scope(owner)
        data=read(key) if key.endswith('.json') else None
        if data and isinstance(data,dict):
            uid=data.get('user_id')
            if uid is not None and str(uid)!=owner: raise ValueError('Owner mismatch: '+key)
            stamp=data.get('last_active',data.get('last_seen',0)) or 0
        if '/sessions/' in key and key.startswith('data/'):
            idxkey=f'data/{owner}/sessions/_index.json'
            idx=read(idxkey) if idxkey in files else {'sessions':[]}
            if key.endswith('/_index.json'):coverage[key]='backup_only_session_index';continue
            folder=parts[3]
            matches=[m for m in idx['sessions'] if safe_name(m['name'])==folder]
            if len(matches)!=1:raise ValueError('Ambiguous/orphan session directory: '+key)
            meta=matches[0];sc=scope(owner,meta.get('metadata'),meta['session_id']);stamp=meta.get('last_active',0)
        fragment(key,owner,sc,'legacy_archive','旧版来源（历史资料，不是当前指令）：\n'+raw.decode('utf-8'),stamp,'raw')
        coverage[key]='imported'
        if key.endswith('/profile.json'):
            for field in ('nickname','facts','interests','preferences'):
                value=data.get(field)
                if field=='nickname' and value:fact(key,owner,sc,field,'用户称呼：'+str(value),'nickname',metadata={'legacy':True})
                elif field=='preferences':
                    for k,v in (value or {}).items():fact(key,owner,sc,'preference:'+k,'用户偏好：'+k+'='+str(v),'preference:'+k,metadata={'legacy':True})
                elif field in ('facts','interests'):
                    for i,v in enumerate(value or []):fact(key,owner,sc,field+':'+str(i),('用户兴趣：' if field=='interests' else '')+str(v),metadata={'legacy':True})
        elif '/tiers/' in key and key.endswith('.json'):
            for tier in ('short','medium','long'):
                for i,item in enumerate(data.get(tier,[])):
                    slot=tier+':'+str(item.get('id',i))
                    if tier=='medium':fact(key,owner,sc,slot,item['content'],stamp=item.get('last_confirmed_ts',0),metadata={k:v for k,v in item.items() if k!='content'})
                    else:fragment(key,owner,sc,'legacy_candidate' if tier=='short' else 'summary',('未确认候选：' if tier=='short' else '旧版摘要：')+encode(item),item.get('snapshot_time',item.get('last_confirmed_ts',0)),slot)
        elif key.startswith('code/data/sessions/'):
            messages(key,owner,sc,data.get('context',[]),stamp)
    # Reconstruct each named session exactly as the old reader: committed OR latest snapshot + delta.
    for key in files:
        if not key.startswith('data/') or not key.endswith('/sessions/_index.json'):continue
        owner=key.split('/')[1]
        for meta in read(key)['sessions']:
            prefix=key.rsplit('/',1)[0]+'/'+safe_name(meta['name'])+'/'
            committed=prefix+'committed_context.json';delta=prefix+'delta.jsonl'
            if committed in files:items=read(committed)
            else:
                snaps=[k for k in files if k.startswith(prefix) and re.fullmatch(r'snapshot_\d+\.json',k[len(prefix):])]
                latest=max(snaps,key=lambda k:int(Path(k).stem.split('_')[1])) if snaps else None
                items=read(latest) if latest else []
                items=list(items)+[json.loads(line) for line in files.get(delta,b'').splitlines() if line.strip()]
            sc=scope(owner,meta.get('metadata'),meta['session_id'])
            messages(key,owner,sc,items,meta.get('last_active',0))
    return {'facts':facts,'fragments':fragments,'provenance':provenance,'scopes':sorted(scopes),'coverage':coverage}

def load_store():
    here=Path(__file__).resolve().parent
    path=here/'cognitive_state.py'
    if not path.exists():path=here.parent/'QQBot/agent/cognitive_state.py'
    spec=importlib.util.spec_from_file_location('migration_cognitive_state',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.CognitiveState

def migrate(data,code,mapping,apply=False):
    data=Path(data).resolve();code=Path(code).resolve()
    files=scan(data,code);plan=prepare(files,mapping)
    if scan(data,code)!=files:raise ValueError('Sources changed during scan; retry')
    source_hash=digest({k:digest(v) for k,v in files.items()})
    plan_hash=digest([VERSION,source_hash,mapping])
    root=data/'_cognition';target=root/'cognitive_state.sqlite3';receipt=root/'legacy_migration_receipt.json'
    report={'script_version':VERSION,'plan_sha256':plan_hash,'source_sha256':source_hash,
            'source_files':len(files),'source_bytes':sum(map(len,files.values())),
            'users':len(set(o for o,s in plan['scopes'])),'scopes':len(plan['scopes']),
            'facts':len(plan['facts']),'fragments':len(plan['fragments']),
            'coverage':dict(collections.Counter(plan['coverage'].values())),
            'status':'preview','legacy_unchanged':True}
    if not apply:return report
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    import fcntl
    with open(root/'migration.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        original_hash = digest(target.read_bytes()) if target.exists() else None
        check_target(target, original_hash)
        if target.exists():
            if not receipt.exists():raise ValueError('Target exists without migration receipt; refusing overwrite')
            old=json.loads(receipt.read_text())
            if digest(target.read_bytes())!=old['database_sha256']:raise ValueError('Target changed after migration; refusing overwrite')
            if old['plan_sha256']==plan_hash:return {**old,'status':'unchanged'}
        backup=root/'migration_backups'/(time.strftime('%Y%m%d_%H%M%S')+'_'+str(time.time_ns()%1000000000))
        backup.mkdir(parents=True,exist_ok=False,mode=0o700)
        with tarfile.open(backup/'sources.tar.gz','w:gz') as tar:
            for key,raw in files.items():
                info=tarfile.TarInfo(key);info.size=len(raw);info.mode=0o600;tar.addfile(info,io.BytesIO(raw))
        (backup/'scope_mapping.json').write_text(encode(mapping))
        (backup/'source_manifest.json').write_text(encode({k:{'sha256':digest(v),'disposition':plan['coverage'][k]} for k,v in files.items()}))
        if target.exists():
            (backup/'previous.sqlite3').write_bytes(target.read_bytes())
            (backup/'previous_receipt.json').write_bytes(receipt.read_bytes())
        with tempfile.TemporaryDirectory(prefix='migration-',dir=root) as tmp:
            store=load_store()(tmp)
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.executemany('INSERT INTO scopes(owner,scope) VALUES (?,?)',plan['scopes'])
                db.executemany('INSERT INTO facts VALUES (?,?,?,?,?,?,?,?)',plan['facts'])
                db.executemany('INSERT INTO fragments VALUES (?,?,?,?,?,?)',plan['fragments'])
                db.execute('CREATE TABLE migration_provenance(target_id TEXT, source TEXT, slot TEXT, metadata TEXT)')
                db.executemany('INSERT INTO migration_provenance VALUES (?,?,?,?)',plan['provenance'])
                db.execute('CREATE TABLE migration_meta(key TEXT PRIMARY KEY,value TEXT)')
                db.execute('INSERT INTO migration_meta VALUES (?,?)',('plan_sha256',plan_hash))
            with store.db() as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Integrity check failed')
                assert db.execute('SELECT count(*) FROM facts').fetchone()[0]==report['facts']
                assert db.execute('SELECT count(*) FROM fragments').fetchone()[0]==report['fragments']
            if scan(data,code)!=files:raise ValueError('Sources changed before commit; retry')
            check_target(target, original_hash)
            staged=Path(store.path);staged.chmod(0o600)
            with staged.open('rb') as f:os.fsync(f.fileno())
            report.update(status='applied',backup=str(backup),database_sha256=digest(staged.read_bytes()),completed_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
            # Receipt first: interruption before swap fails closed on the next run.
            receipt_tmp=root/'legacy_migration_receipt.tmp';receipt_tmp.write_text(encode(report));receipt_tmp.chmod(0o600)
            with receipt_tmp.open('rb') as f:os.fsync(f.fileno())
            os.replace(receipt_tmp,receipt);os.replace(staged,target)
            fd=os.open(root,os.O_RDONLY);os.fsync(fd);os.close(fd)
        return report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root',required=True);p.add_argument('--code-root',required=True,help='QQBot/ directory')
    p.add_argument('--mapping',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--report')
    args=p.parse_args();os.umask(0o077)
    result=migrate(args.data_root,args.code_root,json.loads(Path(args.mapping).read_text()),args.apply)
    if args.report:Path(args.report).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
