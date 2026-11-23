(()=>{
const $=id=>document.getElementById(id), enc=encodeURIComponent;
async function api(path,options={}){const r=await fetch('/api/resources'+path,{...options,headers:{'X-Resource-Admin':'1',...(options.headers||{})}});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d));return d;}
function action(id,fn,target='status'){$(id).onclick=async()=>{const b=$(id);b.disabled=true;try{$(target).textContent='处理中…';await fn();}catch(e){$(target).textContent=e.message;}finally{b.disabled=false;}};}
function jsonPost(data){return {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};}
function stickerCard(asset, persona) {
  const endpoint = '/stickers/' + enc(persona) + '/' + enc(asset.id);
  const card = document.createElement('div');
  card.style.width = 'min(240px, 100%)';
  const img = document.createElement('img');
  img.src = '/api/resources' + endpoint;
  img.style = 'width:160px;height:160px;object-fit:contain';
  img.alt = asset.description || asset.id;

  const id = document.createElement('p');
  id.textContent = asset.id;
  id.style.overflowWrap = 'anywhere';
  const form = document.createElement('form');
  const label = document.createElement('label');
  const description = document.createElement('input');
  description.type = 'text';
  description.maxLength = 300;
  description.value = asset.description || '';
  description.style.width = '100%';
  label.append('描述 ', description);
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = '保存描述';
  save.className = 'btn btn-primary';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.textContent = '从该人格下架';
  remove.className = 'btn btn-danger';
  const status = document.createElement('span');
  status.setAttribute('role', 'status');

  form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = description.disabled = remove.disabled = true;
    try {
      const result = await api(endpoint, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({description: description.value}),
      });
      description.value = result.description;
      img.alt = result.description || asset.id;
      status.textContent = '描述已保存';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      save.disabled = description.disabled = remove.disabled = false;
    }
  };

  remove.onclick = async () => {
    if (!confirm('确认下架这张表情包？已发送消息不会撤回。')) return;
    remove.disabled = true;
    try {
      await api(endpoint, {method: 'DELETE'});
      await refresh();
    } catch (error) {
      status.textContent = error.message;
      remove.disabled = false;
    }
  };

  form.append(label, save, status);
  card.append(img, id, form, remove);
  return card;
}

async function refresh() {
  const data = await api('');
  if ($('kb')) {
    const old = $('kb').value;
    $('kb').replaceChildren();
    for (const name of Object.keys(data.knowledge_bases || {})) {
      const option = document.createElement('option');
      option.value = option.textContent = name;
      $('kb').append(option);
    }
    if ([...$('kb').options].some((option) => option.value === old)) $('kb').value = old;
    $('status').textContent = '已加载注册知识库';
    return;
  }

  const names = new Set(Object.keys(data.personas || {}));
  for (const asset of data.media || []) {
    for (const name of asset.personas || []) names.add(name);
  }
  $('persona-list').replaceChildren();
  for (const name of names) {
    const option = document.createElement('option');
    option.value = name;
    $('persona-list').append(option);
  }
  const persona = $('persona').value.trim();
  const assets = (data.media || []).filter((asset) => (asset.personas || []).includes(persona));
  $('stickers').replaceChildren(...assets.map((asset) => stickerCard(asset, persona)));
  $('status').textContent = persona ? '已加载 ' + persona : '请输入人格ID';
}
action('refresh',refresh);
if($('kb')){action('query',async()=>{const d=await api('/query',jsonPost({kb:$('kb').value,sql:$('sql').value}));$('results').replaceChildren();for(const row of [d.columns,...d.rows]){const tr=document.createElement('tr');for(const value of row){const cell=document.createElement('td');cell.textContent=value===null?'NULL':String(value);tr.append(cell);}$('results').append(tr);}$('status').textContent=`返回${d.rows.length}行`+(d.truncated?'（已截断）':'');});action('publish',async()=>{const f=$('docs').files[0];if(!f)throw Error('请选择JSON文件');if(f.size>8*1024*1024)throw Error('文件超过8 MiB');const documents=JSON.parse(await f.text());if(!confirm('确认发布并替换该知识库的人格绑定？'))return;await api('/knowledge',jsonPost({id:$('kid').value.trim(),personas:$('personas').value.split(',').map(x=>x.trim()).filter(Boolean),documents}));await refresh();$('publish-status').textContent='已发布，下轮检索生效';},'publish-status');}else{action('upload',async()=>{if(!$('approved').checked)throw Error('请先确认内容与发布权限');const f=$('image').files[0];if(!f)throw Error('请选择图片');if(f.size>8*1024*1024)throw Error('图片超过8 MiB');await api('/stickers/'+enc($('persona').value.trim())+'?description='+enc($('description').value)+'&mood='+enc($('mood').value),{method:'POST',body:f});$('image').value='';$('approved').checked=false;await refresh();});}
refresh().catch(e=>$('status').textContent=e.message);
})();
