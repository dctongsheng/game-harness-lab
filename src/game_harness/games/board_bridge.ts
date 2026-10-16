/**
 * Isolated JSON-line bridge for MIT Decision Arena board rules.
 * Vendored sources are unchanged; see vendor/arena-board/LICENSE and SOURCE.json.
 * No authoritative board, seed, replay state or hidden identity crosses this IPC.
 */
import readline from 'node:readline';
import { army, armyTerrain } from './vendor/arena-board/army';
import { gomoku, gomokuWinningLine } from './vendor/arena-board/gomoku';

// Rule functions are exported for offline synthetic tests. The live IPC exposes
// only init/step/snapshot, never the process-owned authoritative state.
export { army, gomoku };

export function projectArmy(state: any, seat: 0 | 1) {
  // Upstream reveals every rank after a finished game. Our hidden-information
  // contract also protects terminal replays, so project a shallow phase-only
  // copy. The actual result, battle intel and authoritative state stay intact.
  const projection = state.phase === 'finished' ? { ...state, phase: 'playing' } : state;
  const privateView = army.view(projection, seat);
  const publicRaw = army.view(projection, 'spectator');
  const { legalActions: _ignored, ...publicView } = publicRaw;
  return {
    cells: privateView.cells,
    observation: army.observe(projection, seat),
    public_view: {
      ...publicView, phase: state.phase, result: publicRaw.result ?? null, terrain: armyTerrain,
      observation: {
        game: '暗棋军棋公共观察', rules: army.rules, turn: state.turn,
        coordinates: 'A-E列从左至右，1-12行从上至下；蓝方后排为1，红方后排为12。',
        visibility: '只含双方共同可知的棋子信息，不是任何一方的私有视角。',
        terrain: armyTerrain,
        pieces: publicView.cells.flatMap((piece: any, index: number) => piece ? [{
          coordinate: `${String.fromCharCode(65 + index % 5)}${Math.floor(index / 5) + 1}`,
          seat: piece.seat, kind: piece.kind, label: piece.label,
        }] : []),
      },
    },
  };
}

export function createController() {
  let gameId: 'army' | 'gomoku' | undefined;
  let engine: any;
  let state: any;
  let candidateSeat: 0 | 1;
  let maxSteps: number;

  function snapshot() {
    if (!state) throw new Error('Game has not been initialized');
    const outcome = state.result;
    const truncated = !outcome && state.ply >= maxSteps;
    const done = Boolean(outcome || truncated);
    const seat = state.turn as 0 | 1;
    const result = outcome ? (outcome.winner === null ? 'draw' : outcome.winner === candidateSeat ? 'win' : 'loss') : null;
    const score = result === 'win' ? 100 : result === 'loss' ? 0 : 50;
    let view: any;
    if (gameId === 'army') {
      view = { rows: 12, cols: 5, ...projectArmy(state, seat), terrain: armyTerrain };
      if (truncated) view.public_view.phase = 'finished';
    } else {
      const publicView = gomoku.view(state, seat);
      view = { rows: state.size, cols: state.size, cells: publicView.cells,
               observation: gomoku.observe(state, seat), winning_line: gomokuWinningLine(state) };
    }
    return {
      game_id: gameId, ...view, step: state.ply, max_steps: maxSteps,
      turn: seat, actor: seat, viewer_seat: seat, candidate_seat: candidateSeat,
      policy_role: seat === candidateSeat ? 'candidate' : 'opponent',
      phase: done ? 'finished' : 'playing', score, done,
      reason: outcome?.reason ?? (truncated ? 'step_limit' : null),
      result, winner: outcome?.winner ?? null, truncated: Boolean(truncated),
      score_kind: outcome ? 'outcome' : truncated ? 'cutoff_neutral' : 'in_progress_neutral',
    };
  }

  function output() {
    const current = snapshot();
    const candidates = current.done ? [] : engine.legalActions(state, state.turn).map((action: any) => ({
      id: action.id, label: action.label,
    }));
    if (!current.done && !candidates.length) throw new Error('Nonterminal game has no legal actions');
    return { snapshot: current, candidates };
  }

  return {
    request(request: any) {
      if (request.op === 'init') {
        if (state) throw new Error('A session can only be initialized once');
        if (!['army', 'gomoku'].includes(request.game)) throw new Error('Unknown board game');
        if (!Number.isSafeInteger(request.seed) || request.seed < 0) throw new Error('Invalid scenario seed');
        if (!Number.isInteger(request.max_steps) || request.max_steps < 1 || request.max_steps > 2000) throw new Error('Invalid step budget');
        gameId = request.game;
        candidateSeat = (request.seed % 2) as 0 | 1;
        maxSteps = request.max_steps;
        engine = gameId === 'army' ? army : gomoku;
        state = engine.create(gameId === 'gomoku' ? { size: request.gomoku_size ?? 9 } : {}, String(request.seed));
        if (gameId === 'army') {
          // This task begins after a deterministic legal pre-arrangement.
          // Setup is environment initialization, NOT a filtered setup action
          // catalog or a decision made on behalf of either playing agent.
          state = army.apply(state, 0, 'ready');
          state = army.apply(state, 1, 'ready');
        }
        return output();
      }
      if (!state) throw new Error('Game has not been initialized');
      if (request.op === 'snapshot') return output();
      if (request.op !== 'step') throw new Error('Unsupported board operation');
      const current = output();
      if (current.snapshot.done) throw new Error('Game already ended');
      if (request.seat !== undefined && request.seat !== state.turn) throw new Error('Wrong acting seat');
      if (typeof request.action !== 'string' || !current.candidates.some((a: any) => a.id === request.action)) {
        throw new Error('Action is not in the complete current legal catalog');
      }
      state = engine.apply(state, state.turn, request.action);
      return output();
    },
  };
}

function main() {
  const controller = createController();
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on('line', (line) => {
    let id: unknown = null;
    try {
      const request = JSON.parse(line);
      id = request.id;
      if (!Number.isInteger(id)) throw new Error('Request requires an integer id');
      const value = controller.request(request);
      process.stdout.write(JSON.stringify({ id, ok: true, value }) + '\n');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Board operation failed';
      process.stdout.write(JSON.stringify({ id, ok: false, error: message }) + '\n');
    }
  });
}

if (require.main === module) main();
