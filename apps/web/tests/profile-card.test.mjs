import test from 'node:test';
import assert from 'node:assert/strict';
import {PROFILE_CARD_FORMATS,profileCardData,profileSampleStatus,profileAccuracyLabel,profileScoreLabel,profileHistoryMarks,profileTextLines,renderProfileCard} from '../public/profile-card.mjs';

const AS_OF=Date.parse('2026-09-10T02:30:00Z');
function snapshot(){return {
  schemaVersion:1,asOf:AS_OF,
  user:{id:'user_fixture',displayName:'Ada Park',handle:'ada_forecasts',createdAt:AS_OF-86400000},
  metrics:{totalForecasts:20,resolvedForecasts:16,correctForecasts:12,invalidForecasts:2,accuracy:75,brierScore:.182,calibrationScore:.863},
  sampleStatus:'established',
  history:Array.from({length:8},(_,index)=>({forecastId:`forecast-${index}`,title:`Fixture forecast ${index}`,outcome:index%2?'NO':'YES',correct:index<4?true:index<7?false:null,confidence:70,finalizedAt:AS_OF-(index+1)*1000,resolvedOutcome:index===7?'INVALID':null})),
  historyTruncated:true,
  highlight:{forecastId:'forecast-0',title:'Will a lunar landing be announced before the deadline?',outcome:'YES',confidence:85,resolvedAt:AS_OF-1000,specificationHash:'b'.repeat(64)},
  methodology:{version:'profile-card-v1'},snapshotHash:'a'.repeat(64),
};}
function newSnapshot(){return {...snapshot(),metrics:{totalForecasts:0,resolvedForecasts:0,correctForecasts:0,invalidForecasts:0,accuracy:null,brierScore:null,calibrationScore:null},sampleStatus:'new',history:[],historyTruncated:false,highlight:null};}

test('profile snapshot whitelists public fields and keeps the source unchanged',()=>{
  const original=snapshot();
  original.wallet={address:'wallet-private-marker'};original.recoveryCode='secret-marker';
  original.user.session='session-marker';original.metrics.privateKey='private-key-marker';
  original.methodology.secret='methodology-secret-marker';original.commitmentProfile={secret:'extra-secret-marker'};
  const before=structuredClone(original);
  const data=profileCardData(original,'https://forecast.example');
  assert.deepEqual(original,before);
  assert.equal(data.metrics.accuracy,75);
  assert.equal(data.snapshotHash,original.snapshotHash);
  assert.ok(Object.isFrozen(data)&&Object.isFrozen(data.metrics)&&Object.isFrozen(data.history[0]));
  assert.doesNotMatch(JSON.stringify(data),/private-marker|secret-marker|session-marker|private-key-marker/);
  assert.deepEqual(data.methodology,{version:'profile-card-v1'});
});

test('published record URLs use the canonical creator route and reject credential origins',()=>{
  const input=snapshot();input.url='https://attacker.example';input.user.profileUrl='https://attacker.example';
  const data=profileCardData(input,'https://forecast.example/ignored?override=yes');
  assert.equal(data.profileUrl,'https://forecast.example/creators/user_fixture');
  assert.equal(data.url,`https://forecast.example/creators/user_fixture?record=${'a'.repeat(64)}`);
  assert.equal(data.domain,'forecast.example');
  for(const origin of ['https://user:password@forecast.example','javascript:alert(1)','//forecast.example'])assert.throws(()=>profileCardData(input,origin),TypeError);
  input.user.id='../../different?record=other';
  assert.equal(new URL(profileCardData(input,'https://forecast.example').url).pathname,'/creators/..%2F..%2Fdifferent%3Frecord%3Dother');
  input.user.id='..';assert.throws(()=>profileCardData(input,'https://forecast.example'),TypeError);
});

