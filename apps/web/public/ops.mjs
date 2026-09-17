const DAY_MS=86400000;
const safe=value=>Number.isSafeInteger(value)&&value>=0;
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const errors={locked:'먼저 관리자 토큰을 입력해 주세요.',token:'관리자 토큰의 길이와 형식을 확인해 주세요.',window:'완료된 UTC 날짜 범위를 확인해 주세요. 각 기간은 1~366일이어야 합니다.',forbidden:'관리자 인증에 실패했습니다. 토큰을 다시 입력해 주세요.',network:'연결하지 못했습니다. 네트워크를 확인하고 다시 조회해 주세요.',server:'서버가 조회를 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.',response:'응답 형식이 맞지 않습니다. 서버와 운영 화면의 버전을 확인해 주세요.',timeout:'조회 시간이 초과되었습니다. 다시 시도해 주세요.'};

export function formatCount(value){return safe(value)?value.toLocaleString('ko-KR'):'자료 없음';}
export function formatFixed(value,places,{trim=false}={}){
  if(!safe(value)||!Number.isInteger(places)||places<0||places>13)return '자료 없음';
  const divisor=10n**BigInt(places),integer=BigInt(value),whole=(integer/divisor).toLocaleString('ko-KR');
  let fraction=(integer%divisor).toString().padStart(places,'0');
  if(trim)fraction=fraction.replace(/0+$/,'');
  return places&&fraction?whole+'.'+fraction:whole;
}
export function ratioPresentation(ratio){
  if(!object(ratio))return {value:'자료 없음',detail:'집계 자료 없음',status:'unknown'};
  const status=ratio.status;
  const detail=`분자 ${formatCount(ratio.numerator)} / 분모 ${formatCount(ratio.denominator)}`;
  if(status==='out_of_range')return {value:'표시 범위 초과',detail,status};
  if(status==='immature')return {value:'아직 미성숙',detail,status};
  if(status==='no_denominator')return {value:'분모 없음',detail,status};
  if(status!=='available'||ratio.scale!==10000)return {value:'자료 없음',detail,status:'unknown'};
  if(ratio.unit==='share')return {value:safe(ratio.valueBp)?formatFixed(ratio.valueBp,2)+'%':'자료 없음',detail,status};
  if(!safe(ratio.valueScaled))return {value:'자료 없음',detail,status:'unknown'};
  const units={days_per_active_user_week:[4,'일 / 활성 사용자·주'],USD_MICRO_per_predictor:[10,'USD / 활성 예측자'],USD_MICRO_per_question:[10,'USD / 활성 질문'],DEVNET_LAMPORT_per_predictor:[13,'Devnet SOL / 활성 예측자'],DEVNET_LAMPORT_per_question:[13,'Devnet SOL / 활성 질문']};
  const unit=units[ratio.unit];
  return unit?{value:formatFixed(ratio.valueScaled,unit[0],{trim:true})+' '+unit[1],detail,status}:{value:'단위 확인 필요',detail,status:'unknown'};
}
export function utcDate(ms){return safe(ms)&&ms<=8640000000000000?new Date(ms).toISOString().slice(0,10):'자료 없음';}
export function knownCostLabel(cost){
  if(!object(cost))return '자료 없음';
  if(cost.knownOperations===0)return '확인된 영수증 없음';
  if(!safe(cost.knownOperations))return '자료 없음';
  if(cost.unit==='USD_MICRO')return formatFixed(cost.knownRecordedSubtotalAtomic,6,{trim:true})+' USD';
  if(cost.unit==='DEVNET_LAMPORT')return formatFixed(cost.knownRecordedSubtotalAtomic,9,{trim:true})+' Devnet SOL';
  return '단위 확인 필요';
}
function dateMs(value){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))throw new Error('window');
  const ms=Date.parse(value+'T00:00:00.000Z');
  if(!safe(ms)||utcDate(ms)!==value)throw new Error('window');
  return ms;
}
export function analyticsQuery(dates,nowMs){
  if(!object(dates)||!safe(nowMs))throw new Error('window');
  const params={start:dateMs(dates.start),end:dateMs(dates.end),cohortStart:dateMs(dates.cohortStart),cohortEnd:dateMs(dates.cohortEnd)};
  const latest=Math.floor(nowMs/DAY_MS)*DAY_MS;
  for(const [start,end] of [[params.start,params.end],[params.cohortStart,params.cohortEnd]]){
    if(start>=end||end>latest||end-start>366*DAY_MS)throw new Error('window');
  }
  return '/api/admin/analytics?'+new URLSearchParams(Object.entries(params).map(([key,value])=>[key,String(value)]));
}
export function validateReport(payload,url){
  const report=payload?.data;
  if(!object(report)||report.formulaVersion!=='product-analytics-v1'||!safe(report.asOfMs)
    ||!object(report.window)||report.window.timezone!=='UTC'||report.window.complete!==true
    ||!object(report.population)||!['application','fixture','isolated-load'].includes(report.population.kind)
    ||!object(report.activity)||!Array.isArray(report.activity.daily)||report.activity.daily.length>366
    ||!Array.isArray(report.cohorts)||report.cohorts.length>366||!object(report.retention)
    ||!object(report.quality)||!object(report.disputes)||!object(report.costs)
    ||report.costs.provider?.unit!=='USD_MICRO'||report.costs.chain?.unit!=='DEVNET_LAMPORT'
    ||typeof report.inputHash!=='string'||!/^[a-f0-9]{64}$/.test(report.inputHash))throw new Error('response');
  const query=new URL(url,'https://local.invalid').searchParams;
  if(report.window.startMs!==Number(query.get('start'))||report.window.endMs!==Number(query.get('end'))
    ||report.cohortWindow?.startMs!==Number(query.get('cohortStart'))||report.cohortWindow?.endMs!==Number(query.get('cohortEnd')))throw new Error('response');
  return report;
}

