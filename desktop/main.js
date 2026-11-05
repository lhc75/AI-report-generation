'use strict';

/**
 * Electron 主进程：启动内置 Python 后端（SQLite + 本地文件存储），
 * 后端同时托管前端页面，窗口直接加载本机 HTTP 地址。
 */
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const https = require('https');
const net = require('net');
const os = require('os');
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
    APP_VERSION: app.getVersion(),
    AI_REPORT_DESKTOP: '1',
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

// ---------------------------------------------------------------- 更新安装

const UPDATE_DIR = path.join(app.getPath('userData'), 'updates');
const ALLOWED_UPDATE_HOSTS = new Set([
  'github.com',
  'objects.githubusercontent.com',
  'release-assets.githubusercontent.com',
  'github-releases.githubusercontent.com',
]);

function isAllowedUpdateUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && ALLOWED_UPDATE_HOSTS.has(url.hostname);
  } catch (_) {
    return false;
  }
}

function sendUpdateProgress(payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('update:progress', payload);
  }
}

function downloadUpdateFile(url, destination, onProgress, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 6) return reject(new Error('更新包下载重定向次数过多'));
    if (!isAllowedUpdateUrl(url)) return reject(new Error(`更新下载地址不受信任：${url}`));
    const request = https.get(url, {
      headers: {
        'User-Agent': 'ai-report-desktop',
        Accept: 'application/octet-stream',
      },
    }, (response) => {
      const status = response.statusCode || 0;
      if (status >= 300 && status < 400 && response.headers.location) {
        response.resume();
        const next = new URL(response.headers.location, url).toString();
        return resolve(downloadUpdateFile(next, destination, onProgress, redirects + 1));
      }
      if (status !== 200) {
        response.resume();
        return reject(new Error(`更新包下载失败（HTTP ${status}）`));
      }
      const total = Number(response.headers['content-length'] || 0);
      let received = 0;
      let reported = 0;
      const partial = `${destination}.part`;
      const file = fs.createWriteStream(partial);
      response.on('data', (chunk) => {
        received += chunk.length;
        if (received - reported >= 1024 * 1024 || (total && received >= total)) {
          reported = received;
          onProgress({ received, total });
        }
      });
      response.pipe(file);
      response.on('error', reject);
      file.on('error', (error) => {
        try { file.close(); } catch (_) { /* ignore */ }
        try { fs.unlinkSync(partial); } catch (_) { /* ignore */ }
        reject(error);
      });
      file.on('finish', () => {
        file.close(() => {
          try {
            fs.renameSync(partial, destination);
            resolve(destination);
          } catch (error) {
            reject(error);
          }
        });
      });
    });
    request.on('error', reject);
    request.setTimeout(60000, () => request.destroy(new Error('更新包下载超时')));
  });
}

function currentAppBundlePath() {
  if (process.platform !== 'darwin') return '';
  const marker = `${path.sep}Contents${path.sep}MacOS${path.sep}`;
  const index = process.execPath.indexOf(marker);
  return index > 0 ? process.execPath.slice(0, index) : '';
}

function findAppBundle(root) {
  const pending = [{ dir: root, depth: 0 }];
  while (pending.length) {
    const { dir, depth } = pending.shift();
    if (depth > 3) continue;
    let entries = [];
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (_) { continue; }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory() && entry.name.endsWith('.app')) return full;
      if (entry.isDirectory()) pending.push({ dir: full, depth: depth + 1 });
    }
  }
  return '';
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

