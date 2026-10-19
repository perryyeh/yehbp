# 独立 IPTV 参考脚本

只需复制 **`iptv.py`** 一个文件。当前只支持上海电信，不是 YehBP 菜单功能。需要 Python 3.9+ 和 OpenSSL；真正向运营商获取频道时，还需要已接通 IPTV 的 Linux、对应网卡和 root。macOS 可以用已有数据生成列表。

`config=tel` 是脚本旁边的 `tel` 目录，不看你在哪个目录运行。身份请换成自己机顶盒的 UserID、SN 和 MAC。

```sh
# 首次或修改：保存、获取并生成 tel/playlist.m3u。改一项时只写要改的字段
python3 iptv.py config=tel userinfo=YOUR_USER_ID SN=YOUR_BOX_SN MAC=YOUR_BOX_MAC \
  interface=iptv85 baseURL=http://127.0.0.1:5140

# 重新获取并生成
python3 iptv.py run config=tel

# 用已有数据离线生成；output 可选
python3 iptv.py config=tel
python3 iptv.py config=tel output=exports
python3 iptv.py config=tel output=/path/channels.m3u
```

以 `.m3u` 结尾的 `output` 是文件。其他 `output` 是目录，`config=tel` 时写入 `exports/tel.m3u`。不写 `output` 就只生成配置目录里的 `playlist.m3u`。目录不存在会新建；建不了时仍保留主列表。同名播放列表直接覆盖。

| 参数 | 作用 |
|---|---|
| `config` | 配置目录。首次可省略，此时目录名用 `interface` |
| `output` | 额外一份列表 |
| `userinfo` / `SN` / `MAC` | 机顶盒身份，首次必填 |
| `interface` | 鉴权/回放网卡，首次必填 |
| `baseURL` | rtp2httpd 地址，默认 `http://127.0.0.1:5140` |
| `multicast_interface` / `fcc_interface` | 组播、FCC 网卡，可省略 |
| `auth_origin` | 鉴权地址，默认 `http://222.68.208.73:7001` |
| `live_path` / `replay_path` | 直播/回放路径，默认 `/rtp/`、`/rtsp/` |
| `seek_template` | 回放时间格式 |
| `allowed_origins` | 额外允许的鉴权地址，多个用空格分开 |

只写 `config`（可再加 `output`）时不联网。`run` 会联网。命令里出现身份或其他配置字段时，保留没写的旧字段，保存后获取并生成；首次缺身份不会留下半份配置。身份写在命令里会被 shell 记住。

协议参考 [pcg562240/pcg-iptv 固定提交 38653f0](https://github.com/pcg562240/pcg-iptv/blob/38653f0bd722c4dececae434efa3593be97e8aca/shctiptv.py)；该文件写明参考 [denymz/sh-tel-iptv-spider](https://github.com/denymz/sh-tel-iptv-spider)，这里只记录来源。固定提交没有许可证，本脚本没有复制其实现。身份、JSON 和 M3U 不要提交到 Git 或公开订阅。