export function createAnalyticsSession({fetchImpl=globalThis.fetch,now=()=>Date.now(),onChange=()=>{},timeoutMs=20000}={}){
  let token='',report=null,controller=null,epoch=0,phase='locked';
  const emit=(code=null)=>onChange({phase,hasCredential:token.length>0,report,error:code?errors[code]||errors.response:null});
  function clear(){epoch++;controller?.abort();controller=null;token='';report=null;phase='locked';emit();}
  async function refresh(dates){
    controller?.abort();controller=null;const requestEpoch=++epoch;report=null;
    if(!token){phase='locked';emit('locked');return false;}
    let url;
    try{url=analyticsQuery(dates,now());}catch{report=null;phase='error';emit('window');return false;}
    const requestController=new AbortController();controller=requestController;const signal=requestController.signal;
    report=null;phase='loading';emit();let timedOut=false;
    const timer=setTimeout(()=>{timedOut=true;requestController.abort();},timeoutMs);
    try{
      const response=await fetchImpl(url,{method:'GET',headers:{Authorization:'Bearer '+token},mode:'same-origin',credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal});
      if(requestEpoch!==epoch)return false;
      if(timedOut)throw new Error('timeout');
      if(response.status===401||response.status===403){clear();emit('forbidden');return false;}
      if(!response.ok)throw new Error('server');
      const text=await response.text();if(requestEpoch!==epoch)return false;if(timedOut)throw new Error('timeout');
      if(text.length>2_000_000)throw new Error('response');
      let payload;try{payload=JSON.parse(text);}catch{throw new Error('response');}
      report=validateReport(payload,url);phase='ready';emit();return true;
    }catch(error){
      if(requestEpoch!==epoch)return false;
      report=null;phase='error';const code=timedOut?'timeout':error?.message==='server'?'server':error?.message==='response'?'response':'network';emit(code);return false;
    }finally{clearTimeout(timer);if(requestEpoch===epoch)controller=null;}
  }
  async function connect(value,dates){
    clear();
    if(typeof value!=='string'||value.length<32||value.length>2048||!/^[\x21-\x7e]+$/.test(value)){emit('token');return false;}
    token=value;return refresh(dates);
  }
  return Object.freeze({connect,refresh,clear,hasCredential:()=>token.length>0});
}

