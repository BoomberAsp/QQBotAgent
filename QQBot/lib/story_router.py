"""Authoritative persona -> game binding shared by tools, injection and delegation."""
import contextvars
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .hsr_story import HsrStoryKB, render

log=logging.getLogger(__name__)
current_story_scope=contextvars.ContextVar('current_story_scope', default=None)
_lock=threading.RLock()
_registries={}
_readers={}


def audit(message,*args):
    try:
        from loguru import logger
        logger.info(message % args)
    except ImportError:
        log.info(message,*args)


def registry():
    path=Path(os.environ.get('STORY_KB_REGISTRY') or Path(__file__).resolve().parents[1]/'config/story_kb_registry.json').resolve()
    with _lock:
        try:
            raw=path.read_bytes(); version=hashlib.sha256(raw).hexdigest()
            if path in _registries and _registries[path][0]==version:
                return _registries[path]
            data=json.loads(raw)
            if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('bindings'),dict) or not isinstance(data.get('kbs'),dict):
                raise ValueError('Invalid story registry')
            for key,kb in data['kbs'].items():
                if not isinstance(kb,dict):
                    raise ValueError('Invalid KB entry')
                if (kb.get('game'),kb.get('driver')) not in {('hsr','hsr_local'),('ark_re','ark_legacy')}:
                    raise ValueError('Unknown story driver/game')
                if kb['driver']=='hsr_local':
                    name=Path(kb.get('path',''))
                    if not kb.get('path') or name.is_absolute() or '..' in name.parts:
                        raise ValueError('Invalid HSR published path')
            for persona,binding in data['bindings'].items():
                if not isinstance(binding,dict):
                    raise ValueError('Invalid binding entry')
                if binding.get('kb_id') not in data['kbs'] or not isinstance(binding.get('auto_inject',True),bool):
                    raise ValueError('Invalid story binding')
            value=(version,data)
            _registries[path]=value
            return value
        except (OSError,ValueError,TypeError,KeyError):
            log.warning('story registry invalid; retaining last valid configuration')
            return _registries.get(path, ('unavailable',{'kbs':{},'bindings':{}}))


def binding(persona):
    return registry()[1]['bindings'].get(persona)


@dataclass(frozen=True)
class StoryScope:
    persona: str
    kb_id: str
    game: str
    audience: str
    revision: str
    valid: object = None


def make_scope(persona,audience='private',valid=None):
    version,data=registry()
    item=data['bindings'].get(persona)
    if not item:
        return None
    return StoryScope(persona,item['kb_id'],data['kbs'][item['kb_id']]['game'],audience,version,valid)


def active(scope):
    try:
        if scope is None or (scope.valid and not scope.valid()):
            return False
    except Exception:
        return False
    version,data=registry()
    return version==scope.revision and data['bindings'].get(scope.persona,{}).get('kb_id')==scope.kb_id


def _hsr(scope):
    _,data=registry()
    config=data['kbs'][scope.kb_id]
    root=Path(os.environ.get('STORY_KB_DATA_ROOT') or os.environ.get('USER_DATA_ROOT') or Path(__file__).resolve().parents[1]/'data').resolve()
    path=(root/config['path']).resolve()
    if not path.is_relative_to(root):
        raise ValueError('HSR path escaped published data root')
    manifest=path.with_name('manifest.json')
    stamp=(str(path),path.stat().st_ino,path.stat().st_size,path.stat().st_mtime_ns,
           manifest.stat().st_mtime_ns,manifest.stat().st_size,scope.revision)
    with _lock:
        reader=_readers.get(stamp)
        if reader is None:
            reader=HsrStoryKB(path,config.get('sha256',''))
            if config.get('build_id') and reader.meta['build_id']!=config['build_id']:
                raise ValueError('Unexpected HSR build')
            _readers.clear()  # only the immutable metadata is shared, never SQLite connections
            _readers[stamp]=reader
        return reader


def tool_search(query='',limit=4,scope=None):
    scope=scope or current_story_scope.get()
    if not active(scope):
        return '[story_search] 当前人格未绑定剧情库，或会话范围已失效。'
    query=(query or '').strip()
    if not query:
        return '[story_search] 请提供资料检索关键词。'
    try:
        limit=max(1,min(int(limit),8))
    except (TypeError,ValueError):
        limit=4
    try:
        if scope.game=='hsr':
            reader=_hsr(scope)
            hits=reader.search(query,limit)
            result=render(hits,6000) or '[story_search] 当前 HSR 库未找到匹配的已归属资料，请换用角色名、话题或书名。'
            audit('story tool persona=%s game=hsr audience=%s build=%s chunks=%s',scope.persona,
                     hashlib.sha256(scope.audience.encode()).hexdigest()[:12],reader.meta['build_id'],[h['chunk_id'] for h in hits])
        else:
            from .story_kb import get_story_kb, persona_char_id
            kb=get_story_kb()
            result=kb.format_tool_result(query,limit=limit,persona_char_id=persona_char_id(scope.persona)) if kb.available else '[story_search] Ark 剧情库不可用。'
        return result if active(scope) else '[story_search] 资料作用域已变更，请重新提问。'
    except Exception as error:
        log.warning('story tool unavailable game=%s error=%s',scope.game,type(error).__name__)
        return '[story_search] '+('HSR' if scope.game=='hsr' else 'Ark')+' 资料库不可用；未切换到其他作品。'


def hsr_injection(persona,query,scope=None,budget=1800):
    scope=scope or make_scope(persona)
    if not active(scope) or scope.persona!=persona or scope.game!='hsr' or not binding(persona).get('auto_inject',True):
        return ''
    try:
        reader=_hsr(scope)
        hits=reader.search(query,2)
        result=render(hits,budget)
        audit('story injection persona=%s game=hsr audience=%s build=%s chunks=%s',persona,
                 hashlib.sha256(scope.audience.encode()).hexdigest()[:12],reader.meta['build_id'],[h['chunk_id'] for h in hits])
        return result if active(scope) else ''
    except Exception as error:
        log.warning('HSR injection unavailable error=%s',type(error).__name__)
        return ''
