#!/usr/bin/env node
/**
 * 跨平台桌面端打包脚本：构建前端、整理后端运行文件、准备内置 Python
 * （python-build-standalone），最后调用 electron-builder 生成安装包。
 *
 * 用法：
 *   node scripts/build-desktop.mjs --platform=mac
 *   node scripts/build-desktop.mjs --platform=win
 *
 * 可通过 ELECTRON_CACHE / ELECTRON_BUILDER_CACHE 指定 Electron 缓存目录；
 * 默认复用本机对应的 electron / electron-builder 缓存，避免重复下载。
 * 内置 Python 压缩包缓存在 build/cache 下，CI 可直接用 actions/cache 复用。
 */
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STAGE = path.join(ROOT, 'build', 'desktop');
const CACHE = path.join(ROOT, 'build', 'cache');
const RELEASE = path.join(ROOT, 'release');
const FRONTEND = path.join(ROOT, 'frontend');
const DESKTOP = path.join(ROOT, 'desktop');
const BACKEND = path.join(ROOT, 'backend');

// 与 build/cache 中已有的 python-build-standalone 版本保持一致。
const PY_VERSION = '3.13.18';
const PBS_RELEASE = '20261026';
const PBS_BASE = `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}`;

const args = process.argv.slice(2);
const argValue = (name) => {
  const hit = args.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.split('=').slice(1).join('=') : undefined;
};

const hostPlatform = process.platform === 'win32' ? 'win' : process.platform === 'darwin' ? 'mac' : 'linux';
const targetPlatform = argValue('platform') || hostPlatform;
const targetArch = argValue('arch') || (targetPlatform === 'mac' ? 'arm64' : 'x64');

if (targetPlatform === 'mac' && process.platform !== 'darwin') {
  fail('macOS 安装包只能在 macOS 上构建。');
}
if (targetPlatform === 'win' && process.platform !== 'win32') {
  fail('Windows 安装包需要在 Windows 上构建（内置 Python 无法跨平台安装依赖），请使用 GitHub Actions 或 Windows 机器。');
}

const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const useShell = process.platform === 'win32';
// 直接调用 npm-cli.js，避免 Windows 下 shell 拼接路径（可能含空格/中文）出错。
const npmCli = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');

function fail(message) {
  console.error(`\n[ERROR] ${message}`);
  process.exit(1);
}

function step(message) {
  console.log(`\n[...] ${message}`);
}

