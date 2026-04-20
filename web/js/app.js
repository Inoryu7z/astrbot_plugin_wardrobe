(function(){
  const $=s=>document.querySelector(s);
  const $$=s=>document.querySelectorAll(s);

  const POOL_LABELS={
    'style':'风格','clothing_type':'服装类型','exposure_level':'暴露程度',
    'scene':'场景','atmosphere':'氛围','pose_type':'姿势',
    'body_orientation':'朝向','dynamic_level':'动态程度','action_style':'动作风格',
    'shot_size':'景别','camera_angle':'角度','expression':'表情',
  };

  let state={
    page:1, perPage:24, total:0,
    category:'', persona:'', style:'', scene:'', shot_size:'', atmosphere:'',
    searchQuery:'', batchMode:false,
    selectedIds:new Set(),
    currentImageId:null,
  };

  function getToken(){
    return localStorage.getItem('wardrobe_token')||'';
  }

  async function api(path,opts={}){
    const headers=opts.headers||{};
    headers['X-Wardrobe-Token']=getToken();
    if(opts.json){headers['Content-Type']='application/json';opts.body=JSON.stringify(opts.json);delete opts.json;}
    opts.headers=headers;
    const resp=await fetch(path,opts);
    if(resp.status===401){localStorage.removeItem('wardrobe_token');window.location.href='/login';return null;}
    if(!resp.ok){console.error('[Wardrobe] API error:',resp.status,resp.statusText,path);return null;}
    return resp;
  }

  function toast(msg,type='info'){
    const el=document.createElement('div');
    el.className='toast toast-'+type;
    el.textContent=msg;
    $('#toastContainer').appendChild(el);
    setTimeout(()=>el.remove(),3000);
  }

  async function loadStats(){
    const resp=await api('/api/stats');
    if(!resp)return;
    const data=await resp.json();
    $('#statTotal').textContent=data.total||0;
    $('#statPerson').textContent=(data.by_category&&data.by_category['人物'])||0;
    $('#statCloth').textContent=(data.by_category&&data.by_category['衣服'])||0;
  }

  async function loadFilters(){
    try{
      const resp=await api('/api/filters');
      if(!resp)return;
      const data=await resp.json();

      const container=$('#personaFilters');
      container.innerHTML='<label class="filter-item"><input type="radio" name="persona" value="" checked><span class="filter-label">全部</span></label>';
      (data.personas||[]).forEach(p=>{
        const label=document.createElement('label');
        label.className='filter-item';
        label.innerHTML=`<input type="radio" name="persona" value="${esc(p)}"><span class="filter-label">${esc(p)}</span>`;
        container.appendChild(label);
      });
      container.querySelectorAll('input[name="persona"]').forEach(inp=>{
        inp.addEventListener('change',()=>{
          state.persona=inp.value;state.page=1;loadImages();
        });
      });

      const pools=data.pools||{};
      const filterMap={
        style:{sel:'#styleFilter',stateKey:'style'},
        scene:{sel:'#sceneFilter',stateKey:'scene'},
        shot_size:{sel:'#shotSizeFilter',stateKey:'shot_size'},
        atmosphere:{sel:'#atmosphereFilter',stateKey:'atmosphere'},
      };
      Object.entries(filterMap).forEach(([poolKey,cfg])=>{
        const sel=$(cfg.sel);
        if(!sel)return;
        sel.innerHTML='<option value="">全部</option>';
        (pools[poolKey]||[]).forEach(v=>{
          const opt=document.createElement('option');
          opt.value=v;opt.textContent=v;
          sel.appendChild(opt);
        });
        sel.onchange=()=>{state[cfg.stateKey]=sel.value;state.page=1;loadImages();};
      });
    }catch(e){
      console.error('[Wardrobe] loadFilters error:',e);
    }
  }

  async function loadImages(){
    let url;
    if(state.searchQuery){
      url=`/api/search?q=${encodeURIComponent(state.searchQuery)}&persona=${encodeURIComponent(state.persona)}&category=${encodeURIComponent(state.category)}&limit=50`;
    }else{
      url=`/api/images?page=${state.page}&per_page=${state.perPage}&category=${encodeURIComponent(state.category)}&persona=${encodeURIComponent(state.persona)}&style=${encodeURIComponent(state.style)}&scene=${encodeURIComponent(state.scene)}&shot_size=${encodeURIComponent(state.shot_size)}&atmosphere=${encodeURIComponent(state.atmosphere)}`;
    }
    const resp=await api(url);
    if(!resp)return;
    const data=await resp.json();
    const images=data.images||[];
    state.total=data.total||images.length;
    renderGrid(images);
    if(!state.searchQuery)renderPagination();
    else $('#pagination').innerHTML='';
    $('#emptyState').classList.toggle('hidden',images.length>0);
  }

  function renderGrid(images){
    const grid=$('#imageGrid');
    grid.innerHTML='';
    images.forEach(img=>{
      const card=document.createElement('div');
      card.className='image-card';
      const personaText=img.persona?`<div class="image-card-persona">${esc(img.persona)}</div>`:'';
      card.innerHTML=`
        <img src="/api/image-file/${img.id}" loading="lazy" alt="" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22180%22 height=%22240%22><rect fill=%22%23F8F0F4%22 width=%22180%22 height=%22240%22/><text x=%2290%22 y=%22125%22 text-anchor=%22middle%22 fill=%22%23C8B8D0%22 font-size=%2214%22>加载失败</text></svg>'">
        <div class="image-card-overlay">
          <span class="image-card-category">${esc(img.category||'')}</span>
          ${personaText}
        </div>
        <div class="image-card-checkbox" data-id="${img.id}"></div>
      `;
      card.addEventListener('click',e=>{
        if(state.batchMode){
          if(e.target.closest('.image-card-checkbox'))return;
          toggleSelect(img.id);
          return;
        }
        showDetail(img.id);
      });
      const cb=card.querySelector('.image-card-checkbox');
      cb.addEventListener('click',e=>{e.stopPropagation();toggleSelect(img.id);});
      grid.appendChild(card);
    });
  }

  function toggleSelect(id){
    if(state.selectedIds.has(id))state.selectedIds.delete(id);
    else state.selectedIds.add(id);
    updateBatchUI();
  }

  function updateBatchUI(){
    $$('.image-card-checkbox').forEach(cb=>{
      cb.classList.toggle('checked',state.selectedIds.has(cb.dataset.id));
    });
    $('#batchCount').textContent=`已选 ${state.selectedIds.size} 张`;
  }

  function renderPagination(){
    const total=state.total;
    const pages=Math.ceil(total/state.perPage)||1;
    const cur=state.page;
    const container=$('#pagination');
    container.innerHTML='';
    if(pages<=1)return;
    const prev=document.createElement('button');
    prev.className='page-btn';prev.textContent='‹';prev.disabled=cur<=1;
    prev.onclick=()=>{state.page--;loadImages();};
    container.appendChild(prev);
    let start=Math.max(1,cur-2),end=Math.min(pages,start+4);
    if(end-start<4)start=Math.max(1,end-4);
    for(let i=start;i<=end;i++){
      const btn=document.createElement('button');
      btn.className='page-btn'+(i===cur?' active':'');
      btn.textContent=i;
      btn.onclick=()=>{state.page=i;loadImages();};
      container.appendChild(btn);
    }
    const next=document.createElement('button');
    next.className='page-btn';next.textContent='›';next.disabled=cur>=pages;
    next.onclick=()=>{state.page++;loadImages();};
    container.appendChild(next);
  }

  async function showDetail(id){
    state.currentImageId=id;
    const resp=await api(`/api/images/${id}`);
    if(!resp)return;
    const img=await resp.json();
    $('#modalImage').src=`/api/image-file/${id}`;
    const tags=$('#modalTags');
    tags.innerHTML='';
    const tagColors=['tag-pink','tag-lavender','tag-mint','tag-peach'];
    const tagData=[
      ...(img.style||[]).map(s=>({t:s,c:0})),
      ...(img.scene||[]).map(s=>({t:s,c:1})),
      ...(img.atmosphere||[]).map(s=>({t:s,c:2})),
      ...(img.action_style||[]).map(s=>({t:s,c:3})),
    ];
    if(img.clothing_type)tagData.push({t:img.clothing_type,c:0});
    if(img.shot_size)tagData.push({t:img.shot_size,c:1});
    if(img.expression)tagData.push({t:img.expression,c:2});
    if(img.persona)tagData.push({t:img.persona,c:3});
    tagData.forEach(td=>{
      const span=document.createElement('span');
      span.className='tag '+tagColors[td.c%4];
      span.textContent=td.t;
      tags.appendChild(span);
    });
    $('#modalDesc').textContent=img.description||'无描述';
    const userTags=$('#modalUserTags');
    if(img.user_tags){
      userTags.textContent='用户标签: '+img.user_tags;
      userTags.classList.remove('hidden');
    }else{
      userTags.classList.add('hidden');
    }
    const meta=$('#modalMeta');
    const lines=[];
    if(img.exposure_level)lines.push(`暴露程度: ${img.exposure_level}`);
    if(img.pose_type)lines.push(`姿势: ${img.pose_type}`);
    if(img.body_orientation)lines.push(`朝向: ${img.body_orientation}`);
    if(img.camera_angle)lines.push(`角度: ${img.camera_angle}`);
    if(img.color_tone)lines.push(`色调: ${img.color_tone}`);
    if(img.composition)lines.push(`构图: ${img.composition}`);
    if(img.background)lines.push(`背景: ${img.background}`);
    lines.push(`ID: ${img.id}`);
    lines.push(`创建时间: ${img.created_at||'未知'}`);
    meta.innerHTML=lines.map(l=>`<span>${esc(l)}</span>`).join('');
    $('#detailModal').classList.remove('hidden');
  }

  function closeDetail(){$('#detailModal').classList.add('hidden');state.currentImageId=null;}

  async function deleteImage(id){
    if(!confirm('确定删除此图片？'))return;
    const resp=await api(`/api/images/${id}`,{method:'DELETE'});
    if(!resp)return;
    const data=await resp.json();
    if(data.success){toast('已删除','success');closeDetail();loadImages();loadStats();}
    else toast('删除失败','error');
  }

  async function batchDelete(){
    if(state.selectedIds.size===0)return;
    if(!confirm(`确定删除选中的 ${state.selectedIds.size} 张图片？`))return;
    const resp=await api('/api/images/batch-delete',{
      method:'POST',
      json:{ids:[...state.selectedIds]}
    });
    if(!resp)return;
    const data=await resp.json();
    toast(`已删除 ${data.deleted} 张图片`,'success');
    state.selectedIds.clear();
    updateBatchUI();
    loadImages();loadStats();
  }

  function setupUpload(){
    const zone=$('#uploadZone');
    const fileInput=$('#uploadFile');
    const preview=$('#uploadPreview');
    const previewImg=$('#previewImg');
    const submitBtn=$('#uploadSubmit');
    let selectedFile=null;

    zone.addEventListener('click',()=>fileInput.click());
    zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('dragover');});
    zone.addEventListener('dragleave',()=>zone.classList.remove('dragover'));
    zone.addEventListener('drop',e=>{
      e.preventDefault();zone.classList.remove('dragover');
      if(e.dataTransfer.files.length)handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change',()=>{if(fileInput.files.length)handleFile(fileInput.files[0]);});

    function handleFile(file){
      if(!file.type.startsWith('image/')){toast('请选择图片文件','error');return;}
      selectedFile=file;
      previewImg.src=URL.createObjectURL(file);
      preview.classList.remove('hidden');
      zone.classList.add('hidden');
      submitBtn.disabled=false;
    }

    submitBtn.addEventListener('click',async()=>{
      if(!selectedFile)return;
      submitBtn.disabled=true;
      $('#uploadStatus').textContent='上传中...';
      const fd=new FormData();
      fd.append('image',selectedFile);
      fd.append('persona',$('#uploadPersona').value);
      fd.append('description',$('#uploadDescription').value);
      try{
        const resp=await api('/api/images/upload',{method:'POST',body:fd});
        if(!resp){toast('上传失败','error');$('#uploadStatus').textContent='服务器错误';submitBtn.disabled=false;return;}
        const data=await resp.json();
        if(data.success){
          toast('上传成功，正在分析...','success');
          $('#uploadModal').classList.add('hidden');
          resetUpload();
          setTimeout(()=>{loadImages();loadStats();},2000);
        }else{
          toast(data.error||'上传失败','error');
          $('#uploadStatus').textContent=data.error||'上传失败';
          submitBtn.disabled=false;
        }
      }catch(err){
        toast('上传失败','error');
        $('#uploadStatus').textContent='网络错误';
        submitBtn.disabled=false;
      }
    });

    function resetUpload(){
      selectedFile=null;
      fileInput.value='';
      preview.classList.add('hidden');
      zone.classList.remove('hidden');
      submitBtn.disabled=true;
      $('#uploadStatus').textContent='';
    }

    $('#uploadModalClose').addEventListener('click',()=>{$('#uploadModal').classList.add('hidden');resetUpload();});
  }

  function setupUploadPersonaSelect(){
    api('/api/filters').then(resp=>resp?resp.json():null).then(data=>{
      if(!data)return;
      const sel=$('#uploadPersona');
      (data.personas||[]).forEach(p=>{
        const opt=document.createElement('option');
        opt.value=p;opt.textContent=p;
        sel.appendChild(opt);
      });
    });
  }

  function esc(s){
    const d=document.createElement('div');
    d.textContent=s||'';
    return d.innerHTML.replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function poolLabel(key){
    return POOL_LABELS[key]||key;
  }

  async function loadPoolsModal(){
    const resp=await api('/api/pools');
    if(!resp)return;
    const data=await resp.json();
    const pools=data.pools||{};
    const list=$('#poolsList');
    list.innerHTML='';
    Object.entries(pools).sort((a,b)=>(poolLabel(a[0])).localeCompare(poolLabel(b[0]))).forEach(([key,values])=>{
      const group=document.createElement('div');
      group.className='pool-group';
      group.innerHTML=`
        <div class="pool-group-header">
          <span>${esc(poolLabel(key))} <span class="pool-group-count">(${values.length})</span></span>
          <button class="pool-delete-btn" data-key="${esc(key)}">删除分类</button>
        </div>
        <div class="pool-group-body">
          ${values.map(v=>`<span class="pool-tag">${esc(v)}<span class="pool-tag-remove" data-key="${esc(key)}" data-value="${esc(v)}">&times;</span></span>`).join('')}
        </div>
        <div class="pool-group-add">
          <input type="text" class="login-input pool-inline-input" data-key="${esc(key)}" placeholder="输入新选项...">
          <button class="btn btn-accent btn-sm pool-inline-add-btn" data-key="${esc(key)}">添加</button>
        </div>
      `;
      list.appendChild(group);
    });
    list.querySelectorAll('.pool-tag-remove').forEach(btn=>{
      btn.addEventListener('click',async()=>{
        const key=btn.dataset.key;
        const value=btn.dataset.value;
        const resp=await api('/api/pools',{method:'POST',json:{key,action:'remove_value',value}});
        if(resp&&resp.ok){toast('已移除','success');loadPoolsModal();loadFilters();}
      });
    });
    list.querySelectorAll('.pool-delete-btn').forEach(btn=>{
      btn.addEventListener('click',async()=>{
        const key=btn.dataset.key;
        if(!confirm(`确定删除分类「${poolLabel(key)}」？该分类下所有选项都会被移除。`))return;
        const resp=await api('/api/pools',{method:'POST',json:{key,action:'remove_pool'}});
        if(resp&&resp.ok){toast('已删除','success');loadPoolsModal();loadFilters();}
      });
    });
    list.querySelectorAll('.pool-inline-add-btn').forEach(btn=>{
      btn.addEventListener('click',async()=>{
        const key=btn.dataset.key;
        const input=btn.parentElement.querySelector('.pool-inline-input');
        const val=input.value.trim();
        if(!val){toast('请输入选项名称','error');return;}
        const resp=await api('/api/pools',{method:'POST',json:{key,action:'add_value',value:val}});
        if(resp&&resp.ok){toast('已添加','success');input.value='';loadPoolsModal();loadFilters();}
      });
    });
    list.querySelectorAll('.pool-inline-input').forEach(input=>{
      input.addEventListener('keydown',e=>{
        if(e.key==='Enter'){
          const btn=input.parentElement.querySelector('.pool-inline-add-btn');
          if(btn)btn.click();
        }
      });
    });
  }

  function init(){
    $$('input[name="category"]').forEach(inp=>{
      inp.addEventListener('change',()=>{
        state.category=inp.value;state.page=1;loadImages();
      });
    });

    $('#poolsBtn').addEventListener('click',()=>{$('#poolsModal').classList.remove('hidden');loadPoolsModal();});
    $('#poolsModalClose').addEventListener('click',()=>{$('#poolsModal').classList.add('hidden');});
    $('#poolAddKeyBtn').addEventListener('click',async()=>{
      const val=$('#poolNewKey').value.trim();
      if(!val){toast('请输入分类名称','error');return;}
      const resp=await api('/api/pools',{method:'POST',json:{key:val,action:'add_pool',value:val}});
      if(resp&&resp.ok){toast('分类已创建，现在可以添加选项了','success');$('#poolNewKey').value='';loadPoolsModal();loadFilters();}
    });

    $('#searchBtn').addEventListener('click',doSearch);
    $('#searchInput').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});

    $('#uploadBtn').addEventListener('click',()=>{
      setupUploadPersonaSelect();
      $('#uploadModal').classList.remove('hidden');
    });

    $('#batchModeBtn').addEventListener('click',()=>{
      state.batchMode=!state.batchMode;
      state.selectedIds.clear();
      document.body.classList.toggle('batch-mode',state.batchMode);
      $('#batchBar').classList.toggle('hidden',!state.batchMode);
      $('#batchModeBtn').classList.toggle('btn-accent',state.batchMode);
      $('#batchModeBtn').classList.toggle('btn-secondary',!state.batchMode);
      updateBatchUI();
    });

    $('#batchDeleteBtn').addEventListener('click',batchDelete);
    $('#batchCancelBtn').addEventListener('click',()=>{
      state.batchMode=false;state.selectedIds.clear();
      document.body.classList.remove('batch-mode');
      $('#batchBar').classList.add('hidden');
      $('#batchModeBtn').classList.remove('btn-accent');
      $('#batchModeBtn').classList.add('btn-secondary');
      updateBatchUI();
    });

    $('#modalClose').addEventListener('click',closeDetail);
    $('#detailModal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeDetail();});
    $('#modalImage').addEventListener('click',e=>{
      e.stopPropagation();
      const src=$('#modalImage').src;
      if(src){$('#lightboxImage').src=src;$('#lightbox').classList.remove('hidden');}
    });
    $('#lightbox').addEventListener('click',()=>{$('#lightbox').classList.add('hidden');});
    $('#modalDeleteBtn').addEventListener('click',()=>{if(state.currentImageId)deleteImage(state.currentImageId);});

    $('#logoutBtn').addEventListener('click',async()=>{
      await api('/api/logout',{method:'POST'});
      localStorage.removeItem('wardrobe_token');
      window.location.href='/login';
    });

    setupUpload();
    loadStats();
    loadFilters();
    loadImages();
  }

  function doSearch(){
    const q=$('#searchInput').value.trim();
    state.searchQuery=q;
    state.page=1;
    loadImages();
  }

  document.addEventListener('DOMContentLoaded',init);
})();
