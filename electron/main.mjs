import { app, BrowserWindow, ipcMain, session } from 'electron';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import {
  sanitizeRequestHeaders,
  sanitizeResponseHeaders,
  stripTrackingFromUrl,
} from './privacy.mjs';
import { issueIntentToken, listContexts, revokeContext } from './credentials.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = !app.isPackaged;
const PRIVACY_INSTALLED = new WeakSet();
let CURRENT_INTENT_CONTEXT = 'general';

function installWindowIpc() {
  ipcMain.on('window:minimize', () => BrowserWindow.getFocusedWindow()?.minimize());
  ipcMain.on('window:maximize', () => {
    const win = BrowserWindow.getFocusedWindow();
    if (!win) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on('window:close', () => BrowserWindow.getFocusedWindow()?.close());
  ipcMain.on('intent:setContext', (_event, context) => {
    const next = String(context || 'general').toLowerCase().replace(/[^a-z0-9-]/g, '');
    CURRENT_INTENT_CONTEXT = next || 'general';
  });
  ipcMain.handle('intent:listContexts', () => listContexts());
  ipcMain.on('intent:revokeContext', (_event, context) => {
    revokeContext(context);
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

function installPrivacyLayer(targetSession) {
  if (!targetSession || PRIVACY_INSTALLED.has(targetSession)) return;
  PRIVACY_INSTALLED.add(targetSession);

  targetSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
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
      preload: path.join(__dirname, 'preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
    },
  });

  installPrivacyLayer(session.defaultSession);

  if (isDev) {
    await win.loadURL('http://127.0.0.1:5173');
    win.webContents.openDevTools({ mode: 'detach' });
  } else {
    await win.loadFile(path.join(__dirname, '../dist/index.html'));
  }
}

app.whenReady().then(async () => {
  installWindowIpc();
  app.on('session-created', (sess) => installPrivacyLayer(sess));
  await createWindow();

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      await createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
