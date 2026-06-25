/* ===========================================================================
   ABM Control — shared data + friendly-naming layer (single source of truth)
   ---------------------------------------------------------------------------
   Every mockup (control-v1..v4, control-drawer-panel) loads this file. All
   human-facing names live HERE so they stay friendly & consistent everywhere:
     • module .name is the friendly label; .raw is the config-class key
       (shown small/mono next to it so engineers can still map to config)
     • dropdown option strings are pre-prettified (e.g. "Clear area (quarry)"
       instead of AreaClear, "To overflow chest" instead of TO_OVERFLOW)
     • pretty() title-cases item registry ids for display
   To rename anything across the whole dashboard, edit it once, here.
   =========================================================================== */

/* ---- item icon (real MC texture via CDN, graceful colored fallback) ---- */
function icon(id, label, size){
  const rid = (id||'').replace(/^minecraft:/,'');
  const l = (label||id).replace(/.*:/,'').slice(0,2).toUpperCase();
  return `<span class="ii" style="--s:${size||22}px"><img src="https://mc.nerothe.com/img/1.21/minecraft_${rid}.png" alt="" `+
    `onerror="this.style.display='none';this.parentNode.classList.add('fb');this.parentNode.setAttribute('data-l','${l}')"></span>`;
}

/* ---- pretty(): friendly display name for a raw item id ---- */
const ITEM_OVERRIDES = {
  'enchanted_golden_apple':'Enchanted Golden Apple','totem_of_undying':'Totem of Undying',
  'ender_pearl':'Ender Pearl','ender_chest':'Ender Chest','experience_bottle':'XP Bottle',
  'filled_map':'Ocean Map','ancient_debris':'Ancient Debris','enchanted_book':'Enchanted Book',
};
function pretty(id){
  const k = (id||'').replace(/^minecraft:/,'');
  if (ITEM_OVERRIDES[k]) return ITEM_OVERRIDES[k];
  return k.replace(/_/g,' ').replace(/\b\w/g, c=>c.toUpperCase());
}

/* ---- friendly enum / option lists (the strings users actually see) ---- */
const DIMS   = ['Overworld','Nether','The End'];
const PROF   = ['Any profession','Armorer','Butcher','Cartographer','Cleric','Farmer','Fisherman',
                'Fletcher','Leatherworker','Librarian','Mason','Nitwit','Shepherd','Toolsmith','Weaponsmith'];
const HWY    = ['North','South','East','West','Northeast','Southeast','Northwest','Southwest'];
const KICK   = ['Firework','Sprint start','Synth (no firework)'];
const STORE  = ["Don't store",'Back to restock','To overflow chest'];
const FLIGHT = ['Cruise','Highway','E-bounce'];
const MINEMODE = ['Clear area (quarry)','Search for ores'];
const AREAMODE = ['Unlimited','Chunks from start','Two corners','Loaded at start'];
const ANCHOR   = ['Centered','Corner'];
const ORETOOL  = ['Fortune (drops)','Silk Touch (blocks)'];
const SCANMODE = ['Region scan','Coordinate list'];
const MATCH    = ['Loose (item type)','Smart (ignore cosmetic)','Exact (all parts)'];
const SNIFDIR  = ['Incoming','Outgoing','Both'];

const T = (l,k,v,o)=>Object.assign({l,k,v},o||{});  // field helper

/* ============================ MODULES ====================================
   Item shape:  { n, c, tag, tk, trade, ench, detail, patch, mx, my, route }
   Module shape:{ id, name(friendly), raw(config key), icon, cat, tag(tagline),
                  status, sdot, enabled, metric, actions, list?, groups,
                  signature?, hint?, pinKind?, mapBox?, station? }
   ========================================================================= */
