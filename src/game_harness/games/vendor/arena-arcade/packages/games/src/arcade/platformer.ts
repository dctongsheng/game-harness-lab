import type { ArcadeAdapter, ArcadeButton, ArcadePoint, ArcadeRect, ArcadeState } from '@decision-arena/core';
import { actions, advance, baseState, clearInput, finish, integer, observationClock, observationControls, setInput, validInput } from './common';

// Integer fixed-point physics: one tile = 256 units; render projection = 32 pixels/tile.
const TILE = 256, SCALE = 8, WIDTH = 48 * TILE, HEIGHT = 12 * TILE;
const PLAYER_WIDTH = 192, PLAYER_HEIGHT = 224, START_X = 2 * TILE + 32, START_CENTER = START_X + PLAYER_WIDTH / 2;
const BUTTONS: ArcadeButton[] = ['left', 'right', 'jump'];
interface Player extends ArcadeRect { vx: number; vy: number; onGround: boolean }
interface Patrol { minX: number; maxX: number; y: number; speed: number }
interface Level { platforms: ArcadeRect[]; hazards: ArcadeRect[]; coins: ArcadePoint[]; enemies: Patrol[]; exit: ArcadeRect }
export interface PlatformerState extends ArcadeState {
  gameId: 'platformer'; level: 1 | 2 | 3; player: Player; pendingEdges: ArcadeButton[]; remainingCoins: number[]; maxX: number;
}
const block = (x: number, y: number, width = 1, height = 1): ArcadeRect => ({ x: x * TILE, y: y * TILE, width: width * TILE, height: height * TILE });
const exit = block(45, 8, 1, 2);
/** Original hand-authored maps; no procedural obstacles, copyrighted sprites, or external assets. */
const LEVELS: Record<1 | 2 | 3, Level> = {
  1: { platforms: [block(0, 10, 48, 2), block(12, 9), block(24, 9), block(36, 9)], hazards: [],
    coins: [{ x: 12.5 * TILE, y: 7.5 * TILE }, { x: 24.5 * TILE, y: 7.5 * TILE }, { x: 36.5 * TILE, y: 7.5 * TILE }], enemies: [], exit },
  2: { platforms: [block(0, 10, 12, 2), block(14, 10, 10, 2), block(26, 10, 10, 2), block(38, 10, 10, 2)], hazards: [],
    coins: [{ x: 13 * TILE, y: 7.5 * TILE }, { x: 25 * TILE, y: 7.5 * TILE }, { x: 37 * TILE, y: 7.5 * TILE }], enemies: [], exit },
  3: { platforms: [block(0, 10, 48, 2)], hazards: [],
    coins: [{ x: 12.5 * TILE, y: 7.5 * TILE }, { x: 25.5 * TILE, y: 7.5 * TILE }, { x: 38 * TILE, y: 7.5 * TILE }],
    enemies: [{ minX: 10 * TILE, maxX: 15 * TILE, y: 10 * TILE - 192, speed: 8 },
      { minX: 23 * TILE, maxX: 28 * TILE, y: 10 * TILE - 192, speed: 8 },
      { minX: 36 * TILE, maxX: 40 * TILE, y: 10 * TILE - 192, speed: 8 }], exit },
};
function intersects(a: ArcadeRect, b: ArcadeRect): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}
function enemies(s: PlatformerState): ArcadeRect[] {
  return LEVELS[s.level].enemies.map(e => {
    const distance = e.maxX - e.minX, phase = (s.tick * e.speed) % (2 * distance);
    return { x: e.minX + (phase <= distance ? phase : 2 * distance - phase), y: e.y, width: 192, height: 192 };
  });
}
const renderRect = (r: ArcadeRect): ArcadeRect => ({ x: r.x / SCALE, y: r.y / SCALE, width: r.width / SCALE, height: r.height / SCALE });
function updateScore(s: PlatformerState): void {
  s.score.progress = Math.min(100, Math.floor(Math.max(0, s.maxX - START_CENTER) * 10000 / (exit.x - START_CENTER)) / 100);
  s.score.coins = LEVELS[s.level].coins.length - s.remainingCoins.length;
  s.score.points = Math.floor(s.score.progress * 10) + s.score.coins * 100 + (s.score.completed ? 10000 : 0);
}
function frame(s: PlatformerState): void {
  const p = s.player, level = LEVELS[s.level], held = new Set(s.held), pressed = new Set(s.pendingEdges);
  s.pendingEdges = [];
  const direction = Number(held.has('right')) - Number(held.has('left')) || Number(pressed.has('right')) - Number(pressed.has('left'));
  if (direction) p.vx = Math.max(-24, Math.min(24, p.vx + direction * 4));
  else p.vx = Math.sign(p.vx) * Math.max(0, Math.abs(p.vx) - 6);
  if (pressed.has('jump') && p.onGround) { p.vy = -84; p.onGround = false; }
  p.vy = Math.min(128, p.vy + 4);
  p.x += p.vx;
  for (const solid of level.platforms) if (intersects(p, solid)) {
    if (p.vx > 0) p.x = solid.x - p.width;
    else if (p.vx < 0) p.x = solid.x + solid.width;
    p.vx = 0;
  }
  if (p.x < 0) { p.x = 0; p.vx = 0; }
  if (p.x + p.width > WIDTH) { p.x = WIDTH - p.width; p.vx = 0; }
  p.y += p.vy; p.onGround = false;
  for (const solid of level.platforms) if (intersects(p, solid)) {
    if (p.vy > 0) { p.y = solid.y - p.height; p.onGround = true; }
    else if (p.vy < 0) p.y = solid.y + solid.height;
    p.vy = 0;
  }
  s.maxX = Math.max(s.maxX, Math.min(exit.x, p.x + p.width / 2));
  // A fatal collision wins over a goal/coin collision on the same tick.
  if (p.y >= HEIGHT || level.hazards.some(h => intersects(p, h))) finish(s, p.y >= HEIGHT ? 'FELL_IN_PIT' : 'HAZARD_COLLISION');
  else if (enemies(s).some(enemy => intersects(p, enemy))) finish(s, 'ENEMY_COLLISION');
  if (s.status === 'playing') {
    s.remainingCoins = s.remainingCoins.filter(i => { const c = level.coins[i]; return !intersects(p, { x: c.x - 64, y: c.y - 64, width: 128, height: 128 }); });
    if (intersects(p, level.exit)) { s.score.completed = true; s.maxX = exit.x; finish(s, 'GOAL_REACHED'); }
  }
  updateScore(s);
}
export const platformer: ArcadeAdapter = {
  id: 'platformer', name: '平台跳跃', rulesVersion: 'platformer-realtime-v1',
  rules: '原创3个可选固定关卡：1教学障碍、2坑洞、3移动敌人。单关一命、120秒；碰敌即死，不能踩敌。60Hz整数物理，左右加速，跳跃仅在着地且按键新按下时触发，保持跳跃不会自动连跳。先判断致死碰撞，再判断出口。AI按键100/300/1000ms后自动释放，无自动避险。比较已通关优先；双方通关比较更少tick；否则比较最远进度、金币数、存活tick。',
  create(config): PlatformerState {
    const level = config.level ?? 1;
    if (![1, 2, 3].includes(level)) throw new Error('平台关卡必须为1、2或3');
    return { gameId: 'platformer', tick: 0, status: 'playing', score: { points: 0, coins: 0, progress: 0, completed: false, survivedTicks: 0 },
      held: [], releaseTick: null, level, player: { x: START_X, y: 10 * TILE - PLAYER_HEIGHT, width: PLAYER_WIDTH, height: PLAYER_HEIGHT, vx: 0, vy: 0, onGround: true },
      pendingEdges: [], remainingCoins: [0, 1, 2], maxX: START_CENTER };
  },
  candidates(state) {
    if (state.status !== 'playing') return [];
    return actions([{ buttons: [], label: '释放所有按键，惯性和重力继续', durations: [100] }, { buttons: ['left'], label: '持续向左移动' }, { buttons: ['right'], label: '持续向右移动' },
      { buttons: ['jump'], label: '尝试跳跃一次（着地且跳键新按下时）', durations: [100] },
      { buttons: ['left', 'jump'], label: '持续向左移动，并尝试跳跃一次（着地且跳键新按下时，不自动连跳）' },
      { buttons: ['right', 'jump'], label: '持续向右移动，并尝试跳跃一次（着地且跳键新按下时，不自动连跳）' }]);
  },
  input(state, input) {
    validInput(input, BUTTONS);
    if (input.buttons.includes('left') && input.buttons.includes('right')) throw new Error('不能同时向左和向右');
    return setInput(state as PlatformerState, input);
  },
  release(state) { return clearInput(state as PlatformerState); },
  step(state, ticks) { return advance(state as PlatformerState, ticks, frame); },
  observe(state) {
    const s = state as PlatformerState, view = platformer.view(s);
    return { ...view, ...observationClock(s),
      coordinates: '所有位置、宽高和距离统一为像素(px)。左上角为(0,0)，x向右、y向下；矩形x/y是左上角，右边界x+width、底边界y+height。金币坐标是中心。世界坐标与屏幕滚动无关。',
      units: { position: 'px', velocity: 'px/tick', acceleration: 'px/tick²', tickSeconds: 1 / 60, tilePixels: 32 },
      physics: { acceleration: 0.5, maxSpeed: 3, friction: 0.75, gravity: 0.5, jumpVelocity: -10.5, terminalFallSpeed: 16,
        units: 'acceleration/friction/gravity 是 px/tick²；maxSpeed/jumpVelocity/terminalFallSpeed 是 px/tick。负 vy 向上。',
        updateOrder: '每tick先更新水平速度；着地且jump新按下时设置vy=-10.5；再加重力0.5（上限16），随后移动并处理碰撞。松开横移后按friction减速，不是立即静止。' },
      geometry: { platforms: '实心矩形，可站立并阻挡水平/垂直移动；没有地面的区域可下落', hazards: '接触即结束的矩形',
        enemies: '接触即结束的移动矩形，不能踩敌', coins: '中心附近16×16px收集区域', exit: '接触即可完成的矩形' },
      enemies: view.enemies!.map((enemy, index) => {
        const patrol = LEVELS[s.level].enemies[index], distance = patrol.maxX - patrol.minX;
        const direction = (s.tick * patrol.speed) % (2 * distance) < distance ? 1 : -1;
        return { ...enemy, vx: direction * patrol.speed / SCALE, vy: 0,
          motion: '水平往返匀速移动，到巡逻端点反向；vx表示当前朝向，端点处为反向后的朝向',
          patrolMinX: patrol.minX / SCALE, patrolMaxX: patrol.maxX / SCALE };
      }),
      controls: { ...observationControls(s), jumpRequiresNewPress: true,
        jumpSemantics: 'jump只在着地且新按下时尝试一次；空中按下不会排队等落地跳。移动+jump中，横向键持续指定时长，jump不自动重复；已按住jump时重复同一组合不会产生新跳跃。' },
      maxX: s.maxX / SCALE, maxXMeaning: '角色中心曾到达的最远x，用于进度计分，不是当前位置' };
  },
  view(state) {
    const s = state as PlatformerState, level = LEVELS[s.level];
    return { gameId: 'platformer', tick: s.tick, status: s.status, score: { ...s.score }, width: WIDTH / SCALE, height: HEIGHT / SCALE,
      currentButtons: [...s.held], reason: s.reason, level: s.level,
      player: { ...renderRect(s.player), vx: s.player.vx / SCALE, vy: s.player.vy / SCALE, onGround: s.player.onGround },
      platforms: level.platforms.map(renderRect), hazards: level.hazards.map(renderRect),
      coins: s.remainingCoins.map(i => ({ x: level.coins[i].x / SCALE, y: level.coins[i].y / SCALE })), enemies: enemies(s).map(renderRect), exit: renderRect(level.exit) };
  },
  restore(value) {
    const s = baseState(value, 'platformer', BUTTONS) as PlatformerState;
    if (![1, 2, 3].includes(s.level)) throw new Error('平台关卡存档无效');
    validInput({ buttons: s.pendingEdges }, BUTTONS);
    const p = s.player;
    if (!p || p.width !== PLAYER_WIDTH || p.height !== PLAYER_HEIGHT || !integer(p.x, 0, WIDTH - PLAYER_WIDTH) ||
      !integer(p.y, -TILE, HEIGHT + 128) || !integer(p.vx, -24, 24) || !integer(p.vy, -84, 128) || typeof p.onGround !== 'boolean' ||
      !integer(s.maxX, START_CENTER, exit.x) || !Array.isArray(s.remainingCoins) || s.remainingCoins.length > 3 ||
      !s.remainingCoins.every(i => integer(i, 0, 2)) || new Set(s.remainingCoins).size !== s.remainingCoins.length ||
      typeof s.score.completed !== 'boolean' || !Number.isFinite(s.score.progress)) throw new Error('平台角色存档无效');
    const score = { ...s.score }; updateScore(s);
    if (s.score.points !== score.points || s.score.coins !== score.coins || s.score.progress !== score.progress ||
      (s.score.completed && (s.status !== 'finished' || s.reason !== 'GOAL_REACHED'))) throw new Error('平台分数存档无效');
    return s;
  },
  compare(a, b) {
    if (!!a.completed !== !!b.completed) return Number(!!a.completed) - Number(!!b.completed);
    if (a.completed && b.completed) return b.survivedTicks - a.survivedTicks;
    return (a.progress ?? 0) - (b.progress ?? 0) || (a.coins ?? 0) - (b.coins ?? 0) || a.survivedTicks - b.survivedTicks;
  },
};
