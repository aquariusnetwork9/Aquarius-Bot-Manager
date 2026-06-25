/* ===========================================================================
   ABM Control — live wiring layer  (Phase A / v3.0.0)
   ---------------------------------------------------------------------------
   Turns the static Mission-Control mockup into a live control surface. It is
   LAYOUT-AWARE (window.STYLE = 'v1'|'v2'|'v3') but mostly layout-agnostic: it
   patches the shared `MODULES` data model + the shared `ABMMap` Live-Map
   component, so every theme that loads `abm-control-data.js` goes live at once.

   Endpoints (all relative to LIVE.base):
     GET  /control/state        → {modules:[{name,enabled}], control}
     POST /control/command      → run a console command, returns {title,lines}
     GET  /viewer/inventory     → vitals + armor + inventory (Phase B uses more)
     GET  /viewer/stream (SSE)  → ~20 Hz {x,y,z,yaw,health,food,dimension,entities}
     GET  /viewer/map.png       → bot-centred map tile (Live-Map background)

   base resolution:
     ?mock=<url>   → hit a bot loopback base directly (cross-origin; testing)
     ?direct=1     → same-origin /control/* + /viewer/* (page served by the bot/mock)
     default       → /api/instances/<inst>/...  (the ABM relay, production)
   Config subcards are READ-ONLY in Phase A (values are model defaults, not the
   bot's live config) — live editing arrives with the /control/config API.
   =========================================================================== */
