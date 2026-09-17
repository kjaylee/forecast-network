import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=await readFile(new URL('../public/app.js',import.meta.url),'utf8');
function section(start,end){
  const from=source.indexOf(start);const to=source.indexOf(end,from);
  assert.ok(from>=0&&to>from,'production action boundary must exist');
  return source.slice(from,to);
}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
const seekerSource=section('const seekerState=','async function loadWallet(');
const moreSource=section("else if(action==='more'){","\n  }catch(error){showError(error);}").replace("else if(action==='more')","if(action==='more')");
const commentSource=section("if(form.id==='comment-form'){","  if(form.id==='evidence-form'){");
const formSource=section('async function withForm(','let shareSession=');

function fixture({responses=[]}={}){
  const state={sequence:1,user:{id:'user-a'},me:{user:{id:'user-a'},seeker:{available:true,status:null}},detail:{forecast:{id:'forecast-a'},comments:[]}};
  const pointsState={epoch:1};const location={pathname:'/forecasts/forecast-a',search:''};
  const response=deferred();const entered=deferred();const auth=deferred();
  const requests=[];const notices=[];const errors=[];const appended=[];let renders=0;let resets=0;let authWait=false;
  const list={innerHTML:''};const submit={disabled:false,innerHTML:'Post',isConnected:true};const errorTarget={textContent:''};
  const form={id:'comment-form',get isConnected(){return submit.isConnected;},querySelector:()=>submit,reset(){resets+=1;}};
  const document={querySelector:()=>list,getElementById:()=>errorTarget};
  const api=(path,options)=>{requests.push({path,options});entered.resolve();return (responses[requests.length-1]||response).promise;};
  const context={state,pointsState,location,api,document,FormData:class{get(){return 'A comment';}},ensureAuth:async()=>authWait?auth.promise:true,routeId:()=>location.pathname.split('/').at(-1),commentsMarkup:comments=>JSON.stringify(comments),toast:text=>notices.push(text),t:key=>key,uiError:key=>new Error(key),errorText:error=>error.message,showError:error=>errors.push(error),renderFeedItems:(...args)=>appended.push(args),resetPoints:()=>{pointsState.epoch+=1;},shareDialog:{open:false},shareSession:null,updateAccount:()=>{},openAuth:()=>errors.push('auth-open')};
  const build=body=>new Function(...Object.keys(context),body)(...Object.values(context));
  const comment=build(`${formSource}\nreturn async form=>{${commentSource}};`);
  const more=build(`return async target=>{const action='more';try{${moreSource}}catch(error){showError(error);}};`);
  // Keep the real markup too: loading/error must belong to the current request.
  context.esc=value=>String(value);context.icon=()=>'';context.formatNumber=String;context.button=()=>'<button>verify</button>';
  const seeker=build(`${seekerSource}\nrenderSeeker=()=>document.querySelector('#seeker-content').render();return {verifySeeker,seekerMarkup,seekerState};`);
  list.render=()=>{renders+=1;};
  const change=kind=>{
    if(kind==='route'){state.sequence+=1;location.pathname='/forecasts/forecast-b';state.detail={forecast:{id:'forecast-b'},comments:[]};submit.isConnected=false;}
    else if(kind==='filter'){state.sequence+=1;location.search='?category=science';submit.isConnected=false;}
    else{pointsState.epoch+=1;state.user=kind==='logout'?null:{id:kind==='same-user'?'user-a':'user-b'};state.me={user:state.user,seeker:{available:true,status:null}};}
  };
  return {state,pointsState,location,response,entered,auth,requests,notices,errors,appended,list,submit,form,comment,more,seeker,change,renders:()=>renders,resets:()=>resets,waitAuth:()=>{authWait=true;}};
}