const MODULES = [
/* ---------- CONTROL surfaces ---------- */
{ id:'livemap', name:'Live Map', raw:'LiveViewer', icon:'🗺️', cat:'control',
  tag:'Live world map scaled to the bot · entity & waypoint overlays', status:'run', sdot:'run', enabled:true,
  metric:'Following oldmanmango · 20 Hz · 7 entities · −2480, 71, 15330', hint:'Click the map to drop an Elytra destination, or a pin to act. ⛶ opens the fullscreen map.', signature:'liveMap',
  actions:[['go','⛶ Open fullscreen'],['','◎ Recenter on bot'],['','💾 Save view']],
  groups:[
    {title:'View & follow', open:true, rows:[T('Camera','seg',0,{opts:['Follow bot','Free pan']}),T('Map span','select','512 blocks',{opts:['128 blocks','256 blocks','512 blocks','1024 blocks','2048 blocks']}),T('Lock to bot dimension','toggle',true),T('Rotate map with bot facing','toggle',false),T('Coordinate grid','toggle',true),T('Chunk borders','toggle',false)]},
    {title:'Overlays', open:true, rows:[T('Players','toggle',true),T('Other bots','toggle',true),T('Hostile mobs','toggle',true),T('Passive mobs','toggle',false),T('Dropped items','toggle',true),T('Saved trips / waypoints','toggle',true),T('Pearl stasis locations','toggle',true),T('Stash chests','toggle',true),T('Deposit / supply chests','toggle',true),T('Nether highway grid','toggle',true),T('Spawn ring','toggle',true)]},
    {title:'Live feed', open:false, rows:[T('Feed source','select','Per-bot SSE',{opts:['Per-bot SSE','HTTP polling']}),T('Update rate','select','20 Hz',{opts:['20 Hz','10 Hz','5 Hz','1 Hz']}),T('Auto-detect bot viewer port','toggle',true),T('Interpolate movement','toggle',true),T('Pause feed when tab hidden','toggle',true)]},
    {title:'Scale & range', open:false, rows:[T('Render range','num',48,{unit:'chunks'}),T('Entity range','num',96,{unit:'blocks'}),T('Pin label density','select','Hover only',{opts:['Always','Hover only','Selected only']}),T('Marker size','num',9,{unit:'px'})]},
    {title:'Interaction', open:false, rows:[T('Click map → set Elytra destination','toggle',true),T('Click pin → act','toggle',true),T('Confirm before sending commands','toggle',true),T('Default pin action','select','Open in module',{opts:['Open in module','Walk to','Fly to']})]},
    {title:'POV inset  ·  per-bot first-person', open:false, rows:[T('Show POV inset','toggle',false),T('POV render range','select','48 blocks',{opts:['32 blocks','48 blocks','64 blocks']}),T('Fullbright','toggle',true),T('Ambient occlusion','toggle',true),T('Fog','toggle',true),T('Inset corner','select','Bottom-right',{opts:['Bottom-right','Bottom-left','Top-right','Top-left']})]},
  ]},
{ id:'elytra', name:'Elytra Autopilot', raw:'ElytraPilot', icon:'🪂', cat:'control',
  tag:'Trip planner & long-haul flight', status:'run', sdot:'run', enabled:true,
  metric:'Cruising · 312 b/s · 8,420 blocks to target', hint:'Click the map to drop a destination, or pick a saved trip below.', pinKind:'trip',
  actions:[['go','▶ Fly there'],['danger','■ Stop'],['','💾 Save trip']],
  list:{title:'Saved trips', kind:'trips', addLabel:'New trip', cols:['','TRIP','DESTINATION','DIM'], items:[
    {n:'Spawn → Stash', c:'-2480, 71, 15330 · cruise', tag:'Overworld', tk:'ov', mx:78,my:22, route:1, patch:{dimension:'Overworld',mode:0,tx:-2480,tz:15330}},
    {n:'2b2t +X Highway', c:'1,000,000, 120, 0 · e-bounce', tag:'Nether', tk:'ne', mx:90,my:50, route:1, patch:{dimension:'Nether',mode:2,tx:1000000,tz:0}},
    {n:'End Gateway Run', c:'0, 64, 1000 · cruise', tag:'The End', tk:'en', mx:50,my:12, patch:{dimension:'The End',mode:0,tx:0,tz:1000}},
    {n:'Base → Spawn', c:'0, 64, 0 · highway', tag:'Highway', tk:'ne', mx:24,my:74, patch:{dimension:'Nether',mode:1,tx:0,tz:0}},
  ]},
  groups:[
    {title:'Destination', open:true, rows:[T('Mode','seg',0,{opts:['Coordinates','Heading']}),T('Target X','num',-2480,{id:'tx'}),T('Target Z','num',15330,{id:'tz'}),T('Free-fly heading','num',90,{unit:'°'}),T('Dimension','select','Overworld',{id:'dimension',opts:DIMS}),T('Begin descent within','num',24,{unit:'blocks'}),T('Arrived within','num',6,{unit:'blocks'}),T('Approx. ground Y at target','num',64)]},
    {title:'Flight engine', open:true, rows:[T('Flight engine','select','Cruise',{id:'mode',opts:FLIGHT}),T('Highway direction','select','Northeast',{opts:HWY}),T('Highway level-cruise (never touch road)','toggle',false),T('Native nether routing','toggle',true),T('Take off with double-jump','toggle',true),T('Start / resume trip on connect','toggle',true)]},
    {title:'Cruise climb & glide  ·  overworld / End', open:false, rows:[T('Glide ceiling Y','num',2000),T('Glide floor Y','num',1000),T('Glide pitch (nose-down)','num',2.0,{unit:'°'}),T('Climb pitch (nose-up)','num',42,{unit:'°'}),T('Glide ratio','num',6.0,{unit:'blk/blk'}),T('Scale climb to trip distance','toggle',true),T('Cruise glide ratio','num',6.0,{unit:'blk/blk'}),T('Cruise ceiling cap Y','num',3000),T('Climb-fire interval','num',120,{unit:'ticks'}),T('Altitude gained per rocket','num',140,{unit:'blocks'})]},
    {title:'Cruise speed governor', open:false, rows:[T('Soft speed — low','num',30,{unit:'b/s'}),T('Soft speed — high','num',35,{unit:'b/s'}),T('Hard speed cap','num',55,{unit:'b/s'}),T('Glide pitch trim','num',8,{unit:'°'}),T('Overspeed brake pitch','num',25,{unit:'°'}),T('Boost when below','num',24,{unit:'b/s'}),T('Min boost spacing','num',14,{unit:'ticks'}),T('Re-boost interval (cap)','num',30,{unit:'ticks'}),T('Re-boost below speed','num',0.55,{unit:'b/t'})]},
    {title:'E-bounce skim  ·  flat roads', open:false, rows:[T('Enable e-bounce','toggle',false),T('Road surface Y','num',120),T('How it reaches speed','select','Firework',{opts:KICK}),T('Target cruise speed','num',40,{unit:'b/s'}),T('Skim pitch (level glide)','num',0,{unit:'°'}),T('Skim height above road','num',1.0,{unit:'blocks'}),T('Acceleration','num',2.0,{unit:'b/s/tick'}),T('Allow launch jump','toggle',true),T('Constant pitch on diagonals','toggle',true),T('Diagonal pitch','num',72,{unit:'°'}),T('Kickstart pitch','num',10,{unit:'°'}),T('Run-start speed','num',5.0,{unit:'b/s'}),T('Run-start ramp','num',25,{unit:'b/s/s'}),T('Abort if it drops below road by','num',6,{unit:'blocks'})]},
    {title:'E-bounce physics  ·  expert tuning', open:false, rows:[T('Dive pitch','num',45,{unit:'°'}),T('Dive starts at height','num',0.5,{unit:'blocks'}),T('Dive gain','num',40,{unit:'°/blk'}),T('Re-deploy height','num',0.3,{unit:'blocks'}),T('Re-deploy max vertical speed','num',0.10,{unit:'b/t'}),T('Re-deploy spacing','num',3,{unit:'ticks'}),T('Clear fall-flying on ground touch','toggle',false),T('Max inject per tick','num',0.06,{unit:'b/t'}),T('Restore spread','num',3,{unit:'ticks'}),T('Restore cap','num',0.30,{unit:'b/t'}),T('Hold-enter fraction','num',0.78),T('Hold-exit fraction','num',0.55),T('Hover band — low','num',0.8,{unit:'blocks'}),T('Hover band — high','num',1.4,{unit:'blocks'}),T('Climb velocity','num',0.03,{unit:'b/t'}),T('Boost smoothing','num',0.5),T('Per-tick telemetry log','toggle',false)]},
    {title:'Highway cruise', open:false, rows:[T('Clearance above road','num',3,{unit:'blocks'}),T('Ceiling above road','num',5,{unit:'blocks'}),T('Look-ahead','num',64,{unit:'blocks'}),T('3D pathfinder steering','toggle',false)]},
    {title:'Nether routing', open:false, rows:[T('Native nether routing','toggle',true),T('World seed','text','146008555100680'),T('Route search timeout','num',10000,{unit:'ms'}),T('Nether ceiling Y','num',122),T('Cruise Y','num',70),T('Slow at frontier within','num',56,{unit:'blocks'}),T('Hold at frontier within','num',28,{unit:'blocks'}),T('Seek exit portal within','num',500,{unit:'blocks'}),T('Fly overworld-direct within','num',100000,{unit:'blocks'}),T('Walk onto highway within','num',100,{unit:'blocks'}),T('Ride highways on nether leg','toggle',false),T('Destination is in the nether','toggle',false),T('Nether-entry grace','num',100,{unit:'ticks'})]},
    {title:'Grief hardening  ·  nether highways', open:false, rows:[T('Power-fly across open gaps','toggle',true),T('Reroute around full walls','toggle',true),T('Record hazards to local grief map','toggle',true),T('Wither-tank while god-apples ≥','num',12),T('Corridor probe look-ahead','num',24,{unit:'blocks'}),T('Probe band below road','num',2,{unit:'blocks'}),T('Probe band above road','num',4,{unit:'blocks'}),T('Reroute clearance','num',24,{unit:'blocks'}),T('Reroute arrive radius','num',24,{unit:'blocks'}),T('Reroute timeout','num',1200,{unit:'ticks'})]},
    {title:'Obstacle passing & recovery', open:false, rows:[T('Pass obstacles via Baritone','toggle',true),T('Aim bypass ahead','num',12,{unit:'blocks'}),T('Settle before bypass','num',20,{unit:'ticks'}),T('Bypass timeout','num',200,{unit:'ticks'}),T('Max bypass attempts','num',8),T('Recover after falling off road','toggle',true),T('Max recover episodes','num',6),T('Stall limit','num',100,{unit:'ticks'}),T('Stall speed','num',8,{unit:'b/s'}),T('Hop pitch','num',28,{unit:'°'}),T('Hop timeout','num',40,{unit:'ticks'}),T('Terrain reroute','toggle',true),T('Max reroute deviation','num',70,{unit:'°'}),T('Glide-path clearance','num',4,{unit:'blocks'}),T('Terrain look-ahead','num',16,{unit:'blocks'})]},
    {title:'Pre-flight gear-up', open:false, rows:[T('Audit kit before each trip','toggle',true),T('Flight kit profile','text',''),T('Min armour pieces','num',1),T('Min elytras (worn + spare)','num',2),T('Min totems','num',2),T('Require totem in offhand','toggle',true),T('Min fireworks','num',64),T('Size fireworks to distance','toggle',true),T('Firework safety margin','num',1.5,{unit:'×'}),T('Blocks per elytra','num',10000),T('Min enchanted golden apples','num',64),T('Require a pickaxe','toggle',true),T('Bring a weapon if in kit','toggle',true),T('Min ender chests','num',8)]},
    {title:'Elytra durability & swap', open:false, rows:[T('Auto-swap worn elytra','toggle',true),T('Swap below durability','num',20),T('Fresh spare must exceed','num',10),T('Last-elytra emergency at','num',100),T('Logout on last elytra','toggle',true),T('Min clearance to swap','num',50,{unit:'blocks'})]},
    {title:'E-bounce resupply', open:false, rows:[T('Resupply elytras from ender chest','toggle',true),T('E-bounce kit profile','text',''),T('Resupply when spares below','num',2),T('Refill to','num',9,{unit:'elytras'})]},
    {title:'Landing', open:false, rows:[T('Air-landing search radius','num',12,{unit:'blocks'}),T('Walk last leg with Baritone','toggle',true),T('Stop firing below ceiling by','num',30,{unit:'blocks'}),T('Drop-in clearance','num',6,{unit:'blocks'}),T('Landing spin step','num',18,{unit:'°/tick'}),T('Brake to speed','num',6,{unit:'b/s'}),T('Aggressive dive above Y','num',380),T('Dive pitch','num',70,{unit:'°'}),T('Relaunch pitch — covered','num',-20,{unit:'°'}),T('Relaunch pitch — open sky','num',-75,{unit:'°'}),T('Open-sky clearance','num',6,{unit:'blocks'})]},
    {title:'Safety & solver', open:false, rows:[T('Simulation solver','toggle',true),T('Solver sim ticks','num',20,{unit:'ticks'}),T('Solver pitch sweep','num',25,{unit:'°'}),T('Max flight time','num',36000,{unit:'ticks'}),T('Abort on totem pops','toggle',true),T('Totem-pop limit','num',1),T('Logout on totem abort','toggle',true),T('Stall-recover after','num',100,{unit:'ticks'}),T('Max ground recoveries','num',3),T('Setback rocket-hold','num',15,{unit:'ticks'}),T('Flight input priority','num',5000)]},
  ]},

{ id:'trader', name:'Villager Trading', raw:'VillagerTrader', icon:'💰', cat:'control',
  tag:'Automated villager trade hall', status:'run', sdot:'run', enabled:true,
  metric:'3 trades active · 1,204 emeralds banked this run', hint:'Trade hall is stationary at base. Manage trades below.', station:'Trade hall · 120, 64, -30',
  actions:[['go','▶ Run trader'],['danger','■ Stop'],['','💾 Save']],
  list:{title:'Trades', kind:'trades', addLabel:'New trade', cols:['','TRADE','SOURCE','OUTPUT'], items:[
    {trade:[['emerald','book'],'enchanted_book'], n:'Enchanted Book — Mending', c:'Librarian · emerald + book', ench:['Mending'], patch:{}, tradeRef:{prof:'Librarian',give1:'minecraft:emerald',give2:'minecraft:book',get:'Enchanted Book',ench:'Mending'}},
    {trade:[['emerald'],'diamond_pickaxe'], n:'Diamond Pickaxe', c:'Toolsmith · emerald', patch:{}, tradeRef:{prof:'Toolsmith',give1:'minecraft:emerald',give2:'__none',get:'Enchanted Diamond Pickaxe'}},
    {trade:[['emerald','compass'],'filled_map'], n:'Ocean Explorer Map', c:'Cartographer · emerald + compass', patch:{}, tradeRef:{prof:'Cartographer',give1:'minecraft:emerald',give2:'minecraft:compass',get:'Ocean Explorer Map'}},
  ]},
  groups:[
    {title:'Trade', open:true, builder:'trade', rows:[T('Enabled','toggle',true)]},
    {title:'Per-trade limits', open:false, rows:[T('Max give-1 per trade','num',1),T('Max give-2 per trade','num',1)]},
    {title:'Chests', open:true, rows:[T('Give-1 supply chest','coord',[120,64,-30]),T('Give-2 supply chest','coord',[120,64,-31]),T('Output chest','coord',[122,64,-30])]},
    {title:'Restock & storage', open:false, rows:[T('Restock stacks','num',8),T('Restock threshold','num',64),T('Output store threshold','num',128),T('When output chest full','select','To overflow chest',{opts:STORE}),T('Overflow chest','coord',[124,64,-30])]},
    {title:'Result enchantments', open:false, kind:'ench', rows:[]},
  ]},

{ id:'pearl', name:'Pearl Stasis', raw:'PearlManager', icon:'🔮', cat:'control',
  tag:'Ender-pearl stasis loader', status:'idle', sdot:'warn', enabled:false,
  metric:'3 pearls stored · default “base” · player johannes', hint:'Pearl stasis chambers plotted below. Click one to load it.', pinKind:'pearl',
  actions:[['go','▶ Load pearl'],['','⤓ Drop'],['','💾 Save']],
  list:{title:'Pearl locations', kind:'pearls', addLabel:'Add pearl', cols:['','NAME','COORDS','DEFAULT'], items:[
    {n:'base', c:'142, 64, -2048', tag:'Default', tk:'ov', mx:56,my:28, patch:{}},
    {n:'stasis', c:'0, 120, 0', tag:'', mx:50,my:50, patch:{}},
    {n:'exit', c:'-880, 71, 1200', tag:'', mx:38,my:66, patch:{}},
  ]},
  groups:[
    {title:'Selected pearl', open:true, rows:[T('Name','text','base'),T('Coordinates','coord',[142,64,-2048])]},
    {title:'Player & defaults', open:true, rows:[T('Player name','text','johannes'),T('Default pearl','select','base',{opts:['base','stasis','exit']})]},
    {title:'Auto-load on join', open:false, rows:[T('Enabled','toggle',true),T('Return to start position','toggle',true),T('Drop pearl after load','toggle',false),T('Whitelist only','toggle',true)]},
    {title:'Auto-detect new pearls', open:false, rows:[T('Enabled','toggle',true),T('Temporary mode','toggle',false),T('Distance check','toggle',true),T('Temp removal range','num',8,{unit:'blocks'})]},
    {title:'Whitelist', open:false, kind:'chips', chips:['johannes','oldmanmango','goldfarm'], rows:[]},
  ]},

{ id:'stash', name:'Stash Manager', raw:'StashScanner', icon:'🛰️', cat:'control',
  tag:'Index & sort an owned stash', status:'busy', sdot:'busy', enabled:true,
  metric:'Indexing · 96 containers · 1,284 item types catalogued', hint:'Your stash containers, indexed and sorted. Click one to inspect contents.', pinKind:'stash',
  actions:[['go','▶ Re-index'],['','⇄ Sort & compact'],['','⤓ Export manifest']],
  list:{title:'Stash containers', kind:'stash', addLabel:null, cols:['','LOCATION','CONTENTS','KIND'], items:[
    {n:'1920, 64, -880', c:'double · red shulker', detail:'64× Golden Apple · 12× Totem of Undying', tag:'PvP', tk:'ov', mx:70,my:34},
    {n:'-2400, 12, 330', c:'single · blue shulker', detail:'2× Netherite Block · 30× Diamond', tag:'Storage', tk:'ne', mx:32,my:42},
    {n:'512, 70, 512', c:'double · purple shulker', detail:'full netherite kit · 8× XP Bottle', tag:'Gear', tk:'en', mx:60,my:64},
  ]},
  groups:[
    {title:'Scan area', open:true, rows:[T('Mode','select','Region scan',{opts:SCANMODE}),T('Dimension','select','Overworld',{opts:DIMS}),T('Origin','coord',[0,64,0]),T('Radius','num',900,{unit:'blocks'})]},
    {title:'Height band', open:false, rows:[T('Scan below origin','num',60,{unit:'blocks'}),T('Scan above origin','num',60,{unit:'blocks'})]},
    {title:'Behavior', open:false, rows:[T('Dry run (no chest ops)','toggle',false),T('Pause near players','toggle',true),T('Player pause range','num',48,{unit:'blocks'})]},
    {title:'Pacing', open:false, rows:[T('Settle ticks','num',6),T('Action delay','num',2,{unit:'ticks'}),T('Read delay','num',2,{unit:'ticks'}),T('Reach timeout','num',60,{unit:'ticks'})]},
  ]},

{ id:'miner', name:'Auto Miner', raw:'AquariusMiner', icon:'⛏️', cat:'control',
  tag:'Top-down quarry & ore search', status:'run', sdot:'run', enabled:true,
  metric:'Clearing area · layer 7 / 40 · 23 shulkers stored', hint:'Mining bounds shown on the map. Drag corners or edit below.', signature:'minerBounds',
  mapBox:{left:'34%',top:'40%',width:'18%',height:'16%',label:'quarry · 256×256'},
  actions:[['go','▶ Start mining'],['danger','■ Stop'],['','💾 Save']],
  groups:[
    {title:'Mining engine', open:true, builder:'ores', rows:[T('Enabled','toggle',true),T('What to mine','select','Clear area (quarry)',{opts:MINEMODE}),T('Tool / drops','select','Fortune (drops)',{opts:ORETOOL}),T('Auto-derive keep list from ores','toggle',true)]},
    {title:'Area & bounds', open:true, rows:[T('Area shape','select','Two corners',{opts:AREAMODE}),T('Anchor','select','Centered',{opts:ANCHOR}),T('Width','num',3,{unit:'chunks'}),T('Length','num',3,{unit:'chunks'}),T('Corner 1','coord',[200,0,200]),T('Corner 2','coord',[456,0,456]),T('Lowest Y','num',-59),T('Highest Y','num',16),T('Lowest ender-chest Y','num',-64)]},
    {title:'Haul — keep list', open:false, builder:'minerKeep', rows:[T('Only store when fully packed','toggle',true),T('Free slots before "full"','num',1)]},
    {title:'Drop junk', open:false, builder:'minerJunk', rows:[T('Drop junk while mining','toggle',true),T('Drop anything not kept','toggle',true),T('Junk-drop delay','num',10,{unit:'ticks'}),T('Also drop risky foods','toggle',true)]},
    {title:'Risky-food drop list', open:false, builder:'minerRisky', rows:[]},
    {title:'Storage cycle', open:false, rows:[T('Place-shulker fallback (no ender chest)','toggle',true),T('Break & carry the shulker','toggle',false),T('Silk-touch the field ender chest','toggle',true),T('Deposit click delay','num',2,{unit:'ticks'}),T('Storage step timeout','num',300,{unit:'ticks'}),T('Storage step settle','num',10,{unit:'ticks'}),T('Dry-run storage (diagnostic)','toggle',false)]},
    {title:'Base deposit trips', open:false, rows:[T('Haul to base chests','toggle',false),T('Refill empty shulkers','toggle',true),T('Empties per trip','num',6),T('Max deposit distance','num',1024,{unit:'blocks'})], extra:'minerChests'},
    {title:'Tool restock', open:false, rows:[T('Restock tools','toggle',false),T('Source container','text','ender_chest'),T('Restock below durability','num',60),T('Tool keyword','text','pickaxe'),T('Also restock a shovel','toggle',true)]},
    {title:'Food restock', open:false, rows:[T('Restock food','toggle',false),T('Min food on hand','num',1),T('Refill to count','num',64)]},
    {title:'Mining style', open:false, rows:[T('Legit (line-of-sight only)','toggle',true),T('Mining reach','num',2.5,{unit:'blocks'}),T('Sprint while mining','toggle',true)]},
    {title:'Drop collection', open:false, rows:[T('Sub-box size','num',8),T('Layer height','num',1),T('Vacuum pass for drops','toggle',true),T('Vacuum time cap','num',20,{unit:'seconds'})]},
    {title:'Cave movement', open:false, rows:[T('Relaxed cave profile','toggle',true),T('Max fall (no water)','num',20,{unit:'blocks'}),T('Bridge gaps mid-jump','toggle',true),T('Diagonal descend','toggle',true),T('Parkour jumps','toggle',false),T('Diagonal ascend','toggle',false)]},
    {title:'Pacing & verify', open:false, rows:[T('Step delay','num',4,{unit:'ticks'}),T('Max clear time per chunk','num',3600,{unit:'ticks'}),T('Verify clears','toggle',true),T('Clear retries','num',2)]},
    {title:'Safety & lifecycle', open:false, rows:[T('Pause near players','toggle',true),T('Player pause range','num',48,{unit:'blocks'}),T('Disconnect when run ends','toggle',false)]},
  ]},

{ id:'enchanter', name:'Auto Enchanter', raw:'Enchanter', icon:'📜', cat:'control',
  tag:'Anvil auto-enchant station', status:'idle', sdot:'warn', enabled:false,
  metric:'Station ready · plan est. 38 / 40 levels (feasible)', hint:'Anvil station at base. Pick variants & curses below.', station:'Anvil station · 44, 64, 18', signature:'enchStation',
  actions:[['go','▶ Run enchanter'],['danger','■ Stop'],['','💾 Save']],
  list:{title:'Enchant plan', kind:'plan', addLabel:null, cols:['','ITEM','ENCHANTS','COST'], items:[
    {n:'Netherite Sword', c:'Sharpness V · Mending · Unbreaking III', tag:'9 lvl', tk:'ov'},
    {n:'Netherite Sword', c:'+ Looting III · Fire Aspect II', tag:'7 lvl', tk:'ov'},
    {n:'Netherite Chestplate', c:'Protection IV · Mending', tag:'11 lvl', tk:'ov'},
    {n:'Diamond Pickaxe', c:'Efficiency V · Fortune III · Mending', tag:'11 lvl', tk:'ov'},
  ]},
  groups:[
    {title:'Discovery', open:true, rows:[T('Scan radius','num',16,{unit:'blocks'}),T('Scan below feet','num',4),T('Scan above feet','num',4)]},
    {title:'Enchantment choices', open:true, rows:[T('Sword damage','select','Sharpness',{opts:['Sharpness','Smite','Bane of Arthropods']}),T('Armor protection','select','Protection',{opts:['Protection','Blast Protection','Projectile Protection','Fire Protection']}),T('Tool speed','select','Efficiency',{opts:['Efficiency']}),T('Boots mobility','select','Depth Strider',{opts:['Depth Strider','Frost Walker']})]},
    {title:'Curses', open:false, rows:[T('Curse of Binding','toggle',false),T('Curse of Vanishing','toggle',false)]},
    {title:'XP bottles', open:false, rows:[T('Throw XP bottles','toggle',true),T('Keep buffer levels','num',5),T('Throw spacing','num',4,{unit:'ticks'}),T('Bottles in reserve','num',64)]},
    {title:'Limits & pacing', open:false, rows:[T('Max items (0 = all)','num',0),T('Action delay','num',2,{unit:'ticks'}),T('Anvil settle','num',6,{unit:'ticks'})]},
  ]},

{ id:'kitmaker', name:'Kit Builder', raw:'KitMaker', icon:'🎒', cat:'control',
  tag:'Template shulker kit filler', status:'idle', sdot:'warn', enabled:false,
  metric:'Kit “PvP” · 18 / 27 slots defined · 4 kits queued', hint:'Kit template defined at base. Edit slots & matching below.', station:'Kit room · 10, 64, 10', signature:'kitGrid',
  actions:[['go','▶ Build kits'],['danger','■ Stop'],['','💾 Save']],
  groups:[
    {title:'Template', open:true, rows:[T('Template chest (0=auto)','coord',[0,0,0]),T('Scan radius','num',8,{unit:'blocks'}),T('Floor band down','num',2),T('Floor band up','num',2)]},
    {title:'Item matching', open:true, rows:[T('Match strictness','select','Smart (ignore cosmetic)',{opts:MATCH}),T('Ignore enchant levels','toggle',true)]},
    {title:'Build run', open:false, rows:[T('Max kits (0 = unlimited)','num',0),T('Allow partial kits','toggle',false)]},
    {title:'Sources', open:false, rows:[T('Empty-shulker source','coord',[10,64,10]),T('Finished-kit chest','coord',[12,64,10])], extra:'kitSrcs'},
    {title:'Pacing', open:false, rows:[T('Settle ticks','num',6),T('Action delay','num',2,{unit:'ticks'}),T('Fill delay','num',2,{unit:'ticks'}),T('Place verify','num',4,{unit:'ticks'})]},
  ]},

{ id:'sniffer', name:'Packet Sniffer', raw:'AquariusSniffer', icon:'📡', cat:'control',
  tag:'Live packet inspector (debug)', status:'busy', sdot:'busy', enabled:true,
  metric:'Live · 1,284 packets buffered · filter “Chunk”', hint:'Live packet capture — independent of world position.', signature:'snifferStream',
  actions:[['go','▶ Start capture'],['warn','⏸ Pause'],['','⤓ Dump']],
  groups:[
    {title:'Capture', open:true, rows:[T('Capturing','toggle',true),T('Log live to console','toggle',true),T('Include full packet body','toggle',false)]},
    {title:'Filter', open:true, rows:[T('Direction','select','Both',{opts:SNIFDIR}),T('Scenario template','select','Chunks',{opts:['Everything','Movement','Chunks','Combat','Inventory']}),T('Name contains','text','Chunk')]},
    {title:'Buffer', open:false, rows:[T('Buffer size','num',2000,{unit:'lines'})]},
  ]},

/* ---------- UTILITY / safety modules (friendly names for ugly config keys) ---------- */
/* ---------- CONTROL surfaces (continued) ---------- */
/* ---------- CONTROL surfaces (continued) ---------- */
{ id:'highway', name:'Highway Builder', raw:'HighwayBuilder', icon:'🛣️', cat:'control',
  tag:'Auto nether-highway paver (clears tunnel + lays obsidian road)', status:'run', sdot:'run', enabled:true, metric:'Paving east · 1,240 blocks done · restock in 320',
  actions:[['go','▶ Start build'],['danger','■ Stop'],['','💾 Save']],
  groups:[
    {title:'Road', open:true, rows:[T('Enabled','toggle',true),T('Direction','select','East',{opts:['North','South','East','West','Northeast','Northwest','Southeast','Southwest']}),T('Width to each side','num',2,{unit:'blocks'}),T('Clear height','num',3,{unit:'blocks'}),T('Lay floor','toggle',true),T('Edge walls','toggle',false),T('Building block','text','obsidian'),T('Build at current Y','toggle',true),T('Fixed road Y','num',120),T('Stop after','num',0,{unit:'blocks (0=∞)'})]},
    {title:'Origin', open:false, rows:[T('Origin set','toggle',false),T('Origin X','num',0),T('Origin Z','num',0)]},
    {title:'Restock', open:false, rows:[T('From chests / barrels','toggle',true),T('From placed shulkers','toggle',true),T('Restock radius','num',16,{unit:'blocks'}),T('Vertical range','num',8,{unit:'blocks'})]},
    {title:'Advanced', open:false, rows:[T('Settle ticks','num',10),T('Max retries / block','num',4),T('Pause near players','toggle',false),T('Player pause range','num',48,{unit:'blocks'})]},
  ]},
{ id:'schematic', name:'Schematic Builder', raw:'Litematica', icon:'🏗️', cat:'control',
  tag:'Builds .litematic / .nbt schematics via Baritone', status:'idle', sdot:'warn', enabled:false, metric:'Loaded “spawn_base.litematic” · origin not set',
  actions:[['go','▶ Build'],['danger','■ Stop'],['','📂 Load file']],
  groups:[
    {title:'Schematic', open:true, rows:[T('Enabled','toggle',false),T('File','text','spawn_base.litematic'),T('Origin','coord',[0,64,0]),T('Origin set','toggle',false)]},
    {title:'Materials', open:true, rows:[T('Restock from chests','toggle',true),T('Restock from shulkers','toggle',true),T('Restock radius','num',16,{unit:'blocks'}),T('Vertical range','num',8,{unit:'blocks'})]},
    {title:'Advanced', open:false, rows:[T('Schematics folder','text','run/schematics'),T('Settle ticks','num',8),T('Max place retries','num',3),T('Pause near players','toggle',false),T('Player pause range','num',48,{unit:'blocks'})]},
  ]},
{ id:'boat', name:'Boat Autopilot', raw:'Boat', icon:'🚤', cat:'control',
  tag:'Open-water boat travel — command-driven (.boat goto x z)', status:'idle', sdot:'warn', enabled:false, metric:'Idle · no destination set',
  actions:[['go','▶ Go'],['danger','■ Stop']],
  groups:[
    {title:'Destination', open:true, rows:[T('Target X','num',12000),T('Target Z','num',-4000),T('Arrive radius','num',16,{unit:'blocks'})]},
    {title:'Note', open:false, rows:[T('This module is driven by the .boat command — it has no persistent config of its own.','toggle',false)]},
  ]},
{ id:'regear', name:'Auto Regear', raw:'Regear', icon:'🛡️', cat:'control',
  tag:'One-shot gear restock from an ender-chest kit shulker', status:'run', sdot:'run', enabled:true, metric:'Armed · full kit · last regear 2m ago',
  actions:[['go','▶ Regear now'],['','💾 Save']],
  groups:[
    {title:'Kit selection', open:true, rows:[T('Enabled','toggle',true),T('Kit profile','text',''),T('Kit shulker name','text','regear'),T('Match by color','toggle',false),T('Shulker color','text',''),T('Match by contents','toggle',false),T('Match by elytra count','toggle',false),T('Min elytras to match','num',9)]},
    {title:'After pulling', open:true, rows:[T('Equip best armor','toggle',true),T('Equip elytra (vs chestplate)','toggle',false),T('Totem to off-hand','toggle',true),T('Return emptied shulker','toggle',true)]},
    {title:'Spawn relocation', open:false, rows:[T('Self-kill to relocate','toggle',false),T('Min sky clearance','num',4,{unit:'blocks'}),T('Max relocate attempts','num',40),T('Stuck-path ticks','num',100),T('Kill-wait ticks','num',60)]},
    {title:'Advanced', open:false, rows:[T('Echest scan radius','num',48,{unit:'blocks'}),T('Ghost-hand interact','toggle',true),T('Ghost reach','num',6,{unit:'blocks'}),T('Disable when done','toggle',true),T('Pause near players','toggle',false),T('Player pause range','num',48,{unit:'blocks'}),T('Settle ticks','num',8),T('Action delay','num',3,{unit:'ticks'}),T('Step timeout','num',200,{unit:'ticks'})]},
  ]},
{ id:'orderfiller', name:'Order Filler', raw:'OrderFiller', icon:'📦', cat:'control',
  tag:'Payment-gated order picker (needs database + stash)', status:'idle', sdot:'warn', enabled:false, metric:'Idle · 0 paid orders queued',
  actions:[['go','▶ Start'],['danger','■ Stop'],['','💾 Save']],
  groups:[
    {title:'Orders', open:true, rows:[T('Enabled','toggle',false),T('Dimension','select','Overworld',{opts:DIMS}),T('Max shulkers / trip','num',18),T('Re-scan before fill','toggle',false),T('Allow partial fills','toggle',true),T('Soft-hold','num',15,{unit:'minutes'}),T('Manifest channel id','text','')]},
    {title:'Advanced', open:false, rows:[T('Pause near players','toggle',true),T('Player pause range','num',48,{unit:'blocks'}),T('Settle ticks','num',8),T('Action delay','num',5,{unit:'ticks'}),T('Fill delay','num',2,{unit:'ticks'}),T('Reach timeout','num',200,{unit:'ticks'})]},
  ]},
{ id:'pearldrop', name:'Pearl Drop', raw:'PearlDrop', icon:'🟣', cat:'control',
  tag:'Throws pearls into stasis chambers', status:'idle', sdot:'warn', enabled:false, metric:'Idle · 0 chambers found',
  actions:[['go','▶ Drop pearls'],['','🔍 Scan'],['','💾 Save']],
  groups:[
    {title:'Drop', open:true, rows:[T('Enabled','toggle',false),T('Pearls per chamber','num',1),T('Min water depth','num',5,{unit:'blocks'}),T('Close open trapdoor first','toggle',true)]},
    {title:'Scan', open:true, rows:[T('Scan radius','num',48,{unit:'blocks'}),T('Scan Y range','num',24,{unit:'± blocks'}),T('Coord tolerance','num',2,{unit:'blocks'})]},
    {title:'Advanced', open:false, rows:[T('Eye height','num',1.27),T('Input priority','num',5000),T('Center-stall ticks','num',6),T('Throw spacing','num',25,{unit:'ticks'}),T('Throw timeout','num',40,{unit:'ticks'}),T('Max throw retries','num',3),T('Retreat distance','num',1.0,{unit:'blocks'}),T('Path timeout','num',200,{unit:'ticks'}),T('Approach timeout','num',120,{unit:'ticks'}),T('Retreat timeout','num',80,{unit:'ticks'})]},
  ]},
{ id:'flightgear', name:'Flight Gear', raw:'FlightGear', icon:'🎽', cat:'control',
  tag:'Pre-flight gear-up — runs Regear with an elytra kit', status:'run', sdot:'run', enabled:true, metric:'Ready · fresh elytra equipped',
  actions:[['go','▶ Gear up']],
  groups:[
    {title:'Gear', open:true, rows:[T('Kit profile','text','flight'),T('Equip elytra (forces Regear equipElytra)','toggle',true)]},
    {title:'Note', open:false, rows:[T('Flight Gear is a thin wrapper over Auto Regear — its real options live on the Regear module.','toggle',false)]},
  ]},

/* ---------- COMBAT ---------- */
{ id:'killaura', name:'Combat Assist', raw:'KillAura', icon:'⚔️', cat:'combat',
  tag:'Auto-attacks nearby targets', status:'idle', sdot:'warn', enabled:false, metric:'Standby · no targets in range',
  actions:[['go','▶ Enable'],['','💾 Save']],
  groups:[
    {title:'Targets', open:true, rows:[T('Enabled','toggle',false),T('Players','toggle',false),T('Hostile mobs','toggle',true),T('Neutral mobs','toggle',false),T('Custom list','toggle',false),T('Only aggro neutrals','toggle',false),T('Only aggro hostiles','toggle',false)]},
    {title:'Weapon', open:true, rows:[T('Switch to best weapon','toggle',true),T('Weapon type','select','Any',{opts:['Any','Sword','Axe']}),T('Weapon material','select','Any',{opts:['Any','Netherite','Diamond']}),T('Target priority','select','Nearest',{opts:['None','Nearest']})]},
    {title:'Custom targets', open:false, kind:'chips', chips:['ender_dragon','wither','warden'], rows:[]},
    {title:'Advanced', open:false, rows:[T('Action priority','num',0),T('Attack delay','num',10,{unit:'ticks'}),T('TPS-sync attack rate','toggle',false),T('Raycast (require line of sight)','toggle',false),T('Target armor stands','toggle',false)]},
  ]},
{ id:'autobow', name:'Auto Bow', raw:'AutoBow', icon:'🏹', cat:'combat',
  tag:'Auto-draws & fires a bow / crossbow at the ranged band', status:'idle', sdot:'warn', enabled:false, metric:'Standby',
  actions:[['go','▶ Enable'],['','💾 Save']],
  groups:[
    {title:'Targets & range', open:true, rows:[T('Enabled','toggle',false),T('Hostile mobs','toggle',true),T('Players','toggle',false),T('Min range','num',4,{unit:'blocks'}),T('Max range','num',32,{unit:'blocks'}),T('Max vertical distance','num',6,{unit:'blocks'})]},
    {title:'Aiming', open:true, rows:[T('On-target tolerance','num',8.0,{unit:'°'}),T('Lead moving targets','toggle',true),T('Aim height fraction','num',0.6,{unit:'0–1'}),T('Require line of sight','toggle',true),T('Prefer crossbow','toggle',false)]},
    {title:'Movement', open:false, rows:[T('Stay mobile while engaging','toggle',true),T('Dodge incoming arrows','toggle',true)]},
    {title:'Advanced', open:false, rows:[T('Priority','num',0),T('Debug logging','toggle',false),T('Bow draw ticks','num',20),T('Crossbow charge ticks','num',25),T('TPS-sync charge','toggle',true),T('Post-shot cooldown','num',10,{unit:'ticks'})]},
  ]},
{ id:'autoarmor', name:'Auto Armor', raw:'AutoArmor', icon:'🦺', cat:'combat',
  tag:'Equips the best available armor', status:'run', sdot:'run', enabled:true, metric:'Full netherite equipped',
  actions:[['','💾 Save']],
  groups:[
    {title:'Armor', open:true, rows:[T('Enabled','toggle',true)]},
    {title:'Advanced', open:false, rows:[T('Input priority','num',0)]},
  ]},

/* ---------- SURVIVAL ---------- */
{ id:'autoeat', name:'Auto Eat', raw:'AutoEat', icon:'🍖', cat:'survival',
  tag:'Eats automatically when low on health / hunger', status:'run', sdot:'run', enabled:true, metric:'Idle · food 17 / 20',
  actions:[['','💾 Save']],
  groups:[
    {title:'Eating', open:true, rows:[T('Enabled','toggle',true),T('Eat below health','num',10,{unit:'♥'}),T('Eat below hunger','num',10,{unit:'/20'}),T('Food mode','select','All foods',{opts:['All foods','Blacklist','Whitelist']}),T('Allow unsafe food','toggle',false)]},
    {title:'Food list', open:true, builder:'foods', rows:[]},
    {title:'Fire protection', open:false, rows:[T('Eat gapple in lava','toggle',false),T('Require enchanted gapple','toggle',true)]},
    {title:'Alerts', open:false, rows:[T('Warn when out of food','toggle',true),T('Mention on warning','toggle',false),T('Input priority','num',0)]},
  ]},
{ id:'autototem', name:'Auto Totem', raw:'AutoTotem', icon:'✨', cat:'survival',
  tag:'Keeps a totem in your off-hand', status:'run', sdot:'run', enabled:true, metric:'Off-hand armed · 26 spare',
  actions:[['','💾 Save']],
  groups:[
    {title:'Totems', open:true, rows:[T('Enabled','toggle',true),T('Re-equip even while you play','toggle',false),T('Re-equip at health','num',20,{unit:'♥'})]},
    {title:'Alerts', open:false, rows:[T('Alert when out of totems','toggle',false),T('Mention on no-totems','toggle',false),T('Alert on totem pop','toggle',false),T('Mention on pop','toggle',false),T('Input priority','num',0)]},
  ]},
{ id:'autorespawn', name:'Auto Respawn', raw:'AutoRespawn', icon:'⚰️', cat:'survival',
  tag:'Respawns immediately on death', status:'run', sdot:'run', enabled:true, metric:'0 deaths this session',
  actions:[['','💾 Save']],
  groups:[
    {title:'Respawn', open:true, rows:[T('Enabled','toggle',true),T('Respawn delay','num',100,{unit:'ms'})]},
  ]},
{ id:'automend', name:'Auto Mend', raw:'AutoMend', icon:'🔧', cat:'survival',
  tag:'Repairs held gear with XP (Mending)', status:'idle', sdot:'warn', enabled:false, metric:'Idle',
  actions:[['','💾 Save']],
  groups:[
    {title:'Mending', open:true, rows:[T('Enabled','toggle',false)]},
    {title:'Advanced', open:false, rows:[T('Input priority','num',0)]},
  ]},
{ id:'autodisconnect', name:'Auto Disconnect', raw:'AutoDisconnect', icon:'🔌', cat:'survival',
  tag:'Logs out when things go wrong', status:'idle', sdot:'warn', enabled:false, metric:'Watching · no triggers active',
  actions:[['','💾 Save']],
  groups:[
    {title:'Triggers', open:true, rows:[T('Enabled','toggle',false),T('On low health','toggle',true),T('Health threshold','num',5,{unit:'♥'}),T('On thunderstorm','toggle',false),T('On totem pop','toggle',false),T('Min totems remaining','num',50),T('On unknown player in range','toggle',false)]},
    {title:'Behavior', open:false, rows:[T('While you are connected','toggle',false),T('Also disconnect your client','toggle',false),T('Cancel auto-reconnect','toggle',true),T('Mention on disconnect','toggle',false)]},
  ]},

/* ---------- CONNECTION ---------- */
{ id:'account', name:'Account & Login', raw:'Authentication', icon:'🔑', cat:'connection',
  tag:'Microsoft login, target server, and optional login proxy', status:'run', sdot:'run', enabled:true, metric:'Logged in · oldmanmango',
  actions:[['','💾 Save']],
  groups:[
    {title:'Account', open:true, rows:[T('Login method','select','Device code',{opts:['Device code','Device code (no device token)','Microsoft password','Prism launcher','Offline (cracked)']}),T('In-game name','text','oldmanmango'),T('Microsoft email','text','not@set.com'),T('Microsoft password','pass','S0largl0w!winter-87',{hint:'Only used for the password login method'}),T('Account has priority queue','toggle',false)]},
    {title:'Target server', open:true, rows:[T('Server address','text','connect.2b2t.org'),T('Port','num',25565)]},
    {title:'Login proxy', open:false, rows:[T('Route login through a proxy','toggle',false),T('Proxy type','select','SOCKS5',{opts:['SOCKS5','SOCKS4','HTTP']}),T('Host','text','127.0.0.1'),T('Port','num',7890),T('Proxy username','text',''),T('Proxy password','pass','')]},
    {title:'Token refresh', open:false, rows:[T('Auto-refresh auth token','toggle',true),T('Always refresh on login','toggle',false),T('Open browser on login','toggle',true),T('Max refresh interval','num',360,{unit:'minutes'}),T('Failed logins before cache wipe','num',2)]},
  ]},
{ id:'autoreconnect', name:'Auto Reconnect', raw:'AutoReconnect', icon:'🔁', cat:'connection',
  tag:'Rejoins after a disconnect', status:'run', sdot:'run', enabled:true, metric:'Standing by · 0 reconnects today',
  actions:[['','💾 Save']],
  groups:[
    {title:'Reconnect', open:true, rows:[T('Enabled','toggle',true),T('Delay before retry','num',5,{unit:'seconds'}),T('Max attempts','num',200)]},
  ]},
{ id:'antikick', name:'Anti-Kick', raw:'AntiKick', icon:'🪫', cat:'connection',
  tag:'Survives the 2b2t inactivity kick', status:'run', sdot:'run', enabled:true, metric:'Healthy',
  actions:[['','💾 Save']],
  groups:[
    {title:'Keep-alive', open:true, rows:[T('Enabled','toggle',false),T('Inactivity kick window','num',15,{unit:'minutes'}),T('Min walk distance','num',2,{unit:'blocks'})]},
  ]},
{ id:'antiafk', name:'Anti-AFK', raw:'AntiAFK', icon:'🕹️', cat:'connection',
  tag:'Performs small actions so you look active', status:'run', sdot:'run', enabled:true, metric:'Active',
  actions:[['','💾 Save']],
  groups:[
    {title:'Actions', open:true, rows:[T('Enabled','toggle',true),T('Walk','toggle',true),T('Walk distance','num',8,{unit:'blocks'}),T('Safe walk (avoid ledges)','toggle',true),T('Swing hand','toggle',true),T('Rotate','toggle',true),T('Jump','toggle',false),T('Jump only in water','toggle',true),T('Sneak','toggle',false)]},
    {title:'Advanced', open:false, rows:[T('Input priority','num',0),T('Walk delay','num',400,{unit:'ticks'}),T('Swing delay','num',3000,{unit:'ticks'}),T('Rotate delay','num',300,{unit:'ticks'}),T('Jump delay','num',1,{unit:'ticks'}),T('Sneak delay','num',200,{unit:'ticks'})]},
  ]},
{ id:'actionlimiter', name:'Action Limiter', raw:'ActionLimiter', icon:'⛔', cat:'connection',
  tag:'Locks down what the account may do (movement, interactions, illegal items)', status:'idle', sdot:'warn', enabled:false,
  metric:'Disabled · 13 illegal items listed · movement ≤ 1000 from home',
  actions:[['','💾 Save']],
  groups:[
    {title:'Limiter', open:true, rows:[T('Enabled','toggle',false),T('Exempt the proxy account','toggle',false)]},
    {title:'Allowed actions', open:true, rows:[T('Respawn','toggle',true),T('Movement','toggle',true),T('Open ender chest','toggle',true),T('Break blocks','toggle',true),T('Inventory actions','toggle',true),T('Use items','toggle',true),T('Sign books','toggle',true),T('Interact (right-click)','toggle',true),T('Outbound chat','toggle',true),T('Commands & whispers','toggle',true)]},
    {title:'Movement bounds', open:false, rows:[T('Home X','num',0),T('Home Z','num',0),T('Max distance from home','num',1000,{unit:'blocks'}),T('Minimum Y','num',-64)]},
    {title:'Illegal items', open:true, builder:'illegals', rows:[T('Enforce illegal-item list','toggle',false),T('On a blacklisted item','select','Allow holding, block use/place',{opts:['Allow holding, block use/place','Disconnect on possession']})]},
  ]},
{ id:'requeue', name:'Auto Re-queue', raw:'Requeue', icon:'🎟️', cat:'connection',
  tag:'Rejoins the server queue (handled with Auto Reconnect)', status:'idle', sdot:'warn', enabled:true, metric:'In game · queue not active',
  actions:[['','💾 Save']],
  groups:[
    {title:'Queue', open:true, rows:[T('Enabled','toggle',true),T('Re-queue on kick','toggle',true)]},
    {title:'Note', open:false, rows:[T('Re-queue shares its settings with Auto Reconnect — most options live there.','toggle',false)]},
  ]},
{ id:'queuewarn', name:'Queue Alert', raw:'QueueWarning', icon:'📣', cat:'connection',
  tag:'Alerts as you near the front of the queue', status:'run', sdot:'run', enabled:true, metric:'Armed · alerts at positions 1, 2, 3, 10',
  actions:[['','💾 Save']],
  groups:[
    {title:'Alert', open:true, rows:[T('Enabled','toggle',true)]},
    {title:'Warn at positions', open:true, kind:'chips', chips:['1','2','3','10'], rows:[]},
    {title:'Mention at positions', open:false, kind:'chips', chips:[], rows:[]},
  ]},
{ id:'activehours', name:'Active Hours', raw:'ActiveHours', icon:'⏰', cat:'connection',
  tag:'Only stays online during scheduled times', status:'idle', sdot:'warn', enabled:false, metric:'Off · online 24/7',
  actions:[['','💾 Save']],
  groups:[
    {title:'Schedule', open:true, rows:[T('Enabled','toggle',false),T('Time zone','text','Universal'),T('Force reconnect at start','toggle',false),T('Estimate queue ETA','toggle',true),T('Full session until next disconnect','toggle',true)]},
    {title:'Active windows', open:true, kind:'chips', chips:['08:00–23:00 Mon–Fri','10:00–02:00 Sat–Sun'], rows:[]},
  ]},
{ id:'sessionlimit', name:'Session Time Limit', raw:'SessionTimeLimit', icon:'⏳', cat:'connection',
  tag:'Warns/acts on the 2b2t session time limit', status:'run', sdot:'run', enabled:true, metric:'Tracking · session 6h 21m',
  actions:[['','💾 Save']],
  groups:[
    {title:'Limit', open:true, rows:[T('Enabled','toggle',true),T('Dynamic 2b2t session limit','toggle',true)]},
    {title:'In-game warn at positions', open:true, kind:'chips', chips:['1','2','3','4','5','6','7','8','9','10'], rows:[]},
    {title:'Discord notify positions', open:false, kind:'chips', chips:[], rows:[]},
    {title:'Discord mention positions', open:false, kind:'chips', chips:[], rows:[]},
  ]},

/* ---------- PRIVACY & SECURITY ---------- */
{ id:'coordprivacy', name:'Coordinate Privacy', raw:'CoordObfuscation', icon:'🕶️', cat:'privacy',
  tag:'Feeds the server fake coordinates so your real base stays hidden', status:'idle', sdot:'warn', enabled:false, metric:'Off · real coords sent to server',
  actions:[['','💾 Save']],
  groups:[
    {title:'Obfuscation', open:true, rows:[T('Enabled','toggle',false),T('Mode','select','Random offset',{opts:['Random offset','Constant offset','At location']}),T('Validate setup','toggle',true),T('Exempt the proxy account','toggle',false)]},
    {title:'What to obfuscate', open:true, rows:[T('Bedrock layer','toggle',true),T('Chunk heightmap','toggle',true),T('Chunk lighting','toggle',true),T('Biomes','toggle',false),T('Biome to fake','text','plains')]},
    {title:'Random offset', open:false, rows:[T('Min distance from self','num',100000,{unit:'blocks'}),T('Min distance from spawn','num',100000,{unit:'blocks'}),T('Max distance from spawn','num',29000000,{unit:'blocks'}),T('Regenerate after moving','num',64,{unit:'blocks'})]},
    {title:'Constant / At-location', open:false, rows:[T('Constant offset X','num',0),T('Constant offset Z','num',0),T('Nether-translate offset','toggle',true),T('Min spawn distance','num',100000,{unit:'blocks'}),T('At location X','num',0),T('At location Z','num',0)]},
    {title:'Safety', open:false, rows:[T('Disconnect if eye of ender thrown','toggle',true),T('Disconnect near offset blocks','toggle',true),T('Login delay after TP','num',1000,{unit:'ms'}),T('Debug packet log','toggle',false)]},
  ]},
{ id:'antileak', name:'Anti-Leak', raw:'AntiLeak', icon:'🛡️', cat:'privacy',
  tag:'Blocks chat that leaks numbers near your coordinates', status:'run', sdot:'run', enabled:true, metric:'Active · 0 leaks blocked today',
  actions:[['','💾 Save']],
  groups:[
    {title:'Filter', open:true, rows:[T('Enabled','toggle',true),T('Range check chat numbers','toggle',true),T('Range factor','num',10.0)]},
  ]},
{ id:'visualrange', name:'Visual Range Alerts', raw:'VisualRange', icon:'👁️', cat:'privacy',
  tag:'Alerts when players enter render distance', status:'run', sdot:'run', enabled:true, metric:'Clear · no players in range',
  actions:[['','💾 Save']],
  groups:[
    {title:'Alerts', open:true, rows:[T('Enabled','toggle',true),T('Ignore friends','toggle',false),T('Alert on enter','toggle',true),T('Mention on enter','toggle',true),T('Alert on leave','toggle',true),T('Alert on logout','toggle',true)]},
    {title:'Auto-whisper', open:false, rows:[T('Whisper on enter','toggle',false),T('Whisper message','text','Hello, I am using AquariusProxy!'),T('Whisper cooldown','num',30,{unit:'seconds'}),T('Whisper while you are connected','toggle',false)]},
    {title:'Recording', open:false, rows:[T('Record replay on enter','toggle',false),T('Record mode','select','Enemy',{opts:['Enemy','All']}),T('Record cooldown','num',5,{unit:'minutes'})]},
  ]},
{ id:'whispercontrol', name:'Whisper Control', raw:'WhisperControl', icon:'💬', cat:'privacy',
  tag:'Authorized players drive the bot by whisper (protect / come / goto / patrol / mine)', status:'run', sdot:'run', enabled:true, metric:'Listening · RBAC enabled',
  actions:[['','💾 Save']],
  groups:[
    {title:'Protect', open:true, rows:[T('Enabled','toggle',true),T('Follow radius','num',32,{unit:'blocks'}),T('Engage radius','num',2,{unit:'blocks'}),T('Roam / perimeter patrol','toggle',true),T('Wander radius','num',12,{unit:'blocks'}),T('Also enable Combat Assist','toggle',true),T('Also enable Auto Bow','toggle',true),T('Swap elytra → chestplate in combat','toggle',true)]},
    {title:'Come', open:false, rows:[T('Fly when farther than','num',200,{unit:'blocks'}),T('Use reported positions','toggle',true),T('Reported position max age','num',30,{unit:'seconds'})]},
    {title:'Patrol preset', open:false, rows:[T('Configured','toggle',false),T('Center','coord',[0,64,0]),T('Range','num',64,{unit:'blocks'})]},
    {title:'Mine preset', open:false, rows:[T('Configured','toggle',false),T('Corner 1 X','num',0),T('Corner 1 Z','num',0),T('Corner 2 X','num',0),T('Corner 2 Z','num',0),T('Min Y','num',-59),T('Max Y','num',-50)]},
  ]},
{ id:'bridge', name:'Proxy Bridge', raw:'Bridge', icon:'🌉', cat:'privacy',
  tag:'Links to the ProxyBridge client mod (waypoints + allow-listed commands)', status:'run', sdot:'run', enabled:true, metric:'Connected · client linked',
  actions:[['','💾 Save']],
  groups:[
    {title:'Bridge', open:true, rows:[T('Enabled','toggle',true),T('Publish PearlDrop chambers','toggle',true),T('Strip register channel','toggle',true),T('Broadcast to spectators','toggle',false)]},
    {title:'Commands the mod may invoke', open:true, kind:'chips', chips:['pearldrop','pd','pearlplus','swap'], rows:[]},
    {title:'Advanced', open:false, rows:[T('Publish interval','num',20,{unit:'ticks'})]},
  ]},

/* ---------- AUTOMATION ---------- */
{ id:'autofish', name:'Auto Fish', raw:'AutoFish', icon:'🎣', cat:'automation',
  tag:'Automatic AFK fishing', status:'idle', sdot:'warn', enabled:false, metric:'Idle · 0 catches',
  actions:[['go','▶ Start'],['danger','■ Stop'],['','💾 Save']],
  groups:[
    {title:'Fishing', open:true, rows:[T('Enabled','toggle',false),T('Look yaw','num',0.0,{unit:'°'}),T('Look pitch','num',0.0,{unit:'°'})]},
    {title:'Advanced', open:false, rows:[T('Input priority','num',0)]},
  ]},
{ id:'autodrop', name:'Auto Drop', raw:'AutoDrop', icon:'🗑️', cat:'automation',
  tag:'Drops unwanted items as they pile up', status:'run', sdot:'run', enabled:true, metric:'Active · 0 dropped this run',
  actions:[['','💾 Save']],
  groups:[
    {title:'Drop', open:true, rows:[T('Enabled','toggle',true),T('Mode','select','Whitelist',{opts:['Drop all','Blacklist','Whitelist']}),T('Drop whole stacks','toggle',true)]},
    {title:'Item list', open:true, builder:'dropjunk', rows:[]},
    {title:'Advanced', open:false, rows:[T('Input priority','num',0),T('Delay between drops','num',10,{unit:'ticks'}),T('Rotate before dropping','toggle',false),T('Drop yaw','num',0.0,{unit:'°'}),T('Drop pitch','num',0.0,{unit:'°'})]},
  ]},
{ id:'spammer', name:'Chat Broadcaster', raw:'Spammer', icon:'📢', cat:'automation',
  tag:'Posts chat / whisper messages on a timer', status:'idle', sdot:'warn', enabled:false, metric:'Off · 0 messages sent',
  actions:[['go','▶ Send now'],['','💾 Save']],
  groups:[
    {title:'Behavior', open:true, rows:[T('Enabled','toggle',false),T('Send as whisper','toggle',false),T('While you are connected','toggle',false),T('Interval','num',200,{unit:'ticks'}),T('Random order','toggle',false),T('Append random suffix','toggle',false)]},
    {title:'Messages', open:true, kind:'chips', chips:['AquariusProxy on top!','I just skipped queue thanks to AquariusProxy!','Download AquariusProxy on GitHub today - free!'], rows:[]},
  ]},
{ id:'autoreply', name:'Auto Reply', raw:'AutoReply', icon:'↩️', cat:'automation',
  tag:'Auto-replies to incoming whispers', status:'idle', sdot:'warn', enabled:false, metric:'Off',
  actions:[['','💾 Save']],
  groups:[
    {title:'Reply', open:true, rows:[T('Enabled','toggle',false),T('Message','text','I am currently AFK, check back later.'),T('Cooldown per player','num',15,{unit:'seconds'})]},
  ]},
{ id:'autoportal', name:'Auto Portal', raw:'AutoPortal', icon:'🌀', cat:'automation',
  tag:'Builds & lights a nether portal — command-driven (.portal build)', status:'idle', sdot:'warn', enabled:false, metric:'Idle',
  actions:[['go','▶ Build portal']],
  groups:[
    {title:'Portal', open:true, rows:[T('Build distance','num',2,{unit:'blocks'}),T('Light with','select','Flint and Steel',{opts:['Flint and Steel','Fire Charge']})]},
    {title:'Advanced', open:false, rows:[T('Place interval','num',3,{unit:'ticks'}),T('Build timeout','num',200,{unit:'ticks'})]},
  ]},
{ id:'autoomen', name:'Bad Omen', raw:'AutoOmen', icon:'🏴', cat:'automation',
  tag:'Manages Bad Omen / Ominous bottles for raids', status:'idle', sdot:'warn', enabled:false, metric:'Off · no omen active',
  actions:[['','💾 Save']],
  groups:[
    {title:'Omen', open:true, rows:[T('Enabled','toggle',false),T('While a raid is active','toggle',false),T('While an omen is active','toggle',false)]},
    {title:'Advanced', open:false, rows:[T('Input priority','num',0),T('Raid cooldown','num',1000,{unit:'ms'}),T('Omen cooldown','num',1000,{unit:'ms'})]},
  ]},
{ id:'chat', name:'Chat', raw:'Chat', icon:'🗨️', cat:'automation',
  tag:'Chat display, filtering, prefix/suffix & clickable links', status:'run', sdot:'run', enabled:true, metric:'Active',
  actions:[['','💾 Save']],
  groups:[
    {title:'Display', open:true, rows:[T('Enabled','toggle',true),T('Hide chat','toggle',false),T('Hide whispers','toggle',false),T('Hide death messages','toggle',false),T('Show connection messages','toggle',false),T('Hide 2b2t action-bar text','toggle',false),T('Insert clickable links','toggle',false)]},
    {title:'Prefix / suffix', open:false, rows:[T('Prefix your chats','toggle',false),T('Prefix','text','>'),T('Suffix your chats','toggle',false),T('Suffix','text','| Sent from my AquariusProxy'),T('Random suffix','toggle',false)]},
    {title:'Advanced', open:false, rows:[T('Replace 2b2t chat commands','toggle',false),T('Keep replacing while DB on','toggle',true)]},
  ]},

/* ---------- DIAGNOSTICS ---------- */
{ id:'replaymod', name:'Replay Recorder', raw:'ReplayMod', icon:'🎥', cat:'diagnostics',
  tag:'Records sessions to ReplayMod files', status:'idle', sdot:'warn', enabled:false, metric:'Not recording · 4 saved replays',
  actions:[['go','● Record'],['','📂 Open folder']],
  groups:[
    {title:'Recording', open:true, rows:[T('Auto-record','select','Off',{opts:['Off','When proxy connects','When player connects','On low health']}),T('Health threshold','num',5,{unit:'♥'}),T('Max recording time','num',0,{unit:'minutes (0=∞)'})]},
    {title:'Output', open:false, rows:[T('Send recordings to Discord','toggle',false),T('Upload if file too large','toggle',true),T('Feature flags','toggle',true)]},
  ]},
{ id:'spawnpatrol', name:'Spawn Patrol', raw:'SpawnPatrol', icon:'🧭', cat:'diagnostics',
  tag:'Patrols spawn and engages naked / fresh players', status:'idle', sdot:'warn', enabled:false, metric:'Idle',
  actions:[['go','▶ Patrol'],['danger','■ Stop'],['','💾 Save']],
  groups:[
    {title:'Patrol', open:true, rows:[T('Enabled','toggle',false),T('Nether','toggle',true),T('Goal','coord',[0,120,0]),T('Max patrol range','num',500,{unit:'blocks'})]},
    {title:'Targeting', open:true, rows:[T('Ignore friends','toggle',true),T('Only nakeds','toggle',true),T('Only at bedrock','toggle',false),T('Target attackers','toggle',true),T('Sticky targeting','toggle',true)]},
    {title:'Stuck-kill', open:false, rows:[T('Enabled','toggle',true),T('After seconds','num',60,{unit:'s'}),T('Min distance','num',10,{unit:'blocks'}),T('Anti-stuck','toggle',true)]},
    {title:'Ignore list', open:false, kind:'chips', chips:['johannes','oldmanmango'], rows:[T('Input priority','num',0)]},
  ]},
{ id:'discord', name:'Discord Notifications', raw:'Discord', icon:'🟦', cat:'diagnostics',
  tag:'Discord bot — status, alerts, chat relay', status:'run', sdot:'run', enabled:true, metric:'Connected · #bot-status',
  actions:[['','💾 Save']],
  groups:[
    {title:'Connection', open:true, rows:[T('Enabled','toggle',true),T('Bot token','pass','MTA4NzI5.Gx7q2v.kZ3rN9wL1pYf8sQ2dH6bV0cTmA'),T('Channel id','text','000000000000000000'),T('Command prefix','text','.'),T('Owner role id','text',''),T('Mention role id','text','')]},
    {title:'Reporting', open:true, rows:[T('Report coordinates','toggle',true),T('Client connection messages','toggle',true),T('Show non-whitelist login IP','toggle',true),T('Ignore other bots','toggle',true)]},
    {title:'Mention role on…', open:false, rows:[T('Connect','toggle',false),T('Disconnect','toggle',false),T('Player online','toggle',false),T('Death','toggle',false),T('Start queue','toggle',false),T('Server restart','toggle',false),T('Login failed','toggle',false)]},
    {title:'Profile management', open:false, rows:[T('Manage profile image','toggle',true),T('Manage nickname','toggle',true),T('Manage description','toggle',true),T('Manage presence','toggle',true)]},
    {title:'Chat relay', open:false, rows:[T('Enabled','toggle',false),T('Ignore queue','toggle',true),T('Mention on whisper','toggle',true),T('Mention on name mention','toggle',true),T('Relay while connected','toggle',false)]},
  ]},
];
const MAP = Object.fromEntries(MODULES.map(m=>[m.id, m]));
function statusWord(s){ return s==='run'?'running':s==='busy'?'working':'idle'; }
const CAT_LABEL = {control:'Control',combat:'Combat',survival:'Survival',connection:'Connection',privacy:'Privacy & security',automation:'Automation',diagnostics:'Diagnostics'};
function catLabel(c){ return CAT_LABEL[c]||c; }

