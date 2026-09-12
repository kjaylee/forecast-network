/** Display-only, content-addressed translations. No forecast or account mutation. */
export const TRANSLATION_LANGUAGES=Object.freeze(['ko','ja','zh-Hant']);
const SOURCE_PREFIX='forecast-network:sha256:display-source:v1\n';
const TRANSLATION_PREFIX='forecast-network:sha256:display-translation:v1\n';
const HASH=/^[0-9a-f]{64}$/;
const text=value=>typeof value==='string'&&value.length>0&&value.length<=20000;
const fail=()=>{throw new Error('translation_verification_failed');};
export function canonicalTranslationJson(value){
  if(value===null||typeof value!=='object')return JSON.stringify(value);
  if(Array.isArray(value))return `[${value.map(canonicalTranslationJson).join(',')}]`;
  return `{${Object.keys(value).sort().map(key=>`${JSON.stringify(key)}:${canonicalTranslationJson(value[key])}`).join(',')}}`;
}
async function digest(prefix,value){
  const bytes=new TextEncoder().encode(prefix+value);
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(byte=>byte.toString(16).padStart(2,'0')).join('');
}
function sourceIdentity(forecast){
  return canonicalTranslationJson({id:forecast.id,hash:forecast.specificationHash,title:forecast.title,question:forecast.question,
    openAt:forecast.openAt,closeAt:forecast.closeAt,rationale:forecast.ai?.rationale??null,
    rules:forecast.specification?.rules??null,invalidations:forecast.specification?.invalidationRules??null});
}
function sourceMatches(source,forecast){
  if(!source||source.schemaVersion!==1||source.language!=='en'||source.forecastId!==forecast.id||source.specificationHash!==forecast.specificationHash)return false;
  if(!HASH.test(source.specificationHash)||!text(source.title)||!text(source.question)||source.title!==forecast.title||source.question!==forecast.question)return false;
  if(!Number.isSafeInteger(source.openAt)||!Number.isSafeInteger(source.closeAt))return false;
  if(forecast.openAt!==undefined&&source.openAt!==forecast.openAt||forecast.closeAt!==undefined&&source.closeAt!==forecast.closeAt)return false;
  if(!Array.isArray(source.rules)||!source.rules.length||source.rules.length>100||!source.rules.every(rule=>text(rule.clauseId)&&['YES','NO','INVALID'].includes(rule.outcome)&&text(rule.condition)))return false;
  if(new Set(source.rules.map(rule=>rule.clauseId)).size!==source.rules.length)return false;
  if(!Array.isArray(source.invalidationRules)||source.invalidationRules.length>100||!source.invalidationRules.every(text))return false;
  if(source.aiRationale!==null&&!text(source.aiRationale))return false;
  if(source.aiRationale!==(forecast.ai?.rationale??null))return false;
  const spec=forecast.specification;
  if(spec&&(canonicalTranslationJson(source.rules)!==canonicalTranslationJson((spec.rules||[]).map(({clauseId,outcome,condition})=>({clauseId,outcome,condition})))||canonicalTranslationJson(source.invalidationRules)!==canonicalTranslationJson(spec.invalidationRules||[])))return false;
  return true;
}
export async function verifyTranslationResponse(response,forecast,language,{sourceHash=null}={}){
  if(!TRANSLATION_LANGUAGES.includes(language)||!['ready','missing'].includes(response?.status)||!sourceMatches(response.source,forecast)||!HASH.test(response.sourceHash))fail();
  if(sourceHash&&sourceHash!==response.sourceHash)fail();
  const sourceJson=canonicalTranslationJson(response.source);
  if(sourceJson.length>131072||await digest(SOURCE_PREFIX,sourceJson)!==response.sourceHash)fail();
  if(response.status==='missing'){if(response.translation!==null)fail();return {source:response.source,sourceHash:response.sourceHash,translation:null};}
  const envelope=response.translation;
  if(!envelope||typeof envelope.canonicalJson!=='string'||envelope.canonicalJson.length>131072||!HASH.test(envelope.translationHash)||envelope.commitmentProfile?.algorithm!=='SHA-256'||envelope.commitmentProfile.prefix!==TRANSLATION_PREFIX)fail();
  if(await digest(TRANSLATION_PREFIX,envelope.canonicalJson)!==envelope.translationHash)fail();
  const translated=JSON.parse(envelope.canonicalJson);
  if(!translated||canonicalTranslationJson(translated)!==envelope.canonicalJson)fail();
  if(translated.forecastId!==forecast.id||translated.specificationHash!==forecast.specificationHash||translated.sourceHash!==response.sourceHash||translated.language!==language||translated.sourceLanguage!=='en'||translated.attribution!=='AI translation'||!Number.isSafeInteger(translated.translatedAt)||translated.translatedAt<0)fail();
  if(!text(translated.title)||!text(translated.question)||!Array.isArray(translated.rules)||translated.rules.length!==response.source.rules.length)fail();
  if(!translated.rules.every((rule,index)=>rule.clauseId===response.source.rules[index].clauseId&&rule.outcome===response.source.rules[index].outcome&&text(rule.condition)))fail();
  if(!Array.isArray(translated.invalidationRules)||translated.invalidationRules.length!==response.source.invalidationRules.length||!translated.invalidationRules.every(text))fail();
  if(response.source.aiRationale===null?translated.aiRationale!==null:!text(translated.aiRationale))fail();
  return {source:response.source,sourceHash:response.sourceHash,translation:translated};
}

