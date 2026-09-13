import {escapeHtml as esc, safeExternalUrl, probability, percentage, forecastIsOpen, canChallenge, makeHistoryPath, createApi, formatEpochDate, displayForecast, pointsForUser, pointStakeLimit, pointStakeValue, validatePointStake, forecastSubmission} from './lib.mjs';
import {shareCardData, renderShareCard, canvasPng, shareWithPlatform} from './share-card.mjs';
import {profileCardData, renderProfileCard, profileAccuracyLabel, profileScoreLabel} from './profile-card.mjs';
import {loadCardFonts} from './card-art.mjs';
import {discoverWallets, connectWallet, preferredAccount, observeWallet, signOwnershipChallenge, disconnectWallet} from './wallet.mjs';
import {t,getLocale,setLocale,initializeLocale,formatNumber,intlLocale,errorText,uiError,fieldValidationMessage} from './i18n.mjs';
import {captureLocaleState,restoreLocaleState} from './locale-state.mjs';
import {languageControlMarkup} from './language-control.mjs';
import {createMarketClient,claimsInPoints,validateMarket} from './market-client.mjs';
import {createWalletAuthClient} from './wallet-auth-client.mjs';
import {initializeMobileWallet} from './mobile-wallet.mjs';
import {registerNativeWallet,nativePluginAvailable,attestForecast} from './native-wallet.mjs';
import {createForecastTranslations,TRANSLATION_LANGUAGES} from './forecast-translation.mjs';

initializeLocale();
let pendingWrites=0;
let localePainting=false;
const requestApi=createApi();
// Display translations have their own same-origin API boundary, not an account write lock.
const forecastTranslations=createForecastTranslations({api:createApi(),onChange:paintForecastTranslations});
const api=async(path,options={})=>{
  const write=options.method && !['GET','HEAD'].includes(options.method);
  if(write){pendingWrites+=1;syncLanguageControls();}
  try{return await requestApi(path,options);}finally{if(write){pendingWrites-=1;syncLanguageControls();}}
};
const marketClient=createMarketClient({api,onChange:paintMarket});
const app = document.querySelector('#app');
const authDialog = document.querySelector('#auth-dialog');
const shareDialog = document.querySelector('#share-dialog');
const walletDiscovery=discoverWallets();
const walletState={record:null,phase:'idle',wallet:null,accounts:[],address:null,challenge:null,generation:0,operation:0,connection:0,busy:false,error:'',notice:'',accountChanged:false,off:null};
const pointsState={snapshot:null,request:0,epoch:0,loading:false,error:'',accountChanged:false};
const state = {user:null,me:null,status:null,feed:null,detail:null,sequence:0,selection:null,confidence:70,stakeRaw:null,stakeDirty:false,stakeOwner:null,stakeForecastId:null,draft:null,question:'',authMode:'wallet',authResolve:null,authPending:false,authWalletError:null};
const categories = {technology:'ui.technology',crypto:'ui.crypto',science:'ui.science',entertainment:'ui.culture',world:'ui.world',sports:'ui.sports',other:'ui.other'};
const stateLabels = {DRAFT:'ui.draft',VALIDATING:'ui.underReview',OPEN:'ui.open',LOCKED:'ui.closed',RESOLVING:'ui.checkingEvidence',PROPOSED:'ui.outcomeProposed',CHALLENGE:'ui.challengeWindow',DISPUTED:'ui.disputeReview',ESCALATED:'ui.escalated',FINALIZED:'ui.finalized',ARCHIVED:'ui.archived',PAUSED:'ui.reviewPaused'};
const commandLabels = {PUBLISH:'ui.forecastPublished',BEGIN_VALIDATION:'ui.ruleReviewStarted',ACCEPT_VALIDATION:'ui.ruleReviewCompleted',LOCK:'ui.closed',BEGIN_RESOLUTION:'ui.evidenceReviewStarted',PROPOSE_RESOLUTION:'ui.outcomeProposed',OPEN_CHALLENGE:'ui.challengeOpened',SUBMIT_DISPUTE:'ui.disputeSubmitted',REVIEW_DISPUTE:'ui.disputeReviewed',ESCALATE:'ui.escalatedReview',FINALIZE:'ui.finalized',ARCHIVE:'ui.archived',PAUSE:'ui.reviewPaused',RESUME:'ui.reviewResumed'};
const iconPaths = {
  home:'m3 10 9-7 9 7M5 9v11h5v-7h4v7h5V9',search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',plus:'M12 5v14M5 12h14',activity:'M4 4v16h16M7 14l4-5 4 3 5-7',user:'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0',arrow:'M5 12h14m-5-5 5 5-5 5',back:'M19 12H5m5-5-5 5 5 5',close:'m6 6 12 12M18 6 6 18',share:'M12 16V3m-4 4 4-4 4 4M5 12v8h14v-8',comment:'M21 11a9 9 0 0 1-9 9H4l-2 2V11a9 9 0 0 1 19 0Z',people:'M16 21v-2a5 5 0 0 0-5-5H8a5 5 0 0 0-5 5v2M14 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0M18 3a4 4 0 0 1 0 8m1 4a5 5 0 0 1 3 4v2',clock:'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',check:'m5 12 4 4L19 6',external:'M14 3h7v7m0-7L10 14M10 3H3v18h18v-7',spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',book:'M12 5v16M12 5C8 2 5 2 2 3v16c3-1 6-1 10 2 4-3 7-3 10-2V3c-3-1-6-1-10 2Z',bell:'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4',shield:'m12 2 9 4v6c0 6-9 10-9 10S3 18 3 12V6l9-4Zm-4 10 3 3 5-6',copy:'M9 9h12v12H9V9ZM15 5V2H2v13h3',download:'M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4',refresh:'M20 7V2l-3 3M4 17v5l3-3M20 7A9 9 0 0 0 4 6m0 11a9 9 0 0 0 16 1',logout:'M9 3H3v18h6M9 12h13m-5-5 5 5-5 5',flag:'M4 22V3m0 0c5-4 10 4 16 0v11c-6 4-11-4-16 0',filter:'M3 6h18M7 12h10m-6 6h2',globe:'M2 12h20M12 2c-6 6-6 14 0 20 6-6 6-14 0-20ZM22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',question:'M9 8a3 3 0 0 1 6 0c0 2-3 2-3 5m0 4h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',mail:'M3 5h18v14H3V5Zm0 0 9 8 9-8',edit:'m16 3 5 5L8 21H3v-5L16 3Zm-3 3 5 5',chart:'M3 3v18h18M7 15l4-5 4 3 5-7',lock:'M5 10h14v12H5V10Zm3 0V6a4 4 0 0 1 8 0v4'};
