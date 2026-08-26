# Yeh Bypass (Gateway)

自用一键旁路由交互式菜单脚本，提供从 DNS 缓存、域名分流、远程解析、代理接入，到在外回家入口的一整套网络方案。

支持 IPv6，已在群晖 7.3+、飞牛 1.0+、Armbian（Linux 6.1+）上测试通过；并新增 iStoreOS/OpenWrt 后端。

> [!NOTE]
> iStoreOS/OpenWrt 使用 `opkg`、`procd` 和 `logd`，不是 systemd。该平台支持 Docker 容器安装、macvlan、macvlan bridge 持久化及 Docker `data_root` 迁移；不提供脚本内格式化/挂载磁盘、Docker 安装、journald 优化或 systemd Dockcheck 定时任务。

## ✅ 适用场景与前置要求

YehBP 主要用于在局域网内搭建轻量旁路网关。核心容器是：

- AdGuardHome：DNS 缓存与管理入口
- MosDNS：域名分流与 FakeIP 解析
- Mihomo：代理入口与 FakeIP 流量承载（Mihomo 换成 Surge 也可）

分流后不需要代理的域名拿到的是真实 DNS，直接走路由器出去，不走旁路；只有需要代理的域名才走旁路。DNS 流量非常小，普通家用场景下，类似 RK3566 + 1GB 内存 + 千兆网口 + Armbian 这一级别的设备即可使用。

需要注意：

- 如果代理流量较大 / 客户端较多，瓶颈在 Mihomo 上，可使用 CPU 性能、网速更高的 x86 安装 Mihomo，或用 macOS 上的 Surge 替代 Mihomo，以获得更好的代理性能。
- FakeIP 旁路需要 DNS 与数据面路由同时可用：仅能解析到 FakeIP 不代表流量能到达代理入口。IPv4/IPv6 的地址规划、Surge/Mihomo 转发和验证见下方「5. FakeIP 旁路与 IPv6 规划」。
> [!WARNING]
> 如果主路由不支持静态路由或 LAN RA 路由宣告，客户端即使能解析到 FakeIP，流量也无法正确到达代理入口，FakeIP 旁路方案不可完整工作。

- 个人目前使用的路由器是 Ubiquiti UDM 系列 / UCG 系列 / UX 系列，已完美支持。

## ✨ 功能特性
- 交互式选择网卡并确认 IP / 网关 / 子网配置
- 创建 Docker macvlan 网络
- 创建宿主机 `macvlan-bridge` 接口用于互通
- 写入并启用 Systemd 服务，确保开机自启
- 安装、升级、删除 `rtp2httpd`，将 IPTV RTP 组播转为 HTTP 单播
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
| 9  | 删除 macvlan                  |
| 10 | 安装 Portainer                |
| 11 | 安装 LibreSpeed               |
| 14 | 安装 AdGuardHome              |
| 19 | 安装 mosdns                   |
| 20 | 安装 mihomo                   |
| 21 | 安装 ddnsgo                   |
| 22 | 安装 lucky                    |
| 95 | 添加/管理 SOCKS5 代理          |
| 70 | 迁移docker目录                  |
| 71 | 优化docker日志                  |
| 72 | 优化journald日志                |
| 80 | 安装IPTV(rtp2httpd)            |
| 81 | 删除IPTV(rtp2httpd)            |
| 90 | 创建macvlan bridge            |
| 91 | 删除macvlan bridge            |
| 97 | Dockcheck 安装/删除/管理          |
| 98 | Dockcheck 检查/更新镜像           |
| 99 / exit / quit / q | 退出脚本           |
| 999 / del / delete / uninstall / remove / rm | 删除 `yehbp` |

### 安装IPTV(rtp2httpd)（菜单 80）

此功能面向使用 **NetworkManager + systemd** 的 Linux/NAS 主机，不支持 OpenWrt。它会：

1. 菜单 `80` 先搜索并选择 `dockerapps` 安装目录；共享二进制固定安装在 `<dockerapps>/rtp2httpd/rtp2httpd`，不会为每个配置重复下载。
2. 输入配置名，例如 `tel` 会创建 `rtp2httpd_tel.conf`、状态文件 `rtp2httpd_tel.env` 和开机启动服务 `yehbp-rtp2httpd_tel.service`；留空时自动使用最小未占用编号 `_1`、`_2`。
3. 分别输入组播 VLAN ID 与 FCC/DHCP VLAN ID，以及一个已经配置在本机的 IPv4 监听地址及端口（默认 `5140`）。
4. 创建两个 YehBP 管理的 VLAN profile：组播 VLAN 禁用 IPv4，FCC/DHCP VLAN 使用 DHCP 且不接管默认路由。
5. 首次安装时从 `stackia/rtp2httpd` 官方 release 下载与本机架构匹配的二进制，并校验 GitHub 发布的 SHA-256 digest。

