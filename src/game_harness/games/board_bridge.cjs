/*! Bundled Decision Arena board rules. MIT License

Copyright (c) 2026 Dougo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

*/
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
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
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/game_harness/games/board_bridge.ts
var board_bridge_exports = {};
__export(board_bridge_exports, {
  army: () => army,
  createController: () => createController,
  gomoku: () => gomoku,
  projectArmy: () => projectArmy
});
module.exports = __toCommonJS(board_bridge_exports);
var import_node_readline = __toESM(require("node:readline"));

// src/game_harness/games/vendor/arena-board/common.ts
var other = (seat) => seat === 0 ? 1 : 0;
function requireTurn(state, seat) {
  if (seat !== 0 && seat !== 1) throw new Error("\u65E0\u6548\u7684\u73A9\u5BB6\u5E2D\u4F4D");
  if (state.phase !== "playing" || state.result) throw new Error("\u5F53\u524D\u68CB\u5C40\u4E0D\u80FD\u843D\u5B50");
  if (state.turn !== seat) throw new Error("\u5C1A\u672A\u8F6E\u5230\u4F60");
}
function record(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("\u5B58\u6863\u683C\u5F0F\u65E0\u6548");
  return value;
}
function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
function randomFor(seed) {
  let n = 2166136261;
  for (let i = 0; i < seed.length; i++) n = Math.imul(n ^ seed.charCodeAt(i), 16777619) >>> 0;
  return () => {
    n = Math.imul(n, 1664525) + 1013904223 >>> 0;
    return n / 4294967296;
  };
}
function shuffle(items, random) {
  const result = items.slice();
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

// src/game_harness/games/vendor/arena-board/army.ts
var names = { commander: "\u53F8\u4EE4", general: "\u519B\u957F", division: "\u5E08\u957F", brigade: "\u65C5\u957F", regiment: "\u56E2\u957F", battalion: "\u8425\u957F", company: "\u8FDE\u957F", platoon: "\u6392\u957F", engineer: "\u5DE5\u5175", mine: "\u5730\u96F7", bomb: "\u70B8\u5F39", flag: "\u519B\u65D7" };
var rank = { commander: 9, general: 8, division: 7, brigade: 6, regiment: 5, battalion: 4, company: 3, platoon: 2, engineer: 1 };
var rankNames = ["", "\u5DE5\u5175", "\u6392\u957F", "\u8FDE\u957F", "\u8425\u957F", "\u56E2\u957F", "\u65C5\u957F", "\u5E08\u957F", "\u519B\u957F", "\u53F8\u4EE4"];
var roster = ["commander", "general", "division", "division", "brigade", "brigade", "regiment", "regiment", "battalion", "battalion", "company", "company", "company", "platoon", "platoon", "platoon", "engineer", "engineer", "engineer", "mine", "mine", "mine", "bomb", "bomb", "flag"];
var armyTerrain = {
  camps: [11, 13, 17, 21, 23, 36, 38, 42, 46, 48],
  headquarters: [1, 3, 56, 58],
  railway: Array.from({ length: 60 }, (_, i) => i).filter((i) => {
    const row = Math.floor(i / 5), col = i % 5;
    return row > 0 && row < 11 && (col === 0 || col === 4 || [1, 5, 6, 10].includes(row));
  })
};
var camp = new Set(armyTerrain.camps);
var hq = new Set(armyTerrain.headquarters);
var railway = new Set(armyTerrain.railway);
var localRow = (index, seat) => seat === 0 ? Math.floor(index / 5) : 11 - Math.floor(index / 5);
var coordinate = (index) => `${String.fromCharCode(65 + index % 5)}${Math.floor(index / 5) + 1}`;
var rules = "\u6697\u68CB\u519B\u68CB\uFF1A12\xD75\u68CB\u76D8\uFF0C\u6BCF\u65B925\u5B50\uFF0C\u84DD\u65B9\u5148\u624B\u3002\u519B\u65D7\u5728\u5DF1\u65B9\u5927\u672C\u8425\uFF0C\u5730\u96F7\u5728\u540E\u4E24\u6392\uFF0C\u70B8\u5F39\u4E0D\u5728\u524D\u7EBF\uFF0C\u884C\u8425\u4E0D\u5F97\u5E03\u5B50\u3002\u7B49\u7EA7\u4F9D\u6B21\u4E3A\u53F8\u4EE4\u3001\u519B\u957F\u3001\u5E08\u957F\u3001\u65C5\u957F\u3001\u56E2\u957F\u3001\u8425\u957F\u3001\u8FDE\u957F\u3001\u6392\u957F\u3001\u5DE5\u5175\uFF1B\u5927\u5403\u5C0F\uFF0C\u540C\u7EA7\u540C\u6BC1\u3002\u519B\u65D7\u548C\u5730\u96F7\u4E0D\u53EF\u79FB\u52A8\uFF1B\u8FDB\u5165\u5927\u672C\u8425\u7684\u4EFB\u4F55\u68CB\u5B50\u4E0D\u80FD\u518D\u8D70\u3002\u519B\u65D7\u88AB\u5403\u7ACB\u5373\u5224\u8D1F\uFF1B\u5426\u5219\u70B8\u5F39\u4E0E\u76EE\u6807\u540C\u6BC1\uFF08\u5305\u62EC\u5730\u96F7\uFF09\uFF0C\u5DE5\u5175\u6392\u96F7\u4E14\u5B58\u6D3B\uFF0C\u5176\u4ED6\u68CB\u5B50\u78B0\u96F7\u9635\u4EA1\u800C\u96F7\u7559\u5B58\u3002\u666E\u901A\u9053\u8DEF\u8D70\u4E00\u6B65\uFF0C\u884C\u8425\u8FDE\u7EBF\u53EF\u659C\u8D70\uFF0C\u884C\u8425\u5185\u68CB\u5B50\u4E0D\u80FD\u88AB\u653B\u51FB\u3002\u94C1\u8DEF\u76F4\u884C\u4E0D\u9650\u6B65\u3001\u5DE5\u5175\u53EF\u4EE5\u8F6C\u5F2F\uFF0C\u5747\u4E0D\u80FD\u8D8A\u5B50\uFF1B\u4E2D\u592E\u4EC5A/C/E\u5217\u4E92\u901A\u3002\u5931\u53BB\u519B\u65D7\u6216\u65E0\u5408\u6CD5\u52A8\u4F5C\u5224\u8D1F\uFF0C\u540C\u5C40\u9762\u540C\u8F6E\u8F6C\u4E09\u6B21\u5224\u548C\u3002\u654C\u65B9\u8EAB\u4EFD\u4F9D\u53CC\u65B9\u5404\u81EA\u7684\u6218\u6597\u4FE1\u606F\u9010\u6B65\u63ED\u793A\uFF1B\u53F8\u4EE4\u9635\u4EA1\u4EAE\u65D7\u3002";
function canPlace(kind, index, seat) {
  const row = localRow(index, seat);
  if (index < 0 || index >= 60 || row < 0 || row > 5 || camp.has(index)) return false;
  if (kind === "flag") return row === 0 && hq.has(index);
  if (kind === "mine") return row < 2;
  if (kind === "bomb") return row !== 5;
  return true;
}
function create(_config, seed) {
  if (typeof seed !== "string" || seed.length > 512) throw new Error("\u5E03\u9635\u79CD\u5B50\u65E0\u6548");
  const random = randomFor(seed), board = Array(60).fill(null);
  for (const seat of [0, 1]) {
    const empty = Array.from({ length: 60 }, (_, i) => i).filter((i) => localRow(i, seat) >= 0 && localRow(i, seat) < 6 && !camp.has(i));
    const pieces = ["flag", "mine", "mine", "mine", "bomb", "bomb", ...shuffle(roster.filter((k) => !["flag", "mine", "bomb"].includes(k)), random)];
    for (const kind of pieces) {
      const options = empty.filter((i) => canPlace(kind, i, seat));
      const index = options[Math.floor(random() * options.length)];
      if (index === void 0) throw new Error("\u65E0\u6CD5\u751F\u6210\u5408\u6CD5\u5E03\u9635");
      board[index] = { id: `piece-${seat}-${index}`, seat, kind, moved: false };
      empty.splice(empty.indexOf(index), 1);
    }
  }
  return { gameId: "army", seed, board, ready: [false, false], turn: 0, phase: "setup", ply: 0, intel: [{}, {}], positions: [], actions: [] };
}
function crossingAllowed(a, b) {
  const ra = Math.floor(a / 5), rb = Math.floor(b / 5);
  return !(ra === 5 && rb === 6 || ra === 6 && rb === 5) || [0, 2, 4].includes(a % 5);
}
function neighbours(index, diagonal = false) {
  const row = Math.floor(index / 5), col = index % 5, result = [];
  for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
    if (!dr && !dc || row + dr < 0 || row + dr >= 12 || col + dc < 0 || col + dc >= 5) continue;
    const to = (row + dr) * 5 + col + dc;
    if (dr && dc && (!diagonal || !camp.has(index) && !camp.has(to))) continue;
    if (crossingAllowed(index, to)) result.push(to);
  }
  return result;
}
function destinations(state, from) {
  const piece = state.board[from];
  if (!piece || piece.kind === "mine" || piece.kind === "flag" || hq.has(from)) return [];
  const reachable = /* @__PURE__ */ new Set();
  const land = (to) => {
    const target = state.board[to];
    if (target?.seat !== piece.seat && !(target && camp.has(to))) reachable.add(to);
  };
  for (const to of neighbours(from, true)) land(to);
  if (railway.has(from)) {
    if (piece.kind === "engineer") {
      const visited = /* @__PURE__ */ new Set([from]), queue = [from];
      for (let i = 0; i < queue.length; i++) {
        for (const to of neighbours(queue[i])) {
          if (!railway.has(to) || visited.has(to)) continue;
          visited.add(to);
          land(to);
          if (state.board[to] === null) queue.push(to);
        }
      }
    } else {
      for (const [dr, dc] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        let current = from;
        while (true) {
          const row = Math.floor(current / 5) + dr, col = current % 5 + dc;
          if (row < 0 || row >= 12 || col < 0 || col >= 5) break;
          const to = row * 5 + col;
          if (!railway.has(to) || !crossingAllowed(current, to)) break;
          land(to);
          if (state.board[to]) break;
          current = to;
        }
      }
    }
  }
  return [...reachable].sort((a, b) => a - b);
}
function playingActions(state, seat) {
  return state.board.flatMap((piece, from) => piece?.seat === seat ? destinations(state, from).map((to) => ({ id: `move:${from}:${to}`, from, to, label: `${coordinate(from)}\u2192${coordinate(to)}` })) : []);
}
function legalActions(input, seat) {
  const state = input;
  if (seat !== 0 && seat !== 1) return [];
  if (state.phase === "setup") {
    if (state.ready[seat]) return [];
    const indices = state.board.flatMap((piece, i) => piece?.seat === seat ? [i] : []);
    const swaps = [];
    for (const from of indices) for (const to of indices) {
      if (from === to) continue;
      if (canPlace(state.board[from].kind, to, seat) && canPlace(state.board[to].kind, from, seat)) swaps.push({ id: `swap:${from}:${to}`, from, to, label: `\u4EA4\u6362 ${coordinate(from)} \u4E0E ${coordinate(to)}` });
    }
    return [...swaps, { id: "ready", label: "\u786E\u8BA4\u5E03\u9635\uFF0C\u51C6\u5907\u5F00\u59CB" }];
  }
  if (state.phase !== "playing" || state.result || state.turn !== seat) return [];
  return playingActions(state, seat);
}
function positionKey(state) {
  return `${state.turn}|${state.board.map((p) => p ? `${p.seat}:${p.kind}` : ".").join(",")}`;
}
function revealFlag(state, seat) {
  const flag = state.board.find((p) => p?.seat === seat && p.kind === "flag");
  if (flag) state.intel[other(seat)][flag.id] = { exact: "flag" };
}
function battleIntel(state, attacker, defender, to, outcome) {
  state.intel[defender.seat][attacker.id] = { exact: attacker.kind };
  if (outcome === "win") {
    state.intel[attacker.seat][defender.id] = { exact: defender.kind };
  } else if (outcome === "lose") {
    const intel = state.intel[attacker.seat][defender.id];
    if (intel?.exact) return;
    const attackingRank = rank[attacker.kind];
    if (attackingRank === 9) state.intel[attacker.seat][defender.id] = { exact: "mine" };
    else state.intel[attacker.seat][defender.id] = {
      above: Math.max(intel?.above ?? 0, attackingRank),
      minePossible: attackingRank !== 1 && !defender.moved && localRow(to, defender.seat) < 2 && !intel?.notMine,
      notMine: attackingRank === 1 || defender.moved || localRow(to, defender.seat) >= 2 || intel?.notMine
    };
  }
}
function apply(input, seat, actionId) {
  const state = input;
  if (seat !== 0 && seat !== 1) throw new Error("\u65E0\u6548\u7684\u73A9\u5BB6\u5E2D\u4F4D");
  if (!legalActions(state, seat).some((action) => action.id === actionId)) throw new Error("\u519B\u68CB\u52A8\u4F5C\u4E0D\u5408\u6CD5\u6216\u5C1A\u672A\u8F6E\u5230\u4F60");
  const next = structuredClone(state);
  next.actions.push({ seat, id: actionId });
  if (state.phase === "setup") {
    if (actionId === "ready") {
      next.ready[seat] = true;
      if (next.ready.every(Boolean)) {
        next.phase = "playing";
        next.turn = 0;
        next.positions.push(positionKey(next));
      }
    } else {
      const [, from2, to2] = actionId.split(":").map(Number);
      [next.board[from2], next.board[to2]] = [next.board[to2], next.board[from2]];
    }
    return next;
  }
  requireTurn(state, seat);
  const [, from, to] = actionId.split(":").map(Number), attacker = next.board[from], defender = next.board[to];
  let outcome = "win";
  if (defender) {
    if (defender.kind === "flag") next.result = { winner: seat, reason: "FLAG_CAPTURED" };
    else if (attacker.kind === "bomb" || defender.kind === "bomb") outcome = "both";
    else if (defender.kind === "mine") outcome = attacker.kind === "engineer" ? "win" : "lose";
    else outcome = rank[attacker.kind] > rank[defender.kind] ? "win" : rank[attacker.kind] === rank[defender.kind] ? "both" : "lose";
    battleIntel(next, attacker, defender, to, outcome);
  }
  next.board[from] = null;
  if (outcome === "win") {
    attacker.moved = true;
    next.board[to] = attacker;
    const knowledge = next.intel[other(seat)][attacker.id];
    if (knowledge && !knowledge.exact) {
      knowledge.minePossible = false;
      knowledge.notMine = true;
    }
  } else if (outcome === "both") next.board[to] = null;
  if (attacker.kind === "commander" && outcome !== "win") revealFlag(next, seat);
  if (defender?.kind === "commander" && outcome !== "lose") revealFlag(next, other(seat));
  if (!next.result && outcome === "win" && hq.has(to) && localRow(to, other(seat)) === 0) revealFlag(next, other(seat));
  next.turn = other(seat);
  next.ply++;
  next.positions.push(positionKey(next));
  if (!next.result && playingActions(next, next.turn).length === 0) next.result = { winner: seat, reason: "NO_LEGAL_MOVES" };
  if (!next.result && next.positions.filter((key) => key === next.positions.at(-1)).length >= 3) next.result = { winner: null, reason: "THREEFOLD_REPETITION" };
  if (next.result) next.phase = "finished";
  return next;
}
function projectPiece(state, piece, viewer) {
  const full = viewer === "full" || state.phase === "finished";
  const knowledge = state.intel[typeof viewer === "number" ? viewer : other(piece.seat)][piece.id];
  if (full || viewer === piece.seat) return { id: piece.id, seat: piece.seat, kind: piece.kind, label: names[piece.kind] };
  if (knowledge?.exact) return { id: piece.id, seat: piece.seat, kind: knowledge.exact, label: names[knowledge.exact] };
  const label = knowledge?.above ? `>${rankNames[knowledge.above]}${knowledge.minePossible ? "\u6216\u96F7" : ""}` : "?";
  return { id: piece.id, seat: piece.seat, kind: "unknown", label, hidden: true };
}
function view(input, viewer) {
  const state = input;
  return {
    gameId: "army",
    rows: 12,
    cols: 5,
    cells: state.board.map((piece) => piece ? projectPiece(state, piece, viewer) : null),
    turn: state.turn,
    phase: state.phase,
    ply: state.ply,
    ready: [...state.ready],
    legalActions: typeof viewer === "number" ? legalActions(state, viewer) : [],
    ...state.result ? { result: { ...state.result } } : {}
  };
}
function restore(input) {
  const data = record(input);
  if (data.gameId !== "army" || typeof data.seed !== "string" || !Array.isArray(data.actions) || data.actions.length > 5e3) throw new Error("\u519B\u68CB\u5B58\u6863\u683C\u5F0F\u65E0\u6548");
  let state = create({}, data.seed);
  for (const action of data.actions) {
    const row = record(action);
    if (row.seat !== 0 && row.seat !== 1 || typeof row.id !== "string") throw new Error("\u519B\u68CB\u52A8\u4F5C\u8BB0\u5F55\u65E0\u6548");
    state = apply(state, row.seat, row.id);
  }
  for (const field of ["board", "intel", "ready", "positions", "turn", "ply", "phase", "result"]) {
    if (!same(state[field], data[field])) throw new Error("\u519B\u68CB\u5B58\u6863\u4E0E\u52A8\u4F5C\u8BB0\u5F55\u4E0D\u4E00\u81F4");
  }
  return state;
}
var army = {
  id: "army",
  name: "\u519B\u68CB",
  rulesVersion: "army-hidden-1",
  rules,
  create,
  legalActions,
  apply,
  view,
  restore,
  observe(input, seat) {
    const projected = view(input, seat);
    return {
      game: "\u6697\u68CB\u519B\u68CB",
      rules,
      yourSeat: seat,
      yourColor: seat === 0 ? "\u84DD\uFF08\u4E0A\u65B9\uFF09" : "\u7EA2\uFF08\u4E0B\u65B9\uFF09",
      turn: projected.turn,
      coordinates: "A-E\u5217\u4ECE\u5DE6\u81F3\u53F3\uFF0C1-12\u884C\u4ECE\u4E0A\u81F3\u4E0B\uFF1B\u84DD\u65B9\u540E\u6392\u4E3A1\uFF0C\u7EA2\u65B9\u540E\u6392\u4E3A12\u3002",
      terrain: armyTerrain,
      pieces: projected.cells.flatMap((piece, index) => piece ? [{ coordinate: coordinate(index), seat: piece.seat, kind: piece.kind, label: piece.label }] : [])
    };
  }
};

