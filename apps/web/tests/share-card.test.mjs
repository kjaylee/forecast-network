import test from 'node:test';
import assert from 'node:assert/strict';
import {wrapCardText,shareCardData,shareProbabilityLabel,shareWithPlatform,renderShareCard} from '../public/share-card.mjs';

test('Korean questions wrap without losing text or exceeding the measured width',()=>{
  const question='한국은2027년말까지달궤도선을새로발사할까요?'.repeat(12);
  const measure=text=>Array.from(text).length*10;
  const lines=wrapCardText(question,measure,160);
  assert.equal(lines.join(''),question);
  assert.ok(lines.every(line=>measure(line)<=160));
  assert.ok(lines.length>10,'long questions remain complete rather than truncated');
});

test('wrapping respects words, explicit line breaks and grapheme boundaries',()=>{
  const measure=text=>Array.from(new Intl.Segmenter('ko',{granularity:'grapheme'}).segment(text)).length;
  assert.deepEqual(wrapCardText('First question\n다음 질문',measure,15),['First question','다음 질문']);
  assert.deepEqual(wrapCardText('Will Apple announce',measure,12),['Will Apple','announce']);
  const grapheme='👩🏽‍💻';
  assert.deepEqual(wrapCardText(grapheme.repeat(3),measure,1),[grapheme,grapheme,grapheme]);
  assert.throws(()=>wrapCardText('question',measure,0),RangeError);
});

test('share snapshot preserves real zero values and excludes absent personal forecasts',()=>{
  const data=shareCardData({forecast:{id:'a/b',question:'실제 질문',category:'science',crowd:{probability:0},top:{probability:null},ai:{probability:100}}},'https://forecast.example/path',123);
  assert.equal(data.question,'실제 질문');
  assert.deepEqual(data.groups.map(group=>group.value),[0,null,100]);
  assert.equal(data.personal,null);
  assert.equal(data.url,'https://forecast.example/forecasts/a%2Fb');
  assert.equal(data.at,123);
  assert.equal(shareProbabilityLabel(0),'0%');
  assert.equal(shareProbabilityLabel(null),'No data yet');
  assert.equal(shareProbabilityLabel('65'),'No data yet');
});

test('only accepted personal outcome and confidence enter the card',()=>{
  const forecast={id:'a',title:'Question'};
  assert.deepEqual(shareCardData({forecast,myForecast:{outcome:'NO',confidence:0}},'https://example.org').personal,{outcome:'NO',confidence:0});
  assert.equal(shareCardData({forecast,myForecast:{outcome:'YES',confidence:101}},'https://example.org').personal,null);
  assert.equal(shareCardData({forecast,myForecast:{outcome:'INVALID',confidence:50}},'https://example.org').personal,null);
  assert.throws(()=>shareCardData({forecast},'javascript:alert(1)'),TypeError);
});

test('share cards use the same hash-bound English translation as forecast details',()=>{
  const detail={forecast:{id:'a',specificationHash:'abc',question:'원본 질문'},displayTranslation:{language:'en',sourceLanguage:'ko',specificationHash:'abc',question:'Will this happen?',rules:[],invalidationRules:[]}};
  assert.equal(shareCardData(detail,'https://example.org').question,'Will this happen?');
  assert.equal(detail.forecast.question,'원본 질문');
});

test('native share is recorded only after success, never after cancellation or failure',async()=>{
  let records=0;
  const payload={title:'Actual question',url:'https://example.org/forecasts/a'};
  const record=()=>{records+=1;};
  assert.equal(await shareWithPlatform(async received=>assert.equal(received,payload),payload,record),true);
  assert.equal(records,1);
  assert.equal(await shareWithPlatform(async()=>{throw new DOMException('User cancelled','AbortError');},payload,record),false);
  assert.equal(records,1);
  await assert.rejects(shareWithPlatform(async()=>{throw new Error('Platform error');},payload,record),/Platform error/);
  assert.equal(records,1);
});


test('forecast comparison tracks represent only valid supplied probabilities',()=>{
  const rectangles=[];
  const ctx={font:'',textAlign:'left',fillStyle:'',
    measureText(value){return {width:Array.from(String(value)).length*10};},
    fillText(){},fillRect(x,y,width,height){rectangles.push({x,y,width,height,color:this.fillStyle});},
    createLinearGradient:()=>({addColorStop(){}}),save(){},restore(){},clip(){},closePath(){},beginPath(){},moveTo(){},lineTo(){},fill(){},stroke(){},
  };
  const canvas={getContext:()=>ctx};
  const data=shareCardData({forecast:{id:'a',question:'Actual forecast?',crowd:{probability:0},top:{probability:null},ai:{probability:100}}},'https://forecast.example',0);
  renderShareCard(canvas,data);
  const tracks=rectangles.filter(item=>item.height===4);
  assert.equal(tracks.length,3,'zero and 100 have base tracks, only 100 has an accent fill');
  const fills=tracks.filter(item=>item.color==='#344cf1');
  assert.equal(fills.length,1);
  assert.equal(fills[0].width,(952-56)/3,'100% fills the entire comparison track');
  assert.equal(fills[0].x,64+2*((952-56)/3+28));
});
