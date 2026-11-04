'use strict';

/**
 * Electron 主进程：启动内置 Python 后端（SQLite + 本地文件存储），
 * 后端同时托管前端页面，窗口直接加载本机 HTTP 地址。
 */
const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const APP_NAME = 'AI报告生成';
const PREFERRED_PORT = 18080;
const STARTUP_TIMEOUT_MS = 120000;

app.setName(APP_NAME);

const isPackaged = app.isPackaged;
const PROJECT_ROOT = path.join(__dirname, '..');
// 打包后资源位于 Contents/Resources（mac）或 resources（Windows）下；
// 未打包时使用 build/desktop 内的构建产物，便于本地调试。
const RESOURCE_ROOT = process.env.AI_REPORT_RESOURCES_DIR
  || (isPackaged ? process.resourcesPath : path.join(PROJECT_ROOT, 'build', 'desktop'));

const DATA_DIR = path.join(app.getPath('userData'), 'data');
const LOG_DIR = path.join(app.getPath('userData'), 'logs');
const CONFIG_DIR = app.getPath('userData');

let mainWindow = null;
let backendProcess = null;
let backendPort = 0;
let quitting = false;
let logStream = null;

function ensureDirs() {
  for (const dir of [DATA_DIR, LOG_DIR, CONFIG_DIR, path.join(DATA_DIR, 'storage')]) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function openBackendLog() {
  const logFile = path.join(LOG_DIR, 'backend.log');
  try {
    if (fs.existsSync(logFile) && fs.statSync(logFile).size > 10 * 1024 * 1024) {
      fs.renameSync(logFile, logFile + '.1');
    }
  } catch (_) { /* 日志轮转失败不影响启动 */ }
  logStream = fs.createWriteStream(logFile, { flags: 'a' });
  logStream.write(`\n===== ${new Date().toISOString()} 启动后端 =====\n`);
  return logFile;
}

function loadSecrets() {
  const file = path.join(CONFIG_DIR, 'app-secrets.json');
  let secrets = null;
  try {
    secrets = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) { /* 首次运行或文件损坏时重新生成 */ }
  const fernetKey = crypto.randomBytes(32).toString('base64').replace(/\+/g, '-').replace(/\//g, '_');
  if (!secrets || typeof secrets.fernetKey !== 'string' || secrets.fernetKey.length < 40) {
    secrets = { ...(secrets || {}), fernetKey };
  }
  if (!secrets.jwtSecret || typeof secrets.jwtSecret !== 'string') {
    secrets.jwtSecret = crypto.randomBytes(48).toString('hex');
  }
  fs.writeFileSync(file, JSON.stringify(secrets, null, 2), { mode: 0o600 });
  return secrets;
}

function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => server.close(() => resolve(true)));
    server.listen(port, '127.0.0.1');
  });
}

function pickFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = address && typeof address === 'object' ? address.port : null;
      server.close(() => (port ? resolve(port) : reject(new Error('系统未能分配空闲端口'))));
    });
  });
}

async function pickBackendPort() {
  if (await isPortFree(PREFERRED_PORT)) return PREFERRED_PORT;
  return pickFreePort();
}

function resolvePython() {
  const candidates = process.platform === 'win32'
    ? [path.join(RESOURCE_ROOT, 'python', 'python.exe')]
    : [
        path.join(RESOURCE_ROOT, 'python', 'bin', 'python3'),
        path.join(RESOURCE_ROOT, 'python', 'bin', 'python3.13'),
        path.join(RESOURCE_ROOT, 'python', 'bin', 'python'),
      ];
  return candidates.find((file) => fs.existsSync(file)) || candidates[0];
}

function backendEnv(port, secrets) {
  return {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONNOUSERSITE: '1',
    // 内置数据库与本地文件存储
    DB_ENGINE: 'sqlite',
    SQLITE_PATH: path.join(DATA_DIR, 'app.db'),
    STORAGE_BACKEND: 'local',
    STORAGE_LOCAL_DIR: path.join(DATA_DIR, 'storage'),
    // 由桌面端托管前端构建产物
    AI_REPORT_FRONTEND_DIST: path.join(RESOURCE_ROOT, 'frontend', 'dist'),
    // 客户端不内置 LibreOffice/poppler，导出时跳过原生渲染验收（结构校验仍执行）
    VISUAL_QA_REQUIRE_NATIVE_RENDERER: process.env.VISUAL_QA_REQUIRE_NATIVE_RENDERER || 'false',
    // 用户可在数据目录的 .env 中覆盖模型、渲染器等等级配置
    AI_REPORT_ENV_FILE: path.join(CONFIG_DIR, 'app.env'),
    MODEL_CONFIG_ENCRYPTION_KEY: secrets.fernetKey,
    JWT_SECRET_KEY: secrets.jwtSecret,
    BACKEND_PORT: String(port),
    CORS_ORIGINS: `http://127.0.0.1:${port},http://localhost:${port}`,
  };
}

