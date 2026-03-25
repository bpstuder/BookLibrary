/* app.js — Manga Collection */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentView      = 'all';
let currentLayout    = localStorage.getItem('layout') || 'grid';
let currentSortField = 'title';
let currentSortOrder = 'asc';
let currentBook      = null;
let selectMode       = false;          // library selection mode
let libSelection     = new Set();      // selected book IDs in library view
let searchTimer      = null;
let fbCurrentPath    = '';
let selectedMetaId   = null;
let _lastFetchSource = null;   // track which source was last fetched

// Table columns visibility — stored in localStorage
const ALL_COLUMNS = ['cover','title','series','volume','authors','format','category','status','size'];
let visibleColumns = JSON.parse(localStorage.getItem('tbl_cols') || 'null')
  || ['cover','title','series','volume','format','category','status'];

const VIEW_FILTERS = {
  all:     {},
  manga:   { category: 'manga' },
  comics:  { category: 'comics' },
  books:   { category: 'book' },
  reading: { status: 'reading' },
  unread:  { status: 'unread' },
  read:    { status: 'read' },
  series:  null,
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  loadBooks();
  // Apply saved layout
  _applyLayout(currentLayout, false);
});

// ---------------------------------------------------------------------------
// Page switching
// ---------------------------------------------------------------------------
function showSettings() {
  document.getElementById('library-page').classList.add('hidden');
  document.getElementById('settings-page').classList.add('active');
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  loadSettingsPage();
}
function showLibrary() {
  document.getElementById('settings-page').classList.remove('active');
  document.getElementById('batch-page').classList.remove('active');
  document.getElementById('library-page').classList.remove('hidden');
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
function setView(view) {
  currentView = view;
  showLibrary();
  document.querySelectorAll('.nav-item').forEach(el =>
    el.classList.toggle('active', el.dataset.view === view));
  document.getElementById('search-input').value = '';

  if (view === 'series') { _updateLayoutButtons(); loadSeriesView(); }
  else { _updateLayoutButtons(); loadBooks(); }
}

// ---------------------------------------------------------------------------
// Layout — FIX: persist + don't clobber series view
// ---------------------------------------------------------------------------
function switchLayout(layout) {
  currentLayout = layout;
  localStorage.setItem('layout', layout);
  if (currentView === 'series') {
    // In series view, layout buttons are disabled — just keep the active state
    // but don't trigger any re-render (series always shows its own card grid)
    ['grid','list','table'].forEach(l =>
      document.getElementById('vbtn-'+l)?.classList.toggle('active', l === layout));
    return;
  }
  _applyLayout(layout, true);
}

function _updateLayoutButtons() {
  const inSeries = currentView === 'series';
  ['grid','list','table'].forEach(l => {
    const btn = document.getElementById('vbtn-'+l);
    if (!btn) return;
    btn.classList.toggle('active', l === currentLayout);
    btn.style.opacity = inSeries ? '0.35' : '';
    btn.style.pointerEvents = inSeries ? 'none' : '';
    btn.title = inSeries ? 'Layout not applicable in series view' : '';
  });
}

function _applyLayout(layout, rerender) {
  if (currentView === 'series') {
    // Series view always shows series-grid regardless of layout preference
    ['book-grid','book-list','book-table-wrap'].forEach(id =>
      document.getElementById(id).style.display = 'none');
    document.getElementById('series-grid').style.display = 'grid';
    ['grid','list','table'].forEach(l =>
      document.getElementById('vbtn-'+l)?.classList.toggle('active', l === layout));
    return;
  }
  const ids = { grid:'book-grid', list:'book-list', table:'book-table-wrap' };
  Object.entries(ids).forEach(([k, id]) =>
    document.getElementById(id).style.display = k === layout ? '' : 'none');
  document.getElementById('series-grid').style.display = 'none';
  _updateLayoutButtons();
  if (rerender) renderBooks(window._lastBooks || []);
}

// ---------------------------------------------------------------------------
// Sorting — FIX: decouple from filter-sort dropdown
// ---------------------------------------------------------------------------
function sortBy(field) {
  if (currentSortField === field) {
    currentSortOrder = currentSortOrder === 'asc' ? 'desc' : 'asc';
  } else {
    currentSortField = field;
    currentSortOrder = 'asc';
  }
  // Update dropdown to match
  const sel = document.getElementById('filter-sort');
  if (sel && [...sel.options].some(o => o.value === field)) sel.value = field;
  _updateSortHeaders();
  loadBooks();
}

function _updateSortHeaders() {
  document.querySelectorAll('#book-table th[data-sort]').forEach(th => {
    const f = th.dataset.sort;
    const arrow = f === currentSortField ? (currentSortOrder === 'asc' ? ' ▲' : ' ▼') : '';
    th.textContent = th.dataset.label + arrow;
  });
}

// ---------------------------------------------------------------------------
// Load books — FIX: always use currentSortField, not the dropdown alone
// ---------------------------------------------------------------------------
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadBooks, 280);
}

async function loadBooks() {
  if (currentView === 'series') { loadSeriesView(); return; }

  const q      = document.getElementById('search-input').value.trim();
  const type   = document.getElementById('filter-type').value;
  // Sync sort from dropdown if user changed it there
  const selSort = document.getElementById('filter-sort').value;
  if (selSort) currentSortField = selSort;

  const extra  = VIEW_FILTERS[currentView] || {};
  const params = new URLSearchParams({
    sort: currentSortField, order: currentSortOrder, limit: 500, offset: 0, ...extra,
  });
  if (q)    params.set('q', q);
  if (type) params.set('type', type);

  try {
    const books = await api(`/books?${params}`);
    window._lastBooks = books;
    document.getElementById('result-count').textContent =
      `${books.length} book${books.length !== 1 ? 's' : ''}`;
    renderBooks(books);
    _updateSortHeaders();
  } catch { toast('Failed to load books', 'err'); }
}

// ---------------------------------------------------------------------------
// Series view — FIX: respect currentLayout
// ---------------------------------------------------------------------------
async function loadSeriesView() {
  // Hide book containers, show series grid
  ['book-grid','book-list','book-table-wrap'].forEach(id =>
    document.getElementById(id).style.display = 'none');
  const sg = document.getElementById('series-grid');
  sg.style.display = 'grid';
  sg.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div></div>';

  try {
    const series = await api('/books/series');
    document.getElementById('result-count').textContent = `${series.length} series`;
    if (!series.length) { sg.innerHTML = emptyState(); return; }

    // Store series names in a lookup to avoid quote-escaping bugs in onclick attributes
    window._seriesIndex = series.map(s => s.series);

    sg.innerHTML = series.map((s, i) => {
      const total = s.count;
      const r = s.statuses?.read    || 0;
      const p = s.statuses?.reading || 0;
      const pctR = total ? Math.round(r/total*100) : 0;
      const pctP = total ? Math.round(p/total*100) : 0;
      const pctU = 100 - pctR - pctP;
      return `
        <div class="series-card" data-series-idx="${i}" onclick="filterBySeriesIdx(this)">
          <div class="series-name">${esc(s.series)}</div>
          <div class="series-meta">${s.count} vol. &nbsp;·&nbsp;
            <span class="cat-badge cat-${s.category}">${s.category}</span></div>
          <div class="series-bar">
            ${pctR ? `<div class="series-bar-r" style="flex:${pctR}"></div>` : ''}
            ${pctP ? `<div class="series-bar-p" style="flex:${pctP}"></div>` : ''}
            ${pctU ? `<div class="series-bar-u" style="flex:${pctU}"></div>` : ''}
          </div>
        </div>`;
    }).join('');
  } catch { sg.innerHTML = emptyState(); toast('Failed to load series', 'err'); }
}

function filterBySeries(series) {
  currentView = 'all';
  document.querySelectorAll('.nav-item').forEach(el =>
    el.classList.toggle('active', el.dataset.view === 'all'));
  document.getElementById('search-input').value = series;
  _applyLayout(currentLayout, false);
  loadBooks();
}