async function installOnMac(file, name) {
  const lower = name.toLowerCase();
  if (lower.endsWith('.dmg') || lower.endsWith('.pkg')) {
    const error = await shell.openPath(file);
    if (error) throw new Error(`无法打开安装程序：${error}`);
    return { mode: 'installer', message: '已打开安装镜像，请按提示安装后重新启动应用。' };
  }
  if (!lower.endsWith('.zip')) throw new Error('暂不支持该更新包格式');

  const extractDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ai-report-update-'));
  const unzip = spawnSync('ditto', ['-x', '-k', file, extractDir]);
  if (unzip.status !== 0) {
    throw new Error(`解压更新包失败：${(unzip.stderr || '').toString().slice(0, 300)}`);
  }
  const newBundle = findAppBundle(extractDir);
  if (!newBundle) throw new Error('更新包中未找到 .app 应用');
  const currentBundle = currentAppBundlePath();
  if (!currentBundle) throw new Error('无法定位当前应用位置，请手动安装下载的更新包');

  try {
    fs.accessSync(path.dirname(currentBundle), fs.constants.W_OK);
  } catch (_) {
    const error = await shell.openPath(newBundle);
    if (error) throw new Error('当前应用目录不可写，且无法打开新版本，请手动安装');
    return { mode: 'manual', message: '当前应用目录不可写，已打开新版本，请手动替换安装。' };
  }

  // 等主进程退出后再替换正在运行的 .app，然后重新启动新版本。
  const script = path.join(extractDir, 'install.sh');
  fs.writeFileSync(script, [
    '#!/bin/sh',
    `while kill -0 ${process.pid} 2>/dev/null; do sleep 0.5; done`,
    `rm -rf ${shellQuote(currentBundle)}`,
    `ditto ${shellQuote(newBundle)} ${shellQuote(currentBundle)}`,
    `xattr -dr com.apple.quarantine ${shellQuote(currentBundle)} 2>/dev/null || true`,
    `open ${shellQuote(currentBundle)}`,
  ].join('\n') + '\n', { mode: 0o755 });
  const helper = spawn('/bin/sh', [script], { detached: true, stdio: 'ignore' });
  helper.unref();
  setTimeout(() => app.quit(), 400);
  return { mode: 'auto', message: '更新已下载，应用关闭后会自动替换并重新启动。' };
}

async function installOnWindows(file, name) {
  const lower = name.toLowerCase();
  if (lower.includes('portable')) {
    shell.showItemInFolder(file);
    return { mode: 'manual', message: '已下载便携版，请关闭应用后手动替换。' };
  }
  // 等主进程退出后再运行安装程序，避免占用中的后端/Python 文件导致覆盖失败。
  const script = path.join(os.tmpdir(), `ai-report-install-${process.pid}-${Date.now()}.ps1`);
  const quote = (value) => `'${String(value).replace(/'/g, "''")}'`;
  fs.writeFileSync(script, [
    `Wait-Process -Id ${process.pid} -ErrorAction SilentlyContinue`,
    `Start-Process -FilePath ${quote(file)} -ArgumentList '/S'`,
    'Start-Sleep -Seconds 2',
    `Remove-Item -LiteralPath ${quote(script)} -Force -ErrorAction SilentlyContinue`,
  ].join('\r\n') + '\r\n', { encoding: 'utf8' });
  const helper = spawn('powershell.exe', [
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', script,
  ], { detached: true, stdio: 'ignore', windowsHide: true });
  helper.unref();
  setTimeout(() => app.quit(), 400);
  return { mode: 'auto', message: '更新已下载，应用关闭后会自动运行安装程序。' };
}

function cleanUpdateDir(keepName) {
  let entries = [];
  try { entries = fs.readdirSync(UPDATE_DIR, { withFileTypes: true }); } catch (_) { return; }
  for (const entry of entries) {
    if (!entry.isFile() || entry.name === keepName) continue;
    try { fs.rmSync(path.join(UPDATE_DIR, entry.name), { force: true }); } catch (_) { /* ignore */ }
  }
}

ipcMain.handle('update:install', async (_event, asset) => {
  const url = String(asset?.url || '');
  const rawName = String(asset?.name || '');
  const name = path.basename(rawName);
  if (!name || !isAllowedUpdateUrl(url)) {
    throw new Error('更新包信息无效');
  }
  fs.mkdirSync(UPDATE_DIR, { recursive: true });
  cleanUpdateDir(name);
  const target = path.join(UPDATE_DIR, name);
  sendUpdateProgress({ phase: 'downloading', received: 0, total: Number(asset?.size || 0) });
  await downloadUpdateFile(url, target, (progress) => {
    sendUpdateProgress({ phase: 'downloading', ...progress });
  });
  sendUpdateProgress({ phase: 'installing' });
  if (process.platform === 'darwin') return installOnMac(target, name);
  if (process.platform === 'win32') return installOnWindows(target, name);
  shell.showItemInFolder(target);
  return { mode: 'manual', message: '更新包已下载，请手动安装。' };
});


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
    { label: '帮助', submenu: [
      { label: '检查更新…', click: () => {
        if (mainWindow) mainWindow.webContents.send('update:open-about');
      } },
      { label: '项目主页', click: () => {
        shell.openExternal('https://github.com/lhc75/AI-report-generation');
      } },
    ] },
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
      preload: path.join(__dirname, 'preload.js'),
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
