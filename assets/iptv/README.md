# IPTV 参考脚本

本脚本为参考代码，已在上海电信环境下测试通过，其他运营商需自行修改适配。需要 Python 3.9+ 和 OpenSSL；真正向运营商获取频道时，还需要已接通 IPTV 的 Linux、对应网卡和 root。macOS 可以用已有数据生成列表。

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

以 `.m3u` 结尾的 `output` 是文件。其他 `output` 是目录，`config=tel` 时写入 `exports/tel.m3u`。不写 `output` 就只生成配置目录里的 `playlist.m3u`。目录不存在会新建；建不了时仍保留主列表。同名播放列表直接覆盖。

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

只写 `config`（可再加 `output`）时不联网。`run` 会联网。命令里出现身份或其他配置字段时，保留没写的旧字段，保存后获取并生成；首次缺身份不会留下半份配置。身份写在命令里会被 shell 记住。

协议参考 [pcg562240/pcg-iptv](https://github.com/pcg562240/pcg-iptv)。
