import type { Action, DecisionMetrics, DecisionReasoning, PublicSeatConfig, SeatConfig } from './index';

export type ArcadeGameId = 'snake' | 'tetris' | 'platformer';
export type ArcadeMode = 'practice' | 'ai-solo' | 'human-ai' | 'ai-ai';
export type ArcadeButton = 'up' | 'down' | 'left' | 'right' | 'rotate-cw' | 'rotate-ccw' | 'soft-drop' | 'hard-drop' | 'hold' | 'jump';
export interface ArcadeInput { buttons: ArcadeButton[]; durationMs?: number }
export interface ArcadeAction extends Action { input: ArcadeInput }
export interface ArcadePoint { x: number; y: number }
export interface ArcadeRect extends ArcadePoint { width: number; height: number }
export interface ArcadeScore { points: number; food?: number; lines?: number; coins?: number; progress?: number; completed?: boolean; survivedTicks: number }
export interface ArcadeState {
  gameId: ArcadeGameId; tick: number; status: 'playing' | 'finished'; score: ArcadeScore;
  held: ArcadeButton[]; releaseTick: number | null; reason?: string;
  [key: string]: unknown;
}
/** Render-only projection: never includes credentials or provider payloads. */
export interface ArcadeView {
  gameId: ArcadeGameId; tick: number; status: 'playing' | 'finished'; score: ArcadeScore;
  width: number; height: number; currentButtons: ArcadeButton[]; reason?: string;
  cells?: number[]; snake?: ArcadePoint[]; food?: ArcadePoint | null;
  active?: { type: string; cells: ArcadePoint[] }; next?: string[]; hold?: string | null; pieceId?: number;
  player?: ArcadeRect & { vx: number; vy: number; onGround: boolean };
  platforms?: ArcadeRect[]; hazards?: ArcadeRect[]; coins?: ArcadePoint[]; enemies?: ArcadeRect[]; exit?: ArcadeRect; level?: number;
}
export interface ArcadeAdapter {
  id: ArcadeGameId; name: string; rulesVersion: string; rules: string;
  create(config: ArcadeConfig, seed: string): ArcadeState;
  candidates(state: ArcadeState): ArcadeAction[];
  input(state: ArcadeState, input: ArcadeInput): ArcadeState;
  /** System pause/end releases held keys and cancels queued press edges. Ordinary key-up does not. */
  release(state: ArcadeState): ArcadeState;
  step(state: ArcadeState, ticks?: number): ArcadeState;
  observe(state: ArcadeState): unknown;
  view(state: ArcadeState): ArcadeView;
  restore(value: unknown): ArcadeState;
  /** Positive means left wins; zero is a tie. */
  compare(left: ArcadeScore, right: ArcadeScore): number;
}
export interface ArcadeConfig { level?: 1 | 2 | 3 }
export interface CreateArcadeInput {
  gameId: ArcadeGameId; mode: ArcadeMode; config: ArcadeConfig; seed?: string;
  controllers: SeatConfig[];
}
export type ArcadeStatus = 'ready' | 'running' | 'paused' | 'finished' | 'error';
export interface ArcadeResult { winner: 0 | 1 | null; reason: string; scores: ArcadeScore[]; aborted: boolean }
export interface ArcadeEvent {
  seq: number; type: 'created' | 'input' | 'decision' | 'status' | 'ended' | 'error'; tick: number; at: number;
  runId?: string; input?: ArcadeInput; release?: true; actionId?: string; observedTick?: number; appliedTick?: number;
  requestId?: string; outcome?: 'applied' | 'dropped' | 'failed' | 'unknown'; message?: string;
  /** Model-facing protocol version for this request; absent in older recordings. */
  promptVersion?: string;
  metrics?: DecisionMetrics; reasoning?: DecisionReasoning;
}
export interface ArcadeRunSnapshot {
  id: string; controller: PublicSeatConfig; view: ArcadeView; calls: number;
  /** Owner-only human render prediction; never accepted as input or included in public replay. */
  predictionState?: ArcadeState;
  inflight?: { requestId: string; observedTick: number; startedAt: number };
  lastDecision?: { observedTick: number; appliedTick?: number; outcome: 'applied' | 'dropped' | 'failed' | 'unknown'; metrics?: DecisionMetrics; reasoning?: DecisionReasoning };
}
export interface ArcadeSession {
  id: string; engineKind: 'arcade'; schemaVersion: 2; gameId: ArcadeGameId; mode: ArcadeMode;
  config: ArcadeConfig; seed: string; rulesVersion: string; status: ArcadeStatus; version: number; tick: number;
  runs: ArcadeRunSnapshot[]; events: ArcadeEvent[]; result?: ArcadeResult; error?: string; needsRetry?: boolean;
  limits: { maxTicks: number; maxCallsPerRun: number; minRequestIntervalMs: number };
  createdAt: number; updatedAt: number;
}
export interface ArcadeCommand { type: 'start' | 'pause' | 'resume' | 'retry' | 'end'; operationId: string }
export type ArcadeClientMessage =
  | { type: 'claim'; runId: string }
  | { type: 'heartbeat'; controllerSession: string }
  | { type: 'input'; runId: string; controllerSession: string; clientSeq: number; input: ArcadeInput };
export type ArcadeServerMessage =
  | { type: 'snapshot'; snapshot: ArcadeSession }
  | { type: 'controller'; runId: string; controllerSession: string }
  | { type: 'ack'; runId: string; clientSeq: number; appliedTick: number }
  | { type: 'error'; message: string };
export interface ArcadeReplayFrame { tick: number; views: ArcadeView[] }
export interface ArcadeReplayV2 {
  version: 2; engineKind: 'arcade'; matchId: string; gameId: ArcadeGameId; mode: ArcadeMode;
  rulesVersion: string; promptVersion: string; seed: string; config: ArcadeConfig;
  controllers: PublicSeatConfig[]; runIds: string[]; result: ArcadeResult; createdAt: number; finishedAt: number;
  maxTicks: number; frames: ArcadeReplayFrame[]; events: ArcadeEvent[];
}
export const ARCADE_TICK_RATE = 60;
export const ARCADE_MAX_TICKS = 120 * ARCADE_TICK_RATE;
export const ARCADE_MAX_CALLS = 300;
export const ARCADE_MIN_REQUEST_MS = 500;
export const ARCADE_GAME_IDS: readonly ArcadeGameId[] = ['snake', 'tetris', 'platformer'];
