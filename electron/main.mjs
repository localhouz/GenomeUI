import { app, BrowserWindow, ipcMain, session, Notification } from 'electron';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import { createRequire } from 'node:module';

// electron-updater is a CommonJS module — use createRequire to import it in ESM
const _require = createRequire(import.meta.url);
const { autoUpdater } = _require('electron-updater');
import {
  sanitizeRequestHeaders,
  sanitizeResponseHeaders,
  stripTrackingFromUrl,
} from './privacy.mjs';
import { issueIntentToken, listContexts, revokeContext } from './credentials.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = !app.isPackaged;
let PENDING_UPDATE_INFO = null;

// Load .env from project root so keys are available regardless of how Electron was launched
try {
  const dotEnv = path.join(__dirname, '..', '.env');
  const lines = fs.readFileSync(dotEnv, 'utf8').split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.replace(/^\uFEFF/, '').trim(); // strip BOM
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).trim();
    if (key && !(key in process.env)) process.env[key] = val;
  }
} catch { /* no .env — fine */ }

// ── Crash reporting ───────────────────────────────────────────────────────────
const CRASH_LOG = path.join(__dirname, '..', 'electron.crash.log');
const BACKEND_CRASH_URL = 'http://127.0.0.1:8787/api/crash';

function writeCrashLog(processName, message, stack = '') {
  const line = JSON.stringify({
    ts: Date.now(),
    process: processName,
    message: String(message).slice(0, 512),
    stack: String(stack).slice(0, 4096),
    version: app.getVersion(),
  });
  try { fs.appendFileSync(CRASH_LOG, line + '\n'); } catch { /* disk full etc. */ }
  // Best-effort POST to backend so it's aggregated in one log
  fetch(BACKEND_CRASH_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ process: processName, message: String(message).slice(0, 512),
                           stack: String(stack).slice(0, 4096), version: app.getVersion(), ts: Date.now() }),
  }).catch(() => {});
}

process.on('uncaughtException', (err) => {
  writeCrashLog('electron', err?.message || String(err), err?.stack || '');
});
process.on('unhandledRejection', (reason) => {
  const msg = reason instanceof Error ? reason.message : String(reason);
  const stack = reason instanceof Error ? (reason.stack || '') : '';
  writeCrashLog('electron', msg, stack);
});

// Use a project-local profile so multiple dev instances don't stomp each other's cache
if (isDev) {
  app.setPath('userData', path.join(__dirname, '..', '.electron-profile'));
}
const PRIVACY_INSTALLED = new WeakSet();
let CURRENT_INTENT_CONTEXT = 'general';

function normalizeWindowTarget(targetUrl = '') {
  const raw = String(targetUrl || '').trim();
  if (!raw) return { devUrl: '', fileSearch: '' };
  try {
    const parsed = new URL(raw);
    const localDevOrigins = new Set(['http://localhost:5173', 'http://127.0.0.1:5173']);
    if (isDev && localDevOrigins.has(parsed.origin)) {
      return { devUrl: parsed.toString(), fileSearch: parsed.search || '' };
    }
    return { devUrl: '', fileSearch: parsed.search || '' };
  } catch {
    return { devUrl: '', fileSearch: '' };
  }
}

async function openNewWindow(targetUrl = '') {
  const windowTarget = normalizeWindowTarget(targetUrl);
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 768,
    minHeight: 500,
    backgroundColor: '#060d14',
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#060d14',
      symbolColor: 'rgba(255,255,255,0.5)',
      height: 32,
    },
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
    },
  });

  // Allow OAuth popups in new windows too
  win.webContents.setWindowOpenHandler(({ url }) => {
    const allowed = ['accounts.google.com', 'slack.com', 'spotify.com', 'github.com'];
    const isOAuth = allowed.some(h => url.includes(h)) || url.includes('/api/connectors/oauth');
    if (isOAuth) return { action: 'allow' };
    return { action: 'deny' };
  });

  win.webContents.on('did-finish-load', () => {
    if (PENDING_UPDATE_INFO) {
      emitUpdaterStatus('ready', { version: PENDING_UPDATE_INFO.version || null });
    }
  });

  if (isDev) {
    await win.loadURL(windowTarget.devUrl || 'http://localhost:5173');
  } else {
    await win.loadFile(path.join(__dirname, '../dist/index.html'), {
      search: windowTarget.fileSearch,
    });
  }
}