同名配置已存在时，菜单 `80` 会询问是否替换对应配置和 service。菜单 `81` 会枚举 `yehbp-rtp2httpd_*.service`，确认后删除选定 service、对应 `.conf`/`.env` 及该实例状态文件中记录的两个 VLAN profile；共享二进制保留，不会按 VLAN ID 猜测或删除既有网络配置。

## 🚀 使用方法

### 1. 安装 yehbp

```bash
curl -fsSL -H 'Accept: application/vnd.github.raw+json' https://api.github.com/repos/perryyeh/yehbp/contents/install.sh?ref=main | sudo bash
```

在 iStoreOS/OpenWrt 中，SSH 登录用户通常已是 `root`，系统默认也没有 `sudo`，使用：

```sh
curl -fsSL -H 'Accept: application/vnd.github.raw+json' https://api.github.com/repos/perryyeh/yehbp/contents/install.sh?ref=main | bash
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

### 2. 删除 yehbp

先进入 `yehbp` 交互菜单：

```bash
sudo yehbp
```

然后输入任一删除命令：

```text
999 / del / delete / uninstall / remove / rm
```

脚本会二次确认后删除 `/usr/local/bin/yehbp`、同目录 SOCKS5 配置 `/usr/local/bin/yehbpproxy.conf` 和历史备份 `/usr/local/bin/yehbp.bak-*`，不会删除已安装的 Docker 容器、配置目录、macvlan、systemd 服务等。

也可以手动删除：

```bash
sudo rm -f /usr/local/bin/yehbp /usr/local/bin/yehbpproxy.conf /usr/local/bin/yehbp.bak-*
```

### SOCKS5 下载代理（菜单 95）

菜单 `95` 可保存一个无认证 SOCKS5 代理。输入格式为 `IP或域名:端口`，也可带 `socks5://` 前缀；有效端口为 `1–65535`。配置保存到与 `yehbp` 命令同目录的 `/usr/local/bin/yehbpproxy.conf`，再次添加会直接替换该单一值，删除操作会删除该文件。

有效配置会用于 YehBP 的版本检查、升级及功能下载，并通过 `socks5h` 让代理端解析下载域名。配置文件不存在或内容无效时，YehBP 不使用代理；已配置有效代理但系统未安装 `curl` 时，为避免绕过代理，下载会取消而非退回直连。

### 3. 设置旁路由步骤

