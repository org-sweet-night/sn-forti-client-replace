# GitHub Release 自动更新

采用 [Sparkle 2.10.0](https://sparkle-project.org/) 的标准更新界面、版本比较、后台下载和 Ed25519 验签。GitHub Releases 保存完整安装包及签名 appcast；更新程序不把发行说明中的文字当成 shell 命令执行。

## 使用流程

正常打开 App（包括登录 macOS 时的开机启动）后，检查本仓库最新 Release 的 `appcast.xml`。没有新版或后台检查遇到网络错误时不打扰用户；手动“检查更新…”会显示结果。开发目录、预览、构建自检不执行在线检查。

发现新版后先提示安装将暂时断开 VPN，再显示发行说明。选择暂不更新或跳过版本时不下载安装包；选择安装后才由 Sparkle 在后台下载。未开放“以后自动下载/安装”的选项，每个版本仍需用户选择。下载、验签失败不会更换已安装文件，也不会断开 VPN。

安装阶段仍需要 macOS 管理员授权。下载和签名检查通过后，Sparkle 安装完整 `.pkg`，更新程序按顺序执行：

1. 确认当前登录账号与已有助手授权账号一致，检查完整发布输入。
2. 必要时停止当前 VPN，再调用统一事务升级器更新 App、受限助手、后台代码、钥匙串工具及连接引擎。
3. 核验托管文件、签名、助手通信及原有凭据的可读性，再运行完整 `--check`。
4. 原先有活动连接时，成功后或已安全回滚后请求恢复原连接；真实可达状态继续由 App 显示。未完成的恢复事务会保留，停止自动重连和清理。

钥匙串不兼容仍会停止并回滚，不自动改写密码或放宽权限。该问题需要用户单独授权处理。管理员取消、网络错误、签名错误、安装失败均不能算作更新成功。

## 公开更新源与信任

固定 feed 为 `https://github.com/org-sweet-night/sn-forti-client-replace/releases/latest/download/appcast.xml`。当前仓库需要由所有者设为 public；私有仓库的匿名 404 不会被解释为“已是最新版”。客户端没有内置 GitHub 令牌。

主 App 的 Info.plist 固定签名公钥和安全选项，更新程序只接受同一 GitHub 仓库 HTTPS Release 地址下的完整 `.pkg`，拒绝只替换 `.app` 的更新。包和包含发行说明的 feed 均要求 Ed25519 签名，安装包在解包前验签。没有 Developer ID 证书仍可使用该签名链；它不能代替 Apple 公证或首次安装时的 Gatekeeper 放行。

主 App 与 root 助手保留 hardened runtime 和现有 entitlement 限制。只有独立、非特权的 `SN VPN Updater.app` 为加载 Sparkle 使用 `disable-library-validation`；它没有新增 root helper 操作接口。框架所需符号链接只允许留在自带 Sparkle.framework 内部，复制、备份与回滚均保留链接和签名。

## 维护者首次设置

新增永久签名密钥属于凭据设置，需由维护者明确批准后执行：

```bash
bash scripts/prepare-sparkle.sh
python3 scripts/setup-update-signing.py --create
```

官方 `generate_keys` 将私钥保存在当前用户登录钥匙串，专用 account 为 `local.snvpn.release-signing`。脚本只把公钥写入 `app/update-config.json`，不导出私钥、不覆盖不同的已固定公钥。保管好钥匙串备份；无 Apple Developer ID 回退链时，丢失密钥不能靠随意换公钥继续更新已有客户端。

未设置永久公钥时，本地构建的在线更新明确禁用，发布打包拒绝继续。通过 `SNVPN_UPDATE_CONFIG` 指定临时配置的构建标记为测试构建，也不能发布。

## 发布一个更新

先提交代码、检查签名设置，再按 README 构建完整 ZIP。旧版客户端没有更新入口，需要先手动安装一次带此功能的版本。

```bash
python3 scripts/setup-update-signing.py --check
python3 -m unittest discover -s tests
bash scripts/build-release-runtime.sh
bash scripts/prepare-runtime.sh
bash app/build.sh
python3 scripts/release.py --package
python3 scripts/package-update.py --archive dist/SN-VPN-VERSION-macOS-arm64.zip --tag vVERSION --output dist/SN-VPN-VERSION-macOS-arm64.pkg
python3 scripts/publish-update.py --archive dist/SN-VPN-VERSION-macOS-arm64.zip --package dist/SN-VPN-VERSION-macOS-arm64.pkg --tag vVERSION --notes-file tmp/release-notes.md --output dist/signed-update-VERSION
```

将 `VERSION` 替换为同一个实际版本。`package-update.py` 同时生成 `.pkg.json`，绑定 ZIP、安装包、版本和构建号。`publish-update.py` 使用 Sparkle 官方 `sign_update` 签名包与 feed，并核对钥匙串公钥与 App 固定公钥相同。发行说明文件应与该 GitHub Release 的说明一致；appcast 嵌入该说明并共同签名。Sparkle 官方 `generate_appcast` 不支持 package 更新，因此这里按官方格式生成 feed，并使用官方签名工具。

以上命令只准备本地产物。先检视包、发行说明及签名结果；获得发布授权并推送对应 tag 后，在最后一条命令添加 `--publish` 才会创建 GitHub 草稿、上传 ZIP/校验文件/完整 pkg/签名 feed、核对 GitHub 返回的文件摘要，最后设为 latest。不要用不包含 appcast 的普通 Release 覆盖 latest，也不要替换已经发布的签名资产。

## 验收边界

离线测试和本地签名、依赖检查不等于真实自动更新验收。仍需在目标 Mac 上，从已安装且包含本功能的旧构建，使用公开 GitHub 签名 feed 验证：取消不下载、接受后后台下载、管理员授权、VPN 中断/恢复、钥匙串不兼容回滚、升级后重新打开及下次启动检查。Sparkle 官方也提示，package 更新在开发构建中需通过真实 package 安装或公证构建验证。未经这些步骤不能宣称整条线上更新链路已验收。
