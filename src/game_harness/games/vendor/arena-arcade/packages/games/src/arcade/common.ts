import { ARCADE_MAX_TICKS, type ArcadeAction, type ArcadeButton, type ArcadeInput, type ArcadeState } from '@decision-arena/core';

export const DURATIONS = [100, 300, 1000] as const;
export interface ControlChoice { buttons: ArcadeButton[]; label: string; durations?: readonly (typeof DURATIONS[number])[] }

/** Serializable, deterministic PRNG. Never use this for credentials or IDs. */
export function seedNumber(seed: string): number {
  let n = 2166136261;
  for (let i = 0; i < seed.length; i++) n = Math.imul(n ^ seed.charCodeAt(i), 16777619) >>> 0;
  return n;
}
export function random(state: { rng: number }): number {
  state.rng = (Math.imul(state.rng, 1664525) + 1013904223) >>> 0;
  return state.rng;
}
export function actions(choices: ControlChoice[]): ArcadeAction[] {
  return choices.flatMap(choice => (choice.durations ?? DURATIONS).map(durationMs => ({
    id: `${choice.buttons.join('+') || 'noop'}:${durationMs}`,
    label: `${choice.label}，${durationMs} 毫秒`, input: { buttons: [...choice.buttons], durationMs },
  })));
}
/** Describe the current clock and accepted controls, without advancing or choosing an action. */
export function observationClock(state: ArcadeState) {
  return { tick: state.tick, tickRate: 60, ticksRemaining: ARCADE_MAX_TICKS - state.tick,
    secondsRemaining: Number(((ARCADE_MAX_TICKS - state.tick) / 60).toFixed(3)) };
}
export function observationControls(state: ArcadeState) {
  return { held: [...state.held], releaseTick: state.releaseTick,
    releaseInTicks: state.releaseTick === null ? null : state.releaseTick - state.tick,
    pendingEdges: Array.isArray(state.pendingEdges) ? [...state.pendingEdges] as ArcadeButton[] : [],
    semantics: 'held 是当前按住的键；pendingEdges 是已收到但尚未在下一物理 tick 消费的新按下。新输入替换整组 held；到期只松键，不暂停游戏。已经按住的键再次提交不产生新的按下边沿。' };
}
export function validInput(input: ArcadeInput, allowed: readonly ArcadeButton[]): void {
  if (!input || !Array.isArray(input.buttons) || input.buttons.length > allowed.length ||
    new Set(input.buttons).size !== input.buttons.length || input.buttons.some(b => !allowed.includes(b))) throw new Error('无效的游戏按键');
  if (input.durationMs !== undefined && !DURATIONS.includes(input.durationMs as 100 | 300 | 1000)) throw new Error('按键时长必须为 100、300 或 1000 毫秒');
}
export function setInput<T extends ArcadeState>(state: T, input: ArcadeInput): T {
  if (state.status !== 'playing') throw new Error('本局已经结束');
  const copy = structuredClone(state);
  copy.held = [...input.buttons];
  // Capture a genuine key-down even when key-up arrives before the next physics tick.
  // Renewing an already held button must not synthesize another edge.
  const controls: ArcadeState = copy;
  if (Array.isArray(controls.pendingEdges)) controls.pendingEdges = [...new Set([...controls.pendingEdges, ...input.buttons.filter(button => !state.held.includes(button))])];
  copy.releaseTick = input.durationMs === undefined ? null : state.tick + input.durationMs * 60 / 1000;
  return copy;
}
function release(state: ArcadeState): void {
  state.held = []; state.releaseTick = null;
}
/** Internal cancellation for pause/disconnect/end; ordinary user key-up keeps accepted tap edges. */
export function clearInput<T extends ArcadeState>(state: T): T {
  const copy = structuredClone(state);
  release(copy);
  const controls: ArcadeState = copy;
  if (Array.isArray(controls.pendingEdges)) controls.pendingEdges = [];
  if ('queued' in controls) controls.queued = null;
  return copy;
}
export function finish(state: ArcadeState, reason: string): void {
  state.status = 'finished'; state.reason = reason; release(state);
  if (Array.isArray(state.pendingEdges)) state.pendingEdges = [];
  if ('queued' in state) state.queued = null;
}
/** The tick clock advances even when there is no input or a model is pending. */
export function advance<T extends ArcadeState>(state: T, ticks: number | undefined, frame: (state: T) => void): T {
  const count = ticks ?? 1;
  if (!Number.isInteger(count) || count < 0 || count > ARCADE_MAX_TICKS) throw new Error('无效的 tick 数');
  const copy = structuredClone(state);
  for (let i = 0; i < count && copy.status === 'playing'; i++) {
    if (copy.releaseTick !== null && copy.tick >= copy.releaseTick) release(copy);
    copy.tick++;
    frame(copy);
    copy.score.survivedTicks = copy.tick;
    if (copy.status === 'playing' && copy.tick >= ARCADE_MAX_TICKS) finish(copy, 'TIME_LIMIT');
    if (copy.releaseTick !== null && copy.tick >= copy.releaseTick) release(copy);
  }
  return copy;
}
export function integer(value: unknown, min: number, max: number): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max;
}
export function baseState(value: unknown, gameId: ArcadeState['gameId'], allowed: readonly ArcadeButton[]): ArcadeState {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('存档格式无效');
  const s = value as ArcadeState;
  if (s.gameId !== gameId || !integer(s.tick, 0, ARCADE_MAX_TICKS) || !['playing', 'finished'].includes(s.status) ||
    !s.score || typeof s.score !== 'object' || !integer(s.score.survivedTicks, 0, ARCADE_MAX_TICKS) || s.score.survivedTicks !== s.tick ||
    !Number.isFinite(s.score.points) || s.score.points < 0 ||
    (s.releaseTick !== null && !integer(s.releaseTick, s.tick, s.tick + 60))) throw new Error('存档格式无效');
  validInput({ buttons: s.held }, allowed);
  if (s.status === 'playing' && (s.tick >= ARCADE_MAX_TICKS || s.reason !== undefined)) throw new Error('存档状态无效');
  if (s.status === 'finished' && (typeof s.reason !== 'string' || s.held.length !== 0 || s.releaseTick !== null)) throw new Error('存档结束状态无效');
  return structuredClone(s);
}
