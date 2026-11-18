"""Deterministic identity boundaries; no tools, model calls or user storage."""
import re


def persona_address(message, names):
    compact=re.sub(r'[\s，,。.!！?？~～、：:]+','',message).casefold()
    if not compact or len(compact)>30:
        return False
    aliases=set()
    for name in names:
        for part in re.findall(r'[\u3400-\u9fff]+|[A-Za-z][A-Za-z _-]*',name or ''):
            aliases.add(re.sub(r'\s+','',part).casefold())
    if compact in {'在吗','还在吗','你好','嗨','喂','哈喽','你是谁','你是谁呀'}:
        return True
    for prefix in ('你是','是你吗','你好','嗨','喂','哈喽',''):
        for suffix in ('还在吗','在吗','在不在','是你吗','对吗','是谁','吗','呀','呢',''):
            value=compact[len(prefix):] if prefix and compact.startswith(prefix) else compact if not prefix else ''
            if suffix:
                value=value[:-len(suffix)] if value.endswith(suffix) else ''
            if len(value)>=2 and any(value==alias or (all('\u3400'<=c<='\u9fff' for c in value) and value in alias) for alias in aliases):
                return True
    return False


def task_presentation(persona):
    if not persona or persona=='assistant':
        return ''
    return ('本轮执行的是角色 '+str(persona)[:100]+' 的内部工具任务。'
            'assistant/Roxy 是内部执行身份，不是当前对外会话身份。'
            '只返回任务结果、事实、来源和必要限制，不向用户自我介绍为 Roxy，'
            '不否认用户正在交谈的角色，不说“我不是该角色”。'
            '无需声称自己亲历或扮演角色，也不得改写事实、代码和引用。')


def strip_worker_identity(text, user_request=''):
    # Preserve explicitly requested quotations/translations and fenced/quoted source text.
    if re.search(r'逐字|原样|引用|翻译',user_request) and re.search(r'Roxy',user_request,re.I):
        return text
    text=text or ''
    pattern=re.compile(r'(^|[。！？!?])\s*(?:我这边是|我这儿是|我是|我叫|我的名字是)\s*Roxy'
                       r'(?:[，,]\s*(?:而?不是|并非)[^，,。！？!?~～\n]{1,35})?[~～。！!]*',re.I)
    reverse=re.compile(r'(^|[。！？!?])\s*我(?:并)?不是[^，,。！？!?~～\n]{1,25}[，,]\s*我(?:是|叫)\s*Roxy[~～。！!]*',re.I)
    result=[]; fenced=False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('```'):
            fenced=not fenced; result.append(line); continue
        if not fenced and not line.lstrip().startswith(('>','“','"',"'")):
            line=reverse.sub(lambda m:m[1],line)
            line=pattern.sub(lambda m:m[1],line)
        result.append(line)
    return ''.join(result).strip()
