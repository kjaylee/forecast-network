import {wrapCardText,canvasPng} from './share-card.mjs';
import {t,getLocale,formatNumber,intlLocale} from './i18n.mjs';
import {CARD_MATERIALS,cardFont,cardMaterial,cardRule,cardBrand} from './card-art.mjs';

export {canvasPng};
export const PROFILE_CARD_FORMATS=Object.freeze({
  landscape:Object.freeze({width:1200,height:675}),
  portrait:Object.freeze({width:1080,height:1350}),
});

const THEMES=CARD_MATERIALS;
const HASH=/^[a-f0-9]{64}$/i;

function invalid(field){throw new TypeError(`Invalid profile card snapshot: ${field}`);}
function record(value,field){if(!value || typeof value!=='object' || Array.isArray(value))invalid(field);return value;}
function count(value,field){if(!Number.isSafeInteger(value)||value<0)invalid(field);return value;}
function timestamp(value,field){if(!Number.isSafeInteger(value)||value<0||value>8640000000000000)invalid(field);return value;}
function text(value,field,max=2000){if(typeof value!=='string'||!value.trim()||value.length>max||/[\u0000-\u001f\u007f]/.test(value))invalid(field);return value.trim();}
function score(value,field,max=1){if(value===null||value===undefined)return null;if(typeof value!=='number'||!Number.isFinite(value)||value<0||value>max)invalid(field);return value;}
function hash(value,field){if(typeof value!=='string'||!HASH.test(value))invalid(field);return value;}
function choice(value,field){if(value!=='YES'&&value!=='NO')invalid(field);return value;}
function deepFreeze(value){if(value && typeof value==='object'){Object.values(value).forEach(deepFreeze);Object.freeze(value);}return value;}

export function profileSampleStatus(resolvedForecasts){
  count(resolvedForecasts,'resolvedForecasts');
  return resolvedForecasts===0?'new':resolvedForecasts<10?'provisional':'established';
}

