export type Seat = 0 | 1;
export type GameId = 'gomoku' | 'xiangqi' | 'army';
export type Viewer = Seat | 'spectator' | 'full';
export type GameResult = { winner: Seat | null; reason: string };
export interface Action { id: string; label: string; from?: number; to?: number }
export interface Piece { id: string; seat: Seat; kind: string; label: string; hidden?: boolean }
export interface GameView {
  gameId: GameId; rows: number; cols: number; cells: (Piece | null)[];
  turn: Seat; phase: 'setup' | 'playing' | 'finished'; ply: number;
  legalActions: Action[]; result?: GameResult; ready?: [boolean, boolean];
}
export interface GameState { gameId: GameId; turn: Seat; phase: 'setup' | 'playing' | 'finished'; ply: number; result?: GameResult; [key: string]: unknown }
export interface GameAdapter {
  id: GameId; name: string; rulesVersion: string; rules: string;
  create(config: Record<string, unknown>, seed: string): GameState;
  legalActions(state: GameState, seat: Seat): Action[];
  apply(state: GameState, seat: Seat, actionId: string): GameState;
  observe(state: GameState, seat: Seat): unknown;
  view(state: GameState, viewer: Viewer): GameView;
  restore(state: unknown): GameState;
}
export type ProviderId = 'openrouter' | 'systemone' | 'custom-openrouter' | 'custom-systemone';
export interface PublicModelProfile { name: string; provider: ProviderId; model: string; thinkMode?: 'never' | 'auto' | 'always'; thinkBudget?: number }
export interface ModelProfile extends PublicModelProfile { id: string; ownerId: string; endpoint: string; credentialId: string }
export interface SeatConfig { kind: 'human' | 'ai'; profile?: ModelProfile }
export interface PublicSeatConfig { kind: 'human' | 'ai'; profile?: PublicModelProfile }
export interface DecisionMetrics { elapsedMs: number; actualModel?: string; confidence?: number; probabilities?: Record<string, number>; usage?: Record<string, number>; thoughtUsed?: boolean }
/** Provider-returned text for private match views only; never a public replay field. */
export interface DecisionReasoning { text: string; truncated: boolean }
export interface DecisionRequest { state: unknown; actions: Action[]; instructions: string }
export interface DecisionResponse { actionId: string; metrics: DecisionMetrics; reasoning?: DecisionReasoning }
export interface ProviderAdapter { decide(profile: ModelProfile, key: string, request: DecisionRequest, signal?: AbortSignal): Promise<DecisionResponse> }
export interface MatchEvent { seq: number; type: 'created' | 'action' | 'status' | 'error'; at: number; seat?: Seat; action?: Action; metrics?: DecisionMetrics; reasoning?: DecisionReasoning; message?: string; view?: GameView }
export type MatchStatus = 'ready' | 'running' | 'paused' | 'finished' | 'error';
export interface MatchSnapshot {
  id: string; gameId: GameId; status: MatchStatus; version: number; view: GameView;
  seats: [PublicSeatConfig, PublicSeatConfig]; events: MatchEvent[]; error?: string;
  limits: { maxPlies: number; maxActiveMs: number; activeMs: number };
}
export interface ReplayManifest {
  version: 1; matchId: string; gameId: GameId; rulesVersion: string; promptVersion: string;
  seed: string; config: Record<string, unknown>; seats: [PublicSeatConfig, PublicSeatConfig];
  result: GameResult; createdAt: number; finishedAt: number; frames: GameView[]; events: MatchEvent[];
}
export interface CreateMatchInput { gameId: GameId; config: Record<string, unknown>; seats: [SeatConfig, SeatConfig]; seed?: string }
export interface MatchCommand { type: 'action' | 'start' | 'step' | 'pause' | 'resume' | 'end'; actionId?: string; seat?: Seat; version?: number; operationId: string }
export function publicProfile(p: ModelProfile): PublicModelProfile { return { name:p.name, provider:p.provider, model:p.model, ...(p.thinkMode ? {thinkMode:p.thinkMode}:{}), ...(p.thinkBudget ? {thinkBudget:p.thinkBudget}:{}) }; }
export function publicSeats(seats: [SeatConfig, SeatConfig]): [PublicSeatConfig, PublicSeatConfig] { return seats.map(s => ({kind:s.kind, ...(s.profile ? {profile:publicProfile(s.profile)} : {})})) as [PublicSeatConfig, PublicSeatConfig]; }
export function assertSeat(value: unknown): asserts value is Seat { if(value !== 0 && value !== 1) throw new Error('Invalid seat'); }
export * from './arcade';