/* ===========================================================================
   Villager Trade Builder — friendly item dropdowns gated by the REAL Java
   Edition villager trade catalog (all 13 professions, all 5 levels). Picking a
   profession limits the give/receive dropdowns to items that form an ACTUAL
   trade (profession -> inputs -> output cascade), so an impossible offer the
   bot would wait on forever can't be configured. Outputs are keyed by friendly
   display name, so the four Cartographer maps stay distinct despite sharing the
   filled_map id.  TC(profession, level, [[giveId,count],...], [getId,count,name?])
   =========================================================================== */
const _mc = id => id.indexOf(':')>=0 ? id : 'minecraft:'+id;
const TC = (p,l,g,o)=>({p,l,g:g.map(x=>[_mc(x[0]),x[1]]),o:[_mc(o[0]),o[1],o[2]]});
const TRADE_CATALOG = [
  TC('Farmer','Novice',[['wheat',20]],['emerald',1]),
  TC('Farmer','Novice',[['potato',26]],['emerald',1]),
  TC('Farmer','Novice',[['carrot',22]],['emerald',1]),
  TC('Farmer','Novice',[['beetroot',15]],['emerald',1]),
  TC('Farmer','Novice',[['emerald',1]],['bread',6]),
  TC('Farmer','Apprentice',[['pumpkin',6]],['emerald',1]),
  TC('Farmer','Apprentice',[['emerald',1]],['pumpkin_pie',4]),
  TC('Farmer','Apprentice',[['emerald',1]],['apple',4]),
  TC('Farmer','Journeyman',[['melon_slice',4]],['emerald',1]),
  TC('Farmer','Journeyman',[['emerald',3]],['cookie',18]),
  TC('Farmer','Expert',[['emerald',1]],['suspicious_stew',1]),
  TC('Farmer','Expert',[['emerald',1]],['cake',1]),
  TC('Farmer','Master',[['emerald',3]],['golden_carrot',3]),
  TC('Farmer','Master',[['emerald',4]],['glistering_melon_slice',3]),
  TC('Fisherman','Novice',[['string',20]],['emerald',1]),
  TC('Fisherman','Novice',[['coal',10]],['emerald',1]),
  TC('Fisherman','Novice',[['emerald',3]],['cod_bucket',1,'Bucket of Cod']),
  TC('Fisherman','Novice',[['cod',6],['emerald',1]],['cooked_cod',6]),
  TC('Fisherman','Apprentice',[['cod',15]],['emerald',1]),
  TC('Fisherman','Apprentice',[['emerald',2]],['campfire',1]),
  TC('Fisherman','Apprentice',[['salmon',6],['emerald',1]],['cooked_salmon',6]),
  TC('Fisherman','Journeyman',[['salmon',13]],['emerald',1]),
  TC('Fisherman','Journeyman',[['emerald','8-22']],['fishing_rod',1,'Enchanted Fishing Rod']),
  TC('Fisherman','Expert',[['tropical_fish',6]],['emerald',1]),
  TC('Fisherman','Master',[['pufferfish',4]],['emerald',1]),
  TC('Fisherman','Master',[['oak_boat',1]],['emerald',1]),
  TC('Shepherd','Novice',[['white_wool',18]],['emerald',1]),
  TC('Shepherd','Novice',[['brown_wool',18]],['emerald',1]),
  TC('Shepherd','Novice',[['black_wool',18]],['emerald',1]),
  TC('Shepherd','Novice',[['gray_wool',18]],['emerald',1]),
  TC('Shepherd','Novice',[['emerald',2]],['shears',1]),
  TC('Shepherd','Apprentice',[['white_dye',12]],['emerald',1]),
  TC('Shepherd','Apprentice',[['gray_dye',12]],['emerald',1]),
  TC('Shepherd','Apprentice',[['black_dye',12]],['emerald',1]),
  TC('Shepherd','Apprentice',[['light_blue_dye',12]],['emerald',1]),
  TC('Shepherd','Apprentice',[['lime_dye',12]],['emerald',1]),
  TC('Shepherd','Apprentice',[['emerald',1]],['white_wool',1,'Wool (any color)']),
  TC('Shepherd','Apprentice',[['emerald',1]],['white_carpet',4,'Carpet (any color)']),
  TC('Shepherd','Journeyman',[['yellow_dye',12]],['emerald',1]),
  TC('Shepherd','Journeyman',[['light_gray_dye',12]],['emerald',1]),
  TC('Shepherd','Journeyman',[['orange_dye',12]],['emerald',1]),
  TC('Shepherd','Journeyman',[['red_dye',12]],['emerald',1]),
  TC('Shepherd','Journeyman',[['pink_dye',12]],['emerald',1]),
  TC('Shepherd','Journeyman',[['emerald',3]],['red_bed',1,'Bed (any color)']),
  TC('Shepherd','Expert',[['brown_dye',12]],['emerald',1]),
  TC('Shepherd','Expert',[['purple_dye',12]],['emerald',1]),
  TC('Shepherd','Expert',[['blue_dye',12]],['emerald',1]),
  TC('Shepherd','Expert',[['green_dye',12]],['emerald',1]),
  TC('Shepherd','Expert',[['magenta_dye',12]],['emerald',1]),
  TC('Shepherd','Expert',[['cyan_dye',12]],['emerald',1]),
  TC('Shepherd','Expert',[['emerald',3]],['white_banner',1,'Banner (any color)']),
  TC('Shepherd','Master',[['emerald',2]],['painting',3]),
  TC('Fletcher','Novice',[['stick',32]],['emerald',1]),
  TC('Fletcher','Novice',[['emerald',1]],['arrow',16]),
  TC('Fletcher','Novice',[['gravel',10],['emerald',1]],['flint',10]),
  TC('Fletcher','Apprentice',[['flint',26]],['emerald',1]),
  TC('Fletcher','Apprentice',[['emerald',2]],['bow',1]),
  TC('Fletcher','Journeyman',[['string',14]],['emerald',1]),
  TC('Fletcher','Journeyman',[['emerald',3]],['crossbow',1]),
  TC('Fletcher','Expert',[['feather',24]],['emerald',1]),
  TC('Fletcher','Expert',[['emerald','7-21']],['bow',1,'Enchanted Bow']),
  TC('Fletcher','Master',[['tripwire_hook',8]],['emerald',1]),
  TC('Fletcher','Master',[['emerald','8-22']],['crossbow',1,'Enchanted Crossbow']),
  TC('Fletcher','Master',[['emerald',2],['arrow',5]],['tipped_arrow',5]),
  TC('Librarian','Novice',[['paper',24]],['emerald',1]),
  TC('Librarian','Novice',[['emerald',9]],['bookshelf',1]),
  TC('Librarian','Novice',[['emerald','5-64'],['book',1]],['enchanted_book',1]),
  TC('Librarian','Apprentice',[['book',4]],['emerald',1]),
  TC('Librarian','Apprentice',[['emerald',1]],['lantern',1]),
  TC('Librarian','Apprentice',[['emerald','5-64'],['book',1]],['enchanted_book',1]),
  TC('Librarian','Journeyman',[['ink_sac',5]],['emerald',1]),
  TC('Librarian','Journeyman',[['emerald',1]],['glass',4]),
  TC('Librarian','Journeyman',[['emerald','5-64'],['book',1]],['enchanted_book',1]),
  TC('Librarian','Expert',[['writable_book',2]],['emerald',1]),
  TC('Librarian','Expert',[['emerald',4]],['compass',1]),
  TC('Librarian','Expert',[['emerald',5]],['clock',1]),
  TC('Librarian','Expert',[['emerald','5-64'],['book',1]],['enchanted_book',1]),
  TC('Librarian','Master',[['emerald',3]],['red_candle',1]),
  TC('Librarian','Master',[['emerald',3]],['yellow_candle',1]),
  TC('Cartographer','Novice',[['paper',24]],['emerald',1]),
  TC('Cartographer','Novice',[['emerald',7]],['map',1,'Empty Map']),
  TC('Cartographer','Apprentice',[['glass_pane',11]],['emerald',1]),
  TC('Cartographer','Apprentice',[['emerald',8],['compass',1]],['filled_map',1,'Explorer Map']),
  TC('Cartographer','Journeyman',[['compass',1]],['emerald',1]),
  TC('Cartographer','Journeyman',[['emerald',13],['compass',1]],['filled_map',1,'Ocean Explorer Map']),
  TC('Cartographer','Journeyman',[['emerald',12],['compass',1]],['filled_map',1,'Trial Explorer Map']),
  TC('Cartographer','Expert',[['emerald',7]],['item_frame',1]),
  TC('Cartographer','Expert',[['emerald',3]],['white_banner',1,'Banner (any color)']),
  TC('Cartographer','Master',[['emerald',8]],['globe_banner_pattern',1]),
  TC('Cartographer','Master',[['emerald',14],['compass',1]],['filled_map',1,'Woodland Explorer Map']),
  TC('Cleric','Novice',[['rotten_flesh',32]],['emerald',1]),
  TC('Cleric','Novice',[['emerald',1]],['redstone',2]),
  TC('Cleric','Apprentice',[['gold_ingot',3]],['emerald',1]),
  TC('Cleric','Apprentice',[['emerald',1]],['lapis_lazuli',1]),
  TC('Cleric','Journeyman',[['rabbit_foot',2]],['emerald',1]),
  TC('Cleric','Journeyman',[['emerald',4]],['glowstone',1]),
  TC('Cleric','Expert',[['turtle_scute',4]],['emerald',1]),
  TC('Cleric','Expert',[['glass_bottle',9]],['emerald',1]),
  TC('Cleric','Expert',[['emerald',5]],['ender_pearl',1]),
  TC('Cleric','Master',[['nether_wart',22]],['emerald',1]),
  TC('Cleric','Master',[['emerald',3]],['experience_bottle',1,"Bottle o' Enchanting"]),
  TC('Armorer','Novice',[['coal',15]],['emerald',1]),
  TC('Armorer','Novice',[['emerald',5]],['iron_helmet',1]),
  TC('Armorer','Novice',[['emerald',9]],['iron_chestplate',1]),
  TC('Armorer','Novice',[['emerald',7]],['iron_leggings',1]),
  TC('Armorer','Novice',[['emerald',4]],['iron_boots',1]),
  TC('Armorer','Apprentice',[['iron_ingot',4]],['emerald',1]),
  TC('Armorer','Apprentice',[['emerald',36]],['bell',1]),
  TC('Armorer','Apprentice',[['emerald',3]],['chainmail_leggings',1]),
  TC('Armorer','Apprentice',[['emerald',1]],['chainmail_boots',1]),
  TC('Armorer','Journeyman',[['lava_bucket',1]],['emerald',1]),
  TC('Armorer','Journeyman',[['diamond',1]],['emerald',1]),
  TC('Armorer','Journeyman',[['emerald',1]],['chainmail_helmet',1]),
  TC('Armorer','Journeyman',[['emerald',4]],['chainmail_chestplate',1]),
  TC('Armorer','Journeyman',[['emerald',5]],['shield',1]),
  TC('Armorer','Expert',[['emerald','19-33']],['diamond_leggings',1,'Enchanted Diamond Leggings']),
  TC('Armorer','Expert',[['emerald','13-27']],['diamond_boots',1,'Enchanted Diamond Boots']),
  TC('Armorer','Master',[['emerald','13-27']],['diamond_helmet',1,'Enchanted Diamond Helmet']),
  TC('Armorer','Master',[['emerald','21-35']],['diamond_chestplate',1,'Enchanted Diamond Chestplate']),
  TC('Butcher','Novice',[['chicken',14]],['emerald',1]),
  TC('Butcher','Novice',[['rabbit',4]],['emerald',1]),
  TC('Butcher','Novice',[['porkchop',7]],['emerald',1]),
  TC('Butcher','Novice',[['emerald',1]],['rabbit_stew',1]),
  TC('Butcher','Apprentice',[['coal',15]],['emerald',1]),
  TC('Butcher','Apprentice',[['emerald',1]],['cooked_chicken',8]),
  TC('Butcher','Apprentice',[['emerald',1]],['cooked_porkchop',5]),
  TC('Butcher','Journeyman',[['beef',10]],['emerald',1]),
  TC('Butcher','Journeyman',[['mutton',7]],['emerald',1]),
  TC('Butcher','Expert',[['dried_kelp_block',10]],['emerald',1]),
  TC('Butcher','Master',[['sweet_berries',10]],['emerald',1]),
  TC('Leatherworker','Novice',[['leather',6]],['emerald',1]),
  TC('Leatherworker','Novice',[['emerald',3]],['leather_leggings',1,'Leather Pants']),
  TC('Leatherworker','Novice',[['emerald',7]],['leather_chestplate',1,'Leather Tunic']),
  TC('Leatherworker','Apprentice',[['flint',26]],['emerald',1]),
  TC('Leatherworker','Apprentice',[['emerald',5]],['leather_helmet',1,'Leather Cap']),
  TC('Leatherworker','Apprentice',[['emerald',4]],['leather_boots',1]),
  TC('Leatherworker','Journeyman',[['rabbit_hide',9]],['emerald',1]),
  TC('Leatherworker','Expert',[['turtle_scute',4]],['emerald',1]),
  TC('Leatherworker','Expert',[['emerald',6]],['leather_horse_armor',1]),
  TC('Leatherworker','Master',[['emerald',6]],['saddle',1]),
  TC('Mason','Novice',[['clay_ball',10]],['emerald',1]),
  TC('Mason','Novice',[['emerald',1]],['brick',10]),
  TC('Mason','Apprentice',[['stone',20]],['emerald',1]),
  TC('Mason','Apprentice',[['emerald',1]],['chiseled_stone_bricks',4]),
  TC('Mason','Journeyman',[['granite',16]],['emerald',1]),
  TC('Mason','Journeyman',[['andesite',16]],['emerald',1]),
  TC('Mason','Journeyman',[['diorite',16]],['emerald',1]),
  TC('Mason','Journeyman',[['emerald',1]],['dripstone_block',4]),
  TC('Mason','Journeyman',[['emerald',1]],['polished_andesite',4]),
  TC('Mason','Journeyman',[['emerald',1]],['polished_diorite',4]),
  TC('Mason','Journeyman',[['emerald',1]],['polished_granite',4]),
  TC('Mason','Expert',[['quartz',12]],['emerald',1]),
  TC('Mason','Expert',[['emerald',1]],['white_terracotta',1,'Dyed Terracotta']),
  TC('Mason','Expert',[['emerald',1]],['white_glazed_terracotta',1,'Glazed Terracotta']),
  TC('Mason','Master',[['emerald',1]],['quartz_pillar',1]),
  TC('Mason','Master',[['emerald',1]],['quartz_block',1,'Block of Quartz']),
  TC('Toolsmith','Novice',[['coal',15]],['emerald',1]),
  TC('Toolsmith','Novice',[['emerald',1]],['stone_axe',1]),
  TC('Toolsmith','Novice',[['emerald',1]],['stone_shovel',1]),
  TC('Toolsmith','Novice',[['emerald',1]],['stone_pickaxe',1]),
  TC('Toolsmith','Novice',[['emerald',1]],['stone_hoe',1]),
  TC('Toolsmith','Apprentice',[['iron_ingot',4]],['emerald',1]),
  TC('Toolsmith','Apprentice',[['emerald',36]],['bell',1]),
  TC('Toolsmith','Journeyman',[['flint',30]],['emerald',1]),
  TC('Toolsmith','Journeyman',[['emerald','6-20']],['iron_axe',1,'Enchanted Iron Axe']),
  TC('Toolsmith','Journeyman',[['emerald','7-21']],['iron_pickaxe',1,'Enchanted Iron Pickaxe']),
  TC('Toolsmith','Expert',[['diamond',1]],['emerald',1]),
  TC('Toolsmith','Expert',[['emerald','8-22']],['diamond_axe',1,'Enchanted Diamond Axe']),
  TC('Toolsmith','Expert',[['emerald','10-24']],['diamond_pickaxe',1,'Enchanted Diamond Pickaxe']),
  TC('Weaponsmith','Novice',[['coal',15]],['emerald',1]),
  TC('Weaponsmith','Novice',[['emerald',5]],['iron_sword',1]),
  TC('Weaponsmith','Apprentice',[['iron_ingot',4]],['emerald',1]),
  TC('Weaponsmith','Apprentice',[['emerald',36]],['bell',1]),
  TC('Weaponsmith','Journeyman',[['flint',24]],['emerald',1]),
  TC('Weaponsmith','Journeyman',[['emerald','9-23']],['iron_sword',1,'Enchanted Iron Sword']),
  TC('Weaponsmith','Expert',[['diamond',1]],['emerald',1]),
  TC('Weaponsmith','Expert',[['emerald','16-30']],['diamond_sword',1,'Enchanted Diamond Sword']),
];
const BOOK_ENCHANTS = ['Mending','Unbreaking III','Efficiency V','Fortune III','Silk Touch','Sharpness V',
  'Smite V','Bane of Arthropods V','Looting III','Sweeping Edge III','Fire Aspect II','Knockback II',
  'Protection IV','Blast Protection IV','Projectile Protection IV','Fire Protection IV','Feather Falling IV','Thorns III',
  'Power V','Punch II','Flame','Infinity','Respiration III','Aqua Affinity','Depth Strider III','Frost Walker II',
  'Soul Speed III','Swift Sneak III','Loyalty III','Impaling V','Riptide III','Channeling',
  'Multishot','Piercing IV','Quick Charge III','Luck of the Sea III','Lure III','Curse of Binding','Curse of Vanishing'];
