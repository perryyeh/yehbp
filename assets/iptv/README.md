# 独立 IPTV 参考脚本

运行时只需 **`assets/iptv/iptv.py`** 一个文件，无菜单或服务依赖。当前仅实现上海电信协议，不宣称各地区通用。Python 3.9+ 标准库及 OpenSSL CLI；真实 fetch 需已接通 IPTV 的 Linux、iproute2 和 SO_BINDTODEVICE 权限（通常 root）。macOS 可配置、离线转换和测试。

```sh
# 首次全参数：保存配置、获取原始数据并生成playlist（身份仅为示意）
python3 iptv.py config=tel userinfo=YOUR_USER_ID SN=YOUR_BOX_SN MAC=YOUR_BOX_MAC \
  interface=iptv85 multicast_interface=iptv51 fcc_interface=iptv85 \
  baseURL=http://127.0.0.1:5140
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

主入口使用 `key=value`，参数之间以空格分隔。下表默认值仅用于新配置；已有配置未提供的字段保留原值。身份占位符须替换为本人已授权的机顶盒信息，不能用STBID或主机网卡MAC替代。

| 参数（别名） | 用途 | 必填 / 默认 | 示例或备注 |
|---|---|---|---|
| `config` | 配置目录，也是唯一实例 | 可省略；省略时必须明确提供 `interface`，以接口名为目录名 | `config=tel` 指向脚本同级tel目录；相对路径均以脚本目录为基准，与调用cwd无关，也接受绝对路径 |
| `output` | 额外导出的完整目的路径 | 可选；省略不导出 | `output=/your/export/channels.m3u`；目录与末级文件名规则见下文，主产物始终为配置目录内 `playlist.m3u` |
| `userinfo`（`userid`） | IPTV UserID | 首次必填 | `userinfo=YOUR_USER_ID` |
| `SN`（`sn`） | 机顶盒独立SN | 首次必填 | `SN=YOUR_BOX_SN` |
| `MAC`（`mac`） | 机顶盒MAC | 首次必填 | `MAC=YOUR_BOX_MAC`；实际值须为冒号分隔的六组两位十六进制数 |
| `interface` | 鉴权及回放接口 | 首次必填；不猜测eth0 | `interface=iptv85`；接口名为1–15位字母、数字或 `_.:-` |
| `baseURL`（`base_url`） | rtp2httpd服务基址 | 可选；`http://127.0.0.1:5140` | `baseURL=http://127.0.0.1:5140`；支持HTTP/HTTPS及路径前缀，不接受查询参数、片段或用户凭据 |
| `multicast_interface` | 组播接口 | 可选；空值沿用 `interface` | `multicast_interface=iptv51`；`multicast_interface=` 清空已有覆盖 |
| `fcc_interface` | FCC接口 | 可选；空值不另指定 | `fcc_interface=iptv85`；`fcc_interface=` 清空已有覆盖 |
| `auth_origin` | 鉴权入口 | 可选；`http://222.68.208.73:7001` | 仅HTTP/HTTPS origin，不含业务路径或查询参数 |
| `live_path` | rtp2httpd直播路径 | 可选；`/rtp/` | `live_path=/udp/`；首尾为 `/`，中间仅字母、数字、下划线、连字符或 `/` |
| `replay_path` | rtp2httpd回放路径 | 可选；`/rtsp/` | `replay_path=/proxy/rtsp/`；路径格式同上 |
| `seek_template` | 回放时间模板 | 可选；`{utc:YmdHMS}-{utcend:YmdHMS}` | 例如 `'seek_template=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}'`；用单引号防shell展开，模板不得为空或含 `&`、`#`、`?` |
| `allowed_origins` | 额外允许的鉴权/门户origin | 可选；空列表 | `'allowed_origins=http://example.test:8080 http://example.test:8081'`；多个origin须放在同一个参数中，`allowed_origins=` 清空；须先核实目标 |

首次提供身份、接口及所需播放覆盖参数，验证通过后保存配置并fetch、convert；缺少必要身份或接口会失败，不写不完整配置。**出现任一配置字段（除config/output外的表中参数）即保存配置并主动fetch、convert**，并非只修改离线播放设置；只给config/output时严格离线。别名不能与原键重复传入，未知键或重复键会报错。`directory_metadata` 已移除，无需设置：fetch始终获取完整频道目录。

身份全参数会出现在进程argv及可能的shell历史中；脚本不打印身份或完整认证URL，可选受保护stdin入口见下文。成功写入同名文件时逐文件原子替换，失败不保证跨文件事务。