function installWindowIpc() {
  ipcMain.on('window:minimize', () => BrowserWindow.getFocusedWindow()?.minimize());
  ipcMain.on('window:maximize', () => {
    const win = BrowserWindow.getFocusedWindow();
    if (!win) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on('window:close', () => BrowserWindow.getFocusedWindow()?.close());
  ipcMain.on('window:new', (_event, targetUrl) => openNewWindow(String(targetUrl || '')).catch(() => {}));
  ipcMain.on('intent:setContext', (_event, context) => {
    const next = String(context || 'general').toLowerCase().replace(/[^a-z0-9-]/g, '');
    CURRENT_INTENT_CONTEXT = next || 'general';
  });
  ipcMain.handle('intent:listContexts', () => listContexts());
  ipcMain.on('intent:revokeContext', (_event, context) => {
    revokeContext(context);
  });

  ipcMain.handle('os:reportCrash', (_event, report) => {
    const msg   = String(report?.message || '').slice(0, 512);
    const stack = String(report?.stack   || '').slice(0, 4096);
    writeCrashLog(String(report?.process || 'renderer'), msg, stack);
  });

  ipcMain.handle('os:notify', (_event, opts) => {
    if (!Notification.isSupported()) return;
    const title = String(opts?.title || 'GenomeUI').slice(0, 80);
    const body  = String(opts?.body  || '').slice(0, 240);
    const route = String(opts?.route || '').slice(0, 200);
    const n = new Notification({ title, body, silent: false });
    n.on('click', () => {
      const win = focusPrimaryWindow();
      if (win) {
        if (route) win.webContents.send('os:notification:click', { route });
      }
    });
    n.show();
  });

  ipcMain.handle('updater:installNow', () => {
    if (isDev || !PENDING_UPDATE_INFO) {
      return { ok: false, reason: 'no-pending-update' };
    }
    setImmediate(() => {
      try {
        autoUpdater.quitAndInstall(false, true);
      } catch (err) {
        writeCrashLog('electron-updater', err?.message || String(err), err?.stack || '');
      }
    });
    return { ok: true, version: PENDING_UPDATE_INFO.version || null };
  });
}

function emitPrivacyEvent(event) {
  const windows = BrowserWindow.getAllWindows();
  for (const win of windows) {
    try {
      win.webContents.send('privacy:event', event);
    } catch {
      // Best-effort telemetry.
    }
  }
}

function emitUpdaterStatus(event, payload = {}) {
  const windows = BrowserWindow.getAllWindows();
  for (const win of windows) {
    try {
      win.webContents.send('updater:status', { event, ...payload });
    } catch {
      // Best-effort status fanout.
    }
  }
}

function focusPrimaryWindow() {
  const win = BrowserWindow.getAllWindows()[0];
  if (!win) return null;
  if (win.isMinimized()) win.restore();
  win.focus();
  return win;
}

function installPrivacyLayer(targetSession) {
  if (!targetSession || PRIVACY_INSTALLED.has(targetSession)) return;
  PRIVACY_INSTALLED.add(targetSession);

  targetSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    // Block Chromium's internal geolocation probe — it always 403s without a
    // Geolocation Web Service API key. OS GPS still works via Windows Location Services.
    if (details?.url?.includes('googleapis.com/geolocation')) {
      callback({ cancel: true });
      return;
    }
    const stripped = stripTrackingFromUrl(details?.url);
    if (stripped.ok && stripped.changed) {
      emitPrivacyEvent({
        type: 'tracking_params_stripped',
        strippedCount: stripped.strippedCount,
        url: stripped.url,
        ts: Date.now(),
      });
      callback({ redirectURL: stripped.url });
      return;
    }
    callback({});
  });

  targetSession.webRequest.onBeforeSendHeaders({ urls: ['<all_urls>'] }, (details, callback) => {
    const headers = sanitizeRequestHeaders(details, details?.requestHeaders || {});
    try {
      const domain = new URL(String(details?.url || '')).hostname || 'unknown.local';
      headers['X-Genome-Intent-Token'] = issueIntentToken(CURRENT_INTENT_CONTEXT, domain);
    } catch {
      // Ignore malformed URLs.
    }
    callback({ requestHeaders: headers });
  });

  targetSession.webRequest.onHeadersReceived({ urls: ['<all_urls>'] }, (details, callback) => {
    const headers = sanitizeResponseHeaders(details, details?.responseHeaders || {});
    callback({ responseHeaders: headers });
  });
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 768,
    minHeight: 500,
    backgroundColor: '#060d14',
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#060d14',
      symbolColor: 'rgba(255,255,255,0.5)',
      height: 32,
    },
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
    },
  });

  installPrivacyLayer(session.defaultSession);

  // Allow OAuth popups (window.open from renderer for accounts.google.com etc.)
  win.webContents.setWindowOpenHandler(({ url }) => {
    const allowed = ['accounts.google.com', 'slack.com', 'spotify.com', 'github.com'];
    const isOAuth = allowed.some(h => url.includes(h)) || url.includes('/api/connectors/oauth');
    if (isOAuth) return { action: 'allow' };
    return { action: 'deny' };
  });

  // Always boot clean — OS never resumes from a stale renderer cache.
  await session.defaultSession.clearCache();
  await session.defaultSession.clearStorageData({
    storages: ['serviceworkers', 'cachestorage'],
  });

  // Clear localStorage in the renderer after load — clearStorageData requires an
  // origin which varies by env (localhost:5173 dev vs file:// prod), so this is
  // more reliable: run it directly in the page context.
  win.webContents.on('did-finish-load', () => {
    win.webContents.executeJavaScript('localStorage.clear(); sessionStorage.clear();').catch(() => {});
    if (PENDING_UPDATE_INFO) {
      emitUpdaterStatus('ready', { version: PENDING_UPDATE_INFO.version || null });
    }
  });

  if (isDev) {
    await win.loadURL('http://localhost:5173');
  } else {
    await win.loadFile(path.join(__dirname, '../dist/index.html'));
  }
}

