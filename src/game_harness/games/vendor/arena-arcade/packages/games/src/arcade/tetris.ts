import type { ArcadeAdapter, ArcadeButton, ArcadePoint, ArcadeState } from '@decision-arena/core';
import { actions, advance, baseState, clearInput, finish, integer, observationClock, observationControls, random, seedNumber, setInput, validInput, type ControlChoice } from './common';

export const TETROMINOES = ['I', 'O', 'T', 'S', 'Z', 'J', 'L'] as const;
export type Tetromino = typeof TETROMINOES[number];
type Rotation = 0 | 1 | 2 | 3;
export interface FallingPiece { type: Tetromino; rotation: Rotation; x: number; y: number }
export interface TetrisState extends ArcadeState {
  gameId: 'tetris'; board: number[]; active: FallingPiece | null; next: Tetromino[]; hold: Tetromino | null;
  holdUsed: boolean; pieceId: number; rng: number; pendingEdges: ArcadeButton[];
  fallTicks: number; lockTicks: number; lockResets: number; horizontal: -1 | 0 | 1; horizontalTicks: number;
}
const BUTTONS: ArcadeButton[] = ['left', 'right', 'rotate-cw', 'rotate-ccw', 'soft-drop', 'hard-drop', 'hold'];
const SHAPES: Record<Tetromino, number[][]> = {
  I: [[0, 1], [1, 1], [2, 1], [3, 1]], O: [[1, 0], [2, 0], [1, 1], [2, 1]],
  T: [[1, 0], [0, 1], [1, 1], [2, 1]], S: [[1, 0], [2, 0], [0, 1], [1, 1]],
  Z: [[0, 0], [1, 0], [1, 1], [2, 1]], J: [[0, 0], [0, 1], [1, 1], [2, 1]], L: [[2, 0], [0, 1], [1, 1], [2, 1]],
};
/** Independently expressed SRS offsets. Table y points upward; board y points downward. */
const KICKS: Record<string, number[][]> = {
  '01': [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
  '10': [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
  '12': [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
  '21': [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
  '23': [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]],
  '32': [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
  '30': [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
  '03': [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]],
};
const I_KICKS: Record<string, number[][]> = {
  '01': [[0, 0], [-2, 0], [1, 0], [-2, -1], [1, 2]],
  '10': [[0, 0], [2, 0], [-1, 0], [2, 1], [-1, -2]],
  '12': [[0, 0], [-1, 0], [2, 0], [-1, 2], [2, -1]],
  '21': [[0, 0], [1, 0], [-2, 0], [1, -2], [-2, 1]],
  '23': [[0, 0], [2, 0], [-1, 0], [2, 1], [-1, -2]],
  '32': [[0, 0], [-2, 0], [1, 0], [-2, -1], [1, 2]],
  '30': [[0, 0], [1, 0], [-2, 0], [1, -2], [-2, 1]],
  '03': [[0, 0], [-1, 0], [2, 0], [-1, 2], [2, -1]],
};
export function tetrisCells(piece: FallingPiece): ArcadePoint[] {
  return SHAPES[piece.type].map(([x0, y0]) => {
    let x = x0, y = y0;
    if (piece.type !== 'O') for (let r = 0; r < piece.rotation; r++) [x, y] = [(piece.type === 'I' ? 3 : 2) - y, x];
    return { x: piece.x + x, y: piece.y + y };
  });
}
function fits(s: TetrisState, piece: FallingPiece): boolean {
  return tetrisCells(piece).every(p => p.x >= 0 && p.x < 10 && p.y >= -2 && p.y < 20 && s.board[(p.y + 2) * 10 + p.x] === 0);
}
function grounded(s: TetrisState): boolean { return !!s.active && !fits(s, { ...s.active, y: s.active.y + 1 }); }
function fillQueue(s: TetrisState): void {
  while (s.next.length < 7) {
    const bag = [...TETROMINOES];
    for (let i = bag.length - 1; i > 0; i--) { const j = random(s) % (i + 1); [bag[i], bag[j]] = [bag[j], bag[i]]; }
    s.next.push(...bag);
  }
}
function spawn(s: TetrisState, type?: Tetromino): void {
  if (!type) { fillQueue(s); type = s.next.shift()!; fillQueue(s); }
  s.active = { type, rotation: 0, x: 3, y: -2 }; s.pieceId++;
  s.fallTicks = 0; s.lockTicks = 0; s.lockResets = 0;
  if (!fits(s, s.active)) finish(s, 'SPAWN_BLOCKED');
}
function lock(s: TetrisState): void {
  if (!s.active) return;
  const color = TETROMINOES.indexOf(s.active.type) + 1;
  for (const p of tetrisCells(s.active)) s.board[(p.y + 2) * 10 + p.x] = color;
  const rows: number[][] = [];
  let cleared = 0;
  for (let y = 0; y < 22; y++) {
    const row = s.board.slice(y * 10, y * 10 + 10);
    if (row.every(Boolean)) cleared++; else rows.push(row);
  }
  while (rows.length < 22) rows.unshift(Array<number>(10).fill(0));
  s.board = rows.flat();
  const level = Math.floor((s.score.lines ?? 0) / 10) + 1;
  s.score.lines = (s.score.lines ?? 0) + cleared;
  s.score.points += ([0, 100, 300, 500, 800][cleared] ?? 0) * level;
  s.holdUsed = false; s.active = null;
  if (s.board.slice(0, 20).some(Boolean)) { finish(s, 'TOP_OUT'); return; }
  spawn(s);
}
function resetLock(s: TetrisState, wasGrounded: boolean): void {
  if (wasGrounded && s.lockResets < 15) { s.lockTicks = 0; s.lockResets++; }
}
function move(s: TetrisState, dx: number): void {
  if (!s.active) return;
  const oldGrounded = grounded(s), next = { ...s.active, x: s.active.x + dx };
  if (fits(s, next)) { s.active = next; resetLock(s, oldGrounded); }
}
function rotate(s: TetrisState, clockwise: boolean): void {
  if (!s.active) return;
  const old = s.active, nextRotation = ((old.rotation + (clockwise ? 1 : 3)) % 4) as Rotation;
  const offsets = old.type === 'O' ? [[0, 0]] : (old.type === 'I' ? I_KICKS : KICKS)[`${old.rotation}${nextRotation}`];
  const oldGrounded = grounded(s);
  for (const [dx, dy] of offsets) {
    const next = { ...old, rotation: nextRotation, x: old.x + dx, y: old.y - dy };
    if (fits(s, next)) { s.active = next; resetLock(s, oldGrounded); return; }
  }
}
export function tetrisGravityTicks(s: TetrisState): number { return Math.max(6, 48 - Math.floor((s.score.lines ?? 0) / 10) * 3); }
function frame(s: TetrisState): void {
  const held = new Set(s.held), pressed = new Set(s.pendingEdges), edge = (button: ArcadeButton) => pressed.has(button);
  s.pendingEdges = [];
  if (edge('hold') && !s.holdUsed && s.active) {
    const type = s.active.type, stored = s.hold; s.hold = type; s.holdUsed = true;
    spawn(s, stored ?? undefined);
  }
  if (s.status !== 'playing') return;
  if (edge('rotate-cw')) rotate(s, true);
  if (edge('rotate-ccw')) rotate(s, false);
  const direction = (held.has('right') ? 1 : 0) - (held.has('left') ? 1 : 0) as -1 | 0 | 1;
  if (direction !== s.horizontal) { s.horizontal = direction; s.horizontalTicks = 0; if (direction) move(s, direction); }
  else if (direction) { s.horizontalTicks++; if (s.horizontalTicks >= 9 && (s.horizontalTicks - 9) % 3 === 0) move(s, direction); }
  if (!direction && edge('left') !== edge('right')) move(s, edge('left') ? -1 : 1);
  if (edge('hard-drop') && s.active) {
    while (fits(s, { ...s.active, y: s.active.y + 1 })) s.active.y++;
    lock(s); return;
  }
  if (s.active) {
    s.fallTicks++;
    const interval = held.has('soft-drop') ? Math.min(3, tetrisGravityTicks(s)) : tetrisGravityTicks(s);
    if (s.fallTicks >= interval) {
      s.fallTicks = 0;
      if (fits(s, { ...s.active, y: s.active.y + 1 })) s.active.y++;
    }
    if (grounded(s)) { s.lockTicks++; if (s.lockTicks >= 30) lock(s); }
  }
}
export const tetris: ArcadeAdapter = {
  id: 'tetris', name: '俄罗斯方块', rulesVersion: 'tetris-realtime-v1',
  rules: '10×20可见格、顶部2隐藏行；7-bag、预览5块、每块可hold一次。初始800ms下一行，每消10行减50ms，最低100ms；接地锁定500ms，移动/旋转最多重置15次。左右按住150ms后每50ms重复；软降20行/秒。SRS顺/逆时针旋转；旋转、hold和硬降只在按下边沿触发。消1/2/3/4行得100/300/500/800乘当前等级分；等级为1+累计消行数除10取整，以消行前等级计分。软硬降无分数，无T-spin/combo额外分。隐藏行有锁定方块或生成受阻结束，最多120秒；比较消行数、分数、存活tick。',
  create(_config, seed): TetrisState {
    const s: TetrisState = { gameId: 'tetris', tick: 0, status: 'playing', score: { points: 0, lines: 0, survivedTicks: 0 }, held: [], releaseTick: null,
      board: Array<number>(220).fill(0), active: null, next: [], hold: null, holdUsed: false, pieceId: 0, rng: seedNumber(seed), pendingEdges: [],
      fallTicks: 0, lockTicks: 0, lockResets: 0, horizontal: 0, horizontalTicks: 0 };
    spawn(s); return s;
  },
  candidates(state) {
    const s = state as TetrisState;
    if (s.status !== 'playing') return [];
    const choices: ControlChoice[] = [
      { buttons: [], label: '释放所有按键，重力继续，不等待一整段时长', durations: [100] }, { buttons: ['left'], label: '持续向左移动' }, { buttons: ['right'], label: '持续向右移动' },
      { buttons: ['rotate-cw'], label: '尝试顺时针旋转一次（新按下时）', durations: [100] }, { buttons: ['rotate-ccw'], label: '尝试逆时针旋转一次（新按下时）', durations: [100] },
      { buttons: ['soft-drop'], label: '持续软降' }, { buttons: ['hard-drop'], label: '硬降并锁定一次（新按下时）', durations: [100] },
    ];
    if (!s.holdUsed) choices.push({ buttons: ['hold'], label: '暂存/交换方块一次（新按下时）', durations: [100] });
    return actions(choices);
  },
  input(state, input) {
    validInput(input, BUTTONS);
    if (input.buttons.includes('left') && input.buttons.includes('right')) throw new Error('不能同时向左和向右');
    if (input.buttons.includes('rotate-cw') && input.buttons.includes('rotate-ccw')) throw new Error('不能同时向两个方向旋转');
    return setInput(state as TetrisState, input);
  },
  release(state) { return clearInput(state as TetrisState); },
  step(state, ticks) { return advance(state as TetrisState, ticks, frame); },
  observe(state) {
    const s = state as TetrisState;
    return { gameId: 'tetris', status: s.status, width: 10, height: 20, hiddenRows: 2, ...observationClock(s),
      coordinates: '单位是格；x 为 0..9，向右增加；y 向下增加，-2、-1 是顶部隐藏生成行，0..19 是可见行。每行第 x 个字符对应该列。',
      board: { columns: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        rows: Array.from({ length: 22 }, (_, row) => ({ y: row - 2, cells: s.board.slice(row * 10, row * 10 + 10).map(cell => cell === 0 ? '.' : TETROMINOES[cell - 1]).join('') })),
        legend: { '.': '空格', I: '已锁定 I 块', O: '已锁定 O 块', T: '已锁定 T 块', S: '已锁定 S 块', Z: '已锁定 Z 块', J: '已锁定 J 块', L: '已锁定 L 块' },
        contents: 'board 仅包含已锁定方块；当前下落方块单独记录在 active，未叠加进 board。没有预测落点。' },
      active: s.active && { ...s.active, cells: tetrisCells(s.active),
        coordinateMeaning: 'x、y 是旋转形状局部框的原点，不一定是占用格；cells 是此刻实际占用的绝对格坐标。rotation=0/1/2/3 表示生成朝向起顺时针旋转0/90/180/270度。' },
      next: s.next.slice(0, 5), nextOrder: 'next[0] 是下一块，仅显示可见的五块预览', hold: s.hold,
      holdAvailable: !s.holdUsed, pieceId: s.pieceId, gravityEveryTicks: tetrisGravityTicks(s), fallTicks: s.fallTicks,
      effectiveFallEveryTicks: s.held.includes('soft-drop') ? Math.min(3, tetrisGravityTicks(s)) : tetrisGravityTicks(s),
      grounded: grounded(s), lockTicks: s.lockTicks, lockLimitTicks: 30, lockResets: s.lockResets, maxLockResets: 15,
      horizontalDirection: s.horizontal, horizontalHeldTicks: s.horizontalTicks,
      controls: { ...observationControls(s),
        edgeSemantics: '旋转、硬降、hold 只在新按下的下一物理 tick 尝试一次；持续按住不重复，尚未松开时重复提交同键也不会再触发。左右及软降按候选时长持续。noop 只松键，仍会自然下落。' },
      score: { ...s.score } };
  },
  view(state) {
    const s = state as TetrisState;
    return { gameId: 'tetris', tick: s.tick, status: s.status, score: { ...s.score }, width: 10, height: 20,
      currentButtons: [...s.held], reason: s.reason, cells: s.board.slice(20),
      active: s.active ? { type: s.active.type, cells: tetrisCells(s.active) } : undefined,
      next: s.next.slice(0, 5), hold: s.hold, pieceId: s.pieceId };
  },
  restore(value) {
    const s = baseState(value, 'tetris', BUTTONS) as TetrisState;
    validInput({ buttons: s.pendingEdges }, BUTTONS);
    if (!Array.isArray(s.board) || s.board.length !== 220 || !s.board.every(n => integer(n, 0, 7)) ||
      !Array.isArray(s.next) || s.next.length < 5 || s.next.length > 13 || !s.next.every(p => TETROMINOES.includes(p)) ||
      (s.hold !== null && !TETROMINOES.includes(s.hold)) || typeof s.holdUsed !== 'boolean' || !integer(s.pieceId, 1, 20000) ||
      !integer(s.rng, 0, 0xffffffff) || !integer(s.score.lines, 0, 28800) || !integer(s.fallTicks, 0, 47) ||
      !integer(s.lockTicks, 0, 30) || !integer(s.lockResets, 0, 15) || !integer(s.horizontal, -1, 1) || !integer(s.horizontalTicks, 0, 7200)) throw new Error('方块存档无效');
    if (s.active !== null && (!TETROMINOES.includes(s.active.type) || !integer(s.active.rotation, 0, 3) ||
      !integer(s.active.x, -3, 9) || !integer(s.active.y, -2, 19))) throw new Error('当前方块存档无效');
    if (s.status === 'playing' && (!s.active || !fits(s, s.active))) throw new Error('当前方块碰撞');
    return s;
  },
  compare(a, b) { return (a.lines ?? 0) - (b.lines ?? 0) || a.points - b.points || a.survivedTicks - b.survivedTicks; },
};
