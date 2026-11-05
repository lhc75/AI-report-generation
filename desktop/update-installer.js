'use strict';

/**
 * Windows 更新安装脚本构造。
 *
 * 生成 PowerShell 脚本交回主进程，用 -EncodedCommand（UTF-16LE Base64）执行：
 * 中文用户目录（%APPDATA%\AI报告生成）在 Windows PowerShell 5.1 里按 ANSI
 * 解析无 BOM 的 UTF-8 脚本会乱码，编码后传参可彻底避开该问题。
 */

function psQuote(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function buildWindowsInstallScript({ pid, installer, relaunch, portableFile, downloaded }) {
  const lines = [
    `Wait-Process -Id ${Number(pid)} -ErrorAction SilentlyContinue`,
    'Start-Sleep -Milliseconds 800',
  ];
  if (portableFile) {
    lines.push(
      `Copy-Item -LiteralPath ${psQuote(downloaded)} -Destination ${psQuote(portableFile)} -Force`,
      `Start-Process -FilePath ${psQuote(portableFile)}`,
    );
  } else {
    lines.push(
      `Start-Process -FilePath ${psQuote(installer)} -ArgumentList '/S' -Wait`,
      'Start-Sleep -Seconds 2',
      `if (Test-Path -LiteralPath ${psQuote(relaunch)}) { Start-Process -FilePath ${psQuote(relaunch)} }`,
    );
  }
  return lines.join('\r\n') + '\r\n';
}

function encodePowerShell(script) {
  return Buffer.from(String(script), 'utf16le').toString('base64');
}

module.exports = { psQuote, buildWindowsInstallScript, encodePowerShell };
