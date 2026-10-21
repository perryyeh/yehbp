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

命令本身不是 `key=value`。`run`、`epg`、`resolution` 不能和身份或播放参数写在一起；要改配置就直接写字段，不写这些命令。下表是命令行参数名。

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

`config.json` 的可设字段与这些参数同名。多个 `authurl` 时，第一个写入 `authurl`，其余写入 `allowed_origins`；`provider` 不是命令参数。

只写 `config`（可再加 `output` 或 `filter`）时不联网。`run` 刷新频道和回看。`epg` 只更新节目单，不刷新回看。`resolution` 重测当前全部直播。

分辨率优先用本机已有的 `ffprobe`，没有再用 `ffmpeg`。两个都没有时，这次测到的源记为不可用，不安装工具。`run` 只测新增频道、直播地址变了、或还没有记录的源；已有记录直接用，已经缓存的失败也要等 `resolution` 才重测。只写 `config` 的离线生成只读 `resolution.json`，不测。`resolution` 重测当前全部直播，不生成列表。只有 `ffprobe` 会看同一路流里的多路视频，取第一路宽高都大于 0 的，前面的 `0x0` 忽略；没有 `ffprobe` 时，`ffmpeg` 只解第一路视频。缓存只存实测结果；过滤另看 `config.json` 的 `filter`。没有可用高度时，筛选按 0 和 `filter.min_height` 比，默认低于 720 排除。关过滤仍保留全部频道。

## 生成文件

都在配置目录里。

| 文件 | 内容 |
|---|---|
| `config.json` | 配置 |
| `session.json` | 会话 |
| `catchup.json` | 原始直播和回看 |
| `channel.json` | 原始频道目录 |
| `resolution.json` | 频道分辨率检测结果 |
| `playlist.m3u` | 播放列表 |
| `epg.xml` | 节目单，执行 `epg` 时生成 |

`filter` 在 `config.json` 里。默认排除高度低于 720，或名称含“购物”“理财”“聚鲨”的频道。`filter=false` 只关开关。可选字段：

```json
{
  "filter": {"enabled": true, "min_height": 720, "exclude_names": ["购物", "理财", "聚鲨"], "exclude_ids": ["YOUR_CHANNEL_ID"]},
  "channel_names": {"YOUR_CHANNEL_ID": "显示名"}
}
```

`exclude_ids` 和 `channel_names` 的键都是完整 ChannelID。

协议参考 [pcg562240/pcg-iptv](https://github.com/pcg562240/pcg-iptv)。

DIYP 适配见 [README-diyp.md](README-diyp.md)。
