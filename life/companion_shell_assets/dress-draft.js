/** A dressing page's uncommitted look; browsing never writes to the desktop. */
export function createDressDraft(commit) {
  let base=null, value=null, saving=false;
  const copy=x=>structuredClone(x);
  const dirty=()=>base!==null&&JSON.stringify(base)!==JSON.stringify(value);
  return {
    get dirty(){return dirty()}, get saving(){return saving}, get value(){return copy(value)}, get applied(){return copy(base)},
    receive(next){if(dirty()||saving)return false;base=copy(next);value=copy(next);return true},
    update(patch){value={...value,...copy(patch)}},
    reset(){value=copy(base);return copy(value)},
    async save(){if(!dirty()||saving)return false;saving=true;const selected=copy(value);try{await commit(selected);base=selected;return true}finally{saving=false}},
  };
}

export function mountDressDraft({read,restore}) {
  const bar=document.createElement('div');bar.style.cssText='position:sticky;bottom:0;z-index:20;display:flex;gap:12px;align-items:center;padding:14px;background:var(--bg,#202733);border-top:1px solid #697080';
  const hint=document.createElement('span');hint.style.flex='1';
  const cancel=document.createElement('button');cancel.type='button';cancel.textContent='取消修改';
  const save=document.createElement('button');save.type='button';save.textContent='保存装扮';
  bar.append(hint,cancel,save);document.querySelector('#dress').append(bar);
  const draft=createDressDraft(async value=>{const r=await fetch('/api/skin',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(value)});if(!r.ok)throw Error('保存失败，请重试')});
  function render(message){hint.textContent=message||(draft.dirty?'正在预览 · 保存后才会应用':'浏览与预览不会改变桌面形象');save.disabled=!draft.dirty||draft.saving;cancel.disabled=!draft.dirty||draft.saving}
  cancel.onclick=()=>{restore(draft.reset());render()};
  save.onclick=async()=>{draft.update(read());const task=draft.save();render();try{await task;render('已保存')}catch(e){render(e.message)}};
  render();
  return {get dirty(){return draft.dirty||draft.saving},receive(value){const accepted=draft.receive(value);render();return accepted},stage(){draft.update(read());render();return Promise.resolve()},isApplied(id){return draft.applied?.skin.figure===id}};
}
