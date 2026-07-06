# Yeh Bypass (Gateway)

自用一键旁路由交互式菜单脚本，提供从 DNS 缓存、域名分流、远程解析、代理接入，到在外回家入口的一整套网络方案。

核心容器包括 AdGuardHome、MosDNS、Mihomo、ddns-go：AdGuardHome 负责 DNS 缓存；MosDNS 负责域名分流，内部域名返回真实 IP，外部域名返回 FakeIP；代理由 Mihomo 或 Surge 承载。

在外回家方案以 ddns-go + Mihomo 作为入口，统一处理 IPv4 / IPv6 访问与入站流量。

支持 IPv6，已在群晖 7.3+（可能需要补全欠缺命令）、飞牛 1.0+、Armbian（Linux 6.1+）上测试通过。


## ✨ 功能特性
- 交互式选择网卡并确认 IP / 网关 / 子网配置
- 创建 Docker macvlan 网络
- 创建宿主机 `macvlan-bridge` 接口用于互通
- 写入并启用 Systemd 服务，确保开机自启
- 预定义多个容器 IP：librespeed（.111） AdGuardhome（.114）、MosDNS（.119）、Mihomo（.120）


## ⚙️ 脚本菜单说明
| 序号 | 功能描述                        |
|----|-----------------------------|
| 0  | 显示菜单                        |
| 1  | 显示操作系统信息                    |
| 2  | 显示网卡信息                      |
| 3  | 显示磁盘信息                      |
| 4  | 显示 Docker 信息                |
| 5  | 格式化磁盘并挂载                    |
| 7  | 安装 Docker                   |
| 8  | 创建macvlan（包括ipv4+ipv6）      |
| 9  | 清理 macvlan                  |
| 10 | 安装 Portainer                |
| 11 | 安装 LibreSpeed               |
| 14 | 安装 AdGuardHome              |
| 19 | 安装 mosdns                   |
| 20 | 安装 mihomo                   |
| 21 | 安装 ddnsgo                   |
| 22 | 安装 lucky                    |
| 70 | 迁移docker目录                  |
| 71 | 优化docker日志                  |
| 72 | 优化journald日志                |
| 90 | 创建macvlan bridge            |
| 91 | 清理macvlan bridge            |
| 96 | 安装 Dockcheck 自动更新           |
| 97 | 清理 Dockcheck 自动更新           |
| 98 | 立即执行 Dockcheck 检查/更新一次      |
| 100 / del | 删除 `yehbp` 命令          |
| 99 | 退出脚本                        |

## 🚀 使用方法

### 1. 安装命令

```bash
curl -fsSL https://raw.githubusercontent.com/perryyeh/yehbp/refs/heads/main/install.sh | sudo bash
```

安装后直接运行：

```bash
sudo yehbp
```

每次运行 `yehbp` 时会检查仓库版本。如果发现新版本，会提示是否升级：

```text
是否现在升级？[y/N]:
```

输入 `y` 才会升级；默认回车或输入 `n` 都不会升级。版本检查只读取仓库里的 `VERSION` 文件；只有远程版本严格高于当前版本才会提示升级，确认升级后才会下载最新脚本并做语法检查，然后覆盖当前 `yehbp` 命令。

### 2. 删除 yehbp 命令

可以直接运行删除命令：

```bash
sudo yehbp del
```

也可以进入交互菜单后输入：

```text
100
```

或：

```text
del
```

脚本会二次确认后删除 `/usr/local/bin/yehbp`。

如需手动删除：

```bash
sudo rm -f /usr/local/bin/yehbp
```

如需同时清理旧版本遗留的历史备份：

```bash
sudo rm -f /usr/local/bin/yehbp.bak-*
```

这只会删除 `yehbp` 命令和历史备份，不会删除已安装的 Docker 容器、配置目录、macvlan、systemd 服务等。

### 3. 安装步骤

1. 确认 Docker 容器安装目录；如需新硬盘，先完成格式化和挂载。
2. 确认 Docker 已安装；群晖和飞牛通常已有 Docker，可跳过安装。
3. 群晖的网卡建议先开启 Open vSwitch。
4. 确认专用 IP 段给 macvlan 使用：IPv4 建议使用新的 `/24` 段，IPv6 ULA 建议使用 `/64` 段，并在路由器 DHCP 中避开该地址段。
5. 选择网卡创建 macvlan；群晖建议选择 `ovs` 开头网卡。
6. 没有 Surge / OpenWrt 作为代理时，可安装 Mihomo 替代；Mihomo 需开启 TUN 模式并配置好上游代理。
7. 安装 MosDNS；选择 Surge 作为上游时 DNS 写 `198.18.0.2`，选择 Mihomo 作为上游时 DNS 写 Mihomo 的 IP。
8. 安装 AdGuardHome，并使用 MosDNS 作为上游 DNS。
9. 最后创建 macvlan bridge，解决宿主机和容器之间的互通。
10. 配置 FakeIP 路由：
    - fake IPv4：Surge 和 Mihomo 都是在路由器添加静态路由：`198.18.0.0/15` 下一跳到 Surge / Mihomo 的局域网 IP。
    - fake IPv6：
      - Mihomo：需要局域网开启 ULA IPv6，在路由器添加静态路由：`fd00:6152:0:9::/64` 下一跳到 Mihomo 所在的局域网 IPv6。
      - Surge：需要局域网开启 ULA IPv6，然后参考下面的「5. MosDNS 在 Surge 下使用 fake IPv6」。