function filterBySeriesIdx(el) {
  const idx    = parseInt(el.dataset.seriesIdx, 10);
  const series = (window._seriesIndex || [])[idx];
  if (series !== undefined) filterBySeries(series);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function renderBooks(books) {
  if (currentView === 'series') { loadSeriesView(); return; }
  if (currentLayout === 'grid')  renderGrid(books);
  else if (currentLayout === 'list')  renderList(books);
  else renderTable(books);
}

function _showOnly(activeId) {
  ['book-grid','book-list','book-table-wrap','series-grid'].forEach(id =>
    document.getElementById(id).style.display = id === activeId ? '' : 'none');
  if (activeId === 'book-grid') document.getElementById('book-grid').style.display = 'grid';
  if (activeId === 'book-list') document.getElementById('book-list').style.display = 'flex';
  if (activeId === 'series-grid') document.getElementById('series-grid').style.display = 'grid';
}

function renderGrid(books) {
  _showOnly('book-grid');
  const el = document.getElementById('book-grid');
  if (!books.length) { el.innerHTML = emptyState(); return; }
  el.innerHTML = books.map(b => {
    const checked = libSelection.has(b.id);
    return `
    <div class="book-card ${checked ? 'sel-checked' : ''}" id="bc-${b.id}"
         onclick="${selectMode ? `toggleLibSel(${b.id})` : `openModal(${b.id})`}">
      <input type="checkbox" class="book-card-check" ${checked ? 'checked' : ''}
             onclick="event.stopPropagation();toggleLibSel(${b.id})"
             title="Select">
      <div class="status-dot ${b.status||'unread'}"></div>
      ${b.cover_path
        ? `<img class="book-cover" src="/books/${b.id}/cover" loading="lazy" alt="">`
        : `<div class="book-cover-ph">${typeIcon(b.type)}</div>`}
      <div class="book-info">
        <div class="book-title">${esc(b.title)}</div>
        ${b.series ? `<div class="book-series">${esc(b.series)}${b.volume!=null?` T${String(b.volume).padStart(2,'0')}`:''}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

function renderList(books) {
  _showOnly('book-list');
  const el = document.getElementById('book-list');
  if (!books.length) { el.innerHTML = emptyState(); return; }
  el.innerHTML = books.map(b => {
    const checked = libSelection.has(b.id);
    return `
    <div class="book-row ${checked ? 'sel-checked' : ''}" id="br-${b.id}"
         onclick="${selectMode ? `toggleLibSel(${b.id})` : `openModal(${b.id})`}"
         style="${checked ? 'background:rgba(123,97,255,.08);border-color:var(--accent)' : ''}">
      <input type="checkbox" class="row-check" ${checked ? 'checked' : ''}
             onclick="event.stopPropagation();toggleLibSel(${b.id})">
      ${b.cover_path
        ? `<img class="row-thumb" src="/books/${b.id}/cover" loading="lazy" alt="">`
        : `<div class="row-thumb-ph">${typeIcon(b.type)}</div>`}
      <div class="row-main">
        <div class="row-title">${esc(b.title)}</div>
        <div class="row-sub">
          ${b.series ? esc(b.series)+(b.volume!=null?` T${String(b.volume).padStart(2,'0')}`:'')+'&nbsp;·&nbsp;' : ''}
          ${(b.tags||[]).map(t=>`<span class="tag-pill">${esc(t)}</span>`).join(' ')}
        </div>
      </div>
      <div class="row-meta">
        <span class="type-badge type-${b.type}">${b.type.toUpperCase()}</span>
        <span class="cat-badge cat-${b.category||'unknown'}">${b.category||'?'}</span>
        <span class="status-badge ${b.status||'unread'}">${b.status||'unread'}</span>
      </div>
    </div>`;
  }).join('');
}

function renderTable(books) {
  _showOnly('book-table-wrap');
  _rebuildTableHeader();
  const tbody = document.getElementById('book-tbody');
  if (!books.length) {
    tbody.innerHTML = `<tr><td colspan="${visibleColumns.length + 2}">${emptyState()}</td></tr>`;
    return;
  }
  tbody.innerHTML = books.map(b => {
    const checked = libSelection.has(b.id);
    const cells = visibleColumns.map(col => {
      switch (col) {
        case 'cover':
          return `<td>${b.cover_path
            ? `<img class="tbl-thumb" src="/books/${b.id}/cover" loading="lazy" alt="">`
            : `<div class="tbl-thumb-ph">${typeIcon(b.type)}</div>`}</td>`;
        case 'title':    return `<td><strong>${esc(b.title)}</strong></td>`;
        case 'series':   return `<td>${b.series ? esc(b.series) : '<span style="color:var(--muted)">—</span>'}</td>`;
        case 'volume':   return `<td style="text-align:center">${b.volume!=null ? String(b.volume).padStart(2,'0') : '—'}</td>`;
        case 'authors':  return `<td style="color:var(--muted);font-size:.78rem">${esc((b.authors||[]).join(', ') || '—')}</td>`;
        case 'format':   return `<td><span class="type-badge type-${b.type}">${b.type.toUpperCase()}</span></td>`;
        case 'category': return `<td><span class="cat-badge cat-${b.category||'unknown'}">${b.category||'?'}</span></td>`;
        case 'status':   return `<td><span class="status-badge ${b.status||'unread'}">${b.status||'unread'}</span></td>`;
        case 'size':     return `<td style="color:var(--muted);font-size:.78rem">${b.file_size ? fmtSize(b.file_size) : '—'}</td>`;
        default: return '<td></td>';
      }
    }).join('');
    return `<tr onclick="${selectMode ? `toggleLibSel(${b.id})` : `openModal(${b.id})`}"
               id="tr-${b.id}" style="${checked ? 'background:rgba(123,97,255,.08)' : ''}">
      <td onclick="event.stopPropagation()">
        <input type="checkbox" ${checked ? 'checked' : ''} style="accent-color:var(--accent)"
               onclick="toggleLibSel(${b.id})">
      </td>
      ${cells}
    </tr>`;
  }).join('');
}

function _rebuildTableHeader() {
  const LABELS   = { cover:'', title:'Title', series:'Series', volume:'Vol.',
    authors:'Authors', format:'Format', category:'Category', status:'Status', size:'Size' };
  const SORTABLE = ['title','series','volume','category','status'];
  const allChecked = window._lastBooks?.length > 0 &&
    window._lastBooks.every(b => libSelection.has(b.id));

  const thead = document.querySelector('#book-table thead tr');
  // Checkbox header col + data cols + settings col
  thead.innerHTML =
    `<th style="width:28px">
       <input type="checkbox" ${allChecked ? 'checked' : ''} style="accent-color:var(--accent)"
              onclick="libToggleAll(this.checked)" title="Select all">
     </th>` +
    visibleColumns.map(col => {
      const label    = LABELS[col] || col;
      const sortable = SORTABLE.includes(col);
      const arrow    = col === currentSortField ? (currentSortOrder === 'asc' ? ' ▲' : ' ▼') : '';
      return sortable
        ? `<th data-sort="${col}" data-label="${label}" onclick="sortBy('${col}')"
               style="cursor:pointer;user-select:none">${label}${arrow}</th>`
        : `<th>${label}</th>`;
    }).join('') +
    `<th style="width:28px;text-align:right">
       <button onclick="toggleColMenu()" title="Columns" style="background:none;border:none;
         cursor:pointer;color:var(--muted);font-size:.9rem;padding:0">⚙</button>
     </th>`;
}

function toggleColMenu() {
  let menu = document.getElementById('col-menu');
  if (menu) { menu.remove(); return; }

  menu = document.createElement('div');
  menu.id = 'col-menu';
  menu.style.cssText = `position:fixed;background:var(--surface);border:1px solid var(--border);
    border-radius:9px;padding:.6rem .8rem;z-index:150;box-shadow:0 4px 24px rgba(0,0,0,.4);
    min-width:160px;font-size:.82rem;`;

  const LABELS = { cover:'Cover', title:'Title', series:'Series', volume:'Volume',
    authors:'Authors', format:'Format', category:'Category', status:'Status', size:'File size' };

  menu.innerHTML = `<div style="font-weight:700;font-size:.75rem;color:var(--muted);
      text-transform:uppercase;letter-spacing:.07em;margin-bottom:.5rem">Columns</div>`
    + ALL_COLUMNS.map(col =>
      `<label style="display:flex;align-items:center;gap:.5rem;padding:.2rem 0;cursor:pointer">
        <input type="checkbox" ${visibleColumns.includes(col)?'checked':''} value="${col}"
          onchange="toggleColumn('${col}',this.checked)" style="accent-color:var(--accent)">
        ${LABELS[col]||col}
      </label>`
    ).join('')
    + `<div style="margin-top:.5rem;border-top:1px solid var(--border);padding-top:.5rem">
        <button onclick="document.getElementById('col-menu')?.remove()"
          style="background:none;border:none;cursor:pointer;color:var(--muted);font-size:.8rem">Close</button>
      </div>`;

  // Position near the column header button
  const btn = document.querySelector('#book-table thead th:last-child button');
  if (btn) {
    const r = btn.getBoundingClientRect();
    menu.style.top  = (r.bottom + 6) + 'px';
    menu.style.right = (window.innerWidth - r.right) + 'px';
  } else {
    menu.style.top = '100px'; menu.style.right = '20px';
  }

  document.body.appendChild(menu);
  // Close on outside click
  setTimeout(() => document.addEventListener('click', function _close(e) {
    if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', _close); }
  }), 50);
}

function toggleColumn(col, checked) {
  if (checked && !visibleColumns.includes(col)) visibleColumns.push(col);
  else if (!checked) visibleColumns = visibleColumns.filter(c => c !== col);
  // Preserve order from ALL_COLUMNS
  visibleColumns = ALL_COLUMNS.filter(c => visibleColumns.includes(c));
  localStorage.setItem('tbl_cols', JSON.stringify(visibleColumns));
  renderTable(window._lastBooks || []);
}

function emptyState() {
  return `<div class="empty"><div class="empty-icon">📭</div><div>No books found</div></div>`;
}

// ---------------------------------------------------------------------------
// Library selection mode
// ---------------------------------------------------------------------------

function toggleSelectMode() {
  selectMode = !selectMode;
  if (!selectMode) libSelection.clear();

  const btn  = document.getElementById('vbtn-sel');
  const bar  = document.getElementById('select-bar');
  const page = document.getElementById('library-page');

  btn?.classList.toggle('active', selectMode);
  page?.classList.toggle('sel-mode', selectMode);

  if (selectMode) {
    bar?.classList.add('visible');
  } else {
    bar?.classList.remove('visible');
  }
  _updateSelectBar();
  renderBooks(window._lastBooks || []);
}

function toggleLibSel(id) {
  if (libSelection.has(id)) {
    libSelection.delete(id);
  } else {
    libSelection.add(id);
    if (!selectMode) {
      // Auto-enter select mode on first check
      selectMode = true;
      document.getElementById('vbtn-sel')?.classList.add('active');
      document.getElementById('library-page')?.classList.add('sel-mode');
      document.getElementById('select-bar')?.classList.add('visible');
    }
  }
  // Update visual state without full re-render
  _updateCardCheck(id);
  _updateSelectBar();
}

function _updateCardCheck(id) {
  const checked = libSelection.has(id);
  // Grid card
  const card = document.getElementById('bc-' + id);
  if (card) {
    card.classList.toggle('sel-checked', checked);
    const cb = card.querySelector('.book-card-check');
    if (cb) cb.checked = checked;
  }
  // List row
  const row = document.getElementById('br-' + id);
  if (row) {
    row.style.background = checked ? 'rgba(123,97,255,.08)' : '';
    row.style.borderColor = checked ? 'var(--accent)' : '';
    const cb = row.querySelector('.row-check');
    if (cb) cb.checked = checked;
  }
  // Table row
  const tr = document.getElementById('tr-' + id);
  if (tr) {
    tr.style.background = checked ? 'rgba(123,97,255,.08)' : '';
    const cb = tr.querySelector('input[type=checkbox]');
    if (cb) cb.checked = checked;
  }
  // Rebuild table header "select all" state
  if (currentLayout === 'table') {
    const allChecked = window._lastBooks?.length > 0 &&
      window._lastBooks.every(b => libSelection.has(b.id));
    const hcb = document.querySelector('#book-table thead input[type=checkbox]');
    if (hcb) hcb.checked = allChecked;
  }
}

function _updateSelectBar() {
  const count = libSelection.size;
  const el    = document.getElementById('select-bar-count');
  if (el) el.textContent = `${count} selected`;
  // Also sync with batch page selection
  _batchSelected = new Set(libSelection);
}

function libToggleAll(checked) {
  (window._lastBooks || []).forEach(b => {
    if (checked) libSelection.add(b.id);
    else libSelection.delete(b.id);
  });
  // Auto-enter select mode if checking
  if (checked && !selectMode) {
    selectMode = true;
    document.getElementById('vbtn-sel')?.classList.add('active');
    document.getElementById('library-page')?.classList.add('sel-mode');
    document.getElementById('select-bar')?.classList.add('visible');
  } else if (!checked && libSelection.size === 0) {
    // Unchecking all — exit select mode
    selectMode = false;
    document.getElementById('vbtn-sel')?.classList.remove('active');
    document.getElementById('library-page')?.classList.remove('sel-mode');
    document.getElementById('select-bar')?.classList.remove('visible');
  }
  _updateSelectBar();
  renderBooks(window._lastBooks || []);
}

function libSelectAll()    { libToggleAll(true); }
function libSelectNone()   { libSelection.clear(); _updateSelectBar(); renderBooks(window._lastBooks||[]); }
function libInvertSel()    {
  (window._lastBooks||[]).forEach(b => {
    if (libSelection.has(b.id)) libSelection.delete(b.id);
    else libSelection.add(b.id);
  });
  _updateSelectBar();
  renderBooks(window._lastBooks || []);
}

// ---------------------------------------------------------------------------
// Quick batch actions from library selection bar
// ---------------------------------------------------------------------------

async function libBatchScrape() {
  if (!libSelection.size) return;
  // Navigate to batch page with selection pre-loaded
  _batchSelected = new Set(libSelection);
  showBatch();
  // Expand scrape section and scroll to it
  const body = document.getElementById('scrape-body');
  if (body?.classList.contains('collapsed')) toggleBatchSection('scrape');
  setTimeout(() => body?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 200);
}

async function libBatchApply() {
  if (!libSelection.size) { toast('Select books first', 'err'); return; }
  if (!confirm(`Apply pinned metadata to ${libSelection.size} book(s)?`)) return;

  const ids    = [...libSelection];
  const fields = ['title','series','synopsis','authors','genres','year','publisher'];
  const btn    = document.getElementById('select-bar');

  // Quick inline operation — no navigation needed
  const resp = await fetch('/batch/metadata/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_ids: ids, fields, pinned_only: true }),
  });
  // Read SSE to completion silently
  await _drainSSE(resp, (done, total) => {
    const el = document.getElementById('select-bar-count');
    if (el) el.textContent = `Applying… ${done}/${total}`;
  });
  toast(`Applied metadata to ${ids.length} books`, 'ok');
  loadBooks(); loadStats();
  _updateSelectBar();
}

async function libBatchEdit() {
  if (!libSelection.size) { toast('Select books first', 'err'); return; }
  _batchSelected = new Set(libSelection);
  showBatch();
  const body = document.getElementById('edit-body');
  if (body?.classList.contains('collapsed')) toggleBatchSection('edit');
  setTimeout(() => body?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 200);
}

async function libBatchDelete() {
  if (!libSelection.size) { toast('Select books first', 'err'); return; }
  if (!confirm(`Delete scraped metadata for ${libSelection.size} book(s)?\nManual entries will be kept.`)) return;

  const ids  = [...libSelection];
  const resp = await fetch('/batch/metadata/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_ids: ids, keep_manual: true }),
  });
  await _drainSSE(resp);
  toast(`Metadata deleted for ${ids.length} books`, 'ok');
}

async function libBatchWebp() {
  if (!libSelection.size) { toast('Select books first', 'err'); return; }
  // Only CBZ/CBR support conversion
  const books    = (window._lastBooks || []).filter(b => libSelection.has(b.id));
  const eligible = books.filter(b => ['cbz','cbr'].includes(b.type));
  if (!eligible.length) {
    toast('No CBZ/CBR files in selection — WebP conversion requires CBZ or CBR', 'err');
    return;
  }
  const skipped = books.length - eligible.length;
  const msg = skipped
    ? `Convert ${eligible.length} CBZ/CBR files to WebP?\n(${skipped} non-CBZ files will be skipped)`
    : `Convert ${eligible.length} CBZ/CBR files to WebP?`;
  if (!confirm(msg)) return;

  // Read quality from config defaults
  let quality = 85;
  try { const cfg = await api('/config'); quality = cfg.std_webp_quality ?? 85; } catch (_) {}

  const ids  = eligible.map(b => b.id);
  const btn  = document.getElementById('select-bar-count');
  const orig = btn?.textContent;
  if (btn) btn.textContent = `Converting… 0/${ids.length}`;

  const resp = await fetch('/batch/convert/webp', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_ids: ids, webp_quality: quality, delete_old: false }),
  });
  await _drainSSE(resp, (done, total) => {
    if (btn) btn.textContent = `Converting… ${done}/${total}`;
  });
  if (btn) btn.textContent = orig;
  toast(`WebP conversion complete for ${ids.length} files`, 'ok');
  loadBooks();
}

