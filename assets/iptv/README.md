# 独立 IPTV 参考脚本

运行时只需 **`assets/iptv/iptv.py`** 一个文件，无菜单或服务依赖。当前仅实现上海电信协议，不宣称各地区通用。Python 3.9+ 标准库及 OpenSSL CLI；真实 fetch 需已接通 IPTV 的 Linux、iproute2 和 SO_BINDTODEVICE 权限（通常 root）。macOS 可配置、离线转换和测试。

```sh
# 首次全参数：保存配置、获取原始数据并生成playlist（身份仅为示意）
python3 iptv.py config=tel userinfo=USER SN=BOX_SN MAC=00:11:22:33:44:55 \
  interface=iptv85 multicast_interface=iptv51 fcc_interface=iptv85 \
  baseURL=http://127.0.0.1:5140 directory_metadata=y
# 后续最简：离线转换，主产物仍为tel/playlist.m3u；另导出指定完整路径
python3 iptv.py config=tel output=/your/export/channels.m3u
# output是已有目录或以/结尾时，副本名为配置目录basename.m3u
python3 iptv.py config=tel output=/your/export/
# 不导出；output=foo.m3u等无目录部分且不是已有目录的值同样不导出
python3 iptv.py config=tel
# 可选安全身份入口：首次configure仅保存，然后显式run获取并转换
python3 iptv.py configure --config-dir /your/iptv/tel --stdin
python3 iptv.py run --config-dir /your/iptv/tel
```

主入口使用 `key=value`。`config=tel` 是 **iptv.py所在目录下的tel子目录**，不需前置或结尾斜杠；所有相对config路径统一相对脚本目录，与调用cwd无关，也接受绝对路径。首次省略config时，必须明确提供 `interface=所选接口`，使用该接口名作为脚本同级配置子目录名，绝不默认猜eth0。缺少身份或接口会失败，不写不完整配置。

准确键名为 `config`、`output`、`userinfo`（也接受`userid`）、`SN`（`sn`）、`MAC`（`mac`）、`interface`、`multicast_interface`、`fcc_interface`、`baseURL`（`base_url`）、`auth_origin`、`live_path`、`replay_path`、`seek_template`、`allowed_origins`（空格分隔的一个参数）、`directory_metadata`（y/n或true/false）。身份全参数会出现在进程argv及可能的shell历史中；脚本不打印身份或完整认证URL，可选受保护stdin入口见下文。出现任一配置字段即保存配置并fetch、convert；只给config/output时严格离线。成功写入同名文件时逐文件原子替换，失败不保证跨文件事务。

`output`是完整目的路径。结尾 `/` 或已存在目录认定纯目录，用配置目录basename加`.m3u`；含目录部分的非目录值视完整文件名，例如 `/dir/file.m3u` 或 `./file.m3u`。不存在且无斜杠的 `foo.m3u` 或 `newdir` 不导出，不猜成目录。相对output按调用cwd解析。省略output也不导出；每次转换仍生成配置目录内`playlist.m3u`。

每个配置目录就是唯一实例，固定文件如下：

| 文件 | 内容 |
|---|---|
| `config.json` | 身份、接口、鉴权入口、rtp2httpd base URL等配置 |
| `catchup.json` | 完整解析的原始channelArray数组，字段、顺序、重复及null均保留 |
| `channels.json` | 可选原始名称目录，完整保留12分类响应及额外字段/重复记录 |
| `playlist.m3u` | 最终完整M3U，每次convert始终写在配置目录 |
| `.iptv.lock` | 内部实例锁 |

配置目录与额外输出目录可不存在，但其父目录必须已经存在，脚本只创建末级目录（0700，不递归创建父目录）。现存用户目录、脚本父目录、服务目录一律不修改权限；目录须属当前用户且路径不经过符号链接。所有新写文件及锁为0600；身份配置要求当前用户所有且0600。目录应由用户选择为适当私有位置。

辅助旧式参数（相对`--config-dir`按调用cwd解析）仍兼容：`--output-dir` 是主输出之外的副本，默认名 `playlist.m3u`；`--output-name` 仅改变副本名，必须同时提供输出目录，接受不含路径、控制字符且非隐藏的安全basename。两个目录指向同一路径且名称相同时只写一次；同目录不同名会额外生成副本。禁止导出覆盖本实例配置、原始数据、目录、锁或脚本。不生成 raw/named/override。每个文件成功时同目录原子替换；主输出先完成，额外导出失败返回非零并明确提示主playlist已生成，需检查导出产物状态。这不是跨文件事务；替换前失败保留旧文件，替换后的fsync失败可能已写入新文件。

默认无命令与显式 `convert` 完全相同：仅读取已有catchup/channels，不鉴权、不发网络请求。缺少catchup时提示先执行fetch；缺少channels时以原始名称或ChannelID占位。`fetch` 和 `run` 才主动刷新；`run` 的fetch失败时不会继续convert。

