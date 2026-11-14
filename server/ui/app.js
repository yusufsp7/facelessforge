/* ContentForge UI — vanilla JS, no build step. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const api = async (path, opts = {}) => {
  const headers = { 'Content-Type': 'application/json',
    ...(opts.headers || {}) };
  const tok = localStorage.getItem('forge_token');
  if (tok) headers['X-Forge-Token'] = tok;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && !headers['X-Forge-Token']) {
    const t = await inlineTokenPrompt();  // prompt() tidak andal di UI embed
    if (t) {
      localStorage.setItem('forge_token', t.trim());
      headers['X-Forge-Token'] = t.trim();
      const retry = await fetch(path, { ...opts, headers });
      if (!retry.ok) throw new Error(`${retry.status} ${await retry.text()}`);
      return retry.status === 200 ? retry.json() : null;
    }
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 200 ? res.json() : null;
};

let state = { platforms: [], accounts: [], styles: [], assets: [], jobs: [],
              selectedStyle: null, genStatus: null, lives: [] };

/* ---------- tabs ---------- */
$$('.tab').forEach(btn => btn.addEventListener('click', () => {
  $$('.tab').forEach(b => b.classList.remove('active'));
  $$('.panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  $('#tab-' + btn.dataset.tab).classList.add('active');
  refreshAll();
}));

/* ---------- akun ---------- */
async function renderPlatforms() {
  const sel = $('#add-plat');
  if (sel && !sel.children.length) {
    for (const p of state.platforms) {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name; sel.appendChild(o);
    }
  }
  const wrap = $('#platform-cards'); wrap.innerHTML = '';
  for (const p of state.platforms) {
    const count = state.accounts.filter(a => a.platform === p.id).length;
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="title">${p.name}</div>
      <div class="sub">jadwal native: ${p.schedule_supported ? '✓' : '✗ (worker antre)'}
        · ${count} akun</div>
      <div class="row"><input type="text" class="label-input" data-label="${p.id}"
        placeholder="label (opsional)" style="min-width:0;flex:1">
      <button class="primary" data-connect="${p.id}">Hubungkan</button></div>
      <div class="hint" data-status="${p.id}"></div>`;
    wrap.appendChild(card);
  }
  wrap.querySelectorAll('[data-connect]').forEach(b =>
    b.addEventListener('click', async () => {
      const platform = b.dataset.connect;
      const labelInput = wrap.querySelector(`[data-label="${platform}"]`);
      const label = labelInput ? labelInput.value.trim() : '';
      const statusEl = wrap.querySelector(`[data-status="${platform}"]`);
      try {
        await api('/api/accounts/connect', { method: 'POST',
          body: JSON.stringify({ platform, label: label || null }) });
        if (statusEl) statusEl.textContent =
          '⏳ Chrome terbuka — silakan login di jendela Chrome…';
      } catch (e) {
        if (statusEl) statusEl.textContent = '❌ gagal: ' + e.message;
      }
    }));
}

/* live connect status (in-flight connects from the server) */
async function renderConnectStatus() {
  let flights = [];
  try { flights = await api('/api/connects'); } catch (e) { return; }
  for (const f of flights) {
    const el = document.querySelector(`[data-status="${f.platform}"]`);
    if (el) el.textContent =
      `⏳ ${f.detail || f.status} (${f.elapsed || 0}s) — selesaikan login di Chrome`;
  }
}

async function renderAccounts() {
  const tb = $('#accounts-table tbody'); tb.innerHTML = '';
  for (const a of state.accounts) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${a.platform}</td><td>${esc(a.label || '')}</td>
      <td>${esc(a.account_name || '—')}</td>
      <td><span class="badge ${a.connected ? 'ok' : 'no'}">
        ${a.connected ? 'terhubung' : 'putus'}</span></td>
      <td class="row">
        <button data-verify="${a.key}" class="ghost">Cek sesi</button>
        <button data-wipe="${a.key}" class="danger">Hapus</button>
      </td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('[data-verify]').forEach(b => b.addEventListener('click',
    async () => {
      b.textContent = '…'; 
      const r = await api(`/api/accounts/${encodeURIComponent(b.dataset.verify)}/verify`,
        { method: 'POST' });
      b.textContent = r.ok ? '✓ valid' : '✗ ' + (r.detail || 'invalid');
      setTimeout(() => (b.textContent = 'Cek sesi'), 5000);
    }));
  tb.querySelectorAll('[data-wipe]').forEach(b => b.addEventListener('click',
    async () => {
      if (!confirm('Hapus akun + profil browser?')) return;
      await api(`/api/accounts/${encodeURIComponent(b.dataset.wipe)}/wipe`,
        { method: 'POST' });
      refreshAll();
    }));
}

/* ---------- generator (faceless-shorts) ---------- */
async function renderStyles() {
  const wrap = $('#styles-grid');
  if (!wrap) return;
  wrap.innerHTML = '';
  if (!state.styles.length) {
    wrap.innerHTML = '<div class="hint">skill tidak tersedia di vendor/</div>';
    return;
  }
  for (const s of state.styles) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'style-card' +
      (state.selectedStyle === s.id ? ' selected' : '');
    card.innerHTML = `
      <div class="media">
        ${s.sheet ? `<img src="${s.sheet}" alt="${esc(s.label)}"
          loading="lazy">` : ''}
        <video class="hovervid" muted loop playsinline preload="none"
          data-style="${s.id}"></video>
        <div class="preview-note hint hidden" data-note="${s.id}"></div>
      </div>
      <div class="body">
        <div class="name">${esc(s.label)}</div>
        <div class="desc">${esc(s.description)}</div>
      </div>`;
    const vid = card.querySelector('video');
    const note = card.querySelector('[data-note]');
    let tried = false;
    card.addEventListener('mouseenter', async () => {
      if (!tried) {
        tried = true;
        vid.src = `/api/styles/${s.id}/preview.mp4`;
        vid.addEventListener('error', () => {
          vid.classList.remove('show');
          note.classList.remove('hidden');
          note.textContent = '⏳ preview dirender di background…';
          setTimeout(() => { tried = false; }, 15000);
        }, { once: true });
        vid.addEventListener('loadeddata', () => {
          note.classList.add('hidden');
        }, { once: true });
      }
      if (vid.src) {
        vid.classList.add('show');
        vid.play().catch(() => {});
      }
    });
    card.addEventListener('mouseleave', () => {
      vid.classList.remove('show');
      vid.pause();
    });
    card.addEventListener('click', () => {
      state.selectedStyle = s.id;
      renderStyles();
    });
    wrap.appendChild(card);
  }
}

async function renderVoices() {
  const sel = $('#gen-voice');
  if (!sel || sel.children.length) return;
  let voices = [];
  try { voices = await api('/api/generator/voices'); } catch (e) {}
  for (const v of voices) {
    const o = document.createElement('option');
    o.value = v.id;
    o.textContent = `${v.id} — ${v.desc}`;
    if (v.id === 'am_michael') o.selected = true;
    sel.appendChild(o);
  }
}

async function renderGenStatus() {
  const box = $('#gen-ready'), setupRow = $('#gen-setup-row');
  if (!box) return;
  try {
    const st = await api('/api/generator/status');
    state.genStatus = st;
    if (st.ready) {
      box.textContent = '✅ Siap — pilih style, tulis topik, generate.';
      setupRow.classList.add('hidden');
      return;
    }
    box.textContent = '⚠ ' + (st.hint || 'belum siap');
    setupRow.classList.remove('hidden');
    const log = (st.setup_log || []).slice(-3).join(' | ');
    $('#gen-setup-log').textContent = st.setup_running
      ? '⏳ setup berjalan… ' + log : (log || '');
  } catch (e) { box.textContent = 'status gagal: ' + e.message; }
}

async function renderGens() {
  const wrap = $('#gens-list');
  if (!wrap) return;
  let gens = [];
  try { gens = await api('/api/generations'); } catch (e) { return; }
  wrap.innerHTML = gens.length
    ? '<h3>Generasi terakhir</h3>' : '';
  for (const g of gens.slice(0, 8)) {
    const div = document.createElement('div');
    div.className = 'card';
    div.style.marginBottom = '8px';
    div.innerHTML = `
      <div class="title">${esc(g.style)} — ${esc((g.topic || '').slice(0, 60))}</div>
      <div class="sub"><span class="badge ${
        g.status === 'done' ? 'ok' : g.status === 'failed' ? 'bad' : 'warn'
      }">${g.status}</span> ${esc(g.detail || '')} ${g.elapsed || 0}s
        ${g.error ? '· ' + esc(g.error) : ''}
        ${g.asset_id ? `· <a href="/api/media/${g.asset_id}" target="_blank">lihat video</a>` : ''}</div>`;
    wrap.appendChild(div);
  }
}

async function generate() {
  const st = $('#gen-status');
  if (!state.selectedStyle) {
    st.textContent = '❌ pilih style dulu (klik kartu di atas)';
    return;
  }
  const topic = $('#gen-topic').value.trim();
  if (!topic) {
    st.textContent = '❌ tulis topiknya dulu';
    return;
  }
  try {
    const r = await api('/api/generate', { method: 'POST',
      body: JSON.stringify({
        style: state.selectedStyle, topic,
        voice: $('#gen-voice').value.trim() || 'am_michael',
        length_s: +$('#gen-length').value || 35,
        end_line: $('#gen-endline').value.trim(),
      }) });
    st.textContent = '⏳ generasi mulai (pipeline: script → voice → timing → render)…';
    pollGen(r.gen_id, st);
  } catch (e) {
    st.textContent = '❌ ' + e.message;
  }
}

async function pollGen(genId, st, tries = 1200) {
  for (let i = 0; i < tries; i++) {
    await new Promise(r => setTimeout(r, 3000));
    let g;
    try { g = await api(`/api/generations/${genId}`); } catch (e) { continue; }
    if (!g) continue;
    st.textContent = `⏳ [${g.status}] ${g.detail || ''} (${g.elapsed || 0}s)`;
    if (g.status === 'done') {
      st.textContent = '✅ Video jadi! Buka tab Konten untuk preview & jadwalkan upload.';
      refreshAll(); return;
    }
    if (g.status === 'failed') {
      st.textContent = '❌ ' + (g.error || 'gagal');
      return;
    }
  }
  st.textContent = '❌ timeout polling generation';
}

$('#btn-generator-status').addEventListener('click', renderGenStatus);
$('#btn-generator-setup').addEventListener('click', async () => {
  const r = await api('/api/generator/setup', { method: 'POST' });
  if (!r.started) { alert(r.detail || 'setup tidak bisa dimulai'); return; }
  $('#gen-setup-log').textContent = '⏳ setup berjalan… lihat log di sini';
  const t = setInterval(async () => {
    await renderGenStatus();
    const st2 = state.genStatus;
    if (st2 && st2.ready) { clearInterval(t); refreshAll(); }
  }, 5000);
});

/* ---------- konten ---------- */
async function renderAssets() {
  const wrap = $('#assets-grid'); wrap.innerHTML = '';
  for (const a of state.assets) {
    const card = document.createElement('div');
    card.className = 'card';
    const media = a.kind === 'audio' && a.path
      ? `<audio controls src="/api/media/${a.id}"></audio>`
      : a.kind === 'video' && a.path
        ? `<video controls src="/api/media/${a.id}"></video>`
        : a.kind === 'image' && a.path
          ? `<img src="/api/media/${a.id}" style="width:100%;border-radius:8px">`
          : `<a href="/api/media/${a.id}" target="_blank">Buka file</a>`;
    const qc = a.qc_status || 'approved';
    const qcBadge = `<div class="sub">QC: <span class="badge ${
      qc === 'approved' ? 'ok' : qc === 'rejected' ? 'bad' : 'warn'
    }">${qc}</span>
      ${qc === 'pending' && a.engine.startsWith('faceless-shorts') && a.path
        ? `<a class="hint" href="#" data-qcsheet="${a.id}">lihat contact sheet</a>` : ''}</div>`;
    card.innerHTML = `
      <div class="title">${esc(a.title || a.id)}</div>
      <div class="sub">${a.kind} · ${a.engine} · ${a.created_at}</div>
      ${media}${qcBadge}
      ${qc === 'pending'
        ? `<button class="primary" data-qc="${a.id}" data-v="1">QC lolos ✓</button>
           <button class="danger" data-qc="${a.id}" data-v="0">QC tolak ✗</button>`
        : `<button class="primary" data-sched="${a.id}">Jadwalkan upload</button>`}
      <button class="danger" data-del="${a.id}">Hapus</button>`;
    wrap.appendChild(card);
  }
  wrap.querySelectorAll('[data-sched]').forEach(b => b.addEventListener(
    'click', () => openSchedule(b.dataset.sched)));
  wrap.querySelectorAll('[data-del]').forEach(b => b.addEventListener(
    'click', async () => {
      if (!confirm('Hapus asset? (job terkait akan dibatalkan)')) return;
      await api(`/api/assets/${b.dataset.del}?force=true`, { method: 'DELETE' });
      refreshAll();
    }));
  wrap.querySelectorAll('[data-qc]').forEach(b => b.addEventListener(
    'click', async () => {
      await api(`/api/assets/${b.dataset.qc}/qc?approved=${b.dataset.v}`,
        { method: 'POST' });
      refreshAll();
    }));
  wrap.querySelectorAll('[data-qcsheet]').forEach(a => a.addEventListener(
    'click', async (e) => {
      e.preventDefault();
      const assets = await api('/api/assets');
      const a2 = assets.find(x => x.id === a.dataset.qcsheet);
      const meta = typeof a2?.meta_json === 'string'
        ? JSON.parse(a2.meta_json) : (a2?.meta_json || {});
      if (meta.qc_sheet) window.open(`/api/preview-file?path=${encodeURIComponent(meta.qc_sheet)}`);
    }));
}

/* ---------- jadwal (modal) ---------- */
let schedAssetId = null;

function openSchedule(assetId) {
  schedAssetId = assetId;
  const wrap = $('#sched-accounts'); wrap.innerHTML = '<b>Akun tujuan:</b>';
  const supportedPlats = new Set(state.platforms
    .filter(p => p.schedule_supported).map(p => p.id));
  for (const a of state.accounts.filter(x => x.connected)) {
    const label = document.createElement('label');
    label.className = 'check';
    label.innerHTML = `<input type="checkbox" value="${a.key}" checked>
      ${a.platform} — ${esc(a.label || a.key)}`;
    wrap.appendChild(label);
  }
  if (!wrap.querySelectorAll('input').length)
    wrap.innerHTML += '<div class="hint">Belum ada akun terhubung!</div>';
  const anyNative = $$('#sched-accounts input:checked').some(i => {
    const acc = state.accounts.find(x => x.key === i.value);
    return acc && supportedPlats.has(acc.platform);
  });
  $('#sched-native-wrap').classList.toggle('hidden', !anyNative);
  $('#schedule-modal').classList.remove('hidden');
}

$('#btn-sched-cancel').addEventListener('click', () =>
  $('#schedule-modal').classList.add('hidden'));

$('#btn-sched-save').addEventListener('click', async () => {
  const keys = $$('#sched-accounts input:checked').map(i => i.value);
  if (!keys.length) { alert('pilih minimal 1 akun'); return; }
  const body = {
    asset_id: schedAssetId,
    account_keys: keys,
    mode: $('#sched-mode').value,
    schedule_at: $('#sched-at').value || null,
    visibility: $('#sched-visibility').value,
    caption: {
      title: $('#sched-title').value,
      description: $('#sched-desc').value,
      tags: $('#sched-tags').value.split(',').map(s => s.trim()).filter(Boolean),
      schedule_native: $('#sched-native').checked,
    },
  };
  const r = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
  $('#schedule-modal').classList.add('hidden');
  alert(`✅ ${r.created.length} job dibuat`);
  refreshAll();
});

/* ---------- jadwal (tabel) ---------- */
async function renderJobs() {
  const tb = $('#jobs-table tbody'); tb.innerHTML = '';
  for (const j of state.jobs) {
    const asset = state.assets.find(a => a.id === j.asset_id);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${j.id}</td>
      <td>${esc(asset ? asset.title : j.asset_id)}</td>
      <td>${esc(j.account_key)}</td>
      <td>${j.schedule_at}</td>
      <td><span class="badge ${
        j.status === 'done' ? 'ok' : j.status === 'failed' ? 'bad'
        : j.status === 'running' ? 'warn' : 'no'}">${j.status}</span>
        ${j.error ? `<div class="hint">${esc(j.error)}</div>` : ''}</td>
      <td><progress value="${j.progress}" max="100"></progress></td>
      <td class="row">
        ${['queued', 'retrying'].includes(j.status)
          ? `<button data-cancel="${j.id}" class="ghost">Cancel</button>` : ''}
        ${['failed', 'cancelled'].includes(j.status)
          ? `<button data-retry="${j.id}" class="ghost">Retry</button>` : ''}
      </td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('[data-cancel]').forEach(b => b.addEventListener(
    'click', async () => {
      await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: 'POST' });
      refreshAll();
    }));
  tb.querySelectorAll('[data-retry]').forEach(b => b.addEventListener(
    'click', async () => {
      await api(`/api/jobs/${b.dataset.retry}/retry`, { method: 'POST' });
      refreshAll();
    }));
}

/* ---------- riwayat ---------- */
async function renderHistory() {
  const rows = await api('/api/history?limit=100');
  const tb = $('#history-table tbody'); tb.innerHTML = '';
  for (const h of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${h.at}</td><td>${h.event}</td>
      <td class="hint">${esc(h.detail_json)}</td>`;
    tb.appendChild(tr);
  }
}