`output`是完整目的路径。结尾 `/` 或已存在目录认定纯目录，用配置目录basename加`.m3u`；含目录部分的非目录值视完整文件名，例如 `/dir/file.m3u` 或 `./file.m3u`。不存在且无斜杠的 `foo.m3u` 或 `newdir` 不导出，不猜成目录。相对output按调用cwd解析。省略output也不导出；每次转换仍生成配置目录内`playlist.m3u`。

每个配置目录就是唯一实例，固定文件如下：

| 文件 | 内容 |
|---|---|
| `config.json` | 身份、接口、鉴权入口、rtp2httpd base URL等配置 |
| `catchup.json` | 完整解析的原始channelArray数组，字段、顺序、重复及null均保留 |
| `channels.json` | 原始名称目录（fetch始终获取），完整保留12分类响应及额外字段/重复记录 |
| `playlist.m3u` | 最终完整M3U，每次convert始终写在配置目录 |
| `.iptv.lock` | 内部实例锁 |

配置目录与额外输出目录可不存在，但其父目录必须已经存在，脚本只创建末级目录（0700，不递归创建父目录）。现存用户目录、脚本父目录、服务目录一律不修改权限；目录须属当前用户且路径不经过符号链接。所有新写文件及锁为0600；身份配置要求当前用户所有且0600。目录应由用户选择为适当私有位置。

辅助子命令仍受支持，与主key=value入口分别使用；所有子命令均需 `--config-dir`。省略子命令（例如 `python3 iptv.py --config-dir /your/iptv/tel`）等同显式 `convert`，裸运行脚本仍会因缺少 `--config-dir` 报错。

| 子命令 | 用途 | 是否主动联网 | 示例 |
|---|---|---|---|
| `configure` | 交互或stdin录入并保存配置 | 否 | `python3 iptv.py configure --config-dir /your/iptv/tel` |
| `fetch` | 获取原始频道及完整名称目录，不转换 | 是 | `python3 iptv.py fetch --config-dir /your/iptv/tel` |
| `convert`（默认） | 从已有数据生成主playlist及可选副本 | 否 | `python3 iptv.py convert --config-dir /your/iptv/tel` |
| `run` | 先fetch，成功后再convert | 是 | `python3 iptv.py run --config-dir /your/iptv/tel` |

| 辅助参数 | 用途 | 必填 / 默认 | 示例或备注 |
|---|---|---|---|
| `--config-dir DIR` | 配置、源数据和主playlist目录 | 所有子命令必填 | `--config-dir ./tel` 相对调用cwd解析；与主入口 `config=tel` 的基准不同 |
| `--output-dir DIR` | 主输出之外的副本目录 | 可选；仅convert/run | `--output-dir /your/export`；默认副本名为 `playlist.m3u`，不使用配置目录basename |
| `--output-name NAME` | 指定副本文件名 | 可选；默认 `playlist.m3u`，需同时提供 `--output-dir` | `--output-name channels.m3u`；必须为不含路径、控制字符且非隐藏的安全basename |
| `--stdin` | 按固定顺序从受保护stdin读取12行配置 | 可选；仅configure，默认交互输入 | `configure --config-dir /your/iptv/tel --stdin`；行顺序及空行规则见下文 |
| `-h`、`--help` | 显示CLI帮助 | 可选 | `python3 iptv.py --help` |

两个目录指向同一路径且名称相同时只写一次；同目录不同名会额外生成副本。禁止导出覆盖本实例配置、原始数据、目录、锁或脚本。不生成 raw/named/override。每个文件成功时同目录原子替换；主输出先完成，额外导出失败返回非零并明确提示主playlist已生成，需检查导出产物状态。这不是跨文件事务；替换前失败保留旧文件，替换后的fsync失败可能已写入新文件。

默认convert仅读取已有catchup/channels，不鉴权、不发网络请求。缺少catchup时提示先执行fetch；缺少channels时以原始名称或ChannelID占位。辅助命令中 `fetch` 和 `run` 才主动刷新；`run` 的fetch失败时不会继续convert。

`configure` 依次询问 UserID、盒子独立SN、盒子MAC（均隐藏且不显示已有身份）、鉴权/回放接口、组播接口、FCC接口、鉴权origin、rtp2httpd base URL、直播路径、回放路径、时间模板、额外允许origin。空输入保留已有值；首次缺少必要身份或鉴权接口会失败，不猜主机接口或盒子信息。不能用STBID或主机网卡MAC替代盒子身份。组播/FCC输入 `-` 可清空，额外origin以空格分隔或 `-` 清空。默认组播沿用鉴权接口，FCC不另指定。fetch/run和首次全参数获取始终同时刷新完整频道目录。

