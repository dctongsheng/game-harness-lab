import type { GameState, Seat } from '@decision-arena/core';

export const other = (seat: Seat): Seat => seat === 0 ? 1 : 0;

export function requireTurn(state: GameState, seat: Seat) {
  if (seat !== 0 && seat !== 1) throw new Error('无效的玩家席位');
  if (state.phase !== 'playing' || state.result) throw new Error('当前棋局不能落子');
  if (state.turn !== seat) throw new Error('尚未轮到你');
}

export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('存档格式无效');
  return value as Record<string, unknown>;
}

export function same(a: unknown, b: unknown) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** A reproducible shuffle for arrangements, never used for credentials or IDs. */
export function randomFor(seed: string): () => number {
  let n = 2166136261;
  for (let i = 0; i < seed.length; i++) n = Math.imul(n ^ seed.charCodeAt(i), 16777619) >>> 0;
  return () => {
    n = (Math.imul(n, 1664525) + 1013904223) >>> 0;
    return n / 4294967296;
  };
}

export function shuffle<T>(items: T[], random: () => number): T[] {
  const result = items.slice();
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}