/* ---------- settings (kosong — engine auto-detect vendor/) ---------- */

/* ---------- update (GitHub self-updater) ---------- */
async function refreshUpdate() {
  const btn = $('#btn-update'), chk = $('#update-auto');
  if (!btn) return;
  let st;
  try { st = await api('/api/update'); } catch (e) { return; }
  if (!st.supported) {
    btn.style.display = 'none';
    $('#update-auto-label').style.display = 'none';
    return;
  }
  chk.checked = !!st.auto;
  btn.classList.toggle('busy', !!st.updating);
  btn.classList.toggle('updates', !!st.updates && !st.updating);
  btn.textContent = st.updating ? '⏳ Update…'
    : st.updates ? `⬆ Update (${st.behind} baru)`
    : '⬆ Update';
  if (st.updates) btn.title = `${st.behind} commit baru di GitHub (origin: ${st.remote_head || '?'}) — klik untuk pasang & restart`;
  else if (!st.updating) btn.title = 'Cek & pasang update dari GitHub';
  if (st.applied) {
    btn.textContent = '✓ terpasang';
    btn.classList.remove('busy', 'updates');
  }
}

$('#btn-update').addEventListener('click', async () => {
  const btn = $('#btn-update');
  if (btn.classList.contains('busy')) return;
  btn.classList.add('busy');
  btn.textContent = '⏳ cek…';
  let st;
  try { st = await api('/api/update/check', { method: 'POST' }); }
  catch (e) { btn.textContent = '❌ cek gagal'; btn.classList.remove('busy'); return; }
  if (st.error) {
    btn.textContent = '❌ ' + (st.error || 'gagal'); btn.classList.remove('busy');
    return;
  }
  if (!st.updates) {
    btn.textContent = '✓ sudah terbaru';
    setTimeout(() => { btn.textContent = '⬆ Update'; btn.classList.remove('busy'); }, 3000);
    return;
  }
  if (!confirm(`Ada ${st.behind} commit baru di GitHub. Pasang update & restart server sekarang?`)) {
    refreshUpdate(); return;
  }
  btn.textContent = '⏳ memperbarui & restart…';
  const r = await api('/api/update/apply', { method: 'POST' });
  if (!r.ok) { btn.textContent = '❌ ' + (r.error || 'gagal'); btn.classList.remove('busy'); return; }
  waitRestart(st.local_head);
});

