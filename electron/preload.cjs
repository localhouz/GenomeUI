'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  isElectron: true,
  platform: process.platform,
  setIntentContext: (context) => ipcRenderer.send('intent:setContext', String(context || 'general')),
  listIntentContexts: () => ipcRenderer.invoke('intent:listContexts'),
  revokeContext: (context) => ipcRenderer.send('intent:revokeContext', String(context || 'general')),
  onPrivacyEvent: (callback) => {
    if (typeof callback !== 'function') return;
    ipcRenderer.on('privacy:event', (_evt, data) => callback(data));
  },
  minimizeWindow: () => ipcRenderer.send('window:minimize'),
  maximizeWindow: () => ipcRenderer.send('window:maximize'),
  closeWindow: () => ipcRenderer.send('window:close'),
  newWindow: (targetUrl = '') => ipcRenderer.send('window:new', String(targetUrl || '')),
  notify: (opts) => ipcRenderer.invoke('os:notify', opts),
  onNotificationClick: (callback) => {
    if (typeof callback !== 'function') return;
    ipcRenderer.on('os:notification:click', (_evt, data) => callback(data));
  },
  reportCrash: (report) => ipcRenderer.invoke('os:reportCrash', report),
  installUpdateNow: () => ipcRenderer.invoke('updater:installNow'),
  onUpdaterStatus: (callback) => {
    if (typeof callback !== 'function') return;
    ipcRenderer.on('updater:status', (_evt, data) => callback(data));
  },
});

// Capture renderer-process errors and forward to main process crash log
window.addEventListener('error', (evt) => {
  ipcRenderer.invoke('os:reportCrash', {
    process: 'renderer',
    message: evt?.message || String(evt),
    stack: evt?.error?.stack || '',
  }).catch(() => {});
});
window.addEventListener('unhandledrejection', (evt) => {
  const msg   = evt?.reason instanceof Error ? evt.reason.message : String(evt?.reason || '');
  const stack = evt?.reason instanceof Error ? (evt.reason.stack || '') : '';
  ipcRenderer.invoke('os:reportCrash', { process: 'renderer', message: msg, stack }).catch(() => {});
});