const ABMTrade = {
  state:{prof:'Librarian', give1:'minecraft:emerald', give2:'minecraft:book', get:'Enchanted Book', bookEnch:'Mending'},
  _uniq(a){ const o=[]; a.forEach(x=>{ if(!o.includes(x)) o.push(x); }); return o; },
  _g2(e){ return e.g[1]?e.g[1][0]:'__none'; },
  outName(e){ return e.o[2]||pretty(e.o[0]); },
  _entries(f){ return TRADE_CATALOG.filter(e=>
       (!f.prof  || e.p===f.prof) &&
       (!f.give1 || e.g[0][0]===f.give1) &&
       (!f.give2 || this._g2(e)===f.give2) &&
       (!f.get   || this.outName(e)===f.get)); },
  profs(){ return this._uniq(TRADE_CATALOG.map(e=>e.p)); },
  opts(field){ const s=this.state;
    if(field==='give1') return this._uniq(this._entries({prof:s.prof}).map(e=>e.g[0][0]));
    if(field==='give2') return this._uniq(this._entries({prof:s.prof,give1:s.give1}).map(e=>this._g2(e)));
    if(field==='get')   return this._uniq(this._entries({prof:s.prof,give1:s.give1,give2:s.give2}).map(e=>this.outName(e)));
    return []; },
  match(){ return this._entries(this.state)[0]||null; },
  valid(){ return !!this.match(); },
  isBook(){ return this.state.get==='Enchanted Book'; },
  _repair(){ const s=this.state; let o;
    o=this.opts('give1'); if(!o.includes(s.give1)) s.give1=o[0];
    o=this.opts('give2'); if(!o.includes(s.give2)) s.give2=o[0];
    o=this.opts('get');   if(!o.includes(s.get))   s.get=o[0]; },
  set(f,v){ this.state[f]=v; this._repair(); this._render(); },
  load(ref){ this.state={prof:ref.prof,give1:ref.give1,give2:ref.give2||'__none',get:ref.get,bookEnch:ref.ench||'Mending'}; this._repair(); this._render(); },
  _render(){ const el=document.getElementById('tradeBox'); if(el) el.innerHTML=this.html(); },
  box(){ return `<div id="tradeBox">${this.html()}</div>`; },
  _ico(id){ return (id&&id!=='__none') ? icon(id,id,22) : `<span class="ii" style="opacity:.35;font-size:.7rem">∅</span>`; },
  _outIco(name){ const e=TRADE_CATALOG.find(x=>this.outName(x)===name); return e?icon(e.o[0],name,22):this._ico('__none'); },
  _selItem(field,val){ return `<select onchange="ABMTrade.set('${field}',this.value)">`+
      this.opts(field).map(o=>`<option value="${o}" ${o===val?'selected':''}>${o==='__none'?'— none —':pretty(o)}</option>`).join('')+`</select>`; },
  _selGet(val){ return `<select onchange="ABMTrade.set('get',this.value)">`+
      this.opts('get').map(o=>`<option ${o===val?'selected':''}>${o}</option>`).join('')+`</select>`; },
  _selProf(){ return `<select onchange="ABMTrade.set('prof',this.value)">`+
      this.profs().map(p=>`<option ${p===this.state.prof?'selected':''}>${p}</option>`).join('')+`</select>`; },
  html(){ const s=this.state, m=this.match(), ok=!!m, book=this.isBook();
    const bookSel=`<select onchange="ABMTrade.set('bookEnch',this.value)">`+BOOK_ENCHANTS.map(e=>`<option ${e===s.bookEnch?'selected':''}>${e}</option>`).join('')+`</select>`;
    const row=(ico,label,sub,ctrl)=>`<div class="tr">${ico}<div class="lb">${label}${sub?` <small>${sub}</small>`:''}</div>${ctrl}</div>`;
    const giveStr = m ? m.g.map(x=>`${x[1]}× ${pretty(x[0])}`).join(' + ') : '';
    const recv = book ? `Enchanted Book (${s.bookEnch})` : (m ? `${m.o[1]>1?m.o[1]+'× ':''}${this.outName(m)}` : '');
    const summary = ok ? `✓ ${s.prof} · ${m.l} — give ${giveStr}, receive ${recv}`
                       : `⚠ Not a valid villager trade — the bot would wait forever for this offer`;
    const sc = ok ? 'color:#3ddc97;border:1px solid #1f7a55;background:#3ddc9712'
                  : 'color:#ffb454;border:1px solid #5a3b1f;background:#ffb45412';
    return `<div class="tradeB">
        ${row('<span class="ti">🧑‍🌾</span>','Villager profession','',this._selProf())}
        ${row(this._ico(s.give1),'Item to give','',this._selItem('give1',s.give1))}
        ${row(this._ico(s.give2),'Second item','optional',this._selItem('give2',s.give2))}
        ${row(this._outIco(s.get),'Item to receive','',this._selGet(s.get))}
        ${book?row('<span class="ti">📖</span>','Book enchantment','librarian',bookSel):''}
        <div class="tstat" style="${sc}">${summary}</div>
      </div>`; },
};
/* one-time scoped CSS so the builder looks consistent in every theme */
(function(){ if(typeof document==='undefined'||!document.head) return;
  var st=document.createElement('style');
  st.textContent=".tradeB{display:flex;flex-direction:column;gap:.45rem}"+
    ".tradeB .tr{display:flex;align-items:center;gap:.6rem}"+
    ".tradeB .ti{width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;font-size:1rem;flex:none}"+
    ".tradeB .ii{width:24px;height:24px;flex:none}"+
    ".tradeB .lb{flex:1;font-size:.82rem}.tradeB .lb small{opacity:.5}"+
    ".tradeB select{font-family:var(--sans,sans-serif);font-size:.8rem;color:inherit;background:rgba(0,0,0,.34);"+
      "border:1px solid rgba(255,255,255,.16);border-radius:8px;padding:.4rem .5rem;min-width:172px;max-width:60%;cursor:pointer}"+
    ".tradeB select:focus{outline:none;border-color:#3ddc97}"+
    ".tradeB .tstat{font-family:var(--mono,monospace);font-size:.68rem;margin-top:.25rem;padding:.45rem .6rem;border-radius:8px;line-height:1.45}";
  document.head.appendChild(st);
})();