test('missing measurements stay empty while actual zero accuracy remains zero',()=>{
  const empty=profileCardData(newSnapshot(),'https://forecast.example');
  assert.equal(empty.metrics.accuracy,null);assert.equal(profileAccuracyLabel(empty.metrics.accuracy),'—');
  const zero={...newSnapshot(),sampleStatus:'provisional',metrics:{totalForecasts:3,resolvedForecasts:3,correctForecasts:0,invalidForecasts:0,accuracy:0,brierScore:1,calibrationScore:0}};
  const data=profileCardData(zero,'https://forecast.example');
  assert.equal(profileAccuracyLabel(data.metrics.accuracy),'0%');
  assert.equal(profileScoreLabel(data.metrics.calibrationScore),'0.000');
  delete zero.metrics.accuracy;
  assert.equal(profileCardData(zero,'https://forecast.example').metrics.accuracy,null);
  assert.equal(profileAccuracyLabel(99.999),'>99.9%');
  assert.equal(profileAccuracyLabel(.001),'<0.1%');
  assert.equal(profileScoreLabel(.00001),'<0.001');
  assert.equal(profileScoreLabel(.99999),'>0.999');
});

test('sample labels and score denominators agree with valid finalized results',()=>{
  assert.equal(profileSampleStatus(0),'new');assert.equal(profileSampleStatus(1),'provisional');
  assert.equal(profileSampleStatus(9),'provisional');assert.equal(profileSampleStatus(10),'established');
  for(const mutate of [
    value=>{value.sampleStatus='provisional';},
    value=>{value.metrics.correctForecasts=17;},
    value=>{value.metrics.totalForecasts=17;},
    value=>{value.metrics.accuracy=99;},
    value=>{value.metrics.resolvedForecasts='16';},
    value=>{value.metrics.brierScore=NaN;},
    value=>{value.metrics.calibrationScore=1.01;},
  ]){const input=snapshot();mutate(input);assert.throws(()=>profileCardData(input,'https://forecast.example'),TypeError);}
  const input=newSnapshot();input.metrics.accuracy=0;
  assert.throws(()=>profileCardData(input,'https://forecast.example'),/unscored profile/);
});

test('snapshot identity, chronology, methodology and history validation fail closed',()=>{
  for(const mutate of [
    value=>{value.schemaVersion=2;},value=>{value.asOf=Infinity;},
    value=>{value.user.createdAt=AS_OF+1;},value=>{value.user.displayName='';},
    value=>{value.methodology.version='unknown';},value=>{value.snapshotHash='not-a-hash';},
    value=>{value.history[0].finalizedAt=AS_OF+1;},value=>{value.history[0].correct='yes';},
    value=>{value.history[0].outcome='INVALID';},value=>{value.history[0].resolvedOutcome='INVALID';},
    value=>{value.history[1].forecastId=value.history[0].forecastId;},
    value=>{value.highlight.confidence=101;},value=>{value.highlight.resolvedAt=AS_OF+1;},
  ]){const input=snapshot();mutate(input);assert.throws(()=>profileCardData(input,'https://forecast.example'),TypeError);}
});

test('recent marks represent only supplied history with no placeholders or invented wins',()=>{
  const data=profileCardData(snapshot(),'https://forecast.example');
  assert.deepEqual(profileHistoryMarks(data,8).map(item=>item.label),['Correct','Correct','Correct','Correct','Miss','Miss','Miss','Invalid']);
  assert.equal(profileHistoryMarks(data,6).length,6);
  assert.deepEqual(profileHistoryMarks(profileCardData(newSnapshot(),'https://forecast.example'),8),[]);
  const input=snapshot();input.history=input.history.slice().reverse();
  assert.equal(profileCardData(input,'https://forecast.example').history[0].forecastId,'forecast-0');
});

