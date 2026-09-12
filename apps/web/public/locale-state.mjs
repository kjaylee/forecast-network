/** Short-lived DOM state for a language repaint. Never persisted or sent to APIs. */
export function captureLocaleState(document){
  const controls=[...document.querySelectorAll('input,textarea,select')].filter(node=>
    !node.classList.contains('language-select')&&!node.readOnly&&node.type!=='file');
  return {
    controls:controls.map(node=>({id:node.id,formId:node.form?.id,name:node.name,value:node.value,checked:node.checked})),
    details:[...document.querySelectorAll('details')].map(node=>node.open),
    errors:[...document.querySelectorAll('.error-message[id]')].filter(node=>node.textContent).map(node=>({id:node.id,text:node.textContent,code:node.dataset.errorCode,key:node.dataset.uiKey})),
    focusId:document.activeElement?.id,
    selectionStart:document.activeElement?.selectionStart,
    selectionEnd:document.activeElement?.selectionEnd,
  };
}
export function restoreLocaleState(document,snapshot,{translateError=entry=>entry.text}={}){
  for(const saved of snapshot.controls){
    const node=saved.id?document.getElementById(saved.id):document.getElementById(saved.formId)?.elements?.namedItem(saved.name);
    if(!node||node.readOnly||node.type==='file')continue;
    node.value=saved.value;if('checked' in node)node.checked=saved.checked;
  }
  document.querySelectorAll('details').forEach((node,index)=>{if(index<snapshot.details.length)node.open=snapshot.details[index];});
  for(const entry of snapshot.errors){const node=document.getElementById(entry.id);if(node){node.textContent=translateError(entry);node.dataset.errorCode=entry.code||'';node.dataset.uiKey=entry.key||'';}}
  const focused=document.getElementById(snapshot.focusId);
  if(focused){focused.focus({preventScroll:true});if(Number.isInteger(snapshot.selectionStart))try{focused.setSelectionRange(snapshot.selectionStart,snapshot.selectionEnd);}catch{/* Range/number controls have no text cursor. */}}
}
