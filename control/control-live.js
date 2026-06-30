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

  /* capability gating (shared-access guests): owner = full; else view < operate < config */
  var CAP_RANK = { view:0, operate:1, config:2 };
  LIVE.cap = null;     // null = owner / no gating
  LIVE.perms = null;   // set for named users: {modules:{id:{use,config}}, console, lifecycle, ...}
  function capOk(level){ return LIVE.cap == null || CAP_RANK[LIVE.cap] >= CAP_RANK[level]; }
  /* fine-grained checks: named users go by their perms; guests/owner fall back to the cap tier */
  function moduleUse(id){ if(LIVE.perms){ var e=LIVE.perms.modules[id]; return !!(e && (e.use||e.config)); } return capOk('operate'); }
  function moduleConfig(id){ if(LIVE.perms){ var e=LIVE.perms.modules[id]; return !!(e && e.config); } return capOk('config'); }
  function consoleOk(){ return LIVE.perms ? !!LIVE.perms.console : capOk('operate'); }
  function fetchPrincipal(){
    return fetch('/api/authstatus', {cache:'no-store'})
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){
        if(!d) return;
        if(d.principal === 'guest'){ LIVE.cap = d.capability || 'view';
          document.body.classList.add('lguest','lguest-'+LIVE.cap); }
        else if(d.principal === 'user' && !d.is_admin){ LIVE.cap = d.capability || 'view'; LIVE.perms = d.perms || null;
          document.body.classList.add('lguest','lguest-'+LIVE.cap); }
        // owner / admin: no gating
      })
      .catch(function(){});
  }
  /* hide module nav rows the user isn't allowed to use (server still enforces every action) */
  function filterNav(){
    if(!LIVE.perms) return;
    [].forEach.call(document.querySelectorAll(LO.nav), function(row){
      var id=row.dataset.id; if(id && !moduleUse(id)) row.style.display='none';
    });
  }

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

  /* open the live fullscreen map for THIS bot (the mockup hard-coded a relative URL with no ?inst) */
  function openFullscreenMap(){
    var u=new URL('/control/control-v4-spatial-map.html', location.origin);
    if(INST) u.searchParams.set('inst', INST);
    if(STYLE && STYLE!=='v1') u.searchParams.set('style', STYLE);
    if(MOCK) u.searchParams.set('mock', MOCK);
    if(DIRECT) u.searchParams.set('direct','1');
    window.open(u.toString(), '_blank');
  }
  window.abmOpenFullscreenMap=openFullscreenMap;

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
    if(!moduleUse(cur)){   // user can't use this module: actions are read-only
      [].forEach.call(bar.querySelectorAll('button'), function(b){ b.disabled=true; b.style.opacity='.5'; b.style.cursor='not-allowed'; });
      return;
    }
    [].forEach.call(bar.querySelectorAll('button'), function(btn){
      if(btn.dataset.lw) return; btn.dataset.lw='1';
      var label=(btn.textContent||'').trim();
      btn.addEventListener('click', function(){
        if(/fullscreen/i.test(label)){ openFullscreenMap(); return; }
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
    var tg=box.querySelector('.tgl');
    if(!moduleUse(cur)){ tg.style.pointerEvents='none'; tg.style.opacity='.5'; return; }
    tg.addEventListener('click', function(){
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
    b.innerHTML='ℹ Friendly overview (read-only). Edit the bot’s real, live values in <b>⚙ Live configuration</b> above.';
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
    // make click-to-destination actually send to Elytra (parameterized command = console grant)
    if(consoleOk()) cv.onclick = function(ev){ onMapClick(ev, cv, span); };
    // the mockup's "⛶ Fullscreen" button hard-codes a relative URL with no ?inst — point it at the live page
    [].forEach.call(document.querySelectorAll('.amBtn'), function(b){
      if(/fullscreen/i.test(b.textContent||'')){ b.onclick=function(e){ if(e&&e.preventDefault)e.preventDefault(); openFullscreenMap(); }; }
    });
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
    if(!consoleOk()) cr.style.display='none';   // no free-form console without the console grant
    var inp=$('#cmdInput');
    function go(){ var c=(inp.value||'').trim(); if(!c) return; runCommand(c).then(function(){ inp.value=''; }); }
    $('#cmdGo').onclick=go;
    inp.addEventListener('keydown', function(e){ if(e.key==='Enter') go(); });
    $('#styleSel').onchange=function(){
      try{ localStorage.setItem('abmControlStyle', this.value); }catch(e){}
      var u=new URL(location.href); u.searchParams.set('style', this.value); location.href=u.toString();
    };
  }

  /* ---------------- live config (v3.1) ---------------- */
  /* modules not under client.extra.<lcfirst(raw)> get an explicit root */
  var CFG_ROOT_OVERRIDE = { account:'authentication', discord:'discord', livemap:'server.viewer' };

  function fetchConfig(){
    return fetch(api('/control/config'), {cache:'no-store'})
      .then(function(r){ if(!r.ok) throw 0; return r.json(); })
      .then(function(d){ LIVE.config = (d && !d.offline) ? d : null; return LIVE.config; })
      .catch(function(){ LIVE.config = null; });
  }
  function getPath(obj, path){ var o=obj, ps=path.split('.'); for(var i=0;i<ps.length;i++){ if(o==null) return undefined; o=o[ps[i]]; } return o; }
  function setLocalPath(obj, path, v){ var ps=path.split('.'), o=obj; for(var i=0;i<ps.length-1;i++){ if(o[ps[i]]==null) return; o=o[ps[i]]; } o[ps[ps.length-1]]=v; }
  function lcfirst(s){ return s ? s.charAt(0).toLowerCase()+s.slice(1) : s; }
  function prettyKey(k){ return k.replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/[_-]+/g,' ').replace(/^./,function(c){return c.toUpperCase();}).trim(); }

  function moduleRoot(m){
    if(!m || !LIVE.config) return null;
    if(CFG_ROOT_OVERRIDE[m.id]) return getPath(LIVE.config, CFG_ROOT_OVERRIDE[m.id])!==undefined ? CFG_ROOT_OVERRIDE[m.id] : null;
    var guess='client.extra.'+lcfirst(m.raw||'');
    if(getPath(LIVE.config, guess)!==undefined) return guess;
    var ex=getPath(LIVE.config,'client.extra')||{}, t=(m.raw||'').toLowerCase();
    for(var k in ex){ if(k.toLowerCase()===t) return 'client.extra.'+k; }
    return null;
  }

  function cfgFieldHtml(path, label, v){
    var t=typeof v, c='';
    if(t==='boolean') c='<span class="lcTgl tgl '+(v?'on':'')+'" data-path="'+esc(path)+'"></span>';
    else if(t==='number') c='<input class="lcInp" type="text" inputmode="decimal" spellcheck="false" data-path="'+esc(path)+'" data-kind="num" value="'+v+'">';
    else c='<input class="lcInp" type="text" spellcheck="false" data-path="'+esc(path)+'" data-kind="str" value="'+esc(v)+'">';
    return '<div class="lcRow"><span class="lcLbl">'+esc(label)+'</span><span class="lcCtl">'+c+'</span></div>';
  }
  function cfgNodeHtml(obj, base, depth){
    var html='';
    for(var k in obj){
      var v=obj[k]; if(v===null||v===undefined) continue;
      var path=base+'.'+k, t=typeof v;
      if(t==='boolean'||t==='number'||t==='string') html+=cfgFieldHtml(path, prettyKey(k), v);
      else if(Array.isArray(v)) html+='<div class="lcRow"><span class="lcLbl">'+esc(prettyKey(k))+'</span><span class="lcCtl lcRO">'+v.length+' item'+(v.length===1?'':'s')+' · edit via console</span></div>';
      else if(t==='object' && depth<2) html+='<div class="lcSub">'+esc(prettyKey(k))+'</div>'+cfgNodeHtml(v, path, depth+1);
    }
    return html;
  }
  function buildLiveConfig(){
    var m=MAP[cur]; if(!m) return;
    var cont=$(LO.cfg); if(!cont || $('#lcPanel')) return;
    var root=moduleRoot(m), sub=root?getPath(LIVE.config, root):null;
    var panel=document.createElement('div'); panel.id='lcPanel'; panel.className='lcPanel';
    if(!LIVE.config){
      panel.innerHTML='<div class="lcHead">⚙ Live configuration</div><div class="lcNote">Config API not available on this bot — needs a v3.1+ build with <code>server.viewer.control</code>.</div>';
    } else if(!sub || typeof sub!=='object' || Array.isArray(sub)){
      panel.innerHTML='<div class="lcHead">⚙ Live configuration</div><div class="lcNote">No editable config is mapped for this module.</div>';
    } else {
      panel.innerHTML='<div class="lcHead">⚙ Live configuration <span class="lcRootTag">'+esc(root)+'</span></div>'+
        '<div class="lcNote">Edits apply on the bot immediately and persist to its config.</div>'+cfgNodeHtml(sub, root, 0);
    }
    cont.insertBefore(panel, cont.firstChild);
    if(!moduleConfig(cur)){
      // no config grant for this module: show values read-only
      [].forEach.call(panel.querySelectorAll('.lcInp'), function(e){ e.setAttribute('disabled','disabled'); e.style.opacity='.6'; });
      [].forEach.call(panel.querySelectorAll('.lcTgl'), function(e){ e.style.pointerEvents='none'; e.style.opacity='.6'; });
      var n=panel.querySelector('.lcNote'); if(n) n.textContent='Read-only — your access doesn’t include editing this module’s configuration.';
      return;
    }
    [].forEach.call(panel.querySelectorAll('.lcTgl'), function(tg){
      tg.addEventListener('click', function(){ var on=!this.classList.contains('on'); this.classList.toggle('on',on); setConfig(this.dataset.path, on, this); });
    });
    [].forEach.call(panel.querySelectorAll('.lcInp'), function(inp){
      inp.addEventListener('change', function(){
        var val=this.dataset.kind==='num'?parseFloat(this.value):this.value;
        if(this.dataset.kind==='num' && !isFinite(val)){ toast('not a number','err'); return; }
        setConfig(this.dataset.path, val, this);
      });
    });
  }
  function setConfig(path, value, el){
    if(el) el.classList.add('lcBusy');
    fetch(api('/control/config'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:path,value:value})})
      .then(function(r){ if(r.status===403) throw new Error('protected field / control disabled'); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(d){ if(d && d.ok===false) throw new Error(d.error||'set failed');
        var nv=(d && d.value!==undefined)?d.value:value;
        if(LIVE.config) setLocalPath(LIVE.config, path, nv);
        toast(prettyKey(path.split('.').pop())+' = '+nv, 'ok');
        if(el){ el.classList.remove('lcBusy'); el.classList.add('lcOk'); setTimeout(function(){ el.classList.remove('lcOk'); }, 900); }
      })
      .catch(function(e){ if(el) el.classList.remove('lcBusy'); toast(path.split('.').pop()+': '+e.message,'err',4200); });
  }

  function injectLiveConfigCss(){
    if($('#lcCss')) return;
    var s=document.createElement('style'); s.id='lcCss';
    s.textContent=
      '.lcPanel{border:1px solid var(--acc-dim,#1f7a55);border-radius:12px;background:#3ddc970a;padding:.55rem .8rem .7rem;margin-bottom:.7rem}'+
      '.lcHead{font-weight:700;font-size:.84rem;display:flex;align-items:center;gap:.5rem;margin-bottom:.15rem}'+
      '.lcRootTag{font-family:var(--mono,monospace);font-size:.58rem;color:var(--dim,#7b8a98);border:1px solid var(--line,#1d2730);border-radius:5px;padding:.04rem .32rem;font-weight:400}'+
      '.lcNote{font-family:var(--mono,monospace);font-size:.6rem;color:var(--dim,#7b8a98);margin-bottom:.45rem}'+
      '.lcNote code{color:var(--warn,#ffb454)}'+
      '.lcSub{font-family:var(--mono,monospace);font-size:.58rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim,#7b8a98);margin:.5rem 0 .15rem;border-top:1px solid #ffffff10;padding-top:.35rem}'+
      '.lcRow{display:flex;align-items:center;gap:.7rem;min-height:32px;padding:.12rem 0}'+
      '.lcRow+.lcRow{border-top:1px solid #ffffff08}'+
      '.lcLbl{flex:1;font-size:.8rem}'+
      '.lcCtl{display:flex;align-items:center;justify-content:flex-end;min-width:40%}'+
      '.lcInp{font-family:var(--mono,monospace);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:7px;padding:.32rem .45rem;width:130px;text-align:right;transition:.2s}'+
      '.lcInp:focus{outline:none;border-color:var(--acc,#3ddc97)}'+
      '.lcInp.lcOk,.lcTgl.lcOk{box-shadow:0 0 0 1px var(--acc,#3ddc97)}'+
      '.lcInp.lcBusy{opacity:.5}'+
      '.lcTgl{position:relative;display:inline-block;width:38px;height:20px;border-radius:20px;background:#2a3640;border:1px solid var(--line,#1d2730);cursor:pointer;transition:.15s}'+
      '.lcTgl::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#8696a3;transition:.15s}'+
      '.lcTgl.on{background:var(--acc-dim,#1f7a55);border-color:var(--acc,#3ddc97)}.lcTgl.on::after{left:20px;background:var(--acc,#3ddc97)}'+
      '.lcRO{font-family:var(--mono,monospace);font-size:.62rem;color:var(--dim,#7b8a98)}';
    document.head.appendChild(s);
  }

  /* ===========================================================================
     List editors (v3.10) — the three "add an entry" lists the mockup drew but
     never wired: villager trades, saved Elytra trips, and pearl locations. Each
     maps to a real config CONTAINER (a Map or a List) under client.extra; the
     editor reads live entries from LIVE.config, deletes by key/index, and adds
     via a small modal form that POSTs the new entry to /control/config (op
     put|add|remove). The bot deserializes it straight into the config type.
     Lives in the config column (LO.cfg) so it works in every theme; the dead
     mockup ".addrow" buttons are also wired to open the same form.
     =========================================================================== */
  function mcStrip(id){ return String(id||'').replace(/^minecraft:/,''); }
  function capWords(s){ return String(s||'').toLowerCase().replace(/(^|[_\s])([a-z])/g, function(_,a,b){ return (a===''?'':' ')+b.toUpperCase(); }).trim(); }

  /* book-enchant display name -> {id, level} (bare registry id, as the bot's EnchantmentRegistry keys them) */
  var ENCH_ROMAN = { i:1, ii:2, iii:3, iv:4, v:5 };
  function parseEnch(disp){
    var parts=String(disp||'').trim().split(/\s+/);
    var last=(parts[parts.length-1]||'').toLowerCase(), level=1;
    if(ENCH_ROMAN[last]){ level=ENCH_ROMAN[last]; parts.pop(); }
    var nm=parts.join(' ').toLowerCase(), id;
    if(nm.indexOf('curse of ')===0) id=nm.slice(9).replace(/\s+/g,'_')+'_curse';   // Curse of Binding -> binding_curse
    else id=nm.replace(/\s+/g,'_');
    return { id:id, level:level };
  }
  function enchMap(disp){ var e=parseEnch(disp), o={}; o[e.id]=e.level; return o; }
  /* reverse maps for EDIT prefill: stored Trade (bare ids/enum) -> ABMTrade builder state */
  function tradeGetName(prof,give1,give2,outputItem){
    if(typeof TRADE_CATALOG==='undefined'||typeof ABMTrade==='undefined') return null;
    var oid='minecraft:'+outputItem;
    for(var i=0;i<TRADE_CATALOG.length;i++){ var e=TRADE_CATALOG[i]; var g2=e.g[1]?e.g[1][0]:'__none';
      if(e.p===prof && e.g[0][0]===give1 && g2===give2 && e.o[0]===oid) return ABMTrade.outName(e); }
    return null;
  }
  function enchDisplay(emap){
    if(!emap||typeof BOOK_ENCHANTS==='undefined') return 'Mending';
    var keys=Object.keys(emap); if(!keys.length) return 'Mending';
    var id=keys[0], lvl=emap[id];
    for(var i=0;i<BOOK_ENCHANTS.length;i++){ var p=parseEnch(BOOK_ENCHANTS[i]); if(p.id===id && p.level===lvl) return BOOK_ENCHANTS[i]; }
    return 'Mending';
  }

  /* container readers — a config Map serializes to a JSON object, a List to an array */
  function mapRows(c){ if(!c||typeof c!=='object'||Array.isArray(c)) return []; return Object.keys(c).map(function(k){ return [k, c[k]]; }); }
  function listRows(c){ return Array.isArray(c) ? c.map(function(v,i){ return [i, v]; }) : []; }
  function existingKeys(path){ return mapRows(getPath(LIVE.config, path)).map(function(e){ return e[0]; }); }

  /* form-input helpers (inputs are tagged data-le="<name>") */
  function $le(ov,name){ return ov.querySelector('[data-le="'+name+'"]'); }
  function leVal(ov,name){ var e=$le(ov,name); return e?String(e.value):''; }
  function leNum(ov,name,def){ var e=$le(ov,name); if(!e||String(e.value).trim()==='') return def; var n=parseInt(e.value,10); return isFinite(n)?n:def; }
  function leChecked(ov,name){ var e=$le(ov,name); return !!(e&&e.checked); }
  function leCoord(ov,prefix,label){
    function one(ax){ var e=$le(ov,prefix+ax); var v=e?String(e.value).trim():''; if(!/^-?\d+$/.test(v)) throw new Error('Enter whole numbers for '+label+' (x, y, z).'); return parseInt(v,10); }
    return { x:one('x'), y:one('y'), z:one('z') };
  }

  /* form-field HTML helpers */
  function leTextRow(label,name,ph,def,ro){ return '<div class="leRow"><label>'+esc(label)+'</label><input data-le="'+name+'" placeholder="'+esc(ph||'')+'" value="'+esc(def||'')+'"'+(ro?' readonly':'')+'></div>'; }
  function leNumRow(label,name,def){ return '<div class="leRow"><label>'+esc(label)+'</label><input data-le="'+name+'" class="leN" inputmode="numeric" value="'+esc(def)+'"></div>'; }
  function leToggleRow(label,name,on){ return '<div class="leRow"><label>'+esc(label)+'</label><input type="checkbox" data-le="'+name+'"'+(on?' checked':'')+'></div>'; }
  function leCoordRow(label,prefix,val){ val=val||{};
    function inp(ax){ var v=(val[ax]!=null&&val[ax]!=='')?(' value="'+esc(val[ax])+'"'):''; return '<input data-le="'+prefix+ax+'" placeholder="'+ax+'" inputmode="numeric"'+v+'>'; }
    return '<div class="leRow"><label>'+esc(label)+'</label><span class="leXYZ">'+inp('x')+inp('y')+inp('z')+
      '<button type="button" class="leLook" data-le-look="'+prefix+'" title="Use the block the bot is looking at">📍</button></span></div>'; }
  function leSub(t){ return '<div class="leFormSub">'+esc(t)+'</div>'; }
  function leHint(t){ return '<div class="leHint">'+esc(t)+'</div>'; }
  function leSelectRow(label,name,opts,sel){
    var o=opts.map(function(p){ var v=p[0],t=p[1]||p[0]; return '<option value="'+esc(v)+'"'+(v===sel?' selected':'')+'>'+esc(t)+'</option>'; }).join('');
    return '<div class="leRow"><label>'+esc(label)+'</label><select data-le="'+esc(name)+'">'+o+'</select></div>'; }
  /* coord reader that tolerates a fully-empty triple (returns 0,0,0) — for optional chests */
  function leCoordOpt(ov,prefix,label){
    var X=$le(ov,prefix+'x'),Y=$le(ov,prefix+'y'),Z=$le(ov,prefix+'z');
    var xs=X?String(X.value).trim():'',ys=Y?String(Y.value).trim():'',zs=Z?String(Z.value).trim():'';
    if(!xs&&!ys&&!zs) return {x:0,y:0,z:0};
    return leCoord(ov,prefix,label); }

  /* ---- forms (return HTML; collectors build the config entry + return the POST promise, or throw to show inline) ---- */
  var TRADER_PATH='client.extra.villagerTrader.trades',
      GROUPS_PATH='client.extra.villagerTrader.groups',
      ELYTRA_PATH='client.extra.elytraPilot.tripRoutes',
      PEARL_PATH ='client.extra.pearlLoader.pearls';
  var SELECTED_GROUP=null;   // left-panel selection, highlights member trades on the right
  /* group keys + an emerald-earner check shared by the trade form/collector */
  function groupKeys(){ return mapRows(getPath(LIVE.config, GROUPS_PATH)).map(function(e){ return e[0]; }); }
  function tradeIsEarner(){ var m=(typeof ABMTrade!=='undefined')?ABMTrade.match():null; return !!(m && m.o && mcStrip(m.o[0])==='emerald'); }

  function tradeFormHtml(val,key,mode){
    var ro = mode==='edit';
    if(val){   // EDIT: reverse-map the stored Trade back into the builder
      var prof=capWords(val.villagerProfession);
      var give1='minecraft:'+val.inputItem1;
      var give2=(val.inputItem2 && val.inputItem2!=='air')?('minecraft:'+val.inputItem2):'__none';
      var get=tradeGetName(prof,give1,give2,val.outputItem);
      try{ ABMTrade.load({prof:prof,give1:give1,give2:give2,get:get||'',ench:enchDisplay(val.outputItemEnchantments)}); }catch(e){}
    } else {   // ADD: reset the shared builder to a known-valid default
      try{ ABMTrade.load({prof:'Librarian',give1:'minecraft:emerald',give2:'minecraft:book',get:'Enchanted Book',ench:'Mending'}); }catch(e){}
    }
    var v=val||{};
    var gopts=[['__none','(no group — own resupply)']].concat(groupKeys().map(function(k){ return [k,k]; }));
    return leHint(ro?'Adjust the offer or chests, then Save.':'Pick a real villager offer, name it, then point it at the chests at your trade hall.')+
      ABMTrade.box()+
      leTextRow('Trade name','leKey','e.g. mending-books', key||'', ro)+
      leSelectRow('Group','leGroup', gopts, (v.group&&v.group!=='')?v.group:'__none')+
      '<div class="leHint" data-le-note="group" style="display:none"></div>'+
      leSub('Output  ·  always per-trade (each enchant to its own chest)')+
      leCoordRow('Output chest','co', v.outputChest)+
      leNumRow('Store output when above','st', v.outputItemStoreCountThreshold!=null?v.outputItemStoreCountThreshold:64)+
      leNumRow('Max give-1 per trade','mx1', v.maxInput1PerTrade!=null?v.maxInput1PerTrade:99)+
      leNumRow('Max give-2 per trade','mx2', v.maxInput2PerTrade!=null?v.maxInput2PerTrade:99)+
      '<div id="leSupply">'+
        leSub('Supply  ·  x y z  —  📍 uses the block the bot is looking at')+
        leCoordRow('Give-1 supply chest','c1', v.inputItem1Chest)+
        leCoordRow('Give-2 supply chest (if second input)','c2', v.inputItem2Chest)+
        leNumRow('Restock stacks (give-1)','rs1', v.inputItem1RestockStacks!=null?v.inputItem1RestockStacks:4)+
        leNumRow('Restock when below (give-1)','rt1', v.inputItem1RestockCountThreshold!=null?v.inputItem1RestockCountThreshold:64)+
        leNumRow('Restock stacks (give-2)','rs2', v.inputItem2RestockStacks!=null?v.inputItem2RestockStacks:4)+
        leNumRow('Restock when below (give-2)','rt2', v.inputItem2RestockCountThreshold!=null?v.inputItem2RestockCountThreshold:64)+
      '</div>';
  }
  /* show/hide the shared supply block: a grouped SPENDER inherits it; earners + ungrouped keep their own */
  function tradeWire(ov){
    function apply(){
      var sel=leVal(ov,'leGroup'), grouped=(sel&&sel!=='__none');
      var earner=tradeIsEarner();
      var sup=ov.querySelector('#leSupply'), note=ov.querySelector('[data-le-note="group"]');
      var inherit = grouped && !earner;
      if(sup) sup.style.display = inherit ? 'none' : '';
      if(note){
        if(grouped && earner){ note.style.display=''; note.textContent='Earner: keeps its OWN give-1 chest (the sell-item source); its emeralds fund group “'+sel+'”.'; }
        else if(inherit){ note.style.display=''; note.textContent='Supply (give chests + restock) inherited from group “'+sel+'”. Output stays per-trade above.'; }
        else { note.style.display='none'; note.textContent=''; }
      }
    }
    var g=$le(ov,'leGroup'); if(g) g.addEventListener('change', apply);
    // re-evaluate when the offer builder changes (earner status can flip)
    [].forEach.call(ov.querySelectorAll('select,[data-le]'), function(e){ e.addEventListener('change', apply); });
    apply();
  }
  function tradeCollect(ov,ctx){
    var m=(typeof ABMTrade!=='undefined')?ABMTrade.match():null;
    if(!m) throw new Error('Choose a valid villager trade first (the builder must show a green ✓).');
    var s=ABMTrade.state;
    var key = ctx.mode==='edit' ? ctx.key : leVal(ov,'leKey').trim();
    if(!key) throw new Error('Give the trade a name.');
    if(ctx.mode==='add' && existingKeys(TRADER_PATH).indexOf(key)>=0) throw new Error('A trade named “'+key+'” already exists.');
    var base = ctx.orig || {};                       // preserve fields the form doesn't expose (carry caps, etc.)
    var has2 = s.give2 && s.give2!=='__none';
    var grp = leVal(ov,'leGroup'); var grouped = grp && grp!=='__none';
    var earner = mcStrip(m.o[0])==='emerald';
    var own = !grouped || earner;                    // grouped SPENDER inherits give chests; else use the form's
    var co=leCoord(ov,'co','the output chest');
    var c1 = own ? leCoord(ov,'c1','the give-1 supply chest') : (base.inputItem1Chest||{x:0,y:0,z:0});
    var c2 = (own && has2) ? leCoord(ov,'c2','the give-2 supply chest') : (base.inputItem2Chest||{x:0,y:0,z:0});
    var value=Object.assign({}, base, {
      enabled: (base.enabled!=null?base.enabled:true),
      group: grouped ? grp : '',
      villagerProfession: s.prof.toUpperCase(),
      inputItem1: mcStrip(s.give1),
      inputItem2: has2 ? mcStrip(s.give2) : 'air',
      outputItem: mcStrip(m.o[0]),
      inputItem1Chest:c1, inputItem2Chest:c2, outputChest:co,
      inputItem1RestockStacks: leNum(ov,'rs1',4),
      inputItem1RestockCountThreshold: leNum(ov,'rt1',64),
      inputItem2RestockStacks: leNum(ov,'rs2',4),
      inputItem2RestockCountThreshold: leNum(ov,'rt2',64),
      outputItemStoreCountThreshold: leNum(ov,'st',64),
      maxInput1PerTrade: leNum(ov,'mx1',99),
      maxInput2PerTrade: leNum(ov,'mx2',99),
      outputItemEnchantments: (ABMTrade.isBook() ? enchMap(s.bookEnch) : {})
    });
    return leMutate('put', TRADER_PATH, { key:key, value:value });
  }

  function tripFormHtml(val,key,mode){ var ro=mode==='edit', v=val||{};
    return leHint('A direct trip: the bot flies open-nether to the destination. Overworld targets are projected to the nether automatically.')+
      leTextRow('Trip name','leKey','e.g. spawn-to-base', key||'', ro)+
      leToggleRow('Destination is in the Nether','leNether', !!v.endInNether)+
      leCoordRow('Destination','cd', (val?{x:v.destX,y:v.destY,z:v.destZ}:undefined));
  }
  function tripCollect(ov,ctx){
    var key = ctx.mode==='edit' ? ctx.key : leVal(ov,'leKey').trim();
    if(!key) throw new Error('Give the trip a name.');
    if(ctx.mode==='add' && existingKeys(ELYTRA_PATH).indexOf(key)>=0) throw new Error('A trip named “'+key+'” already exists.');
    var endNether=leChecked(ov,'leNether');
    var d=leCoord(ov,'cd','the destination');
    var legX = endNether ? d.x : Math.round(d.x/8);
    var legZ = endNether ? d.z : Math.round(d.z/8);
    var value={ id:key, endInNether:endNether, destX:d.x, destY:d.y, destZ:d.z,
      legs:[ { ride:false, x:legX, z:legZ, roadY:70 } ] };
    return leMutate('put', ELYTRA_PATH, { key:key, value:value });
  }

  function pearlFormHtml(val,key,mode){ var v=val||{};
    return leHint('The block the bot interacts with to release the stasis pearl. Tip: aim the bot at it and hit 📍.')+
      leTextRow('Pearl name','leKey','e.g. base', v.id||'', false)+
      leCoordRow('Interact block','cp', (val?{x:v.x,y:v.y,z:v.z}:undefined));
  }
  function pearlCollect(ov,ctx){
    var id=leVal(ov,'leKey').trim();
    if(!id) throw new Error('Give the pearl a name.');
    var c=leCoord(ov,'cp','the interact block');
    var value={ id:id, x:c.x, y:c.y, z:c.z };
    if(ctx.mode==='edit') return leMutate('put', PEARL_PATH, { index:ctx.index, value:value });
    return leMutate('add', PEARL_PATH, { value:value });
  }

  /* ---- trade GROUPS: a shared supply profile for a set of trades (give chests + restock + caps + self-refill) ---- */
  /* POST a single config mutation WITHOUT toast/refresh — for batching several writes behind one refresh. */
  function lePostRaw(op, path, body){
    var payload={ op:op, path:path }; for(var k in body) payload[k]=body[k];
    return fetch(api('/control/config'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
      .then(function(r){ if(r.status===403) throw new Error('control disabled, or you lack permission'); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(d){ if(d && d.ok===false) throw new Error(d.error||'change failed'); return d; });
  }
  /* checklist of every existing trade — checked = belongs to group `key`; lets you pull existing trades in/out in bulk */
  function grpMemberRows(key){
    var trades=mapRows(getPath(LIVE.config, TRADER_PATH));
    if(!trades.length) return '<div class="leEmpty">No trades yet — add trades first, then group them here.</div>';
    return trades.map(function(e){
      var tk=e[0], t=e[1]||{};
      var inThis = !!key && t.group===key;
      var has2=t.inputItem2&&t.inputItem2!=='air';
      var elsewhere = (t.group && t.group!==key) ? (' · in “'+t.group+'”') : '';
      var sub=capWords(t.villagerProfession)+' · '+pretty(mcStrip(t.inputItem1))+(has2?'+'+pretty(mcStrip(t.inputItem2)):'')+' → '+pretty(mcStrip(t.outputItem))+elsewhere;
      return '<label class="grpMem"><input type="checkbox" data-grpmember="'+esc(tk)+'"'+(inThis?' checked':'')+'>'+
        '<span class="grpMemBd"><span class="grpMemT">'+esc(tk)+'</span><span class="grpMemS">'+esc(sub)+'</span></span></label>';
    }).join('');
  }
  function groupFormHtml(val,key,mode){ var ro=mode==='edit', v=val||{};
    return leHint('A group shares ONE emerald (+book) supply across its member trades — configure the give chests + restock once here; every member keeps its own OUTPUT chest. Add an emerald-earning trade (sells items → emeralds) to the group and the bot self-refills before it runs dry; once supply is gone it parks instead of wandering.')+
      leTextRow('Group name','leKey','e.g. book-hall', key||'', ro)+
      leToggleRow('Enabled','gEnabled', v.enabled!==false)+
      leSub('Member trades  ·  check existing trades to pull them into this group (uncheck to remove)')+
      '<div class="grpMembers">'+grpMemberRows(key)+'</div>'+
      leSub('Shared supply chests  ·  x y z  —  📍 uses the block the bot is looking at')+
      leCoordRow('Give-1 (e.g. emerald) chest','gc1', v.inputItem1Chest)+
      leCoordRow('Give-2 (e.g. book) chest','gc2', v.inputItem2Chest)+
      leSub('Restock, carry caps & self-refill')+
      leNumRow('Restock stacks (give-1)','grs1', v.inputItem1RestockStacks!=null?v.inputItem1RestockStacks:4)+
      leNumRow('Restock when below (give-1)','grt1', v.inputItem1RestockCountThreshold!=null?v.inputItem1RestockCountThreshold:64)+
      leNumRow('Restock stacks (give-2)','grs2', v.inputItem2RestockStacks!=null?v.inputItem2RestockStacks:4)+
      leNumRow('Restock when below (give-2)','grt2', v.inputItem2RestockCountThreshold!=null?v.inputItem2RestockCountThreshold:64)+
      leNumRow('Carry cap give-1 stacks (0 = none)','gmc1', v.inputItem1MaxCarryStacks!=null?v.inputItem1MaxCarryStacks:0)+
      leNumRow('Carry cap give-2 stacks (0 = none)','gmc2', v.inputItem2MaxCarryStacks!=null?v.inputItem2MaxCarryStacks:2)+
      leNumRow('Min emeralds before self-refill (0 = passive)','gmin', v.minEmeralds!=null?v.minEmeralds:0)+
      leSub('Post-trade leftovers')+
      leSelectRow('After each trade','gpost', [['NONE','Keep (none)'],['TO_RESTOCK','Back to supply chests'],['TO_OVERFLOW','To overflow chest']], v.postTradeStoreMode||'NONE')+
      leCoordRow('Overflow chest (if To overflow)','gco', v.overflowChestPos);
  }
  function groupCollect(ov,ctx){
    var base = ctx.orig || {};
    var key = ctx.mode==='edit' ? ctx.key : leVal(ov,'leKey').trim();
    if(!key) throw new Error('Give the group a name.');
    if(ctx.mode==='add' && existingKeys(GROUPS_PATH).indexOf(key)>=0) throw new Error('A group named “'+key+'” already exists.');
    var post=leVal(ov,'gpost')||'NONE';
    var value=Object.assign({}, base, {
      enabled: leChecked(ov,'gEnabled'),
      inputItem1Chest: leCoord(ov,'gc1','the give-1 chest'),
      inputItem2Chest: leCoordOpt(ov,'gc2','the give-2 chest'),
      inputItem1RestockStacks: leNum(ov,'grs1',4),
      inputItem1RestockCountThreshold: leNum(ov,'grt1',64),
      inputItem2RestockStacks: leNum(ov,'grs2',4),
      inputItem2RestockCountThreshold: leNum(ov,'grt2',64),
      inputItem1MaxCarryStacks: leNum(ov,'gmc1',0),
      inputItem2MaxCarryStacks: leNum(ov,'gmc2',2),
      minEmeralds: leNum(ov,'gmin',0),
      postTradeStoreMode: post,
      overflowChestPos: (post==='TO_OVERFLOW') ? leCoord(ov,'gco','the overflow chest') : leCoordOpt(ov,'gco','the overflow chest')
    });
    // membership: diff the checklist against current trade.group, building one put per changed trade
    var checked={};
    [].forEach.call(ov.querySelectorAll('[data-grpmember]'), function(cb){ checked[cb.getAttribute('data-grpmember')]=cb.checked; });
    var tradePuts=[];
    mapRows(getPath(LIVE.config, TRADER_PATH)).forEach(function(e){
      var tk=e[0], t=e[1]||{}, want=!!checked[tk], isMember=(t.group===key);
      if(want && !isMember) tradePuts.push({ key:tk, value:Object.assign({}, t, {group:key}) });
      else if(!want && isMember) tradePuts.push({ key:tk, value:Object.assign({}, t, {group:''}) });
    });
    // write the group first, then each membership change, behind a single refresh
    var chain=lePostRaw('put', GROUPS_PATH, { key:key, value:value });
    tradePuts.forEach(function(tp){ chain=chain.then(function(){ return lePostRaw('put', TRADER_PATH, { key:tp.key, value:tp.value }); }); });
    return chain.then(function(){
      toast('saved group'+(tradePuts.length?(' · '+tradePuts.length+' trade'+(tradePuts.length===1?'':'s')+' updated'):''),'ok');
      return refreshAfterMutate();
    }).catch(function(err){ toast(err.message,'err',4200); throw err; });
  }
  var GROUP_SPEC={ title:'Trade Groups', addLabel:'New group', noun:'group', kind:'map', path:GROUPS_PATH, form:groupFormHtml, collect:groupCollect };

  var LIST_EDITORS = {
    trader: { title:'Trades', addLabel:'New trade', noun:'trade', kind:'map', path:TRADER_PATH,
      rowText:function(k,t){ var has2=t.inputItem2&&t.inputItem2!=='air';
        var sub=capWords(t.villagerProfession)+' · '+pretty(mcStrip(t.inputItem1))+(has2?' + '+pretty(mcStrip(t.inputItem2)):'')+' → '+pretty(mcStrip(t.outputItem));
        if(t.group) sub+='  ·  ⬡ '+t.group;
        return { title:k, sub:sub, group:(t.group||'') }; },
      form:tradeFormHtml, collect:tradeCollect, wire:tradeWire },
    elytra: { title:'Saved trips', addLabel:'New trip', noun:'trip', kind:'map', path:ELYTRA_PATH,
      rowText:function(k,r){ var n=(r.legs&&r.legs.length)||0;
        return { title:k, sub:(r.endInNether?'Nether':'Overworld')+' → '+r.destX+', '+r.destY+', '+r.destZ+' · '+n+' leg'+(n===1?'':'s') }; },
      form:tripFormHtml, collect:tripCollect },
    pearl: { title:'Pearl locations', addLabel:'Add pearl', noun:'pearl', kind:'list', path:PEARL_PATH,
      rowText:function(i,p){ return { title:(p.id||('#'+i)), sub:p.x+', '+p.y+', '+p.z }; },
      form:pearlFormHtml, collect:pearlCollect }
  };

  function leMutate(op, path, body){
    var payload={ op:op, path:path };
    for(var k in body) payload[k]=body[k];
    return fetch(api('/control/config'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
      .then(function(r){ if(r.status===403) throw new Error('control disabled, or you lack permission'); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(d){ if(d && d.ok===false) throw new Error(d.error||'change failed');
        toast(op==='remove'?'removed':'saved · '+(d&&d.size!=null?d.size+' total':'ok'), 'ok');
        return refreshAfterMutate();
      })
      .catch(function(e){ toast(e.message,'err',4200); throw e; });
  }
  function refreshAfterMutate(){ return fetchConfig().then(function(){ try{ render(); }catch(e){} }); }

  function closeLeModal(){ var m=document.getElementById('leModal'); if(m) m.remove(); }
  function leShowErr(ov,msg){ var e=ov.querySelector('.leErr'); if(e){ e.style.display='block'; e.textContent=msg; } }
  /* wire every 📍 button in a form to fetch the block the bot is looking at and fill that coord row */
  function wireLookButtons(ov){
    [].forEach.call(ov.querySelectorAll('.leLook'), function(b){
      b.onclick=function(){
        var pre=this.dataset.leLook, self=this; self.disabled=true; self.textContent='…';
        fetch(api('/control/lookingat'), {cache:'no-store'})
          .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
          .then(function(d){
            self.disabled=false; self.textContent='📍';
            if(d && d.hit){
              var X=$le(ov,pre+'x'), Y=$le(ov,pre+'y'), Z=$le(ov,pre+'z');
              if(X) X.value=d.x; if(Y) Y.value=d.y; if(Z) Z.value=d.z;
              toast('filled from the '+(d.block||'block')+' the bot is looking at','ok');
            } else if(d && d.offline){ toast('the bot isn’t in-game right now','err',3200); }
            else { toast('nothing in the bot’s crosshair — aim it at the block first','err',3600); }
          })
          .catch(function(e){ self.disabled=false; self.textContent='📍'; toast('look-up failed: '+e.message,'err',3600); });
      };
    });
  }

  function openForm(spec, mode, rowKey, val){
    closeLeModal();
    var ctx={ mode:mode, key: spec.kind==='map' ? rowKey : null, index: spec.kind==='list' ? rowKey : null, orig: val||null };
    var title = mode==='edit' ? ('Edit '+esc(spec.noun)) : ('＋ '+esc(spec.addLabel));
    var ov=document.createElement('div'); ov.id='leModal'; ov.className='leOv';
    ov.innerHTML='<div class="leCard"><div class="leTitle">'+title+'<span class="leClose">×</span></div>'+
      '<div class="leBody">'+spec.form(val, rowKey, mode)+'</div>'+
      '<div class="leErr" style="display:none"></div>'+
      '<div class="leFoot"><button class="leBtn leCancel">Cancel</button><button class="leBtn leSaveBtn">Save</button></div></div>';
    document.body.appendChild(ov);
    ov.querySelector('.leClose').onclick=closeLeModal;
    ov.querySelector('.leCancel').onclick=closeLeModal;
    ov.addEventListener('click', function(e){ if(e.target===ov) closeLeModal(); });
    wireLookButtons(ov);
    if(typeof spec.wire==='function'){ try{ spec.wire(ov, val, mode); }catch(e){} }
    ov.querySelector('.leSaveBtn').onclick=function(){
      var p; try{ p=spec.collect(ov, ctx); }catch(err){ leShowErr(ov, err.message); return; }
      var btn=this; btn.disabled=true; btn.textContent='Saving…';
      Promise.resolve(p).then(function(){ closeLeModal(); })
        .catch(function(err){ btn.disabled=false; btn.textContent='Save'; leShowErr(ov, (err&&err.message)||'failed'); });
    };
  }

  function buildListEditor(){
    var spec=LIST_EDITORS[cur]; if(!spec) return;
    var cont=$(LO.cfg); if(!cont) return;
    var stale=document.getElementById('leEditor'); if(stale) stale.remove();
    if(!LIVE.config) return;                              // config not loaded yet — leave the mockup preview alone
    var container=getPath(LIVE.config, spec.path);
    var entries = spec.kind==='map' ? mapRows(container) : listRows(container);
    var canEdit = moduleConfig(cur);
    var rows = entries.map(function(e){
      var rt=spec.rowText(e[0], e[1]);
      var acts = canEdit ? '<button class="leEdit" title="Edit" data-k="'+esc(String(e[0]))+'">✎</button>'+
                           '<button class="leDel" title="Delete" data-k="'+esc(String(e[0]))+'" data-lbl="'+esc(String(rt.title))+'">🗑</button>' : '';
      var gAttr = rt.group ? ' data-group="'+esc(rt.group)+'"' : '';
      return '<div class="leItem"'+gAttr+'><div class="leItemMain"><div class="leItemT">'+esc(rt.title)+'</div><div class="leItemS">'+esc(rt.sub)+'</div></div>'+acts+'</div>';
    }).join('');
    var panel=document.createElement('div'); panel.id='leEditor'; panel.className='lePanel';
    panel.innerHTML='<div class="leHead">'+esc(spec.title)+' <span class="leCount">'+entries.length+'</span>'+
      (canEdit?'<button class="leAdd">＋ '+esc(spec.addLabel)+'</button>':'')+'</div>'+
      '<div class="leList">'+(rows||'<div class="leEmpty">None yet — add one below.</div>')+'</div>';
    cont.insertBefore(panel, cont.firstChild);
    if(!canEdit) return;
    var addBtn=panel.querySelector('.leAdd'); if(addBtn) addBtn.onclick=function(){ openForm(spec,'add',null,null); };
    [].forEach.call(panel.querySelectorAll('.leEdit'), function(b){
      b.onclick=function(){
        var k=this.dataset.k, c=getPath(LIVE.config, spec.path), rowKey, val;
        if(spec.kind==='map'){ rowKey=k; val=c?c[k]:null; }
        else { rowKey=parseInt(k,10); val=Array.isArray(c)?c[rowKey]:null; }
        if(val==null){ toast('entry not found — refresh','err'); return; }
        openForm(spec,'edit', rowKey, val);
      };
    });
    [].forEach.call(panel.querySelectorAll('.leDel'), function(b){
      b.onclick=function(){
        var k=this.dataset.k, lbl=this.dataset.lbl;
        if(!window.confirm('Delete “'+lbl+'”?')) return;
        var body = spec.kind==='map' ? { key:k } : { index:parseInt(k,10) };
        leMutate('remove', spec.path, body);
      };
    });
  }

  /* highlight the trades that belong to the selected group, over in the right-hand list editor */
  function highlightGroupMembers(name){
    var cont=$(LO.cfg); if(!cont) return;
    [].forEach.call(cont.querySelectorAll('#leEditor .leItem'), function(it){
      it.classList.toggle('grpMember', !!name && it.getAttribute('data-group')===name);
    });
  }
  function selectGroup(name){
    SELECTED_GROUP = (SELECTED_GROUP===name) ? null : name;   // click again to clear
    var pane=document.getElementById('listPane');
    if(pane) [].forEach.call(pane.querySelectorAll('.grpItem'), function(it){
      it.classList.toggle('sel', it.getAttribute('data-gsel')===SELECTED_GROUP);
    });
    highlightGroupMembers(SELECTED_GROUP);
  }

  /* Repurpose the v1 left "Trades" panel (#listPane) into a live Trade Groups manager.
     The right column keeps the live trade list (#leEditor); selecting a group highlights its members there. */
  function buildGroupsPanel(){
    if(cur!=='trader') return;
    var pane=document.getElementById('listPane'); if(!pane) return;   // only the v1 (Mission Control) layout has it
    if(!LIVE.config) return;                                          // leave the mockup preview until config loads
    var groups=mapRows(getPath(LIVE.config, GROUPS_PATH));
    var trades=mapRows(getPath(LIVE.config, TRADER_PATH));
    var canEdit=moduleConfig('trader');
    function members(name){ var n=0; trades.forEach(function(e){ if(e[1] && e[1].group===name) n++; }); return n; }
    var rows=groups.map(function(e){
      var name=e[0], g=e[1]||{}, mc=members(name), off=(g.enabled===false);
      var sub=mc+' trade'+(mc===1?'':'s')+' · restock '+(g.inputItem1RestockStacks!=null?g.inputItem1RestockStacks:'?')+'/'+(g.inputItem2RestockStacks!=null?g.inputItem2RestockStacks:'?')+(g.minEmeralds?(' · refill <'+g.minEmeralds+'⬡'):'');
      var acts=canEdit?'<button class="leEdit" title="Edit" data-gk="'+esc(name)+'">✎</button><button class="leDel" title="Delete" data-gk="'+esc(name)+'">🗑</button>':'';
      return '<div class="leItem grpItem'+(name===SELECTED_GROUP?' sel':'')+(off?' grpOff':'')+'" data-gsel="'+esc(name)+'">'+
        '<div class="leItemMain"><div class="leItemT">'+esc(name)+(off?' (off)':'')+'</div><div class="leItemS">'+esc(sub)+'</div></div>'+acts+'</div>';
    }).join('');
    pane.innerHTML='<h3>Trade Groups <span class="sub">'+groups.length+'</span></h3>'+
      '<div class="list grpList">'+(rows||'<div class="leEmpty">No groups yet. Group the trades that share an emerald supply, then set that supply once.</div>')+'</div>'+
      (canEdit?'<div class="addrow grpAddRow">＋ New group</div>':'')+
      '<div class="grpFoot">A group shares its give chests + restock + carry caps across its members; each trade keeps its own output chest. Add an emerald-earning trade and the bot self-refills, then parks instead of wandering when supply is gone. Click a group to highlight its trades →</div>';
    if(canEdit){
      var add=pane.querySelector('.grpAddRow'); if(add){ add.style.cursor='pointer'; add.onclick=function(){ openForm(GROUP_SPEC,'add',null,null); }; }
      [].forEach.call(pane.querySelectorAll('.grpItem .leEdit'), function(b){ b.onclick=function(ev){ ev.stopPropagation();
        var k=this.dataset.gk, c=getPath(LIVE.config,GROUPS_PATH), val=c?c[k]:null;
        if(val==null){ toast('group not found — refresh','err'); return; } openForm(GROUP_SPEC,'edit',k,val); }; });
      [].forEach.call(pane.querySelectorAll('.grpItem .leDel'), function(b){ b.onclick=function(ev){ ev.stopPropagation();
        var k=this.dataset.gk; if(!window.confirm('Delete group “'+k+'”? Member trades fall back to their own resupply.')) return;
        leMutate('remove', GROUPS_PATH, { key:k }); }; });
    }
    [].forEach.call(pane.querySelectorAll('.grpItem'), function(it){ it.onclick=function(){ selectGroup(this.getAttribute('data-gsel')); }; });
    highlightGroupMembers(SELECTED_GROUP);
  }

  /* the mockup's dead "＋ New …" buttons (v1 left list panel) — open the same form */
  function wireAddRows(){
    var spec=LIST_EDITORS[cur]; if(!spec) return;
    [].forEach.call(document.querySelectorAll('.addrow'), function(el){
      if(el.dataset.lw) return; el.dataset.lw='1';
      el.style.cursor='pointer'; el.style.pointerEvents='auto';
      el.addEventListener('click', function(){
        if(moduleConfig(cur)) openForm(spec,'add',null,null);
        else toast('read-only — your access doesn’t include editing this module','err',3200);
      });
    });
  }

  function injectListEditorCss(){
    if($('#leCss')) return;
    var s=document.createElement('style'); s.id='leCss';
    s.textContent=
      '.lePanel{border:1px solid var(--acc-dim,#1f7a55);border-radius:12px;background:#3ddc9712;padding:.55rem .7rem .65rem;margin-bottom:.7rem}'+
      '.lePanel .leHead{display:flex;align-items:center;gap:.5rem;font-weight:700;font-size:.84rem;margin-bottom:.45rem}'+
      '.leCount{font-family:var(--mono,monospace);font-size:.6rem;color:var(--dim,#7b8a98);border:1px solid var(--line,#1d2730);border-radius:20px;padding:.04rem .42rem}'+
      '.leAdd{margin-left:auto;font-family:var(--sans,sans-serif);font-size:.74rem;font-weight:700;color:var(--acc,#3ddc97);background:var(--panel,#11171e);border:1px solid var(--acc-dim,#1f7a55);border-radius:8px;padding:.32rem .6rem;cursor:pointer}'+
      '.leAdd:hover{background:#3ddc9718}'+
      '.leList{display:flex;flex-direction:column;gap:.3rem}'+
      '.leItem{display:flex;align-items:center;gap:.6rem;background:#0b0f14;border:1px solid var(--line,#1d2730);border-radius:9px;padding:.4rem .55rem}'+
      '.leItemMain{flex:1;min-width:0}'+
      '.leItemT{font-size:.8rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'+
      '.leItemS{font-family:var(--mono,monospace);font-size:.64rem;color:var(--dim,#7b8a98);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'+
      '.leEdit,.leDel{flex:none;background:none;border:1px solid transparent;border-radius:7px;cursor:pointer;font-size:.85rem;padding:.2rem .35rem;opacity:.6}'+
      '.leEdit:hover{opacity:1;border-color:var(--acc-dim,#1f7a55);background:#3ddc9712}'+
      '.leDel:hover{opacity:1;border-color:#5a1f1f;background:#ff545412}'+
      '.leLook{flex:none;background:#0b0f14;border:1px solid var(--line,#1d2730);border-radius:7px;cursor:pointer;font-size:.8rem;padding:.2rem .34rem;line-height:1}'+
      '.leLook:hover{border-color:var(--acc,#3ddc97)}.leLook:disabled{opacity:.5;cursor:default}'+
      '.leEmpty{font-family:var(--mono,monospace);font-size:.66rem;color:var(--dim,#7b8a98);padding:.3rem .1rem}'+
      /* modal */
      '.leOv{position:fixed;inset:0;z-index:10000;background:rgba(4,7,10,.66);backdrop-filter:blur(3px);display:flex;align-items:flex-start;justify-content:center;padding:5vh 1rem;overflow:auto}'+
      '.leCard{width:min(720px,96vw);background:var(--panel,#11171e);border:1px solid var(--line,#1d2730);border-radius:14px;box-shadow:0 24px 64px #000a;overflow:hidden}'+
      '.leTitle{display:flex;align-items:center;font-weight:700;font-size:.92rem;padding:.7rem .85rem;border-bottom:1px solid var(--line,#1d2730)}'+
      '.leClose{margin-left:auto;cursor:pointer;font-size:1.1rem;color:var(--dim,#7b8a98);line-height:1}.leClose:hover{color:#fff}'+
      '.leBody{padding:.7rem .85rem;display:flex;flex-direction:column;gap:.4rem;max-height:64vh;overflow:auto}'+
      '.leHint{font-size:.72rem;color:var(--dim,#7b8a98);line-height:1.45;margin-bottom:.2rem}'+
      '.leFormSub{font-family:var(--mono,monospace);font-size:.58rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim,#7b8a98);margin:.45rem 0 .05rem;border-top:1px solid #ffffff10;padding-top:.4rem}'+
      '.leRow{display:flex;align-items:center;gap:.7rem;min-height:30px}'+
      '.leRow label{flex:1;font-size:.78rem}'+
      '.leRow input{font-family:var(--mono,monospace);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:7px;padding:.32rem .45rem}'+
      '.leRow input:not(.leN):not([type=checkbox]){width:160px}'+
      '.leRow input.leN{width:90px;text-align:right}'+
      '.leRow input[type=checkbox]{width:auto}'+
      '.leXYZ{display:flex;gap:.3rem}.leXYZ input{width:58px;text-align:right}'+
      '.leErr{color:#ff7676;font-size:.72rem;padding:0 .85rem;min-height:0}'+
      '.leErr:not([style*="none"]){padding:.2rem .85rem .1rem}'+
      '.leFoot{display:flex;justify-content:flex-end;gap:.5rem;padding:.65rem .85rem;border-top:1px solid var(--line,#1d2730)}'+
      '.leBtn{font-family:var(--sans,sans-serif);font-size:.78rem;border-radius:8px;padding:.4rem .8rem;cursor:pointer;border:1px solid var(--line,#1d2730);background:#0b0f14;color:var(--dim,#cdd9e2)}'+
      '.leSaveBtn{font-weight:700;color:var(--acc,#3ddc97);border-color:var(--acc-dim,#1f7a55)}.leSaveBtn:hover{background:#3ddc9718}.leBtn:disabled{opacity:.6;cursor:default}'+
      /* trade-groups left panel + member highlight */
      '.grpList{display:flex;flex-direction:column;gap:.3rem}'+
      '.grpItem{cursor:pointer;transition:.12s}'+
      '.grpItem:hover{border-color:var(--acc-dim,#1f7a55)}'+
      '.grpItem.sel{border-color:var(--acc,#3ddc97);background:#3ddc9722}'+
      '.grpItem.grpOff{opacity:.55}'+
      '.leItem.grpMember{border-color:var(--acc,#3ddc97);box-shadow:inset 3px 0 0 var(--acc,#3ddc97)}'+
      '.grpAddRow{margin-top:.5rem;cursor:pointer}'+
      '.grpFoot{font-family:var(--mono,monospace);font-size:.58rem;color:var(--dim,#7b8a98);margin-top:.55rem;line-height:1.45}'+
      '.grpMembers{display:flex;flex-direction:column;gap:.25rem;max-height:200px;overflow:auto;border:1px solid var(--line,#1d2730);border-radius:9px;padding:.35rem}'+
      '.grpMem{display:flex;align-items:center;gap:.55rem;padding:.28rem .35rem;border-radius:7px;cursor:pointer}'+
      '.grpMem:hover{background:#3ddc970d}'+
      '.grpMem input{flex:none;width:15px;height:15px;cursor:pointer}'+
      '.grpMemBd{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.2}'+
      '.grpMemT{font-size:.78rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'+
      '.grpMemS{font-family:var(--mono,monospace);font-size:.62rem;color:var(--dim,#7b8a98);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'+
      '.leSelectRowSel,.leRow select{font-family:var(--mono,monospace);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:7px;padding:.32rem .45rem}';
    document.head.appendChild(s);
  }

  /* ---------------- render hook ---------------- */
  function afterRender(){
    filterNav();
    injectEnableToggle();
    wireActions();
    lockConfig();
    buildLiveConfig();
    buildListEditor();
    buildGroupsPanel();
    wireAddRows();
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
    injectLiveConfigCss();
    injectListEditorCss();
    // learn our capability (shared-access guests) before first render so gating applies immediately
    fetchPrincipal().then(function(){
      injectTopbar();
      // default the surface to the Live Map (the headline) on first load
      if(typeof cur!=='undefined' && MAP['livemap']){ try{ cur='livemap'; render(); }catch(e){} }
      else { try{ render(); }catch(e){} }
    });
    pollState(); setInterval(pollState, 3000);
    // pull the live config once (then re-render so the settings panel populates), refresh occasionally
    fetchConfig().then(function(){ try{ render(); }catch(e){} });
    setInterval(function(){ fetchConfig(); }, 15000);
    startStream();
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