async function waitRestart(oldHead, tries = 180) {
  for (let i = 0; i < tries; i++) {
    await new Promise(r => setTimeout(r, 2000));
    let st;
    try { st = await api('/api/update'); } catch (e) { continue; } // server sedang restart
    if (st.local_head && st.local_head !== oldHead) {
      $('#btn-update').textContent = '✓ terbaru — memuat ulang…';
      location.reload();
      return;
    }
  }
  $('#btn-update').textContent = '❌ timeout — cek manual';
  $('#btn-update').classList.remove('busy');
}

$('#update-auto').addEventListener('change', async (e) => {
  await api('/api/update/auto', { method: 'POST',
    body: JSON.stringify({ enabled: e.target.checked }) });
  refreshUpdate();
});

/* ---------- tambah akun placeholder ---------- */
$('#btn-add-account').addEventListener('click', async () => {
  const platform = $('#add-plat').value;
  const label = $('#add-label').value.trim();
  if (!label) { alert('isi label akun dulu'); return; }
  const r = await api('/api/accounts/add', { method: 'POST',
    body: JSON.stringify({ platform, label,
      channel_id: $('#add-ch').value.trim() }) });
  alert(`akun ${r.key} dibuat — impor cookies lewat tombol akun, atau Hubungkan untuk login manual`);
  refreshAll();
});