for(const kind of ['route','account','logout','same-user'])for(const failed of [false,true])test(`comment ignores stale ${failed?'failure':'success'} after ${kind}`,async()=>{
  const run=fixture();const pending=run.comment(run.form);await run.entered.promise;
  assert.equal(run.requests[0].path,'/api/forecasts/forecast-a/comments');run.change(kind);
  if(failed)run.response.reject(Object.assign(new Error('old failure'),{status:401}));else run.response.resolve({comment:{id:'old-comment'}});
  await pending;
  assert.deepEqual(run.state.detail.comments,[]);assert.equal(run.list.innerHTML,'');assert.equal(run.resets(),0);assert.deepEqual(run.notices,[]);assert.deepEqual(run.errors,[]);
  if(kind==='route')assert.equal(run.state.user.id,'user-a','a stale 401 must not log out the current page');
});
for(const failed of [false,true])test(`current comment ${failed?'failure':'success'} preserves form behavior`,async()=>{
  const run=fixture();const pending=run.comment(run.form);await run.entered.promise;
  if(failed)run.response.reject(new Error('current failure'));else run.response.resolve({comment:{id:'new-comment'}});
  await pending;assert.equal(run.submit.disabled,false);
  if(failed)assert.equal(run.errors[0].message,'current failure');
  else{assert.deepEqual(run.state.detail.comments,[{id:'new-comment'}]);assert.equal(run.resets(),1);assert.deepEqual(run.notices,['ui.commentPosted']);}
});
for(const kind of ['route','account','logout'])test(`comment does not submit when ${kind} changes during authentication`,async()=>{
  const run=fixture();run.waitAuth();const pending=run.comment(run.form);run.change(kind);run.auth.resolve(true);run.response.resolve({comment:{id:'unexpected'}});await pending;
  assert.equal(run.requests.length,0);
});

for(const kind of ['route','filter','account','logout','same-user'])for(const failed of [false,true])test(`pagination ignores stale ${failed?'failure':'success'} after ${kind}`,async()=>{
  const run=fixture();run.location.pathname='/explore';run.location.search='?category=technology';const target={disabled:false,dataset:{cursor:'page-2'}};
  const pending=run.more(target);await run.entered.promise;run.change(kind);
  if(failed)run.response.reject(new Error('old failure'));else run.response.resolve({items:[{id:'old-result'}]});await pending;
  assert.deepEqual(run.appended,[]);assert.deepEqual(run.errors,[]);
});
for(const failed of [false,true])test(`current pagination ${failed?'failure':'success'} preserves action behavior`,async()=>{
  const run=fixture();run.location.pathname='/explore';const target={disabled:false,dataset:{cursor:'page-2'}};
  const pending=run.more(target);await run.entered.promise;
  if(failed)run.response.reject(new Error('current failure'));else run.response.resolve({items:[{id:'next-result'}]});await pending;
  assert.equal(target.disabled,false);
  if(failed)assert.equal(run.errors[0].message,'current failure');else assert.deepEqual(run.appended,[[{items:[{id:'next-result'}]},true,true]]);
});

