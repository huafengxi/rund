# 声明对象与本机执行合同

rund 是直接运行的节点底座。Service 表达持续运行意图并直接监督进程；Task 表达一次性工作。声明合同归 runtime/config.py，执行状态归 runtime/engine.py。

## 文件与写权

运行根默认为 ~/.rund，通过 `--root` 可指定独立实例。机制源码直接运行 rund/ Git checkout；私有配置仓位于 config/。config/host-id 每行 hostname node，忽略空行与 # 注释，第一条有效记录为 hub。hostname 不区分大小写，hostname 和 node 均唯一，未知本机 hostname 报错。node 同时作为 SSH 别名。

两类对象均保存不可变的 declaration-vN.json，N 为正整数且无前导零。每版是完整声明，读数字版本最高者，允许版本间有空缺。相同版本只能具有相同内容；本地发布不会覆盖已有版本。多节点并发产生的同版本冲突留给同步层处理。

Service 声明位于 config/objects/services/<id>/；静态 Task 位于 config/objects/tasks/<id>/；动态 Task 位于 shared/tasks/<id>/。路径组织文件，正文 kind/id 定义身份。配置附件位于 config/objects/configs/。

静态 Task 在目标节点执行前按同一版本号保存共享快照；取消在共享目录追加新版本。配置仓与共享树的同 ID Task 按数字版本合并，同版本内容须一致。Service 直接读取配置仓。共享树中的 Service 声明仅供历史保留。

各节点可以发布 Task 与取消意图。node 表示唯一执行位置，目标节点写 shared/services/<id>/state.json 或 shared/tasks/<id>/state.json。id 全局唯一，kind、id 和 node 在对象创建后不可改变。Task 终态整份冻结。shared 与 local 不相交；锁、日志、凭据、源码缓存、工作目录和应用私有数据位于 local。

## 声明

公共必需字段为 kind、id、node，可选 name 为显示名和本机 Service 选择名称。声明修订号仅由文件名表达。

| 字段 | 合同 |
| --- | --- |
| kind | Service 或 Task |
| id | 全局唯一身份 |
| node | 执行节点，须在 host-id 登记 |
| name | 可选非空显示名 |
| argv | 必需；非空字符串数组，直接执行 |
| needs | 可选本机文件路径数组，全部为普通文件后才启动 |
| cwd | 可选，默认本次执行的私有工作目录 |
| env | 可选字符串映射，覆盖基础环境 |
| resources | 可选全局资源数组；省略时独立执行，空数组仍占并发名额 |

Task 的 desired 为 run/cancel；cancelMode 为 terminate/kill，默认 terminate。除 desired、cancelMode 外，发布后的执行定义不可改变。取消不可撤回，再次取消不改变模式。重新执行或修改执行定义须使用新 ID。

Service 额外必需 version、desired、restart，可选 probe。version 为执行版本，与文件名中的声明修订号独立。

| Service 字段 | 合同 |
| --- | --- |
| version | 正整数；执行定义、策略或探针变化须提高 |
| desired | online/offline，作为配置默认意图 |
| restart | manual/on-failure/always |
| probe | `{http,timeout?}` 或 `{cmd,expect?,timeout?}`，互斥 |

manual 在进程退出后保持停止，on-failure 在失败后重试，always 在退出后重试。重试指数退避为 0.5 至 30 秒。HTTP 探针以请求可达为成功，包括 HTTP 错误响应；命令探针要求退出码 0 且 stdout 包含 expect。探针用于查询健康，不触发自动重启。

本机 Service 启停意图位于 local/services/<id>/intent.json，包含 declarationDigest 和 desired；摘要匹配当前声明才生效。配置声明变化后采用其默认 desired。start 幂等，显式重启使用 stop、start。本机启停在执行节点操作。

路径占位符为 `${shared}`、`${private}`、`${artifacts}`、`${work}`、`${runtime}`、`${config}`、`${home}`、`${node}`。argv、needs、cwd、env 值支持展开，~ 和 ~/ 使用当前用户 home。基础环境包含 HOME、PATH、LC_ALL、TMPDIR、PYTHONDONTWRITEBYTECODE；work 与 TMPDIR 属于本次执行私有目录。

```json
{
  "kind": "Service",
  "id": "example-service",
  "name": "example",
  "node": "node-a",
  "version": 1,
  "desired": "online",
  "restart": "on-failure",
  "argv": ["/bin/sleep", "3600"]
}
```

## 状态

公共字段为 kind、id、node、declarationDigest、updatedAt、phase、reason。时间为带时区 ISO 字符串，仅作观测。

| 状态字段 | 含义 |
| --- | --- |
| process | 启动前 null；启动后为 pid、start、boot、uid、pgid、sid |
| startedAt / finishedAt | 启动与终结时间，未发生时 null |
| exitCode / signal | 正常退出码或终止信号，未知时 null |
| waitObserved | 是否取得负责执行者持有的真实 wait 结果 |
| queuedAt | 按需出现的排队时间 |
| stopStep / stopDeadline | 按需出现的信号阶段和 Unix 秒截止时间 |
| Service.runId | 当前或最近一次运行标识，关联日志目录 |
| Service.runVersion | 当前或最近一次运行采用的 Service 执行版本 |
| Service.nextStartAt | 重试截止 Unix 秒或 null |
| Service.restartCount | 自动重试计数 |

