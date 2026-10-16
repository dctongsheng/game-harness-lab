import type { Action, GameAdapter, GameState, GameView, Piece, Seat, Viewer } from '@decision-arena/core';
import { other, randomFor, record, requireTurn, same, shuffle } from './common';

type Kind = 'commander' | 'general' | 'division' | 'brigade' | 'regiment' | 'battalion' | 'company' | 'platoon' | 'engineer' | 'mine' | 'bomb' | 'flag';
interface ArmyPiece { id: string; seat: Seat; kind: Kind; moved: boolean }
interface Intel { exact?: Kind; above?: number; minePossible?: boolean; notMine?: boolean }
export interface ArmyState extends GameState {
  gameId: 'army'; seed: string; board: (ArmyPiece | null)[]; ready: [boolean, boolean];
  intel: [Record<string, Intel>, Record<string, Intel>];
  positions: string[]; actions: { seat: Seat; id: string }[];
}

const names: Record<Kind, string> = { commander: '司令', general: '军长', division: '师长', brigade: '旅长', regiment: '团长', battalion: '营长', company: '连长', platoon: '排长', engineer: '工兵', mine: '地雷', bomb: '炸弹', flag: '军旗' };
const rank: Partial<Record<Kind, number>> = { commander: 9, general: 8, division: 7, brigade: 6, regiment: 5, battalion: 4, company: 3, platoon: 2, engineer: 1 };
const rankNames = ['', '工兵', '排长', '连长', '营长', '团长', '旅长', '师长', '军长', '司令'];
const roster: Kind[] = ['commander', 'general', 'division', 'division', 'brigade', 'brigade', 'regiment', 'regiment', 'battalion', 'battalion', 'company', 'company', 'company', 'platoon', 'platoon', 'platoon', 'engineer', 'engineer', 'engineer', 'mine', 'mine', 'mine', 'bomb', 'bomb', 'flag'];
export const armyTerrain = {
  camps: [11, 13, 17, 21, 23, 36, 38, 42, 46, 48],
  headquarters: [1, 3, 56, 58],
  railway: Array.from({ length: 60 }, (_, i) => i).filter(i => {
    const row = Math.floor(i / 5), col = i % 5;
    return row > 0 && row < 11 && (col === 0 || col === 4 || [1, 5, 6, 10].includes(row));
  }),
};
const camp = new Set(armyTerrain.camps), hq = new Set(armyTerrain.headquarters), railway = new Set(armyTerrain.railway);
const localRow = (index: number, seat: Seat) => seat === 0 ? Math.floor(index / 5) : 11 - Math.floor(index / 5);
const coordinate = (index: number) => `${String.fromCharCode(65 + index % 5)}${Math.floor(index / 5) + 1}`;
const rules = '暗棋军棋：12×5棋盘，每方25子，蓝方先手。军旗在己方大本营，地雷在后两排，炸弹不在前线，行营不得布子。等级依次为司令、军长、师长、旅长、团长、营长、连长、排长、工兵；大吃小，同级同毁。军旗和地雷不可移动；进入大本营的任何棋子不能再走。军旗被吃立即判负；否则炸弹与目标同毁（包括地雷），工兵排雷且存活，其他棋子碰雷阵亡而雷留存。普通道路走一步，行营连线可斜走，行营内棋子不能被攻击。铁路直行不限步、工兵可以转弯，均不能越子；中央仅A/C/E列互通。失去军旗或无合法动作判负，同局面同轮转三次判和。敌方身份依双方各自的战斗信息逐步揭示；司令阵亡亮旗。';

function canPlace(kind: Kind, index: number, seat: Seat): boolean {
  const row = localRow(index, seat);
  if (index < 0 || index >= 60 || row < 0 || row > 5 || camp.has(index)) return false;
  if (kind === 'flag') return row === 0 && hq.has(index);
  if (kind === 'mine') return row < 2;
  if (kind === 'bomb') return row !== 5;
  return true;
}