/* ===========================================================================
   Populated item-list picker (no typing): chips for what's selected + a
   pre-populated "+ add item" dropdown. Used by Action Limiter's illegal-items
   list; reusable for any item-set field via builder:'illegals'.
   =========================================================================== */
const ILLEGAL_ITEMS = ['command_block','chain_command_block','repeating_command_block','command_block_minecart',
  'barrier','structure_block','structure_void','jigsaw','light','spawner','trial_spawner','bedrock',
  'end_portal_frame','reinforced_deepslate','budding_amethyst','debug_stick','knowledge_book',
  'petrified_oak_slab','dragon_egg','farmland','dirt_path','frosted_ice','moving_piston','end_gateway'];
const FOOD_ITEMS = ['cooked_beef','cooked_porkchop','golden_carrot','bread','baked_potato','cooked_chicken',
  'cooked_mutton','cooked_cod','cooked_salmon','golden_apple','enchanted_golden_apple','sweet_berries',
  'melon_slice','apple','mushroom_stew','rabbit_stew','beetroot_soup','pumpkin_pie','dried_kelp'];
const JUNK_ITEMS = ['cobblestone','stone','dirt','gravel','andesite','diorite','granite','smooth_basalt','netherrack',
  'rotten_flesh','poisonous_potato','flint','stick','wheat_seeds','sand','tuff','cobbled_deepslate','kelp','seagrass'];