Task 阶段为 accepted、preparing、queued、starting、running、canceling、unknown、succeeded、failed、canceled；后三者为冻结终态。启动前取消不等待依赖或 grant。运行中取消先 TERM，约三秒后 KILL；kill 模式直接 KILL。执行须取得真实 wait 且进程组收口才能完成，丢失身份或 wait 保持 unknown。

Service 阶段为 stopped、preparing、queued、starting、running、stopping、backoff、unknown。offline 或执行版本变化先收口原进程，再停止或启动新版本。未知结果阻塞重复启动。每次运行的状态和日志位于 local/services/<id>/runs/<runId>/；Task 日志和工作目录位于 local/tasks/<id>/。

status 的 actual、run_version、state、drift 为派生健康视图，检查进程身份、rund 存活、执行版本和探针。

## 配置与凭据

节点运行信息由 host-id 和路径约定推导。home 来自当前用户，Python 固定为 ~/miniconda3/bin/python3；PATH 包含 local/tools/bin、用户 conda、Node 和常用系统工具目录。特殊工具及环境由业务声明显式提供。节点协调周期为 0.2 秒。

明文密钥保存在共享树外。应用包装器可读取配置附件、在内存加载或解密凭据再 exec，rund 核心不解释 secret。业务源码由普通准备 Task 获取固定 Git 对象，产物 ready.json 最后发布；已完成产物不可变。prepare.py 的 --link 可在完成后原子更新普通工具入口。

源码版本由 checkout 的 Git revision 追踪。更换机制版本前先停止节点负载。

## Bootstrap

调用方将机制仓和配置仓 checkout 到 ~/.rund/rund 和 ~/.rund/config，登记 host-id 后执行 `~/.rund/rund/runctl start`。前台使用 `runctl run` 或 `python -B rund.py`。

启动直接读取 host-id，创建运行目录、单实例锁和自身进程记录，进入协调循环。rund 是节点程序，系统开机入口由调用方选择。start 返回只说明节点已启动，业务就绪独立查询 status。同步、scheduler 和源码准备按普通声明执行。

启动前须具备 conda Python、Git，以及声明引用的工具与凭据。运行过程直接使用源码 checkout。已有节点先停止 rund，再用 tools/migrate.py 转换旧声明与本机接受记录；历史声明、日志、终态状态和业务数据保留。

## 统一入口

| 命令（全局支持 --root） | 行为 |
| --- | --- |
| start | 后台启动本节点，已运行时复用 |
| run | 当前进程前台运行 |
| stop | 检查 Task 已终结，收口 Service 后退出 |
| status | 节点与 Service 汇总，包括 total、drift、diagnostics、blockedTargets |
| status <Service name/id> | 本节点 Service 期望与实际状态 |
| status <Task id> | Task 声明与状态 |
| start / stop <Service name/id> | 保存本机 Service 启停意图，等待约 120 秒内收敛 |
| cancel <Task id> [--mode terminate\|kill] | 本地追加取消版本，不等待终态 |
| publish --input <file\|-> | 发布完整声明，自动分配下一个数字版本，相同内容幂等 |
| desired <id> <value> [--mode terminate\|kill] | Task 追加意图版本；Service 保存本机启停意图 |
| sync | 按约定执行一次共享树同步 |

客户端不直接 spawn/kill 受管负载或回写执行状态。节点停止前已有 Task 须先执行完或取消。服务操作超时表示意图已记录；status 不主动同步。

## 全局执行放行

scheduler 为 hub 上的普通 Service，执行 tools/scheduler.py，省略 resources 以独立启动。默认并发上限为 4，部署可用 --max-concurrent 指定。本机 scheduler.lock 保证一个调度进程。

scheduler 写 shared/scheduling/state.json，包含 node、grants、maxConcurrent、updatedAt。节点只接受 node 等于 host-id hub 的记录；grant 绑定对象 ID 和执行定义摘要。资源名按原样比较，机器隔离由调用方命名。

显式 resources 的对象在文件齐备后排队，匹配 grant 后启动。Task 终态释放 grant；Service 停止、退避或准备阶段释放。unknown、缺失事实和离线副本保留占位。scheduler 停止不影响已运行进程；新候补等待放行。

## 共享树运输

shared 容纳 Task 协议、Service 状态、调度记录和 agents 业务文件。agents、configs、gc、scheduling 为保留命名空间，不扫描其中的业务声明。

tools/sync.py 调用 local/tools/transport/sync/ssh-sync.py；该路径由普通 prepare Task 使用 --link 装配。hub 交换树为 hub 用户 ~/.rund/hub/shared，探测目录为 ~/.rund/hub/probe，远端 home 经 SSH 发现；本机探测目录为 local/apps/agents-sync/probe。锁固定为 local/locks/shared-sync.lock，SSH 使用 ~/.ssh/config，interval=15、minCycle=3。

常驻与一次性同步共用 cycleLock。同步工具当前按原件/replica 运输；并发同路径冲突的检测、保存与收敛归同步层，rund 不提供仲裁。
