# 独立 IPTV 参考脚本

IPTV 参考实现文件是 **`assets/iptv/iptv.py`**（本目录的 `iptv.py`），运行时只需这一个文件，可直接放在 rtp2httpd 目录，自行修改适配本地 IPTV。当前仅实现上海电信协议，不宣称各地区通用。Python 3.9+ 标准库及 OpenSSL CLI；真实 fetch 需已接通 IPTV 的 Linux、iproute2 和 SO_BINDTODEVICE 权限（通常 root）。macOS 可配置、离线转换和测试。

```sh
python3 iptv.py configure --instance shct \
  --interface enp3s0.85 --multicast-interface enp3s0.51 --fcc-interface enp3s0.85 \
  --base-url http://127.0.0.1:5140 --directory-metadata \
  --m3u-output /your/existing/directory/channels.m3u
python3 iptv.py fetch --instance shct
python3 iptv.py convert --instance shct
# run 依次 fetch、convert；CLI 输出路径覆盖已保存配置
python3 iptv.py run --instance shct --m3u-output /your/existing/directory/another.m3u
```

仅三个功能：配置、获取完整源数据、转换 rtp2httpd M3U；`run` 是后两者的顺序快捷方式。无菜单、服务、定时、自动网络配置或发布。取得 rtp2httpd 列表与频道信息后，由用户自行格式化筛选、分组、排序或模板适配。

未指定 `--directory` 时，身份及数据保存在 **iptv.py 所在目录**，不随 cwd 改变；也可为所有命令指定 `--directory /existing/parent/private`。目录必须专用，因为操作会设为0700。实例文件为 `<instance>.auth.json`、`<instance>.lock`、`catchup_<instance>.json`、`channels_<instance>.json`，均0600。未配置 M3U 输出时默认同目录 `raw_<instance>.m3u`。`configure --m3u-output` 保存绝对路径；`convert/run --m3u-output` 只覆盖本次输出。相对 CLI 路径按调用时 cwd 解析。输出父目录必须存在，不允许覆盖本实例配置、原始数据、目录、锁或运行脚本。

`configure` 隐藏询问 UserID、盒子独立 SN 和盒子 MAC，空输入保留已有值；不能用 STBID 或主机网卡 MAC 替代。自动化可用 `--identity-stdin` 从受保护 stdin 依次输入三行，禁止放在 argv/history。配置命令还支持 `--auth-origin`、可重复 `--allow-origin`、`--base-url`、`--live-path`、`--replay-path`、`--seek-template`。省略设置保留旧值；`--no-directory-metadata` 关闭今后 fetch 的目录子阶段。目录默认未启用；已保存的目录仍可供离线 convert 使用，需明确移走元数据文件才完全不使用它。

fetch 重新读取鉴权接口唯一全局 IPv4，禁用环境代理并绑定已有接口。表单、重定向、门户目标均受精确 origin 白名单限制：配置鉴权入口及已实证的 `http://124.75.29.176:7001`、`http://218.83.165.50:8084`、`http://218.83.165.36:8084`。额外地址须先核实再用 `--allow-origin`；不会自动信任任意 EPGDomain、IpPort 或整个运营商网段。网络错误只输出固定脱敏信息。

`catchup_<instance>.json` 保存完整解析 channelArray，字段、顺序、重复不丢失，不合并、不改名。可选目录子阶段沿用同一认证 cookie 会话登录门户，保留 DynamicAuthIP，调用12个分类的 getChannelList；不下载逐节目 EPG/XMLTV。每个分类的完整原始 JSON 响应（包括 status/data/额外字段、原始分类及重复记录）单独包存在 `channels_<instance>.json`，不写回 catchup。响应解析/分类结构/失败状态/空总目录任一异常都使 fetch exit1，原有原始与目录文件不变，run 不执行 convert。成功 exit0。两个候选先完成验证，再各自同目录原子替换；文件间不是跨文件事务，磁盘写入故障可能出现新目录与旧 catchup，需修复后重跑。convert 失败 exit1，保留上次 M3U；run 的两个阶段也分别原子。

名称优先原始 ChannelName，其次仅按精确 `UserChannelID ↔ mixNo` 匹配目录；不同目录 name 只有一个时使用其原文。HD、高清、4K、括号、前后及连续空格全部保留，不 compact_name。不按 mixNo 去重原始频道。多个不同 name 候选完整保留在目录，统计 ambiguous 并以精确 ChannelID 占位；无目录或未匹配统计 unmatched 并占位。convert 打印 source/matched/ambiguous/unmatched 数量。源端分组字段保留在目录，不照搬上游 group_for；M3U 不推断分组，未确认分组为“待核查”。不得按复用的回放地址猜名字。M3U 文本无法安全表达的换行、双引号等使转换失败，不静默修改名称。

直播经 `/rtp/<组播IP:端口>`；有效 FCC 地址端口才追加 `fcc=IP:port`，缺失不写 `fcc=:`。鉴权/回放用 `--interface`，组播用 `--multicast-interface`（省略时用 interface），FCC 可另设 `--fcc-interface`，通过每请求 r2h-ifname 参数选择，不修改服务。回放经 `/rtsp/<主机:端口>/<路径>`，默认 RTSP 端口554。原有查询字符原样保留，包括百分号大小写、`+`、重复键，尾部追加 playseek 和接口参数。已有 playseek 或非空坏地址使整个 convert 失败。TimeShiftURL 为 null、空、字面 null 时写 catchup="none"。默认模板 `{utc:YmdHMS}-{utcend:YmdHMS}`，亦可显式选播放器 `${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}`；时间语义由播放器与运营商决定，不擅自增加8小时或启用 Range。规则参考 [rtp2httpd URL](https://rtp2httpd.com/en/guide/url-formats)及 [M3U](https://rtp2httpd.com/en/guide/m3u-integration)，实测版本 v3.16.0。

协议参考 [pcg562240/pcg-iptv 固定提交 38653f0](https://github.com/pcg562240/pcg-iptv/blob/38653f0bd722c4dececae434efa3593be97e8aca/shctiptv.py)。该文件自述再参考 [denymz/sh-tel-iptv-spider](https://github.com/denymz/sh-tel-iptv-spider) 的核心流程；此处按原声明记录来源，未另行验证 denymz 的实现。固定树无 LICENSE，未复制其 AES/客户端实现或宣称再许可；AES 使用 OpenSSL，表单/JS字符串/门户及转换均独立实现。鉴权基础来自此前本机 YehBP 独立 helper，本文件已去除其菜单、服务和定时依赖。

本脚本不是 YehBP 菜单功能，需自行复制和适配本地。完整数据及目录不会筛选、合并，取得列表与频道信息后由用户自行整理。JSON、目录和 M3U 可能含身份或认证数据，应保存在私有目录，不可提交 Git 或公开订阅。原独立项目保留；仓库不包含实际验证数据、日志或私有配置。

维护测试在仓库 `tests/test_iptv_reference_*.py`，从仓库根目录执行 `python3 -m unittest discover -s tests -p "test_iptv_reference_*.py" -v`。可选只读独立产物检查器：`python3 tests/verify_iptv_artifacts.py /absolute/private/dir --instance shct --m3u /absolute/list.m3u`；不导入实现，仅报告数量和文件哈希，不输出频道 URL 或身份。