function installAutoUpdater() {
  if (isDev) return; // never auto-update in dev mode

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('checking-for-update',  () => emitUpdaterStatus('checking'));
  autoUpdater.on('update-not-available', () => emitUpdaterStatus('up-to-date'));
  autoUpdater.on('update-available',     (info) => emitUpdaterStatus('available', { version: info.version }));
  autoUpdater.on('download-progress',    (p)    => emitUpdaterStatus('downloading', { percent: Math.round(p.percent) }));
  autoUpdater.on('update-downloaded',    (info) => {
    PENDING_UPDATE_INFO = { version: info.version || null, downloadedAt: Date.now() };
    emitUpdaterStatus('ready', { version: info.version });
    // Show native notification so user knows without looking at the app
    if (Notification.isSupported()) {
      const n = new Notification({
        title: 'GenomeUI update ready',
        body: `Version ${info.version} is ready. Click to focus GenomeUI and restart when you are ready.`,
        silent: true,
      });
      n.on('click', () => {
        focusPrimaryWindow();
        emitUpdaterStatus('ready', { version: info.version });
      });
      n.show();
    }
  });
  autoUpdater.on('error', (err) => {
    // Non-fatal — log but don't crash the app
    writeCrashLog('electron-updater', err?.message || String(err), err?.stack || '');
    emitUpdaterStatus('error', { message: err?.message });
  });

  // Check silently 5 seconds after launch so startup isn't delayed
  setTimeout(() => autoUpdater.checkForUpdates().catch(() => {}), 5000);
}

// Suppress Chromium's network location provider — it requires a Geolocation Web
// Service API key (separate from Maps API) and returns 403 without one.
// OS-level GPS (navigator.geolocation via Windows Location Services) still works.
app.commandLine.appendSwitch('disable-features', 'NetworkLocationProvider');

app.whenReady().then(async () => {
  installWindowIpc();
  app.on('session-created', (sess) => installPrivacyLayer(sess));
  await createWindow();
  installAutoUpdater();

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      await createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