/* ---------- live ---------- */
async function renderLive() {
  const wrap = $('#live-cards'); wrap.innerHTML = '';
  const ytAccounts = state.accounts.filter(a => a.platform === 'youtube'
    && a.connected);
  if (!ytAccounts.length) {
    wrap.innerHTML = '<div class="hint">Belum ada akun YouTube terhubung.</div>';
    return;
  }
  for (const a of ytAccounts) {
    const p = state.lives.find(x => x.account_key === a.key) || {};
    const st = p.status || {};
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="title">${esc(a.label || a.key)} <span class="badge ${
        st.data_flowing ? 'ok' : st.encoder_alive ? 'warn' : 'no'
      }">${st.data_flowing ? 'MENGALIR' : st.encoder_alive ? 'encoder hidup, data belum mengalir'
        : 'mati'}</span></div>
      <label>Stream key (rahasia)
        <input type="password" data-live-sk="${a.key}"
          placeholder="${p.stream_key_masked || 'belum diset'}"></label>
      <label>Ingest
        <select data-live-ingest="${a.key}">
          <option value="rtmp://a.rtmp.youtube.com/live2">a.rtmp (utama)</option>
          <option value="rtmp://b.rtmp.youtube.com/live2?backup=1">b.rtmp (cadangan)</option>
        </select></label>
      <label>File media sumber <input type="text" data-live-media="${a.key}"
        value="${esc(p.media_path || '')}" placeholder="/path/pluto.mp4"></label>
      <label class="check"><input type="checkbox" data-live-loop="${a.key}"
        ${p.loop ? 'checked' : ''}> loop media</label>
      <label class="check"><input type="checkbox" data-live-auto="${a.key}"
        ${p.desired_on ? 'checked' : ''}> auto (lanjut sendiri setelah reboot)</label>
      <div class="sub">broadcast: ${esc(p.broadcast_id || '—')} ·
        restart: ${st.restarts || 0} · bytes: ${st.bytes || 0}</div>
      <div class="row">
        <button class="primary" data-live-save="${a.key}">Simpan profil</button>
        <button data-live-start="${a.key}">▶ Start</button>
        <button class="danger" data-live-stop="${a.key}">■ Stop</button>
        <button class="ghost" data-live-bc="${a.key}">Buat broadcast (Studio)</button>
        <button class="ghost" data-live-log="${a.key}">Log</button>
      </div>`;
    wrap.appendChild(card);
  }
  wrap.querySelectorAll('[data-live-save]').forEach(b => b.addEventListener(
    'click', async () => {
      const key = b.dataset.liveSave;
      await api(`/api/live/${encodeURIComponent(key)}`,
        { method: 'PUT', body: JSON.stringify({
          stream_key: wrap.querySelector(`[data-live-sk="${key}"]`).value,
          ingest_url: wrap.querySelector(`[data-live-ingest="${key}"]`).value,
          media_path: wrap.querySelector(`[data-live-media="${key}"]`).value,
          loop: wrap.querySelector(`[data-live-loop="${key}"]`).checked,
          desired_on: wrap.querySelector(`[data-live-auto="${key}"]`).checked,
        }) });
      alert('profil live tersimpan'); refreshAll();
    }));
  wrap.querySelectorAll('[data-live-start]').forEach(b => b.addEventListener(
    'click', async () => {
      const r = await api(`/api/live/${encodeURIComponent(b.dataset.liveStart)}/start`,
        { method: 'POST' });
      if (r.error) alert('❌ ' + r.error); else refreshAll();
    }));
  wrap.querySelectorAll('[data-live-stop]').forEach(b => b.addEventListener(
    'click', async () => {
      await api(`/api/live/${encodeURIComponent(b.dataset.liveStop)}/stop`,
        { method: 'POST' });
      refreshAll();
    }));
  wrap.querySelectorAll('[data-live-bc]').forEach(b => b.addEventListener(
    'click', async () => {
      b.textContent = '⏳ Studio…';
      const r = await api(`/api/live/${encodeURIComponent(b.dataset.liveBc)}/ensure-broadcast`,
        { method: 'POST' });
      b.textContent = 'Buat broadcast (Studio)';
      alert(JSON.stringify(r));
    }));
  wrap.querySelectorAll('[data-live-log]').forEach(b => b.addEventListener(
    'click', async () => {
      const r = await api(`/api/live/${encodeURIComponent(b.dataset.liveLog)}/log?n=15`);
      $('#live-log').textContent = (r.lines || []).join('\n') || '(kosong)';
    }));
}

/* ---------- token inline (401 non-localhost) ---------- */
function inlineTokenPrompt() {
  return new Promise((resolve) => {
    const box = document.createElement('div');
    box.className = 'modal';
    box.innerHTML = `<div class="modal-body">
      <h3>Token API dibutuhkan</h3>
      <p class="hint">Server menuntut header X-Forge-Token (non-localhost).
      Token ada di log server pertama kali (data/.api-token).</p>
      <input type="text" id="tok-input" style="width:100%">
      <div class="row end"><button id="tok-ok" class="primary">Simpan</button></div>
    </div>`;
    document.body.appendChild(box);
    const done = (v) => { box.remove(); resolve(v); };
    box.querySelector('#tok-ok').onclick = () => done(box.querySelector('#tok-input').value);
    box.querySelector('#tok-input').focus();
  });
}

/* ---------- helpers ---------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
            "'": '&#39;' }[c]));
}

async function refreshAll() {
  try {
    const [platforms, accounts, styles, assets, jobs, lives] =
      await Promise.all([
        api('/api/platforms'), api('/api/accounts'), api('/api/styles'),
        api('/api/assets'), api('/api/jobs'), api('/api/live')]);
    state.platforms = platforms; state.accounts = accounts;
    state.styles = styles; state.assets = assets; state.jobs = jobs;
    state.lives = lives;
    if (document.querySelector('#tab-akun.active')) {
      if (!$('#platform-cards').children.length) renderPlatforms();
      renderAccounts();
      renderConnectStatus();
    }
    if (document.querySelector('#tab-generator.active')) {
      if (!$('#styles-grid').children.length) renderStyles();
      renderVoices();
      renderGenStatus();
      renderGens();
    }
    if (document.querySelector('#tab-konten.active')) renderAssets();
    if (document.querySelector('#tab-jadwal.active')) renderJobs();
    if (document.querySelector('#tab-riwayat.active')) renderHistory();
    if (document.querySelector('#tab-live.active')) renderLive();
    $('#conn-status').textContent =
      `${accounts.filter(a => a.connected).length} akun terhubung · ` +
      `${assets.length} asset · ${jobs.filter(j => j.status === 'queued').length} job antre`;
  } catch (e) {
    $('#conn-status').textContent = 'offline: ' + e.message;
  }
}

$('#btn-refresh-accounts').addEventListener('click', refreshAll);
$('#btn-refresh-assets').addEventListener('click', refreshAll);
$('#btn-refresh-jobs').addEventListener('click', refreshAll);
$('#btn-refresh-history').addEventListener('click', refreshAll);
$('#btn-generate').addEventListener('click', generate);
$('#sched-mode').addEventListener('change', () => {
  $('#sched-at').disabled = $('#sched-mode').value === 'now';
});
$('#sched-at').disabled = true;

setInterval(() => {
  if (document.querySelector('#tab-jadwal.active')) refreshAll();
  if (document.querySelector('#tab-akun.active')) renderConnectStatus();
  if (document.querySelector('#tab-generator.active')) renderGens();
}, 3000);

refreshAll();
refreshUpdate();
// badge update tetap segar walau tidak sedang di tab manapun
setInterval(refreshUpdate, 120000);
