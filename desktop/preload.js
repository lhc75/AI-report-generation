'use strict';

/**
 * 预加载脚本：只暴露更新安装与进度事件，不开放 Node 能力给页面。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('aiReportDesktop', {
  isDesktop: true,
  installUpdate: (asset) => ipcRenderer.invoke('update:install', asset),
  onUpdateProgress: (callback) => {
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on('update:progress', handler);
    return () => ipcRenderer.removeListener('update:progress', handler);
  },
  onOpenAbout: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('update:open-about', handler);
    return () => ipcRenderer.removeListener('update:open-about', handler);
  },
});