function create(_config: Record<string, unknown>, seed: string): ArmyState {
  if (typeof seed !== 'string' || seed.length > 512) throw new Error('布阵种子无效');
  const random = randomFor(seed), board: (ArmyPiece | null)[] = Array(60).fill(null);
  for (const seat of [0, 1] as const) {
    const empty = Array.from({ length: 60 }, (_, i) => i).filter(i => localRow(i, seat) >= 0 && localRow(i, seat) < 6 && !camp.has(i));
    // Place constrained types first, then shuffle the remaining ranks.
    const pieces: Kind[] = ['flag', 'mine', 'mine', 'mine', 'bomb', 'bomb', ...shuffle(roster.filter(k => !['flag', 'mine', 'bomb'].includes(k)), random)];
    for (const kind of pieces) {
      const options = empty.filter(i => canPlace(kind, i, seat));
      const index = options[Math.floor(random() * options.length)];
      if (index === undefined) throw new Error('无法生成合法布阵');
      // IDs convey only the initial location, never a rank or roster offset.
      board[index] = { id: `piece-${seat}-${index}`, seat, kind, moved: false };
      empty.splice(empty.indexOf(index), 1);
    }
  }
  return { gameId: 'army', seed, board, ready: [false, false], turn: 0, phase: 'setup', ply: 0, intel: [{}, {}], positions: [], actions: [] };
}

function crossingAllowed(a: number, b: number): boolean {
  const ra = Math.floor(a / 5), rb = Math.floor(b / 5);
  return !((ra === 5 && rb === 6) || (ra === 6 && rb === 5)) || [0, 2, 4].includes(a % 5);
}

function neighbours(index: number, diagonal = false): number[] {
  const row = Math.floor(index / 5), col = index % 5, result: number[] = [];
  for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
    if ((!dr && !dc) || row + dr < 0 || row + dr >= 12 || col + dc < 0 || col + dc >= 5) continue;
    const to = (row + dr) * 5 + col + dc;
    if (dr && dc && (!diagonal || (!camp.has(index) && !camp.has(to)))) continue;
    if (crossingAllowed(index, to)) result.push(to);
  }
  return result;
}

function destinations(state: ArmyState, from: number): number[] {
  const piece = state.board[from];
  if (!piece || piece.kind === 'mine' || piece.kind === 'flag' || hq.has(from)) return [];
  const reachable = new Set<number>();
  const land = (to: number) => {
    const target = state.board[to];
    if (target?.seat !== piece.seat && !(target && camp.has(to))) reachable.add(to);
  };
  for (const to of neighbours(from, true)) land(to);
  if (railway.has(from)) {
    if (piece.kind === 'engineer') {
      const visited = new Set([from]), queue = [from];
      for (let i = 0; i < queue.length; i++) {
        for (const to of neighbours(queue[i])) {
          if (!railway.has(to) || visited.has(to)) continue;
          visited.add(to); land(to);
          if (state.board[to] === null) queue.push(to);
        }
      }
    } else {
      for (const [dr, dc] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        let current = from;
        while (true) {
          const row = Math.floor(current / 5) + dr, col = current % 5 + dc;
          if (row < 0 || row >= 12 || col < 0 || col >= 5) break;
          const to = row * 5 + col;
          if (!railway.has(to) || !crossingAllowed(current, to)) break;
          land(to);
          if (state.board[to]) break;
          current = to;
        }
      }
    }
  }
  return [...reachable].sort((a, b) => a - b);
}

function playingActions(state: ArmyState, seat: Seat): Action[] {
  return state.board.flatMap((piece, from) => piece?.seat === seat ? destinations(state, from).map(to => ({ id: `move:${from}:${to}`, from, to, label: `${coordinate(from)}→${coordinate(to)}` })) : []);
}