自动化使用 `configure --config-dir DIR --stdin`，由受保护stdin依上述顺序提供**12行**，前三行是身份，其余与交互顺序相同；空行保留，提前EOF报错且不替换旧配置。stdin模式是明确选择，输入不会回显；不要把身份放在argv或命令历史。配置保留未知JSON字段。旧JSON中的`directory_metadata`键兼容读取但忽略（包括false），fetch/convert不自动回写配置；主动configure或全参数保存时移除该旧键，新配置不写入。旧命令行`directory_metadata`键明确报错并提示无需设置，不写配置也不联网。旧13行stdin输入应删去末尾的目录开关行；新入口只消费12行，前三行身份和后续字段顺序不变。

这是新目录格式：不接受旧 `--instance`、`--directory`、`--identity-stdin` 或 `--m3u-output` 参数，不自动迁移、移动、删除或读取旧 `<instance>.auth.json`、`catchup_<instance>.json` 等文件。请在独立新目录运行configure，并显式fetch生成新格式；旧实例数据保持原状。旧配置里的输出覆盖设置不作为新输出规则使用。

fetch重新读取鉴权接口唯一全局IPv4，禁用环境代理并绑定已有接口。表单、重定向、门户目标受精确origin白名单限制：配置鉴权入口及已实证的 `http://124.75.29.176:7001`、`http://218.83.165.50:8084`、`http://218.83.165.36:8084`。额外地址须先核实再配置允许origin，不自动信任任意EPGDomain、IpPort或整个运营商网段。网络错误仅输出固定脱敏信息。

fetch不合并、不改名。目录阶段始终沿用同一认证cookie会话登录门户、保留DynamicAuthIP，调用12分类getChannelList，不下载逐节目EPG/XMLTV。每分类完整JSON响应（status/data/额外字段、分类及重复记录）包存在channels.json，不写回catchup。目录解析、分类、失败状态或空总目录异常均使fetch退出1并保留原有两文件。候选完成验证后分别原子替换，文件间不是事务；磁盘写入故障可能出现新目录与旧catchup，修复后需重跑。

名称优先原始ChannelName，其次按精确 `UserChannelID ↔ mixNo` 匹配目录；仅当不同name候选唯一时使用原文。HD、高清、4K、括号、前后及连续空格全部保留。不按mixNo去重原始频道。冲突候选保留在目录；原始名称缺失时，目录候选不唯一则统计ambiguous，无目录或未匹配则统计unmatched，均以精确ChannelID占位。convert打印source/matched/ambiguous/unmatched数量。分组字段保留在目录，M3U不推断分组；不得按复用回放地址猜名字。M3U无法安全表达的换行、双引号等使转换失败，不静默修改名称，保留上次主输出。

直播经 `/rtp/<组播IP:端口>`；有效FCC地址端口才追加 `fcc=IP:port`，缺失不写 `fcc=:`。鉴权/回放用interface，组播用multicast_interface（省略则沿用interface），FCC可另设fcc_interface，通过每请求r2h-ifname参数选择，不修改服务。回放经 `/rtsp/<主机:端口>/<路径>`，默认RTSP端口554；原始查询字符原样保留，包括百分号大小写、`+`、重复键，尾部追加playseek和接口参数。已有playseek或非空坏地址使整个convert失败。TimeShiftURL为null、空或字面null时写 `catchup="none"`。默认模板 `{utc:YmdHMS}-{utcend:YmdHMS}`，亦可配置播放器 `${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}`；不擅自增加8小时或启用Range。参考 [rtp2httpd URL](https://rtp2httpd.com/en/guide/url-formats)及 [M3U](https://rtp2httpd.com/en/guide/m3u-integration)，此前实测版本v3.16.0；本轮目录模型仅本机离线验证。

协议参考 [pcg562240/pcg-iptv固定提交38653f0](https://github.com/pcg562240/pcg-iptv/blob/38653f0bd722c4dececae434efa3593be97e8aca/shctiptv.py)。该文件自述参考 [denymz/sh-tel-iptv-spider](https://github.com/denymz/sh-tel-iptv-spider) 核心流程，此处记录其来源，未另行验证denymz实现。固定树无LICENSE，未复制其AES/客户端实现或宣称再许可；AES使用OpenSSL，表单/JS字符串/门户及转换独立实现。

本脚本不是YehBP菜单功能，需自行复制和适配。取得完整列表和目录后由用户自行格式化筛选、分组、排序或模板适配。身份、JSON和M3U可能含认证数据，不可提交Git或公开订阅。

维护测试在仓库tests：`python3 -m unittest discover -s tests -p 'test_iptv_reference_*.py' -v`。可选只读独立产物检查：`python3 tests/verify_iptv_artifacts.py /absolute/config/dir`，默认检查本目录playlist.m3u，也可用 `--m3u /absolute/export/playlist.m3u` 指定副本；不导入实现，仅报告数量/哈希，不输出身份或URL。