1. 确认 Docker 容器安装目录；如需新硬盘，先完成格式化和挂载。
2. 确认 Docker 已安装；群晖和飞牛通常已有 Docker，可跳过安装。
3. 群晖的网卡建议先开启 Open vSwitch。
4. **IP 段规划：**
   - **IPv4**
     - 为 macvlan 使用新的、独立的 `/24` 网段，避免与现有 LAN 的 IPRange 或静态地址重叠。
     - 示例：将 LAN 从 `10.0.0.1/24` 放宽为网关 `10.0.0.1`、Subnet `10.0.0.0/23`。
     - DHCP IPRange 仍保持 `10.0.0.2–10.0.0.255`；为 YehBP/Docker macvlan 设置相同的 Subnet `10.0.0.0/23` 与独立的 IPRange `10.0.1.0/24`。
   - **IPv6 ULA**
     - 创建时会提供四个明确选项：`1` 使用所选网卡读取到的 IPv6 默认网关和 CIDR（读取失败时不可选择）；`2` 按 IPv4 网关推导 ULA `/64`（[规则与原因](#ula-derivation)）；`3` 手动输入 IPv6 网关、CIDR 与 range；`4` 不启用 macvlan IPv6。
     - 回车默认选择 `2`，`n` 等同于 `4`。VLAN 子接口无完整 IPv6 信息时，选项 `1` 会回退检查物理 parent。
5. 选择网卡创建 macvlan；群晖建议选择 `ovs` 开头网卡。
6. 没有 Surge / OpenWrt 作为代理时，可安装 Mihomo 替代；Mihomo 需开启 TUN 模式并配置好上游代理。
7. 安装 MosDNS；选择 Surge 作为上游时 DNS 写 `198.18.0.2`，选择 Mihomo 作为上游时 DNS 写 Mihomo 的 局域网IP。
8. 安装 AdGuardHome，并使用 MosDNS 作为上游 DNS。
9. 最后创建 macvlan bridge，解决宿主机和容器之间的互通。bridge IPv4 使用 macvlan IPv4 IPRange 的最后一个可用地址；bridge IPv6 使用 Docker IPv6 IPRange 的最后一个地址，宿主机也对该相同 IPv6 IPRange 写入 bridge 路由。手动输入的 IPv6 CIDR/IPRange/Gateway 优先；如 IPv6 IPRange 与 parent 接口现有 RA/on-link 前缀重叠，应确认该路由设计符合预期。
10. 按下方「[5. FakeIP 旁路与 IPv6 规划](#fakeip-routing)」完成 Surge 或 Mihomo 的 FakeIP 数据面转发与验证。
11. 在路由器把 AdGuardHome 的 IP 设置为局域网 DNS。

> [!WARNING]
> 如果安装 `macvlan bridge` 后将 FakeIP 路由指向本机 Mihomo，且 ddns-go 容器使用 `host` 网络模式，请注释其配置中的 `httpinterface: end0`（或实际物理网口名）；否则可能导致公网 IPv4 获取或 DNS 记录提交异常。

#### 安装时的网络模式选择

菜单 `20`、`21`、`22` 安装容器时会询问网络模式。选择不会自动由“回家”场景推断，应按实际用途确认：

| 容器 / 菜单 | 默认模式 | `host` | `macvlan` |
|---|---|---|---|
| Mihomo / `20` | `macvlan` | 使用宿主机网络，适合提供在外回家入口；会占用宿主机 `7890`、`7891`、`7892`、`9090` 等端口。 | 独立 LAN IP/MAC，适合旁路由出站代理。 |
| ddns-go / `21` | `host` | 直接使用宿主机的 IPv4/IPv6，适合双栈 DDNS；会占用宿主机 `9876` 端口。 | 独立 LAN IP/MAC，适合仅 IPv4 的环境。 |
| Lucky / `22` | `host` | 直接使用宿主机网络，适合 IPv4 + IPv6；会占用宿主机 `16601` 端口。 | 独立 LAN IP/MAC，适合仅 IPv4 的环境。 |

同一宿主机需要同时运行不同用途的实例时，为每个实例指定不同的容器/目录名称，并确认其端口不会冲突。

### 4. Docker 镜像自动更新

菜单 `97` 提供 Dockcheck 管理：

- 查看 Dockcheck 状态/版本。
- 安装 Dockcheck。
- 删除 Dockcheck。
- 升级 Dockcheck。

安装 Dockcheck 时：

- 选择 `dockerapps` 目录后，组件会安装到 `<dockerapps>/_auto_update`。
- Dockcheck 脚本优先从上游 raw 脚本地址下载：`https://raw.githubusercontent.com/mag37/dockcheck/main/dockcheck.sh`；如该 raw 地址下载失败，则使用 yehbp 仓库内置副本。
- 可设置新镜像发布后延迟 N 天再更新。
- 可选择更新后自动删除 dangling images。
- 可选择是否启用每日 systemd timer。

删除 Dockcheck 时会停用并移除 systemd service/timer，并可选择是否删除 `_auto_update` 目录。

升级 Dockcheck 时会先显示本地与上游版本；仅上游版本严格更高时才替换 Dockcheck 本体。无论 Dockcheck 是否有新版本，都会同步 YehBP 的 wrapper、MAC 检查脚本和模板。升级不会修改 `auto-update.conf`，也不会执行 Dockcheck、更新容器或重启 timer。

菜单 `98` 提供镜像检查和更新操作：

- 检查并更新 compose 容器。
- 只检查全部容器，不更新。
- 检查/拉取非 compose 容器镜像；不会重建该类容器。
- 若已有 Dockcheck 任务，交互运行可选择强制终止旧任务后继续，或取消返回菜单；非交互任务不会终止已有任务。


需要固定容器 MAC 的服务，应在 compose 网络配置中显式写 `mac_address`；Dockcheck 更新后会检查 compose 期望 MAC 与实际容器 MAC 是否一致。

<a id="fakeip-routing"></a>

### 5. FakeIP 旁路与 IPv6 规划

FakeIP 的 DNS 返回地址只是代理流量的目标地址；客户端、路由器或宿主机还必须把该地址段的数据流送到 Surge / Mihomo。验证时始终使用当前 DNS 实际返回的 FakeIP，不要写死某次解析结果。

<a id="ula-derivation"></a>

#### 5.1 ULA：YehBP 为什么这样推导

YehBP 默认把 RFC1918 IPv4 网段映射为独立 ULA `/64`：

| IPv4 网段 | 默认 ULA `/64` |
|---|---|
| `10.A.B.0/24` | `fd00:10:A:B::/64` |
| `172.A.B.0/24` | `fd00:172:A:B::/64` |
| `192.168.B.0/24` | `fd00:192:168:B::/64` |

这样可完整保留 IPv4 前三个 octet 的可读性，例如 `10.88.99.0/24` 对应 `fd00:10:88:99::/64`；每个 macvlan 网络也能拥有独立 ULA `/64`，供容器、bridge 和代理下一跳使用，而不依赖可能变化的公网 IPv6 GUA。IPv6 hextet 按十六进制解析；此处使用 IPv4 十进制文本作为可读标签，不是二进制位级映射。

选择自动 ULA 模式时，YehBP 还会按 IPv4 `IPRange` 的第三段建议 IPv6 `IPRange`：例如 IPv4 gateway 为 `10.86.8.1`、IPv4 `IPRange` 为 `10.86.9.0/24` 时，默认 ULA Subnet 为 `fd00:10:86:8::/64`，默认 IPv6 `IPRange` 为 `fd00:10:86:8::9:0/112`。这是便于兼容 `::第三段:最后段` 静态容器地址的推导建议；IPv4 `/25` 或更窄时仍使用对应第三段的 `/112`，不改变现有静态 IPv6 地址格式。手动输入 IPv6 `IPRange` 始终优先。

这只是便捷、确定性的映射，不是 RFC4193 随机 Global ID。多站点、跨网络互联或长期正式部署时，建议自行规划 RFC4193 ULA，并在创建 macvlan 时输入完整 IPv6 CIDR。

> Fake-IP 地址池与 LAN ULA 是不同概念：LAN ULA 用于稳定的局域网下一跳；Fake-IP 是 DNS 为代理域名返回的目标地址。

#### 5.2 转发到 macOS Surge

##### Fake IPv4

| 项目 | 地址 / 前缀 |
|---|---|
| Surge VIF 网关 | `198.18.0.1` |
| Fake DNS IPv4 | `198.18.0.2` |
| Fake IPv4 池 | `198.18.0.0/15` |

主路由必须添加静态路由：`198.18.0.0/15` 的下一跳为运行 Surge 的 Mac 局域网 IPv4，例子：如果surge所在的mac局域网ip是10.0.0.120，那么在路由器上静态路由198.18.0.0/15下一跳为10.0.0.120。

##### Fake IPv6：Surge VIF 与 LAN RIO

Surge 使用 VIF 承载 FakeIP，必须设置 `ipv6-vif = always`。Mac 须开启 `net.inet6.ip6.forwarding=1`；Mac 开机、Surge 重启或主网卡变化后，需要确认当前 VIF、本机路由和 LAN RA 仍保持一致。

###### Surge 旧版与新版 Fake IPv6 对照（新版 6.8.0，2026.08.06 发布）

| 项目 | 旧版 Surge | 新版 Surge |
|---|---|---|
| Surge VIF 网关 | `fd00:6152::1` | `2001:2:0:6152::1` |
| Fake DNS IPv6 | `fd00:6152::2` | `2001:2:0:6152::2` |
| 实际 Fake IPv6 池 | `fd00:6152:0:9::/64` | `2001:2:0:6152:0:9::/96` |
| macmini 本机路由 / LAN RIO | `fd00:6152::1/127`、`fd00:6152:0:9::/64` | `2001:2:0:6152::/64` |

旧版：mac进行ra宣告`fd00:6152::1/127`、`fd00:6152:0:9::/64`，并把流量转发给surge所在的tun；

新版：mac进行ra宣告`2001:2:0:6152::/64` ，并把流量转发给surge所在的tun；

###### macOS Surge RA reference

脱敏参考实现位于 [`assets/macmini/`](assets/macmini/)，文件与 macOS 安装位置对应：

| 仓库文件 | macOS 目标位置 | 职责 |
|---|---|---|
| `assets/macmini/surge-ipv6-router` | `/usr/local/sbin/surge-ipv6-router` | 找到 Surge `utun`、将 Fake IPv6 `/64` 路由到 VIF、生成配置并管理 `rtadvd`。 |
| `assets/macmini/surge-fake-rtadvd.conf` | `/usr/local/etc/surge-fake-rtadvd.conf` | 参考模板；运行时由 helper 生成，向 LAN 发布 RIO。 |
| `assets/macmini/local.surge-ipv6-router.plist` | `/Library/LaunchDaemons/local.surge-ipv6-router.plist` | 以 launchd 在开机和异常退出后启动 helper。 |

参考脚本不含特定家庭 LAN IP、ULA 或 MAC 地址；需要 macOS 自带的 `scutil` 与 Python 3。部署前编辑 `assets/macmini/local.surge-ipv6-router.plist`：

- helper 每 5 秒读取 macOS SystemConfiguration 的 ServiceOrder，选择首个有 IPv4 Router 状态的非 VPN/bridge 物理服务；不要用 Surge 接管后的默认路由判断接口。
- `__LAN_ULA_ADDR__` 是可选稳定 ULA 下一跳；若需要路由器静态路由兜底，替换为该 LAN ULA `/64` 内一个未使用地址；否则保持占位符不变，helper 不会添加 ULA alias。接口切换时，该 alias 会从旧物理接口迁移到当前承载接口。

运行时 helper 用当前选中的接口生成 `surge-fake-rtadvd.conf`；仓库中的同名文件是带 `__LAN_INTERFACE__` 占位符的参考模板。

> [!WARNING]
> 注意 Surge 版本区别：YehBP 当前代码只处理新版 Surge 的 `2001:2:0:6152` 地址体系。旧版 Surge 用户需按上表自行对照修改 VIF 路由、LAN RIO 与相关 Fake-IP 配置。

如果上述 Surge 链路未验证完成，安装 mosdns 时不要开启 fake IPv6 解析。

#### 5.3 转发到 Mihomo

| 流量 | 路由前缀 | 下一跳 |
|---|---|---|
| Fake IPv4 | `198.18.0.0/15` | Mihomo 的 LAN IPv4 |
| Fake IPv6 | 字段 `dns.fake-ip-range6`，默认值 `2001:2:0:6152:0:9::/96`；路由器与 YehBP host bridge 均应使用该实际精确前缀。如有变更，需要重新创建宿主机到容器的 bridge 互通。 | Mihomo 的 LAN ULA IPv6 |

Mihomo 也使用 FakeIP，但与 Surge 不同：它没有 Surge VIF 的 `::1` 网关和 `::2` Fake DNS 地址，Fake-IP 前缀由 Mihomo 配置直接决定。主路由直接设置 Fake IPv4 和 Fake IPv6 的静态路由：IPv4 下一跳为 Mihomo 的 LAN IPv4，IPv6 下一跳为 Mihomo 的 LAN ULA IPv6。

YehBP 创建 macvlan bridge 时，如探测到 Mihomo，会询问是否同时写入宿主机直达 Mihomo 的 FakeIP route；这只优化宿主机访问，不替代 LAN 或 macvlan 客户端所需的路由。

#### 5.4 macvlan 容器：接受 Surge RA 的 Fake IPv6 路由

使用 Docker `macvlan` 的容器，在通过 Surge RA 获得非默认 Fake IPv6 路由时，需要显式接受该 RIO。否则容器即使能解析出 Fake IPv6，也可能不会安装指向 Surge 的专用路由，而是错误走默认 IPv6 网关，导致 HTTPS 超时。

在每个需要访问 Fake IPv6 的服务的 `macvlan` network endpoint 中加入：

```yaml
services:
  app:
    networks:
      macvlan:
        ipv4_address: ${ipv4}
        ipv6_address: ${ipv6}
        driver_opts:
          com.docker.network.endpoint.sysctls: net.ipv6.conf.IFNAME.accept_ra_rt_info_max_plen=128
```

该项是服务级 `networks.macvlan` 配置，不是顶层 `networks` 配置。容器重建后，对当前 DNS 返回的 Fake IPv6 执行：

```bash
ip -6 route get <当前 DNS 返回的 Fake IPv6>
```

正常结果应命中 Surge RA 宣告的专用下一跳，而不是仅依赖默认 IPv6 网关。

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
- YehBP 的 IPv4→ULA 默认推导、适用范围和 RFC4193 注意事项见「5.1 ULA：YehBP 为什么这样推导」。
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
- https://github.com/MetaCubeX/mihomo
- https://github.com/mag37/dockcheck

## 📜 License
MIT License © 2026
