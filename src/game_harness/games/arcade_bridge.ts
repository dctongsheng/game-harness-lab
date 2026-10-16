/** Paused controller for unchanged MIT Decision Arena arcade engines.
 * Copyright notice is preserved in vendor/arena-arcade/LICENSE.
 * This wrapper applies one selected native input; it never chooses an action.
 */
import { createInterface } from 'node:readline';
import { tetris } from './vendor/arena-arcade/packages/games/src/arcade/tetris';
import { platformer } from './vendor/arena-arcade/packages/games/src/arcade/platformer';
import type { ArcadeAdapter, ArcadeState } from './vendor/arena-arcade/packages/core/src/arcade';

// Exports allow direct deterministic rules tests without a production fixture API.
export { tetris, platformer };

let adapter: ArcadeAdapter | undefined;
let state: ArcadeState;
let decisions = 0;
let maxSteps = 80;
let lastInput: object | null = null;

function snapshot() {
  const naturalDone = state.status === 'finished';
  const truncated = !naturalDone && decisions >= maxSteps;
  const done = naturalDone || truncated;
  const candidates = done ? [] : adapter!.candidates(state).map(action => ({
    id: action.id,
    label: action.id === 'noop:100' ? '释放所有按键，自然推进100毫秒（6物理tick）' : action.label,
  }));
  return {
    snapshot: {
      game_id: adapter!.id,
      rules_version: `game-harness-lab/${adapter!.id}-paused-v1-arena-4e2bda1`,
      step: decisions, max_steps: maxSteps, score: state.score.points,
      done, reason: naturalDone ? state.reason : truncated ? 'step_limit' : null,
      truncated, policy_role: 'candidate', tick: state.tick,
      metrics: { ...state.score }, render: adapter!.view(state),
      observation: adapter!.observe(state), last_input: lastInput,
      simulation: { tick_rate: 60, wall_clock_advances_game: false,
        action_duration_semantics: 'One selected native input advances exactly durationMs*60/1000 ticks unless the game naturally ends first. Waiting for a model advances zero ticks.' },
    },
    candidates,
  };
}

function dispatch(input: Record<string, unknown>) {
  if (input.op === 'init') {
    if (adapter) throw new Error('already_initialized');
    if (input.game_id !== 'tetris' && input.game_id !== 'platformer') throw new Error('unknown_game');
    if (!Number.isInteger(input.max_steps) || Number(input.max_steps) < 1 || Number(input.max_steps) > 2000) throw new Error('invalid_step_budget');
    if (!Number.isSafeInteger(input.seed)) throw new Error('invalid_seed');
    adapter = input.game_id === 'tetris' ? tetris : platformer;
    maxSteps = Number(input.max_steps);
    const level = ((Number(input.seed) % 3 + 3) % 3 + 1) as 1 | 2 | 3;
    state = adapter.create(adapter.id === 'platformer' ? { level } : {}, String(input.seed));
    return snapshot();
  }
  if (!adapter) throw new Error('not_initialized');
  if (input.op === 'state') return snapshot();
  if (input.op !== 'step') throw new Error('unknown_operation');
  if (state.status !== 'playing' || decisions >= maxSteps) throw new Error('episode_already_ended');
  const action = adapter.candidates(state).find(candidate => candidate.id === input.action);
  if (!action) throw new Error('action_is_not_in_native_catalog');
  const durationMs = action.input.durationMs!;
  const ticks = durationMs * 60 / 1000;
  const before = state.tick;
  state = adapter.input(state, action.input);
  state = adapter.step(state, ticks);
  decisions++;
  lastInput = { action_id: action.id, buttons: [...action.input.buttons],
    duration_ms: durationMs, requested_ticks: ticks, simulated_ticks: state.tick - before };
  return snapshot();
}

function main() {
  createInterface({ input: process.stdin, crlfDelay: Infinity }).on('line', line => {
    let input: Record<string, unknown> | undefined;
    try {
      input = JSON.parse(line);
      process.stdout.write(JSON.stringify({ id: input!.id, ok: true, value: dispatch(input!) }) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({ id: input?.id, ok: false,
        error: error instanceof Error ? error.message : 'rules_error' }) + '\n');
    }
  });
}

if (require.main === module) main();