11. 在路由器把 AdGuardHome 的 IP 设置为局域网 DNS。

### 4. Docker 镜像自动更新

菜单 `96` 可安装 Dockcheck 自动更新组件：

- 选择 `dockerapps` 目录后，组件会安装到 `<dockerapps>/_auto_update`。
- Dockcheck 脚本优先从上游 raw 脚本地址下载：`https://raw.githubusercontent.com/mag37/dockcheck/main/dockcheck.sh`；如该 raw 地址下载失败，则使用 yehbp 仓库内置副本。
- 可设置新镜像发布后延迟 N 天再更新。
- 可选择更新后自动清理 dangling images。
- 可选择是否启用每日 systemd timer。

菜单 `97` 可清理 Dockcheck 自动更新：

- 停用并移除 systemd service/timer。
- 可选择是否删除 `_auto_update` 目录。

菜单 `98` 可立即执行一次：

- 只检查，不更新。
- 检查并更新一次。

需要固定容器 MAC 的服务，应在 compose 网络配置中显式写 `mac_address`；Dockcheck 更新后会检查 compose 期望 MAC 与实际容器 MAC 是否一致。

### 5. MosDNS 在 Surge 下使用 fake IPv6

mosdns 选择 Surge 作为上游，并开启 fake IPv6 解析前，需要先确认 Surge 的 fake IPv6 链路完整可用。仅 DNS 能返回 fake IPv6 不够，客户端还必须能把该 IPv6 段路由到运行 Surge 的 Mac，并由 Surge VIF 承载。

Surge / Mac 侧：
1. Surge 配置必须设置 `ipv6-vif = always`，不能用 `auto`，否则 IPv6 VIF 可能不会按预期拉起。
2. 开启 IPv6 转发：`net.inet6.ip6.forwarding=1`。
3. 在主网卡发送 RA，让局域网客户端知道 fake IPv6 路由由这台 Mac 承载；当前方案宣告的是 `fd00:6152::/60`。
4. 把 `fd00:6152::2` 和 `fd00:6152:0:9::/64` 路由到当前 Surge VIF。
5. Mac 开机、Surge 重启或主网卡变动后，需要重新确认 RA 和路由仍指向当前主网卡 / 当前 Surge VIF。

如果上述条件不满足，安装 mosdns 时不要开启 fake IPv6 解析，让 AAAA 也走 fake IPv4。

### 6. IPv4 + IPv6 回家
⚠️ 入站协议尽量避免udp。下列方案依赖mihomo入站，请先安装mihomo并配置好入站端口。

| 场景 | 公网ipv4 | 公网ipv6 | 容器可得ipv6 | 入站方式                                                                                           
|----|---|---|----------|------------------------------------------------------------------------------------------------|
| 1  | ✅ | ✅ | ✅        | 输入21，安装和mihomo共用ip【局域网ipv4+公网ipv6】的ddnsgo来更新ipv4+ipv6。ipv4在路由器上端口转发到mihomo，ipv6在路由器上开放ipv6端口入站 |
| 2  | ❌ | ✅ | ✅        | 输入21，安装和mihomo共用ip【局域网ipv6】的ddnsgo来更新ipv6。 IPv4考虑relay(比如lucky), ipv6在路由器上开放ipv6端口入站           |
| 3  | ✅ | ❌ | ❌        | 随意ddns后，路由器加端口转发，仅IPv4。                                                                        |
| 4  | ❌ | ✅ | ❌        | IPv6入站可做但不推荐，视作行5考虑                                                                            |
| 5  | ❌ | ❌ | ❌        | 选relay/tunnel方案，比如cloudflare tunnel，frp，tailscale什么的                                           |

## 📌 注意事项
- 默认使用ipv4计算容器的mac地址，mac地址格式类似02:*:86
- 默认使用ipv4计算ipv6 ula地址（⚠️这不符合RFC4193，想合规可手工输入合规的ipv6 ula），生成fd10::/64（对应10.0.0.0/8）、fd17::/64（对应172.16.0.0/12）、fd19::/64（对应192.168.0.0/16）作为 IPv6 网段，如不默认则一定要手工输入ipv6 ula
- 安装macvlan bridge错误请回滚操作，以免流量死循环导致无法进入而重新刷机

## 📦 依赖

| 类型 | 依赖 |
|---|---|
| 基础脚本依赖 | `ipcalc`, `curl`, `jq`, `tar` |
| Docker 功能依赖 | `docker`, `docker compose` |
| 自动更新依赖 | `dockcheck`, `flock`, `python3`, `systemctl`, `regctl` |

其中 Dockcheck 默认从 `mag37/dockcheck` 获取；yehbp 仓库保留一份 `assets/docker-auto-update/dockcheck.sh` 作为 fallback。`regctl` 会在安装 Dockcheck 自动更新时下载到 `_auto_update/bin`。

不同 NAS / Linux 发行版自带命令差异较大，安装前建议先确认基础依赖和 Docker Compose 是否可用。

- https://github.com/perryyeh/librespeed
- https://github.com/perryyeh/adguardhome
- https://github.com/perryyeh/mosdns
- https://github.com/perryyeh/mihomo
- https://github.com/perryyeh/ddnsgo
- https://github.com/perryyeh/lucky

## 📚 参考文献：
- https://github.com/IrineSistiana/mosdns
- https://github.com/AdguardTeam/AdGuardHome
- https://github.com/mag37/dockcheck

## 📜 License
MIT License © 2026