(function(){
  'use strict';
  if (typeof MODULES === 'undefined') { console.error('[live] data model not loaded'); return; }

  var STYLE = window.STYLE || 'v1';
  var Q = new URLSearchParams(location.search);
  var INST = Q.get('inst') || '';
  var MOCK = Q.get('mock');
  var DIRECT = Q.get('direct') === '1' || Q.get('direct') === 'true';
  var BASE = MOCK ? MOCK.replace(/\/+$/,'')
           : DIRECT ? ''
           : ('/api/instances/' + encodeURIComponent(INST));
  var LIVE = window.LIVE = { style:STYLE, inst:INST, base:BASE, connected:false, state:null, modState:{} };

  /* raw config-class name (what /control/state returns) → model module id */
  var RAW2ID = {};
  MODULES.forEach(function(m){ if(m.raw) RAW2ID[m.raw.toLowerCase()] = m.id; });
  /* a few aliases where the live module name differs from the model's raw key */
  var RAW_ALIAS = { liveviewer:'livemap', pearlmanager:'pearl' };

  /* per-theme selectors — the three layouts render the SAME data model + ABMMap
     differently, so the live wiring targets each via this table */
  var LAYOUT = {
    v1:{ nav:'#mlist .mrow', dotBase:'sd', word:'.mi-tx .s', chip:'.mhead .statchip', chipCls:'statchip', acts:'.actbar', cfg:'.groups', topAnchor:'.topbar .sp', metric:'.mhead .metric' },
    v2:{ nav:'#pills .pill', dotBase:'pd', word:null,         chip:'.hero .statchip', chipCls:'statchip', acts:'.acts',   cfg:'.cfg',     topAnchor:'header .sp',  metric:'.hero .met' },
    v3:{ nav:'#rail .rrow', dotBase:'sd', word:null,          chip:'#chead .stat',    chipCls:'stat',     acts:'.acts',   cfg:'#ibody',   topAnchor:null,          metric:'#chead .met' }
  };
  var LO = LAYOUT[STYLE] || LAYOUT.v1;

  /* ---------------- small helpers ---------------- */
  function $(s,r){ return (r||document).querySelector(s); }
  function api(path){ return BASE + path; }
  function fnum(v,d){ v=parseFloat(v); return isFinite(v)?v:(d||0); }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }

  var toastWrap;
  function toast(msg, kind, ms){
    if(!toastWrap){ toastWrap=document.createElement('div'); toastWrap.id='liveToast'; document.body.appendChild(toastWrap); }
    var t=document.createElement('div'); t.className='toast '+(kind||''); t.textContent=msg; toastWrap.appendChild(t);
    setTimeout(function(){ t.style.transition='.3s'; t.style.opacity='0'; setTimeout(function(){ t.remove(); }, 320); }, ms||2600);
  }

  /* ---------------- command execution ---------------- */
  function runCommand(cmd, opts){
    opts = opts || {};
    return fetch(api('/control/command'), {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ command: cmd })
      })
      .then(function(r){
        if(r.status===403) throw new Error('control disabled on this bot (server.viewer.control=false)');
        if(!r.ok) throw new Error('HTTP '+r.status);
        return r.json();
      })
      .then(function(d){
        if(!opts.quiet){
          var line = (d && (d.title || (d.lines && d.lines[0]))) || ('› '+cmd);
          toast(line, 'ok');
        }
        return d;
      })
      .catch(function(e){ toast((opts.label||cmd)+': '+e.message, 'err', 4200); throw e; });
  }
  window.liveRun = runCommand;

  /* ---------------- connection chrome ---------------- */
  function setConn(on){
    LIVE.connected = on;
    var d=$('#connDot'), t=$('#connText'), bd=$('#botDot'), bn=$('#botName'), bx=$('#bcBox');
    if(d) d.classList.toggle('off', !on);
    if(t) t.textContent = on ? (INST||'bot')+' · live' : (INST||'bot')+' · offline';
    if(bd) bd.classList.toggle('off', !on);
    if(bn) bn.textContent = INST || 'bot';
    if(bx) bx.textContent = INST || 'bot';
    document.title = 'ABM · Mission Control · ' + (INST||'bot');
  }

  /* ---------------- module-state polling (in place, no full re-render) ---------------- */
  function resolveId(rawName){
    var k=(rawName||'').toLowerCase();
    return RAW_ALIAS[k] || RAW2ID[k] || null;
  }
  function applyState(d){
    if(!d || d.offline){ setConn(false); return; }
    setConn(true);
    LIVE.controlEnabled = d.control !== false;
    // the Live Map is the viewer itself — it's "running" whenever we're connected
    if(MAP['livemap']){ MAP['livemap'].status='run'; MAP['livemap'].sdot='run'; MAP['livemap'].enabled=true; }
    (d.modules||[]).forEach(function(ms){
      var id=resolveId(ms.name); if(!id) return;
      var m=MAP[id]; if(!m) return;
      LIVE.modState[id] = !!ms.enabled;
      m.enabled = !!ms.enabled;
      m.status  = ms.enabled ? 'run' : 'idle';
      m.sdot    = ms.enabled ? 'run' : '';
    });
    refreshNavDots();
    refreshHeaderStatus();
  }
  function pollState(){
    fetch(api('/control/state'), {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(applyState)
      .catch(function(){ setConn(false); });
  }

  function statusWord(s){ return s==='run'?'running':s==='busy'?'working':'idle'; }

  /* update the nav status dots (+ words on v1) in place — never nukes open subcards */
  function refreshNavDots(){
    document.querySelectorAll(LO.nav).forEach(function(row){
      var id=row.dataset.id; if(!id) return; var m=MAP[id]; if(!m) return;
      var dot=row.querySelector('.'+LO.dotBase); if(dot) dot.className=LO.dotBase+' '+(m.sdot||'');
      if(LO.word){ var s=row.querySelector(LO.word); if(s) s.textContent=statusWord(m.status); }
    });
  }
  function refreshHeaderStatus(){
    var m=MAP[cur]; if(!m) return;
    var chip=$(LO.chip);
    if(chip){
      chip.className=LO.chipCls+' '+m.status;
      chip.innerHTML = (LO.chipCls==='statchip' ? '<span class="d"></span>' : '') + statusWord(m.status);
    }
    var tg=$('.enbox .tgl'); if(tg) tg.classList.toggle('on', !!m.enabled);
  }

  /* ---------------- enable toggle + action wiring (layout-aware) ---------------- */
  var STOP_RE = /stop|disconnect|pause|halt|■/i;
  var SAVE_RE = /save/i;
  /* per-module command overrides for the primary action buttons */
  var ACTIONS = {
    elytra:  { start: elytraFly, stop: function(){ return runCommand('fly stop'); } },
    highway: { start: function(){ return runCommand('highway start'); }, stop: function(){ return runCommand('highway stop'); } }
  };
  function moduleCmd(id){ var m=MAP[id]; return (m && m.raw ? m.raw : id).toLowerCase(); }

  function setEnabled(id, on){
    var act = ACTIONS[id];
    var p = (act && (on?act.start:act.stop)) ? (on?act.start:act.stop)() : runCommand(moduleCmd(id)+(on?' on':' off'), {label:MAP[id].name});
    Promise.resolve(p).then(function(){ setTimeout(pollState, 600); });
  }

  function elytraFly(){
    var dim = (LIVE.state && (LIVE.state.dimension||'')).toLowerCase();
    var d = dim.indexOf('nether')>=0 ? 'nether' : dim.indexOf('end')>=0 ? 'end' : 'overworld';
    var tx = $('.fctrl[data-fid="tx"] input'), tz = $('.fctrl[data-fid="tz"] input');
    if(!tx || !tz){ toast('open the Destination card and set Target X/Z first','err',3200); return Promise.reject(); }
    var x=Math.round(fnum(tx.value)), z=Math.round(fnum(tz.value));
    return runCommand('fly trip '+d+' '+x+' '+z, {label:'Fly'});
  }

  /* wire whichever action buttons the active layout rendered for the current module */
  function wireActions(){
    var m=MAP[cur]; if(!m) return;
    var bar = $(LO.acts); if(!bar) return;
    [].forEach.call(bar.querySelectorAll('button'), function(btn){
      if(btn.dataset.lw) return; btn.dataset.lw='1';
      var label=(btn.textContent||'').trim();
      btn.addEventListener('click', function(){
        if(SAVE_RE.test(label)){ toast('saved locally — live config save arrives in v3.1','ok',3000); return; }
        if(cur==='elytra' && /fly/i.test(label)){ elytraFly(); return; }
        if(STOP_RE.test(label)) setEnabled(cur, false);
        else setEnabled(cur, true);
      });
    });
  }

  /* inject a real enable toggle next to the status chip (every layout has a header chip) */
  function injectEnableToggle(){
    var m=MAP[cur]; if(!m) return;
    var head=$(LO.chip); if(!head || $('.enbox')) return;
    var box=document.createElement('span'); box.className='enbox';
    box.style.cssText='display:inline-flex;align-items:center;gap:.4rem;margin-left:.5rem;font-family:var(--mono,monospace);font-size:.62rem;color:var(--dim,#7b8a98)';
    box.innerHTML='module <span class="tgl '+(m.enabled?'on':'')+'" style="position:relative;display:inline-block;width:34px;height:18px;border-radius:18px;cursor:pointer;vertical-align:middle"></span>';
    head.insertAdjacentElement('afterend', box);
    box.querySelector('.tgl').addEventListener('click', function(){
      var on=!this.classList.contains('on'); this.classList.toggle('on',on); setEnabled(cur,on);
    });
  }

  /* mark config subcards read-only with an honest banner (Phase A) — JS-driven so it
     works regardless of each theme's own CSS */
  function lockConfig(){
    var g=$(LO.cfg); if(!g || g.dataset.ro) return; g.dataset.ro='1';
    var b=document.createElement('div');
    b.style.cssText='display:flex;align-items:center;gap:.5rem;font-size:.74rem;color:#ffb454;background:#ffb4540d;'+
      'border:1px solid #5a3b1f;border-radius:10px;padding:.55rem .75rem;margin-bottom:.6rem';
    b.innerHTML='⚠ Settings preview — these are defaults, not the bot’s live config. Live editing lands with the config API (v3.1).';
    g.insertBefore(b, g.firstChild);
    [].forEach.call(g.querySelectorAll('input,select'), function(e){ e.setAttribute('disabled','disabled'); e.style.opacity='.65'; });
    [].forEach.call(g.querySelectorAll('.tgl,.seg button,.chip .x,.chip.add'), function(e){ e.style.pointerEvents='none'; e.style.opacity='.65'; });
  }

  /* ---------------- live Map (shared across all themes) ---------------- */
  var mapTimer=null;
  function hostile(t){ return /ZOMBIE|SKELET|CREEPER|WITHER|BLAZE|PIGLIN|HOGLIN|GHAST|ENDERMAN|SPIDER|SLIME|MAGMA|VEX|VINDICATOR|PILLAGER|RAVAGER|WARDEN|PHANTOM|DROWNED|HUSK|STRAY|GUARDIAN|SHULKER|WITCH/i.test(t||''); }
  function entClass(t){ t=(t||'').toUpperCase(); if(t.indexOf('PLAYER')>=0)return'player'; if(t.indexOf('ITEM')>=0)return'item'; return hostile(t)?'mobH':'mobP'; }

  function bindMap(){
    var cv=$('.amCanvas'); if(!cv) return;
    // real bot-centred map.png as the backdrop; bot stays at centre
    var span = fnum(cv.dataset.span, 512);
    cv.style.backgroundImage = "url('"+api('/viewer/map.png')+"?t="+Date.now()+"')";
    cv.style.backgroundSize = 'cover'; cv.style.backgroundPosition='center';
    // strip the mockup's fake entities/pins; we draw real ones from the SSE feed
    [].forEach.call(cv.querySelectorAll('.amE,.amPin'), function(e){ e.remove(); });
    refreshMapOverlay();
    if(mapTimer) clearInterval(mapTimer);
    mapTimer=setInterval(function(){
      var c=$('.amCanvas'); if(!c){ clearInterval(mapTimer); mapTimer=null; return; }
      c.style.backgroundImage = "url('"+api('/viewer/map.png')+"?t="+Date.now()+"')";
    }, 2000);
    // make click-to-destination actually send to Elytra
    cv.onclick = function(ev){ onMapClick(ev, cv, span); };
  }
  function refreshMapOverlay(){
    var cv=$('.amCanvas'); if(!cv) return;
    var s=LIVE.state; var span=fnum(cv.dataset.span,512);
    if(s){ cv.dataset.bx=Math.round(s.x); cv.dataset.bz=Math.round(s.z);
      var pill=$('.amTop .amMono'); if(pill) pill.textContent='⌖ '+Math.round(s.x)+', '+Math.round(s.y)+', '+Math.round(s.z);
      var dimPill=cv.closest('.abmmap'); if(dimPill){ var dp=dimPill.querySelector('.amPill:not(.amMono)'); if(dp) dp.textContent='🌍 '+prettyDim(s.dimension); }
    }
    [].forEach.call(cv.querySelectorAll('.amE'), function(e){ e.remove(); });
    if(s && s.entities && s.entities.length){
      var bx=s.x, bz=s.z, frag=document.createDocumentFragment(), n=0;
      s.entities.forEach(function(e){
        var px=50+((e.x-bx)/span)*100, py=50+((e.z-bz)/span)*100;
        if(px<-2||px>102||py<-2||py>102) return;
        var sp=document.createElement('span'); sp.className='amE amE-'+entClass(e.type);
        sp.style.left=px+'%'; sp.style.top=py+'%'; sp.title=e.type||''; cv.appendChild(sp); n++;
      });
      var leg=cv.closest('.abmmap'); var ro=leg&&leg.querySelector('.amReadout');
      if(ro && !ro.dataset.dest) ro.textContent='Tracking '+n+' nearby entit'+(n===1?'y':'ies')+' · click the map to set an Elytra destination.';
    }
  }
  function onMapClick(ev, cv, span){
    if(ev.target && ev.target.closest && ev.target.closest('.amPin')) return;
    var r=cv.getBoundingClientRect(); var px=(ev.clientX-r.left)/r.width, py=(ev.clientY-r.top)/r.height;
    px=Math.max(0,Math.min(1,px)); py=Math.max(0,Math.min(1,py));
    var bx=fnum(cv.dataset.bx), bz=fnum(cv.dataset.bz);
    var wx=Math.round(bx+(px-.5)*span), wz=Math.round(bz+(py-.5)*span);
    var d=cv.querySelector('.amDest'); if(d){ d.style.display='block'; d.style.left=(px*100)+'%'; d.style.top=(py*100)+'%'; }
    var box=cv.closest('.abmmap'), ro=box&&box.querySelector('.amReadout');
    if(ro){ ro.dataset.dest='1';
      ro.innerHTML='<b>Destination</b> <span class="amMono">'+wx+', ~64, '+wz+'</span> '+
        '<button class="amAct" id="amSendDest">▶ Send to Elytra</button>'+
        '<button class="amAct ghost" id="amClearDest">clear</button>';
      var dim=(LIVE.state&&(LIVE.state.dimension||'')).toLowerCase();
      var dd=dim.indexOf('nether')>=0?'nether':dim.indexOf('end')>=0?'end':'overworld';
      $('#amSendDest').onclick=function(){
        runCommand('fly trip '+dd+' '+wx+' '+wz, {label:'Fly'}).then(function(){
          ro.innerHTML='✅ Sent to Elytra Autopilot — flying to '+wx+', '+wz+' ('+dd+').';
        });
      };
      $('#amClearDest').onclick=function(){ if(d)d.style.display='none'; delete ro.dataset.dest; ro.textContent='Tip: click the map to drop an Elytra destination.'; };
    }
  }
  function prettyDim(d){ d=(d||'').toLowerCase(); return d.indexOf('nether')>=0?'Nether':d.indexOf('end')>=0?'The End':'Overworld'; }

  /* ---------------- SSE telemetry (vitals + map + speed) ---------------- */
  var es=null, lastPos=null, lastT=0, speedEMA=0;
  function startStream(){
    try{ es=new EventSource(api('/viewer/stream')); }catch(e){ return; }
    es.onmessage=function(ev){ try{ onState(JSON.parse(ev.data)); }catch(e){} };
    es.onerror=function(){ /* ABM 503s → browser retries; vitals just pause */ };
  }
  function onState(s){
    LIVE.state=s;
    // speed estimate from position deltas (b/s, EMA) — used by the Elytra cockpit later
    var now=(s.t||Date.now()); if(lastPos){ var dt=Math.max(1,(now-lastT))/1000;
      var d=Math.hypot(s.x-lastPos.x, s.z-lastPos.z); speedEMA=speedEMA*0.6 + (d/dt)*0.4; }
    lastPos={x:s.x,z:s.z}; lastT=now; LIVE.speed=speedEMA;
    if(MAP['livemap']){ var ne=(s.entities&&s.entities.length)||0;
      MAP['livemap'].metric='Following '+(INST||'bot')+' · live · '+ne+' entit'+(ne===1?'y':'ies')+' · '+Math.round(s.x)+', '+Math.round(s.y)+', '+Math.round(s.z);
      if(cur==='livemap'){ var mt=$(LO.metric); if(mt) mt.textContent=MAP['livemap'].metric; } }
    updateVitals(s);
    refreshMapOverlay();
  }
  function updateVitals(s){
    var chip=$('#vitChip'); if(!chip) return;
    var hp=Math.round((s.health!=null?s.health:0));
    var fd=Math.round((s.food!=null?s.food:0));
    chip.innerHTML='<span class="hp">♥ '+hp+'</span><span class="fd">🍗 '+fd+'</span>'+
      '<span class="ps">⌖ '+Math.round(s.x)+', '+Math.round(s.y)+', '+Math.round(s.z)+'</span>'+
      '<span>'+prettyDim(s.dimension)+'</span>'+
      (LIVE.speed>1?'<span>'+Math.round(LIVE.speed)+' b/s</span>':'');
  }

  /* ---------------- topbar injections (vitals, command runner, style switch) ---------------- */
  function injectTopbar(){
    // live vitals chip
    var v=document.createElement('span'); v.className='vchip'; v.id='vitChip'; v.textContent='—';
    v.style.cssText='display:inline-flex;align-items:center;gap:.5rem;font-family:var(--mono,monospace);font-size:.66rem;'+
      'color:var(--dim,#7b8a98);border:1px solid var(--line,#1d2730);border-radius:9px;padding:.3rem .55rem;white-space:nowrap';
    // command runner
    var cr=document.createElement('span'); cr.style.cssText='display:inline-flex;align-items:center;gap:.4rem';
    cr.innerHTML='<input id="cmdInput" placeholder="run command…" spellcheck="false" '+
      'style="font-family:var(--mono,monospace);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:8px;padding:.4rem .55rem;width:170px">'+
      '<button id="cmdGo" class="go" style="border:1px solid var(--acc-dim,#1f7a55);color:var(--acc,#3ddc97);background:var(--panel,#11171e);border-radius:8px;padding:.42rem .7rem;font-weight:700;cursor:pointer;font-size:.78rem">Run</button>';
    // style switcher
    var sw=document.createElement('span');
    sw.innerHTML='<select id="styleSel" title="appearance" '+
      'style="font-family:var(--sans,sans-serif);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:8px;padding:.4rem .5rem;cursor:pointer">'+
      ['v1','v2','v3'].map(function(x){ return '<option value="'+x+'"'+(x===STYLE?' selected':'')+'>'+
        ({v1:'Mission Control',v2:'Aurora Glass',v3:'Console Pro'}[x])+'</option>'; }).join('')+'</select>';

    var anchor = LO.topAnchor ? $(LO.topAnchor) : null;
    if(anchor){
      anchor.insertAdjacentElement('afterend', v);
      v.insertAdjacentElement('afterend', cr);
      cr.insertAdjacentElement('afterend', sw);
    } else {
      var bar=document.createElement('div');
      bar.style.cssText='position:fixed;top:10px;right:14px;z-index:9990;display:flex;align-items:center;gap:.5rem;'+
        'background:rgba(10,14,18,.85);backdrop-filter:blur(8px);border:1px solid var(--line,#1d2730);border-radius:12px;padding:.35rem .5rem;box-shadow:0 8px 24px #0007';
      bar.appendChild(v); bar.appendChild(cr); bar.appendChild(sw); document.body.appendChild(bar);
    }
    var inp=$('#cmdInput');
    function go(){ var c=(inp.value||'').trim(); if(!c) return; runCommand(c).then(function(){ inp.value=''; }); }
    $('#cmdGo').onclick=go;
    inp.addEventListener('keydown', function(e){ if(e.key==='Enter') go(); });
    $('#styleSel').onchange=function(){
      try{ localStorage.setItem('abmControlStyle', this.value); }catch(e){}
      var u=new URL(location.href); u.searchParams.set('style', this.value); location.href=u.toString();
    };
  }

  /* ---------------- render hook ---------------- */
  function afterRender(){
    injectEnableToggle();
    wireActions();
    lockConfig();
    if(MAP[cur] && MAP[cur].signature==='liveMap') bindMap();
    refreshHeaderStatus();
  }
  // wrap the layout's global render() so every re-render re-applies live wiring
  if(typeof render==='function'){
    var _render=render;
    window.render=function(){ _render.apply(this,arguments); try{ afterRender(); }catch(e){ console.error(e); } };
  }

  /* ---------------- boot ---------------- */
  function boot(){
    setConn(false);
    // honest default: nothing reads as "running" until /control/state says so
    MODULES.forEach(function(m){ if(m.id!=='livemap'){ m.status='idle'; m.sdot=''; m.enabled=false; } });
    // label the Live Map with the real bot + drop the mockup's fake pins
    if(typeof ABMMap!=='undefined' && ABMMap.bot){ ABMMap.bot.name = INST || ABMMap.bot.name; ABMMap.pins=[]; }
    injectTopbar();
    // default the surface to the Live Map (the headline) on first load
    if(typeof cur!=='undefined' && MAP['livemap']){ try{ cur='livemap'; render(); }catch(e){} }
    else { try{ render(); }catch(e){} }
    pollState(); setInterval(pollState, 3000);
    startStream();
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
