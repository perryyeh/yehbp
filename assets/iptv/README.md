# IPTV 频道抓取参考

本参考代码已在上海电信测试通过，其他运营商需自行适配。请在已接通 IPTV 的 Linux 设备上以 root 运行，需 Python 3.9+ 和 OpenSSL。

下载 iptv.py 后运行，使用方法及参数说明见下文。身份请换成自己机顶盒的 UserID、SN 和 MAC。

下载脚本：

```sh
curl -fL https://raw.githubusercontent.com/perryyeh/yehbp/main/assets/iptv/iptv.py -o iptv.py
```

首次使用，保存、获取并生成 `tel/playlist.m3u`：

```sh
python3 iptv.py config=tel userid=YOUR_USER_ID sn=YOUR_BOX_SN mac=YOUR_BOX_MAC \
  interface=iptv85 rtp2http=http://127.0.0.1:5140
```

修改参数也一样：只写要改的字段，其余保留，但仍会获取并生成：

```sh
python3 iptv.py config=tel rtp2http=http://127.0.0.1:5140
```

重新获取频道和回看授权，再生成列表：

```sh
python3 iptv.py run config=tel
```

用已有数据直接生成列表，不联网：

```sh
python3 iptv.py config=tel
```

更新节目单并重新生成列表。这和参数 `epg=` 不同：裸命令从运营商取节目；`epg=` 只写入第三方链接，写入时仍会获取并生成。

```sh
python3 iptv.py epg config=tel
```

```sh
python3 iptv.py config=tel epg=https://example.com/epg.xml
```

重测当前全部直播分辨率，不生成列表。已有结果直接看文件：

```sh
python3 iptv.py resolution config=tel
cat tel/resolution.json
```

命令本身不是 `key=value`。`run`、`epg`、`resolution` 不能和身份或播放参数写在一起；要改配置就直接写字段，不写这些命令。下表是命令行参数名。

| 参数 | 必填 | 默认 | 作用 |
|---|---|---|---|
| `config` | 可选 | 首次可省略，改用 `interface` 的值作目录名 | 配置目录。已有配置后每次都要写。相对路径相对脚本所在目录 |
| `userid` | 首次必填 | 之后读取已保存的值 | 机顶盒 UserID。只改这一项时只写它 |
| `sn` | 首次必填 | 之后读取已保存的值 | 机顶盒 SN。只改这一项时只写它 |
| `mac` | 首次必填 | 之后读取已保存的值 | 机顶盒 MAC。只改这一项时只写它 |
| `interface` | 首次必填 | 之后读取已保存的值；省略 `config` 时也用它作目录名 | 鉴权网卡。只用于登录，不会自动填到播放地址 |
| `rtp2http` | 可选 | `http://127.0.0.1:5140` | rtp2httpd 访问地址。直播和回看都拼在它后面 |
| `multicast` | 可选 | 空，不写入播放地址 | 组播网卡。只填组播时 FCC 不自动跟随 |
| `fcc` | 可选 | 空，不写入播放地址 | FCC 网卡。需要和组播分开时单独填写 |
| `authurl` | 可选 | `http://222.68.208.73:7001` | 鉴权地址，空格分隔。第一个是登录入口，其余只允许跳转 |
| `live` | 可选 | `/rtp/` | 直播代理路径，接在 `rtp2http` 后面，再接组播地址 |
| `replay` | 可选 | `/rtsp/` | 回看代理路径，接在 `rtp2http` 后面，再接回看地址 |
| `seek` | 可选 | `${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}` | 回放时间格式。已保存的值会保留，直到显式再传 `seek` |
| `epg` | 可选 | 不填时保留已保存的第三方地址；没有第三方地址且已有本地 `epg.xml` 时，列表头引用该文件；两者都没有则头部不写引用 | 第三方 EPG 的 http(s) 链接。和裸命令 `epg` 不同，不从运营商取节目 |
| `output` | 可选 | 不额外导出，只生成配置目录里的 `playlist.m3u` | 额外一份列表，可用于 `run`、`epg` 和离线生成，不能用于 `resolution`。以 `.m3u` 结尾是文件，否则是目录（没有则创建），目录里的文件名为 `<配置目录名>.m3u`。相对路径按当前工作目录解析。导出到其他目录时同时复制一份 `epg.xml` |
| `filter` | 可选 | 见下文 | 只能写 `true` 或 `false`，只改开关 |

`config.json` 的可设字段与这些参数同名。

分辨率检测优先使用 `ffprobe`，其次 `ffmpeg`。`run` 复用已有结果；需要重新检测时执行 `resolution config=tel`。过滤条件在 `config.json` 的 `filter` 中配置。

配置参考：[tel/config.json](tel/config.json)。

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
  "filter": {"enabled": true, "min_height": 720, "exclude_names": ["购物", "理财", "聚鲨"], "exclude_channels": ["YOUR_CHANNEL_ID"]},
  "channel_names": {"YOUR_CHANNEL_ID": "显示名"}
}
```

`exclude_channels` 和 `channel_names` 的键都是完整 ChannelID。

可选 `name_cleanup` 只改播放列表的显示名和分组。没有该字段时，名称和顺序保持原样，也不写 `group-title`。`remove` 是要去掉的字面标记；`groups` 数组顺序就是分组的输出顺序。组内按配置的名称顺序排列，不按输入原序：有 `names` 的组用这份名单，只有 `contains` 或没有名单的组用可选 `order`。名单里没有的频道留在该组末尾，仍按原相对顺序。同名多条源保持彼此的原相对顺序，不合并。`contains` 先于 `names` 匹配，所以带 4K 的频道不会被前面的名单分走；4K 自己的顺序写在 `order`，不改这条优先规则。只有分组名的最后一项是其余频道。

| 字段 | 作用 |
|---|---|
| `remove` | 去掉这些标记。英文按独立词去掉，`高清` 按字面去掉；去掉后为空则保留原名 |
| `groups[].name` | 分组名，数组顺序即播放列表的分组顺序 |
| `groups[].names` | 清理后的完整显示名，精确匹配，同时是该组的输出顺序 |
| `groups[].contains` | 名称包含即归入，不区分大小写，优先于名单 |
| `groups[].order` | 组内输出顺序。`contains` 组用它排序；未列出的名称留在组尾 |

```json
"name_cleanup": {
  "remove": ["HD", "高清"],
  "groups": [
    {"name": "央视", "names": ["CCTV1综合"]},
    {"name": "卫视", "names": ["东方卫视"]},
    {"name": "4K", "contains": "4K", "order": ["CCTV4K"]},
    {"name": "上海台", "names": ["新闻综合"]},
    {"name": "其它"}
  ]
}
```

完整名单见 [tel/config.json](tel/config.json)。

协议参考 [pcg562240/pcg-iptv](https://github.com/pcg562240/pcg-iptv)。

DIYP 适配见 [README-diyp.md](README-diyp.md)。
