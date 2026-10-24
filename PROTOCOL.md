# 声明对象与本机执行合同

rund 是直接运行的节点底座。Service 表达持续运行意图并直接监督进程；Task 表达一次性工作。声明合同归 runtime/config.py，执行状态归 runtime/engine.py。

## 文件与写权

运行根默认为 ~/.rund，通过 `--root` 可指定独立实例。机制源码直接运行 rund/ Git checkout；私有配置仓位于 config/。config/host-id 每行 hostname node，忽略空行与 # 注释，第一条有效记录为 hub。hostname 不区分大小写，hostname 和 node 均唯一，未知本机 hostname 报错。node 同时作为 SSH 别名。

每个对象保存一份 declaration.json。Service 配置原地更新，历史由配置仓 Git 保存；Task 声明发布后不可改变，相同内容重复发布幂等。同 ID Task 必须具有相同内容。多节点写冲突由同步层处理。

Service 声明位于 config/objects/services/<id>/；静态 Task 位于 config/objects/tasks/<id>/；动态 Task 位于 shared/tasks/<id>/。路径组织文件，正文 kind/id 定义身份。配置附件位于 config/objects/configs/。

静态 Task 在目标节点执行前保存不可变的共享快照；配置仓与共享树的同 ID Task 内容必须一致。取消请求保存到共享 Task 目录的 cancel.json，与声明分别读取。Service 直接读取配置仓；shared/services 仅提供执行状态。

各节点可以发布 Task 与取消意图。node 表示唯一执行位置，目标节点写 shared/services/<id>/state.json 或 shared/tasks/<id>/state.json。id 全局唯一，kind、id 和 node 在对象创建后不可改变。Task 终态整份冻结。shared 与 local 不相交；锁、日志、凭据、源码缓存、工作目录和应用私有数据位于 local。

## 声明

公共必需字段为 kind、id、node，可选 name 为显示名和本机 Service 选择名称。声明文件名固定为 declaration.json。

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

Task 的 desired 固定为 run。声明全部字段不可变，重新执行或修改执行定义须使用新 ID。取消请求为独立的 cancel.json：`{"mode":"terminate"}` 或 `{"mode":"kill"}`，默认 terminate。请求不可撤回；重复取消沿用已观察到的请求模式，重新发布原声明不清除取消。多节点同时产生不同模式的冲突由同步层处理。

Service 必需 desired、restart，可选 repos、probe。repos 为仓库名到不可变 Git tag 的映射，仓库 URL、固定 checkout 路径、默认分支和节点范围由配置仓 repos.json 提供。

| Service 字段 | 合同 |
| --- | --- |
| repos | 可选仓库名到 tag 的映射，同节点同仓库的依赖版本须一致 |
| desired | online/offline，作为配置默认意图 |
| restart | manual/on-failure/always |
| probe | `{http,timeout?}` 或 `{cmd,expect?,timeout?}`，互斥 |

rund 比较当前完整声明摘要与本次启动采用的 declarationDigest，摘要变化且期望在线时替换进程。新依赖须在指定 checkout 中就绪：HEAD 等于 tag 所指提交、tracked 工作树与 index 干净。依赖未就绪时保留旧进程，首次启动则等待。摘要仅在新运行启动时更新；同摘要的显式 stop/start 仍产生新 runId。

Git tag 发布后不可移动或复用。git-sync 汇总声明并维护每仓库唯一固定 checkout；源码更新本身不触发重启。旧进程可以读到新文件，运行代码不保证等于启动时源码。Service 无依赖冲突是部署前提，冲突时报告并停止 tag 切换。

manual 在进程退出后保持停止，on-failure 在失败后重试，always 在退出后重试。重试指数退避为 0.5 至 30 秒。HTTP 探针以请求可达为成功，包括 HTTP 错误响应；命令探针要求退出码 0 且 stdout 包含 expect。探针用于查询健康，不触发自动重启。

本机 Service 启停意图位于 local/services/<id>/intent.json，包含 declarationDigest 和 desired；摘要匹配当前声明才生效。配置声明变化后采用其默认 desired。start 幂等，显式重启使用 stop、start。本机启停在执行节点操作。

路径占位符为 `${shared}`、`${private}`、`${work}`、`${runtime}`、`${config}`、`${home}`、`${node}`。argv、needs、cwd、env 值支持展开，~ 和 ~/ 使用当前用户 home。基础环境包含 HOME、PATH、LC_ALL、TMPDIR、PYTHONDONTWRITEBYTECODE；work 与 TMPDIR 属于本次执行私有目录。