/* Auto Miner catalogs — ore search targets, the keep/haul list, and risky foods (all populated dropdowns) */
const ORE_TARGETS = ['coal_ore','deepslate_coal_ore','iron_ore','deepslate_iron_ore','copper_ore','deepslate_copper_ore',
  'gold_ore','deepslate_gold_ore','redstone_ore','deepslate_redstone_ore','lapis_ore','deepslate_lapis_ore',
  'diamond_ore','deepslate_diamond_ore','emerald_ore','deepslate_emerald_ore','nether_gold_ore','nether_quartz_ore','ancient_debris'];
const KEEP_ITEMS = ['cobbled_deepslate','tuff','deepslate','diamond','raw_iron','raw_gold','raw_copper','coal','redstone',
  'lapis_lazuli','emerald','quartz','ancient_debris','netherite_scrap','glowstone_dust','obsidian','gold_ingot'];
const RISKY_FOODS = ['rotten_flesh','spider_eye','poisonous_potato','pufferfish','chicken','chorus_fruit',
  'suspicious_stew','mushroom_stew','rabbit_stew','beetroot_soup'];
/* generic populated chip+dropdown picker — no typing; one instance per list field */
function mkPicker(name, catalog, sel, label){ return { name:name, catalog:catalog, sel:sel.slice(), label:label||'+ add item…',
  box(){ return '<div id="pick_'+this.name+'">'+this.html()+'</div>'; },
  _render(){ var el=document.getElementById('pick_'+this.name); if(el) el.innerHTML=this.html(); },
  add(id){ if(id && this.sel.indexOf(id)<0) this.sel.push(id); this._render(); },
  remove(id){ this.sel=this.sel.filter(function(x){return x!==id;}); this._render(); },
  html(){ var n=this.name, self=this;
    var chips=this.sel.map(function(id){ return '<span class="ilchip">'+icon(id,id,18)+pretty(id)+
      '<span class="x" onclick="ABMPickers[\''+n+'\'].remove(\''+id+'\')">×</span></span>'; }).join('');
    var avail=this.catalog.filter(function(id){ return self.sel.indexOf(id)<0; });
    var add='<select class="iladd" onchange="ABMPickers[\''+n+'\'].add(this.value);this.selectedIndex=0">'+
      '<option value="">'+this.label+'</option>'+
      avail.map(function(id){ return '<option value="'+id+'">'+pretty(id)+'</option>'; }).join('')+'</select>';
    return '<div class="illegals"><div class="ilchips">'+(chips||'<span class="ilempty">None selected</span>')+
      '</div>'+(avail.length?add:'')+'</div>';
  } }; }