function legalActions(input: GameState, seat: Seat): Action[] {
  const state = input as ArmyState;
  if (seat !== 0 && seat !== 1) return [];
  if (state.phase === 'setup') {
    if (state.ready[seat]) return [];
    const indices = state.board.flatMap((piece, i) => piece?.seat === seat ? [i] : []);
    const swaps: Action[] = [];
    for (const from of indices) for (const to of indices) {
      if (from === to) continue;
      if (canPlace(state.board[from]!.kind, to, seat) && canPlace(state.board[to]!.kind, from, seat)) swaps.push({ id: `swap:${from}:${to}`, from, to, label: `交换 ${coordinate(from)} 与 ${coordinate(to)}` });
    }
    return [...swaps, { id: 'ready', label: '确认布阵，准备开始' }];
  }
  if (state.phase !== 'playing' || state.result || state.turn !== seat) return [];
  return playingActions(state, seat);
}

function positionKey(state: ArmyState): string {
  return `${state.turn}|${state.board.map(p => p ? `${p.seat}:${p.kind}` : '.').join(',')}`;
}

function revealFlag(state: ArmyState, seat: Seat) {
  const flag = state.board.find(p => p?.seat === seat && p.kind === 'flag');
  if (flag) state.intel[other(seat)][flag.id] = { exact: 'flag' };
}

function battleIntel(state: ArmyState, attacker: ArmyPiece, defender: ArmyPiece, to: number, outcome: 'win' | 'lose' | 'both') {
  state.intel[defender.seat][attacker.id] = { exact: attacker.kind };
  if (outcome === 'win') {
    state.intel[attacker.seat][defender.id] = { exact: defender.kind };
  } else if (outcome === 'lose') {
    const intel = state.intel[attacker.seat][defender.id];
    if (intel?.exact) return;
    const attackingRank = rank[attacker.kind]!;
    if (attackingRank === 9) state.intel[attacker.seat][defender.id] = { exact: 'mine' };
    else state.intel[attacker.seat][defender.id] = {
      above: Math.max(intel?.above ?? 0, attackingRank),
      minePossible: attackingRank !== 1 && !defender.moved && localRow(to, defender.seat) < 2 && !intel?.notMine,
      notMine: attackingRank === 1 || defender.moved || localRow(to, defender.seat) >= 2 || intel?.notMine,
    };
  }
}

function apply(input: GameState, seat: Seat, actionId: string): ArmyState {
  const state = input as ArmyState;
  if (seat !== 0 && seat !== 1) throw new Error('无效的玩家席位');
  if (!legalActions(state, seat).some(action => action.id === actionId)) throw new Error('军棋动作不合法或尚未轮到你');
  const next: ArmyState = structuredClone(state);
  next.actions.push({ seat, id: actionId });
  if (state.phase === 'setup') {
    if (actionId === 'ready') {
      next.ready[seat] = true;
      if (next.ready.every(Boolean)) {
        next.phase = 'playing'; next.turn = 0; next.positions.push(positionKey(next));
      }
    } else {
      const [, from, to] = actionId.split(':').map(Number);
      [next.board[from], next.board[to]] = [next.board[to], next.board[from]];
    }
    return next;
  }
  requireTurn(state, seat);
  const [, from, to] = actionId.split(':').map(Number), attacker = next.board[from]!, defender = next.board[to];
  let outcome: 'win' | 'lose' | 'both' = 'win';
  if (defender) {
    if (defender.kind === 'flag') next.result = { winner: seat, reason: 'FLAG_CAPTURED' };
    else if (attacker.kind === 'bomb' || defender.kind === 'bomb') outcome = 'both';
    else if (defender.kind === 'mine') outcome = attacker.kind === 'engineer' ? 'win' : 'lose';
    else outcome = rank[attacker.kind]! > rank[defender.kind]! ? 'win' : rank[attacker.kind] === rank[defender.kind] ? 'both' : 'lose';
    battleIntel(next, attacker, defender, to, outcome);
  }
  next.board[from] = null;
  if (outcome === 'win') {
    attacker.moved = true; next.board[to] = attacker;
    const knowledge = next.intel[other(seat)][attacker.id];
    if (knowledge && !knowledge.exact) { knowledge.minePossible = false; knowledge.notMine = true; }
  } else if (outcome === 'both') next.board[to] = null;
  if (attacker.kind === 'commander' && outcome !== 'win') revealFlag(next, seat);
  if (defender?.kind === 'commander' && outcome !== 'lose') revealFlag(next, other(seat));
  if (!next.result && outcome === 'win' && hq.has(to) && localRow(to, other(seat)) === 0) revealFlag(next, other(seat));
  next.turn = other(seat); next.ply++;
  next.positions.push(positionKey(next));
  if (!next.result && playingActions(next, next.turn).length === 0) next.result = { winner: seat, reason: 'NO_LEGAL_MOVES' };
  if (!next.result && next.positions.filter(key => key === next.positions.at(-1)).length >= 3) next.result = { winner: null, reason: 'THREEFOLD_REPETITION' };
  if (next.result) next.phase = 'finished';
  return next;
}

