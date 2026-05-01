(function(){
  const $=s=>document.querySelector(s);
  const $$=s=>document.querySelectorAll(s);

  function _fmtRelative(iso){
    if(!iso)return '';
    const d=new Date(iso);
    const now=Date.now();
    const diff=now-d.getTime();
    if(diff<60000)return '刚刚';
    if(diff<3600000)return Math.floor(diff/60000)+'分钟前';
    if(diff<86400000)return Math.floor(diff/3600000)+'小时前';
    if(diff<2592000000)return Math.floor(diff/86400000)+'天前';
    return Math.floor(diff/2592000000)+'月前';
  }

  const POOL_LABELS={
    'style':'风格','clothing_type':'服装类型','exposure_level':'暴露程度',
    'scene':'场景','atmosphere':'氛围','pose_type':'姿势',
    'body_orientation':'朝向','dynamic_level':'动态程度','action_style':'动作风格',
    'shot_size':'景别','camera_angle':'角度','expression':'表情',
    'exposure_features':'暴露特征','key_features':'关键特征','prop_objects':'道具物品','allure_features':'魅力特征','body_focus':'身体焦点',
    'ref_strength':'参考强度',
  };

  const FIELD_DEFS=[
    {key:'category',label:'分类',type:'select',options:['人物','衣服']},
    {key:'style',label:'风格',type:'tags'},
    {key:'clothing_type',label:'服装类型',type:'tags'},
    {key:'exposure_level',label:'暴露程度',type:'select',options:['保守','轻微','中等','明显','极限']},
    {key:'exposure_features',label:'暴露特征',type:'tags',hint:'如：露肩、露背、透视、开叉'},
    {key:'key_features',label:'关键特征',type:'tags',hint:'3-5个最突出的视觉特征'},
    {key:'prop_objects',label:'道具物品',type:'tags',hint:'如：手机、扇子、玩偶'},
    {key:'allure_features',label:'魅力特征',type:'tags',hint:'动作/表情带来的魅力感，如：舔唇、眼神诱惑'},
    {key:'body_focus',label:'身体焦点',type:'tags',hint:'画面刻意突出的部位，如：胸部特写、腿部特写'},
    {key:'scene',label:'场景',type:'tags'},
    {key:'atmosphere',label:'氛围',type:'tags'},
    {key:'pose_type',label:'姿势',type:'text'},
    {key:'body_orientation',label:'朝向',type:'text'},
    {key:'dynamic_level',label:'动态程度',type:'text'},
    {key:'action_style',label:'动作风格',type:'tags'},
    {key:'shot_size',label:'景别',type:'text'},
    {key:'camera_angle',label:'角度',type:'text'},
    {key:'expression',label:'表情',type:'text'},
    {key:'color_tone',label:'色调',type:'text'},
    {key:'composition',label:'构图',type:'text'},
    {key:'background',label:'背景',type:'text'},
    {key:'description',label:'描述',type:'textarea'},
    {key:'user_tags',label:'用户标签',type:'text'},
    {key:'persona',label:'人格',type:'text'},
    {key:'favorite',label:'收藏',type:'select',options:['none','favorite','like']},
    {key:'use_count',label:'热度',type:'number',min:0},
  ];

  let state={
    page:1, perPage:24, total:0,
    category:'', persona:'', style:'', scene:'', shot_size:'', atmosphere:'', favorite:'', ref_strength:'', sort_by:'created_at',
    searchQuery:'', batchMode:false,
    selectedIds:new Set(),
    currentImageId:null,
    currentImageData:null,
    editing:false,
    loading:false,
    allLoaded:false,
    batchUploading:false,
    batchUploadProgress:{current:0,total:0,uploaded:0,failed:0},
    batchReanalyzing:false,
    batchOps:[],
    gridImageIds:[],
    preloadedPage2:null,
    loadedOriginals:new Set(),
    detailCache:new Map(),
    detailAbortController:null,
    contextMenuTargetId:null,
    viewMode:'compact',
    lightboxId:null,
  };

  let _originalObserver=null;
  const _originalQueue=[];
  let _originalActive=0;
  const MAX_ORIGINAL_CONCURRENT=3;

  function _initOriginalObserver(){
    if(_originalObserver)return;
    _originalObserver=new IntersectionObserver(entries=>{
      entries.forEach(entry=>{
        if(entry.isIntersecting){
          const card=entry.target;
          const id=card.dataset.id;
          _enqueueOriginal(card,id);
          _originalObserver.unobserve(card);
        }
      });
    },{rootMargin:'300px'});
  }

  function _enqueueOriginal(card,id){
    if(state.loadedOriginals.has(id))return;
    _originalQueue.push({card,id});
    _processOriginalQueue();
  }

  function _processOriginalQueue(){
    while(_originalActive<MAX_ORIGINAL_CONCURRENT&&_originalQueue.length>0){
      const{card,id}=_originalQueue.shift();
      if(state.loadedOriginals.has(id)){continue;}
      const img=card.querySelector('img[data-original]');
      if(!img){continue;}
      _originalActive++;
      const origSrc=img.dataset.original;
      const preloader=new Image();
      preloader.onload=()=>{
        img.src=origSrc;
        state.loadedOriginals.add(id);
        card.style.contentVisibility='visible';
        card.style.containIntrinsicSize='';
        _originalActive--;
        _processOriginalQueue();
      };
      preloader.onerror=()=>{_originalActive--;_processOriginalQueue();};
      preloader.src=origSrc;
    }
  }

  function _prioritizeOriginal(id){
    for(let i=0;i<_originalQueue.length;i++){
      if(_originalQueue[i].id===id){
        const[item]=_originalQueue.splice(i,1);
        _originalQueue.unshift(item);
        break;
      }
    }
  }

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
    if(!resp.ok){
      let errMsg=resp.status+' '+resp.statusText;
      try{const d=await resp.json();if(d.error)errMsg=d.error;}catch(e){}
      console.error('[Wardrobe] API error:',errMsg,path);
      return {ok:false,error:errMsg};
    }
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
          state.persona=inp.value;state.page=1;state.allLoaded=false;loadImages(true);
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
        sel.onchange=()=>{state[cfg.stateKey]=sel.value;state.page=1;state.allLoaded=false;loadImages(true);};
      });

      const statsPersonaSel=$('#statsPersonaFilter');
      if(statsPersonaSel){
        statsPersonaSel.innerHTML='<option value="">全部人格</option>';
        (data.personas||[]).forEach(p=>{
          const opt=document.createElement('option');
          opt.value=p;opt.textContent=p;
          statsPersonaSel.appendChild(opt);
        });
      }
    }catch(e){
      console.error('[Wardrobe] loadFilters error:',e);
    }
  }

  async function loadImages(resetGrid){
    if(state.loading)return;
    if(state.allLoaded&&!resetGrid)return;

    if(resetGrid){
      state.page=1;
      state.allLoaded=false;
      state.gridImageIds=[];
      state.preloadedPage2=null;
      state.loadedOriginals=new Set();
      state.detailCache.clear();
      _originalQueue.length=0;
      _originalActive=0;
      if(_originalObserver){_originalObserver.disconnect();}
      $('#imageGrid').innerHTML='';
    }

    state.loading=true;
    $('#loadingIndicator').classList.remove('hidden');
    $('#scrollSentinel').classList.add('hidden');

    let images=[];
    let total=0;

    if(!resetGrid && !state.searchQuery && state.preloadedPage2){
      images=state.preloadedPage2;
      state.preloadedPage2=null;
      const statsResp=await api('/api/stats');
      if(statsResp&&statsResp.ok){
        const statsData=await statsResp.json();
        total=statsData.total||0;
      }else{
        total=state.total;
      }
    }else{
      let url;
      if(state.searchQuery){
        url=`/api/search?q=${encodeURIComponent(state.searchQuery)}&persona=${encodeURIComponent(state.persona)}&category=${encodeURIComponent(state.category)}&favorite=${encodeURIComponent(state.favorite)}&limit=${state.perPage}`;
      }else{
        url=`/api/images?page=${state.page}&per_page=${state.perPage}&category=${encodeURIComponent(state.category)}&persona=${encodeURIComponent(state.persona)}&style=${encodeURIComponent(state.style)}&scene=${encodeURIComponent(state.scene)}&shot_size=${encodeURIComponent(state.shot_size)}&atmosphere=${encodeURIComponent(state.atmosphere)}&favorite=${encodeURIComponent(state.favorite)}&ref_strength=${encodeURIComponent(state.ref_strength)}&sort_by=${encodeURIComponent(state.sort_by)}&lightweight=1`;
      }

      const resp=await api(url);

      if(!resp)return;
      const data=await resp.json();
      images=data.images||[];
      total=data.total||images.length;
    }

    state.total=total;

    if(resetGrid){
      $('#imageGrid').innerHTML='';
    }

    await appendGrid(images);

    state.loading=false;
    $('#loadingIndicator').classList.add('hidden');

    if(!state.searchQuery && images.length<state.perPage){
      state.allLoaded=true;
    }else if(!state.searchQuery){
      state.page++;
    }

    const loadedCount=$('#imageGrid').children.length;
    if(!state.searchQuery && loadedCount<state.total){
      $('#scrollSentinel').classList.remove('hidden');
    }else{
      $('#scrollSentinel').classList.add('hidden');
    }

    $('#emptyState').classList.toggle('hidden',loadedCount>0);

    if(resetGrid && !state.searchQuery){
      preloadPage2AndOriginals();
    }else if(!state.searchQuery){
      preloadNextPage();
    }
  }

  function getGridColumnCount(){
    if(window.innerWidth<=480)return 1;
    if(window.innerWidth<=768)return 2;
    return state.viewMode==='compact'?5:3;
  }

  async function appendGrid(images){
    const grid=$('#imageGrid');
    if(!grid||!images.length)return;
    const cols=getGridColumnCount();
    const gap=16;
    const gridWidth=grid.clientWidth;
    const cardWidth=(gridWidth-(cols-1)*gap)/cols;
    if(cardWidth<=0)return;

    const cardsWithHeights=await Promise.all(images.map(img=>{
      return new Promise((resolve)=>{
        const preload=new Image();
        preload.onload=()=>{
          const ratio=preload.naturalWidth/preload.naturalHeight;
          const h=Math.round(cardWidth/ratio);
          resolve({img,height:Math.max(h,60),aspectRatio:ratio});
        };
        preload.onerror=()=>{
          resolve({img,height:Math.round(cardWidth/(3/4)),aspectRatio:3/4});
        };
        preload.src=`/api/image-file/${img.id}/thumbnail`;
      });
    }));

    const fragment=document.createDocumentFragment();
    cardsWithHeights.forEach(({img,height,aspectRatio})=>{
      state.gridImageIds.push(img.id);
      const card=document.createElement('div');
      card.className='image-card';
      card.dataset.id=img.id;
      card.dataset.aspectRatio=aspectRatio;
      card.style.gridRowEnd='span '+height;
      card.style.containIntrinsicSize='auto '+height+'px';
      const personaText=img.persona?`<div class="image-card-persona">${esc(img.persona)}</div>`:'';
      const favIcon=img.favorite==='favorite'?'❤️':img.favorite==='like'?'👍':'';
      const favMark=favIcon?`<div class="image-card-fav">${favIcon}</div>`:'';
      const useCount=img.use_count?`<span class="image-card-uses">🔥${img.use_count}</span>`:'';
      const lastUsed=img.last_used_at?`<span class="image-card-last-used">🕐${_fmtRelative(img.last_used_at)}</span>`:'';
      const similarityMark=img._similarity!=null?`<div class="image-card-similarity">${(img._similarity*100).toFixed(0)}%</div>`:'';
      const rsIcon=img.ref_strength==='full'?'📸':img.ref_strength==='reimagine'?'🔄':'🎨';
      const rsMark=img.ref_strength&&img.category==='人物'?`<div class="image-card-rs image-card-rs-${esc(img.ref_strength||'style')}">${rsIcon}</div>`:'';
      card.innerHTML=`
        ${favMark}
        ${similarityMark}
        ${rsMark}
        <img src="/api/image-file/${img.id}/thumbnail" data-original="/api/image-file/${img.id}" loading="lazy" decoding="async" alt="" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22180%22 height=%22240%22><rect fill=%22%23F8F0F4%22 width=%22180%22 height=%22240%22/><text x=%2290%22 y=%22125%22 text-anchor=%22middle%22 fill=%22%23C8B8D0%22 font-size=%2214%22>加载失败</text></svg>'">
        <div class="image-card-overlay">
          ${useCount}
          ${lastUsed}
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
      card.addEventListener('contextmenu',e=>{
        e.preventDefault();
        if(state.batchMode)return;
        showContextMenu(e,img.id);
      });
      const cb=card.querySelector('.image-card-checkbox');
      cb.addEventListener('click',e=>{e.stopPropagation();toggleSelect(img.id);});
      if(_originalObserver)_originalObserver.observe(card);
      fragment.appendChild(card);
    });
    grid.appendChild(fragment);
  }

  function toggleSelect(id){
    if(state.selectedIds.has(id))state.selectedIds.delete(id);
    else state.selectedIds.add(id);
    updateBatchUI();
  }

  async function preloadPage2AndOriginals(){
    if(!state.allLoaded){
      try{
        const url=`/api/images?page=${state.page}&per_page=${state.perPage}&category=${encodeURIComponent(state.category)}&persona=${encodeURIComponent(state.persona)}&style=${encodeURIComponent(state.style)}&scene=${encodeURIComponent(state.scene)}&shot_size=${encodeURIComponent(state.shot_size)}&atmosphere=${encodeURIComponent(state.atmosphere)}&favorite=${encodeURIComponent(state.favorite)}&ref_strength=${encodeURIComponent(state.ref_strength)}&sort_by=${encodeURIComponent(state.sort_by)}&lightweight=1`;
        const resp=await api(url);
        if(resp&&resp.ok){
          const data=await resp.json();
          state.preloadedPage2=data.images||[];
        }
      }catch(e){
        console.warn('[Wardrobe] 预加载第二页失败:',e);
      }
    }
  }

  async function preloadNextPage(){
    if(state.allLoaded||state.preloadedPage2)return;
    try{
      const url=`/api/images?page=${state.page}&per_page=${state.perPage}&category=${encodeURIComponent(state.category)}&persona=${encodeURIComponent(state.persona)}&style=${encodeURIComponent(state.style)}&scene=${encodeURIComponent(state.scene)}&shot_size=${encodeURIComponent(state.shot_size)}&atmosphere=${encodeURIComponent(state.atmosphere)}&favorite=${encodeURIComponent(state.favorite)}&ref_strength=${encodeURIComponent(state.ref_strength)}&sort_by=${encodeURIComponent(state.sort_by)}&lightweight=1`;
      const resp=await api(url);
      if(resp&&resp.ok){
        const data=await resp.json();
        const images=data.images||[];
        if(images.length<state.perPage)state.allLoaded=true;
        state.preloadedPage2=images;
      }
    }catch(e){
      console.warn('[Wardrobe] 预加载下一页失败:',e);
    }
  }

  function updateBatchUI(){
    $$('.image-card-checkbox').forEach(cb=>{
      cb.classList.toggle('checked',state.selectedIds.has(cb.dataset.id));
    });
    $('#batchCount').textContent=`已选 ${state.selectedIds.size} 张`;
  }

  async function showDetail(id){
    state.currentImageId=id;
    state.editing=false;

    if(state.detailAbortController){
      try{state.detailAbortController.abort();}catch(e){}
    }
    state.detailAbortController=new AbortController();
    const signal=state.detailAbortController.signal;

    const cached=state.detailCache.get(id);
    if(cached){
      state.currentImageData=cached;
      const card=document.querySelector(`.image-card[data-id="${id}"] img`);
      if(card&&state.loadedOriginals.has(id)){
        $('#modalImage').src=card.src;
      }else{
        $('#modalImage').src=`/api/image-file/${id}/thumbnail`;
        const preloader=new Image();
        preloader.onload=()=>{$('#modalImage').src=`/api/image-file/${id}`;};
        preloader.src=`/api/image-file/${id}`;
      }
      renderDetailFields(cached,false);
      const metaRO=$('#modalMetaReadonly');
      metaRO.innerHTML=`<span>ID: ${esc(cached.id)}</span><span>创建时间: ${esc(cached.created_at||'未知')}</span>`;
      updateFavoriteBtns(cached.favorite||'none');
      updateRefStrengthBtns(cached.ref_strength||'style', cached.ref_strength_reason||'');
      updateEditButtons();
      updateNavArrows();
      $('#detailModal').classList.remove('hidden');
    }

    try{
      const headers={'X-Wardrobe-Token':getToken()};
      const resp=await fetch(`/api/images/${id}`,{headers,signal});
      if(signal.aborted)return;
      if(resp.status===401){localStorage.removeItem('wardrobe_token');window.location.href='/login';return;}
      if(!resp.ok)return;
      const img=await resp.json();
      if(signal.aborted)return;
      state.detailCache.set(id,img);
      state.currentImageData=img;

      if(!cached){
        const card=document.querySelector(`.image-card[data-id="${id}"] img`);
        if(card&&state.loadedOriginals.has(id)){
          $('#modalImage').src=card.src;
        }else{
          $('#modalImage').src=`/api/image-file/${id}/thumbnail`;
          const preloader=new Image();
          preloader.onload=()=>{$('#modalImage').src=`/api/image-file/${id}`;};
          preloader.src=`/api/image-file/${id}`;
        }
        renderDetailFields(img,false);
        const metaRO=$('#modalMetaReadonly');
        metaRO.innerHTML=`<span>ID: ${esc(img.id)}</span><span>创建时间: ${esc(img.created_at||'未知')}</span>`;
        updateFavoriteBtns(img.favorite||'none');
        updateRefStrengthBtns(img.ref_strength||'style', img.ref_strength_reason||'');
        updateEditButtons();
        updateNavArrows();
        $('#detailModal').classList.remove('hidden');
      }else{
        const metaRO=$('#modalMetaReadonly');
        metaRO.innerHTML=`<span>ID: ${esc(img.id)}</span><span>创建时间: ${esc(img.created_at||'未知')}</span>`;
        updateFavoriteBtns(img.favorite||'none');
        updateRefStrengthBtns(img.ref_strength||'style', img.ref_strength_reason||'');
      }
    }catch(e){
      if(e.name==='AbortError')return;
      console.error('[Wardrobe] showDetail error:',e);
    }
  }

  function updateNavArrows(){
    const prevBtn=$('#navPrevBtn');
    const nextBtn=$('#navNextBtn');
    if(!prevBtn||!nextBtn)return;
    const idx=state.gridImageIds.indexOf(state.currentImageId);
    prevBtn.style.visibility=idx>0?'visible':'hidden';
    nextBtn.style.visibility=idx<state.gridImageIds.length-1?'visible':'hidden';
  }

  async function navigateDetail(direction){
    const idx=state.gridImageIds.indexOf(state.currentImageId);
    const newIdx=idx+direction;
    if(newIdx<0||newIdx>=state.gridImageIds.length)return;
    const newId=state.gridImageIds[newIdx];
    await showDetail(newId);
  }

  function updateFavoriteBtns(fav){
    const favBtn=$('#favFavoriteBtn');
    const likeBtn=$('#favLikeBtn');
    favBtn.textContent=fav==='favorite'?'❤️':'🤍';
    likeBtn.textContent=fav==='like'?'👍🏻':'👍';
    favBtn.classList.toggle('active-favorite',fav==='favorite');
    likeBtn.classList.toggle('active-like',fav==='like');
  }

  async function toggleFavorite(value){
    if(!state.currentImageId)return;
    const current=state.currentImageData?.favorite||'none';
    const newFav=current===value?'none':value;
    const resp=await api(`/api/images/${state.currentImageId}/favorite`,{
      method:'PATCH',
      json:{favorite:newFav},
    });
    if(!resp){toast('操作失败','error');return;}
    const result=await resp.json();
    if(result.success){
      state.currentImageData.favorite=newFav;
      state.detailCache.set(state.currentImageId,state.currentImageData);
      updateFavoriteBtns(newFav);
      toast(newFav==='none'?'已取消':newFav==='favorite'?'已收藏':'已标记喜欢','success');
    }else{
      toast(result.error||'操作失败','error');
    }
  }

  const RS_OPTIONS=[
    {value:'full',label:'📸完整参考',desc:'保留姿势、构图与服装细节'},
    {value:'style',label:'🎨风格参考',desc:'保留服装与氛围，必须对姿势或构图做明确小变动'},
    {value:'reimagine',label:'🔄重构',desc:'仅保留服装款式，重新设计姿势与构图'},
  ];

  function updateRefStrengthBtns(rs, reason){
    const btn=$('#refStrengthBtn');
    if(!btn)return;
    const opt=RS_OPTIONS.find(o=>o.value===(rs||'style'))||RS_OPTIONS[1];
    btn.innerHTML=opt.label+'<span class="rs-dropdown-arrow">▾</span>';
    btn.dataset.value=rs||'style';
    btn.classList.toggle('rs-full',rs==='full');
    btn.classList.toggle('rs-reimagine',rs==='reimagine');
    const reasonEl=$('#refStrengthReason');
    if(reasonEl){
      if(reason){
        reasonEl.textContent=reason;
        reasonEl.classList.remove('hidden');
      }else{
        reasonEl.classList.add('hidden');
      }
    }
    const dropdown=$('#refStrengthPanel');
    if(dropdown){
      dropdown.querySelectorAll('.rs-panel-option').forEach(el=>{
        el.classList.toggle('rs-option-active',el.dataset.value===(rs||'style'));
      });
    }
  }

  function toggleRefStrengthDropdown(){
    const dropdown=$('#refStrengthPanel');
    if(!dropdown)return;
    dropdown.classList.toggle('hidden');
  }

  async function setRefStrength(value){
    if(!state.currentImageId)return;
    const dropdown=$('#refStrengthPanel');
    if(dropdown)dropdown.classList.add('hidden');
    const resp=await api(`/api/images/${state.currentImageId}`,{
      method:'PUT',
      json:{ref_strength:value},
    });
    if(!resp){toast('操作失败','error');return;}
    const result=await resp.json();
    if(result.success){
      state.currentImageData.ref_strength=value;
      state.detailCache.set(state.currentImageId,state.currentImageData);
      updateRefStrengthBtns(value, state.currentImageData.ref_strength_reason||'');
      updateCardInGrid(state.currentImageId, {ref_strength: value});
      toast(`参考强度: ${RS_OPTIONS.find(o=>o.value===value)?.label||value}`,'success');
    }else{
      toast(result.error||'操作失败','error');
    }
  }
  window.setRefStrength=setRefStrength;

  function updateCardInGrid(id, updates){
    const card=document.querySelector(`.image-card[data-id="${id}"]`);
    if(!card)return;
    if(updates.ref_strength!==undefined){
      const oldRsMark=card.querySelector('.image-card-rs');
      if(oldRsMark)oldRsMark.remove();
      const rs=updates.ref_strength||'style';
      const rsIcon=rs==='full'?'📸':rs==='reimagine'?'🔄':'🎨';
      const img=card.querySelector('img');
      const rsEl=document.createElement('div');
      rsEl.className=`image-card-rs image-card-rs-${esc(rs)}`;
      rsEl.textContent=rsIcon;
      if(img&&img.nextSibling)img.parentNode.insertBefore(rsEl,img.nextSibling);
      else card.appendChild(rsEl);
    }
    if(updates.style!==undefined){
      const oldStyle=card.querySelector('.image-card-style');
      if(oldStyle)oldStyle.remove();
      const styleArr=Array.isArray(updates.style)?updates.style:[];
      if(styleArr.length){
        const catSpan=card.querySelector('.image-card-category');
        const styleEl=document.createElement('span');
        styleEl.className='image-card-style';
        styleEl.textContent=styleArr.slice(0,2).join(' ');
        if(catSpan&&catSpan.nextSibling)catSpan.parentNode.insertBefore(styleEl,catSpan.nextSibling);
        else if(catSpan)catSpan.parentNode.appendChild(styleEl);
      }
    }
  }

  function renderDetailFields(img,editMode){
    const container=$('#modalFields');
    container.innerHTML='';
    const tagColors=['tag-pink','tag-lavender','tag-mint','tag-peach'];
    let colorIdx=0;

    FIELD_DEFS.forEach(def=>{
      const val=img[def.key];
      const row=document.createElement('div');
      row.className='field-row';
      row.dataset.fieldKey=def.key;

      const label=document.createElement('div');
      label.className='field-label';
      label.textContent=def.label;
      row.appendChild(label);

      if(editMode){
        const inputWrap=document.createElement('div');
        inputWrap.className='field-input-wrap';

        if(def.type==='tags'){
          let tagsArr=Array.isArray(val)?val:[];
          if(!tagsArr.length && val){
            tagsArr=String(val).split(/[,、]/).map(s=>s.trim()).filter(Boolean);
          }
          const tagInput=document.createElement('div');
          tagInput.className='tag-input-group';
          const tagList=document.createElement('div');
          tagList.className='tag-input-list';
          tagsArr.forEach(t=>{
            const tag=document.createElement('span');
            tag.className='tag tag-editable '+tagColors[colorIdx%4];
            tag.innerHTML=esc(t)+'<span class="tag-remove" data-key="'+def.key+'">&times;</span>';
            tag.querySelector('.tag-remove').addEventListener('click',()=>{
              tag.remove();
            });
            tagList.appendChild(tag);
          });
          colorIdx++;
          const addRow=document.createElement('div');
          addRow.className='tag-add-row';
          const addInput=document.createElement('input');
          addInput.type='text';
          addInput.className='login-input tag-add-input';
          addInput.placeholder=def.hint||'输入后回车添加';
          addInput.addEventListener('keydown',e=>{
            if(e.key==='Enter'){
              e.preventDefault();
              const v=addInput.value.trim();
              if(!v)return;
              const newTag=document.createElement('span');
              newTag.className='tag tag-editable '+tagColors[(colorIdx-1)%4];
              newTag.innerHTML=esc(v)+'<span class="tag-remove" data-key="'+def.key+'">&times;</span>';
              newTag.querySelector('.tag-remove').addEventListener('click',()=>{newTag.remove();});
              tagList.appendChild(newTag);
              addInput.value='';
            }
          });
          addRow.appendChild(addInput);
          tagInput.appendChild(tagList);
          tagInput.appendChild(addRow);
          inputWrap.appendChild(tagInput);
        }else if(def.type==='select'){
          const sel=document.createElement('select');
          sel.className='field-select';
          const emptyOpt=document.createElement('option');
          emptyOpt.value='';emptyOpt.textContent='未选择';
          sel.appendChild(emptyOpt);
          (def.options||[]).forEach(o=>{
            const opt=document.createElement('option');
            opt.value=o;opt.textContent=o;
            if(val===o)opt.selected=true;
            sel.appendChild(opt);
          });
          inputWrap.appendChild(sel);
        }else if(def.type==='textarea'){
          const ta=document.createElement('textarea');
          ta.className='field-textarea';
          ta.value=val||'';
          inputWrap.appendChild(ta);
        }else if(def.type==='number'){
          const inp=document.createElement('input');
          inp.type='number';
          inp.className='field-text-input';
          inp.value=val!=null?val:0;
          if(def.min!=null)inp.min=def.min;
          if(def.max!=null)inp.max=def.max;
          inputWrap.appendChild(inp);
        }else{
          const inp=document.createElement('input');
          inp.type='text';
          inp.className='field-text-input';
          inp.value=val||'';
          inputWrap.appendChild(inp);
        }
        row.appendChild(inputWrap);
      }else{
        const valWrap=document.createElement('div');
        valWrap.className='field-value-wrap';

        if(def.type==='tags'){
          let tagsArr=Array.isArray(val)?val:[];
          if(!tagsArr.length && val){
            tagsArr=String(val).split(/[,、]/).map(s=>s.trim()).filter(Boolean);
          }
          if(tagsArr.length===0){
            valWrap.innerHTML='<span class="field-empty">-</span>';
          }else{
            tagsArr.forEach(t=>{
              const tag=document.createElement('span');
              tag.className='tag '+tagColors[colorIdx%4];
              tag.textContent=t;
              valWrap.appendChild(tag);
            });
            colorIdx++;
          }
        }else if(def.type==='select' && val){
          const tag=document.createElement('span');
          tag.className='tag tag-single '+tagColors[colorIdx%4];
          tag.textContent=val;
          valWrap.appendChild(tag);
          colorIdx++;
        }else if(def.type==='number' && def.key==='use_count'){
          const fav=img.favorite||'none';
          let hint='';
          if(fav==='favorite')hint=' (收藏-3 → 有效热度 '+(val-3)+')';
          else if(fav==='like')hint=' (喜欢-1 → 有效热度 '+(val-1)+')';
          valWrap.innerHTML=`<span class="field-value-text">🔥${val!=null?val:0}${hint}</span>`;
        }else{
          valWrap.innerHTML=val?`<span class="field-value-text">${esc(String(val))}</span>`:'<span class="field-empty">-</span>';
        }
        row.appendChild(valWrap);
      }

      container.appendChild(row);
    });
  }

  function updateEditButtons(){
    const editBtn=$('#modalEditBtn');
    const saveBtn=$('#modalSaveBtn');
    const cancelBtn=$('#modalCancelEditBtn');
    const title=$('#modalEditTitle');
    if(state.editing){
      editBtn.classList.add('hidden');
      saveBtn.classList.remove('hidden');
      cancelBtn.classList.remove('hidden');
      title.textContent='编辑图片属性';
    }else{
      editBtn.classList.remove('hidden');
      saveBtn.classList.add('hidden');
      cancelBtn.classList.add('hidden');
      title.textContent='图片详情';
    }
  }

  function collectEditData(){
    const data={};
    const container=$('#modalFields');
    FIELD_DEFS.forEach(def=>{
      const row=container.querySelector(`[data-field-key="${def.key}"]`);
      if(!row)return;

      if(def.type==='tags'){
        const tags=row.querySelectorAll('.tag-editable');
        const vals=[];
        tags.forEach(t=>{
          let text=t.textContent;
          if(text.endsWith('×'))text=text.slice(0,-1);
          text=text.trim();
          if(text)vals.push(text);
        });
        data[def.key]=vals;
      }else if(def.type==='select'){
        const sel=row.querySelector('.field-select');
        data[def.key]=sel?sel.value:'';
      }else if(def.type==='textarea'){
        const ta=row.querySelector('.field-textarea');
        data[def.key]=ta?ta.value:'';
      }else if(def.type==='number'){
        const inp=row.querySelector('.field-text-input');
        data[def.key]=inp?parseInt(inp.value,10)||0:0;
      }else{
        const inp=row.querySelector('.field-text-input');
        data[def.key]=inp?inp.value:'';
      }
    });
    return data;
  }

  async function saveEdit(){
    if(!state.currentImageId)return;
    const data=collectEditData();
    const resp=await api(`/api/images/${state.currentImageId}`,{
      method:'PUT',
      json:data,
    });
    if(!resp){toast('保存失败','error');return;}
    const result=await resp.json();
    if(result.success){
      toast('保存成功','success');
      state.editing=false;
      const freshResp=await api(`/api/images/${state.currentImageId}`);
      if(freshResp){
        state.currentImageData=await freshResp.json();
        state.detailCache.set(state.currentImageId,state.currentImageData);
        renderDetailFields(state.currentImageData,false);
        updateCardInGrid(state.currentImageId, {
          style: state.currentImageData.style,
          ref_strength: state.currentImageData.ref_strength,
        });
      }
      updateEditButtons();
    }else{
      toast(result.error||'保存失败','error');
    }
  }

  function closeDetail(){
    $('#detailModal').classList.add('hidden');
    state.currentImageId=null;
    state.currentImageData=null;
    state.editing=false;
  }

  async function deleteImage(id){
    if(!confirm('确定删除此图片？'))return;
    const resp=await api(`/api/images/${id}`,{method:'DELETE'});
    if(!resp)return;
    const data=await resp.json();
    if(data.success){toast('已删除','success');closeDetail();state.page=1;state.allLoaded=false;loadImages(true);loadStats();}
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
    state.page=1;state.allLoaded=false;loadImages(true);loadStats();
  }

  async function batchReanalyze(){
    if(state.selectedIds.size===0){
      toast('请先选择图片','warning');
      return;
    }
    const ids=[...state.selectedIds];
    state.batchOps=ids.map(id=>({id,type:'reanalyze',status:'pending'}));
    state.batchReanalyzing=true;
    updateBatchOpsPanel();
    updateBatchReanalyzeIndicator();
    toast(`开始重新分析 ${ids.length} 张图片`,'info');

    await runBatchOps();
  }

  async function batchReanalyzeFailed(){
    const resp=await api('/api/images/failed');
    if(!resp){toast('获取失败图列表失败','error');return;}
    const data=await resp.json();
    const ids=data.ids||[];
    if(!ids.length){toast('没有分析失败的图片','info');return;}
    state.batchOps=ids.map(id=>({id,type:'reanalyze',status:'pending'}));
    state.batchReanalyzing=true;
    updateBatchOpsPanel();
    updateBatchReanalyzeIndicator();
    toast(`开始重新分析 ${ids.length} 张失败图片`,'info');
    await runBatchOps();
  }

  async function runBatchOps(){
    const pending=state.batchOps.filter(o=>o.status==='pending'||o.status==='failed');
    if(!pending.length)return;

    const promises=[];

    for(let i=0;i<pending.length;i++){
      const op=pending[i];
      op.status='analyzing';
      updateBatchOpsPanel();
      if(state.batchReanalyzing) updateBatchReanalyzeIndicator();

      const p=(async()=>{
        try{
          const resp=await api(`/api/images/${op.id}/reanalyze`,{method:'POST',json:{description:''}});
          if(!resp||resp.error){
            op.status='failed';
          }else{
            const data=await resp.json();
            op.status=data.success?'done':'failed';
          }
        }catch(e){
          console.error('[Wardrobe] 批量重新分析单张失败:',e);
          op.status='failed';
        }
        updateBatchOpsPanel();
        if(state.batchReanalyzing) updateBatchReanalyzeIndicator();
      })();

      promises.push(p);

      if(i<pending.length-1){
        await new Promise(r=>setTimeout(r,5000));
      }
    }

    await Promise.all(promises);

    state.batchReanalyzing=false;
    updateBatchReanalyzeIndicator();
    const done=state.batchOps.filter(o=>o.status==='done').length;
    const failed=state.batchOps.filter(o=>o.status==='failed').length;
    toast(`重新分析完成：${done}成功，${failed}失败`,done>0?'success':'error');
    state.page=1;state.allLoaded=false;loadImages(true);loadStats();
  }

  async function retryBatchOps(){
    const failed=state.batchOps.filter(o=>o.status==='failed');
    if(!failed.length){
      toast('没有失败的任务','info');
      return;
    }
    toast(`重试 ${failed.length} 张失败图片`,'info');
    state.batchReanalyzing=true;
    updateBatchReanalyzeIndicator();
    await runBatchOps();
  }

  function updateBatchReanalyzeIndicator(){
    const indicator=$('#batchReanalyzeIndicator');
    if(!state.batchReanalyzing){
      indicator.classList.add('hidden');
      return;
    }
    indicator.classList.remove('hidden');
    const done=state.batchOps.filter(o=>o.status==='done').length;
    const failed=state.batchOps.filter(o=>o.status==='failed').length;
    indicator.textContent=`分析中 ${done+failed}/${state.batchOps.length}`;
  }

  function updateBatchOpsPanel(){
    const panel=$('#batchOpsPanel');
    const list=$('#batchOpsList');
    const summary=$('#batchOpsSummary');
    if(!state.batchOps.length){
      panel.classList.add('hidden');
      return;
    }
    panel.classList.remove('hidden');
    const done=state.batchOps.filter(o=>o.status==='done').length;
    const failed=state.batchOps.filter(o=>o.status==='failed').length;
    const skipped=state.batchOps.filter(o=>o.status==='skipped').length;
    const pending=state.batchOps.filter(o=>o.status==='pending').length;
    const analyzing=state.batchOps.filter(o=>o.status==='analyzing').length;
    summary.textContent=`共${state.batchOps.length}张 | ✓${done} ✗${failed} ⊘${skipped} ⏳${pending+analyzing}`;
    list.innerHTML=state.batchOps.map((o,idx)=>{
      const icon=o.status==='done'?'✓':o.status==='failed'?'✗':o.status==='skipped'?'⊘':o.status==='analyzing'?'⏳':'○';
      const cls=o.status==='done'?'op-done':o.status==='failed'?'op-fail':o.status==='skipped'?'op-skip':o.status==='analyzing'?'op-active':'op-pending';
      const retryBtn=o.status==='failed'?`<button class="op-retry-btn" onclick="retrySingleOp(${idx})">↻</button>`:'';
      return `<div class="batch-op-item ${cls}"><span class="op-icon">${icon}</span><span class="op-id">${o.id.length>12?o.id.slice(0,8):o.id}</span>${retryBtn}</div>`;
    }).join('');

    const retryAllBtn=$('#batchOpsRetryAll');
    if(retryAllBtn){
      retryAllBtn.classList.toggle('hidden',failed===0);
    }
  }

  async function retrySingleOp(idx){
    const op=state.batchOps[idx];
    if(!op||op.status!=='failed')return;
    op.status='analyzing';
    updateBatchOpsPanel();
    try{
      if(op.type==='upload'){
        const fd=new FormData();
        fd.append('image',op.file);
        fd.append('persona',$('#uploadPersona')?$('#uploadPersona').value:'');
        fd.append('description',$('#uploadDescription')?$('#uploadDescription').value:'');
        const resp=await api('/api/images/upload',{method:'POST',body:fd});
        if(!resp||resp.error){
          op.status='failed';
        }else{
          const data=await resp.json();
          op.status=(data.success||data.duplicate)?'done':'failed';
        }
      }else{
        const resp=await api(`/api/images/${op.id}/reanalyze`,{method:'POST',json:{description:''}});
        if(!resp||resp.error){
          op.status='failed';
        }else{
          const data=await resp.json();
          op.status=data.success?'done':'failed';
        }
      }
    }catch(e){
      op.status='failed';
    }
    updateBatchOpsPanel();
  }

  function toggleBatchOpsPanel(){
    const list=$('#batchOpsList');
    const toggle=$('#batchOpsToggle');
    if(list.classList.contains('hidden')){
      list.classList.remove('hidden');
      toggle.textContent='收起';
    }else{
      list.classList.add('hidden');
      toggle.textContent='详情';
    }
  }

  function clearBatchOps(){
    state.batchOps=[];
    updateBatchOpsPanel();
  }

  function setupUpload(){
    const zone=$('#uploadZone');
    const fileInput=$('#uploadFile');
    const preview=$('#uploadPreview');
    const previewImg=$('#previewImg');
    const submitBtn=$('#uploadSubmit');
    const fileList=$('#uploadFileList');
    let selectedFiles=[];

    zone.addEventListener('click',()=>fileInput.click());
    zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('dragover');});
    zone.addEventListener('dragleave',()=>zone.classList.remove('dragover'));
    zone.addEventListener('drop',e=>{
      e.preventDefault();zone.classList.remove('dragover');
      const files=[...e.dataTransfer.files].filter(f=>f.type.startsWith('image/'));
      if(files.length)handleFiles(files);
    });
    fileInput.addEventListener('change',()=>{
      if(fileInput.files.length){
        handleFiles([...fileInput.files].filter(f=>f.type.startsWith('image/')));
      }
    });

    function handleFiles(files){
      selectedFiles=files;
      if(files.length===1){
        previewImg.src=URL.createObjectURL(files[0]);
        preview.classList.remove('hidden');
        fileList.classList.add('hidden');
      }else{
        preview.classList.add('hidden');
        fileList.classList.remove('hidden');
        fileList.innerHTML=files.map((f,i)=>`<div class="upload-filelist-item"><span>${esc(f.name)}</span><span class="file-size">${(f.size/1024).toFixed(0)}KB</span><span class="file-status" id="fileStatus${i}">待上传</span></div>`).join('');
      }
      zone.classList.add('hidden');
      submitBtn.disabled=false;
    }

    submitBtn.addEventListener('click',async()=>{
      if(!selectedFiles.length)return;
      submitBtn.disabled=true;

      if(selectedFiles.length===1){
        $('#uploadStatus').textContent='上传中...';
        const fd=new FormData();
        fd.append('image',selectedFiles[0]);
        fd.append('persona',$('#uploadPersona').value);
        fd.append('description',$('#uploadDescription').value);
        try{
          const resp=await api('/api/images/upload',{method:'POST',body:fd});
          if(!resp){toast('上传失败','error');$('#uploadStatus').textContent='请求失败';submitBtn.disabled=false;return;}
          if(resp.error){toast(resp.error,'error');$('#uploadStatus').textContent=resp.error;submitBtn.disabled=false;return;}
          const data=await resp.json();
          if(data.duplicate){
            const personaInfo=data.existing_persona?`（人格: ${esc(data.existing_persona)}）`:'';
            toast(`图片重复，已存在于衣柜库中${personaInfo}，跳过保存`,'warning');
            $('#uploadStatus').textContent='图片重复，已跳过';
            submitBtn.disabled=false;
            return;
          }
          if(data.success){
            toast('上传成功，正在分析...','success');
            $('#uploadModal').classList.add('hidden');
            resetUpload();
            setTimeout(()=>{state.page=1;state.allLoaded=false;loadImages(true);loadStats();},2000);
          }else{
            toast(data.error||'上传失败','error');
            $('#uploadStatus').textContent=data.error||'上传失败';
            submitBtn.disabled=false;
          }
        }catch(err){
          toast('上传失败','error');
          $('#uploadStatus').textContent='网络错误: '+err.message;
          submitBtn.disabled=false;
        }
      }else{
        const progress=$('#uploadProgress');
        const progressBar=$('#uploadProgressBar');
        const progressText=$('#uploadProgressText');
        progress.classList.remove('hidden');

        const persona=$('#uploadPersona').value;
        const description=$('#uploadDescription').value;
        const files=[...selectedFiles];

        state.batchUploading=true;
        state.batchUploadProgress={current:0,total:files.length,uploaded:0,failed:0};
        state.batchOps=files.map((f,i)=>({id:f.name,type:'upload',file:f,status:'pending'}));

        $('#uploadModal').classList.add('hidden');
        toast(`开始批量上传 ${files.length} 张图片`,'info');
        updateBatchOpsPanel();

        const uploadPromises=[];

        for(let i=0;i<files.length;i++){
          state.batchUploadProgress.current=i+1;
          state.batchOps[i].status='analyzing';
          updateBatchUploadIndicator();
          updateBatchOpsPanel();

          const p=(async()=>{
            try{
              const fd=new FormData();
              fd.append('image',files[i]);
              fd.append('persona',persona);
              fd.append('description',description);
              const resp=await api('/api/images/upload',{method:'POST',body:fd});
              if(resp&&typeof resp.json==='function'&&!resp.error){
                const data=await resp.json();
                if(data.duplicate){
                  state.batchOps[i].status='skipped';
                }else if(data.success){
                  state.batchUploadProgress.uploaded++;
                  state.batchOps[i].status='done';
                  if(!state.loading){state.page=1;state.allLoaded=false;loadImages(true);loadStats();}
                }else{
                  state.batchUploadProgress.failed++;
                  state.batchOps[i].status='failed';
                }
              }else{
                state.batchUploadProgress.failed++;
                state.batchOps[i].status='failed';
              }
            }catch(err){
              console.error('[Wardrobe] 批量上传单张失败:',err);
              state.batchUploadProgress.failed++;
              state.batchOps[i].status='failed';
            }
            updateBatchUploadIndicator();
            updateBatchOpsPanel();
          })();

          uploadPromises.push(p);

          if(i<files.length-1){
            await new Promise(r=>setTimeout(r,20000));
          }
        }

        await Promise.all(uploadPromises);

        const up=state.batchUploadProgress.uploaded;
        const fl=state.batchUploadProgress.failed;
        const sk=state.batchOps.filter(o=>o.status==='skipped').length;
        const parts=[`${up}成功`];
        if(fl)parts.push(`${fl}失败`);
        if(sk)parts.push(`${sk}跳过(重复)`);
        toast(`批量上传完成：${parts.join('，')}`,up>0?'success':'error');
        state.batchUploading=false;
        updateBatchUploadIndicator();
        state.page=1;state.allLoaded=false;loadImages(true);loadStats();
        resetUpload();
      }
    });

    function resetUpload(){
      selectedFiles=[];
      fileInput.value='';
      preview.classList.add('hidden');
      fileList.classList.add('hidden');
      fileList.innerHTML='';
      zone.classList.remove('hidden');
      submitBtn.disabled=true;
      $('#uploadStatus').textContent='';
      $('#uploadProgress').classList.add('hidden');
      $('#uploadProgressBar').style.width='0%';
      $('#uploadProgressText').textContent='';
    }

    $('#uploadModalClose').addEventListener('click',()=>{
      if(state.batchUploading){
        toast('批量上传正在进行中，请等待完成','warning');
        return;
      }
      $('#uploadModal').classList.add('hidden');
      resetUpload();
    });
  }

  function updateBatchUploadIndicator(){
    const indicator=$('#batchUploadIndicator');
    if(!state.batchUploading){
      indicator.classList.add('hidden');
      return;
    }
    indicator.classList.remove('hidden');
    const done=state.batchOps.filter(o=>o.status==='done').length;
    const failed=state.batchOps.filter(o=>o.status==='failed').length;
    const total=state.batchOps.length;
    indicator.textContent=`上传中 ${done+failed}/${total}（✓${done} ✗${failed}）`;
  }

  function setupUploadPersonaSelect(){
    api('/api/filters').then(resp=>resp?resp.json():null).then(data=>{
      if(!data)return;
      const sel=$('#uploadPersona');
      sel.innerHTML='<option value="">不指定</option>';
      (data.personas||[]).forEach(p=>{
        const opt=document.createElement('option');
        opt.value=p;opt.textContent=p;
        sel.appendChild(opt);
      });
    });
  }

  function setupBackup(){
    const zone=$('#backupUploadZone');
    const fileInput=$('#backupFile');
    const importBtn=$('#backupImportBtn');
    let selectedBackupFile=null;

    zone.addEventListener('click',()=>fileInput.click());
    fileInput.addEventListener('change',()=>{
      if(fileInput.files.length){
        selectedBackupFile=fileInput.files[0];
        zone.querySelector('.backup-upload-text').textContent=selectedBackupFile.name;
        importBtn.disabled=false;
      }
    });

    $('#backupExportBtn').addEventListener('click',async()=>{
      const btn=$('#backupExportBtn');
      btn.disabled=true;
      btn.textContent='正在打包...';
      try{
        const resp=await fetch('/api/backup/export',{
          headers:{'X-Wardrobe-Token':getToken()}
        });
        if(resp.status===401){localStorage.removeItem('wardrobe_token');window.location.href='/login';return;}
        if(!resp.ok){
          toast('导出失败','error');
          return;
        }
        const blob=await resp.blob();
        const url=URL.createObjectURL(blob);
        const a=document.createElement('a');
        a.href=url;
        a.download=`wardrobe_backup_${new Date().toISOString().slice(0,10)}.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        toast('备份导出成功','success');
      }catch(e){
        toast('导出失败: '+e.message,'error');
      }finally{
        btn.disabled=false;
        btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> 导出备份';
      }
    });

    importBtn.addEventListener('click',async()=>{
      if(!selectedBackupFile)return;
      if(!confirm('确定要恢复此备份？已有数据不会被覆盖，只会导入新数据。'))return;
      importBtn.disabled=true;
      importBtn.textContent='正在恢复...';
      $('#backupStatus').textContent='上传并恢复中，请稍候...';
      try{
        const fd=new FormData();
        fd.append('backup',selectedBackupFile);
        const resp=await api('/api/backup/import',{method:'POST',body:fd});
        if(!resp){toast('恢复失败','error');$('#backupStatus').textContent='请求失败';return;}
        const data=await resp.json();
        if(data.success){
          toast(`恢复成功！导入 ${data.imported} 条记录，${data.copied_files} 个图片文件`,'success');
          $('#backupStatus').textContent=`导入 ${data.imported}/${data.total_in_backup} 条记录，${data.copied_files} 个图片文件`;
          state.page=1;state.allLoaded=false;loadImages(true);loadStats();
        }else{
          toast(data.error||'恢复失败','error');
          $('#backupStatus').textContent=data.error||'恢复失败';
        }
      }catch(e){
        toast('恢复失败: '+e.message,'error');
        $('#backupStatus').textContent='网络错误: '+e.message;
      }finally{
        importBtn.disabled=false;
        importBtn.textContent='恢复备份';
      }
    });

    $('#backupModalClose').addEventListener('click',()=>{
      $('#backupModal').classList.add('hidden');
      selectedBackupFile=null;
      fileInput.value='';
      zone.querySelector('.backup-upload-text').textContent='点击选择备份文件（.zip）';
      importBtn.disabled=true;
      $('#backupStatus').textContent='';
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

  let _statsCharts=[];

  const STATS_COLORS=[
    '#c084fc','#f0abfc','#e879f9','#d946ef','#a855f7',
    '#818cf8','#93c5fd','#67e8f9','#5eead4','#86efac',
    '#fde68a','#fdba74','#fca5a5','#f9a8d4','#c4b5fd',
    '#a5b4fc','#99f6e4','#bef264','#fcd34d','#fda4af',
  ];

  function _top5(counts){
    return Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,5);
  }

  function _renderTop5(containerId,counts,total){
    const top5=_top5(counts);
    if(!top5.length){$('#'+containerId).innerHTML='';return;}
    const html=top5.map(([name,count])=>{
      const pct=total?((count/total)*100).toFixed(1):'0';
      return `<span class="stats-top5-tag">${name} <b>${count}</b> <small>${pct}%</small></span>`;
    }).join('');
    $('#'+containerId).innerHTML=html;
  }

  function _mergeSmall(counts,threshold=0.05){
    const total=Object.values(counts).reduce((a,b)=>a+b,0);
    if(total===0)return counts;
    const merged={};
    let otherCount=0;
    const otherItems=[];
    for(const [k,v] of Object.entries(counts)){
      if(v/total<threshold){
        otherCount+=v;
        otherItems.push(k);
      }else{
        merged[k]=v;
      }
    }
    if(otherCount>0){
      merged['其他']=otherCount;
    }
    return merged;
  }

  function _buildTreemapData(counts,groups){
    const tagToGroup={};
    for(const [gName,tags] of Object.entries(groups)){
      for(const t of tags){tagToGroup[t]=gName;}
    }
    const groupData={};
    for(const [gName] of Object.entries(groups)){
      groupData[gName]={name:gName,children:[]};
    }
    if(!groupData['自定义'])groupData['自定义']={name:'自定义',children:[]};

    for(const [tag,count] of Object.entries(counts)){
      const gName=tagToGroup[tag]||'自定义';
      if(!groupData[gName])groupData[gName]={name:gName,children:[]};
      groupData[gName].children.push({name:tag,value:count});
    }

    return Object.values(groupData).filter(g=>g.children.length>0);
  }

  function _renderPieChart(containerId,counts,chartTitle,filterKey){
    const merged=_mergeSmall(counts,0.05);
    const data=Object.entries(merged).map(([name,value])=>({name,value}));
    const dom=$('#'+containerId);
    if(!dom)return;
    const chart=echarts.init(dom);
    _statsCharts.push(chart);
    chart.setOption({
      title:{text:chartTitle,left:'center',top:0,textStyle:{fontSize:14,color:'#9ca3af'}},
      tooltip:{trigger:'item',formatter:'{b}: {c} ({d}%)'},
      color:STATS_COLORS,
      series:[{
        type:containerId==='chartShotSize'?'pie':'pie',
        radius:containerId==='chartShotSize'?'55%':['40%','65%'],
        center:['50%','55%'],
        data,
        label:{
          formatter:'{b}\n{d}%',
          fontSize:11,
          color:'#d1d5db',
        },
        emphasis:{
          itemStyle:{shadowBlur:10,shadowOffsetX:0,shadowColor:'rgba(0,0,0,0.5)'},
        },
        animationType:'scale',
        animationEasing:'elasticOut',
      }],
    });
    chart.on('click',(params)=>{
      if(params.name&&params.name!=='其他'){
        _statsChartClick(filterKey,params.name);
      }
    });
  }

  const GROUP_COLORS={
    '洛丽塔系':'#c084fc','JK系':'#818cf8','汉服系':'#f87171','甜系':'#fb7185',
    '纯欲系':'#f0abfc','法式优雅系':'#fbbf24','暗黑系':'#6366f1','日韩系':'#34d399',
    '性感系':'#f472b6','其他':'#94a3b8','自定义':'#a78bfa',
    '日常':'#34d399','社交':'#fbbf24','拍摄':'#818cf8','季节':'#fb923c',
    '氛围场景':'#c084fc','私密':'#f472b6',
  };

  function _renderTreemapChart(containerId,counts,groups,filterKey){
    const treemapData=_buildTreemapData(counts,groups);
    const dom=$('#'+containerId);
    if(!dom)return;
    const chart=echarts.init(dom);
    _statsCharts.push(chart);

    treemapData.forEach(group=>{
      const baseColor=GROUP_COLORS[group.name]||'#94a3b8';
      group.itemStyle={color:baseColor};
      if(group.children){
        group.children.forEach((child,i)=>{
          const n=group.children.length;
          const lightness=40+Math.round((i/Math.max(n-1,1))*30);
          child.itemStyle={
            color:baseColor,
            colorAlpha:[0.5+((i%3)*0.15),0.7+((i%3)*0.1)],
          };
        });
      }
    });

    chart.setOption({
      tooltip:{formatter:function(info){
        const val=info.value;
        const treePathInfo=info.treePathInfo;
        const treePath=[];
        for(let i=1;i<treePathInfo.length;i++){treePath.push(treePathInfo[i].name);}
        return treePath.join(' / ')+'<br/>数量: '+val;
      }},
      series:[{
        type:'treemap',
        data:treemapData,
        width:'95%',
        height:'90%',
        top:10,
        roam:false,
        nodeClick:'link',
        breadcrumb:{
          show:true,
          top:0,
          left:0,
          height:22,
          itemStyle:{
            color:'rgba(196,168,224,0.2)',
            borderColor:'rgba(196,168,224,0.4)',
            borderWidth:1,
          },
          textStyle:{color:'#d1d5db',fontSize:11},
          emphasis:{itemStyle:{color:'rgba(196,168,224,0.4)'}},
        },
        label:{
          show:true,
          formatter:'{b}',
          fontSize:11,
          color:'#fff',
        },
        upperLabel:{
          show:true,
          height:24,
          formatter:'{b}',
          fontSize:12,
          fontWeight:'bold',
          color:'#fff',
        },
        itemStyle:{
          borderColor:'#1f2937',
          borderWidth:2,
          gapWidth:2,
        },
        levels:[{
          itemStyle:{
            borderColor:'#374151',
            borderWidth:3,
            gapWidth:4,
          },
          upperLabel:{show:true},
        },{
          colorSaturation:[0.4,0.8],
          itemStyle:{
            borderColorSaturation:0.6,
            gapWidth:1,
            borderWidth:1,
          },
        }],
      }],
      animation:true,
      animationDuration:800,
      animationEasing:'cubicOut',
    });
    chart.on('click',(params)=>{
      if(params.data&&params.data.name&&!params.data.children){
        _statsChartClick(filterKey,params.data.name);
      }
    });
  }

  function _statsChartClick(filterKey,value){
    toggleStatsView(false);
    state.style='';state.scene='';state.atmosphere='';state.shot_size='';
    $('#styleFilter').value='';$('#sceneFilter').value='';
    $('#atmosphereFilter').value='';$('#shotSizeFilter').value='';
    if(filterKey==='style'){state.style=value;$('#styleFilter').value=value;}
    else if(filterKey==='scene'){state.scene=value;$('#sceneFilter').value=value;}
    else if(filterKey==='atmosphere'){state.atmosphere=value;$('#atmosphereFilter').value=value;}
    else if(filterKey==='shot_size'){state.shot_size=value;$('#shotSizeFilter').value=value;}
    state.page=1;state.allLoaded=false;loadImages(true);
  }

  async function loadStatsDetail(){
    _statsCharts.forEach(c=>{try{c.dispose();}catch(e){}});
    _statsCharts=[];

    const category=$('#statsCategoryFilter').value;
    const persona=$('#statsPersonaFilter').value;
    const favorite=$('#statsFavoriteFilter').value;

    const params=new URLSearchParams();
    if(category)params.set('category',category);
    if(persona)params.set('persona',persona);
    if(favorite)params.set('favorite',favorite);

    const resp=await api('/api/stats/detail?'+params.toString());
    if(!resp||!resp.ok)return;
    const data=await resp.json();

    $('#statsOverviewTotal').textContent=data.total||0;

    const statsResp=await api('/api/stats');
    if(statsResp&&statsResp.ok){
      const stats=await statsResp.json();
      $('#statsOverviewPerson').textContent=stats.by_category?.人物||0;
      $('#statsOverviewCloth').textContent=stats.by_category?.衣服||0;
    }

    _renderTop5('statsTop5ShotSize',data.shot_size||{},data.total);
    _renderTop5('statsTop5Atmosphere',data.atmosphere||{},data.total);
    _renderTop5('statsTop5Style',data.style||{},data.total);
    _renderTop5('statsTop5Scene',data.scene||{},data.total);

    _renderPieChart('chartShotSize',data.shot_size||{},'景别','shot_size');
    _renderPieChart('chartAtmosphere',data.atmosphere||{},'氛围','atmosphere');
    _renderTreemapChart('chartStyle',data.style||{},data.style_groups||{},'style');
    _renderTreemapChart('chartScene',data.scene||{},data.scene_groups||{},'scene');

    _loadTimeline(params);
  }

  async function _loadTimeline(params){
    const resp=await api('/api/stats/timeline?'+params.toString());
    if(!resp||!resp.ok)return;
    const data=await resp.json();
    if(!data||!data.length)return;

    const dates=data.map(d=>d.date);
    const counts=data.map(d=>d.count);
    const dom=$('#chartTimeline');
    if(!dom)return;
    const chart=echarts.init(dom);
    _statsCharts.push(chart);
    chart.setOption({
      tooltip:{
        trigger:'axis',
        formatter:function(p){
          return p[0].axisValue+'<br/>存图: '+p[0].value+' 张';
        },
      },
      grid:{left:50,right:20,top:20,bottom:30},
      xAxis:{
        type:'category',
        data:dates,
        axisLabel:{
          fontSize:10,
          color:'#9ca3af',
          formatter:function(v){
            return v.slice(5);
          },
        },
        axisLine:{lineStyle:{color:'#374151'}},
      },
      yAxis:{
        type:'value',
        minInterval:1,
        axisLabel:{fontSize:10,color:'#9ca3af'},
        splitLine:{lineStyle:{color:'rgba(55,65,81,0.5)'}},
      },
      series:[{
        type:'line',
        data:counts,
        smooth:true,
        symbol:'circle',
        symbolSize:4,
        lineStyle:{
          width:2,
          color:new echarts.graphic.LinearGradient(0,0,1,0,[
            {offset:0,color:'#c084fc'},
            {offset:1,color:'#f472b6'},
          ]),
        },
        areaStyle:{
          color:new echarts.graphic.LinearGradient(0,0,0,1,[
            {offset:0,color:'rgba(192,132,252,0.3)'},
            {offset:1,color:'rgba(192,132,252,0.02)'},
          ]),
        },
        itemStyle:{color:'#c084fc'},
        animationDuration:1200,
        animationEasing:'cubicOut',
      }],
    });
  }

  function toggleStatsView(show){
    const statsView=$('#statsView');
    const imageGrid=$('#imageGrid');
    const emptyState=$('#emptyState');
    const loadMoreSection=$('#scrollSentinel');
    const loadingIndicator=$('#loadingIndicator');
    const batchBar=$('#batchBar');
    const sidebar=$('#sidebar');

    if(show){
      document.body.classList.add('stats-view-active');
      statsView.classList.remove('hidden');
      imageGrid.classList.add('hidden');
      emptyState.classList.add('hidden');
      loadMoreSection.classList.add('hidden');
      loadingIndicator.classList.add('hidden');
      batchBar.classList.add('hidden');
      sidebar.classList.add('hidden');
      $('#statsViewBtn').classList.add('btn-accent');
      $('#statsViewBtn').classList.remove('btn-secondary');
      loadStatsDetail();
    }else{
      document.body.classList.remove('stats-view-active');
      statsView.classList.add('hidden');
      imageGrid.classList.remove('hidden');
      sidebar.classList.remove('hidden');
      $('#statsViewBtn').classList.remove('btn-accent');
      $('#statsViewBtn').classList.add('btn-secondary');
      _statsCharts.forEach(c=>{try{c.dispose();}catch(e){}});
      _statsCharts=[];
    }
  }

  function showContextMenu(e,id){
    hideContextMenu();
    state.contextMenuTargetId=id;

    const menu=document.createElement('div');
    menu.className='context-menu';
    menu.id='contextMenu';
    menu.style.left=e.clientX+'px';
    menu.style.top=e.clientY+'px';

    const items=[
      {icon:'❤️',label:'收藏',action:()=>quickFavorite(id,'favorite')},
      {icon:'👍',label:'喜欢',action:()=>quickFavorite(id,'like')},
      {icon:'🎨',label:'切换参考强度',action:()=>quickRefStrength(id)},
      {type:'divider'},
      {icon:'🔄',label:'重新分析',action:()=>quickReanalyze(id)},
      {type:'divider'},
      {icon:'🗑️',label:'删除',action:()=>quickDelete(id),danger:true},
    ];

    items.forEach(item=>{
      if(item.type==='divider'){
        const div=document.createElement('div');
        div.className='context-menu-divider';
        menu.appendChild(div);
        return;
      }
      const btn=document.createElement('button');
      btn.className='context-menu-item'+(item.danger?' danger':'');
      btn.innerHTML=`<span class="context-menu-icon">${item.icon}</span><span>${item.label}</span>`;
      btn.addEventListener('click',()=>{
        hideContextMenu();
        item.action();
      });
      menu.appendChild(btn);
    });

    document.body.appendChild(menu);

    const rect=menu.getBoundingClientRect();
    if(rect.right>window.innerWidth)menu.style.left=(e.clientX-rect.width)+'px';
    if(rect.bottom>window.innerHeight)menu.style.top=(e.clientY-rect.height)+'px';
  }

  function hideContextMenu(){
    const existing=document.getElementById('contextMenu');
    if(existing)existing.remove();
    state.contextMenuTargetId=null;
  }

  async function quickFavorite(id,value){
    const resp=await api(`/api/images/${id}/favorite`,{method:'PATCH',json:{favorite:value}});
    if(!resp){toast('操作失败','error');return;}
    const result=await resp.json();
    if(result.success){
      const newFav=result.favorite||value;
      toast(newFav===value?'已'+ (value==='favorite'?'收藏':'标记喜欢'):'已取消','success');
      if(state.currentImageId===id){
        state.currentImageData.favorite=newFav;
        state.detailCache.set(id,state.currentImageData);
        updateFavoriteBtns(newFav);
      }
      updateCardFavInGrid(id,newFav);
    }else{
      toast(result.error||'操作失败','error');
    }
  }

  function updateCardFavInGrid(id,fav){
    const card=document.querySelector(`.image-card[data-id="${id}"]`);
    if(!card)return;
    let favMark=card.querySelector('.image-card-fav');
    if(!favMark){
      favMark=document.createElement('div');
      favMark.className='image-card-fav';
      card.appendChild(favMark);
    }
    favMark.textContent=fav==='favorite'?'❤️':fav==='like'?'👍':'';
  }

  async function quickDelete(id){
    if(!confirm('确定删除此图片？'))return;
    const resp=await api(`/api/images/${id}`,{method:'DELETE'});
    if(!resp)return;
    const data=await resp.json();
    if(data.success){
      toast('已删除','success');
      const card=document.querySelector(`.image-card[data-id="${id}"]`);
      if(card)card.remove();
      if(state.currentImageId===id)closeDetail();
      loadStats();
    }else toast('删除失败','error');
  }

  function quickReanalyze(id){
    showDetail(id);
    setTimeout(()=>{
      const btn=$('#modalReanalyzeBtn');
      if(btn)btn.click();
    },300);
  }

  function quickRefStrength(id){
    showDetail(id);
    setTimeout(()=>{
      const btn=$('#refStrengthBtn');
      if(btn)btn.click();
    },300);
  }

  function setupViewToggle(){
    const saved=localStorage.getItem('wardrobe_view_mode')||'compact';
    state.viewMode=saved;
    applyViewMode();

    $$('#viewToggleGroup .view-toggle-btn').forEach(btn=>{
      btn.addEventListener('click',()=>{
        state.viewMode=btn.dataset.mode;
        localStorage.setItem('wardrobe_view_mode',state.viewMode);
        applyViewMode();
        updateViewToggleBtns();
      });
    });
    updateViewToggleBtns();

    window.addEventListener('resize',()=>applyViewMode());
  }

  function applyViewMode(){
    const grid=$('#imageGrid');
    if(!grid)return;
    grid.classList.remove('grid-compact','grid-large');
    grid.classList.add('grid-'+state.viewMode);
    recalculateAllSpans();
  }

  function recalculateAllSpans(){
    const grid=$('#imageGrid');
    if(!grid)return;
    const cols=getGridColumnCount();
    const gap=cols===1?0:window.innerWidth<=768?8:16;
    const gridWidth=grid.clientWidth;
    const cardWidth=(gridWidth-(cols-1)*gap)/cols;
    if(cardWidth<=0)return;
    grid.querySelectorAll('.image-card').forEach(card=>{
      const ratio=parseFloat(card.dataset.aspectRatio);
      if(!ratio||ratio<=0)return;
      const h=Math.round(cardWidth/ratio);
      const span=Math.max(h,60);
      card.style.gridRowEnd='span '+span;
      card.style.containIntrinsicSize='auto '+span+'px';
    });
  }

  function updateViewToggleBtns(){
    $$('#viewToggleGroup .view-toggle-btn').forEach(btn=>{
      btn.classList.toggle('active',btn.dataset.mode===state.viewMode);
    });
  }

  function _updateLightboxCounter(){
    const idx=state.gridImageIds.indexOf(state.lightboxId);
    if(idx>=0){
      $('#lightboxCounter').textContent=(idx+1)+' / '+state.gridImageIds.length;
    }else{
      $('#lightboxCounter').textContent='';
    }
  }

  function _navigateLightbox(direction){
    if(!state.lightboxId)return;
    const idx=state.gridImageIds.indexOf(state.lightboxId);
    if(idx<0)return;
    const newIdx=idx+direction;
    if(newIdx<0||newIdx>=state.gridImageIds.length)return;
    const newId=state.gridImageIds[newIdx];
    state.lightboxId=newId;
    _prioritizeOriginal(newId);
    _processOriginalQueue();
    const card=document.querySelector(`.image-card[data-id="${newId}"]`);
    const img=card?card.querySelector('img[data-original]'):null;
    const src=img?(img.src.replace('/thumbnail','')):`/api/image-file/${newId}`;
    $('#lightboxImage').src=src;
    _updateLightboxCounter();
    if(newId===state.currentImageId)$('#modalImage').src=src;
  }

  function init(){
    _initOriginalObserver();

    setupViewToggle();

    document.addEventListener('click',e=>{
      if(!e.target.closest('.context-menu'))hideContextMenu();
    });
    document.addEventListener('keydown',e=>{
      if(e.key==='Escape')hideContextMenu();
    });

    $$('input[name="category"]').forEach(inp=>{
      inp.addEventListener('change',()=>{
        state.category=inp.value;state.page=1;state.allLoaded=false;loadImages(true);
      });
    });

    $$('input[name="favorite"]').forEach(inp=>{
      inp.addEventListener('change',()=>{
        state.favorite=inp.value;state.page=1;state.allLoaded=false;loadImages(true);
      });
    });

    $('#sortByFilter').addEventListener('change',e=>{
      state.sort_by=e.target.value;state.page=1;state.allLoaded=false;loadImages(true);
    });

    $('#refStrengthFilter').addEventListener('change',e=>{
      state.ref_strength=e.target.value;state.page=1;state.allLoaded=false;loadImages(true);
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

    $('#backupBtn').addEventListener('click',()=>{
      $('#backupModal').classList.remove('hidden');
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

    $('#statsViewBtn').addEventListener('click',()=>{
      const statsView=$('#statsView');
      if(statsView.classList.contains('hidden')){
        toggleStatsView(true);
      }else{
        toggleStatsView(false);
      }
    });
    $('#statsBackBtn').addEventListener('click',()=>toggleStatsView(false));
    $('#statsCategoryFilter').addEventListener('change',()=>loadStatsDetail());
    $('#statsPersonaFilter').addEventListener('change',()=>loadStatsDetail());
    $('#statsFavoriteFilter').addEventListener('change',()=>loadStatsDetail());

    window.addEventListener('resize',()=>{
      _statsCharts.forEach(c=>{try{c.resize();}catch(e){}});
    });

    $('#batchDeleteBtn').addEventListener('click',batchDelete);
    $('#batchReanalyzeBtn').addEventListener('click',batchReanalyze);
    $('#batchReanalyzeFailedBtn').addEventListener('click',batchReanalyzeFailed);
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
    $('#navPrevBtn').addEventListener('click',e=>{e.stopPropagation();navigateDetail(-1);});
    $('#navNextBtn').addEventListener('click',e=>{e.stopPropagation();navigateDetail(1);});
    document.addEventListener('keydown',e=>{
      if(!$('#lightbox').classList.contains('hidden')){
        if(e.key==='ArrowLeft')_navigateLightbox(-1);
        else if(e.key==='ArrowRight')_navigateLightbox(1);
        else if(e.key==='Escape'){$('#lightbox').classList.add('hidden');state.lightboxId=null;}
        return;
      }
      if($('#detailModal').classList.contains('hidden'))return;
      if(e.key==='ArrowLeft')navigateDetail(-1);
      else if(e.key==='ArrowRight')navigateDetail(1);
      else if(e.key==='Escape')closeDetail();
    });
    $('#modalImage').addEventListener('click',e=>{
      e.stopPropagation();
      const id=state.currentImageId;
      const src=$('#modalImage').src;
      if(src&&id){
        state.lightboxId=id;
         _prioritizeOriginal(id);
         _processOriginalQueue();
         $('#lightboxImage').src=src;
        _updateLightboxCounter();
        $('#lightbox').classList.remove('hidden');
      }
    });
    $('#lightbox').addEventListener('click',e=>{
      const el=e.target instanceof Element?e.target:e.target.parentElement;
      if(el?.closest('.lightbox-arrow'))return;
      state.lightboxId=null;
      $('#lightbox').classList.add('hidden');
    });
    $('#lightboxPrev').addEventListener('click',e=>{e.stopPropagation();_navigateLightbox(-1);});
    $('#lightboxNext').addEventListener('click',e=>{e.stopPropagation();_navigateLightbox(1);});
    $('#modalDeleteBtn').addEventListener('click',()=>{if(state.currentImageId)deleteImage(state.currentImageId);});
    $('#modalEditBtn').addEventListener('click',()=>{
      state.editing=true;
      renderDetailFields(state.currentImageData,true);
      updateEditButtons();
    });
    $('#modalCancelEditBtn').addEventListener('click',()=>{
      state.editing=false;
      renderDetailFields(state.currentImageData,false);
      updateEditButtons();
    });
    $('#modalSaveBtn').addEventListener('click',saveEdit);
    $('#favFavoriteBtn').addEventListener('click',()=>toggleFavorite('favorite'));
    $('#favLikeBtn').addEventListener('click',()=>toggleFavorite('like'));
    $('#refStrengthBtn').addEventListener('click',()=>toggleRefStrengthDropdown());
    document.addEventListener('click',(e)=>{
      const wrap=$('.rs-dropdown-wrap');
      if(wrap&&!wrap.contains(e.target)){
        const dd=$('#refStrengthPanel');
        if(dd)dd.classList.add('hidden');
      }
    });
    $('#modalReanalyzeBtn').addEventListener('click',()=>{
      $('#reanalyzeSection').classList.remove('hidden');
      $('#reanalyzeDesc').value='';
      $('#reanalyzeStatus').classList.add('hidden');
    });
    $('#reanalyzeCancelBtn').addEventListener('click',()=>{
      $('#reanalyzeSection').classList.add('hidden');
    });
    $('#reanalyzeConfirmBtn').addEventListener('click',async()=>{
      if(!state.currentImageId)return;
      const desc=$('#reanalyzeDesc').value.trim();
      const statusEl=$('#reanalyzeStatus');
      const btn=$('#modalReanalyzeBtn');
      btn.disabled=true;
      btn.textContent='分析中...';
      statusEl.textContent='正在调用模型重新分析，请稍候...';
      statusEl.classList.remove('hidden');
      try{
        const resp=await api(`/api/images/${state.currentImageId}/reanalyze`,{
          method:'POST',
          json:{description:desc},
        });
        if(!resp){
          statusEl.textContent='请求失败';
          return;
        }
        const result=await resp.json();
        if(result.success){
          toast('重新分析完成','success');
          state.currentImageData=result.image||null;
          state.editing=false;
          if(state.currentImageData){
            renderDetailFields(state.currentImageData,false);
          }
          $('#reanalyzeSection').classList.add('hidden');
          updateEditButtons();
          statusEl.classList.add('hidden');
          $('#reanalyzeDesc').value='';
        }else{
          statusEl.textContent=result.error||'分析失败';
        }
      }catch(e){
        statusEl.textContent='网络错误: '+e.message;
      }finally{
        btn.disabled=false;
        btn.textContent='重新分析';
      }
    });

    $('#logoutBtn').addEventListener('click',async()=>{
      await api('/api/logout',{method:'POST'});
      localStorage.removeItem('wardrobe_token');
      window.location.href='/login';
    });

    let _scrollObserver=new IntersectionObserver(entries=>{
      if(entries[0].isIntersecting && !state.loading && !state.searchQuery){
        const loadedCount=$('#imageGrid').children.length;
        if(loadedCount<state.total){
          loadImages(false);
        }
      }
    },{rootMargin:'400px'});
    _scrollObserver.observe($('#scrollSentinel'));

    setupUpload();
    setupBackup();
    loadStats();
    loadFilters();
    loadImages(true);
  }

  function doSearch(){
    const q=$('#searchInput').value.trim();
    state.searchQuery=q;
    state.page=1;
    state.allLoaded=false;
    loadImages(true);
  }

  document.addEventListener('DOMContentLoaded',init);

  window.toggleBatchOpsPanel=toggleBatchOpsPanel;
  window.clearBatchOps=clearBatchOps;
  window.retryBatchOps=retryBatchOps;
  window.retrySingleOp=retrySingleOp;
})();
