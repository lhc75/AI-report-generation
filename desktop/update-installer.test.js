'use strict';

const test = require('node:test');
const assert = require('node:assert');
const {
  psQuote,
  buildWindowsInstallScript,
  encodePowerShell,
} = require('./update-installer');

test('psQuote escapes single quotes', () => {
  assert.strictEqual(psQuote("C:\\Users\\o'brien\\a.exe"), "'C:\\Users\\o''brien\\a.exe'");
});

test('setup script waits, installs silently and relaunches', () => {
  const script = buildWindowsInstallScript({
    pid: 1234,
    installer: 'C:\\Users\\张三\\AppData\\Roaming\\AI报告生成\\updates\\AIReport-Setup-1.1.2.exe',
    relaunch: 'C:\\Users\\张三\\AppData\\Local\\Programs\\AI报告生成\\AI报告生成.exe',
    downloaded: 'C:\\Users\\张三\\AppData\\Roaming\\AI报告生成\\updates\\AIReport-Setup-1.1.2.exe',
    portableFile: '',
  });
  assert.match(script, /Wait-Process -Id 1234/);
  assert.match(script, /-ArgumentList '\/S' -Wait/);
  assert.match(script, /Test-Path/);
  assert.match(script, /AI报告生成\.exe/);
  assert.doesNotMatch(script, /Copy-Item/);
});

test('portable script replaces the current exe instead of running an installer', () => {
  const script = buildWindowsInstallScript({
    pid: 99,
    installer: 'C:\\Temp\\AIReport-Portable-1.1.2.exe',
    relaunch: 'D:\\Tools\\AI报告生成.exe',
    downloaded: 'C:\\Temp\\AIReport-Portable-1.1.2.exe',
    portableFile: 'D:\\Tools\\AI报告生成.exe',
  });
  assert.match(script, /Copy-Item/);
  assert.match(script, /Start-Process -FilePath 'D:\\Tools\\AI报告生成\.exe'/);
  assert.doesNotMatch(script, /AIReport-Portable-1\.1\.2\.exe'.*ArgumentList/);
});

test('encoded command round-trips as UTF-16LE', () => {
  const script = "Write-Output '中文路径'\r\n";
  const decoded = Buffer.from(encodePowerShell(script), 'base64').toString('utf16le');
  assert.strictEqual(decoded, script);
});
