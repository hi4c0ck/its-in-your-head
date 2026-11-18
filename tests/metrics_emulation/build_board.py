"""Generate tests/metrics_emulation/board.html — a self-contained board.

Inlines the PRODUCTION portrait engine block from desktop/plugin.js byte-for-byte
(same recipe as tools/build_motion_tuner.py) and embeds the exported run JSON,
so the page works from file:// with no server and no fetches. Every portrait is
rendered by IYH_PORTRAIT.buildPortraitSvg — the real renderer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "desktop" / "plugin.js"
RUNS = Path(__file__).resolve().parent / "runs"
OUT = Path(__file__).resolve().parent / "board.html"
START = "// IYH-PORTRAIT-ENGINE-START"
END = "// IYH-PORTRAIT-ENGINE-END"

TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IYH · metrics emulation board</title>
<style>
:root{
  --fg:var(--foreground,#e8e6e0); --muted:var(--muted-foreground,#9a958c);
  --border:var(--border,#2a2d33); --accent:var(--accent,#d8c9a3);
  --card:var(--card,#14161a); --paper:#ebe5db; --ink:#26241f;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--card);color:var(--fg);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13px}
.app{display:flex;height:100%;min-height:0}
.rail{width:264px;flex:0 0 264px;border-right:1px solid var(--border);
  display:flex;flex-direction:column;min-height:0}
.rail h1{font-size:12px;letter-spacing:.12em;text-transform:uppercase;margin:0;
  padding:14px 14px 10px;border-bottom:1px solid var(--border);color:var(--accent)}
.rail .sub{padding:8px 14px;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)}
.list{overflow:auto;flex:1;min-height:0}
.run{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--border);
  background:none;color:var(--fg);font:inherit;padding:10px 12px;cursor:pointer}
.run:hover{background:rgba(255,255,255,.04)}
.run.active{background:rgba(216,201,163,.12);border-left:3px solid var(--accent);padding-left:9px}
.run .name{font-weight:700;font-size:12px;display:flex;gap:6px;align-items:center}
.run .name .stat{margin-left:auto;color:var(--muted);font-weight:500;font-size:10px}
.run .meta{color:var(--muted);font-size:10.5px;margin-top:3px;display:flex;gap:8px}
.run .ops{display:flex;gap:5px;margin-top:6px}
.run .ops button{border:1px solid var(--border);background:none;color:var(--muted);
  border-radius:4px;padding:2px 7px;font-size:10px;cursor:pointer}
.run .ops button:hover{color:var(--accent);border-color:var(--accent)}
.rail .foot{padding:9px 12px;border-top:1px solid var(--border);color:var(--muted);font-size:10px}
.main{flex:1;min-width:0;overflow:auto;padding:14px 18px 24px}
.head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.head h2{font-size:16px;margin:0}
.chip{border:1px solid var(--border);border-radius:999px;padding:2px 9px;font-size:10.5px;color:var(--muted)}
.band{position:relative;height:26px;margin:14px 0 8px;display:flex;border:1px solid var(--border);border-radius:4px;overflow:hidden}
.phase{position:relative;border-right:1px solid var(--border);display:flex;align-items:center;
  padding:0 6px;font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden}
.cornerdot{position:absolute;top:2px;width:5px;height:5px;border-radius:50%;background:var(--accent)}
.corners{display:flex;flex-wrap:wrap;gap:6px;padding-bottom:6px}
.corner{flex:0 0 auto;border:1px solid var(--border);border-radius:6px;padding:5px 8px;
  background:none;color:var(--muted);font:inherit;cursor:pointer;font-size:10px;min-width:96px}
.corner.active{border-color:var(--accent);color:var(--fg)}
.corner b{display:block;color:var(--fg);font-size:11px}
.stage{display:grid;grid-template-columns:minmax(220px,360px) 1fr;gap:16px;margin-top:10px}
@media(max-width:900px){.stage{grid-template-columns:1fr}}
.paper{background:var(--paper);border-radius:8px;padding:6px;aspect-ratio:720/760;overflow:hidden}
.paper svg{display:block;width:100%;height:100%}
.controls{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
.controls button{border:1px solid var(--border);background:none;color:var(--fg);
  border-radius:5px;padding:4px 12px;font:inherit;cursor:pointer}
.controls button:hover{border-color:var(--accent)}
.controls input[type=range]{flex:1;min-width:120px;accent-color:var(--accent)}
.readout{font-variant-numeric:tabular-nums;color:var(--muted);font-size:11px}
.metrics{border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.metrics h3{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
.ax{display:grid;grid-template-columns:120px 1fr 42px;gap:8px;align-items:center;margin-bottom:5px;font-size:11px}
.bar{height:7px;background:rgba(255,255,255,.06);border-radius:4px;position:relative;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);opacity:.85}
.bar u{position:absolute;top:0;bottom:0;width:2px;background:var(--fg);opacity:.5;text-decoration:none}
.ax .v{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.lanes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}
.lane{border:1px solid var(--border);border-radius:6px;padding:8px 10px}
.lane .k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.lane .row{display:flex;justify-content:space-between;gap:6px;font-size:10.5px;color:var(--muted);margin-top:3px}
.spark{margin-top:12px;height:38px;border:1px solid var(--border);border-radius:6px;padding:4px}
.spark svg{width:100%;height:100%;display:block}
.upset{margin-top:10px;display:flex;gap:14px;font-size:11px;color:var(--muted);flex-wrap:wrap}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <h1>IYH · emulation runs</h1>
    <div class="sub">Real engine · synthetic core prompts · GOAL board is synthetic; production replay is in tests/fixtures/production_operation_descriptors.json</div>
    <div class="list" id="list"></div>
    <div class="foot">Bookmark/hide are local to this page. Delete only hides a run; use the CLI <code>--purge</code> to remove files.</div>
  </aside>
  <main class="main" id="main"></main>
</div>

<script>__ENGINE__</script>
<script type="application/json" id="run-data">__DATA__</script>
<script>
(function(){
'use strict';
var RUNS=JSON.parse(document.getElementById('run-data').textContent);
var prefs={};
try{prefs=JSON.parse(localStorage.getItem('iyyh-emul-board')||'{}')}catch(e){prefs={}}
prefs.bm=prefs.bm||[]; prefs.hidden=prefs.hidden||[];
function save(){try{localStorage.setItem('iyyh-emul-board',JSON.stringify(prefs))}catch(e){}}
var key=function(r){return r.run.story+'-'+r.run.seed};
var current=null, playhead=0, timer=null;
var list=document.getElementById('list'), main=document.getElementById('main');
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function visible(){return RUNS.filter(function(r){return prefs.hidden.indexOf(key(r))<0})
  .sort(function(a,b){var x=prefs.bm.indexOf(key(a))>=0?0:1,y=prefs.bm.indexOf(key(b))>=0?0:1;return x-y})}
function renderList(){
  list.innerHTML=visible().map(function(r){
    var k=key(r),s=r.summary||{},span=Math.max.apply(null,Object.values(s.portrait_span||{}).map(function(v){return v||0}));
    var dr=Math.max.apply(null,Object.values(s.max_drift||{}).map(function(v){return v||0}));
    return '<button class="run '+(current&&key(current)===k?'active':'')+'" data-run="'+esc(k)+'">'+
      '<span class="name">'+esc(r.run.story)+' <span class="stat">'+(prefs.bm.indexOf(k)>=0?'★':'☆')+'</span></span>'+
      '<span class="meta"><span>◷ '+r.turns.length+'</span><span>↗ '+span.toFixed(3)+'</span><span>≈ '+dr.toFixed(3)+'</span><span>! '+(s.friction_peak||0).toFixed(2)+'</span></span>'+
      '<span class="ops"><button data-bm="'+esc(k)+'">'+(prefs.bm.indexOf(k)>=0?'unstar':'star')+'</button>'+
      '<button data-hide="'+esc(k)+'">delete</button></span></button>';
  }).join('')||'<div class="sub" style="padding:14px">No visible runs.</div>';
  Array.prototype.forEach.call(list.querySelectorAll('[data-run]'),function(b){
    b.addEventListener('click',function(e){
      var bm=e.target.getAttribute('data-bm'),hd=e.target.getAttribute('data-hide');
      if(bm){var i=prefs.bm.indexOf(bm);if(i>=0)prefs.bm.splice(i,1);else prefs.bm.push(bm);save();renderList();return}
      if(hd){prefs.hidden.push(hd);save();if(current&&key(current)===hd){current=null;main.innerHTML=''}renderList();return}
      select(b.getAttribute('data-run'));
    })
  });
}
function select(k){var r=RUNS.filter(function(x){return key(x)===k})[0];if(!r)return;
  current=r;playhead=r.corners.length?r.corners[0].turn:0;stop();renderList();renderMain()}
function stop(){if(timer){clearInterval(timer);timer=null}}
function cornerFor(i){var c=null;current.corners.forEach(function(x){if(x.turn<=i)c=x});return c}
function renderMain(){
  if(!current){main.innerHTML='<p>Select a run.</p>';return}
  var r=current,d=r.run.drawing,s=r.summary;
  var band=r.phases.map(function(p){var w=((p.to-p.from+1)/r.turns.length*100).toFixed(2);
    return '<div class="phase" style="width:'+w+'%" title="'+esc(p.label)+'">'+esc(p.label)+'</div>'}).join('');
  band+=r.corners.map(function(c){return '<i class="cornerdot" style="left:'+(c.turn/r.turns.length*100).toFixed(2)+'%"></i>'}).join('');
  main.innerHTML='<div class="head"><h2>'+esc(r.run.label)+'</h2>'+
    '<span class="chip">DRAWING '+esc(d.template)+'</span><span class="chip">Ink '+(d.ink?'on':'off')+'</span>'+
    '<span class="chip">animated '+(d.animated?'on':'off')+'</span><span class="chip">seed '+r.run.seed+'</span></div>'+
    '<div class="band">'+band+'</div>'+
    '<div class="corners">'+r.corners.map(function(c){return '<button class="corner" data-turn="'+c.turn+'"><b>'+esc(c.label)+'</b>turn '+c.turn+' · '+esc(c.phase)+'</button>'}).join('')+'</div>'+
    '<div class="stage"><div><div class="paper" id="paper"></div>'+
    '<div class="controls"><button id="prev">← step</button><button id="play">play</button>'+
    '<button id="next">step →</button><input type="range" id="seek" min="0" max="'+(r.turns.length-1)+'" value="'+playhead+'">'+
    '<span class="readout" id="ro"></span></div></div>'+
    '<div class="metrics"><h3>Metrics at playhead</h3><div id="axes"></div>'+
    '<div class="lanes" id="lanes"></div>'+
    '<div class="spark" id="spark"></div><div class="upset" id="upset"></div></div></div>';
  Array.prototype.forEach.call(main.querySelectorAll('.corner'),function(b){b.addEventListener('click',function(){
    playhead=Number(b.getAttribute('data-turn'));stop();draw()})});
  document.getElementById('prev').onclick=function(){playhead=Math.max(0,playhead-1);stop();draw()};
  document.getElementById('next').onclick=function(){playhead=Math.min(r.turns.length-1,playhead+1);stop();draw()};
  document.getElementById('play').onclick=function(){if(timer){stop();draw();return}
    timer=setInterval(function(){if(playhead>=r.turns.length-1){stop();draw();return}playhead++;draw()},850);draw()};
  document.getElementById('seek').oninput=function(e){playhead=Number(e.target.value);stop();draw()};
  draw();
}
function draw(){
  if(!current)return;
  var r=current,t=r.turns[playhead],d=r.run.drawing;
  var opts={template:d.template,rendering:'studio',id:'emul-'+playhead,clock:d.animated?playhead:0};
  if(d.ink)opts.graphite={primary:'#E8E2D5',secondary:'#B9BDC0',construction:'#8A939B',shade:'#C7C2B6'};
  var svg=IYH_PORTRAIT.buildPortraitSvg(t.portrait,opts);
  document.getElementById('paper').innerHTML=svg.replace('<svg ','<svg width="100%" height="100%" ');
  var c=cornerFor(playhead);
  document.getElementById('ro').textContent='turn '+playhead+' · '+esc(c?c.phase:t.phase)+' / '+r.turns.length;
  var dims=['directness','context_richness','exploration','analytical_depth','iteration','scope'];
  document.getElementById('axes').innerHTML=dims.map(function(dm){
    var v=t.portrait.dimensions[dm],cf=t.portrait.confidence[dm];
    var pct=Math.max(0,Math.min(100,((typeof v==='number'?v:0.5)*100)));
    return '<div class="ax"><span>'+dm.replace('_',' ')+'</span><span class="bar"><i style="width:'+pct.toFixed(1)+'%"></i>'+
      (typeof cf==='number'?'<u style="left:'+(cf*100).toFixed(1)+'%"></u>':'')+'</span>'+
      '<span class="v">'+(typeof v==='number'?v.toFixed(3):'—')+'</span></div>'}).join('');
  document.getElementById('lanes').innerHTML=['live','goal','good','friction'].map(function(m){
    var es=t.pulse.lanes[m]||[];
    return '<div class="lane"><div class="k">'+m+'</div>'+(es.length?es.map(function(e){
      return '<div class="row"><span>'+esc(e.tool_name||'—')+'</span><span>'+
        (typeof e.value==='number'?e.value.toFixed(3):'—')+' '+(e.level||'')+'</span></div>'}).join(''):'<div class="row">empty</div>')+'</div>'}).join('');
  var ser=t.drift.series||{},pts=(ser.directness||[]).slice(-24),w=300,h=30;
  var path=pts.map(function(v,i){return (i/Math.max(1,pts.length-1)*w).toFixed(1)+','+((1-v)*h).toFixed(1)}).join(' ');
  document.getElementById('spark').innerHTML='<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="'+path+'"/></svg>';
  var u=t.pulse.upset||{};
  document.getElementById('upset').innerHTML='<span>F′ '+esc(typeof u.value==='number'?u.value.toFixed(4):'—')+'</span>'+
    '<span>tier '+esc(u.tier)+'</span><span>mood '+esc(typeof u.mood==='number'?u.mood.toFixed(4):'—')+'</span>'+
    '<span>pulse rows '+t.pulse.rows+'</span>';
  Array.prototype.forEach.call(main.querySelectorAll('.corner'),function(b){
    b.className='corner'+(Number(b.getAttribute('data-turn'))===playhead?' active':'')});
  var seek=document.getElementById('seek');if(seek)seek.value=playhead;
}
renderList();if(visible().length)select(key(visible()[0]));
})();
</script>
</body>
</html>
'''


def build(out: Path = OUT, runs_dir: Path = RUNS) -> Path:
    src = PLUGIN.read_text(encoding="utf-8")
    a = src.index(START) + len(START)
    b = src.index(END)
    engine_block = src[a:b]
    sha = hashlib.sha256(engine_block.encode("utf-8")).hexdigest()[:12]
    runs = []
    for f in sorted(runs_dir.glob("*.json")):
        try:
            runs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    data = json.dumps(runs, separators=(",", ":"), ensure_ascii=False)
    # Avoid premature </script> termination from any embedded string.
    data = data.replace("</", "<\\/")
    html = (TEMPLATE.replace("__ENGINE__", engine_block)
            .replace("__DATA__", data)
            .replace("</html>", "<!-- portrait engine sha12: " + sha + " -->\n</html>"))
    out.write_text(html, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate the self-contained metrics board")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--runs", type=Path, default=RUNS)
    a = ap.parse_args()
    p = build(a.out, a.runs)
    print("wrote %s (%d bytes)" % (p, p.stat().st_size))


if __name__ == "__main__":
    main()