/** One user gesture starts work. Changing context invalidates all pending presentation. */
export function createForecastTranslations({api,onChange=()=>{},maxEntries=100}={}){
  let context=0,locale='en';
  const entries=new Map(),cache=new Map();
  const notify=()=>onChange();
  const key=(entry,language)=>`${entry.identity}\n${language}`;
  function register(forecast){
    const identity=sourceIdentity(forecast),prior=entries.get(forecast.id);
    if(!prior||prior.identity!==identity){
      entries.set(forecast.id,{identity,forecast,status:'original',language:null,translation:null,choices:false,error:null});
    }
    return entries.get(forecast.id);
  }
  function reset(nextLocale){context+=1;locale=nextLocale;entries.clear();}
  function get(id){return entries.get(id)??null;}
  function original(id){const entry=get(id);if(!entry)return;entry.token=null;entry.status='original';entry.translation=null;entry.choices=false;entry.error=null;notify();}
  async function translate(id,requestedLanguage){
    const entry=get(id);if(locale==='en'||!entry||entry.status==='pending')return;
    const language=requestedLanguage||(locale==='en'?null:locale);
    if(!language){entry.choices=!entry.choices;notify();return;}
    if(!TRANSLATION_LANGUAGES.includes(language))return;
    if(locale!=='en'&&language!==locale)return;
    const token=Symbol(),generation=context;
    const current=()=>context===generation&&entries.get(id)===entry&&entry.token===token;
    entry.token=token;entry.language=language;entry.choices=false;entry.status='pending';entry.error=null;entry.translation=null;notify();
    try{
      let result=cache.get(key(entry,language));
      if(!result){
        const endpoint=`/api/forecasts/${encodeURIComponent(id)}/translation`;
        result=await verifyTranslationResponse(await api(`${endpoint}?language=${encodeURIComponent(language)}`),entry.forecast,language);
        if(!current())return;
        if(!result.translation){
          result=await verifyTranslationResponse(await api(endpoint,{method:'POST',timeout:130000,body:{language,specificationHash:entry.forecast.specificationHash,sourceHash:result.sourceHash}}),entry.forecast,language,{sourceHash:result.sourceHash});
          if(!result.translation)fail();
        }
        if(!current())return;
        cache.set(key(entry,language),result);
        if(cache.size>maxEntries)cache.delete(cache.keys().next().value);
      }
      if(!current())return;
      entry.translation=result.translation;entry.status='translated';notify();
    }catch(error){
      if(!current())return;
      entry.status='error';entry.error=['ai_work_in_progress','translation_in_progress'].includes(error?.code)?'busy':error?.status===429?'limited':'failed';notify();
    }
  }
  return {register,reset,get,original,translate};
}