// Drain an SSE stream silently, calling onProgress(done,total) on each progress event
async function _drainSSE(resp, onProgress) {
  if (!resp.body) return;
  const reader = resp.body.getReader();
  const dec    = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n'); buf = parts.pop();
    for (const part of parts) {
      const dl = part.split('\n').find(l => l.startsWith('data:'));
      const el = part.split('\n').find(l => l.startsWith('event:'));
      if (!dl || !onProgress) continue;
      try {
        const evt  = el ? el.slice(6).trim() : 'log';
        const data = JSON.parse(dl.slice(5).trim());
        if (evt === 'progress' && onProgress) onProgress(data.done, data.total);
      } catch (_) {}
    }
  }
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
// ---------------------------------------------------------------------------
// Scan — SSE progress popup
// ---------------------------------------------------------------------------

let _scanAbortController = null;   // AbortController for the SSE fetch

async function triggerScan() {
  const btn = document.querySelector('.scan-btn');
  if (btn) { btn.textContent = '⟳ Scanning…'; btn.disabled = true; }

  // Reset and show popup
  _scanPopupReset();
  document.getElementById('scan-popup').classList.add('visible');
  document.getElementById('btn-cancel-scan').style.display = '';

  _scanAbortController = new AbortController();

  try {
    const resp = await fetch('/scan', {
      method: 'POST',
      signal: _scanAbortController.signal,
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      _scanPopupError(err.detail || 'Scan failed');
      return;
    }

    await _readScanSSE(resp);

  } catch (e) {
    if (e.name !== 'AbortError') {
      _scanPopupError(e.message);
    }
  } finally {
    _scanAbortController = null;
    if (btn) { btn.textContent = '⟳ Scan library'; btn.disabled = false; }
    document.getElementById('btn-cancel-scan').style.display = 'none';
  }
}

async function cancelScan() {
  if (_scanAbortController) {
    // Tell the server to stop
    fetch('/scan', { method: 'DELETE' }).catch(() => {});
    // Cut the SSE connection
    _scanAbortController.abort();
  }
  document.getElementById('scan-popup-title').textContent = 'Scan cancelled';
  document.getElementById('scan-popup-icon').textContent = '⚠';
  document.getElementById('btn-cancel-scan').style.display = 'none';
  document.getElementById('scan-status-text').textContent = 'Scan was cancelled.';
}

function closeScanPopup() {
  document.getElementById('scan-popup').classList.remove('visible');
}

function _scanPopupReset() {
  document.getElementById('scan-popup-title').textContent = 'Scanning library…';
  document.getElementById('scan-popup-icon').textContent = '⟳';
  document.getElementById('scan-bar-fill').style.width = '0%';
  document.getElementById('scan-bar-text').textContent = 'Discovering files…';
  document.getElementById('scan-bar-pct').textContent = '';
  document.getElementById('scan-log-wrap').innerHTML = '';
  document.getElementById('scan-status-text').textContent = '';
}

function _scanPopupError(msg) {
  document.getElementById('scan-popup-title').textContent = 'Scan error';
  document.getElementById('scan-popup-icon').textContent = '✗';
  document.getElementById('scan-status-text').textContent = msg;
  document.getElementById('scan-bar-fill').style.background = 'var(--red)';
  toast(`Scan failed: ${msg}`, 'err');
}

function _scanLogLine(text, cls = '') {
  const wrap = document.getElementById('scan-log-wrap');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = text;
  wrap.appendChild(line);
  // Keep only last 40 lines to avoid DOM bloat
  while (wrap.children.length > 40) wrap.removeChild(wrap.firstChild);
  wrap.scrollTop = wrap.scrollHeight;
}

async function _readScanSSE(resp) {
  if (!resp.body) return;
  const reader = resp.body.getReader();
  const dec    = new TextDecoder();
  let buf = '';
  let total = 0;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop();

    for (const part of parts) {
      const eline = part.split('\n').find(l => l.startsWith('event:'));
      const dline = part.split('\n').find(l => l.startsWith('data:'));
      if (!dline) continue;

      const etype = eline ? eline.slice(6).trim() : 'log';
      let payload;
      try { payload = JSON.parse(dline.slice(5).trim()); } catch { continue; }

      if (etype === 'count') {
        total = payload.total;
        document.getElementById('scan-bar-text').textContent =
          `Found ${total} file${total !== 1 ? 's' : ''} — processing…`;
        document.getElementById('scan-bar-fill').style.width = '2%';
        _scanLogLine(`📂 ${total} files discovered`);

      } else if (etype === 'progress') {
        const pct = total ? Math.round((payload.done / total) * 100) : 0;
        document.getElementById('scan-bar-fill').style.width = pct + '%';
        document.getElementById('scan-bar-pct').textContent = pct + '%';
        document.getElementById('scan-bar-text').textContent =
          `${payload.done} / ${total}`;
        if (payload.action === 'added') {
          _scanLogLine(`+ ${payload.file}`, 'log-added');
        } else if (payload.action === 'error') {
          _scanLogLine(`✗ ${payload.file}`, 'log-error');
        }

      } else if (etype === 'removed') {
        if (payload.count > 0) {
          _scanLogLine(`− ${payload.count} removed from library`);
        }

      } else if (etype === 'done') {
        const { added, removed, errors } = payload;
        document.getElementById('scan-bar-fill').style.width = '100%';
        document.getElementById('scan-bar-fill').style.background = 'var(--green)';
        document.getElementById('scan-popup-title').textContent = 'Scan complete';
        document.getElementById('scan-popup-icon').textContent = '✓';
        document.getElementById('scan-bar-text').textContent = 'Done';
        document.getElementById('scan-bar-pct').textContent = '';
        const parts = [];
        if (added)   parts.push(`+${added} added`);
        if (removed) parts.push(`-${removed} removed`);
        if (errors.length) parts.push(`${errors.length} error${errors.length > 1 ? 's' : ''}`);
        const summary = parts.join(' · ') || 'Nothing changed';
        document.getElementById('scan-status-text').textContent = summary;
        toast(`Scan done: ${summary}`, 'ok');
        loadBooks(); loadStats();

      } else if (etype === 'cancelled') {
        document.getElementById('scan-popup-title').textContent = 'Scan cancelled';
        document.getElementById('scan-popup-icon').textContent = '⚠';

      } else if (etype === 'error') {
        _scanPopupError(payload.msg || String(payload));
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------
async function openModal(bookId) {
  try {
    const b = await api(`/books/${bookId}`);
    currentBook   = b;
    selectedMetaId = null;
    _lastFetchSource = null;

    document.getElementById('modal-cover-wrap').innerHTML = b.cover_path
      ? `<img class="modal-cover" src="/books/${b.id}/cover" alt="">`
      : `<div class="modal-cover-ph">${typeIcon(b.type)}</div>`;
    document.getElementById('modal-title').textContent  = b.title;
    document.getElementById('modal-series').textContent =
      b.series ? `${b.series}${b.volume!=null?` — Vol. ${b.volume}`:''}` : '';
    document.getElementById('modal-path').textContent   = b.path;
    document.getElementById('modal-badges').innerHTML = `
      <span class="type-badge type-${b.type}">${b.type.toUpperCase()}</span>
      <span class="cat-badge cat-${b.category||'unknown'}">${b.category||'unknown'}</span>
      <span class="status-badge ${b.status||'unread'}">${b.status||'unread'}</span>
      ${b.file_size ? `<span class="tag-pill">${fmtSize(b.file_size)}</span>` : ''}`;

    document.getElementById('std-tab').style.display = ['cbz','cbr'].includes(b.type) ? '' : 'none';

    document.querySelectorAll('.modal-tab').forEach((t,i)  => t.classList.toggle('active', i===0));
    document.querySelectorAll('.modal-panel').forEach((p,i) => p.classList.toggle('active', i===0));

    populateInfoTab(b);
    renderStatusOpts(b.status || 'unread');
    document.getElementById('progress-input').value = b.progress || '';
    renderTags(b.tags || []);
    populateMetaTab(b);
    populateStdTab();
    populateMoveTab(b);

    document.getElementById('modal-overlay').classList.add('open');
  } catch { toast('Failed to load book', 'err'); }
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  currentBook = null; selectedMetaId = null;
}
function closeModalOnBg(e) {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}

// ---------------------------------------------------------------------------
// Modal tabs
// ---------------------------------------------------------------------------
function switchTab(name) {
  const names = ['info','reading','tags','metadata','conversion','move'];
  const idx = names.indexOf(name);
  document.querySelectorAll('.modal-tab').forEach((t,i)  => t.classList.toggle('active', i===idx));
  document.querySelectorAll('.modal-panel').forEach((p,i) => p.classList.toggle('active', i===idx));
}

// ---------------------------------------------------------------------------
// Tab: Info
// ---------------------------------------------------------------------------
function populateInfoTab(b) {
  // File classification fields (still editable)
  document.getElementById('info-type').value = b.type || 'unknown';
  renderCatOpts(b.category || 'unknown');
  // Metadata display (read-only, loaded from cache)
  _renderInfoMeta(b.id);
}

async function _renderInfoMeta(bookId) {
  const panel = document.getElementById('info-meta-panel');
  if (!panel) return;
  panel.innerHTML = '<div style="color:var(--muted);font-size:.8rem">Loading…</div>';

  let rows = [];
  try { rows = await api(`/books/${bookId}/metadata`); } catch (_) {}

  if (!rows.length) {
    panel.innerHTML = `<div style="color:var(--muted);font-size:.82rem;font-style:italic;padding:.4rem 0">
      No metadata yet — go to the <strong>Metadata</strong> tab to search or add some.
    </div>`;
    return;
  }

  // Priority: pinned > manual > highest score > most recent
  const pinned = rows.find(r => r.is_pinned);
  const manual = rows.find(r => r.is_manual || r.source === 'manual');
  const best   = pinned
    || manual
    || [...rows].sort((a, z) => (z.score || 0) - (a.score || 0))[0];

  const srcLabel = best.source.replace(/_\d+$/, '');
  const isPinned = best.is_pinned;
  const isManual = best.is_manual || best.source === 'manual';

  const field = (key, val) => {
    if (!val || (Array.isArray(val) && !val.length)) return '';
    const display = Array.isArray(val) ? val.map(esc).join(', ') : esc(String(val));
    return `<div class="info-meta-field">
      <span class="info-meta-key">${key}</span>
      <span class="info-meta-val">${display}</span>
    </div>`;
  };

  let html = '';

  // Cover thumbnail (float right)
  if (best.cover_url) {
    html += `<img src="${esc(best.cover_url)}" class="info-meta-cover"
               onerror="this.style.display='none'" loading="lazy" alt="cover">`;
  }

  // Synopsis at top
  if (best.synopsis) {
    const syn = best.synopsis.length > 500
      ? best.synopsis.slice(0, 500) + '…'
      : best.synopsis;
    html += `<div class="info-meta-synopsis">${esc(syn)}</div>`;
  }

  // Structured fields
  html += field('Title',     best.title);
  html += field('Series',    best.series);
  html += field('Volume',    best.volume != null ? best.volume : null);
  html += field('Authors',   best.authors);
  html += field('Artists',   best.artists);
  html += field('Genres',    best.genres);
  html += field('Tags',      (best.tags || []).slice(0, 8));
  html += field('Publisher', best.publisher);
  html += field('Year',      best.year);
  html += field('Language',  best.language);
  html += field('Country',   best.country);
  html += field('Status',    best.pub_status);
  html += field('Score',     best.score ? `${best.score}/10${best.score_count ? ` (${best.score_count.toLocaleString()} votes)` : ''}` : null);
  html += field('Popularity',best.popularity ? best.popularity.toLocaleString() : null);
  html += field('ISBN',      best.isbn || best.isbn13);

  // Source info
  const flags = [
    isPinned ? '📌 Pinned' : '',
    isManual ? '✏️ Manual' : '',
    !isPinned && !isManual ? `from ${srcLabel}` : srcLabel,
  ].filter(Boolean).join(' · ');

  html += `<div class="info-meta-source">${esc(flags)}`;

  // Quick link to metadata tab
  html += `&nbsp;<button onclick="switchTab('metadata')"
    style="background:none;border:none;cursor:pointer;color:var(--accent2);
    font-size:.7rem;padding:0;text-decoration:underline">
    Edit in Metadata tab →
  </button></div>`;

  // If more rows available, show count
  if (rows.length > 1) {
    html += `<div style="font-size:.7rem;color:var(--muted);margin-top:.3rem">
      ${rows.length - 1} other result${rows.length > 2 ? 's' : ''} available in Metadata tab
    </div>`;
  }

  panel.innerHTML = html;
}

function renderCatOpts(current) {
  document.getElementById('cat-opts').innerHTML =
    ['manga','comics','book','unknown'].map(c =>
      `<div class="cat-opt ${current===c?'sel-'+c:''}" onclick="selectCat('${c}')">${c}</div>`
    ).join('');
}
function selectCat(cat) {
  document.querySelectorAll('.cat-opt').forEach(el => {
    const c = el.textContent.trim();
    el.className = `cat-opt ${c===cat?'sel-'+c:''}`;
  });
}

// Save only the file classification fields (category + type)
async function saveFileClass() {
  if (!currentBook) return;
  const sel  = document.querySelector('.cat-opt[class*="sel-"]');
  const type = document.getElementById('info-type').value;
  const body = {};
  if (sel)  body.category = sel.textContent.trim();
  if (type) body.type     = type;
  if (!Object.keys(body).length) return;
  try {
    const updated = await api(`/books/${currentBook.id}`, { method: 'PATCH', body });
    currentBook = updated;
    // Refresh badges in modal header
    document.getElementById('modal-badges').innerHTML = `
      <span class="type-badge type-${updated.type}">${updated.type.toUpperCase()}</span>
      <span class="cat-badge cat-${updated.category||'unknown'}">${updated.category||'unknown'}</span>
      <span class="status-badge ${updated.status||'unread'}">${updated.status||'unread'}</span>
      ${updated.file_size ? `<span class="tag-pill">${fmtSize(updated.file_size)}</span>` : ''}`;
    toast('Classification saved', 'ok');
    loadBooks();
  } catch (e) { toast(`Save failed: ${e.message}`, 'err'); }
}

// ---------------------------------------------------------------------------
// Tab: Reading status
// ---------------------------------------------------------------------------
function renderStatusOpts(current) {
  document.getElementById('status-opts').innerHTML =
    ['unread','reading','read'].map(s =>
      `<div class="status-opt ${current===s?'sel-'+s:''}" onclick="selectStatus('${s}')">${s}</div>`
    ).join('');
}
function selectStatus(s) {
  document.querySelectorAll('.status-opt').forEach(el => {
    const v = el.textContent.trim();
    el.className = `status-opt ${v===s?'sel-'+v:''}`;
  });
}
async function saveStatus() {
  if (!currentBook) return;
  const sel = document.querySelector('.status-opt[class*="sel-"]');
  if (!sel) return;
  const status   = sel.textContent.trim();
  const progress = parseInt(document.getElementById('progress-input').value) || 0;
  try {
    await api(`/books/${currentBook.id}/status`, { method:'PUT', body:{status, progress} });
    toast('Status saved', 'ok'); loadBooks(); loadStats();
  } catch { toast('Failed to save status', 'err'); }
}

// ---------------------------------------------------------------------------
// Tab: Tags
// ---------------------------------------------------------------------------
function renderTags(tags) {
  const el = document.getElementById('tag-editor');
  el.innerHTML = tags.length
    ? tags.map(t => `<span class="tag-pill-rm">${esc(t)}
        <button onclick="removeTag('${esc(t)}')" title="Remove">✕</button></span>`).join('')
    : '<span style="color:var(--muted);font-size:.78rem">No tags yet</span>';
}
async function addTag() {
  if (!currentBook) return;
  const input = document.getElementById('tag-input');
  const tag = input.value.trim().toLowerCase();
  if (!tag) return;
  try {
    await api(`/books/${currentBook.id}/tags/${encodeURIComponent(tag)}`, { method:'POST' });
    if (!currentBook.tags) currentBook.tags = [];
    if (!currentBook.tags.includes(tag)) currentBook.tags.push(tag);
    renderTags(currentBook.tags); input.value = '';
  } catch { toast('Failed to add tag', 'err'); }
}
async function removeTag(tag) {
  if (!currentBook) return;
  try {
    await api(`/books/${currentBook.id}/tags/${encodeURIComponent(tag)}`, { method:'DELETE' });
    currentBook.tags = (currentBook.tags||[]).filter(t => t !== tag);
    renderTags(currentBook.tags);
  } catch { toast('Failed to remove tag', 'err'); }
}

// ---------------------------------------------------------------------------
// Tab: Metadata — full rewrite
// ---------------------------------------------------------------------------

let _allSources = [];   // loaded once, list of {id, label, enabled, key_set, …}

function populateMetaTab(b) {
  document.getElementById('meta-results').innerHTML = '';
  document.getElementById('meta-apply-bar').classList.remove('visible');
  document.getElementById('meta-query-title').value  = b.series || b.title || '';
  document.getElementById('meta-query-author').value = '';
  selectedMetaId   = null;
  _lastFetchSource = null;

  _loadSources().then(() => loadMetadata(b.id));
}

async function _loadSources() {
  try {
    _allSources = await api('/metadata/sources');
    const sel = document.getElementById('meta-source');
    if (!sel) return;
    sel.innerHTML = _allSources
      .filter(s => s.enabled)
      .map(s => {
        const keyWarn = s.requires_key && !s.key_set ? ' ⚠' : '';
        return `<option value="${s.id}">${esc(s.label)}${keyWarn}</option>`;
      }).join('');
    if (!sel.value && _allSources.length) sel.value = _allSources[0].id;
  } catch (_) {}
}

async function loadMetadata(bookId) {
  try {
    const rows = await api(`/books/${bookId}/metadata`);
    // On open: only show pinned + manual, not stale search results
    if (!_lastFetchSource) {
      const show = rows.filter(r => r.is_pinned || r.is_manual || r.source === 'manual');
      renderMeta(show.length ? show : rows.filter(r => r.source === 'manual'), null);
    } else {
      renderMeta(rows, _lastFetchSource);
    }
  } catch (_) {}
}

// FIX: extract title correctly from each source's raw structure
function _extractTitle(m) {
  // Prefer the stored title column (new schema)
  if (m.title) return m.title;
  try {
    const raw = typeof m.raw_json === 'string' ? JSON.parse(m.raw_json) : (m.raw_json || {});
    if (raw.title && typeof raw.title === 'object') {
      return raw.title.english || raw.title.romaji || raw.title.native || '';
    }
    if (typeof raw.title === 'string') return raw.title;
    if (typeof raw.name  === 'string') return raw.name;
  } catch (_) {}
  return '';
}

function _extractSubtitle(m) {
  const parts = [];
  if (m.year)        parts.push(String(m.year));
  if (m.pub_status)  parts.push(m.pub_status);
  if (m.score)       parts.push(`★ ${m.score}/10`);
  if (m.publisher)   parts.push(m.publisher);
  return parts.join(' · ');
}

function renderMeta(rows, activeSrc) {
  const el = document.getElementById('meta-results');
  if (!rows || !rows.length) {
    el.innerHTML = '<p style="color:var(--muted);font-size:.8rem;margin-top:.4rem">No metadata yet — search above or edit manually below.</p>';
    _populateManualForm(null);
    return;
  }

  const manual   = rows.find(r => r.source === 'manual' || r.is_manual);
  const apiRows  = rows.filter(r => !r.is_manual && r.source !== 'manual');

  // Filter to active source if a search just ran
  const showSrc  = activeSrc || _lastFetchSource;
  const filtered = showSrc
    ? apiRows.filter(r => r.source.replace(/_\d+$/, '') === showSrc)
    : apiRows;

  // Also always show pinned rows even if from a different source
  const pinned = apiRows.filter(r => r.is_pinned && !filtered.includes(r));

  const toShow = [...(pinned.length ? pinned : []), ...filtered];

  // Group by base source
  const groups = {};
  for (const m of toShow) {
    const base = m.source.replace(/_\d+$/, '');
    if (!groups[base]) groups[base] = [];
    groups[base].push(m);
  }

  let html = '';
  if (!Object.keys(groups).length && !manual) {
    html = '<p style="color:var(--muted);font-size:.8rem;margin-top:.4rem">No results. Search above.</p>';
  }

  // Pinned banner
  const pinnedRow = rows.find(r => r.is_pinned);
  if (pinnedRow && showSrc && pinnedRow.source.replace(/_\d+$/, '') !== showSrc) {
    const pt = _extractTitle(pinnedRow);
    html += `<div style="background:rgba(123,97,255,.1);border:1px solid rgba(123,97,255,.3);
      border-radius:7px;padding:.45rem .75rem;margin-bottom:.5rem;font-size:.78rem;
      color:var(--accent2)">📌 Pinned: ${esc(pt || pinnedRow.source)}</div>`;
  }

  for (const [base, items] of Object.entries(groups)) {
    html += `<div style="font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
      color:var(--accent2);margin:.7rem 0 .3rem;font-weight:700;
      border-bottom:1px solid var(--border);padding-bottom:.2rem">
      ${esc(base)} — ${items.length} result${items.length!==1?'s':''}</div>`;

    for (const m of items) {
      const title    = _extractTitle(m);
      const subtitle = _extractSubtitle(m);
      const isPinned = m.is_pinned;
      html += `
        <div class="meta-source ${selectedMetaId===m.id?'selected':''} ${isPinned?'pinned':''}"
             onclick="selectMeta(${m.id})" id="meta-row-${m.id}">
          <div class="meta-source-header">
            <div style="display:flex;align-items:center;gap:.4rem;min-width:0">
              ${isPinned ? '<span title="Pinned" style="color:var(--accent2)">📌</span>' : ''}
              <span class="meta-source-name" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${title ? esc(title) : esc(m.source)}</span>
              ${subtitle ? `<span style="font-size:.7rem;color:var(--muted);flex-shrink:0">${esc(subtitle)}</span>` : ''}
            </div>
            <span style="font-size:.68rem;color:var(--muted);flex-shrink:0;margin-left:.4rem">${(m.fetched_at||'').slice(0,10)}</span>
          </div>
          ${m.synopsis ? `<div class="meta-synopsis">${esc(m.synopsis.slice(0,200))}${m.synopsis.length>200?'…':''}</div>` : ''}
          <div style="display:flex;gap:.75rem;flex-wrap:wrap;margin-top:.3rem">
            ${(m.authors||[]).length ? `<div class="meta-field"><span>Authors:</span> ${m.authors.slice(0,3).map(esc).join(', ')}</div>` : ''}
            ${(m.artists||[]).length ? `<div class="meta-field"><span>Artists:</span> ${m.artists.slice(0,2).map(esc).join(', ')}</div>` : ''}
            ${(m.genres||[]).length  ? `<div class="meta-field"><span>Genres:</span>  ${m.genres.slice(0,4).map(esc).join(', ')}</div>` : ''}
            ${m.isbn || m.isbn13 ? `<div class="meta-field"><span>ISBN:</span> ${esc(m.isbn13 || m.isbn)}</div>` : ''}
            ${m.cover_url ? `<img src="${esc(m.cover_url)}" style="height:60px;border-radius:4px;margin-top:.2rem" loading="lazy">` : ''}
          </div>
        </div>`;
    }
  }
  el.innerHTML = html;
  _populateManualForm(manual || null);
}

function _populateManualForm(m) {
  if (!m) {
    ['title','series','volume','synopsis','authors','artists','genres','tags',
     'publisher','year','isbn','isbn13','score'].forEach(f => {
      const el = document.getElementById('manual-'+f);
      if (el) el.value = '';
    });
    return;
  }
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
  set('manual-title',   m.title);
  set('manual-series',  m.series);
  set('manual-volume',  m.volume != null ? m.volume : '');
  set('manual-synopsis',m.synopsis);
  set('manual-authors', (m.authors||[]).join(', '));
  set('manual-artists', (m.artists||[]).join(', '));
  set('manual-genres',  (m.genres||[]).join(', '));
  set('manual-tags',    (m.tags||[]).join(', '));
  set('manual-publisher',m.publisher);
  set('manual-year',    m.year);
  set('manual-isbn',    m.isbn);
  set('manual-isbn13',  m.isbn13);
  set('manual-score',   m.score);
  // Open the details if populated
  if (m.title || m.synopsis || (m.authors||[]).length) {
    const d = document.getElementById('manual-details');
    if (d) d.open = true;
  }
}

function selectMeta(metaId) {
  selectedMetaId = metaId;
  document.querySelectorAll('.meta-source').forEach(el =>
    el.classList.toggle('selected', el.id === `meta-row-${metaId}`));
  document.getElementById('meta-apply-bar').classList.add('visible');
}

async function fetchMeta() {
  if (!currentBook) return;
  const source  = document.getElementById('meta-source').value;
  const titleQ  = document.getElementById('meta-query-title').value.trim();
  const authorQ = document.getElementById('meta-query-author').value.trim();
  if (!titleQ) { toast('Enter a title to search', 'err'); return; }

  let query = titleQ;
  if (authorQ) query += ' ' + authorQ;

  document.getElementById('meta-results').innerHTML =
    `<p style="color:var(--muted);font-size:.8rem">Searching ${esc(source)}…</p>`;

  try {
    const resp = await api('/metadata/fetch', {
      method: 'POST', body: { book_id: currentBook.id, source, query },
    });
    _lastFetchSource = source;
    // Always read back from DB — the stored rows have all fields (title, authors…)
    // properly populated, unlike the raw fetcher response
    const rows  = await api(`/books/${currentBook.id}/metadata`);
    const count = rows.filter(r => r.source.startsWith(source + '_')).length;
    renderMeta(rows, source);
    toast(`${count} result${count!==1?'s':''} from ${source}`, 'ok');
    // Refresh info tab metadata panel
    _renderInfoMeta(currentBook.id);
  } catch (e) {
    toast(`Fetch failed: ${e.message}`, 'err');
    document.getElementById('meta-results').innerHTML =
      `<p style="color:var(--red);font-size:.8rem">${esc(e.message)}</p>`;
  }
}

async function pinSelectedMeta() {
  if (!currentBook || !selectedMetaId) return;
  try {
    await api(`/metadata/${currentBook.id}/pin/${selectedMetaId}`, { method: 'POST' });
    toast('Result pinned as best match', 'ok');
    const rows = await api(`/books/${currentBook.id}/metadata`);
    renderMeta(rows, _lastFetchSource);
    _renderInfoMeta(currentBook);
    loadBooks();
  } catch (e) { toast(`Pin failed: ${e.message}`, 'err'); }
}

async function applySelectedMeta() {
  if (!currentBook || !selectedMetaId) return;
  const fields = [];
  ['title','series','volume','authors','synopsis','genres','year','publisher'].forEach(f => {
    if (document.getElementById('apply-'+f)?.checked) fields.push(f);
  });
  if (!fields.length) { toast('Select at least one field', 'err'); return; }
  try {
    await api(`/metadata/${currentBook.id}/apply/${selectedMetaId}`, {
      method: 'POST', body: { metadata_id: selectedMetaId, fields, pin: true },
    });
    const updated = await api(`/books/${currentBook.id}`);
    currentBook = updated;
    document.getElementById('modal-title').textContent  = updated.title;
    document.getElementById('modal-series').textContent =
      updated.series ? `${updated.series}${updated.volume!=null?` — Vol. ${updated.volume}`:''}` : '';
    populateInfoTab(updated);
    toast('Fields applied', 'ok'); loadBooks();
  } catch (e) { toast(`Apply failed: ${e.message}`, 'err'); }
}

async function deleteSelectedMeta() {
  if (!currentBook || !selectedMetaId) return;
  if (!confirm('Delete this metadata result?')) return;
  try {
    await api(`/metadata/${currentBook.id}/${selectedMetaId}`, { method: 'DELETE' });
    selectedMetaId = null;
    document.getElementById('meta-apply-bar').classList.remove('visible');
    const rows = await api(`/books/${currentBook.id}/metadata`);
    renderMeta(rows, _lastFetchSource);
    toast('Deleted', 'ok');
  } catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
}

async function deleteAllMeta() {
  if (!currentBook) return;
  if (!confirm('Delete ALL metadata for this book?')) return;
  try {
    await api(`/metadata/${currentBook.id}`, { method: 'DELETE' });
    selectedMetaId = null; _lastFetchSource = null;
    document.getElementById('meta-apply-bar').classList.remove('visible');
    renderMeta([], null);
    _renderInfoMeta(currentBook);
    loadBooks();
    toast('All metadata deleted', 'ok');
  } catch (e) { toast(`Failed: ${e.message}`, 'err'); }
}

async function saveManualMeta(andPin = false) {
  if (!currentBook) return;
  const csvToArr = id => {
    const v = document.getElementById(id)?.value || '';
    return v.split(',').map(s => s.trim()).filter(Boolean);
  };
  const body = {
    title:     document.getElementById('manual-title')?.value.trim()   || null,
    series:    document.getElementById('manual-series')?.value.trim()  || null,
    volume:    parseInt(document.getElementById('manual-volume')?.value) || null,
    synopsis:  document.getElementById('manual-synopsis')?.value.trim()  || null,
    publisher: document.getElementById('manual-publisher')?.value.trim() || null,
    year:      parseInt(document.getElementById('manual-year')?.value)   || null,
    isbn:      document.getElementById('manual-isbn')?.value.trim()    || null,
    isbn13:    document.getElementById('manual-isbn13')?.value.trim()   || null,
    score:     parseFloat(document.getElementById('manual-score')?.value) || null,
    authors:   csvToArr('manual-authors'),
    artists:   csvToArr('manual-artists'),
    genres:    csvToArr('manual-genres'),
    tags:      csvToArr('manual-tags'),
  };
  // Remove null values
  Object.keys(body).forEach(k => (body[k] === null || body[k] !== body[k]) && delete body[k]);

  try {
    const saved = await api(`/metadata/${currentBook.id}/manual`, { method: 'PUT', body });
    if (andPin && saved.id) {
      await api(`/metadata/${currentBook.id}/pin/${saved.id}`, { method: 'POST' });
    }
    const rows = await api(`/books/${currentBook.id}/metadata`);
    renderMeta(rows, _lastFetchSource);
    const updated = await api(`/books/${currentBook.id}`);
    currentBook = updated;
    populateInfoTab(updated);
    toast('Manual metadata saved' + (andPin ? ' & pinned' : ''), 'ok');
    loadBooks();
  } catch (e) { toast(`Save failed: ${e.message}`, 'err'); }
}

// ---------------------------------------------------------------------------
// Tab: Conversion
// ---------------------------------------------------------------------------
function populateStdTab() {
  document.getElementById('std-log').textContent = 'Ready.';
  api('/config').then(cfg => {
    document.getElementById('std-webp').checked   = !!cfg.std_webp;
    document.getElementById('std-quality').value  = cfg.std_webp_quality ?? 85;
    document.getElementById('std-delete').checked = !!cfg.std_delete_old;
  }).catch(() => {});
}

async function runStandardize() {
  if (!currentBook) return;
  const log  = document.getElementById('std-log');
  const btn  = document.getElementById('btn-std');
  const webp = document.getElementById('std-webp').checked;
  const qual = parseInt(document.getElementById('std-quality').value) || 85;
  const del  = document.getElementById('std-delete').checked;

  log.textContent = 'Starting…\n'; btn.disabled = true;
  const resp = await fetch(`/books/${currentBook.id}/standardize`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ webp, webp_quality:qual, delete_old:del }),
  });
  if (!resp.body) { log.textContent += 'Streaming not supported.\n'; btn.disabled=false; return; }

  await streamSSE(resp, {
    log:   line => { log.textContent += line+'\n'; log.scrollTop=log.scrollHeight; },
    done:  path => { log.textContent += `\n✓ Done: ${path}\n`; toast('Conversion complete','ok'); loadBooks(); },
    error: msg  => { log.textContent += `\n✗ Error: ${msg}\n`; toast(`Error: ${msg}`,'err'); },
  });
  btn.disabled = false;
}

// ---------------------------------------------------------------------------
// Tab: Move / Rename — FIX: add dry-run
// ---------------------------------------------------------------------------
function populateMoveTab(b) {
  document.getElementById('move-pattern').value = '{series}/{title}';
  document.getElementById('move-delete').checked = false;
  document.getElementById('move-dry').checked = true;   // default: dry run
  document.getElementById('move-result').textContent = '';
  updateMovePreview();
}

function updateMovePreview() {
  if (!currentBook) return;
  const pattern = document.getElementById('move-pattern').value;
  const vars = {
    series:        sanitizePreview(currentBook.series || 'Unknown Series'),
    title:         sanitizePreview(currentBook.title),
    'volume:02d':  String(currentBook.volume || 0).padStart(2,'0'),
    category:      currentBook.category || 'unknown',
    type:          currentBook.type,
  };
  let preview = pattern;
  for (const [k, v] of Object.entries(vars)) preview = preview.replaceAll('{'+k+'}', v);
  const ext = currentBook.path.split('.').pop() || currentBook.type;
  document.getElementById('move-preview').textContent = preview + '.' + ext;
}

function insertMoveVar(v) {
  const input = document.getElementById('move-pattern');
  const pos   = input.selectionStart;
  input.value = input.value.slice(0,pos) + v.trim() + input.value.slice(pos);
  updateMovePreview();
}
function insertVar(v) {
  const input = document.getElementById('cfg-rename-pattern');
  const pos   = input.selectionStart;
  input.value = input.value.slice(0,pos) + v.trim() + input.value.slice(pos);
}
function sanitizePreview(s) {
  return (s||'').replace(/[<>:"/\\|?*]/g,'_');
}

async function moveBook() {
  if (!currentBook) return;
  const pattern    = document.getElementById('move-pattern').value.trim();
  const delete_old = document.getElementById('move-delete').checked;
  const dry_run    = document.getElementById('move-dry').checked;
  const resultEl   = document.getElementById('move-result');
  const btn        = document.getElementById('btn-move');

  if (!pattern) { toast('Pattern cannot be empty', 'err'); return; }
  if (!dry_run && delete_old &&
      !confirm('This will delete the original file after moving. Continue?')) return;

  btn.disabled = true;
  resultEl.textContent = dry_run ? 'Previewing…' : 'Moving…';
  resultEl.style.color = 'var(--muted)';

  try {
    if (dry_run) {
      // Preview only — call the route with dry_run flag via query param
      const resp = await api(`/books/${currentBook.id}/move/preview`, {
        method:'POST', body:{ pattern }
      });
      resultEl.textContent = `→ ${resp.destination}`;
      resultEl.style.color = 'var(--accent2)';
    } else {
      const updated = await api(`/books/${currentBook.id}/move`, {
        method:'POST', body:{ pattern, delete_old }
      });
      currentBook = updated;
      document.getElementById('modal-path').textContent = updated.path;
      resultEl.textContent = `✓ Moved to: ${updated.path}`;
      resultEl.style.color = 'var(--green)';
      toast('File moved successfully', 'ok'); loadBooks();
    }
  } catch (e) {
    resultEl.textContent = `✗ ${e.message}`;
    resultEl.style.color = 'var(--red)';
    toast(`Move failed: ${e.message}`, 'err');
  } finally { btn.disabled = false; }
}

// ---------------------------------------------------------------------------
// Delete book
// ---------------------------------------------------------------------------
async function deleteBook() {
  if (!currentBook || !confirm(`Delete "${currentBook.title}" from the collection?`)) return;
  try {
    await api(`/books/${currentBook.id}`, { method:'DELETE' });
    closeModal(); loadBooks(); loadStats(); toast('Book removed', 'ok');
  } catch { toast('Failed to delete', 'err'); }
}

// ---------------------------------------------------------------------------
// Settings page
// ---------------------------------------------------------------------------
async function loadSettingsPage() {
  setSaveStatus('');
  try {
    const config = await api('/config');
    const locked = new Set(config._env_locked || []);

    // Helper — set input value and mark read-only if env-locked
    const setField = (id, val, lockKey) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.value = val ?? '';
      const isLocked = lockKey && locked.has(lockKey);
      el.readOnly = isLocked;
      el.style.opacity = isLocked ? '0.6' : '';
      el.title = isLocked ? `Set via environment variable — edit your .env to change` : '';
      // Add/remove lock badge
      const badge = document.getElementById(id + '-lock');
      if (badge) badge.style.display = isLocked ? 'inline' : 'none';
    };

    setField('cfg-library-path', config.library_path, 'library_path');
    document.getElementById('cfg-scan-startup').checked = !!config.scan_on_startup;
    document.getElementById('cfg-std-webp').checked     = !!config.std_webp;
    document.getElementById('cfg-std-quality').value    = config.std_webp_quality ?? 85;
    document.getElementById('cfg-std-delete').checked   = !!config.std_delete_old;
    document.getElementById('cfg-debug').checked        = !!config.debug;

    const storageEl = document.getElementById('cfg-meta-storage');
    if (storageEl) storageEl.value = config.metadata_storage || 'db';
    const dirEl = document.getElementById('cfg-meta-files-dir');
    if (dirEl) dirEl.value = config.metadata_files_dir || 'data/metadata';

    // Provider checkboxes
    const provList = document.getElementById('cfg-providers-list');
    if (provList) {
      const sources = await api('/metadata/sources').catch(() => []);
      const enabled = config.metadata_providers_enabled || sources.map(s => s.id);
      const LABELS = {
        anilist:     'AniList (manga, free)',
        comicvine:   'ComicVine (comics, needs key)',
        googlebooks: 'Google Books (free)',
        hardcover:   'Hardcover (needs key)',
        openlib:     'Open Library (free)',
      };
      provList.innerHTML = sources.map(s => {
        const keyTag = s.requires_key
          ? (s.key_set
              ? `<span style="color:var(--green);font-size:.7rem"> ✓ key set</span>`
              : `<span style="color:var(--amber);font-size:.7rem"> ⚠ key not set</span>`)
          : `<span style="color:var(--muted);font-size:.7rem"> (free)</span>`;
        return `<div class="s-checkbox-row">
          <input type="checkbox" id="prov-${s.id}" value="${s.id}"
            ${enabled.includes(s.id) ? 'checked' : ''} style="accent-color:var(--accent)">
          <label for="prov-${s.id}" style="font-size:.85rem">${esc(LABELS[s.id]||s.id)}${keyTag}</label>
        </div>`;
      }).join('');
    }

    // API keys
    document.getElementById('cfg-comicvine-key').value = '';
    document.getElementById('cfg-hardcover-key').value = '';
    _showKeyStatus('cfg-comicvine-key',
      config.comicvine_api_key === '••••••••',
      locked.has('comicvine_api_key'));
    _showKeyStatus('cfg-hardcover-key',
      config.hardcover_api_key === '••••••••',
      locked.has('hardcover_api_key'));

    // Show global env notice if any keys are locked
    const envNotice = document.getElementById('env-locked-notice');
    if (envNotice) {
      if (locked.size > 0) {
        const names = [...locked].map(k => `<code>${k}</code>`).join(', ');
        envNotice.innerHTML = `🔒 ${names} ${locked.size === 1 ? 'is' : 'are'} set via environment variable and cannot be changed from the UI.`;
        envNotice.style.display = 'block';
      } else {
        envNotice.style.display = 'none';
      }
    }
  } catch { toast('Failed to load settings', 'err'); }
}

function _showKeyStatus(inputId, isSet, isLocked) {
  const el = document.getElementById(inputId);
  if (!el) return;
  if (isLocked) {
    el.placeholder = '(set via environment variable — read-only)';
    el.readOnly = true;
    el.style.opacity = '0.6';
  } else {
    el.placeholder = isSet ? '(key configured — leave blank to keep)' : 'Enter API key…';
    el.readOnly = false;
    el.style.opacity = '';
  }
}

async function saveSettings() {
  const comicvineKey = document.getElementById('cfg-comicvine-key').value.trim();
  const hardcoverKey = document.getElementById('cfg-hardcover-key').value.trim();

  // Collect enabled providers
  const enabledProviders = [];
  document.querySelectorAll('#cfg-providers-list input[type=checkbox]').forEach(cb => {
    if (cb.checked) enabledProviders.push(cb.value);
  });

  const patch = {
    library_path:                document.getElementById('cfg-library-path').value.trim(),
    scan_on_startup:             document.getElementById('cfg-scan-startup').checked,
    std_webp:                    document.getElementById('cfg-std-webp').checked,
    std_webp_quality:            parseInt(document.getElementById('cfg-std-quality').value) || 85,
    std_delete_old:              document.getElementById('cfg-std-delete').checked,
    debug:                       document.getElementById('cfg-debug').checked,
    metadata_storage:            document.getElementById('cfg-meta-storage')?.value || 'db',
    metadata_files_dir:          document.getElementById('cfg-meta-files-dir')?.value.trim() || 'data/metadata',
    metadata_providers_enabled:  enabledProviders,
  };
  if (comicvineKey) patch.comicvine_api_key = comicvineKey;
  if (hardcoverKey) patch.hardcover_api_key = hardcoverKey;

  try {
    await api('/config', { method: 'PATCH', body: patch });
    setSaveStatus('✓ Saved', true);
    toast('Settings saved', 'ok');
    document.getElementById('cfg-comicvine-key').value = '';
    document.getElementById('cfg-hardcover-key').value = '';
    loadSettingsPage();
  } catch { toast('Failed to save', 'err'); setSaveStatus('✗ Failed'); }
}

function setSaveStatus(msg, ok=false) {
  const el = document.getElementById('save-status');
  el.textContent = msg;
  el.className = 'save-status' + (ok ? ' saved' : '');
}

// Path verify & folder browser
async function verifyPath() {
  const pathEl = document.getElementById('cfg-library-path');
  const path   = pathEl.value.trim();
  if (!path) return;
  const el = document.getElementById('path-verify');
  el.style.display = 'block'; el.className = 'path-verify'; el.textContent = 'Checking…';
  try {
    const r = await api('/config/verify-path', { method: 'POST', body: { path } });
    if (r.valid) {
      const parts = Object.entries(r.by_format || {})
        .map(([k, v]) => `${v} ${k.toUpperCase()}`).join('  ·  ');
      el.className = 'path-verify ok';
      el.innerHTML = `✓ ${r.total} files found${parts ? '  —  ' + parts : ''}
        &nbsp;&nbsp;<button onclick="saveLibraryPath('${esc(path)}')"
          style="background:var(--accent);color:#fff;border:none;border-radius:5px;
          padding:.2rem .55rem;font-size:.75rem;cursor:pointer;font-weight:600">
          Save this path
        </button>`;
    } else {
      el.className = 'path-verify err';
      el.textContent = `✗ ${r.error}`;
    }
  } catch (e) {
    el.className = 'path-verify err';
    el.textContent = `✗ ${e.message}`;
  }
}

async function saveLibraryPath(path) {
  try {
    await api('/config', { method: 'PATCH', body: { library_path: path } });
    document.getElementById('path-verify').innerHTML =
      `<span style="color:var(--green)">✓ Path saved: ${esc(path)}</span>`;
    setSaveStatus('✓ Library path saved', true);
    toast('Library path saved', 'ok');
  } catch (e) { toast(`Failed to save: ${e.message}`, 'err'); }
}
function clearPathVerify() {
  const el=document.getElementById('path-verify'); el.className='path-verify'; el.style.display='none';
}
function toggleBrowser() {
  const b=document.getElementById('folder-browser');
  if (b.classList.contains('open')) { b.classList.remove('open'); return; }
  fbNavigate(document.getElementById('cfg-library-path').value.trim() || '~');
}
async function fbNavigate(path) {
  try {
    const r = await api(`/config/browse?path=${encodeURIComponent(path)}`);
    fbCurrentPath = r.current;
    document.getElementById('fb-path').textContent = r.current;
    document.getElementById('folder-browser').classList.add('open');
    document.getElementById('fb-entries').innerHTML =
      `<div class="fb-entry" style="color:var(--accent2);font-weight:600"
        onclick="fbSelect('${esc(r.current)}')">✓ Select this folder</div>`
      + r.entries.map(e =>
        `<div class="fb-entry" onclick="fbNavigate('${esc(e.path)}')">
          <span style="color:var(--amber)">📁</span> ${esc(e.name)}</div>`
      ).join('');
  } catch (e) { toast(`Browse error: ${e.message}`, 'err'); }
}
function fbUp() {
  api(`/config/browse?path=${encodeURIComponent(fbCurrentPath)}`)
    .then(r => { if (r.parent) fbNavigate(r.parent); }).catch(()=>{});
}
function fbSelect(path) {
  document.getElementById('cfg-library-path').value = path;
  document.getElementById('folder-browser').classList.remove('open');
  clearPathVerify();
}

// Batch rename
async function runRename(dryRun) {
  const pattern = document.getElementById('cfg-rename-pattern').value.trim();
  const scope   = document.getElementById('cfg-rename-scope').value;
  const log     = document.getElementById('rename-log');
  const btn     = document.getElementById('btn-rename-apply');
  if (!dryRun && !confirm(`Apply rename?\n\nPattern: ${pattern}`)) return;
  log.textContent = dryRun ? 'Preview (no files modified):\n\n' : 'Applying…\n\n';
  log.classList.add('visible');
  if (!dryRun) btn.disabled = true;
  try {
    const resp = await fetch('/config/rename-all', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ dry_run:dryRun, pattern, scope }),
    });
    await streamSSE(resp, {
      log:   line => { log.textContent += line+'\n'; log.scrollTop=log.scrollHeight; },
      done:  msg  => { log.textContent += `\n✓ ${msg}\n`; toast(msg,'ok'); if (!dryRun) { loadBooks(); loadStats(); } },
      error: msg  => { log.textContent += `\n✗ ${msg}\n`; toast(msg,'err'); },
    });
  } catch (e) { log.textContent += `\n✗ Error: ${e.message}\n`; }
  finally { btn.disabled = false; }
}