const ABMPickers = {
  illegals: mkPicker('illegals', ILLEGAL_ITEMS, ['command_block','chain_command_block','repeating_command_block','command_block_minecart','barrier','structure_block','jigsaw','light','spawner','bedrock','end_portal_frame','debug_stick','knowledge_book'], '+ add illegal item…'),
  foods:    mkPicker('foods', FOOD_ITEMS, ['golden_carrot','cooked_beef','bread'], '+ add food…'),
  dropjunk: mkPicker('dropjunk', JUNK_ITEMS, ['cobblestone','dirt','gravel','netherrack','rotten_flesh'], '+ add junk item…'),
  ores:     mkPicker('ores', ORE_TARGETS, ORE_TARGETS.slice(), '+ add ore target…'),
  minerKeep:mkPicker('minerKeep', KEEP_ITEMS, ['cobbled_deepslate','tuff'], '+ add keep item…'),
  minerJunk:mkPicker('minerJunk', JUNK_ITEMS, ['cobblestone','stone','diorite','granite','andesite','gravel','dirt','smooth_basalt'], '+ add junk item…'),
  minerRisky:mkPicker('minerRisky', RISKY_FOODS, RISKY_FOODS.slice(), '+ add risky food…'),
};
const ABMBuilders = { trade:()=>ABMTrade.box(), illegals:()=>ABMPickers.illegals.box(),
  foods:()=>ABMPickers.foods.box(), dropjunk:()=>ABMPickers.dropjunk.box(),
  ores:()=>ABMPickers.ores.box(), minerKeep:()=>ABMPickers.minerKeep.box(),
  minerJunk:()=>ABMPickers.minerJunk.box(), minerRisky:()=>ABMPickers.minerRisky.box() };
(function(){ if(typeof document==='undefined'||!document.head) return;
  var st=document.createElement('style');
  st.textContent=".illegals{display:flex;flex-direction:column;gap:.55rem;width:100%;margin-top:.2rem}"+
    ".ilchips{display:flex;flex-wrap:wrap;gap:.4rem}"+
    ".ilchip{display:inline-flex;align-items:center;gap:.4rem;font-size:.76rem;border:1px solid rgba(255,255,255,.16);"+
      "background:rgba(255,255,255,.05);border-radius:8px;padding:.26rem .5rem .26rem .34rem}"+
    ".ilchip .ii{width:18px;height:18px;flex:none}"+
    ".ilchip .x{cursor:pointer;opacity:.5;font-weight:700;padding:0 .15rem;font-size:.95rem;line-height:1}"+
    ".ilchip .x:hover{opacity:1;color:#ff6b6b}"+
    ".ilempty{font-size:.76rem;opacity:.5;font-style:italic}"+
    ".iladd{font-family:var(--sans,sans-serif);font-size:.78rem;color:inherit;background:rgba(0,0,0,.34);"+
      "border:1px dashed rgba(255,255,255,.24);border-radius:8px;padding:.42rem .55rem;cursor:pointer;align-self:flex-start;min-width:210px}"+
    ".iladd:focus{outline:none;border-color:#3ddc97}";
  document.head.appendChild(st);
})();


/* =====================================================================
   ABMMap — scaled live 2D map component. Bot-centered, entity overlays,
   clickable waypoint pins, click-to-set-destination. Shared by the Live
   Map module in V1/V2/V3; mirrors the shipped ABM live viewer (follow/pan,
   overlays, 20 Hz SSE, per-bot port). The V4 page is its fullscreen view.
   ===================================================================== */
