import test from 'node:test';
import assert from 'node:assert/strict';
import {cardMessages} from '../public/locales/cards.mjs';
import {t,setLocale,getLocale} from '../public/i18n.mjs';
import {shareCardData,renderShareCard,shareProbabilityLabel,canvasPng} from '../public/share-card.mjs';
import {profileCardData,renderProfileCard,profileHistoryMarks,PROFILE_CARD_FORMATS} from '../public/profile-card.mjs';

const locales=['en','ko','ja','zh-Hant'];
const asOf=Date.parse('2026-09-10T02:30:00Z');
function snapshot({empty=false,provisional=false}={}){
  return {
    schemaVersion:1,asOf,
    user:{id:'public-user',displayName:'山田 張 예측자 👩🏽‍💻',handle:'real_forecaster',createdAt:asOf-86400000},
    metrics:empty?{totalForecasts:0,resolvedForecasts:0,correctForecasts:0,invalidForecasts:0,accuracy:null,brierScore:null,calibrationScore:null}:
      {totalForecasts:20,resolvedForecasts:provisional?8:16,correctForecasts:provisional?6:12,invalidForecasts:2,accuracy:75,brierScore:.182,calibrationScore:.863},
    sampleStatus:empty?'new':provisional?'provisional':'established',
    history:empty?[]:[
      {forecastId:'correct',title:'Canonical question?',outcome:'YES',correct:true,confidence:85,finalizedAt:asOf-1000,resolvedOutcome:'YES'},
      {forecastId:'miss',title:'Canonical question two?',outcome:'NO',correct:false,confidence:60,finalizedAt:asOf-2000,resolvedOutcome:'YES'},
      {forecastId:'invalid',title:'Canonical question three?',outcome:'YES',correct:null,confidence:40,finalizedAt:asOf-3000,resolvedOutcome:'INVALID'},
    ],
    historyTruncated:false,
    highlight:empty?null:{forecastId:'correct',title:'Canonical question?',outcome:'YES',confidence:85,resolvedAt:asOf-1000,specificationHash:'b'.repeat(64)},
    methodology:{version:'profile-card-v1'},snapshotHash:'a'.repeat(64),
  };
}

function recorder({onText}={}){
  const calls=[];
  const canvas={width:0,height:0};
  const segments=new Intl.Segmenter('en',{granularity:'grapheme'});
  const ctx={createLinearGradient:()=>({addColorStop(){}}),save(){},restore(){},clip(){},closePath(){},font:'500 20px sans-serif',textAlign:'left',fillStyle:'',
    measureText(value){
      const size=Number(this.font.match(/([\d.]+)px/)?.[1]||20);
      return {width:[...segments.segment(String(value))].reduce((sum,{segment})=>sum+size*(/[^\u0000-\u007f]/u.test(segment)?1:/\s/u.test(segment)?.3:.57),0)};
    },
    fillText(value,x,y){calls.push({text:String(value),x,y,width:this.measureText(value).width,align:this.textAlign,font:this.font});onText?.(String(value));},
    fillRect(){},beginPath(){},roundRect(){},fill(){},stroke(){},moveTo(){},lineTo(){},
  };
  canvas.getContext=()=>ctx;
  return {canvas,calls};
}
function assertBounded(canvas,calls){
  for(const call of calls){
    const left=call.align==='right'?call.x-call.width:call.x;
    const right=call.align==='right'?call.x:call.x+call.width;
    assert.ok(left>=0&&right<=canvas.width+1&&call.y>0&&call.y<canvas.height,JSON.stringify(call));
  }
}

test('card catalogs cover the same keys and interpolation fields in all four languages',()=>{
  const placeholders=value=>[...value.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)].map(match=>match[1]).sort();
  for(const locale of locales){
    assert.deepEqual(Object.keys(cardMessages[locale]).sort(),Object.keys(cardMessages.en).sort());
    for(const [key,value] of Object.entries(cardMessages[locale])){
      assert.equal(typeof value,'string');assert.ok(value.trim());
      assert.deepEqual(placeholders(value),placeholders(cardMessages.en[key]),`${locale}: ${key}`);
    }
  }
});

test('localized forecast cards preserve question, personal outcome and real zero probabilities',()=>{
  const detail={forecast:{id:'canonical',question:'Will the launch happen before the deadline?',category:'science',crowd:{probability:0},top:{probability:null},ai:{probability:100}},myForecast:{outcome:'NO',confidence:0}};
  const before=structuredClone(detail);
  for(const locale of locales){
    const data=shareCardData(detail,'https://forecast.example',asOf,locale);
    const {canvas,calls}=recorder();renderShareCard(canvas,data);
    const text=calls.map(call=>call.text);
    assert.equal(data.question,detail.forecast.question);assert.deepEqual(data.personal,{outcome:'NO',confidence:0});
    assert.equal(data.locale,locale);assert.deepEqual(data.groups.map(group=>group.value),[0,null,100]);
    for(const key of ['card.crowd','card.top','card.ai','card.noData','card.yesProbability','card.myForecast','card.principles'])assert.ok(text.includes(t(key,{},locale)),`${locale}: ${key}`);
    assert.ok(text.includes(t('card.personal',{outcome:'NO',confidence:'0'},locale)));
    assert.ok(text.some(value=>value.includes('UTC')));
    assertBounded(canvas,calls);
  }
  assert.deepEqual(detail,before);
  assert.equal(shareProbabilityLabel(null,'ja'),'データなし');
});

