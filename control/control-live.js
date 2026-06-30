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
    /* pass the bot we're viewing so the server reports this user's role/perms ON THIS BOT (per-bot roles) */
    return fetch('/api/authstatus'+(INST?('?inst='+encodeURIComponent(INST)):''), {cache:'no-store'})
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
    LIVE.modStatus = LIVE.modStatus || {};
    (d.modules||[]).forEach(function(ms){
      var id=resolveId(ms.name); if(!id) return;
      var m=MAP[id]; if(!m) return;
      LIVE.modState[id] = !!ms.enabled;
      if(ms.status) LIVE.modStatus[id] = ms.status;   // richer per-module status (e.g. KitMaker estimate/errors)
      m.enabled = !!ms.enabled;
      m.status  = ms.enabled ? 'run' : 'idle';
      m.sdot    = ms.enabled ? 'run' : '';
    });
    refreshNavDots();
    refreshHeaderStatus();
    try{ refreshKitStatus(); }catch(e){}   // live-update the Kit Maker status panel between full renders
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
    /* the VillagerTrader console command is "trader", NOT the raw class name "villagertrader" —
       moduleCmd() would emit the wrong command, so the module toggle + Run/Stop never worked. */
    trader:  { start: function(){ return runCommand('trader on'); }, stop: function(){ return runCommand('trader off'); } },
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

  /* config maps that have their OWN dedicated list editor (Trades/Groups/Trips/Pearls): don't dump every
     entry's fields into the live-config tree — that's what made the trader config absurdly long with 12 trades.
     Summarize as "N trades · edit in the list above" instead. */
  var LIST_CONTAINER_PATHS = {
    'client.extra.villagerTrader.trades':'trade',
    'client.extra.villagerTrader.groups':'group',
    'client.extra.kitMaker.template':'kit slot',
    'client.extra.kitMaker.kits':'saved kit',
    'client.extra.kitMaker.captured':'detected slot',
    'client.extra.elytraPilot.tripRoutes':'trip',
    'client.extra.pearlLoader.pearls':'pearl'
  };

  /* collapsible panels — state persisted per panel key so a re-render keeps your choice */
  var COLLAPSE=(function(){ try{ return JSON.parse(localStorage.getItem('abmCollapse')||'{}'); }catch(e){ return {}; } })();
  function saveCollapse(){ try{ localStorage.setItem('abmCollapse', JSON.stringify(COLLAPSE)); }catch(e){} }
  function makeCollapsible(panel, head, key, def){
    if(!panel || !head) return;
    head.classList.add('cl-head');
    var car=document.createElement('span'); car.className='cl-caret';
    head.insertBefore(car, head.firstChild);
    var col = (key in COLLAPSE) ? COLLAPSE[key] : def;
    function apply(){ panel.classList.toggle('cl-collapsed', col); car.textContent = col?'▸':'▾'; }
    function toggle(){ col=!col; COLLAPSE[key]=col; saveCollapse(); apply(); }
    car.addEventListener('click', function(e){ e.stopPropagation(); toggle(); });
    head.addEventListener('click', function(e){ if(e.target.closest('button,input,select,a,.lcTgl,.tgl')) return; toggle(); });
    head.style.cursor='pointer'; apply();
  }

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
      else if(LIST_CONTAINER_PATHS[path]){ var noun=LIST_CONTAINER_PATHS[path], cn=Array.isArray(v)?v.length:Object.keys(v).length;
        html+='<div class="lcRow"><span class="lcLbl">'+esc(prettyKey(k))+'</span><span class="lcCtl lcRO">'+cn+' '+noun+(cn===1?'':'s')+' · edit in the list above</span></div>'; }
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
    makeCollapsible(panel, panel.querySelector('.lcHead'), 'lc:'+cur, false);
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

  /* ---- live list/set pickers (chip + dropdown) wired to the bot's real config ----
     The Action Limiter illegal-items, Auto Eat foods, Auto Miner ore targets, and Auto Drop
     list are populated mockup pickers; lockConfig() freezes them as a read-only overview.
     Here we bind each to its REAL config field: show the bot's live list and persist every
     add/remove through the config op API. Needs AquariusProxy 5.12.2+ (string set add/remove +
     list remove-by-value); on older bots / unmapped fields the picker stays read-only. */
  var PICKER_PATHS = {
    ores:     'client.extra.aquariusMiner.oreTargets',     // List<String>
    dropjunk: 'client.extra.autoDrop.items',               // List<String>
    foods:    'client.extra.autoEat.foods',                // Set<String>
    illegals: 'client.extra.actionLimiter.itemsBlacklist'  // Set<String>
  };
  function wireLivePickers(){
    if(!LIVE.config || typeof ABMPickers==='undefined') return;
    var canCfg=moduleConfig(cur);
    Object.keys(PICKER_PATHS).forEach(function(name){
      var host=document.getElementById('pick_'+name); if(!host) return;   // this picker isn't on the current page
      var pk=ABMPickers[name]; if(!pk) return;
      var arr=getPath(LIVE.config, PICKER_PATHS[name]);
      if(!Array.isArray(arr)) return;                                      // bot doesn't expose this field — leave read-only
      pk.sel=arr.slice();                                                  // show the bot's REAL list
      pk.onChange = canCfg ? (function(p){ return function(op,id){ leMutate(op, p, { value:id }).catch(function(){}); }; })(PICKER_PATHS[name]) : null;
      pk._render();                                                        // redraw: live data + a fresh, enabled dropdown
      if(!canCfg){                                                         // no config grant: read-only, but still show live data
        var s=host.querySelector('select'); if(s){ s.setAttribute('disabled','disabled'); s.style.opacity='.65'; }
        [].forEach.call(host.querySelectorAll('.ilchip .x'), function(x){ x.style.pointerEvents='none'; x.style.opacity='.65'; });
      }
    });
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
    return leHint('A group buckets EXISTING trades under one shared emerald/book supply. Just name it and tick the trades below — you do NOT need to create a trade. The shared supply is optional here: open “Shared supply” to set it now, or leave it and set it later (before you run the group).')+
      leTextRow('Group name','leKey','e.g. book-hall', key||'', ro)+
      leToggleRow('Enabled','gEnabled', v.enabled!==false)+
      leSub('Member trades  ·  tick existing trades to include (uncheck to remove)')+
      '<div class="grpMembers">'+grpMemberRows(key)+'</div>'+
      '<details class="grpSupply"'+(mode==='edit'?' open':'')+'>'+
        '<summary>Shared supply &amp; restock  ·  optional — set now or later</summary>'+
        leSub('Supply chests  ·  x y z  —  📍 uses the block the bot is looking at')+
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
        leCoordRow('Overflow chest (if To overflow)','gco', v.overflowChestPos)+
      '</details>';
  }
  function groupCollect(ov,ctx){
    var base = ctx.orig || {};
    var key = ctx.mode==='edit' ? ctx.key : leVal(ov,'leKey').trim();
    if(!key) throw new Error('Give the group a name.');
    if(ctx.mode==='add' && existingKeys(GROUPS_PATH).indexOf(key)>=0) throw new Error('A group named “'+key+'” already exists.');
    var post=leVal(ov,'gpost')||'NONE';
    var value=Object.assign({}, base, {
      enabled: leChecked(ov,'gEnabled'),
      inputItem1Chest: leCoordOpt(ov,'gc1','the give-1 chest'),   // optional — a group can be created just to bucket trades; set supply later
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
        return { title:k, sub:r.destX+', '+r.destY+', '+r.destZ+' · '+n+' leg'+(n===1?'':'s') }; },
      leftPill:function(r){ return r.endInNether ? {text:'Nether',cls:'ne'} : {text:'Overworld',cls:'ov'}; },
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
    makeCollapsible(panel, panel.querySelector('.leHead'), 'le:'+cur, false);
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

  function isZeroPos(p){ return !p || (!p.x && !p.y && !p.z); }

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
      var name=e[0], g=e[1]||{}, mc=members(name), on=(g.enabled!==false);
      var supply=isZeroPos(g.inputItem1Chest)?'no shared chest (members keep own)':('restock '+(g.inputItem1RestockStacks!=null?g.inputItem1RestockStacks:'?')+'/'+(g.inputItem2RestockStacks!=null?g.inputItem2RestockStacks:'?'));
      var sub=mc+' trade'+(mc===1?'':'s')+' · '+supply+(g.minEmeralds?(' · refill <'+g.minEmeralds+'⬡'):'');
      var tog=canEdit?'<span class="grpTgl lcTgl'+(on?' on':'')+'" data-gtog="'+esc(name)+'" title="turn group '+(on?'off':'on')+'"></span>':'';
      var acts=canEdit?'<button class="leEdit" title="Edit / assign trades" data-gk="'+esc(name)+'">✎</button><button class="leDel" title="Delete" data-gk="'+esc(name)+'">🗑</button>':'';
      return '<div class="leItem grpItem'+(name===SELECTED_GROUP?' sel':'')+(on?'':' grpOff')+'" data-gsel="'+esc(name)+'">'+
        tog+'<div class="leItemMain"><div class="leItemT">'+esc(name)+'</div><div class="leItemS">'+esc(sub)+'</div></div>'+acts+'</div>';
    }).join('');
    pane.innerHTML='<h3>Trade Groups <span class="sub">'+groups.length+'</span></h3>'+
      '<div class="list grpList">'+(rows||'<div class="leEmpty">No groups yet. Group the trades that share an emerald supply, then set that supply once.</div>')+'</div>'+
      (canEdit?'<div class="addrow grpAddRow">＋ New group</div>':'')+
      '<div class="grpFoot">A group shares its give chests + restock + carry caps across its members; each trade keeps its own output chest. Add an emerald-earning trade and the bot self-refills, then parks instead of wandering when supply is gone. Click a group to highlight its trades →</div>';
    makeCollapsible(pane, pane.querySelector('h3'), 'grp:'+cur, false);
    if(canEdit){
      var add=pane.querySelector('.grpAddRow'); if(add){ add.style.cursor='pointer'; add.onclick=function(){ openForm(GROUP_SPEC,'add',null,null); }; }
      [].forEach.call(pane.querySelectorAll('.grpItem .grpTgl'), function(t){ t.onclick=function(ev){ ev.stopPropagation();
        var name=this.dataset.gtog, c=getPath(LIVE.config,GROUPS_PATH), g=c?c[name]:null; if(!g) return;
        var nv=Object.assign({}, g, { enabled: (g.enabled===false) });   // flip
        this.classList.toggle('on', nv.enabled);
        lePostRaw('put', GROUPS_PATH, { key:name, value:nv })
          .then(function(){ toast('group “'+name+'” '+(nv.enabled?'on':'off'),'ok'); return refreshAfterMutate(); })
          .catch(function(e){ toast(e.message,'err',4000); }); }; });
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

  /* Make the v1 left "#listPane" a LIVE, working list for a single-list module (elytra trips, pearls),
     rendered in the mockup's nice ".it" row style. Reuses the module's LIST_EDITORS spec (rowText/form/collect),
     so clicking a row edits it, the add row creates one, and 🗑 deletes — all against the bot's live config.
     (The trader uses buildGroupsPanel here instead; its trades editor stays in the right column.) */
  function buildLeftLiveList(spec){
    var pane=document.getElementById('listPane'); if(!pane) return;   // v1 only
    if(!LIVE.config) return;                                          // leave the mockup until config loads
    var container=getPath(LIVE.config, spec.path);
    var entries = spec.kind==='map' ? mapRows(container) : listRows(container);
    var canEdit=moduleConfig(cur);
    var ico=(MAP[cur]&&MAP[cur].icon)||'•';
    var rows=entries.map(function(e){
      var rt=spec.rowText(e[0], e[1]);
      var pill=spec.leftPill ? spec.leftPill(e[1]) : null;
      var pillH = pill ? '<span class="pill '+esc(pill.cls||'')+'">'+esc(pill.text)+'</span>' : '';
      var del = canEdit ? '<span class="x" data-k="'+esc(String(e[0]))+'" title="Delete">🗑</span>' : '';
      return '<div class="it" data-k="'+esc(String(e[0]))+'"><span class="it-ic">'+ico+'</span>'+
        '<div class="it-bd"><div class="n">'+esc(rt.title)+'</div><div class="c">'+esc(rt.sub)+'</div></div>'+pillH+del+'</div>';
    }).join('');
    pane.innerHTML='<h3>'+esc(spec.title)+'<span class="sub">'+entries.length+'</span></h3>'+
      '<div class="list">'+(rows||'<div class="leEmpty">None yet — add one below.</div>')+'</div>'+
      (canEdit?'<div class="addrow llAddRow">＋ '+esc(spec.addLabel)+'</div>':'');
    makeCollapsible(pane, pane.querySelector('h3'), 'left:'+cur, false);
    if(!canEdit) return;
    var add=pane.querySelector('.llAddRow'); if(add){ add.style.cursor='pointer'; add.onclick=function(){ openForm(spec,'add',null,null); }; }
    [].forEach.call(pane.querySelectorAll('.it .x'), function(b){ b.onclick=function(ev){ ev.stopPropagation();
      var k=this.dataset.k; if(!window.confirm('Delete “'+k+'”?')) return;
      leMutate('remove', spec.path, spec.kind==='map' ? { key:k } : { index:parseInt(k,10) }); }; });
    [].forEach.call(pane.querySelectorAll('.list .it'), function(it){ it.onclick=function(){
      var k=this.dataset.k, c=getPath(LIVE.config, spec.path), rowKey, val;
      if(spec.kind==='map'){ rowKey=k; val=c?c[k]:null; } else { rowKey=parseInt(k,10); val=Array.isArray(c)?c[rowKey]:null; }
      if(val==null){ toast('entry not found — refresh','err'); return; }
      openForm(spec,'edit',rowKey,val); }; });
  }

  /* ===================== Kit Maker — live builder + item picker + status =====================
     v1 (Mission Control): the LEFT col's static kit-grid panel becomes a live, editable 27-slot grid wired to
     client.extra.kitMaker.template; the RIGHT col gains a dynamic Setup card cluster + a live Status panel
     (driven by the bot's controlState `status`, incl. the green "≈ N kits buildable" estimate). Clicking a slot
     opens an item picker (all 1.21.4 items, categorized + searchable, stack-aware count, and a per-slot match
     selector whose "By enchantments" mode is the independent silk-vs-fortune rule). */
  var KIT_PATH='client.extra.kitMaker.template';          // legacy single template (fallback)
  var KITS_PATH='client.extra.kitMaker.kits';             // named kit library: name -> {slots:{i:slot}}
  var ACTIVE_PATH='client.extra.kitMaker.activeKit';      // which kit the builder edits + the module builds
  var CAPTURED_PATH='client.extra.kitMaker.captured';     // auto-detected kit written by the bot on a physical run
  var AUTO_KIT='__auto';                                  // sentinel: build a physically auto-detected kit
  function kmCfg(){ return getPath(LIVE.config,'client.extra.kitMaker')||{}; }
  function asMap(v){ return (v && typeof v==='object' && !Array.isArray(v)) ? v : {}; }
  function kitsMap(){ return asMap(getPath(LIVE.config, KITS_PATH)); }
  function capturedMap(){ return asMap(getPath(LIVE.config, CAPTURED_PATH)); }
  function kitTemplate(){ return asMap(getPath(LIVE.config, KIT_PATH)); }   // legacy
  /* the kit name the builder currently shows/edits: a saved kit, AUTO_KIT, or '' = legacy template mode */
  function kmActiveName(){
    var cfg=kmCfg(), kits=kitsMap(), keys=Object.keys(kits), ak=cfg.activeKit||'';
    if(ak===AUTO_KIT) return AUTO_KIT;
    if(ak && kits[ak]) return ak;
    if(keys.length) return keys[0];        // display the first saved kit when nothing valid is selected
    return '';                             // no library yet → legacy single-template mode
  }
  function kmActiveSlots(name){
    if(name===AUTO_KIT) return capturedMap();
    var kits=kitsMap(); if(name && kits[name]) return asMap(kits[name].slots);
    return kitTemplate();
  }
  function kmEditable(name){ return name!==AUTO_KIT && moduleConfig('kitmaker'); }
  function kitStatus(){ return (LIVE.modStatus && LIVE.modStatus.kitmaker) || null; }
  var KM_MATCHES=[['Auto','Auto (global match mode)'],['ByType','By type only'],['ByEnchants','By enchantments'],['Exact','Exact components']];

  function kitIcon(id,size){ id=mcStrip(id);
    return '<img class="kmImg" data-id="'+esc(id)+'" alt="" loading="lazy" src="https://mc.nerothe.com/img/1.21/minecraft_'+esc(id)+'.png" style="width:'+size+'px;height:'+size+'px;image-rendering:pixelated">'; }
  function wireKitIcons(root){ [].forEach.call((root||document).querySelectorAll('img.kmImg'), function(im){
    if(im.dataset.kw) return; im.dataset.kw='1';
    im.onerror=function(){ this.onerror=null; var s=document.createElement('span'); s.className='kmFb'; s.textContent=(this.dataset.id||'?').slice(0,2).toUpperCase(); if(this.parentNode) this.replaceWith(s); };
    if(im.complete && im.naturalWidth===0) im.onerror();
  }); }
  function kmPretty(id){ return (typeof ABMItems!=='undefined') ? ABMItems.pretty(id) : capWords(id); }
  function kmStack(id){ return (typeof ABMItems!=='undefined') ? ABMItems.stackOf(id) : 64; }

  function buildKitMaker(){
    if(cur!=='kitmaker' || !LIVE.config) return;
    if(typeof ABMItems==='undefined'){ return; }                 // catalog not loaded — leave the mockup alone
    var canEdit=moduleConfig('kitmaker');
    var gridEl=document.querySelector('.kitgrid');
    var panel=gridEl?gridEl.closest('.panel'):null;
    if(panel) renderKitGrid(panel, canEdit);
    var cont=$(LO.cfg);
    if(cont){ renderKitSetup(cont); renderKitStatusPanel(cont); }
  }

  function renderKitGrid(panel, canEditModule){
    var name=kmActiveName(), slots=kmActiveSlots(name), editable=kmEditable(name) && canEditModule;
    var defined=Object.keys(slots).length, cells='';
    for(var i=0;i<27;i++){ var s=slots[String(i)]; var has=!!(s&&s.item);
      var inner = has ? (kitIcon(s.item,30)
        + ((s.count>1)?'<span class="kmCt">'+s.count+'</span>':'')
        + ((s.match==='ByEnchants'&&s.enchants&&s.enchants.length)?'<span class="kmEn" title="'+esc(s.enchants.map(kmPretty).join(', '))+'">✦</span>':'')) : '';
      var tip = 'Slot '+(i+1)+(has?(' · '+kmPretty(s.item)+' ×'+s.count):' · empty');
      cells+='<div class="kmSlot'+(has?' filled':'')+(editable?'':' kmRO')+'" data-i="'+i+'" title="'+esc(tip)+'">'+inner+'</div>';
    }
    panel.innerHTML='<h3>Kit Builder <span class="sub">'+defined+' / 27 slots</span></h3>'+
      kmKitBarHtml(name, canEditModule)+
      '<div class="kitgrid kmGrid">'+cells+'</div>'+
      '<div class="kmGridHint">'+kmGridHint(name, editable)+'</div>';
    makeCollapsible(panel, panel.querySelector('h3'), 'km:grid', false);
    wireKitIcons(panel);
    wireKitBar(panel, name, canEditModule);
    if(editable){
      [].forEach.call(panel.querySelectorAll('.kmSlot'), function(sl){ sl.onclick=function(ev){ openKitPicker(+this.dataset.i, ev); }; });
    } else if(name===AUTO_KIT){
      [].forEach.call(panel.querySelectorAll('.kmSlot'), function(sl){ sl.onclick=function(){ kmImportDetected(); }; });
    }
  }

  /* ---- kit library bar (selector + new/dup/rename/delete/import/clear) ---- */
  function kmKitBarHtml(name, canEdit){
    var kits=kitsMap(), keys=Object.keys(kits), legacy=(keys.length===0 && Object.keys(kitTemplate()).length>0);
    var opts='';
    keys.forEach(function(k){ opts+='<option value="'+esc(k)+'"'+(k===name?' selected':'')+'>'+esc(k)+'</option>'; });
    if(legacy) opts+='<option value=""'+(name===''?' selected':'')+'>(unsaved template)</option>';
    if(!keys.length && !legacy) opts+='<option value="" selected>(no kits yet — ＋ to add)</option>';
    var nCap=Object.keys(capturedMap()).length;
    opts+='<option value="'+AUTO_KIT+'"'+(name===AUTO_KIT?' selected':'')+'>🛰 Auto-detect (physical)'+(nCap?(' · '+nCap+' captured'):'')+'</option>';
    var btns = canEdit ? (
      '<button class="kmKB" data-kb="new" title="New empty kit">＋</button>'+
      '<button class="kmKB" data-kb="dup" title="Duplicate / save current as a new kit">⧉</button>'+
      '<button class="kmKB" data-kb="ren" title="Rename this kit">✎</button>'+
      '<button class="kmKB kmKBdel" data-kb="del" title="Delete this kit">🗑</button>'+
      '<button class="kmKB kmKBimp" data-kb="imp" title="Import the bot’s auto-detected kit">⤓ import</button>'+
      '<button class="kmKB kmKBdel" data-kb="clr" title="Clear all slots in this kit">clear</button>'
    ) : '';
    return '<div class="kmKitBar"><span class="kmKBl">Kit</span><select class="kmKitSel">'+opts+'</select>'+btns+'</div>'+
      '<div class="kmKitNote">'+kmKitNote(name)+'</div>';
  }
  function kmKitNote(name){
    if(name===AUTO_KIT) return 'The bot builds a physically auto-detected example shulker (no saved design). After a run it captures what it found — ⤓ import to edit &amp; save it.';
    if(name==='') return 'Unsaved single template (legacy). Use ⧉ to save it as a named kit.';
    return 'The bot builds this kit. Slot edits save instantly (overwrite).';
  }
  function kmGridHint(name, editable){
    if(name===AUTO_KIT) return 'Auto-detect preview (read-only). Click any slot or ⤓ import to copy it into an editable kit.';
    return editable ? 'Click a slot to set its item, count &amp; matching. Same item can fill several slots (e.g. 4×16 rockets).'
                     : 'Read-only — your access doesn’t include editing this module.';
  }
  function wireKitBar(panel, name, canEdit){
    var sel=panel.querySelector('.kmKitSel'); if(sel) sel.onchange=function(){ kmSetActive(this.value); };
    if(!canEdit) return;
    [].forEach.call(panel.querySelectorAll('.kmKB'), function(b){ b.onclick=function(ev){ ev.stopPropagation();
      var a=this.dataset.kb;
      if(a==='new') kmNewKit();
      else if(a==='dup') kmDuplicateKit(name);
      else if(a==='ren') kmRenameKit(name);
      else if(a==='del') kmDeleteKit(name);
      else if(a==='imp') kmImportDetected();
      else if(a==='clr'){ if(window.confirm('Clear all slots in this kit?')) kmClearActive(); }
    }; });
  }

  /* ---- kit-library mutations ---- */
  function kmValidName(n){ n=(n||'').trim(); return (n && n!==AUTO_KIT && /^[\w. -]{1,40}$/.test(n)) ? n : null; }
  function kmSetActive(name){
    return fetch(api('/control/config'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:ACTIVE_PATH,value:name})})
      .then(function(r){ if(r.status===403) throw new Error('control disabled or no permission'); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(d){ if(d&&d.ok===false) throw new Error(d.error||'failed'); toast('building: '+(name===AUTO_KIT?'auto-detect':(name||'unsaved template')),'ok'); return refreshAfterMutate(); })
      .catch(function(e){ toast(e.message,'err',4000); });
  }
  function kmNewKit(){
    var n=kmValidName(window.prompt('New kit name:','')); if(!n) return;
    if(kitsMap()[n]){ toast('a kit named “'+n+'” already exists','err',3500); return; }
    lePostRaw('put', KITS_PATH, {key:n, value:{slots:{}}}).then(function(){ return kmSetActive(n); }).catch(function(e){ toast(e.message,'err',4000); });
  }
  function kmDuplicateKit(name){
    var src=kmActiveSlots(name), base=(name&&name!==AUTO_KIT)?name:'kit';
    var n=kmValidName(window.prompt('Save current slots as a new kit named:', base+'-copy')); if(!n) return;
    if(kitsMap()[n]){ toast('a kit named “'+n+'” already exists','err',3500); return; }
    lePostRaw('put', KITS_PATH, {key:n, value:{slots:JSON.parse(JSON.stringify(src))}}).then(function(){ return kmSetActive(n); }).catch(function(e){ toast(e.message,'err',4000); });
  }
  function kmRenameKit(name){
    if(!name || name===AUTO_KIT){ toast('select a saved kit to rename','err',3000); return; }
    var n=kmValidName(window.prompt('Rename “'+name+'” to:', name)); if(!n||n===name) return;
    if(kitsMap()[n]){ toast('a kit named “'+n+'” already exists','err',3500); return; }
    var slots=kmActiveSlots(name);
    lePostRaw('put', KITS_PATH, {key:n, value:{slots:JSON.parse(JSON.stringify(slots))}})
      .then(function(){ return lePostRaw('remove', KITS_PATH, {key:name}); })
      .then(function(){ return kmSetActive(n); }).catch(function(e){ toast(e.message,'err',4000); });
  }
  function kmDeleteKit(name){
    if(!name || name===AUTO_KIT){ toast('select a saved kit to delete','err',3000); return; }
    if(!window.confirm('Delete kit “'+name+'”?')) return;
    lePostRaw('remove', KITS_PATH, {key:name})
      .then(function(){ var rest=Object.keys(kitsMap()).filter(function(k){return k!==name;}); return kmSetActive(rest.length?rest[0]:''); })
      .catch(function(e){ toast(e.message,'err',4000); });
  }
  function kmImportDetected(){
    var cap=capturedMap(); if(!Object.keys(cap).length){ toast('nothing captured yet — run once with Auto-detect so the bot reads a physical kit','err',5200); return; }
    var name=kmActiveName();
    if(name && name!==AUTO_KIT){
      if(!window.confirm('Overwrite “'+name+'” with the '+Object.keys(cap).length+' auto-detected slots?')) return;
      lePostRaw('put', KITS_PATH, {key:name, value:{slots:JSON.parse(JSON.stringify(cap))}})
        .then(function(){ toast('imported detected kit into “'+name+'”','ok'); return refreshAfterMutate(); }).catch(function(e){ toast(e.message,'err',4000); });
    } else {
      var n=kmValidName(window.prompt('Import detected kit as a new kit named:','detected')); if(!n) return;
      if(kitsMap()[n]){ toast('a kit named “'+n+'” already exists','err',3500); return; }
      lePostRaw('put', KITS_PATH, {key:n, value:{slots:JSON.parse(JSON.stringify(cap))}}).then(function(){ return kmSetActive(n); }).catch(function(e){ toast(e.message,'err',4000); });
    }
  }
  /* slot writes dispatch to the active named kit (whole-kit put) or the legacy template (per-slot) */
  function kmWriteSlot(i, val){
    var name=kmActiveName();
    if(name && name!==AUTO_KIT){ var kits=kitsMap(); var slots=Object.assign({}, asMap((kits[name]||{}).slots)); slots[String(i)]=val;
      return lePostRaw('put', KITS_PATH, {key:name, value:{slots:slots}}); }
    return lePostRaw('put', KIT_PATH, {key:String(i), value:val});
  }
  function kmDeleteSlotCfg(i){
    var name=kmActiveName();
    if(name && name!==AUTO_KIT){ var kits=kitsMap(); var slots=Object.assign({}, asMap((kits[name]||{}).slots)); delete slots[String(i)];
      return lePostRaw('put', KITS_PATH, {key:name, value:{slots:slots}}); }
    return lePostRaw('remove', KIT_PATH, {key:String(i)});
  }
  function kmClearActive(){
    var name=kmActiveName();
    if(name && name!==AUTO_KIT){ lePostRaw('put', KITS_PATH, {key:name, value:{slots:{}}}).then(function(){ toast('kit cleared','ok'); return refreshAfterMutate(); }).catch(function(e){ toast(e.message,'err',4000); }); return; }
    var keys=Object.keys(kitTemplate()); if(!keys.length) return;
    var chain=Promise.resolve(); keys.forEach(function(k){ chain=chain.then(function(){ return lePostRaw('remove', KIT_PATH, {key:k}); }); });
    chain.then(function(){ toast('template cleared','ok'); return refreshAfterMutate(); }).catch(function(e){ toast(e.message,'err',4000); });
  }

  function renderKitSetup(cont){
    var cfg=getPath(LIVE.config,'client.extra.kitMaker')||{}, st=kitStatus();
    var name=kmActiveName(), slots=kmActiveSlots(name), nSaved=Object.keys(kitsMap()).length;
    var buildLabel = name===AUTO_KIT ? 'auto-detect' : (name || 'unsaved template');
    function card(k,v,sub){ return '<div class="kmCard"><span class="kmK">'+esc(k)+'</span><span class="kmV">'+v+'</span>'+(sub?'<span class="kmCSub">'+esc(sub)+'</span>':'')+'</div>'; }
    var detected=function(ok){ return ok?'<span class="kmOk">detected ✓</span>':'<span class="kmDimt">scan to detect</span>'; };
    var cards=[
      card('Building', '<span class="kmOk">'+esc(buildLabel)+'</span>', Object.keys(slots).length+' slots · '+nSaved+' saved kit'+(nSaved===1?'':'s')),
      card('Shulker source', st?detected(st.shulkerSrcOk):'<span class="kmDimt">—</span>', 'auto-classified'),
      card('Item supply', (st&&st.itemSrcs)?(st.itemSrcs+' chest'+(st.itemSrcs===1?'':'s')+' detected'):'floor chests', 'auto · radius '+(cfg.scanRadius!=null?cfg.scanRadius:20)),
      card('Deposit (kit chest)', st?detected(st.depositOk):'<span class="kmDimt">—</span>', 'auto-classified'),
      card('Max kits', (cfg.maxKits&&cfg.maxKits>0)?String(cfg.maxKits):'∞'),
      card('Match mode', esc(cfg.matchMode||'Smart'), cfg.ignoreEnchantLevels?'enchant levels ignored':'levels matter'),
      card('Allow partial', cfg.allowPartial?'<span class="kmOk">on</span>':'<span class="kmDimt">off</span>')
    ];
    var old=document.getElementById('kmSetup'); if(old) old.remove();
    var p=document.createElement('div'); p.className='panel kmPanel'; p.id='kmSetup';
    p.innerHTML='<h3>Setup <span class="sub">live · auto-detected at base</span></h3><div class="kmCards">'+cards.join('')+'</div>'+
      '<div class="kmGridHint">Shulker / item / deposit chests are auto-classified from the floor chests near the bot — edit radius &amp; the rest in ⚙ Live configuration below.</div>';
    cont.insertBefore(p, cont.firstChild);
    makeCollapsible(p, p.querySelector('h3'), 'km:setup', false);
  }

  function renderKitStatusPanel(cont){
    var old=document.getElementById('kmStatus'); if(old) old.remove();
    var p=document.createElement('div'); p.className='panel kmPanel'; p.id='kmStatus';
    p.innerHTML='<h3>Status <span class="sub">live from bot</span></h3><div class="kmStatusBody">'+kitStatusBodyHtml()+'</div>';
    var setup=document.getElementById('kmSetup');
    cont.insertBefore(p, setup ? setup.nextSibling : cont.firstChild);
    makeCollapsible(p, p.querySelector('h3'), 'km:status', false);
  }

  function kmProg(done,total){ var pct=total>0?Math.min(100,Math.round(done/total*100)):0; return '<div class="kmProg"><i style="width:'+pct+'%"></i></div>'; }
  function kmErrCards(st){
    var k=st.lastErrorKind, msg=esc(st.lastError||'');
    if(k==='shulkerEmpty') return '<div class="kmErr">⚠ <div><b>Shulker source empty.</b> '+msg+'. Refill the empty-shulker chest, then ▶ Make kit.</div></div>';
    if(k==='depositFull')  return '<div class="kmErr">⚠ <div><b>Kit chest full.</b> '+msg+'. Empty the deposit chest to continue.</div></div>';
    if(k==='missingSupply'){
      var sf=st.shortfalls||[];
      var lines=sf.map(function(s){ var en=(s.enchants&&s.enchants.length)?(' <span class="kmEnTag">'+esc(s.enchants.map(kmPretty).join(' + '))+'</span>'):'';
        return '<div class="kmErr">⚠ <div><b>Missing supply:</b> '+s.have+'× <b>'+esc(kmPretty(s.item))+'</b>'+en+' in the item chests (kit needs '+s.need+').</div></div>'; }).join('');
      return lines || ('<div class="kmErr">⚠ <div><b>Not enough materials.</b> '+msg+'</div></div>');
    }
    return '<div class="kmErr">⚠ <div>'+msg+'</div></div>';
  }
  function kitStatusBodyHtml(){
    var st=kitStatus();
    if(!st) return '<div class="kmDimt">Waiting for the bot — connect to see live status.</div>';
    var kits=st.kits||0, running=st.running, paused=st.paused, complete=st.complete, hasErr=st.lastErrorKind&&st.lastErrorKind!=='';
    var dot = hasErr||paused ? 'var(--crash,#ff6b6b)' : (running||complete) ? 'var(--acc,#3ddc97)' : 'var(--dim,#7b8a98)';
    var head = '<div class="kmStatLine"><span class="kmB" style="background:'+dot+'"></span> '+
      (paused ? 'Paused after '+kits+' kit'+(kits===1?'':'s')+' — needs attention'
       : running ? 'Building — '+esc(st.phase||'working')
       : complete ? 'Done — '+kits+' kit'+(kits===1?'':'s')+' made'
       : 'Idle'+(kits?(' — '+kits+' kit'+(kits===1?'':'s')+' made'):''))+'</div>';
    var prog = (running && st.maxKits>0) ? kmProg(kits, st.maxKits) : '';
    var meta = '<div class="kmMeta">template '+(st.templateSlots||0)+' slots · shulker '+(st.shulkerSrcOk?'✓':'–')+' · sources '+(st.itemSrcs||0)+' · deposit '+(st.depositOk?'✓':'–')+'</div>';
    var body='';
    if(hasErr) body+=kmErrCards(st);
    var kb=st.kitsBuildable, sf=st.shortfalls||[];
    if(kb!=null && kb>=0){
      body+='<div class="kmOkBox">✓ '+(kb>0 ? ('Enough materials for <b>'+kb+'</b> complete kit'+(kb===1?'':'s')+' at current stock.')
                                       : 'Not enough for a complete kit right now.')+'</div>';
      // what the NEXT kit is short, + the 30% partial note
      if(sf.length){
        var pct=(st.nextKitFillablePct!=null?st.nextKitFillablePct:0), min=(st.partialMinPct!=null?st.partialMinPct:30);
        var shortTxt=sf.slice(0,6).map(function(s){ var en=(s.enchants&&s.enchants.length)?(' '+s.enchants.map(kmPretty).join('+')):''; return (s.short!=null?s.short:s.need)+'× '+kmPretty(s.item)+en; }).join(', ')+(sf.length>6?', …':'');
        var partialNote = pct<min ? (' — below '+min+'%, won’t build a partial') : '';
        body+='<div class="kmNext"><b>Next kit needs:</b> '+esc(shortTxt)+'.'+
          ' <span class="kmDimt">('+pct+'% of slots stocked'+partialNote+')</span></div>';
      }
    } else if(!hasErr){
      body+='<div class="kmMeta">A stock estimate appears once the bot scans the room — start a run.</div>';
    }
    return head+prog+meta+body;
  }
  function refreshKitStatus(){
    if(cur!=='kitmaker') return;
    var b=document.querySelector('#kmStatus .kmStatusBody'); if(b) b.innerHTML=kitStatusBodyHtml();
    var cont=$(LO.cfg); if(cont && document.getElementById('kmSetup')) renderKitSetup(cont);   // refresh detected ✓ marks
  }

  /* ---- item picker popup ---- */
  var KMP=null;
  function closeKitPicker(){ ['kmPop','kmScrim'].forEach(function(id){ var e=document.getElementById(id); if(e) e.remove(); }); KMP=null; }
  function openKitPicker(i, ev){
    if(typeof ABMItems==='undefined'){ toast('item catalog not loaded — hard-refresh','err',3600); return; }
    closeKitPicker();
    var s=kitTemplate()[String(i)]||null;
    var rect=(ev&&ev.currentTarget&&ev.currentTarget.getBoundingClientRect)?ev.currentTarget.getBoundingClientRect():null;
    KMP={ slot:i, cat:ABMItems.cats[0], q:'', rect:rect,
          item:s?mcStrip(s.item):null, count:s?(s.count||1):1,
          match:s?(s.match||'Auto'):'Auto', enchants:(s&&s.enchants)?s.enchants.slice():[] };
    var scrim=document.createElement('div'); scrim.className='kmScrim'; scrim.id='kmScrim'; scrim.onclick=closeKitPicker;
    var pop=document.createElement('div'); pop.className='kmPop'; pop.id='kmPop';
    document.body.appendChild(scrim); document.body.appendChild(pop);
    renderPicker();
  }
  function kmItemsHtml(){
    var list = KMP.q ? ABMItems.all.filter(function(id){ return id.indexOf(KMP.q)>=0; }) : (ABMItems.byCat[KMP.cat]||[]);
    var shown=list.slice(0,240);
    var items=shown.map(function(id){ var st=kmStack(id);
      return '<div class="kmIt'+(id===KMP.item?' sel':'')+'" data-id="'+esc(id)+'" title="'+esc(kmPretty(id))+(st<64?(' · stacks to '+st):'')+'">'+kitIcon(id,26)+(st<64?'<span class="kmStk">'+st+'</span>':'')+'</div>'; }).join('');
    var more = list.length>shown.length ? '<div class="kmMore">+'+(list.length-shown.length)+' more — type to search</div>' : (items?'':'<div class="kmMore">no matches</div>');
    return items+more;
  }
  function renderPicker(){
    var pop=document.getElementById('kmPop'); if(!pop) return;
    var cats=ABMItems.cats.map(function(c){ return '<span class="kmCat'+(c===KMP.cat&&!KMP.q?' on':'')+'" data-cat="'+esc(c)+'">'+esc(c)+'</span>'; }).join('');
    var cfg='';
    if(KMP.item){
      var mx=kmStack(KMP.item);
      cfg='<div class="kmCfg"><div class="kmCfgRow"><span>Count <span class="kmDimt">(max '+mx+')</span></span>'+
        '<span class="kmStep"><button type="button" data-st="-">−</button><input id="kmCount" inputmode="numeric" value="'+KMP.count+'"><button type="button" data-st="+">+</button></span></div>';
      var et=ABMItems.enchTypeOf(KMP.item);
      if(et){
        cfg+='<div class="kmCfgRow"><span>Match</span><select id="kmMatch">'+
          KM_MATCHES.map(function(o){ return '<option value="'+o[0]+'"'+(o[0]===KMP.match?' selected':'')+'>'+esc(o[1])+'</option>'; }).join('')+'</select></div>';
        if(KMP.match==='ByEnchants'){
          var es=ABMItems.enchantsFor(KMP.item);
          cfg+='<div class="kmEnchHint">Tick the enchant(s) that DEFINE this slot — a source item matches when it carries all ticked (extra enchants ignored). Keeps a Silk Touch and a Fortune pick as separate, independently-enforced needs.</div>'+
            '<div class="kmEnch">'+es.map(function(e){ var on=KMP.enchants.indexOf(e[0])>=0;
              return '<label class="kmEnchI'+(on?' on':'')+'"><input type="checkbox" data-ench="'+esc(e[0])+'"'+(on?' checked':'')+'> '+esc(e[1])+'</label>'; }).join('')+'</div>';
        }
      } else {
        cfg+='<div class="kmEnchHint kmDimt">This item has no enchantments — matched by type.</div>';
      }
      cfg+='</div>';
    }
    pop.innerHTML='<div class="kmPopHd">Slot '+(KMP.slot+1)+(KMP.item?(' · '+esc(kmPretty(KMP.item))):'')+'<span class="kmX">✕</span></div>'+
      '<div class="kmSearch"><input id="kmQ" placeholder="Search all 1.21.4 items…" value="'+esc(KMP.q)+'" autocomplete="off" spellcheck="false"></div>'+
      '<div class="kmCats">'+cats+'</div>'+
      '<div class="kmItems">'+kmItemsHtml()+'</div>'+cfg+
      '<div class="kmPopFoot">'+(KMP.item?'<button class="kmDel">Clear slot</button>':'')+'<span class="kmGrow"></span>'+
        '<button class="kmCancel">Cancel</button><button class="kmSave"'+(KMP.item?'':' disabled')+'>Save slot</button></div>';
    positionPicker();
    wirePicker();
  }
  function positionPicker(){
    var pop=document.getElementById('kmPop'); if(!pop) return;
    var w=pop.offsetWidth||360, h=pop.offsetHeight||520, r=KMP.rect, left, top;
    if(r){ left=Math.min(r.right+10, window.innerWidth-w-10); top=Math.min(r.top, window.innerHeight-h-10); }
    else { left=(window.innerWidth-w)/2; top=56; }
    pop.style.left=Math.max(10,left)+'px'; pop.style.top=Math.max(10,top)+'px';
  }
  function kmUpdateItems(){ var pop=document.getElementById('kmPop'); var box=pop&&pop.querySelector('.kmItems'); if(!box) return; box.innerHTML=kmItemsHtml(); wireItemClicks(); }
  function wireItemClicks(){ var pop=document.getElementById('kmPop'); if(!pop) return;
    wireKitIcons(pop);
    [].forEach.call(pop.querySelectorAll('.kmIt'), function(it){ it.onclick=function(){
      var id=this.dataset.id, prevType=KMP.item?ABMItems.enchTypeOf(KMP.item):null;
      KMP.item=id; var mx=kmStack(id); if(KMP.count>mx) KMP.count=mx; if(KMP.count<1) KMP.count=1;
      if(ABMItems.enchTypeOf(id)!==prevType){ KMP.match='Auto'; KMP.enchants=[]; }
      renderPicker();
    }; });
  }
  function wirePicker(){
    var pop=document.getElementById('kmPop'); if(!pop) return;
    pop.querySelector('.kmX').onclick=closeKitPicker;
    pop.querySelector('.kmCancel').onclick=closeKitPicker;
    var q=pop.querySelector('#kmQ'); if(q){ q.oninput=function(){ KMP.q=this.value.trim().toLowerCase().replace(/[\s]+/g,'_'); kmUpdateItems();
      [].forEach.call(pop.querySelectorAll('.kmCat'), function(x){ x.classList.toggle('on', !KMP.q && x.dataset.cat===KMP.cat); }); }; q.focus(); }
    [].forEach.call(pop.querySelectorAll('.kmCat'), function(ch){ ch.onclick=function(){ KMP.cat=this.dataset.cat; KMP.q=''; var qi=pop.querySelector('#kmQ'); if(qi) qi.value='';
      [].forEach.call(pop.querySelectorAll('.kmCat'), function(x){ x.classList.toggle('on', x===ch); }); kmUpdateItems(); }; });
    wireItemClicks();
    var save=pop.querySelector('.kmSave'); if(save) save.onclick=saveKitSlot;
    var del=pop.querySelector('.kmDel'); if(del) del.onclick=function(){ removeKitSlot(KMP.slot); };
    var ci=pop.querySelector('#kmCount');
    [].forEach.call(pop.querySelectorAll('.kmStep button'), function(b){ b.onclick=function(){ var mx=kmStack(KMP.item), v=(parseInt(ci.value,10)||1)+(this.dataset.st==='+'?1:-1); v=Math.max(1,Math.min(mx,v)); KMP.count=v; ci.value=v; }; });
    if(ci) ci.onchange=function(){ var mx=kmStack(KMP.item), v=Math.max(1,Math.min(mx,parseInt(this.value,10)||1)); KMP.count=v; this.value=v; };
    var ms=pop.querySelector('#kmMatch'); if(ms) ms.onchange=function(){ KMP.match=this.value; renderPicker(); };
    [].forEach.call(pop.querySelectorAll('.kmEnch input'), function(cb){ cb.onchange=function(){ var id=this.dataset.ench, k=KMP.enchants.indexOf(id);
      if(this.checked){ if(k<0) KMP.enchants.push(id); } else if(k>=0) KMP.enchants.splice(k,1);
      var lab=this.closest('.kmEnchI'); if(lab) lab.classList.toggle('on', this.checked); }; });
  }
  function saveKitSlot(){
    if(!KMP || !KMP.item) return;
    var val={ item:KMP.item, count:KMP.count, match:KMP.match, enchants:(KMP.match==='ByEnchants')?KMP.enchants.slice():[] };
    var slot=KMP.slot, name=kmPretty(KMP.item), cnt=KMP.count;
    kmWriteSlot(slot, val)
      .then(function(){ toast('slot '+(slot+1)+' = '+name+' ×'+cnt, 'ok'); closeKitPicker(); return refreshAfterMutate(); })
      .catch(function(e){ toast(e.message,'err',4200); });
  }
  function removeKitSlot(i){
    kmDeleteSlotCfg(i)
      .then(function(){ toast('slot '+(i+1)+' cleared','ok'); closeKitPicker(); return refreshAfterMutate(); })
      .catch(function(e){ toast(e.message,'err',4000); });
  }

  function injectKitCss(){
    if($('#kmCss')) return;
    var s=document.createElement('style'); s.id='kmCss';
    s.textContent=
      '.kmPanel{margin-bottom:.7rem}'+
      '.kmGrid{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:4px;background:#06090c;border:1px solid var(--line,#1d2730);border-radius:10px;padding:8px;width:100%;box-sizing:border-box}'+
      '.kmSlot{aspect-ratio:1;min-width:0;background:#161e26;border:1px solid #232f3a;border-radius:6px;position:relative;cursor:pointer;display:flex;align-items:center;justify-content:center}'+
      '.kmSlot:hover{border-color:var(--acc,#3ddc97)}'+
      '.kmSlot.filled{background:#11202099}'+
      '.kmGrid .kmImg{width:76%!important;height:76%!important}'+
      '.kmGrid .kmFb{width:76%;height:76%;font-size:.5rem}'+
      '.kmCt{position:absolute;right:1px;bottom:-1px;font-family:var(--mono,monospace);font-size:.62rem;font-weight:700;color:#fff;text-shadow:1px 1px 0 #000}'+
      '.kmEn{position:absolute;left:1px;top:0;font-size:.6rem;color:var(--acc,#3ddc97);text-shadow:0 0 3px #000}'+
      '.kmFb{font-family:var(--mono,monospace);font-size:.52rem;color:var(--dim,#7b8a98);display:flex;align-items:center;justify-content:center;width:26px;height:26px;background:#0b1117;border-radius:5px}'+
      '.kmGridHint{font-family:var(--mono,monospace);font-size:.6rem;color:var(--dim,#7b8a98);margin-top:.5rem;line-height:1.4}'+
      '.kmClear{margin-left:auto;font-size:.68rem;color:#ffb4b4;background:#0b0f14;border:1px solid #5a2b2b;border-radius:7px;padding:.18rem .5rem;cursor:pointer}'+
      '.kmSlot.kmRO{cursor:default}'+
      /* kit-library bar */
      '.kmKitBar{display:flex;align-items:center;flex-wrap:wrap;gap:.3rem;margin:.1rem 0 .35rem}'+
      '.kmKBl{font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;color:var(--dim,#7b8a98)}'+
      '.kmKitSel{flex:1;min-width:120px;font-family:var(--sans,sans-serif);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:7px;padding:.3rem .4rem}'+
      '.kmKB{font-size:.72rem;background:#0b0f14;border:1px solid var(--line,#1d2730);border-radius:7px;padding:.26rem .42rem;cursor:pointer;color:#cdd9e2;line-height:1}'+
      '.kmKB:hover{border-color:var(--acc,#3ddc97)}'+
      '.kmKBimp{color:var(--acc,#3ddc97);border-color:var(--acc-dim,#1f7a55)}'+
      '.kmKBdel{color:#ffb4b4;border-color:#5a2b2b}'+
      '.kmKitNote{font-family:var(--mono,monospace);font-size:.58rem;color:var(--dim,#7b8a98);line-height:1.4;margin-bottom:.4rem}'+
      '.kmNext{font-size:.78rem;background:#ffb4540d;border:1px solid #5a3b1f;border-radius:9px;padding:.45rem .6rem;color:#ffd9a8;line-height:1.45}'+
      /* dynamic setup cards: content-sized, wrap */
      '.kmCards{display:flex;flex-wrap:wrap;gap:.5rem}'+
      '.kmCard{background:#11181f;border:1px solid var(--line,#1d2730);border-radius:10px;padding:.5rem .65rem;display:flex;flex-direction:column;gap:.16rem;min-width:0}'+
      '.kmK{font-size:.6rem;text-transform:uppercase;letter-spacing:.06em;color:var(--dim,#7b8a98)}'+
      '.kmV{font-size:.84rem;font-weight:600}'+
      '.kmCSub{font-family:var(--mono,monospace);font-size:.58rem;color:var(--dim,#7b8a98)}'+
      '.kmOk{color:var(--acc,#3ddc97)}.kmDimt{color:var(--dim,#7b8a98)}'+
      /* status */
      '.kmStatusBody{display:flex;flex-direction:column;gap:.45rem}'+
      '.kmStatLine{display:flex;align-items:center;gap:.5rem;font-size:.82rem}'+
      '.kmB{width:9px;height:9px;border-radius:50%;flex:none}'+
      '.kmMeta{font-family:var(--mono,monospace);font-size:.62rem;color:var(--dim,#7b8a98);line-height:1.4}'+
      '.kmProg{height:7px;border-radius:7px;background:#0b0f14;border:1px solid var(--line,#1d2730);overflow:hidden}.kmProg i{display:block;height:100%;background:var(--acc,#3ddc97)}'+
      '.kmErr{display:flex;gap:.5rem;align-items:flex-start;font-size:.78rem;background:#ff6b6b12;border:1px solid #5a2b2b;border-radius:9px;padding:.45rem .55rem;color:#ffb4b4;line-height:1.4}'+
      '.kmEnTag{color:var(--acc,#3ddc97)}'+
      '.kmOkBox{font-size:.8rem;background:#3ddc9712;border:1px solid var(--acc-dim,#1f7a55);border-radius:9px;padding:.45rem .6rem;color:var(--acc,#3ddc97);line-height:1.4}'+
      /* picker popup */
      '.kmScrim{position:fixed;inset:0;background:rgba(4,7,10,.55);z-index:10005}'+
      '.kmPop{position:fixed;width:360px;max-height:90vh;overflow:auto;background:var(--panel,#11171e);border:1px solid var(--acc-dim,#1f7a55);border-radius:12px;box-shadow:0 24px 60px #000c;padding:.7rem;z-index:10006}'+
      '.kmPopHd{display:flex;align-items:center;gap:.5rem;font-size:.84rem;font-weight:700;margin-bottom:.45rem}.kmPopHd .kmX{margin-left:auto;color:var(--dim,#7b8a98);cursor:pointer}'+
      '.kmSearch input{width:100%;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:8px;padding:.4rem .55rem;font-size:.82rem}'+
      '.kmCats{display:flex;flex-wrap:wrap;gap:.25rem;margin:.45rem 0}'+
      '.kmCat{font-size:.66rem;color:var(--dim,#7b8a98);border:1px solid var(--line,#1d2730);border-radius:20px;padding:.14rem .46rem;cursor:pointer}'+
      '.kmCat.on{color:var(--acc,#3ddc97);border-color:var(--acc-dim,#1f7a55);background:#3ddc9712}'+
      '.kmItems{display:grid;grid-template-columns:repeat(auto-fill,38px);gap:5px;max-height:210px;overflow:auto;align-content:start}'+
      '.kmIt{width:38px;height:38px;border:1px solid #232f3a;border-radius:7px;background:#161e26;cursor:pointer;display:flex;align-items:center;justify-content:center;position:relative}'+
      '.kmIt:hover{border-color:var(--acc,#3ddc97)}.kmIt.sel{border-color:var(--acc,#3ddc97);box-shadow:0 0 0 1px var(--acc,#3ddc97)}'+
      '.kmIt .kmStk{position:absolute;right:1px;bottom:0;font-family:var(--mono,monospace);font-size:.5rem;color:var(--warn,#ffb454)}'+
      '.kmMore{grid-column:1/-1;font-family:var(--mono,monospace);font-size:.6rem;color:var(--dim,#7b8a98);padding:.3rem .1rem}'+
      '.kmCfg{margin-top:.55rem;border-top:1px solid #ffffff10;padding-top:.5rem;display:flex;flex-direction:column;gap:.4rem}'+
      '.kmCfgRow{display:flex;align-items:center;justify-content:space-between;gap:.6rem;font-size:.78rem}'+
      '.kmCfgRow select{font-family:var(--sans,sans-serif);font-size:.76rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:7px;padding:.28rem .4rem}'+
      '.kmStep{display:flex;align-items:center;gap:.3rem}.kmStep button{width:22px;height:22px;border-radius:6px;border:1px solid var(--line,#1d2730);background:#0b0f14;color:#cdd9e2;cursor:pointer}'+
      '.kmStep input{width:46px;text-align:center;background:#06090c;color:#cdd9e2;border:1px solid var(--line,#1d2730);border-radius:6px;padding:.24rem;font-family:var(--mono,monospace)}'+
      '.kmEnchHint{font-size:.66rem;color:var(--dim,#7b8a98);line-height:1.4}'+
      '.kmEnch{display:flex;flex-wrap:wrap;gap:.3rem}'+
      '.kmEnchI{display:inline-flex;align-items:center;gap:.25rem;font-size:.7rem;color:var(--dim,#7b8a98);border:1px solid var(--line,#1d2730);border-radius:7px;padding:.2rem .42rem;cursor:pointer}'+
      '.kmEnchI.on{color:var(--acc,#3ddc97);border-color:var(--acc-dim,#1f7a55);background:#3ddc9712}.kmEnchI input{width:13px;height:13px}'+
      '.kmPopFoot{display:flex;align-items:center;gap:.5rem;margin-top:.6rem}.kmGrow{flex:1}'+
      '.kmPopFoot button{font-size:.76rem;border-radius:8px;padding:.36rem .7rem;cursor:pointer;border:1px solid var(--line,#1d2730);background:#0b0f14;color:#cdd9e2}'+
      '.kmSave{font-weight:700;color:var(--acc,#3ddc97);border-color:var(--acc-dim,#1f7a55)!important}.kmSave:disabled{opacity:.5;cursor:default}'+
      '.kmDel{color:#ffb4b4;border-color:#5a2b2b!important}';
    document.head.appendChild(s);
  }

  /* the mockup's dead "＋ New …" buttons (v1 left list panel) — open the same form.
     NB: exclude .grpAddRow / .llAddRow — those panels wire their own add buttons; without :not()
     they'd get double-bound (and .grpAddRow would wrongly open the TRADE form). */
  function wireAddRows(){
    var spec=LIST_EDITORS[cur]; if(!spec) return;
    [].forEach.call(document.querySelectorAll('.addrow:not(.grpAddRow):not(.llAddRow)'), function(el){
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
      '.leBody{padding:.7rem .85rem;display:flex;flex-direction:column;gap:.4rem;max-height:80vh;overflow:auto}'+
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
      '.grpMembers{display:flex;flex-direction:column;gap:.1rem;max-height:46vh;overflow:auto;border:1px solid var(--line,#1d2730);border-radius:9px;padding:.3rem;background:#06090c}'+
      /* collapsible panels */
      '.cl-collapsed > :not(.cl-head){display:none!important}'+
      '.cl-caret{display:inline-block;width:.9em;text-align:center;opacity:.65;font-size:.7em;flex:none}'+
      '.cl-head{cursor:pointer;user-select:none}'+
      '.cl-head:hover .cl-caret{opacity:1}'+
      '.grpMem{display:flex;align-items:center;gap:.5rem;padding:.32rem .45rem;border-radius:7px;cursor:pointer}'+
      '.grpMem:hover{background:rgba(255,255,255,.05)}'+
      '.grpMem input{flex:none;width:15px;height:15px;cursor:pointer}'+
      '.grpMemBd{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.2}'+
      '.grpMemT{font-size:.78rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'+
      '.grpMemS{font-family:var(--mono,monospace);font-size:.62rem;color:var(--dim,#7b8a98);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'+
      '.grpSupply{border:1px solid var(--line,#1d2730);border-radius:9px;padding:.1rem .55rem;margin-top:.2rem}'+
      '.grpSupply > summary{cursor:pointer;font-size:.74rem;font-weight:700;color:#cdd9e2;padding:.45rem .1rem;list-style:none}'+
      '.grpSupply > summary::-webkit-details-marker{display:none}'+
      '.grpSupply > summary::before{content:"▸ ";opacity:.6}'+
      '.grpSupply[open] > summary::before{content:"▾ "}'+
      '.grpSupply[open] > summary{border-bottom:1px solid #ffffff10;margin-bottom:.3rem}'+
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
    wireLivePickers();                                             // make the chip+dropdown pickers live + persistent
    if(cur==='trader'){ buildListEditor(); buildGroupsPanel(); }   // trader: trades editor (right) + groups (left)
    else if(cur==='kitmaker'){ buildKitMaker(); }                 // kit maker: live grid (left) + setup/status (right)
    else if(LIST_EDITORS[cur]){ buildLeftLiveList(LIST_EDITORS[cur]); }  // elytra/pearl: live list in the nice left panel
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
    injectKitCss();
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
