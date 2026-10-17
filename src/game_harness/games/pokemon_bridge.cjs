/* Pinned native Pokemon Showdown rules. No Player, bot, planner or default choice. */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const readline = require('readline');
const {execFileSync} = require('child_process');
const ROOT = path.resolve(__dirname, '../../..');
const SOURCE = path.join(ROOT, '.runtime/pokemon-showdown');
const COMMIT = '7244fe8e28fa8bc4d0809ae30d183c608b62bb2b';
let Battle, Dex, extractChannelMessages;
let engine, requests, events, pending, needed, revision, maxSteps, candidateSeat;
let cutoff = false;

function dependencies() {
  const actual = execFileSync('git', ['-C', SOURCE, 'rev-parse', 'HEAD'], {encoding:'utf8', stdio:['ignore','pipe','ignore']}).trim();
  if (actual !== COMMIT) throw new Error('provenance');
  ({Battle, Dex} = require(path.join(SOURCE, 'dist/sim')));
  ({extractChannelMessages} = require(path.join(SOURCE, 'dist/sim/battle')));
}

// Environment fixtures, not policies. Party composition is never given to the
// opposing player or spectator until native protocol reveals the Pokemon.
const ROSTER = [
  ['Charizard', ['Flamethrower','Wing Attack','Earthquake','Protect']],
  ['Blastoise', ['Surf','Ice Beam','Bite','Protect']],
  ['Venusaur', ['Razor Leaf','Sludge Bomb','Sleep Powder','Protect']],
  ['Raichu', ['Thunderbolt','Quick Attack','Iron Tail','Protect']],
  ['Golem', ['Earthquake','Rock Slide','Double-Edge','Protect']],
  ['Alakazam', ['Psychic','Shadow Ball','Calm Mind','Recover']],
];
function setup(seed) {
  const digest = crypto.createHash('sha256').update('pokemon-gen3-fixture-v1:' + seed).digest();
  const pool = ROSTER.map((row,i)=>({row, rank:crypto.createHash('sha256').update(digest).update(String(i)).digest('hex')}));
  pool.sort((a,b)=>a.rank.localeCompare(b.rank));
  const teams = [pool.slice(0,3),pool.slice(3)].map(rows=>rows.map(({row})=>({
    name:row[0], species:row[0], moves:row[1], level:50, gender:'M', nature:'Serious',
    ability:Dex.mod('gen3').species.get(row[0]).abilities['0'], item:'',
    evs:{hp:0,atk:0,def:0,spa:0,spd:0,spe:0}, ivs:{hp:31,atk:31,def:31,spa:31,spd:31,spe:31},
  })));
  return {teams, prng:[0,2,4,6].map(i=>digest.readUInt16BE(i))};
}
function receive(type, data) {
  if (type === 'update') {
    const split = extractChannelMessages(Array.isArray(data) ? data.join('\n') : data, [0,1,2]);
    // Native timestamps are transport metadata, not game evidence or PRNG state.
    const clean = lines=>lines.filter(line=>line && !line.startsWith('|t:|'));
    events.push({public:clean(split[0]), p1:clean(split[1]), p2:clean(split[2])});
  } else if (type === 'sideupdate') {
    const text = Array.isArray(data) ? data.join('\n') : data;
    const newline = text.indexOf('\n');
    const side = text.slice(0,newline), message = text.slice(newline+1);
    if (message.startsWith('|request|')) requests[side] = JSON.parse(message.slice(9));
    else if (message.startsWith('|error|')) throw new Error('native_choice');
  }
  // Deliberately ignore native `end`: it contains full private teams and seed.
}
function actionable() {
  if (engine.ended || cutoff) return [];
  return ['p1','p2'].filter(side=>requests[side] && !requests[side].wait);
}
function result() {
  const winner = engine.ended && engine.winner ? (engine.winner === 'Seat 1' ? 0 : 1) : null;
  const status = cutoff ? 'unresolved' : !engine.ended ? 'ongoing' : winner === null ? 'draw' : winner === candidateSeat ? 'win' : 'loss';
  const out = {revision, native_turn:engine.turn, done:engine.ended || cutoff,
    native_terminal:engine.ended, truncated:cutoff, winner, result:status,
    reason:cutoff ? 'step_limit' : engine.ended ? winner === null ? 'native_tie' : 'native_win' : null,
    score:status === 'win' ? 100 : status === 'loss' ? 0 : 50,
    actor:needed.find(side=>!Object.hasOwn(pending,side)) || null,
    requests, events};
  events=[];
  return out;
}
function init(payload) {
  if (engine) throw new Error('already_initialized');
  if (!Number.isSafeInteger(payload.seed) || payload.seed < 0) throw new Error('seed');
  if (!Number.isInteger(payload.max_steps) || payload.max_steps < 1 || payload.max_steps > 2000) throw new Error('budget');
  dependencies(); requests={}; events=[]; pending={}; revision=0; cutoff=false;
  maxSteps=payload.max_steps; candidateSeat=payload.seed % 2;
  const {teams,prng}=setup(payload.seed);
  const format = {...Dex.formats.get('gen3customgame'), debug:false,
    ruleset:['HP Percentage Mod','Cancel Mod','Max Team Size = 3','Max Move Count = 4','Max Level = 50','Default Level = 50']};
  engine=new Battle({format, seed:prng, strictChoices:true, send:receive});
  engine.setPlayer('p1',{name:'Seat 1',team:teams[0]});
  engine.setPlayer('p2',{name:'Seat 2',team:teams[1]});
  engine.sendUpdates();
  needed=actionable();
  if (!needed.length) throw new Error('no_initial_request');
  return result();
}
function legalCommands(request) {
  if (!request || request.wait) return [];
  if (request.teamPreview) throw new Error('unexpected_preview');
  const force = Boolean(request.forceSwitch && request.forceSwitch[0]);
  const active = request.active && request.active[0];
  const commands=[];
  if (!force && active) {
    for (let i=0;i<active.moves.length;i++) {
      const move=active.moves[i];
      if (!move.disabled && (move.pp > 0 || move.id === 'struggle' || move.id === 'recharge')) commands.push(`move ${i+1}`);
    }
  }
  if (force || !active || !active.trapped) {
    for (let i=0;i<request.side.pokemon.length;i++) {
      const p=request.side.pokemon[i];
      if (!p.active && !p.condition.includes('fnt')) commands.push(`switch ${i+1}`);
    }
  }
  return commands;
}
function step(payload) {
  if (!engine || engine.ended || cutoff) throw new Error('ended');
  if (payload.expected_revision !== revision) throw new Error('revision');
  const actor=needed.find(side=>!Object.hasOwn(pending,side));
  if (payload.actor !== actor || !legalCommands(requests[actor]).includes(payload.command)) throw new Error('action');
  pending[actor]=payload.command; revision++;
  if (needed.every(side=>Object.hasOwn(pending,side))) {
    const submitting = pending; pending={};
    // Native simultaneous resolution occurs only after every required model choice.
    for (const side of needed) if (!engine.choose(side, submitting[side])) throw new Error('native_choice');
    engine.sendUpdates(); needed=actionable();
    if (!engine.ended && !needed.length) throw new Error('missing_request');
  }
  if (!engine.ended && revision >= maxSteps) { cutoff=true; pending={}; needed=[]; }
  return result();
}
const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
lines.on('line', line=>{
  let payload;
  try {
    payload=JSON.parse(line);
    const value=payload.op === 'init' ? init(payload) : payload.op === 'step' ? step(payload) : (()=>{throw new Error('operation');})();
    process.stdout.write(JSON.stringify({id:payload.id,ok:true,value})+'\n');
  } catch(error) {
    process.stdout.write(JSON.stringify({id:payload && payload.id,ok:false,error_code:String(error.message)})+'\n');
  }
});