/** Whitelist public snapshot fields; never carry account, wallet or secret data. */
export function profileCardData(snapshot,origin){
  record(snapshot,'snapshot');
  if(snapshot.schemaVersion!==1)invalid('schemaVersion');
  const asOf=timestamp(snapshot.asOf,'asOf');
  const user=record(snapshot.user,'user');
  const metrics=record(snapshot.metrics,'metrics');
  const publicUser={id:text(user.id,'user.id',200),displayName:text(user.displayName,'displayName',1000),handle:text(user.handle,'handle',200),createdAt:timestamp(user.createdAt,'createdAt')};
  if(publicUser.id==='.'||publicUser.id==='..')invalid('user.id');
  if(publicUser.createdAt>asOf)invalid('createdAt is after snapshot');
  const publicMetrics={
    totalForecasts:count(metrics.totalForecasts,'totalForecasts'),
    resolvedForecasts:count(metrics.resolvedForecasts,'resolvedForecasts'),
    correctForecasts:count(metrics.correctForecasts,'correctForecasts'),
    invalidForecasts:count(metrics.invalidForecasts,'invalidForecasts'),
    accuracy:score(metrics.accuracy,'accuracy',100),
    brierScore:score(metrics.brierScore,'brierScore'),
    calibrationScore:score(metrics.calibrationScore,'calibrationScore'),
  };
  const {totalForecasts,resolvedForecasts,correctForecasts,invalidForecasts,accuracy}=publicMetrics;
  if(correctForecasts>resolvedForecasts || resolvedForecasts+invalidForecasts>totalForecasts)invalid('count consistency');
  if(!resolvedForecasts && [publicMetrics.accuracy,publicMetrics.brierScore,publicMetrics.calibrationScore].some(value=>value!==null))invalid('unscored profile has scores');
  if(resolvedForecasts && accuracy!==null && Math.abs(accuracy-100*correctForecasts/resolvedForecasts)>.11)invalid('accuracy denominator');
  const sampleStatus=profileSampleStatus(resolvedForecasts);
  if(snapshot.sampleStatus!==sampleStatus)invalid('sampleStatus');
  if(!Array.isArray(snapshot.history)||snapshot.history.length>200)invalid('history');
  const ids=new Set();
  const history=snapshot.history.map(item=>{
    record(item,'history item');
    const forecastId=text(item.forecastId,'history.forecastId',200);
    if(ids.has(forecastId))invalid('duplicate history forecast');
    ids.add(forecastId);
    if(item.correct!==true&&item.correct!==false&&item.correct!==null)invalid('history.correct');
    if(item.resolvedOutcome!==undefined&&item.resolvedOutcome!==null&&!['YES','NO','INVALID'].includes(item.resolvedOutcome))invalid('history.resolvedOutcome');
    if(item.resolvedOutcome==='INVALID'&&item.correct!==null)invalid('invalid outcome must be unscored');
    const finalizedAt=timestamp(item.finalizedAt,'history.finalizedAt');
    if(finalizedAt>asOf)invalid('future history');
    const confidence=score(item.confidence,'history.confidence',100);
    if(confidence===null)invalid('history.confidence');
    return {forecastId,title:text(item.title,'history.title'),outcome:choice(item.outcome,'history.outcome'),correct:item.correct,confidence,finalizedAt,resolvedOutcome:item.resolvedOutcome ?? null};
  }).sort((a,b)=>b.finalizedAt-a.finalizedAt);
  if(history.length>resolvedForecasts+invalidForecasts || history.filter(item=>item.correct===true).length>correctForecasts || history.filter(item=>item.correct===false).length>resolvedForecasts-correctForecasts)invalid('history count consistency');
  if(typeof snapshot.historyTruncated!=='boolean')invalid('historyTruncated');
  let highlight=null;
  if(snapshot.highlight!==null&&snapshot.highlight!==undefined){
    const item=record(snapshot.highlight,'highlight');
    const confidence=score(item.confidence,'highlight.confidence',100);
    const resolvedAt=timestamp(item.resolvedAt,'highlight.resolvedAt');
    if(confidence===null||resolvedAt>asOf||!resolvedForecasts)invalid('highlight consistency');
    highlight={forecastId:text(item.forecastId,'highlight.forecastId',200),title:text(item.title,'highlight.title'),outcome:choice(item.outcome,'highlight.outcome'),confidence,resolvedAt,specificationHash:hash(item.specificationHash,'highlight.specificationHash')};
  }
  if(snapshot.methodology?.version!=='profile-card-v1')invalid('methodology.version');
  const snapshotHash=hash(snapshot.snapshotHash,'snapshotHash');
  let base;
  try{base=new URL(origin);}catch{invalid('origin');}
  if(!['https:','http:'].includes(base.protocol)||base.username||base.password)invalid('origin');
  const profileUrl=new URL(`/creators/${encodeURIComponent(publicUser.id)}`,base.origin).href;
  const url=new URL(profileUrl);url.searchParams.set('record',snapshotHash);
  return deepFreeze({schemaVersion:1,asOf,user:publicUser,metrics:publicMetrics,sampleStatus,history,historyTruncated:snapshot.historyTruncated,highlight,methodology:{version:'profile-card-v1'},snapshotHash,url:url.href,profileUrl,domain:base.host});
}

export function profileAccuracyLabel(value,locale=getLocale()){
  if(value===null||value===undefined)return '—';
  score(value,'accuracy',100);
  if(value>0&&value<.1)return '<0.1%';
  if(value>99.9&&value<100)return '>99.9%';
  return `${formatNumber(value,{maximumFractionDigits:1},locale)}%`;
}
export function profileScoreLabel(value,locale=getLocale()){
  if(value===null||value===undefined)return '—';
  score(value,'score');
  if(value>0&&value<.001)return '<0.001';
  if(value>.999&&value<1)return '>0.999';
  return formatNumber(value,{minimumFractionDigits:3,maximumFractionDigits:3},locale);
}
export function profileCountLabel(value,locale=getLocale()){count(value,'count');return formatNumber(value,value>=1000000?{notation:'compact',maximumFractionDigits:1}:{},locale);}
export function profileHistoryMarks(data,limit=6,locale=getLocale()){
  if(!Number.isInteger(limit)||limit<0||limit>12)throw new RangeError('Invalid outcome strip limit');
  return data.history.slice(0,limit).map(item=>({forecastId:item.forecastId,label:t(item.correct===true?'card.correct':item.correct===false?'card.miss':item.resolvedOutcome==='INVALID'?'card.invalid':'card.unscored',{},locale)}));
}