function node(tag,text,attrs={}){const element=document.createElement(tag);if(text!=null)element.textContent=text;for(const [key,value]of Object.entries(attrs))element.setAttribute(key,value);return element;}
function ratioNode(metric){const value=ratioPresentation(metric),wrap=node('div');wrap.append(node('span',value.value,{class:'ratio-main'}),node('small',value.detail,{class:'ratio-evidence'}));return wrap;}
function table(headers,rows,label){
  const region=node('div',null,{class:'table-region',tabindex:'0',role:'region','aria-label':label}),table=node('table'),head=node('thead'),tr=node('tr'),body=node('tbody');
  table.append(node('caption',label));headers.forEach(label=>tr.append(node('th',label,{scope:'col'})));head.append(tr);table.append(head,body);
  rows.forEach(cells=>{const row=node('tr');cells.forEach((value,index)=>{const cell=node(index===0?'th':'td',typeof value==='string'?value:null,index===0?{scope:'row'}:{});if(value instanceof Node)cell.append(value);row.append(cell);});body.append(row);});region.append(table);return region;
}
function metricList(entries){const list=node('dl',null,{class:'metric-list'});entries.forEach(([label,value])=>{const dd=node('dd',typeof value==='string'?value:null);if(value instanceof Node)dd.append(value);list.append(node('dt',label),dd);});return list;}
function section(id,title,description){const result=node('section',null,{id,class:'metric-section'});result.append(node('h2',title),node('p',description,{class:'section-description'}));return result;}
function detail(title,...children){const d=node('details',null,{class:'detail-block'});d.append(node('summary',title),...children);return d;}
function statusLabel(status){return {available:'집계 가능',no_denominator:'분모 없음',immature:'목표일 미완료',out_of_range:'정확한 표시 범위 초과'}[status]||'자료 없음';}

