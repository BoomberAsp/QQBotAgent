"""Validate and atomically activate reviewed, immutable sticker pack versions."""
import fcntl,hashlib,json,os,re,shutil,tempfile
from pathlib import Path
from PIL import Image

MOODS={'happy','sad','angry','greeting','thanks','surprised','tsundere','comfort'}

def publish(manifest_path,registry_path):
    manifest_path=Path(manifest_path).resolve();source=manifest_path.parent
    doc=json.loads(manifest_path.read_text());registry_path=Path(registry_path).resolve()
    for field in ('pack_id','persona','version'):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',doc.get(field,'')) or doc[field] in ('.','..'):raise ValueError('invalid '+field)
    if doc.get('schema')!=2 or not 1<=len(doc.get('stickers',[]))<=100:raise ValueError('schema or pack size')
    seen=set();records=[]
    for a in doc['stickers']:
        sid=a.get('id','')
        if not re.fullmatch('[a-z0-9_]{4,32}',sid) or sid in seen:raise ValueError('invalid/duplicate sticker id')
        seen.add(sid)
        if a.get('rights_status')!='approved' or a.get('review_status')!='approved':raise ValueError('pack contains unapproved content')
        if not all(a.get(k) for k in ('source_id','author','license')):raise ValueError('missing rights provenance')
        if set(a.get('mood',[]))-MOODS or len(a.get('caption',''))>120 or len(a.get('when',''))>300:raise ValueError('invalid metadata')
        path=(source/a['file']).resolve()
        if not path.is_relative_to(source) or not path.is_file():raise ValueError('path escape')
        raw=path.read_bytes()
        if len(raw)>8*1024*1024 or len(raw)!=a.get('bytes') or hashlib.sha256(raw).hexdigest()!=a.get('sha256'):raise ValueError('hash/size mismatch')
        with Image.open(path) as im:
            if im.width!=a.get('width') or im.height!=a.get('height') or im.width*im.height>20000000 or getattr(im,'n_frames',1)!=1 or im.format not in ('PNG','JPEG','WEBP','GIF'):raise ValueError('unsupported image')
            im.verify()
        records.append((a,path))
    root=registry_path.parent;root.mkdir(parents=True,exist_ok=True)
    with open(str(registry_path)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=json.loads(registry_path.read_text()) if registry_path.exists() else {}
        target=root/'packs'/doc['pack_id']/doc['version'];target.parent.mkdir(parents=True,exist_ok=True)
        staging=Path(tempfile.mkdtemp(prefix='.staging-',dir=target.parent))
        try:
            for a,path in records:shutil.copyfile(path,staging/(a['id']+path.suffix.lower()))
            stored={**doc,'stickers':[{**a,'file':a['id']+path.suffix.lower()} for a,path in records]}
            (staging/'manifest.json').write_text(json.dumps(stored,ensure_ascii=False))
            if target.exists():
                if json.loads((target/"manifest.json").read_text())!=stored:raise ValueError("immutable manifest conflict")
                for a,path in records:
                    if hashlib.sha256((target/(a['id']+path.suffix.lower())).read_bytes()).hexdigest()!=a['sha256']:raise ValueError('immutable version conflict')
            else:os.replace(staging,target)
            media=[a for a in data.get('media',[]) if not(a.get('pack_id')==doc['pack_id'] and doc['persona'] in a.get('personas',[]))]
            for a,path in records:
                if any(x.get('id')==a['id'] for x in media):raise ValueError('ID already published by another pack')
                media.append(dict(a,path=str((target/(a['id']+path.suffix.lower())).relative_to(root)),personas=[doc['persona']],visibility='public',pack_id=doc['pack_id'],version=doc['version'],description=a.get('caption','')+' '+a.get('when','')))
            data['media']=media
            tmp=registry_path.with_suffix('.'+__import__('secrets').token_hex(8)+'.tmp')
            try:tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));os.replace(tmp,registry_path)
            finally:tmp.unlink(missing_ok=True)
        finally:
            if staging.exists():shutil.rmtree(staging)
    return len(records)
