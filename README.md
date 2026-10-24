# FortiReplace

**macOS 上的 FortiClient VPN 替代客户端。**

FortiReplace 使用自带的 OpenConnect 连接 Fortinet / FortiGate SSL VPN，提供菜单栏操作、多连接配置、自动重连和独立的启动选项，适合通过用户名和密码接入办公网络的场景。仓库名称为 [`sn-forti-client-replace`](https://github.com/org-sweet-night/sn-forti-client-replace)。

本项目替代的是 **FortiClient 的基础 SSL VPN 连接功能**。FortiClient 的 IPsec、企业身份集成、EMS、ZTNA 和终端防护不在当前实现范围内；不同版本和授权下的官方产品能力见 [Fortinet 产品说明](https://www.fortinet.com/products/endpoint-security/forticlient)。本项目为独立的非官方客户端。

> 当前构建产物仍名为 `SN VPN.app`，窗口和菜单也保留 SN VPN 名称。本文统一使用 FortiReplace 作为项目名；下方命令中的实际 App 名称和安装路径仍按现有代码填写。

## 已实现的功能

| 功能 | 当前实现与边界 |
| --- | --- |
| Fortinet SSL VPN 连接 | 通过 OpenConnect 的 `--protocol=fortinet` 建立隧道，不调用 FortiClient 建立连接。 |
| 用户名和密码认证 | 首次连接时验证账号密码，后续使用保存的凭据。当前使用非交互式连接方式。 |
| 网关证书确认 | 首次连接显示证书 SHA-256 指纹，人工核对后保存公钥固定值；公钥变更或证书校验失败时停止连接。 |
| 密码安全保存 | 每条已验证配置的密码分别保存在 macOS 系统钥匙串中，不写入连接配置 JSON。 |
| 多连接配置 | 新建、编辑、删除和选择连接，配置包含名称、网关、端口、用户名；同一时间只运行一条隧道。 |
| 连接切换 | 可以切换到另一条配置；配置或认证失败时尝试恢复原配置与凭据，恢复失败会明确报错。 |
| 路由和 DNS 配置 | 通过随附的 `vpnc-script` 应用、清理网关下发的网络配置。具体分流结果取决于网关策略，没有图形化路由或 DNS 策略编辑器。 |
| 网络中断后的恢复 | OpenConnect 尝试会话重连；网络错误或会话过期退出后，后台服务按节流策略重新启动。认证、证书、额外认证或配置错误会停止自动重试。 |
| 菜单栏控制 | 菜单栏显示连接状态，支持打开窗口、连接和断开；主窗口提供重新验证入口。App 不占用 Dock。 |
| 开机启动 | 登录 macOS 时打开 App，由系统登录项管理；与自动连接分别保存和控制。 |
| 自动连接 | 打开或重新打开 App 时连接所选配置；已有连接运行时保持原连接。 |
| 日常启停免重复管理员密码 | 首次安装或更新受限助手需要管理员授权；之后由助手执行固定 VPN 操作。助手按需启动，空闲后退出。 |
| 闲置睡眠保活 | VPN 运行期间阻止系统因闲置进入睡眠，仍允许锁屏和熄屏。合盖和手动睡眠不在保活范围内，电池消耗可能增加。 |

### 连接状态如何判断

金色锁表示隧道已报告连接成功，且探测到了经 VPN 接口路由、TCP 53 端口可达的私网 DNS 地址。灰色锁表示尚未满足该条件，悬停可以查看具体状态。

这项检查只覆盖上述探测条件，不代表所有内网服务、域名解析或业务系统均已验收。没有私网 DNS、禁用 DNS TCP 端口或采用其他 DNS 架构的网络，可能出现隧道已建立但界面仍提示连接异常的情况。

## 支持范围

目前仅提供 Apple Silicon Mac 上的 Fortinet SSL VPN，使用用户名和密码登录，一次运行一条隧道。不支持 IPsec、MFA/OTP、SAML、客户端证书、EMS/ZTNA 和终端防护，也没有 Windows、Linux 或 Intel Mac 发布包。网关输入支持域名和 IPv4，完整 IPv6 尚未验收。

## 是否还需要 FortiClient

**不需要安装或运行 FortiClient。** 运行环境、连接引擎、密码存储和后台控制均由本项目提供。

首次打开且没有本项目配置时，App 提供两种方式：

- **导入现有配置**：读取本机 FortiClient 的旧版 `vpn.plist` 中可识别的 SSL VPN 连接信息，经用户确认后独立保存；不导入密码。
- **手动创建**：填写连接名称、VPN 网关、端口和用户名。不需要 FortiClient 或它的配置文件。

无法读取、没有可用的 FortiClient 配置时可以直接手动创建。已有本项目配置会保留；损坏的配置不会被自动覆盖。保存或导入只创建连接列表，首次点击连接时才确认网关证书并请求 VPN 密码。

历史配置仍保留读取旧 FortiClient 配置的兼容路径。使用旧版运行环境的用户，应先在 App 中导入或重新验证，确认独立保存后再移除旧配置。

## 开机启动与自动连接

两个选项默认关闭，含义独立：

| 开机启动 | 自动连接 | 行为 |
| --- | --- | --- |
| 关闭 | 关闭 | 登录后不自动打开；手动打开 App 后按需连接。 |
| 开启 | 关闭 | 登录后打开 App，等待手动连接。 |
| 关闭 | 开启 | 登录后不自动打开；手动打开 App 时自动连接。 |
| 开启 | 开启 | 登录后打开 App，并尝试连接所选 VPN。 |

已有连接运行时，打开 App 不会自动切换到另一条配置。手动断开不改变这两个选项，也不会因为定时刷新立即重连；下次打开或重新打开 App 时再应用自动连接选项。关闭窗口或退出 App 不会主动断开已经运行的 VPN。

## 环境与安装

### 当前支持条件

- 当前 App 构建要求 **macOS 27.0 或以上**。
- 随附 OpenConnect、钥匙串工具等运行时二进制为 **Apple Silicon / arm64**。
- 当前构建使用本地 ad-hoc 签名，尚无 Developer ID 签名和公证的正式发行包，跨机器安装未完成完整验收。
- 运行需要可用的 `/usr/bin/python3`；部分机器需要先安装 Apple Command Line Tools 才能使用该入口。从源码构建还需要 Swift 编译工具和 macOS SDK。
- 需要可访问的 Fortinet SSL VPN 网关，以及允许用户名密码认证的账号。
- 首次安装、更新助手或更换授权本机账号需要 macOS 管理员权限。

### 全新安装（无需 FortiClient）

先安装 Apple Command Line Tools、Homebrew，以及 OpenConnect：

```bash
xcode-select --install
brew install openconnect
```

已有这些工具时跳过对应步骤。然后以普通用户执行：

```bash
git clone https://github.com/org-sweet-night/sn-forti-client-replace.git
cd sn-forti-client-replace
bash install.sh
```

安装脚本从本机 Homebrew 准备 OpenConnect 与动态库、从源码编译钥匙串工具和 App，然后请求管理员授权，安装到 `/Applications/SN VPN.app` 和 `/Library/Application Support/Local VPN`，并配置受限助手。

**安装过程不连接 VPN，也不要求 FortiClient 配置或 VPN 密码。** 完成后打开 App，按首次使用提示导入或手动创建连接，再按需连接。开机启动与自动连接默认关闭。

脚本检测到已有 App、运行环境或启动项时会停止，保留原状态。若安装中途失败，请先检查已完成的步骤，再按下方维护入口处理，避免覆盖现有配置。

全新安装若在“初始化运行环境”阶段失败，初始化器会清理自己的暂存目录；若已完成初始化、在复制 App 或安装助手时失败，运行环境仍保持未连接状态。确认这是本次未完成的安装后，可用下面的分步命令完成 App 复制与助手安装，无需删除运行环境或重新创建配置。

### 仅构建与维护入口

```bash
bash prepare-runtime.sh
bash app/build.sh
```

产物为 `app/dist/SN VPN.app`，其中包含签名后的受限助手。构建会执行 App 与助手自检，并校验代码签名。生成的二进制、运行时和本机恢复资料不会提交到仓库。

干净环境可分步安装运行时和助手：

```bash
sudo /usr/bin/python3 -I install.py --initialize
# 将本次构建的 SN VPN.app 放到 /Applications 后执行：
sudo /usr/bin/ditto "app/dist/SN VPN.app" "/Applications/SN VPN.app"
sudo /usr/bin/python3 -I install.py --install-helper "/Applications/SN VPN.app" "$(id -u)"
```

`--initialize` 创建运行环境、控制锁和待连接的服务定义，不读取 FortiClient 配置、不创建虚假的账号信息，也不启动隧道。实际连接信息在 App 保存，首次验证成功后才写入系统运行配置并保存钥匙串凭据。

已有安装升级：先退出旧版 App，运行 `sudo /usr/bin/python3 -I install.py --upgrade` 更新控制代码、迁移旧启动项，再用本次构建替换 App，并执行上面的助手安装命令；安装验证通过后移除旧版副本。该维护入口保留现有配置和钥匙串凭据，不主动重启当前隧道。

App 与助手相互校验指定版本的代码签名，助手还校验调用账号。**更新 App 时必须同时更新助手**，版本不匹配时会拒绝操作。助手不提供任意命令执行接口。

首次连接、修改连接目标或执行“重新验证并连接”时，仍可能需要证书确认和 VPN 账号密码；这与反复输入 Mac 管理员密码是两回事。

旧版命令 `sudo /usr/bin/python3 -I install.py '<现有 FortiClient SSL VPN 配置名>'` 保留兼容，仅用于确实存在该配置的全新环境；它会验证账号并启动 VPN。通常应使用上面的独立安装流程。

## 版本与构建

菜单栏菜单底部、“退出”上方显示独立版本信息，上下各有分隔线，例如 `版本：1.0.84-abcdef11`。

- 主版本在 `app/version.py` 中从 `1` 开始，仅在重大变化（例如新增 Windows、Linux 支持）时手动升级；开启新主版本时，将 `app/build-number` 重置为 `0`。
- `app/build-number` 保存当前主版本的累计构建号，每次构建先加 1；失败的构建也占用该号，不回退复用。显示格式为 `主版本.构建号整除100.构建号余100-Git短哈希`。
- build 84 显示 `1.0.84`，build 100 显示 `1.1.0`，build 184 显示 `1.1.84`，build 200 显示 `1.2.0`。
- 哈希为构建源码的 Git 提交前 8 位。发布构建应从已提交的源码开始，构建号文件随发布提交，构建产物不入库。源码未提交时追加 `-dirty` 标记。
- 原固定构建号为 5，新规则首次构建为 `1.0.6`。构建号不以 Git 提交数量计算。

## 验证

离线检查使用模拟服务和凭据，不会实际连接或断开 VPN：

```bash
/usr/bin/python3 -m unittest discover
```

`bash app/build.sh` 还执行 App、菜单和助手自检及代码签名校验。安装后可执行只读助手连通检查：

```bash
"/Applications/SN VPN.app/Contents/MacOS/SN VPN" --helper-check
```

这些检查不代表新设备安装、重启后的启动链路、所有网关、路由、DNS 或业务服务已验收。菜单金锁的探测范围见上文。

## 第三方组件与分发

连接引擎来自 [OpenConnect](https://www.infradead.org/openconnect/)。新安装使用本机 Homebrew 提供的版本，准备脚本会校验其支持 Fortinet 协议。仓库只保存本项目源码和 `vpnc-script` 源码，不分发本机复制的 OpenConnect、动态库或钥匙串二进制。

第三方组件及许可证说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。项目自身尚未指定整体开源许可证；第三方组件继续适用各自许可证。

本机恢复副本、参考资料、运行日志、连接配置与凭据已列入忽略规则。文档和示例使用占位地址，个人账号、真实网关、证书和业务数据应由每位使用者自行配置。
