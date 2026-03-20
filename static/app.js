/* app.js — Manga Collection frontend */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentView   = 'all';
let currentLayout = 'grid';
let currentBook   = null;
let searchTimer   = null;
let currentTab    = 'status';

const VIEW_FILTERS = {
  all:     {},
  manga:   { type: 'cbz' },
  comics:  { type: 'cbz' },   // future: distinguish by tag
  ebooks:  { type: 'epub' },
  reading: { status: 'reading' },
  unread:  { status: 'unread' },
  read:    { status: 'read' },
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  loadBooks();
});

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
function setView(view) {
  currentView = view;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === view);
  });
  document.getElementById('search-input').value = '';
  loadBooks();
}

function switchLayout(layout) {
  currentLayout = layout;
  document.getElementById('book-grid').style.display = layout === 'grid' ? 'grid' : 'none';
  document.getElementById('book-list').style.display = layout === 'list' ? 'flex'  : 'none';
  document.getElementById('vbtn-grid').classList.toggle('active', layout === 'grid');
  document.getElementById('vbtn-list').classList.toggle('active', layout === 'list');
  renderBooks(window._lastBooks || []);
}

// ---------------------------------------------------------------------------
// Load books
// ---------------------------------------------------------------------------
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadBooks, 280);
}

async function loadBooks() {
  const q    = document.getElementById('search-input').value.trim();
  const type = document.getElementById('filter-type').value;
  const sort = document.getElementById('filter-sort').value;

  const extra = VIEW_FILTERS[currentView] || {};
  const params = new URLSearchParams({
    sort, order: 'asc', limit: 200, offset: 0, ...extra,
  });
  if (q)    params.set('q', q);
  if (type) params.set('type', type);

  try {
    const books = await api(`/books?${params}`);
    window._lastBooks = books;
    document.getElementById('result-count').textContent =
      `${books.length} book${books.length !== 1 ? 's' : ''}`;
    renderBooks(books);
  } catch (e) {
    toast('Failed to load books', 'err');
  }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function renderBooks(books) {
  if (currentLayout === 'grid') renderGrid(books);
  else                          renderList(books);
}

function renderGrid(books) {
  const el = document.getElementById('book-grid');
  if (!books.length) { el.innerHTML = emptyState(); return; }
  el.innerHTML = books.map(b => `
    <div class="book-card" onclick="openModal(${b.id})">
      <div class="status-dot ${b.status || 'unread'}"></div>
      ${b.cover_path
        ? `<img class="book-cover" src="/books/${b.id}/cover" loading="lazy" alt="">`
        : `<div class="book-cover-placeholder">${typeIcon(b.type)}</div>`}
      <div class="book-info">
        <div class="book-title">${esc(b.title)}</div>
        ${b.series
          ? `<div class="book-series">${esc(b.series)}${b.volume != null ? ` T${String(b.volume).padStart(2,'0')}` : ''}</div>`
          : ''}
      </div>
    </div>`).join('');
}

function renderList(books) {
  const el = document.getElementById('book-list');
  if (!books.length) { el.innerHTML = emptyState(); return; }
  el.innerHTML = books.map(b => `
    <div class="book-row" onclick="openModal(${b.id})">
      ${b.cover_path
        ? `<img class="book-row-thumb" src="/books/${b.id}/cover" loading="lazy" alt="">`
        : `<div class="book-row-thumb-placeholder">${typeIcon(b.type)}</div>`}
      <div class="book-row-main">
        <div class="book-row-title">${esc(b.title)}</div>
        <div class="book-row-sub">
          ${b.series ? esc(b.series) + ' &nbsp;·&nbsp; ' : ''}
          ${b.tags && b.tags.length ? b.tags.map(t => `<span class="tag-pill">${esc(t)}</span>`).join(' ') : ''}
        </div>
      </div>
      <div class="book-row-meta">
        <span class="type-badge type-${b.type}">${b.type.toUpperCase()}</span>
        <span class="status-badge ${b.status || 'unread'}">${b.status || 'unread'}</span>
      </div>
    </div>`).join('');
}

function emptyState() {
  return `<div class="empty"><div class="empty-icon">📭</div>
    <div class="empty-text">No books found</div></div>`;
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------
async function loadStats() {
  try {
    const s = await api('/books/stats/summary');
    document.getElementById('stat-total').textContent   = s.total;
    document.getElementById('stat-reading').textContent = s.by_status?.reading || 0;
    document.getElementById('stat-read').textContent    = s.by_status?.read    || 0;
    document.getElementById('stat-unread').textContent  = s.by_status?.unread  || 0;
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Scan
// ---------------------------------------------------------------------------
async function triggerScan() {
  const btn = document.querySelector('.scan-btn');
  btn.textContent = '⟳ Scanning…';
  btn.disabled = true;
  try {
    const r = await api('/scan', { method: 'POST' });
    toast(`Scan done: +${r.added} added, ${r.removed} removed${r.errors.length ? `, ${r.errors.length} errors` : ''}`, 'ok');
    loadBooks();
    loadStats();
  } catch (e) {
    toast('Scan failed', 'err');
  } finally {
    btn.textContent = '⟳ Scan library';
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------
async function openModal(bookId) {
  try {
    const b = await api(`/books/${bookId}`);
    currentBook = b;
    currentTab = 'status';

    // Header
    const coverWrap = document.getElementById('modal-cover-wrap');
    coverWrap.innerHTML = b.cover_path
      ? `<img class="modal-cover" src="/books/${b.id}/cover" alt="">`
      : `<div class="modal-cover-placeholder">${typeIcon(b.type)}</div>`;

    document.getElementById('modal-title').textContent  = b.title;
    document.getElementById('modal-series').textContent =
      b.series ? `${b.series}${b.volume != null ? ` — Volume ${b.volume}` : ''}` : '';
    document.getElementById('modal-path').textContent   = b.path;

    const badges = document.getElementById('modal-badges');
    badges.innerHTML = `
      <span class="type-badge type-${b.type}">${b.type.toUpperCase()}</span>
      <span class="status-badge ${b.status || 'unread'}">${b.status || 'unread'}</span>
      ${b.file_size ? `<span class="tag-pill">${fmtSize(b.file_size)}</span>` : ''}
    `;

    // Show/hide standardize tab based on type
    const stdTab = document.querySelector('.modal-tab:last-child');
    stdTab.style.display = ['cbz', 'cbr'].includes(b.type) ? '' : 'none';

    // Reset tabs
    document.querySelectorAll('.modal-tab').forEach((t, i) => t.classList.toggle('active', i === 0));
    document.querySelectorAll('.modal-panel').forEach((p, i) => p.classList.toggle('active', i === 0));

    // Status tab
    renderStatusOpts(b.status || 'unread');
    document.getElementById('progress-input').value = b.progress || '';

    // Tags tab
    renderTags(b.tags || []);

    // Metadata tab
    document.getElementById('meta-results').innerHTML = '';
    document.getElementById('meta-query').value = b.series || b.title;
    loadMetadata(b.id);

    // Standardize tab
    document.getElementById('std-log').textContent = 'Ready.';

    document.getElementById('modal-overlay').classList.add('open');
  } catch (e) {
    toast('Failed to load book details', 'err');
  }
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  currentBook = null;
}

function closeModalOnBg(e) {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
function switchTab(name) {
  currentTab = name;
  const tabs = [...document.querySelectorAll('.modal-tab')];
  const panels = [...document.querySelectorAll('.modal-panel')];
  const names = ['status', 'tags', 'metadata', 'standardize'];
  const idx = names.indexOf(name);
  tabs.forEach((t, i) => t.classList.toggle('active', i === idx));
  panels.forEach((p, i) => p.classList.toggle('active', i === idx));
}

// ---------------------------------------------------------------------------
// Reading status
// ---------------------------------------------------------------------------
function renderStatusOpts(current) {
  const opts = document.getElementById('status-opts');
  opts.innerHTML = ['unread', 'reading', 'read'].map(s => `
    <div class="status-opt ${current === s ? 'sel-' + s : ''}"
      onclick="selectStatus('${s}')">${s}</div>`).join('');
}

function selectStatus(status) {
  document.querySelectorAll('.status-opt').forEach(el => {
    const s = el.textContent.trim();
    el.className = `status-opt ${s === status ? 'sel-' + s : ''}`;
  });
}

async function saveStatus() {
  if (!currentBook) return;
  const selected = document.querySelector('.status-opt[class*="sel-"]');
  if (!selected) return;
  const status   = selected.textContent.trim();
  const progress = parseInt(document.getElementById('progress-input').value) || 0;
  try {
    await api(`/books/${currentBook.id}/status`, {
      method: 'PUT', body: { status, progress },
    });
    currentBook.status = status;
    toast('Status saved', 'ok');
    loadBooks();
    loadStats();
  } catch {
    toast('Failed to save status', 'err');
  }
}

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------
function renderTags(tags) {
  const el = document.getElementById('tag-editor');
  el.innerHTML = tags.length
    ? tags.map(t => `
        <span class="tag-pill-rm">${esc(t)}
          <button onclick="removeTag('${esc(t)}')" title="Remove">✕</button>
        </span>`).join('')
    : '<span style="color:var(--muted);font-size:.8rem">No tags yet</span>';
}

async function addTag() {
  if (!currentBook) return;
  const input = document.getElementById('tag-input');
  const tag = input.value.trim().toLowerCase();
  if (!tag) return;
  try {
    await api(`/books/${currentBook.id}/tags/${encodeURIComponent(tag)}`, { method: 'POST' });
    if (!currentBook.tags.includes(tag)) currentBook.tags.push(tag);
    renderTags(currentBook.tags);
    input.value = '';
    toast(`Tag "${tag}" added`, 'ok');
  } catch {
    toast('Failed to add tag', 'err');
  }
}

async function removeTag(tag) {
  if (!currentBook) return;
  try {
    await api(`/books/${currentBook.id}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' });
    currentBook.tags = currentBook.tags.filter(t => t !== tag);
    renderTags(currentBook.tags);
  } catch {
    toast('Failed to remove tag', 'err');
  }
}

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------
async function loadMetadata(bookId) {
  try {
    const rows = await api(`/books/${bookId}/metadata`);
    renderMeta(rows);
  } catch (_) {}
}

function renderMeta(rows) {
  const el = document.getElementById('meta-results');
  if (!rows.length) {
    el.innerHTML = '<p style="color:var(--muted);font-size:.82rem">No metadata fetched yet.</p>';
    return;
  }
  el.innerHTML = rows.map(m => `
    <div class="meta-source">
      <div class="meta-source-header">
        <span class="meta-source-name">${m.source}</span>
        <span style="font-size:.72rem;color:var(--muted)">${m.fetched_at?.slice(0,10) || ''}</span>
      </div>
      ${m.synopsis ? `<div class="meta-synopsis">${esc(m.synopsis.slice(0, 300))}${m.synopsis.length > 300 ? '…' : ''}</div>` : ''}
      ${m.authors?.length  ? `<div class="meta-field"><span>Authors:</span> ${m.authors.join(', ')}</div>`  : ''}
      ${m.genres?.length   ? `<div class="meta-field"><span>Genres:</span> ${m.genres.join(', ')}</div>`   : ''}
      ${m.publisher        ? `<div class="meta-field"><span>Publisher:</span> ${esc(m.publisher)}</div>`   : ''}
      ${m.year             ? `<div class="meta-field"><span>Year:</span> ${m.year}</div>`                  : ''}
      ${m.score            ? `<div class="meta-field"><span>Score:</span> ${m.score}/10</div>`             : ''}
    </div>`).join('');
}

async function fetchMeta() {
  if (!currentBook) return;
  const source = document.getElementById('meta-source').value;
  const query  = document.getElementById('meta-query').value.trim();
  if (!query) return;

  document.getElementById('meta-results').innerHTML =
    '<p style="color:var(--muted);font-size:.82rem">Fetching…</p>';
  try {
    await api('/metadata/fetch', {
      method: 'POST',
      body: { book_id: currentBook.id, source, query },
    });
    const rows = await api(`/books/${currentBook.id}/metadata`);
    renderMeta(rows);
    toast('Metadata fetched', 'ok');
  } catch (e) {
    toast(`Fetch failed: ${e.message}`, 'err');
    document.getElementById('meta-results').innerHTML =
      `<p style="color:var(--red);font-size:.82rem">${esc(e.message)}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Standardize
// ---------------------------------------------------------------------------
async function runStandardize() {
  if (!currentBook) return;
  const log  = document.getElementById('std-log');
  const btn  = document.getElementById('btn-std');
  const webp = document.getElementById('std-webp').checked;
  const qual = parseInt(document.getElementById('std-quality').value) || 85;

  log.textContent = 'Starting…\n';
  btn.disabled = true;

  const es = new EventSource(`/books/${currentBook.id}/standardize?webp=${webp}&webp_quality=${qual}`);

  // We need POST — use fetch + SSE manually via ReadableStream
  es.close();

  const resp = await fetch(`/books/${currentBook.id}/standardize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ webp, webp_quality: qual }),
  });

  if (!resp.body) { log.textContent += 'Streaming not supported.\n'; btn.disabled = false; return; }

  const reader = resp.body.getReader();
  const dec    = new TextDecoder();
  let buf = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop();
    for (const part of parts) {
      const dataLine = part.split('\n').find(l => l.startsWith('data:'));
      const evtLine  = part.split('\n').find(l => l.startsWith('event:'));
      if (!dataLine) continue;
      const data = dataLine.slice(5).trim();
      const evt  = evtLine ? evtLine.slice(6).trim() : 'log';

      if (evt === 'log') {
        log.textContent += data + '\n';
        log.scrollTop = log.scrollHeight;
      } else if (evt === 'done') {
        log.textContent += `\n✓ Done: ${data}\n`;
        toast('Standardization complete', 'ok');
        loadBooks();
        // Refresh cover
        document.getElementById('modal-cover-wrap').innerHTML =
          `<img class="modal-cover" src="/books/${currentBook.id}/cover?t=${Date.now()}" alt="">`;
      } else if (evt === 'error') {
        log.textContent += `\n✗ Error: ${data}\n`;
        toast(`Error: ${data}`, 'err');
      }
    }
  }
  btn.disabled = false;
}

// ---------------------------------------------------------------------------
// Delete
// ---------------------------------------------------------------------------
async function deleteBook() {
  if (!currentBook || !confirm(`Delete "${currentBook.title}" from the collection?`)) return;
  try {
    await api(`/books/${currentBook.id}`, { method: 'DELETE' });
    closeModal();
    loadBooks();
    loadStats();
    toast('Book removed from collection', 'ok');
  } catch {
    toast('Failed to delete', 'err');
  }
}

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const { method = 'GET', body } = opts;
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function typeIcon(type) {
  return { cbz: '📗', cbr: '📘', epub: '📕', pdf: '📄', mobi: '📙', azw3: '📙' }[type] || '📚';
}

function fmtSize(bytes) {
  if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
  if (bytes > 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
  return (bytes / 1e3).toFixed(0) + ' KB';
}

function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

let _toastTimer;
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}