const ABMMap = {
  bot:{ name:'oldmanmango', x:-2480, y:71, z:15330, dim:'Overworld' },
  span:512,
  entities:[
    {t:'player', l:'Steve_', x:34, y:40},{t:'player', l:'xXEnderXx', x:71, y:28},
    {t:'mobH', l:'Zombie', x:44, y:58},{t:'mobH', l:'Skeleton', x:58, y:64},{t:'mobH', l:'Creeper', x:63, y:46},
    {t:'mobP', l:'Cow', x:40, y:67},{t:'mobP', l:'Sheep', x:36, y:60},
    {t:'item', l:'Diamond', x:52, y:54},{t:'item', l:'Iron', x:48, y:62},{t:'item', l:'Bones', x:60, y:57},
  ],
  pins:[
    {k:'trip',  ic:'🪂', l:'Stash run',     c:'-2480, 71, 15330', x:80, y:20, act:['▶ Fly here','💾 Save trip']},
    {k:'pearl', ic:'🔮', l:'Stasis · base',  c:'-2466, 64, 15344', x:30, y:30, act:['🔮 Load pearl','📍 Walk to']},
    {k:'stash', ic:'🛰️', l:'Found stash',    c:'-1980, 50, 15810', x:88, y:72, act:['🛰️ Route here','📋 Contents']},
    {k:'chest', ic:'📦', l:'Deposit chest',  c:'-2510, 64, 15300', x:20, y:74, act:['📦 Set deposit']},
  ],
  box(){
    var b=this.bot;
    var ents=this.entities.map(function(e){ return '<span class="amE amE-'+e.t+'" style="left:'+e.x+'%;top:'+e.y+'%" title="'+e.l+'"></span>'; }).join('');
    var pins=this.pins.map(function(p){ return '<div class="amPin amPin-'+p.k+'" style="left:'+p.x+'%;top:'+p.y+'%" data-label="'+p.l+'" data-coord="'+p.c+'" data-act="'+p.act.join('|')+'" onclick="abmMapPin(event,this)"><span class="amPinHead">'+p.ic+' '+p.l+'</span><span class="amPinStem"></span><span class="amPinDot"></span></div>'; }).join('');
    var grid='<svg viewBox="0 0 100 100" preserveAspectRatio="none">'+
      '<defs><pattern id="amg" width="8" height="8" patternUnits="userSpaceOnUse"><path d="M8 0H0V8" fill="none" stroke="#ffffff0e" stroke-width=".25"/></pattern></defs>'+
      '<rect width="100" height="100" fill="url(#amg)"/>'+
      '<line x1="50" y1="0" x2="50" y2="100" stroke="#ffffff16" stroke-width=".3"/>'+
      '<line x1="0" y1="50" x2="100" y2="50" stroke="#ffffff16" stroke-width=".3"/></svg>';
    return '<div class="abmmap">'+
      '<div class="amTop">'+
        '<span class="amLive"><i></i>LIVE · 20 Hz</span>'+
        '<span class="amPill">🌍 '+b.dim+'</span>'+
        '<span class="amPill amMono">⌖ '+b.x+', '+b.y+', '+b.z+'</span>'+
        '<span style="flex:1"></span>'+
        '<button class="amBtn on" onclick="abmMapFollow(this)">⌖ Following '+b.name+'</button>'+
        "<button class=\"amBtn\" onclick=\"window.open('control-v4-spatial-map.html','_blank')\">⛶ Fullscreen</button>"+
      '</div>'+
      '<div class="amCanvas" data-bx="'+b.x+'" data-bz="'+b.z+'" data-span="'+this.span+'" onclick="abmMapClick(event,this)">'+
        grid+ents+pins+
        '<div class="amBot"><span class="amRing"></span><span class="amCore"></span><span class="amBotNm">'+b.name+'</span></div>'+
        '<div class="amDest" style="display:none"></div>'+
        '<div class="amCompass">N</div>'+
        '<div class="amScale"><span class="amScaleLn"></span><span class="amScaleTx">'+this.span+' blk</span></div>'+
        '<div class="amClickHint">click map → set destination · click a pin → act</div>'+
      '</div>'+
      '<div class="amBottom">'+
        '<div class="amLegend">'+
          '<span><i class="amLd bot"></i>Bot</span><span><i class="amLd player"></i>Players 2</span>'+
          '<span><i class="amLd mobH"></i>Hostiles 3</span><span><i class="amLd mobP"></i>Passive 2</span>'+
          '<span><i class="amLd item"></i>Items 3</span><span><i class="amLd trip"></i>Waypoints '+this.pins.length+'</span>'+
        '</div>'+
        '<div class="amReadout">Tip: click anywhere on the map to drop an Elytra destination, or a pin to act on it.</div>'+
      '</div></div>';
  }
};
function abmMapFollow(btn){ var on=!btn.classList.contains('on'); btn.classList.toggle('on',on); btn.textContent=on?('⌖ Following '+ABMMap.bot.name):'✋ Free pan'; var box=btn.closest('.abmmap'); if(box){var cv=box.querySelector('.amCanvas'); if(cv) cv.classList.toggle('panning',!on);} }
function abmMapClick(ev,cv){ if(ev.target&&ev.target.closest&&ev.target.closest('.amPin'))return; var r=cv.getBoundingClientRect?cv.getBoundingClientRect():{left:0,top:0,width:1,height:1}; var px=(ev.clientX-r.left)/r.width, py=(ev.clientY-r.top)/r.height; px=Math.max(0,Math.min(1,px||.5)); py=Math.max(0,Math.min(1,py||.5)); var span=parseFloat(cv.dataset.span)||512; var wx=Math.round((parseFloat(cv.dataset.bx)||0)+(px-.5)*span), wz=Math.round((parseFloat(cv.dataset.bz)||0)+(py-.5)*span); var d=cv.querySelector('.amDest'); if(d){d.style.display='block';d.style.left=(px*100)+'%';d.style.top=(py*100)+'%';} var box=cv.closest('.abmmap'),ro=box&&box.querySelector('.amReadout'); if(ro) ro.innerHTML='<b>Destination</b> <span class="amMono">'+wx+', ~64, '+wz+'</span> <button class="amAct" onclick="abmMapSend(this)">▶ Send to Elytra</button><button class="amAct ghost" onclick="abmMapClear(this)">clear</button>'; }
function abmMapPin(ev,el){ if(ev&&ev.stopPropagation)ev.stopPropagation(); var box=el.closest('.abmmap'); if(box) box.querySelectorAll('.amPin').forEach(function(p){p.classList.remove('sel');}); el.classList.add('sel'); var acts=(el.dataset.act||'').split('|').filter(Boolean).map(function(a){return '<button class="amAct">'+a+'</button>';}).join(''); var ro=box&&box.querySelector('.amReadout'); if(ro) ro.innerHTML='<b>'+el.dataset.label+'</b> <span class="amMono">'+el.dataset.coord+'</span> '+acts; }
function abmMapSend(btn){ var ro=btn.closest('.amReadout'); if(ro) ro.innerHTML='✅ Destination handed to Elytra Autopilot — open that module to launch.'; }
function abmMapClear(btn){ var box=btn.closest('.abmmap'); if(!box)return; var d=box.querySelector('.amDest'); if(d)d.style.display='none'; var ro=box.querySelector('.amReadout'); if(ro) ro.innerHTML='Tip: click anywhere on the map to drop an Elytra destination, or a pin to act on it.'; }
(function(){ if(typeof document==='undefined'||!document.head) return;
  var st=document.createElement('style');
  st.textContent=
    ".abmmap{display:flex;flex-direction:column;gap:.55rem;width:100%}"+
    ".amTop{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap}"+
    ".amLive{display:inline-flex;align-items:center;gap:.35rem;font-family:var(--mono);font-size:.6rem;font-weight:700;letter-spacing:.05em;color:var(--acc);border:1px solid #1f7a55;border-radius:20px;padding:.2rem .55rem}"+
    ".amLive i{width:7px;height:7px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc);animation:amPulse 1.4s infinite}"+
    "@keyframes amPulse{0%,100%{opacity:1}50%{opacity:.25}}"+
    ".amPill{font-family:var(--mono);font-size:.62rem;color:var(--dim);border:1px solid var(--line);border-radius:20px;padding:.2rem .55rem}"+
    ".amMono{font-family:var(--mono)}"+
    ".amBtn{font-family:var(--sans,sans-serif);font-size:.7rem;font-weight:600;color:var(--dim);background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:9px;padding:.32rem .58rem;cursor:pointer}"+
    ".amBtn:hover{color:var(--txt);border-color:var(--acc)}"+
    ".amBtn.on{color:var(--acc);border-color:#1f7a55;background:#3ddc9714}"+
    ".amCanvas{position:relative;width:100%;height:320px;border:1px solid var(--line);border-radius:13px;overflow:hidden;cursor:crosshair;background:radial-gradient(900px 600px at 50% 50%,#0e1722,#070a0f 85%)}"+
    ".amCanvas.panning{cursor:grab}"+
    ".amCanvas svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}"+
    ".amE{position:absolute;width:9px;height:9px;border-radius:50%;transform:translate(-50%,-50%);border:1px solid #0b1018;pointer-events:none;z-index:3}"+
    ".amE-player{background:#5cc8ff;box-shadow:0 0 8px #5cc8ff}"+
    ".amE-mobH{background:#ff5d5d;box-shadow:0 0 7px #ff5d5d88}"+
    ".amE-mobP{background:#9fe089}"+
    ".amE-item{width:6px;height:6px;background:#ffb454;box-shadow:0 0 6px #ffb45488}"+
    ".amPin{position:absolute;transform:translate(-50%,-100%);cursor:pointer;z-index:6;text-align:center}"+
    ".amPin:hover{transform:translate(-50%,-100%) scale(1.06)}"+
    ".amPinHead{display:inline-flex;align-items:center;gap:.25rem;background:rgba(13,19,28,.85);border:1px solid var(--line);border-radius:8px;padding:.22rem .42rem;font-size:.66rem;font-weight:700;white-space:nowrap}"+
    ".amPinStem{display:block;width:2px;height:11px;margin:0 auto;background:var(--line)}"+
    ".amPinDot{display:block;width:10px;height:10px;border-radius:50%;margin:0 auto;border:2px solid #0b1018}"+
    ".amPin-trip .amPinDot{background:#5cc8ff}.amPin-pearl .amPinDot{background:#b98cff}.amPin-stash .amPinDot{background:#ffb454}.amPin-chest .amPinDot{background:#3ddc97}"+
    ".amPin.sel .amPinHead{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}"+
    ".amBot{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:5}"+
    ".amCore{display:block;width:13px;height:13px;border-radius:50%;background:var(--acc);box-shadow:0 0 16px var(--acc)}"+
    ".amRing{position:absolute;inset:-8px;border:1.5px solid #3ddc9766;border-radius:50%;animation:amRg 2.4s ease-out infinite}"+
    "@keyframes amRg{0%{transform:scale(.6);opacity:.9}100%{transform:scale(1.9);opacity:0}}"+
    ".amBotNm{position:absolute;left:16px;top:-4px;font-family:var(--mono);font-size:.58rem;color:var(--acc);white-space:nowrap}"+
    ".amDest{position:absolute;z-index:7;transform:translate(-50%,-50%);width:18px;height:18px;pointer-events:none}"+
    ".amDest::before,.amDest::after{content:'';position:absolute;background:var(--acc);box-shadow:0 0 6px var(--acc)}"+
    ".amDest::before{left:50%;top:0;width:1.5px;height:100%;transform:translateX(-50%)}"+
    ".amDest::after{top:50%;left:0;height:1.5px;width:100%;transform:translateY(-50%)}"+
    ".amScale{position:absolute;right:10px;bottom:9px;font-family:var(--mono);font-size:.56rem;color:var(--dim);display:flex;align-items:center;gap:.35rem}"+
    ".amScaleLn{display:inline-block;width:60px;height:5px;border:1px solid var(--dim);border-top:none}"+
    ".amCompass{position:absolute;left:10px;top:9px;font-family:var(--mono);font-size:.6rem;font-weight:700;color:var(--dim);border:1px solid var(--line);border-radius:50%;width:21px;height:21px;display:flex;align-items:center;justify-content:center}"+
    ".amClickHint{position:absolute;left:50%;bottom:9px;transform:translateX(-50%);font-family:var(--mono);font-size:.55rem;color:#5a6a7c;pointer-events:none}"+
    ".amBottom{display:flex;flex-direction:column;gap:.4rem}"+
    ".amLegend{display:flex;flex-wrap:wrap;gap:.7rem;font-size:.66rem;color:var(--dim)}"+
    ".amLegend span{display:inline-flex;align-items:center;gap:.32rem}"+
    ".amLd{width:9px;height:9px;border-radius:50%;display:inline-block}"+
    ".amLd.bot{background:var(--acc)}.amLd.player{background:#5cc8ff}.amLd.mobH{background:#ff5d5d}.amLd.mobP{background:#9fe089}.amLd.item{background:#ffb454}.amLd.trip{background:#5cc8ff}"+
    ".amReadout{font-family:var(--mono);font-size:.66rem;color:var(--dim);background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:9px;padding:.5rem .6rem;min-height:1.7rem;display:flex;align-items:center;flex-wrap:wrap;gap:.35rem}"+
    ".amReadout b{color:var(--txt);font-family:var(--sans,sans-serif)}"+
    ".amAct{font-family:var(--sans,sans-serif);font-size:.66rem;font-weight:600;color:var(--acc);background:#3ddc9714;border:1px solid #1f7a55;border-radius:7px;padding:.24rem .5rem;cursor:pointer}"+
    ".amAct.ghost{color:var(--dim);background:transparent;border-color:var(--line)}"+
    "@media(max-width:720px){.amCanvas{height:240px}}";
  document.head.appendChild(st);
})();


/* ---- password fields (Discord token, etc.) — masked with a reveal eye ---- */
function togglePass(el){ var w=el.parentNode; w.classList.toggle('show'); el.textContent=w.classList.contains('show')?'🙈':'👁'; }
/* ---- "mask coordinates" — obfuscate coordinate fields like a password ---- */
(function(){ if(typeof document==='undefined'||!document.head) return;
  var st=document.createElement('style');
  st.textContent='body.maskcoords .coord input,body.maskcoords [data-fid="tx"] input,body.maskcoords [data-fid="tz"] input'+
    '{-webkit-text-security:disc;text-security:disc;letter-spacing:.14em}'+
    '.passwrap{display:flex;align-items:center;gap:.35rem;width:100%}'+
    '.passwrap input{flex:1;min-width:0;-webkit-text-security:disc;text-security:disc;letter-spacing:.12em}'+
    '.passwrap.show input{-webkit-text-security:none;text-security:none;letter-spacing:0}'+
    '.passwrap .eye{cursor:pointer;opacity:.55;font-size:.85rem;user-select:none;flex:none}'+
    '.passwrap .eye:hover{opacity:1}';
  document.head.appendChild(st);
})();
if(typeof window!=='undefined' && window.addEventListener){
  window.addEventListener('DOMContentLoaded', function(){
    var b=document.createElement('button'); b.id='coordMaskBtn'; b.type='button'; b.textContent='🙈 Mask coordinates';
    b.style.cssText='position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:9999;font-family:inherit;'+
      'font-size:.74rem;font-weight:600;color:#cdd9e2;background:rgba(10,14,18,.85);backdrop-filter:blur(8px);'+
      'border:1px solid rgba(255,255,255,.18);border-radius:20px;padding:.42rem .85rem;cursor:pointer;box-shadow:0 8px 24px #0008';
    b.onclick=function(){ var on=document.body.classList.toggle('maskcoords');
      b.textContent=on?'👁 Reveal coordinates':'🙈 Mask coordinates';
      b.style.borderColor=on?'#3ddc97':'rgba(255,255,255,.18)'; b.style.color=on?'#3ddc97':'#cdd9e2'; };
    document.body.appendChild(b);
  });
}
