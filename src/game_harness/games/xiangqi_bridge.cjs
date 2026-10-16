/* Mechanical rules bridge. No model, evaluation search, or move selection lives here. */
'use strict';
const readline = require('node:readline');
const { Xiangqi } = require('./vendor/xiangqi.cjs');

const labels = { r: '车', n: '马', b: '象', a: '士', k: '将', c: '炮', p: '卒',
  R: '车', N: '马', B: '相', A: '仕', K: '帅', C: '炮', P: '兵' };
let board;

function outcome() {
  if (board.in_checkmate()) return { winner: board.turn() === 'r' ? 'b' : 'r', reason: 'checkmate' };
  if (board.in_stalemate()) return { winner: board.turn() === 'r' ? 'b' : 'r', reason: 'stalemate' };
  if (board.in_threefold_repetition()) return { winner: null, reason: 'threefold_repetition' };
  if (Number(board.fen().split(' ')[4]) >= 120) return { winner: null, reason: 'noncapture_120' };
  if (board.insufficient_material()) return { winner: null, reason: 'insufficient_material' };
  return null;
}

function state() {
  const result = outcome();
  const cells = board.board().flat().map(p => p ? {
    type: p.type, kind: p.type, color: p.color,
    label: labels[p.color === 'r' ? p.type.toUpperCase() : p.type],
  } : null);
  const candidates = result ? [] : board.moves({ verbose: true }).map(move => ({
    id: move.iccs,
    label: `${labels[move.color === 'r' ? move.piece.toUpperCase() : move.piece]} ${move.from}→${move.to}${move.captured ? `（吃${labels[move.color === 'r' ? move.captured : move.captured.toUpperCase()]}）` : ''}`,
  })).sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  return { fen: board.fen(), turn: board.turn(), cells, board_ascii: board.ascii(),
    in_check: board.in_check(), outcome: result, candidates };
}

function dispatch(input) {
  if (input.op === 'init') {
    if (board) throw new Error('already_initialized');
    board = input.fen ? new Xiangqi(input.fen) : new Xiangqi();
    return state();
  }
  if (!board) throw new Error('not_initialized');
  if (input.op === 'state') return state();
  if (input.op === 'step') {
    if (outcome()) throw new Error('episode_already_ended');
    if (typeof input.action !== 'string' || !/^[a-i][0-9][a-i][0-9]$/.test(input.action)) {
      throw new Error('invalid_move_format');
    }
    if (!board.move(input.action)) throw new Error('illegal_move');
    return state();
  }
  throw new Error('unknown_operation');
}

readline.createInterface({ input: process.stdin, crlfDelay: Infinity }).on('line', line => {
  let input;
  try {
    input = JSON.parse(line);
    const value = dispatch(input);
    process.stdout.write(JSON.stringify({ id: input.id, ok: true, value }) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ id: input?.id, ok: false, error: String(error.message) }) + '\n');
  }
});
