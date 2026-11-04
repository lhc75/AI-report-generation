'use strict';

const path = require('path');
const base = require('./electron-builder.base');

module.exports = {
  ...base,
  win: {
    target: [
      { target: 'nsis', arch: ['x64'] },
      { target: 'portable', arch: ['x64'] },
    ],
    icon: path.join(__dirname, 'icon.png'),
  },
  nsis: {
    artifactName: 'AIReport-Setup-${version}.${ext}',
    oneClick: false,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
  },
  portable: {
    artifactName: 'AIReport-Portable-${version}.${ext}',
  },
};