function renderReport(report,root){
  root.replaceChildren();
  const activity=section('activity','활동과 전환','활성 질문은 기간 안에 적격 예측이 있었던 질문입니다. 공개 질문 재고와는 별도로 집계합니다.');
  const columns=node('div',null,{class:'two-columns'});
  columns.append(metricList([['활성 예측자',formatCount(report.activity.activePredictors)+'명'],['활성 질문',formatCount(report.activity.activeQuestions)+'개'],['예측자·질문·UTC 날짜 조합',formatCount(report.activity.distinctPredictorQuestionDays)+'건'],['조회 시점 열린 질문',formatCount(report.activity.openQuestionsAtAsOf)+'개']]));
  columns.append(table(['전환 단계','비율·분모','미성숙'],[
    ['가입 → 7일 안에 첫 예측',ratioNode(report.activationWithin7Days),formatCount(report.activationWithin7Days?.immatureAccounts)+'명'],
    ['질문 발행 → 7일 안에 외부 참여',ratioNode(report.creationToExternalParticipationWithin7Days),formatCount(report.creationToExternalParticipationWithin7Days?.immatureQuestions)+'개']
  ],'7일 관찰이 끝난 대상만 전환율 분모에 포함'));
  activity.append(columns,node('h3','주간 활동 빈도',{class:'subheading'}),ratioNode(report.weeklyActiveDaysPerActiveUserWeek),node('p','완료된 월요일~월요일 UTC 주의 활성 날짜 수 / 활성 사용자·주 수입니다.',{class:'section-description'}));
  activity.append(detail('UTC 날짜별 활동',table(['UTC 날짜','활성 예측자','활성 질문','예측자·질문·일'],report.activity.daily.map(day=>[utcDate(day.dayStartMs),formatCount(day.activePredictors),formatCount(day.activeQuestions),formatCount(day.distinctPredictorQuestionDays)]),'완료된 날짜별 원집계')));root.append(activity);

  const retention=section('retention','D1 · D7 · D30 유지율','처음 적격 예측을 제출한 UTC 날짜가 코호트의 시작입니다. 목표 날짜 전체가 끝나지 않은 사용자는 실패가 아니라 미성숙으로 남습니다.');
  retention.append(table(['목표일','유지율·성숙 분모','미성숙 사용자','상태'],['D1','D7','D30'].map(name=>[name,ratioNode(report.retention[name]),formatCount(report.retention[name]?.immatureUsers)+'명',statusLabel(report.retention[name]?.status)]),'성숙 코호트만 합산한 유지율'));
  retention.append(detail('코호트 날짜별 분자·분모',table(['첫 예측 UTC 날짜','활성화 사용자','D1','D7','D30'],report.cohorts.map(cohort=>[utcDate(cohort.cohortStartMs),formatCount(cohort.activatedUsers)+'명',...['D1','D7','D30'].map(name=>ratioNode(cohort.retention?.[name]))]),'미성숙 값은 0%로 대체하지 않음')));root.append(retention);

  const quality=section('quality','질문 품질과 분쟁','확정 이벤트와 연결된 결과·분쟁 기록을 집계합니다. 작성자 계정이 제외되어도 실제 공개 질문과 다른 사용자의 참여는 유지됩니다.');
  quality.append(table(['지표','값·분자·분모'],[
    ['확정된 질문',formatCount(report.quality.finalizedQuestions)+'개'],['확정 결과가 미확인인 질문',formatCount(report.quality.unavailableFinalizedOutcomes)+'개'],
    ['질문 무효율',ratioNode(report.quality.invalidity)],['질문 유효율',ratioNode(report.quality.questionValidity)],
    ['발행 질문 명확성',ratioNode(report.quality.publishedQuestionClarity)],['결과가 있는 일반 작성자',formatCount(report.quality.creatorsWithFinalizedResults)+'명'],
    ['제출된 분쟁',formatCount(report.disputes.submitted)+'건'],['검토된 분쟁',formatCount(report.disputes.reviewed)+'건'],
    ['검토 분쟁 중 실질적 충돌',ratioNode(report.disputes.materialConflictAmongReviewed)],['확정 질문 중 분쟁이 있었던 비율',ratioNode(report.disputes.disputedAmongFinalizedQuestions)]
  ],'자료가 없는 품질 지표는 미확인으로 유지'));root.append(quality);

  const costs=section('costs','확인된 운영비와 미확인 비용','공급자 USD와 Devnet SOL은 합산하거나 환산하지 않습니다. 확인된 영수증 소계가 전체 실제 비용을 뜻하지는 않습니다.');
  const costColumns=node('div',null,{class:'two-columns'});
  for(const [kind,label,places,currency]of [['provider','공급자 비용',6,'USD'],['chain','Devnet 체인 비용',9,'Devnet SOL']]){
    const cost=report.costs[kind],column=node('div',null,{class:'cost-column'}),name=node('div',null,{class:'cost-name'});name.append(node('h3',label),node('span','부분 집계'));column.append(name,node('p',knownCostLabel(cost),{class:'cost-amount'}),node('p','확인된 실제 영수증만 합산한 소계. 비용이 0이라는 뜻은 아닙니다.',{class:'cost-caption'}));
    column.append(metricList([['확인 소계 · 원단위',formatCount(cost.knownRecordedSubtotalAtomic)+' '+(kind==='provider'?'마이크로달러':'lamports')],['확인된 작업',formatCount(cost.knownOperations)+'건'],['금액 미확인 작업',formatCount(cost.unknownRecordedOperations)+'건'],['추정값만 있는 작업',formatCount(cost.estimatedOperations)+'건'],['추정 소계 · 실제 비용 아님',formatFixed(cost.estimatedRecordedSubtotalAtomic,places,{trim:true})+' '+currency],['활성 예측자당 확인 소계',ratioNode(cost.knownSubtotalPerActivePredictor)],['활성 질문당 확인 소계',ratioNode(cost.knownSubtotalPerActiveQuestion)]]));
    const complete=node('p',null,{class:'complete-cost'});complete.append(node('span','전체 실제 비용'),node('strong',cost.completeActualTotalAtomic===null?'미확인':formatFixed(cost.completeActualTotalAtomic,places,{trim:true})+' '+currency));column.append(complete);
    if(kind==='chain')column.append(metricList([['관측된 거래 서명',formatCount(cost.observedDeliverySignatures)+'건'],['실제 수수료가 없는 서명',formatCount(cost.deliverySignaturesWithoutActualFee)+'건'],['예산 예약액 · 실제 비용 아님',formatCount(cost.reservedLamportsAllPopulations)+' lamports']]));
    else column.append(metricList([['기간 내 보존된 AI 예측',formatCount(cost.retainedAiForecastRecordsInWindow)+'건'],['전체 과거 공급자 호출 수',cost.historicalProviderOperationCount===null?'미확인':formatCount(cost.historicalProviderOperationCount)+'건']]));
    costColumns.append(column);
  }
  costs.append(costColumns);root.append(costs);
  const method=section('method','집계 근거','백엔드가 반환한 값과 원분자·분모를 그대로 표시합니다. 이 화면에서 유지율이나 비용을 다시 산출하지 않습니다.');
  const descriptions=[['공식 버전',report.formulaVersion],['관측 기준 시각',new Date(report.asOfMs).toISOString()],['집단 종류',report.population.kind],['자료 성격',report.population.evidenceClass],['중복 정리 후 계정',formatCount(report.population.deduplicatedIdentities)+'개'],['제외된 계정',formatCount(report.population.excludedIdentities)+'개'],['분류 미확인 계정',formatCount(report.population.unclassifiedAccounts)+'개'],['사람 고유 신원 검증',report.population.humanIdentityVerified===true?'검증됨':'이 집계로 검증하지 않음'],['입력 스냅샷 해시',report.inputHash]];
  const definition=node('dl',null,{class:'method-list'});descriptions.forEach(([label,value])=>{const dd=node('dd');dd.append(node(label==='입력 스냅샷 해시'?'code':'span',value));definition.append(node('dt',label),dd);});method.append(definition);
  const notes=node('ul',null,{class:'method-copy'});['삭제·계정 병합·적격성 검토에 따라 과거 집계가 다시 정리될 수 있습니다.','운영자와 테스트 계정은 사용자 성과에서 제외합니다. 실제 운영 질문과 운영비의 범위는 별도로 판단합니다.','표시 범위를 초과한 값은 0이나 최댓값으로 대체하지 않습니다. 원분자와 분모로 확인할 수 있습니다.','검증용 데이터의 D30 성과는 실제 사용자의 D30 유지율 증거가 아닙니다.'].forEach(text=>notes.append(node('li',text)));method.append(notes);root.append(method);
}