for(const kind of ['route','account','logout','same-user'])for(const failed of [false,true])test(`Seeker ignores stale ${failed?'failure':'success'} after ${kind}`,async()=>{
  const run=fixture();const pending=run.seeker.verifySeeker();await run.entered.promise;const renders=run.renders();run.change(kind);
  if(failed)run.response.reject(new Error('old failure'));else run.response.resolve({memberNumber:123});await pending;
  assert.equal(run.state.me.seeker.status,null);assert.deepEqual(run.notices,[]);assert.equal(run.renders(),renders);
  assert.equal(run.seeker.seekerMarkup().includes('ui.seekerVerifying'),false);assert.equal(run.seeker.seekerMarkup().includes('old failure'),false);
});
for(const failed of [false,true])test(`current Seeker ${failed?'failure':'success'} preserves action behavior`,async()=>{
  const run=fixture();const pending=run.seeker.verifySeeker();await run.entered.promise;
  assert.equal(run.requests[0].options.body.expectedUserId,'user-a');
  if(failed)run.response.reject(new Error('current failure'));else run.response.resolve({memberNumber:123});await pending;
  assert.equal(run.seeker.seekerState.busy,false);
  if(failed)assert.equal(run.seeker.seekerState.error,'current failure');else{assert.equal(run.state.me.seeker.status.memberNumber,123);assert.deepEqual(run.notices,['ui.seekerVerifiedToast']);}
});
for(const failed of [false,true])test(`older Seeker ${failed?'failure':'success'} cannot clear the next account's pending verification`,async()=>{
  const oldResponse=deferred();const nextResponse=deferred();const run=fixture({responses:[oldResponse,nextResponse]});
  const first=run.seeker.verifySeeker();await run.entered.promise;run.change('account');
  const second=run.seeker.verifySeeker();assert.equal(run.requests.length,2);assert.equal(run.requests[1].options.body.expectedUserId,'user-b');
  if(failed)oldResponse.reject(new Error('old failure'));else oldResponse.resolve({memberNumber:123});await first;
  assert.equal(run.seeker.seekerState.busy,true);assert.equal(run.seeker.seekerState.error,'');assert.equal(run.renders(),2);
  assert.equal(run.state.me.seeker.status,null);assert.deepEqual(run.notices,[]);
  nextResponse.resolve({memberNumber:456});await second;
  assert.deepEqual(run.notices,['ui.seekerVerifiedToast']);assert.equal(run.state.me.seeker.status.memberNumber,456);assert.equal(run.renders(),3);
});
test('comment can submit after its anonymous author signs in on the same detail',async()=>{
  const run=fixture();run.state.user=null;run.waitAuth();const pending=run.comment(run.form);
  run.state.user={id:'user-a'};run.pointsState.epoch+=1;run.state.sequence+=1;run.auth.resolve(true);
  run.response.resolve({comment:{id:'new-comment'}});await pending;
  assert.deepEqual(run.state.detail.comments,[{id:'new-comment'}]);
});
test('repeat pagination and Seeker clicks do not start overlapping current requests',async()=>{
  const run=fixture();const target={disabled:false,dataset:{cursor:'page-2'}};
  const page=run.more(target);await run.more(target);assert.equal(run.requests.length,1);
  const verification=run.seeker.verifySeeker();await run.seeker.verifySeeker();assert.equal(run.requests.length,2);
  run.response.resolve({items:[],memberNumber:123});await Promise.all([page,verification]);
});

for(const code of ['seeker_not_found','seeker_verification_changed','seeker_rpc_unavailable'])test(`Seeker revalidation ${code} updates cached badge only for definitive invalidation`,async()=>{
  const run=fixture();const status={memberNumber:123};run.state.me.seeker.status=status;
  const pending=run.seeker.verifySeeker();await run.entered.promise;run.response.reject(Object.assign(new Error(code),{code}));await pending;
  assert.equal(run.state.me.seeker.status,code==='seeker_rpc_unavailable'?status:null);
  assert.equal(run.seeker.seekerState.error,code);
});
test('a stale definitive Seeker failure cannot clear a newly authenticated user’s badge',async()=>{
  const run=fixture();const pending=run.seeker.verifySeeker();await run.entered.promise;run.change('account');
  run.state.me.seeker.status={memberNumber:456};run.response.reject(Object.assign(new Error('no token'),{code:'seeker_not_found'}));await pending;
  assert.deepEqual(run.state.me.seeker.status,{memberNumber:456});
});

