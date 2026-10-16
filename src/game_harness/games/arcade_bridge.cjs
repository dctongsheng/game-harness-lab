/* Includes Decision Arena arcade engines: Copyright (c) 2026 Dougo, MIT. Full license: vendor/arena-arcade/LICENSE. */
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// arcade_bridge.ts
var arcade_bridge_exports = {};
__export(arcade_bridge_exports, {
  platformer: () => platformer,
  tetris: () => tetris
});
module.exports = __toCommonJS(arcade_bridge_exports);
var import_node_readline = require("node:readline");

// vendor/arena-arcade/packages/core/src/arcade.ts
var ARCADE_TICK_RATE = 60;
var ARCADE_MAX_TICKS = 120 * ARCADE_TICK_RATE;

// vendor/arena-arcade/packages/games/src/arcade/common.ts
var DURATIONS = [100, 300, 1e3];
function seedNumber(seed) {
  let n = 2166136261;
  for (let i = 0; i < seed.length; i++) n = Math.imul(n ^ seed.charCodeAt(i), 16777619) >>> 0;
  return n;
}
function random(state2) {
  state2.rng = Math.imul(state2.rng, 1664525) + 1013904223 >>> 0;
  return state2.rng;
}
function actions(choices) {
  return choices.flatMap((choice) => (choice.durations ?? DURATIONS).map((durationMs) => ({
    id: `${choice.buttons.join("+") || "noop"}:${durationMs}`,
    label: `${choice.label}\uFF0C${durationMs} \u6BEB\u79D2`,
    input: { buttons: [...choice.buttons], durationMs }
  })));
}
function observationClock(state2) {
  return {
    tick: state2.tick,
    tickRate: 60,
    ticksRemaining: ARCADE_MAX_TICKS - state2.tick,
    secondsRemaining: Number(((ARCADE_MAX_TICKS - state2.tick) / 60).toFixed(3))
  };
}
function observationControls(state2) {
  return {
    held: [...state2.held],
    releaseTick: state2.releaseTick,
    releaseInTicks: state2.releaseTick === null ? null : state2.releaseTick - state2.tick,
    pendingEdges: Array.isArray(state2.pendingEdges) ? [...state2.pendingEdges] : [],
    semantics: "held \u662F\u5F53\u524D\u6309\u4F4F\u7684\u952E\uFF1BpendingEdges \u662F\u5DF2\u6536\u5230\u4F46\u5C1A\u672A\u5728\u4E0B\u4E00\u7269\u7406 tick \u6D88\u8D39\u7684\u65B0\u6309\u4E0B\u3002\u65B0\u8F93\u5165\u66FF\u6362\u6574\u7EC4 held\uFF1B\u5230\u671F\u53EA\u677E\u952E\uFF0C\u4E0D\u6682\u505C\u6E38\u620F\u3002\u5DF2\u7ECF\u6309\u4F4F\u7684\u952E\u518D\u6B21\u63D0\u4EA4\u4E0D\u4EA7\u751F\u65B0\u7684\u6309\u4E0B\u8FB9\u6CBF\u3002"
  };
}
function validInput(input, allowed) {
  if (!input || !Array.isArray(input.buttons) || input.buttons.length > allowed.length || new Set(input.buttons).size !== input.buttons.length || input.buttons.some((b) => !allowed.includes(b))) throw new Error("\u65E0\u6548\u7684\u6E38\u620F\u6309\u952E");
  if (input.durationMs !== void 0 && !DURATIONS.includes(input.durationMs)) throw new Error("\u6309\u952E\u65F6\u957F\u5FC5\u987B\u4E3A 100\u3001300 \u6216 1000 \u6BEB\u79D2");
}
function setInput(state2, input) {
  if (state2.status !== "playing") throw new Error("\u672C\u5C40\u5DF2\u7ECF\u7ED3\u675F");
  const copy = structuredClone(state2);
  copy.held = [...input.buttons];
  const controls = copy;
  if (Array.isArray(controls.pendingEdges)) controls.pendingEdges = [.../* @__PURE__ */ new Set([...controls.pendingEdges, ...input.buttons.filter((button) => !state2.held.includes(button))])];
  copy.releaseTick = input.durationMs === void 0 ? null : state2.tick + input.durationMs * 60 / 1e3;
  return copy;
}
function release(state2) {
  state2.held = [];
  state2.releaseTick = null;
}
function clearInput(state2) {
  const copy = structuredClone(state2);
  release(copy);
  const controls = copy;
  if (Array.isArray(controls.pendingEdges)) controls.pendingEdges = [];
  if ("queued" in controls) controls.queued = null;
  return copy;
}
function finish(state2, reason) {
  state2.status = "finished";
  state2.reason = reason;
  release(state2);
  if (Array.isArray(state2.pendingEdges)) state2.pendingEdges = [];
  if ("queued" in state2) state2.queued = null;
}
function advance(state2, ticks, frame3) {
  const count = ticks ?? 1;
  if (!Number.isInteger(count) || count < 0 || count > ARCADE_MAX_TICKS) throw new Error("\u65E0\u6548\u7684 tick \u6570");
  const copy = structuredClone(state2);
  for (let i = 0; i < count && copy.status === "playing"; i++) {
    if (copy.releaseTick !== null && copy.tick >= copy.releaseTick) release(copy);
    copy.tick++;
    frame3(copy);
    copy.score.survivedTicks = copy.tick;
    if (copy.status === "playing" && copy.tick >= ARCADE_MAX_TICKS) finish(copy, "TIME_LIMIT");
    if (copy.releaseTick !== null && copy.tick >= copy.releaseTick) release(copy);
  }
  return copy;
}
function integer(value, min, max) {
  return typeof value === "number" && Number.isInteger(value) && value >= min && value <= max;
}
function baseState(value, gameId, allowed) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("\u5B58\u6863\u683C\u5F0F\u65E0\u6548");
  const s = value;
  if (s.gameId !== gameId || !integer(s.tick, 0, ARCADE_MAX_TICKS) || !["playing", "finished"].includes(s.status) || !s.score || typeof s.score !== "object" || !integer(s.score.survivedTicks, 0, ARCADE_MAX_TICKS) || s.score.survivedTicks !== s.tick || !Number.isFinite(s.score.points) || s.score.points < 0 || s.releaseTick !== null && !integer(s.releaseTick, s.tick, s.tick + 60)) throw new Error("\u5B58\u6863\u683C\u5F0F\u65E0\u6548");
  validInput({ buttons: s.held }, allowed);
  if (s.status === "playing" && (s.tick >= ARCADE_MAX_TICKS || s.reason !== void 0)) throw new Error("\u5B58\u6863\u72B6\u6001\u65E0\u6548");
  if (s.status === "finished" && (typeof s.reason !== "string" || s.held.length !== 0 || s.releaseTick !== null)) throw new Error("\u5B58\u6863\u7ED3\u675F\u72B6\u6001\u65E0\u6548");
  return structuredClone(s);
}

