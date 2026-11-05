'use strict';

const path = require('path');

const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'build', 'desktop');

module.exports = {
  appId: 'com.lhc75.aireport',
  productName: 'AI报告生成',
  directories: {
    output: path.join(ROOT, 'release'),
  },
  asar: true,
  // 桌面端没有原生 Node 模块，跳过 rebuild 可显著缩短打包时间。
  npmRebuild: false,
  files: [
    'main.js',
    'preload.js',
    'package.json',
    'icon.png',
    '!**/*.test.js',
  ],
  extraResources: [
    { from: path.join(STAGE, 'backend'), to: 'backend' },
    { from: path.join(STAGE, 'frontend', 'dist'), to: 'frontend/dist' },
    { from: path.join(STAGE, 'python'), to: 'python' },
  ],
};