function run(command, commandArgs, options = {}) {
  const result = spawnSync(command, commandArgs, {
    cwd: options.cwd || ROOT,
    env: { ...process.env, ...(options.env || {}) },
    stdio: 'inherit',
    shell: options.shell ?? false,
  });
  if (result.error) {
    fail(`${command} 执行失败：${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(`${command} ${commandArgs.join(' ')} 失败，退出码 ${result.status}`);
  }
}

function runNpm(npmArgs, options = {}) {
  if (fs.existsSync(npmCli)) {
    run(process.execPath, [npmCli, ...npmArgs], options);
    return;
  }
  run(npmCmd, npmArgs, { ...options, shell: useShell });
}

function rm(target) {
  fs.rmSync(target, { recursive: true, force: true });
}

function copyDir(from, to, filter) {
  fs.cpSync(from, to, { recursive: true, filter });
}

function cacheDirs() {
  const home = os.homedir();
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local');
    return {
      electron: path.join(local, 'electron', 'Cache'),
      builder: path.join(local, 'electron-builder', 'Cache'),
    };
  }
  if (process.platform === 'darwin') {
    return {
      electron: path.join(home, 'Library', 'Caches', 'electron'),
      builder: path.join(home, 'Library', 'Caches', 'electron-builder'),
    };
  }
  return {
    electron: path.join(home, '.cache', 'electron'),
    builder: path.join(home, '.cache', 'electron-builder'),
  };
}

const caches = cacheDirs();
const buildEnv = {
  ELECTRON_CACHE: process.env.ELECTRON_CACHE || caches.electron,
  ELECTRON_BUILDER_CACHE: process.env.ELECTRON_BUILDER_CACHE || caches.builder,
  CSC_IDENTITY_AUTO_DISCOVERY: process.env.CSC_IDENTITY_AUTO_DISCOVERY || 'false',
};
// 本机构建默认走 npmmirror 镜像（GitHub 直连经常超时）；CI 环境保持官方源。
// 需要官方源时可显式设置 ELECTRON_MIRROR=""。
if (!process.env.CI) {
  buildEnv.ELECTRON_MIRROR = process.env.ELECTRON_MIRROR ?? 'https://npmmirror.com/mirrors/electron/';
  buildEnv.ELECTRON_BUILDER_BINARIES_MIRROR = process.env.ELECTRON_BUILDER_BINARIES_MIRROR
    ?? 'https://npmmirror.com/mirrors/electron-builder-binaries/';
}

async function download(url, destination) {
  console.log(`    下载 ${url}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const response = await fetch(url, {
    redirect: 'follow',
    headers: { 'User-Agent': 'ai-report-build-script' },
  });
  if (!response.ok) {
    fail(`下载失败（HTTP ${response.status}）：${url}`);
  }
  const buffer = Buffer.from(await response.arrayBuffer());
  const partial = `${destination}.part`;
  fs.writeFileSync(partial, buffer);
  fs.renameSync(partial, destination);
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function pythonTriple() {
  const key = `${targetPlatform === 'mac' ? 'darwin' : targetPlatform === 'win' ? 'win32' : 'linux'}-${targetArch}`;
  const map = {
    'darwin-arm64': 'aarch64-apple-darwin',
    'darwin-x64': 'x86_64-apple-darwin',
    'win32-x64': 'x86_64-pc-windows-msvc',
    'linux-x64': 'x86_64-unknown-linux-gnu',
    'linux-arm64': 'aarch64-unknown-linux-gnu',
  };
  const triple = map[key];
  if (!triple) fail(`暂不支持的内置 Python 目标：${key}`);
  return triple;
}

async function preparePython() {
  const triple = pythonTriple();
  const asset = `cpython-${PY_VERSION}+${PBS_RELEASE}-${triple}-install_only.tar.gz`;
  const archive = path.join(CACHE, asset);
  const pythonDir = path.join(STAGE, 'python');
  const pythonExe = targetPlatform === 'win'
    ? path.join(pythonDir, 'python.exe')
    : path.join(pythonDir, 'bin', 'python3');
  const marker = path.join(pythonDir, '.pbs-version');

  if (!fs.existsSync(archive)) {
    await download(`${PBS_BASE}/${asset}`, archive);
  } else {
    step(`使用已缓存的内置 Python：${asset}`);
  }

  const markerOk = fs.existsSync(marker) && fs.readFileSync(marker, 'utf8').trim() === asset;
  if (!fs.existsSync(pythonExe) || !markerOk) {
    step('解压内置 Python 运行时');
    rm(pythonDir);
    fs.mkdirSync(STAGE, { recursive: true });
    run('tar', ['-xzf', archive, '-C', STAGE]);
    if (!fs.existsSync(pythonExe)) {
      fail(`内置 Python 解压后未找到可执行文件：${pythonExe}`);
    }
    fs.writeFileSync(marker, asset + '\n');
  }

  step('安装后端依赖到内置 Python');
  // 用 requirements 的哈希做标记，依赖未变化时跳过重复安装。
  const requirements = path.join(BACKEND, 'requirements.txt');
  const requirementsMarker = path.join(pythonDir, '.requirements-sha256');
  const requirementsHash = sha256(requirements);
  const installed = fs.existsSync(requirementsMarker)
    && fs.readFileSync(requirementsMarker, 'utf8').trim() === requirementsHash;
  if (!installed) {
    run(pythonExe, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', requirements], {
      env: { PYTHONUTF8: '1' },
    });
    fs.writeFileSync(requirementsMarker, requirementsHash + '\n');
  } else {
    console.log('    依赖未变化，跳过 pip install');
  }

  run(pythonExe, ['-c', 'import fastapi, uvicorn, sqlalchemy; print("内置 Python 依赖检查通过")']);
}

function stageFiles() {
  step('整理后端运行文件');
  const backendStage = path.join(STAGE, 'backend');
  rm(backendStage);
  fs.mkdirSync(backendStage, { recursive: true });
  copyDir(path.join(BACKEND, 'app'), path.join(backendStage, 'app'), (source) => {
    return !source.split(path.sep).includes('__pycache__');
  });
  copyDir(path.join(BACKEND, 'certs'), path.join(backendStage, 'certs'));
  fs.copyFileSync(path.join(BACKEND, 'requirements.txt'), path.join(backendStage, 'requirements.txt'));

  step('整理前端构建产物');
  const distStage = path.join(STAGE, 'frontend', 'dist');
  rm(path.join(STAGE, 'frontend'));
  fs.mkdirSync(path.dirname(distStage), { recursive: true });
  copyDir(path.join(FRONTEND, 'dist'), distStage);
}

function buildFrontend() {
  if (!fs.existsSync(path.join(FRONTEND, 'node_modules'))) {
    step('安装前端依赖');
    runNpm(['ci'], { cwd: FRONTEND });
  }
  step('构建前端');
  runNpm(['run', 'build'], { cwd: FRONTEND });
}

function installDesktopDeps() {
  const ready = fs.existsSync(path.join(DESKTOP, 'node_modules', 'electron', 'package.json'))
    && fs.existsSync(path.join(DESKTOP, 'node_modules', 'electron-builder', 'package.json'));
  if (ready) {
    console.log('\n[...] 桌面端依赖已存在，跳过 npm ci');
    return;
  }
  step('安装桌面端依赖');
  runNpm(['ci'], { cwd: DESKTOP, env: buildEnv });
}

function cleanOldArtifacts() {
  if (!fs.existsSync(RELEASE)) return;
  const patterns = targetPlatform === 'mac'
    ? [/\.dmg$/, /\.zip$/, /\.blockmap$/]
    : [/\.exe$/, /\.blockmap$/];
  for (const name of fs.readdirSync(RELEASE)) {
    if (patterns.some((pattern) => pattern.test(name))) {
      rm(path.join(RELEASE, name));
    }
  }
}

async function main() {
  console.log('============================================');
  console.log(` 构建目标：${targetPlatform}-${targetArch}`);
  console.log(` 项目目录：${ROOT}`);
  console.log(` Electron 缓存：${buildEnv.ELECTRON_CACHE}`);
  console.log(` 内置 Python：${PY_VERSION} (${pythonTriple()})`);
  console.log('============================================');

  buildFrontend();
  stageFiles();
  await preparePython();
  installDesktopDeps();
  cleanOldArtifacts();

  step(targetPlatform === 'mac' ? '生成 macOS 安装包（DMG + ZIP）' : '生成 Windows 安装包与便携版');
  runNpm(['run', targetPlatform === 'mac' ? 'dist:mac' : 'dist:win'], {
    cwd: DESKTOP,
    env: buildEnv,
  });

  const artifacts = fs.existsSync(RELEASE)
    ? fs.readdirSync(RELEASE).filter((name) => /\.(dmg|zip|exe)$/.test(name))
    : [];
  if (!artifacts.length) fail('打包完成但未在 release/ 下找到安装包。');

  console.log('\n[OK] 打包完成，产物：');
  for (const name of artifacts) {
    const full = path.join(RELEASE, name);
    console.log(`     ${path.join('release', name)}  (${(fs.statSync(full).size / 1024 / 1024).toFixed(1)} MB)`);
  }
}

main().catch((error) => fail(error.stack || String(error)));