function startBackend(port, logFile) {
  const python = resolvePython();
  if (!fs.existsSync(python)) {
    return Promise.reject(new Error(`未找到内置 Python 运行时：${python}\n请先运行打包脚本生成 build/desktop 目录。`));
  }
  const secrets = loadSecrets();
  const args = ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port), '--log-level', 'info'];
  backendPort = port;

  backendProcess = spawn(python, args, {
    cwd: path.join(RESOURCE_ROOT, 'backend'),
    env: backendEnv(port, secrets),
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  const write = (chunk) => {
    const text = chunk.toString();
    if (logStream) logStream.write(text);
    process.stdout.write(text);
  };
  backendProcess.stdout.on('data', write);
  backendProcess.stderr.on('data', write);

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve();
    };
    backendProcess.once('error', (error) => finish(new Error(`后端进程启动失败：${error.message}`)));
    backendProcess.once('exit', (code, signal) => {
      const message = `后端进程已退出（code=${code}, signal=${signal}）。日志：${logFile}`;
      if (quitting) return;
      if (!settled) finish(new Error(message));
      else if (mainWindow) {
        dialog.showErrorBox(APP_NAME, message);
        app.quit();
      }
    });
    waitForHealth(port, STARTUP_TIMEOUT_MS)
      .then(() => finish())
      .catch((error) => {
        stopBackend();
        finish(new Error(`${error.message}\n日志：${logFile}`));
      });
  });
}

function waitForHealth(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (quitting) return reject(new Error('应用正在退出'));
      const req = http.get({ host: '127.0.0.1', port, path: '/api/health', timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on('error', retry);
      req.on('timeout', () => req.destroy());
    };
    const retry = () => {
      if (Date.now() > deadline) return reject(new Error('后端启动超时，请查看日志'));
      setTimeout(attempt, 400);
    };
    attempt();
  });
}

function stopBackend() {
  const proc = backendProcess;
  backendProcess = null;
  if (!proc || proc.exitCode != null || proc.signalCode != null) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    proc.once('exit', finish);
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(proc.pid), '/t', '/f'], { windowsHide: true })
        .on('error', () => { try { proc.kill(); } catch (_) {} })
        .on('exit', finish);
      setTimeout(finish, 5000);
    } else {
      try { proc.kill('SIGTERM'); } catch (_) { finish(); }
      setTimeout(() => {
        try { proc.kill('SIGKILL'); } catch (_) {}
        finish();
      }, 5000);
    }
  });
}

function buildMenu() {
  const template = [
    ...(process.platform === 'darwin'
      ? [{ label: APP_NAME, submenu: [
          { role: 'about' }, { type: 'separator' }, { role: 'hide' },
          { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }, { role: 'quit' },
        ] }]
      : []),
    { label: '编辑', submenu: [
      { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
    ] },
    { label: '视图', submenu: [
      { role: 'reload' }, { role: 'forceReload' }, { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' }, { role: 'togglefullscreen' },
      ...(isPackaged ? [] : [{ role: 'toggleDevTools' }]),
    ] },
    { label: '窗口', submenu: [{ role: 'minimize' }, { role: 'close' }] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    title: APP_NAME,
    backgroundColor: '#f5f6f8',
    autoHideMenuBar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  mainWindow.once('ready-to-show', () => {
    if (mainWindow) mainWindow.show();
  });
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const origin = `http://127.0.0.1:${backendPort}`;
    if (!url.startsWith(origin)) {
      event.preventDefault();
      if (/^https?:/i.test(url)) shell.openExternal(url);
    }
  });
  return mainWindow;
}

async function boot() {
  ensureDirs();
  const logFile = openBackendLog();
  try {
    const port = await pickBackendPort();
    await startBackend(port, logFile);
    buildMenu();
    const win = createWindow();
    await win.loadURL(`http://127.0.0.1:${port}/`);
  } catch (error) {
    dialog.showErrorBox(`${APP_NAME} 启动失败`, error.message || String(error));
    app.quit();
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.on('before-quit', (event) => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    stopBackend().finally(() => {
      if (logStream) logStream.end();
      app.quit();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0 && backendPort && !quitting) {
      const win = createWindow();
      win.loadURL(`http://127.0.0.1:${backendPort}/`);
    }
  });

  app.whenReady().then(boot);
}