// vendor/arena-arcade/packages/games/src/arcade/tetris.ts
var TETROMINOES = ["I", "O", "T", "S", "Z", "J", "L"];
var BUTTONS = ["left", "right", "rotate-cw", "rotate-ccw", "soft-drop", "hard-drop", "hold"];
var SHAPES = {
  I: [[0, 1], [1, 1], [2, 1], [3, 1]],
  O: [[1, 0], [2, 0], [1, 1], [2, 1]],
  T: [[1, 0], [0, 1], [1, 1], [2, 1]],
  S: [[1, 0], [2, 0], [0, 1], [1, 1]],
  Z: [[0, 0], [1, 0], [1, 1], [2, 1]],
  J: [[0, 0], [0, 1], [1, 1], [2, 1]],
  L: [[2, 0], [0, 1], [1, 1], [2, 1]]
};
var KICKS = {
  "01": [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
  "10": [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
  "12": [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
  "21": [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
  "23": [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]],
  "32": [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
  "30": [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
  "03": [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]]
};
var I_KICKS = {
  "01": [[0, 0], [-2, 0], [1, 0], [-2, -1], [1, 2]],
  "10": [[0, 0], [2, 0], [-1, 0], [2, 1], [-1, -2]],
  "12": [[0, 0], [-1, 0], [2, 0], [-1, 2], [2, -1]],
  "21": [[0, 0], [1, 0], [-2, 0], [1, -2], [-2, 1]],
  "23": [[0, 0], [2, 0], [-1, 0], [2, 1], [-1, -2]],
  "32": [[0, 0], [-2, 0], [1, 0], [-2, -1], [1, 2]],
  "30": [[0, 0], [1, 0], [-2, 0], [1, -2], [-2, 1]],
  "03": [[0, 0], [-1, 0], [2, 0], [-1, 2], [2, -1]]
};
function tetrisCells(piece) {
  return SHAPES[piece.type].map(([x0, y0]) => {
    let x = x0, y = y0;
    if (piece.type !== "O") for (let r = 0; r < piece.rotation; r++) [x, y] = [(piece.type === "I" ? 3 : 2) - y, x];
    return { x: piece.x + x, y: piece.y + y };
  });
}
function fits(s, piece) {
  return tetrisCells(piece).every((p) => p.x >= 0 && p.x < 10 && p.y >= -2 && p.y < 20 && s.board[(p.y + 2) * 10 + p.x] === 0);
}
function grounded(s) {
  return !!s.active && !fits(s, { ...s.active, y: s.active.y + 1 });
}
function fillQueue(s) {
  while (s.next.length < 7) {
    const bag = [...TETROMINOES];
    for (let i = bag.length - 1; i > 0; i--) {
      const j = random(s) % (i + 1);
      [bag[i], bag[j]] = [bag[j], bag[i]];
    }
    s.next.push(...bag);
  }
}
function spawn(s, type) {
  if (!type) {
    fillQueue(s);
    type = s.next.shift();
    fillQueue(s);
  }
  s.active = { type, rotation: 0, x: 3, y: -2 };
  s.pieceId++;
  s.fallTicks = 0;
  s.lockTicks = 0;
  s.lockResets = 0;
  if (!fits(s, s.active)) finish(s, "SPAWN_BLOCKED");
}
function lock(s) {
  if (!s.active) return;
  const color = TETROMINOES.indexOf(s.active.type) + 1;
  for (const p of tetrisCells(s.active)) s.board[(p.y + 2) * 10 + p.x] = color;
  const rows = [];
  let cleared = 0;
  for (let y = 0; y < 22; y++) {
    const row = s.board.slice(y * 10, y * 10 + 10);
    if (row.every(Boolean)) cleared++;
    else rows.push(row);
  }
  while (rows.length < 22) rows.unshift(Array(10).fill(0));
  s.board = rows.flat();
  const level = Math.floor((s.score.lines ?? 0) / 10) + 1;
  s.score.lines = (s.score.lines ?? 0) + cleared;
  s.score.points += ([0, 100, 300, 500, 800][cleared] ?? 0) * level;
  s.holdUsed = false;
  s.active = null;
  if (s.board.slice(0, 20).some(Boolean)) {
    finish(s, "TOP_OUT");
    return;
  }
  spawn(s);
}
function resetLock(s, wasGrounded) {
  if (wasGrounded && s.lockResets < 15) {
    s.lockTicks = 0;
    s.lockResets++;
  }
}
function move(s, dx) {
  if (!s.active) return;
  const oldGrounded = grounded(s), next = { ...s.active, x: s.active.x + dx };
  if (fits(s, next)) {
    s.active = next;
    resetLock(s, oldGrounded);
  }
}
function rotate(s, clockwise) {
  if (!s.active) return;
  const old = s.active, nextRotation = (old.rotation + (clockwise ? 1 : 3)) % 4;
  const offsets = old.type === "O" ? [[0, 0]] : (old.type === "I" ? I_KICKS : KICKS)[`${old.rotation}${nextRotation}`];
  const oldGrounded = grounded(s);
  for (const [dx, dy] of offsets) {
    const next = { ...old, rotation: nextRotation, x: old.x + dx, y: old.y - dy };
    if (fits(s, next)) {
      s.active = next;
      resetLock(s, oldGrounded);
      return;
    }
  }
}
function tetrisGravityTicks(s) {
  return Math.max(6, 48 - Math.floor((s.score.lines ?? 0) / 10) * 3);
}
function frame(s) {
  const held = new Set(s.held), pressed = new Set(s.pendingEdges), edge = (button) => pressed.has(button);
  s.pendingEdges = [];
  if (edge("hold") && !s.holdUsed && s.active) {
    const type = s.active.type, stored = s.hold;
    s.hold = type;
    s.holdUsed = true;
    spawn(s, stored ?? void 0);
  }
  if (s.status !== "playing") return;
  if (edge("rotate-cw")) rotate(s, true);
  if (edge("rotate-ccw")) rotate(s, false);
  const direction = (held.has("right") ? 1 : 0) - (held.has("left") ? 1 : 0);
  if (direction !== s.horizontal) {
    s.horizontal = direction;
    s.horizontalTicks = 0;
    if (direction) move(s, direction);
  } else if (direction) {
    s.horizontalTicks++;
    if (s.horizontalTicks >= 9 && (s.horizontalTicks - 9) % 3 === 0) move(s, direction);
  }
  if (!direction && edge("left") !== edge("right")) move(s, edge("left") ? -1 : 1);
  if (edge("hard-drop") && s.active) {
    while (fits(s, { ...s.active, y: s.active.y + 1 })) s.active.y++;
    lock(s);
    return;
  }
  if (s.active) {
    s.fallTicks++;
    const interval = held.has("soft-drop") ? Math.min(3, tetrisGravityTicks(s)) : tetrisGravityTicks(s);
    if (s.fallTicks >= interval) {
      s.fallTicks = 0;
      if (fits(s, { ...s.active, y: s.active.y + 1 })) s.active.y++;
    }
    if (grounded(s)) {
      s.lockTicks++;
      if (s.lockTicks >= 30) lock(s);
    }
  }
}
var tetris = {
  id: "tetris",
  name: "\u4FC4\u7F57\u65AF\u65B9\u5757",
  rulesVersion: "tetris-realtime-v1",
  rules: "10\xD720\u53EF\u89C1\u683C\u3001\u9876\u90E82\u9690\u85CF\u884C\uFF1B7-bag\u3001\u9884\u89C85\u5757\u3001\u6BCF\u5757\u53EFhold\u4E00\u6B21\u3002\u521D\u59CB800ms\u4E0B\u4E00\u884C\uFF0C\u6BCF\u6D8810\u884C\u51CF50ms\uFF0C\u6700\u4F4E100ms\uFF1B\u63A5\u5730\u9501\u5B9A500ms\uFF0C\u79FB\u52A8/\u65CB\u8F6C\u6700\u591A\u91CD\u7F6E15\u6B21\u3002\u5DE6\u53F3\u6309\u4F4F150ms\u540E\u6BCF50ms\u91CD\u590D\uFF1B\u8F6F\u964D20\u884C/\u79D2\u3002SRS\u987A/\u9006\u65F6\u9488\u65CB\u8F6C\uFF1B\u65CB\u8F6C\u3001hold\u548C\u786C\u964D\u53EA\u5728\u6309\u4E0B\u8FB9\u6CBF\u89E6\u53D1\u3002\u6D881/2/3/4\u884C\u5F97100/300/500/800\u4E58\u5F53\u524D\u7B49\u7EA7\u5206\uFF1B\u7B49\u7EA7\u4E3A1+\u7D2F\u8BA1\u6D88\u884C\u6570\u966410\u53D6\u6574\uFF0C\u4EE5\u6D88\u884C\u524D\u7B49\u7EA7\u8BA1\u5206\u3002\u8F6F\u786C\u964D\u65E0\u5206\u6570\uFF0C\u65E0T-spin/combo\u989D\u5916\u5206\u3002\u9690\u85CF\u884C\u6709\u9501\u5B9A\u65B9\u5757\u6216\u751F\u6210\u53D7\u963B\u7ED3\u675F\uFF0C\u6700\u591A120\u79D2\uFF1B\u6BD4\u8F83\u6D88\u884C\u6570\u3001\u5206\u6570\u3001\u5B58\u6D3Btick\u3002",
  create(_config, seed) {
    const s = {
      gameId: "tetris",
      tick: 0,
      status: "playing",
      score: { points: 0, lines: 0, survivedTicks: 0 },
      held: [],
      releaseTick: null,
      board: Array(220).fill(0),
      active: null,
      next: [],
      hold: null,
      holdUsed: false,
      pieceId: 0,
      rng: seedNumber(seed),
      pendingEdges: [],
      fallTicks: 0,
      lockTicks: 0,
      lockResets: 0,
      horizontal: 0,
      horizontalTicks: 0
    };
    spawn(s);
    return s;
  },
  candidates(state2) {
    const s = state2;
    if (s.status !== "playing") return [];
    const choices = [
      { buttons: [], label: "\u91CA\u653E\u6240\u6709\u6309\u952E\uFF0C\u91CD\u529B\u7EE7\u7EED\uFF0C\u4E0D\u7B49\u5F85\u4E00\u6574\u6BB5\u65F6\u957F", durations: [100] },
      { buttons: ["left"], label: "\u6301\u7EED\u5411\u5DE6\u79FB\u52A8" },
      { buttons: ["right"], label: "\u6301\u7EED\u5411\u53F3\u79FB\u52A8" },
      { buttons: ["rotate-cw"], label: "\u5C1D\u8BD5\u987A\u65F6\u9488\u65CB\u8F6C\u4E00\u6B21\uFF08\u65B0\u6309\u4E0B\u65F6\uFF09", durations: [100] },
      { buttons: ["rotate-ccw"], label: "\u5C1D\u8BD5\u9006\u65F6\u9488\u65CB\u8F6C\u4E00\u6B21\uFF08\u65B0\u6309\u4E0B\u65F6\uFF09", durations: [100] },
      { buttons: ["soft-drop"], label: "\u6301\u7EED\u8F6F\u964D" },
      { buttons: ["hard-drop"], label: "\u786C\u964D\u5E76\u9501\u5B9A\u4E00\u6B21\uFF08\u65B0\u6309\u4E0B\u65F6\uFF09", durations: [100] }
    ];
    if (!s.holdUsed) choices.push({ buttons: ["hold"], label: "\u6682\u5B58/\u4EA4\u6362\u65B9\u5757\u4E00\u6B21\uFF08\u65B0\u6309\u4E0B\u65F6\uFF09", durations: [100] });
    return actions(choices);
  },
  input(state2, input) {
    validInput(input, BUTTONS);
    if (input.buttons.includes("left") && input.buttons.includes("right")) throw new Error("\u4E0D\u80FD\u540C\u65F6\u5411\u5DE6\u548C\u5411\u53F3");
    if (input.buttons.includes("rotate-cw") && input.buttons.includes("rotate-ccw")) throw new Error("\u4E0D\u80FD\u540C\u65F6\u5411\u4E24\u4E2A\u65B9\u5411\u65CB\u8F6C");
    return setInput(state2, input);
  },
  release(state2) {
    return clearInput(state2);
  },
  step(state2, ticks) {
    return advance(state2, ticks, frame);
  },
  observe(state2) {
    const s = state2;
    return {
      gameId: "tetris",
      status: s.status,
      width: 10,
      height: 20,
      hiddenRows: 2,
      ...observationClock(s),
      coordinates: "\u5355\u4F4D\u662F\u683C\uFF1Bx \u4E3A 0..9\uFF0C\u5411\u53F3\u589E\u52A0\uFF1By \u5411\u4E0B\u589E\u52A0\uFF0C-2\u3001-1 \u662F\u9876\u90E8\u9690\u85CF\u751F\u6210\u884C\uFF0C0..19 \u662F\u53EF\u89C1\u884C\u3002\u6BCF\u884C\u7B2C x \u4E2A\u5B57\u7B26\u5BF9\u5E94\u8BE5\u5217\u3002",
      board: {
        columns: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        rows: Array.from({ length: 22 }, (_, row) => ({ y: row - 2, cells: s.board.slice(row * 10, row * 10 + 10).map((cell) => cell === 0 ? "." : TETROMINOES[cell - 1]).join("") })),
        legend: { ".": "\u7A7A\u683C", I: "\u5DF2\u9501\u5B9A I \u5757", O: "\u5DF2\u9501\u5B9A O \u5757", T: "\u5DF2\u9501\u5B9A T \u5757", S: "\u5DF2\u9501\u5B9A S \u5757", Z: "\u5DF2\u9501\u5B9A Z \u5757", J: "\u5DF2\u9501\u5B9A J \u5757", L: "\u5DF2\u9501\u5B9A L \u5757" },
        contents: "board \u4EC5\u5305\u542B\u5DF2\u9501\u5B9A\u65B9\u5757\uFF1B\u5F53\u524D\u4E0B\u843D\u65B9\u5757\u5355\u72EC\u8BB0\u5F55\u5728 active\uFF0C\u672A\u53E0\u52A0\u8FDB board\u3002\u6CA1\u6709\u9884\u6D4B\u843D\u70B9\u3002"
      },
      active: s.active && {
        ...s.active,
        cells: tetrisCells(s.active),
        coordinateMeaning: "x\u3001y \u662F\u65CB\u8F6C\u5F62\u72B6\u5C40\u90E8\u6846\u7684\u539F\u70B9\uFF0C\u4E0D\u4E00\u5B9A\u662F\u5360\u7528\u683C\uFF1Bcells \u662F\u6B64\u523B\u5B9E\u9645\u5360\u7528\u7684\u7EDD\u5BF9\u683C\u5750\u6807\u3002rotation=0/1/2/3 \u8868\u793A\u751F\u6210\u671D\u5411\u8D77\u987A\u65F6\u9488\u65CB\u8F6C0/90/180/270\u5EA6\u3002"
      },
      next: s.next.slice(0, 5),
      nextOrder: "next[0] \u662F\u4E0B\u4E00\u5757\uFF0C\u4EC5\u663E\u793A\u53EF\u89C1\u7684\u4E94\u5757\u9884\u89C8",
      hold: s.hold,
      holdAvailable: !s.holdUsed,
      pieceId: s.pieceId,
      gravityEveryTicks: tetrisGravityTicks(s),
      fallTicks: s.fallTicks,
      effectiveFallEveryTicks: s.held.includes("soft-drop") ? Math.min(3, tetrisGravityTicks(s)) : tetrisGravityTicks(s),
      grounded: grounded(s),
      lockTicks: s.lockTicks,
      lockLimitTicks: 30,
      lockResets: s.lockResets,
      maxLockResets: 15,
      horizontalDirection: s.horizontal,
      horizontalHeldTicks: s.horizontalTicks,
      controls: {
        ...observationControls(s),
        edgeSemantics: "\u65CB\u8F6C\u3001\u786C\u964D\u3001hold \u53EA\u5728\u65B0\u6309\u4E0B\u7684\u4E0B\u4E00\u7269\u7406 tick \u5C1D\u8BD5\u4E00\u6B21\uFF1B\u6301\u7EED\u6309\u4F4F\u4E0D\u91CD\u590D\uFF0C\u5C1A\u672A\u677E\u5F00\u65F6\u91CD\u590D\u63D0\u4EA4\u540C\u952E\u4E5F\u4E0D\u4F1A\u518D\u89E6\u53D1\u3002\u5DE6\u53F3\u53CA\u8F6F\u964D\u6309\u5019\u9009\u65F6\u957F\u6301\u7EED\u3002noop \u53EA\u677E\u952E\uFF0C\u4ECD\u4F1A\u81EA\u7136\u4E0B\u843D\u3002"
      },
      score: { ...s.score }
    };
  },
  view(state2) {
    const s = state2;
    return {
      gameId: "tetris",
      tick: s.tick,
      status: s.status,
      score: { ...s.score },
      width: 10,
      height: 20,
      currentButtons: [...s.held],
      reason: s.reason,
      cells: s.board.slice(20),
      active: s.active ? { type: s.active.type, cells: tetrisCells(s.active) } : void 0,
      next: s.next.slice(0, 5),
      hold: s.hold,
      pieceId: s.pieceId
    };
  },
  restore(value) {
    const s = baseState(value, "tetris", BUTTONS);
    validInput({ buttons: s.pendingEdges }, BUTTONS);
    if (!Array.isArray(s.board) || s.board.length !== 220 || !s.board.every((n) => integer(n, 0, 7)) || !Array.isArray(s.next) || s.next.length < 5 || s.next.length > 13 || !s.next.every((p) => TETROMINOES.includes(p)) || s.hold !== null && !TETROMINOES.includes(s.hold) || typeof s.holdUsed !== "boolean" || !integer(s.pieceId, 1, 2e4) || !integer(s.rng, 0, 4294967295) || !integer(s.score.lines, 0, 28800) || !integer(s.fallTicks, 0, 47) || !integer(s.lockTicks, 0, 30) || !integer(s.lockResets, 0, 15) || !integer(s.horizontal, -1, 1) || !integer(s.horizontalTicks, 0, 7200)) throw new Error("\u65B9\u5757\u5B58\u6863\u65E0\u6548");
    if (s.active !== null && (!TETROMINOES.includes(s.active.type) || !integer(s.active.rotation, 0, 3) || !integer(s.active.x, -3, 9) || !integer(s.active.y, -2, 19))) throw new Error("\u5F53\u524D\u65B9\u5757\u5B58\u6863\u65E0\u6548");
    if (s.status === "playing" && (!s.active || !fits(s, s.active))) throw new Error("\u5F53\u524D\u65B9\u5757\u78B0\u649E");
    return s;
  },
  compare(a, b) {
    return (a.lines ?? 0) - (b.lines ?? 0) || a.points - b.points || a.survivedTicks - b.survivedTicks;
  }
};

// vendor/arena-arcade/packages/games/src/arcade/platformer.ts
var TILE = 256;
var SCALE = 8;
var WIDTH = 48 * TILE;
var HEIGHT = 12 * TILE;
var PLAYER_WIDTH = 192;
var PLAYER_HEIGHT = 224;
var START_X = 2 * TILE + 32;
var START_CENTER = START_X + PLAYER_WIDTH / 2;
var BUTTONS2 = ["left", "right", "jump"];
var block = (x, y, width = 1, height = 1) => ({ x: x * TILE, y: y * TILE, width: width * TILE, height: height * TILE });
var exit = block(45, 8, 1, 2);
var LEVELS = {
  1: {
    platforms: [block(0, 10, 48, 2), block(12, 9), block(24, 9), block(36, 9)],
    hazards: [],
    coins: [{ x: 12.5 * TILE, y: 7.5 * TILE }, { x: 24.5 * TILE, y: 7.5 * TILE }, { x: 36.5 * TILE, y: 7.5 * TILE }],
    enemies: [],
    exit
  },
  2: {
    platforms: [block(0, 10, 12, 2), block(14, 10, 10, 2), block(26, 10, 10, 2), block(38, 10, 10, 2)],
    hazards: [],
    coins: [{ x: 13 * TILE, y: 7.5 * TILE }, { x: 25 * TILE, y: 7.5 * TILE }, { x: 37 * TILE, y: 7.5 * TILE }],
    enemies: [],
    exit
  },
  3: {
    platforms: [block(0, 10, 48, 2)],
    hazards: [],
    coins: [{ x: 12.5 * TILE, y: 7.5 * TILE }, { x: 25.5 * TILE, y: 7.5 * TILE }, { x: 38 * TILE, y: 7.5 * TILE }],
    enemies: [
      { minX: 10 * TILE, maxX: 15 * TILE, y: 10 * TILE - 192, speed: 8 },
      { minX: 23 * TILE, maxX: 28 * TILE, y: 10 * TILE - 192, speed: 8 },
      { minX: 36 * TILE, maxX: 40 * TILE, y: 10 * TILE - 192, speed: 8 }
    ],
    exit
  }
};
function intersects(a, b) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}
function enemies(s) {
  return LEVELS[s.level].enemies.map((e) => {
    const distance = e.maxX - e.minX, phase = s.tick * e.speed % (2 * distance);
    return { x: e.minX + (phase <= distance ? phase : 2 * distance - phase), y: e.y, width: 192, height: 192 };
  });
}
var renderRect = (r) => ({ x: r.x / SCALE, y: r.y / SCALE, width: r.width / SCALE, height: r.height / SCALE });
function updateScore(s) {
  s.score.progress = Math.min(100, Math.floor(Math.max(0, s.maxX - START_CENTER) * 1e4 / (exit.x - START_CENTER)) / 100);
  s.score.coins = LEVELS[s.level].coins.length - s.remainingCoins.length;
  s.score.points = Math.floor(s.score.progress * 10) + s.score.coins * 100 + (s.score.completed ? 1e4 : 0);
}
function frame2(s) {
  const p = s.player, level = LEVELS[s.level], held = new Set(s.held), pressed = new Set(s.pendingEdges);
  s.pendingEdges = [];
  const direction = Number(held.has("right")) - Number(held.has("left")) || Number(pressed.has("right")) - Number(pressed.has("left"));
  if (direction) p.vx = Math.max(-24, Math.min(24, p.vx + direction * 4));
  else p.vx = Math.sign(p.vx) * Math.max(0, Math.abs(p.vx) - 6);
  if (pressed.has("jump") && p.onGround) {
    p.vy = -84;
    p.onGround = false;
  }
  p.vy = Math.min(128, p.vy + 4);
  p.x += p.vx;
  for (const solid of level.platforms) if (intersects(p, solid)) {
    if (p.vx > 0) p.x = solid.x - p.width;
    else if (p.vx < 0) p.x = solid.x + solid.width;
    p.vx = 0;
  }
  if (p.x < 0) {
    p.x = 0;
    p.vx = 0;
  }
  if (p.x + p.width > WIDTH) {
    p.x = WIDTH - p.width;
    p.vx = 0;
  }
  p.y += p.vy;
  p.onGround = false;
  for (const solid of level.platforms) if (intersects(p, solid)) {
    if (p.vy > 0) {
      p.y = solid.y - p.height;
      p.onGround = true;
    } else if (p.vy < 0) p.y = solid.y + solid.height;
    p.vy = 0;
  }
  s.maxX = Math.max(s.maxX, Math.min(exit.x, p.x + p.width / 2));
  if (p.y >= HEIGHT || level.hazards.some((h) => intersects(p, h))) finish(s, p.y >= HEIGHT ? "FELL_IN_PIT" : "HAZARD_COLLISION");
  else if (enemies(s).some((enemy) => intersects(p, enemy))) finish(s, "ENEMY_COLLISION");
  if (s.status === "playing") {
    s.remainingCoins = s.remainingCoins.filter((i) => {
      const c = level.coins[i];
      return !intersects(p, { x: c.x - 64, y: c.y - 64, width: 128, height: 128 });
    });
    if (intersects(p, level.exit)) {
      s.score.completed = true;
      s.maxX = exit.x;
      finish(s, "GOAL_REACHED");
    }
  }
  updateScore(s);
}
var platformer = {
  id: "platformer",
  name: "\u5E73\u53F0\u8DF3\u8DC3",
  rulesVersion: "platformer-realtime-v1",
  rules: "\u539F\u521B3\u4E2A\u53EF\u9009\u56FA\u5B9A\u5173\u5361\uFF1A1\u6559\u5B66\u969C\u788D\u30012\u5751\u6D1E\u30013\u79FB\u52A8\u654C\u4EBA\u3002\u5355\u5173\u4E00\u547D\u3001120\u79D2\uFF1B\u78B0\u654C\u5373\u6B7B\uFF0C\u4E0D\u80FD\u8E29\u654C\u300260Hz\u6574\u6570\u7269\u7406\uFF0C\u5DE6\u53F3\u52A0\u901F\uFF0C\u8DF3\u8DC3\u4EC5\u5728\u7740\u5730\u4E14\u6309\u952E\u65B0\u6309\u4E0B\u65F6\u89E6\u53D1\uFF0C\u4FDD\u6301\u8DF3\u8DC3\u4E0D\u4F1A\u81EA\u52A8\u8FDE\u8DF3\u3002\u5148\u5224\u65AD\u81F4\u6B7B\u78B0\u649E\uFF0C\u518D\u5224\u65AD\u51FA\u53E3\u3002AI\u6309\u952E100/300/1000ms\u540E\u81EA\u52A8\u91CA\u653E\uFF0C\u65E0\u81EA\u52A8\u907F\u9669\u3002\u6BD4\u8F83\u5DF2\u901A\u5173\u4F18\u5148\uFF1B\u53CC\u65B9\u901A\u5173\u6BD4\u8F83\u66F4\u5C11tick\uFF1B\u5426\u5219\u6BD4\u8F83\u6700\u8FDC\u8FDB\u5EA6\u3001\u91D1\u5E01\u6570\u3001\u5B58\u6D3Btick\u3002",
  create(config) {
    const level = config.level ?? 1;
    if (![1, 2, 3].includes(level)) throw new Error("\u5E73\u53F0\u5173\u5361\u5FC5\u987B\u4E3A1\u30012\u62163");
    return {
      gameId: "platformer",
      tick: 0,
      status: "playing",
      score: { points: 0, coins: 0, progress: 0, completed: false, survivedTicks: 0 },
      held: [],
      releaseTick: null,
      level,
      player: { x: START_X, y: 10 * TILE - PLAYER_HEIGHT, width: PLAYER_WIDTH, height: PLAYER_HEIGHT, vx: 0, vy: 0, onGround: true },
      pendingEdges: [],
      remainingCoins: [0, 1, 2],
      maxX: START_CENTER
    };
  },
  candidates(state2) {
    if (state2.status !== "playing") return [];
    return actions([
      { buttons: [], label: "\u91CA\u653E\u6240\u6709\u6309\u952E\uFF0C\u60EF\u6027\u548C\u91CD\u529B\u7EE7\u7EED", durations: [100] },
      { buttons: ["left"], label: "\u6301\u7EED\u5411\u5DE6\u79FB\u52A8" },
      { buttons: ["right"], label: "\u6301\u7EED\u5411\u53F3\u79FB\u52A8" },
      { buttons: ["jump"], label: "\u5C1D\u8BD5\u8DF3\u8DC3\u4E00\u6B21\uFF08\u7740\u5730\u4E14\u8DF3\u952E\u65B0\u6309\u4E0B\u65F6\uFF09", durations: [100] },
      { buttons: ["left", "jump"], label: "\u6301\u7EED\u5411\u5DE6\u79FB\u52A8\uFF0C\u5E76\u5C1D\u8BD5\u8DF3\u8DC3\u4E00\u6B21\uFF08\u7740\u5730\u4E14\u8DF3\u952E\u65B0\u6309\u4E0B\u65F6\uFF0C\u4E0D\u81EA\u52A8\u8FDE\u8DF3\uFF09" },
      { buttons: ["right", "jump"], label: "\u6301\u7EED\u5411\u53F3\u79FB\u52A8\uFF0C\u5E76\u5C1D\u8BD5\u8DF3\u8DC3\u4E00\u6B21\uFF08\u7740\u5730\u4E14\u8DF3\u952E\u65B0\u6309\u4E0B\u65F6\uFF0C\u4E0D\u81EA\u52A8\u8FDE\u8DF3\uFF09" }
    ]);
  },
  input(state2, input) {
    validInput(input, BUTTONS2);
    if (input.buttons.includes("left") && input.buttons.includes("right")) throw new Error("\u4E0D\u80FD\u540C\u65F6\u5411\u5DE6\u548C\u5411\u53F3");
    return setInput(state2, input);
  },
  release(state2) {
    return clearInput(state2);
  },
  step(state2, ticks) {
    return advance(state2, ticks, frame2);
  },
  observe(state2) {
    const s = state2, view = platformer.view(s);
    return {
      ...view,
      ...observationClock(s),
      coordinates: "\u6240\u6709\u4F4D\u7F6E\u3001\u5BBD\u9AD8\u548C\u8DDD\u79BB\u7EDF\u4E00\u4E3A\u50CF\u7D20(px)\u3002\u5DE6\u4E0A\u89D2\u4E3A(0,0)\uFF0Cx\u5411\u53F3\u3001y\u5411\u4E0B\uFF1B\u77E9\u5F62x/y\u662F\u5DE6\u4E0A\u89D2\uFF0C\u53F3\u8FB9\u754Cx+width\u3001\u5E95\u8FB9\u754Cy+height\u3002\u91D1\u5E01\u5750\u6807\u662F\u4E2D\u5FC3\u3002\u4E16\u754C\u5750\u6807\u4E0E\u5C4F\u5E55\u6EDA\u52A8\u65E0\u5173\u3002",
      units: { position: "px", velocity: "px/tick", acceleration: "px/tick\xB2", tickSeconds: 1 / 60, tilePixels: 32 },
      physics: {
        acceleration: 0.5,
        maxSpeed: 3,
        friction: 0.75,
        gravity: 0.5,
        jumpVelocity: -10.5,
        terminalFallSpeed: 16,
        units: "acceleration/friction/gravity \u662F px/tick\xB2\uFF1BmaxSpeed/jumpVelocity/terminalFallSpeed \u662F px/tick\u3002\u8D1F vy \u5411\u4E0A\u3002",
        updateOrder: "\u6BCFtick\u5148\u66F4\u65B0\u6C34\u5E73\u901F\u5EA6\uFF1B\u7740\u5730\u4E14jump\u65B0\u6309\u4E0B\u65F6\u8BBE\u7F6Evy=-10.5\uFF1B\u518D\u52A0\u91CD\u529B0.5\uFF08\u4E0A\u965016\uFF09\uFF0C\u968F\u540E\u79FB\u52A8\u5E76\u5904\u7406\u78B0\u649E\u3002\u677E\u5F00\u6A2A\u79FB\u540E\u6309friction\u51CF\u901F\uFF0C\u4E0D\u662F\u7ACB\u5373\u9759\u6B62\u3002"
      },
      geometry: {
        platforms: "\u5B9E\u5FC3\u77E9\u5F62\uFF0C\u53EF\u7AD9\u7ACB\u5E76\u963B\u6321\u6C34\u5E73/\u5782\u76F4\u79FB\u52A8\uFF1B\u6CA1\u6709\u5730\u9762\u7684\u533A\u57DF\u53EF\u4E0B\u843D",
        hazards: "\u63A5\u89E6\u5373\u7ED3\u675F\u7684\u77E9\u5F62",
        enemies: "\u63A5\u89E6\u5373\u7ED3\u675F\u7684\u79FB\u52A8\u77E9\u5F62\uFF0C\u4E0D\u80FD\u8E29\u654C",
        coins: "\u4E2D\u5FC3\u9644\u8FD116\xD716px\u6536\u96C6\u533A\u57DF",
        exit: "\u63A5\u89E6\u5373\u53EF\u5B8C\u6210\u7684\u77E9\u5F62"
      },
      enemies: view.enemies.map((enemy, index) => {
        const patrol = LEVELS[s.level].enemies[index], distance = patrol.maxX - patrol.minX;
        const direction = s.tick * patrol.speed % (2 * distance) < distance ? 1 : -1;
        return {
          ...enemy,
          vx: direction * patrol.speed / SCALE,
          vy: 0,
          motion: "\u6C34\u5E73\u5F80\u8FD4\u5300\u901F\u79FB\u52A8\uFF0C\u5230\u5DE1\u903B\u7AEF\u70B9\u53CD\u5411\uFF1Bvx\u8868\u793A\u5F53\u524D\u671D\u5411\uFF0C\u7AEF\u70B9\u5904\u4E3A\u53CD\u5411\u540E\u7684\u671D\u5411",
          patrolMinX: patrol.minX / SCALE,
          patrolMaxX: patrol.maxX / SCALE
        };
      }),
      controls: {
        ...observationControls(s),
        jumpRequiresNewPress: true,
        jumpSemantics: "jump\u53EA\u5728\u7740\u5730\u4E14\u65B0\u6309\u4E0B\u65F6\u5C1D\u8BD5\u4E00\u6B21\uFF1B\u7A7A\u4E2D\u6309\u4E0B\u4E0D\u4F1A\u6392\u961F\u7B49\u843D\u5730\u8DF3\u3002\u79FB\u52A8+jump\u4E2D\uFF0C\u6A2A\u5411\u952E\u6301\u7EED\u6307\u5B9A\u65F6\u957F\uFF0Cjump\u4E0D\u81EA\u52A8\u91CD\u590D\uFF1B\u5DF2\u6309\u4F4Fjump\u65F6\u91CD\u590D\u540C\u4E00\u7EC4\u5408\u4E0D\u4F1A\u4EA7\u751F\u65B0\u8DF3\u8DC3\u3002"
      },
      maxX: s.maxX / SCALE,
      maxXMeaning: "\u89D2\u8272\u4E2D\u5FC3\u66FE\u5230\u8FBE\u7684\u6700\u8FDCx\uFF0C\u7528\u4E8E\u8FDB\u5EA6\u8BA1\u5206\uFF0C\u4E0D\u662F\u5F53\u524D\u4F4D\u7F6E"
    };
  },
  view(state2) {
    const s = state2, level = LEVELS[s.level];
    return {
      gameId: "platformer",
      tick: s.tick,
      status: s.status,
      score: { ...s.score },
      width: WIDTH / SCALE,
      height: HEIGHT / SCALE,
      currentButtons: [...s.held],
      reason: s.reason,
      level: s.level,
      player: { ...renderRect(s.player), vx: s.player.vx / SCALE, vy: s.player.vy / SCALE, onGround: s.player.onGround },
      platforms: level.platforms.map(renderRect),
      hazards: level.hazards.map(renderRect),
      coins: s.remainingCoins.map((i) => ({ x: level.coins[i].x / SCALE, y: level.coins[i].y / SCALE })),
      enemies: enemies(s).map(renderRect),
      exit: renderRect(level.exit)
    };
  },
  restore(value) {
    const s = baseState(value, "platformer", BUTTONS2);
    if (![1, 2, 3].includes(s.level)) throw new Error("\u5E73\u53F0\u5173\u5361\u5B58\u6863\u65E0\u6548");
    validInput({ buttons: s.pendingEdges }, BUTTONS2);
    const p = s.player;
    if (!p || p.width !== PLAYER_WIDTH || p.height !== PLAYER_HEIGHT || !integer(p.x, 0, WIDTH - PLAYER_WIDTH) || !integer(p.y, -TILE, HEIGHT + 128) || !integer(p.vx, -24, 24) || !integer(p.vy, -84, 128) || typeof p.onGround !== "boolean" || !integer(s.maxX, START_CENTER, exit.x) || !Array.isArray(s.remainingCoins) || s.remainingCoins.length > 3 || !s.remainingCoins.every((i) => integer(i, 0, 2)) || new Set(s.remainingCoins).size !== s.remainingCoins.length || typeof s.score.completed !== "boolean" || !Number.isFinite(s.score.progress)) throw new Error("\u5E73\u53F0\u89D2\u8272\u5B58\u6863\u65E0\u6548");
    const score = { ...s.score };
    updateScore(s);
    if (s.score.points !== score.points || s.score.coins !== score.coins || s.score.progress !== score.progress || s.score.completed && (s.status !== "finished" || s.reason !== "GOAL_REACHED")) throw new Error("\u5E73\u53F0\u5206\u6570\u5B58\u6863\u65E0\u6548");
    return s;
  },
  compare(a, b) {
    if (!!a.completed !== !!b.completed) return Number(!!a.completed) - Number(!!b.completed);
    if (a.completed && b.completed) return b.survivedTicks - a.survivedTicks;
    return (a.progress ?? 0) - (b.progress ?? 0) || (a.coins ?? 0) - (b.coins ?? 0) || a.survivedTicks - b.survivedTicks;
  }
};

// arcade_bridge.ts
var adapter;
var state;
var decisions = 0;
var maxSteps = 80;
var lastInput = null;
function snapshot() {
  const naturalDone = state.status === "finished";
  const truncated = !naturalDone && decisions >= maxSteps;
  const done = naturalDone || truncated;
  const candidates = done ? [] : adapter.candidates(state).map((action) => ({
    id: action.id,
    label: action.id === "noop:100" ? "\u91CA\u653E\u6240\u6709\u6309\u952E\uFF0C\u81EA\u7136\u63A8\u8FDB100\u6BEB\u79D2\uFF086\u7269\u7406tick\uFF09" : action.label
  }));
  return {
    snapshot: {
      game_id: adapter.id,
      rules_version: `game-harness-lab/${adapter.id}-paused-v1-arena-4e2bda1`,
      step: decisions,
      max_steps: maxSteps,
      score: state.score.points,
      done,
      reason: naturalDone ? state.reason : truncated ? "step_limit" : null,
      truncated,
      policy_role: "candidate",
      tick: state.tick,
      metrics: { ...state.score },
      render: adapter.view(state),
      observation: adapter.observe(state),
      last_input: lastInput,
      simulation: {
        tick_rate: 60,
        wall_clock_advances_game: false,
        action_duration_semantics: "One selected native input advances exactly durationMs*60/1000 ticks unless the game naturally ends first. Waiting for a model advances zero ticks."
      }
    },
    candidates
  };
}
function dispatch(input) {
  if (input.op === "init") {
    if (adapter) throw new Error("already_initialized");
    if (input.game_id !== "tetris" && input.game_id !== "platformer") throw new Error("unknown_game");
    if (!Number.isInteger(input.max_steps) || Number(input.max_steps) < 1 || Number(input.max_steps) > 2e3) throw new Error("invalid_step_budget");
    if (!Number.isSafeInteger(input.seed)) throw new Error("invalid_seed");
    adapter = input.game_id === "tetris" ? tetris : platformer;
    maxSteps = Number(input.max_steps);
    const level = (Number(input.seed) % 3 + 3) % 3 + 1;
    state = adapter.create(adapter.id === "platformer" ? { level } : {}, String(input.seed));
    return snapshot();
  }
  if (!adapter) throw new Error("not_initialized");
  if (input.op === "state") return snapshot();
  if (input.op !== "step") throw new Error("unknown_operation");
  if (state.status !== "playing" || decisions >= maxSteps) throw new Error("episode_already_ended");
  const action = adapter.candidates(state).find((candidate) => candidate.id === input.action);
  if (!action) throw new Error("action_is_not_in_native_catalog");
  const durationMs = action.input.durationMs;
  const ticks = durationMs * 60 / 1e3;
  const before = state.tick;
  state = adapter.input(state, action.input);
  state = adapter.step(state, ticks);
  decisions++;
  lastInput = {
    action_id: action.id,
    buttons: [...action.input.buttons],
    duration_ms: durationMs,
    requested_ticks: ticks,
    simulated_ticks: state.tick - before
  };
  return snapshot();
}
function main() {
  (0, import_node_readline.createInterface)({ input: process.stdin, crlfDelay: Infinity }).on("line", (line) => {
    let input;
    try {
      input = JSON.parse(line);
      process.stdout.write(JSON.stringify({ id: input.id, ok: true, value: dispatch(input) }) + "\n");
    } catch (error) {
      process.stdout.write(JSON.stringify({
        id: input?.id,
        ok: false,
        error: error instanceof Error ? error.message : "rules_error"
      }) + "\n");
    }
  });
}
if (require.main === module) main();
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  platformer,
  tetris
});