function mount(){
  const $=id=>document.getElementById(id),today=Math.floor(Date.now()/DAY_MS)*DAY_MS;
  for(const id of ['start-date','cohort-start'])$(id).value=utcDate(Math.max(0,today-30*DAY_MS));
  for(const id of ['end-date','cohort-end']){$(id).value=utcDate(today);$(id).max=utcDate(today);}
  const dates=()=>({start:$('start-date').value,end:$('end-date').value,cohortStart:$('cohort-start').value,cohortEnd:$('cohort-end').value});
  const session=createAnalyticsSession({onChange:state=>{
    $('auth-panel').hidden=state.hasCredential;$('disconnect').disabled=!state.hasCredential;$('refresh').disabled=!state.hasCredential||state.phase==='loading';
    $('loading').hidden=state.phase!=='loading';$('report-shell').hidden=!state.report;$('locked-help').hidden=state.hasCredential;
    $('access-status').textContent={locked:'잠김',loading:'조회 중',ready:'인증됨',error:'조회 오류'}[state.phase];
    $('feedback').textContent=state.error||(state.phase==='loading'?'운영 지표를 조회하고 있습니다.':state.phase==='ready'?'조회가 완료되었습니다.':'');$('feedback').classList.toggle('error',Boolean(state.error));
    if(!state.report){$('report').replaceChildren();$('report-meta').replaceChildren();return;}
    const report=state.report;renderReport(report,$('report'));const meta=$('report-meta');meta.replaceChildren(node('span',`집계 ${utcDate(report.window.startMs)} ~ ${utcDate(report.window.endMs)} · 종료일 미포함`),node('span',`코호트 ${utcDate(report.cohortWindow.startMs)} ~ ${utcDate(report.cohortWindow.endMs)}`),node('strong',report.formulaVersion));
    if(report.population.kind!=='application')meta.append(node('p','검증·부하 테스트 데이터입니다. 실사용자 성과로 해석하지 마세요.',{class:'population-note'}));
  }});
  $('auth-form').addEventListener('submit',event=>{event.preventDefault();const value=$('admin-token').value;$('admin-token').value='';void session.connect(value,dates());});
  $('range-form').addEventListener('submit',event=>{event.preventDefault();void session.refresh(dates());});
  $('disconnect').addEventListener('click',()=>{session.clear();$('admin-token').value='';$('admin-token').focus();});
  window.addEventListener('pagehide',()=>{session.clear();$('admin-token').value='';});
}
if(typeof document!=='undefined'&&document.getElementById('auth-form'))mount();
