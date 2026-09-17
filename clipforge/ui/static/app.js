/* ClipForge local UI. Vanilla ES2020, no build step, no external assets.
 * Talks to the FastAPI server on the same origin (see the API contract in clipforge/ui). All server text is rendered
 * through textContent (the h() helper) - never as raw HTML strings. */
'use strict';
(() => {
  const POLL_MS = 2000;
  const POLL_FAIL_MS = 5000;
  const LOGS_MS = 10000;
  const PLATFORMS = ['youtube', 'tiktok'];
  const PLATFORM_LABEL = { youtube: 'YouTube', tiktok: 'TikTok' };
  const TABS = ['dashboard', 'review', 'publish', 'schedule', 'settings'];
  const TITLE_MAX = 100;
  const MIN_TAGS = 3;
  const MAX_TAGS = 6;
  const DOT = ' \u00b7 ';

  // ---- tiny DOM helpers ----------------------------------------------------------------------------------------------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const enc = (v) => encodeURIComponent(String(v));

  /** h('tag', {attrs}, ...children): children are text (escaped by textContent) or nodes; on* attrs are listeners. */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (v === true) el.setAttribute(k, '');
        else el.setAttribute(k, String(v));
      }
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    for (const c of children) {
      if (c == null || c === false) continue;
      if (Array.isArray(c)) append(el, c);
      else if (c instanceof Node) el.appendChild(c);
      else el.appendChild(document.createTextNode(String(c)));
    }
  }
  function setText(el, text) {
    const t = text == null ? '' : String(text);
    if (el.textContent !== t) el.textContent = t;
  }
  function show(el, on) { el.hidden = !on; }
  /** Pending state for a control while its request is in flight: disabled (cannot activate twice) + aria-busy for AT/CSS. */
  function setPending(el, on) {
    if (!el) return;
    el.disabled = !!on;
    if (on) el.setAttribute('aria-busy', 'true'); else el.removeAttribute('aria-busy');
  }
  function showErr(el, msg) { setText(el, msg || ''); show(el, !!msg); }
  /** Only http(s) or same-origin absolute paths may become href values (server data never becomes javascript: URLs). */
  function safeHref(url) {
    const s = String(url || '').trim();
    if (/^https?:\/\//i.test(s)) return s;
    if (s.startsWith('/') && !s.startsWith('//')) return s;
    return '#';
  }
  const looksLikeUrl = (s) => /^https?:\/\//i.test(String(s || ''));

  /** Keyed list sync: keeps existing DOM nodes (focus, playing video, open editors survive), adds/removes/reorders. */
  function syncList(container, items, keyOf, create, update, opts = {}) {
    const keys = new Set(items.map((item) => String(keyOf(item))));
    const existing = new Map();
    // drop stale nodes FIRST so survivors keep their slot (a removal never moves, and so never blurs, a later node)
    for (const el of Array.from(container.children)) {
      if (keys.has(el.dataset.key)) existing.set(el.dataset.key, el); else el.remove();
    }
    items.forEach((item, i) => {
      const key = String(keyOf(item));
      let el = existing.get(key);
      if (!el) { el = create(item); el.dataset.key = key; }
      update(el, item, i);
      // new nodes always land at their index; existing nodes only move when reordering is allowed
      if (!el.parentNode || !opts.noReorder) {
        const ref = container.children[i];
        if (ref === el) return;
        // genuine reorder: state-preserving move where available (Chromium 133+, Firefox 144+, Safari 26+)
        if (el.parentNode && typeof container.moveBefore === 'function') container.moveBefore(el, ref || null);
        else container.insertBefore(el, ref || null);
      }
    });
  }

  /** Keep a <select>'s options in sync without dropping the current choice. */
  function syncOptions(sel, pairs, current) {
    const sig = JSON.stringify(pairs);
    if (sel._sig === sig) return;
    sel._sig = sig;
    sel.replaceChildren(...pairs.map(([value, label]) => h('option', { value }, label)));
    sel.value = pairs.some((p) => p[0] === current) ? current : (pairs.length ? pairs[0][0] : '');
  }

  // ---- formatting ----------------------------------------------------------------------------------------------------
  function parseTs(v) {
    if (!v) return null;
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function relTime(v) {
    const d = parseTs(v);
    if (!d) return '';
    const diff = (Date.now() - d.getTime()) / 1000;
    const abs = Math.abs(diff);
    let s;
    if (abs < 5) return 'just now';
    if (abs < 60) s = `${Math.round(abs)} s`;
    else if (abs < 3600) s = `${Math.round(abs / 60)} min`;
    else if (abs < 86400) s = `${abs < 36000 ? (abs / 3600).toFixed(1) : Math.round(abs / 3600)} h`;
    else s = `${Math.round(abs / 86400)} d`;
    return diff < 0 ? `in ${s}` : `${s} ago`;
  }
  function absTime(v) {
    const d = parseTs(v);
    return d ? d.toLocaleString() : '';
  }
  function timeEl(v, cls) {
    const el = h('time', { class: cls || null });
    updateTime(el, v);
    return el;
  }
  function updateTime(el, v) {
    if (!v) { setText(el, '-'); el.removeAttribute('title'); el.removeAttribute('datetime'); return; }
    el.setAttribute('datetime', v);
    el.title = absTime(v);
    setText(el, relTime(v));
  }
  function fmtDur(s) {
    if (s == null || Number.isNaN(Number(s))) return '-';
    const total = Math.max(0, Math.round(Number(s)));
    const hh = Math.floor(total / 3600), mm = Math.floor((total % 3600) / 60), ss = total % 60;
    const p = (n) => String(n).padStart(2, '0');
    return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
  }
  const fmtScore = (v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(2) : '-');
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  const PILL_KIND = {
    queued: 'neutral', downloaded: 'info', transcribed: 'info', selected: 'purple', rendered: 'teal', done: 'ok', failed: 'bad',
    candidate: 'neutral', ready: 'ok', rejected: 'bad', posted: 'info',
    running: 'warn', pending: 'neutral', uploading: 'warn',
    OK: 'ok', WARN: 'warn', FAIL: 'bad', INFO: 'info',
    info: 'neutral', warning: 'warn', error: 'bad', debug: 'neutral', critical: 'bad',
    configured: 'ok', 'not configured': 'neutral',
  };
  function pill(status, opts) {
    const el = h('span', { class: 'pill' });
    updatePill(el, status, opts);
    return el;
  }
  function updatePill(el, status, opts = {}) {
    const label = status == null ? '-' : String(status);
    const kind = PILL_KIND[label] || 'neutral';
    const sig = `${label}|${kind}|${opts.spinner ? 1 : 0}`;
    if (el._sig === sig) return;
    el._sig = sig;
    el.dataset.kind = kind;
    el.dataset.status = label;
    el.replaceChildren();
    if (opts.spinner) el.appendChild(h('span', { class: 'spinner', 'aria-hidden': 'true' }));
    el.appendChild(document.createTextNode(label));
  }

  // ---- toasts, dialogs -------------------------------------------------------------------------------------------------
  function toast(msg, kind = 'info', ms) {
    const el = h('div', { class: `toast toast-${kind}`, role: 'status' },
      h('span', { class: 'toast-text' }, msg),
      h('button', { class: 'toast-close', type: 'button', 'aria-label': 'Dismiss', onClick: () => el.remove() }, '\u2715'));
    $('#toasts').appendChild(el);
    const ttl = ms || (kind === 'error' ? 7000 : 4000);
    setTimeout(() => { el.classList.add('hide'); setTimeout(() => el.remove(), 300); }, ttl);
  }

  function confirmDialog(message, okLabel = 'Confirm') {
    const d = $('#confirm-dialog');
    if (typeof d.showModal !== 'function') return Promise.resolve(window.confirm(message));
    setText($('#confirm-text'), message);
    setText($('#confirm-ok'), okLabel);
    return new Promise((resolve) => {
      const onClose = () => { d.removeEventListener('close', onClose); resolve(d.returnValue === 'ok'); };
      d.addEventListener('close', onClose);
      d.returnValue = '';
      d.showModal();
    });
  }
  function openDialog(d) {
    if (typeof d.showModal === 'function') { if (!d.open) d.showModal(); } else d.setAttribute('open', '');
  }
  function closeDialog(d) {
    if (typeof d.close === 'function') { if (d.open) d.close(); } else d.removeAttribute('open');
  }

  async function copyText(text, sourceEl) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(text); return true; }
    } catch (_) { /* fall through to the selection fallback */ }
    try {
      let ta = sourceEl && sourceEl.tagName === 'TEXTAREA' ? sourceEl : null;
      const temp = !ta;
      if (temp) {
        ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
      }
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      if (temp) ta.remove();
      return ok;
    } catch (_) { return false; }
  }

  // ---- API -----------------------------------------------------------------------------------------------------------
  function detailText(data, res) {
    if (data && data.detail != null) {
      const d = data.detail;
      if (typeof d === 'string') return d;
      if (Array.isArray(d)) {
        return d.map((e) => {
          if (e && typeof e === 'object') {
            const loc = Array.isArray(e.loc) ? e.loc.filter((x) => x !== 'body').join('.') : '';
            return (loc ? `${loc}: ` : '') + (e.msg || JSON.stringify(e));
          }
          return String(e);
        }).join('; ');
      }
      return JSON.stringify(d);
    }
    return `${res.status} ${res.statusText || ''}`.trim();
  }
  const API_TIMEOUT_MS = 15000; // most calls answer in milliseconds; doctor and settings reloads get longer budgets
  const GET_RETRY_MS = 600; // one jittered retry for idempotent reads that failed on the network
  async function api(method, path, body, opts) {
    // X-ClipForge: the server refuses POST/PUT/DELETE without it, so a cross-site page cannot drive the API without a CORS preflight
    const timeoutMs = (opts && opts.timeoutMs) || API_TIMEOUT_MS;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    const init = { method, headers: { Accept: 'application/json', 'X-ClipForge': '1' }, signal: ctl.signal };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, init); } catch (err) {
      clearTimeout(timer);
      const timedOut = err && err.name === 'AbortError';
      if (method === 'GET' && !(opts && opts.noRetry)) {
        // idempotent read: one more try after a short jittered pause (a mutation is never retried blindly)
        await new Promise((r) => setTimeout(r, GET_RETRY_MS + Math.random() * GET_RETRY_MS));
        return api(method, path, body, { ...(opts || {}), noRetry: true });
      }
      // a timed-out mutation may still have been applied server-side: the next poll shows the truth
      const e = new Error(timedOut ? `this is taking too long (no answer after ${Math.round(timeoutMs / 1000)} s); check the ClipForge window` : 'server unreachable');
      e.network = true;
      throw e;
    }
    clearTimeout(timer);
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = { detail: text.slice(0, 500) }; }
    }
    if (!res.ok) {
      const e = new Error(detailText(data, res));
      e.status = res.status;
      e.data = data;
      throw e;
    }
    return data;
  }

  // ---- state + polling -------------------------------------------------------------------------------------------------
  const S = {
    state: null,
    tab: 'dashboard',
    failing: false,
    polling: false,
    gen: 0, // bumped by refresh(): a /api/state response requested before a mutation is discarded and re-polled at once
    pollTimer: null,
    logsTimer: null,
    watchers: new Map(), // job id -> {onDone, onFail}
    selected: new Set(), // publish table selection (clip ids)
    review: { video: '', status: 'rendered', sort: 'score_desc' },
    publishJobId: null,
    settingsDirty: false,
    settingsLoaded: false,
    defaultsApplied: false,
    manual: null,
  };
  const videosById = () => new Map(((S.state && S.state.videos) || []).map((v) => [v.id, v]));
  const videoLabel = (v) => (v ? (v.title || v.source || v.id) : '');
  const publishable = (clips) => clips.filter((c) => c.status === 'ready' || c.status === 'posted');

  function schedulePoll(ms) {
    clearTimeout(S.pollTimer);
    S.pollTimer = setTimeout(poll, ms);
  }
  async function poll() {
    if (document.hidden || S.polling) return;
    S.polling = true;
    const gen = S.gen;
    try {
      const q = S.review && S.review.video ? '?video=' + encodeURIComponent(S.review.video) : '';
      const state = await api('GET', '/api/state' + q, undefined, { timeoutMs: 15000 });
      S.failing = false;
      setOffline(false);
      if (gen === S.gen) { S.state = normaliseState(state); render(); checkWatchers(); }
      if (document.body.dataset.loaded !== 'true') document.body.dataset.loaded = 'true';
    } catch (err) {
      S.failing = true;
      setOffline(true);
    } finally {
      S.polling = false;
    }
    if (!document.hidden) schedulePoll(gen !== S.gen ? 0 : S.failing ? POLL_FAIL_MS : POLL_MS);
  }
  const refresh = () => { S.gen++; schedulePoll(60); };
  function normaliseState(st) {
    st = st || {};
    st.videos = Array.isArray(st.videos) ? st.videos : [];
    st.clips = Array.isArray(st.clips) ? st.clips : [];
    st.jobs = Array.isArray(st.jobs) ? st.jobs : [];
    st.scheduler = st.scheduler || {};
    st.settings = st.settings || {};
    return st;
  }
  function setOffline(on) {
    show($('#offline-banner'), on);
    document.body.classList.toggle('offline', on);
  }
  function watchJob(jobId, handlers) {
    if (jobId == null) return;
    S.watchers.set(String(jobId), handlers);
  }
  function checkWatchers() {
    if (!S.watchers.size) return;
    for (const job of S.state.jobs) {
      const w = S.watchers.get(String(job.id));
      if (!w) continue;
      if (job.status === 'done') { S.watchers.delete(String(job.id)); if (w.onDone) w.onDone(job); }
      else if (job.status === 'failed') { S.watchers.delete(String(job.id)); if (w.onFail) w.onFail(job); }
      else if (job.status === 'running' && w.onRunning && !w.ran) { w.ran = true; w.onRunning(job); }
    }
  }

  // ---- render dispatch -------------------------------------------------------------------------------------------------
  function render() {
    const st = S.state;
    if (!st) return;
    renderNav(st);
    renderFooter(st);
    applyDefaults(st);
    if (S.tab === 'dashboard') renderDashboard(st);
    else if (S.tab === 'review') renderReview(st);
    else if (S.tab === 'publish') renderPublish(st);
    else if (S.tab === 'schedule') renderSchedule(st);
  }
  function renderNav(st) {
    const active = st.jobs.filter((j) => j.status === 'running' || j.status === 'queued');
    const busy = $('#nav-busy');
    show(busy, active.length > 0);
    if (active.length) {
      setText($('#nav-busy-text'), plural(active.length, 'job') + ' running');
      busy.title = active.map((j) => `${j.kind}: ${j.progress || j.detail || j.status}`).join('\n');
    }
  }
  function renderFooter(st) {
    setText($('#workspace-path'), st.settings.workspace || '-');
    setText($('#config-path'), st.settings.config_path || '-');
  }

  // ---- tabs ----------------------------------------------------------------------------------------------------------
  function showTab(name, fromHash) {
    if (!TABS.includes(name)) name = 'dashboard';
    S.tab = name;
    for (const b of $$('.tab')) {
      const on = b.dataset.tab === name;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    }
    for (const p of $$('.panel')) show(p, p.id === `tab-${name}`);
    if (!fromHash && location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'schedule') startLogs(); else stopLogs();
    if (name === 'settings' && !S.settingsLoaded) loadSettingsYaml(false);
    render();
  }
  function initTabs() {
    const list = $('#tablist');
    list.addEventListener('click', (ev) => {
      const b = ev.target.closest('.tab');
      if (b) showTab(b.dataset.tab);
    });
    list.addEventListener('keydown', (ev) => {
      const keys = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1, Home: 'first', End: 'last' };
      const step = keys[ev.key];
      if (step === undefined) return;
      ev.preventDefault();
      const tabs = $$('.tab', list);
      let i = tabs.findIndex((b) => b.dataset.tab === S.tab);
      i = step === 'first' ? 0 : step === 'last' ? tabs.length - 1 : (i + step + tabs.length) % tabs.length;
      tabs[i].focus();
      showTab(tabs[i].dataset.tab);
    });
    window.addEventListener('hashchange', () => showTab(location.hash.slice(1), true));
  }

  // ==== Dashboard =====================================================================================================
  function applyDefaults(st) {
    const s = st.settings;
    const f = $('#add-form');
    const styles = Array.isArray(s.styles) ? s.styles : [];
    syncOptions(f.elements.style, styles.map((n) => [n, n]), f.elements.style.value || s.style || '');
    if (S.defaultsApplied) return;
    S.defaultsApplied = true;
    const clips = s.clips || {};
    if (clips.count != null) f.elements.count.value = clips.count;
    if (clips.min_s != null) f.elements.min_s.value = clips.min_s;
    if (clips.max_s != null) f.elements.max_s.value = clips.max_s;
    if (styles.includes(s.style)) f.elements.style.value = s.style;
    if (s.layout) f.elements.layout.value = s.layout;
    f.elements.tighten.checked = !!s.tighten;
    f.elements.smart.checked = !!s.smart;
    f.elements.punch.checked = !!s.punch;
  }

  function renderDashboard(st) {
    renderStats(st);
    renderVideos(st);
    renderJobs(st);
  }
  function renderStats(st) {
    const count = (status) => st.clips.filter((c) => c.status === status).length;
    const total = st.totals && st.totals.videos != null ? st.totals.videos : st.videos.length;
    const queued = st.videos.filter((v) => v.status === 'queued').length;
    const running = st.videos.filter((v) => ['downloaded', 'transcribed', 'selected', 'rendered'].includes(v.status)).length;
    const failed = st.videos.filter((v) => v.status === 'failed').length;
    setText($('#stat-videos'), String(total));
    setText($('#stat-videos-sub'), running ? `${plural(running, 'video')} processing` : queued ? `${plural(queued, 'video')} queued` : failed ? `${plural(failed, 'video')} failed` : total ? 'all processed' : 'none queued');
    setText($('#stat-rendered'), String(count('rendered')));
    setText($('#stat-ready'), String(count('ready')));
    setText($('#stat-posted'), String(count('posted')));
  }

  function renderVideos(st) {
    const posted = new Set();
    for (const c of st.clips) {
      if (c.status === 'posted' || PLATFORMS.some((p) => c.posts && c.posts[p] && c.posts[p].status === 'posted')) posted.add(c.video_id);
    }
    const videos = [...st.videos].reverse(); // newest first
    show($('#videos-empty'), videos.length === 0);
    show($('#videos-table'), videos.length > 0);
    setText($('#videos-count'), videos.length ? plural(videos.length, 'video') : '');
    syncList($('#videos-body'), videos, (v) => v.id, createVideoRow, (tr, v) => updateVideoRow(tr, v, posted.has(v.id)));
  }
  function createVideoRow(v) {
    const r = {};
    const id = v.id;
    r.title = h('div', { class: 'cell-title' });
    r.source = h('div', { class: 'cell-sub' });
    r.pill = pill(v.status);
    r.duration = h('span');
    r.clips = h('span');
    r.errorText = h('pre', { class: 'error-text' });
    r.error = h('details', { class: 'error-details' }, h('summary', {}, 'show error'), r.errorText);
    r.noError = h('span', { class: 'muted' }, '-');
    r.run = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => runVideo(id, false) }, 'Run');
    r.rerun = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => runVideo(id, true) }, 'Re-run (force)');
    r.del = h('button', { class: 'btn btn-sm btn-danger', type: 'button', onClick: () => deleteVideo(id) }, 'Delete');
    const tr = h('tr', {},
      h('td', { 'data-label': 'Video' }, r.title, r.source),
      h('td', { 'data-label': 'Status' }, r.pill),
      h('td', { 'data-label': 'Duration' }, r.duration),
      h('td', { 'data-label': 'Clips' }, r.clips),
      h('td', { 'data-label': 'Error' }, r.error, r.noError),
      h('td', { 'data-label': 'Actions', class: 'actions' }, r.run, r.rerun, r.del));
    tr._refs = r;
    return tr;
  }
  function updateVideoRow(tr, v, hasPosted) {
    const r = tr._refs;
    setText(r.title, v.title || v.source || v.id);
    const bits = [v.kind, v.id];
    if (v.title) bits.push(v.source);
    setText(r.source, bits.filter(Boolean).join(DOT));
    updatePill(r.pill, v.status);
    r.pill.title = v.updated_at ? `updated ${absTime(v.updated_at)}` : '';
    setText(r.duration, fmtDur(v.duration));
    setText(r.clips, v.clip_count != null ? String(v.clip_count) : '0');
    const err = v.error || '';
    show(r.error, !!err);
    show(r.noError, !err);
    if (err) { setText(r.errorText, err); r.error.title = err; }
    r.del.disabled = hasPosted;
    r.del.title = hasPosted ? 'A clip of this video is posted; it cannot be deleted' : 'Remove the video, its clips and its workspace folder';
  }
  async function runVideo(id, force) {
    if (force && !(await confirmDialog(`Re-run ${id}? Existing clips are discarded, re-selected and re-rendered (posted clips are kept; the download and transcript are reused).`, 'Re-run'))) return;
    try {
      const r = await api('POST', `/api/videos/${enc(id)}/run`, { force: !!force });
      toast(`${force ? 're-run' : 'run'} started for ${id} (job ${r.job_id})`, 'ok');
      refresh();
    } catch (e) { toast(e.message, 'error'); }
  }
  async function deleteVideo(id) {
    if (!(await confirmDialog(`Delete ${id}, its clips and its workspace folder? This cannot be undone.`, 'Delete'))) return;
    try {
      await api('DELETE', `/api/videos/${enc(id)}`);
      toast(`deleted ${id}`, 'ok');
      refresh();
    } catch (e) { toast(e.message, 'error'); }
  }
  async function runQueue() {
    const btn = $('#run-queue');
    setPending(btn, true);
    try {
      const r = await api('POST', '/api/run');
      toast(`queue run started (job ${r.job_id})`, 'ok');
      refresh();
    } catch (e) { toast(e.message, 'error'); } finally { setPending(btn, false); }
  }
  function numOrUndef(v) {
    const s = String(v ?? '').trim();
    if (!s) return undefined;
    const n = Number(s);
    return Number.isFinite(n) ? n : undefined;
  }
  async function addVideo(ev) {
    ev.preventDefault();
    const f = ev.currentTarget;
    const errEl = $('#add-error');
    const source = f.elements.source.value.trim();
    if (!source) { showErr(errEl, 'Enter a URL or a local file path.'); f.elements.source.focus(); return; }
    const options = {};
    const count = numOrUndef(f.elements.count.value);
    const minS = numOrUndef(f.elements.min_s.value);
    const maxS = numOrUndef(f.elements.max_s.value);
    if (count !== undefined) options.count = Math.max(1, Math.round(count));
    if (minS !== undefined) options.min_s = minS;
    if (maxS !== undefined) options.max_s = maxS;
    if (minS !== undefined && maxS !== undefined && minS > maxS) { showErr(errEl, 'Min seconds must not exceed max seconds.'); return; }
    if (f.elements.style.value) options.style = f.elements.style.value;
    options.layout = f.elements.layout.value;
    options.tighten = f.elements.tighten.checked;
    options.smart = f.elements.smart.checked;
    options.punch = f.elements.punch.checked;
    options.force_whisper = f.elements.force_whisper.checked;
    const submit = f.querySelector('button[type=submit]');
    setPending(submit, true);
    try {
      const r = await api('POST', '/api/videos', { source, options });
      showErr(errEl, '');
      if (r.created) { toast(`queued ${r.video_id}`, 'ok'); f.elements.source.value = ''; }
      else toast(`already added (${r.video_id}, ${r.status || 'in queue'})`, 'info');
      refresh();
    } catch (e) { showErr(errEl, e.message); } finally { setPending(submit, false); }
  }

  function renderJobs(st) {
    const jobs = st.jobs;
    show($('#jobs-empty'), jobs.length === 0);
    setText($('#jobs-count'), jobs.length ? `${jobs.length} recent` : '');
    syncList($('#jobs-list'), jobs, (j) => j.id, createJobRow, updateJobRow);
  }
  function createJobRow(j) {
    const r = {};
    r.kind = h('span', { class: 'job-kind' });
    r.pill = pill(j.status);
    r.time = timeEl(j.started_at);
    r.detail = h('div', { class: 'job-detail' });
    r.note = h('div', { class: 'job-progress' }); // the backend's free-text progress line (which pair is uploading, the sign-in URL hint)
    r.bar = h('span');
    r.progress = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100 }, r.bar);
    r.error = h('div', { class: 'job-error' });
    const li = h('li', { class: 'job' }, r.kind, r.pill, r.time, r.detail, r.note, r.progress, r.error);
    li._refs = r;
    return li;
  }
  /** Numeric progress (0-1 or 0-100) -> percentage; null for the free-text lines the backend sends. */
  function progressPct(p) {
    if (p == null || p === '' || typeof p === 'boolean' || !Number.isFinite(Number(p))) return null;
    const n = Number(p);
    return Math.max(0, Math.min(100, n <= 1 ? n * 100 : n));
  }
  function updateJobRow(li, j) {
    const r = li._refs;
    setText(r.kind, `${j.kind || 'job'}${DOT}${j.id}`);
    updatePill(r.pill, j.status, { spinner: j.status === 'running' });
    const ts = j.finished_at || j.started_at;
    updateTime(r.time, ts);
    if (ts) r.time.title = `${j.finished_at ? 'finished' : 'started'} ${absTime(ts)}`;
    const detail = j.detail || (j.status === 'queued' ? 'waiting for a worker' : '');
    setText(r.detail, detail);
    show(r.detail, !!detail);
    const pct = progressPct(j.progress);
    const note = pct == null && typeof j.progress === 'string' ? j.progress.trim() : '';
    setText(r.note, note);
    show(r.note, !!note);
    show(r.progress, pct != null && j.status === 'running');
    if (pct != null) { r.bar.style.width = `${pct}%`; r.progress.setAttribute('aria-valuenow', Math.round(pct)); }
    setText(r.error, j.error || '');
    show(r.error, !!j.error);
  }

  // ==== Review =========================================================================================================
  function reviewFilter(st) {
    const f = S.review;
    return st.clips.filter((c) => (!f.video || c.video_id === f.video) && (f.status === 'all' || c.status === f.status));
  }
  function sortClips(clips) {
    const s = S.review.sort;
    const arr = [...clips];
    if (s === 'score_asc') arr.sort((a, b) => (a.score || 0) - (b.score || 0) || a.id.localeCompare(b.id));
    else if (s === 'position') arr.sort((a, b) => a.video_id.localeCompare(b.video_id) || (a.idx || 0) - (b.idx || 0));
    else arr.sort((a, b) => (b.score || 0) - (a.score || 0) || a.id.localeCompare(b.id));
    return arr;
  }
  function renderReview(st) {
    const videos = videosById();
    syncOptions($('#review-video'), [['', 'all videos'], ...st.videos.map((v) => [v.id, videoLabel(v)])], S.review.video);
    S.review.video = $('#review-video').value;
    const clips = sortClips(reviewFilter(st));
    const grid = $('#review-grid');
    const anyPlaying = $$('video', grid).some((v) => !v.paused && !v.ended);
    syncList(grid, clips, (c) => c.id, createClipCard, (card, c) => updateClipCard(card, c, videos), { noReorder: anyPlaying });
    const empty = $('#review-empty');
    show(empty, clips.length === 0);
    if (!clips.length) {
      const f = S.review;
      if (!st.clips.length) setText(empty, 'No clips yet - run a video from the Dashboard.');
      else if (f.status === 'all') setText(empty, 'No clips for this video.');
      else setText(empty, `No ${f.status} clips${f.video ? ' for this video' : ''} - ${plural(st.clips.length, 'clip')} in other statuses (set Status to "all").`);
    }
    const tr = S.state && S.state.truncated && S.state.truncated.clips && !S.review.video;
    const total = S.state && S.state.totals ? S.state.totals.clips : null;
    setText($('#review-count'), clips.length ? plural(clips.length, 'clip') + (tr && total ? ` shown of ${total} - pick a video to see all of its clips` : '') : '');
    const rendered = st.clips.filter((c) => c.status === 'rendered' && (!S.review.video || c.video_id === S.review.video));
    const bulk = $('#approve-all');
    bulk.disabled = rendered.length === 0;
    setText(bulk, rendered.length ? `Approve all rendered (${rendered.length})` : 'Approve all rendered');
  }
  function createClipCard(c) {
    const r = {};
    const id = c.id;
    r.video = h('video', { controls: true, preload: 'metadata', playsinline: true, onError: () => videoFailed(r) });
    r.noMedia = h('div', { class: 'no-media' }, 'Not rendered yet');
    r.pill = pill(c.status);
    r.score = h('b');
    r.badge = h('span', { class: 'score-badge', title: 'selection score' }, h('small', {}, 'score'), r.score);
    r.badges = h('div', { class: 'clip-badges' }, r.badge, r.pill);
    r.box = h('div', { class: 'video-box' }, r.video, r.noMedia, r.badges);
    r.hook = h('p', { class: 'hook' });
    r.len = h('b');
    r.vtitle = h('span', { class: 'clip-video muted' });
    r.approve = h('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => setClipStatus(id, 'ready') }, 'Approve');
    r.reject = h('button', { class: 'btn btn-sm btn-danger', type: 'button', onClick: () => setClipStatus(id, 'rejected') }, 'Reject');
    r.undo = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => setClipStatus(id, 'ready') }, 'Undo (back to ready)');
    r.note = h('span', { class: 'muted small' });
    r.actions = h('div', { class: 'card-actions' }, r.approve, r.reject, r.undo, r.note);
    // metadata editor
    const fid = (name) => `clip-${id}-${name}`; // label[for] -> input id: clicking a label focuses, screen readers read the hint
    r.title = h('input', { type: 'text', name: 'title', id: fid('title'), autocomplete: 'off' });
    r.counter = h('span', { class: 'counter muted small' }, `0/${TITLE_MAX}`);
    r.desc = h('textarea', { name: 'description', id: fid('desc'), rows: 4 });
    r.tagsYt = h('input', { type: 'text', name: 'hashtags_youtube', id: fid('tags-yt'), placeholder: '#shorts #topic ...' });
    r.tagsTt = h('input', { type: 'text', name: 'hashtags_tiktok', id: fid('tags-tt'), placeholder: '#fyp #topic ...' });
    r.formError = h('p', { class: 'form-error', role: 'alert', hidden: true });
    r.saveNote = h('span', { class: 'muted small' });
    r.save = h('button', { class: 'btn btn-sm btn-primary', type: 'submit' }, 'Save metadata');
    r.form = h('form', { class: 'meta-form', novalidate: true, onSubmit: (ev) => saveMeta(ev, id, r) },
      h('div', { class: 'field' }, h('div', { class: 'field-head' }, h('label', { for: fid('title') }, 'Title'), r.counter), r.title),
      h('div', { class: 'field' }, h('label', { for: fid('desc') }, 'Description'), r.desc),
      h('div', { class: 'field' }, h('label', { for: fid('tags-yt') }, 'YouTube hashtags (3-6, comma or space separated)'), r.tagsYt),
      h('div', { class: 'field' }, h('label', { for: fid('tags-tt') }, 'TikTok hashtags (3-6)'), r.tagsTt),
      h('div', { class: 'row' }, r.save, r.saveNote),
      r.formError);
    r.title.addEventListener('input', () => updateCounter(r));
    r.details = h('details', { class: 'meta-edit' }, h('summary', {}, 'Edit metadata'), r.form);
    const card = h('article', { class: 'clip-card', tabindex: 0, onKeydown: (ev) => cardKeys(ev, card) },
      r.box,
      h('div', { class: 'clip-body' },
        r.hook,
        h('p', { class: 'facts' }, h('span', {}, 'length ', r.len)),
        r.vtitle,
        r.actions,
        r.details));
    card._refs = r;
    return card;
  }
  function updateCounter(r) {
    const n = r.title.value.length;
    setText(r.counter, `${n}/${TITLE_MAX}`);
    r.counter.classList.toggle('over', n > TITLE_MAX);
  }
  function fillMetaForm(r, meta) {
    const m = meta || {};
    r.title.value = m.title || '';
    r.desc.value = m.description || '';
    const tags = m.hashtags || {};
    r.tagsYt.value = (tags.youtube || []).join(' ');
    r.tagsTt.value = (tags.tiktok || []).join(' ');
    updateCounter(r);
  }
  function updateClipCard(card, c, videos) {
    const r = card._refs;
    card._clip = c;
    card.dataset.status = c.status;
    card.setAttribute('aria-label', `Clip ${c.id}, ${c.status}`);
    const src = c.media_url || '';
    if (src) {
      if (r.video.getAttribute('src') !== src) { r.video.setAttribute('src', src); r.failedSrc = null; }
      const failed = r.failedSrc === src;
      show(r.video, !failed);
      show(r.noMedia, failed);
    } else {
      if (r.video.getAttribute('src')) r.video.removeAttribute('src');
      r.failedSrc = null;
      setText(r.noMedia, 'Not rendered yet');
      show(r.video, false);
      show(r.noMedia, true);
    }
    setText(r.hook, c.hook || (c.meta && c.meta.title) || '(no hook)');
    setText(r.score, fmtScore(c.score));
    const len = c.duration != null ? c.duration : (Number(c.end) - Number(c.start));
    setText(r.len, fmtDur(len));
    r.len.title = `${fmtDur(c.start)} - ${fmtDur(c.end)} in the source`;
    updatePill(r.pill, c.status);
    const v = videos.get(c.video_id);
    setText(r.vtitle, videoLabel(v) || c.video_id);
    r.vtitle.title = c.video_id;
    show(r.approve, c.status === 'rendered');
    show(r.reject, c.status === 'rendered' || c.status === 'ready');
    show(r.undo, c.status === 'rejected');
    const note = c.status === 'posted' ? 'posted - decisions are locked' : c.status === 'candidate' ? 'waiting to be rendered' : c.status === 'failed' ? (c.error || 'render failed') : '';
    setText(r.note, note);
    show(r.note, !!note);
    if (!r.details.open) fillMetaForm(r, c.meta); // never clobber a form the user is editing
    const posted = c.status === 'posted';
    r.save.disabled = posted;
    setText(r.saveNote, posted ? 'locked after posting' : (c.meta ? '' : 'no metadata file yet'));
  }
  /** The browser could not decode the file (e.g. a Chromium build without H.264): say so and offer the download. */
  function videoFailed(r) {
    const src = r.video.getAttribute('src');
    if (!src || !r.video.error) return;
    r.failedSrc = src;
    r.noMedia.replaceChildren(h('span', { class: 'no-media-text' }, 'This browser cannot play the file - ',
      h('a', { href: safeHref(src), download: '' }, 'download it'), ' and check it in a video player.'));
    show(r.video, false);
    show(r.noMedia, true);
  }
  function cardKeys(ev, card) {
    if (ev.target.closest('input, textarea, select')) return;
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
    const c = card._clip;
    if (!c) return;
    const k = ev.key.toLowerCase();
    if (k === 'a' && c.status === 'rendered') { ev.preventDefault(); setClipStatus(c.id, 'ready'); }
    else if (k === 'r' && (c.status === 'rendered' || c.status === 'ready')) { ev.preventDefault(); setClipStatus(c.id, 'rejected'); }
  }
  async function setClipStatus(id, status) {
    try {
      const clip = await api('POST', `/api/clips/${enc(id)}/status`, { status });
      if (S.state) {
        const i = S.state.clips.findIndex((c) => c.id === id);
        if (i >= 0) S.state.clips[i] = { ...S.state.clips[i], ...(clip || {}), status: (clip && clip.status) || status };
        render();
      }
      toast(`${id} \u2192 ${status}`, 'ok', 2500);
      refresh();
    } catch (e) { toast(e.message, 'error'); }
  }
  async function approveAllRendered() {
    if (!S.state) return;
    const clips = S.state.clips.filter((c) => c.status === 'rendered' && (!S.review.video || c.video_id === S.review.video));
    if (!clips.length) { toast('no rendered clips to approve'); return; }
    if (!(await confirmDialog(`Approve ${plural(clips.length, 'rendered clip')} (mark as ready)?`, 'Approve all'))) return;
    let ok = 0;
    const failed = [];
    for (const c of clips) {
      try { await api('POST', `/api/clips/${enc(c.id)}/status`, { status: 'ready' }); ok++; } catch (e) { failed.push(`${c.id}: ${e.message}`); }
    }
    toast(`approved ${ok}${failed.length ? `, ${failed.length} failed: ${failed[0]}` : ''}`, failed.length ? 'error' : 'ok');
    refresh();
  }
  function parseTags(text) {
    return String(text || '').split(/[\s,]+/).map((t) => t.trim()).filter(Boolean).map((t) => (t.startsWith('#') ? t : `#${t}`));
  }
  function validateMeta(title, hashtags) {
    const errs = [];
    if (!title) errs.push('Title is required.');
    else if (title.length > TITLE_MAX) errs.push(`Title is ${title.length} characters; the limit is ${TITLE_MAX}.`);
    for (const p of PLATFORMS) {
      const tags = hashtags[p] || [];
      if (tags.length < MIN_TAGS || tags.length > MAX_TAGS) errs.push(`${PLATFORM_LABEL[p]}: ${plural(tags.length, 'hashtag')}, need ${MIN_TAGS}-${MAX_TAGS}.`);
      if (tags.some((t) => !t.startsWith('#') || t.length < 2 || /\s/.test(t))) errs.push(`${PLATFORM_LABEL[p]}: every hashtag must start with # and contain no spaces.`);
    }
    return errs;
  }
  async function saveMeta(ev, clipId, r) {
    ev.preventDefault();
    const title = r.title.value.trim();
    const description = r.desc.value;
    const hashtags = { youtube: parseTags(r.tagsYt.value), tiktok: parseTags(r.tagsTt.value) };
    const errs = validateMeta(title, hashtags);
    if (errs.length) { showErr(r.formError, errs.join(' ')); return; }
    setPending(r.save, true);
    try {
      const meta = await api('PUT', `/api/clips/${enc(clipId)}/meta`, { title, description, hashtags });
      showErr(r.formError, '');
      fillMetaForm(r, meta && meta.title ? meta : { title, description, hashtags });
      setText(r.saveNote, `saved ${new Date().toLocaleTimeString()}`);
      toast(`metadata saved for ${clipId}`, 'ok');
      refresh();
    } catch (e) { showErr(r.formError, e.message); } finally { setPending(r.save, false); }
  }

  // ==== Publish ========================================================================================================
  function renderPublish(st) {
    const sch = st.scheduler;
    const configured = sch.configured || {};
    const limits = sch.limits || {};
    for (const p of PLATFORMS) {
      const card = $(`#plat-${p}`);
      const isOn = !!configured[p];
      updatePill($('[data-role=configured]', card), isOn ? 'configured' : 'not configured');
      const lim = limits[p];
      const limEl = $('[data-role=limits]', card);
      const noteEl = $('[data-role=note]', card);
      if (lim) {
        setText(limEl, `${lim.posted_today ?? 0} of ${lim.per_day ?? '?'} posted today${DOT}${lim.remaining ?? '?'} remaining`);
        setText(noteEl, lim.note || '');
      } else {
        setText(limEl, isOn ? 'Limits unavailable' : `Not configured - connect ${PLATFORM_LABEL[p]} or use the manual path per clip.`);
        setText(noteEl, '');
      }
    }
    fillTikTokPosting((st.settings && st.settings.tiktok) || null);
    const videos = videosById();
    const clips = publishable(st.clips);
    const ids = new Set(clips.map((c) => c.id));
    for (const id of [...S.selected]) if (!ids.has(id)) S.selected.delete(id);
    show($('#publish-empty'), clips.length === 0);
    show($('#publish-table'), clips.length > 0);
    syncList($('#publish-body'), clips, (c) => c.id, createPublishRow, (tr, c) => updatePublishRow(tr, c, videos, configured));
    updatePublishControls(clips);
    renderPublishResult(st);
  }
  function updatePublishControls(clips) {
    const all = $('#select-all');
    const n = S.selected.size;
    all.checked = clips.length > 0 && n === clips.length;
    all.indeterminate = n > 0 && n < clips.length;
    const platforms = PLATFORMS.filter((p) => $(`#pub-${p}`).checked);
    const btn = $('#publish-selected');
    btn.disabled = n === 0 || platforms.length === 0;
    setText(btn, n ? `Publish ${n} selected` : 'Publish selected');
  }
  function createPublishRow(c) {
    const r = { cells: {} };
    const id = c.id;
    r.check = h('input', { type: 'checkbox', 'aria-label': `Select ${id}`, onChange: (ev) => {
      if (ev.target.checked) S.selected.add(id); else S.selected.delete(id);
      if (S.state) updatePublishControls(publishable(S.state.clips));
    } });
    r.hook = h('div', { class: 'cell-title' });
    r.sub = h('div', { class: 'cell-sub' });
    r.pill = pill(c.status);
    const cells = PLATFORMS.map((p) => {
      const state = h('div', { class: 'post-state' });
      const manual = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => openManual(id, p) }, 'Manual\u2026');
      r.cells[p] = { state, manual };
      return h('td', { 'data-label': PLATFORM_LABEL[p] }, h('div', { class: 'post-cell' }, state, manual));
    });
    const tr = h('tr', {},
      h('td', { 'data-label': 'Select', class: 'col-check' }, r.check),
      h('td', { 'data-label': 'Clip' }, r.hook, r.sub, r.pill),
      ...cells);
    tr._refs = r;
    return tr;
  }
  function updatePublishRow(tr, c, videos, configured) {
    const r = tr._refs;
    r.check.checked = S.selected.has(c.id);
    setText(r.hook, (c.meta && c.meta.title) || c.hook || c.id);
    setText(r.sub, [c.id, fmtDur(c.duration != null ? c.duration : c.end - c.start), videoLabel(videos.get(c.video_id)) || c.video_id].join(DOT));
    updatePill(r.pill, c.status);
    for (const p of PLATFORMS) {
      const cell = r.cells[p];
      const post = c.posts ? c.posts[p] : null;
      const sig = JSON.stringify([post, !!configured[p]]);
      if (cell.sig !== sig) {
        cell.sig = sig;
        cell.state.replaceChildren(...postStateNodes(post, !!configured[p]));
      }
      const manualOn = !(post && post.status === 'posted');
      if (!manualOn && document.activeElement === cell.manual) r.check.focus(); // a hidden button would drop focus to <body>
      show(cell.manual, manualOn);
    }
  }
  /** Link for a post: the server's `url` (a URL post id as-is, a bare YouTube id as its Shorts URL), else a URL-looking id. */
  const postHref = (url, id) => (looksLikeUrl(url) ? safeHref(url) : looksLikeUrl(id) ? safeHref(id) : null);
  function postStateNodes(post, configured) {
    if (post && post.status === 'posted') {
      const href = postHref(post.url, post.post_id);
      const idNode = href
        ? h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'break' }, post.post_id)
        : h('code', { class: 'break' }, post.post_id || '');
      return [pill('posted'), idNode];
    }
    if (post) {
      const out = [pill(post.status)];
      if (post.attempts) out.push(h('span', { class: 'muted small' }, plural(post.attempts, 'attempt')));
      if (post.next_attempt_at) out.push(h('span', { class: 'muted small', title: absTime(post.next_attempt_at) }, `retry ${relTime(post.next_attempt_at)}`));
      if (post.error) out.push(h('div', { class: 'post-err' }, post.error));
      return out;
    }
    if (!configured) return [h('span', { class: 'muted small' }, 'not configured')];
    return [h('span', { class: 'muted small' }, 'not posted')];
  }
  async function publishSelected() {
    const clip_ids = [...S.selected];
    const platforms = PLATFORMS.filter((p) => $(`#pub-${p}`).checked);
    if (!clip_ids.length) { toast('select at least one clip'); return; }
    if (!platforms.length) { toast('pick at least one platform'); return; }
    const btn = $('#publish-selected');
    setPending(btn, true);
    try {
      const r = await api('POST', '/api/publish', { clip_ids, platforms });
      S.publishJobId = r.job_id;
      toast(`publish job ${r.job_id} started (${plural(clip_ids.length, 'clip')} \u00d7 ${platforms.join(', ')})`, 'ok');
      watchJob(r.job_id, {
        onDone: (j) => {
          const res = Array.isArray(j.result) ? j.result : [];
          const posted = res.filter((x) => x.state === 'posted').length;
          const errors = res.filter((x) => x.state === 'error').length;
          toast(`publish finished: ${posted} posted${errors ? `, ${plural(errors, 'error')}` : ''}`, errors ? 'error' : 'ok');
        },
        onFail: (j) => toast(`publish job failed: ${j.error || 'unknown error'}`, 'error'),
      });
      refresh();
    } catch (e) { toast(e.message, 'error'); } finally { if (S.state) updatePublishControls(publishable(S.state.clips)); }
  }
  const TT_KEYS = ['allow_comments', 'allow_duet', 'allow_stitch', 'commercial_content', 'brand_organic', 'branded_content', 'music_usage_confirmed'];
  function fillTikTokPosting(tt) {
    const form = $('#tiktok-posting');
    if (!form || !tt || S.ttDirty || form.contains(document.activeElement)) return;
    form.elements.privacy.value = tt.privacy || '';
    for (const k of TT_KEYS) if (form.elements[k]) form.elements[k].checked = !!tt[k];
    show($('#tt-disclosure'), !!tt.commercial_content);
    setText($('#tiktok-posting-status'), tt.privacy && tt.music_usage_confirmed ? 'ready to post' : 'incomplete: choose privacy and accept the music terms');
  }
  async function saveTikTokPosting(ev) {
    ev.preventDefault();
    const form = ev.target;
    const errEl = $('#tiktok-posting-error');
    const body = { privacy: form.elements.privacy.value || '' };
    for (const k of TT_KEYS) body[k] = !!form.elements[k].checked;
    show(errEl, false);
    try {
      await api('PUT', '/api/platforms/tiktok', body);
      S.ttDirty = false;
      toast('TikTok posting choices saved', 'ok');
      refresh();
    } catch (err) {
      setText(errEl, err.message || String(err));
      show(errEl, true);
    }
  }
  function renderPublishResult(st) {
    let job = S.publishJobId != null ? st.jobs.find((j) => String(j.id) === String(S.publishJobId)) : null;
    if (!job) job = st.jobs.find((j) => j.kind === 'publish') || null;
    const card = $('#publish-result-card');
    show(card, !!job);
    if (!job) return;
    const status = $('#publish-job-status');
    const sig = JSON.stringify([job.id, job.status, job.detail, job.error, job.result]);
    if (card._sig === sig) return;
    card._sig = sig;
    status.replaceChildren(pill(job.status, { spinner: job.status === 'running' }), ' ', h('span', { class: 'muted small' }, `job ${job.id}`), ' ', timeEl(job.finished_at || job.started_at, 'muted small'));
    const out = $('#publish-result');
    const nodes = [];
    if (job.detail) nodes.push(h('p', { class: 'muted small' }, job.detail));
    if (job.error) nodes.push(h('p', { class: 'form-error' }, job.error));
    if (Array.isArray(job.result) && job.result.length) {
      nodes.push(h('div', { class: 'table-wrap' }, h('table', { class: 'stack' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Clip'), h('th', {}, 'Platform'), h('th', {}, 'Result'), h('th', {}, 'Detail'))),
        h('tbody', {}, job.result.map((row) => {
          const href = postHref(row.url, row.detail);
          return h('tr', {},
            h('td', { 'data-label': 'Clip' }, h('code', {}, row.clip_id || '')),
            h('td', { 'data-label': 'Platform' }, PLATFORM_LABEL[row.platform] || row.platform || ''),
            h('td', { 'data-label': 'Result' }, h('span', { class: 'result-state', 'data-state': row.state || '' }, row.state || '')),
            h('td', { 'data-label': 'Detail' }, href
              ? h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'break' }, row.detail)
              : h('span', { class: 'break' }, row.detail || '')));
        })))));
    } else if (job.status === 'done') nodes.push(h('p', { class: 'muted' }, 'Nothing to publish.'));
    out.replaceChildren(...nodes);
  }

  // ---- manual publish dialog -------------------------------------------------------------------------------------------
  async function openManual(clipId, platform) {
    const clip = S.state && S.state.clips.find((c) => c.id === clipId);
    if (!clip) { toast('clip not found', 'error'); return; }
    const d = $('#manual-dialog');
    S.manual = { clipId, platform };
    setText($('#manual-title'), `Manual post to ${PLATFORM_LABEL[platform] || platform}`);
    setText($('#manual-clip'), [clip.id, clip.hook || ''].filter(Boolean).join(DOT));
    const caption = $('#manual-caption');
    caption.value = 'Loading caption\u2026';
    const upload = $('#manual-upload');
    upload.href = '#';
    upload.setAttribute('aria-disabled', 'true');
    const dl = $('#manual-download');
    if (clip.media_url) { dl.href = safeHref(clip.media_url); dl.setAttribute('download', `${clip.id}.mp4`); show(dl, true); } else show(dl, false);
    $('#manual-post-id').value = '';
    showErr($('#manual-error'), '');
    openDialog(d);
    try {
      const cap = await api('GET', `/api/clips/${enc(clipId)}/caption?platform=${enc(platform)}`);
      if (!S.manual || S.manual.clipId !== clipId || S.manual.platform !== platform) return; // dialog moved on
      caption.value = cap.caption || '';
      if (cap.upload_url) { upload.href = safeHref(cap.upload_url); upload.removeAttribute('aria-disabled'); }
    } catch (e) {
      caption.value = '';
      showErr($('#manual-error'), e.message);
    }
  }
  async function markPosted(ev) {
    ev.preventDefault();
    if (!S.manual) return;
    const { clipId, platform } = S.manual;
    const input = $('#manual-post-id');
    const post_id = input.value.trim();
    if (!post_id) { showErr($('#manual-error'), 'Enter the post URL or id.'); input.focus(); return; }
    const btn = ev.currentTarget.querySelector('button[type=submit]');
    setPending(btn, true);
    try {
      await api('POST', `/api/clips/${enc(clipId)}/posted`, { platform, post_id });
      toast(`${clipId} marked as posted on ${PLATFORM_LABEL[platform] || platform}`, 'ok');
      closeDialog($('#manual-dialog'));
      S.manual = null;
      const tr = $$('#publish-body tr').find((t) => t.dataset.key === clipId); // the Manual button that opened the dialog is about to hide
      if (tr && tr._refs) tr._refs.check.focus();
      refresh();
    } catch (e) { showErr($('#manual-error'), e.message); } finally { setPending(btn, false); }
  }

  // ---- auth ----------------------------------------------------------------------------------------------------------
  async function connectYouTube() {
    const btn = $('#connect-youtube');
    const errEl = $('#youtube-error'); // setup instructions are several lines: a toast is too small and too short-lived for them
    setPending(btn, true);
    try {
      const r = await api('POST', '/api/auth/youtube/start');
      showErr(errEl, '');
      toast(`YouTube sign-in started (job ${r.job_id})`, 'info', 2500);
      watchJob(r.job_id, {
        // only claim a window once the job actually runs the flow; the job row shows the sign-in URL if none opens
        onRunning: () => toast('a browser window opened on this machine; finish the Google sign-in there (the URL is in the job row and on the server console if none opened)', 'info', 9000),
        onDone: () => toast('YouTube connected', 'ok'),
        onFail: (j) => toast(`YouTube sign-in failed: ${j.error || 'unknown error'}`, 'error', 9000),
      });
      refresh();
    } catch (e) { showErr(errEl, e.message); } finally { setPending(btn, false); }
  }
  async function connectTikTok() {
    const btn = $('#connect-tiktok');
    const box = $('#tiktok-auth');
    const errEl = $('#tiktok-error');
    setPending(btn, true);
    try {
      const r = await api('POST', '/api/auth/tiktok/start');
      showErr(errEl, '');
      const link = $('#tiktok-auth-url');
      link.href = safeHref(r.auth_url);
      setText($('#tiktok-auth-text'), r.auth_url || '');
      $('#tiktok-redirect').value = '';
      show(box, true);
      $('#tiktok-redirect').focus();
    } catch (e) { show(box, false); showErr(errEl, e.message); } finally { setPending(btn, false); }
  }
  async function completeTikTok(ev) {
    ev.preventDefault();
    const input = $('#tiktok-redirect');
    const redirect_url = input.value.trim();
    const errEl = $('#tiktok-error');
    if (!redirect_url) { showErr(errEl, 'Paste the URL you were redirected to.'); input.focus(); return; }
    const btn = ev.currentTarget.querySelector('button[type=submit]');
    setPending(btn, true);
    try {
      await api('POST', '/api/auth/tiktok/complete', { redirect_url });
      showErr(errEl, '');
      show($('#tiktok-auth'), false);
      toast('TikTok connected', 'ok');
      refresh();
    } catch (e) { showErr(errEl, e.message); } finally { setPending(btn, false); }
  }

  // ==== Schedule =======================================================================================================
  function renderSchedule(st) {
    const s = st.scheduler;
    const running = !!s.running;
    const stopping = !running && !!s.stopping; // stop requested, the last tick is still rendering/uploading
    $('#sched-dot').classList.toggle('on', running);
    setText($('#sched-state'), running ? 'running' : stopping ? 'stopping (tick in progress)' : 'stopped');
    const btn = $('#sched-toggle');
    setText(btn, running ? 'Stop scheduler' : 'Start scheduler');
    btn.classList.toggle('btn-primary', !running);
    btn.classList.toggle('btn-danger', running);
    if (!btn._busy) btn.disabled = stopping;
    setText($('#s-tick'), s.tick_s != null ? `every ${s.tick_s} s` : '-');
    setText($('#s-times'), Array.isArray(s.times) && s.times.length ? s.times.join(', ') : 'none');
    setText($('#s-gap'), s.min_gap_h != null ? `${s.min_gap_h} h between posts per platform` : '-');
    const next = s.next_slot || {};
    const configured = s.configured || {};
    const due = s.due || {};
    const nextEl = $('#s-next');
    const nextSig = JSON.stringify([next, configured, due]);
    if (nextEl._sig !== nextSig) {
      nextEl._sig = nextSig;
      nextEl.replaceChildren(...PLATFORMS.map((p) => h('div', {}, `${PLATFORM_LABEL[p]}: `, slotNode(next[p], !!configured[p], !!due[p]))));
    }
    const last = $('#s-last');
    if (s.last_tick) { updateTime(last, s.last_tick); } else setText(last, 'never');
    setText($('#s-summary'), s.last_summary || '-');
  }
  /** next_slot values are 'HH:MM' (the slot the scheduler acts on next; `due` = it has opened and a post is pending now);
   *  a full timestamp is shown with its relative time. */
  function slotNode(slot, configured, due) {
    if (!slot) return h('span', { class: 'muted' }, configured ? 'no slot pending' : 'not configured');
    const isHm = /^\d{1,2}:\d{2}$/.test(String(slot));
    const d = isHm ? null : parseTs(slot);
    const text = d ? `${absTime(slot)} (${relTime(slot)})` : String(slot);
    return h('span', {}, text,
      isHm && due ? h('span', { class: 'muted', title: 'this slot has opened and nothing was posted since: the next tick posts' }, ' (due now)') : null,
      configured ? null : h('span', { class: 'muted' }, ' (not configured)'));
  }
  async function toggleScheduler() {
    const running = !!(S.state && S.state.scheduler && S.state.scheduler.running);
    const btn = $('#sched-toggle');
    setPending(btn, true);
    btn._busy = true;
    try {
      const block = await api('POST', '/api/scheduler', { running: !running });
      if (S.state) { S.state.scheduler = block || S.state.scheduler; render(); }
      if (running) toast(block && block.stopping ? 'scheduler stopping: the tick in progress finishes first' : 'scheduler stopped', 'ok');
      else if (block && block.stopping) toast('the previous tick is still finishing; start the scheduler again when it is done', 'info', 6000);
      else toast('scheduler started', 'ok');
      refresh();
    } catch (e) { toast(e.message, 'error'); } finally { btn._busy = false; btn.disabled = !!(S.state && S.state.scheduler && S.state.scheduler.stopping && !S.state.scheduler.running); }
  }
  function tickItem(x) {
    if (Array.isArray(x)) return x.filter((v) => v !== '' && v != null).join(' \u2192 ');
    if (x && typeof x === 'object') return [x.clip_id, x.platform, x.post_id].filter(Boolean).join(' \u2192 ');
    return String(x);
  }
  function renderTickResult(r) {
    r = r && typeof r === 'object' ? r : {};
    const box = $('#tick-result');
    const list = (title, items, cls) => (items && items.length ? [h('h3', {}, title), h('ul', { class: cls || null }, items.map((x) => h('li', {}, tickItem(x))))] : []);
    box.replaceChildren(
      h('div', { class: 'row' }, pill(r.dry_run ? 'dry run' : 'tick'), h('strong', {}, r.summary || '')),
      ...list(r.dry_run ? 'Would post' : 'Posted', r.posted),
      ...list('Processed videos', r.processed),
      ...list('Skipped', r.skipped),
      ...list('Errors', r.errors, 'bad-list'),
      h('p', { class: 'muted small' }, `finished ${new Date().toLocaleTimeString()}`));
    show(box, true);
  }
  /** Dry run: the report comes back inline. Real tick: the server answers {job_id} (a tick may render for minutes); the
   *  buttons stay disabled until the watched job finishes and its result is rendered. */
  async function runTick(dry) {
    const status = $('#tick-status');
    const buttons = [$('#tick-now'), $('#tick-dry')];
    const setBusy = (on) => buttons.forEach((b) => { b.disabled = on; });
    setBusy(true);
    status.replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }), ` ${dry ? 'dry run' : 'tick'} in progress\u2026`);
    try {
      const r = await api('POST', '/api/tick', { dry_run: !!dry });
      if (dry || !r || r.job_id == null) {
        status.replaceChildren();
        renderTickResult(r);
        setBusy(false);
        refresh();
        loadLogs();
        return;
      }
      toast(`tick started (job ${r.job_id})`, 'ok');
      watchJob(r.job_id, {
        onDone: (job) => { status.replaceChildren(); renderTickResult(job.result); setBusy(false); refresh(); loadLogs(); },
        onFail: (job) => { status.replaceChildren(); toast(job.error || 'tick failed', 'error'); setBusy(false); refresh(); loadLogs(); },
      });
      refresh();
    } catch (e) { status.replaceChildren(); toast(e.message, 'error'); setBusy(false); }
  }
  function startLogs() {
    stopLogs();
    loadLogs();
    S.logsTimer = setInterval(() => { if (!document.hidden) loadLogs(); }, LOGS_MS);
  }
  function stopLogs() {
    if (S.logsTimer) { clearInterval(S.logsTimer); S.logsTimer = null; }
  }
  let logsInflight = false;
  async function loadLogs() {
    if (logsInflight) return;
    logsInflight = true;
    try {
      const r = await api('GET', '/api/logs?limit=200');
      const rows = Array.isArray(r.db) ? r.db : [];
      const list = $('#activity-list');
      show($('#activity-empty'), rows.length === 0);
      list.replaceChildren(...rows.map((e) => h('li', {},
        timeEl(e.ts),
        pill(e.level || 'info'),
        h('span', { class: 'act-action' }, e.action || ''),
        e.detail ? h('span', { class: 'act-detail' }, e.detail) : null)));
      const file = Array.isArray(r.file) ? r.file : [];
      setText($('#activity-file'), file.length ? file.join('\n') : '(log file is empty)');
      setText($('#activity-updated'), `updated ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setText($('#activity-updated'), e.network ? 'server unreachable' : e.message);
    } finally { logsInflight = false; }
  }

  // ==== Settings =======================================================================================================
  async function runDoctor() {
    const btn = $('#doctor-run');
    const status = $('#doctor-status');
    setPending(btn, true);
    status.replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }), ' Running checks\u2026');
    try {
      const rows = await api('GET', '/api/doctor', undefined, { timeoutMs: 90000 });
      const checks = Array.isArray(rows) ? rows : [];
      $('#doctor-body').replaceChildren(...checks.map((c) => h('tr', {},
        h('td', {}, c.name || ''), h('td', {}, pill(c.status || 'INFO')), h('td', { class: 'break' }, c.detail || ''))));
      show($('#doctor-table'), checks.length > 0);
      const fails = checks.filter((c) => c.status === 'FAIL').length;
      const warns = checks.filter((c) => c.status === 'WARN').length;
      setText(status, `${checks.length} checks: ${fails} failed, ${plural(warns, 'warning')}${DOT}${new Date().toLocaleTimeString()}`);
    } catch (e) { setText(status, `environment check failed: ${e.message}`); } finally { setPending(btn, false); }
  }
  async function loadSettingsYaml(force) {
    if (S.settingsDirty && !force) return;
    const ta = $('#settings-yaml');
    try {
      const r = await api('GET', '/api/settings');
      ta.value = r.yaml || '';
      S.settingsLoaded = true;
      S.settingsDirty = false;
      setText($('#settings-dirty'), '');
      setText($('#settings-path'), r.path || '');
      setText($('#settings-note'), r.exists ? 'Edit the YAML and press Save; the server validates it and reloads its settings in place.' : 'The config file does not exist yet - saving creates it with the values below.');
      showErr($('#settings-error'), '');
    } catch (e) { showErr($('#settings-error'), e.message); }
  }
  async function saveSettings() {
    const btn = $('#settings-save');
    setPending(btn, true);
    try {
      const r = await api('PUT', '/api/settings', { yaml: $('#settings-yaml').value });
      showErr($('#settings-error'), '');
      S.settingsDirty = false;
      setText($('#settings-dirty'), 'saved');
      if (r && r.path) setText($('#settings-path'), r.path);
      toast('settings saved and reloaded', 'ok');
      S.defaultsApplied = false; // pick up new defaults for the add form
      refresh();
    } catch (e) { showErr($('#settings-error'), e.message); } finally { setPending(btn, false); }
  }
  async function reloadSettings() {
    if (S.settingsDirty && !(await confirmDialog('Discard your unsaved edits and reload the file from disk?', 'Reload'))) return;
    await loadSettingsYaml(true);
    toast('reloaded from disk');
  }

  // ==== wiring =========================================================================================================
  function showFatal(message) {
    let box = document.getElementById('fatal');
    if (!box) {
      box = h('div', { id: 'fatal', class: 'fatal', role: 'alert' },
        h('span', {}, ''),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: () => location.reload() }, 'Reload'),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: () => box.remove() }, 'Dismiss'));
      document.body.appendChild(box);
    }
    box.firstChild.textContent = `Something went wrong: ${message}`;
  }
  window.addEventListener('error', (ev) => showFatal((ev.error && ev.error.message) || ev.message || 'unknown error'));
  window.addEventListener('unhandledrejection', (ev) => {
    const r = ev.reason;
    if (r && r.network) return; // already surfaced as a toast / offline banner by the caller
    showFatal((r && r.message) || String(r));
  });

  function init() {
    initTabs();
    $('#add-form').addEventListener('submit', addVideo);
    $('#run-queue').addEventListener('click', runQueue);
    $('#approve-all').addEventListener('click', approveAllRendered);
    for (const [id, key] of [['#review-video', 'video'], ['#review-status', 'status'], ['#review-sort', 'sort']]) {
      const sel = $(id);
      if (key !== 'video') sel.value = S.review[key];
      sel.addEventListener('change', () => { S.review[key] = sel.value; render(); if (key === 'video') refresh(); });
    }
    $('#select-all').addEventListener('change', (ev) => {
      if (!S.state) return;
      S.selected = ev.target.checked ? new Set(publishable(S.state.clips).map((c) => c.id)) : new Set();
      render();
    });
    for (const p of PLATFORMS) $(`#pub-${p}`).addEventListener('change', () => { if (S.state) render(); });
    $('#publish-selected').addEventListener('click', publishSelected);
    $('#connect-youtube').addEventListener('click', connectYouTube);
    $('#connect-tiktok').addEventListener('click', connectTikTok);
    $('#tiktok-complete-form').addEventListener('submit', completeTikTok);
    $('#tiktok-posting').addEventListener('submit', saveTikTokPosting);
    $('#tiktok-posting').addEventListener('input', () => { S.ttDirty = true; show($('#tt-disclosure'), $('#tt-commercial').checked); });
    $('#tiktok-copy').addEventListener('click', async () => {
      const ok = await copyText($('#tiktok-auth-url').href);
      toast(ok ? 'link copied' : 'copy failed - select the link text and copy it by hand', ok ? 'ok' : 'error');
    });
    $('#manual-close').addEventListener('click', () => { closeDialog($('#manual-dialog')); S.manual = null; });
    $('#manual-form').addEventListener('submit', markPosted);
    $('#manual-copy').addEventListener('click', async () => {
      const ta = $('#manual-caption');
      const ok = await copyText(ta.value, ta);
      toast(ok ? 'caption copied' : 'copy failed - select the caption and copy it by hand', ok ? 'ok' : 'error');
    });
    $('#manual-dialog').addEventListener('close', () => { S.manual = null; });
    $('#sched-toggle').addEventListener('click', toggleScheduler);
    $('#tick-now').addEventListener('click', () => runTick(false));
    $('#tick-dry').addEventListener('click', () => runTick(true));
    $('#doctor-run').addEventListener('click', runDoctor);
    $('#settings-save').addEventListener('click', saveSettings);
    $('#settings-reload').addEventListener('click', reloadSettings);
    $('#settings-yaml').addEventListener('input', () => { S.settingsDirty = true; setText($('#settings-dirty'), 'unsaved changes'); });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(S.pollTimer); S.pollTimer = null; return; }
      poll();
      if (S.tab === 'schedule') loadLogs();
    });
    window.addEventListener('online', () => poll());
    showTab(location.hash.slice(1) || 'dashboard', true);
    poll();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
