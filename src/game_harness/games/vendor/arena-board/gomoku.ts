import type { Action, GameAdapter, GameResult, GameState, GameView, Seat, Viewer } from '@decision-arena/core';
import { other, record, requireTurn, same } from './common';

export interface GomokuState extends GameState {
  gameId: 'gomoku'; size: 9 | 15; board: (Seat | null)[]; history: number[];
}

const rules = '自由五子棋：黑棋先手，双方轮流在空交叉点落一子。横、竖或任一斜线连续五子及以上获胜；无禁手、无提子、无交换开局。棋盘满且无人连五则和棋。坐标列从左至右，行从上至下。';
const coordinate = (index: number, size: number) => `${String.fromCharCode(65 + index % size)}${Math.floor(index / size) + 1}`;

function create(config: Record<string, unknown>): GomokuState {
  const size = config.size ?? 15;
  if (size !== 9 && size !== 15) throw new Error('五子棋棋盘须为 9×9 或 15×15');
  return { gameId: 'gomoku', size, turn: 0, phase: 'playing', ply: 0, board: Array(size * size).fill(null), history: [] };
}

/** All winning stones through the final move; used by renderers and tests. */
export function gomokuWinningLine(state: GomokuState): number[] {
  const last = state.history.at(-1);
  if (last === undefined) return [];
  const seat = state.board[last], x = last % state.size, y = Math.floor(last / state.size);
  for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
    const line = [last];
    for (const direction of [-1, 1]) {
      let nx = x + direction * dx, ny = y + direction * dy;
      while (nx >= 0 && nx < state.size && ny >= 0 && ny < state.size && state.board[ny * state.size + nx] === seat) {
        if (direction < 0) line.unshift(ny * state.size + nx); else line.push(ny * state.size + nx);
        nx += direction * dx; ny += direction * dy;
      }
    }
    if (line.length >= 5) return line;
  }
  return [];
}

function legalActions(input: GameState, seat: Seat): Action[] {
  const state = input as GomokuState;
  if (state.phase !== 'playing' || state.result || state.turn !== seat) return [];
  return state.board.flatMap((stone, to) => stone === null ? [{ id: `place:${to}`, to, label: `落子 ${coordinate(to, state.size)}` }] : []);
}

function apply(input: GameState, seat: Seat, actionId: string): GomokuState {
  const state = input as GomokuState;
  requireTurn(state, seat);
  if (!/^place:(0|[1-9]\d*)$/.test(actionId)) throw new Error('落子动作无效');
  const to = Number(actionId.slice(6));
  if (to >= state.board.length || state.board[to] !== null) throw new Error('落点越界或已有棋子');
  const next: GomokuState = { ...state, board: state.board.slice(), history: [...state.history, to], turn: other(seat), ply: state.ply + 1 };
  next.board[to] = seat;
  let result: GameResult | undefined;
  if (gomokuWinningLine(next).length) result = { winner: seat, reason: 'FIVE_IN_ROW' };
  else if (next.ply === state.board.length) result = { winner: null, reason: 'BOARD_FULL' };
  if (result) { next.result = result; next.phase = 'finished'; }
  return next;
}

function view(input: GameState, viewer: Viewer): GameView {
  const state = input as GomokuState;
  return {
    gameId: 'gomoku', rows: state.size, cols: state.size, turn: state.turn, phase: state.phase, ply: state.ply,
    cells: state.board.map((seat, i) => seat === null ? null : { id: `stone-${i}`, seat, kind: 'stone', label: seat === 0 ? '黑' : '白' }),
    legalActions: typeof viewer === 'number' ? legalActions(state, viewer) : [],
    ...(state.result ? { result: { ...state.result } } : {}),
  };
}

function restore(input: unknown): GomokuState {
  const data = record(input);
  if (data.gameId !== 'gomoku' || !Array.isArray(data.history) || data.history.length > 225) throw new Error('五子棋存档无效');
  let state = create({ size: data.size });
  for (const index of data.history) {
    if (!Number.isInteger(index)) throw new Error('五子棋棋谱坐标无效');
    state = apply(state, state.turn, `place:${index}`);
  }
  if (!same(data.board, state.board) || data.turn !== state.turn || data.ply !== state.ply || data.phase !== state.phase || !same(data.result, state.result)) throw new Error('五子棋存档与棋谱不一致');
  return state;
}

export const gomoku: GameAdapter = {
  id: 'gomoku', name: '五子棋', rulesVersion: 'freestyle-1', rules,
  create: config => create(config), legalActions, apply, view, restore,
  observe(input, seat) {
    const state = input as GomokuState;
    return {
      game: '自由五子棋', rules, yourSeat: seat, yourColor: seat === 0 ? '黑' : '白', turn: state.turn, size: state.size,
      board: Array.from({ length: state.size }, (_, row) => state.board.slice(row * state.size, (row + 1) * state.size).map(p => p === null ? '.' : p === 0 ? 'X' : 'O').join(' ')),
      legend: 'X=黑棋，O=白棋，.=空位；A1位于左上角',
      stones: [0, 1].map(owner => state.board.flatMap((p, i) => p === owner ? [coordinate(i, state.size)] : [])),
    };
  },
};
