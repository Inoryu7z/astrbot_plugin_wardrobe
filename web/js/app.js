(function(){
  const $=s=>document.querySelector(s);
  const $$=s=>document.querySelectorAll(s);

  const POOL_LABELS={
    'style':'风格','clothing_type':'服装类型','exposure_level':'暴露程度',
    'scene':'场景','atmosphere':'氛围','pose_type':'姿势',
    'body_orientation':'朝向','dynamic_level':'动态程度','action_style':'动作风格',
    'shot_size':'景别','camera_angle':'角度','expression':'表情',
    'exposure_features':'暴露特征','key_features':'关键特征','prop_objects':'道具物品','allure_features':'魅力特征','body_focus':'身体焦点',
  };

  const FIELD_DEFS=[
    {key:'category',label:'分类',type:'select',options:['人物','衣服']},
    {key:'style',label:'风格',type:'tags'},
    {key:'clothing_type',label:'服装类型',type:'text'},
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
  ];

  let state={
    page:1, perPage:24, total:0,
    category:'', persona:'', style:'', scene:'', shot_size:'', atmosphere:'', favorite:'', sort_by:'created_at',
    searchQuery:'', batchMode:false,
    selectedIds:new Set(),
    currentImageId:null,
    currentImageData:null,
    editing:false,
    loading:false,
    allLoaded:false,
    batchUploading:false,
    batchUploadProgress:{current:0,total:0,uploaded:0,failed:0},
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
      $('#imageGrid').innerHTML='';
    }

    state.loading=true;
    $('#loadingIndicator').classList.remove('hidden');
    $('#loadMoreSection').classList.add('hidden');

    let url;
    if(state.searchQuery){
      url=`/api/search?q=${encodeURIComponent(state.searchQuery)}&persona=${encodeURIComponent(state.persona)}&category=${encodeURIComponent(state.category)}&favorite=${encodeURIComponent(state.favorite)}&limit=${state.perPage}`;
    }else{
      url=`/api/images?page=${state.page}&per_page=${state.perPage}&category=${encodeURIComponent(state.category)}&persona=${encodeURIComponent(state.persona)}&style=${encodeURIComponent(state.style)}&scene=${encodeURIComponent(state.scene)}&shot_size=${encodeURIComponent(state.shot_size)}&atmosphere=${encodeURIComponent(state.atmosphere)}&favorite=${encodeURIComponent(state.favorite)}&sort_by=${encodeURIComponent(state.sort_by)}&lightweight=1`;
    }

    const resp=await api(url);
    state.loading=false;
    $('#loadingIndicator').classList.add('hidden');

    if(!resp)return;
    const data=await resp.json();
    const images=data.images||[];
    state.total=data.total||images.length;

    if(resetGrid){
      $('#imageGrid').innerHTML='';
    }

    appendGrid(images);

    if(!state.searchQuery && images.length<state.perPage){
      state.allLoaded=true;
    }else if(!state.searchQuery){
      state.page++;
    }

    const loadedCount=$('#imageGrid').children.length;
    if(!state.searchQuery && loadedCount<state.total){
      $('#loadMoreSection').classList.remove('hidden');
      $('#loadMoreInfo').textContent=`已加载 ${loadedCount} / ${state.total}`;
    }else{
      $('#loadMoreSection').classList.add('hidden');
    }

    $('#emptyState').classList.toggle('hidden',loadedCount>0);
  }

  function appendGrid(images){
    const grid=$('#imageGrid');
    images.forEach(img=>{
      const card=document.createElement('div');
      card.className='image-card';
      card.dataset.id=img.id;
      const personaText=img.persona?`<div class="image-card-persona">${esc(img.persona)}</div>`:'';
      const favIcon=img.favorite==='favorite'?'❤️':img.favorite==='like'?'👍':'';
      const favMark=favIcon?`<div class="image-card-fav">${favIcon}</div>`:'';
      const useCount=img.use_count?`<span class="image-card-uses">🔥${img.use_count}</span>`:'';
      card.innerHTML=`
        ${favMark}
        <img src="/api/image-file/${img.id}" loading="lazy" alt="" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22180%22 height=%22240%22><rect fill=%22%23F8F0F4%22 width=%22180%22 height=%22240%22/><text x=%2290%22 y=%22125%22 text-anchor=%22middle%22 fill=%22%23C8B8D0%22 font-size=%2214%22>加载失败</text></svg>'">
        <div class="image-card-overlay">
          <span class="image-card-category">${esc(img.category||'')}</span>
          ${useCount}
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

  async function showDetail(id){
    state.currentImageId=id;
    state.editing=false;
    const resp=await api(`/api/images/${id}`);
    if(!resp)return;
    const img=await resp.json();
    state.currentImageData=img;
    $('#modalImage').src=`/api/image-file/${id}`;
    renderDetailFields(img,false);
    const metaRO=$('#modalMetaReadonly');
    metaRO.innerHTML=`<span>ID: ${esc(img.id)}</span><span>创建时间: ${esc(img.created_at||'未知')}</span>`;
    updateFavoriteBtns(img.favorite||'none');
    updateEditButtons();
    $('#detailModal').classList.remove('hidden');
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
      updateFavoriteBtns(newFav);
      toast(newFav==='none'?'已取消':newFav==='favorite'?'已收藏':'已标记喜欢','success');
    }else{
      toast(result.error||'操作失败','error');
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
          const tagsArr=Array.isArray(val)?val:(val?String(val).split(',').map(s=>s.trim()).filter(Boolean):[]);
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
          const tagsArr=Array.isArray(val)?val:(val?String(val).split(',').map(s=>s.trim()).filter(Boolean):[]);
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
        renderDetailFields(state.currentImageData,false);
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
    if(state.selectedIds.size===0)return;
    const ids=[...state.selectedIds];
    if(!confirm(`确定重新分析选中的 ${ids.length} 张图片？将覆盖现有分析结果。`))return;
    const btn=$('#batchReanalyzeBtn');
    btn.disabled=true;
    btn.textContent='分析中...';
    try{
      const resp=await api('/api/images/batch-reanalyze',{
        method:'POST',
        json:{ids},
      });
      if(!resp||resp.error){toast((resp&&resp.error)||'请求失败','error');return;}
      const data=await resp.json();
      if(data.success){
        toast(`重新分析完成：${data.reanalyzed}成功，${data.failed}失败`,data.reanalyzed>0?'success':'error');
        state.page=1;state.allLoaded=false;loadImages(true);loadStats();
      }else{
        toast(data.error||'重新分析失败','error');
      }
    }catch(e){
      toast('网络错误: '+e.message,'error');
    }finally{
      btn.disabled=false;
      btn.textContent='重新分析';
    }
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

        $('#uploadModal').classList.add('hidden');
        toast(`开始批量上传 ${files.length} 张图片，可继续其他操作`,'info');

        for(let i=0;i<files.length;i++){
          state.batchUploadProgress.current=i+1;
          updateBatchUploadIndicator();
          const statusEl=document.getElementById('fileStatus'+i);
          const fd=new FormData();
          fd.append('image',files[i]);
          fd.append('persona',persona);
          fd.append('description',description);
          try{
            const resp=await api('/api/images/upload',{method:'POST',body:fd});
            if(resp&&!resp.error){
              const data=await resp.json();
              if(data.duplicate){
                state.batchUploadProgress.failed++;
                if(statusEl){statusEl.textContent='重复';statusEl.className='file-status dup';}
              }else if(data.success){
                state.batchUploadProgress.uploaded++;
                if(statusEl){statusEl.textContent='✓';statusEl.className='file-status ok';}
              }else{
                state.batchUploadProgress.failed++;
                if(statusEl){statusEl.textContent='✗';statusEl.className='file-status fail';}
              }
            }else{
              state.batchUploadProgress.failed++;
              if(statusEl){statusEl.textContent='✗';statusEl.className='file-status fail';}
            }
          }catch(err){
            state.batchUploadProgress.failed++;
            if(statusEl){statusEl.textContent='✗';statusEl.className='file-status fail';}
          }
          updateBatchUploadIndicator();
        }

        const up=state.batchUploadProgress.uploaded;
        const fl=state.batchUploadProgress.failed;
        toast(`批量上传完成：${up}成功，${fl}失败`,up>0?'success':'error');
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
    const p=state.batchUploadProgress;
    indicator.textContent=`上传中 ${p.current}/${p.total}（✓${p.uploaded} ✗${p.failed}）`;
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

  function init(){
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

    $('#batchDeleteBtn').addEventListener('click',batchDelete);
    $('#batchReanalyzeBtn').addEventListener('click',batchReanalyze);
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

    $('#loadMoreBtn').addEventListener('click',()=>loadImages(false));

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
})();