test('localized profile cards preserve immutable records and truthful score denominators',()=>{
  const input=snapshot();const before=structuredClone(input);
  const data=profileCardData(input,'https://forecast.example');
  const unchanged=JSON.stringify(data);
  for(const locale of locales)for(const format of ['landscape','portrait']){
    const {canvas,calls}=recorder();renderProfileCard(canvas,data,{format,locale});
    const text=calls.map(call=>call.text);
    for(const key of ['card.accuracy','card.brier','card.calibration','card.recentOutcomes','card.standout','card.finalized'])assert.ok(text.includes(t(key,{},locale)),`${locale} ${format}: ${key}`);
    assert.ok(text.includes(t('card.denominator',{correct:'12',scored:'16'},locale)));
    assert.ok(text.includes(t('card.exclusion',{invalid:'2'},locale)));
    assert.ok(text.includes(t('card.recordHash',{hash:'a'.repeat(12)},locale)));
    assert.ok(text.includes('Canonical question?'));
    assert.deepEqual(profileHistoryMarks(data,3,locale).map(item=>item.label),['card.correct','card.miss','card.invalid'].map(key=>t(key,{},locale)));
    assertBounded(canvas,calls);
    assert.equal(JSON.stringify(data),unchanged);
    assert.deepEqual({width:canvas.width,height:canvas.height},PROFILE_CARD_FORMATS[format]);
  }
  assert.deepEqual(input,before);
  assert.equal(data.snapshotHash,input.snapshotHash);
});

test('provisional, empty and long CJK cards fit without omitting scoring caveats',()=>{
  for(const locale of locales)for(const empty of [true,false])for(const format of ['landscape','portrait']){
    const input=snapshot({empty,provisional:!empty});
    input.user.displayName='山田張예측자👩🏽‍💻'.repeat(30);
    if(input.highlight)input.highlight.title='將來の새로운科學發布예측質問'.repeat(25);
    const data=profileCardData(input,'https://forecast.example');
    const {canvas,calls}=recorder();renderProfileCard(canvas,data,{format,locale});
    const text=calls.map(call=>call.text);
    assert.ok(text.includes(t(empty?'card.awaiting':'card.provisional',{},locale)),`${locale} ${format}: sample caveat must not truncate`);
    assert.ok(text.includes(t('card.exclusion',{invalid:empty?'0':'2'},locale)),`${locale} ${format}: exclusion must not truncate`);
    if(empty){assert.ok(text.includes(t('card.noScore',{},locale)));assert.ok(text.includes(t('card.noFinalized',{},locale)));}
    assert.ok(text.some(value=>value.endsWith('…')),'long actual names/titles indicate truncation');
    assertBounded(canvas,calls);
  }
});

test('rendering captures the locale despite a language switch during rendering or PNG completion',async()=>{
  const prior=getLocale();
  try{
    setLocale('ja');
    const data=shareCardData({forecast:{id:'one',question:'Canonical question?'}},'https://forecast.example',asOf);
    setLocale('ko');
    const share=recorder();renderShareCard(share.canvas,data);
    assert.ok(share.calls.some(call=>call.text===t('card.prompt',{},'ja')));
    assert.ok(!share.calls.some(call=>call.text===t('card.prompt',{},'ko')));
    setLocale('ja');
    const profile=recorder({onText:()=>setLocale('ko')});
    renderProfileCard(profile.canvas,profileCardData(snapshot(),'https://forecast.example'));
    assert.ok(profile.calls.some(call=>call.text===t('card.finalized',{},'ja')));
    assert.ok(!profile.calls.some(call=>call.text===t('card.finalized',{},'ko')));
    setLocale('zh-Hant');
    let finish;
    const result=canvasPng({toBlob:callback=>{finish=callback;}});
    setLocale('en');finish(null);
    await assert.rejects(result,{message:t('card.imageFailed',{},'zh-Hant')});
  }finally{setLocale(prior);}
});

test('missing canvas support returns actionable localized share and profile messages',()=>{
  const data=profileCardData(snapshot(),'https://forecast.example');
  for(const locale of locales){
    assert.throws(()=>renderShareCard({getContext:()=>null},{locale}),{message:t('card.imageUnsupported',{},locale)});
    assert.throws(()=>renderProfileCard({getContext:()=>null},data,{locale}),{message:t('card.profileUnsupported',{},locale)});
  }
});
