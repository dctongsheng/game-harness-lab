/* Local read-only bridge for the unmodified upstream Showdown Battle renderer. */
'use strict';
(function () {
  const MAX_LINES = 50000;
  const MAX_LINE_LENGTH = 4096;
  let battle = null;
  let episodeId = null;
  let lines = [];
  let cursor = 0;
  let finished = false;
  let speed = 1;
  let failed = false;
  const status = document.getElementById('playback-status');
  const errorBox = document.getElementById('viewer-error');
  const toggle = document.getElementById('toggle-playback');
  const replay = document.getElementById('replay-animation');
  const speedControl = document.getElementById('animation-speed');

  function send(payload) {
    // The sandbox intentionally has an opaque origin; the host checks source.
    window.parent.postMessage(payload, '*');
  }

  function reportError(message) {
    failed = true;
    if (battle) battle.pause();
    errorBox.textContent = String(message);
    errorBox.style.display = 'block';
    status.textContent = '观战中断；对局决策不受影响';
    send({type:'showdown-error', episodeId, message:String(message)});
  }

  function queuedTurn() {
    for (let index = lines.length - 1; index >= 0; index--) {
      if (lines[index].startsWith('|turn|')) return Number(lines[index].slice(6)) || 0;
    }
    return 0;
  }

  function update() {
    if (!battle || failed) return;
    const atEnd = battle.atQueueEnd === true && battle.currentStep >= battle.stepQueue.length;
    const animatedTurn = Math.max(0, battle.turn || 0);
    const turn = queuedTurn();
    toggle.textContent = battle.paused ? '继续动画' : '暂停动画';
    toggle.disabled = false;
    replay.disabled = false;
    status.textContent = `动画第 ${animatedTurn} 回合 / 实盘第 ${turn} 回合 · ` +
      (battle.paused ? '已暂停' : atEnd ? (finished ? '本局已播完' : '等待双方决策') : `${speed}× 播放中`);
    send({type:'showdown-state', episodeId, cursor, atEnd, paused:!!battle.paused,
      animatedTurn, queuedTurn:turn, finished, nativeEnded:!!battle.ended});
  }

  function fit() {
    const stage = document.getElementById('battle-stage');
    const canvas = document.getElementById('battle');
    const scale = Math.min(1, Math.max(0.2, stage.clientWidth / 642));
    canvas.style.transform = `scale(${scale})`;
    canvas.style.left = `${Math.max(0, (stage.clientWidth - 642 * scale) / 2)}px`;
    stage.style.height = `${362 * scale}px`;
  }

  function applySpeed() {
    if (!battle) return;
    battle.messageFadeTime = 300 / speed;
    battle.messageShownTime = 500 / speed;
    battle.scene.updateAcceleration();
  }

  function validLines(value) {
    return Array.isArray(value) && value.length <= MAX_LINES && value.every(line =>
      typeof line === 'string' && line.length <= MAX_LINE_LENGTH &&
      !/[\r\n\u0000]/.test(line) && (line === '' || line.startsWith('|')) &&
      !/^\|(?:request|split|sentchoice|choice|inputlog|debug|seed|team|showteam|sideupdate|end|html|raw|uhtml|uhtmlchange)\|/.test(line));
  }

  function validEnvelope(message) {
    return message && typeof message === 'object' &&
      typeof message.episodeId === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(message.episodeId) &&
      Number.isSafeInteger(message.cursor) && message.cursor >= 0 && message.cursor <= MAX_LINES &&
      typeof message.finished === 'boolean' && validLines(message.lines);
  }

  function reset(message) {
    if (!validEnvelope(message) || message.cursor !== message.lines.length) {
      reportError('对战协议重置格式无效'); return;
    }
    if (battle) battle.destroy();
    battle = null;
    episodeId = message.episodeId;
    lines = message.lines.slice();
    cursor = message.cursor;
    finished = message.finished;
    failed = false;
    errorBox.textContent = '';
    errorBox.style.display = 'none';
    const currentId = episodeId;
    battle = new Battle({
      id: 'battle-local-' + episodeId.replace(/[^a-zA-Z0-9-]/g, '-'),
      $frame: $('#battle'), $logFrame: $('#battle-log'), log: lines.slice(),
      // A temporarily empty live queue is normal, not a premature replay end.
      isReplay:false, paused:true, autoresize:false,
    });
    battle.setMute(true);
    // Set the documented scene animation multiplier for this isolated viewer.
    // Upstream's default maps its qualitative presets to 1/2/3; our numeric
    // controls deliberately use 1/2/4 and do not affect simulation time.
    battle.scene.updateAcceleration = function () { this.acceleration = speed; };
    applySpeed();
    battle.subscribe(state => {
      if (episodeId !== currentId) return;
      if (state === 'error') reportError('Showdown 无法解析当前公开对战记录');
      else update();
    });
    battle.play();
    fit();
    update();
  }

  function append(message) {
    if (!validEnvelope(message) || !Number.isSafeInteger(message.start_cursor) ||
        message.start_cursor < 0 || message.start_cursor + message.lines.length !== message.cursor) {
      reportError('对战协议增量格式无效'); return;
    }
    if (!battle || message.episodeId !== episodeId) return; // stale response from an earlier battle
    if (failed) return;
    if (message.start_cursor < cursor && message.cursor <= cursor) {
      // Idempotent retry only when every byte matches already accepted data.
      if (!message.lines.every((line,index) => lines[message.start_cursor + index] === line)) {
        reportError('重复的对战记录与已接收内容不一致'); return;
      }
      if (message.cursor === cursor) finished = finished || message.finished;
      update(); return;
    }
    if (message.start_cursor !== cursor || message.cursor > MAX_LINES || (finished && message.lines.length)) {
      reportError('对战记录序号不连续，需要重新载入观战'); return;
    }
    lines.push(...message.lines);
    cursor = message.cursor;
    finished = finished || message.finished;
    // addBatch resumes only when the animation queue was waiting for input.
    battle.addBatch(message.lines);
    update();
  }

  window.addEventListener('message', event => {
    if (event.source !== window.parent || !event.data || typeof event.data !== 'object') return;
    const message = event.data;
    try {
      if (message.type === 'showdown-reset') reset(message);
      else if (message.type === 'showdown-append') append(message);
      else if (message.type === 'showdown-seek-live' && message.episodeId === episodeId && battle && !failed) {
        battle.seekTurn(Infinity); update();
      }
    } catch (error) { reportError(error instanceof Error ? error.message : String(error)); }
  });
  toggle.addEventListener('click', () => {
    if (!battle || failed) return;
    if (battle.paused) battle.play(); else battle.pause();
    update();
  });
  replay.addEventListener('click', () => {
    if (!battle || failed) return;
    battle.pause(); battle.seekTurn(0); battle.play(); update();
  });
  speedControl.addEventListener('change', () => {
    const selected = Number(speedControl.value);
    if (![1,2,4].includes(selected)) return;
    speed = selected; applySpeed(); update();
  });
  window.addEventListener('resize', fit);
  window.addEventListener('error', event => reportError(event.message || '原版动画资源加载失败'));
  if (typeof Battle !== 'function' || typeof $ !== 'function') {
    reportError('Showdown 资源未就绪，请先执行 scripts/setup_showdown_viewer.py');
  } else {
    fit();
    status.textContent = '原版动画已就绪，等待对局';
    send({type:'showdown-ready'});
  }
})();