```json
{
  "kind": "Service",
  "id": "example-service",
  "name": "example",
  "node": "node-a",
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
| Service.nextStartAt | 重试截止 Unix 秒或 null |
| Service.restartCount | 自动重试计数 |

Task 阶段为 accepted、preparing、queued、starting、running、canceling、unknown、succeeded、failed、canceled；后三者为冻结终态。启动前取消不等待依赖或 grant。运行中取消先 TERM，约三秒后 KILL；kill 模式直接 KILL。执行须取得真实 wait 且进程组收口才能完成，丢失身份或 wait 保持 unknown。

Service 阶段为 stopped、preparing、queued、starting、running、stopping、backoff、unknown。offline 或声明摘要变化先收口原进程，再停止或启动新声明。未知结果阻塞重复启动。每次运行的状态和日志位于 local/services/<id>/runs/<runId>/；Task 日志和工作目录位于 local/tasks/<id>/。

status 的 actual、declarationDigest、runDeclarationDigest、state、drift 为派生健康视图，检查进程身份、rund 存活、运行声明摘要和探针。

## 配置与凭据

节点运行信息由 host-id 和路径约定推导。home 来自当前用户，Python 固定为 ~/miniconda3/bin/python3；PATH 包含 local/tools/bin、用户 conda、Node 和常用系统工具目录。特殊工具及环境由业务声明显式提供。节点协调周期为 0.2 秒。

明文密钥保存在共享树外。应用包装器可读取配置附件、在内存加载或解密凭据再 exec，rund 核心不解释 secret。业务源码直接运行 git-sync 管理的固定 Git checkout；运行数据、日志、凭据与源码目录分离，业务初始化只创建运行目录、配置与必要链接。

源码版本由 checkout 的 Git revision 追踪。checkout 可持续更新；需要完整加载新机制时通过 runctl stop/start 重启节点。

## Bootstrap

调用方将机制仓和配置仓 checkout 到 ~/.rund/rund 和 ~/.rund/config，登记 host-id 后执行 `~/.rund/rund/runctl start`。前台使用 `runctl run` 或 `python -B rund.py`。

启动直接读取 host-id，创建运行目录、单实例锁和自身进程记录，进入协调循环。rund 是节点程序，系统开机入口由调用方选择。start 返回只说明节点已启动，业务就绪独立查询 status。同步、scheduler 和源码准备按普通声明执行。

启动前须具备 conda Python、Git，以及声明引用的工具与凭据。运行过程直接使用源码 checkout。已有节点先全部停止 rund，再用 tools/migrate.py 将数字版本收为当前声明、拆出取消请求并转换本机接受记录。配置仓旧版本由 Git 保存，共享树版本文件保留；被替换的旧单文件声明归档到 local/migrations/single-declaration。日志、终态状态和业务数据保留。

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
| cancel <Task id> [--mode terminate\|kill] | 本地发布不可变取消请求，不等待终态 |
| publish --input <file\|-> | 发布完整声明；Service 原地更新，Task 相同内容幂等 |
| desired <id> <value> [--mode terminate\|kill] | Task 发布取消请求，run 仅在未取消时幂等；Service 到执行节点保存本机启停意图 |
| sync | 按约定执行一次共享树同步 |

客户端不直接 spawn/kill 受管负载或回写执行状态。节点停止前已有 Task 须先执行完或取消。服务操作超时表示意图已记录；status 不主动同步。

## 全局执行放行

scheduler 为 hub 上的普通 Service，执行 tools/scheduler.py，省略 resources 以独立启动。默认并发上限为 4，部署可用 --max-concurrent 指定。本机 scheduler.lock 保证一个调度进程。

scheduler 写各对象 shared/services/<id>/grant.json 或 shared/tasks/<id>/grant.json，包含 node、id、active、executionDigest、resources、updatedAt。节点只接受 hub 发布且摘要匹配的 active grant。scheduler 汇总全部有效 grant，包括已放行但尚未启动者，并按 grant 保存的资源快照计算占用。释放通过 active=false 改写，不依赖删除传播；重启先恢复已有 grant。缺失执行中的 grant 时阻止新放行。

显式 resources 的对象在文件齐备后排队，匹配 grant 后启动。Task 终态释放 grant；Service 停止、退避或准备阶段释放。unknown、缺失事实和离线副本保留占位。scheduler 停止不影响已运行进程；新候补等待放行。

## 共享树运输

shared 容纳 Task 协议、Service 状态、调度记录和 agents 业务文件。agents、configs、gc、scheduling 为保留命名空间，不扫描其中的业务声明。

tools/sync.py 直接调用当前机制 checkout 的 sync/ssh-sync.py。shared-sync 和 scheduler 均由普通 Service 声明启动；同步实现随机制源码发布。hub 交换树为 hub 用户 ~/.rund/hub/shared，探测目录为 ~/.rund/hub/probe，远端 home 经 SSH 发现；本机探测目录为 local/apps/agents-sync/probe。锁固定为 local/locks/shared-sync.lock，SSH 使用 ~/.ssh/config，interval=15、minCycle=3。

常驻与一次性同步共用 cycleLock。同步工具当前按原件/replica 运输；并发同路径冲突的检测、保存与收敛归同步层，rund 不提供仲裁。