// Debug
async function loadDebugInfo() {
  const panel=document.getElementById('debug-panel');
  const btn=document.getElementById('btn-debug-load');
  panel.classList.add('visible'); panel.innerHTML='Loading…'; btn.disabled=true;
  try {
    const d = await api('/debug');
    panel.innerHTML = [
      renderDebugSection('System',   { Python:d.system?.python, Platform:d.system?.platform, FastAPI:d.system?.fastapi }),
      renderDebugSection('Library',  { Status:d.library?.status, Path:d.library?.path, Files:d.library?.total_files },
        { Status:v=>v==='ok'?'ok':'err' }),
      renderDebugSection('Database', { Status:d.database?.status, Size:d.database?.size_human, Books:d.database?.tables?.books },
        { Status:v=>v==='ok'?'ok':'err' }),
      renderDebugSection('Dependencies', Object.fromEntries(
        Object.entries(d.deps||{}).map(([k,v])=>[k,v.available?`✓ v${v.version}`:`✗ ${v.note||''}`])
      ), Object.fromEntries(Object.keys(d.deps||{}).map(k=>[k,v=>v.startsWith('✓')?'ok':'warn']))),
    ].join('');
  } catch (e) {
    panel.innerHTML=`<span style="color:var(--red)">Failed: ${esc(e.message)}</span><br>
      <span style="color:var(--muted);font-size:.73rem">Debug endpoint requires debug mode.</span>`;
  } finally { btn.disabled=false; }
}
function renderDebugSection(title, rows, colorFns={}) {
  return `<div class="debug-section"><div class="debug-section-title">${esc(title)}</div>`
    + Object.entries(rows).filter(([,v])=>v!==undefined&&v!==null).map(([k,v])=>{
        const fn=colorFns[k]; const cls=fn?fn(String(v)):'';
        return `<div class="debug-row"><span class="debug-key">${esc(k)}</span>
          <span class="debug-val ${cls}">${esc(String(v))}</span></div>`;
      }).join('')+'</div>';
}

