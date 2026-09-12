import {probability,displayForecast} from './lib.mjs';
import {t,getLocale,formatNumber,intlLocale} from './i18n.mjs';
import {CARD_FONT,CARD_MATERIALS,cardFont,cardMaterial,cardRule,cardBrand} from './card-art.mjs';

const FONT=CARD_FONT;
const COLORS=CARD_MATERIALS.paper;
const GROUP_LABELS = ['card.crowd','card.top','card.ai'];

/** Word-aware wrapping also splits long Korean/URL runs without dropping characters. */
export function wrapCardText(text, measure, maxWidth) {
  if (!(maxWidth > 0)) throw new RangeError('A positive text width is required');
  const segments = typeof Intl.Segmenter === 'function'
    ? value => Array.from(new Intl.Segmenter('en',{granularity:'grapheme'}).segment(value), item => item.segment)
    : value => Array.from(value);
  const lines = [];
  for (const paragraph of String(text ?? '').split(/\r?\n/)) {
    let line = '';
    for (const token of paragraph.match(/\S+|\s+/gu) || []) {
      if (/^\s+$/u.test(token)) { if (line && !line.endsWith(' ')) line += ' '; continue; }
      if (measure(line + token) <= maxWidth) { line += token; continue; }
      if (measure(token) <= maxWidth) {
        if (line.trimEnd()) lines.push(line.trimEnd());
        line = token;
        continue;
      }
      for (const grapheme of segments(token)) {
        if (line && measure(line + grapheme) > maxWidth) { lines.push(line.trimEnd()); line = ''; }
        line += grapheme;
      }
    }
    if (line.trimEnd() || !paragraph) lines.push(line.trimEnd());
  }
  return lines.length ? lines : [''];
}

export function shareCardData(detail, origin, at = Date.now(), locale = getLocale()) {
  const forecast = displayForecast(detail.forecast,detail.displayTranslation);
  const base = new URL(origin);
  if (!['https:','http:'].includes(base.protocol)) throw new TypeError('Invalid share origin');
  const personal = detail.myForecast;
  return {
    id: String(forecast.id),
    question: String(forecast.question || forecast.title || ''),
    url: new URL(`/forecasts/${encodeURIComponent(forecast.id)}`,base.origin).href,
    category: forecast.category,
    at,
    locale,
    groups: [
      {label:t('card.crowd',{},locale),value:probability(forecast.crowd?.probability)},
      {label:t('card.top',{},locale),value:probability(forecast.top?.probability)},
      {label:t('card.ai',{},locale),value:probability(forecast.ai?.probability)},
    ],
    personal: personal && ['YES','NO'].includes(personal.outcome) && probability(personal.confidence) !== null
      ? {outcome:personal.outcome,confidence:personal.confidence} : null,
  };
}

export function shareProbabilityLabel(value,locale=getLocale()) {
  const valid = probability(value);
  return valid === null ? t('card.noData',{},locale) : `${formatNumber(Math.round(valid),{},locale)}%`;
}

export async function shareWithPlatform(share,payload,onSuccess) {
  try {
    await share(payload);
    onSuccess();
    return true;
  } catch(error) {
    if(error.name==='AbortError')return false;
    throw error;
  }
}

function textLines(ctx,lines,x,y,lineHeight) {
  lines.forEach((line,index) => ctx.fillText(line,x,y+index*lineHeight));
}

function fittedText(ctx,value,x,y,maxWidth,{size,minSize=size,weight=500}={}) {
  ctx.font=`${weight} ${size}px ${FONT}`;
  while(size>minSize&&ctx.measureText(value).width>maxWidth){size-=1;ctx.font=`${weight} ${size}px ${FONT}`;}
  ctx.fillText(value,x,y);
}

