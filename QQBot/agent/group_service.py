"""Group administration independent of NoneBot; all mutations use store transactions."""
import hashlib,json,re,secrets,time
from pathlib import Path
from .group_state import Conflict

class GroupService:
    def __init__(self,store,personas=None):
        self.store=store
        if personas is None:
            from .character_registry import get_character_registry
            personas=get_character_registry()
        self.personas=personas

    def resolve_persona(self,name):
        if name in ('默认','default'):name=self.personas.get_default()
        if self.personas.is_builtin(name):return name
        matches=[p['key'] for p in self.personas.list_personalities() if self.personas.is_builtin(p['key']) and name in (p.get('name'),p.get('display_name'))]
        if len(matches)!=1:raise ValueError('请选择明确的内置人格名称；不支持自创人格')
        return matches[0]

    def patch(self,scope,patch,actor,role,expected=None,prompt=False):
        if not isinstance(patch,dict):raise ValueError('配置必须为对象')
        allowed={'memo','persona','features','mode','receive_all','quiet','proactive_stickers','story_progress','history'}
        if set(patch)-allowed:raise ValueError('包含不可写字段')
        if role not in ('admin','group_or_bot'):raise PermissionError('需要群管理员或机器人管理员权限')
        patch=dict(patch)
        if 'persona' in patch:
            if role!='admin':raise PermissionError('群人格仅机器人管理员可修改')
            patch['persona']=self.resolve_persona(patch['persona'])
            if patch.get('history') not in ('keep','clear'):raise ValueError('请选择保留或清空历史')
        elif 'history' in patch:raise ValueError('history仅用于人格切换')
        for k in ('receive_all','proactive_stickers'):
            if k in patch and type(patch[k]) is not bool:raise ValueError(k+'必须为布尔值')
        if 'memo' in patch and (not isinstance(patch['memo'],str) or len(patch['memo'])>40):raise ValueError('备注最多40字')
        if 'mode' in patch and patch['mode'] not in ('mention','follow','natural'):raise ValueError('参与模式无效')
        if 'quiet' in patch and (not isinstance(patch['quiet'],str) or (patch['quiet'] and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d',patch['quiet']))):raise ValueError('安静时段格式错误')
        if 'features' in patch:
            f=patch['features']
            if not isinstance(f,dict) or set(f)-{'image','voice','gacha'} or any(type(v) is not bool for v in f.values()):raise ValueError('功能开关无效')
        if 'story_progress' in patch:
            from .group_resources import registry
            registered=registry()[0].get('knowledge_bases',{})
            v=patch['story_progress']
            if not isinstance(v,dict) or any(k not in registered or type(n) is not int or not 0<=n<=1000000 for k,n in v.items()):raise ValueError('剧情库或章节无效')
        def apply(db,c,m,row):
            old=c['persona'];new=patch.get('persona',old)
            for k,v in patch.items():
                if k=='history':continue
                if k in ('features','story_progress'):c[k]={**c.get(k,{}),**v}
                else:c[k]=v
            if patch.get('receive_all') is False:c['mode']='mention'
            if c['mode']!='mention' and not c['receive_all']:raise ValueError('请先开启非 @ 上下文接收')
            if new!=old:
                db.execute('DELETE FROM group_actions WHERE scope=?',(scope,))
                notice=f'本群人格已从「{old}」切换为「{new}」。历史回复风格不代表当前人格，从现在起仅使用当前人格的语气、自称与用词。'
                if patch['history']=='clear':
                    db.execute('DELETE FROM turns WHERE scope=?',(scope,));db.execute('DELETE FROM notices WHERE scope=?',(scope,))
                else:
                    db.execute('INSERT INTO notices(scope,created,text) VALUES (?,?,?)',(scope,time.time(),notice))
                    db.execute('DELETE FROM notices WHERE scope=? AND id NOT IN (SELECT id FROM notices WHERE scope=? ORDER BY id DESC LIMIT 10)',(scope,scope))
                c['persona_pending']=dict(operation_id=secrets.token_hex(8),by=actor,expected_generation=row['generation']+1,old=old,new=new,expires=time.time()+600) if prompt else None
        return self.store.update_state(scope,apply,expected,invalidate=bool(set(patch)-{'memo'}))

    def choose_history(self,scope,actor,role,operation,choice,expected=None):
        if role!='admin':raise PermissionError('仅切换发起的机器人管理员可操作')
        if choice not in ('keep','clear'):raise ValueError('请选择保留或清空')
        def apply(db,c,m,row):
            p=c.get('persona_pending')
            if not p or p['operation_id']!=operation or p['by']!=actor or p['expires']<time.time() or p['expected_generation']!=row['generation']:raise ValueError('此切换确认已失效，请刷新面板')
            if choice=='clear':
                db.execute('DELETE FROM turns WHERE scope=?',(scope,));db.execute('DELETE FROM notices WHERE scope=?',(scope,))
            c['persona_pending']=None
        return self.store.update_state(scope,apply,expected,invalidate=choice=='clear')

    def expire_pending(self,scope):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT config FROM groups WHERE scope=?',(scope,)).fetchone()
            if row:
                c=json.loads(row[0]);p=c.get('persona_pending')
                if p and p['expires']<time.time():
                    c['persona_pending']=None
                    db.execute('UPDATE groups SET config=?,config_revision=config_revision+1 WHERE scope=?',(json.dumps(c),scope))

    def token(self,scope,actor,action,expected=None):
        token=secrets.token_hex(12);state=self.store.group(scope)
        with self.store.db() as db:
            db.execute('DELETE FROM group_actions WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO group_actions VALUES (?,?,?,?,?,?)',(token,scope,actor,state['config_revision'] if expected is None else expected,json.dumps(action),time.time()+600))
        return token

    def consume(self,scope,actor,token,role):
        # Single-use reservation; all actions still CAS against the captured revision.
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM group_actions WHERE token=? AND scope=? AND actor=? AND expires>?',(token,scope,actor,time.time())).fetchone()
            if row is None:raise ValueError('按钮已失效或不属于当前操作者，请刷新面板')
            action=json.loads(row['action'])
            if action['kind'] in ('persona','history') and role!='admin':raise PermissionError('仅机器人管理员可操作')
            if role not in ('admin','group_or_bot'):raise PermissionError('请通过群命令重新验证管理权限')
            db.execute('DELETE FROM group_actions WHERE token=?',(token,))
        if action['kind']=='history':return self.choose_history(scope,actor,role,action['operation'],action['choice'],row['revision'])
        if action['kind']=='reset':return self.store.reset(scope,clear=action.get('clear',False),expected=row['revision'])
        if action['kind']=='close':
            with self.store.db() as db:db.execute('DELETE FROM group_actions WHERE scope=? AND actor=?',(scope,actor))
            return self.store.group(scope)
        return self.patch(scope,action['patch'],actor,role,row['revision'],prompt=action['kind']=='persona')

    def delete_fact(self,scope,fid,expected):
        def apply(db,c,m,row):
            if not db.execute("SELECT 1 FROM facts WHERE scope=? AND id=? AND status='active'",(scope,fid)).fetchone():raise ValueError('未找到本群来源事实')
            self.store.revoke_facts(db,scope,fid)
        return self.store.update_state(scope,apply,expected)

    def observe(self,scope,kind,timestamp,event=''):
        self.store.group(scope)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT config FROM groups WHERE scope=?',(scope,)).fetchone();c=json.loads(row[0])
            if kind=='full':
                if timestamp<=c.get('last_full_event_at',0):return
                c['last_full_event_at']=timestamp
                if timestamp>=c.get('platform_state_at',0):c['platform_full_events']='on'
                invalid=False
            else:
                if timestamp<=c.get('platform_state_at',0):return
                c['platform_state_at']=timestamp;c['platform_state_event']=event
                if kind in ('added','removed'):c['status']='active' if kind=='added' else 'removed'
                c['active_enabled']=kind in ('added','receive')
                c['notification_state']='on' if c['active_enabled'] else 'off'
                invalid=True
            db.execute('UPDATE groups SET config=?,generation=generation+?,config_revision=config_revision+? WHERE scope=?',(json.dumps(c),int(invalid),int(invalid),scope))

    @staticmethod
    def gid(scope):return hashlib.sha256(scope.encode()).hexdigest()

    def scope(self,gid):
        with self.store.db() as db:rows=db.execute('SELECT scope FROM groups').fetchall()
        for r in rows:
            if self.gid(r[0])==gid:return r[0]
        raise KeyError('未找到群')

    def snapshot(self,scope):
        self.expire_pending(scope);state=self.store.group(scope);parts=json.loads(scope)
        with self.store.db() as db:
            stats=db.execute('SELECT count(*),max(created) FROM events WHERE scope=?',(scope,)).fetchone()
            state['deliveries']=[dict(r) for r in db.execute('SELECT id,event,status,created FROM deliveries WHERE scope=? ORDER BY created DESC LIMIT 20',(scope,))]
            state['facts_count']=db.execute("SELECT count(*) FROM facts WHERE scope=? AND status='active'",(scope,)).fetchone()[0]
            state['turn_count']=db.execute('SELECT count(*) FROM turns WHERE scope=?',(scope,)).fetchone()[0]
            state['facts']=[dict(r) for r in db.execute("SELECT id,event,visibility,created FROM facts WHERE scope=? AND status='active' ORDER BY created DESC LIMIT 200",(scope,))]
        state.update(gid=self.gid(scope),platform=parts[0],bot_id=parts[1],group_id=parts[3],recent_event_count=stats[0],last_active=stats[1],invalid_persona=not self.personas.is_builtin(state['config']['persona']))
        return state

    def listing(self):
        with self.store.db() as db:scopes=[r[0] for r in db.execute('SELECT scope FROM groups')]
        return [{k:v for k,v in self.snapshot(scope).items() if k not in ('facts','memory','deliveries')} for scope in scopes]

    def legacy(self,root):
        result=[]
        for name,field in [('group_personality.json','persona'),('group_features.json','features')]:
            path=Path(root)/name
            try:data=json.loads(path.read_text())
            except (OSError,ValueError):continue
            for group,value in data.items():
                entry=dict(group=str(group),field=field,value=value)
                ident=hashlib.sha256((name+json.dumps(entry,sort_keys=True)).encode()).hexdigest()
                with self.store.db() as db:
                    db.execute('INSERT OR IGNORE INTO legacy_migrations VALUES (?,?,?,?,?,?,?)',(ident,name,json.dumps(entry),'','pending','',time.time()))
        with self.store.db() as db:
            for row in db.execute('SELECT * FROM legacy_migrations ORDER BY created'):
                item=dict(row);item['entry']=json.loads(item['entry']);result.append(item)
        return result

    def migrate(self,ident,scope,actor,expected,ignore=False):
        if ignore:
            with self.store.db() as db:
                db.execute("UPDATE legacy_migrations SET status='ignored',actor=? WHERE id=? AND status='pending'",(actor,ident))
            return
        def apply(db,c,m,row):
            old=db.execute("SELECT entry FROM legacy_migrations WHERE id=? AND status='pending'",(ident,)).fetchone()
            if not old:raise ValueError('迁移记录已处理')
            entry=json.loads(old[0]);raw=json.loads(row['config'])
            if entry['group']!=json.loads(scope)[3]:raise ValueError('目标群不匹配')
            field=entry['field']
            if field in raw and not (field=='persona' and raw[field]=='assistant' and row['config_revision']==0 and not db.execute('SELECT 1 FROM turns WHERE scope=? LIMIT 1',(scope,)).fetchone()):raise ValueError('目标已有配置，不能覆盖；请手动配置后忽略旧记录')
            if field=='persona':
                if not self.personas.is_builtin(entry['value']):raise ValueError('旧自创人格不可迁入；请选择内置人格并忽略此记录')
                c[field]=entry['value']
            else:
                if not isinstance(entry['value'],dict) or set(entry['value'])-{'image','voice','gacha'} or any(type(v)is not bool for v in entry['value'].values()):raise ValueError('旧功能配置无效')
                c[field]={**c['features'],**entry['value']}
            db.execute("UPDATE legacy_migrations SET target=?,status='applied',actor=? WHERE id=?",(scope,actor,ident))
        return self.store.update_state(scope,apply,expected)