// src/game_harness/games/vendor/arena-board/gomoku.ts
var rules2 = "\u81EA\u7531\u4E94\u5B50\u68CB\uFF1A\u9ED1\u68CB\u5148\u624B\uFF0C\u53CC\u65B9\u8F6E\u6D41\u5728\u7A7A\u4EA4\u53C9\u70B9\u843D\u4E00\u5B50\u3002\u6A2A\u3001\u7AD6\u6216\u4EFB\u4E00\u659C\u7EBF\u8FDE\u7EED\u4E94\u5B50\u53CA\u4EE5\u4E0A\u83B7\u80DC\uFF1B\u65E0\u7981\u624B\u3001\u65E0\u63D0\u5B50\u3001\u65E0\u4EA4\u6362\u5F00\u5C40\u3002\u68CB\u76D8\u6EE1\u4E14\u65E0\u4EBA\u8FDE\u4E94\u5219\u548C\u68CB\u3002\u5750\u6807\u5217\u4ECE\u5DE6\u81F3\u53F3\uFF0C\u884C\u4ECE\u4E0A\u81F3\u4E0B\u3002";
var coordinate2 = (index, size) => `${String.fromCharCode(65 + index % size)}${Math.floor(index / size) + 1}`;
function create2(config) {
  const size = config.size ?? 15;
  if (size !== 9 && size !== 15) throw new Error("\u4E94\u5B50\u68CB\u68CB\u76D8\u987B\u4E3A 9\xD79 \u6216 15\xD715");
  return { gameId: "gomoku", size, turn: 0, phase: "playing", ply: 0, board: Array(size * size).fill(null), history: [] };
}
function gomokuWinningLine(state) {
  const last = state.history.at(-1);
  if (last === void 0) return [];
  const seat = state.board[last], x = last % state.size, y = Math.floor(last / state.size);
  for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
    const line = [last];
    for (const direction of [-1, 1]) {
      let nx = x + direction * dx, ny = y + direction * dy;
      while (nx >= 0 && nx < state.size && ny >= 0 && ny < state.size && state.board[ny * state.size + nx] === seat) {
        if (direction < 0) line.unshift(ny * state.size + nx);
        else line.push(ny * state.size + nx);
        nx += direction * dx;
        ny += direction * dy;
      }
    }
    if (line.length >= 5) return line;
  }
  return [];
}
function legalActions2(input, seat) {
  const state = input;
  if (state.phase !== "playing" || state.result || state.turn !== seat) return [];
  return state.board.flatMap((stone, to) => stone === null ? [{ id: `place:${to}`, to, label: `\u843D\u5B50 ${coordinate2(to, state.size)}` }] : []);
}
function apply2(input, seat, actionId) {
  const state = input;
  requireTurn(state, seat);
  if (!/^place:(0|[1-9]\d*)$/.test(actionId)) throw new Error("\u843D\u5B50\u52A8\u4F5C\u65E0\u6548");
  const to = Number(actionId.slice(6));
  if (to >= state.board.length || state.board[to] !== null) throw new Error("\u843D\u70B9\u8D8A\u754C\u6216\u5DF2\u6709\u68CB\u5B50");
  const next = { ...state, board: state.board.slice(), history: [...state.history, to], turn: other(seat), ply: state.ply + 1 };
  next.board[to] = seat;
  let result;
  if (gomokuWinningLine(next).length) result = { winner: seat, reason: "FIVE_IN_ROW" };
  else if (next.ply === state.board.length) result = { winner: null, reason: "BOARD_FULL" };
  if (result) {
    next.result = result;
    next.phase = "finished";
  }
  return next;
}
function view2(input, viewer) {
  const state = input;
  return {
    gameId: "gomoku",
    rows: state.size,
    cols: state.size,
    turn: state.turn,
    phase: state.phase,
    ply: state.ply,
    cells: state.board.map((seat, i) => seat === null ? null : { id: `stone-${i}`, seat, kind: "stone", label: seat === 0 ? "\u9ED1" : "\u767D" }),
    legalActions: typeof viewer === "number" ? legalActions2(state, viewer) : [],
    ...state.result ? { result: { ...state.result } } : {}
  };
}
function restore2(input) {
  const data = record(input);
  if (data.gameId !== "gomoku" || !Array.isArray(data.history) || data.history.length > 225) throw new Error("\u4E94\u5B50\u68CB\u5B58\u6863\u65E0\u6548");
  let state = create2({ size: data.size });
  for (const index of data.history) {
    if (!Number.isInteger(index)) throw new Error("\u4E94\u5B50\u68CB\u68CB\u8C31\u5750\u6807\u65E0\u6548");
    state = apply2(state, state.turn, `place:${index}`);
  }
  if (!same(data.board, state.board) || data.turn !== state.turn || data.ply !== state.ply || data.phase !== state.phase || !same(data.result, state.result)) throw new Error("\u4E94\u5B50\u68CB\u5B58\u6863\u4E0E\u68CB\u8C31\u4E0D\u4E00\u81F4");
  return state;
}
var gomoku = {
  id: "gomoku",
  name: "\u4E94\u5B50\u68CB",
  rulesVersion: "freestyle-1",
  rules: rules2,
  create: (config) => create2(config),
  legalActions: legalActions2,
  apply: apply2,
  view: view2,
  restore: restore2,
  observe(input, seat) {
    const state = input;
    return {
      game: "\u81EA\u7531\u4E94\u5B50\u68CB",
      rules: rules2,
      yourSeat: seat,
      yourColor: seat === 0 ? "\u9ED1" : "\u767D",
      turn: state.turn,
      size: state.size,
      board: Array.from({ length: state.size }, (_, row) => state.board.slice(row * state.size, (row + 1) * state.size).map((p) => p === null ? "." : p === 0 ? "X" : "O").join(" ")),
      legend: "X=\u9ED1\u68CB\uFF0CO=\u767D\u68CB\uFF0C.=\u7A7A\u4F4D\uFF1BA1\u4F4D\u4E8E\u5DE6\u4E0A\u89D2",
      stones: [0, 1].map((owner) => state.board.flatMap((p, i) => p === owner ? [coordinate2(i, state.size)] : []))
    };
  }
};

