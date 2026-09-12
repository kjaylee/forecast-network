/** Explicit, account-bound quote and acceptance. No client pricing or optimistic debit. */
const modes=['shadow','active','preview'];
const integer=value=>Number.isSafeInteger(value)&&value>=0;
const atomic=value=>typeof value==='string'&&/^(0|[1-9][0-9]{0,15})$/.test(value)&&BigInt(value)<=BigInt(Number.MAX_SAFE_INTEGER);
const invalid=()=>Object.assign(new Error('Market response failed validation.'),{code:'market_response_invalid'});
const evidenceStopped=market=>['eligibility_review','frozen_before_evidence'].includes(market.probabilityStatus);
export function validateMarket(value,forecastId){
  if(!value||value.forecastId!==forecastId||!modes.includes(value.mode)||!integer(value.revision)||value.atomicScale!==1000000||!Number.isSafeInteger(value.maxSpendPoints)||value.maxSpendPoints<1||value.maxSpendPoints>100)throw invalid();
  const status=value.probabilityStatus??'current';
  if(!['current','eligibility_review','frozen_before_evidence'].includes(status))throw invalid();
  if(status==='eligibility_review'){
    if(value.yesProbabilityBps!==null||value.probabilityRevision!==null)throw invalid();
  }else{
    if(!integer(value.yesProbabilityBps)||value.yesProbabilityBps>10000)throw invalid();
    if((status==='frozen_before_evidence'||value.probabilityRevision!==undefined)&&(!integer(value.probabilityRevision)||value.probabilityRevision>value.revision))throw invalid();
  }
  return value;
}
export function validateMarketQuote(value,{market,userId,side,spendPoints},now=Date.now()){
  if(!value||value.forecastId!==market.forecastId||value.side!==side||value.spendPoints!==spendPoints||value.specificationHash!==market.specificationHash||value.policyHash!==market.policyHash||!atomic(value.claimsAtomic)||BigInt(value.claimsAtomic)===0n||!integer(value.revision)||value.revision<market.revision||!integer(value.expiresAt)||value.expiresAt<=now)throw invalid();
  if(value.priceAfterBps<value.priceBeforeBps)throw invalid();
  if(![value.priceBeforeBps,value.priceAfterBps].every(item=>integer(item)&&item<=10000))throw invalid();
  if(userId){if(value.userId!==userId||value.mode!==market.mode||typeof value.quoteId!=='string'||!value.quoteId)throw invalid();}
  else if(value.mode!=='preview'||value.userId!=='preview'||value.quoteId!==null||value.nonbinding!==true)throw invalid();
  return Object.freeze({...value});
}
export function claimsInPoints(claimsAtomic){if(!atomic(claimsAtomic))throw invalid();return Number(BigInt(claimsAtomic))/1000000;}
export function createMarketClient({api,onChange=()=>{},now=()=>Date.now(),randomId=()=>crypto.randomUUID()}={}){
  let current=null,generation=0;const unresolved=new Map(),drafts=new Map();
  const key=(market,userId)=>`${userId||''}:${market.forecastId}`;
  const notify=()=>onChange(current);
  function attach(market,userId){
    generation++;if(!market){current=null;return;}
    validateMarket(market,market.forecastId);
    const pending=unresolved.get(key(market,userId)),draft=drafts.get(key(market,userId));
    current=pending?{...pending,market,phase:'uncertain',reconciling:false}:{market,userId:userId||null,side:draft?.side||'YES',spendRaw:draft?.spendRaw||String(Math.min(50,market.maxSpendPoints)),phase:'idle',quote:null,receipt:null,error:null,request:null,reconciling:false};
  }
  function detach(){generation++;current=null;}
  function input(side,spendRaw){if(!current||['filling','uncertain'].includes(current.phase))return;current.side=side;current.spendRaw=spendRaw;drafts.set(key(current.market,current.userId),{side,spendRaw});if(drafts.size>100)drafts.delete(drafts.keys().next().value);current.quote=null;current.phase='idle';current.error=null;generation++;}
  async function quote(){
    if(!current||evidenceStopped(current.market)||['quoting','filling','uncertain'].includes(current.phase))return;
    const entry=current,token=++generation,spendPoints=Number(entry.spendRaw);
    const valid=()=>current===entry&&token===generation;
    if(!['YES','NO'].includes(entry.side)||!/^\d+$/.test(entry.spendRaw)||!Number.isSafeInteger(spendPoints)||spendPoints<1||spendPoints>entry.market.maxSpendPoints){entry.error={code:'market_spend_invalid'};notify();return;}
    entry.phase='quoting';entry.quote=null;entry.receipt=null;entry.error=null;notify();
    try{
      const body={side:entry.side,spendPoints,...(entry.userId?{expectedUserId:entry.userId}:{})};
      const result=await api(`/api/forecasts/${encodeURIComponent(entry.market.forecastId)}/market/quote`,{method:'POST',body});
      if(!valid()||evidenceStopped(entry.market))return;
      entry.quote=validateMarketQuote(result,{market:entry.market,userId:entry.userId,side:entry.side,spendPoints},now());entry.phase='quoted';notify();
    }catch(error){if(valid()){entry.phase='idle';entry.error=error;notify();}}
  }
  async function fill(){
    const entry=current;if(!entry||entry.reconciling||evidenceStopped(entry.market)||!entry.userId||!entry.quote||!['quoted','uncertain'].includes(entry.phase)||entry.market.mode==='preview'||entry.market.mode==='active'&&!entry.market.liveEnabled)return;
    if(entry.phase!=='uncertain'&&entry.quote.expiresAt<=now()){entry.phase='expired';entry.error={code:'market_quote_expired'};notify();return;}
    const token=++generation,valid=()=>current===entry&&token===generation;
    entry.request??={quoteId:entry.quote.quoteId,minClaimsAtomic:entry.quote.claimsAtomic,idempotencyKey:randomId(),expectedUserId:entry.userId};
    entry.phase='filling';entry.error=null;unresolved.set(key(entry.market,entry.userId),entry);notify();
    try{
      const receipt=await api(`/api/forecasts/${encodeURIComponent(entry.market.forecastId)}/market/fill`,{method:'POST',body:entry.request});
      if(!receipt||receipt.status!=='accepted'||typeof receipt.id!=='string'||receipt.quoteId!==entry.quote.quoteId||receipt.forecastId!==entry.market.forecastId||receipt.userId!==entry.userId||receipt.mode!==entry.quote.mode||receipt.side!==entry.quote.side||receipt.spendPoints!==entry.quote.spendPoints||receipt.claimsAtomic!==entry.quote.claimsAtomic)throw invalid();
      unresolved.delete(key(entry.market,entry.userId));
      if(!valid())return;
      entry.receipt=receipt;entry.phase='filled';entry.request=null;notify();
    }catch(error){
      const uncertain=!error.status||error.status>=500;
      if(!uncertain)unresolved.delete(key(entry.market,entry.userId));
      if(valid()){entry.phase=uncertain?'uncertain':'expired';entry.error=error;if(!uncertain)entry.request=null;notify();}
    }
  }
  async function reconcile(){
    const entry=current;if(!entry||entry.phase!=='uncertain'||entry.reconciling||!entry.userId||!entry.quote||!entry.request)return;
    const token=++generation,valid=()=>current===entry&&token===generation;
    entry.reconciling=true;entry.error=null;notify();
    try{
      const query=new URLSearchParams({quoteId:entry.request.quoteId,idempotencyKey:entry.request.idempotencyKey});
      const result=await api(`/api/forecasts/${encodeURIComponent(entry.market.forecastId)}/market/receipt?${query}`,{method:'GET'});
      if(!valid())return;
      if(!result||result.forecastId!==entry.market.forecastId||result.userId!==entry.userId||result.quoteId!==entry.quote.quoteId||!['accepted','void','not_accepted','pending'].includes(result.status))throw invalid();
      if(['pending','not_accepted'].includes(result.status)){
        if(result.receipt!==null)throw invalid();
        if(result.status==='pending')return;
        entry.receipt=null;entry.quote=null;entry.phase='not_accepted';
      }else{
        const receipt=result.receipt;
        if(!receipt||receipt.status!==result.status||typeof receipt.id!=='string'||!receipt.id||!integer(receipt.acceptedAt))throw invalid();
        for(const field of ['forecastId','userId','quoteId','mode','side','spendPoints','claimsAtomic','specificationHash','policyHash','revision','priceBeforeBps','priceAfterBps','expiresAt'])if(receipt[field]!==entry.quote[field])throw invalid();
        if(result.status==='void'&&(!integer(receipt.refundedPoints)||receipt.refundedPoints!==entry.quote.spendPoints||!integer(receipt.voidedAt)||receipt.voidedAt<receipt.acceptedAt||typeof receipt.eligibilityDecisionId!=='string'||!receipt.eligibilityDecisionId))throw invalid();
        entry.receipt=Object.freeze({...receipt});entry.phase=result.status==='void'?'void':'filled';
      }
      unresolved.delete(key(entry.market,entry.userId));entry.request=null;
    }catch(error){if(valid())entry.error=error;}
    finally{entry.reconciling=false;if(valid())notify();}
  }
  return {attach,detach,input,quote,fill,reconcile,get:()=>current};
}