// ---------------------------------------------------------------------------
// SSE helper
// ---------------------------------------------------------------------------
async function streamSSE(resp, handlers) {
  const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf='';
  while (true) {
    const { done, value }=await reader.read(); if (done) break;
    buf += dec.decode(value,{stream:true});
    const parts=buf.split('\n\n'); buf=parts.pop();
    for (const part of parts) {
      const dl=part.split('\n').find(l=>l.startsWith('data:'));
      const el=part.split('\n').find(l=>l.startsWith('event:'));
      if (!dl) continue;
      const data=dl.slice(5).trim(); const evt=el?el.slice(6).trim():'log';
      if (handlers[evt]) handlers[evt](data);
    }
  }
}

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------
async function api(path, opts={}) {
  const { method='GET', body }=opts;
  const res=await fetch(path, {
    method, headers:body?{'Content-Type':'application/json'}:{},
    body:body?JSON.stringify(body):undefined,
  });
  if (!res.ok) {
    const err=await res.json().catch(()=>({detail:res.statusText}));
    throw new Error(err.detail||res.statusText);
  }
  if (res.status===204) return null;
  return res.json();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function typeIcon(t){ return {cbz:'📗',cbr:'📘',epub:'📕',pdf:'📄',mobi:'📙',azw3:'📙'}[t]||'📚'; }
function fmtSize(b){ if(b>1e9) return (b/1e9).toFixed(1)+' GB'; if(b>1e6) return (b/1e6).toFixed(1)+' MB'; return (b/1e3).toFixed(0)+' KB'; }
function esc(str){ return String(str??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
let _toastTimer;
function toast(msg,type='ok'){
  const el=document.getElementById('toast'); el.textContent=msg; el.className=`show ${type}`;
  clearTimeout(_toastTimer); _toastTimer=setTimeout(()=>el.classList.remove('show'),3500);
}

// ===========================================================================
// BATCH OPERATIONS PAGE
// ===========================================================================

let _batchAllBooks  = [];    // full list fetched once
let _batchFiltered  = [];    // after search filter
let _batchSelected  = new Set();

// ---------------------------------------------------------------------------
// Page navigation
// ---------------------------------------------------------------------------

function showBatch() {
  document.getElementById('library-page').classList.add('hidden');
  document.getElementById('settings-page').classList.remove('active');
  document.getElementById('batch-page').classList.add('active');
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  // Pre-load selection from library if any
  if (libSelection.size > 0) {
    _batchSelected = new Set(libSelection);
  }
  _initBatchPage();
}

function _initBatchPage() {
  loadBatchBooks();
  _populateBatchSources();
}

// ---------------------------------------------------------------------------
// Source dropdown
// ---------------------------------------------------------------------------

async function _populateBatchSources() {
  try {
    const sources = await api('/metadata/sources');
    const sel = document.getElementById('batch-source');
    if (!sel) return;
    sel.innerHTML = sources
      .filter(s => s.enabled)
      .map(s => {
        const warn = s.requires_key && !s.key_set ? ' ⚠' : '';
        return `<option value="${s.id}">${esc(s.label)}${warn}</option>`;
      }).join('');
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Section toggle
// ---------------------------------------------------------------------------

function toggleBatchSection(name) {
  const body    = document.getElementById(name + '-body');
  const toggle  = document.getElementById(name + '-toggle');
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  if (toggle) toggle.style.transform = collapsed ? 'rotate(-90deg)' : '';
}

// ---------------------------------------------------------------------------
// Book selector
// ---------------------------------------------------------------------------

async function loadBatchBooks() {
  const cat   = document.getElementById('batch-filter-cat')?.value   || '';
  const meta  = document.getElementById('batch-filter-meta')?.value  || '';

  const params = new URLSearchParams({ limit: 1000, sort: 'series' });
  if (cat) params.set('category', cat);

  try {
    let books = await api(`/books?${params}`);

    // Apply meta filter
    if (meta === 'no_meta') {
      // Books with no metadata_cache rows at all — we don't know without querying each
      // For now: show all and let the operation itself skip/handle
    } else if (meta === 'has_pinned') {
      // We'd need extra info — show all for now, batch op handles skip_existing
    }

    _batchAllBooks = books;
    filterBatchBooks();
  } catch { _batchAllBooks = []; filterBatchBooks(); }
}

function filterBatchBooks() {
  const q = (document.getElementById('batch-search')?.value || '').toLowerCase();
  _batchFiltered = q
    ? _batchAllBooks.filter(b =>
        (b.title||'').toLowerCase().includes(q) ||
        (b.series||'').toLowerCase().includes(q))
    : [..._batchAllBooks];
  _renderBatchBooks();
}

function _renderBatchBooks() {
  const el = document.getElementById('batch-book-list');
  if (!el) return;

  if (!_batchFiltered.length) {
    el.innerHTML = '<div class="book-check-item" style="color:var(--muted)">No books found</div>';
    _updateSelCount();
    return;
  }

  el.innerHTML = _batchFiltered.map(b => {
    const checked = _batchSelected.has(b.id) ? 'checked' : '';
    const sub     = b.series
      ? `${esc(b.series)}${b.volume != null ? ` T${String(b.volume).padStart(2,'0')}` : ''}`
      : '';
    return `<div class="book-check-item" onclick="toggleBatchBook(${b.id}, this)">
      <input type="checkbox" ${checked} onclick="event.stopPropagation();toggleBatchBook(${b.id},this.parentElement)">
      <span class="book-check-label">${esc(b.title)}</span>
      ${sub ? `<span class="book-check-sub">${sub}</span>` : ''}
    </div>`;
  }).join('');
  _updateSelCount();
}

function toggleBatchBook(id, rowEl) {
  if (_batchSelected.has(id)) {
    _batchSelected.delete(id);
    rowEl.querySelector('input').checked = false;
    rowEl.style.background = '';
  } else {
    _batchSelected.add(id);
    rowEl.querySelector('input').checked = true;
    rowEl.style.background = 'rgba(123,97,255,.08)';
  }
  _updateSelCount();
}

function selectAllBatch() {
  _batchFiltered.forEach(b => _batchSelected.add(b.id));
  _renderBatchBooks();
}
function selectNoneBatch() {
  _batchSelected.clear();
  _renderBatchBooks();
}
function invertBatchSelection() {
  _batchFiltered.forEach(b => {
    if (_batchSelected.has(b.id)) _batchSelected.delete(b.id);
    else _batchSelected.add(b.id);
  });
  _renderBatchBooks();
}

function _updateSelCount() {
  const el = document.getElementById('sel-count');
  if (el) el.textContent = `— ${_batchSelected.size} selected`;
}

function _getSelectedIds() {
  return [..._batchSelected];
}

// ---------------------------------------------------------------------------
// Batch scrape
// ---------------------------------------------------------------------------

async function runBatchScrape() {
  const ids = _getSelectedIds();
  if (!ids.length) { toast('Select at least one book first', 'err'); return; }

  const btn = document.getElementById('btn-batch-scrape');
  btn.disabled = true;

  const body = {
    book_ids:      ids,
    source:        document.getElementById('batch-source')?.value,
    auto_pin:      document.getElementById('batch-auto-pin')?.checked ?? true,
    min_score:     parseFloat(document.getElementById('batch-min-score')?.value) || 0,
    skip_existing: document.getElementById('batch-skip-existing')?.checked ?? true,
    query_field:   document.getElementById('batch-query-field')?.value || 'series',
  };

  _resetBatchUI('scrape');
  document.getElementById('scrape-log').classList.add('visible');
  document.getElementById('scrape-progress').classList.add('visible');

  const resp = await fetch('/batch/metadata/fetch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  await _runBatchSSE(resp, 'scrape', ids.length);
  btn.disabled = false;
  loadStats();
}

// ---------------------------------------------------------------------------
// Batch apply
// ---------------------------------------------------------------------------

async function runBatchApply() {
  const ids = _getSelectedIds();
  if (!ids.length) { toast('Select at least one book first', 'err'); return; }

  const btn = document.getElementById('btn-batch-apply');
  btn.disabled = true;

  const fields = [...document.querySelectorAll('.apply-field:checked')].map(el => el.value);
  if (!fields.length) { toast('Select at least one field to apply', 'err'); btn.disabled=false; return; }

  const body = {
    book_ids:     ids,
    fields,
    pinned_only:  document.getElementById('apply-pinned-only')?.checked ?? true,
  };

  _resetBatchUI('apply');
  document.getElementById('apply-log').classList.add('visible');
  document.getElementById('apply-progress').classList.add('visible');

  const resp = await fetch('/batch/metadata/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  await _runBatchSSE(resp, 'apply', ids.length);
  btn.disabled = false;
  loadBooks(); loadStats();
}

// ---------------------------------------------------------------------------
// Batch field edit
// ---------------------------------------------------------------------------

function addEditField() {
  const container = document.getElementById('edit-fields-list');
  const ALL_FIELDS = [
    ['series',    'Series',     'text'],
    ['language',  'Language',   'text'],
    ['publisher', 'Publisher',  'text'],
    ['year',      'Year',       'number'],
    ['category',  'Category',   'select:manga,comics,book,unknown'],
    ['country',   'Country',    'text'],
    ['pub_status','Pub. status','text'],
    ['title',     'Title',      'text'],
    ['volume',    'Volume',     'number'],
    ['score',     'Score',      'number'],
  ];

  const rowId = 'ef-' + Date.now();
  const row   = document.createElement('div');
  row.className = 'edit-field-row';
  row.id        = rowId;

  const optionsHtml = ALL_FIELDS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');

  row.innerHTML = `
    <select class="edit-key-sel" onchange="_updateEditFieldInput('${rowId}', this.value)">
      ${optionsHtml}
    </select>
    <input class="edit-value" type="text" placeholder="Value…">
    <button onclick="document.getElementById('${rowId}').remove()">✕</button>`;
  container.appendChild(row);
}

function _updateEditFieldInput(rowId, field) {
  const row = document.getElementById(rowId);
  if (!row) return;

  // Always target the value element by its class, never by tag position
  const current = row.querySelector('.edit-value');
  if (!current) return;

  const typeMap = {
    year:     'number',
    volume:   'number',
    score:    'number',
    category: 'select:manga,comics,book,unknown',
  };
  const t = typeMap[field] || 'text';

  if (t.startsWith('select:')) {
    // Replace input with a <select>
    const opts = t.slice(7).split(',').map(v => `<option value="${v}">${v}</option>`).join('');
    const sel  = document.createElement('select');
    sel.className = 'edit-value';
    sel.innerHTML = opts;
    current.replaceWith(sel);
  } else {
    // Ensure it's an input (not a leftover select from a previous category choice)
    if (current.tagName === 'SELECT') {
      const inp = document.createElement('input');
      inp.className   = 'edit-value';
      inp.type        = t;
      inp.placeholder = 'Value…';
      current.replaceWith(inp);
    } else {
      current.type        = t;
      current.placeholder = 'Value…';
      current.value       = '';
    }
  }
}

function _collectEditFields() {
  const rows  = document.querySelectorAll('.edit-field-row');
  const edits = {};
  rows.forEach(row => {
    const keyEl = row.querySelector('.edit-key-sel');
    const valEl = row.querySelector('.edit-value');
    if (!keyEl || !valEl) return;
    const key = keyEl.value;
    const raw = valEl.value.trim();
    if (!raw) return;
    edits[key] = ['year', 'volume', 'score'].includes(key) ? Number(raw) : raw;
  });
  return edits;
}

async function previewBatchEdit() {
  const ids   = _getSelectedIds();
  const edits = _collectEditFields();
  if (!ids.length) { toast('Select books first', 'err'); return; }
  if (!Object.keys(edits).length) { toast('Add at least one field to edit', 'err'); return; }

  try {
    const preview = await api('/batch/preview', {
      method: 'POST', body: { book_ids: ids, edits },
    });
    const el = document.getElementById('edit-preview');
    el.style.display = 'block';
    if (!preview.items.length) {
      el.innerHTML = '<span style="color:var(--muted)">No changes to make</span>'; return;
    }
    el.innerHTML = preview.items.map(item => {
      const changes = Object.entries(item.changes)
        .map(([k, c]) => `<span style="color:var(--muted)">${esc(k)}:</span> ${esc(String(c.from))} → <strong style="color:var(--accent2)">${esc(String(c.to))}</strong>`)
        .join('&emsp;');
      return `<div style="padding:.2rem 0;border-bottom:1px solid rgba(255,255,255,.05)">
        <span style="font-weight:600">${esc(item.series || item.title || '#'+item.id)}</span>
        &nbsp; ${changes}</div>`;
    }).join('');
  } catch (e) { toast(`Preview failed: ${e.message}`, 'err'); }
}

async function runBatchEdit() {
  const ids   = _getSelectedIds();
  const edits = _collectEditFields();
  if (!ids.length) { toast('Select books first', 'err'); return; }
  if (!Object.keys(edits).length) { toast('Add at least one field to edit', 'err'); return; }
  if (!confirm(`Apply edits to ${ids.length} book(s)?`)) return;

  const btn = document.getElementById('btn-batch-edit');
  btn.disabled = true;
  _resetBatchUI('edit');
  document.getElementById('edit-log').classList.add('visible');
  document.getElementById('edit-progress').classList.add('visible');

  const resp = await fetch('/batch/metadata/edit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_ids: ids, edits }),
  });
  await _runBatchSSE(resp, 'edit', ids.length);
  btn.disabled = false;
  loadBooks(); loadStats();
}

// ---------------------------------------------------------------------------
// Batch delete
// ---------------------------------------------------------------------------

async function runBatchDelete() {
  const ids = _getSelectedIds();
  if (!ids.length) { toast('Select books first', 'err'); return; }
  const keep = document.getElementById('del-keep-manual')?.checked ?? true;
  const msg  = keep
    ? `Delete scraped metadata for ${ids.length} book(s)? Manual entries will be kept.`
    : `Delete ALL metadata (including manual) for ${ids.length} book(s)?`;
  if (!confirm(msg)) return;

  const btn = document.getElementById('btn-batch-del');
  btn.disabled = true;
  _resetBatchUI('del');
  document.getElementById('del-log').classList.add('visible');
  document.getElementById('del-progress').classList.add('visible');

  const resp = await fetch('/batch/metadata/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_ids: ids, keep_manual: keep }),
  });
  await _runBatchSSE(resp, 'del', ids.length);
  btn.disabled = false;
}

// ---------------------------------------------------------------------------
// SSE runner (shared)
// ---------------------------------------------------------------------------

async function _runBatchSSE(resp, prefix, total) {
  const logEl  = document.getElementById(prefix + '-log');
  const barEl  = document.getElementById(prefix + '-progress-bar');
  const sumEl  = document.getElementById(prefix + '-summary');

  if (!resp.body) { if (logEl) logEl.textContent += 'Streaming not supported\n'; return; }
  const reader = resp.body.getReader();
  const dec    = new TextDecoder();
  let buf      = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n'); buf = parts.pop();

    for (const part of parts) {
      const dataLine  = part.split('\n').find(l => l.startsWith('data:'));
      const eventLine = part.split('\n').find(l => l.startsWith('event:'));
      if (!dataLine) continue;

      const raw  = dataLine.slice(5).trim();
      const evt  = eventLine ? eventLine.slice(6).trim() : 'log';
      let payload;
      try { payload = JSON.parse(raw); } catch { payload = { msg: raw }; }

      if (evt === 'progress' && barEl) {
        const pct = total ? Math.round((payload.done / total) * 100) : 0;
        barEl.style.width = pct + '%';
      } else if (evt === 'log' && logEl) {
        const color = payload.level === 'error' ? '\x1b[31m'
                    : payload.level === 'warn'  ? '\x1b[33m' : '';
        logEl.textContent += (payload.msg || '') + '\n';
        logEl.scrollTop = logEl.scrollHeight;
      } else if (evt === 'done' && sumEl) {
        const s = payload;
        sumEl.textContent =
          `✓ Done — ${s.ok ?? ''} ok${s.skipped != null ? `, ${s.skipped} skipped` : ''}${s.failed ? `, ${s.failed} failed` : ''}`;
        sumEl.classList.add('visible');
        if (barEl) barEl.style.width = '100%';
        toast('Batch operation complete', 'ok');
      } else if (evt === 'error' && logEl) {
        logEl.textContent += `✗ ${payload.msg}\n`;
        toast(`Error: ${payload.msg}`, 'err');
      }
    }
  }
}

function _resetBatchUI(prefix) {
  const logEl = document.getElementById(prefix + '-log');
  const barEl = document.getElementById(prefix + '-progress-bar');
  const sumEl = document.getElementById(prefix + '-summary');
  if (logEl) { logEl.textContent = ''; logEl.classList.remove('visible'); }
  if (barEl) { barEl.style.width = '0%'; }
  if (sumEl) { sumEl.textContent = ''; sumEl.classList.remove('visible'); }
  const progEl = document.getElementById(prefix + '-progress');
  if (progEl) progEl.classList.remove('visible');
}