function graphemes(value){return typeof Intl.Segmenter==='function'?Array.from(new Intl.Segmenter('en',{granularity:'grapheme'}).segment(value),item=>item.segment):Array.from(value);}

/** Bounded text blocks keep complete graphemes and indicate omitted content. */
export function profileTextLines(value,measure,{maxWidth,maxLines=2}={}){
  if(!(maxWidth>0)||!Number.isInteger(maxLines)||maxLines<1)throw new RangeError('Invalid text bounds');
  const source=String(value ?? '').replace(/[ \t]+/gu,' ').trim();
  const lines=wrapCardText(source,measure,maxWidth);
  const clipped=lines.length>maxLines;
  const result=lines.slice(0,maxLines);
  if(clipped){
    const last=graphemes(result.at(-1));
    while(last.length&&measure(`${last.join('')}…`)>maxWidth)last.pop();
    const ending=last.join('').trimEnd().replace(/[,;:.!?…]+$/u,'');
    result[result.length-1]=measure('…')<=maxWidth?`${ending}…`:'';
  }
  return {lines:result,truncated:clipped};
}

function label(ctx,value,x,y,size,color,{weight=500,width=Infinity,align='left',minSize=size}={}){
  cardFont(ctx,size,weight);ctx.fillStyle=color;ctx.textAlign=align;
  while(size>minSize&&ctx.measureText(value).width>width){size--;cardFont(ctx,size,weight);}
  const line=Number.isFinite(width)?profileTextLines(value,text=>ctx.measureText(text).width,{maxWidth:width,maxLines:1}).lines[0]:String(value);
  ctx.fillText(line||'',x,y);ctx.textAlign='left';
}
function block(ctx,value,x,y,{size,width,lines=2,lineHeight=1.18,color,weight=650,minSize=size}={}){
  cardFont(ctx,size,weight);
  while(size>minSize&&wrapCardText(value,text=>ctx.measureText(text).width,width).length>lines){size--;cardFont(ctx,size,weight);}
  const result=profileTextLines(value,text=>ctx.measureText(text).width,{maxWidth:width,maxLines:lines});ctx.fillStyle=color;
  result.lines.forEach((line,index)=>ctx.fillText(line,x,y+index*size*lineHeight));return result;
}
function utcDate(value,withTime=false,locale=getLocale()){return new Intl.DateTimeFormat(intlLocale(locale),{timeZone:'UTC',month:'short',day:'numeric',year:'numeric',...(withTime?{hour:'2-digit',minute:'2-digit',hourCycle:'h23'}:{})}).format(new Date(value));}
function sampleLabel(data,locale){return t(data.sampleStatus==='new'?'card.awaiting':data.sampleStatus==='provisional'?'card.provisional':'card.finalized',{},locale);}
function denominator(data,locale){return t('card.denominator',{correct:profileCountLabel(data.metrics.correctForecasts,locale),scored:profileCountLabel(data.metrics.resolvedForecasts,locale)},locale);}
function exclusion(data,locale){return t('card.exclusion',{invalid:profileCountLabel(data.metrics.invalidForecasts,locale)},locale);}
function forecastsMade(data,locale){return t(data.metrics.totalForecasts===1?'card.forecastsMade.one':'card.forecastsMade.other',{count:profileCountLabel(data.metrics.totalForecasts,locale)},locale);}
function resultMarks(ctx,data,x,y,{width,theme,locale,portrait}){
  const history=data.history.slice(0,portrait?8:6),size=portrait?42:30,gap=portrait?16:13;
  if(!history.length){label(ctx,t('card.noFinalized',{},locale),x,y,portrait?24:19,theme.muted,{width,minSize:16});return;}
  history.forEach((item,index)=>{
    const left=x+index*(size+gap);ctx.fillStyle=item.correct===true?theme.accent:theme.quiet;ctx.fillRect(left,y-size+2,size,size);
    ctx.strokeStyle=item.correct===true?theme.accentInk:theme.ink;ctx.lineWidth=2;ctx.beginPath();
    if(item.correct===true){ctx.moveTo(left+size*.23,y-size*.4);ctx.lineTo(left+size*.43,y-size*.2);ctx.lineTo(left+size*.78,y-size*.64);}
    else if(item.correct===false){ctx.moveTo(left+size*.28,y-size*.66);ctx.lineTo(left+size*.72,y-size*.22);ctx.moveTo(left+size*.72,y-size*.66);ctx.lineTo(left+size*.28,y-size*.22);}
    else{ctx.moveTo(left+size*.26,y-size*.44);ctx.lineTo(left+size*.74,y-size*.44);}ctx.stroke();
  });
  label(ctx,t('card.recentOutcomes',{},locale),x,y+31,portrait?24:18,theme.ink,{width,weight:650});
  label(ctx,t('card.latest',{count:formatNumber(history.length,{},locale)},locale),x,y+57,portrait?20:15,theme.muted,{width});
  if(portrait)label(ctx,t('card.legend',{},locale),x,y+82,20,theme.muted,{width});
}
function scoreReceipt(ctx,data,x,y,{width,theme,locale,portrait}){
  const tr=key=>t(key,{},locale);
  const values=[['card.accuracy',profileAccuracyLabel(data.metrics.accuracy,locale)],['card.brier',profileScoreLabel(data.metrics.brierScore,locale)],['card.calibration',profileScoreLabel(data.metrics.calibrationScore,locale)]];
  if(portrait){
    const column=width/3;
    values.forEach(([key,value],index)=>{label(ctx,tr(key),x+index*column,y,21,theme.muted,{width:column-18,minSize:17});label(ctx,value,x+index*column,y+58,45,theme.ink,{weight:650,width:column-18,minSize:32});});
  }else{
    values.forEach(([key,value],index)=>{const top=y+index*58;label(ctx,tr(key),x,top,18,theme.muted,{width:width*.53,minSize:14});label(ctx,value,x+width,top,31,theme.ink,{align:'right',weight:650,width:width*.45,minSize:24});cardRule(ctx,x,top+17,width,theme.line);});
  }
}
function evidence(ctx,data,x,y,{width,theme,locale,portrait}){
  const tr=(key,params={})=>t(key,params,locale);
  if(data.highlight){
    block(ctx,data.highlight.title,x,y,{size:portrait?48:35,minSize:portrait?42:30,width,lines:portrait?3:2,color:theme.ink});
    const detailY=y+(portrait?187:91);
    label(ctx,tr('card.standout'),x,detailY,portrait?23:18,theme.accent,{weight:650,width});
    label(ctx,tr('card.highlightDetail',{outcome:tr(`card.outcome.${data.highlight.outcome}`),confidence:formatNumber(data.highlight.confidence,{maximumFractionDigits:1},locale),date:utcDate(data.highlight.resolvedAt,false,locale)}),x,detailY+(portrait?36:28),portrait?23:18,theme.muted,{width,minSize:16});
  }else{
    block(ctx,tr(data.sampleStatus==='new'?'card.recordStarts':'card.nextCall'),x,y,{size:portrait?68:45,minSize:portrait?52:37,width,lines:3,lineHeight:1.16,color:theme.ink});
    label(ctx,tr(data.sampleStatus==='new'?'card.noScore':'card.recordAdvice'),x,y+(portrait?195:134),portrait?25:19,theme.muted,{width,minSize:17});
  }
}
/**
 * THESIS: Identity first, the actual prediction second, performance as a receipt.
 * OWN-WORLD: An anodized folio with one folded reflective plane and Sora lettering.
 * STORY: Recognize the forecaster, read their call, inspect the scored denominator.
 * FORM: Independent wide and portrait compositions retain the complete public proof.
 * FINISH: Fixed export sizes; empty/provisional truth; no synthetic badges or scores.
 */
