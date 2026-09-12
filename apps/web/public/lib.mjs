/** Browser boundary utilities: no provider keys, business rules or mutable domain state. */
import {t,getLocale,intlLocale,formatNumber,errorText} from './i18n.mjs';
export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}

export function safeExternalUrl(value) {
  try {
    const url = new URL(String(value));
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function probability(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

export function percentage(value) {
  const number = probability(value);
  return number === null ? '—' : `${Math.round(number)}%`;
}

export function formatEpochDate(value,{full=false,timeZone,locale=getLocale()}={}) {
  if(!Number.isFinite(value))return t('common.pending',{},locale);
  return new Intl.DateTimeFormat(intlLocale(locale),{
    month:'short',day:'numeric',
    ...(full?{year:'numeric',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}:{}),
    ...(timeZone?{timeZone}:{}),
  }).format(new Date(value));
}

/** Display-only English text. Canonical specifications and hashes stay untouched. */
export function displayForecast(forecast,translation=forecast.displayTranslation) {
  if(!translation || translation.language!=='en' ||
      typeof forecast.specificationHash!=='string' ||
      translation.specificationHash!==forecast.specificationHash)return forecast;
  const rules=new Map((translation.rules || []).filter(rule=>
    typeof rule.clauseId==='string' && typeof rule.condition==='string'
  ).map(rule=>[rule.clauseId,rule.condition]));
  const localized={
    ...forecast,
    title:typeof translation.title==='string'?translation.title:forecast.title,
    question:typeof translation.question==='string'?translation.question:forecast.question,
    translationLanguage:'en',sourceLanguage:translation.sourceLanguage,
    ai:forecast.ai?{...forecast.ai,rationale:typeof translation.aiRationale==='string'?translation.aiRationale:forecast.ai.rationale}:forecast.ai,
  };
  if(forecast.specification){
    localized.specification={...forecast.specification,
      canonicalQuestion:typeof translation.question==='string'?translation.question:forecast.specification.canonicalQuestion,
      rules:(forecast.specification.rules || []).map(rule=>({...rule,condition:rules.get(rule.clauseId) ?? rule.condition})),
      invalidationRules:Array.isArray(translation.invalidationRules) && translation.invalidationRules.every(rule=>typeof rule==='string')
        ? [...translation.invalidationRules] : forecast.specification.invalidationRules,
    };
  }
  return localized;
}

export function forecastIsOpen(forecast, now = Date.now()) {
  return !forecast.participationHold && forecast.state === 'OPEN' && Number.isFinite(forecast.closeAt) && forecast.closeAt > now && (!Number.isFinite(forecast.openAt) || forecast.openAt <= now);
}

export function canChallenge(forecast, now = Date.now()) {
  return ['CHALLENGE','DISPUTED'].includes(forecast.state) && Number.isFinite(forecast.challengeUntil) && forecast.challengeUntil > now;
}

/** Point balances belong to one authenticated account; never reuse another user's. */
export function pointsForUser(snapshot,userId){
  if(!userId||snapshot?.userId!==userId)return null;
  const valid=value=>Number.isSafeInteger(value)&&value>=0;
  if(!valid(snapshot.available)||!valid(snapshot.committed)||!valid(snapshot.total)||snapshot.available+snapshot.committed!==snapshot.total)return null;
  const policy=snapshot.policy;
  if(!policy||typeof policy.version!=='string'||!valid(policy.profileGrant)||!valid(policy.walletGrant)||!valid(policy.maxStake)||policy.maxStake<1)return null;
  if(!valid(policy.winReturnMultiplier)||policy.winReturnMultiplier<1||policy.purchasable!==false||policy.transferable!==false||policy.redeemable!==false||policy.reputationWeighted!==false)return null;
  return snapshot;
}

export function pointStakeLimit(points,userId,position){
  const balance=pointsForUser(points,userId);
  if(!balance)return 0;
  const held=position?.status==='committed'&&Number.isSafeInteger(position.amount)&&position.amount>=0?position.amount:0;
  return Math.min(balance.policy.maxStake,balance.available+held);
}

export function pointStakeValue(value){
  if(typeof value==='string'&&!/^\d+$/.test(value.trim()))throw new ApiError(t('error.stake_invalid'),400,'stake_invalid');
  const amount=typeof value==='string'?Number(value.trim()):value;
  if(!Number.isSafeInteger(amount)||amount<0)throw new ApiError(t('error.stake_invalid'),400,'stake_invalid');
  return amount;
}

export function validatePointStake(value,{points,userId,position}={}){
  const amount=pointStakeValue(value);
  if(amount===0)return 0;
  const balance=pointsForUser(points,userId);
  if(!balance)throw new ApiError(t('error.points_unavailable'),409,'points_unavailable');
  if(amount>balance.policy.maxStake){
    const params={limit:formatNumber(balance.policy.maxStake)};
    const error=new ApiError(t('error.stake_limit_amount',params),400,'stake_limit_exceeded');
    error.translationKey='error.stake_limit_amount';error.translationParams=params;throw error;
  }
  if(amount>pointStakeLimit(balance,userId,position))throw new ApiError(t('error.insufficient_points'),409,'insufficient_points');
  return amount;
}

export function forecastSubmission({forecast,outcome,confidence,stakePoints,expectedUserId,points,position}){
  if(typeof expectedUserId!=='string'||!expectedUserId)throw new ApiError(t('error.account_changed'),409,'account_changed');
  return {outcome,confidence,revision:forecast.revision,stakePoints:validatePointStake(stakePoints,{points,userId:expectedUserId,position}),expectedUserId};
}

export function makeHistoryPath(history) {
  const observations = history.filter(item => Number.isFinite(item.at) && probability(item.probability) !== null).sort((a,b) => a.at-b.at);
  if (!observations.length) return null;
  const start = observations[0].at;
  const end = observations.at(-1).at;
  const points = observations.map((item,index) => ({
    x: end === start ? 250 : 20 + (item.at - start) / (end - start) * 460,
    y: 150 - item.probability * 1.3,
    index,
  }));
  return {points, path:points.map(point => `${point.index ? 'L':'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' '),start,end};
}

export class ApiError extends Error {
  constructor(message, status = 0, code = 'network_error') { super(message); this.name = 'ApiError'; this.status = status; this.code = code; }
}

export function createApi(fetcher = globalThis.fetch.bind(globalThis)) {
  const pendingKeys = new Map();
  return async function api(path, {method = 'GET', body, timeout = 25000, idempotent = false} = {}) {
    if (!path.startsWith('/api/') || path.startsWith('//')) throw new Error('API path must be same-origin');
    const identity = `${method}:${path}:${JSON.stringify(body ?? {})}`;
    const headers = {'Accept':'application/json','X-Forecast-Client':'web'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (idempotent) {
      if (!pendingKeys.has(identity)) pendingKeys.set(identity, crypto.randomUUID());
      headers['Idempotency-Key'] = pendingKeys.get(identity);
      body = {...body, idempotencyKey:headers['Idempotency-Key']};
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetcher(path, {method,headers,credentials:'same-origin',cache:'no-store',signal:controller.signal,...(body === undefined ? {}:{body:JSON.stringify(body)})});
      let result;
      try { result = await response.json(); } catch { throw new ApiError(t('error.invalid_response'),response.status,'invalid_response'); }
      if (!response.ok) {
        if (response.status === 409) pendingKeys.delete(identity);
        const failure=new ApiError(result?.error?.message || t('error.request_failed'),response.status,result?.error?.code || 'request_failed');
        failure.message=errorText(failure);throw failure;
      }
      if (idempotent) pendingKeys.delete(identity);
      return result.data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error.name === 'AbortError') throw new ApiError(t('error.timeout'),0,'timeout');
      throw new ApiError(t('error.network_error'),0,'network_error');
    } finally { clearTimeout(timer); }
  };
}
