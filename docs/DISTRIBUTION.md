# SN VPN 下载包

适用于 **macOS 15 或以上、Apple Silicon（M1/M2/M3/M4 及后续芯片）**。Intel Mac 不支持。

## 安装

1. 从本项目 GitHub Releases 下载 ZIP 和 `.sha256`，在下载目录执行 `shasum -a 256 -c SN-VPN-*.zip.sha256` 核对文件。
2. 解压 ZIP，保留整个文件夹。首次使用需要 Apple Command Line Tools 提供的 `/usr/bin/python3`。若尚未安装，在终端执行 `xcode-select --install`，完成后再继续。不需要 Homebrew、FortiClient 或自行编译。
3. 双击 `Install.command`，按提示输入 Mac 管理员密码。也可以在终端进入解压目录后执行 `bash scripts/install.sh`。
4. 打开 SN VPN，手动创建连接或导入本机 FortiClient 的连接信息。首次连接时核对网关证书并输入 VPN 密码。安装本身不会连接 VPN。

本包使用 ad-hoc 临时签名，**没有 Apple Developer ID 签名或公证**。首次运行安装入口或 App 时，macOS 可能阻止打开。确认下载来源与校验值后，按 [Apple 官方说明](https://support.apple.com/102445) 在“系统设置 → 隐私与安全性”中找到刚才被阻止的项目，选择“仍要打开”。管理员管理的电脑可能不允许此操作，请联系 IT。无需全局关闭 Gatekeeper、SIP 或其他系统保护。

签名校验和 SHA-256 用于检测文件变化，不代表 Apple 已验证开发者身份。通过浏览器下载后的隔离提示、其他电脑的安装与真实 VPN 连通性仍需目标电脑验收。

## 已有安装

先主动断开 VPN 并退出 SN VPN，再双击 `Update.command`。升级会保留连接设置、密码和启动偏好，并检查已有凭据是否仍可读取；失败时回滚。不要只拖动替换 App，也不要对已有安装运行 Install。

升级完成后，在解压目录运行 `bash scripts/update.sh --check` 核验。若临时签名导致旧钥匙串授权不兼容，升级会失败并回滚；重新授权需要用户明确同意。中断的升级使用 `bash scripts/update.sh --recover`，保留恢复资料直到恢复成功。

安装位置为 `/Applications/SN VPN.app` 和 `/Library/Application Support/Local VPN`。首次安装需要管理员权限，安装后日常连接通过受限助手操作。开机启动和自动连接默认关闭。

若首次安装在“复制 App”或“安装受限助手”阶段中止，已初始化的文件会保留，Install 会拒绝再次覆盖。这时不要删除运行环境、改跑 Update 或反复安装。保留终端错误信息，由维护者先检查该次安装留下的文件；确认是本次安装且运行环境完整后，在解压目录补齐 App 复制和助手安装：

```bash
sudo /usr/bin/ditto 'dist/SN VPN.app' '/Applications/SN VPN.app'
sudo /usr/bin/python3 -I scripts/install.py --install-helper '/Applications/SN VPN.app' "$(id -u)"
```

这些是检查后的维修步骤，不能用于覆盖他人的已有安装。完成后打开 App，确认助手可以读取连接状态，再创建或导入配置。此过程不需要删除或重建连接配置；`update.sh --check` 的完整性清单由成功的完整升级生成，不能用来认证这次补齐安装。

## 范围与验证

支持 Fortinet SSL VPN 用户名密码认证；不支持 IPsec、MFA/OTP、SAML、EMS/ZTNA 或终端防护。

最低系统版本经过编译目标和所有随附 Mach-O 依赖检查；构建机上的 App/助手自检及引擎 Fortinet 协议检查通过后才打包。尚未在 macOS 15 的全新机器上完成安装、Gatekeeper 提示、钥匙串、登录项及真实连接验收。

包内 `RELEASE.json` 记录版本、源码提交及文件 SHA-256。可执行 `/usr/bin/python3 scripts/release.py --verify .` 检查解压内容（不安装、不连接 VPN）。该校验不能代替下载来源认证。

`sources/` 提供本包 OpenConnect 与 OpenSSL 的对应完整源码，`runtime-licenses/` 和 `LICENSES/` 提供许可证；构建方法见源码仓库的 `scripts/build-release-runtime.sh`。第三方源码及动态库可按相应许可证重新构建或替换；替换后需重新签名并按源码维护流程安装，原包的完整性校验将不再通过。
