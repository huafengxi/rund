# 声明对象与本机执行合同

rund 是直接运行的节点底座。Service 表达持续运行意图并直接拥有进程状态；Task 表达一次性工作。声明校验由 runtime/config.py 定义，运行状态由 runtime/engine.py 写入。

## 文件与写权

机制源码直接运行 Git checkout。配置仓的 rund.json 为共用配置，host-id 将系统 hostname 映射到稳定节点 ID；objects/services/<id>/declaration.json 保存 Service，objects/tasks/<id>/declaration.json 可保存静态 Task，objects/configs/ 保存附件。每个 rund 只执行 node 等于本节点的对象。

动态 Task 的 declaration.json 位于 shared/tasks/<id>/，由 publisher 映射的原发布节点单写。两类对象的 state.json 均由目标节点单写，分别位于 shared/services/<id>/ 和 shared/tasks/<id>/。配置仓声明直接读取，不经过共享树发布；共享树中的 Service 声明仅供历史查询，不参与执行。配置仓与共享协议出现同 ID Task 时正文必须一致。

id 全局唯一；声明修改须提高 revision；相同 revision 的内容必须一致。Task 终态整份 state.json 冻结。同步依据原写者与 replica 属组传输，副本不能取得写权。shared 与 local 不相交；密钥、进程锁、日志、源码缓存、工作目录和应用私有数据保存在 local。

## 声明

公共必需字段为 kind、id、publisher、node、revision，可选 name 为显示和本机 Service 选择名称。

| 字段 | 合同 |
| --- | --- |
| kind | Service 或 Task |
| id | 全局唯一身份，与 publisher、node 一经接受不可改变 |
| publisher | 声明写者，由共用配置 publishers 映射到原发布节点 |
| node | 唯一执行节点及状态写者 |
| revision | 正整数；每次修改递增 |
| name | 可选非空显示名 |
| argv | 两类对象均必需；非空字符串数组，直接执行 |
| needs | 可选本机文件路径数组，全部为普通文件后才启动 |
| cwd | 可选，默认当前执行的私有工作目录 |
| env | 可选字符串映射，覆盖基础环境 |
| resources | 可选全局资源数组；省略表示独立执行，空数组表示只占并发名额 |

Task 的 desired 为 run/cancel，可选 cancelMode 为 terminate/kill，默认 terminate。除 revision、desired、cancelMode 外，发布后的执行定义不可变；取消不可撤回，再次取消不改变模式。历史 Task 的 service 引用保留为原始执行出处。

Service 额外必需 version、desired、restart，可选 probe。执行字段直接位于 Service 正文。

| Service 字段 | 合同 |
| --- | --- |
| version | 正整数；执行定义、策略或探针变化须提高版本 |
| desired | online/offline，作为配置默认意图 |
| restart | manual/on-failure/always |
| probe | `{http,timeout?}` 或 `{cmd,expect?,timeout?}`，互斥 |

manual 在进程退出后保持停止，on-failure 在失败后重试，always 在退出后重试。重试指数退避为 0.5 至 30 秒。HTTP 探针以请求可达为成功，包括 HTTP 错误响应；命令探针要求退出码 0 且 stdout 包含 expect。探针用于查询健康，不触发自动重启。

本机 Service 启停意图位于 local/services/<id>/intent.json，包含 declarationDigest 和 desired。摘要匹配当前声明才生效；配置声明变化后使用其默认 desired。start 是幂等操作，显式重启使用 stop、start。

路径占位符为 `${shared}`、`${private}`、`${artifacts}`、`${work}`、`${runtime}`、`${config}`、`${home}`、`${node}`。argv、needs、cwd、env 值支持展开，~ 和 ~/ 使用当前用户 home。基础环境包含 HOME、PATH、LC_ALL、TMPDIR、PYTHONDONTWRITEBYTECODE；work 与 TMPDIR 属于本次执行私有目录。

```json
{
  "kind": "Service",
  "id": "example-service",
  "name": "example",
  "publisher": "operator",
  "node": "node-a",
  "revision": 1,
  "version": 1,
  "desired": "online",
  "restart": "on-failure",
  "argv": ["/bin/sleep", "3600"],
  "needs": [],
  "env": {}
}
```

一次性 Task 使用相同执行字段，将 kind 改为 Task、desired 改为 run，并省略 version、restart、probe。

## 状态

公共字段为 kind、id、node、declarationDigest、updatedAt、phase、reason；时间为带时区 ISO 字符串，仅作观测。

| 状态字段 | 含义 |
| --- | --- |
| process | 启动前 null；启动后为 pid、start、boot、uid、pgid、sid |
| startedAt / finishedAt | 启动与终结时间，未发生时 null |
| exitCode / signal | 正常退出码或终止信号，未知时 null |
| waitObserved | 是否取得负责执行者持有的真实 wait 结果 |
| queuedAt | 按需出现的排队时间 |
| stopStep / stopDeadline | 按需出现的信号阶段和 Unix 秒截止时间 |
| Service.runId | 当前或最近一次运行标识，关联日志目录，不是 Task |
| Service.runVersion | 当前或最近一次运行采用的 Service 版本 |
| Service.nextStartAt | 重试截止 Unix 秒或 null |
| Service.restartCount | 自动重试计数 |

