// Zaein Drive Token — Cloudflare Worker
// Serves index.html directly from Worker

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
};

const SUPABASE_URL_DEFAULT = 'https://awfpxjwfjtyovbrpbcar.supabase.co';
const SUPABASE_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF3ZnB4andmanR5b3ZicnBiY2FyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYwMTMyODQsImV4cCI6MjA5MTU4OTI4NH0.f6xxjNwCJ8sVhzh_lprzmNs6kU-SQlr12sMM3mtpNzI';
// Settings / reports / OAuth state tetap pakai project default supaya feature lama tidak rusak.
const SUPABASE_URL = SUPABASE_URL_DEFAULT;
const ADMIN_URL = 'https://admin2.zaeinstream.workers.dev/';

function sbHeaders() {
  return {
    'apikey': SUPABASE_ANON,
    'Authorization': 'Bearer ' + SUPABASE_ANON,
    'Content-Type': 'application/json',
  };
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Batch threshold: only send email every N new reports (saves Resend quota)
const REPORT_EMAIL_BATCH_SIZE = 5;

async function _getSetting(key) {
  try {
    const r = await fetch(SUPABASE_URL + '/rest/v1/settings?key=eq.' + encodeURIComponent(key) + '&select=value', { headers: sbHeaders() });
    if (!r.ok) return null;
    const arr = await r.json();
    if (arr && arr.length) return arr[0].value;
  } catch {}
  return null;
}

async function _setSetting(key, value) {
  try {
    await fetch(SUPABASE_URL + '/rest/v1/settings?on_conflict=key', {
      method: 'POST',
      headers: { ...sbHeaders(), 'Prefer': 'resolution=merge-duplicates,return=minimal' },
      body: JSON.stringify({ key, value, updated_at: new Date().toISOString() }),
    });
  } catch {}
}

async function handleReport(request) {
  let body;
  try { body = await request.json(); }
  catch { return jsonError(400, 'invalid json'); }
  const name = (body.name || '').toString().trim();
  const drive_id = (body.drive_id || '').toString().trim();
  const kind = (body.kind || 'folder').toString().trim();
  if (!name || !drive_id) return jsonError(400, 'name & drive_id required');
  if (kind !== 'folder' && kind !== 'file') return jsonError(400, 'kind must be folder or file');

  const drive_link = kind === 'folder'
    ? 'https://drive.google.com/drive/folders/' + drive_id + '?usp=drive_link'
    : 'https://drive.google.com/file/d/' + drive_id + '/view?usp=drive_link';

  // Prevent duplicate open report for same drive_id
  try {
    const dupRes = await fetch(SUPABASE_URL + '/rest/v1/reports?status=eq.open&drive_id=eq.' + encodeURIComponent(drive_id) + '&select=id&limit=1', { headers: sbHeaders() });
    if (dupRes.ok) {
      const arr = await dupRes.json();
      if (arr && arr.length) {
        return new Response(JSON.stringify({ ok: true, duplicate: true, message: 'Sudah ada laporan terbuka untuk item ini.' }), {
          headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
        });
      }
    }
  } catch {}

  // Insert
  const insertRes = await fetch(SUPABASE_URL + '/rest/v1/reports', {
    method: 'POST',
    headers: { ...sbHeaders(), 'Prefer': 'return=representation' },
    body: JSON.stringify({ name, drive_id, drive_link, kind, status: 'open' }),
  });
  if (!insertRes.ok) {
    const t = await insertRes.text();
    return jsonError(500, 'insert failed: ' + t);
  }

  // Fetch all open reports (in order)
  let openReports = [];
  try {
    const listRes = await fetch(SUPABASE_URL + '/rest/v1/reports?status=eq.open&order=created_at.asc&select=name,kind,drive_link', { headers: sbHeaders() });
    if (listRes.ok) openReports = await listRes.json();
  } catch {}

  // Increment pending counter (settings.report_pending_count)
  // We send email only when pending reaches REPORT_EMAIL_BATCH_SIZE, then reset to 0.
  let pending = 0;
  const pendingRaw = await _getSetting('report_pending_count');
  if (pendingRaw) { const n = parseInt(pendingRaw); if (!isNaN(n)) pending = n; }
  pending = pending + 1;

  let emailStatus = 'queued';
  let batchSent = false;

  if (pending >= REPORT_EMAIL_BATCH_SIZE) {
    // Read resend_config from settings
    let cfg = null;
    try {
      const cfgRaw = await _getSetting('resend_config');
      if (cfgRaw) { try { cfg = JSON.parse(cfgRaw); } catch {} }
    } catch {}

    if (cfg && cfg.api_key && cfg.from_email && cfg.to_email) {
      // Send the LATEST `pending` reports as the batch (most recent first)
      let batch = [];
      try {
        const lr = await fetch(SUPABASE_URL + '/rest/v1/reports?status=eq.open&order=created_at.desc&limit=' + pending + '&select=name,kind,drive_link,created_at', { headers: sbHeaders() });
        if (lr.ok) batch = await lr.json();
      } catch {}
      // Show in chronological order in the email
      batch = batch.reverse();

      const lines = batch.map(function (r, i) { return (i + 1) + '. ' + r.name; }).join('\n');
      const linesHtml = batch.map(function (r) {
        return '<li style="margin-bottom:6px;"><b>' + escapeHtml(r.name) + '</b><br><a href="' + escapeHtml(r.drive_link) + '" style="color:#7c3aed;font-size:12px;font-family:monospace;">' + escapeHtml(r.drive_link) + '</a></li>';
      }).join('');
      const subject = '🚨 ' + batch.length + ' Laporan Drive Baru — total ' + openReports.length + ' belum diselesaikan';
      const text = 'Ada ' + batch.length + ' laporan baru:\n\n' + lines + '\n\nTotal belum diselesaikan: ' + openReports.length + '\n\nBuka admin: ' + ADMIN_URL;
      const html = '<div style="font-family:system-ui,sans-serif;max-width:560px;">'
        + '<h2 style="margin:0 0 12px;color:#7c3aed;">🚨 ' + batch.length + ' laporan baru terkumpul</h2>'
        + '<p style="margin:0 0 12px;">Berikut <b>' + batch.length + '</b> item terbaru yang dilaporkan (total <b>' + openReports.length + '</b> belum diselesaikan):</p>'
        + '<ol style="font-size:14px;line-height:1.8;background:#f7f5ff;padding:14px 14px 14px 32px;border-radius:8px;border-left:4px solid #7c3aed;">'
        + linesHtml
        + '</ol>'
        + '<p style="margin:16px 0 0;"><a href="' + ADMIN_URL + '" style="background:#7c3aed;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;">Buka Admin Panel</a></p>'
        + '<p style="margin:14px 0 0;color:#888;font-size:11px;">Email ini dikirim setiap ' + REPORT_EMAIL_BATCH_SIZE + ' laporan baru, bukan per laporan, untuk hemat kuota Resend.</p>'
        + '</div>';

      try {
        const r = await fetch('https://api.resend.com/emails', {
          method: 'POST',
          headers: {
            'Authorization': 'Bearer ' + cfg.api_key,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            from: cfg.from_email,
            to: cfg.to_email,
            subject,
            html,
            text,
          }),
        });
        emailStatus = r.ok ? 'sent' : 'failed:' + r.status;
        batchSent = r.ok;
      } catch (e) { emailStatus = 'error:' + (e.message || e); }
    } else {
      emailStatus = 'no_config';
    }
    // Reset counter regardless of email result if we attempted to send (avoid stuck state)
    if (batchSent || emailStatus === 'no_config') {
      await _setSetting('report_pending_count', '0');
      pending = 0;
    }
    // If failed (network/transient), keep pending as-is so next report retries
    if (!batchSent && emailStatus !== 'no_config') {
      await _setSetting('report_pending_count', String(pending));
    }
  } else {
    // Not yet at threshold — just persist new pending count
    await _setSetting('report_pending_count', String(pending));
    emailStatus = 'queued:' + pending + '/' + REPORT_EMAIL_BATCH_SIZE;
  }

  return new Response(JSON.stringify({
    ok: true,
    total_open: openReports.length,
    pending,
    batch_size: REPORT_EMAIL_BATCH_SIZE,
    email: emailStatus,
  }), {
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}

function jsonError(status, msg) {
  return new Response(JSON.stringify({ error: msg }), {
    status,
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}

function envValue(env, key, fallback = '') {
  try {
    if (env && typeof env[key] !== 'undefined' && env[key] !== null) return String(env[key]);
  } catch {}
  return fallback || '';
}

function jsonOk(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}

async function handleStreamConfig(env) {
  // URL & key boleh di-override lewat Cloudflare Variables. Kalau user set
  // WEBSTREAM_SUPABASE_URL / WEBSTREAM_SUPABASE_SERVICE_KEY, value itu yang
  // dipakai. Kalau tidak ada, fallback ke SUPABASE_URL / SUPABASE_SERVICE_KEY,
  // dan terakhir ke project default + anon key supaya dropdown tetap muncul.
  const envUrlRaw = envValue(env, 'WEBSTREAM_SUPABASE_URL') || envValue(env, 'SUPABASE_URL');
  const envKey = envValue(env, 'WEBSTREAM_SUPABASE_SERVICE_KEY') || envValue(env, 'SUPABASE_SERVICE_KEY');
  const supabaseUrl = (envUrlRaw || SUPABASE_URL_DEFAULT).replace(/\/$/, '');
  const supabaseKey = envKey || SUPABASE_ANON;
  const supabaseKeySource = envValue(env, 'WEBSTREAM_SUPABASE_SERVICE_KEY')
    ? 'WEBSTREAM_SUPABASE_SERVICE_KEY'
    : envValue(env, 'SUPABASE_SERVICE_KEY')
      ? 'SUPABASE_SERVICE_KEY'
      : 'SUPABASE_ANON (fallback)';
  const supabaseUrlSource = envValue(env, 'WEBSTREAM_SUPABASE_URL')
    ? 'WEBSTREAM_SUPABASE_URL'
    : envValue(env, 'SUPABASE_URL')
      ? 'SUPABASE_URL'
      : 'default';

  async function loadPlayer4meDomains() {
    try {
      const r = await fetch(
        supabaseUrl + '/rest/v1/player4me_domains?select=id,name,domain,is_default&order=is_default.desc,name.asc',
        {
          headers: {
            'apikey': supabaseKey,
            'Authorization': 'Bearer ' + supabaseKey,
            'Content-Type': 'application/json',
          },
        }
      );
      const arr = await r.json().catch(function(){ return []; });
      if (!r.ok) {
        const detail = (arr && (arr.message || arr.error || arr.hint)) || ('HTTP ' + r.status);
        return { ok: false, error: 'Supabase ' + supabaseUrlSource + ' (' + supabaseKeySource + '): ' + detail, domains: [] };
      }
      return { ok: true, domains: Array.isArray(arr) ? arr : [] };
    } catch (e) {
      return { ok: false, error: (e && e.message) || String(e), domains: [] };
    }
  }

  const domainResult = await loadPlayer4meDomains();
  // Fallback hard-coded sesuai data user (Zaeinstore QZZ default, sholeh,
  // Zaeins) supaya UI tidak kosong saat env Supabase belum benar.
  const fallbackDomains = [
    { id: 'fallback-qzz', name: 'Zaeinstore QZZ', domain: 'https://zaeinstore.qzz.io', is_default: true },
    { id: 'fallback-sholeh', name: 'sholeh', domain: 'https://zaeinstream.my.id', is_default: false },
    { id: 'fallback-zaeins', name: 'Zaeins', domain: 'https://zaeinstreamm', is_default: false },
  ];
  const usedFallback = !(Array.isArray(domainResult.domains) && domainResult.domains.length);
  const loadedDomains = usedFallback ? fallbackDomains : domainResult.domains;
  return jsonOk({
    ok: true,
    tmdb_ready: !!envValue(env, 'TMDB_API_KEY'),
    bot_api_ready: !!envValue(env, 'BOT_API_BASE'),
    webstream_api_ready: !!envValue(env, 'WEBSTREAM_API_BASE'),
    webstream_api_base: envValue(env, 'WEBSTREAM_API_BASE'),
    supabase_url: supabaseUrl,
    supabase_url_source: supabaseUrlSource,
    supabase_key_source: supabaseKeySource,
    player4me_domains_ready: !!domainResult.ok,
    player4me_domains_error: domainResult.error || '',
    player4me_domains_using_fallback: usedFallback,
    player4me_domains: loadedDomains,
  });
}

async function handleTmdbSearch(request, env) {
  const apiKey = envValue(env, 'TMDB_API_KEY');
  if (!apiKey) return jsonError(500, 'TMDB_API_KEY belum di-set di Cloudflare Variables.');
  const url = new URL(request.url);
  const type = url.searchParams.get('type') === 'tv' ? 'tv' : 'movie';
  const query = (url.searchParams.get('query') || '').trim();
  if (!query) return jsonError(400, 'query required');

  async function searchLang(language) {
    const tmdbUrl = 'https://api.themoviedb.org/3/search/' + type
      + '?api_key=' + encodeURIComponent(apiKey)
      + '&language=' + encodeURIComponent(language)
      + '&query=' + encodeURIComponent(query)
      + '&include_adult=false';
    const r = await fetch(tmdbUrl);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((data && data.status_message) || ('TMDB HTTP ' + r.status));
    return data;
  }

  let en;
  try { en = await searchLang('en-US'); }
  catch (e) { return jsonError(502, e.message || String(e)); }

  // Default webstream pakai English supaya judul tidak berubah ke China/Jepang/Korea.
  // Data Indonesia tetap disiapkan sebagai fallback jika user cari film Indonesia.
  try {
    const id = await searchLang('id-ID');
    const idById = new Map((id.results || []).map(function(x){ return [String(x.id), x]; }));
    en.results = (en.results || []).map(function(item) {
      const local = idById.get(String(item.id));
      const origin = String((item.origin_country || [])[0] || '').toUpperCase();
      const originalLang = String(item.original_language || '').toLowerCase();
      const isIndonesia = origin === 'ID' || originalLang === 'id';
      if (local) {
        item.id_title = local.title || local.name || '';
        item.id_overview = local.overview || '';
        if (isIndonesia) {
          item.title = local.title || item.title;
          item.name = local.name || item.name;
          item.overview = local.overview || item.overview;
          item.overview_language = 'id-ID';
        } else if (!String(item.overview || '').trim() && String(local.overview || '').trim()) {
          item.overview = local.overview;
          item.overview_language = 'id-ID';
        }
      }
      return item;
    });
  } catch (e) {
    // Fallback Indonesia gagal tidak boleh merusak hasil English.
  }

  return jsonOk(en);
}

async function handleStreamJob(request, env) {
  const botBase = envValue(env, 'BOT_API_BASE').replace(/\/$/, '');
  const botSecret = envValue(env, 'BOT_API_SECRET');
  if (!botBase) return jsonError(500, 'BOT_API_BASE belum di-set di Cloudflare Variables.');

  // GET dipakai UI progress supaya BOT_API_SECRET tetap aman di Cloudflare Worker.
  if (request.method === 'GET') {
    const r = await fetch(botBase + '/api/jobs?show=active', {
      headers: { ...(botSecret ? { 'Authorization': 'Bearer ' + botSecret } : {}) },
    });
    const data = await r.json().catch(async () => ({ raw: await r.text().catch(() => '') }));
    if (!r.ok) return jsonError(r.status, data.error || data.message || ('Bot API HTTP ' + r.status));
    return jsonOk(data);
  }

  if (request.method !== 'POST') return jsonError(405, 'method not allowed');

  let payload;
  try { payload = await request.json(); }
  catch { return jsonError(400, 'invalid json'); }
  const kind = payload && payload.kind === 'series' ? 'series' : 'movie';
  const endpoint = botBase + '/api/jobs/' + kind;
  const r = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(botSecret ? { 'Authorization': 'Bearer ' + botSecret } : {}),
    },
    body: JSON.stringify(payload),
  });
  const data = await r.json().catch(async () => ({ raw: await r.text().catch(() => '') }));
  if (!r.ok) return jsonError(r.status, data.error || data.message || ('Bot API HTTP ' + r.status));
  return jsonOk(data);
}

const HTML = `<!DOCTYPE html>
<html lang="id" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zaein Drive Token</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📁</text></svg>"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<style>
:root {
  --bg: #f0f2f7; --bg2: #ffffff; --bg3: #e8ecf5; --bg4: #d8dff0;
  --border: #00000010; --border2: #00000018;
  --text: #1a1e28; --text2: #5a6070; --text3: #9aa0b0;
  --accent: #4f8ef7; --accent2: #3a7ae0; --accent-glow: #4f8ef720;
  --green: #34c77b; --green-bg: #34c77b15;
  --red: #ff5c5c; --red-bg: #ff5c5c15;
  --yellow: #f5a623; --yellow-bg: #f5a62315;
  --purple: #a07cf5; --purple-bg: #a07cf515;
  --radius: 12px; --radius-sm: 8px;
  --sidebar-w: 220px; --header-h: 56px;
  --font: 'DM Sans', system-ui, sans-serif; --mono: 'DM Mono', monospace;
  --transition: 0.18s ease; --shadow: 0 8px 32px rgba(0,0,0,0.1);
}
[data-theme="dark"] {
  --bg: #0d0f14; --bg2: #13161e; --bg3: #1a1e28; --bg4: #21263300;
  --border: #ffffff10; --border2: #ffffff18;
  --text: #e8ecf0; --text2: #8b92a0; --text3: #555d6e;
  --accent-glow: #4f8ef730; --shadow: 0 8px 32px rgba(0,0,0,0.4);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-family: var(--font); background: var(--bg); color: var(--text); }
body { min-height: 100dvh; display: flex; flex-direction: column; overflow-x: hidden; }
a { color: inherit; text-decoration: none; }
button { font-family: var(--font); cursor: pointer; border: none; background: none; }
input { font-family: var(--font); }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 99px; }
.app { display: flex; height: 100dvh; overflow: hidden; }

/* SIDEBAR */
.sidebar { width: var(--sidebar-w); min-width: var(--sidebar-w); background: var(--bg2); border-right: 1px solid var(--border); display: flex; flex-direction: column; z-index: 30; transition: transform var(--transition); }
.sidebar-logo { height: var(--header-h); display: flex; align-items: center; padding: 0 16px; border-bottom: 1px solid var(--border); gap: 10px; }
.sidebar-logo .logo-icon { width: 28px; height: 28px; background: linear-gradient(135deg, var(--accent), #7c5ef5); border-radius: 8px; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 14px; font-weight: 600; flex-shrink: 0; }
.sidebar-logo .logo-text { font-size: 14px; font-weight: 600; letter-spacing: -0.3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sidebar-nav { flex: 1; padding: 12px 10px; display: flex; flex-direction: column; gap: 2px; overflow-y: auto; }
.nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: var(--radius-sm); font-size: 13.5px; font-weight: 500; color: var(--text2); cursor: pointer; transition: all var(--transition); }
.nav-item:hover { background: var(--bg3); color: var(--text); }
.nav-item.active { background: var(--accent-glow); color: var(--accent); }
.nav-item i { font-size: 16px; flex-shrink: 0; }
.sidebar-bottom { padding: 12px 10px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; }
.add-from-link-btn { display: flex; align-items: center; justify-content: center; gap: 7px; padding: 10px 12px; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; border: none; border-radius: var(--radius-sm); font-size: 12.5px; font-weight: 600; cursor: pointer; transition: all 0.18s; box-shadow: 0 2px 6px rgba(79,142,247,0.30); width: 100%; }
.add-from-link-btn:hover { filter: brightness(1.08); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(79,142,247,0.42); }
.add-from-link-btn .bi { font-size: 15px; }
.token-badge { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: var(--radius-sm); background: var(--bg3); cursor: pointer; transition: background var(--transition); }
.token-badge:hover { background: var(--border); }
.token-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; transition: background var(--transition); }
.token-dot.active { background: var(--green); box-shadow: 0 0 6px var(--green); }
.token-dot.inactive { background: var(--red); box-shadow: 0 0 6px var(--red); }
.token-badge-text { flex: 1; min-width: 0; }
.token-badge-text .tb-label { font-size: 11px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; color: var(--text3); }
.token-badge-text .tb-status { font-size: 12.5px; font-weight: 500; }
.tb-status.active { color: var(--green); }
.tb-status.inactive { color: var(--red); }

/* MAIN */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }
.topbar { height: var(--header-h); min-height: var(--header-h); display: flex; align-items: center; padding: 0 20px; gap: 12px; background: var(--bg2); border-bottom: 1px solid var(--border); z-index: 20; }
.topbar-title { font-size: 15px; font-weight: 600; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.search-wrap { position: relative; flex: 1; max-width: 320px; }
.search-wrap input { width: 100%; padding: 7px 14px 7px 36px; background: var(--bg3); border: 1px solid var(--border); border-radius: 20px; font-size: 13px; color: var(--text); outline: none; transition: border var(--transition); }
.search-wrap input:focus { border-color: var(--accent); }
.search-wrap input::placeholder { color: var(--text3); }
.search-wrap .si { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--text3); font-size: 14px; }
.topbar-btn { width: 34px; height: 34px; border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--text2); font-size: 16px; transition: all var(--transition); cursor: pointer; }
.topbar-btn:hover { background: var(--bg3); color: var(--text); }
.menu-btn { display: none; }

/* BREADCRUMB */
.breadcrumb-bar { padding: 10px 20px; display: flex; align-items: center; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--border); background: var(--bg2); min-height: 40px; position: relative; }
.bc-item { font-size: 12.5px; color: var(--text2); cursor: pointer; padding: 2px 6px; border-radius: 5px; transition: all var(--transition); display: flex; align-items: center; gap: 4px; }
.bc-item:hover { background: var(--bg3); color: var(--text); }
.bc-item.active { color: var(--text); font-weight: 500; }
.bc-sep { color: var(--text3); font-size: 12px; }

/* SELECTION BAR */
.selection-bar { display: none; align-items: center; gap: 12px; padding: 8px 20px; background: var(--accent-glow); border-bottom: 1px solid rgba(79,142,247,0.2); animation: slideDown 0.15s ease; }
.selection-bar.visible { display: flex; }
@keyframes slideDown { from { opacity:0; transform:translateY(-8px); } to { opacity:1; transform:translateY(0); } }
.sel-count { font-size: 13px; font-weight: 500; color: var(--accent); }
.sel-clear { font-size: 12px; color: var(--text3); cursor: pointer; }
.sel-clear:hover { color: var(--text); }

/* LOAD PROGRESS */
.load-progress { display: flex; align-items: center; gap: 8px; padding: 5px 20px; background: var(--bg3); border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text3); }
.load-progress.hidden { display: none; }

/* FILE TABLE */
.file-area { flex: 1; overflow-y: auto; }
.file-table { width: 100%; border-collapse: collapse; }
.file-table thead th { padding: 10px 12px; font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text3); text-align: left; border-bottom: 1px solid var(--border); background: var(--bg2); position: sticky; top: 0; z-index: 5; white-space: nowrap; }
.file-table thead th:first-child { padding-left: 20px; width: 36px; }
.file-table thead th:last-child { padding-right: 20px; width: 80px; }
.file-table tbody tr { border-bottom: 1px solid var(--border); transition: background var(--transition); }
.file-table tbody tr:hover { background: var(--bg2); }
.file-table tbody tr.selected { background: var(--accent-glow) !important; }
.file-table td { padding: 10px 12px; font-size: 13.5px; vertical-align: middle; }
.file-table td:first-child { padding-left: 20px; }
.file-table td:last-child { padding-right: 20px; }
.cb-wrap { display: flex; align-items: center; }
.cb-wrap input[type=checkbox] { width: 15px; height: 15px; cursor: pointer; accent-color: var(--accent); }
.file-name-cell { display: flex; align-items: center; gap: 10px; cursor: pointer; min-width: 0; }
.file-name-cell .copy-link-btn { flex-shrink: 0; }
.format-badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; font-family: var(--mono); background: var(--bg3); color: var(--text2); white-space: nowrap; text-transform: uppercase; }
.format-text { font-size: 12.5px; color: var(--text3); }

/* CLEANUP DROPDOWN + PROGRESS OVERLAY */
.bc-cleanup-btn .bi { font-size: 14px; }
.bc-report-btn .bi { font-size: 13px; color: var(--red); }
.bc-report-btn:hover { background: rgba(239,68,68,0.12); }
.bc-report-btn:hover .bi { color: var(--red); }
.bc-kirim-btn { display: inline-flex; align-items: center; gap: 5px; padding: 6px 11px; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff !important; border: 1px solid transparent; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.18s; box-shadow: 0 1px 3px rgba(79,142,247,0.30); margin-left: 2px; }
.bc-kirim-btn .bi { font-size: 13px; color: #fff; }
.bc-kirim-btn:hover { transform: translateY(-1px); box-shadow: 0 3px 8px rgba(79,142,247,0.40); filter: brightness(1.05); }
.bc-kirim-btn:disabled { opacity: 0.55; cursor: not-allowed; transform: none; box-shadow: none; }
.bc-kirim-btn.in-cart { background: var(--accent-glow); color: var(--accent) !important; box-shadow: none; }
.bc-kirim-btn.in-cart .bi { color: var(--accent); }

/* BACK-TO-SEARCH PILL (in breadcrumb when navigated from a search result) */
.bc-back-search { display: inline-flex; align-items: center; gap: 6px; padding: 7px 13px 7px 10px; background: var(--accent); color: #fff; border: none; border-radius: 999px; font-size: 12.5px; font-weight: 600; cursor: pointer; transition: all 0.18s; margin-right: 6px; max-width: 100%; box-shadow: 0 2px 6px rgba(79,142,247,0.30); }
.bc-back-search:hover { background: var(--accent2); transform: translateX(-2px); box-shadow: 0 3px 10px rgba(79,142,247,0.45); }
.bc-back-search .bi { font-size: 14px; }
.bc-back-search .bbs-q { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: 0.95; }
.bc-back-search-sep { color: var(--text3); margin: 0 6px 0 2px; opacity: 0.5; font-size: 14px; }

/* Folder contents count column */
.col-count { white-space: nowrap; font-size: 12.5px; color: var(--text3); }
.col-count .cc-num { color: var(--text); font-weight: 600; }
.col-count .cc-loading { display: inline-flex; align-items: center; gap: 5px; color: var(--text3); }
.col-count .cc-dash { color: var(--text3); opacity: 0.5; }
@media (max-width: 900px) { .file-table .col-count { display: none; } }
.cleanup-dropdown { position: absolute; top: 100%; left: 12px; right: 12px; max-width: 360px; background: var(--bg2); border: 1px solid var(--border2); border-radius: var(--radius); box-shadow: var(--shadow); padding: 14px; z-index: 90; display: none; flex-direction: column; gap: 10px; margin-top: 6px; }
.cleanup-dropdown.open { display: flex; animation: cdIn 0.18s ease; }
@keyframes cdIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }
.cd-title { font-size: 12px; font-weight: 600; color: var(--text2); text-transform: uppercase; letter-spacing: 0.4px; }
.cd-list { display: flex; flex-wrap: wrap; gap: 6px; max-height: 160px; overflow-y: auto; }
.cd-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 6px 4px 10px; border-radius: 99px; background: var(--bg3); font-size: 12px; color: var(--text); font-family: var(--mono); }
.cd-chip button { background: var(--border2); border: none; width: 18px; height: 18px; border-radius: 50%; color: var(--text2); cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; font-size: 12px; line-height: 1; transition: all var(--transition); }
.cd-chip button:hover { background: var(--red); color: #fff; }
.cd-add { display: flex; gap: 6px; }
.cd-add input { flex: 1; min-width: 0; padding: 8px 10px; border: 1px solid var(--border2); border-radius: var(--radius-sm); background: var(--bg); color: var(--text); font-size: 13px; font-family: var(--font); }
.cd-add input:focus { outline: none; border-color: var(--accent); }
.cd-add button { padding: 8px 14px; border: none; border-radius: var(--radius-sm); background: var(--accent); color: #fff; font-weight: 600; cursor: pointer; font-size: 13px; }
.cd-add button:hover { background: var(--accent2); }
.cd-run { padding: 10px; border: none; border-radius: var(--radius-sm); background: linear-gradient(135deg, var(--accent), #7c5ef5); color: #fff; font-weight: 600; cursor: pointer; font-size: 13px; }
.cd-run:hover { filter: brightness(1.05); }
.cd-empty { font-size: 12px; color: var(--text3); padding: 6px 2px; }

.cleanup-modal { max-width: 640px; }
.co-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; }
.co-stat { text-align: center; padding: 14px 8px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg3); }
.co-stat-num { font-size: 22px; font-weight: 700; color: var(--text); font-family: var(--mono); line-height: 1; }
.co-stat-label { font-size: 10.5px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.4px; margin-top: 6px; }
.co-stat.highlight { background: linear-gradient(135deg, rgba(123,109,255,0.18), rgba(110,199,255,0.12)); border-color: var(--accent); }
.co-stat.highlight .co-stat-num { color: var(--accent); }
.co-stat.success { background: linear-gradient(135deg, rgba(34,197,94,0.15), rgba(34,197,94,0.05)); border-color: var(--green); }
.co-stat.success .co-stat-num { color: var(--green); }
.co-stat.danger { background: linear-gradient(135deg, rgba(239,68,68,0.15), rgba(239,68,68,0.05)); border-color: var(--red); }
.co-stat.danger .co-stat-num { color: var(--red); }
.co-current { display: flex; align-items: center; gap: 8px; padding: 9px 12px; background: var(--bg3); border-radius: var(--radius-sm); font-size: 12px; color: var(--text2); margin-bottom: 10px; font-family: var(--mono); }
.co-current i { color: var(--accent); animation: coScanPulse 1s ease-in-out infinite; flex-shrink: 0; }
@keyframes coScanPulse { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.2); opacity: 0.65; } }
.co-current span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.co-progressbar { background: var(--bg3); border-radius: 99px; height: 6px; overflow: hidden; margin-bottom: 12px; }
.co-progressbar-inner { background: linear-gradient(90deg, var(--accent), #7c5ef5); height: 100%; width: 0%; transition: width 0.25s ease; }
.co-list { max-height: 280px; overflow-y: auto; padding: 4px; border: 1px solid var(--border); border-radius: var(--radius-sm); display: flex; flex-direction: column; gap: 4px; background: var(--bg); }
.co-list:empty { padding: 24px; text-align: center; color: var(--text3); font-size: 12.5px; }
.co-list:empty::before { content: 'Belum ada item ditemukan...'; }
.co-item { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: var(--radius-sm); background: var(--bg3); font-size: 11.5px; animation: coItemIn 0.3s cubic-bezier(0.34, 1.56, 0.64, 1); min-width: 0; }
.co-item .co-item-icon { color: var(--accent); flex-shrink: 0; font-size: 13px; }
.co-item .co-item-old { color: var(--text3); text-decoration: line-through; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 45%; flex: 0 1 auto; font-family: var(--mono); }
.co-item .co-item-arrow { color: var(--text3); flex-shrink: 0; opacity: 0.7; }
.co-item .co-item-new { color: var(--text); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
.co-item.success .co-item-icon { color: var(--green); }
.co-item.success .co-item-new { color: var(--green); }
.co-item.failed { background: rgba(239,68,68,0.1); }
.co-item.failed .co-item-new { color: var(--red); }
.co-item.failed .co-item-icon { color: var(--red); }
.co-item.processing { background: rgba(123,109,255,0.1); animation: coItemPulse 0.9s ease-in-out infinite; }
@keyframes coItemPulse { 0%, 100% { background: rgba(123,109,255,0.08); } 50% { background: rgba(123,109,255,0.22); } }
@keyframes coItemIn { from { opacity: 0; transform: translateY(-8px) scale(0.96); } to { opacity: 1; transform: translateY(0) scale(1); } }
.co-item.deleting { animation: coDelete 0.55s ease forwards; overflow: hidden; }
@keyframes coDelete { 0% { opacity: 1; max-height: 40px; transform: translateX(0); padding-top: 7px; padding-bottom: 7px; margin-top: 0; } 100% { opacity: 0; max-height: 0; transform: translateX(-40px); padding-top: 0; padding-bottom: 0; margin-top: -4px; } }
.co-state { display: none; }
.co-state.active { display: block; animation: coStateIn 0.25s ease; }
@keyframes coStateIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.co-summary { font-size: 13.5px; color: var(--text2); display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
.co-summary b { color: var(--text); font-size: 16px; font-family: var(--mono); }
.file-icon { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; flex-shrink: 0; }
.file-icon.folder { background: var(--yellow-bg); color: var(--yellow); }
.file-icon.video { background: var(--purple-bg); color: var(--purple); }
.file-icon.image { background: var(--green-bg); color: var(--green); }
.file-icon.audio { background: #e91e6315; color: #e91e63; }
.file-icon.doc { background: var(--accent-glow); color: var(--accent); }
.file-icon.generic { background: var(--bg3); color: var(--text3); }
.file-name { font-size: 13.5px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 320px; }
.file-name:hover { color: var(--accent); }

/* QUALITY BADGES — resolution-based, color-coded */
.quality-badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; font-family: var(--mono); white-space: nowrap; }
.quality-badge.q4k   { background: var(--purple-bg); color: var(--purple); }   /* 4K — purple */
.quality-badge.q1080 { background: var(--accent-glow); color: var(--accent); }  /* 1080p — blue */
.quality-badge.q720  { background: var(--green-bg); color: var(--green); }      /* 720p — green */
.quality-badge.q480  { background: var(--yellow-bg); color: var(--yellow); }    /* 480p — amber */
.quality-badge.qsd   { background: var(--bg3); color: var(--text3); }           /* SD — muted */

.size-text { font-size: 12.5px; color: var(--text2); font-family: var(--mono); white-space: nowrap; }
.size-loading { color: var(--text3); font-size: 11px; }
.dur-text { font-size: 12px; color: var(--text3); font-family: var(--mono); white-space: nowrap; }

.copy-link-btn { --color: var(--text3); --size: 20px; display: flex; justify-content: center; align-items: center; position: relative; cursor: pointer; font-size: var(--size); user-select: none; fill: var(--color); background: none; border: none; padding: 4px; border-radius: 6px; transition: background var(--transition); }
.copy-link-btn:hover { background: var(--bg3); --color: var(--accent); }
.copy-link-btn .clipboard { position: absolute; animation: keyframes-fill .4s; }
.copy-link-btn .clipboard-check { position: absolute; display: none; animation: keyframes-fill .4s; fill: var(--green); }
.copy-link-btn.copied .clipboard { display: none; }
.copy-link-btn.copied .clipboard-check { display: block; }
@keyframes keyframes-fill { 0%{transform:rotate(0deg) scale(0);opacity:0} 50%{transform:rotate(-10deg) scale(1.2)} }

.copy-fab { position: fixed; bottom: 28px; right: 28px; z-index: 50; display: none; align-items: center; gap: 10px; background: var(--accent); color: #fff; padding: 12px 20px; border-radius: 99px; font-size: 14px; font-weight: 600; box-shadow: 0 4px 24px var(--accent-glow), 0 2px 8px rgba(0,0,0,0.3); cursor: pointer; transition: all var(--transition); animation: fabIn 0.2s ease; }
.copy-fab:hover { background: var(--accent2); transform: translateY(-2px); }
.copy-fab.visible { display: flex; }
.copy-fab i { font-size: 16px; }
@keyframes fabIn { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }

.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 80px 20px; gap: 12px; color: var(--text3); }
.empty-state i { font-size: 48px; opacity: 0.3; }
.empty-state p { font-size: 14px; }
.loading-row td { padding: 40px; text-align: center; color: var(--text3); }
.spinner { display: inline-block; width: 20px; height: 20px; border: 2px solid var(--border2); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 8px; }
@keyframes spin { to { transform: rotate(360deg); } }

/* MODALS */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 100; display: flex; align-items: center; justify-content: center; padding: 20px; backdrop-filter: blur(4px); opacity: 0; pointer-events: none; transition: opacity 0.2s; }
.modal-overlay.open { opacity: 1; pointer-events: all; }
.modal { background: var(--bg2); border: 1px solid var(--border2); border-radius: 16px; width: 100%; max-width: 520px; box-shadow: var(--shadow); transform: scale(0.95); transition: transform 0.2s; overflow: hidden; }
.modal-overlay.open .modal { transform: scale(1); }
.modal-header { padding: 18px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
.modal-title { font-size: 15px; font-weight: 600; }
.modal-close { width: 28px; height: 28px; border-radius: 6px; display: flex; align-items: center; justify-content: center; color: var(--text3); cursor: pointer; transition: all var(--transition); }
.modal-close:hover { background: var(--bg3); color: var(--text); }
.modal-body { padding: 20px; max-height: 70vh; overflow-y: auto; }
.modal-footer { padding: 14px 20px; border-top: 1px solid var(--border); display: flex; gap: 8px; justify-content: flex-end; }

.oauth-config-wrap { display: flex; flex-direction: column; gap: 16px; }
.oauth-intro { font-size: 13px; color: var(--text2); line-height: 1.6; padding: 12px 14px; background: var(--bg3); border-radius: var(--radius-sm); border-left: 3px solid var(--accent); }
.oauth-intro strong { color: var(--text); }
.form-field { display: flex; flex-direction: column; gap: 5px; }
.form-field label { font-size: 11.5px; font-weight: 600; color: var(--text3); text-transform: uppercase; letter-spacing: 0.4px; }
.form-field .field-hint { font-size: 11px; color: var(--text3); }
.form-field input { width: 100%; padding: 9px 12px; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 13px; color: var(--text); outline: none; transition: border var(--transition); font-family: var(--mono); }
.form-field input:focus { border-color: var(--accent); }
.form-field input[type=password] { letter-spacing: 2px; }
.token-status-block { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: var(--radius-sm); font-size: 13px; }
.token-status-block.active { background: var(--green-bg); color: var(--green); }
.token-status-block.inactive { background: var(--red-bg); color: var(--red); }
.oauth-active-box { background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }
.oauth-active-box .oab-title { font-size: 11.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--green); display: flex; align-items: center; gap: 6px; }
.oauth-active-box .oab-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.oab-label { color: var(--text3); font-weight: 600; min-width: 90px; }
.oab-val { color: var(--text2); font-family: var(--mono); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }

.dest-list { max-height: 280px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.dest-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px; cursor: pointer; font-size: 13.5px; border-bottom: 1px solid var(--border); transition: background var(--transition); }
.dest-item:last-child { border-bottom: none; }
.dest-item:hover { background: var(--bg3); }
.dest-item i { font-size: 16px; color: var(--yellow); flex-shrink: 0; }
.dest-breadcrumb { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; padding: 8px 0; margin-bottom: 8px; }
.dest-bc { font-size: 12px; color: var(--text3); cursor: pointer; padding: 2px 5px; border-radius: 4px; }
.dest-bc:hover { background: var(--bg3); color: var(--text); }
.dest-bc.active { color: var(--text); font-weight: 500; }

.success-anim { text-align: center; padding: 20px 0; }
.success-icon { font-size: 52px; color: var(--green); animation: popIn 0.4s cubic-bezier(0.34,1.56,0.64,1); display: block; }
@keyframes popIn { from{transform:scale(0);opacity:0} to{transform:scale(1);opacity:1} }
.success-title { font-size: 18px; font-weight: 600; margin-top: 12px; }
.success-sub { font-size: 13px; color: var(--text2); margin-top: 6px; }
.copy-result-list { margin-top: 14px; max-height: 160px; overflow-y: auto; text-align: left; }
.copy-result-item { display: flex; align-items: center; gap: 8px; padding: 6px 0; font-size: 13px; border-bottom: 1px solid var(--border); }
.copy-result-item:last-child { border-bottom: none; }
.copy-result-item i.ok { color: var(--green); }
.copy-result-item i.fail { color: var(--red); }
.progress-wrap { margin-top: 12px; }
.progress-bar-outer { background: var(--bg3); border-radius: 99px; height: 6px; overflow: hidden; }
.progress-bar-inner { height: 100%; background: var(--accent); border-radius: 99px; transition: width 0.3s ease; }
.progress-label { font-size: 12px; color: var(--text3); margin-top: 6px; }

.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: var(--radius-sm); font-size: 13.5px; font-weight: 500; cursor: pointer; transition: all var(--transition); border: none; font-family: var(--font); }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent2); }
.btn-secondary { background: var(--bg3); color: var(--text2); border: 1px solid var(--border); }
.btn-secondary:hover { color: var(--text); border-color: var(--border2); }
.btn-danger { background: var(--red-bg); color: var(--red); }
.btn-danger:hover { background: var(--red); color: #fff; }
.btn:disabled { opacity: 0.5; pointer-events: none; }
.btn-sm { padding: 5px 12px; font-size: 12.5px; }
.btn-test { background: var(--bg3); color: var(--text2); border: 1px solid var(--border); }
.btn-test:hover { border-color: var(--accent); color: var(--accent); }

.toast-wrap { position: fixed; top: 20px; right: 20px; z-index: 200; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
.toast { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-radius: var(--radius-sm); font-size: 13.5px; font-weight: 500; box-shadow: var(--shadow); pointer-events: all; animation: toastIn 0.25s ease; max-width: 320px; }
.toast.success { background: var(--green-bg); color: var(--green); border: 1px solid rgba(52,199,123,0.3); }
.toast.error { background: var(--red-bg); color: var(--red); border: 1px solid rgba(255,92,92,0.3); }
.toast.info { background: var(--accent-glow); color: var(--accent); border: 1px solid rgba(79,142,247,0.3); }
@keyframes toastIn { from{opacity:0;transform:translateX(30px)} to{opacity:1;transform:translateX(0)} }
@keyframes toastOut { from{opacity:1;transform:translateX(0)} to{opacity:0;transform:translateX(30px)} }

/* THEME SWITCH */
.theme-switch-wrap { display: flex; align-items: center; }
.theme-switch { --toggle-size: 16px; --container-width: 5.625em; --container-height: 2.5em; --container-radius: 6.25em; --container-light-bg: #3d7eae; --container-night-bg: #1d1f2c; --circle-container-diameter: 3.375em; --sun-moon-diameter: 2.125em; --sun-bg: #ecca2f; --moon-bg: #c4c9d1; --spot-color: #959db1; --circle-container-offset: calc((var(--circle-container-diameter) - var(--container-height)) / 2 * -1); --stars-color: #fff; --clouds-color: #f3fdff; --back-clouds-color: #aacadf; --transition: 0.5s cubic-bezier(0, -0.02, 0.4, 1.25); --circle-transition: 0.3s cubic-bezier(0, -0.02, 0.35, 1.17); }
.theme-switch, .theme-switch *, .theme-switch *::before, .theme-switch *::after { -webkit-box-sizing: border-box; box-sizing: border-box; margin: 0; padding: 0; font-size: var(--toggle-size); }
.theme-switch__container { width: var(--container-width); height: var(--container-height); background-color: var(--container-light-bg); border-radius: var(--container-radius); overflow: hidden; cursor: pointer; box-shadow: 0em -0.062em 0.062em rgba(0,0,0,0.25), 0em 0.062em 0.125em rgba(255,255,255,0.94); transition: var(--transition); position: relative; background-image: linear-gradient(to bottom, var(--container-light-bg) 0%, #5490c0 100%); }
.theme-switch__container::before { content: ""; position: absolute; z-index: 1; inset: 0; box-shadow: 0em 0.05em 0.187em rgba(0,0,0,0.25) inset, 0em 0.05em 0.187em rgba(0,0,0,0.25) inset; border-radius: var(--container-radius); }
.theme-switch__checkbox { display: none; }
.theme-switch__circle-container { width: var(--circle-container-diameter); height: var(--circle-container-diameter); background-color: rgba(255,255,255,0.1); position: absolute; left: var(--circle-container-offset); top: var(--circle-container-offset); border-radius: var(--container-radius); box-shadow: inset 0 0 0 3.375em rgba(255,255,255,0.1), inset 0 0 0 3.375em rgba(255,255,255,0.1), 0 0 0 0.625em rgba(255,255,255,0.1), 0 0 0 1.25em rgba(255,255,255,0.1); display: flex; transition: var(--circle-transition); pointer-events: none; }
.theme-switch__sun-moon-container { pointer-events: auto; position: relative; z-index: 2; width: var(--sun-moon-diameter); height: var(--sun-moon-diameter); margin: auto; border-radius: var(--container-radius); background-color: var(--sun-bg); box-shadow: 0.062em 0.062em 0.062em 0em rgba(254,255,239,0.61) inset, 0em -0.062em 0.062em 0em #a1872a inset; filter: drop-shadow(0.062em 0.125em 0.125em rgba(0,0,0,0.25)) drop-shadow(0em 0.062em 0.125em rgba(0,0,0,0.25)); overflow: hidden; transition: var(--transition); transform: scale(1); }
.theme-switch__sun-moon-container:hover { transform: scale(1.1) rotate(5deg); }
.theme-switch__moon { transform: translateX(100%); width: 100%; height: 100%; background-color: var(--moon-bg); border-radius: inherit; box-shadow: 0.062em 0.062em 0.062em 0em rgba(254,255,239,0.61) inset, 0em -0.062em 0.062em 0em #969696 inset; transition: var(--transition); position: relative; }
.theme-switch__spot { position: absolute; top: 0.75em; left: 0.312em; width: 0.75em; height: 0.75em; border-radius: var(--container-radius); background-color: var(--spot-color); box-shadow: 0em 0.0312em 0.062em rgba(0,0,0,0.25) inset; }
.theme-switch__spot:nth-of-type(2) { width: 0.375em; height: 0.375em; top: 0.937em; left: 1.375em; }
.theme-switch__spot:nth-last-of-type(3) { width: 0.25em; height: 0.25em; top: 0.312em; left: 0.812em; }
.theme-switch__clouds { width: 1.25em; height: 1.25em; background-color: var(--clouds-color); border-radius: var(--container-radius); position: absolute; bottom: -0.625em; left: 0.312em; box-shadow: 0.937em 0.312em var(--clouds-color), -0.312em -0.312em var(--back-clouds-color), 1.437em 0.375em var(--clouds-color), 0.5em -0.125em var(--back-clouds-color), 2.187em 0 var(--clouds-color), 1.25em -0.062em var(--back-clouds-color), 2.937em 0.312em var(--clouds-color), 2em -0.312em var(--back-clouds-color), 3.625em -0.062em var(--clouds-color), 2.625em 0em var(--back-clouds-color), 4.5em -0.312em var(--clouds-color), 3.375em -0.437em var(--back-clouds-color), 4.625em -1.75em 0 0.437em var(--clouds-color), 4em -0.625em var(--back-clouds-color), 4.125em -2.125em 0 0.437em var(--back-clouds-color); transition: 0.5s cubic-bezier(0, -0.02, 0.4, 1.25); }
.theme-switch__stars-container { position: absolute; color: var(--stars-color); top: -100%; left: 0.312em; width: 2.75em; height: auto; transition: var(--transition); }
.theme-switch__checkbox:checked + .theme-switch__container { background-color: var(--container-night-bg); background-image: linear-gradient(to bottom, var(--container-night-bg) 0%, #2d3142 100%); }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__circle-container { left: calc(100% - var(--circle-container-offset) - var(--circle-container-diameter)); }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__circle-container:hover { left: calc(100% - var(--circle-container-offset) - var(--circle-container-diameter) - 0.187em); }
.theme-switch__circle-container:hover { left: calc(var(--circle-container-offset) + 0.187em); }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__moon { transform: translate(0); }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__clouds { bottom: -4.062em; }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__stars-container { top: 50%; transform: translateY(-50%); }
.theme-switch__container:hover .theme-switch__clouds { transform: translateX(15px) scale(1.02); }
.theme-switch__shooting-star, .theme-switch__shooting-star-2 { position: absolute; width: 2px; height: 2px; background: white; top: 20%; left: -10%; opacity: 0; transition: opacity 0.3s ease; }
.theme-switch__shooting-star-2 { top: 35%; width: 1px; height: 1px; }
.theme-switch__meteor { position: absolute; width: 3px; height: 3px; background: #ffd700; border-radius: 50%; top: -10%; left: 50%; opacity: 0; filter: blur(1px); }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__shooting-star { animation: shootingStar 2s linear infinite; opacity: 1; }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__shooting-star-2 { animation: shootingStar 3s linear infinite 1s; opacity: 1; }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__meteor { animation: meteor 4s linear infinite 2s; opacity: 1; }
@keyframes shootingStar { 0%{transform:translateX(0) translateY(0) rotate(45deg);opacity:1} 100%{transform:translateX(150px) translateY(150px) rotate(45deg);opacity:0} }
@keyframes meteor { 0%{transform:translateY(0) scale(1);opacity:1} 100%{transform:translateY(150px) scale(0.3);opacity:0} }
.theme-switch__stars-cluster { position: absolute; inset: 0; opacity: 0; transition: opacity 0.3s ease; }
.theme-switch__stars-cluster .star { position: absolute; width: 2px; height: 2px; background: white; border-radius: 50%; box-shadow: 0 0 4px 1px white; }
.theme-switch__stars-cluster .star:nth-child(1){top:20%;left:20%;animation:twinkle 1s infinite ease-in-out;}
.theme-switch__stars-cluster .star:nth-child(2){top:30%;left:55%;animation:twinkle 1s infinite ease-in-out .3s;}
.theme-switch__stars-cluster .star:nth-child(3){top:40%;left:80%;animation:twinkle 1s infinite ease-in-out .6s;}
.theme-switch__stars-cluster .star:nth-child(4){top:60%;left:30%;animation:twinkle 1s infinite ease-in-out .9s;}
.theme-switch__stars-cluster .star:nth-child(5){top:70%;left:65%;animation:twinkle 1s infinite ease-in-out 1.2s;}
@keyframes twinkle { 0%,100%{opacity:.3;transform:scale(1)} 50%{opacity:1;transform:scale(1.2)} }
.theme-switch__aurora { position: absolute; top: 0; left: 0; right: 0; height: 20px; background: linear-gradient(90deg,rgba(0,255,255,0) 0%,rgba(0,255,255,.2) 25%,rgba(128,0,255,.2) 50%,rgba(0,255,255,.2) 75%,rgba(0,255,255,0) 100%); opacity: 0; filter: blur(4px); transform: translateY(-100%); transition: opacity 0.3s ease; }
.theme-switch__comets { position: absolute; inset: 0; overflow: hidden; opacity: 0; transition: opacity 0.3s ease; }
.theme-switch__comets .comet { position: absolute; width: 2px; height: 2px; background: linear-gradient(90deg, white 0%, transparent 90%); border-radius: 50%; filter: blur(1px); }
.theme-switch__comets .comet:nth-child(1){top:30%;left:-10%;animation:cometMove 4s linear infinite;}
.theme-switch__comets .comet:nth-child(2){top:50%;left:-10%;animation:cometMove 6s linear infinite 2s;}
@keyframes cometMove { 0%{transform:translateX(0) translateY(0) rotate(-45deg) scale(1);opacity:0} 10%{opacity:1} 90%{opacity:1} 100%{transform:translateX(200px) translateY(200px) rotate(-45deg) scale(0.2);opacity:0} }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__stars-cluster { opacity: 1; }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__aurora { opacity: 1; animation: auroraWave 8s linear infinite; }
.theme-switch__checkbox:checked + .theme-switch__container .theme-switch__comets { opacity: 1; }
@keyframes auroraWave { 0%{transform:translateY(-100%) translateX(-50%)} 100%{transform:translateY(-100%) translateX(50%)} }

.sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 29; }

@media (max-width: 768px) {
  .sidebar { position: fixed; left: 0; top: 0; bottom: 0; transform: translateX(-100%); z-index: 30; }
  .sidebar.open { transform: translateX(0); }
  .sidebar-overlay { display: block; opacity: 0; pointer-events: none; transition: opacity 0.2s; }
  .sidebar-overlay.visible { opacity: 1; pointer-events: all; }
  .menu-btn { display: flex; }
  .search-wrap { max-width: 200px; }
  .file-table .col-dur, .file-table .col-quality { display: none; }
  .file-name { max-width: 180px; }
  .copy-fab { bottom: 20px; right: 16px; padding: 11px 16px; font-size: 13px; }
  .main { min-width: 0; }
}
@media (max-width: 480px) {
  .search-wrap { display: none; }
  .file-table .col-size, .file-table .col-format { display: none; }
}
@media (max-width: 720px) {
  .file-table .col-format { display: none; }
}

/* SEMANGAT MARQUEE */
.semangat-overlay {
  position: fixed;
  left: 0;
  right: 0;
  height: 56px;
  pointer-events: none;
  z-index: 250;
  overflow: hidden;
}
.semangat-overlay.pos-top { top: calc(var(--header-h) + 16px); }
.semangat-overlay.pos-mid { top: 50%; }
.semangat-overlay.pos-bot { bottom: 28px; }
.semangat-msg {
  position: absolute;
  top: 50%;
  left: 0;
  white-space: nowrap;
  padding: 12px 28px;
  border-radius: 999px;
  font-family: var(--font);
  font-weight: 700;
  font-size: 18px;
  color: #fff;
  letter-spacing: 0.3px;
  text-shadow: 0 2px 8px rgba(0,0,0,0.25);
  box-shadow: 0 10px 32px rgba(0,0,0,0.22), 0 2px 8px rgba(0,0,0,0.15);
  transform: translate(100vw, -50%);
  animation: semangat-slide 7s linear forwards;
  will-change: transform;
}
@keyframes semangat-slide {
  0%   { transform: translate(100vw, -50%); }
  100% { transform: translate(-110%, -50%); }
}
.semangat-bg-1 { background: linear-gradient(135deg, #ff6a88 0%, #ff99ac 100%); }
.semangat-bg-2 { background: linear-gradient(135deg, #4f8ef7 0%, #a07cf5 100%); }
.semangat-bg-3 { background: linear-gradient(135deg, #34c77b 0%, #4f8ef7 100%); }
.semangat-bg-4 { background: linear-gradient(135deg, #f5a623 0%, #ff6a88 100%); }
.semangat-bg-5 { background: linear-gradient(135deg, #a07cf5 0%, #ff99ac 100%); }
.semangat-bg-6 { background: linear-gradient(135deg, #00c6ff 0%, #0072ff 100%); }
.semangat-bg-7 { background: linear-gradient(135deg, #fc5c7d 0%, #6a82fb 100%); }
.semangat-bg-8 { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
@media (max-width: 480px) {
  .semangat-msg { font-size: 15px; padding: 10px 20px; }
  .semangat-overlay { height: 48px; }
}

/* ============ CART PANEL (Floating right-side) ============ */
.cart-panel { position: fixed; right: 20px; bottom: 90px; width: 320px; max-height: 60vh; background: var(--bg2); border: 1px solid var(--border2); border-radius: 16px; box-shadow: 0 12px 40px rgba(0,0,0,0.18), 0 4px 12px rgba(0,0,0,0.08); display: none; flex-direction: column; z-index: 60; overflow: hidden; animation: cartIn 0.25s cubic-bezier(0.34,1.56,0.64,1); }
.cart-panel.open { display: flex; }
[data-theme=dark] .cart-panel { box-shadow: 0 12px 40px rgba(0,0,0,0.6), 0 4px 12px rgba(0,0,0,0.3); }
@keyframes cartIn { from { opacity: 0; transform: translateY(20px) scale(0.92); } to { opacity: 1; transform: translateY(0) scale(1); } }
.cart-panel-header { padding: 12px 14px; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid var(--border); background: linear-gradient(135deg, var(--accent), #7c5ef5); color: #fff; }
.cart-panel-header .cph-title { flex: 1; font-size: 13.5px; font-weight: 600; }
.cart-panel-header .cph-count { background: rgba(255,255,255,0.2); padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 700; }
.cart-panel-header .cph-close { background: rgba(255,255,255,0.15); border: none; color: #fff; width: 24px; height: 24px; border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background 0.15s; }
.cart-panel-header .cph-close:hover { background: rgba(255,255,255,0.3); }
.cart-panel-body { flex: 1; overflow-y: auto; padding: 8px 6px; min-height: 100px; }
.cart-panel-body:empty::after { content: 'Pilih file atau folder untuk masuk ke daftar...'; display: block; padding: 30px 14px; text-align: center; color: var(--text3); font-size: 12px; }
.cart-item { display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-radius: 8px; transition: background 0.15s; animation: cartItemIn 0.3s cubic-bezier(0.34,1.56,0.64,1); }
.cart-item:hover { background: var(--bg3); }
@keyframes cartItemIn { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }
.cart-item .ci-icon { width: 22px; height: 22px; border-radius: 5px; display: flex; align-items: center; justify-content: center; font-size: 11px; flex-shrink: 0; }
.cart-item .ci-icon.folder { background: var(--yellow-bg); color: var(--yellow); }
.cart-item .ci-icon.video { background: var(--purple-bg); color: var(--purple); }
.cart-item .ci-icon.generic { background: var(--bg3); color: var(--text3); }
.cart-item .ci-name { flex: 1; min-width: 0; font-size: 12px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cart-item .ci-remove { background: transparent; border: none; color: var(--text3); width: 20px; height: 20px; border-radius: 4px; cursor: pointer; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; transition: all 0.15s; }
.cart-item .ci-remove:hover { background: var(--red-bg); color: var(--red); }
.cart-panel-actions { padding: 10px; display: grid; grid-template-columns: repeat(3,1fr); gap: 6px; border-top: 1px solid var(--border); background: var(--bg); }
.cart-act-btn { padding: 8px 6px; border: 1px solid var(--border2); border-radius: 8px; background: var(--bg2); color: var(--text); font-size: 11.5px; font-weight: 600; cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 3px; transition: all 0.15s; font-family: var(--font); }
.cart-act-btn:hover { transform: translateY(-1px); border-color: var(--accent); background: var(--accent-glow); }
.cart-act-btn i { font-size: 16px; }
.cart-act-btn.lapor { color: var(--red); }
.cart-act-btn.lapor:hover { border-color: var(--red); background: var(--red-bg); color: var(--red); }
.cart-act-btn.kirim { color: var(--green); }
.cart-act-btn.kirim:hover { border-color: var(--green); background: var(--green-bg); color: var(--green); }
.cart-act-btn.salin { color: var(--accent); }
.cart-act-btn.salin:hover { border-color: var(--accent); background: var(--accent-glow); color: var(--accent); }

/* Cart fly animation (item flying to cart) */
.cart-fly { position: fixed; z-index: 9999; pointer-events: none; width: 32px; height: 32px; border-radius: 50%; background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 14px; box-shadow: 0 4px 16px rgba(79,142,247,0.4); transition: all 0.7s cubic-bezier(0.4, 0, 0.2, 1); }
.cart-panel.bump { animation: cartBump 0.45s cubic-bezier(0.34,1.56,0.64,1); }
@keyframes cartBump { 0% { transform: scale(1); } 50% { transform: scale(1.06); } 100% { transform: scale(1); } }

/* ============ ROW ACTIONS (3-dot menu) ============ */
.row-actions-btn { background: transparent; border: none; color: var(--text3); width: 30px; height: 30px; border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 16px; transition: all 0.15s; }
.row-actions-btn:hover { background: var(--bg3); color: var(--text); }
.row-actions-menu { position: fixed; min-width: 170px; max-width: 240px; max-height: calc(100vh - 24px); overflow-y: auto; background: var(--bg2); border: 1px solid var(--border2); border-radius: 10px; box-shadow: 0 12px 32px rgba(0,0,0,0.18); padding: 5px; z-index: 110; display: none; animation: ramIn 0.15s ease; }
.row-actions-menu.open { display: block; }
[data-theme=dark] .row-actions-menu { box-shadow: 0 12px 32px rgba(0,0,0,0.6); }
@keyframes ramIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
.ram-item { display: flex; align-items: center; gap: 10px; padding: 8px 12px; cursor: pointer; font-size: 13px; color: var(--text); border-radius: 6px; transition: background 0.12s; }
.ram-item:hover { background: var(--bg3); }
.ram-item i { font-size: 14px; flex-shrink: 0; width: 16px; text-align: center; }
.ram-item.lapor i { color: var(--red); }
.ram-item.kirim i { color: var(--green); }
.ram-item.salin i { color: var(--accent); }
.ram-item.rename i { color: #f59e0b; }

.ram-item.stream-movie i { color: #38bdf8; }
.ram-item.stream-series i { color: #a78bfa; }
.ram-item.stream-progress i { color: #34c77b; }

/* ============ STREAM UPLOAD UI (safe add-on) ============ */
.zu-stream-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.64); z-index: 180; display: none; align-items: center; justify-content: center; padding: 18px; backdrop-filter: blur(8px); }
.zu-stream-overlay.open { display: flex; }
.zu-stream-modal { width: min(960px, 96vw); max-height: 92vh; overflow: hidden; display: flex; flex-direction: column; background: linear-gradient(180deg, var(--bg2), var(--bg)); border: 1px solid var(--border2); border-radius: 24px; box-shadow: 0 24px 90px rgba(0,0,0,0.38); }
.zu-stream-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px; border-bottom: 1px solid var(--border); }
.zu-stream-title { display: flex; align-items: center; gap: 10px; font-size: 16px; font-weight: 800; }
.zu-stream-title i { color: var(--accent); }
.zu-stream-body { padding: 16px; overflow-y: auto; }
.zu-stream-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.zu-stream-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 18px; padding: 14px; }
.zu-stream-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.zu-stream-field label { font-size: 11px; color: var(--text3); text-transform: uppercase; letter-spacing: .5px; font-weight: 800; }
.zu-stream-input, .zu-stream-select { width: 100%; padding: 10px 11px; border: 1px solid var(--border2); border-radius: 12px; background: var(--bg3); color: var(--text); outline: none; font-size: 13px; }
.zu-stream-input:focus, .zu-stream-select:focus { border-color: var(--accent); }
.zu-stream-hint { color: var(--text3); font-size: 11.5px; line-height: 1.45; }
.zu-config-box{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border:1px solid var(--border);background:linear-gradient(135deg,var(--bg3),var(--bg2));border-radius:14px;margin-bottom:12px;font-size:12.5px;color:var(--text2)}
.zu-config-box i{color:var(--accent);font-size:18px;margin-top:1px}.zu-config-box b{color:var(--text);font-size:13px}.zu-config-box small{color:var(--text3)}.zu-ok{color:var(--green);font-weight:700}.zu-warn{color:var(--yellow);font-weight:700}
.zu-stream-actions { display: flex; gap: 8px; justify-content: flex-end; padding: 14px 18px; border-top: 1px solid var(--border); }
.zu-stream-btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; border-radius: 12px; padding: 10px 14px; font-weight: 800; font-size: 13px; cursor: pointer; border: 1px solid var(--border); color: var(--text); background: var(--bg3); }
.zu-stream-btn.primary { color: #fff; border: none; background: linear-gradient(135deg, var(--accent), #7c5ef5); box-shadow: 0 8px 20px rgba(79,142,247,.25); }
.zu-stream-btn.success { color: #fff; border: none; background: linear-gradient(135deg, #34c77b, #4f8ef7); }
.zu-stream-btn:disabled { opacity: .55; pointer-events: none; }
.zu-tmdb-results { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; margin-top: 10px; }
.zu-tmdb-item { border: 1px solid var(--border); background: var(--bg3); border-radius: 15px; overflow: hidden; cursor: pointer; transition: all .16s ease; }
.zu-tmdb-item:hover { transform: translateY(-2px); border-color: var(--accent); }
.zu-tmdb-item.selected { border-color: var(--green); box-shadow: 0 0 0 2px rgba(52,199,123,.18); }
.zu-tmdb-poster { aspect-ratio: 2/3; background: var(--bg4); display: flex; align-items: center; justify-content: center; color: var(--text3); overflow: hidden; }
.zu-tmdb-poster img { width: 100%; height: 100%; object-fit: cover; }
.zu-tmdb-meta { padding: 9px; }
.zu-tmdb-name { font-size: 12.5px; font-weight: 800; line-height: 1.25; }
.zu-tmdb-year { color: var(--text3); font-size: 11px; margin-top: 4px; }
.zu-selected-preview { display: grid; grid-template-columns: 84px 1fr; gap: 12px; align-items: start; padding: 10px; background: var(--bg3); border: 1px solid var(--border); border-radius: 14px; }
.zu-selected-preview img { width: 84px; aspect-ratio: 2/3; object-fit: cover; border-radius: 10px; background: var(--bg4); }
.zu-selected-preview-title { font-weight: 900; font-size: 14px; }
.zu-selected-preview-sub { color: var(--text3); font-size: 12px; margin: 4px 0 7px; }
.zu-selected-preview-overview { color: var(--text2); font-size: 12px; line-height: 1.45; max-height: 72px; overflow: hidden; }
.zu-episode-list { display: flex; flex-direction: column; gap: 8px; max-height: 360px; overflow: auto; padding-right: 2px; }
.zu-episode-row { display: grid; grid-template-columns: 28px 64px 1fr 64px; gap: 8px; align-items: center; padding: 8px; background: var(--bg2); border: 1px solid var(--border); border-radius: 13px; }
.zu-episode-row input[type=number] { width: 64px; padding: 7px 8px; background: var(--bg3); color: var(--text); border: 1px solid var(--border); border-radius: 10px; }
.zu-episode-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 12.5px; }
.zu-move-btns { display: flex; gap: 4px; }
.zu-mini-btn { width: 28px; height: 28px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg3); color: var(--text2); }
.zu-stream-status { min-height: 18px; color: var(--text3); font-size: 12.5px; margin-top: 8px; }
.zu-progress-drawer { position: fixed; right: 18px; bottom: 18px; width: min(430px, calc(100vw - 36px)); max-height: 70vh; overflow: hidden; z-index: 170; display: none; }
.zu-progress-drawer.open { display: block; }
.zu-progress-shell { background: var(--bg2); border: 1px solid var(--border2); border-radius: 22px; box-shadow: var(--shadow); overflow: hidden; }
.zu-progress-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 14px; background: linear-gradient(135deg, var(--accent), #7c5ef5); color: #fff; }
.zu-progress-list { padding: 8px; max-height: 56vh; overflow-y: auto; }
.zu-progress-card { display: grid; grid-template-columns: 68px 1fr; gap: 11px; margin-bottom: 8px; padding: 10px; border-radius: 18px; background: linear-gradient(135deg, var(--bg2), var(--bg3)); border: 1px solid var(--border); }
.zu-progress-poster { width: 68px; height: 98px; border-radius: 12px; background: linear-gradient(135deg,#263143,#151923); overflow: hidden; }
.zu-progress-poster img { width: 100%; height: 100%; object-fit: cover; }
.zu-progress-title { font-weight: 900; font-size: 13.5px; margin-bottom: 3px; }
.zu-progress-meta { color: var(--text2); font-size: 12px; margin-bottom: 8px; }
.zu-progress-track { height: 9px; background: var(--border); border-radius: 99px; overflow: hidden; }
.zu-progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent), #7c5ef5, var(--green)); border-radius: 99px; transition: width .3s ease; }
.zu-progress-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; color: var(--text2); font-size: 12px; }
.zu-progress-card.done { border-color: rgba(52,199,123,.45); background: linear-gradient(135deg, rgba(52,199,123,.14), var(--bg2)); }
.zu-progress-card.failed { border-color: rgba(255,92,92,.45); background: linear-gradient(135deg, rgba(255,92,92,.14), var(--bg2)); }
@media (max-width: 760px) { .zu-stream-grid { grid-template-columns: 1fr; } .zu-stream-modal { border-radius: 18px; } .zu-episode-row { grid-template-columns: 28px 56px 1fr; } .zu-move-btns { display: none; } }

.ram-divider { height: 1px; background: var(--border); margin: 4px 0; }

/* ============ SHARE / KIRIM MODAL ============ */
.share-cart-list { max-height: 140px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 4px; margin-bottom: 12px; background: var(--bg3); }
.share-cart-list:empty::before { content: 'Tidak ada item dalam keranjang'; display: block; padding: 14px; text-align: center; color: var(--text3); font-size: 12.5px; }
.share-cart-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; font-size: 12.5px; border-radius: 6px; }
.share-cart-row + .share-cart-row { border-top: 1px solid var(--border); }
.share-cart-row .scr-icon { width: 24px; height: 24px; border-radius: 5px; display: flex; align-items: center; justify-content: center; font-size: 12px; flex-shrink: 0; }
.share-cart-row .scr-icon.folder { background: var(--yellow-bg); color: var(--yellow); }
.share-cart-row .scr-icon.video { background: var(--purple-bg); color: var(--purple); }
.share-cart-row .scr-icon.generic { background: var(--bg2); color: var(--text3); }
.share-cart-row .scr-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }
.share-result-list { margin-top: 12px; max-height: 200px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.share-result-row { display: flex; align-items: center; gap: 8px; padding: 8px 10px; font-size: 12.5px; border-bottom: 1px solid var(--border); }
.share-result-row:last-child { border-bottom: none; }
.share-result-row i.ok { color: var(--green); }
.share-result-row i.fail { color: var(--red); }
.field-row { display: flex; gap: 8px; align-items: center; }
.field-row > * { flex: 1; }
.kirim-textarea { width: 100%; padding: 10px 12px; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 13px; color: var(--text); font-family: var(--font); resize: vertical; min-height: 64px; outline: none; transition: border var(--transition); }
.kirim-textarea:focus { border-color: var(--accent); }
.kirim-checkbox-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; font-size: 13px; color: var(--text); cursor: pointer; }
.kirim-checkbox-row input { width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }

/* ============ CREATE FOLDER MODAL ============ */
.cf-input { width: 100%; padding: 10px 14px; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 14px; color: var(--text); outline: none; transition: border var(--transition); font-family: var(--font); }
.cf-input:focus { border-color: var(--accent); }

/* "Create folder" inline button (in breadcrumb / dest modal) */
.btn-newfolder { display: inline-flex; align-items: center; gap: 6px; padding: 6px 11px; background: var(--accent-glow); color: var(--accent); border: 1px solid transparent; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
.btn-newfolder:hover { background: var(--accent); color: #fff; }

/* ============ SAVED DESTINATIONS (Copy favorites) ============ */
.saved-dest-wrap { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 0 12px 0; border-bottom: 1px dashed var(--border); margin-bottom: 8px; }
.saved-dest-label { font-size: 11px; font-weight: 600; color: var(--text3); text-transform: uppercase; letter-spacing: 0.4px; flex-basis: 100%; margin-bottom: 2px; }
.saved-dest-chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px 5px 8px; border-radius: 99px; background: var(--bg3); font-size: 12px; color: var(--text); cursor: pointer; transition: all 0.15s; border: 1px solid var(--border); }
.saved-dest-chip:hover { background: var(--accent-glow); color: var(--accent); border-color: var(--accent); }
.saved-dest-chip i { font-size: 12px; color: var(--yellow); }
.saved-dest-chip:hover i { color: var(--accent); }
.saved-dest-chip .sdc-x { background: var(--border); color: var(--text3); border: none; width: 16px; height: 16px; border-radius: 50%; cursor: pointer; font-size: 10px; line-height: 1; padding: 0; display: flex; align-items: center; justify-content: center; }
.saved-dest-chip .sdc-x:hover { background: var(--red); color: #fff; }
.dest-current-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; margin-bottom: 8px; background: var(--bg3); border-radius: var(--radius-sm); border: 1px solid var(--border); }
.dest-current-row .dcr-text { font-size: 12px; color: var(--text2); min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dest-current-row .dcr-text b { color: var(--text); }
.btn-savedest { padding: 5px 10px; font-size: 11.5px; font-weight: 600; border-radius: 6px; border: 1px solid var(--accent); background: transparent; color: var(--accent); cursor: pointer; flex-shrink: 0; transition: all 0.15s; }
.btn-savedest:hover { background: var(--accent); color: #fff; }
.btn-savedest.saved { background: var(--green-bg); color: var(--green); border-color: var(--green); }

/* ============ SEARCH RESULTS DISPLAY ============ */
.search-result-meta { font-size: 11px; color: var(--text3); margin-top: 2px; font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.search-result-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 99px; font-size: 10.5px; font-weight: 600; background: var(--accent-glow); color: var(--accent); margin-right: 6px; }
.search-result-badge.shared { background: var(--purple-bg); color: var(--purple); }

/* Mobile responsive cart panel */
@media (max-width: 768px) {
  .cart-panel { right: 10px; left: 10px; width: auto; bottom: 80px; max-height: 50vh; }
  .copy-fab { bottom: 16px; }
}
</style>
</head>
<body>
<div class="toast-wrap" id="toastWrap"></div>
<div class="sidebar-overlay" id="sidebarOverlay" onclick="closeSidebar()"></div>

<div class="app">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-logo">
      <div class="logo-icon">Z</div>
      <span class="logo-text">Zaein Drive Token</span>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-item active" id="nav-mydrive" onclick="switchView('mydrive')">
        <i class="bi bi-hdd-fill"></i><span>Drive Saya</span>
      </div>
      <div class="nav-item" id="nav-shared" onclick="switchView('shared')">
        <i class="bi bi-share-fill"></i><span>File Dibagikan</span>
      </div>
    </nav>
    <div class="sidebar-bottom">
      <button class="add-from-link-btn" onclick="openAddFromLinkModal()" title="Tempel link Drive untuk salin otomatis">
        <i class="bi bi-link-45deg"></i><span>Tambah dari Link</span>
      </button>
      <div class="token-badge" onclick="openTokenModal()" title="Kelola Token OAuth2">
        <div class="token-dot" id="tokenDot"></div>
        <div class="token-badge-text">
          <div class="tb-label">Token</div>
          <div class="tb-status" id="tokenStatusText">Memeriksa...</div>
        </div>
        <i class="bi bi-gear" style="color:var(--text3);font-size:13px;"></i>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <button class="topbar-btn menu-btn" onclick="toggleSidebar()"><i class="bi bi-list"></i></button>
      <span class="topbar-title" id="topbarTitle">Drive Saya</span>
      <div class="search-wrap">
        <i class="bi bi-search si"></i>
        <input type="text" placeholder="Cari file..." id="searchInput" oninput="filterFiles(this.value)">
      </div>
      <button class="topbar-btn" onclick="refreshFiles()" title="Refresh"><i class="bi bi-arrow-clockwise"></i></button>
      <div class="theme-switch-wrap">
        <label class="theme-switch">
          <input class="theme-switch__checkbox" type="checkbox" id="themeToggle" onchange="onThemeToggle(this.checked)"/>
          <div class="theme-switch__container">
            <div class="theme-switch__clouds"></div>
            <div class="theme-switch__stars-container">
              <svg fill="none" viewBox="0 0 144 55" xmlns="http://www.w3.org/2000/svg">
                <path fill="currentColor" d="M135.831 3.00688C135.055 3.85027 134.111 4.29946 133 4.35447C134.111 4.40947 135.055 4.85867 135.831 5.71123C136.607 6.55462 136.996 7.56303 136.996 8.72727C136.996 7.95722 137.172 7.25134 137.525 6.59129C137.886 5.93124 138.372 5.39954 138.98 5.00535C139.598 4.60199 140.268 4.39114 141 4.35447C139.88 4.2903 138.936 3.85027 138.16 3.00688C137.384 2.16348 136.996 1.16425 136.996 0C136.996 1.16425 136.607 2.16348 135.831 3.00688ZM31 23.3545C32.1114 23.2995 33.0551 22.8503 33.8313 22.0069C34.6075 21.1635 34.9956 20.1642 34.9956 19C34.9956 20.1642 35.3837 21.1635 36.1599 22.0069C36.9361 22.8503 37.8798 23.2903 39 23.3545C38.2679 23.3911 37.5976 23.602 36.9802 24.0053C36.3716 24.3995 35.8864 24.9312 35.5248 25.5913C35.172 26.2513 34.9956 26.9572 34.9956 27.7273C34.9956 26.563 34.6075 25.5546 33.8313 24.7112C33.0551 23.8587 32.1114 23.4095 31 23.3545ZM0 36.3545C1.11136 36.2995 2.05513 35.8503 2.83131 35.0069C3.6075 34.1635 3.99559 33.1642 3.99559 32C3.99559 33.1642 4.38368 34.1635 5.15987 35.0069C5.93605 35.8503 6.87982 36.2903 8 36.3545C7.26792 36.3911 6.59757 36.602 5.98015 37.0053C5.37155 37.3995 4.88644 37.9312 4.52481 38.5913C4.172 39.2513 3.99559 39.9572 3.99559 40.7273C3.99559 39.563 3.6075 38.5546 2.83131 37.7112C2.05513 36.8587 1.11136 36.4095 0 36.3545ZM56.8313 24.0069C56.0551 24.8503 55.1114 25.2995 54 25.3545C55.1114 25.4095 56.0551 25.8587 56.8313 26.7112C57.6075 27.5546 57.9956 28.563 57.9956 29.7273C57.9956 28.9572 58.172 28.2513 58.5248 27.5913C58.8864 26.9312 59.3716 26.3995 59.9802 26.0053C60.5976 25.602 61.2679 25.3911 62 25.3545C60.8798 25.2903 59.9361 24.8503 59.1599 24.0069C58.3837 23.1635 57.9956 22.1642 57.9956 21C57.9956 22.1642 57.6075 23.1635 56.8313 24.0069ZM81 25.3545C82.1114 25.2995 83.0551 24.8503 83.8313 24.0069C84.6075 23.1635 84.9956 22.1642 84.9956 21C84.9956 22.1642 85.3837 23.1635 86.1599 24.0069C86.9361 24.8503 87.8798 25.2903 89 25.3545C88.2679 25.3911 87.5976 25.602 86.9802 26.0053C86.3716 26.3995 85.8864 26.9312 85.5248 27.5913C85.172 28.2513 84.9956 28.9572 84.9956 29.7273C84.9956 28.563 84.6075 27.5546 83.8313 26.7112C83.0551 25.8587 82.1114 25.4095 81 25.3545ZM136 36.3545C137.111 36.2995 138.055 35.8503 138.831 35.0069C139.607 34.1635 139.996 33.1642 139.996 32C139.996 33.1642 140.384 34.1635 141.16 35.0069C141.936 35.8503 142.88 36.2903 144 36.3545C143.268 36.3911 142.598 36.602 141.98 37.0053C141.372 37.3995 140.886 37.9312 140.525 38.5913C140.172 39.2513 139.996 39.9572 139.996 40.7273C139.996 39.563 139.607 38.5546 138.831 37.7112C138.055 36.8587 137.111 36.4095 136 36.3545ZM101.831 49.0069C101.055 49.8503 100.111 50.2995 99 50.3545C100.111 50.4095 101.055 50.8587 101.831 51.7112C102.607 52.5546 102.996 53.563 102.996 54.7273C102.996 53.9572 103.172 53.2513 103.525 52.5913C103.886 51.9312 104.372 51.3995 104.98 51.0053C105.598 50.602 106.268 50.3911 107 50.3545C105.88 50.2903 104.936 49.8503 104.16 49.0069C103.384 48.1635 102.996 47.1642 102.996 46C102.996 47.1642 102.607 48.1635 101.831 49.0069Z" clip-rule="evenodd" fill-rule="evenodd"></path>
              </svg>
            </div>
            <div class="theme-switch__circle-container">
              <div class="theme-switch__sun-moon-container">
                <div class="theme-switch__moon">
                  <div class="theme-switch__spot"></div>
                  <div class="theme-switch__spot"></div>
                  <div class="theme-switch__spot"></div>
                </div>
              </div>
            </div>
            <div class="theme-switch__shooting-star"></div>
            <div class="theme-switch__shooting-star-2"></div>
            <div class="theme-switch__meteor"></div>
            <div class="theme-switch__stars-cluster">
              <div class="star"></div><div class="star"></div><div class="star"></div>
              <div class="star"></div><div class="star"></div>
            </div>
            <div class="theme-switch__aurora"></div>
            <div class="theme-switch__comets">
              <div class="comet"></div><div class="comet"></div>
            </div>
          </div>
        </label>
      </div>
    </div>

    <div class="breadcrumb-bar" id="breadcrumb"></div>
    <div class="selection-bar" id="selectionBar">
      <span class="sel-count" id="selCount">0 dipilih</span>
      <span class="sel-clear" onclick="clearSelection()">Batal pilih</span>
    </div>
    <!-- Pagination progress indicator -->
    <div class="load-progress hidden" id="loadProgress">
      <span class="spinner" style="width:14px;height:14px;border-width:2px;margin-right:0;"></span>
      <span id="loadProgressText">Memuat semua file...</span>
    </div>

    <div class="file-area">
      <table class="file-table">
        <thead>
          <tr>
            <th><input type="checkbox" id="selectAll" onchange="toggleSelectAll(this.checked)" style="width:15px;height:15px;cursor:pointer;accent-color:var(--accent)"></th>
            <th>Nama</th>
            <th class="col-quality">Kualitas</th>
            <th class="col-format">Format</th>
            <th class="col-count">Isi</th>
            <th class="col-size">Ukuran</th>
            <th class="col-dur">Durasi</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="fileList">
          <tr class="loading-row"><td colspan="8"><span class="spinner"></span>Memuat...</td></tr>
        </tbody>
      </table>
    </div>
  </main>
</div>

<button class="copy-fab" id="copyFab" onclick="openCopyDestModal()">
  <i class="bi bi-clipboard-plus"></i>
  <span id="copyFabLabel">Salin ke sini</span>
</button>

<!-- ============ FLOATING CART PANEL (Persistent across navigation/search) ============ -->
<aside class="cart-panel" id="cartPanel">
  <div class="cart-panel-header">
    <i class="bi bi-bag-heart-fill"></i>
    <span class="cph-title">Daftar Pilihan</span>
    <span class="cph-count" id="cartCount">0</span>
    <button class="cph-close" onclick="closeCartPanel()" title="Tutup keranjang"><i class="bi bi-x-lg"></i></button>
  </div>
  <div class="cart-panel-body" id="cartBody"></div>
  <div class="cart-panel-actions">
    <button class="cart-act-btn lapor" onclick="cartActionLapor()" title="Laporkan semua item"><i class="bi bi-flag-fill"></i><span>LAPOR</span></button>
    <button class="cart-act-btn kirim" onclick="cartActionKirim()" title="Bagikan ke email lain"><i class="bi bi-send-fill"></i><span>KIRIM</span></button>
    <button class="cart-act-btn salin" onclick="cartActionSalin()" title="Salin ke folder"><i class="bi bi-clipboard-plus"></i><span>SALIN</span></button>
  </div>
</aside>

<!-- Per-row 3-dot menu (one element, repositioned on demand) -->
<div class="row-actions-menu" id="rowActionsMenu">
  <div class="ram-item lapor" onclick="rowMenuAction('lapor')"><i class="bi bi-flag-fill"></i> Lapor</div>
  <div class="ram-item kirim" onclick="rowMenuAction('kirim')"><i class="bi bi-send-fill"></i> Kirim Akses</div>
  <div class="ram-divider"></div>
  <div class="ram-item salin" onclick="rowMenuAction('salin')"><i class="bi bi-clipboard-plus"></i> Salin</div>
  <div class="ram-item rename" onclick="rowMenuAction('rename')"><i class="bi bi-pencil-square"></i> Ganti Nama</div>
  <div class="ram-divider"></div>
  <div class="ram-item stream-movie" onclick="rowMenuAction('upload_movie')"><i class="bi bi-cloud-arrow-up-fill"></i> Upload Movie</div>
  <div class="ram-item stream-series" onclick="rowMenuAction('upload_series')"><i class="bi bi-collection-play-fill"></i> Upload Series</div>
  <div class="ram-item stream-progress" onclick="rowMenuAction('progress_upload')"><i class="bi bi-activity"></i> Progres Upload</div>
</div>

<!-- RENAME MODAL -->
<div class="modal-overlay" id="renameModal">
  <div class="modal" style="max-width:480px;">
    <div class="modal-header">
      <span class="modal-title"><i class="bi bi-pencil-square" style="color:#f59e0b;"></i> Ganti Nama</span>
      <button class="modal-close" onclick="closeModal('renameModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div style="font-size:12.5px;color:var(--text3);margin-bottom:6px;">Nama lama:</div>
      <div id="renameOldName" style="padding:8px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;font-size:13px;color:var(--text2);margin-bottom:14px;word-break:break-word;">—</div>
      <div style="font-size:12.5px;color:var(--text3);margin-bottom:6px;">Nama baru:</div>
      <input type="text" id="renameInput" style="width:100%;padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13.5px;" onkeydown="if(event.key==='Enter'){submitRename();}" />
      <div id="renameStatus" style="margin-top:8px;min-height:18px;font-size:12.5px;color:var(--text3);"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary btn-sm" onclick="closeModal('renameModal')">Batal</button>
      <button class="btn btn-primary btn-sm" id="renameSubmitBtn" onclick="submitRename()"><i class="bi bi-check-lg"></i> Simpan</button>
    </div>
  </div>
</div>

<!-- ============ KIRIM (SHARE) MODAL ============ -->
<div class="modal-overlay" id="kirimModal">
  <div class="modal" style="max-width:520px;">
    <div class="modal-header">
      <span class="modal-title"><i class="bi bi-send-fill" style="color:var(--green);"></i> Bagikan Akses Drive</span>
      <button class="modal-close" onclick="closeModal('kirimModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div style="font-size:12.5px;color:var(--text2);margin-bottom:8px;">Item yang akan dibagikan (<span id="kirimCount">0</span>):</div>
      <div class="share-cart-list" id="kirimCartList"></div>

      <div class="form-field" style="margin-bottom:12px;">
        <label>Email Penerima</label>
        <input type="email" id="kirimEmail" placeholder="contoh@email.com" style="font-family:var(--mono);">
        <span class="field-hint">Akan diberi akses sebagai <b style="color:var(--accent);">Pelihat (Viewer)</b> — tidak bisa edit.</span>
      </div>

      <label class="kirim-checkbox-row">
        <input type="checkbox" id="kirimNotify" checked>
        <span>Beri tahu lewat email Google Drive</span>
      </label>

      <div class="form-field" id="kirimMsgWrap" style="margin-top:8px;">
        <label>Pesan untuk Penerima (opsional)</label>
        <textarea class="kirim-textarea" id="kirimMessage" placeholder="Tulis pesan singkat... (mis: Halo, ini link film yang kamu minta)"></textarea>
      </div>

      <div id="kirimStatus" style="margin-top:12px;font-size:12.5px;color:var(--text2);min-height:16px;"></div>
      <div class="share-result-list" id="kirimResultList" style="display:none;"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary btn-sm" onclick="closeModal('kirimModal')">Batal</button>
      <button class="btn btn-sm" id="kirimSendBtn" onclick="executeKirim()" style="background:var(--green);color:#fff;border:none;"><i class="bi bi-send-fill"></i> Bagikan Sekarang</button>
    </div>
  </div>
</div>

<!-- ============ CREATE FOLDER MODAL ============ -->
<div class="modal-overlay" id="createFolderModal">
  <div class="modal" style="max-width:420px;">
    <div class="modal-header">
      <span class="modal-title"><i class="bi bi-folder-plus" style="color:var(--accent);"></i> Buat Folder Baru</span>
      <button class="modal-close" onclick="closeModal('createFolderModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div style="font-size:12.5px;color:var(--text2);margin-bottom:8px;">Folder dibuat di: <b id="cfParentName" style="color:var(--text);">—</b></div>
      <div class="form-field">
        <label>Nama Folder</label>
        <input type="text" class="cf-input" id="cfNameInput" placeholder="Folder baru">
      </div>
      <div id="cfStatus" style="margin-top:10px;font-size:12.5px;color:var(--text2);min-height:16px;"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary btn-sm" onclick="closeModal('createFolderModal')">Batal</button>
      <button class="btn btn-primary btn-sm" id="cfCreateBtn" onclick="executeCreateFolder()"><i class="bi bi-folder-plus"></i> Buat</button>
    </div>
  </div>
</div>

<!-- CLEANUP PROGRESS MODAL (centered) -->
<div class="modal-overlay" id="cleanupOverlay">
  <div class="modal cleanup-modal">
    <div class="modal-header">
      <span class="modal-title"><i class="bi bi-magic"></i> <span id="coTitleText">Bersihkan nama file/folder</span></span>
      <button class="modal-close" onclick="closeCleanupOverlay()"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div class="co-stats">
        <div class="co-stat"><div class="co-stat-num" id="coStatFolders">0</div><div class="co-stat-label">Folder dipindai</div></div>
        <div class="co-stat"><div class="co-stat-num" id="coStatItems">0</div><div class="co-stat-label">Total item</div></div>
        <div class="co-stat highlight" id="coStatMatchBox"><div class="co-stat-num" id="coStatMatches">0</div><div class="co-stat-label">Cocok</div></div>
      </div>
      <div class="co-current" id="coCurrent" style="display:none;"><i class="bi bi-folder2-open"></i><span id="coCurrentText">Memulai pemindaian...</span></div>
      <div class="co-progressbar" id="coProgressbarWrap" style="display:none;"><div class="co-progressbar-inner" id="coProgressBarInner"></div></div>
      <div class="co-list" id="coList"></div>
    </div>
    <div class="modal-footer">
      <div class="co-summary" id="coSummary" style="margin-right:auto;"></div>
      <button class="btn btn-secondary btn-sm" id="coCancelBtn" onclick="closeCleanupOverlay()">Batal</button>
      <button class="btn btn-primary btn-sm" id="coRunBtn" onclick="confirmCleanupRun()" style="display:none;"><i class="bi bi-magic"></i> <span id="coRunBtnText">Bersihkan Sekarang</span></button>
    </div>
  </div>
</div>

<!-- REPORT FOLDER MODAL -->
<div class="modal-overlay" id="reportModal">
  <div class="modal" style="max-width:460px;">
    <div class="modal-header">
      <span class="modal-title"><i class="bi bi-flag-fill" style="color:var(--red);"></i> Laporkan Folder</span>
      <button class="modal-close" onclick="closeModal('reportModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div style="font-size:14px;color:var(--text);margin-bottom:14px;">Yakin <span id="rpKindLabel">folder</span> ini bermasalah? Akan dikirim ke akak.</div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;display:flex;align-items:center;gap:10px;">
        <i id="rpKindIcon" class="bi bi-folder-fill" style="font-size:24px;color:var(--accent);flex-shrink:0;"></i>
        <div style="min-width:0;flex:1;">
          <div id="rpName" style="font-weight:600;font-size:13.5px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>
          <div id="rpLink" style="font-size:11.5px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);"></div>
        </div>
      </div>
      <div id="rpStatus" style="margin-top:12px;font-size:12.5px;color:var(--text2);min-height:16px;"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary btn-sm" onclick="closeModal('reportModal')">Batal</button>
      <button class="btn btn-sm" id="rpSendBtn" onclick="submitReport()" style="background:var(--red);color:#fff;border:none;"><i class="bi bi-send-fill"></i> Kirim Laporan</button>
    </div>
  </div>
</div>

<!-- OAUTH TOKEN MODAL -->
<div class="modal-overlay" id="tokenModal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">🔑 Konfigurasi Google Drive OAuth2</span>
      <button class="modal-close" onclick="closeModal('tokenModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div class="oauth-config-wrap">
        <div class="oauth-intro">Masukkan <strong>Client ID</strong>, <strong>Client Secret</strong>, dan <strong>Refresh Token</strong> dari Google OAuth2. Konfigurasi disimpan ke Supabase dan langsung digunakan tanpa perlu deploy ulang.</div>
        <div id="tokenStatusBlock" class="token-status-block inactive">
          <i class="bi bi-x-circle-fill"></i><span id="tokenStatusMsg">Token tidak aktif</span>
        </div>
        <div class="form-field">
          <label>Client ID</label>
          <input type="text" id="oaClientId" placeholder="xxxxxx.apps.googleusercontent.com">
          <span class="field-hint">Dari Google Cloud Console → Credentials → OAuth 2.0 Client IDs</span>
        </div>
        <div class="form-field">
          <label>Client Secret</label>
          <input type="password" id="oaClientSecret" placeholder="Client secret dari OAuth2 credential">
        </div>
        <div class="form-field">
          <label>Refresh Token</label>
          <input type="text" id="oaRefreshToken" placeholder="1//04xDEj...">
          <span class="field-hint">Didapat dari <a href="https://developers.google.com/oauthplayground" target="_blank" style="color:var(--accent);">OAuth Playground</a></span>
        </div>
        <div id="oauthActiveBox" class="oauth-active-box" style="display:none;">
          <div class="oab-title"><i class="bi bi-check-circle-fill"></i> OAuth2 Drive Aktif</div>
          <div class="oab-row"><span class="oab-label">🆔 Client ID:</span><span class="oab-val" id="oabClientId">—</span></div>
          <div class="oab-row"><span class="oab-label">🔄 Refresh Token:</span><span class="oab-val" id="oabRefreshToken">—</span></div>
          <div class="oab-row"><span class="oab-label">🕐 Disimpan:</span><span class="oab-val" id="oabSavedAt">—</span></div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-danger btn-sm" id="oaClearBtn" onclick="clearOAuthCreds()" style="display:none;margin-right:auto;"><i class="bi bi-trash"></i> Hapus</button>
      <button class="btn btn-test btn-sm" onclick="testOAuthConnection()"><i class="bi bi-plug"></i> Test Koneksi</button>
      <button class="btn btn-secondary btn-sm" onclick="closeModal('tokenModal')">Tutup</button>
      <button class="btn btn-primary btn-sm" onclick="saveOAuthCreds()"><i class="bi bi-floppy"></i> Simpan</button>
    </div>
  </div>
</div>

<!-- COPY DEST MODAL -->
<div class="modal-overlay" id="copyDestModal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">Pilih Lokasi Tujuan</span>
      <button class="modal-close" onclick="closeModal('copyDestModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <!-- Saved destinations (favorites) -->
      <div class="saved-dest-wrap" id="savedDestWrap" style="display:none;">
        <span class="saved-dest-label">Tempat Tersimpan</span>
        <!-- chips populated dynamically -->
      </div>
      <div class="dest-breadcrumb" id="destBreadcrumb"></div>
      <!-- current location + save button + create folder -->
      <div class="dest-current-row">
        <div class="dcr-text">📍 Saat ini di: <b id="dcrCurrent">Drive Saya</b></div>
        <button class="btn-newfolder" onclick="openCreateFolderModal('dest')" title="Buat folder baru di sini"><i class="bi bi-folder-plus"></i> Folder Baru</button>
        <button class="btn-savedest" id="btnSaveDest" onclick="toggleSaveDest()"><i class="bi bi-bookmark"></i> Simpan</button>
      </div>
      <div class="dest-list" id="destFolderList"><div style="padding:20px;text-align:center;color:var(--text3);font-size:13px;"><span class="spinner"></span>Memuat...</div></div>
    </div>
    <div class="modal-footer">
      <div style="font-size:11.5px;color:var(--text3);display:flex;align-items:center;gap:6px;flex:1;"><i class="bi bi-magic"></i><span>Nama otomatis dibersihkan dari kata di "Hapus kata"</span></div>
      <button class="btn btn-secondary btn-sm" onclick="closeModal('copyDestModal')">Batal</button>
      <button class="btn btn-primary btn-sm" onclick="executeCopy()"><i class="bi bi-clipboard-check"></i> Salin di Sini</button>
    </div>
  </div>
</div>

<!-- SUCCESS MODAL -->
<div class="modal-overlay" id="successModal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">Selesai</span>
      <button class="modal-close" onclick="closeModal('successModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div class="success-anim">
        <i class="bi bi-check-circle-fill success-icon"></i>
        <div class="success-title" id="successTitle">Berhasil disalin!</div>
        <div class="success-sub" id="successSub"></div>
        <div class="copy-result-list" id="copyResultList"></div>
      </div>
    </div>
    <div class="modal-footer"><button class="btn btn-primary btn-sm" onclick="closeModal('successModal')">Oke</button></div>
  </div>
</div>

<!-- PROGRESS MODAL -->
<div class="modal-overlay" id="progressModal">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">Menyalin file...</span></div>
    <div class="modal-body">
      <div style="font-size:13.5px;color:var(--text2);" id="progressText">Mempersiapkan...</div>
      <div class="progress-wrap">
        <div class="progress-bar-outer"><div class="progress-bar-inner" id="progressBar" style="width:0%"></div></div>
        <div class="progress-label" id="progressLabel">0 / 0</div>
      </div>
    </div>
  </div>
</div>

<!-- ADD FROM LINK MODAL (Feature: tambah dari link → otomatis ke salin) -->
<div class="modal-overlay" id="addFromLinkModal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title"><i class="bi bi-link-45deg"></i> Tambah dari Link</span>
      <button class="modal-close" onclick="closeModal('addFromLinkModal')"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text2);margin-bottom:8px;line-height:1.45;">
        Tempel link Google Drive (file atau folder). Bisa banyak sekaligus, pisahkan dengan <b>spasi</b>, <b>baris baru</b>, atau <b>koma</b>.
      </div>
      <textarea id="afLinkInput" rows="6" style="width:100%;padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;font-family:'Menlo','Consolas',monospace;resize:vertical;line-height:1.5;" placeholder="https://drive.google.com/drive/folders/13I64JPCuGld4aZbZwzU5eV2fyrndWQjO?usp=drive_link
https://drive.google.com/file/d/15BVfJR7IMFdBAaMt1f0jj2BmKeOxFdI3/view"></textarea>
      <div id="afLinkStatus" style="margin-top:10px;min-height:20px;font-size:12.5px;color:var(--text3);"></div>
      <div id="afLinkPreview" style="margin-top:6px;max-height:220px;overflow-y:auto;"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary btn-sm" onclick="closeModal('addFromLinkModal')">Batal</button>
      <button class="btn btn-primary btn-sm" id="afLinkSubmit" onclick="processAddFromLink()"><i class="bi bi-cloud-download"></i> Ambil & Salin</button>
    </div>
  </div>
</div>

<script>
// ===== SUPABASE =====
const SUPA_URL  = 'https://awfpxjwfjtyovbrpbcar.supabase.co';
const SUPA_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF3ZnB4andmanR5b3ZicnBiY2FyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYwMTMyODQsImV4cCI6MjA5MTU4OTI4NH0.f6xxjNwCJ8sVhzh_lprzmNs6kU-SQlr12sMM3mtpNzI';
const { createClient } = supabase;
const sb = createClient(SUPA_URL, SUPA_ANON);

// ===== OAUTH =====
let oauthCfg = null, _accessToken = null, _accessTokenExpiry = 0;

async function getAccessToken() {
  if (_accessToken && Date.now() < _accessTokenExpiry) return _accessToken;
  if (!oauthCfg) {
    const cfg = await loadOAuthFromSupabase();
    if (!cfg) throw new Error('OAuth Drive belum dikonfigurasi. Buka Kelola Token dulu.');
    oauthCfg = cfg;
  }
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: oauthCfg.client_id, client_secret: oauthCfg.client_secret, refresh_token: oauthCfg.refresh_token, grant_type: 'refresh_token' }),
  });
  if (!res.ok) throw new Error('Gagal mendapatkan access token: ' + res.status);
  const data = await res.json();
  if (data.error) throw new Error(data.error_description || data.error);
  _accessToken = data.access_token;
  _accessTokenExpiry = Date.now() + 3500 * 1000;
  return _accessToken;
}

async function loadOAuthFromSupabase() {
  try {
    const { data, error } = await sb.from('settings').select('value').eq('key', 'drive_oauth').single();
    if (error || !data) return null;
    const cfg = JSON.parse(data.value);
    if (!cfg.client_id || !cfg.client_secret || !cfg.refresh_token) return null;
    return cfg;
  } catch { return null; }
}

async function driveAPI(path, method = 'GET', body = null, params = {}) {
  const token = await getAccessToken();
  const qs = new URLSearchParams(params).toString();
  const url = 'https://www.googleapis.com/drive/v3' + path + (qs ? '?' + qs : '');
  const headers = { Authorization: 'Bearer ' + token };
  const opts = { method, headers };
  if (body) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  return fetch(url, opts);
}

// ===== STATE =====
let currentView = 'mydrive';
let currentPath = [{ id: 'root', name: 'Drive Saya' }];
let allFiles = [];
// CART = unified selection that persists across folder navigation, search, view switch.
// Map<id, { id, name, mimeType, isFolder, link, fromPath }>
let cart = new Map();
let destPath = [{ id: 'root', name: 'Drive Saya' }];
let selectedDestFolderId = 'root';
// Search state
let _searchQuery = '';
let _searchTimer = null;
let _searchAbort = null;
let _isSearching = false;
let _searchResults = [];
// When user navigates INTO a folder while in search mode, we save the search context
// here so we can show a "Back to search" pill in the breadcrumb. Persists across
// further sub-folder navigation; cleared on view change / new search / back click.
let _searchOrigin = null; // { query, results, view, currentPath }
// Row menu target
let _rowMenuTarget = null;
// Backwards-compat shim: code still references selectedIds in some places.
// Provide a Set-like wrapper that delegates to cart.
const selectedIds = {
  get size() { return cart.size; },
  has: (id) => cart.has(id),
  add: (id) => { /* deprecated path; use addToCart */ },
  delete: (id) => cart.delete(id),
  clear: () => cart.clear(),
  [Symbol.iterator]: function*() { for (const k of cart.keys()) yield k; },
};

// scroll memory per-folder + cleanup keywords state
const _scrollMemory = new Map();
function _saveScroll() {
  const fa = document.querySelector('.file-area');
  if (!fa || !currentPath.length) return;
  const id = currentPath[currentPath.length - 1].id;
  _scrollMemory.set(id, fa.scrollTop);
}
function _restoreScroll() {
  const fa = document.querySelector('.file-area');
  if (!fa || !currentPath.length) return;
  const id = currentPath[currentPath.length - 1].id;
  fa.scrollTop = _scrollMemory.get(id) || 0;
}
let cleanupKeywords = ['Salinan', 'Salinan dari', 'Copy of', 'Copy'];
let _cleanupKwLoaded = false;

// ===== INIT =====
document.addEventListener('DOMContentLoaded', async () => {
  applyTheme(localStorage.getItem('zaein_theme') || 'light');
  await loadOAuthInfo();
  loadFiles();
});

// ===== THEME =====
function onThemeToggle(isDark) { applyTheme(isDark ? 'dark' : 'light'); }
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('zaein_theme', t);
  const toggle = document.getElementById('themeToggle');
  if (toggle) toggle.checked = (t === 'dark');
}

// ===== SIDEBAR =====
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); document.getElementById('sidebarOverlay').classList.toggle('visible'); }
function closeSidebar() { document.getElementById('sidebar').classList.remove('open'); document.getElementById('sidebarOverlay').classList.remove('visible'); }

// ===== VIEW SWITCH =====
// Note: cart (selection) PERSISTS across view switches and folder navigation.
// User must explicitly close the cart panel to clear it.
function switchView(view) {
  _saveScroll();
  currentView = view;
  _scrollMemory.clear();
  // Clear search if switching view
  const si = document.getElementById('searchInput'); if (si) si.value = '';
  _searchQuery = ''; _isSearching = false; _searchResults = []; _searchOrigin = null;
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.getElementById('nav-' + view).classList.add('active');
  if (view === 'mydrive') { currentPath = [{ id: 'root', name: 'Drive Saya' }]; document.getElementById('topbarTitle').textContent = 'Drive Saya'; }
  else { currentPath = [{ id: 'shared', name: 'File Dibagikan' }]; document.getElementById('topbarTitle').textContent = 'File Dibagikan'; }
  closeSidebar(); loadFiles();
  updateSelectionUI();
}

// ===== BREADCRUMB =====
function renderBreadcrumb() {
  const bar = document.getElementById('breadcrumb');
  bar.innerHTML = '';
  // "Back to search" pill — appears when user navigated INTO a folder from search results
  if (_searchOrigin) {
    const back = document.createElement('button');
    back.className = 'bc-back-search';
    back.title = 'Kembali ke hasil pencarian: ' + _searchOrigin.query;
    back.innerHTML = \`<i class="bi bi-arrow-left-circle-fill"></i><span>Kembali ke pencarian:</span><span class="bbs-q">"\${escHtml(_searchOrigin.query)}"</span>\`;
    back.onclick = (e) => { e.stopPropagation(); backToSearch(); };
    bar.appendChild(back);
    const sepWrap = document.createElement('span');
    sepWrap.className = 'bc-back-search-sep';
    sepWrap.textContent = '›';
    bar.appendChild(sepWrap);
  }
  currentPath.forEach((item, idx) => {
    const span = document.createElement('span');
    span.className = 'bc-item' + (idx === currentPath.length - 1 ? ' active' : '');
    span.innerHTML = (idx === 0 ? \`<i class="bi bi-\${currentView === 'shared' ? 'share' : 'hdd'}"></i> \` : '') + escHtml(item.name);
    if (idx < currentPath.length - 1) span.onclick = () => navigateTo(idx);
    bar.appendChild(span);
    if (idx < currentPath.length - 1) { const sep = document.createElement('span'); sep.className = 'bc-sep'; sep.textContent = '/'; bar.appendChild(sep); }
  });
  // "+ Folder Baru" button in My Drive view (any level — including root)
  const last = currentPath[currentPath.length - 1];
  if (currentView === 'mydrive') {
    const newFolderBtn = document.createElement('button');
    newFolderBtn.className = 'btn-newfolder';
    newFolderBtn.style.marginLeft = '6px';
    newFolderBtn.title = 'Buat folder baru di sini';
    newFolderBtn.innerHTML = '<i class="bi bi-folder-plus"></i> Folder Baru';
    newFolderBtn.onclick = (e) => { e.stopPropagation(); openCreateFolderModal('mydrive'); };
    bar.appendChild(newFolderBtn);
  }
  // Action buttons (copy folder + cleanup) — only when inside a real subfolder
  if (currentPath.length > 1 && last.id && last.id !== 'root' && last.id !== 'shared') {
    const link = buildGDriveLink(last.id, true);
    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-link-btn bc-copy-btn';
    copyBtn.title = 'Salin link folder';
    copyBtn.innerHTML = '<svg viewBox="0 0 384 512" height="1em" xmlns="http://www.w3.org/2000/svg" class="clipboard"><path d="M280 64h40c35.3 0 64 28.7 64 64V448c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V128C0 92.7 28.7 64 64 64h40 9.6C121 27.5 153.3 0 192 0s71 27.5 78.4 64H280zM64 112c-8.8 0-16 7.2-16 16V448c0 8.8 7.2 16 16 16H320c8.8 0 16-7.2 16-16V128c0-8.8-7.2-16-16-16H304v24c0 13.3-10.7 24-24 24H192 104c-13.3 0-24-10.7-24-24V112H64zm128-8a24 24 0 1 0 0-48 24 24 0 1 0 0 48z"></path></svg><svg viewBox="0 0 384 512" height="1em" xmlns="http://www.w3.org/2000/svg" class="clipboard-check"><path d="M192 0c-41.8 0-77.4 26.7-90.5 64H64C28.7 64 0 92.7 0 128V448c0 35.3 28.7 64 64 64H320c35.3 0 64-28.7 64-64V128c0-35.3-28.7-64-64-64H282.5C269.4 26.7 233.8 0 192 0zm0 64a32 32 0 1 1 0 64 32 32 0 1 1 0-64zM305 273L177 401c-9.4 9.4-24.6 9.4-33.9 0L79 337c-9.4-9.4-9.4-24.6 0-33.9s24.6-9.4 33.9 0l47 47L271 239c9.4-9.4 24.6-9.4 33.9 0s9.4 24.6 0 33.9z"></path></svg>';
    copyBtn.onclick = (e) => copyLink(e, link, copyBtn);
    bar.appendChild(copyBtn);

    const cleanupBtn = document.createElement('button');
    cleanupBtn.id = 'cleanupBtn';
    cleanupBtn.className = 'copy-link-btn bc-cleanup-btn';
    cleanupBtn.title = 'Bersihkan kata "Salinan", "Copy", dll dari nama file/folder di sini';
    cleanupBtn.innerHTML = '<i class="bi bi-magic"></i>';
    cleanupBtn.onclick = (e) => { e.stopPropagation(); toggleCleanupDropdown(); };
    bar.appendChild(cleanupBtn);

    const reportBtn = document.createElement('button');
    reportBtn.id = 'reportBtn';
    reportBtn.className = 'copy-link-btn bc-report-btn';
    reportBtn.title = 'Laporkan folder ini bermasalah';
    reportBtn.innerHTML = '<i class="bi bi-flag-fill"></i>';
    reportBtn.onclick = (e) => { e.stopPropagation(); openReportModal(last.id, last.name, 'folder'); };
    bar.appendChild(reportBtn);

    // Kirim — add THIS folder to cart (so user can send/share it from the right-side panel)
    const kirimBtn = document.createElement('button');
    kirimBtn.id = 'kirimFolderBtn';
    const alreadyInCart = cart.has(last.id);
    kirimBtn.className = 'bc-kirim-btn' + (alreadyInCart ? ' in-cart' : '');
    kirimBtn.title = alreadyInCart ? 'Folder ini sudah di keranjang' : 'Tambahkan folder ini ke keranjang';
    kirimBtn.innerHTML = alreadyInCart
      ? '<i class="bi bi-bag-check-fill"></i> Di keranjang'
      : '<i class="bi bi-send-fill"></i> Kirim';
    kirimBtn.onclick = (e) => {
      e.stopPropagation();
      if (cart.has(last.id)) {
        // Already in cart — just bump the panel so user notices
        const panel = document.getElementById('cartPanel');
        if (panel) {
          panel.classList.add('open');
          panel.classList.add('bump');
          setTimeout(() => panel.classList.remove('bump'), 460);
        }
        return;
      }
      const item = {
        id: last.id,
        name: last.name,
        mimeType: 'application/vnd.google-apps.folder',
        isFolder: true,
        link: buildGDriveLink(last.id, true),
        fromPath: currentPath.slice(0, -1).map(p => p.name).join(' / '),
      };
      addToCart(item, kirimBtn);
      // Refresh breadcrumb to flip the button into "Di keranjang" state
      renderBreadcrumb();
    };
    bar.appendChild(kirimBtn);

    const dd = document.createElement('div');
    dd.className = 'cleanup-dropdown';
    dd.id = 'cleanupDropdown';
    dd.innerHTML = \`
      <div class="cd-title"><i class="bi bi-magic"></i> Hapus kata dari nama file/folder</div>
      <div class="cd-list" id="cdList"></div>
      <div class="cd-add">
        <input type="text" id="cdInput" placeholder="Tambah kata (mis: Salinan, -KQRM)" onkeydown="if(event.key==='Enter'){addCleanupKeyword();}">
        <button onclick="addCleanupKeyword()"><i class="bi bi-plus-lg"></i></button>
      </div>
      <button class="cd-run" onclick="startCleanup()"><i class="bi bi-magic"></i> Bersihkan di "\${escHtml(last.name)}"</button>
    \`;
    bar.appendChild(dd);
  }
}
function navigateTo(idx) { _saveScroll(); currentPath = currentPath.slice(0, idx + 1); loadFiles(); }

// ===== LOAD ALL FILES — full pagination, pageSize 1000 =====
async function loadFiles() {
  renderBreadcrumb();
  const tbody = document.getElementById('fileList');
  const prog = document.getElementById('loadProgress');
  tbody.innerHTML = '<tr class="loading-row"><td colspan="8"><span class="spinner"></span>Memuat...</td></tr>';
  prog.classList.add('hidden');

  if (!oauthCfg) {
    const cfg = await loadOAuthFromSupabase();
    if (cfg) { oauthCfg = cfg; }
    else {
      tbody.innerHTML = \`<tr><td colspan="8"><div class="empty-state"><i class="bi bi-plug-fill"></i><p>Konfigurasi OAuth Drive belum diatur</p><button class="btn btn-primary btn-sm" onclick="openTokenModal()"><i class="bi bi-gear"></i> Kelola Token</button></div></td></tr>\`;
      return;
    }
  }

  try {
    const isShared = currentView === 'shared' && currentPath.length === 1;
    const parentId = currentPath[currentPath.length - 1].id;
    let files = [], pageToken = null, page = 0;

    do {
      page++;
      if (page > 1) {
        prog.classList.remove('hidden');
        document.getElementById('loadProgressText').textContent = \`Memuat... sudah \${files.length} item\`;
      }

      const params = {
        // Include videoMediaMetadata for resolution (width/height) + duration (durationMillis)
        fields: 'nextPageToken, files(id, name, mimeType, size, modifiedTime, fileExtension, videoMediaMetadata)',
        orderBy: 'folder, name',
        pageSize: '1000',
        includeItemsFromAllDrives: 'true',
        supportsAllDrives: 'true',
      };
      if (isShared) params.q = 'sharedWithMe = true and trashed = false';
      else params.q = \`'\${parentId}' in parents and trashed = false\`;
      if (pageToken) params.pageToken = pageToken;

      const resp = await driveAPI('/files', 'GET', null, params);
      if (!resp.ok) { const err = await resp.json(); throw new Error(err.error?.message || 'Gagal memuat: ' + resp.status); }
      const data = await resp.json();
      files = files.concat(data.files || []);
      pageToken = data.nextPageToken || null;
    } while (pageToken);

    prog.classList.add('hidden');
    allFiles = files;
    renderFiles(allFiles);
  } catch (e) {
    prog.classList.add('hidden');
    tbody.innerHTML = \`<tr><td colspan="8"><div class="empty-state"><i class="bi bi-exclamation-triangle"></i><p>\${escHtml(e.message)}</p></div></td></tr>\`;
  }
}

function refreshFiles() { _saveScroll(); loadFiles(); }

// ===== RENDER FILES =====
function renderFiles(files, opts) {
  opts = opts || {};
  const tbody = document.getElementById('fileList');
  if (!files.length) {
    const emptyMsg = opts.isSearch
      ? \`<i class="bi bi-search"></i><p>Tidak ada hasil untuk "\${escHtml(_searchQuery)}"</p><p style="font-size:11.5px;color:var(--text3);">Hanya folder & file mkv/mp4 yang dicari</p>\`
      : '<i class="bi bi-folder2-open"></i><p>Folder ini kosong</p>';
    tbody.innerHTML = \`<tr><td colspan="8"><div class="empty-state">\${emptyMsg}</div></td></tr>\`;
    _restoreScroll();
    return;
  }
  tbody.innerHTML = '';
  files.forEach(f => {
    const isFolder = f.mimeType === 'application/vnd.google-apps.folder';
    const tr = document.createElement('tr');
    tr.dataset.id = f.id; tr.dataset.name = f.name; tr.dataset.folder = isFolder ? '1' : '0';
    tr.dataset.mime = f.mimeType || '';
    if (cart.has(f.id)) tr.classList.add('selected');
    const icon = getFileIcon(f);
    const sizeHtml = isFolder
      ? \`<span class="size-text size-loading" id="sz-\${f.id}"><span class="spinner" style="width:12px;height:12px;border-width:1.5px;"></span></span>\`
      : \`<span class="size-text">\${formatSize(f.size)}</span>\`;
    const link = buildGDriveLink(f.id, isFolder);

    // Search-mode badge: where the result was found
    let searchBadge = '';
    if (opts.isSearch) {
      const sc = f._scope || 'mine';
      searchBadge = sc === 'shared'
        ? '<span class="search-result-badge shared"><i class="bi bi-share-fill"></i>Dibagikan</span>'
        : '<span class="search-result-badge"><i class="bi bi-hdd-fill"></i>Drive Saya</span>';
    }

    tr.innerHTML = \`
      <td class="cb-wrap"><input type="checkbox" class="file-cb" data-id="\${f.id}" \${cart.has(f.id)?'checked':''} onchange="toggleSelect('\${f.id}',this.checked)"></td>
      <td>
        <div class="file-name-cell" onclick="\${isFolder ? \`openFolder('\${f.id}','\${escAttr(f.name)}')\` : \`previewFile('\${f.id}')\`}">
          <div class="file-icon \${icon.cls}">\${icon.html}</div>
          <div style="min-width:0;flex:1;">
            <div style="display:flex;align-items:center;gap:6px;min-width:0;">\${searchBadge}<span class="file-name" title="\${escAttr(f.name)}">\${escHtml(f.name)}</span></div>
            \${opts.isSearch ? \`<div class="search-result-meta" title="\${escAttr(f._scope==='shared'?'Dari File Dibagikan':'Dari Drive Saya')}">ID: \${escHtml(f.id)}</div>\` : ''}
          </div>
          <button class="copy-link-btn" onclick="copyLink(event,'\${link}',this)" title="Salin link">
            <svg viewBox="0 0 384 512" height="1em" xmlns="http://www.w3.org/2000/svg" class="clipboard"><path d="M280 64h40c35.3 0 64 28.7 64 64V448c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V128C0 92.7 28.7 64 64 64h40 9.6C121 27.5 153.3 0 192 0s71 27.5 78.4 64H280zM64 112c-8.8 0-16 7.2-16 16V448c0 8.8 7.2 16 16 16H320c8.8 0 16-7.2 16-16V128c0-8.8-7.2-16-16-16H304v24c0 13.3-10.7 24-24 24H192 104c-13.3 0-24-10.7-24-24V112H64zm128-8a24 24 0 1 0 0-48 24 24 0 1 0 0 48z"></path></svg>
            <svg viewBox="0 0 384 512" height="1em" xmlns="http://www.w3.org/2000/svg" class="clipboard-check"><path d="M192 0c-41.8 0-77.4 26.7-90.5 64H64C28.7 64 0 92.7 0 128V448c0 35.3 28.7 64 64 64H320c35.3 0 64-28.7 64-64V128c0-35.3-28.7-64-64-64H282.5C269.4 26.7 233.8 0 192 0zm0 64a32 32 0 1 1 0 64 32 32 0 1 1 0-64zM305 273L177 401c-9.4 9.4-24.6 9.4-33.9 0L79 337c-9.4-9.4-9.4-24.6 0-33.9s24.6-9.4 33.9 0l47 47L271 239c9.4-9.4 24.6-9.4 33.9 0s9.4 24.6 0 33.9z"></path></svg>
          </button>
        </div>
      </td>
      <td class="col-quality">\${getQualityBadge(f)}</td>
      <td class="col-format">\${getFormatHtml(f, isFolder)}</td>
      <td class="col-count">\${isFolder ? \`<span class="cc-loading" id="cc-\${f.id}"><span class="spinner" style="width:11px;height:11px;border-width:1.5px;"></span></span>\` : '<span class="cc-dash">—</span>'}</td>
      <td class="col-size">\${sizeHtml}</td>
      <td class="col-dur">\${getDurationHtml(f)}</td>
      <td><button class="row-actions-btn" onclick="openRowMenu(event,'\${f.id}','\${escAttr(f.name)}','\${isFolder?1:0}','\${escAttr(f.mimeType||'')}')" title="Aksi"><i class="bi bi-three-dots-vertical"></i></button></td>\`;
    tbody.appendChild(tr);
    if (isFolder) { loadFolderSizeAsync(f.id); loadFolderCountAsync(f.id); }
  });
  _restoreScroll();
}

// ===== FOLDER SIZE (recursive, paginated, concurrency-limited) =====
const _sizeSem = { active: 0, max: 8, queue: [] };
function _acquireSizeSlot() {
  return new Promise(resolve => {
    if (_sizeSem.active < _sizeSem.max) { _sizeSem.active++; resolve(); }
    else _sizeSem.queue.push(resolve);
  });
}
function _releaseSizeSlot() {
  const next = _sizeSem.queue.shift();
  if (next) next();
  else _sizeSem.active--;
}

async function computeFolderSize(folderId) {
  let total = 0;
  let pageToken = null;
  const subfolders = [];
  await _acquireSizeSlot();
  try {
    do {
      const params = {
        q: \`'\${folderId}' in parents and trashed = false\`,
        fields: 'nextPageToken, files(id, mimeType, size)',
        pageSize: '1000',
        includeItemsFromAllDrives: 'true',
        supportsAllDrives: 'true',
      };
      if (pageToken) params.pageToken = pageToken;
      const r = await driveAPI('/files', 'GET', null, params);
      if (!r.ok) break;
      const d = await r.json();
      for (const f of d.files || []) {
        if (f.mimeType === 'application/vnd.google-apps.folder') {
          subfolders.push(f.id);
        } else {
          total += parseInt(f.size || '0');
        }
      }
      pageToken = d.nextPageToken || null;
    } while (pageToken);
  } finally {
    _releaseSizeSlot();
  }
  if (subfolders.length) {
    const results = await Promise.all(subfolders.map(id => computeFolderSize(id)));
    for (const v of results) total += v;
  }
  return total;
}

async function loadFolderSizeAsync(folderId) {
  try {
    const total = await computeFolderSize(folderId);
    const el = document.getElementById('sz-' + folderId);
    if (el) el.outerHTML = \`<span class="size-text">\${total > 0 ? formatSize(total) : '0 B'}</span>\`;
  } catch {
    const el = document.getElementById('sz-' + folderId);
    if (el) el.outerHTML = '<span class="size-text">—</span>';
  }
}

// ===== FOLDER CHILDREN COUNT (immediate, non-recursive, light) =====
const _countCache = new Map(); // folderId -> { folders, files }
const _countSem = { active: 0, max: 6, queue: [] };
function _acquireCountSlot() {
  return new Promise(resolve => {
    if (_countSem.active < _countSem.max) { _countSem.active++; resolve(); }
    else _countSem.queue.push(resolve);
  });
}
function _releaseCountSlot() {
  const next = _countSem.queue.shift();
  if (next) next();
  else _countSem.active--;
}
function _renderCountInto(folderId, folders, files) {
  const el = document.getElementById('cc-' + folderId);
  if (!el) return;
  let parts = [];
  if (folders > 0) parts.push(\`<span class="cc-num">\${folders}</span> folder\`);
  if (files > 0) parts.push(\`<span class="cc-num">\${files}</span> file\`);
  if (!parts.length) parts.push('<span class="cc-dash">kosong</span>');
  el.outerHTML = \`<span class="cc-val" id="cc-\${folderId}">\${parts.join(', ')}</span>\`;
}
async function loadFolderCountAsync(folderId) {
  // Use cache if already computed
  if (_countCache.has(folderId)) {
    const c = _countCache.get(folderId);
    _renderCountInto(folderId, c.folders, c.files);
    return;
  }
  await _acquireCountSlot();
  try {
    let folders = 0, files = 0, pageToken = null, pageCount = 0;
    do {
      const params = {
        q: \`'\${folderId}' in parents and trashed = false\`,
        fields: 'nextPageToken, files(id, mimeType)',
        pageSize: '1000',
        includeItemsFromAllDrives: 'true',
        supportsAllDrives: 'true',
      };
      if (pageToken) params.pageToken = pageToken;
      const r = await driveAPI('/files', 'GET', null, params);
      if (!r.ok) break;
      const d = await r.json();
      for (const f of d.files || []) {
        if (f.mimeType === 'application/vnd.google-apps.folder') folders++;
        else files++;
      }
      pageToken = d.nextPageToken || null;
      pageCount++;
      if (pageCount > 5) break; // safety cap on huge folders (>5000 items)
    } while (pageToken);
    _countCache.set(folderId, { folders, files });
    _renderCountInto(folderId, folders, files);
  } catch {
    const el = document.getElementById('cc-' + folderId);
    if (el) el.outerHTML = \`<span class="cc-val cc-dash" id="cc-\${folderId}">—</span>\`;
  } finally {
    _releaseCountSlot();
  }
}

function openFolder(id, name) {
  _saveScroll();
  // If a search query is active when user clicks a folder, capture the context
  // so the user can "Back to search" later. Use _searchQuery (not _isSearching)
  // because the cross-drive API call may not have flipped _isSearching yet —
  // user may click a folder from local-filtered results within the debounce.
  // Don't overwrite an existing origin so going several levels deep still works.
  if (_searchQuery && !_searchOrigin) {
    _searchOrigin = {
      query: _searchQuery,
      results: (_searchResults && _searchResults.length) ? _searchResults.slice() : [],
      view: currentView,
      currentPath: currentPath.map(p => ({ ...p })),
    };
  }
  // Clear active search input/state — we're now in a folder
  const si = document.getElementById('searchInput'); if (si && si.value) si.value = '';
  _searchQuery = ''; _isSearching = false; _searchResults = [];
  currentPath.push({ id, name });
  loadFiles();
}
function backToSearch() {
  if (!_searchOrigin) return;
  const o = _searchOrigin;
  _searchOrigin = null;
  // Restore search context (currentPath as it was when the search was active)
  currentPath = o.currentPath.map(p => ({ ...p }));
  _searchQuery = o.query;
  const si = document.getElementById('searchInput'); if (si) si.value = o.query;
  document.getElementById('topbarTitle').textContent = currentView === 'shared' ? 'File Dibagikan' : 'Drive Saya';
  if (o.results && o.results.length) {
    // Use cached results immediately (instant restoration like Google Drive)
    _searchResults = o.results.slice();
    _isSearching = true;
    renderBreadcrumb();
    renderFiles(_searchResults, { isSearch: true });
    updateSelectionUI();
  } else {
    // No cached results — re-run cross-drive search using the saved query
    _searchResults = [];
    _isSearching = false;
    renderBreadcrumb();
    // Show a quick local-filter while the API call runs
    const localFiltered = (allFiles || []).filter(f => f.name.toLowerCase().includes(o.query.toLowerCase()));
    renderFiles(localFiltered);
    runCrossDriveSearch(o.query);
    updateSelectionUI();
  }
}
function filterFiles(q) {
  // Wrapper: when q is empty render full list; when non-empty trigger cross-drive search.
  q = (q || '').trim();
  _searchQuery = q;
  // Starting a new search clears any previous "back-to-search" anchor.
  _searchOrigin = null;
  if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null; }
  if (!q) {
    _isSearching = false; _searchResults = [];
    renderFiles(allFiles);
    renderBreadcrumb();
    return;
  }
  // Always show instant local-folder filter immediately (for snappy feedback)
  // Then debounce a cross-drive Drive API search.
  const localFiltered = allFiles.filter(f => f.name.toLowerCase().includes(q.toLowerCase()));
  renderFiles(localFiltered);
  if (q.length < 2) return; // too short, skip API
  _searchTimer = setTimeout(() => runCrossDriveSearch(q), 220);
}

// ===== CROSS-DRIVE SEARCH (Feature #5) =====
// Searches My Drive + Shared with Me for folders + video files (mkv/mp4/etc).
// Only includes folders + video files (filter pdf/doc/etc) for speed.
async function runCrossDriveSearch(q) {
  if (!q || q !== _searchQuery) return; // stale
  if (_searchAbort) { try { _searchAbort.abort(); } catch {} }
  _searchAbort = new AbortController();
  _isSearching = true;
  try {
    // Escape ' and \\ for Drive query
    const safeQ = q.replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'");
    // Folders + video MIME types (Drive 'q' supports 'mimeType contains' loosely; use OR list).
    // mkv = video/x-matroska, mp4 = video/mp4. We allow any video/* for safety.
    const mimeFilter = "(mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')";
    const baseFields = 'nextPageToken, files(id, name, mimeType, size, modifiedTime, fileExtension, parents, videoMediaMetadata, shared, ownedByMe)';

    // Run TWO parallel queries to cover My Drive + Shared with Me + shared drives.
    const queries = [
      // My Drive + items I own + items in shared drives (corpora=allDrives ensures both)
      driveAPI('/files', 'GET', null, {
        q: \`name contains '\${safeQ}' and \${mimeFilter} and trashed = false\`,
        fields: baseFields,
        orderBy: 'folder, name',
        pageSize: '200',
        includeItemsFromAllDrives: 'true',
        supportsAllDrives: 'true',
        corpora: 'allDrives',
      }),
      // Shared with Me explicitly (corpora=allDrives doesn't always include sharedWithMe)
      driveAPI('/files', 'GET', null, {
        q: \`name contains '\${safeQ}' and \${mimeFilter} and sharedWithMe = true and trashed = false\`,
        fields: baseFields,
        orderBy: 'folder, name',
        pageSize: '200',
        includeItemsFromAllDrives: 'true',
        supportsAllDrives: 'true',
      }),
    ];
    const results = await Promise.allSettled(queries);
    const merged = new Map();
    let scope0 = 'mine', scope1 = 'shared';
    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      if (r.status !== 'fulfilled') continue;
      if (!r.value.ok) continue;
      const d = await r.value.json().catch(() => null);
      if (!d || !d.files) continue;
      for (const f of d.files) {
        if (!merged.has(f.id)) {
          // Tag scope (where it was found) for UI badge
          f._scope = (i === 0 ? scope0 : scope1);
          // Filter again client-side: only keep folders OR mkv/mp4/video extensions
          const isFolder = f.mimeType === 'application/vnd.google-apps.folder';
          const ext = (f.fileExtension || '').toLowerCase();
          const isVideo = (f.mimeType || '').startsWith('video/') || ['mkv','mp4','avi','mov','webm','m4v'].includes(ext);
          if (isFolder || isVideo) merged.set(f.id, f);
        }
      }
    }
    if (q !== _searchQuery) return; // stale
    _searchResults = Array.from(merged.values()).sort((a, b) => {
      const af = a.mimeType === 'application/vnd.google-apps.folder' ? 0 : 1;
      const bf = b.mimeType === 'application/vnd.google-apps.folder' ? 0 : 1;
      if (af !== bf) return af - bf;
      return (a.name || '').localeCompare(b.name || '');
    });
    renderFiles(_searchResults, { isSearch: true });
  } catch (e) {
    console.error('search error', e);
  } finally {
    _isSearching = false;
  }
}

// ===== CART (selection) =====
function _itemFromFile(f) {
  const isFolder = f.mimeType === 'application/vnd.google-apps.folder';
  return {
    id: f.id,
    name: f.name,
    mimeType: f.mimeType || '',
    isFolder,
    link: buildGDriveLink(f.id, isFolder),
    fromPath: currentPath.map(p => p.name).join(' / '),
  };
}
function addToCart(item, fromEl) {
  if (cart.has(item.id)) return false;
  cart.set(item.id, item);
  if (fromEl) flyToCart(fromEl);
  updateSelectionUI();
  return true;
}
function removeFromCart(id) {
  if (!cart.has(id)) return;
  cart.delete(id);
  // Update row visuals if visible
  const row = document.querySelector(\`tr[data-id="\${id}"]\`);
  if (row) {
    row.classList.remove('selected');
    const cb = row.querySelector('.file-cb'); if (cb) cb.checked = false;
  }
  updateSelectionUI();
}
function toggleSelect(id, checked) {
  // Look up the file data either in the current view or the cart
  let f = allFiles.find(x => x.id === id) || (_searchResults || []).find(x => x.id === id);
  if (checked) {
    if (!cart.has(id)) {
      const item = f ? _itemFromFile(f) : { id, name: id, mimeType: '', isFolder: false, link: '', fromPath: '' };
      cart.set(id, item);
      // Animate fly to cart
      const row = document.querySelector(\`tr[data-id="\${id}"]\`);
      if (row) flyToCart(row);
    }
  } else {
    cart.delete(id);
  }
  const row = document.querySelector(\`tr[data-id="\${id}"]\`);
  if (row) row.classList.toggle('selected', checked);
  updateSelectionUI();
}
function toggleSelectAll(checked) {
  const list = _isSearching ? _searchResults : allFiles;
  list.forEach(f => {
    if (checked) {
      if (!cart.has(f.id)) cart.set(f.id, _itemFromFile(f));
    } else {
      cart.delete(f.id);
    }
    const row = document.querySelector(\`tr[data-id="\${f.id}"]\`);
    if (row) { row.classList.toggle('selected', checked); const cb = row.querySelector('.file-cb'); if (cb) cb.checked = checked; }
  });
  updateSelectionUI();
}
function clearSelection() {
  cart.clear();
  document.querySelectorAll('.file-cb').forEach(cb => cb.checked = false);
  document.querySelectorAll('tr.selected').forEach(tr => tr.classList.remove('selected'));
  const sa = document.getElementById('selectAll'); if (sa) sa.checked = false;
  updateSelectionUI();
}
function updateSelectionUI() {
  const count = cart.size;
  document.getElementById('selectionBar').classList.toggle('visible', count > 0);
  document.getElementById('selCount').textContent = count + ' dipilih';
  const fab = document.getElementById('copyFab');
  if (count > 0) { fab.classList.add('visible'); document.getElementById('copyFabLabel').textContent = 'Salin (' + count + ')'; }
  else { fab.classList.remove('visible'); }
  // Refresh cart panel
  renderCartPanel();
  // Refresh breadcrumb so the "Kirim" folder button reflects current cart state.
  // Only when breadcrumb is actually rendered (after first loadFiles).
  if (document.getElementById('breadcrumb')) {
    const kf = document.getElementById('kirimFolderBtn');
    if (kf) {
      const last = currentPath[currentPath.length - 1];
      if (last && last.id) {
        const inCart = cart.has(last.id);
        kf.classList.toggle('in-cart', inCart);
        kf.title = inCart ? 'Folder ini sudah di keranjang' : 'Tambahkan folder ini ke keranjang';
        kf.innerHTML = inCart
          ? '<i class="bi bi-bag-check-fill"></i> Di keranjang'
          : '<i class="bi bi-send-fill"></i> Kirim';
      }
    }
  }
}
function renderCartPanel() {
  const panel = document.getElementById('cartPanel');
  const body = document.getElementById('cartBody');
  const cnt = document.getElementById('cartCount');
  if (!panel) return;
  cnt.textContent = cart.size;
  if (cart.size === 0) {
    panel.classList.remove('open');
    body.innerHTML = '';
    return;
  }
  panel.classList.add('open');
  body.innerHTML = Array.from(cart.values()).map(it => {
    const cls = it.isFolder ? 'folder' : ((it.mimeType||'').startsWith('video/') ? 'video' : 'generic');
    const ic = it.isFolder ? '<i class="bi bi-folder-fill"></i>' : ((it.mimeType||'').startsWith('video/') ? '<i class="bi bi-play-circle-fill"></i>' : '<i class="bi bi-file-earmark"></i>');
    return \`<div class="cart-item" data-cid="\${escAttr(it.id)}" title="\${escAttr(it.name)} — \${escAttr(it.fromPath||'')}">\
      <div class="ci-icon \${cls}">\${ic}</div>\
      <div class="ci-name">\${escHtml(it.name)}</div>\
      <button class="ci-remove" onclick="removeFromCart('\${escAttr(it.id)}')" title="Hapus dari daftar"><i class="bi bi-x-lg"></i></button>\
    </div>\`;
  }).join('');
}
function closeCartPanel() {
  // Closing the panel = clear selection (per user spec: panel only goes away on close or empty)
  clearSelection();
}

// ===== Cart fly animation =====
function flyToCart(fromEl) {
  const panel = document.getElementById('cartPanel');
  if (!panel || !fromEl) return;
  const fromRect = fromEl.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const fly = document.createElement('div');
  fly.className = 'cart-fly';
  fly.innerHTML = '<i class="bi bi-bag-heart-fill"></i>';
  // Start at center of source element
  fly.style.left = (fromRect.left + fromRect.width/2 - 16) + 'px';
  fly.style.top = (fromRect.top + fromRect.height/2 - 16) + 'px';
  document.body.appendChild(fly);
  // Force layout, then transition to cart panel position
  requestAnimationFrame(() => {
    fly.style.left = (panelRect.left + panelRect.width/2 - 16) + 'px';
    fly.style.top = (panelRect.top + 24) + 'px';
    fly.style.transform = 'scale(0.4) rotate(360deg)';
    fly.style.opacity = '0';
  });
  setTimeout(() => {
    fly.remove();
    panel.classList.add('bump');
    setTimeout(() => panel.classList.remove('bump'), 460);
  }, 700);
}

// ===== COPY LINK =====
function copyLink(e, link, btn) {
  e.stopPropagation();
  navigator.clipboard.writeText(link).then(() => { btn.classList.add('copied'); setTimeout(() => btn.classList.remove('copied'), 2000); });
}
function buildGDriveLink(id, isFolder) {
  return isFolder
    ? 'https://drive.google.com/drive/folders/' + id + '?usp=drive_link'
    : 'https://drive.google.com/file/d/' + id + '/view?usp=drive_link';
}

// ===== COPY TO DRIVE =====
function openCopyDestModal() {
  destPath = [{ id: 'root', name: 'Drive Saya' }];
  selectedDestFolderId = 'root';
  openModal('copyDestModal');
  renderSavedDests();
  // Preload cleanup keywords so name-clean is ready when user hits "Salin"
  loadCleanupKeywords().catch(() => {});
  loadDestFolders('root');
}
async function loadDestFolders(parentId) {
  const list = document.getElementById('destFolderList');
  list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3);font-size:13px;"><span class="spinner"></span>Memuat...</div>';
  renderDestBreadcrumb();
  _refreshSaveDestBtn();
  try {
    const resp = await driveAPI('/files', 'GET', null, { q: \`'\${parentId}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false\`, fields: 'files(id, name)', orderBy: 'name', pageSize: '1000', supportsAllDrives: 'true' });
    const data = await resp.json();
    const folders = data.files || [];
    if (!folders.length) { list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text3);font-size:13px;">Tidak ada sub-folder. Klik "Folder Baru" untuk buat baru.</div>'; return; }
    list.innerHTML = '';
    folders.forEach(f => {
      const div = document.createElement('div'); div.className = 'dest-item';
      div.innerHTML = \`<i class="bi bi-folder-fill"></i><span>\${escHtml(f.name)}</span>\`;
      div.onclick = () => { destPath.push({ id: f.id, name: f.name }); selectedDestFolderId = f.id; loadDestFolders(f.id); };
      list.appendChild(div);
    });
  } catch { list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--red);font-size:13px;">Gagal memuat folder</div>'; }
}
function renderDestBreadcrumb() {
  const bc = document.getElementById('destBreadcrumb'); bc.innerHTML = '';
  destPath.forEach((item, idx) => {
    const span = document.createElement('span'); span.className = 'dest-bc' + (idx === destPath.length - 1 ? ' active' : ''); span.textContent = item.name;
    if (idx < destPath.length - 1) span.onclick = () => { destPath = destPath.slice(0, idx + 1); selectedDestFolderId = destPath[destPath.length-1].id; loadDestFolders(selectedDestFolderId); };
    bc.appendChild(span);
    if (idx < destPath.length - 1) { const sep = document.createElement('span'); sep.className = 'dest-bc'; sep.textContent = ' / '; bc.appendChild(sep); }
  });
  const cur = document.getElementById('dcrCurrent');
  if (cur) cur.textContent = destPath.map(p => p.name).join(' / ');
}
async function executeCopy() {
  closeModal('copyDestModal');
  // Use cart data so cross-folder selections work too
  const items = Array.from(cart.values()).map(it => ({
    id: it.id,
    name: it.name,
    isFolder: !!it.isFolder || it.mimeType === 'application/vnd.google-apps.folder',
    mimeType: it.mimeType || '',
  }));
  if (!items.length) { showToast('Tidak ada item untuk disalin', 'error'); return; }
  // Make sure cleanup keywords are loaded so we can clean copied names
  await loadCleanupKeywords();
  const cleanName = (n) => {
    const cleaned = applyKeywordsToName(n || '', cleanupKeywords);
    return cleaned && cleaned.trim() ? cleaned.trim() : (n || '');
  };
  openModal('progressModal');
  document.getElementById('progressBar').style.width = '0%';
  document.getElementById('progressLabel').textContent = '0 / ' + items.length;
  document.getElementById('progressText').textContent = 'Menyalin ' + items.length + ' item ke "' + destPath[destPath.length-1].name + '"...';
  try {
    let done = 0; const results = [];
    for (const item of items) {
      const newName = cleanName(item.name);
      document.getElementById('progressText').textContent = (item.isFolder ? 'Menyalin folder: ' : 'Menyalin: ') + newName;
      try {
        if (item.isFolder) {
          // Drive API /copy doesn't work on folders — do recursive copy with cleanup
          const newFolderId = await _copyFolderRecursive(item.id, newName, selectedDestFolderId, cleanName);
          results.push({ success: true, name: newName, id: newFolderId, isFolder: true });
        } else {
          const r = await driveAPI(\`/files/\${item.id}/copy\`, 'POST', { name: newName, parents: [selectedDestFolderId] }, { supportsAllDrives: 'true' });
          const d = await r.json();
          results.push({ success: r.ok, name: newName, id: d.id });
        }
      } catch (e) { results.push({ success: false, name: newName, error: e.message }); }
      done++;
      document.getElementById('progressBar').style.width = Math.round(done / items.length * 100) + '%';
      document.getElementById('progressLabel').textContent = done + ' / ' + items.length;
    }
    closeModal('progressModal');
    const ok = results.filter(r => r.success).length, fail = results.filter(r => !r.success).length;
    document.getElementById('successTitle').textContent = ok + ' item berhasil disalin!';
    document.getElementById('successSub').textContent = 'Ke: ' + destPath[destPath.length-1].name + (fail ? \` · \${fail} gagal\` : '');
    document.getElementById('copyResultList').innerHTML = results.map(r => \`<div class="copy-result-item"><i class="bi bi-\${r.success?'check-circle-fill ok':'x-circle-fill fail'}"></i>\${escHtml(r.name)}\${r.isFolder?' <span style="color:var(--text3);font-size:11px;">(folder)</span>':''}</div>\`).join('');
    openModal('successModal'); clearSelection();
  } catch (e) { closeModal('progressModal'); showToast('Gagal: ' + e.message, 'error'); }
}

// Recursive folder copy with name cleanup applied to every child.
// Drive's /files/{id}/copy doesn't work for folders, so we create a new folder
// then page through children and copy them (parallel files, sequential subfolders).
async function _copyFolderRecursive(srcFolderId, newName, parentId, cleanName) {
  // Create destination folder
  const createRes = await driveAPI('/files', 'POST', {
    name: newName,
    mimeType: 'application/vnd.google-apps.folder',
    parents: [parentId],
  }, { supportsAllDrives: 'true' });
  if (!createRes.ok) throw new Error('Gagal buat folder: ' + newName);
  const created = await createRes.json();
  const destFolderId = created.id;

  // List + copy all children
  const subFolders = [];
  const fileTasks = [];
  let pageToken = null;
  do {
    const params = {
      q: \`'\${srcFolderId}' in parents and trashed = false\`,
      fields: 'nextPageToken, files(id, name, mimeType)',
      pageSize: '1000',
      includeItemsFromAllDrives: 'true',
      supportsAllDrives: 'true',
    };
    if (pageToken) params.pageToken = pageToken;
    const r = await driveAPI('/files', 'GET', null, params);
    if (!r.ok) break;
    const d = await r.json();
    for (const f of d.files || []) {
      const childName = cleanName(f.name);
      if (f.mimeType === 'application/vnd.google-apps.folder') subFolders.push({ id: f.id, name: childName });
      else fileTasks.push({ id: f.id, name: childName });
    }
    pageToken = d.nextPageToken || null;
  } while (pageToken);

  // Copy files in parallel batches (8 at a time) for speed
  const BATCH = 8;
  for (let i = 0; i < fileTasks.length; i += BATCH) {
    const batch = fileTasks.slice(i, i + BATCH);
    await Promise.allSettled(batch.map(t =>
      driveAPI(\`/files/\${t.id}/copy\`, 'POST', { name: t.name, parents: [destFolderId] }, { supportsAllDrives: 'true' })
    ));
  }
  // Recurse into subfolders sequentially
  for (const sf of subFolders) {
    try { await _copyFolderRecursive(sf.id, sf.name, destFolderId, cleanName); }
    catch { /* skip individual subfolder failures so the rest still copies */ }
  }
  return destFolderId;
}

// ===== ADD FROM LINK (paste Drive URLs → cart → copy modal) =====
function openAddFromLinkModal() {
  const ta = document.getElementById('afLinkInput'); if (ta) ta.value = '';
  const pv = document.getElementById('afLinkPreview'); if (pv) pv.innerHTML = '';
  const st = document.getElementById('afLinkStatus'); if (st) st.textContent = '';
  openModal('addFromLinkModal');
  setTimeout(() => { const ta2 = document.getElementById('afLinkInput'); if (ta2) ta2.focus(); }, 80);
}
// Parse Drive URLs / IDs out of a free-form text blob.
// Supports: /drive/folders/<id>, /file/d/<id>, ?id=<id>, &id=<id>, raw IDs.
function _parseDriveIds(text) {
  const ids = new Set();
  const patterns = [
    /drive\\.google\\.com\\/drive\\/folders\\/([a-zA-Z0-9_-]{10,})/g,
    /drive\\.google\\.com\\/file\\/d\\/([a-zA-Z0-9_-]{10,})/g,
    /drive\\.google\\.com\\/open\\?id=([a-zA-Z0-9_-]{10,})/g,
    /[?&]id=([a-zA-Z0-9_-]{10,})/g,
  ];
  for (const re of patterns) {
    let m; while ((m = re.exec(text)) !== null) ids.add(m[1]);
  }
  // Also accept bare IDs (typically 25-44 chars) when text is just whitespace-separated tokens
  // — but only if no URL pattern matched the same chunk.
  if (ids.size === 0) {
    const tokens = text.split(/[\\s,]+/).map(t => t.trim()).filter(Boolean);
    for (const tok of tokens) {
      if (/^[a-zA-Z0-9_-]{25,60}$/.test(tok)) ids.add(tok);
    }
  }
  return Array.from(ids);
}
async function processAddFromLink() {
  const ta = document.getElementById('afLinkInput');
  const text = (ta?.value || '').trim();
  const status = document.getElementById('afLinkStatus');
  const preview = document.getElementById('afLinkPreview');
  const submitBtn = document.getElementById('afLinkSubmit');
  if (!text) { showToast('Tempel link Drive dulu', 'error'); return; }
  const ids = _parseDriveIds(text);
  if (!ids.length) {
    showToast('Tidak ada link Drive yang bisa dibaca', 'error');
    if (status) status.innerHTML = '<span style="color:var(--red);">Tidak ada link Drive valid yang ditemukan.</span>';
    return;
  }
  if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = '<span class="spinner" style="width:13px;height:13px;border-width:1.5px;"></span> Mengambil info...'; }
  if (status) status.innerHTML = \`<span class="spinner" style="width:11px;height:11px;border-width:1.5px;"></span> Mengambil info \${ids.length} link...\`;
  if (preview) preview.innerHTML = '';

  // Fetch metadata for each ID in parallel (with limited concurrency)
  const fetchOne = async (id) => {
    try {
      const r = await driveAPI(\`/files/\${id}\`, 'GET', null, { fields: 'id,name,mimeType,size', supportsAllDrives: 'true' });
      if (!r.ok) return { id, error: 'http ' + r.status };
      const d = await r.json();
      return { id, ok: true, meta: d };
    } catch (e) { return { id, error: e.message || String(e) }; }
  };
  // Limit concurrency to 6
  const results = [];
  const queue = ids.slice();
  const workers = Array(Math.min(6, queue.length)).fill(0).map(async () => {
    while (queue.length) {
      const id = queue.shift();
      results.push(await fetchOne(id));
    }
  });
  await Promise.all(workers);

  const valid = results.filter(r => r.ok).map(r => r.meta);
  const failed = results.filter(r => !r.ok);

  if (preview) {
    preview.innerHTML = '';
    valid.forEach(m => {
      const isFolder = m.mimeType === 'application/vnd.google-apps.folder';
      const icon = isFolder ? 'bi-folder-fill' : (m.mimeType?.startsWith('video/') ? 'bi-play-circle-fill' : 'bi-file-earmark');
      const div = document.createElement('div');
      div.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:6px;background:var(--bg3);font-size:12.5px;margin-bottom:4px;';
      div.innerHTML = \`<i class="bi \${icon}" style="color:\${isFolder?'#fbbf24':'var(--accent)'};font-size:14px;"></i><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">\${escHtml(m.name)}</span><span style="color:var(--text3);font-size:11px;">\${isFolder?'Folder':'File'}</span>\`;
      preview.appendChild(div);
    });
    failed.forEach(f => {
      const div = document.createElement('div');
      div.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:6px;background:rgba(239,68,68,0.08);font-size:12.5px;margin-bottom:4px;color:var(--red);';
      div.innerHTML = \`<i class="bi bi-x-circle"></i><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace;">\${escHtml(f.id)}</span><span style="font-size:11px;">Gagal</span>\`;
      preview.appendChild(div);
    });
  }
  if (submitBtn) { submitBtn.disabled = false; submitBtn.innerHTML = '<i class="bi bi-cloud-download"></i> Ambil & Salin'; }

  if (!valid.length) {
    if (status) status.innerHTML = \`<span style="color:var(--red);">Semua \${ids.length} link gagal diambil. Pastikan akses Drive sudah diberikan.</span>\`;
    showToast('Semua link gagal diambil', 'error');
    return;
  }
  if (status) {
    status.innerHTML = failed.length
      ? \`<span style="color:var(--red);">\${valid.length} berhasil, \${failed.length} gagal</span>\`
      : \`<span style="color:var(--green);">\${valid.length} item siap disalin</span>\`;
  }
  // Add valid items to cart
  for (const m of valid) {
    const isFolder = m.mimeType === 'application/vnd.google-apps.folder';
    cart.set(m.id, {
      id: m.id,
      name: m.name,
      mimeType: m.mimeType || '',
      isFolder,
      link: buildGDriveLink(m.id, isFolder),
      fromPath: 'Dari Link',
    });
  }
  updateSelectionUI();
  closeModal('addFromLinkModal');
  showToast(valid.length + ' item ditambah ke keranjang' + (failed.length ? ' (' + failed.length + ' gagal)' : ''), 'success');
  // Auto-open destination picker so the user can pick where to copy
  setTimeout(() => openCopyDestModal(), 280);
}

// ===== SAVED DESTINATIONS (Feature #7) — stored in Supabase settings table =====
// Key in settings table: saved_dest_folders. Value: JSON array of {id,name,path,pathArr,savedAt}.
let _savedDests = [];
let _savedDestsLoaded = false;
async function loadSavedDests() {
  if (_savedDestsLoaded) return _savedDests;
  try {
    const { data, error } = await sb.from('settings').select('value').eq('key', 'saved_dest_folders').single();
    if (!error && data && data.value) {
      const parsed = JSON.parse(data.value);
      if (Array.isArray(parsed)) _savedDests = parsed;
    }
  } catch {}
  _savedDestsLoaded = true;
  return _savedDests;
}
async function persistSavedDests() {
  try {
    await sb.from('settings').upsert({ key: 'saved_dest_folders', value: JSON.stringify(_savedDests) }, { onConflict: 'key' });
  } catch (e) { showToast('Gagal menyimpan tempat: ' + (e.message || e), 'error'); }
}
function _getSavedDests() { return _savedDests || []; }
function renderSavedDests() {
  const wrap = document.getElementById('savedDestWrap');
  if (!wrap) return;
  // Load from Supabase first time, then re-render
  if (!_savedDestsLoaded) {
    loadSavedDests().then(() => renderSavedDests());
    wrap.style.display = 'none';
    return;
  }
  const list = _savedDests;
  if (!list.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'flex';
  // keep label as first child, then rebuild chips
  const label = wrap.querySelector('.saved-dest-label');
  wrap.innerHTML = '';
  if (label) wrap.appendChild(label);
  list.forEach((d, idx) => {
    const chip = document.createElement('span');
    chip.className = 'saved-dest-chip';
    chip.title = d.path || d.name;
    chip.innerHTML = \`<i class="bi bi-bookmark-fill"></i><span>\${escHtml(d.name)}</span><button class="sdc-x" onclick="removeSavedDest(event, \${idx})" title="Hapus">×</button>\`;
    chip.onclick = (e) => {
      if (e.target.closest && e.target.closest('.sdc-x')) return;
      jumpToSavedDest(d);
    };
    wrap.appendChild(chip);
  });
}
function jumpToSavedDest(d) {
  // Reconstruct destPath from saved entry
  destPath = (d.pathArr && d.pathArr.length) ? d.pathArr.slice() : [{ id: d.id, name: d.name }];
  // Always make sure root is the first entry
  if (!destPath.length || destPath[0].id !== 'root') {
    destPath.unshift({ id: 'root', name: 'Drive Saya' });
  }
  selectedDestFolderId = destPath[destPath.length - 1].id;
  loadDestFolders(selectedDestFolderId);
}
function _isCurrentDestSaved() {
  if (!destPath.length) return -1;
  const id = destPath[destPath.length - 1].id;
  const list = _getSavedDests();
  return list.findIndex(x => x.id === id);
}
function _refreshSaveDestBtn() {
  const btn = document.getElementById('btnSaveDest');
  if (!btn) return;
  const idx = _isCurrentDestSaved();
  // Don't allow saving "root" (no point — that's the default)
  const cur = destPath[destPath.length - 1];
  if (!cur || cur.id === 'root') {
    btn.disabled = true;
    btn.style.opacity = '0.4';
    btn.classList.remove('saved');
    btn.innerHTML = '<i class="bi bi-bookmark"></i> Simpan';
    return;
  }
  btn.disabled = false;
  btn.style.opacity = '';
  if (idx >= 0) {
    btn.classList.add('saved');
    btn.innerHTML = '<i class="bi bi-bookmark-check-fill"></i> Tersimpan';
  } else {
    btn.classList.remove('saved');
    btn.innerHTML = '<i class="bi bi-bookmark"></i> Simpan';
  }
}
async function toggleSaveDest() {
  const cur = destPath[destPath.length - 1];
  if (!cur || cur.id === 'root') { showToast('Pilih sub-folder dulu', 'error'); return; }
  // Make sure list is loaded
  if (!_savedDestsLoaded) await loadSavedDests();
  const idx = _savedDests.findIndex(x => x.id === cur.id);
  if (idx >= 0) {
    _savedDests.splice(idx, 1);
    showToast('Tempat dihapus dari favorit', 'info');
  } else {
    _savedDests.unshift({
      id: cur.id,
      name: cur.name,
      path: destPath.map(p => p.name).join(' / '),
      pathArr: destPath.slice(),
      savedAt: Date.now(),
    });
    if (_savedDests.length > 12) _savedDests.length = 12;
    showToast('Tempat tersimpan ke favorit', 'success');
  }
  _refreshSaveDestBtn();
  renderSavedDests();
  await persistSavedDests();
}
async function removeSavedDest(e, idx) {
  e.stopPropagation();
  if (!_savedDestsLoaded) await loadSavedDests();
  if (idx < 0 || idx >= _savedDests.length) return;
  _savedDests.splice(idx, 1);
  renderSavedDests();
  _refreshSaveDestBtn();
  await persistSavedDests();
}

// ===== CREATE FOLDER (Feature #8) =====
let _cfTarget = null; // 'mydrive' (current path in main view) or 'dest' (current destPath in copy modal)
function openCreateFolderModal(target) {
  _cfTarget = target;
  const parentName = target === 'dest'
    ? (destPath[destPath.length-1] || {}).name
    : (currentPath[currentPath.length-1] || {}).name;
  document.getElementById('cfParentName').textContent = parentName || 'Drive Saya';
  document.getElementById('cfNameInput').value = '';
  document.getElementById('cfStatus').textContent = '';
  const btn = document.getElementById('cfCreateBtn');
  btn.disabled = false;
  btn.innerHTML = '<i class="bi bi-folder-plus"></i> Buat';
  openModal('createFolderModal');
  setTimeout(() => document.getElementById('cfNameInput').focus(), 60);
}
async function executeCreateFolder() {
  const name = (document.getElementById('cfNameInput').value || '').trim();
  if (!name) { document.getElementById('cfStatus').textContent = 'Nama tidak boleh kosong'; return; }
  const parentId = _cfTarget === 'dest'
    ? (destPath[destPath.length-1] || {}).id
    : (currentPath[currentPath.length-1] || {}).id;
  if (!parentId) { showToast('Folder induk tidak valid', 'error'); return; }
  const btn = document.getElementById('cfCreateBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Membuat...';
  document.getElementById('cfStatus').textContent = '';
  try {
    const r = await driveAPI('/files', 'POST', {
      name,
      mimeType: 'application/vnd.google-apps.folder',
      parents: [parentId],
    }, { supportsAllDrives: 'true' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error((err.error && err.error.message) || ('HTTP ' + r.status));
    }
    const d = await r.json();
    showToast('Folder "' + name + '" dibuat', 'success');
    closeModal('createFolderModal');
    if (_cfTarget === 'dest') {
      // Navigate into the new folder in dest modal
      destPath.push({ id: d.id, name: d.name });
      selectedDestFolderId = d.id;
      loadDestFolders(d.id);
    } else {
      // Refresh main file list
      loadFiles();
    }
  } catch (e) {
    document.getElementById('cfStatus').textContent = 'Gagal: ' + (e.message || e);
    document.getElementById('cfStatus').style.color = 'var(--red)';
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-folder-plus"></i> Coba Lagi';
  }
}

// ===== ROW MENU (3-dot per row) =====
// Posisi menu di-clamp ke viewport. Kalau bagian bawah layar tidak cukup,
// menu otomatis flip ke atas tombol supaya semua opsi (Lapor, Kirim Akses,
// Upload Movie, Upload Series, Progres Upload, dst.) tetap terlihat.
function openRowMenu(e, id, name, isFolderFlag, mimeType) {
  e.stopPropagation();
  const isFolder = isFolderFlag === '1' || isFolderFlag === 1;
  _rowMenuTarget = { id, name, isFolder, mimeType: mimeType || (isFolder ? 'application/vnd.google-apps.folder' : ''), link: buildGDriveLink(id, isFolder), fromPath: currentPath.map(p=>p.name).join(' / ') };
  const menu = document.getElementById('rowActionsMenu');
  const btnRect = e.currentTarget.getBoundingClientRect();

  // Reset previous inline sizing supaya kita bisa ukur tinggi natural
  menu.style.maxHeight = '';
  menu.style.visibility = 'hidden';
  menu.classList.add('open');

  requestAnimationFrame(function(){
    const margin = 8;
    const gap = 6;
    const viewportH = window.innerHeight || document.documentElement.clientHeight;
    const viewportW = window.innerWidth || document.documentElement.clientWidth;
    const naturalH = menu.scrollHeight || menu.offsetHeight || 260;
    const mw = menu.offsetWidth || 190;

    const spaceBelow = viewportH - btnRect.bottom - margin;
    const spaceAbove = btnRect.top - margin;
    // Pilih sisi yang menyediakan ruang lebih banyak. Kalau bawah cukup
    // menampung menu natural, prefer bawah supaya UX tetap natural.
    const placeAbove = spaceBelow < Math.min(naturalH + gap, 200) && spaceAbove > spaceBelow;
    const maxH = Math.max(140, (placeAbove ? spaceAbove : spaceBelow) - gap);
    const finalH = Math.min(naturalH, maxH);
    menu.style.maxHeight = finalH + 'px';

    let top = placeAbove ? (btnRect.top - finalH - gap) : (btnRect.bottom + gap);
    if (top + finalH > viewportH - margin) top = viewportH - finalH - margin;
    if (top < margin) top = margin;

    let left = btnRect.right - mw;
    if (left + mw > viewportW - margin) left = viewportW - mw - margin;
    if (left < margin) left = margin;

    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    menu.style.visibility = 'visible';
  });

  setTimeout(() => {
    document.addEventListener('click', _rowMenuOutsideClick, { once: true });
  }, 0);
}
function _rowMenuOutsideClick(e) {
  const menu = document.getElementById('rowActionsMenu');
  if (menu.contains(e.target)) {
    document.addEventListener('click', _rowMenuOutsideClick, { once: true });
    return;
  }
  menu.classList.remove('open');
}
function rowMenuAction(action) {
  const t = _rowMenuTarget;
  document.getElementById('rowActionsMenu').classList.remove('open');
  if (!t) return;
  // Rename doesn't operate via the cart — handle it before the cart-add path
  if (action === 'rename') {
    openRenameModal(t.id, t.name, t.isFolder);
    return;
  }
  if (action === 'upload_movie') {
    openStreamUploadModal('movie', t);
    return;
  }
  if (action === 'upload_series') {
    openStreamUploadModal('series', t);
    return;
  }
  if (action === 'progress_upload') {
    openStreamProgressPanel();
    return;
  }
  // Always make sure the targeted item is in the cart (one-shot or for batch)
  const wasAdded = !cart.has(t.id);
  if (wasAdded) {
    cart.set(t.id, { id: t.id, name: t.name, mimeType: t.mimeType, isFolder: t.isFolder, link: t.link, fromPath: t.fromPath });
    const row = document.querySelector(\`tr[data-id="\${t.id}"]\`);
    if (row) {
      row.classList.add('selected');
      const cb = row.querySelector('.file-cb'); if (cb) cb.checked = true;
      flyToCart(row);
    }
    updateSelectionUI();
  }
  if (action === 'lapor') {
    // Per-item report (existing single-item flow is friendlier)
    openReportModal(t.id, t.name, t.isFolder ? 'folder' : 'file');
  } else if (action === 'kirim') {
    openKirimModal();
  } else if (action === 'salin') {
    openCopyDestModal();
  }
}


// ===== STREAM UPLOAD ADD-ON (Movie / Series to bot worker) =====
// Aman: add-on ini tidak mengubah fitur lama seperti Kirim, Lapor, Salin, Rename, Cart.
let _zuStreamTarget = null;
let _zuStreamType = 'movie';
let _zuSelectedTmdb = null;
let _zuEpisodeFiles = [];
let _zuProgressJobs = [];
let _zuProgressTimer = null;
let _zuStreamConfig = null;

async function _zuLoadStreamConfig(force) {
  if (_zuStreamConfig && !force) return _zuStreamConfig;
  try {
    const r = await fetch('/api/stream-config');
    const d = await r.json().catch(function(){ return {}; });
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    _zuStreamConfig = d || {};
  } catch (e) {
    _zuStreamConfig = { ok: false, error: e.message || String(e) };
  }
  return _zuStreamConfig;
}

function _zuConfigHint() {
  const c = _zuStreamConfig || {};
  const tmdb = c.tmdb_ready ? '<span class="zu-ok">TMDB siap</span>' : '<span class="zu-warn">TMDB_API_KEY belum diset</span>';
  const bot = c.bot_api_ready ? '<span class="zu-ok">Bot API siap</span>' : '<span class="zu-warn">BOT_API_BASE belum diset</span>';
  let supa;
  if (c.player4me_domains_ready) {
    const total = Array.isArray(c.player4me_domains) ? c.player4me_domains.length : 0;
    supa = '<span class="zu-ok">Supabase player4me_domains: ' + total + ' baris</span> <small>(' + escHtml(c.supabase_url_source || '?') + ' • ' + escHtml(c.supabase_key_source || '?') + ')</small>';
  } else {
    const err = c.player4me_domains_error ? (' — ' + escHtml(c.player4me_domains_error)) : '';
    supa = '<span class="zu-warn">Supabase player4me_domains gagal dibaca, pakai daftar fallback</span>' + err;
  }
  return '<div class="zu-config-box"><i class="bi bi-shield-check"></i><div><b>Konfigurasi Cloudflare</b><br>' + tmdb + ' • ' + bot + '<br>' + supa + '<br><small>API key dan secret diambil dari Cloudflare Variables, bukan dari input manual.</small></div></div>';
}

function _zuNormalizeDomainValue(v) {
  return String(v || '').trim().replace(/^https?:\\/\\//i, '').replace(/\\/+$/g, '');
}

function _zuGetDefaultDomain() {
  const c = _zuStreamConfig || {};
  const domains = Array.isArray(c.player4me_domains) ? c.player4me_domains : [];
  const row = domains.find(function(d){ return !!d.is_default; }) || domains[0];
  return row ? _zuNormalizeDomainValue(row.domain) : 'zaeinstore.qzz.io';
}

function _zuDomainFieldHtml() {
  const c = _zuStreamConfig || {};
  const domains = Array.isArray(c.player4me_domains) ? c.player4me_domains : [];
  if (domains.length) {
    return '<div class="zu-stream-field"><label>Domain Player4Me</label><select class="zu-stream-select" id="zuPlayerDomain">'
      + domains.map(function(d){
        const domain = _zuNormalizeDomainValue(d.domain);
        const label = (d.name || domain) + (d.is_default ? ' — default' : '');
        return '<option value="' + escAttr(domain) + '"' + (d.is_default ? ' selected' : '') + '>' + escHtml(label) + '</option>';
      }).join('')
      + '</select><div class="zu-stream-hint">Domain diambil dari tabel player4me_domains.</div></div>';
  }
  return '<div class="zu-stream-field"><label>Domain Player4Me</label><input class="zu-stream-input" id="zuPlayerDomain" value="' + escAttr(_zuGetDefaultDomain()) + '" placeholder="zaeinstore.qzz.io"><div class="zu-stream-hint">Default pakai Zaeinstore QZZ. Kalau tabel player4me_domains bisa dibaca, pilihan domain akan muncul otomatis.</div></div>';
}

function _zuGetSelectedDomain() {
  const el = document.getElementById('zuPlayerDomain');
  return _zuNormalizeDomainValue(el && el.value ? el.value : _zuGetDefaultDomain());
}

let _zuTmdbTimer = null;
function _zuQueueTmdbSearch() {
  clearTimeout(_zuTmdbTimer);
  _zuTmdbTimer = setTimeout(function(){ _zuSearchTmdb(); }, 650);
}

function _zuTmdbQueryKey(e) {
  if (e && e.key === 'Enter') {
    e.preventDefault();
    clearTimeout(_zuTmdbTimer);
    _zuSearchTmdb();
  }
}


function _zuStorageGet(key, fallback) { try { return localStorage.getItem(key) || fallback || ''; } catch { return fallback || ''; } }
function _zuStorageSet(key, value) { try { localStorage.setItem(key, value || ''); } catch {} }
function _zuIsFolderItem(item) { return !!(item && item.isFolder); }
function _zuPosterUrl(path) { return path ? ('https://image.tmdb.org/t/p/w342' + path) : ''; }
function _zuBackdropUrl(path) { return path ? ('https://image.tmdb.org/t/p/w780' + path) : ''; }
function _zuYearFromDate(v) { return v ? String(v).slice(0,4) : ''; }
function _zuCleanTitle(name) {
  // Hanya buang token teknis (kualitas/format/extension/tag rilis). Tidak
  // boleh menghapus huruf normal di tengah kata. Pakai \b di kedua sisi.
  let s = String(name || '');
  // Buang extension umum video/sub.
  s = s.replace(/\.(mp4|mkv|mov|webm|avi|m4v|ts|m2ts|srt|ass|ssa|vtt)$/i, '');
  // Dots/underscores jadi spasi.
  s = s.replace(/[._]+/g, ' ');
  // Tag rilis spesifik (Zaein/Zaeinstore) di tengah/akhir.
  s = s.replace(/\b(?:Zaein(?:stream|stress|store)?)\b/ig, '');
  // Kualitas dan codec—pakai \b di kedua sisi supaya tidak makan huruf real.
  s = s.replace(/\b(2160p|1080p|720p|480p|360p|4K|UHD|HDR|SDR|10bit|8bit)\b/ig, '');
  s = s.replace(/\b(WEB[- ]?DL|WEB[- ]?Rip|BluRay|BDRip|HDTV|HDRip|DVDRip|HDCAM|CAMRip|HC|REMUX|Hardsub|Softsub|MULTi)\b/ig, '');
  s = s.replace(/\b(x264|x265|H\.?264|H\.?265|HEVC|AVC|AAC|AC3|DTS|DDP\d?(?:\.\d)?|Atmos|TrueHD|FLAC|MP3|Opus)\b/ig, '');
  // Pemberitahuan bahasa di akhir, contoh "Movie.id" atau "Movie [eng+id]".
  // "ID"/"EN"/"ENG" hanya dihapus kalau muncul dalam kurung atau di akhir,
  // BUKAN sebagai whole-word di tengah judul, supaya tidak memotong
  // huruf valid dalam kata seperti "Mid", "Aside", dll.
  s = s.replace(/[\[\(](?:[A-Z]{2,3}\+?)+[\]\)]/g, '');
  s = s.replace(/\s+(?:ID|EN|ENG|JP|JPN|KR|KOR|CN|CHN)\s*$/i, '');
  // Bersihkan tanda baca residual di pinggir.
  s = s.replace(/[\[\]\(\){}]+/g, ' ');
  s = s.replace(/\s+-\s*$/g, '');
  s = s.replace(/\s+/g, ' ').trim();
  return s || String(name || '').trim();
}
function _zuGuessEpisode(name, fallback) {
  const text = String(name || '');
  const m = text.match(/S\d{1,2}E(\d{1,3})/i) || text.match(/(?:^|[ ._-])E(?:P)?[ ._-]?(\d{1,3})(?:[ ._-]|$)/i) || text.match(/Episode[ ._-]?(\d{1,3})/i) || text.match(/(?:^|[ ._-])(\d{1,3})(?:[ ._-]|$)/);
  if (!m) return fallback || 1;
  const n = parseInt(m[1], 10);
  if (isNaN(n)) return fallback || 1;
  return Math.max(1, Math.min(50, n));
}
function _zuItemToSource(item) {
  return {
    kind: item.isFolder ? 'drive_folder' : 'drive_file',
    id: item.id,
    name: item.name,
    mimeType: item.mimeType || (item.isFolder ? 'application/vnd.google-apps.folder' : ''),
    link: item.link || buildGDriveLink(item.id, item.isFolder),
    fromPath: item.fromPath || ''
  };
}
function _zuEnsureShell() {
  if (!document.getElementById('zuStreamModal')) {
    const modal = document.createElement('div');
    modal.className = 'zu-stream-overlay';
    modal.id = 'zuStreamModal';
    modal.innerHTML = '<div class="zu-stream-modal"><div class="zu-stream-head"><div class="zu-stream-title" id="zuStreamTitle"><i class="bi bi-cloud-arrow-up-fill"></i> Upload</div><button class="modal-close" onclick="_zuCloseStreamModal()"><i class="bi bi-x-lg"></i></button></div><div class="zu-stream-body" id="zuStreamBody"></div><div class="zu-stream-actions"><button class="zu-stream-btn" onclick="_zuCloseStreamModal()">Batal</button><button class="zu-stream-btn success" onclick="_zuOpenProgressFromModal()"><i class="bi bi-activity"></i> Progres</button><button class="zu-stream-btn primary" id="zuStreamStartBtn" onclick="_zuStartUploadJob()"><i class="bi bi-rocket-takeoff-fill"></i> Start Upload</button></div></div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', function(e){ if (e.target === modal) _zuCloseStreamModal(); });
  }
  if (!document.getElementById('zuProgressDrawer')) {
    const drawer = document.createElement('div');
    drawer.className = 'zu-progress-drawer';
    drawer.id = 'zuProgressDrawer';
    drawer.innerHTML = '<div class="zu-progress-shell"><div class="zu-progress-head"><b><i class="bi bi-activity"></i> Progres Upload</b><div style="display:flex;gap:8px;align-items:center;"><button class="zu-mini-btn" title="Hapus semua card yang terlihat" onclick="_zuClearFinishedJobs()"><i class="bi bi-trash3"></i></button><button class="cph-close" onclick="_zuCloseProgressPanel()"><i class="bi bi-x-lg"></i></button></div></div><div class="zu-progress-list" id="zuProgressList"></div></div>';
    document.body.appendChild(drawer);
  }
}
function _zuCloseStreamModal() { const el = document.getElementById('zuStreamModal'); if (el) el.classList.remove('open'); }
function _zuOpenProgressFromModal() { openStreamProgressPanel(); }

function openStreamUploadModal(type, target) {
  _zuEnsureShell();
  _zuStreamType = type || 'movie';
  _zuStreamTarget = target;
  _zuSelectedTmdb = null;
  _zuEpisodeFiles = [];
  const isSeries = _zuStreamType === 'series';
  const title = document.getElementById('zuStreamTitle');
  const body = document.getElementById('zuStreamBody');
  title.innerHTML = isSeries ? '<i class="bi bi-collection-play-fill"></i> Upload Series ke Webstream' : '<i class="bi bi-film"></i> Upload Movie ke Webstream';
  const clean = _zuCleanTitle(target && target.name ? target.name : '');
  body.innerHTML = _zuRenderUploadBody(isSeries, clean, target);
  _zuLoadStreamConfig(true).then(function(){
    const currentQ = (document.getElementById('zuTmdbQuery') || {}).value || clean;
    body.innerHTML = _zuRenderUploadBody(isSeries, currentQ, target);
    if (isSeries && target && target.isFolder) _zuLoadFolderEpisodes(target.id);
    setTimeout(function(){ _zuQueueTmdbSearch(); }, 120);
  });
  document.getElementById('zuStreamModal').classList.add('open');
  setTimeout(function(){ const q = document.getElementById('zuTmdbQuery'); if(q) { q.focus(); _zuQueueTmdbSearch(); } }, 120);
  if (isSeries && target && target.isFolder) _zuLoadFolderEpisodes(target.id);
}
function _zuRenderUploadBody(isSeries, clean, target) {
  const targetName = escHtml(target && target.name ? target.name : 'Item dipilih');
  let seasonOptions = '';
  for (let i=1; i<=50; i++) seasonOptions += '<option value="' + i + '">Season ' + i + '</option>';
  return '<div class="zu-stream-grid">'
    + '<div class="zu-stream-card">'
    + _zuConfigHint()
    + '<div class="zu-stream-field"><label>Cari ' + (isSeries ? 'Series' : 'Movie') + '</label><input class="zu-stream-input" id="zuTmdbQuery" value="' + escAttr(clean) + '" placeholder="Masukkan nama ' + (isSeries ? 'series' : 'film') + '" oninput="_zuQueueTmdbSearch()" onkeydown="_zuTmdbQueryKey(event)"><div class="zu-stream-hint">Ketik akan cari otomatis. Enter juga langsung cari.</div></div>'
    + '<button class="zu-stream-btn primary" onclick="_zuSearchTmdb()"><i class="bi bi-search"></i> Cari TMDB</button>'
    + '<div class="zu-stream-status" id="zuTmdbStatus"></div><div class="zu-tmdb-results" id="zuTmdbResults"></div>'
    + '</div>'
    + '<div class="zu-stream-card">'
    + '<div class="zu-stream-field"><label>Source Drive</label><input class="zu-stream-input" readonly value="' + targetName + '"><div class="zu-stream-hint">' + (target && target.isFolder ? 'Folder akan discan untuk episode/video.' : 'File ini akan dipakai sebagai video utama.') + '</div></div>'
    + '<div class="zu-stream-field"><label>Target Upload</label><select class="zu-stream-select" id="zuUploadTarget"><option value="player4me">Player4Me</option><option value="drive_link">Drive Link saja</option></select></div>'
    + _zuDomainFieldHtml()
    + '<div class="zu-stream-field"><label>Tier</label><select class="zu-stream-select" id="zuTier"><option value="vip">VIP</option><option value="free">Free</option></select></div>'
    + (isSeries ? '<div class="zu-stream-field"><label>Season</label><select class="zu-stream-select" id="zuSeason">' + seasonOptions + '</select></div><div class="zu-stream-field"><label>Episode</label><div class="zu-episode-list" id="zuEpisodeList"><div class="zu-stream-hint">' + (target && target.isFolder ? 'Memuat isi folder...' : 'Pilih folder series agar episode bisa discan otomatis.') + '</div></div></div>' : '')
    + '<div class="zu-stream-field"><label>Status Koneksi</label><div class="zu-stream-hint">Upload akan dikirim lewat endpoint Worker <b>/api/stream-jobs</b>. Rahasia Bot API tetap aman di Cloudflare Variables.</div></div>'
    + '<div id="zuSelectedPreview" class="zu-stream-hint">Belum pilih metadata TMDB.</div><div class="zu-stream-status" id="zuSubmitStatus"></div>'
    + '</div></div>';
}
async function _zuSearchTmdb() {
  const q = (document.getElementById('zuTmdbQuery').value || '').trim();
  const status = document.getElementById('zuTmdbStatus');
  const results = document.getElementById('zuTmdbResults');
  if (!q) { status.textContent = 'Masukkan nama dulu.'; status.style.color = 'var(--red)'; return; }
  status.textContent = 'Mencari di TMDB...'; status.style.color = 'var(--text2)'; results.innerHTML = '';
  try {
    const cfg = await _zuLoadStreamConfig();
    if (!cfg.tmdb_ready) throw new Error('TMDB_API_KEY belum di-set di Cloudflare Variables.');
    const type = _zuStreamType === 'series' ? 'tv' : 'movie';
    const url = '/api/tmdb/search?type=' + encodeURIComponent(type) + '&query=' + encodeURIComponent(q);
    const r = await fetch(url);
    const d = await r.json();
    if (!r.ok) throw new Error((d && d.status_message) || ('HTTP ' + r.status));
    const arr = (d.results || []).slice(0, 12);
    if (!arr.length) { status.textContent = 'Tidak ada hasil.'; return; }
    status.textContent = arr.length + ' hasil ditemukan. Pilih salah satu.';
    results.innerHTML = arr.map(function(it, idx){
      const name = it.title || it.name || 'Tanpa Judul';
      const year = _zuYearFromDate(it.release_date || it.first_air_date);
      const poster = _zuPosterUrl(it.poster_path);
      const posterHtml = poster
        ? '<img src="' + escAttr(poster) + '" onerror="this.style.display=&quot;none&quot;; this.parentNode.innerHTML=&quot;<i class=\\&quot;bi bi-image\\&quot;></i>&quot;;">'
        : '<i class="bi bi-image"></i>';
      return '<div class="zu-tmdb-item" onclick="_zuSelectTmdb(' + idx + ')"><div class="zu-tmdb-poster">' + posterHtml + '</div><div class="zu-tmdb-meta"><div class="zu-tmdb-name">' + escHtml(name) + '</div><div class="zu-tmdb-year">' + escHtml(year || '-') + ' • ' + (_zuStreamType === 'series' ? 'Series' : 'Movie') + '</div></div></div>';
    }).join('');
    window._zuLastTmdbResults = arr;
  } catch (e) {
    status.textContent = 'Gagal cari TMDB: ' + (e.message || e);
    status.style.color = 'var(--red)';
  }
}
function _zuSelectTmdb(idx) {
  const arr = window._zuLastTmdbResults || [];
  const it = arr[idx];
  if (!it) return;
  _zuSelectedTmdb = it;
  document.querySelectorAll('.zu-tmdb-item').forEach(function(el, i){ el.classList.toggle('selected', i === idx); });
  const name = it.title || it.name || 'Tanpa Judul';
  const year = _zuYearFromDate(it.release_date || it.first_air_date);
  const poster = _zuPosterUrl(it.poster_path);
  const prev = document.getElementById('zuSelectedPreview');
  prev.className = 'zu-selected-preview';
  prev.innerHTML = (poster ? '<img src="' + escAttr(poster) + '">' : '<div style="width:84px;aspect-ratio:2/3;border-radius:10px;background:var(--bg4);display:flex;align-items:center;justify-content:center;color:var(--text3);"><i class="bi bi-image"></i></div>')
    + '<div><div class="zu-selected-preview-title">' + escHtml(name) + '</div><div class="zu-selected-preview-sub">TMDB ID: ' + escHtml(it.id) + ' • ' + escHtml(year || '-') + '</div><div class="zu-selected-preview-overview">' + escHtml(it.overview || 'Tidak ada sinopsis.') + '</div></div>';
}
async function _zuLoadFolderEpisodes(folderId) {
  const list = document.getElementById('zuEpisodeList');
  if (!list) return;
  list.innerHTML = '<div class="zu-stream-hint"><span class="spinner" style="width:13px;height:13px;border-width:1.5px;"></span> Membaca isi folder...</div>';
  try {
    let files = [], pageToken = null;
    do {
      const params = { q: "'" + folderId + "' in parents and trashed = false", fields: 'nextPageToken, files(id, name, mimeType, size, fileExtension)', orderBy: 'name', pageSize: '1000', includeItemsFromAllDrives: 'true', supportsAllDrives: 'true' };
      if (pageToken) params.pageToken = pageToken;
      const r = await driveAPI('/files', 'GET', null, params);
      if (!r.ok) throw new Error('Gagal membaca folder: HTTP ' + r.status);
      const d = await r.json();
      files = files.concat(d.files || []);
      pageToken = d.nextPageToken || null;
    } while (pageToken);
    const videos = files.filter(function(f){ const ext = (f.fileExtension || '').toLowerCase(); const mt = f.mimeType || ''; return mt.indexOf('video/') === 0 || ['mkv','mp4','mov','webm','avi'].indexOf(ext) >= 0; });
    videos.sort(function(a,b){ return String(a.name).localeCompare(String(b.name), undefined, { numeric: true, sensitivity: 'base' }); });
    _zuEpisodeFiles = videos.map(function(f, i){ return { id: f.id, name: f.name, mimeType: f.mimeType || '', episode: _zuGuessEpisode(f.name, i+1), checked: true, link: buildGDriveLink(f.id, false) }; });
    _zuRenderEpisodeList();
  } catch(e) {
    list.innerHTML = '<div class="zu-stream-hint" style="color:var(--red);">' + escHtml(e.message || e) + '</div>';
  }
}
function _zuRenderEpisodeList() {
  const list = document.getElementById('zuEpisodeList');
  if (!list) return;
  if (!_zuEpisodeFiles.length) { list.innerHTML = '<div class="zu-stream-hint">Tidak ada file video di folder ini.</div>'; return; }
  list.innerHTML = _zuEpisodeFiles.map(function(f, idx){
    return '<div class="zu-episode-row"><input type="checkbox" ' + (f.checked ? 'checked' : '') + ' onchange="_zuToggleEpisode(' + idx + ', this.checked)"><input type="number" min="1" max="50" value="' + escAttr(f.episode) + '" onchange="_zuSetEpisodeNumber(' + idx + ', this.value)"><div class="zu-episode-name" title="' + escAttr(f.name) + '">' + escHtml(f.name) + '</div><div class="zu-move-btns"><button class="zu-mini-btn" onclick="_zuMoveEpisode(' + idx + ', -1)">↑</button><button class="zu-mini-btn" onclick="_zuMoveEpisode(' + idx + ', 1)">↓</button></div></div>';
  }).join('');
}
function _zuToggleEpisode(idx, checked) { if (_zuEpisodeFiles[idx]) _zuEpisodeFiles[idx].checked = checked; }
function _zuSetEpisodeNumber(idx, value) { if (_zuEpisodeFiles[idx]) _zuEpisodeFiles[idx].episode = Math.max(1, Math.min(50, parseInt(value || '1', 10))); }
function _zuMoveEpisode(idx, dir) {
  const ni = idx + dir;
  if (ni < 0 || ni >= _zuEpisodeFiles.length) return;
  const tmp = _zuEpisodeFiles[idx]; _zuEpisodeFiles[idx] = _zuEpisodeFiles[ni]; _zuEpisodeFiles[ni] = tmp;
  _zuRenderEpisodeList();
}
function _zuBuildPayload() {
  const tier = (document.getElementById('zuTier') || {}).value || 'vip';
  const target = (document.getElementById('zuUploadTarget') || {}).value || 'player4me';
  const playerDomain = _zuGetSelectedDomain();
  const tm = _zuSelectedTmdb;
  if (!tm) throw new Error('Pilih metadata TMDB dulu.');
  const name = tm.title || tm.name || 'Untitled';
  const year = _zuYearFromDate(tm.release_date || tm.first_air_date);
  const payload = {
    kind: _zuStreamType,
    source: _zuItemToSource(_zuStreamTarget),
    target: target,
    tier: tier,
    player_domain: playerDomain,
    domain: playerDomain,
    player4me_domain: playerDomain,
    selected_domain: playerDomain,
    player4me: { domain: playerDomain },
    tmdb: {
      id: tm.id,
      type: _zuStreamType === 'series' ? 'tv' : 'movie',
      title: name,
      original_title: tm.original_title || tm.original_name || '',
      year: year,
      poster_url: _zuPosterUrl(tm.poster_path),
      backdrop_url: _zuBackdropUrl(tm.backdrop_path),
      overview: tm.overview || '',
      genre: tm.genre || '',
      trailer_url: ''
    }
  };
  if (_zuStreamType === 'series') {
    payload.season = parseInt((document.getElementById('zuSeason') || {}).value || '1', 10);
    payload.episodes = _zuEpisodeFiles.filter(function(f){ return f.checked; }).map(function(f){ return { episode: f.episode, drive_file_id: f.id, name: f.name, mimeType: f.mimeType || '', link: f.link || buildGDriveLink(f.id, false), checked: true }; });
    if (!payload.episodes.length) throw new Error('Pilih minimal 1 episode.');
  }
  return payload;
}
async function _zuStartUploadJob() {
  const status = document.getElementById('zuSubmitStatus');
  const btn = document.getElementById('zuStreamStartBtn');
  try {
    const cfg = await _zuLoadStreamConfig(true);
    if (!cfg.bot_api_ready) throw new Error('BOT_API_BASE belum di-set di Cloudflare Variables.');
    const payload = _zuBuildPayload();
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Mengirim job...';
    status.textContent = 'Menyiapkan job upload...'; status.style.color = 'var(--text2)';
    let job = { id: 'local-' + Date.now(), kind: _zuStreamType, title: payload.tmdb.title, poster_url: payload.tmdb.poster_url, season: payload.season || '', episode: payload.episodes && payload.episodes[0] ? payload.episodes[0].episode : '', status: 'queued', stage: 'Mengirim ke bot API', progress: 0, message: 'Job sedang dikirim lewat Worker.' };
    const r = await fetch('/api/stream-jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const d = await r.json().catch(function(){ return {}; });
    if (!r.ok) throw new Error(d.error || d.message || ('Bot API HTTP ' + r.status));
    const fromBot = d.job || (Array.isArray(d.created) && d.created[0]) || d;
    job = Object.assign(job, _zuNormalizeBotJob(fromBot));
    job.message = 'Job berhasil dikirim ke bot.';
    job.stage = job.stage || 'Job dikirim ke bot';
    status.textContent = 'Job berhasil dikirim ke bot.'; status.style.color = 'var(--green)';
    _zuAddOrUpdateJob(job);
    _zuCloseStreamModal();
    openStreamProgressPanel();
    showToast('Job upload dibuat: ' + payload.tmdb.title, 'success');
  } catch(e) {
    status.textContent = 'Gagal: ' + (e.message || e); status.style.color = 'var(--red)';
  } finally {
    btn.disabled = false; btn.innerHTML = '<i class="bi bi-rocket-takeoff-fill"></i> Start Upload';
  }
}

const _ZU_DISMISSED_JOBS_KEY = 'zaein_stream_upload_dismissed_jobs';

function _zuLoadDismissedJobs() {
  try { return new Set(JSON.parse(localStorage.getItem(_ZU_DISMISSED_JOBS_KEY) || '[]') || []); }
  catch { return new Set(); }
}

function _zuSaveDismissedJobs(set) {
  try { localStorage.setItem(_ZU_DISMISSED_JOBS_KEY, JSON.stringify(Array.from(set).slice(-200))); } catch {}
}

function _zuDismissJobId(id) {
  if (!id) return;
  const set = _zuLoadDismissedJobs();
  set.add(String(id));
  _zuSaveDismissedJobs(set);
}

const _ZU_ACTIVE_STATES = new Set([
  'queued', 'pending', 'waiting', 'running', 'in_progress', 'in-progress', 'started',
  'downloading', 'downloaded', 'download', 'uploading', 'upload',
  'encoding', 'transcoding', 'processing', 'subtitle', 'subtitles'
]);
const _ZU_FAIL_WINDOW_MS = 30 * 60 * 1000;

function _zuJobTimestamp(j) {
  const raw = j && (j.updated_at || j.finished_at || j.started_at || j.created_at);
  if (!raw) return 0;
  const t = Date.parse(raw);
  return isNaN(t) ? 0 : t;
}

function _zuJobState(j) {
  const st = String((j && j.status) || '').toLowerCase().trim();
  const stage = String((j && j.stage) || '').toLowerCase();
  const msg = String((j && j.message) || '').toLowerCase();
  if (['failed','error','cancelled','canceled','timeout'].indexOf(st) >= 0
      || msg.indexOf('gagal') >= 0 || msg.indexOf('failed') >= 0 || msg.indexOf('error') >= 0) {
    return 'failed';
  }
  if (['done','success','uploaded','completed','complete','finished'].indexOf(st) >= 0) return 'done';
  if (_ZU_ACTIVE_STATES.has(st) || _ZU_ACTIVE_STATES.has(stage)) return 'active';
  // Default tetap active supaya job baru tidak hilang sebelum punya status final.
  return 'active';
}

function _zuShouldShowJob(j) {
  if (!j || !j.id) return false;
  if (_zuLoadDismissedJobs().has(String(j.id))) return false;
  const state = _zuJobState(j);
  if (state === 'done') return false;
  if (state === 'failed') {
    const ts = _zuJobTimestamp(j);
    // Tanpa timestamp valid, tampilkan sekali (akan otomatis dismiss kalau
    // tidak update lebih dari window berikut karena polling tidak akan
    // melaporkannya lagi).
    if (!ts) return true;
    return (Date.now() - ts) <= _ZU_FAIL_WINDOW_MS;
  }
  return true;
}
function _zuAddOrUpdateJob(job) {
  if (!job || !job.id || !_zuShouldShowJob(job)) return;
  const idx = _zuProgressJobs.findIndex(function(j){ return j.id === job.id; });
  if (idx >= 0) _zuProgressJobs[idx] = Object.assign({}, _zuProgressJobs[idx], job);
  else _zuProgressJobs.unshift(job);
  _zuProgressJobs = _zuProgressJobs.filter(_zuShouldShowJob).slice(0, 30);
  _zuSaveLocalJobs();
  _zuRenderProgressJobs();
}
function _zuSaveLocalJobs() { try { localStorage.setItem('zaein_stream_upload_jobs', JSON.stringify(_zuProgressJobs.slice(0, 50))); } catch {} }
function _zuLoadLocalJobs() {
  try { _zuProgressJobs = JSON.parse(localStorage.getItem('zaein_stream_upload_jobs') || '[]') || []; }
  catch { _zuProgressJobs = []; }
  _zuProgressJobs = _zuProgressJobs.filter(_zuShouldShowJob);
  _zuSaveLocalJobs();
}
function openStreamProgressPanel() {
  _zuEnsureShell();
  _zuLoadLocalJobs();
  document.getElementById('zuProgressDrawer').classList.add('open');
  _zuRenderProgressJobs();
  _zuStartProgressPolling();
}
function _zuCloseProgressPanel() { const d = document.getElementById('zuProgressDrawer'); if (d) d.classList.remove('open'); if (_zuProgressTimer) { clearInterval(_zuProgressTimer); _zuProgressTimer = null; } }
function _zuRemoveProgressJob(id) {
  _zuDismissJobId(id);
  _zuProgressJobs = _zuProgressJobs.filter(function(j){ return String(j.id) !== String(id); });
  _zuSaveLocalJobs();
  _zuRenderProgressJobs();
}
function _zuClearFinishedJobs() {
  _zuProgressJobs.forEach(function(j){ if (j && j.id) _zuDismissJobId(j.id); });
  _zuProgressJobs = [];
  _zuSaveLocalJobs();
  _zuRenderProgressJobs();
}
function _zuNormalizeBotJob(j) {
  const st = String(j.status || '').toLowerCase();
  let progress = parseFloat(j.progress || 0);
  if ((st === 'completed' || st === 'done' || st === 'success' || st === 'uploaded') && progress < 100) progress = 100;
  let kind = j.kind || j.type || 'movie';
  if (String(kind).indexOf('series') >= 0) kind = 'series';
  if (String(kind).indexOf('player4me') >= 0 || String(kind).indexOf('upload') >= 0) kind = 'movie';
  return {
    id: j.id || j.job_id,
    kind: kind,
    title: j.title || j.name || 'Upload',
    poster_url: j.poster_url || j.poster || '',
    season: j.season || '',
    episode: j.episode || '',
    status: j.status || 'queued',
    stage: j.stage || j.progress_text || j.status || 'queued',
    progress: progress || 0,
    message: j.message || j.progress_text || j.error_message || j.status || '',
    created_at: j.created_at || '',
    updated_at: j.updated_at || j.finished_at || j.started_at || ''
  };
}
function _zuRenderProgressJobs() {
  const list = document.getElementById('zuProgressList');
  if (!list) return;
  _zuProgressJobs = _zuProgressJobs.filter(_zuShouldShowJob);
  if (!_zuProgressJobs.length) { list.innerHTML = '<div style="padding:22px;text-align:center;color:var(--text3);font-size:12.5px;">Tidak ada job yang sedang berjalan.</div>'; return; }
  list.innerHTML = _zuProgressJobs.map(function(j){
    const pct = Math.max(0, Math.min(100, parseFloat(j.progress || 0)));
    const state = _zuJobState(j);
    const cls = state === 'failed' ? 'failed' : '';
    const poster = j.poster_url ? '<img src="' + escAttr(j.poster_url) + '">' : '';
    const meta = (j.kind === 'series' ? ('Season ' + (j.season || '-') + (j.episode ? ' • Episode ' + j.episode : '')) : 'Movie') + ' • ' + (j.stage || j.status || 'queued');
    const removable = '<button class="zu-mini-btn" title="Hapus card ini" onclick="_zuRemoveProgressJob(&quot;' + escAttr(String(j.id || '')) + '&quot;)"><i class="bi bi-x-lg"></i></button>';
    return '<div class="zu-progress-card ' + cls + '"><div class="zu-progress-poster">' + poster + '</div><div style="min-width:0;"><div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;"><div class="zu-progress-title">' + escHtml(j.title || 'Upload') + '</div>' + removable + '</div><div class="zu-progress-meta">' + escHtml(meta) + '</div><div class="zu-progress-track"><div class="zu-progress-fill" style="width:' + pct + '%"></div></div><div class="zu-progress-foot"><span>' + escHtml(j.message || j.status || 'queued') + '</span><b>' + Math.round(pct) + '%</b></div></div></div>';
  }).join('');
}
async function _zuPollProgressOnce() {
  try {
    const cfg = await _zuLoadStreamConfig(false);
    if (!cfg || !cfg.bot_api_ready) return;
    const r = await fetch('/api/stream-jobs');
    const d = await r.json().catch(function(){ return {}; });
    if (!r.ok) return;
    const jobs = d.jobs || d.data || [];
    jobs.forEach(function(j){
      const nj = _zuNormalizeBotJob(j);
      if (nj.id) _zuAddOrUpdateJob(nj);
    });
    _zuProgressJobs = _zuProgressJobs.filter(_zuShouldShowJob);
    _zuSaveLocalJobs();
    _zuRenderProgressJobs();
  } catch {}
}
function _zuStartProgressPolling() {
  if (_zuProgressTimer) clearInterval(_zuProgressTimer);
  _zuPollProgressOnce();
  _zuProgressTimer = setInterval(_zuPollProgressOnce, 2500);
}

// ===== RENAME (ganti nama file/folder lewat menu titik tiga) =====
let _renameTarget = null; // { id, oldName, isFolder }
function openRenameModal(id, name, isFolder) {
  _renameTarget = { id, oldName: name, isFolder };
  document.getElementById('renameOldName').textContent = name;
  const inp = document.getElementById('renameInput');
  inp.value = name;
  document.getElementById('renameStatus').textContent = '';
  const btn = document.getElementById('renameSubmitBtn');
  btn.disabled = false;
  btn.innerHTML = '<i class="bi bi-check-lg"></i> Simpan';
  openModal('renameModal');
  setTimeout(() => {
    inp.focus();
    // Select the base name (everything before the last extension dot) so the
    // user can quickly retype without losing the .mkv/.mp4 suffix.
    if (!isFolder) {
      const dot = name.lastIndexOf('.');
      if (dot > 0 && dot < name.length - 1) inp.setSelectionRange(0, dot);
      else inp.select();
    } else {
      inp.select();
    }
  }, 60);
}
async function submitRename() {
  const t = _renameTarget;
  if (!t) return;
  const inp = document.getElementById('renameInput');
  const status = document.getElementById('renameStatus');
  const btn = document.getElementById('renameSubmitBtn');
  const newName = (inp.value || '').trim();
  if (!newName) { status.innerHTML = '<span style="color:var(--red);">Nama tidak boleh kosong</span>'; return; }
  if (newName === t.oldName) { closeModal('renameModal'); return; }
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="width:13px;height:13px;border-width:1.5px;"></span> Menyimpan...';
  status.innerHTML = '';
  try {
    const r = await driveAPI(\`/files/\${t.id}\`, 'PATCH', { name: newName }, { supportsAllDrives: 'true' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err?.error?.message || ('HTTP ' + r.status));
    }
    closeModal('renameModal');
    showToast('Nama diganti jadi "' + newName + '"', 'success');
    // Update local data so UI reflects the change without a full reload
    const upd = (arr) => { const it = arr && arr.find && arr.find(f => f.id === t.id); if (it) it.name = newName; };
    upd(allFiles);
    upd(_searchResults);
    if (cart.has(t.id)) {
      const c = cart.get(t.id); c.name = newName; cart.set(t.id, c);
    }
    // Update breadcrumb if this folder is in the current path (renaming current folder)
    const pIdx = currentPath.findIndex(p => p.id === t.id);
    if (pIdx >= 0) currentPath[pIdx].name = newName;
    renderBreadcrumb();
    // Re-render the visible list (search results or normal listing)
    if (_isSearching && _searchResults.length) renderFiles(_searchResults, { isSearch: true });
    else renderFiles(allFiles);
    updateSelectionUI();
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-check-lg"></i> Simpan';
    status.innerHTML = '<span style="color:var(--red);">Gagal: ' + escHtml(e.message || String(e)) + '</span>';
  }
}

// ===== CART ACTION BUTTONS =====
function cartActionLapor() {
  if (cart.size === 0) { showToast('Keranjang kosong', 'error'); return; }
  // Confirm batch lapor
  if (cart.size > 1) {
    if (!confirm('Laporkan ' + cart.size + ' item sekaligus? Email batch akan dikirim ke admin sesuai ambang ' + 5 + ' laporan.')) return;
    _runBatchLapor();
  } else {
    const it = Array.from(cart.values())[0];
    openReportModal(it.id, it.name, it.isFolder ? 'folder' : 'file');
  }
}
async function _runBatchLapor() {
  const items = Array.from(cart.values());
  let sent = 0, dup = 0, fail = 0;
  showToast('Mengirim ' + items.length + ' laporan...', 'info');
  for (const it of items) {
    try {
      const r = await fetch('/api/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: it.name, drive_id: it.id, kind: it.isFolder ? 'folder' : 'file' }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) fail++;
      else if (d.duplicate) dup++;
      else sent++;
    } catch { fail++; }
  }
  let msg = sent + ' laporan baru terkirim';
  if (dup) msg += ', ' + dup + ' sudah ada';
  if (fail) msg += ', ' + fail + ' gagal';
  showToast(msg, fail ? 'error' : 'success');
}
function cartActionKirim() {
  if (cart.size === 0) { showToast('Keranjang kosong', 'error'); return; }
  openKirimModal();
}
function cartActionSalin() {
  if (cart.size === 0) { showToast('Keranjang kosong', 'error'); return; }
  openCopyDestModal();
}

// ===== KIRIM (SHARE via Drive permissions) (Feature #4) =====
function openKirimModal() {
  document.getElementById('kirimEmail').value = '';
  document.getElementById('kirimMessage').value = '';
  document.getElementById('kirimNotify').checked = true;
  document.getElementById('kirimStatus').textContent = '';
  document.getElementById('kirimResultList').style.display = 'none';
  document.getElementById('kirimResultList').innerHTML = '';
  const btn = document.getElementById('kirimSendBtn');
  btn.disabled = false;
  btn.innerHTML = '<i class="bi bi-send-fill"></i> Bagikan Sekarang';
  // Render cart list inside modal
  const list = document.getElementById('kirimCartList');
  document.getElementById('kirimCount').textContent = cart.size;
  list.innerHTML = Array.from(cart.values()).map(it => {
    const cls = it.isFolder ? 'folder' : ((it.mimeType||'').startsWith('video/') ? 'video' : 'generic');
    const ic = it.isFolder ? '<i class="bi bi-folder-fill"></i>' : '<i class="bi bi-play-circle-fill"></i>';
    return \`<div class="share-cart-row"><div class="scr-icon \${cls}">\${ic}</div><span class="scr-name" title="\${escAttr(it.name)}">\${escHtml(it.name)}</span></div>\`;
  }).join('');
  openModal('kirimModal');
  setTimeout(() => document.getElementById('kirimEmail').focus(), 60);
}
function _isValidEmail(e) { return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(e); }
async function executeKirim() {
  const email = (document.getElementById('kirimEmail').value || '').trim();
  const notify = document.getElementById('kirimNotify').checked;
  const message = (document.getElementById('kirimMessage').value || '').trim();
  const status = document.getElementById('kirimStatus');
  const btn = document.getElementById('kirimSendBtn');
  if (!_isValidEmail(email)) { status.textContent = 'Email tidak valid'; status.style.color = 'var(--red)'; return; }
  if (cart.size === 0) { status.textContent = 'Keranjang kosong'; status.style.color = 'var(--red)'; return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Mengirim...';
  status.textContent = 'Memberikan akses ke ' + email + '...';
  status.style.color = 'var(--text2)';

  const items = Array.from(cart.values());
  const results = [];
  // Run with limited concurrency to avoid hitting Drive rate limits
  const CONC = 4;
  let cursor = 0;
  async function work() {
    while (cursor < items.length) {
      const my = cursor++;
      const it = items[my];
      try {
        const params = { supportsAllDrives: 'true', sendNotificationEmail: notify ? 'true' : 'false' };
        if (notify && message) params.emailMessage = message;
        const r = await driveAPI(\`/files/\${it.id}/permissions\`, 'POST', { role: 'reader', type: 'user', emailAddress: email }, params);
        if (r.ok) {
          results.push({ success: true, name: it.name });
        } else {
          const err = await r.json().catch(() => ({}));
          results.push({ success: false, name: it.name, error: (err.error && err.error.message) || ('HTTP ' + r.status) });
        }
      } catch (e) {
        results.push({ success: false, name: it.name, error: e.message || String(e) });
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(CONC, items.length) }, () => work()));
  const ok = results.filter(r => r.success).length;
  const fail = results.filter(r => !r.success).length;
  status.textContent = ok + ' berhasil dibagikan ke ' + email + (fail ? ' · ' + fail + ' gagal' : '');
  status.style.color = fail ? 'var(--yellow)' : 'var(--green)';
  const list = document.getElementById('kirimResultList');
  list.style.display = 'block';
  list.innerHTML = results.map(r => \`<div class="share-result-row"><i class="bi bi-\${r.success?'check-circle-fill ok':'x-circle-fill fail'}"></i><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">\${escHtml(r.name)}</span>\${r.error ? \`<span style="font-size:11px;color:var(--red);">\${escHtml(r.error)}</span>\` : ''}</div>\`).join('');
  btn.disabled = false;
  btn.innerHTML = ok > 0 ? '<i class="bi bi-check-circle-fill"></i> Bagikan Lagi' : '<i class="bi bi-send-fill"></i> Coba Lagi';
  if (ok > 0 && fail === 0) {
    showToast(ok + ' item dibagikan ke ' + email, 'success');
    setTimeout(() => { clearSelection(); closeModal('kirimModal'); }, 2000);
  }
}

// ===== OAUTH MANAGEMENT =====
async function openTokenModal() { await loadOAuthInfo(); openModal('tokenModal'); }
async function saveOAuthCreds() {
  const clientId = document.getElementById('oaClientId').value.trim();
  const clientSecret = document.getElementById('oaClientSecret').value.trim();
  const refreshToken = document.getElementById('oaRefreshToken').value.trim();
  if (!clientId || !clientSecret || !refreshToken) { showToast('Semua field harus diisi', 'error'); return; }
  const { error } = await sb.from('settings').upsert({ key: 'drive_oauth', value: JSON.stringify({ client_id: clientId, client_secret: clientSecret, refresh_token: refreshToken }), updated_at: new Date().toISOString() });
  if (error) { showToast('Gagal menyimpan: ' + error.message, 'error'); return; }
  oauthCfg = { client_id: clientId, client_secret: clientSecret, refresh_token: refreshToken };
  _accessToken = null; _accessTokenExpiry = 0;
  showToast('✅ Konfigurasi OAuth tersimpan!', 'success');
  await loadOAuthInfo(); loadFiles();
}
async function loadOAuthInfo() {
  const dot = document.getElementById('tokenDot'), txt = document.getElementById('tokenStatusText');
  const block = document.getElementById('tokenStatusBlock'), activeBox = document.getElementById('oauthActiveBox'), clearBtn = document.getElementById('oaClearBtn');
  try {
    const { data, error } = await sb.from('settings').select('value, updated_at').eq('key', 'drive_oauth').single();
    if (error || !data) throw new Error('Belum ada konfigurasi');
    const cfg = JSON.parse(data.value);
    if (!cfg.client_id || !cfg.client_secret || !cfg.refresh_token) throw new Error('Tidak lengkap');
    oauthCfg = cfg;
    dot.className = 'token-dot active'; txt.textContent = 'Aktif'; txt.className = 'tb-status active';
    block.className = 'token-status-block active'; block.innerHTML = '<i class="bi bi-check-circle-fill"></i><span>Token aktif & tersimpan di Supabase</span>';
    document.getElementById('oaClientId').value = cfg.client_id || '';
    document.getElementById('oaClientSecret').value = cfg.client_secret ? '••••••••••••••••' : '';
    document.getElementById('oaRefreshToken').value = cfg.refresh_token || '';
    if (activeBox) { activeBox.style.display = 'flex'; activeBox.style.flexDirection = 'column'; document.getElementById('oabClientId').textContent = (cfg.client_id||'').substring(0,42)+'...'; document.getElementById('oabRefreshToken').textContent = (cfg.refresh_token||'').substring(0,20)+'...'; document.getElementById('oabSavedAt').textContent = new Date(data.updated_at).toLocaleString('id-ID'); }
    if (clearBtn) clearBtn.style.display = 'flex';
  } catch {
    dot.className = 'token-dot inactive'; txt.textContent = 'Tidak Aktif'; txt.className = 'tb-status inactive';
    block.className = 'token-status-block inactive'; block.innerHTML = '<i class="bi bi-x-circle-fill"></i><span>OAuth Drive belum dikonfigurasi</span>';
    if (activeBox) activeBox.style.display = 'none'; if (clearBtn) clearBtn.style.display = 'none';
  }
}
async function clearOAuthCreds() {
  if (!confirm('Hapus konfigurasi OAuth2? Akses Drive akan berhenti.')) return;
  await sb.from('settings').delete().eq('key', 'drive_oauth');
  oauthCfg = null; _accessToken = null; _accessTokenExpiry = 0;
  document.getElementById('oaClientId').value = ''; document.getElementById('oaClientSecret').value = ''; document.getElementById('oaRefreshToken').value = '';
  showToast('Konfigurasi dihapus', 'info'); await loadOAuthInfo(); loadFiles();
}
async function testOAuthConnection() {
  const clientId = document.getElementById('oaClientId').value.trim() || oauthCfg?.client_id;
  const raw = document.getElementById('oaClientSecret').value.trim();
  const clientSecret = raw.includes('•') ? oauthCfg?.client_secret : raw;
  const refreshToken = document.getElementById('oaRefreshToken').value.trim() || oauthCfg?.refresh_token;
  if (!clientId || !clientSecret || !refreshToken) { showToast('Isi semua field terlebih dahulu', 'error'); return; }
  showToast('Menguji koneksi...', 'info');
  try {
    const res = await fetch('https://oauth2.googleapis.com/token', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ client_id: clientId, client_secret: clientSecret, refresh_token: refreshToken, grant_type: 'refresh_token' }) });
    const d = await res.json();
    if (d.access_token) { showToast('✅ Berhasil! Access token aktif.', 'success'); const block = document.getElementById('tokenStatusBlock'); block.className = 'token-status-block active'; block.innerHTML = '<i class="bi bi-check-circle-fill"></i><span>Berhasil! Access token aktif.</span>'; }
    else { showToast('❌ Gagal: ' + (d.error_description || d.error), 'error'); }
  } catch (e) { showToast('❌ Error: ' + e.message, 'error'); }
}

// ===== MODAL =====
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(el => { el.addEventListener('click', e => { if (e.target === el && el.id !== 'progressModal') closeModal(el.id); }); });

// ===== TOAST =====
function showToast(msg, type = 'info') {
  const wrap = document.getElementById('toastWrap'), t = document.createElement('div');
  t.className = 'toast ' + type;
  t.innerHTML = \`<i class="bi bi-\${type==='success'?'check-circle-fill':type==='error'?'x-circle-fill':'info-circle-fill'}"></i>\${escHtml(msg)}\`;
  wrap.appendChild(t);
  setTimeout(() => { t.style.animation = 'toastOut 0.25s ease forwards'; setTimeout(() => t.remove(), 250); }, 3000);
}

// ===== HELPERS =====
function escHtml(str) { return String(str||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function escAttr(str) { return String(str||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function formatSize(bytes) {
  if (!bytes || bytes == 0) return '—';
  const b = parseInt(bytes);
  if (b < 1024) return b + ' B';
  if (b < 1024*1024) return (b/1024).toFixed(1) + ' KB';
  if (b < 1024*1024*1024) return (b/1024/1024).toFixed(1) + ' MB';
  return (b/1024/1024/1024).toFixed(2) + ' GB';
}
function getFileIcon(f) {
  const mime = f.mimeType||'', ext = (f.fileExtension||'').toLowerCase();
  if (mime === 'application/vnd.google-apps.folder') return { cls:'folder', html:'<i class="bi bi-folder-fill"></i>' };
  if (mime.startsWith('video/') || ['mkv','mp4','avi','mov','webm'].includes(ext)) return { cls:'video', html:'<i class="bi bi-play-circle-fill"></i>' };
  if (mime.startsWith('image/') || ['jpg','jpeg','png','gif','webp'].includes(ext)) return { cls:'image', html:'<i class="bi bi-image-fill"></i>' };
  if (mime.startsWith('audio/') || ['mp3','flac','aac','ogg'].includes(ext)) return { cls:'audio', html:'<i class="bi bi-music-note-beamed"></i>' };
  if (['pdf','doc','docx','txt','xlsx','pptx'].includes(ext)||mime.includes('document')||mime.includes('spreadsheet')) return { cls:'doc', html:'<i class="bi bi-file-earmark-fill"></i>' };
  return { cls:'generic', html:'<i class="bi bi-file-earmark"></i>' };
}

/**
 * getQualityBadge — shows actual WxH resolution for video files only.
 * Color: 4K=purple, 1080p=blue, 720p=green, 480p=amber, SD=muted.
 * Non-video and folders → dash.
 */
function getQualityBadge(f) {
  const mime = f.mimeType || '', ext = (f.fileExtension || '').toLowerCase();
  const isVideo = mime.startsWith('video/') || ['mkv','mp4','avi','mov','webm'].includes(ext);
  if (mime === 'application/vnd.google-apps.folder' || !isVideo)
    return '<span style="color:var(--text3);font-size:12px;">—</span>';
  const vm = f.videoMediaMetadata;
  if (!vm || !vm.width || !vm.height)
    return '<span style="color:var(--text3);font-size:12px;">—</span>';
  const w = parseInt(vm.width), h = parseInt(vm.height), max = Math.max(w, h);
  const cls = max >= 3840 ? 'q4k' : max >= 1920 ? 'q1080' : max >= 1280 ? 'q720' : max >= 854 ? 'q480' : 'qsd';
  return \`<span class="quality-badge \${cls}">\${w}x\${h}</span>\`;
}

/**
 * getDurationHtml — formats durationMillis from videoMediaMetadata.
 * Video files only. Returns h:mm:ss or m:ss. Others → dash.
 */
function getDurationHtml(f) {
  const mime = f.mimeType || '', ext = (f.fileExtension || '').toLowerCase();
  const isVideo = mime.startsWith('video/') || ['mkv','mp4','avi','mov','webm'].includes(ext);
  if (mime === 'application/vnd.google-apps.folder' || !isVideo)
    return '<span class="dur-text">—</span>';
  const vm = f.videoMediaMetadata;
  if (!vm || !vm.durationMillis) return '<span class="dur-text">—</span>';
  const totalSec = Math.floor(parseInt(vm.durationMillis) / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const dur = h > 0
    ? \`\${h}:\${String(m).padStart(2,'0')}:\${String(s).padStart(2,'0')}\`
    : \`\${m}:\${String(s).padStart(2,'0')}\`;
  return \`<span class="dur-text">\${dur}</span>\`;
}

function previewFile(id) { window.open('https://drive.google.com/file/d/' + id + '/view', '_blank'); }

/**
 * getFormatHtml — shows file extension badge (MKV, MP4, ...) for files; blank for folders.
 */
function getFormatHtml(f, isFolder) {
  if (isFolder) return '<span class="format-text">—</span>';
  let ext = (f.fileExtension || '').trim();
  if (!ext) {
    const m = (f.name || '').match(/\\.([A-Za-z0-9]{1,8})$/);
    if (m) ext = m[1];
  }
  if (!ext) return '<span class="format-text">—</span>';
  return \`<span class="format-badge">\${escHtml(ext.toUpperCase())}</span>\`;
}

// ===== CLEANUP KEYWORDS (Supabase: settings/cleanup_keywords) =====
async function loadCleanupKeywords() {
  if (_cleanupKwLoaded) return cleanupKeywords;
  try {
    const { data, error } = await sb.from('settings').select('value').eq('key', 'cleanup_keywords').single();
    if (!error && data && data.value) {
      const parsed = JSON.parse(data.value);
      if (Array.isArray(parsed)) cleanupKeywords = parsed.filter(s => typeof s === 'string' && s.length > 0);
    }
  } catch {}
  _cleanupKwLoaded = true;
  return cleanupKeywords;
}
async function saveCleanupKeywords() {
  try {
    await sb.from('settings').upsert({ key: 'cleanup_keywords', value: JSON.stringify(cleanupKeywords) }, { onConflict: 'key' });
  } catch (e) { showToast('Gagal menyimpan kata: ' + e.message, 'error'); }
}

function _escRegex(s) { return s.replace(/[.*+?^\${}()|[\\]\\\\]/g, '\\\\$&'); }
function applyKeywordsToName(name, keywords) {
  let r = name;
  for (const kw of keywords) {
    if (!kw) continue;
    r = r.replace(new RegExp(_escRegex(kw), 'gi'), '');
  }
  r = r.replace(/\\s+/g, ' ').trim();
  return r;
}

function _renderCleanupChips() {
  const list = document.getElementById('cdList');
  if (!list) return;
  if (!cleanupKeywords.length) { list.innerHTML = '<div class="cd-empty">Belum ada kata. Tambah dulu di bawah.</div>'; return; }
  list.innerHTML = cleanupKeywords.map((kw, i) => \`<span class="cd-chip"><span>\${escHtml(kw)}</span><button onclick="removeCleanupKeyword(\${i})" title="Hapus"><i class="bi bi-x"></i></button></span>\`).join('');
}
function addCleanupKeyword() {
  const inp = document.getElementById('cdInput');
  const v = (inp.value || '').trim();
  if (!v) return;
  if (cleanupKeywords.some(k => k.toLowerCase() === v.toLowerCase())) { inp.value = ''; return; }
  cleanupKeywords.push(v);
  inp.value = '';
  _renderCleanupChips();
  saveCleanupKeywords();
}
function removeCleanupKeyword(idx) {
  cleanupKeywords.splice(idx, 1);
  _renderCleanupChips();
  saveCleanupKeywords();
}
async function toggleCleanupDropdown() {
  const dd = document.getElementById('cleanupDropdown');
  if (!dd) return;
  if (dd.classList.contains('open')) { dd.classList.remove('open'); return; }
  await loadCleanupKeywords();
  _renderCleanupChips();
  dd.classList.add('open');
  setTimeout(() => {
    document.addEventListener('click', _cleanupOutsideClick, { once: true });
  }, 0);
}
function _cleanupOutsideClick(e) {
  const dd = document.getElementById('cleanupDropdown');
  const btn = document.getElementById('cleanupBtn');
  if (!dd) return;
  if (dd.contains(e.target) || (btn && btn.contains(e.target))) {
    document.addEventListener('click', _cleanupOutsideClick, { once: true });
    return;
  }
  dd.classList.remove('open');
}

// ===== CLEANUP EXECUTION =====
let _cleanupCancel = false;
let _cleanupMatches = [];
let _cleanupStats = { folders: 0, items: 0, matches: 0 };

function _coSetTitle(t) { document.getElementById('coTitleText').textContent = t; }
function _coSetCurrent(t) {
  const el = document.getElementById('coCurrent');
  const txt = document.getElementById('coCurrentText');
  if (t == null) { el.style.display = 'none'; return; }
  el.style.display = 'flex';
  txt.textContent = t;
}
function _coSetProgress(visible, pct) {
  const wrap = document.getElementById('coProgressbarWrap');
  const inner = document.getElementById('coProgressBarInner');
  wrap.style.display = visible ? 'block' : 'none';
  if (typeof pct === 'number') inner.style.width = pct.toFixed(1) + '%';
}
function _coSetStats(folders, items, matches) {
  _cleanupStats = { folders, items, matches };
  document.getElementById('coStatFolders').textContent = folders;
  document.getElementById('coStatItems').textContent = items;
  document.getElementById('coStatMatches').textContent = matches;
}
function _coSetSummary(html) { document.getElementById('coSummary').innerHTML = html; }
function _coShowRunBtn(show, label) {
  const btn = document.getElementById('coRunBtn');
  btn.style.display = show ? 'inline-flex' : 'none';
  if (label) document.getElementById('coRunBtnText').textContent = label;
}
function _coShowCancelBtn(show, label) {
  const btn = document.getElementById('coCancelBtn');
  btn.style.display = show ? 'inline-flex' : 'none';
  if (label) btn.textContent = label;
}

function _coAppendItem(matchObj) {
  const list = document.getElementById('coList');
  const item = document.createElement('div');
  item.className = 'co-item';
  item.dataset.cleanupId = matchObj.f.id;
  item.innerHTML = \`<i class="bi bi-magic co-item-icon"></i><span class="co-item-old" title="\${escAttr(matchObj.f.name)}">\${escHtml(matchObj.f.name)}</span><span class="co-item-arrow">→</span><span class="co-item-new" title="\${escAttr(matchObj.newName)}">\${escHtml(matchObj.newName)}</span>\`;
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;
  return item;
}

// scan a single folder, returns { subfolders }
async function _cleanupScanFolder(fid) {
  const subfolders = [];
  let pageToken = null;
  do {
    if (_cleanupCancel) return { subfolders };
    const params = {
      q: \`'\${fid}' in parents and trashed = false\`,
      fields: 'nextPageToken, files(id, name, mimeType, capabilities/canRename)',
      pageSize: '1000',
      includeItemsFromAllDrives: 'true',
      supportsAllDrives: 'true',
    };
    if (pageToken) params.pageToken = pageToken;
    let r;
    try { r = await driveAPI('/files', 'GET', null, params); }
    catch (e) { console.error('cleanup list fail', fid, e); break; }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      console.error('cleanup list http', fid, r.status, err);
      break;
    }
    const d = await r.json();
    for (const f of d.files || []) {
      _cleanupStats.items++;
      if (f.mimeType === 'application/vnd.google-apps.folder') subfolders.push(f);
      const newName = applyKeywordsToName(f.name, cleanupKeywords);
      if (newName && newName !== f.name) {
        const m = { f, newName };
        _cleanupMatches.push(m);
        _cleanupStats.matches++;
        _coAppendItem(m);
      }
    }
    pageToken = d.nextPageToken || null;
  } while (pageToken);
  _coSetStats(_cleanupStats.folders, _cleanupStats.items, _cleanupStats.matches);
  return { subfolders };
}

// BFS scan with concurrency
async function _cleanupBfsScan(rootId, rootName) {
  const SCAN_CONC = 4;
  const queue = [{ id: rootId, name: rootName }];
  while (queue.length) {
    if (_cleanupCancel) return;
    const batch = queue.splice(0, SCAN_CONC);
    _coSetCurrent(batch[0].name + (batch.length > 1 ? \` (+\${batch.length - 1} folder lain)\` : ''));
    const results = await Promise.all(batch.map(it => _cleanupScanFolder(it.id).then(r => ({ name: it.name, subs: r.subfolders }))));
    for (const res of results) {
      _cleanupStats.folders++;
      for (const s of res.subs) queue.push({ id: s.id, name: res.name + ' / ' + s.name });
    }
    _coSetStats(_cleanupStats.folders, _cleanupStats.items, _cleanupStats.matches);
  }
  _coSetCurrent(null);
}

async function startCleanup() {
  const last = currentPath[currentPath.length - 1];
  if (!last || !last.id || last.id === 'root' || last.id === 'shared' || currentPath.length < 2) {
    showToast('Buka folder dulu untuk membersihkan.', 'error'); return;
  }
  if (!cleanupKeywords.length) { showToast('Tambahkan minimal satu kata.', 'error'); return; }
  document.getElementById('cleanupDropdown').classList.remove('open');

  // reset state
  _cleanupCancel = false;
  _cleanupMatches = [];
  _cleanupStats = { folders: 0, items: 0, matches: 0 };
  document.getElementById('coList').innerHTML = '';
  _coSetStats(0, 0, 0);
  _coSetSummary('');
  _coSetTitle('Memindai folder & sub-folder');
  _coSetCurrent('Memulai pemindaian...');
  _coSetProgress(false, 0);
  _coShowRunBtn(false);
  _coShowCancelBtn(true, 'Batal');
  document.getElementById('cleanupOverlay').classList.add('open');

  // SCAN PHASE
  try { await _cleanupBfsScan(last.id, last.name); }
  catch (e) {
    console.error(e);
    showToast('Gagal memindai: ' + (e.message || e), 'error');
  }

  if (_cleanupCancel) return;

  if (!_cleanupMatches.length) {
    _coSetTitle('Selesai — tidak ada yang perlu dibersihkan');
    _coSetSummary(\`<span>Total: <b>\${_cleanupStats.items}</b></span><span style="color:var(--text3)">tidak ada match</span>\`);
    _coShowCancelBtn(true, 'Tutup');
    return;
  }

  // CONFIRM PHASE
  _coSetTitle(\`Ditemukan \${_cleanupMatches.length} item — review & bersihkan\`);
  _coSetSummary(\`<span>Cocok: <b style="color:var(--accent)">\${_cleanupMatches.length}</b> dari <b>\${_cleanupStats.items}</b> item</span>\`);
  _coShowRunBtn(true, \`Bersihkan \${_cleanupMatches.length} item\`);
}

async function confirmCleanupRun() {
  // EXECUTE PHASE
  const matches = _cleanupMatches;
  if (!matches.length) return;
  _coShowRunBtn(false);
  _coShowCancelBtn(false);
  _coSetTitle('Membersihkan nama...');
  _coSetSummary('');
  _coSetProgress(true, 0);

  const RENAME_CONC = 4;
  let cursor = 0, done = 0, fail = 0;
  const total = matches.length;

  async function worker() {
    while (cursor < total) {
      if (_cleanupCancel) break;
      const my = cursor++;
      const m = matches[my];
      const itemEl = document.querySelector(\`[data-cleanup-id="\${m.f.id}"]\`);
      if (itemEl) itemEl.classList.add('processing');
      let success = false, errMsg = '';
      if (m.f.capabilities && m.f.capabilities.canRename === false) {
        errMsg = 'Tidak punya izin rename';
      } else {
        try {
          const r = await driveAPI('/files/' + m.f.id, 'PATCH', { name: m.newName }, { supportsAllDrives: 'true' });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            errMsg = (err.error && err.error.message) || ('HTTP ' + r.status);
          } else success = true;
        } catch (e) { errMsg = e.message || String(e); }
      }
      if (success) {
        done++;
        if (itemEl) {
          itemEl.classList.remove('processing');
          itemEl.classList.add('success');
          // delete-anim after a tiny delay
          setTimeout(() => {
            itemEl.classList.add('deleting');
            setTimeout(() => itemEl.remove(), 550);
          }, 250);
        }
      } else {
        fail++;
        if (itemEl) {
          itemEl.classList.remove('processing');
          itemEl.classList.add('failed');
          itemEl.title = errMsg;
          const newSpan = itemEl.querySelector('.co-item-new');
          if (newSpan) newSpan.textContent = '✗ ' + errMsg;
        }
      }
      const t = done + fail;
      _coSetProgress(true, (t / total) * 100);
      _coSetSummary(\`<span>Selesai: <b style="color:var(--green)">\${done}</b></span><span>Gagal: <b style="color:var(--red)">\${fail}</b></span><span style="color:var(--text3)">\${t} / \${total}</span>\`);
    }
  }
  await Promise.all(Array.from({ length: Math.min(RENAME_CONC, total) }, () => worker()));

  _coSetTitle('Selesai');
  _coSetCurrent(null);
  _coSetSummary(\`<span>Sukses: <b style="color:var(--green)">\${done}</b></span><span>Gagal: <b style="color:var(--red)">\${fail}</b></span><span>Total: <b>\${total}</b></span>\`);
  _coShowCancelBtn(true, 'Tutup');
  // refresh list to reflect renamed items
  loadFiles();
}

function closeCleanupOverlay() {
  _cleanupCancel = true;
  document.getElementById('cleanupOverlay').classList.remove('open');
}

// ===== REPORT FOLDER/FILE =====
let _reportTarget = null;
function openReportModal(driveId, name, kind) {
  _reportTarget = { id: driveId, name, kind: kind || 'folder' };
  const link = kind === 'folder'
    ? 'https://drive.google.com/drive/folders/' + driveId + '?usp=drive_link'
    : 'https://drive.google.com/file/d/' + driveId + '/view?usp=drive_link';
  document.getElementById('rpName').textContent = name;
  document.getElementById('rpName').title = name;
  document.getElementById('rpLink').textContent = link;
  document.getElementById('rpLink').title = link;
  document.getElementById('rpKindLabel').textContent = kind === 'folder' ? 'folder' : 'file';
  const icon = document.getElementById('rpKindIcon');
  icon.className = kind === 'folder' ? 'bi bi-folder-fill' : 'bi bi-file-earmark-fill';
  document.getElementById('rpStatus').textContent = '';
  const btn = document.getElementById('rpSendBtn');
  btn.disabled = false;
  btn.innerHTML = '<i class="bi bi-send-fill"></i> Kirim Laporan';
  openModal('reportModal');
}

async function submitReport() {
  if (!_reportTarget) return;
  const btn = document.getElementById('rpSendBtn');
  const status = document.getElementById('rpStatus');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Mengirim...';
  status.textContent = 'Menghubungi server...';
  status.style.color = 'var(--text2)';
  try {
    const r = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: _reportTarget.name, drive_id: _reportTarget.id, kind: _reportTarget.kind }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      status.textContent = 'Gagal: ' + (d.error || ('HTTP ' + r.status));
      status.style.color = 'var(--red)';
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-send-fill"></i> Coba Lagi';
      return;
    }
    if (d.duplicate) {
      status.textContent = d.message || 'Sudah dilaporkan sebelumnya, belum diselesaikan.';
      status.style.color = 'var(--yellow, #d97706)';
      btn.innerHTML = '<i class="bi bi-check-circle-fill"></i> OK';
      setTimeout(() => closeModal('reportModal'), 1200);
      return;
    }
    let msg = 'Laporan dikirim. Total ' + (d.total_open || 1) + ' belum diselesaikan.';
    if (d.email === 'sent') {
      msg += ' Email batch (' + (d.batch_size || 5) + ' laporan) terkirim ke admin.';
    } else if (d.email && d.email.startsWith && d.email.startsWith('queued:')) {
      // queued:X/Y where X is pending, Y is threshold
      const parts = d.email.replace('queued:','').split('/');
      msg += ' Email belum dikirim (' + parts[0] + '/' + parts[1] + ' menuju batch berikutnya).';
    } else if (d.email === 'no_config') {
      msg += ' (Email belum dikonfigurasi.)';
    } else if (d.email && d.email !== 'sent') {
      msg += ' (email: ' + d.email + ')';
    }
    status.textContent = msg;
    status.style.color = 'var(--green)';
    btn.innerHTML = '<i class="bi bi-check-circle-fill"></i> Terkirim';
    showToast('Laporan terkirim ke akak', 'success');
    setTimeout(() => closeModal('reportModal'), 1500);
  } catch (e) {
    status.textContent = 'Error: ' + (e.message || e);
    status.style.color = 'var(--red)';
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-send-fill"></i> Coba Lagi';
  }
}

/* SEMANGAT marquee for ayang */
(function semangatInit() {
  const MESSAGES = [
    'Semangatttt sayanggg ❤️',
    'Aku percaya kamu pasti bisa, ayanggg 💪💖',
    'Kamu hebat banget hari ini, sayanggg 🌸✨',
    'Jangan nyerah ya cintaku, aku selalu support kamu 🥰',
    'Senyum dulu yuk ayang, aku sayang banget sama kamu 💕'
  ];
  const POSITIONS = ['pos-top', 'pos-mid', 'pos-bot'];
  const BG_COUNT = 8;

  function showSemangat() {
    const overlay = document.createElement('div');
    overlay.className = 'semangat-overlay ' + POSITIONS[Math.floor(Math.random() * POSITIONS.length)];
    const msg = document.createElement('div');
    msg.className = 'semangat-msg semangat-bg-' + (Math.floor(Math.random() * BG_COUNT) + 1);
    msg.textContent = MESSAGES[Math.floor(Math.random() * MESSAGES.length)];
    overlay.appendChild(msg);
    document.body.appendChild(overlay);
    msg.addEventListener('animationend', () => overlay.remove());
    setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, 12000);
  }

  // schedule: first show after 5s, then alternate 5 min and 3 min
  const INTERVALS = [5 * 60 * 1000, 3 * 60 * 1000];
  let i = 0;
  setTimeout(function loop() {
    showSemangat();
    setTimeout(loop, INTERVALS[i++ % INTERVALS.length]);
  }, 5000);
})();
</script>
</body>
</html>
`;

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);

    // Serve the web app for root
    if (url.pathname === '/' || url.pathname === '/index.html') {
      return new Response(HTML, {
        headers: { 'Content-Type': 'text/html;charset=UTF-8' },
      });
    }

    // Health check
    if (url.pathname === '/health') {
      return new Response(JSON.stringify({ ok: true }), {
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }

    // Report endpoint
    if (url.pathname === '/api/report' && request.method === 'POST') {
      try { return await handleReport(request); }
      catch (e) { return jsonError(500, 'internal: ' + (e.message || e)); }
    }

    // Stream upload config endpoint (reads Cloudflare Variables, does not expose secrets)
    if (url.pathname === '/api/stream-config' && request.method === 'GET') {
      try { return await handleStreamConfig(env); }
      catch (e) { return jsonError(500, 'internal: ' + (e.message || e)); }
    }

    // TMDB proxy endpoint so TMDB_API_KEY stays in Cloudflare Variables
    if (url.pathname === '/api/tmdb/search' && request.method === 'GET') {
      try { return await handleTmdbSearch(request, env); }
      catch (e) { return jsonError(500, 'internal: ' + (e.message || e)); }
    }

    // Bot API proxy endpoint so BOT_API_SECRET stays server-side
    if (url.pathname === '/api/stream-jobs' && (request.method === 'POST' || request.method === 'GET')) {
      try { return await handleStreamJob(request, env); }
      catch (e) { return jsonError(500, 'internal: ' + (e.message || e)); }
    }

    return new Response(JSON.stringify({ error: 'Not found' }), {
      status: 404,
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  },
};
