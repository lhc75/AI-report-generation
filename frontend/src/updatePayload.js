/**
 * IPC 载荷序列化：Electron 的结构化克隆无法复制 Vue 的响应式 Proxy，
 * 更新安装参数统一转成 JSON 字符串后再交给主进程。
 */
export function serializeInstallPayload(asset, assets = []) {
  return JSON.stringify({
    asset: asset || null,
    assets: Array.isArray(assets) ? assets : [],
  })
}
