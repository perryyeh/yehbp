# Yeh Bypass (Gateway)

自用一键旁路由脚本，提供从 DNS 缓存、域名分流、远程解析、代理接入，到在外回家入口的一整套网络方案。

核心容器包括 AdGuardHome、MosDNS、Mihomo、ddns-go：AdGuardHome 负责 DNS 缓存；MosDNS 负责域名分流，内部域名返回真实 IP，外部域名返回 FakeIP；代理由 Mihomo 或 Surge 承载。

在外回家方案以 ddns-go + Mihomo 作为入口，统一处理 IPv4 / IPv6 访问与入站流量。

支持 IPv6，已在群晖 7.3+（可能需要补全欠缺命令）、飞牛 1.0+、Armbian（Linux 6.1+）上测试通过。


## ✨ 功能特性
- 交互式选择网卡并确认 IP / 网关 / 子网配置
- 创建 Docker macvlan 网络
- 创建宿主机 `macvlan-bridge` 接口用于互通
- 写入并启用 Systemd 服务，确保开机自启
- 预定义多个容器 IP：librespeed（.111） AdGuardhome（.114）、MosDNS（.119）、Mihomo（.120）
- 此代码多数由openai和gemimi生成


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
| 97 | 安装watchtower 自动更新           |
| 98 | 强制使用watchtower更新一次镜像        |
| 99 | 退出脚本                        |

## 🚀 使用方法

### 1. 安装命令

```bash
curl -fsSL https://github.com/perryyeh/yehbp/raw/refs/heads/main/install.sh -o /tmp/yehbp-install.sh
sudo bash /tmp/yehbp-install.sh install
```

安装后直接运行：

```bash
sudo yehbp
```

每次运行 `yehbp` 时会检查仓库版本。如果发现新版本，会提示是否升级：

```text
是否现在升级？[y/N]:
```

输入 `y` 才会升级；默认回车或输入 `n` 都不会升级。版本检查只读取仓库里的 `VERSION` 文件；确认升级后才会下载最新脚本并做语法检查。如果本机已有 `/usr/local/bin/yehbp`，会先备份为 `/usr/local/bin/yehbp.bak-时间戳`，再覆盖更新。

### 2. 或克隆项目运行

```bash
git clone https://github.com/perryyeh/yehbp.git
cd yehbp
chmod +x install.sh
sudo ./install.sh
```

### 3. 安装步骤

1. 确立docker容器安装目录，硬盘没有格式化&加载的先格式化&加载
2. 没docker的先安装docker（群晖和飞牛已有，直接跳过）
3. 群晖和飞牛的网卡建议先开open vSwitch
4. 确立专有ip段给macvlan使用，ipv4建议给一个新的/24段（或现有ipv4的开头/结尾），ipv6 ula建议给/64段，在路由器上设置dhcp时候避开这段不分配
5. 选择网卡（群晖和飞牛建议选ovs开头网卡）创建macvlan
6. 没有surge/openwrt当代理的，可安装mihomo替代，mihomo需开tun模式并配置好上游代理。
7. 路由器里配置静态路由，198.18.0.0/15下一跳到surge/mihomo的ip。
8. 安装mosdns，选surge当上游时dns写198.18.0.2；选mihomo当上游时，dns写mihomo的ip。
9. 安装adguardhome，用mosdns当上游，dns写mosdns的ip。
10. 最后创建macvlan bridge，解决宿主机和容器之间的互通。

### 4. mosdns 在 Surge 下使用 fake IPv6

mosdns 选择 Surge 作为上游，并开启 fake IPv6 解析前，需要先确认 Surge 的 fake IPv6 链路完整可用。仅 DNS 能返回 fake IPv6 不够，客户端还必须能把该 IPv6 段路由到运行 Surge 的 Mac，并由 Surge VIF 承载。

路由器侧：
1. 添加静态路由：`fd00:6152::/126` 下一跳到运行 Surge 的 Mac。

Mac 侧：
1. Surge 配置必须设置 `ipv6-vif = always`，不能用 `auto`，否则 IPv6 VIF 可能不会按预期拉起。
2. 开启 IPv6 转发：`net.inet6.ip6.forwarding=1`。
3. 把 `fd00:6152:0:9::/64` 绑定到 Surge VIF。
4. 在主网卡发送 RA，告诉局域网：`fd00:6152:0:9::/64` 这个 IPv6 段由这台 Mac 承载。
5. Mac 开机后或主网卡变动后，需要重新确认/切换第 3、4 步，确保 fake IPv6 段仍绑定在 Surge VIF，RA 仍从当前主网卡发布。

如果上述条件不满足，安装 mosdns 时不要开启 fake IPv6 解析，让 AAAA 也走 fake IPv4。

### 5.ipv4+ipv6回家
⚠️ 入站协议尽量避免udp。下列方案依赖mihomo入站，请先安装mihomo并配置好入站端口。

| 场景 | 公网ipv4 | 公网ipv6 | 容器可得ipv6 | 入站方式                                                                                           
|----|---|---|----------|------------------------------------------------------------------------------------------------|
| 1  | ✅ | ✅ | ✅        | 输入21，安装和mihomo共用ip【局域网ipv4+公网ipv6】的ddnsgo来更新ipv4+ipv6。ipv4在路由器上端口转发到mihomo，ipv6在路由器上开放ipv6端口入站 |
| 2  | ❌ | ✅ | ✅        | 输入21，安装和mihomo共用ip【局域网ipv6】的ddnsgo来更新ipv6。 IPv4考虑relay(比如lucky), ipv6在路由器上开放ipv6端口入站           |
| 3  | ✅ | ❌ | ❌        | 随意ddns后，路由器加端口转发，仅IPv4。                                                                        |
| 4  | ❌ | ✅ | ❌        | IPv6入站可做但不推荐，视作行5考虑                                                                            |
| 5  | ❌ | ❌ | ❌        | 选relay/tunnel方案，比如cloudflare tunnel，frp，tailscale什么的                                           |

### 5.其他镜像

##### mihomo：解决代理和在外回家入站，入站统一收口到mihomo的ipv4+ipv6
##### ddnsgo：和mihomo共用ipv4+ipv6，解决域名更新问题
##### lucky：和mihomo共用ipv4+ipv6，解决ipv4打洞问题
## 📌 注意事项
- 默认使用ipv4计算容器的mac地址，mac地址格式类似02:*:86
- 默认使用ipv4计算ipv6 ula地址（⚠️这不符合RFC4193，想合规可手工输入合规的ipv6 ula），生成fd10::/64（对应10.0.0.0/8）、fd17::/64（对应172.16.0.0/12）、fd19::/64（对应192.168.0.0/16）作为 IPv6 网段，如不默认则一定要手工输入ipv6 ula
- 安装macvlan bridge错误请回滚操作，以免流量死循环导致无法进入而重新刷机

## 📦 依赖：
- 脚本会自动安装依赖的命令： ipcalc curl jq
- https://github.com/perryyeh/mosdns
- https://github.com/perryyeh/adguardhome

## 📚 参考文献：
- https://github.com/IrineSistiana/mosdns
- https://github.com/AdguardTeam/AdGuardHome

## 🔧 开发备忘
- Docker 容器名与安装目录同名（如 `/data/dockerapps/mosdns` → 容器名 `mosdns`）
- 推送前先在测试环境验证：`git push perryyeh/yehbp main`
- 关联服务仓库：librespeed / adguardhome / mosdns / mihomo / ddnsgo / lucky

## 📜 License
MIT License © 2026