function icon(name) { return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${iconPaths[name] || iconPaths.question}"/></svg>`; }
function initials(name) { return esc(Array.from(String(name || 'F').trim())[0] || 'F'); }
function avatar(name,large=false) { return `<span class="avatar${large?' large':''}" aria-hidden="true">${initials(name)}</span>`; }
function categoryLabel(value,locale=getLocale()) { return t(categories[String(value).toLowerCase()] || 'ui.other',{},locale); }
function stateLabel(value){return stateLabels[value]?t(stateLabels[value]):value;}
function commandLabel(value,nextState){return commandLabels[value]?t(commandLabels[value]):stateLabel(nextState)||value||t('ui.stateChanged');}
function date(value,full=false) { return formatEpochDate(value,{full}); }
function relative(value) { if (!Number.isFinite(value)) return ''; const delta=Date.now()-value;if(delta<60000)return t('ui.justNow');if(delta<3600000)return t('ui.minutesAgo',{count:formatNumber(Math.floor(delta/60000))});if(delta<86400000)return t('ui.hoursAgo',{count:formatNumber(Math.floor(delta/3600000))});if(delta<604800000)return t('ui.daysAgo',{count:formatNumber(Math.floor(delta/86400000))});return date(value); }
function closeLabel(forecast) { if(forecast.participationHold)return t('ui.participationHeld');if(!forecastIsOpen(forecast))return stateLabel(forecast.state);const days=Math.ceil((forecast.closeAt-Date.now())/86400000);if(days>1)return t('ui.closesAt',{date:date(forecast.closeAt)});const hours=Math.ceil((forecast.closeAt-Date.now())/3600000);return hours>1?t('ui.hoursLeft',{count:formatNumber(hours)}):t('ui.closingSoon'); }
function externalLink(url,label) { const safe=safeExternalUrl(url); return safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label || new URL(safe).hostname)}${icon('external')}</a>`:`<span>${esc(label || t('ui.sourceUnavailable'))}</span>`; }
function button(text,action,kind='secondary',extra='') { return `<button type="button" class="button ${kind}" data-action="${action}" ${extra}>${text}</button>`; }
function languageBusy(){return pendingWrites>0||localePainting||state.authPending||walletState.busy;}
function languageControl(id,locale=getLocale()){
  return languageControlMarkup(id,locale,languageBusy());
}
function syncLanguageControls(){document.querySelectorAll('[data-language-select]').forEach(node=>{node.disabled=languageBusy();node.value=getLocale();});}
async function changeLanguage(value){
  if(languageBusy()){syncLanguageControls();toast(t('ui.languageBusy'));return;}
  if(value===getLocale())return;
  const saved=captureLocaleState(document),owner=state.user?.id,epoch=pointsState.epoch,path=location.pathname+location.search;
  const scroll={x:window.scrollX,y:window.scrollY};
  localePainting=true;setLocale(value);syncLanguageControls();
  try{
    await renderRoute({preserve:true,localize:true});
    if(owner!==state.user?.id||epoch!==pointsState.epoch||path!==location.pathname+location.search)return;
    if(authDialog.open)authDialog.innerHTML=authMarkup();
    if(shareDialog.open&&shareSession){
      const session=shareSession;session.locale=getLocale();
      if(session.kind==='profile'&&session.snapshot){session.caption=profileCaption(session.snapshot,session.locale);await renderProfileShare(session);}
      else if(session.kind==='forecast'&&session.detail)await renderForecastShare(session);
    }
    if(owner!==state.user?.id||epoch!==pointsState.epoch||path!==location.pathname+location.search)return;
    restoreLocaleState(document,saved,{translateError:entry=>entry.key?t(entry.key):entry.code?errorText({code:entry.code,message:entry.text}):entry.text});
    window.scrollTo({left:scroll.x,top:scroll.y,behavior:'instant'});
  }finally{localePainting=false;if(main())main().inert=false;syncLanguageControls();}
}
function isAppPath(path) { return path==='/' || /^\/(explore|create|activity|profile|forecasts\/[^/]+|creators\/[^/]+)\/?$/.test(path); }
function routeId() { return location.pathname.split('/')[2] || ''; }
function empty(title,copy,action='',symbol='question',compact=false) { return `<div class="empty-state${compact?' compact':''}"><div class="empty-graphic">${icon(symbol)}</div><h2>${esc(title)}</h2><p>${esc(copy)}</p>${action}</div>`; }
function skeleton() { return `<div aria-busy="true" aria-label="${esc(t('ui.loadingForecasts'))}">${[1,2,3].map(()=>`<div class="forecast-row skeleton"><div class="skeleton-line short"></div><div class="skeleton-line title"></div><div class="skeleton-line title"></div><div class="skeleton-line"></div><div class="skeleton-line short"></div></div>`).join('')}</div>`; }
function showError(error,target) { const message=error?.uiKey?t(error.uiKey,error.uiParams||{}):errorText(error);if(target){target.textContent=message;target.setAttribute('role','alert');target.dataset.errorCode=error?.code || '';target.dataset.errorMessage=error?.message || '';target.dataset.uiKey=error?.uiKey || error?.translationKey || '';}else toast(message); }
let toastTimer;
function toast(message) { const node=document.querySelector('#toast');node.textContent=message;node.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{node.hidden=true;},5500); }
function errorPanel(error) { return `<div class="feed-error" role="alert"><h2>${esc(t('ui.connectionFailed'))}</h2><p>${esc(errorText(error))}</p>${button(`${icon('refresh')} ${esc(t('ui.reload'))}`,'refresh')}</div>`; }
function accountMarkup(){return state.user?`${avatar(state.user.displayName)}<span><span class="account-name">${esc(state.user.displayName)}</span><small>${esc(t('ui.myForecastHistory'))}</small></span>`:`${avatar('F')}<span><span class="account-name">${esc(t('ui.startForecasting'))}</span><small>${esc(t('ui.chooseName'))}</small></span>`;}
function shell() {
  const pathname=location.pathname;
  const nav=[['/','home',t('ui.home')],['/explore','search',t('ui.explore')],['/activity','activity',t('ui.activity')],['/profile','user',t('ui.profile')]];
  const active=path=>pathname===path || (path==='/explore'&&pathname.startsWith('/forecasts'));
  app.innerHTML=`<div class="shell"><aside class="sidebar" aria-label="${esc(t('ui.mainNavigation'))}"><a class="brand" href="/" data-link aria-label="${esc(t('ui.forecastHome'))}">forecast<span class="brand-dot">.</span></a><nav class="nav">${nav.map(([path,symbol,label])=>`<a href="${path}" data-link class="nav-item${active(path)?' active':''}" ${active(path)?'aria-current="page"':''}>${icon(symbol)}${esc(label)}</a>`).join('')}<a href="/create" data-link class="nav-item nav-create${pathname==='/create'?' active':''}">${icon('plus')}${esc(t('ui.createForecast'))}</a></nav><div class="sidebar-bottom"><button class="account-control" data-action="account">${accountMarkup()}</button><div class="side-links"><a href="/blueprint">${esc(t('ui.whitepaper'))}</a><a href="/roadmap">${esc(t('ui.roadmap'))}</a><a href="/privacy">${esc(t('ui.privacy'))}</a><a href="/terms">${esc(t('ui.terms'))}</a></div><p class="side-note">${esc(t('ui.tagline'))}</p></div></aside><div class="workspace"><header class="topbar"><a class="brand" href="/" data-link aria-label="${esc(t('ui.forecastHome'))}">forecast<span class="brand-dot">.</span></a><span class="topbar-label"><span class="dot"></span>${esc(t('ui.tagline'))}</span><div class="topbar-actions">${languageControl('header-language')}<a class="icon-button desktop-only" href="/explore" data-link aria-label="${esc(t('ui.searchForecasts'))}">${icon('search')}</a><a class="icon-button desktop-only" href="/activity" data-link aria-label="${esc(t('ui.myActivity'))}">${icon('bell')}</a><button class="button secondary small" data-action="account" id="header-account">${state.user?esc(state.user.displayName):t('ui.getStarted')}${state.user?'':icon('arrow')}</button></div></header><main id="main" tabindex="-1" class="page-enter"></main></div></div><nav class="mobile-nav" aria-label="${esc(t('ui.mobileNavigation'))}">${[['/','home',t('ui.home')],['/explore','search',t('ui.explore')],['/create','plus',t('ui.create')],['/activity','activity',t('ui.activity')],['/profile','user',t('ui.profile')]].map(([path,symbol,label])=>`<a href="${path}" data-link class="${path==='/create'?'create-link ':''}${active(path)?'active':''}" ${active(path)?'aria-current="page"':''}>${path==='/create'?`<span class="mobile-create-icon">${icon(symbol)}</span>`:icon(symbol)}<span>${esc(label)}</span></a>`).join('')}</nav>`;
}
function updateAccount(){document.querySelectorAll('.account-control').forEach(node=>{node.innerHTML=accountMarkup();});const head=document.querySelector('#header-account');if(head)head.textContent=state.user?.displayName || t('ui.getStarted');}
function context() {
  const total=state.feed?.counts?.active;
  const daily=state.feed?.dailyIds || [];
  const mine=new Set((state.me?.myForecasts||[]).map(item=>item.id));
  const done=daily.filter(id=>mine.has(id)).length;
  return `<aside class="context-column" aria-label="${esc(t('ui.aboutForecasting'))}"><section class="context-block"><div class="context-label"><h2>${esc(t('ui.dailyPractice'))}</h2>${daily.length?`<span>${done} / ${Math.min(daily.length,5)}</span>`:''}</div>${daily.length?`<div class="daily-progress" aria-label="${esc(t('ui.dailyCompleted',{count:formatNumber(done)}))}">${daily.slice(0,5).map(id=>`<span class="${mine.has(id)?'done':''}"></span>`).join('')}</div><p>${esc(t('ui.thoughtfulForecasts'))}<br>${esc(t('ui.learnOutcomes'))}</p>`:`<p>${esc(t('ui.dailyEmpty'))}</p>`}${daily[0]?`<a class="link-arrow" href="/forecasts/${esc(daily.find(id=>!mine.has(id))||daily[0])}" data-link>${esc(t('ui.todaysQuestions'))} ${icon('arrow')}</a>`:`<a class="link-arrow" href="/create" data-link>${esc(t('ui.firstQuestion'))} ${icon('arrow')}</a>`}</section><section class="context-block"><h2>${esc(t('ui.threeViews'))}</h2><div class="explainer-line"><span></span><div><strong>${esc(t('ui.crowd'))}</strong>${esc(t('ui.crowdDefinition'))}</div></div><div class="explainer-line"><span></span><div><strong>${esc(t('ui.topForecasters'))}</strong>${esc(t('ui.topDefinition'))}</div></div><div class="explainer-line"><span></span><div><strong>${esc(t('ui.aiForecast'))}</strong>${esc(t('ui.aiDefinition'))}</div></div><a class="link-arrow" href="/blueprint">${esc(t('ui.howItWorks'))} ${icon('arrow')}</a></section><section class="context-block"><h2>${esc(t('ui.evidenceFirst'))}</h2><p>${esc(t('ui.immutableRulesIntro'))}</p><ul class="context-list">${Number.isFinite(total)?`<li>${esc(t('ui.openForecasts'))}<strong>${formatNumber(total)}</strong></li>`:''}<li>${esc(t('ui.pointsPurchases'))}<strong>${esc(t('ui.none'))}</strong></li></ul><a class="link-arrow" href="/roadmap">${esc(t('ui.building'))} ${icon('arrow')}</a></section><div class="context-links"><a href="/blueprint">${esc(t('ui.whitepaper'))}</a><a href="/roadmap">${esc(t('ui.roadmap'))}</a><a href="/privacy">${esc(t('ui.privacyPolicy'))}</a><a href="/terms">${esc(t('ui.terms'))}</a></div></aside>`;
}
function participationNotice(forecast){
  const hold=forecast.participationHold;if(!hold||forecast.earlyResolution)return '';
  const url=safeExternalUrl(hold.evidenceUrl);
  return `<div class="status-notice participation-notice"><strong>${esc(t('ui.participationHeld'))}</strong><p>${esc(t('ui.participationReviewHint'))}</p>${url?`<a class="link-arrow" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(t('ui.reviewEvidence'))} ${icon('external')}</a>`:''}</div>`;
}
function stateBadge(forecast){return `<span class="state${forecastIsOpen(forecast)?' open':''}">${esc(closeLabel(forecast))}</span>`;}
function translationText(forecast,field,original){
  const entry=forecastTranslations.get(forecast.id),translated=entry?.status==='translated'?entry.translation:null;
  const [kind,index]=field.split(':');
  const value=translated?(kind==='rule'?translated.rules[Number(index)]?.condition:kind==='invalid'?translated.invalidationRules[Number(index)]:translated[kind]):original;
  return `<span data-translation-forecast="${esc(forecast.id)}" data-translation-field="${esc(field)}" lang="${esc(translated?.language||'en')}">${esc(value??original)}</span>`;
}
function translationControl(forecast){
  if(getLocale()==='en')return '';
  const entry=forecastTranslations.get(forecast.id);
  if(!entry)return '';
  const extra=`data-id="${esc(forecast.id)}"`;
  const control=(label,action='translate-forecast',attrs='')=>`<button type="button" class="translation-link" data-action="${action}" ${extra} ${attrs}>${esc(label)}</button>`;
  const active=entry.status==='translated',pending=entry.status==='pending';
  let content=pending?`<span class="translation-pending" role="status">${esc(t('ui.translationPending'))}</span>${control(t('ui.translationOriginal'),'translation-original')}`:
    active?`${control(t('ui.translationOriginal'),'translation-original')}<span class="translation-credit">${esc(t('ui.translationAi'))}</span>`:
    control(t(entry.status==='error'?'ui.translationRetry':'ui.translationTranslate'),'translate-forecast',`aria-expanded="${entry.choices}" ${entry.status==='error'&&entry.language?`data-language="${entry.language}"`: ''}`);
  if(entry.choices)content+=`<div class="translation-languages" role="group" aria-label="${esc(t('ui.translationChoose'))}">${TRANSLATION_LANGUAGES.map(language=>control(({ko:'한국어',ja:'日本語','zh-Hant':'繁體中文'})[language],'translate-forecast',`data-language="${language}" lang="${language}"`)).join('')}</div>`;
  if(active)content+=`<p class="translation-authority">${esc(t('ui.translationAuthority'))}</p>`;
  if(entry.status==='error')content+=`<span class="translation-error" role="status">${esc(t(entry.error==='busy'?'ui.translationBusy':entry.error==='limited'?'ui.translationLimited':'ui.translationFailed'))}</span>`;
  return `<div class="forecast-translation" data-translation-control="${esc(forecast.id)}" aria-busy="${pending}">${content}</div>`;
}
function paintForecastTranslations(){
  // Only display text changes: form nodes, user input, signing dialogs and chart data survive.
  document.querySelectorAll('[data-translation-forecast]').forEach(node=>{
    const entry=forecastTranslations.get(node.dataset.translationForecast);if(!entry)return;
    const field=node.dataset.translationField,[kind,index]=field.split(':');
    const base=entry.forecast;
    const translated=entry.status==='translated'?entry.translation:null;
    const value=translated?(kind==='rule'?translated.rules[Number(index)]?.condition:kind==='invalid'?translated.invalidationRules[Number(index)]:translated[kind]):
      kind==='rule'?base.specification?.rules[Number(index)]?.condition:kind==='invalid'?base.specification?.invalidationRules[Number(index)]:kind==='aiRationale'?base.ai?.rationale:base[kind];
    node.textContent=value??'';node.lang=translated?.language||'en';
  });
  document.querySelectorAll('[data-translation-control]').forEach(node=>{
    const entry=forecastTranslations.get(node.dataset.translationControl);if(!entry)return;
    const focus=node.contains(document.activeElement)?{action:document.activeElement.dataset.action,language:document.activeElement.dataset.language}:null;
    const template=document.createElement('template');template.innerHTML=translationControl(entry.forecast);
    const replacement=template.content.firstElementChild;node.replaceWith(replacement);
    if(focus){const next=[...replacement.querySelectorAll('button')].find(button=>button.dataset.action===focus.action&&button.dataset.language===focus.language)||replacement.querySelector('button');next?.focus({preventScroll:true});}
  });
}
function forecastCard(forecast) {
  forecast=displayForecast(forecast);
  forecastTranslations.register(forecast);
  const crowd=probability(forecast.crowd?.probability);
  const count=forecast.crowd?.count || 0;
  const link=`/forecasts/${encodeURIComponent(forecast.id)}`;
  return `<article class="forecast-row"><div class="row-meta"><span class="category">${esc(categoryLabel(forecast.category))}</span><a href="/creators/${encodeURIComponent(forecast.creator?.id || '')}" data-link class="creator">${avatar(forecast.creator?.displayName)}${esc(forecast.creator?.displayName || t('ui.creator'))}</a><span aria-hidden="true">·</span>${stateBadge(forecast)}</div><h2 class="row-title"><a href="${link}" data-link>${translationText(forecast,forecast.title?'title':'question',forecast.title || forecast.question)}</a></h2>${translationControl(forecast)}${participationNotice(forecast)}<div class="probability-row"><div class="probability-main"><strong>${crowd===null?'—':Math.round(crowd)}${crowd===null?'':`<span class="percent">%</span>`}</strong><span>${crowd===null?t('ui.awaitingForecasts'):t('ui.yesProbability')}</span></div><div class="probability-secondary"><span>${esc(t('ui.topForecasters'))}<b>${percentage(forecast.top?.probability)}</b></span><span>AI<b>${percentage(forecast.ai?.probability)}</b></span></div></div><div class="probability-track" role="img" aria-label="${crowd===null?t('ui.noCrowdYet'):`${t('ui.crowdProbability',{probability:formatNumber(Math.round(crowd))})}`}"><span data-probability="${crowd ?? 0}"></span></div><div class="row-bottom"><div class="row-counts"><span>${icon('people')}${formatNumber(count)}</span><a href="${link}#comments" data-link aria-label="${esc(t('ui.commentsCount',{count:formatNumber(Number(forecast.commentCount)||0)}))}">${icon('comment')} ${Number(forecast.commentCount)||0}</a><button class="text-icon" data-action="share" data-id="${esc(forecast.id)}" data-title="${esc(forecast.title || forecast.question)}" aria-label="${esc(t('ui.shareForecast'))}">${icon('share')}</button></div>${forecastIsOpen(forecast)?`<div class="row-choices"><a class="choice yes" href="${link}?choose=YES#cast" data-link>YES</a><a class="choice" href="${link}?choose=NO#cast" data-link>NO</a></div>`:`<a class="link-arrow row-status-link" href="${link}" data-link>${forecast.finalizedOutcome?t('ui.viewOutcome'):t('ui.viewProgress')}${icon('arrow')}</a>`}</div></article>`;
}
function feedHeading(explore) {
  const params=new URLSearchParams(location.search);
  return `<p class="quiet-note content-language-note">${esc(t('ui.contentLanguage'))}</p><div class="page-heading"><div><h1>${explore?t('ui.exploreForecasts'):t('ui.todayForecasts')}</h1><p>${explore?t('ui.findQuestion'):t('ui.whatNext')}</p></div>${explore?'':`<span class="date-label">${new Intl.DateTimeFormat(intlLocale(getLocale()),{month:'short',day:'numeric',weekday:'short'}).format(new Date())}</span>`}</div>${explore?`<form class="search-form" id="search-form" role="search">${icon('search')}<label class="sr-only" for="search-input">${esc(t('ui.searchForecasts'))}</label><input id="search-input" name="q" type="search" placeholder="${esc(t('ui.searchPlaceholder'))}" value="${esc(params.get('q')||'')}" maxlength="200"><button class="button quiet small" type="submit">${esc(t('ui.search'))}</button></form><div class="filter-chips" aria-label="${esc(t('ui.category'))}">${[['',t('ui.all')],...Object.entries(categories).map(([value,key])=>[value,t(key)])].map(([value,label])=>`<button class="chip${(params.get('category')||'')===value?' active':''}" data-action="category" data-value="${value}" aria-pressed="${(params.get('category')||'')===value}">${esc(label)}</button>`).join('')}</div><div class="sort-row"><span id="result-count">${esc(t('ui.findingForecasts'))}</span><label><span class="sr-only">${esc(t('ui.sort'))}</span><select id="sort-select">${[['trending',t('ui.trending')],['newest',t('ui.newest')],['ending',t('ui.closingSoon')],['ai-gap',t('ui.crowdVsAi')]].map(([value,label])=>`<option value="${value}" ${(params.get('sort')||'trending')===value?'selected':''}>${esc(label)}</option>`).join('')}</select></label></div>`:`<div class="tabs" aria-label="${esc(t('ui.chooseFeed'))}">${[['trending',t('ui.forYou')],['newest',t('ui.new')],['ending',t('ui.closingSoon')],['ai-gap',t('ui.aiVsCrowd')],['following',t('ui.following')]].map(([value,label])=>`<button class="tab${(params.get('sort')||'trending')===value?' active':''}" data-action="sort" data-value="${value}" aria-pressed="${(params.get('sort')||'trending')===value}">${esc(label)}</button>`).join('')}</div>`}`;
}
async function renderFeed(sequence,cached=null) {
  const explore=location.pathname==='/explore';
  main().innerHTML=`<div class="page-columns"><div class="feed-column">${feedHeading(explore)}<div id="feed">${skeleton()}</div></div><div id="context">${context()}</div></div>`;
  const params=new URLSearchParams(location.search);
  const query=new URLSearchParams();['q','category','sort'].forEach(key=>{if(params.get(key))query.set(key,params.get(key));});
  try{
    const data=cached || await api(`/api/forecasts?${query}`);
    if(sequence!==state.sequence)return;
    state.feed=data;
    renderFeedItems(data,explore);
    document.querySelector('#context').innerHTML=context();
    const result=document.querySelector('#result-count');if(result)result.textContent=`${t('ui.forecastCount',{count:formatNumber(data.items?.length || 0)})}${data.nextCursor?` · ${t('ui.moreAvailable')}`:''}`;
  }catch(error){if(sequence===state.sequence)document.querySelector('#feed').innerHTML=errorPanel(error);}
}
function renderFeedItems(data,explore,append=false){
  const list=document.querySelector('#feed');if(!list)return;
  const following=new URLSearchParams(location.search).get('sort')==='following';
  const markup=(data.items||[]).map(forecastCard).join('');
  if(append){list.querySelector('.load-more')?.remove();list.insertAdjacentHTML('beforeend',markup);}else list.innerHTML=markup || empty(following?t('ui.followingEmptyTitle'):explore?t('ui.noMatches'):t('ui.emptyQuestion'),following?t('ui.followHint'):explore?t('ui.searchEmptyHint'):t('ui.feedEmptyHint'),`<a href="${following?'/explore':'/create'}" data-link class="button">${icon(following?'search':'plus')}${following?t('ui.exploreForecasts'):t('ui.createForecast')}</a>`);
  if(data.nextCursor)list.insertAdjacentHTML('beforeend',`<div class="load-more">${button(esc(t('ui.loadMore')),'more','secondary',`data-cursor="${esc(data.nextCursor)}"`)}</div>`);
}
function sourceList(sources){return `<ul class="sources">${(sources||[]).map(source=>`<li>${externalLink(source.url,source.name)}</li>`).join('')}</ul>`;}
function specificationMarkup(spec,forecast=null) {
  if(!spec)return `<p class="quiet-note">${esc(t('ui.rulesLoadFailed'))}</p>`;
  return `<section class="detail-section"><h2>${esc(t('ui.resolutionCriteria'))}</h2>${(spec.rules||[]).map((rule,index)=>`<div class="rule"><strong>${esc(rule.outcome)}</strong><p>${forecast?translationText(forecast,`rule:${index}`,rule.condition):`<span lang="en">${esc(rule.condition)}</span>`}<br><span class="input-hint">${esc(t('ui.clause',{id:rule.clauseId}))}</span></p></div>`).join('')}${spec.invalidationRules?.length?`<span class="inline-label">${esc(t('ui.invalidConditions'))}</span><ul class="quiet-note">${spec.invalidationRules.map((rule,index)=>`<li>${forecast?translationText(forecast,`invalid:${index}`,typeof rule==='string'?rule:rule.condition||rule.description||''):esc(typeof rule==='string'?rule:rule.condition||rule.description||'')}</li>`).join('')}</ul>`:''}<p class="quiet-note">${esc(t('ui.rulesCannotChange'))}</p></section><section class="detail-section"><h2>${esc(t('ui.sourcesToCheck'))}</h2>${sourceList(spec.primarySources)}${spec.fallbackSources?.length?`<span class="inline-label">${esc(t('ui.fallbackSources'))}</span>${sourceList(spec.fallbackSources)}`:''}</section>`;
}
function comparisonMarkup(forecast){return `<div class="comparison" aria-label="${esc(t('ui.compareProbabilities'))}">${[[t('ui.crowd'),forecast.crowd,t('ui.participants',{count:formatNumber(forecast.crowd?.count || 0)})],[t('ui.topForecasters'),forecast.top,forecast.top?.count?t('ui.participants',{count:formatNumber(forecast.top.count)}):t('ui.requiresRecord')],[t('ui.aiForecast'),forecast.ai,forecast.ai?.provider || t('ui.noEstimate')]].map(([label,group,caption])=>{const number=probability(group?.probability);return `<div class="comparison-item"><div class="comparison-label">${esc(label)}</div><div class="comparison-value">${number===null?'—':`${Math.round(number)}<small>%</small>`}</div><div class="comparison-caption">${esc(caption)}</div><div class="probability-track"><span data-probability="${number ?? 0}"></span></div></div>`;}).join('')}</div>`;}
function castMarkup(forecast,myForecast){
  if(forecast.participationHold)return `${participationNotice(forecast)}${stakePositionMarkup()}`;
  if(!forecastIsOpen(forecast))return `<div class="status-notice"><strong>${esc(forecast.finalizedOutcome?`${t('ui.finalized')} · ${forecast.finalizedOutcome}`:stateLabel(forecast.state) || t('ui.forecastingClosed'))}</strong><p>${esc(forecast.pauseReason || (forecast.finalizedOutcome?t('ui.finalizedUnderRules'):t('ui.closedHint')))}</p>${stakePositionMarkup()}</div>`;
  return `<form class="cast-panel" id="cast-form"><div class="cast-panel-header"><h2>${esc(t('ui.whatThink'))}</h2><span>${myForecast?t('ui.canUpdate'):t('ui.myForecast')}</span></div><div class="outcome-options" role="group" aria-label="${esc(t('ui.chooseOutcome'))}">${['YES','NO'].map(outcome=>`<button type="button" class="choice${state.selection===outcome?' selected':''}" data-action="choose" data-outcome="${outcome}" aria-pressed="${state.selection===outcome}"><b>${outcome}</b><span>${outcome==='YES'?t('ui.willHappen'):t('ui.willNotHappen')}</span></button>`).join('')}</div><label class="confidence-label" for="confidence">${esc(t('ui.howConfident'))}<output for="confidence" id="confidence-output">${state.confidence}%</output></label><input type="range" id="confidence" name="confidence" min="0" max="100" step="1" value="${state.confidence}" aria-describedby="cast-summary"><div class="range-labels"><span>0% · ${esc(t('ui.notConfident'))}</span><span>50%</span><span>100% · ${esc(t('ui.certain'))}</span></div><p class="cast-summary" id="cast-summary">${castSummary()}</p><section class="stake-section" id="stake-controls" aria-label="${esc(t('ui.optionalPoints'))}">${stakeMarkup()}</section><p class="error-message" id="cast-error" role="alert"></p><button class="button full" type="submit">${myForecast?t('ui.updateForecast'):t('ui.recordForecast')}${icon('arrow')}</button>${myForecast?`<p class="quiet-note separated-note">${esc(t('ui.currentForecast',{outcome:myForecast.outcome,confidence:formatNumber(myForecast.confidence),date:date(myForecast.submittedAt)}))}</p>`:''}</form>`;
}
function castSummary(){return state.selection?esc(t('ui.castSummary',{outcome:state.selection,confidence:formatNumber(state.confidence),probability:formatNumber(state.selection==='YES'?state.confidence:100-state.confidence)})):esc(t('ui.chooseAndConfidence'));}

function earlyResolutionMarkup(forecast){
  const early=forecast.earlyResolution;if(!early||forecast.finalizedOutcome)return '';
  return `<section class="status-notice early-resolution"><strong>${esc(t('ui.earlyResolution'))}</strong><p>${esc(t('ui.earlyResolutionHint'))}</p>${early.eventTimeBasis==='observed_upper_bound'?`<p>${esc(t('ui.earlyObserved',{date:date(early.observedAt,true)}))}</p>`:''}${(early.sources||[]).map(source=>externalLink(source.url,t('ui.reviewEvidence'))).join(' ')}</section>`;
}
function eligibilityPersonalText(eligibility){
  const personal=eligibility?.personal;
  const keys={eligible:'eligibilityEligible',void:'eligibilityVoid',review:'eligibilityPersonalReview',restored:'eligibilityRestored'};
  if(!personal||!Object.hasOwn(keys,personal.status))return [];
  const lines=[t(`ui.${keys[personal.status]}`)];
  if(personal.adjustmentPending)lines.push(t('ui.eligibilityAdjustmentPending'));
  else if(Number.isSafeInteger(personal.refundedPoints)&&personal.refundedPoints>0)lines.push(t('ui.eligibilityRefunded',{amount:pointCount(personal.refundedPoints)}));
  return lines;
}
function eligibilityMarkup(eligibility){
  if(!eligibility||!['complete','review','pending'].includes(eligibility.status))return '';
  const precise=eligibility.timeBasis==='published_instant'&&Number.isSafeInteger(eligibility.cutoffAt);
  const policy=precise?t('ui.eligibilityCutoff',{date:date(eligibility.cutoffAt,true)}):t('ui.eligibilityUnknownTime');
  const url=safeExternalUrl(eligibility.publishedEvidenceUrl);
  return `<section class="status-notice eligibility-notice" aria-live="polite"><strong>${esc(t('ui.eligibilityTitle'))}</strong><p>${esc(policy)}</p><p>${esc(t('ui.eligibilityPolicy'))}</p>${eligibility.status==='pending'?`<p>${esc(t('ui.eligibilityPending'))}</p>`:''}${eligibilityPersonalText(eligibility).map(line=>`<p>${esc(line)}</p>`).join('')}${url?externalLink(url,t('ui.reviewEvidence')):''}</section>`;
}
function myForecastMarkup(item){
  const personal=item.eligibility?.personal;
  const excluded=['void','review'].includes(personal?.status);
  const choice=excluded?'':`${esc(item.myForecast?.outcome || '')}<small>${esc(t('ui.confidenceValue',{confidence:item.myForecast?.confidence===undefined?'—':formatNumber(item.myForecast.confidence)}))}</small>${item.stake?`<small>${esc(item.stake.amount?`${t('ui.pointsValue',{amount:pointCount(item.stake.amount)})}${item.stake.status==='settled'?` · ${t('ui.returnedValue',{amount:pointCount(item.stake.returned)})}`:''}`:t('ui.practice'))}</small>`:''}`;
  return `<a class="mine-row" href="/forecasts/${encodeURIComponent(item.id)}" data-link><span>${esc(displayForecast(item).title || displayForecast(item).question)}<small>${esc(stateLabel(item.state))}${Number.isSafeInteger(item.myForecast?.submittedAt)?` · ${date(item.myForecast.submittedAt)}`:''}</small>${eligibilityPersonalText(item.eligibility).map(line=>`<small>${esc(line)}</small>`).join('')}</span><span class="mine-choice">${choice}</span></a>`;
}
function marketNumber(value){return formatNumber(value,{maximumFractionDigits:6});}
function marketPriceText(market){
  return Number.isSafeInteger(market.yesProbabilityBps)&&market.yesProbabilityBps>=0&&market.yesProbabilityBps<=10000?`YES ${marketNumber(market.yesProbabilityBps/100)}%`:'YES —';
}
function marketPriceHint(market){
  return t(market.probabilityStatus==='eligibility_review'?'ui.marketEvidenceReview':market.probabilityStatus==='frozen_before_evidence'?'ui.marketEvidenceFrozen':'ui.marketPriceHint');
}
function marketReconciliationMarkup(entry){
  if(entry.phase==='uncertain')return `<p class="quiet-note" role="status">${esc(t(entry.reconciling?'ui.marketCheckingReceipt':'ui.marketUncertain'))}</p>${button(esc(t('ui.marketRetry')),'market-reconcile','secondary small',entry.reconciling?'disabled':'')}`;
  if(entry.phase==='not_accepted')return `<p class="quiet-note" role="status">${esc(t('ui.marketNotAccepted'))}</p>`;
  if(entry.phase==='void')return `<p class="quiet-note" role="status">${esc(t('ui.marketVoidRefund'))}<br>${esc(t('ui.eligibilityRefunded',{amount:pointCount(entry.receipt.refundedPoints)}))}</p>`;
  return '';
}
function marketMarkup(){
  const entry=marketClient.get();if(!entry)return '';
  const {market,quote,receipt,phase}=entry;
  const test=market.mode!=='active',busy=['quoting','filling'].includes(phase),locked=busy||phase==='uncertain';
  const stopped=!forecastIsOpen(state.detail?.forecast)||!['open','OPEN'].includes(market.status)||['eligibility_review','frozen_before_evidence'].includes(market.probabilityStatus)||market.mode==='active'&&!market.liveEnabled;
  const evidenceReview=['eligibility_review','frozen_before_evidence'].includes(market.probabilityStatus);
  const quantity=evidenceReview||phase==='void'?null:receipt||quote;
  return `<section class="market-panel" aria-labelledby="market-heading"><div class="market-heading"><h2 id="market-heading">${esc(t(test?'ui.marketPreview':'ui.marketTitle'))}</h2><span class="market-price">${esc(marketPriceText(market))}</span></div><p class="quiet-note">${esc(t(test?'ui.marketShadowHint':'ui.marketActiveHint'))}</p><p class="input-hint market-price-hint">${esc(marketPriceHint(market))}</p><div id="market-balance">${marketPositionMarkup(entry)}</div>${stopped?`<p class="status-notice">${esc(t('ui.marketStopped'))}</p>`:`<div class="outcome-options" role="group" aria-label="${esc(t('ui.chooseOutcome'))}">${['YES','NO'].map(side=>`<button type="button" class="choice${entry.side===side?' selected':''}" data-action="market-side" data-side="${side}" aria-pressed="${entry.side===side}" ${locked?'disabled':''}>${side}</button>`).join('')}</div><label class="confidence-label" for="market-spend">${esc(t(test?'ui.marketTestSpend':'ui.marketSpend'))}</label><div class="market-input-row"><input class="input" id="market-spend" type="number" inputmode="numeric" min="1" max="${market.maxSpendPoints}" step="1" value="${esc(entry.spendRaw)}" ${locked?'disabled':''}><button type="button" class="button secondary" data-action="market-quote" ${locked?'disabled':''}>${esc(t(phase==='quoting'?'ui.marketQuoting':'ui.marketQuote'))}</button></div>`}<div id="market-review" aria-live="polite">${marketReconciliationMarkup(entry)}${quantity?`<dl class="market-quote"><div><dt>${esc(t('ui.marketTotal'))}</dt><dd>${marketNumber(quantity.spendPoints)}</dd></div><div><dt>${esc(t('ui.marketMove'))} · ${esc(quantity.side)}</dt><dd>${marketNumber(quantity.priceBeforeBps/100)}% → ${marketNumber(quantity.priceAfterBps/100)}%</dd></div><div class="market-return"><dt>${esc(t('ui.marketReturn'))}</dt><dd>${marketNumber(claimsInPoints(quantity.claimsAtomic))}</dd></div></dl><p class="input-hint">${esc(t('ui.marketSettlement'))}</p>`:''}${!evidenceReview&&receipt?.status==='accepted'?`<p class="market-receipt"><strong>${esc(t(test?'ui.marketTestReceipt':'ui.marketReceipt'))}</strong><br>${esc(t('ui.marketReceiptHint'))}</p>`:!evidenceReview&&quote&&phase!=='uncertain'&&phase!=='void'?`${entry.userId?`<p class="quiet-note">${esc(t('ui.marketExpiry',{date:date(quote.expiresAt,true)}))}</p>${phase==='uncertain'?`<p role="alert">${esc(t('ui.marketUncertain'))}</p>`:''}<button type="button" class="button full" data-action="market-fill" ${stopped||!['quoted','uncertain'].includes(phase)?'disabled':''}>${esc(t(phase==='filling'?'ui.marketFilling':phase==='uncertain'?'ui.marketRetry':test?'ui.marketConfirmTest':'ui.marketConfirm'))}</button>`:`<p class="quiet-note">${esc(t('ui.marketAnonymous'))}</p>${button(esc(t('ui.signInPoints')),'market-login','secondary')}`}`:''}${entry.error?`<p class="error-message" role="alert">${esc(errorText(entry.error))}</p>`:''}</div></section>`;
}
function marketPositionMarkup(entry){
  const position=entry?.position;if(!position)return '';
  const balance=entry.market.mode==='shadow'&&Number.isSafeInteger(position.availablePoints)?esc(t('ui.marketBalance',{amount:formatNumber(position.availablePoints)})):'';
  const refund=Number.isSafeInteger(position.voidedFillCount)&&position.voidedFillCount>0?`<p>${esc(t('ui.marketVoidRefund'))}${Number.isSafeInteger(position.refundedPoints)&&position.refundedPoints>0?`<br>${esc(t('ui.eligibilityRefunded',{amount:pointCount(position.refundedPoints)}))}`:''}</p>`:'';
  const claims=['YES','NO'].map(side=>{const raw=position[side==='YES'?'yesClaimsAtomic':'noClaimsAtomic'];if(typeof raw!=='string'||!/^\d{1,15}$/.test(raw)||BigInt(raw)===0n)return '';return `${side}: ${marketNumber(claimsInPoints(raw))}`;}).filter(Boolean);
  return `${balance}${refund}${claims.length?`<p>${esc(t('ui.marketHeldReturns'))}<br><strong>${esc(claims.join(' · '))}</strong></p>`:''}`;
}
function paintMarket(){
  const node=document.querySelector('#market-panel'),entry=marketClient.get();
  if(!node||entry?.market.forecastId!==state.detail?.forecast?.id||entry?.userId!==(state.user?.id||null))return;
  node.innerHTML=marketMarkup();
  if(['filled','void','not_accepted'].includes(entry.phase))void loadMarketBalance(state.sequence,state.user.id);
}
async function loadMarketBalance(sequence,owner){
  try{
    const current=marketClient.get();if(!current)return;
    const [data,latest]=await Promise.all([api('/api/me/markets'),api(`/api/forecasts/${encodeURIComponent(current.market.forecastId)}/market`)]);
    if(sequence!==state.sequence||owner!==state.user?.id)return;
    if(marketClient.get()!==current)return;
    const fresh=latest?.market??latest;
    if(fresh){validateMarket(fresh,current.market.forecastId);if(fresh.mode===current.market.mode&&fresh.specificationHash===current.market.specificationHash){current.market=fresh;const price=document.querySelector('.market-price');if(price)price.textContent=marketPriceText(fresh);const hint=document.querySelector('.market-price-hint');if(hint)hint.textContent=marketPriceHint(fresh);if(['eligibility_review','frozen_before_evidence'].includes(fresh.probabilityStatus)){const panel=document.querySelector('#market-panel');if(panel)panel.innerHTML=marketMarkup();}}}
    const positions=Array.isArray(data)?data:data.positions||[];
    const entry=marketClient.get(),position=positions.find(item=>item.forecastId===entry?.market.forecastId&&item.mode===entry?.market.mode);
    const node=document.querySelector('#market-balance');
    if(node&&position){entry.position=position;node.innerHTML=marketPositionMarkup(entry);}
    if(entry?.market.mode==='active'&&['filled','void','not_accepted'].includes(entry.phase))void loadPoints();
    if(entry?.phase==='uncertain')await marketClient.reconcile();
  }catch{/* Market balance is supplementary; an authoritative quote/fill still enforces it. */}
}

function historyMarkup(history){
  const chart=makeHistoryPath(history || []);
  if(!chart)return empty(t('ui.noHistory'),t('ui.noHistoryHint'),'','chart',true);
  return `<svg class="chart" viewBox="0 0 500 170" role="img" aria-label="${esc(t('ui.historyChart',{count:formatNumber(history.length)}))}"><line x1="20" y1="20" x2="480" y2="20" stroke="#e7eaf0"/><line x1="20" y1="85" x2="480" y2="85" stroke="#e7eaf0" stroke-dasharray="3 5"/><line x1="20" y1="150" x2="480" y2="150" stroke="#e7eaf0"/><text x="0" y="23">100</text><text x="2" y="88">50</text><text x="7" y="153">0</text><path d="${chart.path}" fill="none" stroke="#2456ed" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${chart.points.length===1?`<circle cx="${chart.points[0].x}" cy="${chart.points[0].y}" r="4" fill="#2456ed"/>`:''}</svg><div class="chart-labels"><span>${date(chart.start,true)}</span><span>${date(chart.end,true)}</span></div>`;
}
function resolutionMarkup(forecast,resolution,disputes){
  if(!resolution)return `<section class="detail-section"><h2>${esc(t('ui.outcomeEvidence'))}</h2><p>${forecastIsOpen(forecast)?t('ui.resolutionNext'):t('ui.noProposal')}</p></section>`;
  const evidence=resolution.evidence || [];
  const reviewers=(resolution.providers || []).map(provider=>`${provider.provider} · ${provider.model}${provider.modelVersion ? ` (${provider.modelVersion})` : ''}`);
  const provenance=reviewers.length?`<details class="technical-details"><summary>${esc(t('ui.reviewProvenance'))}</summary><p class="quiet-note">${reviewers.map(esc).join(`<br>`)}</p>${resolution.reviewedAt?`<p class="quiet-note">${esc(t('ui.reviewedAt',{date:date(resolution.reviewedAt,true)}))}</p>`:''}${resolution.hash?`<p class="commitment">${esc(t('ui.resolutionHash'))}<br>${esc(resolution.hash)}</p>`:''}</details>`:'';
  return `<section class="detail-section"><h2>${forecast.finalizedOutcome?t('ui.finalOutcome'):t('ui.proposedOutcome')}</h2><div class="status-notice"><strong>${esc(resolution.proposedOutcome || resolution.outcome || '')}${probability(resolution.confidence)!==null?` · ${esc(t('ui.reviewConfidence',{confidence:formatNumber(Math.round(resolution.confidence))}))}`:''}</strong><p>${esc(resolution.reasonSummary || t('ui.reviewEvidenceHint'))}</p></div>${provenance}${evidence.length?`<span class="inline-label">${esc(t('ui.usedEvidence'))}</span><ul class="sources">${evidence.map(item=>`<li>${externalLink(item.url || item.sourceUrl,item.title || item.name || item.sourceName)}${item.summary?`<p class="quiet-note">${esc(item.summary)}</p>`:''}${item.hash?`<details class="technical-details"><summary>${esc(t('ui.evidenceCommitment'))}</summary><p class="commitment">${esc(item.hash)}</p>${item.collectedAt?`<p class="quiet-note">${esc(t('ui.collectedAt',{date:date(item.collectedAt,true)}))}</p>`:''}</details>`:''}</li>`).join('')}</ul>`:''}${forecast.challengeUntil?`<p class="quiet-note">${esc(t('ui.challengeDeadline',{date:date(forecast.challengeUntil,true)}))}</p>`:''}${canChallenge(forecast)?`<details class="technical-details"><summary>${esc(t('ui.submitDisputePrompt'))}</summary><form id="dispute-form" class="dispute-form"><div class="form-field"><label for="dispute-claim">${esc(t('ui.yourClaim'))}</label><input class="input" id="dispute-claim" name="claim" required maxlength="500" placeholder="${esc(t('ui.reconsiderPlaceholder'))}"></div><div class="form-field"><label for="dispute-url">${esc(t('ui.evidenceUrl'))}</label><input class="input" type="url" id="dispute-url" name="evidenceUrl" required maxlength="2000" placeholder="https://..."></div><div class="form-field"><label for="dispute-clause">${esc(t('ui.relevantClause'))}</label><select class="input" id="dispute-clause" name="ruleClauseId" required>${(forecast.specification?.rules||[]).map(rule=>`<option value="${esc(rule.clauseId)}">${esc(rule.outcome)} · ${esc(rule.clauseId)}</option>`).join('')}</select></div><div class="form-field"><label for="dispute-explanation">${esc(t('ui.whyEvidenceChanges'))}</label><textarea class="input" id="dispute-explanation" name="explanation" required maxlength="5000"></textarea></div><p class="error-message" id="dispute-error" role="alert"></p><button class="button" type="submit">${esc(t('ui.submitEvidence'))}</button></form></details>`:''}${disputes?.length?`<span class="inline-label">${esc(t('ui.submittedDisputes',{count:formatNumber(disputes.length)}))}</span>${disputes.map(dispute=>`<div class="comment"><strong>${esc(dispute.claim)}</strong><p>${esc(dispute.explanation)}</p>${(dispute.evidence || (dispute.evidenceUrl?[{url:dispute.evidenceUrl}]:[])).map(item=>externalLink(item.url,t('ui.submittedEvidence'))).join('')}<p class="quiet-note">${esc(dispute.review ? (dispute.review.materialConflict?t('ui.materialConflict'):t('ui.independentReviewDone')) : t('ui.underReview'))}${dispute.review?.reasonSummary?`<br>${esc(dispute.review.reasonSummary)}`:''}</p></div>`).join('')}`:''}</section>`;
}
function attestationMarkup(forecast,attestation,myForecast){
  if(!attestation||!state.user||!myForecast||!attestation.available||!nativePluginAvailable())return '';
  if(attestation.status==='submitted'||attestation.status==='verified'){
    return `<section class="detail-section" id="attestation"><h2>${esc(t('ui.attestTitle'))}</h2><p class="status-notice"><strong>${esc(t('ui.attestStamped'))}</strong></p><p class="quiet-note">${esc(t('ui.attestMemo'))}</p><p class="commitment">${esc(attestation.memo||'')}</p><a class="link-arrow" href="${esc(attestation.explorer)}" target="_blank" rel="noopener noreferrer">${esc(t('ui.attestViewExplorer'))} ${icon('external')}</a></section>`;
  }
  return `<section class="detail-section" id="attestation"><h2>${esc(t('ui.attestTitle'))}</h2><p class="quiet-note">${esc(t('ui.attestHint'))}</p><button class="button secondary small" data-action="attest" data-id="${esc(forecast.id)}">${esc(t('ui.attestAction'))} ${icon('arrow')}</button><p class="error-message" id="attest-error" role="alert"></p></section>`;
}
function evidenceReportMarkup(forecast,reports){
  if(!forecastIsOpen(forecast)&&forecast.state!=='LOCKED')return '';
  const mine=reports?.mine;
  const status=mine==='held'?t('ui.evidenceReportHeld'):mine==='rewarded'?t('ui.evidenceReportRewarded'):mine==='unrelated'?t('ui.evidenceReportUnrelated'):mine==='received'?t('ui.evidenceReportReceived'):'';
  return `<section class="detail-section" id="evidence-report"><h2>${esc(t('ui.evidenceReportTitle'))}</h2><p class="quiet-note">${esc(t('ui.evidenceReportHint',{reward:formatNumber(reports?.reward||100)}))}</p>${status?`<p class="status-notice"><strong>${esc(status)}</strong></p>`:''}<form class="comment-form" id="evidence-form"><label class="sr-only" for="evidence-url">${esc(t('ui.evidenceReportUrl'))}</label><input class="input" id="evidence-url" name="url" type="url" inputmode="url" placeholder="${esc(t('ui.evidenceReportPlaceholder'))}" required maxlength="2048"><p class="error-message" id="evidence-error" role="alert"></p><button class="button secondary small" type="submit">${esc(t('ui.evidenceReportSubmit'))} ${icon('arrow')}</button></form>${reports?.count?`<p class="quiet-note">${esc(t('ui.evidenceReportCount',{count:formatNumber(reports.count)}))}</p>`:''}</section>`;
}
function commentsMarkup(comments){return `${(comments||[]).map(comment=>`<article class="comment"><div class="comment-meta">${avatar(comment.user?.displayName || comment.author?.displayName || comment.displayName)}<strong>${esc(comment.user?.displayName || comment.author?.displayName || comment.displayName || t('ui.participant'))}</strong><time datetime="${Number.isFinite(comment.createdAt)?new Date(comment.createdAt).toISOString():''}">${relative(comment.createdAt)}</time></div><p>${esc(comment.text)}</p></article>`).join('') || `<p class="quiet-note">${esc(t('ui.noComments'))}</p>`}`;}
function detailMarkup(data){
  const {resolution,disputes=[],audit=[],comments=[],myForecast,history=[]}=data;
  const forecast=displayForecast(data.forecast,data.displayTranslation);
  forecastTranslations.register(forecast);
  return `<div class="narrow-page"><a class="back-link" href="/" data-link>${icon('back')}${esc(t('ui.allForecasts'))}</a><div class="row-meta"><span class="category">${esc(categoryLabel(forecast.category))}</span>${stateBadge(forecast)}</div><h1 class="detail-heading">${translationText(forecast,forecast.question?'question':'title',forecast.question || forecast.title)}</h1>${translationControl(forecast)}${forecast.translationLanguage==='en'&&forecast.sourceLanguage==='ko'?`<p class="quiet-note translation-notice">${esc(t('ui.translatedKorean'))}</p>`:''}<div class="detail-author"><a class="creator" href="/creators/${encodeURIComponent(forecast.creator?.id || '')}" data-link>${avatar(forecast.creator?.displayName)}<span>${esc(forecast.creator?.displayName || t('ui.creator'))}<small>${esc(t('ui.publishedCloses',{published:date(forecast.createdAt),closes:date(forecast.closeAt,true)}))}</small><small>${esc(t('ui.deviceTimezone'))}</small></span></a><div class="detail-actions">${button(icon('share'),'share','quiet',`data-id="${esc(forecast.id)}" data-title="${esc(forecast.title || forecast.question)}" aria-label="${esc(t('ui.shareForecast'))}"`)}</div></div>${comparisonMarkup(forecast)}${forecast.ai?.rationale?`<details class="technical-details ai-rationale"><summary>${esc(t('ui.whyAiEstimate'))}</summary><p class="quiet-note">${translationText(forecast,'aiRationale',forecast.ai.rationale)}</p>${forecast.ai.rationaleAttribution?`<p class="quiet-note">${esc(forecast.ai.rationaleAttribution)}</p>`:''}</details>`:''}${earlyResolutionMarkup(forecast)}${eligibilityMarkup(data.eligibility)}<div id="market-panel">${marketMarkup()}</div><div id="cast">${castMarkup(forecast,myForecast)}</div><section class="detail-section"><h2>${esc(t('ui.forecastsChanged'))}</h2>${historyMarkup(history)}${data.historyTruncated?`<p class="quiet-note">${esc(t('ui.latestObservations'))}</p>`:''}</section>${specificationMarkup(forecast.specification,forecast)}${resolutionMarkup(forecast,resolution,disputes)}${attestationMarkup(forecast,data.attestation,myForecast)}${evidenceReportMarkup(forecast,data.evidenceReports)}<section class="detail-section"><h2>${esc(t('ui.publicAudit'))}</h2>${data.auditTruncated?`<p class="quiet-note">${esc(t('ui.latestStateChanges'))}</p>`:''}${audit.length?`<ol class="timeline">${audit.map(event=>`<li><div><strong>${esc(commandLabel(event.command,event.newState))}</strong><small>${date(event.at,true)}${event.oldState&&event.newState?` · ${esc(stateLabel(event.oldState))} → ${esc(stateLabel(event.newState))}`:''}</small>${event.artifactHash?`<details class="technical-details"><summary>${esc(t('ui.viewRecordHash'))}</summary><p class="commitment">${esc(event.artifactHash)}</p></details>`:''}</div></li>`).join('')}</ol>`:`<p class="quiet-note">${esc(t('ui.noAudit'))}</p>`}<details class="technical-details"><summary>${esc(t('ui.originalRules'))}</summary><p class="quiet-note">${esc(chainText(forecast.chain))}</p><p class="commitment">${esc(t('ui.specificationHash'))}<br>${esc(forecast.specificationHash || t('ui.pending'))}</p><a class="link-arrow" href="/api/forecasts/${encodeURIComponent(forecast.id)}/integrity" target="_blank" rel="noopener noreferrer">${esc(t('ui.viewOriginal'))} ${icon('external')}</a></details></section><section class="detail-section" id="comments"><h2>${esc(t('ui.discussion'))} <span class="quiet-note">${comments.length}</span></h2><div id="comment-list">${commentsMarkup(comments)}</div><form class="comment-form" id="comment-form"><label class="sr-only" for="comment-text">${esc(t('ui.yourComment'))}</label><textarea class="input" id="comment-text" name="text" placeholder="${esc(t('ui.commentPlaceholder'))}" required minlength="1" maxlength="2000"></textarea><p class="error-message" id="comment-error" role="alert"></p><button class="button small" type="submit">${esc(t('ui.postComment'))} ${icon('arrow')}</button></form></section></div>`;
}
function chainText(chain){const transaction=chain?.transactionId || chain?.transaction || chain?.signature;if(chain?.status==='confirmed' && transaction)return t('ui.chainConfirmed',{network:chain.network || chain.cluster || '',transaction});return t('ui.chainNotConnected');}
async function renderDetail(sequence,preserve=false,cached=null){
  main().innerHTML=`<div class="narrow-page">${skeleton()}</div>`;
  const ownerAtRead=state.user?.id;const epochAtRead=pointsState.epoch;
  try{
    const data=cached || await api(`/api/forecasts/${encodeURIComponent(routeId())}`);
    await authLoaded;if(sequence!==state.sequence)return;
    if(epochAtRead!==pointsState.epoch||(ownerAtRead&&ownerAtRead!==state.user?.id)){void renderRoute();return;}
    const owner=state.user?.id || null;
    if(data.points?.userId && data.points.userId!==owner){data.points=null;data.stake=null;data.myForecast=null;pointsState.snapshot=null;pointsState.accountChanged=true;pointsState.error=t('ui.pointsAccountChanged');}
    state.detail=data;
    if(data.points)acceptPoints(data.points,owner);
    if(!preserve){const selected=new URLSearchParams(location.search).get('choose');state.selection=['YES','NO'].includes(selected)?selected:data.myForecast?.outcome || null;state.confidence=data.myForecast?.confidence ?? 70;}
    if(!preserve||state.stakeForecastId!==data.forecast.id||state.stakeOwner!==owner){
      state.stakeOwner=owner;state.stakeForecastId=data.forecast.id;state.stakeDirty=false;
      state.stakeRaw=String(data.myForecast?(data.stake?.amount ?? 0):(pointStakeLimit(currentPoints(),owner,data.stake)>=50?50:0));
    }
    marketClient.attach(data.market?.market??data.market,owner);
    main().innerHTML=detailMarkup(data);
    if(data.market&&owner)void loadMarketBalance(sequence,owner);
    document.title=`${displayForecast(data.forecast,data.displayTranslation).title || displayForecast(data.forecast,data.displayTranslation).question} — Forecast`;
    scrollToHash();if(owner&&!currentPoints()&&!pointsState.accountChanged)void loadPoints();
  }catch(error){if(sequence===state.sequence)main().innerHTML=`<div class="narrow-page">${errorPanel(error)}</div>`;}
}
function renderCreate(){
  main().innerHTML=`<div class="narrow-page"><div class="page-heading"><div><h1>${esc(t('ui.createQuestion'))}</h1><p>${esc(t('ui.createSubtitle'))}</p></div></div><form class="create-form" id="create-form"><div class="form-field"><label class="sr-only" for="question">${esc(t('ui.forecastQuestion'))}</label><textarea class="input question-input" id="question" name="question" maxlength="1200" minlength="12" required placeholder="${esc(t('ui.questionPlaceholder'))}">${esc(state.question)}</textarea><div class="field-row"><span>${esc(t('ui.questionTimingHint'))}</span><span id="question-count">${state.question.length} / 1,200</span></div></div><p class="error-message" id="create-error" role="alert"></p><div class="create-actions"><p>${esc(t('ui.aiChecksCriteria'))}<br>${esc(t('ui.confirmBeforePublish'))}</p><button class="button" type="submit">${icon('spark')}${esc(t('ui.refineAi'))}</button></div></form><div id="draft-preview">${state.draft?previewMarkup(state.draft):''}</div><section class="create-hints"><h2>${esc(t('ui.goodQuestion'))}</h2><button class="question-example" data-action="example" data-question="${esc(t('ui.lunarExampleInput'))}">${esc(t('ui.lunarExample'))} ${icon('arrow')}</button><button class="question-example" data-action="example" data-question="${esc(t('ui.phoneExampleInput'))}">${esc(t('ui.phoneExample'))} ${icon('arrow')}</button><p class="quiet-note separated-note">${esc(t('ui.examplesNote'))}</p></section></div>`;
}
function previewMarkup(draft){
  const spec=draft.specification;
  if(!spec)return '';
  const duplicates=draft.duplicateCandidates || [];
  const issues=draft.assessment?.issues || draft.assessment?.reasons || [];
  const blocked=draft.assessment?.publishable===false || draft.assessment?.accepted===false;
  return `<div class="preview-heading"><h2>${esc(t('ui.reviewBeforePublish'))}</h2><span class="category">${esc(categoryLabel(spec.category))}</span></div><div class="preview"><h3 lang="en">${esc(spec.canonicalQuestion)}</h3><div class="preview-time"><div><span>${esc(t('ui.opens'))}</span>${date(spec.openAt,true)}</div><div><span>${esc(t('ui.closes'))}</span>${date(spec.closeAt,true)}</div></div>${specificationMarkup(spec)}${duplicates.length?`<section class="detail-section"><h2>${esc(t('ui.similarQuestions'))}</h2>${duplicates.map(item=>`<a class="mine-row" href="/forecasts/${encodeURIComponent(item.id || item.forecastId)}" data-link><span>${esc(item.title || item.question || item.canonicalQuestion || t('ui.viewExisting'))}</span>${icon('arrow')}</a>`).join('')}<p class="quiet-note">${esc(t('ui.joinExisting'))}</p></section>`:''}${draft.assessment?.explanation?`<details class="technical-details"><summary>${esc(t('ui.aiReviewNotes'))}</summary><p class="quiet-note">${esc(draft.assessment.explanation)}</p><p class="quiet-note">${esc(draft.assessment.provider || '')} · ${esc(draft.assessment.model || '')}</p></details>`:''}${issues.length?`<div class="preview-status">${issues.map(issue=>esc(typeof issue==='string'?issue:issue.message || issue.description || '')).join(`<br>`)}</div>`:''}<form id="publish-form"><label class="check-label"><input type="checkbox" name="confirmed" required ${blocked?'disabled':''}><span>${esc(t('ui.publishAcknowledgment'))}</span></label><p class="error-message" id="publish-error" role="alert"></p><button class="button full" type="submit" ${blocked?'disabled':''}>${blocked?t('ui.reviseReview'):t('ui.publishRules')}${icon('arrow')}</button><p class="quiet-note separated-note">${esc(t('ui.reviewExpires',{date:date(draft.expiresAt,true)}))}</p></form></div>`;
}
async function renderActivity(sequence){
  main().innerHTML=`<div class="narrow-page"><div class="page-heading"><div><h1>${esc(t('ui.yourActivity'))}</h1><p>${esc(t('ui.followOutcomes'))}</p></div>${state.user?button(esc(t('ui.markAllRead')),'read-activity','quiet small'):''}</div><div id="activity-list">${skeleton()}</div></div>`;
  await authLoaded;if(sequence!==state.sequence)return;
  if(!state.user){document.querySelector('#activity-list').innerHTML=empty(t('ui.stayStory'),t('ui.activitySignup'),button(esc(t('ui.createMyProfile')),'account',''),'bell');return;}
  try{const data=await api('/api/activity');if(sequence!==state.sequence)return;document.querySelector('#activity-list').innerHTML=(data.items||[]).map(item=>`<article class="activity-row"><span class="activity-symbol">${icon(item.kind?.includes('DISPUT')?'flag':item.kind?.includes('FINAL')?'check':'bell')}</span><div class="activity-content"><h2>${item.forecastId?`<a href="/forecasts/${encodeURIComponent(item.forecastId)}" data-link>${esc(item.title)}</a>`:esc(item.title)}${!item.readAt?`<span class="category new-activity">${esc(t('ui.new'))}</span>`:''}</h2><p>${esc(item.body)}</p><time>${date(item.createdAt,true)}</time></div></article>`).join('') || empty(t('ui.caughtUp'),t('ui.activityEmpty'),`<a href="/explore" class="button" data-link>${esc(t('ui.exploreForecasts'))} ${icon('arrow')}</a>`,'bell');}catch(error){if(sequence===state.sequence)document.querySelector('#activity-list').innerHTML=errorPanel(error);}
}
async function renderProfile(sequence,cached=null){
  main().innerHTML=`<div class="narrow-page">${skeleton()}</div>`;await authLoaded;if(sequence!==state.sequence)return;
  if(!state.user){main().innerHTML=`<div class="narrow-page"><div class="page-heading"><div><h1>${esc(t('ui.yourTrackRecord'))}</h1><p>${esc(t('ui.confidenceReality'))}</p></div></div>${empty(t('ui.startRecord'),t('ui.profileSignupHint'),button(esc(t('ui.createProfile')),'account',''),'user')}<section class="detail-section"><h2>${esc(t('ui.buildReputation'))}</h2><p>${esc(t('ui.reputationExplanation'))}</p></section></div>`;return;}
  const ownerAtRead=state.user.id;const epochAtRead=pointsState.epoch;
  try{const data=cached || await api('/api/me');if(sequence!==state.sequence)return;if(ownerAtRead!==state.user?.id||epochAtRead!==pointsState.epoch){void renderRoute();return;}if(state.user?.id!==data.user?.id){resetWallet();resetPoints();}state.me=data;state.user=data.user;if(!data.user){void renderRoute();return;}if(data.points)acceptPoints(data.points,data.user.id);const rep=data.reputation || {};main().innerHTML=`<div class="narrow-page"><div class="profile-head">${avatar(data.user.displayName,true)}<div><h1>${esc(data.user.displayName)}</h1><p>@${esc(data.user.handle)} · ${esc(t('ui.joined',{date:date(data.user.createdAt)}))}</p></div><div class="profile-head-actions">${button(`${icon('share')}${esc(t('ui.shareMyRecord'))}`,'share-profile','small')}<a href="/creators/${encodeURIComponent(data.user.id)}" data-link class="button quiet small">${esc(t('ui.publicProfile'))} ${icon('arrow')}</a></div></div><div class="profile-stats"><div><strong>${Number(rep.resolvedForecasts)||0}</strong><span>${esc(t('ui.resolvedForecasts'))}</span></div><div><strong>${percentage(rep.accuracy)}</strong><span>${esc(t('ui.accuracy'))}</span></div><div><strong>${Number.isFinite(rep.brierScore)?formatNumber(rep.brierScore,{minimumFractionDigits:3,maximumFractionDigits:3}):'—'}</strong><span>${esc(t('ui.brierLower'))}</span></div></div><p class="quiet-note">${esc(t('ui.scoresMissing'))}</p><section class="profile-section" id="points-section"><h2>${esc(t('ui.points'))}</h2><div id="points-content">${pointsSummaryMarkup()}</div></section><section class="profile-section"><h2>${esc(t('ui.myForecasts'))}</h2>${(data.myForecasts||[]).map(myForecastMarkup).join('') || empty(t('ui.noForecasts'),t('ui.chooseQuestionHint'),'','question',true)}</section><section class="profile-section"><h2>${esc(t('ui.profileSettings'))}</h2><form class="profile-form" id="profile-form"><div class="form-field"><label for="display-name">${esc(t('ui.displayName'))}</label><input class="input" id="display-name" name="displayName" value="${esc(data.user.displayName)}" required minlength="2" maxlength="40" autocomplete="nickname"></div><button class="button secondary" type="submit">${esc(t('ui.save'))}</button></form><p class="error-message" id="profile-error" role="alert"></p></section><section class="profile-section" id="wallet-section"><h2>${esc(t('ui.wallet'))}</h2><div id="wallet-content"><p class="loading-label">${esc(t('ui.loadingWallet'))}</p></div></section><section class="profile-section">${profileAuthenticationMarkup()}</section></div>`;updateAccount();if(cached)renderWallet();else void loadWallet(sequence);if(!currentPoints())void loadPoints();}catch(error){if(sequence===state.sequence)main().innerHTML=`<div class="narrow-page">${errorPanel(error)}</div>`;}
}
function publishedRecordMarkup(record){
  const m=record.metrics;
  const locale=getLocale();const tr=(key,params={})=>t(key,params,locale);
  return `<section class="published-record" aria-labelledby="published-record-title"><div class="record-heading"><div><p class="eyebrow">${esc(tr('ui.publicRecord'))}</p><h2 id="published-record-title">${esc(tr('ui.recordSnapshot'))}</h2></div><time>${esc(date(record.asOf,true))}</time></div><div class="published-card-holder"><canvas id="published-profile-card" role="img" aria-label="${esc(record.user.displayName)}. ${esc(profileCaption(record,locale))}"></canvas></div><p class="quiet-note record-caption">${esc(profileCaption(record,locale))}</p><details class="record-evidence"><summary>${esc(tr('ui.resultsBehindCard'))}</summary><p>${esc(tr('ui.allTimeTotals',{correct:formatNumber(m.correctForecasts,{},locale),resolved:formatNumber(m.resolvedForecasts,{},locale),invalid:formatNumber(m.invalidForecasts,{},locale)}))} ${record.sampleStatus==='provisional'?esc(tr('ui.earlyRecord')):''}</p>${record.history.length?`<ol class="record-history">${record.history.map(item=>`<li><span class="record-result ${item.correct===true?'is-correct':item.correct===false?'is-incorrect':''}">${esc(tr(item.correct===true?'card.correct':item.correct===false?'card.miss':item.resolvedOutcome==='INVALID'?'card.invalid':'card.unscored'))}</span><a href="/forecasts/${encodeURIComponent(item.forecastId)}" data-link>${esc(item.title)}</a><small>${esc(tr('ui.called',{outcome:item.outcome,confidence:formatNumber(item.confidence,{},locale),date:date(item.finalizedAt)}))}</small></li>`).join('')}</ol><p class="quiet-note">${esc(record.historyTruncated?tr('ui.latestScored',{count:formatNumber(record.history.length,{},locale)}):tr('ui.allScoredListed'))}</p>`:`<p>${esc(tr(m.resolvedForecasts?'card.noFinalized':'ui.noScoredCard'))}</p>`}<p>${esc(tr('ui.standoutMethod'))}</p><p>${esc(tr('ui.snapshotNotChain'))}</p><a class="link-arrow" href="/api/profile-cards/${encodeURIComponent(record.snapshotHash)}" target="_blank" rel="noopener noreferrer">${esc(tr('ui.viewRetainedRecord'))} ${icon('external')}</a></details></section>`;
}
async function renderCreator(sequence){
  main().innerHTML=`<div class="narrow-page">${skeleton()}</div>`;
  try{
    const data=await api(`/api/creators/${encodeURIComponent(routeId())}`);
    if(sequence!==state.sequence)return;
    const creator=data.creator;
    const name=creator.displayName || creator.user?.displayName || t('ui.creator');
    const rep=creator.reputation || creator;
    let record=null,recordError=null;
    const recordHash=new URLSearchParams(location.search).get('record');
    if(recordHash){
      try{
        if(!/^[0-9a-f]{64}$/.test(recordHash))throw uiError('ui.recordLinkInvalid');
        const snapshot=await api(`/api/profile-cards/${recordHash}`);
        if(sequence!==state.sequence)return;
        record=await verifyProfileRecord(snapshot);
        if(record.user.id!==(creator.id || routeId()))throw uiError('ui.recordOtherProfile');
      }catch(error){record=null;recordError=error;}
    }
    if(sequence!==state.sequence)return;
    const own=state.user?.id===(creator.id || routeId());
    main().innerHTML=`<div class="narrow-page"><a href="/explore" class="back-link" data-link>${icon('back')}${esc(t('ui.exploreForecasts'))}</a><div class="profile-head">${avatar(name,true)}<div><h1>${esc(name)}</h1><p>@${esc(creator.handle || creator.user?.handle || '')}</p></div>${own?button(`${icon('share')}${esc(t('ui.shareMyRecord'))}`,'share-profile','small'):button(data.isFollowing?t('ui.following'):`${icon('plus')}${esc(t('ui.follow'))}`,'follow',data.isFollowing?'secondary':'',`data-id="${esc(creator.id || routeId())}" data-following="${Boolean(data.isFollowing)}"`)}</div>${record?publishedRecordMarkup(record):recordError?`<section class="feed-error" role="alert"><h2>${esc(t('ui.recordUnavailable'))}</h2><p>${esc(errorText(recordError))}</p><a href="/creators/${encodeURIComponent(creator.id || routeId())}" data-link>${esc(t('ui.viewCurrentProfile'))}</a></section>`:''}${record?`<h2 class="current-profile-heading">${esc(t('ui.currentProfile'))}</h2>`:''}<div class="profile-stats"><div><strong>${Number(creator.marketsCreated ?? creator.forecastsCreated ?? creator.createdCount ?? data.forecasts?.length)||0}</strong><span>${esc(t('ui.questionsCreated'))}</span></div><div><strong>${Number(creator.followerCount)||0}</strong><span>${esc(t('ui.followers'))}</span></div><div><strong>${percentage(rep.accuracy)}</strong><span>${esc(t('ui.accuracy'))}</span></div></div><section><div class="page-heading"><h2>${esc(t('ui.creatorQuestions'))}</h2></div>${(data.forecasts||[]).map(forecastCard).join('') || empty(t('ui.noPublishedQuestions'),t('ui.newQuestionsHint'),'','question',true)}</section></div>`;
    document.title=t('ui.creatorTitle',{name});
    if(record){
      await loadCardFonts();
      if(sequence!==state.sequence)return;
      renderProfileCard(document.querySelector('#published-profile-card'),record,{format:'landscape',theme:'paper'});
    }
  }catch(error){if(sequence===state.sequence)main().innerHTML=`<div class="narrow-page">${errorPanel(error)}</div>`;}
}
function resetPoints(){
  pointsState.epoch+=1;pointsState.request+=1;
  Object.assign(pointsState,{snapshot:null,loading:false,error:'',accountChanged:false});
  Object.assign(state,{stakeRaw:null,stakeDirty:false,stakeOwner:null,stakeForecastId:null});
}
function currentPoints(){return pointsForUser(pointsState.snapshot,state.user?.id);}
function acceptPoints(snapshot,owner=state.user?.id){
  if(owner!==state.user?.id)return false;
  const points=pointsForUser(snapshot,owner);if(!points)return false;
  pointsState.request+=1;pointsState.snapshot=points;pointsState.loading=false;pointsState.error='';pointsState.accountChanged=false;
  renderPointsViews();return true;
}
async function loadPoints(){
  const owner=state.user?.id;if(!owner)return null;
  const epoch=pointsState.epoch;const request=++pointsState.request;
  const current=()=>owner===state.user?.id&&epoch===pointsState.epoch&&request===pointsState.request;
  pointsState.loading=true;pointsState.error='';renderPointsViews();
  try{
    const response=await api('/api/points');if(!current())return null;
    const points=pointsForUser(response,owner);
    if(!points){pointsState.snapshot=null;pointsState.accountChanged=response?.userId!==owner;throw new Error(pointsState.accountChanged?t('ui.pointsAccountChanged'):t('ui.pointsUnavailable'));}
    pointsState.snapshot=points;pointsState.accountChanged=false;return points;
  }catch(error){if(current())pointsState.error=errorText(error);return null;}
  finally{if(current()){pointsState.loading=false;renderPointsViews();}}
}
function pointCount(value){return Number.isSafeInteger(value)?formatNumber(value):'—';}
function currentStake(){return state.stakeOwner===state.user?.id&&state.stakeForecastId===state.detail?.forecast?.id?state.detail?.stake:null;}
function stakeExplanation(raw,points){
  if(!/^\d+$/.test(String(raw)))return t('ui.wholePoints');
  const amount=Number(raw);
  if(amount>0&&points)return t('ui.stakeReturns',{amount:pointCount(amount*points.policy.winReturnMultiplier)});
  return t('ui.practiceExplanation');
}
function stakePositionMarkup(position=currentStake()){
  if(!position||!Number.isSafeInteger(position.amount)||position.amount<0)return '';
  if(position.amount===0)return `<p class="quiet-note stake-position">${esc(t('ui.practicePosition'))}</p>`;
  const returned=Number.isSafeInteger(position.returned)?t('ui.pointsReturned',{amount:pointCount(position.returned)}):t('ui.returnPending');
  return `<p class="quiet-note stake-position">${position.status==='settled'?esc(t('ui.settledPosition',{returned,amount:pointCount(position.amount)})):esc(t('ui.committedPosition',{amount:pointCount(position.amount)}))}</p>`;
}
function stakeMarkup(){
  if((state.detail?.market?.market??state.detail?.market)?.mode==='active')return `<p class="quiet-note">${esc(t('ui.marketPractice'))}</p>`;
  const points=currentPoints();const position=currentStake();
  const limit=pointStakeLimit(points,state.user?.id,position);const raw=state.stakeRaw ?? '0';
  const amount=/^\d+$/.test(String(raw))?Number(raw):null;
  const preview=stakeExplanation(raw,points);
  return `<div class="stake-heading"><label for="stake-points">${esc(t('ui.commitPoints'))} <span>${esc(t('ui.optional'))}</span></label>${points?`<span>${esc(t('ui.availableAmount',{amount:pointCount(points.available)}))}${position?.status==='committed'?` · ${esc(t('ui.heldHere',{amount:pointCount(position.amount)}))}`:''}</span>`:''}</div><div class="stake-input-row"><input class="input" id="stake-points" name="stakePoints" type="number" inputmode="numeric" min="0" max="${limit}" step="1" value="${esc(raw)}" aria-describedby="stake-help" ${!points?'readonly':''}><div class="stake-presets" role="group" aria-label="${esc(t('ui.pointsAmount'))}">${[0,10,50,100].map(value=>`<button type="button" class="chip${amount===value?' active':''}" data-action="stake-preset" data-value="${value}" ${value>limit?'disabled':''} aria-pressed="${amount===value}">${value===0?t('ui.practice'):value}</button>`).join('')}</div></div><p class="quiet-note" id="stake-help">${esc(preview)}</p>${points?`<p class="input-hint">${esc(t('ui.stakePolicy',{max:pointCount(points.policy.maxStake)}))}</p>`:`<p class="quiet-note">${state.user?(pointsState.loading?t('ui.loadingPointsBalance'):t('ui.refreshForPoints')):t('ui.signInPointsHint')}</p>`}${pointsState.error?`<p class="error-message" role="alert">${esc(pointsState.error)}</p>`:''}<div class="points-inline-actions">${state.user?button(pointsState.accountChanged?t('ui.refreshAccount'):t('ui.refreshBalance'),pointsState.accountChanged?'refresh-account':'points-refresh','quiet small',pointsState.loading?'disabled':''):button(esc(t('ui.signInPoints')),'account','quiet small')}</div>`;
}
function pointsSummaryMarkup(){
  const points=currentPoints();
  if(!points)return `<p class="quiet-note">${pointsState.loading?t('ui.loadingPoints'):t('ui.pointsNotAvailable')}</p>${pointsState.error?`<p class="error-message" role="alert">${esc(pointsState.error)}</p>`:''}${button(pointsState.accountChanged?t('ui.refreshAccount'):t('ui.refreshBalance'),pointsState.accountChanged?'refresh-account':'points-refresh','secondary small',pointsState.loading?'disabled':'')}`;
  const profile=points.onboarding?.profile || {};const wallet=points.onboarding?.wallet || {};
  const walletStatus=wallet.completed?t('ui.rewardReceived'):wallet.reason==='wallet_already_rewarded'?t('ui.walletRewardUsed'):wallet.eligible?t('ui.readyVerify'):t('ui.connectVerifyWallet');
  const kinds={profile_grant:t('ui.profileReward'),wallet_grant:t('ui.walletReward'),reservation:t('ui.commitmentAdjusted'),settlement:t('ui.forecastSettled'),evidence_refund:t('ui.evidenceRefund'),evidence_restore:t('ui.evidenceRestore'),market_void_refund:t('ui.marketVoidRefund')};
  return `<div class="points-balances"><div><strong>${pointCount(points.available)}</strong><span>${esc(t('ui.availablePoints'))}</span></div><div><strong>${pointCount(points.committed)}</strong><span>${esc(t('ui.committedPoints'))}</span></div><div><strong>${pointCount(points.total)}</strong><span>${esc(t('ui.totalPoints'))}</span></div></div><p class="quiet-note">${esc(t('ui.pointsNotValue'))}</p><ol class="points-checklist"><li><span class="onboarding-check${profile.completed?' completed':''}">${icon(profile.completed?'check':'user')}</span><div><strong>${esc(t('ui.createProfile'))} <span>+${pointCount(profile.reward ?? points.policy.profileGrant)}</span></strong><small>${profile.completed?t('ui.rewardReceived'):t('ui.starterPending')}</small></div></li><li><span class="onboarding-check${wallet.completed?' completed':''}">${icon(wallet.completed?'check':'shield')}</span><div><strong>${esc(t('ui.verifyWallet'))} <span>+${pointCount(wallet.reward ?? points.policy.walletGrant)}</span></strong><small>${esc(walletStatus)}</small></div>${!wallet.completed&&wallet.reason!=='wallet_already_rewarded'?button(esc(t('ui.verifyWallet')),'points-wallet','secondary small'):''}</li></ol><p class="input-hint">${esc(t('ui.oneTimeRewards'))}</p><div class="points-history-heading"><h3>${esc(t('ui.pointsActivity'))}</h3>${button(esc(t('ui.refresh')),'points-refresh','quiet small',pointsState.loading?'disabled':'')}</div>${(points.entries||[]).length?`<ol class="points-history">${points.entries.map(entry=>`<li><div><strong>${esc(kinds[entry.kind] || t('ui.pointsUpdate'))}</strong><small>${date(entry.at,true)}${entry.forecastId?` · <a href="/forecasts/${encodeURIComponent(entry.forecastId)}" data-link>${esc(t('ui.viewForecast'))}</a>`:''}</small>${entry.committedDelta?`<small>${esc(t('ui.committedDelta',{amount:`${entry.committedDelta>0?'+':''}${pointCount(entry.committedDelta)}`}))}</small>`:''}</div><span class="points-delta">${entry.availableDelta>0?'+':''}${pointCount(entry.availableDelta ?? entry.amount)}<small>${esc(t('ui.available'))}</small></span></li>`).join('')}</ol>`:`<p class="quiet-note">${esc(t('ui.noPointsActivity'))}</p>`}${pointsState.error?`<p class="error-message">${esc(pointsState.error)}</p>`:''}`;
}
function renderPointsViews(){
  const summary=document.querySelector('#points-content');if(summary)summary.innerHTML=pointsSummaryMarkup();
  const stake=document.querySelector('#stake-controls');if(stake)stake.innerHTML=stakeMarkup();
}
function updateStakeFeedback(){
  const help=document.querySelector('#stake-help');if(help)help.textContent=stakeExplanation(state.stakeRaw,currentPoints());
  document.querySelectorAll('[data-action="stake-preset"]').forEach(node=>{const selected=String(node.dataset.value)===String(state.stakeRaw);node.classList.toggle('active',selected);node.setAttribute('aria-pressed',String(selected));});
}
async function submitForecast(form){
  const initiatingOwner=state.user?.id || null;const initiatingEpoch=pointsState.epoch;
  const forecast=state.detail?.forecast;
  const outcome=state.selection;const confidence=state.confidence;
  const rawStake=(state.detail?.market?.market??state.detail?.market)?.mode==='active'?'0':form.querySelector('[name="stakePoints"]')?.value ?? '0';
  if(!outcome){showError(uiError('ui.chooseFirst'),document.querySelector('#cast-error'));return;}
  state.stakeRaw=rawStake;state.stakeDirty=true;
  if(!await ensureAuth())return;
  if(initiatingOwner&&(initiatingOwner!==state.user?.id||initiatingEpoch!==pointsState.epoch))return;
  const owner=initiatingOwner || state.user.id;const epoch=pointsState.epoch;const sequence=state.sequence;
  const current=()=>owner===state.user?.id&&epoch===pointsState.epoch;
  await withForm(form,'cast-error',async()=>{
    if(!current())return;
    if(!forecast||!forecastIsOpen(forecast))throw uiError('ui.closedRefresh');
    let points=currentPoints();if(!points)points=await loadPoints();
    if(!current())return;
    if(pointsState.accountChanged)throw Object.assign(uiError('ui.accountBeforeSubmit'),{code:'account_changed',status:409});
    const body=forecastSubmission({forecast,outcome,confidence,stakePoints:rawStake,expectedUserId:owner,points,position:currentStake()});
    const result=await api(`/api/forecasts/${encodeURIComponent(forecast.id)}/forecast`,{method:'POST',body,idempotent:true});
    if(!current())return;
    if(!acceptPoints(result.points,owner))await loadPoints();
    if(!current())return;
    if(pointsState.accountChanged)throw Object.assign(uiError('ui.accountAfterSubmit'),{code:'account_changed',status:409});
    toast(body.stakePoints?t('ui.recordedWithPoints',{amount:pointCount(body.stakePoints)}):t('ui.practiceRecorded'));
    if(sequence===state.sequence){
      state.stakeRaw=String(result.stake?.amount ?? body.stakePoints);state.selection=outcome;state.confidence=confidence;
      history.replaceState({},'',`/forecasts/${encodeURIComponent(forecast.id)}`);void renderRoute({preserve:true});
    }
  },t('ui.recordingForecast'));
}
function main(){return document.querySelector('#main');}
function resetWallet(){walletState.operation+=1;walletState.generation+=1;walletState.connection+=1;walletState.off?.();Object.assign(walletState,{record:null,phase:'idle',wallet:null,accounts:[],address:null,challenge:null,busy:false,error:'',notice:'',accountChanged:false,off:null});}
function walletMarkup(){
  const authentication=state.me?.authentication;
  const address=authentication?.method==='wallet'?authentication.address:walletState.record?.address;
  return `<p class="quiet-note">${esc(t(authentication?.method==='wallet'?'ui.walletReturnHint':'ui.walletMigrationHint'))}</p>${address?`<div class="wallet-status"><strong>${esc(t('ui.walletLinked'))}</strong><code>${esc(address)}</code></div>`:''}${authentication?.method==='wallet'?'':button(esc(t('ui.migrateWallet')),'auth-migrate','secondary')}<p class="error-message" role="alert">${esc(walletState.error)}</p>`;
}
function renderWallet(){const node=document.querySelector('#wallet-content');if(node)node.innerHTML=walletMarkup();syncLanguageControls();}
async function loadWallet(sequence){
  const owner=state.user?.id;
  const operation=walletState.operation;
  const generation=walletState.generation;
  const current=()=>owner===state.user?.id && operation===walletState.operation && generation===walletState.generation && sequence===state.sequence;
  try{const result=await api('/api/wallet');if(!current())return;if(result.points?.userId&&result.points.userId!==owner){walletState.record=null;walletState.accountChanged=true;walletState.error=t('ui.walletAccountRefresh');renderWallet();return;}walletState.record=result.wallet;walletState.error='';renderWallet();}catch(error){if(!current())return;walletState.error=errorText(error);renderWallet();}
}
walletDiscovery.subscribe(()=>{if(walletState.phase==='choose')renderWallet();});
async function walletAction(action,target){
  if(walletState.busy || walletState.accountChanged || !state.user?.id)return;
  const owner=state.user.id;
  const operation=++walletState.operation;
  let generation=walletState.generation;
  const current=()=>owner===state.user?.id && operation===walletState.operation && generation===walletState.generation;
  walletState.error='';
  if(action==='wallet-connect'||action==='wallet-refresh'){walletState.phase='choose';renderWallet();return;}
  if(action==='wallet-cancel'){walletState.challenge=null;walletState.phase='idle';walletState.generation+=1;renderWallet();return;}
  walletState.busy=true;renderWallet();
  try{
    if(action==='choose-wallet'){
      const wallet=walletDiscovery.get()[Number(target.dataset.index)];
      const accounts=await connectWallet(wallet);
      if(!current())return;
      walletState.off?.();
      Object.assign(walletState,{wallet,accounts,address:preferredAccount(accounts)?.address,challenge:null,phase:'account',notice:'',generation:walletState.generation+1});
      generation=walletState.generation;
      const connection=++walletState.connection;
      walletState.off=observeWallet(wallet,()=>{
        if(owner!==state.user?.id || walletState.wallet!==wallet || connection!==walletState.connection)return;
        walletState.generation+=1;walletState.operation+=1;walletState.busy=false;walletState.challenge=null;walletState.phase='choose';walletState.accounts=[];walletState.address=null;
        walletState.notice=t('ui.walletAccountsChanged');
        renderWallet();
      });
    }else if(action==='wallet-challenge'){
      const challenge=await api('/api/wallet/challenge',{method:'POST',body:{address:walletState.address,expectedUserId:owner}});
      if(!current())return;
      walletState.challenge=challenge;walletState.phase='challenge';
    }else if(action==='wallet-sign'){
      const hadWalletReward=currentPoints()?.onboarding?.wallet?.completed;
      const account=walletState.accounts.find(item=>item.address===walletState.address);
      const payload=await signOwnershipChallenge(walletState.wallet,account,walletState.challenge,{stillCurrent:current});
      if(!current())return;
      const result=await api('/api/wallet/link',{method:'POST',body:{...payload,expectedUserId:owner}});
      if(!current())return;
      // The server verifies Ed25519 ownership; connection alone never establishes a link.
      let record=result.wallet;
      if(!record){
        const status=await api('/api/wallet');
        if(!current())return;
        record=status.wallet;
      }
      if(!record)throw uiError('ui.walletNotConfirmed');
      walletState.record=record;
      walletState.challenge=null;walletState.phase='idle';walletState.notice=t('ui.walletOwnershipVerified');
      const points=await loadPoints();if(!current())return;
      if(hadWalletReward===false&&points?.onboarding?.wallet?.completed===true&&Number.isSafeInteger(points.onboarding.wallet.reward)&&points.onboarding.wallet.reward>0)walletState.notice=t('ui.walletPointsReceived',{amount:formatNumber(points.onboarding.wallet.reward)});
    }else if(action==='wallet-unlink'){
      const wallet=walletState.wallet;
      await api('/api/wallet/unlink',{method:'POST',body:{expectedUserId:owner}});
      if(!current())return;
      walletState.record=null;walletState.challenge=null;walletState.phase='idle';walletState.generation+=1;
      generation=walletState.generation;
      walletState.connection+=1;
      walletState.off?.();walletState.off=null;
      try{await disconnectWallet(wallet);}catch{/* Server unlink already succeeded; extension permissions can be removed in the wallet. */}
      if(!current())return;
      walletState.wallet=null;walletState.accounts=[];walletState.address=null;walletState.notice=t('ui.walletDisconnected');
      await loadPoints();if(!current())return;
    }
  }catch(error){if(current()){walletState.error=errorText(error) || t('ui.walletConnectFailed');if(['account_changed','account_precondition_required'].includes(error.code))walletState.accountChanged=true;}}finally{if(current()){walletState.busy=false;renderWallet();}}
}
// Apply individual CSS properties rather than inline style markup, for strict CSP.
new MutationObserver(()=>{app.querySelectorAll('[data-probability]').forEach(element=>{
  const value=probability(Number(element.dataset.probability));
  element.style.transform=`scaleX(${(value ?? 0)/100})`;
});}).observe(app,{childList:true,subtree:true});
function scrollToHash(){if(location.hash)setTimeout(()=>{const target=document.getElementById(decodeURIComponent(location.hash.slice(1)));target?.scrollIntoView({behavior:'auto',block:'start'});},10);}
async function renderRoute({preserve=false,localize=false}={}){
  marketClient.detach();
  forecastTranslations.reset(getLocale());
  const sequence=++state.sequence;shell();if(localePainting)main().inert=true;const path=location.pathname.replace(/\/$/,'')||'/';const titles={'/':t('ui.todayForecasts'),'/explore':t('ui.exploreForecasts'),'/create':t('ui.createForecast'),'/activity':t('ui.yourActivity'),'/profile':t('ui.yourProfile')};document.title=`Forecast — ${titles[path] || t('ui.forecastsEvidence')}`;
  if(path==='/'||path==='/explore')await renderFeed(sequence,localize?state.feed:null);
  else if(path==='/create')renderCreate();
  else if(path.startsWith('/forecasts/'))await renderDetail(sequence,preserve,localize?state.detail:null);
  else if(path==='/activity')await renderActivity(sequence);
  else if(path==='/profile')await renderProfile(sequence,localize?state.me:null);
  else if(path.startsWith('/creators/'))await renderCreator(sequence);
  else main().innerHTML=empty(t('ui.notFound'),t('ui.notFoundHint'),`<a href="/" class="button" data-link>${esc(t('ui.backForecasts'))}</a>`);
}
function navigate(url,{replace=false}={}){const destination=new URL(url,location.origin);if(!isAppPath(destination.pathname)){location.href=destination.href;return;}if(replace)history.replaceState({},'',destination);else history.pushState({},'',destination);window.scrollTo({top:0,behavior:'instant'});void renderRoute();}
function setFilter(key,value){const params=new URLSearchParams(location.search);if(value)params.set(key,value);else params.delete(key);params.delete('cursor');navigate(`${location.pathname}${params.size?`?${params}`:''}`);}

const walletAuth=createWalletAuthClient({api,owner:()=>state.user?.id||null,onChange:()=>{state.authPending=walletAuth.busy();if(authDialog.open)authDialog.innerHTML=authMarkup();syncLanguageControls();},onSuccess:acceptAuthentication,onRefresh:refreshAuthentication});
async function refreshAuthentication({stillCurrent=()=>true,expectedUserId=null}={}){
  const data=await api('/api/me');
  if(!stillCurrent())return null;
  if(expectedUserId&&data.user?.id!==expectedUserId)throw uiError('ui.accountBeforeSubmit');
  resetWallet();resetPoints();state.user=data.user;state.me=data;
  if(data.user&&data.points)acceptPoints(data.points,data.user.id);
  if(shareDialog.open)shareDialog.close();shareSession=null;updateAccount();
  return data;
}
async function acceptAuthentication(result,{stillCurrent=()=>true}={}){
  const data=await refreshAuthentication({stillCurrent,expectedUserId:result.user.id});
  if(!data||!stillCurrent())return;
  closeAuth(true);toast(t('ui.signedIn'));
  void renderRoute({preserve:true});
}
function profileAuthenticationMarkup(){
  const wallet=state.me?.authentication?.method==='wallet';
  return `<h2>${esc(t(wallet?'ui.walletSignIn':'ui.legacyProfile'))}</h2><p class="quiet-note">${esc(t(wallet?'ui.walletReturnHint':'ui.walletMigrationHint'))}</p>${wallet?'':button(esc(t('ui.migrateWallet')),'auth-migrate','secondary')}${button(`${icon('logout')} ${esc(t('ui.signOut'))}`,'logout','quiet separated-button')}`;
}
function authMarkup(){
  const auth=walletAuth.get(),legacy=state.authMode==='legacy',migrate=state.authMode==='migrate';
  const disabled=walletAuth.busy()?'disabled':'';
  let controls='';
  if(legacy){
    controls=`<p class="dialog-copy">${esc(t('ui.legacyImportHint'))}</p><form id="auth-form"><div class="form-field"><label for="auth-input">${esc(t('ui.recoveryCode'))}</label><input class="input" id="auth-input" name="recoveryCode" type="password" minlength="32" maxlength="256" pattern="[A-Za-z0-9_-]{32,256}" autocomplete="off" spellcheck="false" autocapitalize="none" required ${disabled}></div><button class="button full" type="submit" ${disabled}>${esc(t('ui.importLegacy'))}</button></form>${button(esc(t('ui.walletSignIn')),'auth-mode','quiet',`data-mode="wallet" ${disabled}`)}`;
  }else if(auth.phase==='account'||auth.phase==='preparing'){
    controls=`<div class="form-field"><label for="auth-wallet-account">${esc(t('ui.chooseSolanaAccount'))}</label><select class="input" id="auth-wallet-account" ${disabled}>${auth.accounts.map(account=>`<option value="${esc(account.address)}" ${account.address===auth.address?'selected':''}>${esc(account.label||account.address)}</option>`).join('')}</select></div>${button(esc(t('ui.reviewWalletSignIn')),'auth-challenge','full',disabled)}`;
  }else if(['challenge','signing','verifying'].includes(auth.phase)&&auth.challenge){
    controls=`<pre class="wallet-message" lang="en">${esc(auth.challenge.message)}</pre><p class="quiet-note">${esc(t('ui.expires',{date:date(auth.challenge.expiresAt,true)}))}</p>${button(esc(t(migrate?'ui.signMigration':'ui.signWalletLogin')),'auth-sign','full',disabled)}`;
  }else if(auth.phase==='cancel_failed'){
    controls=button(esc(t('ui.retryCancelSignIn')),'close-auth','secondary');
  }else{
    const wallets=walletDiscovery.get();
    controls=wallets.length?`<div class="wallet-list">${wallets.map((wallet,index)=>button(esc(wallet.name),'auth-wallet','secondary',`data-index="${index}" ${disabled}`)).join('')}</div>`:`<p class="quiet-note">${esc(t('ui.noWalletDetected'))}</p>${button(esc(t('ui.checkAgain')),'auth-refresh','secondary',disabled)}`;
    if(!migrate)controls+=button(esc(t('ui.importLegacy')),'auth-mode','quiet separated-button',`data-mode="legacy" ${disabled}`);
  }
  return `${languageControl('auth-language')}<div class="dialog-head"><h2 id="auth-title">${esc(t(migrate?'ui.migrateWallet':legacy?'ui.importLegacy':'ui.walletSignIn'))}</h2><button class="icon-button" data-action="close-auth" aria-label="${esc(t('ui.closeSignIn'))}">${icon('close')}</button></div>${legacy?'':`<p class="dialog-copy">${esc(t(migrate?'ui.walletMigrationHint':'ui.walletSignInHint'))}</p>`}${controls}${walletAuth.busy()?`<p class="loading-label" role="status">${esc(t(auth.phase==='canceling'?'ui.cancelingSignIn':'ui.waitingWallet'))}</p>`:''}<p class="error-message" id="auth-error" role="alert">${esc(auth.error?errorText(auth.error):state.authWalletError?errorText(state.authWalletError):'')}</p><p class="auth-note">${esc(t('ui.termsPrefix'))} <a href="/terms" target="_blank" rel="noopener">${esc(t('ui.terms'))}</a> ${esc(t('ui.and'))} <a href="/privacy" target="_blank" rel="noopener">${esc(t('ui.privacyPolicy'))}</a>.</p>`;
}
async function initializeAuthWallets(){
  // The Android shell registers its native Mobile Wallet Adapter bridge; browsers use the web MWA flow.
  if(registerNativeWallet()){state.authWalletError=null;if(authDialog.open)authDialog.innerHTML=authMarkup();return;}
  try{await initializeMobileWallet();state.authWalletError=null;}catch(error){state.authWalletError=error;}
  if(authDialog.open)authDialog.innerHTML=authMarkup();
}
function openAuth(mode='wallet'){
  if(authDialog.open||walletAuth.busy())return;
  if(!walletAuth.reset())return;
  state.authMode=mode==='migrate'?'migrate':mode==='legacy'?'legacy':'wallet';state.authWalletError=null;
  authDialog.innerHTML=authMarkup();authDialog.showModal();
  void initializeAuthWallets();
  void walletAuth.prepare().catch(error=>{if(authDialog.open){state.authWalletError=error;authDialog.innerHTML=authMarkup();}});
}
let authPromise=null;
async function ensureAuth(){await authLoaded;if(state.user)return true;if(authPromise)return authPromise;authPromise=new Promise(resolve=>{state.authResolve=resolve;openAuth();});try{return await authPromise;}finally{authPromise=null;}}
async function closeAuth(success=false){
  if(!success){try{await walletAuth.cancel();}catch{return;}}
  authDialog.close();authDialog.innerHTML='';state.authPending=false;state.authResolve?.(success);state.authResolve=null;updateAccount();
  if(!success)void renderRoute({preserve:true});
}
authDialog.addEventListener('cancel',event=>{event.preventDefault();void closeAuth();});
walletDiscovery.subscribe(()=>{if(authDialog.open&&!walletAuth.busy())authDialog.innerHTML=authMarkup();});

async function withForm(form,errorId,operation,loading=t('ui.working')){
  const submit=form.querySelector('[type="submit"]');if(submit.disabled)return;
  const owner=state.user?.id;const epoch=pointsState.epoch;
  const current=()=>form.id==='auth-form'||(owner===state.user?.id&&epoch===pointsState.epoch);
  const old=submit.innerHTML;submit.disabled=true;submit.innerHTML=`<span class="spinner" aria-hidden="true"></span>${loading}`;
  const errorTarget=document.getElementById(errorId);if(errorTarget)errorTarget.textContent='';
  try{await operation();}catch(error){
    if(!current())return;
    const accountChanged=['account_changed','account_precondition_required','profile_owner_changed'].includes(error.code);
    const pointsError=/^(points_|stake_|insufficient_points)/.test(error.code || '');
    if(error.code==='invalid_recovery_code'||form.id==='auth-form'){showError(error,errorTarget);}
    else if(accountChanged||pointsError||(error.status===409&&error.code==='revision_conflict')){
      if(errorTarget){
        errorTarget.innerHTML='';errorTarget.append(document.createTextNode(accountChanged||pointsError?`${errorText(error)} `:t('ui.forecastChanged')+' '));
        const refresh=document.createElement('button');refresh.type='button';refresh.className='button secondary small';
        const staleStake=['points_conflict','stake_required'].includes(error.code);
        refresh.dataset.action=accountChanged?'refresh-account':pointsError&&!staleStake?'points-refresh':'refresh-detail';
        refresh.textContent=accountChanged?t('ui.refreshAccount'):staleStake?t('ui.refreshForecastBalance'):pointsError?t('ui.refreshPointsBalance'):t('ui.refreshLatest');errorTarget.append(refresh);
      }
      if(accountChanged){pointsState.snapshot=null;pointsState.accountChanged=true;pointsState.error=errorText(error);renderPointsViews();}
    }else if(error.status===401){state.user=null;resetPoints();if(shareDialog.open)shareDialog.close();shareSession=null;updateAccount();showError(uiError('ui.sessionExpired'),errorTarget);state.authResolve=()=>{void renderRoute();};openAuth('login');}
    else showError(error,errorTarget);
  }finally{if(submit.isConnected&&current()){submit.disabled=false;submit.innerHTML=old;}}
}
let shareSession=null;
function shareHeader(title,locale=getLocale()){return `<div class="dialog-head"><h2 id="share-title">${esc(title??t('ui.shareQuestion',{},locale))}</h2><button class="icon-button" data-action="close-share" aria-label="${esc(t('ui.closeShare',{},locale))}">${icon('close')}</button></div>${languageControl('share-language',locale)}`;}
function shareActions(session){const tr=(key,params={})=>t(key,params,session.locale);return `<label class="share-url-label" for="share-url">${esc(tr('ui.forecastLink'))}</label><input class="input share-url" id="share-url" value="${esc(session.url)}" readonly aria-label="${esc(tr('ui.copyableForecastLink'))}"><div class="share-actions">${session.file?button(`${icon('download')}${esc(tr('ui.downloadImage'))}`,'download-share','secondary'):''}${button(`${icon('copy')}${esc(tr('ui.copyLink'))}`,'copy-share','secondary')}</div>${navigator.share?button(`${icon('share')}${esc(tr(session.canShareFile?'ui.shareImage':'ui.shareLink'))}`,'native-share','full'):''}<p class="error-message" id="share-error" role="alert"></p>`;}
function profileShareCurrent(session,renderVersion=session.renderVersion){
  return shareSession===session && shareDialog.open && state.user?.id===session.owner && session.renderVersion===renderVersion;
}
function profileCaption(data,locale=getLocale()){
  const m=data.metrics;
  const tr=(key,params={})=>t(key,params,locale);
  if(!m.resolvedForecasts)return m.totalForecasts?tr(m.totalForecasts===1?'ui.captionPendingOne':'ui.captionPendingMany',{count:formatNumber(m.totalForecasts,{},locale)}):tr('ui.captionNew');
  const accuracy=m.accuracy===null?tr('ui.accuracyUnavailable'):tr('ui.captionAccuracy',{accuracy:profileAccuracyLabel(m.accuracy,locale)});
  const brier=m.brierScore===null?tr('ui.brierUnavailable'):tr('ui.captionBrier',{score:profileScoreLabel(m.brierScore,locale)});
  const counts=tr(m.correctForecasts===1?(m.resolvedForecasts===1?'ui.captionResultOne':'ui.captionResultMixed'):(m.resolvedForecasts===1?'ui.captionResultSingleForecast':'ui.captionResultMany'),{correct:formatNumber(m.correctForecasts,{},locale),resolved:formatNumber(m.resolvedForecasts,{},locale)});
  return `${accuracy}. ${counts} ${brier}.${data.sampleStatus==='provisional'?` ${tr('ui.captionSmallSample')}`:''} ${tr('ui.captionMyRecord')}`;
}
function profileCardControls(session){
  const tr=key=>t(key,{},session.locale);
  return `<div class="profile-card-options"><fieldset><legend>${esc(tr('ui.imageSize'))}</legend><div class="segmented-control">${button(esc(tr('ui.wide')),'profile-card-format','',`data-value="landscape" aria-pressed="${session.format==='landscape'}"`)}${button(esc(tr('ui.portrait')),'profile-card-format','',`data-value="portrait" aria-pressed="${session.format==='portrait'}"`)}</div></fieldset><fieldset><legend>${esc(tr('ui.style'))}</legend><div class="segmented-control">${button(esc(tr('ui.paper')),'profile-card-theme','',`data-value="paper" aria-pressed="${session.theme==='paper'}"`)}${button(esc(tr('ui.ink')),'profile-card-theme','',`data-value="ink" aria-pressed="${session.theme==='ink'}"`)}</div></fieldset></div>`;
}
function profileCardActions(session){
  if(!session.snapshot)return '<p class="error-message" id="share-error" role="alert"></p>';
  const tr=key=>t(key,{},session.locale);
  const xUrl=new URL('https://x.com/intent/tweet');xUrl.searchParams.set('text',session.caption);xUrl.searchParams.set('url',session.url);
  return `<div class="profile-card-export"><div class="share-actions">${session.file?button(`${icon('download')}${esc(tr('ui.savePng'))}`,'download-share',''):button(esc(tr('ui.renderingPng')),'download-share','','disabled')}${navigator.share&&session.canShareFile?button(`${icon('share')}${esc(tr('ui.shareImage'))}`,'native-share','secondary'):''}</div><div class="profile-card-caption"><label for="profile-caption">${esc(tr('ui.readyCaption'))}</label><textarea class="input" id="profile-caption" rows="3" readonly>${esc(session.caption)}</textarea><div class="profile-caption-actions">${button(`${icon('copy')}${esc(tr('ui.copyCaptionLink'))}`,'copy-caption','quiet small')}<a class="button quiet small" href="${esc(xUrl.href)}" target="_blank" rel="noopener noreferrer">${esc(tr('ui.openX'))} ${icon('external')}</a></div><p class="quiet-note">${esc(tr('ui.xInstructions'))}</p></div><label class="share-url-label" for="share-url">${esc(tr('ui.publicRecord'))}</label><input class="input share-url" id="share-url" value="${esc(session.url)}" readonly><div class="share-actions">${button(`${icon('copy')}${esc(tr('ui.copyRecordLink'))}`,'copy-share','secondary small')}<a class="button quiet small" href="${esc(session.url)}" target="_blank" rel="noopener noreferrer">${esc(tr('ui.viewRecord'))} ${icon('arrow')}</a></div></div><p class="error-message" id="share-error" role="alert"></p>`;
}
async function verifyProfileRecord(snapshot,locale=getLocale()){
  if(!snapshot || !/^[0-9a-f]{64}$/.test(snapshot.snapshotHash) || typeof snapshot.canonicalJson!=='string' || snapshot.canonicalJson.length>131072 || snapshot.commitmentProfile?.algorithm!=='SHA-256' || snapshot.commitmentProfile.prefix!=='forecast-network:sha256:profile-card-json:v1\n')throw new Error(t('ui.recordVerificationMissing',{},locale));
  const bytes=new TextEncoder().encode(snapshot.commitmentProfile.prefix+snapshot.canonicalJson);
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  const hash=Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,'0')).join('');
  if(hash!==snapshot.snapshotHash)throw new Error(t('ui.recordVerificationFailed',{},locale));
  const original=JSON.parse(snapshot.canonicalJson);
  if(original.commitmentProfile?.prefix!==snapshot.commitmentProfile.prefix)throw new Error(t('ui.recordVerificationFormat',{},locale));
  return profileCardData({...original,snapshotHash:hash},location.origin);
}
async function shareProfile(){
  if(!await ensureAuth())return;
  const locale=getLocale();const tr=(key,params={})=>t(key,params,locale);
  const session={kind:'profile',owner:state.user.id,locale,format:'landscape',theme:'paper',renderVersion:0,snapshot:null,file:null,canShareFile:false,caption:'',url:new URL(`/creators/${encodeURIComponent(state.user.id)}`,location.origin).href};
  shareSession=session;shareDialog.classList.add('profile-share-dialog');
  shareDialog.innerHTML=`${shareHeader(tr('ui.recordReady'),locale)}<p class="dialog-copy">${esc(tr('ui.publishRecordCopy'))}</p><div class="share-loading" role="status"><span class="spinner" aria-hidden="true"></span> ${esc(tr('ui.preparingRecord'))}</div>${profileCardActions(session)}`;
  if(!shareDialog.open)shareDialog.showModal();
  try{
    const snapshot=await api('/api/me/share-card',{method:'POST',body:{expectedUserId:session.owner}});
    if(!profileShareCurrent(session))return;
    const data=await verifyProfileRecord(snapshot,locale);
    if(!profileShareCurrent(session))return;
    if(data.user.id!==session.owner)throw new Error(tr('ui.recordNotYours'));
    session.snapshot=data;session.url=data.url;session.title=t('ui.profileRecordTitle',{name:data.user.displayName},session.locale);session.caption=profileCaption(data,session.locale);
    await renderProfileShare(session);
  }catch(error){
    if(!profileShareCurrent(session))return;
    shareDialog.innerHTML=`${shareHeader(tr('ui.yourForecastRecord'),locale)}<p class="dialog-copy">${esc(tr('ui.cardFailed'))}</p><p class="error-message" id="share-error" role="alert"></p><a class="button secondary full" href="/profile">${esc(tr('ui.reloadProfile'))} ${icon('refresh')}</a>`;
    showError(error,shareDialog.querySelector('#share-error'));
  }
}
async function renderProfileShare(session){
  const locale=session.locale??getLocale();const tr=(key,params={})=>t(key,params,locale);
  const renderVersion=++session.renderVersion;
  session.file=null;session.canShareFile=false;
  shareDialog.innerHTML=`${shareHeader(tr('ui.recordReady'),locale)}<p class="dialog-copy">${esc(tr('ui.recordShareSubtitle'))}</p>${profileCardControls(session)}<div class="share-preview-holder profile-card-preview ${session.format==='landscape'?'is-landscape':'is-portrait'}"><div class="share-loading" role="status">${esc(tr('ui.renderingCard'))}</div></div><p class="profile-card-size">${esc(tr('ui.imageAsOf',{size:session.format==='landscape'?'1200 × 675':'1080 × 1350',date:formatEpochDate(session.snapshot.asOf,{full:true,locale})}))}</p>${profileCardActions(session)}`;
  try{
    await loadCardFonts();
    if(!profileShareCurrent(session,renderVersion))return;
    const canvas=document.createElement('canvas');canvas.className='share-preview';canvas.setAttribute('role','img');canvas.setAttribute('aria-label',`${session.snapshot.user.displayName}. ${session.caption}`);
    renderProfileCard(canvas,session.snapshot,{format:session.format,theme:session.theme,locale});
    const blob=await canvasPng(canvas,{locale});
    if(!profileShareCurrent(session,renderVersion))return;
    const handle=session.snapshot.user.handle.replace(/[^a-zA-Z0-9_-]/g,'').slice(0,40);
    session.file=new File([blob],`forecast-record-${handle}-${session.format}.png`,{type:'image/png'});
    try{session.canShareFile=Boolean(navigator.canShare?.({files:[session.file]}));}catch{session.canShareFile=false;}
    shareDialog.querySelector('.share-preview-holder').replaceChildren(canvas);
    const exports=shareDialog.querySelector('.profile-card-export');
    if(exports)exports.outerHTML=profileCardActions(session).replace('<p class="error-message" id="share-error" role="alert"></p>','');
  }catch(error){if(profileShareCurrent(session,renderVersion))showError(error,shareDialog.querySelector('#share-error'));}
}
async function updateProfileCard(action,value){
  const session=shareSession;
  if(session?.kind!=='profile'||!session.snapshot||!profileShareCurrent(session))return;
  if(action==='profile-card-format'&&['landscape','portrait'].includes(value))session.format=value;
  else if(action==='profile-card-theme'&&['paper','ink'].includes(value))session.theme=value;
  else return;
  const rendering=renderProfileShare(session);
  const renderVersion=session.renderVersion;
  await rendering;
  if(profileShareCurrent(session,renderVersion))shareDialog.querySelector(`[data-action="${action}"][data-value="${value}"]`)?.focus({preventScroll:true});
}
async function shareForecast(id,title){
  shareDialog.classList.remove('profile-share-dialog');
  const locale=getLocale();const tr=(key,params={})=>t(key,params,locale);
  const session={kind:'forecast',id,title,locale,detail:null,asOf:null,renderVersion:0,url:new URL(`/forecasts/${encodeURIComponent(id)}`,location.origin).href,file:null,canShareFile:false,recorded:false};
  shareSession=session;
  shareDialog.innerHTML=`${shareHeader(undefined,locale)}<p class="dialog-copy">${esc(tr('ui.shareLatestForecasts'))}</p><div class="share-loading" role="status"><span class="spinner" aria-hidden="true"></span> ${esc(tr('ui.preparingForecastCard'))}</div>${shareActions(session)}`;
  if(!shareDialog.open)shareDialog.showModal();
  try{
    const detail=await api(`/api/forecasts/${encodeURIComponent(id)}`);
    if(shareSession!==session||!shareDialog.open)return;
    session.detail=detail;session.asOf=Date.now();
    await renderForecastShare(session);
  }catch(error){
    if(shareSession!==session||!shareDialog.open)return;
    shareDialog.innerHTML=`${shareHeader(undefined,session.locale)}<p class="dialog-copy">${esc(t('ui.forecastCardFailed',{},session.locale))}</p>${shareActions(session)}`;
    showError(error,document.querySelector('#share-error'));
  }
}
async function renderForecastShare(session){
  if(shareSession!==session||!shareDialog.open||!session.detail)return;
  const locale=session.locale??getLocale();const tr=(key,params={})=>t(key,params,locale);
  const renderVersion=++session.renderVersion;
  const current=()=>shareSession===session&&shareDialog.open&&session.renderVersion===renderVersion;
  session.file=null;session.canShareFile=false;
  shareDialog.innerHTML=`${shareHeader(undefined,locale)}<p class="dialog-copy">${esc(tr('ui.shareLatestForecasts'))}</p><div class="share-loading" role="status"><span class="spinner" aria-hidden="true"></span> ${esc(tr('ui.preparingForecastCard'))}</div>${shareActions(session)}`;
  try{
    await loadCardFonts();
    if(!current())return;
    const data=shareCardData(session.detail,location.origin,session.asOf,locale);
    session.title=data.question;
    const canvas=document.createElement('canvas');
    canvas.className='share-preview';
    canvas.setAttribute('role','img');
    canvas.setAttribute('aria-label',`${data.question}. ${data.groups.map(group=>`${group.label} ${group.value===null?tr('card.noData'):`${formatNumber(Math.round(group.value),{},locale)}%`}`).join(', ')}.${data.personal?` ${tr('card.myForecast')}: ${tr('card.personal',{outcome:data.personal.outcome,confidence:formatNumber(data.personal.confidence,{},locale)})}.`:''}`);
    renderShareCard(canvas,data,categoryLabel(data.category,locale));
    const blob=await canvasPng(canvas,{locale});
    if(!current())return;
    session.file=new File([blob],`forecast-${String(session.id).replace(/[^a-zA-Z0-9_-]/g,'')}.png`,{type:'image/png'});
    try{session.canShareFile=Boolean(navigator.canShare?.({files:[session.file]}));}catch{session.canShareFile=false;}
    shareDialog.innerHTML=`${shareHeader(undefined,locale)}<p class="dialog-copy">${esc(tr('ui.latestCardCopy'))}${data.personal?` ${esc(tr('ui.yourForecastIncluded'))}`:''}</p><div class="share-preview-holder"></div>${shareActions(session)}`;
    shareDialog.querySelector('.share-preview-holder').append(canvas);
  }catch(error){
    if(!current())return;
    shareDialog.innerHTML=`${shareHeader(undefined,locale)}<p class="dialog-copy">${esc(tr('ui.forecastCardFailed'))}</p>${shareActions(session)}`;
    showError(error,document.querySelector('#share-error'));
  }
}
function recordSuccessfulShare(session){
  if(session.kind==='profile'||session.recorded)return;
  session.recorded=true;
  void api(`/api/forecasts/${encodeURIComponent(session.id)}/share`,{method:'POST',body:{}}).catch(()=>{});
}
async function shareAction(action){
  const session=shareSession;
  if(!session)return;
  const tr=(key,params={})=>t(key,params,session.locale);
  if(session.kind==='profile'&&!profileShareCurrent(session)){shareDialog.close();toast(tr('ui.signInShareRecord'));return;}
  try{
    if(action==='copy-share'){
      await copyText(session.url);
      recordSuccessfulShare(session);
      toast(tr(session.kind==='profile'?'ui.recordLinkCopied':'ui.forecastLinkCopied'));
    }else if(action==='copy-caption'&&session.kind==='profile'){
      await copyText(`${session.caption}\n${session.url}`);
      toast(tr('ui.captionCopied'));
    }else if(action==='download-share'&&session.file){
      const url=URL.createObjectURL(session.file);
      const link=document.createElement('a');link.href=url;link.download=session.file.name;
      shareDialog.append(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),30000);
      // Browser download dispatch does not prove a completed share or saved file.
      toast(tr('ui.downloadRequested'));
    }else if(action==='native-share'&&navigator.share){
      const shared=await shareWithPlatform(navigator.share.bind(navigator),{title:session.title || tr('card.forecast'),text:session.caption || tr('card.prompt'),url:session.url,...(session.canShareFile?{files:[session.file]}:{})},()=>recordSuccessfulShare(session));
      if(shared)toast(tr(session.kind==='profile'?'ui.recordShared':'ui.forecastShared'));
    }
  }catch(error){if(error.name!=='AbortError')showError({message:tr('ui.shareFailed')},document.querySelector('#share-error'));}
}
shareDialog.addEventListener('close',()=>{shareSession=null;shareDialog.innerHTML='';shareDialog.classList.remove('profile-share-dialog');});
async function copyText(text){if(navigator.clipboard?.writeText){try{await navigator.clipboard.writeText(text);return;}catch{/* Browser policy may reject the modern API; try selection-based copy. */}}const input=document.createElement('textarea');input.value=text;input.className='sr-only';(document.querySelector('dialog[open]') || document.body).append(input);input.select();input.setSelectionRange(0,input.value.length);let copied=false;try{copied=document.execCommand('copy');}finally{input.remove();}if(!copied)throw uiError('ui.copyFailed');}

document.addEventListener('invalid',event=>{if(event.target.setCustomValidity)event.target.setCustomValidity(fieldValidationMessage(event.target));},true);
document.addEventListener('input',event=>{if(event.target.id==='market-spend'){const entry=marketClient.get();if(entry){marketClient.input(entry.side,event.target.value);document.querySelector('#market-review')?.replaceChildren();}}event.target.setCustomValidity?.('');},true);
document.addEventListener('click',async event=>{
  if(localePainting){event.preventDefault();return;}
  const link=event.target.closest('a[data-link]');if(link&&event.button===0&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey&&!event.altKey){const url=new URL(link.href);if(url.origin===location.origin){event.preventDefault();navigate(url.href);}return;}
  const target=event.target.closest('[data-action]');if(!target||target.disabled)return;
  const action=target.dataset.action;
  try{
    if(['wallet-connect','wallet-refresh','wallet-cancel','choose-wallet','wallet-challenge','wallet-sign','wallet-unlink'].includes(action)){await walletAction(action,target);}
    else if(action==='translate-forecast'){await forecastTranslations.translate(target.dataset.id,target.dataset.language);}
    else if(action==='translation-original'){forecastTranslations.original(target.dataset.id);}
    else if(action==='market-quote'){const entry=marketClient.get(),input=document.querySelector('#market-spend');if(entry&&input)marketClient.input(entry.side,input.value);await marketClient.quote();}
    else if(action==='market-fill'){await marketClient.fill();}
    else if(action==='market-reconcile'){await marketClient.reconcile();}
    else if(action==='market-login'){if(await ensureAuth())void renderRoute({preserve:true});}
    else if(action==='market-side'){const entry=marketClient.get();if(entry){marketClient.input(target.dataset.side,entry.spendRaw);paintMarket();}}
    else if(action==='account'){await authLoaded;if(state.user)navigate('/profile');else{state.authResolve=()=>{if(location.pathname==='/profile'||location.pathname==='/activity')void renderRoute();};openAuth();}}
    else if(action==='refresh')void renderRoute();
    else if(action==='refresh-account')location.assign('/profile');
    else if(action==='points-refresh')await loadPoints();
    else if(action==='points-wallet'){openAuth('migrate');}
    else if(action==='stake-preset'){state.stakeRaw=target.dataset.value;state.stakeDirty=true;const input=document.querySelector('#stake-points');if(input)input.value=state.stakeRaw;updateStakeFeedback();}
    else if(action==='refresh-detail')void renderRoute({preserve:true});
    else if(action==='sort'||action==='category')setFilter(action,target.dataset.value);
    else if(action==='choose'){state.selection=target.dataset.outcome;document.querySelectorAll('[data-action="choose"]').forEach(node=>{const selected=node.dataset.outcome===state.selection;node.classList.toggle('selected',selected);node.setAttribute('aria-pressed',String(selected));});document.querySelector('#cast-summary').innerHTML=castSummary();}
    else if(action==='share')await shareForecast(target.dataset.id,target.dataset.title);
    else if(action==='share-profile')await shareProfile();
    else if(action==='profile-card-format'||action==='profile-card-theme')await updateProfileCard(action,target.dataset.value);
    else if(['copy-share','copy-caption','download-share','native-share'].includes(action))await shareAction(action);
    else if(action==='close-share')shareDialog.close();
    else if(action==='example'){state.question=target.dataset.question;state.draft=null;renderCreate();document.querySelector('#question').focus();}
    else if(action==='auth-mode'){if(walletAuth.busy())return;if(!walletAuth.reset())return;state.authMode=target.dataset.mode==='legacy'?'legacy':'wallet';authDialog.innerHTML=authMarkup();authDialog.querySelector('input')?.focus();}
    else if(action==='auth-migrate'){openAuth('migrate');}
    else if(action==='auth-refresh'){await initializeAuthWallets();}
    else if(action==='auth-wallet'){const wallet=walletDiscovery.get()[Number(target.dataset.index)];await walletAuth.choose(wallet,{mode:state.authMode==='migrate'?'migrate':'login',expectedUserId:state.authMode==='migrate'?state.user?.id:null});}
    else if(action==='auth-challenge'){await walletAuth.challenge();}
    else if(action==='auth-sign'){await walletAuth.submit();}
    else if(action==='close-auth'){await closeAuth();}
    else if(action==='logout'){target.disabled=true;try{await api('/api/auth/logout',{method:'POST',body:{}});state.user=null;state.me=null;state.draft=null;state.question='';resetWallet();resetPoints();if(shareDialog.open)shareDialog.close();shareSession=null;toast(t('ui.signedOut'));void renderRoute();}finally{target.disabled=false;}}
    else if(action==='follow'){if(!await ensureAuth())return;target.disabled=true;try{const data=await api(`/api/creators/${encodeURIComponent(target.dataset.id)}/follow`,{method:'POST',body:{following:target.dataset.following!=='true'}});target.dataset.following=String(data.following);target.textContent=data.following?t('ui.following'):t('ui.follow');target.classList.toggle('secondary',data.following);toast(data.following?t('ui.followingNotice'):t('ui.unfollowed'));}finally{target.disabled=false;}}
    else if(action==='attest'){if(!await ensureAuth())return;target.disabled=true;const errorBox=document.querySelector('#attest-error');if(errorBox)errorBox.textContent='';try{toast(t('ui.attestWaiting'));const result=await attestForecast(target.dataset.id,{api});if(result?.status==='submitted'||result?.status==='verified')toast(t('ui.attestStamped'));void renderRoute({preserve:true});}catch(error){if(errorBox)errorBox.textContent=errorText(error);}finally{target.disabled=false;}}
    else if(action==='read-activity'){target.disabled=true;try{await api('/api/activity/read',{method:'POST',body:{}});toast(t('ui.allRead'));void renderRoute();}finally{target.disabled=false;}}
    else if(action==='more'){target.disabled=true;const params=new URLSearchParams(location.search);params.set('cursor',target.dataset.cursor);try{const data=await api(`/api/forecasts?${params}`);renderFeedItems(data,location.pathname==='/explore',true);}finally{target.disabled=false;}}
  }catch(error){showError(error);}
});
document.addEventListener('input',event=>{if(event.target.id==='stake-points'){state.stakeRaw=event.target.value;state.stakeDirty=true;updateStakeFeedback();}if(event.target.id==='confidence'){state.confidence=Number(event.target.value);document.querySelector('#confidence-output').textContent=`${state.confidence}%`;document.querySelector('#cast-summary').innerHTML=castSummary();}if(event.target.id==='question'){state.question=event.target.value;document.querySelector('#question-count').textContent=`${state.question.length} / 1,200`;if(state.draft){state.draft=null;document.querySelector('#draft-preview').innerHTML='';}}});
document.addEventListener('change',event=>{if(event.target.matches('[data-language-select]')){void changeLanguage(event.target.value).catch(showError);return;}if(event.target.id==='wallet-account'){walletState.address=event.target.value;walletState.challenge=null;walletState.generation+=1;}if(event.target.id==='sort-select')setFilter('sort',event.target.value);if(event.target.id==='auth-wallet-account')walletAuth.select(event.target.value);});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!form.id)return;event.preventDefault();
  if(form.id==='search-form'){setFilter('q',new FormData(form).get('q').trim());return;}
  if(form.id==='auth-form'){
    const code=new FormData(form).get('recoveryCode');
    try{await walletAuth.importLegacy(code);}catch(error){showError(error,document.querySelector('#auth-error'));}
    return;
  }
  if(form.id==='create-form'){
    if(!await ensureAuth())return;
    await withForm(form,'create-error',async()=>{const submittedQuestion=new FormData(form).get('question').trim();state.question=submittedQuestion;state.draft=null;const oldPreview=document.querySelector('#draft-preview');if(oldPreview)oldPreview.innerHTML='';const data=await api('/api/forecasts/compile',{method:'POST',body:{question:submittedQuestion},timeout:115000,idempotent:true});if(state.question!==submittedQuestion)throw uiError('ui.questionChanged');state.draft=data;const preview=document.querySelector('#draft-preview');if(preview){preview.innerHTML=previewMarkup(data);preview.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});}else toast(t('ui.questionReady'));},t('ui.reviewingSources'));return;
  }
  if(form.id==='publish-form'){
    if(!await ensureAuth())return;
    await withForm(form,'publish-error',async()=>{if(!state.draft)throw uiError('ui.reviewAgain');const data=await api('/api/forecasts',{method:'POST',body:{draftId:state.draft.draftId},idempotent:true});state.draft=null;state.question='';toast(t('ui.publishedAddPrediction'));navigate(`/forecasts/${encodeURIComponent(data.forecast.id)}`);},t('ui.publishing'));return;
  }
  if(form.id==='cast-form'){await submitForecast(form);return;}
  if(form.id==='comment-form'){
    if(!await ensureAuth())return;
    await withForm(form,'comment-error',async()=>{const text=new FormData(form).get('text').trim();if(!text)throw uiError('ui.enterComment');const data=await api(`/api/forecasts/${encodeURIComponent(routeId())}/comments`,{method:'POST',body:{text},idempotent:true});state.detail.comments=[...(state.detail.comments||[]),data.comment];document.querySelector('#comment-list').innerHTML=commentsMarkup(state.detail.comments);form.reset();toast(t('ui.commentPosted'));},t('ui.posting'));return;
  }
  if(form.id==='evidence-form'){
    if(!await ensureAuth())return;
    await withForm(form,'evidence-error',async()=>{const url=new FormData(form).get('url').trim();if(!safeExternalUrl(url))throw uiError('ui.publicEvidenceUrl');const result=await api(`/api/forecasts/${encodeURIComponent(routeId())}/evidence`,{method:'POST',body:{url},idempotent:true});toast(t(result.status==='held'?'ui.evidenceReportHeld':result.status==='unrelated'?'ui.evidenceReportUnrelated':'ui.evidenceReportReceived'));void renderRoute({preserve:true});},t('ui.submittingEvidence'));return;
  }
  if(form.id==='dispute-form'){
    if(!await ensureAuth())return;
    await withForm(form,'dispute-error',async()=>{const input=Object.fromEntries(new FormData(form));if(!safeExternalUrl(input.evidenceUrl))throw uiError('ui.publicEvidenceUrl');await api(`/api/forecasts/${encodeURIComponent(routeId())}/disputes`,{method:'POST',body:{...input,revision:state.detail.forecast.revision},idempotent:true});toast(t('ui.disputeSubmittedNotice'));void renderRoute({preserve:true});},t('ui.submittingEvidence'));return;
  }
  if(form.id==='profile-form')await withForm(form,'profile-error',async()=>{const data=await api('/api/me',{method:'PATCH',body:Object.fromEntries(new FormData(form))});state.user=data.user;updateAccount();toast(t('ui.nameUpdated'));void renderRoute();},t('ui.saving'));
});
window.addEventListener('popstate',()=>{void renderRoute();});
const authLoaded=api('/api/me').then(data=>{state.me=data;state.user=data.user;if(data.user&&data.points)acceptPoints(data.points,data.user.id);updateAccount();}).catch(()=>{state.user=null;resetPoints();});
void api('/api/status').then(data=>{state.status=data;}).catch(()=>{});
void renderRoute();
