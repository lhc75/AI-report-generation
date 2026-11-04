'use strict';

const path = require('path');
const base = require('./electron-builder.base');

module.exports = {
  ...base,
  mac: {
    artifactName: 'AIReport-${version}-arm64.${ext}',
    target: [
      { target: 'dmg', arch: ['arm64'] },
      { target: 'zip', arch: ['arm64'] },
    ],
    icon: path.join(__dirname, 'icon.icns'),
    category: 'public.app-category.productivity',
    // 未配置开发者证书时直接产出未签名安装包，避免打包流程被签名探测卡住。
    identity: null,
  },
};
