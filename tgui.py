"""tgui.py — Telegram Web सारखा UI (एकच HTML फाइल, बाहेरची कोणतीही library नाही)."""

ENTER_PAGE = r'''<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>…</title><body style="font:16px system-ui;background:#0e1621;color:#f5f5f5;padding:30px">⏳
<script>
const k=new URLSearchParams(location.search).get('k');history.replaceState(null,'','/app/');
fetch('/app/enter',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'app'},body:JSON.stringify({k})})
 .then(r=>r.ok?location.replace('/app/'):document.body.textContent='✖ Link संपला किंवा आधीच वापरला').catch(()=>document.body.textContent='✖ error');
</script>'''

UI_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Telegram</title>
<style>
:root{--bg:#e6ebee;--side:#fff;--in:#fff;--out:#eeffde;--acc:#3390ec;--fg:#000;--mut:#707579;--bd:#dfe1e5;--hov:#f4f4f5;--sel:#3390ec;--bdg:#3390ec;--bdgm:#c4c9cc;--ink:#fff;--sh:0 1px 2px #0002;--pat:#0001}
[data-theme=dark]{--bg:#0e1621;--side:#17212b;--in:#182533;--out:#2b5278;--acc:#6ab3f3;--fg:#f5f5f5;--mut:#708499;--bd:#0b131b;--hov:#202b36;--sel:#2b5278;--bdg:#3e8cd0;--bdgm:#3e546a;--ink:#fff;--sh:0 1px 2px #0006;--pat:#ffffff08}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html,body{height:100%;margin:0;overflow:hidden}
body{background:var(--bg);color:var(--fg);font:15px/1.35 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
button,input,textarea,select{font:inherit;color:inherit}button{cursor:pointer;background:none;border:0}
#app{display:flex;height:100%}#side{width:min(420px,34vw);min-width:320px;background:var(--side);display:flex;flex-direction:column;border-right:1px solid var(--bd);position:relative}
#main{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--bg);position:relative}
#info{width:360px;background:var(--side);border-left:1px solid var(--bd);display:none;flex-direction:column;overflow:auto}
.info-on #info{display:flex}.hd{display:flex;align-items:center;gap:8px;padding:8px 12px;min-height:56px;background:var(--side);border-bottom:1px solid var(--bd)}
.ib{width:40px;height:40px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:19px;color:var(--mut);flex:none}.ib:hover{background:var(--hov)}
#search{flex:1;min-width:0;border:0;background:var(--hov);border-radius:20px;padding:9px 16px;outline:0}
#folders{display:flex;gap:2px;padding:0 8px;overflow-x:auto;border-bottom:1px solid var(--bd);scrollbar-width:none}
#folders button{padding:11px 12px;color:var(--mut);font-weight:500;border-bottom:3px solid transparent;white-space:nowrap}#folders button.on{color:var(--acc);border-color:var(--acc)}
#folders .n{background:var(--bdg);color:#fff;border-radius:10px;font-size:11px;padding:0 6px;margin-left:5px}
#list{flex:1;overflow-y:auto}.row{display:flex;gap:10px;padding:8px 12px;cursor:pointer;align-items:center}.row:hover{background:var(--hov)}.row.on{background:var(--sel);color:#fff}.row.on .mut,.row.on .t{color:#ffffffcc}
.av{position:relative;width:54px;height:54px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;font-size:20px;overflow:hidden}
.av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.dot{position:absolute;right:1px;bottom:1px;width:13px;height:13px;border-radius:50%;background:#0ac630;border:2px solid var(--side)}
.rm{flex:1;min-width:0}.r1,.r2{display:flex;justify-content:space-between;gap:8px;align-items:center}.r1 b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}.t{color:var(--mut);font-size:12px;white-space:nowrap}
.r2 .mut{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.mut{color:var(--mut)}.typ{color:var(--acc)}
.bd{background:var(--bdg);color:#fff;border-radius:11px;min-width:22px;height:22px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center;font-size:12.5px;font-weight:600;flex:none}.bd.m{background:var(--bdgm)}
.sec{padding:8px 14px;color:var(--acc);font-weight:600;font-size:13px}
#fab{position:absolute;right:16px;bottom:16px;width:54px;height:54px;border-radius:50%;background:var(--acc);color:#fff;font-size:22px;box-shadow:0 3px 10px #0005}
#drawer{position:absolute;inset:0;background:#0007;display:none;z-index:30}#drawer.on{display:block}
#dr{width:min(320px,86%);height:100%;background:var(--side);overflow:auto;display:flex;flex-direction:column}
#dr .it{display:flex;gap:14px;align-items:center;padding:12px 18px;cursor:pointer}#dr .it:hover{background:var(--hov)}#dr .ac{padding:8px 18px;display:flex;gap:10px;align-items:center;cursor:pointer}#dr .ac.on{background:var(--hov)}
#cbar{display:flex;align-items:center;gap:10px;padding:6px 12px;min-height:56px;background:var(--side);border-bottom:1px solid var(--bd)}
#cbar .tt{flex:1;min-width:0;cursor:pointer}#cbar .tt b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#cbar .st{font-size:13px;color:var(--mut)}#cbar .st.on{color:var(--acc)}
#back{display:none}#pin{display:none;gap:10px;align-items:center;padding:6px 14px;background:var(--side);border-bottom:1px solid var(--bd);cursor:pointer}#pin .bar{width:3px;align-self:stretch;background:var(--acc);border-radius:2px}
#msgs{flex:1;overflow-y:auto;padding:10px 0 6px;position:relative;background-image:radial-gradient(var(--pat) 1.5px,transparent 1.5px);background-size:22px 22px}
#mi{max-width:760px;margin:0 auto;padding:0 10px;display:flex;flex-direction:column;gap:2px}
.day{align-self:center;background:#00000040;color:#fff;border-radius:14px;padding:2px 12px;font-size:13px;margin:8px 0;font-weight:500}.unr{align-self:stretch;text-align:center;background:#00000020;color:var(--fg);padding:3px;font-size:13px;margin:6px -10px}
.svc{align-self:center;background:#00000040;color:#fff;border-radius:14px;padding:2px 12px;font-size:13px;margin:4px 0}
.mr{display:flex;gap:8px;align-items:flex-end}.mr.out{flex-direction:row-reverse}.mr .av{width:34px;height:34px;font-size:14px}.mr .sp{width:34px;flex:none}
.m{position:relative;max-width:min(78%,520px);background:var(--in);border-radius:12px;padding:5px 9px 4px;box-shadow:var(--sh);word-break:break-word;min-width:60px}
.out .m{background:var(--out)}.m.tl{border-bottom-left-radius:4px}.out .m.tl{border-bottom-left-radius:12px;border-bottom-right-radius:4px}.m.hl{animation:hl 1.4s}.m.sel{outline:2px solid var(--acc)}
@keyframes hl{0%,60%{filter:brightness(1.45)}100%{filter:none}}
.m.nobg{background:none;box-shadow:none;padding:0}.mn{font-weight:600;font-size:14px;margin-bottom:1px}.mt{white-space:pre-wrap}.mt a,.mt .lk{color:var(--acc);text-decoration:none}.mt a:hover{text-decoration:underline}
.mt code{font-family:ui-monospace,Consolas,monospace;background:#00000022;border-radius:4px;padding:0 4px}.mt pre{margin:4px 0;background:#00000022;padding:6px 8px;border-radius:6px;overflow:auto}.mt blockquote{margin:2px 0;padding:0 0 0 8px;border-left:3px solid var(--acc)}
.sp-l{background:#888;color:transparent;border-radius:4px;cursor:pointer;user-select:none}.sp-l.rv{background:none;color:inherit}
.meta{float:right;margin:6px 0 -2px 10px;font-size:11.5px;color:var(--mut);display:flex;gap:4px;align-items:center;height:14px}.out .meta{color:#6fa66a}[data-theme=dark] .out .meta{color:#9ec2e8}
.tk{color:var(--acc);letter-spacing:-5px;margin-right:5px}.fw{color:var(--acc);font-size:13px;font-weight:600}.rp{border-left:2px solid var(--acc);padding:1px 8px;margin-bottom:3px;cursor:pointer;border-radius:2px;background:#00000010}.rp b{display:block;color:var(--acc);font-size:13px}.rp span{display:block;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px}
.ph,.vid{position:relative;border-radius:8px;overflow:hidden;margin:-2px -4px 3px;cursor:pointer;background:#00000020;min-width:180px;min-height:90px}.ph img,.vid img,.vid video{display:block;width:100%;max-height:420px;object-fit:cover}.vid video{object-fit:contain;background:#000}
.play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}.play i{width:54px;height:54px;border-radius:50%;background:#0009;color:#fff;display:flex;align-items:center;justify-content:center;font-style:normal;font-size:22px}
.chip{position:absolute;left:8px;top:8px;background:#0008;color:#fff;border-radius:10px;padding:1px 8px;font-size:12px}.stk{width:170px;max-width:60vw;display:block}.stk-e{font-size:80px;line-height:1}
.file{display:flex;gap:10px;align-items:center;min-width:220px}.fi{width:46px;height:46px;border-radius:50%;background:var(--acc);color:#fff;display:flex;align-items:center;justify-content:center;font-size:20px;flex:none}.file b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}.file a{color:inherit;text-decoration:none}
audio{max-width:100%;height:40px}.card{border-left:3px solid var(--acc);padding:2px 8px;margin:3px 0;background:#00000010;border-radius:4px}.card b{color:var(--acc)}.card .ds{font-size:13.5px}
.poll b.q{display:block;margin-bottom:4px}.po{position:relative;padding:5px 8px;margin:3px 0;border-radius:6px;background:#00000014;cursor:pointer;overflow:hidden}.po i{position:absolute;left:0;top:0;bottom:0;background:var(--acc);opacity:.28}.po span{position:relative;display:flex;justify-content:space-between;gap:10px}
.rx{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 0}.rx button{background:#00000018;border-radius:12px;padding:1px 8px;font-size:14px}.rx button.me{background:var(--acc);color:#fff}
.bt{display:flex;flex-direction:column;gap:3px;margin-top:5px;min-width:200px}.bt div{display:flex;gap:3px}.bt button{flex:1;background:#00000030;color:#fff;border-radius:6px;padding:7px 4px;font-size:14px}
#dn{position:absolute;right:18px;bottom:12px;display:none;width:44px;height:44px;border-radius:50%;background:var(--side);box-shadow:0 2px 8px #0004;font-size:18px;color:var(--mut)}#dn .bd{position:absolute;top:-8px;right:-4px}
#ups{max-width:760px;margin:0 auto;width:100%;padding:0 10px}.up{background:var(--side);border-radius:10px;padding:6px 10px;margin:4px 0;font-size:13px;box-shadow:var(--sh)}.up .pb{height:4px;border-radius:2px;background:var(--bd);margin-top:4px;overflow:hidden}.up .pb i{display:block;height:100%;background:var(--acc);width:0}
#comp{background:var(--bg)}#cin{max-width:760px;margin:0 auto;padding:6px 10px 10px}#bar2{display:none;background:var(--side);border-radius:10px 10px 0 0;padding:6px 12px;gap:8px;align-items:center}
#box{display:flex;gap:6px;align-items:flex-end}#pillbox{flex:1;display:flex;align-items:flex-end;background:var(--side);border-radius:22px;padding:2px 4px;box-shadow:var(--sh)}
#txt{flex:1;border:0;background:none;outline:0;resize:none;max-height:180px;padding:11px 6px;line-height:1.3}#send{width:48px;height:48px;border-radius:50%;background:var(--acc);color:#fff;font-size:20px;flex:none;box-shadow:var(--sh)}
#emo{display:none;background:var(--side);border-radius:12px;margin-bottom:6px;padding:6px;max-height:190px;overflow:auto;grid-template-columns:repeat(auto-fill,minmax(38px,1fr))}#emo.on{display:grid}#emo button{font-size:24px;padding:3px;border-radius:6px}#emo button:hover{background:var(--hov)}
#sel{display:none;align-items:center;gap:12px;padding:8px 14px;background:var(--side);border-bottom:1px solid var(--bd)}
.menu{position:fixed;z-index:60;background:var(--side);border-radius:10px;box-shadow:0 4px 18px #0006;padding:5px 0;min-width:190px;max-width:260px}.menu .mi2{padding:8px 16px;cursor:pointer;display:flex;gap:12px;white-space:nowrap}.menu .mi2:hover{background:var(--hov)}.menu .rb{display:flex;gap:2px;padding:4px 8px;border-bottom:1px solid var(--bd)}.menu .rb button{font-size:22px;padding:2px 4px;border-radius:6px}.menu .rb button:hover{background:var(--hov)}.menu .red{color:#e53935}
.ov{position:fixed;inset:0;background:#0009;z-index:50;display:flex;align-items:center;justify-content:center;padding:14px}.mdl{background:var(--side);border-radius:14px;width:min(440px,100%);max-height:88vh;display:flex;flex-direction:column;box-shadow:0 8px 30px #0008}
.mdl h3{margin:0;padding:14px 18px 6px}.mb{padding:6px 18px 12px;overflow:auto;display:flex;flex-direction:column;gap:8px}.mf{display:flex;gap:8px;justify-content:flex-end;padding:8px 14px 12px}.mf button{padding:8px 14px;border-radius:8px;color:var(--acc);font-weight:600}.mf button.p{background:var(--acc);color:#fff}.mf button.d{color:#e53935}
.mdl input[type=text],.mdl input[type=password],.mdl input[type=tel],.mdl input:not([type]),.mdl select,.mdl textarea,.mdl input[type=datetime-local]{width:100%;border:1px solid var(--bd);border-radius:8px;padding:9px 12px;background:var(--bg);outline:0}
.mdl label{display:flex;gap:8px;align-items:center}.err{color:#e53935;min-height:1em;font-size:13px}.sw{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:4px 0}
#lb{position:fixed;inset:0;background:#000e;z-index:70;display:none;align-items:center;justify-content:center}#lb img{max-width:96vw;max-height:92vh}#lb .x{position:absolute;top:10px;right:14px;color:#fff;font-size:26px}#lb .nv{position:absolute;top:50%;color:#fff;font-size:34px;padding:10px}
#toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#000c;color:#fff;border-radius:18px;padding:8px 16px;z-index:90;display:none;max-width:90vw}
#info .big{display:flex;flex-direction:column;align-items:center;padding:18px 14px 8px}#info .big .av{width:110px;height:110px;font-size:40px}.kv{padding:8px 18px}.kv small{display:block;color:var(--mut)}
.tabs{display:flex;border-bottom:1px solid var(--bd);overflow-x:auto}.tabs button{padding:10px 12px;color:var(--mut);white-space:nowrap}.tabs button.on{color:var(--acc);border-bottom:2px solid var(--acc)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:2px;padding:2px}.grid div{aspect-ratio:1;background:#00000030;position:relative;overflow:hidden;cursor:pointer}.grid img{width:100%;height:100%;object-fit:cover}
.empty{margin:auto;color:var(--mut);padding:30px;text-align:center}
@media(max-width:820px){#side{width:100%;min-width:0}#main{position:absolute;inset:0;transform:translateX(100%);transition:transform .2s;z-index:10}.chat #main{transform:none}#back{display:inline-flex}.info-on #info{position:absolute;inset:0;width:100%;z-index:20}.m{max-width:86%}}
</style></head><body>
<div id="app">
 <aside id="side"><div class="hd"><button class="ib" id="menu" title="Menu">☰</button><input id="search" placeholder="Search" autocomplete="off"></div>
  <div id="folders"></div><div id="list"></div><button id="fab" title="New message">✎</button>
  <div id="drawer"><div id="dr"></div></div></aside>
 <main id="main"><div id="cbar" style="display:none"><button class="ib" id="back">←</button><div id="cav"></div><div class="tt" id="ctt"><b></b><span class="st"></span></div>
   <button class="ib" id="csr" title="Search">🔍</button><button class="ib" id="cinfo" title="Info">ⓘ</button><button class="ib" id="cmore">⋮</button></div>
  <div id="sel"><button class="ib" id="selx">✕</button><b id="selc" style="flex:1"></b><button class="ib" id="self">➦</button><button class="ib" id="seld">🗑</button></div>
  <div id="pin"><div class="bar"></div><div style="flex:1;min-width:0"><b style="color:var(--acc);font-size:13px">Pinned message</b><div class="mut" id="pint" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div></div></div>
  <div id="msgs"><div id="mi"><div class="empty">Select a chat to start messaging</div></div><button id="dn">⌄</button></div>
  <div id="ups"></div>
  <div id="comp" style="display:none"><div id="cin"><div id="bar2"><div style="flex:1;min-width:0"></div><button class="ib" id="bar2x">✕</button></div><div id="emo"></div>
   <div id="box"><div id="pillbox"><button class="ib" id="emob">☺</button><textarea id="txt" rows="1" placeholder="Message"></textarea><button class="ib" id="md" title="Markdown formatting">𝐁</button><button class="ib" id="att">📎</button></div>
   <button id="send">➤</button></div><input id="file" type="file" multiple hidden></div></div></main>
 <aside id="info"></aside>
</div>
<div id="lb"><button class="x">✕</button><button class="nv" style="left:6px">‹</button><img alt=""><button class="nv" style="right:6px">›</button></div><div id="toast"></div>
<script>
'use strict';
const $=(s,r)=>(r||document).querySelector(s);
const h=(t,p,...k)=>{const e=document.createElement(t);if(p)for(const[a,v]of Object.entries(p)){if(v==null||v===false)continue;if(a==='on')for(const[ev,f]of Object.entries(v))e.addEventListener(ev,f);else if(a==='text')e.textContent=v;else if(a==='class')e.className=v;else if(a==='style')e.style.cssText=v;else e.setAttribute(a,v===true?'':v)}for(const c of k.flat(9)){if(c==null||c===false)continue;e.append(c.nodeType?c:document.createTextNode(String(c)))}return e};
const LS={get(k,d){try{const v=localStorage.getItem('tg_'+k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem('tg_'+k,JSON.stringify(v))}catch(e){}}};
const Q=o=>new URLSearchParams(o).toString(),sleep=ms=>new Promise(r=>setTimeout(r,ms));
const S={acc:null,accs:[],chat:null,cd:null,dl:[],folder:'all',archived:false,msgs:[],readOut:0,readIn:0,pinned:null,typing:{},seq:0,sel:new Set(),reply:null,edit:null,uploads:{},dls:{},bw:null,defApi:false,loadingOld:false,noMore:false,gsearch:[],q:'',info:false,drafts:{},unreadFrom:0,
 set:Object.assign({theme:'auto',avatars:true,previews:true,notif:false,sound:true,md:false,enter:true},LS.get('set',{}))};
const EMOJI='😀😁😂🤣😊😍😘😎🤔😢😭😡👍👎👏🙏💪🔥❤️💔🎉✨🌹🎂🍕☕🚀✅❌⚠️📌📎💯😴🤝👀🙈😅😉😇🥰😋🤗🤩😏😬🙄😤😱🤯🥳😜'.match(/\p{Extended_Pictographic}\uFE0F?/gu)||[];
const REACT=['👍','❤️','🔥','🥰','👏','😁','🤔','😢','🎉','👎'];
const sz=n=>n>=1073741824?(n/1073741824).toFixed(2)+' GB':n>=1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' KB';
const mbs=n=>Math.round(n/1048576)+' MB';
const BIGF=25*1048576,dlKey=m=>S.acc+':'+S.chat+':'+m.id,pctOf=st=>st.size?Math.min(100,Math.round((st.done||0)*100/st.size)):0;
const dur=s=>{s=Math.round(s||0);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
const tm=t=>t?new Date(t*1e3).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
const dayKey=t=>{const d=new Date(t*1e3);return d.getFullYear()+'-'+d.getMonth()+'-'+d.getDate()};
function dayLabel(t){const d=new Date(t*1e3),n=new Date();if(dayKey(t)===dayKey(n/1e3))return 'Today';const y=new Date(n-864e5);if(dayKey(t)===dayKey(y/1e3))return 'Yesterday';return d.toLocaleDateString([],{day:'numeric',month:'long',year:d.getFullYear()===n.getFullYear()?undefined:'numeric'})}
function listTime(t){if(!t)return '';const d=new Date(t*1e3),n=new Date();if(dayKey(t)===dayKey(n/1e3))return tm(t);if(n-d<6048e5)return d.toLocaleDateString([],{weekday:'short'});return d.toLocaleDateString([],{day:'2-digit',month:'2-digit',year:'2-digit'})}
const COLS=['#e17076','#faa774','#a695e7','#7bc862','#6ec9cb','#65aadd','#ee7aae'];
const colorOf=id=>COLS[Math.abs(Number(id)||0)%COLS.length];
function toast(m,ms){const t=$('#toast');t.textContent=m;t.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(()=>t.style.display='none',ms||3200)}
async function api(path,body,opt){const o={credentials:'same-origin'};if(opt&&opt.raw){o.method='POST';o.headers={'X-Requested-With':'app','Content-Type':'application/octet-stream'};o.body=opt.raw}
 else if(body!==undefined){o.method='POST';o.headers={'Content-Type':'application/json','X-Requested-With':'app'};o.body=JSON.stringify(body)}
 const r=await fetch(path,o);let d={};try{d=await r.json()}catch(e){}
 if(!r.ok){if(r.status===404&&!d.error)throw new Error('Session expired — get a new link from the bot');const e=new Error(d.error||('error '+r.status));e.status=r.status;e.data=d;throw e}return d}
const A=(o)=>Object.assign({acc:S.acc},o);
const cur=()=>S.accs.find(a=>a.id===S.acc)||{};
function saveSet(){LS.set('set',S.set);applyTheme()}
function applyTheme(){let t=S.set.theme;if(t==='auto')t=(window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';document.documentElement.dataset.theme=t}
/* ---------- avatar ---------- */
function avatar(id,name,size,online){const a=h('div',{class:'av',style:`background:${colorOf(id)};${size?`width:${size}px;height:${size}px;font-size:${size*.38}px`:''}`,text:(name||'?').trim().slice(0,1).toUpperCase()});
 if(S.set.avatars&&S.acc){const i=h('img',{alt:'',loading:'lazy',src:'/app/api/avatar?'+Q({acc:S.acc,chat:id}),on:{error(){i.remove()}}});a.append(i)}
 if(online)a.append(h('i',{class:'dot'}));return a}
/* ---------- rich text ---------- */
const SAFE=/^(https?:\/\/|mailto:)/i;
function wrapEnt(e,node,seg){const T=e.t;let el;
 if(T==='bold')el=h('b');else if(T==='italic')el=h('i');else if(T==='underline')el=h('u');else if(T==='strike')el=h('s');else if(T==='code')el=h('code');else if(T==='pre')el=h('pre');
 else if(T==='blockquote')el=h('blockquote');else if(T==='spoiler'){el=h('span',{class:'sp-l',on:{click(){el.classList.add('rv')}}})}
 else if(T==='url'){const u=/^https?:\/\//i.test(seg)?seg:'http://'+seg;el=SAFE.test(u)?h('a',{href:u,target:'_blank',rel:'noopener noreferrer'}):h('span')}
 else if(T==='texturl'){el=e.u&&SAFE.test(e.u)?h('a',{href:e.u,target:'_blank',rel:'noopener noreferrer',title:e.u}):h('span')}
 else if(T==='email'){el=h('a',{href:'mailto:'+seg})}
 else if(T==='mention'){el=h('span',{class:'lk',style:'cursor:pointer',on:{click(){openByQuery(seg)}}})}
 else if(T==='hashtag'||T==='cashtag'){el=h('span',{class:'lk',style:'cursor:pointer',on:{click(){$('#search').value=seg;onSearch()}}})}
 else if(T==='botcommand'){el=h('span',{class:'lk',style:'cursor:pointer',on:{click(){const t=$('#txt');t.value=seg;t.focus()}}})}
 else el=h('span');el.append(node);return el}
function richText(text,ents){const f=document.createDocumentFragment();if(!text)return f;const es=(ents||[]).filter(e=>e.l>0&&e.o>=0&&e.o<text.length);
 if(!es.length){f.append(text);return f}
 const pts=new Set([0,text.length]);es.forEach(e=>{pts.add(e.o);pts.add(Math.min(text.length,e.o+e.l))});const P=[...pts].sort((a,b)=>a-b);
 for(let i=0;i<P.length-1;i++){const a=P[i],b=P[i+1],seg=text.slice(a,b);if(!seg)continue;let node=document.createTextNode(seg);
  for(const e of es.filter(e=>e.o<=a&&e.o+e.l>=b).sort((x,y)=>x.l-y.l))node=wrapEnt(e,node,seg);f.append(node)}return f}
function plainLinks(text){const f=document.createDocumentFragment();const re=/https?:\/\/[^\s<>"']+/g;let last=0,m;while((m=re.exec(text))){f.append(text.slice(last,m.index));f.append(h('a',{href:m[0],target:'_blank',rel:'noopener noreferrer',text:m[0]}));last=m.index+m[0].length}f.append(text.slice(last));return f}
/* ---------- menus / modals ---------- */
let MENU=null;function closeMenu(){if(MENU){MENU.remove();MENU=null}}
function showMenu(x,y,items,top){closeMenu();const m=h('div',{class:'menu'});if(top)m.append(top);
 for(const it of items){if(!it)continue;m.append(h('div',{class:'mi2'+(it.red?' red':''),on:{click(){closeMenu();it.fn()}}},h('span',{text:it.ic||''}),it.t))}
 document.body.append(m);const w=m.offsetWidth||220,hh=m.offsetHeight||200;m.style.left=Math.max(6,Math.min(x,innerWidth-w-6))+'px';m.style.top=Math.max(6,Math.min(y,innerHeight-hh-6))+'px';MENU=m}
document.addEventListener('click',e=>{if(MENU&&!MENU.contains(e.target))closeMenu()},true);
function modal(title,body,actions){const ov=h('div',{class:'ov',on:{mousedown(e){if(e.target===ov)close()}}});
 const close=()=>ov.remove();const mf=h('div',{class:'mf'});for(const a of actions||[]){mf.append(h('button',{class:a.c||'',text:a.t,on:{click(){a.fn?a.fn(close):close()}}}))}
 ov.append(h('div',{class:'mdl'},title?h('h3',{text:title}):null,h('div',{class:'mb'},body),(actions&&actions.length)?mf:null));document.body.append(ov);return close}
function confirmBox(msg,okText,fn,red){return modal('',h('div',{text:msg}),[{t:'Cancel'},{t:okText||'OK',c:red?'d':'p',fn(c){c();fn()}}])}
/* ---------- chat list ---------- */
const FOLDERS=[['all','All'],['unread','Unread'],['user','Personal'],['group','Groups'],['channel','Channels'],['bot','Bots']];
function drawFolders(){const f=$('#folders');f.replaceChildren(...FOLDERS.map(([k,n])=>{const c=k==='unread'?S.dl.filter(d=>d.unread&&!d.muted).length:0;return h('button',{class:S.folder===k?'on':'',on:{click(){S.folder=k;drawFolders();drawList()}}},n,c?h('span',{class:'n',text:c}):null)}))}
function lastText(d){const t=S.typing[d.id];if(t&&t.exp>Date.now())return h('span',{class:'typ',text:(t.who&&d.type!=='user'?t.who+' ':'')+(t.action==='recording'?'is recording…':'typing…')});return h('span',{class:'mut',text:(d.out&&d.last?'You: ':'')+d.last})}
function drawList(){const q=S.q.toLowerCase();let L=S.dl.filter(d=>(!q||d.name.toLowerCase().includes(q)));
 if(S.folder==='unread')L=L.filter(d=>d.unread);else if(S.folder==='bot')L=L.filter(d=>d.bot);else if(S.folder!=='all')L=L.filter(d=>d.type===S.folder&&!d.bot);
 const rows=[];if(S.archived)rows.push(h('div',{class:'sec',text:'Archived chats'}));
 for(const d of L){const ub=d.unread?h('span',{class:'bd'+(d.muted?' m':''),text:d.unread>999?'999+':d.unread}):(d.mentions?h('span',{class:'bd',text:'@'}):(d.pinned?h('span',{class:'mut',text:'📌'}):null));
  rows.push(h('div',{class:'row'+(d.id===S.chat?' on':''),on:{click(){openChat(d)},contextmenu(e){e.preventDefault();chatMenu(e.clientX,e.clientY,d)}}},avatar(d.id,d.name,0,d.online),
   h('div',{class:'rm'},h('div',{class:'r1'},h('b',{text:(d.verified?'✔ ':'')+d.name}),h('span',{class:'t',text:(d.muted?'🔇 ':'')+listTime(d.ts)})),h('div',{class:'r2'},lastText(d),ub))))}
 if(S.q.length>=2){if(S.gres&&S.gres.dialog)rows.push(h('div',{class:'sec',text:'Found'}),dialogRow(S.gres.dialog));
  if(S.gsearch.length){rows.push(h('div',{class:'sec',text:'Messages'}));for(const m of S.gsearch)rows.push(h('div',{class:'row',on:{click(){openById(m.chat,m.id)}}},avatar(m.chat,m.chat_name),h('div',{class:'rm'},h('div',{class:'r1'},h('b',{text:m.chat_name||'Chat'}),h('span',{class:'t',text:listTime(m.ts)})),h('div',{class:'r2'},h('span',{class:'mut',text:m.text||'📎'})))))}}
 if(!rows.length)rows.push(h('div',{class:'empty',text:S.dl.length?'No chats found':'Loading…'}));$('#list').replaceChildren(...rows);
 const n=S.dl.reduce((a,d)=>a+(d.muted?0:d.unread),0);document.title=(n?`(${n}) `:'')+'Telegram'}
function dialogRow(d){return h('div',{class:'row',on:{click(){openChat(d)}}},avatar(d.id,d.name),h('div',{class:'rm'},h('div',{class:'r1'},h('b',{text:d.name})),h('div',{class:'r2'},h('span',{class:'mut',text:d.username?'@'+d.username:d.type}))))}
async function loadDialogs(){if(!S.acc)return;try{S.dl=(await api('/app/api/dialogs?'+Q({acc:S.acc,archived:S.archived?1:0}))).dialogs;drawFolders();drawList()}catch(e){$('#list').replaceChildren(h('div',{class:'empty',text:e.message}))}}
async function loadState(){const s=await api('/app/api/state');S.accs=s.accounts;S.bw=s.bw;S.defApi=s.default_api;S.chunk=s.chunk;if(!S.acc||!S.accs.find(a=>a.id===S.acc))S.acc=(LS.get('acc')&&S.accs.find(a=>a.id===LS.get('acc')))?LS.get('acc'):(S.accs[0]||{}).id||null;return s}
function chatMenu(x,y,d){showMenu(x,y,[{ic:d.pinned?'📍':'📌',t:d.pinned?'Unpin':'Pin',fn:()=>chatAct('pin',d,{pin:!d.pinned})},{ic:d.muted?'🔔':'🔇',t:d.muted?'Unmute':'Mute',fn:()=>chatAct('mute',d,{mute:!d.muted})},
 {ic:'🗄',t:S.archived?'Unarchive':'Archive',fn:()=>chatAct('archive',d,{archive:!S.archived})},{ic:'🗑',t:d.type==='user'?'Delete chat':'Leave',red:true,fn:()=>confirmBox((d.type==='user'?'Delete this chat?':'Leave this chat?'),'Confirm',()=>chatAct('delete',d,{}),true)}])}
async function chatAct(k,d,extra){try{await api('/app/api/chat/'+k,A(Object.assign({chat:d.id},extra)));if(k==='delete'&&S.chat===d.id){S.chat=null;showEmpty()}await loadDialogs()}catch(e){toast(e.message)}}
/* ---------- open chat ---------- */
function showEmpty(){$('#cbar').style.display='none';$('#comp').style.display='none';$('#pin').style.display='none';$('#mi').replaceChildren(h('div',{class:'empty',text:'Select a chat to start messaging'}));document.body.classList.remove('chat')}
async function openByQuery(q){try{const r=await api('/app/api/resolve',A({q}));openChat(r.dialog)}catch(e){toast(e.message)}}
async function openById(chat,mid){let d=S.dl.find(x=>x.id===chat)||{id:chat,name:'Chat',type:'user'};await openChat(d,mid)}
async function openChat(d,jump){if(S.chat&&$('#txt'))S.drafts[S.chat]=$('#txt').value;S.chat=d.id;S.cd=d;S.msgs=[];S.noMore=false;S.sel.clear();S.reply=null;S.edit=null;S.pinned=null;S.q='';$('#search').value='';S.gsearch=[];S.gres=null;
 document.body.classList.add('chat');$('#cbar').style.display='flex';$('#comp').style.display='block';$('#txt').value=S.drafts[d.id]||'';autosize();barUpdate();selUpdate();
 const cav=$('#cav');cav.replaceChildren(avatar(d.id,d.name,40,d.online));cav.onclick=$('#ctt').onclick=()=>toggleInfo();$('#ctt b').textContent=(d.verified?'✔ ':'')+d.name;setStatus(d.type==='user'?(d.online?'online':''):'');drawList();
 try{if(jump){S.msgs=(await api('/app/api/around?'+Q({acc:S.acc,chat:d.id,id:jump}))).messages}else{const r=await api('/app/api/messages?'+Q({acc:S.acc,chat:d.id,limit:40}));S.msgs=r.messages;S.readOut=r.read_out||0;S.readIn=r.read_in||d.read_in||0;S.pinned=r.pinned;
  const um=S.msgs.find(m=>!m.out&&m.id>S.readIn);S.unreadFrom=(d.unread&&um)?um.id:0}}catch(e){toast(e.message)}
 drawPinned();drawMsgs(jump?'jump':'unread',jump);markRead();
 api('/app/api/chatinfo?'+Q({acc:S.acc,chat:d.id})).then(r=>{if(S.chat!==d.id)return;S.ci=r.info;setStatus(r.info.status||'');if(S.info)drawInfo()}).catch(()=>{});if(S.info)drawInfo()}
function setStatus(t){const s=$('#cbar .st');s.textContent=t||'';s.classList.toggle('on',t==='online'||/typing|recording/.test(t||''))}
function drawPinned(){const p=$('#pin');if(!S.pinned){p.style.display='none';return}p.style.display='flex';$('#pint').textContent=S.pinned.text;p.onclick=()=>jumpTo(S.pinned.id)}
async function jumpTo(id){const el=document.querySelector(`[data-id="${id}"]`);if(el){flash(el);return}try{S.msgs=(await api('/app/api/around?'+Q({acc:S.acc,chat:S.chat,id}))).messages;S.noMore=false;drawMsgs('jump',id)}catch(e){toast(e.message)}}
function flash(el){if(el.scrollIntoView)el.scrollIntoView({block:'center'});el.classList.remove('hl');void el.offsetWidth;el.classList.add('hl')}
function markRead(){if(S.msgs.length&&S.cd&&(S.cd.unread||0)>=0){const mx=S.msgs[S.msgs.length-1].id;api('/app/api/read',A({chat:S.chat,max_id:mx})).catch(()=>{});const d=S.dl.find(x=>x.id===S.chat);if(d&&d.unread){d.unread=0;d.mentions=0;drawList()}}}
/* ---------- message rendering ---------- */
function mediaNode(m){const x=m.media;if(!x)return null;const b={acc:S.acc,chat:S.chat,id:m.id},file='/app/api/file?'+Q(b),th='/app/api/thumb?'+Q(b);
 if(x.kind==='photo'){const w=h('div',{class:'ph',on:{click(){lightbox(m.id)}}});if(x.w&&x.h)w.style.aspectRatio=x.w+'/'+x.h;
  if(S.set.previews){const i=h('img',{alt:'',loading:'lazy',src:th,on:{error(){w.replaceChildren(h('div',{style:'padding:30px;text-align:center',text:'📷 Photo · '+sz(x.size)+' · tap'}))}}});w.append(i)}else w.append(h('div',{style:'padding:30px;text-align:center',text:'📷 Photo · '+sz(x.size)+' · tap to load'}));return w}
 if(x.kind==='sticker'){return x.mime==='image/webp'?h('img',{class:'stk',alt:x.emoji||'',loading:'lazy',src:th,on:{error(e){e.target.replaceWith(h('div',{class:'stk-e',text:x.emoji||'🏷'}))}}}):h('div',{class:'stk-e',text:x.emoji||'🏷'})}
 if(x.kind==='video'||x.kind==='gif'||x.kind==='videonote'){const w=h('div',{class:'vid'});if(x.w&&x.h)w.style.aspectRatio=x.w+'/'+x.h;if(x.kind==='videonote')w.style.cssText+=';border-radius:50%;width:220px;height:220px';
  const start=async()=>{if(x.size>=BIGF&&x.kind!=='gif'){const st=S.dls[dlKey(m)];if(!(st&&st.state==='cached')&&confirm('This video is '+sz(x.size)+'.\nOK = download to the server first (smooth seeking, replay without re-downloading)\nCancel = stream directly now')){try{await serverDownload(m,true)}catch(e){toast(e.message);return}}}const v=h('video',{src:file,playsinline:true,preload:'metadata'});if(x.kind==='gif'){v.loop=true;v.muted=true;v.autoplay=true}else v.controls=true;const tgt=document.querySelector('.vid[data-dl="'+dlKey(m)+'"]')||w;tgt.replaceChildren(v);tgt.onclick=null;const p=v.play&&v.play();if(p&&p.catch)p.catch(()=>{})};
  if(S.set.previews)w.append(h('img',{alt:'',loading:'lazy',src:th,on:{error(e){e.target.remove()}}}));else w.append(h('div',{style:'height:140px'}));
  w.append(h('div',{class:'play'},h('i',{text:'▶'})),h('span',{class:'chip',text:x.kind==='gif'?'GIF':dur(x.dur)+' · '+sz(x.size)}),h('span',{class:'chip dlp',style:'top:auto;bottom:8px;display:none'}));w.setAttribute('data-dl',dlKey(m));w.onclick=start;return w}
 if(x.kind==='voice'||x.kind==='audio'){return h('div',{},h('div',{class:'mut',style:'font-size:13px',text:(x.kind==='voice'?'🎤 Voice message ':'🎵 '+(x.name||'Audio')+' ')+dur(x.dur)+' · '+sz(x.size)}),h('audio',{controls:true,preload:'none',src:file}))}
 return fileCard(m,x,file)}
function dlText(st,x,big){if(!big)return sz(x.size);if(st.state==='downloading')return 'Downloading to server… '+pctOf(st)+'% ('+sz(st.done||0)+' / '+sz(st.size||x.size)+')';if(st.state==='cached')return sz(x.size)+' · ✔ Ready on server — tap to save';if(st.state==='error')return '⚠ '+(st.err||'failed')+' — tap ⬇ to retry';return sz(x.size)+' · tap ⬇ to download to server'}
function fileCard(m,x,file){const k=dlKey(m),st=S.dls[k]||{state:'none'},big=x.size>=BIGF;
 const icon=(!big||st.state==='cached')?h('a',{class:'fi',href:file+'&dl=1',target:'_blank',rel:'noopener',text:st.state==='cached'?'✔':'⬇'}):h('button',{class:'fi',text:st.state==='downloading'?'✕':'⬇',on:{click(e){e.stopPropagation();st.state==='downloading'?cancelDl(m):serverDownload(m).catch(er=>toast(er.message))}}});
 return h('div',{class:'file','data-dl':big?k:null},icon,h('div',{style:'min-width:0'},h('b',{text:x.name||'File'}),h('span',{class:'mut dlp',text:dlText(st,x,big)}),big?h('div',{class:'dlb',style:'height:3px;background:var(--bd);border-radius:2px;margin-top:3px;overflow:hidden;display:'+(st.state==='downloading'?'block':'none')},h('i',{style:'display:block;height:100%;background:var(--acc);width:'+pctOf(st)+'%'})):null))}
function setDl(k,st){const prev=S.dls[k];S.dls[k]=st;const[a,c]=k.split(':');if(a!==S.acc||String(c)!==String(S.chat))return;if(!prev||prev.state!==st.state)drawMsgs('keep');else updDl(k)}
function updDl(k){const st=S.dls[k];if(!st)return;document.querySelectorAll('[data-dl="'+k+'"]').forEach(el=>{const p=el.querySelector('.dlp'),b=el.querySelector('.dlb i'),pct=pctOf(st),vid=el.classList.contains('vid');if(p){p.textContent=vid?'⬇ '+pct+'%':'Downloading to server… '+pct+'% ('+sz(st.done||0)+' / '+sz(st.size)+')';if(vid)p.style.display='block'}if(b){b.parentNode.style.display='block';b.style.width=pct+'%'}})}
async function serverDownload(m,wait){const k=dlKey(m),cur0=S.dls[k];if(cur0&&cur0.state==='cached')return;
 const r=await api('/app/api/dl/start',A({chat:S.chat,id:m.id}));setDl(k,{state:r.state,done:r.done||0,size:r.size||m.media.size});if(r.state==='downloading')watchDl(k);
 if(!wait)return;while(S.dls[k]&&S.dls[k].state==='downloading')await sleep(400);if(!S.dls[k]||S.dls[k].state!=='cached')throw new Error((S.dls[k]&&S.dls[k].err)||'Download failed')}
const WATCH={};async function watchDl(k){if(WATCH[k])return;WATCH[k]=1;const[acc,chat,id]=k.split(':');try{while(S.dls[k]&&S.dls[k].state==='downloading'){await sleep(3000);const r=await api('/app/api/dl/status?'+Q({acc,chat,ids:id}));const it=r.items[id];if(it&&S.dls[k]&&(it.state!=='downloading'||(it.done||0)>(S.dls[k].done||0)))setDl(k,it.state==='none'?{state:'none'}:Object.assign({},it,{size:it.size||S.dls[k].size}))}}catch(e){}WATCH[k]=0}
async function cancelDl(m){try{await api('/app/api/dl/cancel',A({chat:S.chat,id:m.id}))}catch(e){toast(e.message)}}
let dlBusy=false;async function refreshDlStatus(){if(dlBusy||!S.chat)return;const ids=S.msgs.filter(m=>m.media&&m.media.size>=BIGF&&['file','video','audio','other'].includes(m.media.kind)&&!S.dls[dlKey(m)]).map(m=>m.id);if(!ids.length)return;dlBusy=true;
 try{const r=await api('/app/api/dl/status?'+Q({acc:S.acc,chat:S.chat,ids:ids.slice(0,60).join(',')}));let ch=false;for(const id of ids.slice(0,60)){const it=r.items[id]||{state:'none'},k=S.acc+':'+S.chat+':'+id;S.dls[k]=it;if(it.state==='cached'||it.state==='downloading')ch=true;if(it.state==='downloading')watchDl(k)}if(ch)drawMsgs('keep')}catch(e){}dlBusy=false}
function pollNode(m){const p=m.poll,voted=p.a.some(o=>o.me)||p.closed,tot=p.a.reduce((s,o)=>s+o.n,0)||1;const sel=new Set();const box=h('div',{class:'poll'},h('b',{class:'q',text:'📊 '+p.q}));
 p.a.forEach(o=>{const row=h('div',{class:'po',on:{async click(){if(p.closed||voted)return;if(p.multi){sel.has(o.i)?sel.delete(o.i):sel.add(o.i);row.style.outline=sel.has(o.i)?'2px solid var(--acc)':'';return}await vote([o.i])}}},
  voted?h('i',{style:`width:${Math.round(o.n*100/tot)}%`}):null,h('span',{},h('span',{text:(o.me?'✔ ':'')+o.t}),voted?h('span',{text:Math.round(o.n*100/tot)+'%'}):null));box.append(row)});
 async function vote(ix){try{const r=await api('/app/api/vote',A({chat:S.chat,id:m.id,options:ix}));replaceMsg(r.message)}catch(e){toast(e.message)}}
 if(p.multi&&!voted)box.append(h('button',{class:'mut',text:'Vote',on:{click(){if(sel.size)vote([...sel])}}}));box.append(h('div',{class:'mut',style:'font-size:12px',text:(p.total||0)+' votes'+(p.quiz?' · quiz':'')+(p.closed?' · closed':'')}));return box}
function bodyNode(m){const n=[];
 if(m.fwd)n.push(h('div',{class:'fw',text:'↪ Forwarded from '+m.fwd}));if(m.via)n.push(h('div',{class:'mut',style:'font-size:12px',text:'via @'+m.via}));
 if(m.reply){const r=S.msgs.find(x=>x.id===m.reply);n.push(h('div',{class:'rp',on:{click(e){e.stopPropagation();jumpTo(m.reply)}}},h('b',{text:r?(r.out?'You':r.from||'Message'):'Reply'}),h('span',{text:r?(r.text||'📎 Media'):'Tap to view'})))}
 const mn=mediaNode(m);if(mn)n.push(mn);
 if(m.poll)n.push(pollNode(m));
 if(m.geo)n.push(h('div',{class:'card'},h('b',{text:'📍 '+(m.geo.title||'Location')}),h('div',{class:'ds'},h('a',{href:`https://www.openstreetmap.org/?mlat=${m.geo.lat}&mlon=${m.geo.lon}#map=16/${m.geo.lat}/${m.geo.lon}`,target:'_blank',rel:'noopener noreferrer',text:m.geo.lat.toFixed(5)+', '+m.geo.lon.toFixed(5)}))));
 if(m.contact)n.push(h('div',{class:'card'},h('b',{text:'👤 '+m.contact.name}),h('div',{class:'ds',text:m.contact.phone})));
 if(m.dice)n.push(h('div',{class:'stk-e',text:m.dice.e+' '+m.dice.v}));
 if(m.text)n.push(h('div',{class:'mt'},m.ents&&m.ents.length?richText(m.text,m.ents):plainLinks(m.text)));
 if(m.web){const w=m.web;n.push(h('div',{class:'card',style:'cursor:pointer',on:{click(){if(SAFE.test(w.url))window.open(w.url,'_blank','noopener')}}},h('b',{text:w.site||w.url}),h('div',{style:'font-weight:600',text:w.title}),h('div',{class:'ds mut',text:w.desc}),
  w.photo&&S.set.previews?h('img',{alt:'',loading:'lazy',style:'max-width:100%;border-radius:6px;margin-top:4px',src:'/app/api/thumb?'+Q({acc:S.acc,chat:S.chat,id:m.id}),on:{error(e){e.target.remove()}}}):null))}
 if(m.react)n.push(h('div',{class:'rx'},m.react.map(r=>h('button',{class:r.me?'me':'',text:r.e+' '+r.n,on:{click(e){e.stopPropagation();react(m,r.me?'':r.e)}}}))));
 if(m.btn)n.push(h('div',{class:'bt'},m.btn.map((row,i)=>h('div',{},row.map((b,j)=>h('button',{text:(b.u?'🔗 ':'')+b.t,on:{async click(e){e.stopPropagation();if(b.u){if(SAFE.test(b.u))window.open(b.u,'_blank','noopener');return}try{const r=await api('/app/api/click',A({chat:S.chat,id:m.id,i,j}));if(r.text)(r.alert?alert:toast)(r.text);if(r.url&&SAFE.test(r.url))window.open(r.url,'_blank','noopener')}catch(er){toast(er.message)}}}}))))));
 return n}
function ticks(m){if(!m.out)return null;return h('span',{class:'tk',text:m.id<=S.readOut?'✓✓':'✓'})}
function bubbleRow(m,first,last,showName,album){const out=m.out;const row=h('div',{class:'mr'+(out?' out':''),'data-id':m.id});
 if(!out&&S.cd&&S.cd.type!=='user'){row.append(last?avatar(m.from_id,m.from,34):h('div',{class:'sp'}))}
 const nobg=m.media&&(m.media.kind==='sticker'||m.media.kind==='videonote')&&!m.text;
 const b=h('div',{class:'m'+(last?' tl':'')+(nobg?' nobg':'')+(S.sel.has(m.id)?' sel':'')});
 if(showName&&!out&&S.cd&&S.cd.type!=='user')b.append(h('div',{class:'mn',style:'color:'+colorOf(m.from_id),text:m.from||'User'}));
 if(album&&album.length>1){const g=h('div',{style:`display:grid;grid-template-columns:repeat(${album.length>3?2:Math.min(album.length,2)},1fr);gap:2px;margin:-2px -4px 3px`});album.forEach(a=>g.append(mediaNode(a)));b.append(g);const tx=album.find(a=>a.text);if(tx)b.append(...bodyNode(Object.assign({},tx,{media:null,reply:null,fwd:'',via:'',react:tx.react,btn:null})))}
 else b.append(...bodyNode(m));
 b.append(h('span',{class:'meta'},m.views?'👁 '+m.views:null,m.edited?'edited':null,tm(m.ts),ticks(m)));b.append(h('div',{style:'clear:both'}));
 b.addEventListener('contextmenu',e=>{e.preventDefault();msgMenu(e.clientX,e.clientY,m)});let lp;b.addEventListener('touchstart',e=>{const t=e.touches[0];lp=setTimeout(()=>msgMenu(t.clientX,t.clientY,m),550)},{passive:true});['touchend','touchmove'].forEach(ev=>b.addEventListener(ev,()=>clearTimeout(lp),{passive:true}));
 b.addEventListener('click',e=>{if(S.sel.size||e.shiftKey)toggleSel(m.id);else if(e.detail===2)setReply(m)});row.append(b);return row}
function drawMsgs(mode,jumpId){const box=$('#msgs'),mi=$('#mi'),keep=box.scrollHeight-box.scrollTop;const kids=[];let prev=null;const L=S.msgs;
 for(let i=0;i<L.length;i++){const m=L[i];
  if(!prev||dayKey(prev.ts)!==dayKey(m.ts))kids.push(h('div',{class:'day',text:dayLabel(m.ts)}));
  if(S.unreadFrom&&m.id===S.unreadFrom)kids.push(h('div',{class:'unr',text:'Unread messages'}));
  if(m.service){kids.push(h('div',{class:'svc','data-id':m.id,text:(m.from?m.from+' ':'')+m.text}));prev=m;continue}
  let album=null,end=i;if(m.grp&&m.media&&(m.media.kind==='photo'||m.media.kind==='video')){album=[m];while(L[end+1]&&L[end+1].grp===m.grp){album.push(L[++end])}if(album.length<2)album=null}
  const nxt=L[end+1],sameP=prev&&!prev.service&&prev.from_id===m.from_id&&prev.out===m.out&&m.ts-prev.ts<300&&dayKey(prev.ts)===dayKey(m.ts);
  const last=!nxt||nxt.service||nxt.from_id!==m.from_id||nxt.out!==m.out||nxt.ts-m.ts>=300||dayKey(nxt.ts)!==dayKey(m.ts);
  kids.push(bubbleRow(m,!sameP,last,!sameP,album));prev=L[end];i=end}
 if(!kids.length)kids.push(h('div',{class:'empty',text:'No messages here yet'}));
 const live=[...mi.querySelectorAll('video,audio')].filter(e=>!e.paused&&!e.ended&&e.closest('[data-id]')).map(e=>[e.closest('[data-id]').getAttribute('data-id'),e]);mi.replaceChildren(...kids);
 for(const[id,e]of live){try{const row=mi.querySelector('[data-id="'+id+'"]');if(!row)continue;if(e.tagName==='VIDEO'){const sl=row.querySelector('.vid');if(sl){sl.replaceChildren(e);sl.onclick=null}}else{const sl=row.querySelector('audio');if(sl)sl.replaceWith(e)}}catch(er){}}
 if(mode==='unread'){const u=mi.querySelector('.unr');if(u&&u.scrollIntoView)u.scrollIntoView({block:'start'});else box.scrollTop=box.scrollHeight}
 else if(mode==='bottom')box.scrollTop=box.scrollHeight;else if(mode==='jump'){const el=mi.querySelector(`[data-id="${jumpId}"]`);if(el)flash(el)}else box.scrollTop=box.scrollHeight-keep;onScroll();setTimeout(refreshDlStatus,0)}
function replaceMsg(m){const i=S.msgs.findIndex(x=>x.id===m.id);if(i>=0)S.msgs[i]=m;drawMsgs('keep')}
async function loadOlder(){if(S.loadingOld||S.noMore||!S.msgs.length)return;S.loadingOld=true;try{const r=await api('/app/api/messages?'+Q({acc:S.acc,chat:S.chat,before:S.msgs[0].id,limit:40}));if(!r.messages.length)S.noMore=true;else{S.msgs=r.messages.concat(S.msgs);drawMsgs('keep')}}catch(e){}S.loadingOld=false}
function onScroll(){const b=$('#msgs'),nearEnd=b.scrollHeight-b.scrollTop-b.clientHeight<120;$('#dn').style.display=nearEnd?'none':'block';if(b.scrollTop<90)loadOlder();if(nearEnd)S.newBelow=0;const bd=$('#dn .bd');if(bd)bd.remove();if(!nearEnd&&S.newBelow)$('#dn').append(h('span',{class:'bd',text:S.newBelow}))}
$('#msgs').addEventListener('scroll',onScroll);$('#dn').onclick=()=>{const b=$('#msgs');b.scrollTop=b.scrollHeight};
/* ---------- message actions ---------- */
async function react(m,emoji){try{const r=await api('/app/api/react',A({chat:S.chat,id:m.id,emoji}));if(r.message)replaceMsg(r.message)}catch(e){toast(e.message)}}
function msgMenu(x,y,m){closeMenu();const top=h('div',{class:'rb'},REACT.map(e=>h('button',{text:e,on:{click(){closeMenu();react(m,e)}}})));
 showMenu(x,y,[{ic:'↩',t:'Reply',fn:()=>setReply(m)},m.text?{ic:'⧉',t:'Copy text',fn:()=>copyText(m.text)}:null,{ic:'➦',t:'Forward',fn:()=>forwardDlg([m.id])},
  {ic:m.pinned?'📍':'📌',t:m.pinned?'Unpin':'Pin',fn:()=>pinMsg(m)},(m.out&&m.text&&!m.service)?{ic:'✎',t:'Edit',fn:()=>startEdit(m)}:null,{ic:'☑',t:'Select',fn:()=>toggleSel(m.id)},
  {ic:'🗑',t:'Delete',red:true,fn:()=>deleteDlg([m.id])}],top)}
function copyText(t){if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(t).then(()=>toast('Copied'),()=>toast('Copy failed'));else toast('Copy not available')}
async function pinMsg(m){try{await api('/app/api/pin',A({chat:S.chat,id:m.id,unpin:m.pinned}));m.pinned=!m.pinned;S.pinned=m.pinned?{id:m.id,text:m.text||'📎'}:null;drawPinned();toast(m.pinned?'Pinned':'Unpinned')}catch(e){toast(e.message)}}
function deleteDlg(ids){const rv=h('input',{type:'checkbox',checked:true});modal('Delete '+(ids.length>1?ids.length+' messages':'message')+'?',h('label',{},rv,'Also delete for everyone'),[{t:'Cancel'},{t:'Delete',c:'d',async fn(c){try{await api('/app/api/delete',A({chat:S.chat,ids,revoke:rv.checked}));S.msgs=S.msgs.filter(m=>!ids.includes(m.id));S.sel.clear();selUpdate();drawMsgs('keep');c()}catch(e){toast(e.message)}}}])}
function forwardDlg(ids){const q=h('input',{placeholder:'Search chats'}),list=h('div',{style:'max-height:46vh;overflow:auto'}),hide=h('input',{type:'checkbox'});
 const draw=()=>list.replaceChildren(...S.dl.filter(d=>d.name.toLowerCase().includes(q.value.toLowerCase())).slice(0,60).map(d=>h('div',{class:'row',on:{async click(){try{await api('/app/api/forward',A({from_chat:S.chat,ids,to_chat:d.id,hide_author:hide.checked}));toast('Forwarded to '+d.name);S.sel.clear();selUpdate();drawMsgs('keep');close()}catch(e){toast(e.message)}}}},avatar(d.id,d.name,36),h('b',{text:d.name}))));
 q.oninput=draw;const close=modal('Forward to…',[q,list,h('label',{},hide,'Hide sender name')],[{t:'Cancel'}]);draw()}
function toggleSel(id){S.sel.has(id)?S.sel.delete(id):S.sel.add(id);selUpdate();drawMsgs('keep')}
function selUpdate(){$('#sel').style.display=S.sel.size?'flex':'none';$('#cbar').style.display=(S.sel.size||!S.chat)?'none':'flex';$('#selc').textContent=S.sel.size+' selected'}
$('#selx').onclick=()=>{S.sel.clear();selUpdate();drawMsgs('keep')};$('#self').onclick=()=>forwardDlg([...S.sel]);$('#seld').onclick=()=>deleteDlg([...S.sel]);
/* ---------- composer ---------- */
function autosize(){const t=$('#txt');t.style.height='auto';t.style.height=Math.min(180,t.scrollHeight)+'px'}
function barUpdate(){const b=$('#bar2'),c=b.firstElementChild;b.style.display=(S.reply||S.edit)?'flex':'none';const m=S.edit||S.reply;if(m){c.replaceChildren(h('b',{style:'color:var(--acc);font-size:13px;display:block',text:S.edit?'✎ Edit message':'↩ Reply to '+(m.out?'yourself':m.from||'message')}),h('span',{class:'mut',style:'display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis',text:m.text||'📎 Media'}))}}
function setReply(m){S.reply=m;S.edit=null;barUpdate();$('#txt').focus()}
function startEdit(m){S.edit=m;S.reply=null;$('#txt').value=m.text;autosize();barUpdate();$('#txt').focus()}
function cancelCompose(){if(S.edit)$('#txt').value='';S.reply=null;S.edit=null;barUpdate();autosize()}
$('#bar2x').onclick=cancelCompose;
async function send(opt){opt=opt||{};const t=$('#txt').value;if(!t.trim()||!S.chat)return;const tx=t;$('#txt').value='';autosize();delete S.drafts[S.chat];
 try{if(S.edit){const r=await api('/app/api/edit',A({chat:S.chat,id:S.edit.id,text:tx,md:S.set.md}));replaceMsg(r.message);S.edit=null}
  else{const r=await api('/app/api/send',A({chat:S.chat,text:tx,reply_to:S.reply&&S.reply.id,md:S.set.md,silent:opt.silent,schedule:opt.schedule}));if(opt.schedule)toast('Message scheduled');else{if(!S.msgs.some(x=>x.id===r.message.id))S.msgs.push(r.message);drawMsgs('bottom')}}
  S.reply=null;barUpdate()}catch(e){toast(e.message);$('#txt').value=tx;autosize()}}
$('#send').onclick=()=>send();$('#send').oncontextmenu=e=>{e.preventDefault();sendMenu(e.clientX,e.clientY)};
let sendLP;$('#send').addEventListener('touchstart',e=>{const t=e.touches[0];sendLP=setTimeout(()=>sendMenu(t.clientX,t.clientY-120),600)},{passive:true});$('#send').addEventListener('touchend',()=>clearTimeout(sendLP),{passive:true});
function sendMenu(x,y){showMenu(x-160,y-110,[{ic:'🔕',t:'Send without sound',fn:()=>send({silent:true})},{ic:'⏰',t:'Schedule message',fn:()=>{const dt=h('input',{type:'datetime-local'});modal('Schedule',dt,[{t:'Cancel'},{t:'Schedule',c:'p',fn(c){const ts=Math.floor(new Date(dt.value).getTime()/1e3);if(!ts||ts<Date.now()/1e3+30){toast('Choose a future time');return}c();send({schedule:ts})}}])}}])}
let lastTyping=0;$('#txt').addEventListener('input',()=>{autosize();if(S.chat)S.drafts[S.chat]=$('#txt').value;if(Date.now()-lastTyping>4500&&$('#txt').value&&S.chat){lastTyping=Date.now();api('/app/api/typing',A({chat:S.chat})).catch(()=>{})}});
$('#txt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&S.set.enter&&innerWidth>820){e.preventDefault();send()}else if(e.key==='Escape')cancelCompose();else if(e.key==='ArrowUp'&&!$('#txt').value){const l=[...S.msgs].reverse().find(m=>m.out&&m.text&&!m.service);if(l){e.preventDefault();startEdit(l)}}});
$('#txt').addEventListener('paste',e=>{const fs=[...(e.clipboardData&&e.clipboardData.files||[])];if(fs.length){e.preventDefault();pickFiles(fs,false)}});
$('#md').onclick=()=>{S.set.md=!S.set.md;saveSet();$('#md').style.color=S.set.md?'var(--acc)':'';toast(S.set.md?'Markdown formatting ON (**bold** __italic__ `code`)':'Formatting OFF')};
const emo=$('#emo');EMOJI.forEach(e=>emo.append(h('button',{text:e,on:{click(){const t=$('#txt');t.setRangeText(e,t.selectionStart,t.selectionEnd,'end');t.focus();autosize()}}})));$('#emob').onclick=()=>emo.classList.toggle('on');
$('#att').onclick=e=>{const r=e.currentTarget.getBoundingClientRect();showMenu(r.left-100,r.top-100,[{ic:'🖼',t:'Photo or video',fn:()=>{$('#file').dataset.f='0';$('#file').click()}},{ic:'📄',t:'File (no compression)',fn:()=>{$('#file').dataset.f='1';$('#file').click()}}])};
$('#file').onchange=e=>{pickFiles([...e.target.files],e.target.dataset.f==='1');e.target.value=''};
const msgsEl=$('#msgs');['dragover','drop'].forEach(ev=>msgsEl.addEventListener(ev,e=>{e.preventDefault();if(ev==='drop'&&e.dataTransfer)pickFiles([...e.dataTransfer.files],false)}));
function pickFiles(fs,asFile){if(!S.chat)return;for(const f of fs)queueUpload(f,asFile)}
/* ---------- मोठ्या फाइलचा upload (8 MB तुकडे, resume सकट) ---------- */
function drawUploads(){const u=$('#ups');u.replaceChildren(...Object.values(S.uploads).filter(x=>x.chat===S.chat||x.state==='error').map(x=>{const pct=x.state==='receiving'?x.written/x.size*50:50+x.sent/x.size*50;
 return h('div',{class:'up'},h('div',{style:'display:flex;justify-content:space-between;gap:8px'},h('span',{text:'⬆ '+x.name+' · '+sz(x.size)+' · '+(x.state==='receiving'?'uploading to server':x.state==='sending'?'sending to Telegram':x.state)+(x.err?': '+x.err:'')}),
  (x.state==='receiving'||x.state==='sending')?h('button',{class:'mut',text:'✕',on:{click(){cancelUpload(x)}}}):h('button',{class:'mut',text:'✕',on:{click(){delete S.uploads[x.id];drawUploads()}}})),h('div',{class:'pb'},h('i',{style:'width:'+Math.min(100,pct)+'%'})))}))}
async function cancelUpload(x){x.state='canceled';try{await api('/app/api/up/cancel',{id:x.id})}catch(e){}delete S.uploads[x.id];drawUploads()}
const ab=b=>b.arrayBuffer?b.arrayBuffer():new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsArrayBuffer(b)});
async function queueUpload(file,asFile){const lim=cur().max_upload||2e9;if(file.size>lim){toast(`${file.name}: over Telegram's limit (${mbs(lim)})`);return}
 if(file.size>25*1048576&&!confirm(`Send "${file.name}" (${sz(file.size)})?\nThis uses about ${sz(file.size)} of your monthly bandwidth.`))return;
 const cap=S.edit?'':'';let st;try{st=await api('/app/api/up/start',A({chat:S.chat,size:file.size,name:file.name,caption:cap,reply_to:S.reply&&S.reply.id,as_file:asFile,md:S.set.md}))}catch(e){toast(e.message);return}
 S.reply=null;barUpdate();const U={id:st.id,name:file.name,size:file.size,written:0,sent:0,state:'receiving',chat:S.chat,err:''};S.uploads[U.id]=U;drawUploads();
 try{let off=0;while(off<file.size){if(U.state==='canceled')return;const end=Math.min(off+st.chunk,file.size),buf=await ab(file.slice(off,end));let tries=0;
   for(;;){try{const r=await api('/app/api/up/chunk?'+Q({id:U.id,off}),undefined,{raw:buf});U.written=r.written;off=r.written;break}catch(e){if(e.status===409&&e.data&&e.data.written!=null){off=e.data.written;U.written=off;break}if(++tries>4)throw e;await sleep(1500*tries)}}
   drawUploads()}
  await api('/app/api/up/finish',{id:U.id});U.state='sending';drawUploads();watchUpload(U)}catch(e){U.state='error';U.err=e.message;drawUploads()}}
async function watchUpload(U){while(S.uploads[U.id]&&U.state==='sending'){await sleep(4000);try{const r=await api('/app/api/up/status?'+Q({id:U.id}));if(r.state==='done'){finishUpload(U,r.message)}else if(r.state==='error'||r.state==='canceled'){U.state=r.state;U.err=r.err;drawUploads()}}catch(e){}}}
function finishUpload(U,msg){if(U.state==='done')return;U.state='done';if(msg&&U.chat===S.chat&&!S.msgs.some(x=>x.id===msg.id)){S.msgs.push(msg);drawMsgs('bottom')}setTimeout(()=>{delete S.uploads[U.id];drawUploads()},1500);drawUploads()}
/* ---------- lightbox ---------- */
function lightbox(id){const L=S.msgs.filter(m=>m.media&&m.media.kind==='photo');let i=L.findIndex(m=>m.id===id);if(i<0)return;const lb=$('#lb'),im=lb.querySelector('img');
 const show=()=>{const m=L[i];im.src='/app/api/file?'+Q({acc:S.acc,chat:S.chat,id:m.id})};lb.style.display='flex';show();
 lb.querySelector('.x').onclick=()=>{lb.style.display='none';im.removeAttribute('src')};const nv=lb.querySelectorAll('.nv');nv[0].onclick=()=>{if(i>0){i--;show()}};nv[1].onclick=()=>{if(i<L.length-1){i++;show()}}}
/* ---------- info panel ---------- */
function toggleInfo(){S.info=!S.info;document.body.classList.toggle('info-on',S.info);if(S.info)drawInfo()}
$('#cinfo').onclick=toggleInfo;
function drawInfo(){const p=$('#info'),d=S.cd,ci=S.ci&&S.ci.id===S.chat?S.ci:null;if(!d){p.replaceChildren();return}
 const kv=(k,v)=>v?h('div',{class:'kv'},h('div',{text:v}),h('small',{text:k})):null;
 p.replaceChildren(h('div',{class:'hd'},h('button',{class:'ib',text:'✕',on:{click:toggleInfo}}),h('b',{text:'Info'})),
  h('div',{class:'big'},avatar(d.id,d.name,110),h('b',{style:'font-size:20px;margin-top:8px',text:d.name}),h('span',{class:'mut',text:(ci&&ci.status)||''})),
  kv('Bio',ci&&ci.about),kv('Username',ci&&ci.username?'@'+ci.username:''),kv('Phone',ci&&ci.phone?'+'+ci.phone.replace(/^\+/,''):''),
  h('div',{class:'kv'},h('div',{class:'sw'},h('span',{text:'Notifications'}),h('button',{class:'mut',text:d.muted?'🔇 Muted':'🔔 On',on:{async click(){await chatAct('mute',d,{mute:!d.muted});d.muted=!d.muted;drawInfo()}}}))),
  h('div',{class:'tabs',id:'itabs'},[['media','Media'],['files','Files'],['links','Links'],['voice','Voice'],['music','Music'],['gifs','GIFs']].concat(d.type!=='user'?[['members','Members']]:[]).map(([k,n])=>h('button',{text:n,on:{click(e){document.querySelectorAll('#itabs button').forEach(b=>b.classList.remove('on'));e.target.classList.add('on');loadShared(k)}}}))),h('div',{id:'ibody'}))}
async function loadShared(kind){const b=$('#ibody');b.replaceChildren(h('div',{class:'empty',text:'Loading…'}));try{
 if(kind==='members'){const r=(await api('/app/api/members?'+Q({acc:S.acc,chat:S.chat}))).members;b.replaceChildren(...r.map(u=>h('div',{class:'row',on:{click(){openByQuery(u.username?'@'+u.username:String(u.id))}}},avatar(u.id,u.name,40),h('div',{class:'rm'},h('b',{text:(u.bot?'🤖 ':'')+u.name}),h('div',{class:'mut',style:'font-size:13px',text:u.status||(u.username?'@'+u.username:'')})))));if(!r.length)b.replaceChildren(h('div',{class:'empty',text:'Not available'}));return}
 const ms=(await api('/app/api/shared?'+Q({acc:S.acc,chat:S.chat,kind}))).messages;if(!ms.length){b.replaceChildren(h('div',{class:'empty',text:'Nothing here'}));return}
 if(kind==='media'||kind==='gifs'){b.replaceChildren(h('div',{class:'grid'},ms.map(m=>h('div',{on:{click(){openById(S.chat,m.id)}}},S.set.previews?h('img',{alt:'',loading:'lazy',src:'/app/api/thumb?'+Q({acc:S.acc,chat:S.chat,id:m.id}),on:{error(e){e.target.remove()}}}):null))))}
 else b.replaceChildren(...ms.map(m=>h('div',{class:'kv',style:'cursor:pointer',on:{click(){openById(S.chat,m.id)}}},kind==='links'?h('div',{},m.web?h('b',{text:m.web.title||m.web.url}):null,h('div',{class:'mut',style:'font-size:13px',text:m.text.slice(0,120)})):h('div',{text:(m.media&&m.media.name)||(m.media&&m.media.kind)||'File'}),h('small',{text:tm(m.ts)+' · '+(m.media?sz(m.media.size):'')}))))}catch(e){b.replaceChildren(h('div',{class:'empty',text:e.message}))}}
/* ---------- chat header menu / search ---------- */
$('#cmore').onclick=e=>{const d=S.cd;if(!d)return;const r=e.currentTarget.getBoundingClientRect();chatMenu(r.left-150,r.bottom,d)};
$('#back').onclick=()=>{document.body.classList.remove('chat')};
$('#csr').onclick=()=>{const q=h('input',{placeholder:'Search in this chat'}),res=h('div',{style:'max-height:50vh;overflow:auto'});let t;q.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{if(q.value.length<2)return;try{const r=(await api('/app/api/search?'+Q({acc:S.acc,q:q.value,chat:S.chat}))).messages;res.replaceChildren(...(r.length?r.map(m=>h('div',{class:'row',on:{click(){close();jumpTo(m.id)}}},h('div',{class:'rm'},h('div',{class:'r1'},h('b',{text:m.out?'You':m.from||''}),h('span',{class:'t',text:dayLabel(m.ts)+' '+tm(m.ts)})),h('div',{class:'mut',text:m.text}))) ):[h('div',{class:'empty',text:'No results'})]))}catch(e){res.replaceChildren(h('div',{class:'empty',text:e.message}))}},400)};const close=modal('Search',[q,res],[{t:'Close'}]);q.focus()};
let st2;function onSearch(){S.q=$('#search').value.trim();S.gsearch=[];S.gres=null;drawList();clearTimeout(st2);if(S.q.length<2)return;st2=setTimeout(async()=>{try{if(/^(@|https?:\/\/t\.me\/|\+\d{8,})/.test(S.q)){S.gres=await api('/app/api/resolve',A({q:S.q}))}else S.gsearch=(await api('/app/api/search?'+Q({acc:S.acc,q:S.q}))).messages}catch(e){S.gsearch=[]}drawList()},450)}
$('#search').oninput=onSearch;
/* ---------- drawer / modals ---------- */
function openDrawer(){const dr=$('#dr'),c=cur();dr.replaceChildren(h('div',{style:'padding:16px 18px;background:var(--hov)'},avatar(c.uid,c.name||'?',64),h('b',{style:'display:block;margin-top:8px',text:c.label||c.name||'No account'}),h('span',{class:'mut',text:c.phone||''})),
  ...S.accs.map(a=>h('div',{class:'ac'+(a.id===S.acc?' on':''),on:{click(){switchAcc(a.id);closeDrawer()}}},avatar(a.uid,a.name,34),h('div',{},h('b',{text:a.label||a.name}),h('div',{class:'mut',style:'font-size:12px',text:a.state})))),
  h('div',{class:'it',on:{click(){closeDrawer();addAccount()}}},'＋','Add account'),h('hr',{style:'border:0;border-top:1px solid var(--bd);width:100%'}),
  h('div',{class:'it',on:{click(){closeDrawer();if(c.uid)openById(c.uid,0)}}},'🔖','Saved Messages'),h('div',{class:'it',on:{click(){closeDrawer();newChat()}}},'👤','Contacts / New chat'),
  h('div',{class:'it',on:{click(){closeDrawer();S.archived=!S.archived;loadDialogs()}}},'🗄',S.archived?'Back to chats':'Archived chats'),h('div',{class:'it',on:{click(){closeDrawer();sessions()}}},'🛡','Active sessions'),
  h('div',{class:'it',on:{click(){closeDrawer();profile()}}},'✎','Edit profile'),h('div',{class:'it',on:{click(){closeDrawer();settings()}}},'⚙','Settings'),
  h('div',{class:'it',on:{click(){S.set.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';saveSet()}}},'🌙','Night mode'),
  h('div',{class:'mut',style:'padding:12px 18px;font-size:12px',text:S.bw?`📊 ${(S.bw.used/1048576).toFixed(0)} MB / ${(S.bw.limit/1073741824).toFixed(1)} GB (${S.bw.month})`:''}));$('#drawer').classList.add('on')}
function closeDrawer(){$('#drawer').classList.remove('on')}$('#menu').onclick=openDrawer;$('#drawer').onclick=e=>{if(e.target.id==='drawer')closeDrawer()};
async function switchAcc(id){if(id===S.acc)return;S.acc=id;LS.set('acc',id);S.chat=null;S.cd=null;S.dl=[];S.archived=false;showEmpty();drawList();await loadDialogs()}
function newChat(){const f=h('input',{placeholder:'@username, phone (+91…) or t.me link'}),q=h('input',{placeholder:'Search contacts'}),list=h('div',{style:'max-height:36vh;overflow:auto'});let C=[];
 const draw=()=>list.replaceChildren(...C.filter(c=>c.name.toLowerCase().includes(q.value.toLowerCase())).map(c=>h('div',{class:'row',on:{click(){close();openById(c.id,0)}}},avatar(c.id,c.name,38,c.online),h('div',{class:'rm'},h('b',{text:c.name}),h('div',{class:'mut',style:'font-size:13px',text:c.username?'@'+c.username:''})))));
 const close=modal('New message',[f,h('div',{style:'display:flex;gap:8px'},h('button',{class:'mut',text:'Open',on:{async click(){try{const r=await api('/app/api/resolve',A({q:f.value}));close();openChat(r.dialog)}catch(e){toast(e.message)}}}}),h('button',{class:'mut',text:'Join',on:{async click(){try{const r=await api('/app/api/join',A({q:f.value}));close();await loadDialogs();openChat(r.dialog)}catch(e){toast(e.message)}}}})),q,list],[{t:'Close'}]);
 q.oninput=draw;api('/app/api/contacts?'+Q({acc:S.acc})).then(r=>{C=r.contacts;draw()}).catch(e=>list.replaceChildren(h('div',{class:'empty',text:e.message})))}
$('#fab').onclick=newChat;
function settings(){const sw=(k,t)=>{const i=h('input',{type:'checkbox'});i.checked=!!S.set[k];i.onchange=async()=>{S.set[k]=i.checked;if(k==='notif'&&i.checked&&window.Notification&&Notification.permission!=='granted'){const p=await Notification.requestPermission();if(p!=='granted'){S.set.notif=false;i.checked=false;toast('Notifications blocked by the browser')}}saveSet();if(k==='avatars'||k==='previews'){drawList();drawMsgs('keep')}};return h('div',{class:'sw'},h('span',{text:t}),i)};
 const th=h('select',{},['auto','light','dark'].map(v=>h('option',{value:v,text:v[0].toUpperCase()+v.slice(1)})));th.value=S.set.theme;th.onchange=()=>{S.set.theme=th.value;saveSet()};
 const cacheRow=h('div',{class:'mut',style:'font-size:13px',text:'Server cache: …'}),loadCache=()=>api('/app/api/cache').then(c=>{cacheRow.textContent=`Server cache: ${sz(c.used)} of ${sz(c.limit)} (${c.files} file(s)). Big files you download to the server are kept here and replayed without re-downloading.`}).catch(()=>{}),
  clr=h('button',{class:'mut',style:'text-align:left',text:'🗑 Clear server cache',on:{async click(){try{const r=await api('/app/api/cache/clear',{});toast('Cleared '+r.removed+' file(s)');S.dls={};drawMsgs('keep');loadCache()}catch(e){toast(e.message)}}}});loadCache();
 modal('Settings',[h('div',{class:'sw'},h('span',{text:'Theme'}),th),cacheRow,clr,sw('avatars','Show profile photos'),sw('previews','Auto-load photo/video previews'),sw('notif','Desktop notifications'),sw('sound','Notification sound'),sw('enter','Enter sends message'),sw('md','Markdown formatting by default'),
  h('div',{class:'mut',style:'font-size:13px',text:S.bw?`Bandwidth this month: ${(S.bw.used/1048576).toFixed(0)} MB of ${(S.bw.limit/1073741824).toFixed(1)} GB. Previews and avatars are small; full photos/videos/files use more.`:''}),
  h('div',{class:'mut',style:'font-size:13px',text:`Upload limit for this account: ${mbs(cur().max_upload||0)}${cur().premium?' (Premium)':''} — Telegram's own limit`})],[{t:'Close'}])}
async function sessions(){const body=h('div',{},h('div',{class:'empty',text:'Loading…'}));const close=modal('Active sessions',body,[{t:'Close'}]);try{const r=(await api('/app/api/sessions?'+Q({acc:S.acc}))).sessions;body.replaceChildren(...r.map(s=>h('div',{class:'kv'},h('b',{text:(s.current?'● This device — ':'')+(s.app||s.device)}),h('div',{class:'mut',style:'font-size:13px',text:`${s.device} · ${s.platform} · ${s.ip} · ${s.country}`}),h('div',{class:'mut',style:'font-size:12px',text:'Last active '+new Date(s.active*1e3).toLocaleString()}),
  s.current?null:h('button',{style:'color:#e53935',text:'Terminate',on:{click(){confirmBox('Terminate this session?','Terminate',async()=>{try{await api('/app/api/sessions/terminate',A({hash:s.hash}));close();sessions()}catch(e){toast(e.message)}},true)}}}))))}catch(e){body.replaceChildren(h('div',{class:'err',text:e.message}))}}
async function profile(){const c=cur();let about='';try{about=(await api('/app/api/chatinfo?'+Q({acc:S.acc,chat:c.uid}))).info.about||''}catch(e){}const parts=(c.name||'').split(' ');const f=h('input',{value:parts[0]||'',placeholder:'First name'}),l=h('input',{value:parts.slice(1).join(' '),placeholder:'Last name'}),a=h('textarea',{rows:3,maxlength:70,placeholder:'Bio'});a.value=about;
 modal('Edit profile',[f,l,a],[{t:'Cancel'},{t:'Save',c:'p',async fn(close){try{await api('/app/api/me',A({first:f.value,last:l.value,about:a.value}));close();await loadState();toast('Saved')}catch(e){toast(e.message)}}}])}
/* ---------- account जोडा ---------- */
function addAccount(){const body=h('div',{style:'display:flex;flex-direction:column;gap:8px'}),err=h('div',{class:'err'});const close=modal('Add Telegram account',[body,err],[{t:'Cancel'}]);let LID=null;const say=t=>err.textContent=t||'';
 const lab=h('input',{placeholder:'Label (e.g. Office)'}),ph=h('input',{type:'tel',placeholder:'+919876543210'}),d1=h('input',{type:'radio',name:'api',value:'default'}),d2=h('input',{type:'radio',name:'api',value:'custom'}),aid=h('input',{placeholder:'api_id',style:'display:none'}),ah=h('input',{placeholder:'api_hash',style:'display:none'});
 d1.disabled=!S.defApi;(S.defApi?d1:d2).checked=true;const upd=()=>{const c=d2.checked;aid.style.display=ah.style.display=c?'block':'none'};d1.onchange=d2.onchange=upd;upd();
 const go=h('button',{class:'p',style:'background:var(--acc);color:#fff;border-radius:8px;padding:9px',text:'Send code',on:{async click(){say('…');try{const r=await api('/app/api/login/start',{label:lab.value,phone:ph.value,api:d2.checked?'custom':'default',api_id:aid.value,api_hash:ah.value});LID=r.login_id;say();step2()}catch(e){say(e.message)}}}});
 body.append(lab,ph,h('label',{},d1,' Default API (server)'),h('label',{},d2,' Custom api_id / api_hash'),aid,ah,go);
 function step2(){const c=h('input',{placeholder:'Login code',inputmode:'numeric',autocomplete:'off'}),b=h('button',{style:'background:var(--acc);color:#fff;border-radius:8px;padding:9px',text:'Log in',on:{async click(){say('…');try{const r=await api('/app/api/login/code',{login_id:LID,code:c.value});if(r.next==='password')step3();else done(r)}catch(e){say(e.message)}}}});body.replaceChildren(h('div',{class:'mut',text:'Enter the code Telegram sent to your other device:'}),c,b);c.focus()}
 function step3(){say();const p=h('input',{type:'password',placeholder:'Two-step verification password',autocomplete:'off'}),b=h('button',{style:'background:var(--acc);color:#fff;border-radius:8px;padding:9px',text:'Continue',on:{async click(){say('…');try{done(await api('/app/api/login/password',{login_id:LID,password:p.value}))}catch(e){say(e.message)}}}});body.replaceChildren(h('div',{class:'mut',text:'Cloud password:'}),p,b);p.focus()}
 async function done(r){close();S.acc=r.account;LS.set('acc',r.account);await loadState();await loadDialogs();toast('Account added')}}
/* ---------- live events / notifications ---------- */
function beep(){if(!S.set.sound)return;try{const C=window.AudioContext||window.webkitAudioContext;if(!C)return;const c=new C(),o=c.createOscillator(),g=c.createGain();o.frequency.value=880;g.gain.value=.05;o.connect(g);g.connect(c.destination);o.start();setTimeout(()=>{o.stop();c.close()},120)}catch(e){}}
function notify(e){const d=S.dl.find(x=>x.id===e.chat);if(d&&d.muted)return;if(document.hidden||e.chat!==S.chat){beep();if(S.set.notif&&window.Notification&&Notification.permission==='granted'){try{new Notification((d&&d.name)||e.msg.from||'Telegram',{body:(e.msg.text||'📎 Media').slice(0,120),silent:true})}catch(er){}}}}
let dt=null;const soon=()=>{clearTimeout(dt);dt=setTimeout(()=>{loadDialogs();loadState().catch(()=>{})},700)};
function handleEvent(e){if(e.type==='dl'){const k=e.acc+':'+e.chat+':'+e.mid,st={state:e.state,done:e.done,size:e.total,err:e.err||''};if(e.acc===S.acc&&String(e.chat)===String(S.chat))setDl(k,st);else S.dls[k]=st;return}
 if(e.type==='up'){const U=S.uploads[e.uid];if(U){if(e.state==='sending'){U.state='sending';U.sent=e.sent}else if(e.state==='done')finishUpload(U,e.msg);else{U.state=e.state;U.err=e.err||''}drawUploads()}return}
 if(e.acc!==S.acc)return;
 if(e.type==='typing'){if(e.action){S.typing[e.chat]={action:e.action,who:e.who,exp:Date.now()+6500};setTimeout(drawList,6600)}else delete S.typing[e.chat];if(e.chat===S.chat){const d=S.cd;setStatus(e.action?(d&&d.type!=='user'&&e.who?e.who+' is ':'')+(e.action==='recording'?'recording…':'typing…'):(S.ci&&S.ci.status)||'')}drawList();return}
 if(e.type==='read'){if(e.chat===S.chat){S.readOut=Math.max(S.readOut,e.max_id||0);drawMsgs('keep')}return}
 const d=S.dl.find(x=>x.id===e.chat);
 if(e.type==='new'&&e.msg){if(d&&!e.msg.out){d.last=e.msg.text||'📎';d.ts=e.msg.ts;if(e.chat!==S.chat||document.hidden)d.unread=(d.unread||0)+1}if(!e.msg.out)notify(e);drawList()}
 if(e.chat===S.chat){const box=$('#msgs'),atEnd=box.scrollHeight-box.scrollTop-box.clientHeight<140;
  if(e.type==='new'&&e.msg&&!S.msgs.some(x=>x.id===e.msg.id)){S.msgs.push(e.msg);if(!e.msg.out&&!atEnd)S.newBelow=(S.newBelow||0)+1;drawMsgs(atEnd||e.msg.out?'bottom':'keep');if(atEnd&&!document.hidden)markRead()}
  else if(e.type==='edit'&&e.msg)replaceMsg(e.msg);else if(e.type==='del'){S.msgs=S.msgs.filter(x=>!(e.ids||[]).includes(x.id));drawMsgs('keep')}}
 soon()}
async function poll(){for(;;){try{const r=await api('/app/api/events?'+Q({since:S.seq,wait:20}));S.seq=r.seq;for(const e of r.events)handleEvent(e)}catch(e){await sleep(4000)}}}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeMenu();$('#lb').style.display='none';const ov=document.querySelector('.ov');if(ov)ov.remove();if(S.sel.size){S.sel.clear();selUpdate();drawMsgs('keep')}}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&S.chat)markRead()});
/* ---------- start ---------- */
(async()=>{applyTheme();if(window.matchMedia)try{matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyTheme)}catch(e){}
 $('#md').style.color=S.set.md?'var(--acc)':'';
 try{const s=await loadState();S.seq=s.seq||0;drawFolders();if(!S.accs.length){$('#list').replaceChildren(h('div',{class:'empty'},'No Telegram account yet.',h('br'),h('button',{style:'color:var(--acc);font-weight:600;margin-top:10px',text:'＋ Add account',on:{click:addAccount}})))}else await loadDialogs()}
 catch(e){document.body.replaceChildren(h('div',{class:'empty',text:'✖ '+e.message}));return}
 poll();setInterval(()=>loadState().catch(()=>{}),60000);setInterval(()=>{if(Object.keys(S.typing).length)drawList()},3000)})();
</script></body></html>'''