// Exercise the real auth-success and detail-render functions, including the
// skeleton interval where the submitting anonymous form has been detached.
function anonymousCommentFixture(){
  const state={sequence:1,user:null,detail:{forecast:{id:'forecast-a'},comments:[]}};
  const pointsState={epoch:1};const auth=deferred();const posted=deferred();const oldGet=deferred();const newGet=deferred();
  const mainNode={innerHTML:'detail'};const list={innerHTML:''};const newForm={draft:'',resets:0};const notices=[];const requests=[];const paints=[];
  const form={id:'comment-form',isConnected:true,reset(){throw new Error('detached original form must not reset');}};
  const comment={id:'saved-comment',text:'original anonymous draft'};
  const snapshot=comments=>({forecast:{id:'forecast-a',title:'A'},comments,myForecast:{outcome:'YES',confidence:70}});
  let getCount=0;let controller;
  const document={querySelector:selector=>selector==='#comment-list'&&mainNode.innerHTML==='detail'?list:null};
  const context={state,pointsState,document,authLoaded:Promise.resolve(),
    api:(path,options)=>{requests.push({path,options});if(options?.method==='POST')return posted.promise;getCount+=1;return (getCount===1?oldGet:newGet).promise;},
    ensureAuth:()=>auth.promise,refreshAuthentication:async()=>{state.user={id:'user-a'};pointsState.epoch+=1;return {user:state.user};},closeAuth:()=>auth.resolve(true),
    main:()=>mainNode,skeleton:()=>{form.isConnected=false;return 'loading';},routeId:()=>state.detail.forecast.id,
    currentPoints:()=>({}),pointStakeLimit:()=>0,marketClient:{attach:()=>{}},displayForecast:value=>value,scrollToHash:()=>{},
    detailMarkup:data=>{list.innerHTML=JSON.stringify(data.comments);newForm.draft='';return 'detail';},errorPanel:error=>{throw error;},
    FormData:class{get(){return comment.text;}},commentsMarkup:JSON.stringify,withForm:async(_form,_error,operation)=>operation(),uiError:key=>new Error(key),
    toast:key=>notices.push(key),t:key=>key,
    renderRoute:options=>{const paint=controller.renderDetail(++state.sequence,options?.preserve);paints.push(paint);return paint;}};
  const renderSource=section('async function renderDetail(','function renderCreate(');
  const authSource=section('async function acceptAuthentication(','function profileAuthenticationMarkup(');
  controller=new Function(...Object.keys(context),`${renderSource}\n${authSource}\nreturn {renderDetail,acceptAuthentication,comment:async form=>{${commentSource}}};`)(...Object.values(context));
  return {state,pointsState,form,comment,snapshot,posted,oldGet,newGet,mainNode,list,newForm,notices,requests,paints,controller,getCount:()=>getCount};
}
for(const completion of ['old-first','new-first','navigation','logout'])test(`anonymous comment survives the auth repaint race: ${completion}`,async()=>{
  const run=anonymousCommentFixture();const pending=run.controller.comment(run.form);
  await run.controller.acceptAuthentication({user:{id:'user-a'}});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run.mainNode.innerHTML.includes('loading'),true);assert.equal(run.form.isConnected,false);
  assert.equal(run.requests.find(request=>request.options?.method==='POST').options.body.text,'original anonymous draft');
  run.posted.resolve({comment:run.comment});await new Promise(resolve=>setImmediate(resolve));
  const beforePaintNotices=[...run.notices];
  if(completion==='navigation'){run.state.sequence+=1;run.state.detail={forecast:{id:'forecast-b'},comments:[]};run.mainNode.innerHTML='other detail';}
  if(completion==='logout'){run.state.user=null;run.pointsState.epoch+=1;run.state.sequence+=1;}
  if(completion==='old-first'){run.oldGet.resolve(run.snapshot([]));await new Promise(resolve=>setImmediate(resolve));}
  run.newGet.resolve(run.snapshot([run.comment]));await pending;
  run.oldGet.resolve(run.snapshot([]));await Promise.all(run.paints);
  assert.equal(run.getCount(),2,'POST confirmation must invalidate the older detail GET');
  assert.equal(beforePaintNotices.includes('ui.commentPosted'),false,'wait for the post-save repaint');
  if(['navigation','logout'].includes(completion))assert.equal(run.notices.includes('ui.commentPosted'),false);
  else{assert.deepEqual(run.state.detail.comments,[run.comment]);assert.equal(run.list.innerHTML,JSON.stringify([run.comment]));assert.equal(run.notices.includes('ui.commentPosted'),true);}
  if(completion==='navigation')assert.equal(run.mainNode.innerHTML,'other detail');
});
test('late anonymous POST preserves a new comment draft after the auth detail GET finishes',async()=>{
  const run=anonymousCommentFixture();const pending=run.controller.comment(run.form);
  await run.controller.acceptAuthentication({user:{id:'user-a'}});await new Promise(resolve=>setImmediate(resolve));
  run.oldGet.resolve(run.snapshot([]));await Promise.all(run.paints);run.newForm.draft='next draft';
  run.posted.resolve({comment:run.comment});await pending;
  assert.equal(run.getCount(),1,'an already painted detail should update in place');
  assert.equal(run.newForm.draft,'next draft');assert.deepEqual(run.state.detail.comments,[run.comment]);
  assert.equal(run.notices.includes('ui.commentPosted'),true);
});
