# tcpfit

按每台机器实测推导的 TCP 调优工具. 不套用固定参数, 实测 BDP 与限速器拐点.

本脚本由 [kylin010](https://github.com/Kylin010) 编写和维护.

## 安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Kylin010/tcpfit/main/tcpfit.sh)
```

跑完直接出菜单, 选 1 全自动. 脚本会装到 `/usr/local/bin/tcpfit`, 以后敲 `tcpfit` 即可.

## 三种用法

| 用法 | 命令 |
|---|---|
| 一键跑 | `bash <(curl -fsSL .../main/tcpfit.sh)` |
| 装好后 | `tcpfit` |
| 子命令 | `tcpfit tune --role proxy --bw 500` |

## 菜单

```
   1. 一键调优   Auto-tune (recommended)  ~10 min
   2. 基础调优   Base tuning only          ~1 min
   3. 拐点测试   Policer sweep             ~8 min
   4. 加 swap    Add swap (low-memory box)
   ────────────────────────────────────────────
   5. 查看状态   Status
   6. 端口验证   Verify port capability    ~1 min
   7. 回滚改动   Rollback all changes
   8. 检查更新   Check for updates
   9. 调优存档   Tuning archives
   u. 卸载 tcpfit
```

脚本不会自动更新. 装好之后跑的一直是装的那一版, 想升级用菜单 8 或 `tcpfit update` ——
它只检查, 发现新版本会问你要不要更新.

一键调优只问三个问题: 带宽、测速对端、机器用途. 确认之后跑到底不再打断.

带宽那一问支持四种输入:

| 输入 | 行为 |
|---|---|
| 数字 | 按该带宽推导缓冲区, 然后实测拐点 |
| 回车 | 现场实测带宽, 然后实测拐点 |
| `m` | 直接填限速值, 跳过拐点扫描 |
| `0` | 不做整形 |

## 子命令

```bash
tcpfit detect                                     # 机器画像
tcpfit probe    --peer <近处iperf3服务器>          # 探测可用带宽
tcpfit tune     --role proxy --bw 500             # 基础调优
tcpfit sweep    --peer <近处iperf3服务器> --nominal 500
tcpfit shape    --rate 510                        # 应用整形
tcpfit shape    --off                             # 移除整形, 保留基础调优
tcpfit harden   --swap 2G                         # 加 swap
tcpfit verify   --peer <近处iperf3服务器>          # 测速验证
tcpfit status                                     # 当前配置
tcpfit rollback                                   # 回滚全部改动
tcpfit update                                     # 检查更新
tcpfit archive list                               # 列出存档
tcpfit archive save "晚间配置"                     # 保存当前状态
tcpfit archive restore 0010                       # 恢复第 10 份存档
tcpfit archive rename 0010 "备用配置"              # 存档改名
tcpfit archive delete 0010                        # 删除指定存档
tcpfit uninstall --keep-archives                  # 卸载，保留存档和快照
```

## 拓撲感知預檢與聯合選優

多對端不是獨立測速站。請複製拓撲範例並明確描述本機角色、每個對端方向及
上游到下游的配對；工具不會按 IP、延遲或輸入順序猜測：

```bash
cp inventory/topology.example.yml topology.yml
python3 orchestrator/topology_optimizer.py topology.yml --facts-only \
  --output results/my-topology.json
python3 -m unittest discover -s tests -v
```

預檢記錄 CPU 架構、可用核心、負載、steal ticks、可用記憶體與 cgroup
限制、核心及壅塞控制支援、預設路由網卡的 MTU、佇列、offload、錯誤與丟包。
欄位分成 `observed`、`declared_not_measured` 與 `unavailable`；虛擬網卡宣告速率
明確不作實際頻寬。輸出也會標明是否完成端到端驗證。

選優函式使用接收端 goodput；重傳以 `retransmits / packets` 正規化，缺失不當作
零。候選必須具備全部必要路徑、變異係數不超過 15%，且任一路徑不得比基準
退步超過 10%。在距最佳穩定 goodput 5% 內優先選低重傳者，再看穩定性與 CPU；
不到 2% 的差異視為可能處於測量噪音內。這些門檻與候選、重複、時間預算都可
在 YAML 調整。這組保守預設避免以大幅限速換取零重傳，亦避免單一路徑峰值掩蓋
必要路徑退步。

目前 CLI 僅開放安全的 `--facts-only` 預檢。尚未接入能同時控制上下游兩端的業務
workload driver，因此會明確記錄 `end_to_end_verified: false`，也**不會**把分段
iperf 結果冒充端到端證據、更不會套用或持久化未驗證候選。核心的候選限制、
讀回、回復、持久化與聯合選優元件已獨立實作並具測試；正式套用仍應先在具備
兩端代理與 root/network-admin 權限的 VPS 測試床整合驗證。

## 多机（未上线）

多机编排还没在真实环境验证过, 暂时不建议使用. 下面的用法仅供参考.


```bash
cp inventory/servers.example.yml inventory/servers.yml
chmod 600 inventory/servers.yml
vi inventory/servers.yml

python3 orchestrator/fleet.py detect
python3 orchestrator/fleet.py tune
python3 orchestrator/fleet.py sweep
python3 orchestrator/fleet.py shape --auto
python3 orchestrator/fleet.py verify
```

选项: `--only 机器名` `--tag 标签` `-j 并发数` `--dry-run`.
临时执行任意命令: `fleet.py run -- uptime`.

## 它改了什么

| 类别 | 参数 |
|---|---|
| 拥塞控制 | `tcp_congestion_control=bbr` + `default_qdisc=fq` |
| 缓冲区 | `tcp_rmem` / `tcp_wmem` / `rmem_max` / `wmem_max` / `tcp_mem` |
| 窗口 | `tcp_window_scaling` / `tcp_moderate_rcvbuf` / `tcp_adv_win_scale` |
| 队列 | `netdev_max_backlog` / `netdev_budget` / `somaxconn` 等 |
| 连接 | `tcp_tw_reuse` / `tcp_fin_timeout` / `ip_local_port_range` 等 |
| 起步 | `tcp_slow_start_after_idle=0` / `initcwnd 32` |
| 出向整形 | HTB 全局上限 + fq 叶子 pacing |

共 32 个 sysctl 参数. 缓冲区和整形值按每台机器实测推导, 不是固定值.

## 拐点扫描怎么工作

先不限速跑一次, 看有没有东西在打你:

| 结果 | 动作 |
|---|---|
| 丢包低 | 没有限速器, 不整形 |
| 丢包高 | 有限速器, 从实测吞吐往上扫找拐点 |
| 吞吐 > 2500 Mbit | 超出扫描上限, 不扫（可用 `--cap` 调整） |

拐点在"不限速吞吐"的**上面** —— 打穿限速器会让吞吐掉下来, 所以往上找.

## 回滚

```bash
tcpfit rollback                # 按快照逐项写回, 不是恢复默认
tcpfit rollback --purge-swap   # 同时删掉 harden 建的 /swapfile
tcpfit shape --off    # 只去掉整形
```

首次改动前自动存快照到 `/var/lib/tcpfit/pre-tune.snapshot`, 记录全部 32 项参数的原始值.

0.5.7 起，原始快照同时保留为 `0000 出厂状态`，不可改名或单独删除。
`archive restore 0000` 与 `rollback` 使用同一回滚流程；“出厂状态”指首次调优前的快照。
普通存档位于 `/var/lib/tcpfit/archives/`。基础调优后自动保存，一键调优则在最终整形、验证完成后保存。
序号可以输入 `10` 或 `0010`；名字含空格时请加引号。

恢复普通存档会同步 sysctl 启动配置和整形服务。有路由窗口设置时沿用 networkd-dispatcher hook；
缺少该目录会提示路由只能即时恢复并返回失败。恢复失败可能已经应用部分设置，请按提示检查后重试。

`tcpfit uninstall` 默认删除存档；需要保留则加 `--keep-archives`。
若回滚失败，卸载会停止并保留存档。卸载不删除 swap、iperf3 或 ping。

swap 默认不动 —— 删掉正在用的 swap 可能让机器立刻 OOM, 要一并撤销得显式加 `--purge-swap`.

改动只落在这些文件, 不碰 `/etc/sysctl.conf`:

```
/etc/sysctl.d/99-tcpfit.conf
/etc/systemd/system/tcpfit-qdisc.service
/usr/local/sbin/tcpfit-qdisc.sh
/etc/networkd-dispatcher/routable.d/50-tcpfit-initcwnd
/etc/modules-load.d/tcpfit-bbr.conf
/var/lib/tcpfit/
```

用了 `harden --swap` 还会创建 `/swapfile` 并往 `/etc/fstab` 加一行 —— 这两个 `rollback` 默认不动,
要一并撤销加 `--purge-swap`. 缺 iperf3 时经你确认后会用包管理器安装它.

## 已知限制

- 瓶颈在国际链路而非端口时, 整形不会带来提升, 但输出看起来一切正常
- 扫满区间没找到拐点时会把区间上界当成拐点, 这种情况用 `m` 手动指定
- 需要 Linux + systemd + iproute2. OpenVZ/LXC 上 `tc` 和 `initcwnd` 可能受限
- `sweep` 需要一台近处的 iperf3 对端

## 从 nettune 升级

老机器上的产物文件名还是 `nettune-*`, 新版本会自动检测并搬迁, 快照和 rollback 都保留. 直接跑新版即可.

## 许可证

[MIT](LICENSE)
