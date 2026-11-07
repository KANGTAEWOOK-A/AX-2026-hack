'use strict';
const $=q=>document.querySelector(q), $$=q=>[...document.querySelectorAll(q)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let items=[], queueItems=[], active=null, health=null, toastTimer=null, activeDraft=null, draftBusy=false;
let returnFocus=null, inferenceBusy=false, queueVisible=false, itemCount=0;
const cropRuns=new Map();
let cropRunningId=null;
function toast(msg,bad=false){const el=$('#toast');el.textContent=msg;el.classList.toggle('bad',bad);el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),4200)}
async function api(path, options={}){const r=await fetch(path.replace(/^\//,""),options);let j;try{j=await r.json()}catch{j={detail:`HTTP ${r.status}`}}if(!r.ok)throw Error(typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||j));return j}
const tag=p=>({normal:'AI 결함 미검출',defect:'결함 검출',unknown:'결과 미해석',pending:'분석 대기',error:'분석 오류'}[p]||p);
const reviewTag=s=>({pending:'검토 대기',completed:'검토 완료',hold:'보류'}[s]||s);
const formatPercent=v=>v==null?'평가 불가':`${(v*100).toFixed(1)}%`;
async function refresh(){
  try{
    const [h,s,p]=await Promise.all([api('/api/health'),api('/api/summary'),api('/api/items?limit=400')]);health=h;items=p.items;itemCount=p.count;
    $('#connectionDot').classList.toggle('online',h.aistream_configured);$('#connectionLabel').textContent=h.aistream_configured?'URL 설정됨':'URL 미설정';
    $('#agentDot').classList.toggle('online',h.agentstream_configured);
    $('#agentLabel').textContent=h.agentstream_configured?'API 키 설정됨':'API 키 미설정';
    $('#statTotal').textContent=s.total.toLocaleString();$('#statAnalyzed').textContent=s.analyzed.toLocaleString();$('#statNormals').textContent=s.normals.toLocaleString();$('#statReviewed').textContent=s.reviewed.toLocaleString();
    $('#rankMode').textContent=h.ranker_trained?'오류 예측 모델 학습됨':'규칙 기반 · 학습 전';
    $('#rankExplanation').textContent=h.ranker_trained
      ? '부분 검출 근거를 먼저 표시합니다. 부분 분석 전 사진의 예비 순서는 학습된 오류 순위 모델로 정합니다.'
      : '전체에서 미검출되고 부분에서 검출된 사진을 먼저 표시합니다. 부분 분석 전 사진은 영상 품질 규칙에 따른 예비 후보입니다.';
    $('#simulationNotice').classList.toggle('hidden',s.simulation===0);
    renderItems();
  }catch(e){toast(e.message,true)}
}
function renderItems(){
  const host=$('#itemList');
  const query=$('#itemSearch').value.trim().toLowerCase();
  const filter=$('#itemFilter').value;
  const shown=items.filter(row=>{
    const status=row.api_state==='error'?'error':row.pred;
    const matchesStatus=!filter||status===filter||(filter==='error'&&status==='unknown');
    return matchesStatus&&`${row.filename} ${row.session||''}`.toLowerCase().includes(query);
  });
  const scope=items.length<itemCount?` · 최근 ${items.length}장 기준`:'';
  $('#itemsCaption').textContent=`${shown.length}장 표시 / 전체 ${itemCount}장${scope}`;
  if(!shown.length){
    host.innerHTML=items.length
      ? '<div class="empty-state">검색 조건에 맞는 이미지가 없습니다.</div>'
      : '<div class="empty-state">등록된 이미지가 없습니다.<br>블레이드 사진을 업로드하세요.</div>';
    return;
  }
  host.innerHTML=shown.map(row=>{
    const status=row.api_state==='error'?'error':row.pred;
    const session=row.session||'촬영 세션 미지정';
    return `<button type="button" class="item-row" data-id="${esc(row.id)}">
      <img src="${esc(row.thumbnail_url||row.image_url)}" loading="lazy" alt="">
      <span><b title="${esc(row.filename)}">${esc(row.filename)}</b>
        <small>${row.origin==='simulated'?'시뮬레이션 · ':''}${esc(session)} · ${esc(reviewTag(row.review_status))}</small></span>
      <span class="item-tag ${esc(status)}">${esc(tag(status))}</span>
    </button>`;
  }).join('');
  $$('#itemList .item-row').forEach(button=>button.addEventListener('click',()=>openModal(button.dataset.id)));
}
async function upload(file){if(!file)return;const fd=new FormData();fd.append('file',file);try{toast('이미지를 등록합니다.');const out=await api('/api/upload',{method:'POST',body:fd});await refresh();await openModal(out.id);toast('이미지가 등록됐습니다. 검토창에서 AI 분석을 실행하세요.')}catch(e){toast(e.message,true)}}
async function inferPending(){if(inferenceBusy)return;if(!health?.aistream_configured){toast('먼저 .env에 AISTREAM_WIND_URL을 설정하고 서버를 재시작하세요.',true);return}
  const target=items.filter(r=>r.api_state!=='success'&&r.origin!=='simulated');if(!target.length){toast('추론 대상이 없습니다.');return}
  if(!confirm(`${target.length}장의 AIStream API 추론을 시작합니다. 계속할까요?`))return;
  inferenceBusy=true;updateInferenceButtons();let ok=0,errors=0;for(let i=0;i<target.length;i++){
    $('#inferProgress').textContent=`추론 ${i+1}/${target.length}`;
    try{await api(`/api/infer/${target[i].id}`,{method:'POST'});ok++}catch(e){errors++;if(i===0)toast(`첫 추론 오류: ${e.message}`,true)}
    if((i+1)%3===0||i===target.length-1)await refresh();
  }
  $('#inferProgress').textContent='';inferenceBusy=false;updateInferenceButtons();if(queueVisible)await generateQueue();toast(`추론 완료 ${ok}건 · 오류 ${errors}건`,!!errors)
}
async function generateQueue(){
  const k=Number($('#budget').value)||5;
  try{
    const result=await api(`/api/queue?k=${k}`);
    queueItems=result.items;
    queueVisible=true;
    $('#queueCaption').textContent=result.simulation_only
      ? `시뮬레이션 · ${result.k}장 배정`
      : `부분 검출 근거 ${result.evidence_count}장 · 후보 ${result.eligible}장 중 ${result.k}장 배정`;
    $('#queueList').innerHTML=result.items.map((row,index)=>{
      const regions=(row.detected_regions||[]).map(region=>`T${region}`).join(', ');
      const reason=regions?`전체 미검출 · ${regions} 부분 검출`:`부분 분석 전 · ${row.priority_method}`;
      return `<button class="queue-row" type="button" data-id="${esc(row.id)}">
        <span class="queue-rank">${String(index+1).padStart(2,'0')}</span>
        <img src="${esc(row.thumbnail_url||row.image_url)}" alt="">
        <span class="row-body"><b>${esc(row.filename)}</b><small>${esc(reason)}</small></span>
        <span class="item-tag ${regions?'defect':'pending'}">${regions?'부분 검출':'예비 후보'}</span>
      </button>`;
    }).join('')||'<div class="empty-state compact">배정할 재검수 대기 이미지가 없습니다.</div>';
    $$('#queueList .queue-row').forEach(button=>button.addEventListener('click',()=>openModal(button.dataset.id)));
  }catch(error){
    toast(error.message,true);
  }
}
async function train(){try{const r=await api('/api/train',{method:'POST'});toast(`학습 완료: ${r.train_count}건, 확인된 누락 결함 ${r.missed_defect_count}건`);await refresh()}catch(e){toast(e.message,true)}}
async function evaluate(){try{const k=Number($('#budget').value)||5;const d=await api(`/api/evaluate?k=${k}`);
 if(!d.test_count){$('#evalContent').innerHTML='<div class="empty-state">분리된 TEST 집합에 모델 미검출 결과와 정답이 모두 있는 이미지가 없습니다.</div>';$('#revealWrap').classList.add('hidden');return}
 $('#evalContent').innerHTML=d.methods.map(m=>`<div class="eval-row"><div class="eval-label"><b>${esc(m.method)}</b><span>${formatPercent(m.capture_at_k)} · ${m.found}/${d.missed_defect_count}건 포착</span></div><div class="eval-bar"><div class="eval-fill ${m.method==='Wind Recheck'?'proposed':''}" style="width:${(m.capture_at_k||0)*100}%"></div></div><small>Precision@K ${formatPercent(m.precision_at_k)} · Remaining@K ${m.remaining}건</small></div>`).join('')+
 `<div class="eval-note">기존 규칙 비교 · 부분 재분석 정책은 이 평가에 포함되지 않습니다.<br>TEST ${d.test_count}장 · 실제 누락 결함 ${d.missed_defect_count}장 · 검수 예산 K=${d.k} · 누락 0건일 때 포착률은 평가 불가<br>균등 무작위 기대 포착률 ${formatPercent(d.random_expectation?.capture_at_k)} · 무작위 막대는 고정 seed 1회 결과<br>${esc(d.warning)}</div>`;
 $('#revealWrap').classList.remove('hidden');$('#revealContent').innerHTML='';
 }catch(e){toast(e.message,true)}}
async function reveal(){try{const k=Number($('#budget').value)||5;const x=await api(`/api/reveal?k=${k}`);$('#revealContent').innerHTML=x.items.map(r=>`<div class="reveal-tile"><img src="${esc(r.thumbnail_url||r.image_url)}"/><div>${esc(r.filename)}<br><b>실제 ${r.truth==='defect'?'결함':'정상'}</b></div></div>`).join('')||'선택된 평가 이미지가 없습니다.'}catch(e){toast(e.message,true)}}
function updateInferenceButtons(){
  $('#inferBtn').disabled=inferenceBusy;
  const button=$('#inferItemBtn');
  button.disabled=inferenceBusy||!health?.aistream_configured;
  button.classList.toggle('hidden',!active||active.origin==='simulated'||(active.api_state==='success'&&active.pred!=='unknown'));
  button.textContent=inferenceBusy?'AI 분석 중…':'이 이미지 AI 분석';
}
const regionNames=['왼쪽 위','오른쪽 위','왼쪽 아래','오른쪽 아래'];

function renderCropPanel(){
  const eligible=active?.origin!=='simulated'&&active?.api_state==='success'&&active.pred==='normal';
  $('#cropPanel').classList.toggle('hidden',!eligible);
  if(!eligible)return;
  const results=cropRuns.get(active.id)||[];
  const running=cropRunningId===active.id;
  const detected=results.filter(result=>result.pred==='defect');
  const failed=results.filter(result=>result.pred==='error'||result.pred==='unknown'||result.pred==='pending');
  $('#cropBtn').disabled=Boolean(cropRunningId)||!health?.aistream_configured;
  $('#cropBtn').textContent=running?'분석 중…':(results.length?'다시 분석':'4개 영역 분석');
  let status='부분 분석을 시작하세요.';
  if(running)status=`영역 분석 진행 중 · ${results.length}/4 완료`;
  else if(detected.length)status=`전체 미검출 · 부분 ${detected.length}개 영역 검출 · 작업자 확인 필요`;
  else if(failed.length)status='일부 영역의 결과를 확인할 수 없어 비교를 보류합니다.';
  else if(results.length===4)status='전체와 4개 부분 영역 모두 결함 미검출입니다.';
  else if(results.length)status=`부분 분석 ${results.length}/4개 저장됨 · 나머지 영역 분석이 필요합니다.`;
  if(detected.length&&failed.length&&!running)status+=` · 미확인 ${failed.length}개`;
  $('#cropStatus').textContent=status;
  $('#cropResults').innerHTML=results.map(result=>{
    const picture=result.preview_jpeg&&/^[A-Za-z0-9+/=]+$/.test(result.preview_jpeg)
      ? `<img src="data:image/jpeg;base64,${result.preview_jpeg}" alt="T${result.region} 부분 사진">`:'';
    const detail=result.error||(result.labels||[]).join(' · ');
    return `<article class="crop-card ${result.pred==='defect'?'crop-detected':''}">
      ${picture}<b>T${result.region} · ${regionNames[result.region-1]}</b>
      <span class="item-tag ${esc(result.pred)}">${esc(tag(result.pred))}</span>
      ${detail?`<small>${esc(detail)}</small>`:''}
    </article>`;
  }).join('');
}

async function loadCrops(row){
  if(row.pred!=='normal'||row.api_state!=='success'||row.origin==='simulated'||cropRunningId===row.id)return;
  try{
    const data=await api(`/api/reanalysis/${encodeURIComponent(row.id)}`);
    if(active===row&&cropRunningId!==row.id){
      cropRuns.set(row.id,data.results);
      renderCropPanel();
    }
  }catch(error){
    if(active===row&&cropRunningId!==row.id)toast(error.message,true);
  }
}

async function analyzeCrops(){
  if(!active||cropRunningId||!health?.aistream_configured)return;
  const id=active.id;
  const results=[];
  cropRuns.set(id,results);
  cropRunningId=id;
  renderCropPanel();
  try{
    for(let region=1;region<=4;region++){
      try{
        const result=await api(`/api/reanalyze/${encodeURIComponent(id)}/${region}`,{method:'POST'});
        results.push(result);
      }catch(error){
        results.push({region,pred:'error',error:error.message});
      }
      if(active?.id===id)renderCropPanel();
    }
  }finally{
    cropRunningId=null;
    renderCropPanel();
    if(active?.id===id)await loadCrops(active);
    if(queueVisible||(cropRuns.get(id)||[]).some(result=>result.pred==='defect'))await generateQueue();
  }
}

function renderDetails(row){
  const state=row.api_state==='error'?'error':row.pred;
  const details=[
    tag(state),
    reviewTag(row.review_status),
    row.priority==null?'':`재검수 순위 점수 ${(row.priority*100).toFixed(1)}`,
    row.reason||'',
  ].filter(Boolean);
  $('#modalDetails').innerHTML=details.map(value=>`<span>${esc(value)}</span>`).join('');
  updateInferenceButtons();
  renderCropPanel();
}
async function loadResponse(id){
  $('#rawResponse').textContent='';
  try{
    const result=await api(`/api/response/${id}`);
    if(active?.id===id&&result.response){
      $('#rawResponse').textContent=JSON.stringify(result.response,null,2).slice(0,6000);
    }
  }catch{
    if(active?.id===id)$('#rawResponse').textContent='분석 응답을 불러오지 못했습니다.';
  }
}
async function openModal(id){
  const row=items.find(item=>item.id===id)||queueItems.find(item=>item.id===id);
  if(!row)return;
  returnFocus=document.activeElement;
  active=row;
  activeDraft=null;
  renderDraft(row.review_classification);
  $('#draftNoteBtn').disabled=draftBusy||!health?.agentstream_configured;
  $('#draftNoteBtn').textContent=draftBusy?'AgentStream이 메모를 분류하고 있습니다…':(health?.agentstream_configured?'AgentStream으로 메모 정리':'AgentStream 연결 설정 대기');
  $('#modalTitle').textContent=row.filename;
  $('#modalImage').src=row.image_url;
  $('#reviewNote').value=row.review_note||'';
  renderDetails(row);
  $('#modalBackdrop').classList.remove('hidden');
  $('.app-shell').inert=true;
  document.body.style.overflow='hidden';
  $('.modal').scrollTop=0;
  $('.modal').focus();
  await Promise.all([loadResponse(id),loadCrops(row)]);
}
function closeModal(){
  active=null;
  $('#modalBackdrop').classList.add('hidden');
  $('.app-shell').inert=false;
  document.body.style.overflow='';
  if(returnFocus?.isConnected)returnFocus.focus();
  else $('#uploadBtn').focus();
}
async function inferItem(){
  if(!active||inferenceBusy||!health?.aistream_configured)return;
  const id=active.id;
  inferenceBusy=true;
  updateInferenceButtons();
  try{
    const result=await api(`/api/infer/${id}`,{method:'POST'});
    toast(`AI 분석 완료 · ${tag(result.pred)}`);
  }catch(error){
    toast(error.message,true);
  }finally{
    await refresh();
    inferenceBusy=false;
    if(active?.id===id){
      active=items.find(item=>item.id===id)||active;
      renderDetails(active);
      await loadResponse(id);
    }
    updateInferenceButtons();
    if(queueVisible)await generateQueue();
  }
}
async function review(status){
  if(!active)return;
  const id=active.id;
  const buttons=$$('[data-review]');
  buttons.forEach(button=>button.disabled=true);
  try{
    await api('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-WR-Review':'1'},
      body:JSON.stringify({id,status,note:$('#reviewNote').value,draft_id:activeDraft})});
    if(active?.id===id)closeModal();
    await refresh();
    if(queueVisible)await generateQueue();
    toast(status==='completed'?'검토 기록 저장 완료':(status==='hold'?'보류 상태 저장 완료':'검토 대기로 되돌렸습니다.'));
  }catch(error){
    toast(error.message,true);
  }finally{
    buttons.forEach(button=>button.disabled=false);
  }
}
function renderDraft(result){
 const node=$('#draftNoteResult');node.replaceChildren();if(!result)return;
 const title=document.createElement('p');title.textContent='AgentStream 분류 · 원문 인용';node.append(title);
 result.segments.forEach(s=>{const row=document.createElement('p');const label=document.createElement('strong');label.textContent=s.labels.join(' · ')+': ';row.append(label,document.createTextNode(s.text));node.append(row)});
 const notice=document.createElement('p');notice.className='agent-notice';notice.textContent=result.notice;node.append(notice);
}
async function draftNote(){
 if(!active||draftBusy)return;
 const itemId=active.id,note=$('#reviewNote').value;if(!note.trim()){toast('검토 메모를 먼저 입력하세요.',true);return}
 draftBusy=true;const button=$('#draftNoteBtn');button.disabled=true;button.textContent='AgentStream이 메모를 분류하고 있습니다…';
 try{
  await api('/api/review-session');
  const result=await api('/api/review-draft',{method:'POST',headers:{'Content-Type':'application/json','X-WR-Review':'1'},body:JSON.stringify({id:itemId,note})});
  if(active?.id!==itemId||$('#reviewNote').value!==note){toast('응답 중 메모가 변경되어 분류를 적용하지 않았습니다.',true);return}
  activeDraft=result.draft_id;renderDraft(result);toast('메모 분류 완료. 확인 후 검토 결과를 저장하세요.');
 }catch(e){toast(e.message,true)}finally{draftBusy=false;button.disabled=!health?.agentstream_configured;button.textContent=health?.agentstream_configured?'AgentStream으로 메모 정리':'AgentStream 연결 설정 대기'}
}
function setup(){
 $('#itemSearch').addEventListener('input',renderItems);
 $('#itemFilter').addEventListener('change',renderItems);
 $('#inferItemBtn').addEventListener('click',inferItem);
 $('#cropBtn').addEventListener('click',analyzeCrops);
 $('#draftNoteBtn').addEventListener('click',draftNote);
 $('#reviewNote').addEventListener('input',()=>{activeDraft=null;renderDraft(null)});
 $$('#uploadBtn,#refreshBtn,#queueBtn,#inferBtn,#trainBtn,#evaluateBtn,#revealBtn,#exportBtn').forEach(x=>{x.addEventListener('click',()=>{
 const f={uploadBtn:()=>$('#imageInput').click(),refreshBtn:refresh,queueBtn:generateQueue,inferBtn:inferPending,trainBtn:train,evaluateBtn:evaluate,revealBtn:reveal,exportBtn:()=>location.assign('api/export')};return f[x.id]?.()})});
 $('#imageInput').addEventListener('change',e=>{upload(e.target.files[0]);e.target.value=''});
 $('#modalClose').addEventListener('click',closeModal);
 $('#modalBackdrop').addEventListener('click',event=>{if(event.target.id==='modalBackdrop')closeModal()});
 document.addEventListener('keydown',event=>{if(event.key==='Escape'&&active)closeModal()});
 $$('[data-review]').forEach(x=>x.addEventListener('click',()=>review(x.dataset.review)));
 $$('.nav-link').forEach(x=>x.addEventListener('click',()=>{$$('.nav-link').forEach(b=>b.classList.remove('active'));x.classList.add('active');$('#'+x.dataset.target).scrollIntoView({behavior:'smooth',block:'start'})}));
 refresh();
}
document.addEventListener('DOMContentLoaded',setup);
