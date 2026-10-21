# IPTV 频道抓取参考

本参考代码已在上海电信测试通过，其他运营商需自行适配。请在已接通 IPTV 的 Linux 设备上以 root 运行，需 Python 3.9+ 和 OpenSSL。

下载 iptv.py 后运行，使用方法及参数说明见下文。身份请换成自己机顶盒的 UserID、SN 和 MAC。

下载脚本：

```sh
curl -fL https://raw.githubusercontent.com/perryyeh/yehbp/main/assets/iptv/iptv.py -o iptv.py
```

```sh
# 首次或修改：保存、获取并生成 tel/playlist.m3u。改一项时只写要改的字段
python3 iptv.py config=tel userinfo=YOUR_USER_ID SN=YOUR_BOX_SN MAC=YOUR_BOX_MAC \
  interface=iptv85 rtp2http=http://127.0.0.1:5140

# 重新获取并生成
python3 iptv.py run config=tel

# 重新获取、生成并导出
python3 iptv.py run config=tel output=/your/export/channels.m3u

# 用已有数据离线生成；output 可选
python3 iptv.py config=tel
python3 iptv.py config=tel output=exports
python3 iptv.py config=tel output=/path/channels.m3u
```

`output=` 可指定 `.m3u` 文件或导出目录；目录内文件名为 `<config>.m3u`。省略时仅生成配置目录中的 `playlist.m3u`。

| 参数 | 作用 |
|---|---|
| `config` | 配置目录；省略时使用 interface 的值作为目录名。相对路径相对脚本目录 |
| `output` | 额外一份列表 |
| `userinfo` / `SN` / `MAC` | 机顶盒身份，首次必填 |
| `interface` | 鉴权网卡，首次必填 |
| `rtp2http` | rtp2httpd访问地址，默认 `http://127.0.0.1:5140` |
| `multicast_interface` / `fcc_interface` | 播放接口覆盖；默认不添加，使用rtp2httpd实例配置。只填组播时FCC可能跟着回退，需要分开时两个一起填 |
| `auth` | 鉴权地址；多个以空格分隔，第一个作为入口，其余允许跳转。默认 `http://222.68.208.73:7001` |
| `live` / `replay` | 直播/回放路径，默认 `/rtp/`、`/rtsp/` |
| `seek` | 回放时间格式，默认 `${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}`；已保存配置保留原值，可用 `seek` 显式更新 |
| `epg_url` | 可选。播放器可访问的 XMLTV 地址，例如 `http://访问地址/epg.xml`。不填且已有 `epg.xml` 时，列表头使用相对地址 `epg.xml` |

只写 `config`（可再加 `output`）时不联网，也不测量分辨率。`run` 会联网并刷新频道和回看地址；只在频道新增、直播地址变化或没有分辨率记录时探测直播，结果写入同目录 `resolution.json`。`epg` 只更新节目单：先用 `session.json` 里的门户会话，失效才重新登录；它不改 `catchup.json`，不探测分辨率，也不保证旧回看地址仍然有效。节目单写入同一配置目录的 `epg.xml`。已有该文件时，离线生成的 `playlist.m3u` 头部为 `x-tvg-url="epg.xml"`。播放器不能读这个相对路径时，用 `epg_url=http://访问地址/epg.xml` 写入可访问地址；`output` 导出到其他目录时会同时复制一份 `epg.xml`。身份写在命令里会被 shell 记住。节目单需要新内容时手动再执行 `epg`，本脚本不安排定时任务。

生成列表时读取 `config.json` 里的 `filter` 对象。默认 `enabled` 为 true，`min_height` 为 720，`exclude_names` 为“购物”“理财”“聚鲨”。高度低于 `min_height`，或名称含任一排除词，就不写入列表。没有分辨率记录、指纹对不上、状态不是 identified 或 played、宽或高缺失或不是正整数时，筛选按高度 0 比较；这只用于当次比较，不改 `resolution.json`。一次失败不代表频道永久不可播，重新测量后按新记录再比。`catchup.json` 和 `channels.json` 仍保存全量原始记录。`resolution.json` 只记录实测宽高、编码、状态和时间，不含是否排除。

可选 `channel_names` 写在同一份 `config.json`。键是完整 `ChannelID`，值是显示名，只按完整键精确匹配。生成列表时它优先于原始频道名、门户目录名和数字兜底，并在过滤前生效，因此映射后的名称也会参与 `exclude_names`。它不改 `ChannelID`、`tvg-id`、`catchup.json` 或 `channels.json`。没有该字段时行为与原来相同。示例只说明格式，不是频道清单：

```json
"channel_names": {"YOUR_CHANNEL_ID": "显示名"}
```

要按频道排除而不是按名称时，在 `filter` 里增加可选 `exclude_ids`，每项必须是完整 `ChannelID`。它在名称和高度判断之前生效，只影响生成的列表，不删除原始记录。`enabled` 为 false 时，名称、高度和 `exclude_ids` 全部不生效。没有 `exclude_ids` 时保持原来的过滤。

首次保存写入完整默认对象。已有配置若仍是 `true`/`false` 或缺少该字段，`run` 或 `fetch` 补成对象，并保留原来的开关。已保存的 `min_height` 和 `exclude_names` 不因开关被覆盖。离线生成、`epg`、`resolution` 不改配置。下面两条只改 `enabled`，不重新登录，也不探测：

```sh
python3 iptv.py config=tel filter=false
python3 iptv.py config=tel filter=true
```

要重新测量当前全部直播，另执行：

```sh
python3 iptv.py resolution config=tel
```

需要本机 `ffprobe`；没有时改用 `ffmpeg`。两者都没有时保留已有记录并继续生成列表，不自动安装。

```sh
python3 iptv.py epg config=tel
```

协议参考 [pcg562240/pcg-iptv](https://github.com/pcg562240/pcg-iptv)。

DIYP 适配见 [README-diyp.md](README-diyp.md)。
