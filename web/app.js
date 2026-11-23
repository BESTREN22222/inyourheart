'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const show = (el) => { if (el) el.hidden = false; };
const hide = (el) => { if (el) el.hidden = true; };
const state = { accounts: [], schedules: [], ntfy: {}, timer: null, loginTimer: null, loginActive: false, balanceLoaded: false, jobId: null };

const SOURCE_NAMES = {
  by_me: '我发出去的',
  to_me: '其他OTA&小偶像给我的',
  show: '微博下的评论',
  mentions: '提到我的',
  user: '个人资料',
  by_me_other: '其他OTA&小偶像发出的',
  to_me_other: '其他OTA&小偶像收到的',
  mentions_other: '其他OTA&小偶像被@的',
  timeline_other: '其他OTA&小偶像发出+收到的',
};

function friendlyError(message) {
  const text = String(message || '');
  if (/invalid_access_token|21332|access_token/i.test(text)) return '授权码无效或过期，请重新获取';
  if (/RATE_LIMIT|10023|too many/i.test(text)) return '查得太频繁了，等一会儿再试';
  if (/not found|404/i.test(text)) return '没找到内容，检查一下微博号或链接';
  return text;
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { Accept: 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error((payload && payload.error) || `请求失败 ${response.status}`);
  return payload;
}

let toastTimer;
function toast(message, ms = 3500) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function showError(el, message) {
  el.textContent = message || '';
  el.hidden = !message;
}

function tab(name) {
  if (!document.getElementById(`tab-${name}`)) name = 'accounts';
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('main > section').forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'history') loadHistory().catch(() => {});
}

async function loadHistory() {
  const list = $('#history-list');
  list.innerHTML = '<div class="muted">加载中…</div>';
  try {
    const data = await api('/api/history?limit=100');
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = '<div class="muted">还没有查询记录。去「查评论」跑一次就会出现在这里。</div>';
      return;
    }
    list.innerHTML = items.map((q) => {
      const params = q.params || {};
      const target = params.post_id ? `微博 ${params.post_id}` : (params.uid ? `UID ${params.uid}` : '');
      const when = q.started_at ? new Date(q.started_at).toLocaleString('zh-CN') : '';
      const status = q.status === 'done' ? '完成' : (q.status === 'error' ? '失败' : '进行中');
      return `<div class="item">
        <div class="main">
          <b>${esc(SOURCE_NAMES[q.source] || q.source)} ${esc(target)}</b>
          <small>${esc(q.account_name || q.screen_name || '')} · ${esc(when)} · ${esc(status)} · 读 ${q.fetched || 0} 条 / 存 ${q.saved || 0} 条</small>
          ${q.error ? `<small class="error-text">${esc(q.error)}</small>` : ''}
        </div>
      </div>`;
    }).join('');
  } catch (error) {
    list.innerHTML = `<div class="muted">${esc(friendlyError(error.message))}</div>`;
  }
}

$('#history-refresh').addEventListener('click', () => loadHistory());

/* ---------------------------------------------------------------- state */

async function loadState() {
  const data = await api('/api/state');
  $('#app-version').textContent = `V${data.version}`;
  state.accounts = data.accounts;
  state.schedules = data.schedules;
  state.ntfy = data.ntfy || {};
  renderAccounts();
  renderSchedules();
  renderSelects();
  renderNtfy();
  if (!state.balanceLoaded && state.accounts.some((a) => a.mode === 'cli')) {
    state.balanceLoaded = true;
    refreshBalance(true).catch(() => {});
  }
}

async function refreshBalance(silent = false) {
  const button = $('#refresh-balance');
  if (button) button.disabled = true;
  try {
    const data = await api('/api/balance', { method: 'POST' });
    state.accounts = data.accounts || state.accounts;
    renderAccounts();
    if (!silent) {
      if (data.error) toast(`余额刷新失败：${friendlyError(data.error)}`);
      else toast('余额已刷新');
    }
  } catch (error) {
    if (!silent) toast(friendlyError(error.message));
  } finally {
    if (button) button.disabled = false;
  }
}

