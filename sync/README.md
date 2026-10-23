---
name: agents-sync
anchors:
  watch-cost: "watch 轮次模型：整树协商 + 速率下限（同步面纪律）"
  gc: "gc：清单是权威、schedule 只是计划（删除对账）"
  commands: "常用命令"
---

# agents-sync — rund 的共享树运输与 GC

本目录随 rund checkout 提供共享树运输、原件/副本标记、GC 工具与测试。代码与 rund 一起发布和升级；来源提交与文件对象记录在 SOURCE.json。真实主机、凭据与业务装配由调用方提供。

shared-sync 是普通 Service，执行 tools/sync.py；该包装器从 host-id 推导 hub，直接调用本目录 ssh-sync.py。scheduler 为独立 Service，负责资源放行。生命周期与状态合同见 [rund/PROTOCOL.md](../PROTOCOL.md)。

## 拓扑与文件身份

各节点运行一条到 neutral hub 的双向链路。运输不解释文件正文，shared 可包含 services、tasks、scheduling、agents 和 gc。未标记文件为本机原件，replica 属组表示同步落地的副本；组名跨机一致，gid 无需一致。

push 只运输本机未标记普通文件；pull 保护本机原件，包括未标记 symlink 和特殊文件。真实目录不承担原件组状态。watch 使用内容校验与 --chown=:replica，拒绝 --delete，不以时间戳裁决覆盖。路径不可安全编码、原件清单不完整或扫描失败时拒绝本轮 pull。

启动检查 replica 组、本机 chgrp 功能与端到端 chown 探针。rsync 须支持 --chown；本机可使用 inotify_simple 或 watchdog，hub 的非交互 python3 须具备 inotify_simple。

当前原件/副本运输仍依赖同路径的写入归属；并发写冲突的检测、保存与收敛属于同步层后续工作。

## 显式前台运输

watch --foreground 须显式提供绝对的 --probe-root 与 --remote-probe-root。两侧探测目录均须为当前用户所有、0700 的真实目录，与对应同步树不相交且位于相同 filesystem。同步根与探测目录由调用方创建；tools/sync.py 按运行布局约定完成该准备。

前台运输禁用 SSH master/persist 复用与后台化，在启动前安装停止处理，持有并真实 wait 自己启动的直接运输进程。停止与异常路径等待运输收口；不能取得真实 wait 或 pipe EOF 时保持阻塞，不猜测退出结果。旧 OpenSSH 的 ForkAfterAuthentication 兼容通过限定的 IgnoreUnknown 表达。

探针只清理自身 nonce 目录内已核身份、内容和单链接的已知文件，再删除空子目录；不递归清理调用方根或未知项。丢回复、停止与清理失败可能留下 scratch，只报告未确认结果。

常驻和 watch --foreground --once 使用同一个 --cycle-lock，把整轮 push/pull 串行化。cycleLock 为同步树之外的本机私有绝对路径。

## watch 轮次模型：整树协商 + 速率下限（同步面纪律）

每轮为整树 push 加整树 pull，rsync -c 决定是否搬运。检测器只报告变更，不存路径快照或参与覆盖裁决。每轮重新扫描 hub 全树 NUL 清单，不跟随 symlink；先过滤本机原件、有效 GC 与排除路径，再缩减隐含祖先目录。空叶目录保留。数据清单中的 *?[] 为字面字符，GC 路径仍按其独立契约校验。

扫描、清单或运输失败时不应用 GC，下一轮重新协商。成功 pull 后补打副本标记；macOS 目录恢复主组，避免新原件继承副本组。空转不写搬运日志。

--debounce 控制首个未消费事件窗口，--min-cycle 限制轮次速率，--interval 为强制周期与重新锚定上界。持续变更不能无限推迟首轮；无本地检测器时按周期运行。hub 的 SSH/inotify 断流按 5/10/20/30 秒重连，远端缺依赖时启动拒绝。

本机同步周期与工具参数由 tools/sync.py 的运行约定提供。底层 watch 可独立通过 --help 查询参数。

## gc：清单是权威、schedule 只是计划（删除对账）

gc.py 是 hub 上按次调用的 add/reap/list 工具。delete_contract.py 是生成端与消费端共同的路径、保护资产和审计标记合同。只有 add 写待执行计划；计划丢失时不猜测并删除。默认宽限 300 秒，先传播清单，再清理 hub 副本。

累计清单支持离线节点重连后对账。消费端应用清单并永久拒收已删除路径。树根、裸族容器、绝对路径、..、通配符与 GC 自树受结构保护，--force 不能解除；受保护业务资产仅可经明确授权的审计旁路删除。symlink 删除其本身，不跟随目标。

replica-tag.py 为手工辅助工具，默认 dry-run；调用方须显式提供根、本机身份与 AGENTS_SYNC_HUB。它不在服务启动链路内。

## 常用命令

Service 启停与一次性同步使用 runctl。手工 GC 须指定 hub 的权威共享树；参数以各工具 --help 为准。

```bash
~/.rund/rund/runctl status shared-sync
~/.rund/rund/runctl sync
python3 ./ssh-sync.py --help
GC_AGENTS_DIR="$HUB_SHARED" GC_WORKSPACE="$GC_WORKSPACE" python3 ./gc.py list
AGENTS_SYNC_HUB="$HUB_ID" python3 ./replica-tag.py --root "$SHARED" --self "$NODE_ID"
```

## 验证

测试只在自行创建的隔离 HOME/TMPDIR/workspace 中运行。test_watch.py 和 test_pull_manifest.py 可使用真实本地 rsync；不连接真实 SSH 端点。其他测试覆盖选择、保护、删除合同、标记、独立导入、周期锁和合成运输。

Linux 专用的前台进程、探针和清单测试分别要求 Y_FOREGROUND_OUTPUT、Y_PROBE_OUTPUT、Y_MANIFEST_OUTPUT 指向全新绝对目录。合成探针只清理本次自建的已知文件和空目录，保留其他证据。合成测试不能替代真实跨机运输、属组和进程验收。

mac-rsync-patch/ 保存可选的平台构建工具；安装与变更系统 rsync 由调用方单独决定。
