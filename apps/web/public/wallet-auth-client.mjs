/** Browser-bound wallet sign-in. Only public addresses and one-use message proofs enter JavaScript. */
import {connectWallet,preferredAccount,observeWallet,signOwnershipChallenge,solanaAccounts} from './wallet.mjs';
import {t} from './i18n.mjs';

const failure=code=>Object.assign(new Error(t('error.'+code)),{code,translationKey:'error.'+code});
export function validLegacyCode(value){return typeof value==='string'&&/^[A-Za-z0-9_-]{32,256}$/.test(value);}
function walletDisplayName(account){
  if(typeof account?.label!=='string')return null;
  const label=account.label.trim();
  // Count Unicode code points as the server does. Wallet labels are optional
  // display hints, not verified names, ownership proofs, or unique handles.
  if(!label||[...label].length>40||/[\p{Cc}\p{Cs}]/u.test(label))return null;
  return label;
}
export function createWalletAuthClient({api,owner=()=>null,onChange=()=>{},onSuccess=()=>{},onRefresh=()=>{},now=Date.now,connect=connectWallet,observe=observeWallet,sign=signOwnershipChallenge}={}){
  let generation=0,context=null,refreshContext=false,canceling=null,off=null;
  const requests=new Set();
  let state={phase:'idle',wallet:null,accounts:[],address:null,challenge:null,error:null,mode:'login',expectedUserId:null};
  const notify=()=>onChange(state);
  function request(path,body){const pending=api(path,{method:'POST',body});requests.add(pending);pending.finally(()=>requests.delete(pending)).catch(()=>{});return pending;}
  function busy(){return ['connecting','preparing','signing','verifying','loading_session','canceling','importing'].includes(state.phase);}
  function current(token){return generation===token&&(owner()??null)===state.expectedUserId;}
  function prepare(){
    if(canceling)return canceling.then(prepare);
    if(!context||refreshContext){
      const previous=context;refreshContext=false;
      const prepared=(async()=>{
        // A prior bootstrap may still set a cookie. Serialize replacements so a
        // canceled/logged-out context cannot overwrite the next sign-in context.
        if(previous)await previous.catch(()=>{});
        const result=await request('/api/auth/wallet/context',{});
        if(!Number.isSafeInteger(result?.expiresAt)||result.expiresAt<=now())throw failure('wallet_context_required');
        return result;
      })().catch(error=>{if(context===prepared)context=null;throw error;});
      context=prepared;
    }
    return context;
  }
  async function cancel(reason=null){
    if(canceling)return canceling;
    generation++;off?.();off=null;state={...state,phase:'canceling',challenge:null,error:reason};notify();
    canceling=(async()=>{
      try{
        if(context)await context.catch(()=>{});
        await request('/api/auth/wallet/cancel',{});
        // A response already in flight may still carry a session cookie. Drain it
        // before allowing another sign-in, then read the authoritative session.
        await Promise.allSettled([...requests]);
        await onRefresh();
        state={phase:'idle',wallet:null,accounts:[],address:null,challenge:null,error:reason,mode:'login',expectedUserId:owner()??null};
      }catch(error){state={...state,phase:'cancel_failed',error};throw error;}
      finally{context=null;refreshContext=false;canceling=null;notify();}
    })();
    return canceling;
  }
  function reset(){if(busy()||canceling||state.phase==='cancel_failed')return false;generation++;refreshContext=true;off?.();off=null;state={phase:'idle',wallet:null,accounts:[],address:null,challenge:null,error:null,mode:'login',expectedUserId:owner()??null};notify();return true;}
  async function choose(wallet,{mode='login',expectedUserId=null}={}){
    if(busy()||canceling||state.phase==='cancel_failed')return;
    if(!['login','migrate'].includes(mode)||(mode==='migrate'?(typeof expectedUserId!=='string'||!expectedUserId):expectedUserId!==null)||(owner()??null)!==expectedUserId)throw failure('account_changed');
    const token=++generation;off?.();off=null;state={phase:'connecting',wallet,accounts:[],address:null,challenge:null,error:null,mode,expectedUserId};notify();
    try{
      const accounts=await connect(wallet);if(!current(token))return;
      state.accounts=accounts;state.address=preferredAccount(accounts)?.address;if(!state.address)throw failure('account_unavailable');
      off=observe(wallet,event=>{
        if(state.wallet!==wallet)return;
        const available=Object.hasOwn(event,'accounts')?solanaAccounts(event.accounts):solanaAccounts(wallet.accounts);
        if(!available.some(account=>account.address===state.address))void cancel(failure('wallet_account_changed')).catch(()=>{});
      });
      state.phase='account';notify();
    }catch(error){if(current(token)){state.phase='error';state.error=error;notify();}}
  }
  function select(address){if(busy()||!state.accounts.some(account=>account.address===address))return;generation++;state.address=address;state.challenge=null;state.phase='account';state.error=null;notify();}
  async function challenge(){
    if(busy()||!state.wallet||!state.address||state.phase==='cancel_failed')return;
    const token=++generation;state.phase='preparing';state.error=null;notify();
    try{
      await prepare();if(!current(token))return;
      const body={address:state.address,mode:state.mode,expectedUserId:state.expectedUserId};
      const displayName=state.mode==='login'?walletDisplayName(state.accounts.find(account=>account.address===state.address)):null;
      if(displayName!==null)body.displayName=displayName;
      const result=await request('/api/auth/wallet/challenge',body);
      if(!current(token))return;
      if(result?.address!==state.address||result.mode!==state.mode||result.chain!=='solana:devnet'||typeof result.challengeId!=='string'||!result.challengeId||typeof result.message!=='string'||!result.message||!Number.isSafeInteger(result.expiresAt)||result.expiresAt<=now())throw failure('wallet_challenge_invalid');
      state.challenge=Object.freeze({...result});state.phase='challenge';notify();
    }catch(error){if(current(token)){state.phase='error';state.error=error;notify();}}
  }
  async function submit(){
    if(busy()||state.phase!=='challenge'||!state.challenge)return;
    const token=++generation;state.phase='signing';state.error=null;notify();
    try{
      const account=state.accounts.find(item=>item.address===state.address);
      const proof=await sign(state.wallet,account,state.challenge,{now,stillCurrent:()=>current(token)});
      if(!current(token))return;
      if(proof?.challengeId!==state.challenge.challengeId||proof.address!==state.address||typeof proof.signature!=='string')throw failure('wallet_signature_invalid');
      state.phase='verifying';notify();
      const result=await request('/api/auth/wallet/verify',proof);if(!current(token))return;
      if(typeof result?.user?.id!=='string'||!result.user.id||result.wallet?.address!==state.address||result.wallet.chain!=='solana:devnet'||result.points?.userId!==result.user.id||(state.mode==='migrate'&&result.user.id!==state.expectedUserId))throw failure('wallet_login_changed');
      off?.();off=null;state.phase='loading_session';state.challenge=null;
      await onSuccess(result,{stillCurrent:()=>generation===token});
      if(generation===token){state.phase='success';notify();}
    }catch(error){if(current(token)){state.phase='error';state.error=error;notify();}}
  }
  async function importLegacy(code){
    if(busy()||canceling||state.phase==='cancel_failed')return;
    if(!validLegacyCode(code))throw failure('invalid_recovery_code');
    if(owner())throw failure('account_changed');
    const token=++generation;state.phase='importing';state.expectedUserId=null;state.error=null;notify();
    try{
      await prepare();if(!current(token))return;
      const result=await request('/api/auth/login',{recoveryCode:code});if(!current(token))return;
      if(typeof result?.user?.id!=='string'||!result.user.id)throw failure('wallet_login_changed');
      off?.();off=null;state.phase='loading_session';
      await onSuccess(result,{stillCurrent:()=>generation===token});
      if(generation===token){state.phase='success';notify();}
    }catch(error){if(current(token)){state.phase='error';state.error=error;notify();}}
  }
  return {get:()=>state,busy,prepare,reset,choose,select,challenge,submit,cancel,importLegacy};
}
