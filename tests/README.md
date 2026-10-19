# YehBP 测试

长期测试统一维护在本仓库 `tests/`，随对应功能修改和提交。测试从自身文件位置定位仓库，临时文件使用 `TMPDIR`（未设置时使用 Python `tempfile` 默认目录），不依赖 Hermes、个人 HOME、外部测试脚本或历史验证日志。

## 运行

从 clone 后的仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
```

依赖 Python 3 标准库、Bash 和常见 Unix 命令（包括 `cp`、`stat`、`mktemp`、`grep`、`sed` 等）；无需 pip 包。迁移元数据用例会实际调用 `/usr/bin/rsync -aH --numeric-ids`，因此需要该路径的 rsync。支持本地 macOS / Linux 验证；macOS 自带 Bash 3 的 wrapper 测试提供了有限的 `mapfile` 兼容夹具。不要用 `sudo` 运行测试。

运行单组测试：

```sh
python3 -m unittest discover -s tests -p 'test_docker_datadir_migration.py' -v
```

需要隔离 HOME 和临时数据时，可在自己选择的可写工作目录中执行：

```sh
sandbox=$(mktemp -d "$PWD/.test-sandbox.XXXXXX")
(
    trap 'rm -rf "$sandbox"' EXIT
    mkdir "$sandbox/home"
    export HOME="$sandbox/home" TMPDIR="$sandbox" PYTHONDONTWRITEBYTECODE=1
    unset MOSDNS_TEST_BINARY
    python3 -m unittest discover -s tests -v
)
```

脚本语法检查也从仓库直接运行：

```sh
bash -n install.sh
bash -n assets/docker-auto-update/docker-auto-update.sh
bash -n assets/iptv/rtp2httpd.sh
```

## 覆盖与隔离边界

| 测试文件 | 覆盖 |
|---|---|
| `test_adguardhome_upstreams.py` | 上游替换、交互选择、配置及权限保留 |
| `test_mosdns_isp_upstream.py` | ISP DNS 渲染、阈值、输入与失败处理；可选真实启动 |
| `test_mihomo_subscription_delete.py` | 删除订阅元数据、保留配置、锁与失败处理 |
| `test_dockcheck_dependencies.py` | OpenWrt 包管理与 xargs/flock 依赖选择 |
| `test_docker_datadir_migration.py` | 迁移前置校验、复制失败、回滚、JSON 精度与沙箱元数据保留 |
| `test_iptv_profile_deletion.py` | NetworkManager UUID 所有权校验、删除失败及状态保留 |
| `test_iptv_reference_*.py` | 独立单文件参考脚本：三步协议、完整数据、目录补名及歧义、HD/高清/4K 保留、M3U 查询串、可配输出路径、权限与失败保护（21 项） |
| `test_compose_mac_check.py` | Compose 文件/项目/环境恢复、MAC 覆盖检查、wrapper 退出状态 |
| `test_remaining_fixes.py` | 安装退出状态、VLAN 输入、依赖失败、Compose 私有临时文件与信号清理 |
| `test_repository_consistency.py` | VERSION 与 APP_VERSION 一致、README 数字菜单同步 |

默认套件只执行提取的生产函数或加载受测模块，在临时目录操作夹具；Docker、systemd、NetworkManager、包管理及下载操作由 mock 替代，不运行完整安装入口、不需要真实服务、SSH 或 root。迁移测试实际复制沙箱文件并比较权限、UID/GID、硬链接和符号链接；生产 rsync 的 `-aHAX --numeric-ids` 参数另有断言。macOS 的真实复制仅使用支持的 `-aH --numeric-ids`，不能据此声称 Linux ACL/xattr 已通过集成测试。

## 跳过条件与真实集成

- 未设置 `MOSDNS_TEST_BINARY` 时，真实 MosDNS 启动用例跳过。这是默认离线运行方式。
- 主机仍可读取 mode-000 文件时（如 root 或特定 macOS 权限行为），不可读 IPTV 状态文件用例跳过；测试输出会显示原因。
- 若需可选启动测试，提供兼容的 MosDNS 可执行文件绝对路径：`MOSDNS_TEST_BINARY=/path/to/mosdns python3 -m unittest discover -s tests -p 'test_mosdns_isp_upstream.py' -v`。它会启动本地进程并绑定回环随机端口，检查配置可加载，不验证实际 DNS 查询结果。

真实 Linux/OpenWrt 安装、Docker 数据迁移及 ACL/xattr、systemd/procd、NetworkManager 删除、Compose 版本兼容和真实 DNS/路由链路，需要在专用可恢复环境另行手工验证；默认套件通过只代表上述沙箱回归通过。不要为跑本套件安装 YehBP 或接入生产设备。

新增长期场景应放入对应 `test_*.py`，用相对仓库定位与 `tempfile` 管理夹具，保留真实断言及失败退出状态。一次性审查输出和历史 JSON/日志属于验证证据，不是测试入口，也不应作为测试依赖。

独立 IPTV 产物的可选只读校验入口为 `tests/verify_iptv_artifacts.py`，用法见 `assets/iptv/README.md`；不依赖历史日志或私有配置，默认测试不访问运营商网络。