`configure` 依次询问 UserID、盒子独立SN、盒子MAC（均隐藏且不显示已有身份）、鉴权/回放接口、组播接口、FCC接口、鉴权origin、rtp2httpd base URL、直播路径、回放路径、时间模板、额外允许origin、是否获取名称目录y/n。空输入保留已有值；首次缺少必要身份或鉴权接口会失败，不猜主机接口或盒子信息。不能用STBID或主机网卡MAC替代盒子身份。组播/FCC输入 `-` 可清空，额外origin以空格分隔或 `-` 清空。默认组播沿用鉴权接口，FCC不另指定，目录默认关闭。关闭目录只影响今后fetch，已有channels仍可供离线convert使用；需用户明确移走它才完全不使用。

自动化使用 `configure --config-dir DIR --stdin`，由受保护stdin依上述顺序提供**13行**，前三行是身份，其余与交互顺序相同；空行保留，提前EOF报错且不替换旧配置。stdin模式是明确选择，输入不会回显；不要把身份放在argv或命令历史。配置保留未知JSON字段。

这是新目录格式：不接受旧 `--instance`、`--directory`、`--identity-stdin` 或 `--m3u-output` 参数，不自动迁移、移动、删除或读取旧 `<instance>.auth.json`、`catchup_<instance>.json` 等文件。请在独立新目录运行configure，并显式fetch生成新格式；旧实例数据保持原状。旧配置里的输出覆盖设置不作为新输出规则使用。

fetch重新读取鉴权接口唯一全局IPv4，禁用环境代理并绑定已有接口。表单、重定向、门户目标受精确origin白名单限制：配置鉴权入口及已实证的 `http://124.75.29.176:7001`、`http://218.83.165.50:8084`、`http://218.83.165.36:8084`。额外地址须先核实再配置允许origin，不自动信任任意EPGDomain、IpPort或整个运营商网段。网络错误仅输出固定脱敏信息。

fetch不合并、不改名。可选目录阶段沿用同一认证cookie会话登录门户、保留DynamicAuthIP，调用12分类getChannelList，不下载逐节目EPG/XMLTV。每分类完整JSON响应（status/data/额外字段、分类及重复记录）包存在channels.json，不写回catchup。目录解析、分类、失败状态或空总目录异常均使fetch退出1并保留原有两文件。候选完成验证后分别原子替换，文件间不是事务；磁盘写入故障可能出现新目录与旧catchup，修复后需重跑。

名称优先原始ChannelName，其次按精确 `UserChannelID ↔ mixNo` 匹配目录；仅当不同name候选唯一时使用原文。HD、高清、4K、括号、前后及连续空格全部保留。不按mixNo去重原始频道。冲突候选保留在目录，统计ambiguous并以精确ChannelID占位；无目录或未匹配统计unmatched并占位。convert打印source/matched/ambiguous/unmatched数量。分组字段保留在目录，M3U不推断分组；不得按复用回放地址猜名字。M3U无法安全表达的换行、双引号等使转换失败，不静默修改名称，保留上次主输出。

直播经 `/rtp/<组播IP:端口>`；有效FCC地址端口才追加 `fcc=IP:port`，缺失不写 `fcc=:`。鉴权/回放用interface，组播用multicast_interface（省略则沿用interface），FCC可另设fcc_interface，通过每请求r2h-ifname参数选择，不修改服务。回放经 `/rtsp/<主机:端口>/<路径>`，默认RTSP端口554；原始查询字符原样保留，包括百分号大小写、`+`、重复键，尾部追加playseek和接口参数。已有playseek或非空坏地址使整个convert失败。TimeShiftURL为null、空或字面null时写 `catchup="none"`。默认模板 `{utc:YmdHMS}-{utcend:YmdHMS}`，亦可配置播放器 `${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}`；不擅自增加8小时或启用Range。参考 [rtp2httpd URL](https://rtp2httpd.com/en/guide/url-formats)及 [M3U](https://rtp2httpd.com/en/guide/m3u-integration)，此前实测版本v3.16.0；本轮目录模型仅本机离线验证。

协议参考 [pcg562240/pcg-iptv固定提交38653f0](https://github.com/pcg562240/pcg-iptv/blob/38653f0bd722c4dececae434efa3593be97e8aca/shctiptv.py)。该文件自述参考 [denymz/sh-tel-iptv-spider](https://github.com/denymz/sh-tel-iptv-spider) 核心流程，此处记录其来源，未另行验证denymz实现。固定树无LICENSE，未复制其AES/客户端实现或宣称再许可；AES使用OpenSSL，表单/JS字符串/门户及转换独立实现。

本脚本不是YehBP菜单功能，需自行复制和适配。取得完整列表和目录后由用户自行格式化筛选、分组、排序或模板适配。身份、JSON和M3U可能含认证数据，不可提交Git或公开订阅。

维护测试在仓库tests：`python3 -m unittest discover -s tests -p 'test_iptv_reference_*.py' -v`。可选只读独立产物检查：`python3 tests/verify_iptv_artifacts.py /absolute/config/dir`，默认检查本目录playlist.m3u，也可用 `--m3u /absolute/export/playlist.m3u` 指定副本；不导入实现，仅报告数量/哈希，不输出身份或URL。