test('bounded wrapping preserves Unicode graphemes and marks actual truncation',()=>{
  const segmenter=new Intl.Segmenter('en',{granularity:'grapheme'});
  const measure=value=>[...segmenter.segment(value)].length*10;
  const text='👩🏽‍💻'.repeat(30);
  const result=profileTextLines(text,measure,{maxWidth:100,maxLines:2});
  assert.equal(result.lines.length,2);assert.equal(result.truncated,true);assert.ok(result.lines[1].endsWith('…'));
  assert.ok(result.lines.every(line=>measure(line)<=100));
  assert.deepEqual(profileTextLines('Track record\nstarts here.',measure,{maxWidth:300,maxLines:2}),{lines:['Track record','starts here.'],truncated:false});
  assert.throws(()=>profileTextLines(text,measure,{maxWidth:0}),RangeError);
});

function canvasRecorder(){
  const calls=[];
  const canvas={width:0,height:0};
  const ctx={createLinearGradient:()=>({addColorStop(){}}),save(){},restore(){},clip(){},closePath(){},font:'500 20px sans-serif',textAlign:'left',fillStyle:'',
    measureText(value){const size=Number(this.font.match(/([\d.]+)px/)?.[1] || 20);return {width:[...new Intl.Segmenter('en',{granularity:'grapheme'}).segment(String(value))].length*size*.57};},
    fillText(value,x,y){const width=this.measureText(value).width;calls.push({kind:'text',value:String(value),x,y,width,align:this.textAlign,font:this.font});},
    fillRect(x,y,width,height){calls.push({kind:'rectangle',x,y,width,height});},
    beginPath(){},roundRect(x,y,width,height){calls.push({kind:'round',x,y,width,height});},
    fill(){},stroke(){},moveTo(){},lineTo(){},
  };
  canvas.getContext=()=>ctx;
  return {canvas,calls};
}

test('both fixed layouts and themes remain bounded with long Unicode names and titles',()=>{
  const input=snapshot();input.user.displayName='Alexandra 👩🏽‍💻 김수현 '.repeat(15);input.user.handle='a'.repeat(150);input.highlight.title='A long but genuine question about a future scientific announcement '.repeat(20);
  const data=profileCardData(input,'https://forecast.example');
  for(const format of ['landscape','portrait'])for(const theme of ['paper','ink']){
    const {canvas,calls}=canvasRecorder();renderProfileCard(canvas,data,{format,theme});
    assert.deepEqual({width:canvas.width,height:canvas.height},PROFILE_CARD_FORMATS[format]);
    for(const call of calls){
      if(call.kind==='text'){
        const left=call.align==='right'?call.x-call.width:call.x;
        const right=call.align==='right'?call.x:call.x+call.width;
        assert.ok(left>=0&&right<=canvas.width+1&&call.y>0&&call.y<canvas.height,JSON.stringify(call));
      }else assert.ok(call.x>=0&&call.y>=0&&call.x+call.width<=canvas.width&&call.y+call.height<=canvas.height,JSON.stringify(call));
    }
    assert.ok(calls.some(call=>call.kind==='text'&&call.value.includes('…')));
    assert.ok(calls.some(call=>call.kind==='text'&&call.value==='12 correct / 16 scored'));
    assert.ok(calls.some(call=>call.kind==='text'&&call.value.includes('UTC')));
    assert.ok(calls.some(call=>call.kind==='text'&&call.value.includes('2 invalid excluded')));
    assert.doesNotMatch(calls.filter(call=>call.kind==='text').map(call=>call.value).join(' '),/on-chain|verified|percentile|top \d/i);
  }
});

test('empty cards have a clear beginning without fake scores, history or achievements',()=>{
  const data=profileCardData(newSnapshot(),'https://forecast.example');
  for(const format of ['landscape','portrait']){
    const {canvas,calls}=canvasRecorder();renderProfileCard(canvas,data,{format});
    const text=calls.filter(call=>call.kind==='text').map(call=>call.value).join(' ');
    assert.match(text,/Track record starts here/);
    assert.match(text,/No performance score yet/);
    assert.doesNotMatch(text,/0%|Standout call|correct \/|100%/);
    assert.equal(calls.filter(call=>call.kind==='round').length,0,'no decorative placeholder results');
  }
  assert.throws(()=>renderProfileCard(canvasRecorder().canvas,data,{format:'square'}),RangeError);
});
