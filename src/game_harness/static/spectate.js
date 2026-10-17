/* Read-only spectator controller. No action/control endpoint is called here. */
(function () {
  'use strict';
  class FeedCursor {
    constructor() { this.epoch = 0; this.id = null; this.cursor = 0; }
    select(id) { this.id = id; this.cursor = 0; this.epoch++; }
    accept(feed, epoch) {
      if (epoch !== this.epoch || feed.episode_id !== this.id || !feed.available) return false;
      if (!Number.isSafeInteger(feed.cursor) || !Number.isSafeInteger(feed.start_cursor) || !Array.isArray(feed.lines)) throw Error('公开协议格式无效');
      if (feed.cursor < 0 || feed.start_cursor < 0 || feed.lines.some(line => typeof line !== 'string')) throw Error('公开协议内容无效');
      if (feed.reset) {
        if (feed.start_cursor !== 0 || feed.lines.length !== feed.cursor) throw Error('公开协议重置格式无效');
      } else if (feed.start_cursor !== this.cursor || feed.cursor - feed.start_cursor !== feed.lines.length) throw Error('公开协议序号不连续');
      this.cursor = feed.cursor;
      return true;
    }
  }
  function pendingEpisodes(records, visited, current) {
    return records.filter(row => row.available !== false && !visited.has(row.episode_id) && row.episode_id !== current)
      .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || a.episode_id.localeCompare(b.episode_id));
  }
  function playbackComplete(serverFinished, animation, cursor) {
    return serverFinished && animation.cursor === cursor && animation.atEnd === true && !animation.paused;
  }
  if (typeof module !== 'undefined' && module.exports) { module.exports = {FeedCursor, pendingEpisodes, playbackComplete}; return; }
  const $ = id => document.getElementById(id);
  const frame = $('viewer'), cursor = new FeedCursor(), visited = new Set();
  let ready = false, records = [], initial = true, reset = true, serverFinished = false;
  let animation = {}, finishedAt = null, lastNativeTurn = null, frameError = null, installed = false;
  const pinned = new URLSearchParams(location.search).get('episode');
  if (pinned && /^[A-Za-z0-9_-]+$/.test(pinned)) { cursor.select(pinned); $('follow').checked = false; }
  function notice(message) { $('notice').hidden = !message; $('notice').textContent = message || ''; }
  function send(value) { if (ready) frame.contentWindow.postMessage(value, '*'); }
  function choose(id) {
    if (!id || !/^[A-Za-z0-9_-]+$/.test(id)) return;
    cursor.select(id); visited.add(id); reset = true; serverFinished = false;
    animation = {}; finishedAt = null; frameError = null;
    $('identity').textContent = id; $('episodeSelect').value = id;
    notice('正在读取完整公开战况…');
    send({type: 'showdown-reset', episodeId: id, lines: [], cursor: 0, finished: false});
  }
  async function get(path) {
    const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw Error(`观战请求失败（${response.status}）`);
    return response.json();
  }
  function render() {
    const queue = pendingEpisodes(records, visited, cursor.id);
    const complete = playbackComplete(serverFinished, animation, cursor.cursor);
    $('next').disabled = queue.length === 0;
    $('next').textContent = queue.length ? `下一局（${queue.length}）` : '下一局';
    $('phase').textContent = frameError ? '动画组件异常' : !cursor.id ? '等待对局' : animation.paused ? '动画已暂停' : complete ? '本局播放完成' : serverFinished ? '对局已结束 · 动画继续播放' : '实时观战';
    $('progress').textContent = cursor.id ? `动画回合 ${animation.animatedTurn ?? '—'} / 实盘回合 ${lastNativeTurn ?? '—'} · 已接收 ${cursor.cursor} 条公开事件` : '请在实验台开始一场宝可梦实验';
    if (complete && !frameError) {
      if (finishedAt === null) finishedAt = Date.now();
      if ($('follow').checked && queue.length && Date.now() - finishedAt >= 2500) choose(queue[0].episode_id);
    } else finishedAt = null;
  }
  window.addEventListener('message', event => {
    if (event.source !== frame.contentWindow || event.origin !== 'null' || !event.data || typeof event.data !== 'object') return;
    const value = event.data;
    if (value.type === 'showdown-ready') {
      ready = true; cursor.select(cursor.id); reset = true; serverFinished = false; animation = {}; finishedAt = null;
      if (cursor.id) send({type:'showdown-reset', episodeId:cursor.id, lines:[], cursor:0, finished:false});
    } else if (value.type === 'showdown-state' && value.episodeId === cursor.id) {
      animation = value;
      render();
    } else if (value.type === 'showdown-error') {
      frameError = true; notice('原版动画资源或协议加载失败。可在实验台查看状态棋盘；刷新观战页可重试。');
    }
  });
  $('episodeSelect').addEventListener('change', () => { $('follow').checked = false; choose($('episodeSelect').value); });
  $('next').addEventListener('click', () => { const next = pendingEpisodes(records, visited, cursor.id)[0]; if (next) choose(next.episode_id); });
  $('latest').addEventListener('click', async () => {
    const latest = records.find(row => row.available !== false);
    if (!latest) return;
    for (const row of records) visited.add(row.episode_id);
    $('follow').checked = true;
    if (latest.episode_id !== cursor.id) choose(latest.episode_id);
    // Seek only by an explicit viewer click, never on a polling update.
    try { await pollFeed(); send({type:'showdown-seek-live',episodeId:cursor.id}); } catch (error) { notice(error.message); }
  });
  let feedBusy = false;
  async function pollFeed() {
    if (!ready || !cursor.id || feedBusy || frameError) return;
    feedBusy = true;
    const epoch = cursor.epoch, id = cursor.id, after = reset ? 0 : cursor.cursor;
    try {
      const feed = await get(`/api/spectator/${encodeURIComponent(id)}?after=${after}`);
      if (epoch !== cursor.epoch || id !== cursor.id) return;
      if (!feed.available) { notice('此回合没有完整公开协议，无法生成原版动画。请开始一场新对局；状态回放仍可使用。'); return; }
      if (!cursor.accept(feed, epoch)) return;
      const resetting = reset || feed.reset;
      if (resetting || feed.lines.length) { animation = {...animation, atEnd:false}; finishedAt = null; }
      send({type:resetting ? 'showdown-reset' : 'showdown-append', episodeId:id,
            start_cursor:feed.start_cursor, cursor:feed.cursor, lines:feed.lines, finished:feed.finished});
      reset = false; serverFinished = Boolean(feed.finished); lastNativeTurn = feed.native_turn;
      if (feed.status === 'error') notice('观战事件记录发生错误，已保存片段可观看；不会把缺失事件补成对局结果。');
      else if (feed.truncated || feed.result === 'unresolved') notice('本局预算截断，未分胜负；动画只播放已发生事件。');
      else if (!frameError) notice(null);
    } finally { feedBusy = false; }
  }
  async function poll() {
    try {
      if (!installed) {
        const info = await get('/api/showdown-viewer');
        if (!info.available) { notice(info.message); return; }
        installed = true;
      }
      const listing = await get('/api/spectator/episodes');
      records = listing.episodes;
      const selection = cursor.id;
      const fragment = document.createDocumentFragment();
      if (!records.length) { const o = document.createElement('option'); o.value = ''; o.textContent = '尚无原版观战记录'; fragment.append(o); }
      for (const row of records) {
        const option = document.createElement('option'); option.value = row.episode_id;
        option.textContent = `${row.finished ? '已结束' : '进行中'} · ${row.purpose || '对局'} · ${row.episode_id}`;
        fragment.append(option);
      }
      $('episodeSelect').replaceChildren(fragment);
      if (initial) {
        for (const row of records) visited.add(row.episode_id);
        initial = false;
        const latest = records.find(row => row.available !== false);
        if (!cursor.id && latest) choose(latest.episode_id);
      }
      if (!cursor.id) { const latest = records.find(row => row.available !== false); if (latest) choose(latest.episode_id); }
      $('episodeSelect').value = cursor.id || selection || '';
      await pollFeed(); render();
      if (!records.length && !cursor.id) notice('观战界面已就绪。在实验台点击“开始实验”，新对局会自动出现。');
    } catch (error) { notice('观战连接暂时中断，恢复后按事件序号补齐：' + error.message); }
    finally { setTimeout(poll, 500); }
  }
  poll();
}());
