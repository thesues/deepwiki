"use strict";
(() => {
  const n=id=>document.getElementById(id);
  const make=(tag,text='')=>{const node=document.createElement(tag);node.textContent=text;return node;};
  const styled=(tag,className,text='')=>{const node=make(tag,text);node.className=className;return node;};
  const markdown=text=>{const node=styled('div','md');renderInto(node,text);return node;};
  const states={running:'运行中',ready:'待调度',todo:'等待依赖',blocked:'已阻塞',triage:'待整理',review:'待评审',done:'已完成',archived:'已归档',cancelled:'已停止',finished:'全部完成'};
  const state=s=>states[s]||s;
  const modeLabels={single:'单体执行',kanban:'团队协作',swarm:'独立群研'};
  let selected=null,timer=null,generation=0,roots=[],renderKey='';
  let chatSession='',identity=null,draftGeneration=null,mode='single',modeLocked=false,modeGeneration=0,savingMode=false,loadingMode=false;
  const targetSession=()=>chatSession;
  const modeSelect=n('research-mode-select');

  function setStatus(text,kind='ready'){
    const node=n('board-status');node.hidden=!text;node.dataset.state=kind;
    if(node.textContent!==text)node.textContent=text;
  }
  async function request(path,body){
    const response=await authFetch('/api/kanban/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const value=await response.json();if(!response.ok)throw new Error(value.error||`HTTP ${response.status}`);return value;
  }
  function paintMode(){
    modeSelect.value=mode;modeSelect.disabled=modeLocked||savingMode||loadingMode||!!S.owns||!!S.switching;
    modeSelect.closest('label').hidden=modeLocked;
    const fixed=n('research-mode-fixed');fixed.hidden=!modeLocked;fixed.textContent=modeLabels[mode]+' · 已固定';
    if(loadingMode)return;
    document.body.dataset.executionMode=mode;
    n('board-toggle').hidden=mode==='single';
    if(mode==='single'){n('research-board').hidden=true;n('board-toggle').setAttribute('aria-expanded','false');}
    welcome.title=mode==='single'?'我能帮你做什么？':mode==='swarm'?'让多个 Agent 独立研究':'一起完成一个复杂任务';
    welcome.subtitle=mode==='single'?'直接执行，也可以选择团队协作或独立群研':'主 Agent 负责协调与汇总，执行 Agent 负责研究与验证';
    const fresh=document.querySelector('.chat.fresh');
    if(fresh){
      const title=fresh.querySelector('.fresh-title'),sub=fresh.querySelector('.fresh-sub');
      if(title)title.textContent=welcome.title;if(sub)sub.textContent=welcome.subtitle;
      fresh.classList.toggle('has-capabilities',mode==='single'&&!!n('welcome-introduction'));
    }
  }
  async function loadMode(){
    if(!chatSession||savingMode)return;
    const sid=chatSession,current=++modeGeneration;
    try{
      const value=await request('mode?session_id='+encodeURIComponent(sid));
      if(current===modeGeneration){const wasLoading=loadingMode;mode=value.mode;modeLocked=!!value.locked;loadingMode=false;paintMode();if(wasLoading&&!n('research-board').hidden)refresh();}
    }catch(error){if(current===modeGeneration)setStatus(error.message,'error');}
  }
  window.webuiChatContext=()=>{
    if(S.identityScope===null)throw new Error('正在确认登录身份，请稍候再发送');
    if(loadingMode||savingMode)throw new Error('执行方式仍在加载或保存');
    if(modeSelect.value!==mode)throw new Error('执行方式不同步，请重新选择后发送');
    return {researchMode:mode};
  };
  async function chooseMode(next){
    if(modeLocked||savingMode||loadingMode||S.owns||S.switching||next===mode){paintMode();return;}
    const before=mode,sid=chatSession,current=++modeGeneration;mode=next;savingMode=true;paintMode();
    try{if(sid)await request('mode',{session_id:sid,mode:next});}
    catch(error){if(current===modeGeneration){mode=before;setStatus(error.message,'error');}}
    finally{if(current===modeGeneration){savingMode=false;paintMode();if(sid)loadMode();}}
  }
  modeSelect.onchange=()=>chooseMode(modeSelect.value);
  function clearDetail(){renderKey='';n('swarm-detail').replaceChildren();n('board-overview').hidden=true;}
  function paintSession(){
    const title=(S.sessionRows||[]).find(s=>s.id===chatSession)?.title;
    n('board-session-title').textContent=title||'当前会话';
    n('board-session-title').title=title||'跟随当前聊天会话';
  }
  async function syncSession(){
    const sid=S.pendingNew?'':S.sessionId||'',scope=S.identityScope;
    if(sid===chatSession&&scope===identity&&(sid||draftGeneration===S.viewGen)){paintSession();paintMode();loadMode();return;}
    // First identity resolution belongs to the same unsent draft, not a new user/view.
    // A real identity change or explicit new-session click still resets the mode.
    const keepDraft=identity===null&&scope!==null&&!sid&&!chatSession&&draftGeneration===S.viewGen;
    if(scope!==identity)roots=[];
    chatSession=sid;identity=scope;draftGeneration=S.viewGen;selected=null;mode=keepDraft?mode:'single';modeLocked=false;savingMode=false;loadingMode=!!sid;
    n('board-toggle').textContent='Agent 看板';n('board-updated').textContent='';setStatus('');
    ++modeGeneration;++generation;clearTimeout(timer);clearDetail();n('swarm-list').replaceChildren();paintSession();paintMode();
    refresh();loadMode();
  }
  document.addEventListener('webui:session-change',syncSession);

  const badge=(text,value)=>{const node=styled('span','state-badge',text);node.dataset.state=value;return node;};
  const dot=value=>{const node=styled('span','state-dot');node.dataset.state=value;node.setAttribute('aria-hidden','true');return node;};
  const duration=value=>{
    if(value==null||!Number.isFinite(Number(value)))return '—';
    const seconds=Math.max(0,Number(value));
    if(seconds>0&&seconds<.1)return '<0.1s';
    if(seconds<60)return `${Math.round(seconds*10)/10}s`;
    if(seconds<3600)return `${Math.floor(seconds/60)}m${Math.floor(seconds%60)}s`;
    return `${Math.floor(seconds/3600)}h${Math.floor(seconds%3600/60)}m`;
  };
  const bytes=value=>value>=1048576?(value/1048576).toFixed(1)+' MB':value>=1024?Math.round(value/1024)+' KB':value+' B';
  const clock=at=>{
    const date=new Date(at*1000),time=date.toLocaleTimeString('zh-CN',{hour12:false});
    return date.toDateString()===new Date().toDateString()?time:date.toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})+' '+time;
  };
  function disclosure(key,label,className='worker-disclosure'){
    const node=styled('details',className);node.dataset.viewKey=key;node.append(make('summary',label));return node;
  }
  function renderOverview(groups){
    const workers=groups.flatMap(g=>g.workers),overview=n('board-overview');overview.hidden=!workers.length;
    overview.replaceChildren();if(!workers.length)return;
    const heading=styled('div','overview-heading'),count=styled('div','overview-count');
    count.append(make('strong',String(workers.length)),make('span','个 Agent'));
    heading.append(count,styled('span','overview-caption',selected?'当前任务组':'当前会话'));
    const track=styled('div','board-state-track');track.setAttribute('role','img');
    const counts=new Map();
    for(const w of workers){
      counts.set(w.status,(counts.get(w.status)||0)+1);
      const cell=styled('span','board-state-cell');cell.dataset.state=w.status;cell.title=`${w.model} · ${w.title} · ${state(w.status)}`;track.append(cell);
    }
    track.setAttribute('aria-label',[...counts].map(([s,total])=>`${total} 个${state(s)}`).join('，'));
    const key=styled('div','board-state-key');
    for(const s of ['running','blocked','ready','todo','done',...counts.keys()]){
      if(!counts.has(s))continue;
      const item=make('span');item.append(dot(s),make('span',`${counts.get(s)} ${state(s)}`));key.append(item);counts.delete(s);
    }
    overview.append(heading,track,key);
  }
  function renderWorker(w,i,group){
    const card=styled('article','worker-status');card.dataset.taskId=w.id;card.dataset.state=w.status;
    const heading=styled('div','worker-heading');
    const endpoint=S.endpoints.find(e=>e.key===w.model);
    const model=styled('strong','worker-model',endpoint?.label||w.model);model.title=endpoint?.model||w.model;
    heading.append(model,badge(state(w.status),w.status));card.append(heading);
    card.append(styled('p','worker-assignment',`Agent ${i+1}${group.mode==='kanban'?' · '+w.title:''}`));
    const action=styled('div','worker-action');action.append(make('p',w.activity));
    const source={native_log:'原生操作记录',native_messages:'原生工具记录',native_state:'任务状态',worker_note:'Agent 主动汇报'}[w.activity_source]||'任务状态';
    const sourceTime=styled('small','worker-source',source);sourceTime.dataset.actionSource=source;if(w.activity_at)sourceTime.dataset.actionAt=w.activity_at;
    action.append(sourceTime);card.append(action);
    if(w.status_reason){const reason=styled('div','worker-blocker');reason.append(make('strong','阻塞原因'),make('span',w.status_reason));card.append(reason);}
    if(w.dependencies?.length)card.append(styled('p','worker-dependencies','等待前置：'+w.dependencies.map(p=>`${p.title}（${state(p.status)}）`).join('；')));
    if(w.stats){
      const metrics=styled('dl','worker-metrics');
      for(const [label,text,exact] of [['模型调用',w.stats.model_calls,w.stats.model_calls+' 次'],['模型耗时',duration(w.stats.model_seconds),w.stats.model_seconds+' 秒'],['工具调用',w.stats.tool_calls,w.stats.tool_calls+' 次'],['工具耗时',duration(w.stats.tool_seconds),w.stats.tool_seconds+' 秒']]){
        const item=make('div');item.title=exact;item.append(make('dt',label),make('dd',String(text)));metrics.append(item);
      }
      card.append(metrics);if(w.stats.windowed)card.append(styled('p','worker-window','统计仅覆盖当前日志窗口'));
    }
    const runtime=styled('p','worker-runtime');runtime.dataset.workerTime=w.id;card.append(runtime);
    if(w.recent_actions?.length){
      const actions=disclosure(w.id+':actions','最近操作 · '+w.recent_actions.length),list=styled('ol','worker-tools');
      for(const a of w.recent_actions){
        const item=make('li');item.append(make('code',a.tool),make('span',duration(a.duration)),make('time',clock(a.at)));list.append(item);
      }
      actions.append(list);card.append(actions);
    }
    const diagnostics=disclosure(w.id+':run','运行详情'),body=styled('div','worker-diagnostics');
    const identity=make('p');identity.append(make('span','任务 '),make('code',w.id));body.append(identity);
    if(w.run)body.append(make('p',`运行记录 #${w.run.id}${w.pid?' · PID '+w.pid:''}`));
    if(typeof w.goal_mode==='boolean')body.append(make('p',w.goal_mode?'持续研究 · 原生 goal-loop':'单次任务 · 原生 single-shot'));
    const limit=w.run?.max_runtime_seconds??group.budget.wall_seconds;
    if(limit)body.append(make('p',`每次运行上限 ${limit} 秒`));
    if(w.consecutive_failures)body.append(make('p',`连续失败 ${w.consecutive_failures} 次 · 原生阻塞阈值 ${w.failure_limit} 次`));
    if(w.status==='running'&&w.pid)body.append(make('p',w.alive?'进程存活':'PID 已退出，等待调度器更新'));
    if(w.status==='running'){const heartbeat=make('p');heartbeat.dataset.workerHeartbeat=w.id;body.append(heartbeat);}
    if(w.timing?.sleep_aware)body.append(make('p',`计时扣除系统休眠 · 已暂停 ${w.timing.paused_seconds} 秒`));
    diagnostics.append(body);card.append(diagnostics);
    if(w.attachments?.length){
      const downloads=styled('div','worker-downloads');
      for(const a of w.attachments){
        const link=styled('a','worker-download');link.href=a.url;link.setAttribute('download','');link.title=`下载 ${a.filename}（${a.size} B）`;
        link.append(make('span','↓ '+a.filename),make('small',bytes(a.size)));downloads.append(link);
      }
      card.append(downloads);
    }
    if(w.result){const result=disclosure(w.id+':result','交付结果','worker-disclosure worker-result');result.append(markdown(w.result));card.append(result);}
    return card;
  }
  function renderPublications(group,detail){
    const records=group.public_records||(group.publications||[]).map(p=>({...p,kind:'stage'}));
    const heading=styled('div','board-section-heading');
    heading.append(make('h4','公开成果与验证'),make('span',`${records.filter(p=>p.kind==='stage').length} 条阶段成果 · ${records.filter(p=>p.kind==='review').length} 份验证报告`));detail.append(heading);
    if(!records.length){detail.append(styled('p','publication-empty','尚无公开成果。阶段提交和验证报告会显示在这里。'));return;}
    const feed=styled('div','publication-list');
    const stageStates={PUBLISHED:'待复验',PEER_SUPPORTED:'同行支持',DISPUTED:'有分歧'};
    for(const p of records){
      const author=p.author_label||p.author;
      const title=p.kind==='review'?(p.stage_claim||p.stage):p.kind==='stage'?p.claim:'公开发布';
      const entry=styled('details','publication-entry');entry.dataset.publicKind=p.kind;entry.dataset.viewKey=group.id+':'+p.id;
      const summary=make('summary'),label=styled('span','publication-title',title);label.title=title;
      summary.append(styled('span','publication-kind',p.kind==='review'?(p.independent?'同行验证':'复验报告'):p.kind==='stage'?'阶段成果':'公开发布'),label);
      if(p.kind==='review')summary.append(badge(p.verdict,p.verdict));else if(p.status)summary.append(badge(stageStates[p.status]||p.status,p.status));
      const scope=typeof p.scope==='string'?p.scope.trim():'';
      const byline=styled('span','publication-author',(scope?scope+' · ':'')+author+(p.kind==='review'&&!p.independent?' · 非独立复验':''));
      byline.title=byline.textContent;summary.append(byline);entry.append(summary);
      const body=styled('div','publication-body');body.append(make('p',`${p.kind==='review'?'验证者':'发布者'}：${author}`));
      if(scope)body.append(make('p','成果范围：'+scope));
      if(p.kind==='stage')body.append(make('p',p.claim));
      if(p.kind==='review'){
        body.append(make('p',`对应成果：${p.stage} · 原发布者：${p.stage_author||'尚未匹配'}`));
        if(!p.independent)body.append(make('p','非独立复验：自验或尚未匹配目标成果，不计入独立同行支持。'));
      }
      body.append(markdown('#### 证据\n\n'+p.evidence),markdown('#### 复现\n\n'+(p.reproduction||'')),markdown('#### 限制\n\n'+(p.limitations||'')));
      body.append(styled('small','publication-provenance',`comment #${p.comment_id} · ${p.id}`));entry.append(body);feed.append(entry);
    }
    detail.append(feed,styled('p','publication-note','同行报告不等于形式验证；这里保留成果与复验的原始出处。'));
  }
  function render(group,detail){
    detail.dataset.groupId=group.id;
    const heading=styled('div','group-heading'),title=styled('div','group-title'),meta=styled('div','group-meta');
    title.append(make('h3',group.title));meta.append(make('span',modeLabels[group.mode]),badge(state(group.state),group.state==='finished'?'done':group.state));title.append(meta);heading.append(title);
    if(!['cancelled','finished'].includes(group.state)){
      const cancel=styled('button','board-stop','停止本组');cancel.type='button';
      cancel.onclick=async()=>{
        if(group.session_id!==targetSession()||!confirm('停止并归档这一组任务？'))return;
        cancel.disabled=true;try{await request('cancel',{id:group.id});await refresh();}catch(e){setStatus('未能停止：'+e.message,'error');cancel.disabled=false;}
      };heading.append(cancel);
    }
    detail.append(heading);
    const goal=disclosure(group.id+':goal','目标与执行约束','group-contract'),contract=styled('div','contract-body');
    contract.append(make('p',group.question),make('p',`每次运行最多 ${group.budget.wall_seconds} 秒 · 每回合最多 ${group.budget.max_iterations??'未声明'} 次模型迭代${group.mode==='swarm'?' · 持续研究最多 '+group.budget.max_turns+' 回合':''}`));goal.append(contract);detail.append(goal);
    const workers=styled('div','worker-grid');group.workers.forEach((w,i)=>workers.append(renderWorker(w,i,group)));detail.append(workers);
    renderPublications(group,detail);
  }
  function paintClocks(groups){
    const workers=new Map(groups.flatMap(g=>g.workers).map(w=>[w.id,w]));
    for(const node of n('swarm-detail').querySelectorAll('[data-worker-time]')){
      const w=workers.get(node.dataset.workerTime);if(!w)continue;
      const seconds=w.timing?.elapsed_seconds??(w.run?.started_at?Math.max(0,(w.run.ended_at||Math.floor(Date.now()/1000))-w.run.started_at):null);
      node.textContent=w.run?`运行 #${w.run.id} · 已用 ${duration(seconds)}`:seconds!==null?`已用 ${duration(seconds)}`:'尚无运行记录';
      node.title=seconds===null?'尚无运行记录':`运行记录已用 ${seconds} 秒`;
    }
    for(const node of n('swarm-detail').querySelectorAll('[data-action-at]'))node.textContent=`${node.dataset.actionSource} · ${clock(Number(node.dataset.actionAt))}`;
    for(const node of n('swarm-detail').querySelectorAll('[data-worker-heartbeat]')){
      const w=workers.get(node.dataset.workerHeartbeat);if(!w)continue;
      node.textContent=w.heartbeat_age==null?'尚无存活心跳；动作记录独立展示':`存活心跳：${w.heartbeat_age} 秒前（仅表示存活）`;
      node.classList.toggle('worker-stale',w.heartbeat_age==null||w.heartbeat_age>90);
    }
  }
  function renderGroups(groups){
    const key=JSON.stringify(groups,(k,v)=>['heartbeat_age','elapsed_seconds'].includes(k)?undefined:v);
    if(key!==renderKey){
      const body=n('swarm-detail').closest('.board-body'),scroll=body?.scrollTop||0;
      const open=new Set([...n('swarm-detail').querySelectorAll('details[open][data-view-key]')].map(d=>d.dataset.viewKey));
      const focused=document.activeElement?.closest('details[data-view-key]')?.dataset.viewKey;
      n('swarm-detail').replaceChildren();
      for(const group of groups){const section=styled('section','research-group');render(group,section);n('swarm-detail').append(section);}
      for(const node of n('swarm-detail').querySelectorAll('details[data-view-key]')){node.open=open.has(node.dataset.viewKey);if(node.dataset.viewKey===focused)node.querySelector('summary').focus({preventScroll:true});}
      if(body)body.scrollTop=scroll;renderKey=key;
    }
    renderOverview(groups);paintClocks(groups);
  }
  async function refresh(){
    clearTimeout(timer);const current=++generation,sid=targetSession();
    try{
      if(mode==='single')return;
      const response=await request('swarms');if(current!==generation)return;roots=response.swarms;paintSession();
      const started=chatSession?roots.filter(r=>(r.session_id||'')===chatSession):[];
      if(chatSession&&started.length){modeLocked=true;mode=started.reduce((a,b)=>a.created_at<b.created_at?a:b).mode;paintMode();}
      const list=sid?roots.filter(r=>(r.session_id||'')===sid):[];
      const active=started.reduce((total,r)=>total+(r.active_count??(['running','ready'].includes(r.state)?1:0)),0);
      n('board-toggle').textContent='Agent 看板'+(active?' · '+active:'');
      if(n('research-board').hidden)return;
      n('swarm-list').replaceChildren();
      if(selected&&!list.some(r=>r.id===selected)){selected=null;clearDetail();}
      if(list.length){const all=styled('button','board-group-chip','全部 Agent');all.type='button';all.setAttribute('aria-pressed',String(selected===null));all.onclick=()=>{selected=null;clearDetail();refresh();};n('swarm-list').append(all);}
      for(const group of list){
        const button=styled('button','board-group-chip');button.type='button';button.title=`${group.title} · ${state(group.state)}`;button.append(dot(group.state),make('span',group.title));
        button.dataset.swarmId=group.id;button.setAttribute('aria-pressed',String(selected===group.id));button.onclick=()=>{selected=group.id;clearDetail();refresh();};n('swarm-list').append(button);
      }
      const visible=selected?list.filter(r=>r.id===selected):list;
      const groups=await Promise.all(visible.map(r=>request('swarm?id='+encodeURIComponent(r.id))));if(current!==generation)return;
      if(groups.some(g=>(g.session_id||'')!==sid))throw new Error('任务不属于所选会话');
      groups.sort((a,b)=>Number(['running','ready'].includes(b.state))-Number(['running','ready'].includes(a.state))||Number(b.workers.some(w=>w.status==='blocked'))-Number(a.workers.some(w=>w.status==='blocked'))||b.created_at-a.created_at);
      renderGroups(groups);
      setStatus(list.length?'':'该会话尚无 Agent 任务。发送任务后，执行记录会显示在这里。',list.length?'ready':'empty');
      n('board-updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
    }catch(error){if(current===generation)setStatus('未能更新：'+error.message+(renderKey?' · 保留上次记录':''),'error');}
    finally{if(current===generation)timer=setTimeout(refresh,n('research-board').hidden?5000:3000);}
  }
  function placeBoard(){
    const panel=n('research-board');if(panel.hidden)return;
    const composer=n('composer').getBoundingClientRect(),chat=document.querySelector('.chat').getBoundingClientRect(),header=document.querySelector('.shell > header').getBoundingClientRect();
    if(!chat.width)return;
    const edge=innerWidth<=820?0:12,approval=n('approval-dock');
    const bottomAnchor=approval&&!approval.hidden?Math.min(composer.top,approval.getBoundingClientRect().top):composer.top;
    const top=Math.max(8,header.bottom+8);
    if(bottomAnchor-top<160){
      panel.hidden=true;n('board-toggle').setAttribute('aria-expanded','false');
      if(panel.contains(document.activeElement))n('board-toggle').focus({preventScroll:true});
      return;
    }
    panel.style.right=Math.max(8,innerWidth-chat.right+edge)+'px';
    panel.style.width=Math.min(600,chat.width-edge*2)+'px';
    panel.style.top=top+'px';
    panel.style.bottom=Math.max(12,innerHeight-bottomAnchor+8)+'px';
  }
  function toggle(open){n('research-board').hidden=!open;n('board-toggle').setAttribute('aria-expanded',String(open));if(open)placeBoard();refresh();}
  n('board-toggle').onclick=()=>toggle(n('research-board').hidden);
  n('board-close').onclick=()=>{toggle(false);n('board-toggle').focus();};n('board-refresh').onclick=refresh;
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!n('research-board').hidden){event.preventDefault();toggle(false);n('board-toggle').focus();}});
  if(typeof ResizeObserver!=='undefined'){const observer=new ResizeObserver(placeBoard);observer.observe(n('composer'));observer.observe(n('approval-dock'));observer.observe(document.querySelector('.chat'));}
  window.addEventListener('resize',placeBoard);window.visualViewport?.addEventListener('resize',placeBoard);
  syncSession();
})();