function projectPiece(state: ArmyState, piece: ArmyPiece, viewer: Viewer): Piece {
  const full = viewer === 'full' || state.phase === 'finished';
  const knowledge = state.intel[typeof viewer === 'number' ? viewer : other(piece.seat)][piece.id];
  if (full || viewer === piece.seat) return { id: piece.id, seat: piece.seat, kind: piece.kind, label: names[piece.kind] };
  if (knowledge?.exact) return { id: piece.id, seat: piece.seat, kind: knowledge.exact, label: names[knowledge.exact] };
  const label = knowledge?.above ? `>${rankNames[knowledge.above]}${knowledge.minePossible ? '或雷' : ''}` : '?';
  return { id: piece.id, seat: piece.seat, kind: 'unknown', label, hidden: true };
}

function view(input: GameState, viewer: Viewer): GameView {
  const state = input as ArmyState;
  return {
    gameId: 'army', rows: 12, cols: 5, cells: state.board.map(piece => piece ? projectPiece(state, piece, viewer) : null),
    turn: state.turn, phase: state.phase, ply: state.ply, ready: [...state.ready],
    legalActions: typeof viewer === 'number' ? legalActions(state, viewer) : [],
    ...(state.result ? { result: { ...state.result } } : {}),
  };
}

function restore(input: unknown): ArmyState {
  const data = record(input);
  if (data.gameId !== 'army' || typeof data.seed !== 'string' || !Array.isArray(data.actions) || data.actions.length > 5000) throw new Error('军棋存档格式无效');
  let state = create({}, data.seed);
  for (const action of data.actions) {
    const row = record(action);
    if ((row.seat !== 0 && row.seat !== 1) || typeof row.id !== 'string') throw new Error('军棋动作记录无效');
    state = apply(state, row.seat, row.id);
  }
  for (const field of ['board', 'intel', 'ready', 'positions', 'turn', 'ply', 'phase', 'result'] as const) {
    if (!same(state[field], data[field])) throw new Error('军棋存档与动作记录不一致');
  }
  return state;
}

export const army: GameAdapter = {
  id: 'army', name: '军棋', rulesVersion: 'army-hidden-1', rules,
  create, legalActions, apply, view, restore,
  observe(input, seat) {
    const projected = view(input, seat);
    return { game: '暗棋军棋', rules, yourSeat: seat, yourColor: seat === 0 ? '蓝（上方）' : '红（下方）', turn: projected.turn,
      coordinates: 'A-E列从左至右，1-12行从上至下；蓝方后排为1，红方后排为12。',
      terrain: armyTerrain,
      pieces: projected.cells.flatMap((piece, index) => piece ? [{ coordinate: coordinate(index), seat: piece.seat, kind: piece.kind, label: piece.label }] : []),
    };
  },
};
