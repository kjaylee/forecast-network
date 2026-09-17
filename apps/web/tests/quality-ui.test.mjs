import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {escapeHtml,percentage,probability} from '../public/lib.mjs';
import {coreMessages} from '../public/locales/core.mjs';
import {uiMessages} from '../public/locales/ui.mjs';
const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const start=source.indexOf('function comparisonMarkup('),end=source.indexOf('\nfunction castMarkup(',start);
const factory=new Function('esc','t','percentage','probability','formatNumber','categoryLabel','date',`${source.slice(start,end)};return {comparisonMarkup,qualityReasonMarkup,reputationQualityMarkup};`);
function setup(locale){
 const messages={...coreMessages[locale],...uiMessages[locale]};
 const t=(key,args={})=>{assert.equal(typeof messages[key],'string',`missing ${locale} ${key}`);return messages[key].replace(/\{(\w+)\}/g,(_,name)=>args[name]??'');};
 return {t,...factory(escapeHtml,t,percentage,probability,value=>String(value),value=>value,value=>String(value))};
}
for(const locale of ['en','ko','ja','zh-Hant']){
 test(`four independent comparisons preserve null and zero in ${locale}`,()=>{
  const ui=setup(locale);
  const html=ui.comparisonMarkup({crowd:{probability:0,count:2},top:{probability:null,count:0},expert:{probability:80,count:1},ai:{probability:null,count:0}});
  assert.equal((html.match(/class="comparison-item"/g)||[]).length,4);
  assert.match(html,/0<small>%<\/small>/);
  assert.match(html,/80<small>%<\/small>/);
  assert.ok(html.includes(ui.t('quality.experts')));
  assert.ok(html.includes(escapeHtml(ui.t('quality.expertRules'))));
  const empty=ui.comparisonMarkup({});
  assert.ok(empty.includes(ui.t('quality.noExperts')));
  assert.equal((empty.match(/class="comparison-value">—/g)||[]).length,4);
 });
 test(`profile qualification states are honest and safe in ${locale}`,()=>{
  const ui=setup(locale);
  assert.equal(ui.reputationQualityMarkup({}),'');
  const html=ui.reputationQualityMarkup({methodologyVersion:'forecast-quality-v2',consistencyScore:null,calibrationScore:0,
   consistency:{status:'provisional',windows:[{count:1},{count:0},{count:2}]},expertise:[
    {category:'<script>bad</script>',count:1,minimumSamples:20,status:'provisional',qualified:false,brierScore:0,calibrationScore:1},
    {category:'science',count:20,minimumSamples:20,status:'qualified',qualified:true,brierScore:.1,calibrationScore:.8},
    {category:'world',count:20,minimumSamples:20,status:'not-qualified',qualified:false,brierScore:.4,calibrationScore:.5}]});
  assert.ok(html.includes(ui.t('quality.provisionalRecord')));
  assert.ok(html.includes(escapeHtml(ui.t('quality.consistencyHint'))));
  assert.ok(html.includes(ui.t('quality.notQualified')));
  assert.equal((html.match(/quality-status is-qualified/g)||[]).length,1);
  assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/<dd>0%<\/dd>/);
  const contradictory=ui.reputationQualityMarkup({methodologyVersion:'v2',expertise:[{status:'qualified',qualified:false,category:'science'}]});
  assert.doesNotMatch(contradictory,/quality-status is-qualified/);
 });
 test(`ranking disclosure uses retained facts without inventing expertise in ${locale}`,()=>{
  const ui=setup(locale);
  assert.equal(ui.qualityReasonMarkup({}),'');
  const html=ui.qualityReasonMarkup({quality:{componentsBp:{clarity:9200},creatorSampleStatus:'new'}});
  assert.ok(html.includes('92%'));
  assert.ok(html.includes(escapeHtml(ui.t('quality.newCreator'))));
  assert.ok(html.includes(escapeHtml(ui.t('quality.engagementCap'))));
  assert.doesNotMatch(html,/forecast-discovery-v2|scoreBp|methodologyVersion/);
 });
}

test('feed attaches the actual daily reason without changing the forecast object',()=>{
 const ui=setup('en');const node={innerHTML:''};
 const begin=source.indexOf('function renderFeedItems('),finish=source.indexOf('\nfunction sourceList(',begin);
 const render=new Function('document','location','forecastCard',`${source.slice(begin,finish)};return renderFeedItems;`)({querySelector:()=>node},{search:''},ui.qualityReasonMarkup);
 const forecast={id:'question',quality:{componentsBp:{clarity:9000},creatorSampleStatus:'new'}};
 render({items:[forecast],dailyRecommendations:[{id:'question',reason:'clear-cold-start'}]},false);
 assert.ok(node.innerHTML.includes(ui.t('quality.freshQuestion')));
 assert.equal(forecast.recommendationReason,undefined);
});
