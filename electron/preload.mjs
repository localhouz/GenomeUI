import { contextBridge, ipcRenderer } from 'electron';

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
});
