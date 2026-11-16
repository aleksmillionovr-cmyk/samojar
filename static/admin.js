// Общие помощники админки
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function debounce(fn,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}}
function renderButtons(el,rows){if(!el)return;el.innerHTML=(rows||[]).map(r=>'<div class="r">'+r.map(t=>'<span>'+esc(t)+'</span>').join('')+'</div>').join('')}
const CSRF=document.querySelector('meta[name=csrf]')?.content||'';

// кнопки форматирования над полями: оборачивают выделенный текст в тег
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-wrap]');if(!b)return;e.preventDefault();
  const ta=document.getElementById(b.dataset.for),[o,c]=b.dataset.wrap.split('|'),s=ta.selectionStart,en=ta.selectionEnd;
  let inner=ta.value.slice(s,en);
  if(o.startsWith('<a ')){const url=prompt('Ссылка (https://…)','https://');if(!url)return;ta.setRangeText('<a href="'+url.replace(/"/g,'&quot;')+'">'+(inner||'текст ссылки')+'</a>',s,en,'end');}
  else ta.setRangeText(o+inner+c,s,en,'end');
  ta.focus();ta.dispatchEvent(new Event('input',{bubbles:true}));
});

// подтверждение опасных действий
document.addEventListener('submit',e=>{const m=e.target.dataset.confirm;if(m&&!confirm(m))e.preventDefault()});

// строки таблицы-ссылки
document.addEventListener('click',e=>{
  const tr=e.target.closest('tr[data-href]');
  if(!tr||e.target.closest('a,button,form,input,select,textarea'))return;
  location.href=tr.dataset.href;
});

// мобильное меню
document.addEventListener('click',e=>{
  if(e.target.closest('[data-menu]'))document.body.classList.toggle('menu-open');
  else if(e.target.closest('.scrim'))document.body.classList.remove('menu-open');
});

// показать/скрыть секретное поле
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-reveal]');if(!b)return;e.preventDefault();
  const i=document.getElementById(b.dataset.reveal);i.type=i.type==='password'?'text':'password';
});

// копирование в буфер
document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-copy]');if(!b)return;e.preventDefault();
  try{await navigator.clipboard.writeText(b.dataset.copy);const t=b.textContent;b.textContent='Скопировано ✓';setTimeout(()=>b.textContent=t,1500)}catch(_){}
});

// живое превью: <textarea data-preview-url="..." data-preview-target="pv_id">
function livePreview(ta,url,target,extra){
  const run=debounce(async()=>{
    const fd=new FormData();fd.append('csrf',CSRF);fd.append('value',ta.value);
    if(extra)for(const[k,v]of Object.entries(extra()))fd.append(k,v);
    try{const r=await fetch(url,{method:'POST',body:fd});const j=await r.json();
      document.getElementById(target).innerHTML=j.html;renderButtons(document.getElementById(target+'-btns'),j.buttons);
      const er=document.getElementById(target+'-errs');if(er)er.innerHTML=(j.errors||[]).map(x=>'• '+x).join('<br>');
    }catch(_){}
  },250);
  ta.addEventListener('input',run);return run;
}

// предупреждение о несохранённых изменениях
document.querySelectorAll('form[data-dirty]').forEach(f=>{
  let dirty=false;f.addEventListener('input',()=>dirty=true);f.addEventListener('submit',()=>dirty=false);
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});
});