/** Same material family as the identity folio; the actual question takes the lead. */
export function renderShareCard(canvas,data,categoryName='',{locale=data.locale??getLocale()}={}) {
  const tr=(key,params={})=>t(key,params,locale);
  const ctx=canvas.getContext('2d');if(!ctx)throw new Error(tr('card.imageUnsupported'));
  const width=1080,inset=64,contentWidth=952;
  let titleSize=60;cardFont(ctx,titleSize,650);
  let lines=wrapCardText(data.question,text=>ctx.measureText(text).width,contentWidth);
  if(lines.length>7){titleSize=46;cardFont(ctx,titleSize,650);lines=wrapCardText(data.question,text=>ctx.measureText(text).width,contentWidth);}
  const lineHeight=Math.round(titleSize*1.27);
  const questionBottom=235+(lines.length-1)*lineHeight;
  const decisionY=questionBottom+96;
  const groupsY=decisionY+(data.personal?212:135);
  cardFont(ctx,20,500);
  const urlLines=wrapCardText(data.url,text=>ctx.measureText(text).width,contentWidth);
  const height=Math.max(1280,groupsY+350+urlLines.length*30);
  canvas.width=width;canvas.height=height;ctx.textBaseline='alphabetic';ctx.textAlign='left';
  cardMaterial(ctx,width,height,COLORS);cardBrand(ctx,inset,89,COLORS,34);
  cardFont(ctx,titleSize,650);ctx.fillStyle=COLORS.ink;textLines(ctx,lines,inset,235,lineHeight);
  ctx.fillStyle=COLORS.accent;fittedText(ctx,categoryName||tr('card.forecast'),inset,decisionY-38,contentWidth,{size:23,minSize:18,weight:650});
  cardRule(ctx,inset,decisionY-14,contentWidth,COLORS.line);
  if(data.personal){
    ctx.fillStyle=COLORS.ink;fittedText(ctx,tr('card.personal',{outcome:tr(`card.outcome.${data.personal.outcome}`),confidence:formatNumber(data.personal.confidence,{},locale)}),inset,decisionY+74,contentWidth,{size:49,minSize:32,weight:650});
    ctx.fillStyle=COLORS.muted;fittedText(ctx,tr('card.myForecast'),inset,decisionY+120,contentWidth,{size:24,weight:500});
  }else{
    ctx.fillStyle=COLORS.ink;fittedText(ctx,tr('card.prompt'),inset,decisionY+68,contentWidth,{size:44,minSize:32,weight:650});
  }
  ctx.fillStyle=COLORS.muted;fittedText(ctx,tr('card.yesProbability'),inset,groupsY,contentWidth,{size:22,minSize:18});
  const columnWidth=(contentWidth-56)/3;
  data.groups.forEach((group,index)=>{
    const x=inset+index*(columnWidth+28);
    ctx.fillStyle=COLORS.ink;fittedText(ctx,shareProbabilityLabel(group.value,locale),x,groupsY+75,columnWidth,{size:group.value===null?27:54,minSize:22,weight:650});
    ctx.fillStyle=COLORS.muted;fittedText(ctx,GROUP_LABELS[index]?tr(GROUP_LABELS[index]):group.label,x,groupsY+116,columnWidth,{size:22,minSize:18});
    cardRule(ctx,x,groupsY+143,columnWidth,COLORS.line);
  });
  const footerY=height-146-urlLines.length*30;
  ctx.fillStyle=COLORS.muted;fittedText(ctx,tr('card.principles'),inset,footerY,contentWidth,{size:19,minSize:15,weight:500});
  const timestamp=new Intl.DateTimeFormat(intlLocale(locale),{timeZone:'UTC',year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(data.at));
  cardRule(ctx,inset,footerY+30,contentWidth,COLORS.line);
  ctx.fillStyle=COLORS.ink;fittedText(ctx,tr('card.asOf',{date:timestamp}),inset,footerY+71,contentWidth,{size:20,minSize:16,weight:500});
  cardFont(ctx,20,500);ctx.fillStyle=COLORS.muted;textLines(ctx,urlLines,inset,footerY+109,30);
  return canvas;
}

export function canvasPng(canvas,{locale=getLocale()}={}) {
  return new Promise((resolve,reject) => {
    canvas.toBlob(blob=>blob?resolve(blob):reject(new Error(t('card.imageFailed',{},locale))),'image/png');
  });
}
