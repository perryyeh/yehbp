# IPTV 频道抓取参考

本参考代码已在上海电信测试通过，其他运营商需自行适配。请在已接通 IPTV 的 Linux 设备上以 root 运行，需 Python 3.9+ 和 OpenSSL。

下载 iptv.py 后运行，使用方法及参数说明见下文。身份请换成自己机顶盒的 UserID、SN 和 MAC。

下载脚本：

```sh
curl -fL https://raw.githubusercontent.com/perryyeh/yehbp/main/assets/iptv/iptv.py -o iptv.py
```

```sh
# 首次或修改：保存、获取并生成 tel/playlist.m3u。改一项时只写要改的字段
python3 iptv.py config=tel userid=YOUR_USER_ID sn=YOUR_BOX_SN mac=YOUR_BOX_MAC \
  interface=iptv85 rtp2http=http://127.0.0.1:5140

# 重新获取并生成
python3 iptv.py run config=tel

# 重新获取、生成并导出
python3 iptv.py run config=tel output=/your/export/playlist.m3u

# 用已有数据离线生成；output 可选
python3 iptv.py config=tel
python3 iptv.py config=tel output=exports
python3 iptv.py config=tel output=/path/playlist.m3u

# 只更新节目单，并重新生成列表
python3 iptv.py epg config=tel

# 重新测量当前全部直播，不生成列表
python3 iptv.py resolution config=tel
```

`output=` 可指定 `.m3u` 文件或导出目录；目录内文件名为 `<config>.m3u`。相对路径按当前工作目录解析。省略时仅生成配置目录中的 `playlist.m3u`。`config` 的相对路径则相对脚本所在目录。

命令本身不是 `key=value`。`run`、`epg`、`resolution` 不能和身份或播放参数写在一起；要改配置就直接写字段，不写这些命令。`fetch`、`convert`、`configure` 仍可用，日常用上面的入口即可。下表是命令行参数名。`multicast`、`fcc`、`authurl` 写入配置后仍分别是 `multicast_interface`、`fcc_interface`、`auth_origin`；旧参数名不再接受。

| 参数 | 必填 | 默认 | 作用 |
|---|---|---|---|
| `config` | 可选 | 首次可省略，改用 `interface` 的值作目录名 | 配置目录。已有配置后每次都要写 |
| `userid` | 首次必填 | 之后读取已保存的值 | 机顶盒 UserID。只改这一项时只写它 |
| `sn` | 首次必填 | 之后读取已保存的值 | 机顶盒 SN。只改这一项时只写它 |
| `mac` | 首次必填 | 之后读取已保存的值 | 机顶盒 MAC。只改这一项时只写它 |
| `interface` | 首次必填 | 之后读取已保存的值；省略 `config` 时也用它作目录名 | 鉴权网卡。只用于登录，不会自动填到播放地址 |
| `rtp2http` | 可选 | `http://127.0.0.1:5140` | rtp2httpd 访问地址。直播和回看都拼在它后面 |
| `multicast` | 可选 | 空，不写入播放地址 | 组播网卡。只填组播时 FCC 不自动跟随 |
| `fcc` | 可选 | 空，不写入播放地址 | FCC 网卡。需要和组播分开时单独填写 |
| `authurl` | 可选 | `http://222.68.208.73:7001` | 鉴权地址。多个用空格分隔，第一个是入口，其余只允许跳转 |
| `live` | 可选 | `/rtp/` | 直播代理路径，接在 `rtp2http` 后面，再接组播地址 |
| `replay` | 可选 | `/rtsp/` | 回看代理路径，接在 `rtp2http` 后面，再接回看地址 |
| `seek` | 可选 | `${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}` | 回放时间格式。已保存的值会保留，直到显式再传 `seek` |
| `epg` | 可选 | 不填时保留已保存的第三方地址；没有第三方地址且已有本地 `epg.xml` 时，列表头引用该文件；两者都没有则头部不写引用 | 第三方 EPG 链接；不填使用自己已有 epg.xml。只接受 http(s) |
| `output` | 可选 | 不导出 | 额外一份列表。可用于离线生成、`run`、`epg`；不能用于 `resolution` |
| `filter` | 可选 | 见下文 | 只能写 `true` 或 `false`，只改开关 |

只写 `config`（可再加 `output` 或 `filter`）时不联网，也不测量分辨率。带上表中的身份或播放字段时，会合并已有配置、保存、获取并生成。`run` 会联网并刷新频道和回看地址；只在频道新增、直播地址变化或没有分辨率记录时探测直播，结果写入同目录 `resolution.json`。`epg` 只更新节目单：先用 `session.json` 里的门户会话，失效才重新登录；它不改 `catchup.json`，不探测分辨率，也不保证旧回看地址仍然有效。节目单写入同一配置目录的 `epg.xml`。已有该文件时，生成的 `playlist.m3u` 头部为 `x-tvg-url="epg.xml"`。播放器不能读这个相对路径时，用 `epg=http://访问地址/epg.xml` 写入可访问地址；不填不会清掉已经保存的第三方地址，也不会在没有本地 `epg.xml` 时编造引用。旧参数 `epg_url` 不再接受。`output` 导出到其他目录时会同时复制一份 `epg.xml`。身份写在命令里会被 shell 记住。节目单需要新内容时手动再执行 `epg`，本脚本不安排定时任务。

生成列表时读取 `config.json` 里的 `filter` 对象。首次保存写入完整默认对象：`enabled` 为 true，`min_height` 为 720，`exclude_names` 为“购物”“理财”“聚鲨”。高度低于 `min_height`，或名称含任一排除词，就不写入列表。没有分辨率记录、指纹对不上、状态不是 identified 或 played、宽或高缺失或不是正整数时，筛选按高度 0 比较；这只用于当次比较，不改 `resolution.json`。一次失败不代表频道永久不可播，重新测量后按新记录再比。`catchup.json` 和 `channels.json` 仍保存全量原始记录。`resolution.json` 只记录实测宽高、编码、状态和时间，不含是否排除。

可选 `channel_names` 写在同一份 `config.json`，没有命令行参数。键是完整 `ChannelID`，值是显示名，只按完整键精确匹配。生成列表时它优先于原始频道名、门户目录名和数字兜底，并在过滤前生效，因此映射后的名称也会参与 `exclude_names`。它不改 `ChannelID`、`tvg-id`、`catchup.json` 或 `channels.json`。没有该字段时行为与原来相同。示例只说明格式，不是频道清单：

```json
"channel_names": {"YOUR_CHANNEL_ID": "显示名"}
```

要按频道排除而不是按名称时，在 `filter` 里增加可选 `exclude_ids`，每项必须是完整 `ChannelID`。它在名称和高度判断之前生效，只影响生成的列表，不删除原始记录。`enabled` 为 false 时，名称、高度和 `exclude_ids` 全部不生效。没有 `exclude_ids` 时保持原来的过滤，保存时也不会补上空列表。

已有配置若仍是 `true`/`false` 或缺少该字段，`run` 或 `fetch` 补成对象，并保留原来的开关。已保存的 `min_height` 和 `exclude_names` 不因开关被覆盖。离线生成、`epg`、`resolution` 不改配置。下面两条只改 `enabled`，不重新登录，也不探测：

```sh
python3 iptv.py config=tel filter=false
python3 iptv.py config=tel filter=true
```

需要本机 `ffprobe`；没有时改用 `ffmpeg`。两者都没有时保留已有记录并继续生成列表，不自动安装。

协议参考 [pcg562240/pcg-iptv](https://github.com/pcg562240/pcg-iptv)。

DIYP 适配见 [README-diyp.md](README-diyp.md)。