// src/game_harness/games/board_bridge.ts
function projectArmy(state, seat) {
  const projection = state.phase === "finished" ? { ...state, phase: "playing" } : state;
  const privateView = army.view(projection, seat);
  const publicRaw = army.view(projection, "spectator");
  const { legalActions: _ignored, ...publicView } = publicRaw;
  return {
    cells: privateView.cells,
    observation: army.observe(projection, seat),
    public_view: {
      ...publicView,
      phase: state.phase,
      result: publicRaw.result ?? null,
      terrain: armyTerrain,
      observation: {
        game: "\u6697\u68CB\u519B\u68CB\u516C\u5171\u89C2\u5BDF",
        rules: army.rules,
        turn: state.turn,
        coordinates: "A-E\u5217\u4ECE\u5DE6\u81F3\u53F3\uFF0C1-12\u884C\u4ECE\u4E0A\u81F3\u4E0B\uFF1B\u84DD\u65B9\u540E\u6392\u4E3A1\uFF0C\u7EA2\u65B9\u540E\u6392\u4E3A12\u3002",
        visibility: "\u53EA\u542B\u53CC\u65B9\u5171\u540C\u53EF\u77E5\u7684\u68CB\u5B50\u4FE1\u606F\uFF0C\u4E0D\u662F\u4EFB\u4F55\u4E00\u65B9\u7684\u79C1\u6709\u89C6\u89D2\u3002",
        terrain: armyTerrain,
        pieces: publicView.cells.flatMap((piece, index) => piece ? [{
          coordinate: `${String.fromCharCode(65 + index % 5)}${Math.floor(index / 5) + 1}`,
          seat: piece.seat,
          kind: piece.kind,
          label: piece.label
        }] : [])
      }
    }
  };
}
function createController() {
  let gameId;
  let engine;
  let state;
  let candidateSeat;
  let maxSteps;
  function snapshot() {
    if (!state) throw new Error("Game has not been initialized");
    const outcome = state.result;
    const truncated = !outcome && state.ply >= maxSteps;
    const done = Boolean(outcome || truncated);
    const seat = state.turn;
    const result = outcome ? outcome.winner === null ? "draw" : outcome.winner === candidateSeat ? "win" : "loss" : null;
    const score = result === "win" ? 100 : result === "loss" ? 0 : 50;
    let view3;
    if (gameId === "army") {
      view3 = { rows: 12, cols: 5, ...projectArmy(state, seat), terrain: armyTerrain };
      if (truncated) view3.public_view.phase = "finished";
    } else {
      const publicView = gomoku.view(state, seat);
      view3 = {
        rows: state.size,
        cols: state.size,
        cells: publicView.cells,
        observation: gomoku.observe(state, seat),
        winning_line: gomokuWinningLine(state)
      };
    }
    return {
      game_id: gameId,
      ...view3,
      step: state.ply,
      max_steps: maxSteps,
      turn: seat,
      actor: seat,
      viewer_seat: seat,
      candidate_seat: candidateSeat,
      policy_role: seat === candidateSeat ? "candidate" : "opponent",
      phase: done ? "finished" : "playing",
      score,
      done,
      reason: outcome?.reason ?? (truncated ? "step_limit" : null),
      result,
      winner: outcome?.winner ?? null,
      truncated: Boolean(truncated),
      score_kind: outcome ? "outcome" : truncated ? "cutoff_neutral" : "in_progress_neutral"
    };
  }
  function output() {
    const current = snapshot();
    const candidates = current.done ? [] : engine.legalActions(state, state.turn).map((action) => ({
      id: action.id,
      label: action.label
    }));
    if (!current.done && !candidates.length) throw new Error("Nonterminal game has no legal actions");
    return { snapshot: current, candidates };
  }
  return {
    request(request) {
      if (request.op === "init") {
        if (state) throw new Error("A session can only be initialized once");
        if (!["army", "gomoku"].includes(request.game)) throw new Error("Unknown board game");
        if (!Number.isSafeInteger(request.seed) || request.seed < 0) throw new Error("Invalid scenario seed");
        if (!Number.isInteger(request.max_steps) || request.max_steps < 1 || request.max_steps > 2e3) throw new Error("Invalid step budget");
        gameId = request.game;
        candidateSeat = request.seed % 2;
        maxSteps = request.max_steps;
        engine = gameId === "army" ? army : gomoku;
        state = engine.create(gameId === "gomoku" ? { size: request.gomoku_size ?? 9 } : {}, String(request.seed));
        if (gameId === "army") {
          state = army.apply(state, 0, "ready");
          state = army.apply(state, 1, "ready");
        }
        return output();
      }
      if (!state) throw new Error("Game has not been initialized");
      if (request.op === "snapshot") return output();
      if (request.op !== "step") throw new Error("Unsupported board operation");
      const current = output();
      if (current.snapshot.done) throw new Error("Game already ended");
      if (request.seat !== void 0 && request.seat !== state.turn) throw new Error("Wrong acting seat");
      if (typeof request.action !== "string" || !current.candidates.some((a) => a.id === request.action)) {
        throw new Error("Action is not in the complete current legal catalog");
      }
      state = engine.apply(state, state.turn, request.action);
      return output();
    }
  };
}
function main() {
  const controller = createController();
  const lines = import_node_readline.default.createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on("line", (line) => {
    let id = null;
    try {
      const request = JSON.parse(line);
      id = request.id;
      if (!Number.isInteger(id)) throw new Error("Request requires an integer id");
      const value = controller.request(request);
      process.stdout.write(JSON.stringify({ id, ok: true, value }) + "\n");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Board operation failed";
      process.stdout.write(JSON.stringify({ id, ok: false, error: message }) + "\n");
    }
  });
}
if (require.main === module) main();
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  army,
  createController,
  gomoku,
  projectArmy
});