export function renderProfileCard(canvas,data,{format='landscape',theme='paper',locale=getLocale()}={}){
  if(!Object.hasOwn(PROFILE_CARD_FORMATS,format))throw new RangeError('Unknown profile card format');
  if(!Object.hasOwn(THEMES,theme))throw new RangeError('Unknown profile card theme');
  const portrait=format==='portrait',colors=THEMES[theme],dimensions=PROFILE_CARD_FORMATS[format];
  canvas.width=dimensions.width;canvas.height=dimensions.height;
  const ctx=canvas.getContext('2d');if(!ctx)throw new Error(t('card.profileUnsupported',{},locale));
  cardMaterial(ctx,canvas.width,canvas.height,colors);ctx.textBaseline='alphabetic';ctx.textAlign='left';
  const x=portrait?64:56,width=canvas.width-2*x;
  cardBrand(ctx,x,portrait?87:65,colors,portrait?32:28);
  block(ctx,data.user.displayName,x,portrait?245:162,{size:portrait?100:79,minSize:portrait?70:52,width:portrait?width:820,lines:2,lineHeight:1.08,color:colors.ink,weight:750});
  label(ctx,forecastsMade(data,locale),x,portrait?373:258,portrait?25:20,colors.muted,{width:portrait?width:780});
  cardRule(ctx,x,portrait?412:287,width,colors.line);
  evidence(ctx,data,x,portrait?491:340,{width:portrait?width:692,theme:colors,locale,portrait});
  if(portrait){
    if(data.sampleStatus!=='new'){
      scoreReceipt(ctx,data,x,805,{width,theme:colors,locale,portrait});
      label(ctx,denominator(data,locale),x,917,25,colors.ink,{width,weight:650});
    }
    label(ctx,sampleLabel(data,locale),x,data.sampleStatus==='new'?795:963,23,colors.accent,{width,minSize:20});
    resultMarks(ctx,data,x,1061,{width,theme:colors,locale,portrait});
    label(ctx,exclusion(data,locale),x,1187,19,colors.muted,{width,minSize:16});
    cardRule(ctx,x,1217,width,colors.line);
    label(ctx,data.domain,x,1260,24,colors.ink,{width,weight:650});
    label(ctx,t('card.asOf',{date:utcDate(data.asOf,true,locale)},locale),x,1298,17,colors.muted,{width:660,minSize:15});
    label(ctx,t('card.recordHash',{hash:data.snapshotHash.slice(0,12)},locale),canvas.width-x,1298,16,colors.muted,{align:'right',width:290,minSize:14});
  }else{
    if(data.sampleStatus!=='new'){
      scoreReceipt(ctx,data,830,340,{width:314,theme:colors,locale,portrait});
      label(ctx,denominator(data,locale),830,512,18,colors.ink,{width:314,minSize:15,weight:650});
    }
    label(ctx,sampleLabel(data,locale),830,551,16,colors.accent,{width:314,minSize:13});
    resultMarks(ctx,data,x,514,{width:692,theme:colors,locale,portrait});
    label(ctx,exclusion(data,locale),x,609,15,colors.muted,{width,minSize:13});
    cardRule(ctx,x,628,width,colors.line);
    label(ctx,data.domain,x,657,18,colors.ink,{width:380,weight:650});
    label(ctx,t('card.recordHash',{hash:data.snapshotHash.slice(0,12)},locale),460,657,14,colors.muted,{width:270,minSize:12});
    label(ctx,t('card.asOf',{date:utcDate(data.asOf,true,locale)},locale),1144,657,14,colors.muted,{align:'right',width:408,minSize:12});
  }
  return canvas;
}