function renderSelects() {
  const options = state.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}（${esc(a.mode)}）</option>`).join('');
  ['#pull-account', '#schedule-account'].forEach((sel) => {
    const el = $(sel);
    if (el) el.innerHTML = options || '<option value="">先绑定账号</option>';
  });
}

function renderAccounts() {
  const list = $('#account-list');
  if (!state.accounts.length) {
    list.innerHTML = '<div class="muted">还没有绑定账号。</div>';
    return;
  }
  list.innerHTML = state.accounts.map((a) => `
    <div class="item">
      <div class="main">
        <b>${esc(a.name)}</b>
        <small>UID ${esc(a.uid || '-')} · ${a.mode === 'cli' ? 'CLI 模式' : 'Token 模式'}${a.mode === 'cli' ? ` · 余额 ${esc(a.balance || '—')}` : ''}</small>
      </div>
      <button type="button" class="ghost danger" data-del-account="${a.id}">解绑</button>
    </div>`).join('');
  $$('[data-del-account]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!confirm('解绑这个账号？本地数据也会删除。\n\n注意：解绑不会退出微博登录；要换账号，请先点「退出高级工具登录」。')) return;
      await api(`/api/accounts/${btn.dataset.delAccount}`, { method: 'DELETE' });
      await loadState();
    });
  });
}

function renderSchedules() {
  const list = $('#schedule-list');
  if (!state.schedules.length) {
    list.innerHTML = '<div class="muted">还没有定时任务。</div>';
    return;
  }
  list.innerHTML = state.schedules.map((s) => `
    <div class="item">
      <div class="main">
        <b>${esc(s.account_name || s.screen_name || s.account_id)} · ${SOURCE_NAMES[s.source] || s.source}</b>
        <small>每 ${s.interval_minutes} 分钟 · ${s.enabled ? '运行中' : '已暂停'} · ${esc(s.last_status || '尚未运行')}${s.last_new ? ` · 新增 ${s.last_new}` : ''}</small>
      </div>
      <button type="button" class="ghost" data-toggle="${s.id}" data-enabled="${s.enabled ? 0 : 1}">${s.enabled ? '暂停' : '启用'}</button>
      <button type="button" class="ghost danger" data-del-schedule="${s.id}">删除</button>
    </div>`).join('');
  $$('[data-toggle]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await api(`/api/schedules/${btn.dataset.toggle}/toggle`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: btn.dataset.enabled === '1' }),
      });
      await loadState();
    });
  });
  $$('[data-del-schedule]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!confirm('删除这个定时任务？')) return;
      await api(`/api/schedules/${btn.dataset.delSchedule}`, { method: 'DELETE' });
      await loadState();
    });
  });
}

function renderNtfy() {
  $('#ntfy-enabled').value = state.ntfy.enabled ? '1' : '0';
  $('#ntfy-server').value = state.ntfy.server || 'https://ntfy.sh';
  $('#ntfy-topic').value = state.ntfy.topic || '';
}

/* --------------------------------------------------------------- account */

function syncAccountMode() {
  const cli = $('#account-mode').value === 'cli';
  $('#token-wrap').hidden = cli;
  $('#cli-check').hidden = !cli;
  $('#cli-login').hidden = !cli;
  $('#cli-login-box').hidden = !cli;
}

$('#account-mode').addEventListener('change', syncAccountMode);
syncAccountMode();

$('#cli-check').addEventListener('click', async () => {
  try {
    const data = await api('/api/cli/status');
    if (!data.available) {
      showError($('#account-error'), '还没有安装高级工具。不会装的话，先用「填授权码」查自己就行。');
      return;
    }
    if (data.me) {
      showError($('#account-error'), '');
      toast(`高级工具已登录：${data.me.screen_name || data.me.uid}，可以点「检查并保存」`);
    } else {
      showError($('#account-error'), '高级工具还没登录。先在命令行登录一次，再回来检查。');
    }
  } catch (error) {
    showError($('#account-error'), friendlyError(error.message));
  }
});

function setLoginSteps({ done = 0, active = -1, error = -1 } = {}) {
  const items = Array.from(document.querySelectorAll('#cli-login-steps li'));
  items.forEach((item, index) => {
    item.classList.remove('active', 'done', 'error');
    if (index < done) item.classList.add('done');
    if (index === active) item.classList.add('active');
    if (index === error) item.classList.add('error');
  });
}

function renderCliLogin(data) {
  const box = $('#cli-login-box');
  show(box);
  const text = $('#cli-login-text');
  const link = $('#cli-login-link');
  const code = $('#cli-login-code');
  const copy = $('#cli-login-copy');
  const output = $('#cli-login-output');
  const running = data.status === 'running';

  const phase = data.phase || '';
  if (data.status === 'done') {
    setLoginSteps({ done: 5 });
  } else if (data.status === 'error') {
    setLoginSteps({ done: 1, error: 2 });
  } else if (phase === 'checking') {
    setLoginSteps({ active: 0 });
  } else if (phase === 'starting') {
    setLoginSteps({ done: 1, active: 1 });
  } else if (phase === 'verifying') {
    setLoginSteps({ done: 3, active: 4 });
  } else if (data.url) {
    setLoginSteps({ done: 3, active: 3 });
  } else {
    setLoginSteps({ done: 2, active: 2 });
  }

  let message;
  if (data.status === 'done') {
    const name = data.me ? (data.me.screen_name || data.me.uid) : '';
    message = name ? `登录成功：${name}` : '登录成功';
  } else if (data.status === 'error') {
    message = `登录失败：${data.error || data.message || '未知错误'}`;
  } else if (data.message) {
    message = data.message;
  } else if (data.url) {
    message = '已拿到授权链接。点下面按钮打开微博，确认后这里会自动更新。';
  } else {
    message = '正在申请授权链接，请稍等…';
  }
  text.innerHTML = (running && !data.url ? '<span class="spinner"></span>' : '') + esc(message);

  if (data.url) {
    link.hidden = false;
    link.href = data.url;
  } else {
    link.hidden = true;
  }
  if (data.code) {
    code.hidden = false;
    code.textContent = `授权码：${data.code}`;
  } else {
    code.hidden = true;
  }
  copy.hidden = !data.code;
  if (data.status === 'error' && data.output) {
    output.hidden = false;
    output.textContent = data.output;
  } else {
    output.hidden = true;
  }
}

async function bindCliAccount(me) {
  if (!me) return;
  const exists = state.accounts.some((a) => a.mode === 'cli' && String(a.uid) === String(me.uid));
  if (!exists) {
    await api('/api/accounts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'cli', name: me.screen_name || '高级工具' }),
    });
    toast(`已登录并绑定：${me.screen_name || me.uid}`);
  } else {
    toast(`已登录：${me.screen_name || me.uid}`);
  }
  await loadState();
}

function stopLoginPolling() {
  clearInterval(state.loginTimer);
  state.loginActive = false;
  $('#cli-login').disabled = false;
}

async function startCliLogin() {
  clearInterval(state.loginTimer);
  state.loginActive = true;
  $('#cli-login').disabled = true;
  $('#account-error').hidden = true;
  show($('#cli-login-box'));
  setLoginSteps({ active: 0 });
  $('#cli-login-text').innerHTML = '<span class="spinner"></span>正在检查高级工具…';

  const startedAt = Date.now();
  try {
    const data = await api('/api/cli/login', { method: 'POST' });
    renderCliLogin(data);
    if (data.status !== 'running') {
      stopLoginPolling();
      if (data.status === 'done') await bindCliAccount(data.me);
      return;
    }

    state.loginTimer = setInterval(async () => {
      if (Date.now() - startedAt > 200000) {
        stopLoginPolling();
        setLoginSteps({ done: 1, error: 2 });
        $('#cli-login-text').textContent = '等了太久还没拿到授权链接，请检查网络后重试。';
        return;
      }
      try {
        const status = await api('/api/cli/login/status');
        renderCliLogin(status);
        if (status.status !== 'running') {
          stopLoginPolling();
          if (status.status === 'done') await bindCliAccount(status.me);
        }
      } catch (error) {
        stopLoginPolling();
        setLoginSteps({ error: 3 });
        $('#cli-login-text').textContent = friendlyError(error.message);
      }
    }, 1500);
  } catch (error) {
    stopLoginPolling();
    setLoginSteps({ error: 0 });
    $('#cli-login-text').textContent = friendlyError(error.message);
  }
}

window.startCliLogin = startCliLogin;
window.addEventListener('error', (event) => {
  toast(`页面脚本错误：${event.message || '未知错误'}`, 7000);
});
$('#cli-login-cancel').addEventListener('click', () => {
  stopLoginPolling();
  hide($('#cli-login-box'));
});
$('#cli-login-copy').addEventListener('click', async () => {
  const value = ($('#cli-login-code').textContent || '').replace('授权码：', '').trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast('授权码已复制');
  } catch (_) {
    toast(`授权码：${value}`);
  }
});

$('#refresh-balance').addEventListener('click', () => refreshBalance(false));
$('#cli-logout').addEventListener('click', async () => {
  if (!confirm('退出高级工具登录？\n\n退出后要重新授权，才能查其他OTA&小偶像。')) return;
  try {
    await api('/api/cli/logout', { method: 'POST' });
    state.balanceLoaded = false;
    toast('已退出高级工具登录');
    await loadState();
  } catch (error) {
    toast(friendlyError(error.message));
  }
});

$('#account-add').addEventListener('click', async () => {
  showError($('#account-error'), '');
  const body = {
    mode: $('#account-mode').value,
    name: $('#account-name').value.trim(),
    token: $('#account-token').value.trim(),
  };
  try {
    const data = await api('/api/accounts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast(`绑定好了：${data.screen_name || data.uid}`);
    $('#account-token').value = '';
    await loadState();
  } catch (error) {
    showError($('#account-error'), friendlyError(error.message));
  }
});

/* ------------------------------------------------------------------ pull */

const MODE_HINTS = {
  latest: '从最新一条开始，读你设定的条数。',
  history: '从上次存下的最旧一条继续往更早读；第一次用请先选「只看最近的」。',
  newer: '从上次存下的最新一条往后看看有没有新的。',
  range: '选一段时间，只保留这段时间里的评论。时间越早，需要往后翻的页越多，建议页数别超过 20。',
};

function syncPullMode() {
  const mode = $('#pull-mode').value;
  $('#date-wrap').hidden = mode !== 'range';
  $('#mode-hint').textContent = MODE_HINTS[mode] || '';
  if (mode === 'range' && Number($('#pull-pages').value) < 5) {
    $('#pull-pages').value = '10';
  }
}

$('#pull-mode').addEventListener('change', syncPullMode);
syncPullMode();

$('#pull-source').addEventListener('change', () => {
  const source = $('#pull-source').value;
  $('#uid-wrap').hidden = source === 'show';
  $('#post-wrap').hidden = source !== 'show';
  $('#pull-filter').closest('div').hidden = !['to_me', 'to_me_other', 'show'].includes(source);
});

async function startPull() {
  const accountId = Number($('#pull-account').value);
  if (!accountId) { toast('先绑定账号'); return; }
  const source = $('#pull-source').value;
  const mode = $('#pull-mode').value;
  const params = {
    uid: $('#pull-uid').value.trim(),
    post_id: $('#pull-post').value.trim(),
    count: Number($('#pull-count').value) || 50,
    filter_by_author: Number($('#pull-filter').value) || 0,
    mode,
  };
  if (mode === 'range') {
    params.start_date = $('#pull-start-date').value;
    params.end_date = $('#pull-end-date').value;
    if (!params.start_date || !params.end_date) {
      showError($('#pull-error'), '查某段时间，要选开始和结束日期');
      return;
    }
  }
  showError($('#pull-error'), '');
  $('#pull-progress').hidden = false;
  $('#pull-bar').style.width = '10%';
  $('#pull-start').disabled = true;
  try {
    if (source === 'user') {
      const data = await api('/api/user/lookup', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_id: accountId, uid: params.uid }),
      });
      renderUsers(data.items || []);
      $('#pull-progress').hidden = true;
      return;
    }
    const data = await api('/api/pull', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_id: accountId, source, params, pages: Number($('#pull-pages').value) || 1 }),
    });
    state.jobId = data.job_id;
    pollJob(accountId, source);
  } catch (error) {
    showError($('#pull-error'), error.message);
    $('#pull-progress').hidden = true;
    $('#pull-start').disabled = false;
  }
}

function pollJob(accountId, source) {
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    let job;
    try {
      job = await api(`/api/jobs/${state.jobId}`);
    } catch (error) {
      clearInterval(state.timer);
      showError($('#pull-error'), error.message);
      return;
    }
    $('#pull-bar').style.width = `${Math.min(95, 12 + job.pages * 20)}%`;
    if (job.status === 'running') return;

    clearInterval(state.timer);
    $('#pull-start').disabled = false;
    $('#pull-progress').hidden = true;
    if (job.status === 'done') {
      toast(`查完了：看了 ${job.fetched} 条，新增 ${job.saved} 条`);
      await loadResults(accountId, source);
      await loadState();
      loadHistory().catch(() => {});
    } else {
      showError($('#pull-error'), friendlyError(job.error || '没查成功，再试一次'));
    }
  }, 1200);
}

async function loadResults(accountId, source) {
  const data = await api(`/api/comments?account_id=${accountId}&source=${source}&limit=100`);
  $('#result-count').textContent = `${data.total} 条`;
  const list = $('#result-list');
  if (!data.items.length) {
    list.innerHTML = '<div class="muted">没有数据。</div>';
    return;
  }
  list.innerHTML = data.items.map((item) => `
    <article class="comment">
      <div class="head">
        <b>${esc(item.author || '-')}</b><span>→</span><span>${esc(item.target || '-')}</span>
        <time>${esc(item.time || '')}</time>
      </div>
      <p>${esc(item.text || '')}</p>
      ${item.post_text ? `<div class="post"><span>原微博：</span>${esc(item.post_text)}</div>` : ''}
    </article>`).join('');
}

function renderUsers(items) {
  $('#result-count').textContent = `${items.length} 个用户`;
  $('#result-list').innerHTML = items.map((u) => `
    <article class="comment">
      <div class="head"><b>${esc(u.screen_name || '')}</b><span class="tag">UID ${esc(u.id || '')}</span></div>
      <p>粉丝 ${Number(u.followers_count || 0).toLocaleString()} · 微博 ${Number(u.statuses_count || 0).toLocaleString()}</p>
      ${u.description ? `<div class="post">${esc(u.description)}</div>` : ''}
    </article>`).join('') || '<div class="muted">没有找到用户。</div>';
}

$('#pull-start').addEventListener('click', startPull);

$('#pull-export').addEventListener('click', () => {
  const accountId = Number($('#pull-account').value);
  if (!accountId) { toast('先绑定账号'); return; }
  location.href = `/api/export.csv?account_id=${accountId}`;
});

/* ------------------------------------------------------------- schedules */

$('#schedule-add').addEventListener('click', async () => {
  const accountId = Number($('#schedule-account').value);
  if (!accountId) { toast('先绑定账号'); return; }
  const source = $('#schedule-source').value;
  const params = {
    uid: $('#schedule-uid').value.trim(),
    post_id: $('#schedule-post').value.trim(),
    count: Number($('#schedule-count').value) || 50,
  };
  try {
    await api('/api/schedules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        account_id: accountId, source, params,
        interval_minutes: Number($('#schedule-interval').value) || 60,
      }),
    });
    toast('已开始盯防');
    await loadState();
  } catch (error) {
    toast(friendlyError(error.message));
  }
});

/* --------------------------------------------------------------- settings */

$('#ntfy-save').addEventListener('click', async () => {
  const ntfy = {
    enabled: $('#ntfy-enabled').value === '1',
    server: $('#ntfy-server').value.trim() || 'https://ntfy.sh',
    topic: $('#ntfy-topic').value.trim(),
  };
  await api('/api/settings/ntfy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(ntfy),
  });
  state.ntfy = ntfy;
  toast('已保存');
});

$('#ntfy-test').addEventListener('click', async () => {
  try {
    const result = await api('/api/ntfy/test', { method: 'POST' });
    toast(result.ok ? `测试已发送（${result.message}）` : `失败：${result.message}`, 6000);
  } catch (error) {
    toast(error.message);
  }
});

/* ------------------------------------------------------------------- init */

$$('#nav button').forEach((btn) => btn.addEventListener('click', () => tab(btn.dataset.tab)));
if (location.hash) tab(location.hash.slice(1));

loadState().catch((error) => toast(error.message));
setInterval(() => {
  if (!state.loginActive) loadState().catch(() => {});
}, 15000);
