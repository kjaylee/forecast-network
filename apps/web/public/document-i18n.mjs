/** Localize document navigation; authoritative article text keeps its language. */
import {initializeLocale,getLocale,setLocale,t} from './i18n.mjs';
import {languageControlMarkup} from './language-control.mjs';

initializeLocale();
function render(){
  document.querySelectorAll('[data-document-i18n]').forEach(node=>{
    node.textContent=t(node.dataset.documentI18n);
  });
  document.querySelectorAll('[data-document-nav]').forEach(node=>node.setAttribute('aria-label',t('common.documentNavigation')));
  const language=document.querySelector('#document-language');
  const mount=language?.closest('.language-control')||document.querySelector('#document-language-control');
  if(mount){
    const focused=document.activeElement===language;
    mount.outerHTML=languageControlMarkup('document-language');
    if(focused)document.querySelector('#document-language').focus({preventScroll:true});
  }
}
document.addEventListener('change',event=>{if(event.target.id==='document-language'){setLocale(event.target.value);render();}});
render();