Task 阶段为 accepted、preparing、queued、starting、running、canceling、unknown、succeeded、failed、canceled；后三者为冻结终态。启动前取消不等待文件或 grant；运行中取消先 TERM，约三秒后 KILL，kill 模式直接 KILL。已启动执行须取得真实 wait 且进程组收口才能完成。丢失身份或 wait 的结果保持 unknown，不凭 PID 消失伪造退出码。

Service 阶段为 stopped、preparing、queued、starting、running、stopping、backoff、unknown。offline 或版本变化先收口原进程，再停止或启动新版本；未知结果阻塞重复启动。每次运行的状态和日志保存在 local/services/<id>/runs/<runId>/，不创建子 Task。Task 日志和工作目录位于 local/tasks/<id>/。

status 的 actual、run_version、state、drift 是派生健康视图，检查进程身份、rund 存活、运行版本和探针。它与持久化 phase 分工不同。

## 配置与凭据

共用配置格式为 rund/v1，hosts 指向配置仓内的 hostname 映射文本；每行 hostname node，支持空行与 # 注释。hostname 忽略大小写；未登记或重复映射必须报错。home 从当前用户获取，运行根为配置仓父目录，sharedRoot/privateRoot 分别为其 shared/local，机制源码路径由当前 checkout 确定。

共用配置保存 python、path、publishers、admission、interval 和可选 syncConfig；支持 `${home}`、`${root}`、`${config}`、`${node}` 展开。admission 只引用可信调度发布者；并发策略属于 scheduler 的配置。源码版本由 checkout 的 Git revision 追踪；更换机制版本前先停止节点负载。

明文密钥不进入共享树。应用包装器可读取配置仓附件、在内存加载或解密凭据再 exec，rund 核心不解释 secret。业务源码由普通准备 Task 获取固定 Git 对象，产物完成文件最后发布；已完成产物不可变。源码计划的 sshConfig 可为绝对路径或 ~/ 路径。

## Bootstrap

调用方将机制仓与配置仓分别 checkout 到 ~/.rund/rund 和 ~/.rund/config，执行 `~/.rund/rund/runctl start`。前台使用 `runctl run` 或 `python -B rund.py`。默认共用配置为 ~/.rund/config/rund.json，测试与独立实例可用 --config 显式指定。

启动直接读取配置，创建运行目录、单实例锁和自身进程记录，进入协调循环。rund 不作为 Service 或 Task，也没有常驻父进程监督要求。start 返回只说明 rund 已启动；其他服务就绪由 status 独立查询。同步服务、scheduler 和源码准备按普通声明执行，启动不依赖预先同步或远端声明发布节点在线。

启动前须具备 Python、Git 以及声明实际引用的系统工具和凭据。系统开机启动由调用方选择外部启动入口。运行过程不会安装机制源码、复制节点配置或生成 runctl 副本。

## 统一入口

| 命令（全局支持 --config） | 行为 |
| --- | --- |
| start | 后台启动本节点 rund，已运行时复用 |
| run | 当前进程前台运行 rund |
| stop | 检查一次性 Task 已终结，再请求 rund 收口全部 Service 并退出 |
| status | 节点与 Service 汇总，包含独立 rund 状态、total、drift、services、diagnostics、blockedTargets |
| status <Service name/id> | 本节点 Service 摘要，包含 version、run_version、desired、actual、state、pid、run、note |
| status <Task id> | Task 声明与状态，可查询同步副本及历史 Task |
| start / stop <Service name/id> | 本节点 Service 启停，保存本机意图，等待约 120 秒内收敛 |
| cancel <Task id> [--mode terminate\|kill] | 向原发布者提交取消意图，返回声明，不等待终态 |
| publish --input <file\|-> | Task 发布到共享协议；Service 写入配置仓，配置传播遵循配置仓 Git 工作流 |
| desired <id> <value> [--mode terminate\|kill] | 底层意图入口，Task 可经 SSH 路由原发布者 |
| sync | 有 syncConfig 时执行一次同步 |

客户端不直接 spawn/kill 受管负载或回写状态；节点 rund 的启动与停止由 runctl 直接执行。节点停止期间暂停新执行，Service 逐项收口；未知进程保持阻塞。停止前已有一次性 Task 须先执行完或取消。服务 start/stop 超时表示意图已记录，不能据此断言回滚。status 不主动同步。

## 全局执行放行

scheduler 是普通 Service，argv 执行 tools/scheduler.py，省略 resources 以独立启动。配置包含 nodeConfig、publisher、maxConcurrent 和可选 interval。publisher 必须映射到其执行节点；scheduler.lock 保证唯一写者。

scheduler 单写 shared/scheduling/state.json，包含 node、grants、maxConcurrent、updatedAt。grants 绑定执行对象 ID 与执行定义摘要；节点只读检查来源和摘要。资源名按原样比较，机器隔离由调用方命名。

显式 resources 的对象在文件齐备后进入 queued，匹配 grant 后启动。Task 终态释放 grant；Service 停止或进入退避、准备阶段后释放，下一次执行重新排队。unknown、缺失事实和离线副本保留占位。scheduler 停止不影响已运行进程；新候补等待放行，重启后沿用持久化记录。

## 共享树运输

shared 容纳 Task 协议、Service 状态、调度记录和 agents 业务文件。agents、configs、gc、scheduling 为保留业务命名空间，不扫描其中的业务 declaration.json。工具 tools/sync.py 读取配置仓的运输配置，执行调用方固定的 agents-sync 工具；源码可由普通准备 Task 提供。常驻和一次性同步使用同一个 cycleLock 串行传输。
