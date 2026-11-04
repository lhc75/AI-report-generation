# 模型服务补链证书

2026-10-10 检查 `cdn.juapi.net` 时，服务器仅发送域名证书，未发送其签发者
Let’s Encrypt YE2 和上级交叉签名 CA。域名证书有效期为 2026-08-19 至
2026-11-17，SAN 包含 `cdn.juapi.net`；错误原因是服务端证书链不完整。

`letsencrypt-ye2-chain.pem` 依次包含以下公开 CA，不包含网站证书或私钥：

| CA | 来源 | SHA-256 指纹 | 到期日（UTC） |
| --- | --- | --- | --- |
| YE2 | https://letsencrypt.org/certs/gen-y/int-ye2.pem | `97658DE8C68DFA98ACE1E5028A63D54A1AAE911B3E21471076C6850CD08CBAB4` | 2028-09-02 |
| Root YE（ISRG Root X2 交叉签名） | https://letsencrypt.org/certs/gen-y/root-ye-by-x2.pem | `0FC0901CCA2BAE9E9FDBB02D50D02F1094F7B36672086991B9E897626DC485F0` | 2032-09-02 |
| ISRG Root X2 | 本机 certifi 2026.7.22 的 Mozilla 信任库；官方：https://letsencrypt.org/certs/isrg-root-x2.pem | `69729B8E15A86EFC177A57AFB7171DFC64ADD28C2FCA8CF1507E34453CCB1470` | 2040-09-17 |

保存前使用 cryptography 逐级验证签名、CA 属性和有效期，并使用 OpenSSL 对
YE2 到既有 Mozilla 信任根的路径独立验证。原生 macOS 验证器会将加载的 CA
作为额外信任锚，因此更新时同样需要验证来源和完整信任路径。

项目根目录 `.env` 配置：

```dotenv
LLM_CA_BUNDLE=backend/certs/letsencrypt-ye2-chain.pem
```

修改后重启后端。该设置仅影响模型 HTTP 客户端，不修改系统钥匙串，始终保留
HTTPS 证书和域名校验。服务端应使用完整证书链，例如 Nginx 配置 `fullchain.pem`；
服务端修复并确认验证成功后可清空 `LLM_CA_BUNDLE`。供应商换用其他 CA 时，
需重新检查其完整链并核实是否仍需本地补链。
