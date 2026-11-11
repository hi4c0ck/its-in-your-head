const fs = require('fs');
const SRC = fs.readFileSync(process.argv[2], 'utf8');
function grab(n){ const i=SRC.indexOf('function '+n+'('); if(i<0)throw new Error(n+' not found'); const j=SRC.indexOf('\n}',i); return SRC.slice(i,j+2); }
// PULSE_LANE_ORDER is a const in source; dominantLaneFor is self-contained.
const code = "const PULSE_LANE_ORDER = ['live','goal','good','friction'];\n" + grab('dominantLaneFor') + "\nreturn dominantLaneFor;";
const dominantLaneFor = new Function(code)();
const e=(t)=>({tool_name:t});
const CAP=3; // PULSE_TOP (rows shown per lane)
const rawLanes={
  live:    [e('terminal'),e('execute_code'),e('read_file')],
  goal:    [e('web_search'),e('github'),e('patch')],
  good:    [e('memory'),e('browser_exec'),e('vision_analyze')],
  friction:[e('write_file'),e('terminal'),e('read_file')],
};
let fail=0; const ok=(l,c)=>{ console.log((c?'  ok  - ':'  FAIL - ')+l); if(!c)fail++; };
const by={live:0,goal:0,good:0,friction:0,held:0};
const set=[]; for(const m of ['live','goal','good','friction']) for(const x of rawLanes[m]) if(!set.includes(x.tool_name)) set.push(x.tool_name);
console.log('MASSIVE BUBBLE-ROUTING SPREAD  (cap='+CAP+')');
for(const t of set){ const r=dominantLaneFor(t,rawLanes,CAP); by[r.idx<0?'held':r.metric]++;
  console.log('  '+t.padEnd(15)+' -> '+(r.idx<0?'HELD (no visible lane)':r.metric+' @row '+r.idx)); }
console.log('  spread by lane: '+JSON.stringify(by));
ok('goal-only tool -> goal', dominantLaneFor('web_search',rawLanes,CAP).metric==='goal');
ok('good-only tool -> good', dominantLaneFor('memory',rawLanes,CAP).metric==='good');
ok('friction-only tool -> friction', dominantLaneFor('write_file',rawLanes,CAP).metric==='friction');
ok('live-only tool -> live', dominantLaneFor('execute_code',rawLanes,CAP).metric==='live');
ok('terminal (live row0 + friction row1) -> live (smallest row)', dominantLaneFor('terminal',rawLanes,CAP).metric==='live');
ok('read_file (live row2 + friction row2) -> live (lane-order tie)', dominantLaneFor('read_file',rawLanes,CAP).metric==='live');
ok('github (goal row1) -> goal', dominantLaneFor('github',rawLanes,CAP).metric==='goal');
ok('SPREAD: goal+good+friction all receive whoop-ups', by.goal>0&&by.good>0&&by.friction>0);
const nv={live:[e('terminal')],goal:[e('web_search')],good:[e('memory')],friction:[e('write_file')]};
ok('ghost tool (no lane) -> HELD idx -1', dominantLaneFor('ghost_tool',nv,CAP).idx===-1);
const cp={live:[e('a'),e('b'),e('c'),e('terminal')],goal:[e('terminal')],good:[e('memory')],friction:[e('write_file')]};
ok('terminal hidden behind live cap but visible in goal -> goal', dominantLaneFor('terminal',cp,CAP).metric==='goal');
console.log('');
if(fail===0) console.log('PASS: whoop-ups spread to each tool\'s dominant lane — NOT all in live.');
else { console.log('FAIL: '+fail+' assertion(s).'); process.exit(1); }
