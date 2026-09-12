/** Display-only language packs. Never change signed messages or domain records. */
import {coreMessages} from './locales/core.mjs';
import {uiMessages} from './locales/ui.mjs';
import {cardMessages} from './locales/cards.mjs';

export const SUPPORTED_LOCALES=Object.freeze([
  Object.freeze({code:'en',name:'English'}),
  Object.freeze({code:'ko',name:'한국어'}),
  Object.freeze({code:'ja',name:'日本語'}),
  Object.freeze({code:'zh-Hant',name:'繁體中文'}),
]);
export const LOCALE_STORAGE_KEY='forecast.locale.v1';
const formats=Object.freeze({en:'en-US',ko:'ko-KR',ja:'ja-JP','zh-Hant':'zh-Hant-HK'});
export const messages=Object.freeze(Object.fromEntries(SUPPORTED_LOCALES.map(({code})=>
  [code,Object.freeze({...coreMessages[code],...uiMessages[code],...cardMessages[code]})])));
let activeLocale='en';

export function normalizeLocale(value){
  if(typeof value!=='string')return 'en';
  const tag=value.trim().replaceAll('_','-').toLowerCase();
  if(/^en(?:-|$)/.test(tag))return 'en';
  if(/^ko(?:-|$)/.test(tag))return 'ko';
  if(/^ja(?:-|$)/.test(tag))return 'ja';
  if(/^zh-(?:hant(?:-|$)|tw$|hk$|mo$)/.test(tag))return 'zh-Hant';
  return 'en';
}
export function getLocale(){return activeLocale;}
export function intlLocale(locale=activeLocale){return formats[normalizeLocale(locale)];}
export function localeName(locale=activeLocale){return SUPPORTED_LOCALES.find(item=>item.code===normalizeLocale(locale)).name;}
function browserStorage(){try{return globalThis.localStorage;}catch{return null;}}

/** English is the first-visit default; only an explicit saved choice overrides it. */
export function initializeLocale({storage=browserStorage(),document=globalThis.document}={}){
  let saved=null;
  try{saved=storage?.getItem(LOCALE_STORAGE_KEY);}catch{/* Private mode still has a usable English UI. */}
  return setLocale(saved,{storage:null,document});
}
export function setLocale(locale,{storage=browserStorage(),document=globalThis.document}={}){
  activeLocale=normalizeLocale(locale);
  if(document?.documentElement){document.documentElement.lang=activeLocale;document.documentElement.dir='ltr';}
  for(const node of document?.querySelectorAll?.('[data-core-i18n]')||[])node.textContent=t(node.dataset.coreI18n);
  for(const node of document?.querySelectorAll?.('[data-core-i18n-content]')||[])node.setAttribute('content',t(node.dataset.coreI18nContent));
  try{storage?.setItem(LOCALE_STORAGE_KEY,activeLocale);}catch{/* Keep the selection for this page if storage is unavailable. */}
  return activeLocale;
}

export function hasMessage(key,locale=activeLocale){return Object.hasOwn(messages[normalizeLocale(locale)],key);}
/** Returns plain text. HTML callers must escape the whole interpolated result. */
export function t(key,params={},locale=activeLocale){
  const template=messages[normalizeLocale(locale)][key] ?? messages.en[key] ?? key;
  return template.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g,(token,name)=>
    Object.hasOwn(params,name)?String(params[name]):token);
}
export function formatNumber(value,options={},locale=activeLocale){
  return typeof value==='number'&&Number.isFinite(value)
    ?new Intl.NumberFormat(intlLocale(locale),options).format(value):'—';
}
export function uiError(key,params={},locale=activeLocale){
  const error=new Error(t(key,params,locale));
  error.translationKey=key;error.translationParams=params;
  error.uiKey=key;error.uiParams=params;
  return error;
}
export function fieldValidationMessage(field,locale=activeLocale){
  const validity=field.validity;
  if(validity.valueMissing)return t('error.field_required',{},locale);
  if(validity.tooShort)return t('error.field_short',{min:formatNumber(field.minLength,{},locale)},locale);
  if(validity.tooLong)return t('error.field_long',{max:formatNumber(field.maxLength,{},locale)},locale);
  if(validity.rangeUnderflow)return t('error.field_min',{min:formatNumber(Number(field.min),{},locale)},locale);
  if(validity.rangeOverflow)return t('error.field_max',{max:formatNumber(Number(field.max),{},locale)},locale);
  if(validity.stepMismatch||validity.badInput)return t('error.field_integer',{},locale);
  return t('error.field_format',{},locale);
}
export function errorText(error,locale=activeLocale){
  if(error?.translationKey&&hasMessage(error.translationKey,locale))return t(error.translationKey,error.translationParams||{},locale);
  const code=typeof error?.code==='string'?error.code:'';
  // Keep the existing specific English messages on the English interface.
  if(code&&hasMessage('error.'+code,locale)){
    if(normalizeLocale(locale)==='en'&&typeof error?.message==='string'&&error.message)return error.message;
    return t('error.'+code,{},locale);
  }
  if(code)return t('error.request_failed',{},locale);
  return typeof error?.message==='string'&&error.message?error.message:t('error.request_failed',{},locale);
}
