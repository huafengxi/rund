# 声明对象与本机执行合同

Service 是持续监督对象；每次执行均为独立 Task。rund、scheduler 和应用均使用同一声明合同。Task 是唯一持有进程身份和实际 wait 的执行对象。完整字段和机制由 runtime/config.py 校验，运行数据由 runtime/engine.py 生成。

## 文件与写权

共享根默认用 services/<id>/、tasks/<id>/ 组织对象，各含 declaration.json 和 state.json。处理只依据 kind、id、node，不从路径推断语义或节点。id 全局唯一；相同身份多路径拒绝。声明由 publisher 映射的节点单写，状态由 node 上负责该对象的唯一执行者单写。配置附件由指定发布节点单写。同步副本只读，不能覆盖本机原件或取得执行权。

私有根包含节点配置、rund 锁、接受记录、源码缓存、artifacts 及 tasks/<id> 中的日志和工作目录；不参加同步。共享根与私有根必须不相交。运行源码和机器配置由调用方注入，机制仓不保存部署值。

声明 revision 每次修改递增；状态由来源写者经串行链路覆盖副本，不按墙钟或 mtime 决定权威。Task 终态副本同样冻结。Service version 单独表示执行定义版本。

## 声明

公共必需字段：kind、id、publisher、node、revision。可选 name 用于显示，不决定身份。

| 字段 | 类型与含义 |
| --- | --- |
| kind | `Service` 或 `Task` |
| id | 全局唯一身份；与 publisher、node 一经发布不可修改 |
| publisher | 声明发布者，由节点配置 publishers 映射到唯一写入节点 |
| node | 唯一执行节点，也是状态写入节点 |
| revision | 正整数；内容变化必须递增，同 revision 只接受完全相同的正文 |
| name | 可选非空显示名；Service 的本机 CLI 可按 name 或 id 选择 |

Task 必需 desired、argv；可选 needs、cwd、env、resources、cancelMode、service。desired 为 run/cancel，仅允许 run 转 cancel；除 revision、desired、取消模式外的执行定义发布后不可变。取消发布后模式固定。needs 为本机文件路径，全部在场才启动；缺失时等待，取消不等待依赖。argv 无 shell。cwd 默认任务工作目录，环境从显式节点 HOME/PATH 与声明 env 构造，不继承调用者身份。service 引用生成它的 Service id、version、declarationDigest。

Service 必需 version、desired、restart、task；可选 probe。desired 为 online/offline；restart 为 manual/on-failure/always；task 是 argv、needs、cwd、env、resources 模板。修改模板、策略或探针须提高 version。每次启动复制模板生成新 Task；原 Task 状态未知或负载未收口时阻塞替代执行。offline 取消当前 Task 并停止后续创建。

| 执行字段 | 约束与默认值 |
| --- | --- |
| argv | 必需的非空字符串数组；直接执行，不经过 shell |
| needs | 文件路径数组，默认 `[]`；目标节点逐项检查普通文件存在 |
| cwd | 可选工作目录，默认 `${work}` |
| env | 字符串映射，默认 `{}`；覆盖显式基础环境，不继承 CLI 环境 |
| resources | 可选全局资源数组；省略表示独立执行，`[]` 表示只占并发名额 |

Task 的 cancelMode 默认为 terminate，先 TERM，等待约三秒后 KILL；kill 直接请求 KILL。取消已经发布时，再次取消不改变模式。service 为 rund 生成的父 Service 引用，包含 id、version、declarationDigest。

Service 的 version 为正整数，独立于 revision；单纯启停只改变 desired 和 revision。manual 在本次运行终结后保持停止；on-failure 只对 failed 自动重试；always 对任意终态重试。自动重试采用指数退避，当前范围为 0.5 至 30 秒。探针只用于 CLI 状态检查，不触发 rund 自动重启：HTTP 探针检查请求可达（包括 HTTP 错误响应），命令探针要求退出码 0 且 stdout 包含 expect（默认空串）。probe 使用 `{http,timeout?}` 或 `{cmd,expect?,timeout?}`，两类互斥。

路径占位符：${shared}、${private}、${artifacts}、${work}、${runtime}。needs 不引用其他 Task 身份。产物准备程序在独立 Task 中执行，完整产物原子发布后最后发布完成文件；成功发布的产物不可变，被引用期间不得清理。

占位符在 argv、needs、cwd、env 值中展开；`~` 和 `~/` 使用节点 home。基础环境包含 HOME、PATH、LC_ALL、TMPDIR、PYTHONDONTWRITEBYTECODE。`${work}` 和 TMPDIR 属于当前 Task 私有目录。

以下为独立 Task 与 Service 的声明示例；node 与 publisher 由调用方配置映射：

```json
{
  "kind": "Task",
  "id": "example-task",
  "publisher": "operator",
  "node": "node-a",
  "revision": 1,
  "desired": "run",
  "argv": ["/usr/bin/true"],
  "needs": ["${artifacts}/example/ready.json"],
  "cwd": "${work}",
  "env": {},
  "resources": ["example-repository"]
}
```

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
  "task": {
    "argv": ["/bin/sleep", "3600"],
    "needs": [],
    "cwd": "${work}",
    "env": {}
  }
}
```

## 状态

公共字段：kind、id、node、declarationDigest、updatedAt、phase、reason。摘要对应已接受声明，时间仅作观测说明。

Task 状态另含 process、startedAt、finishedAt、exitCode、signal、waitObserved；process 保留原生 boot、start、pid、uid、pgid、sid 身份。阶段为 accepted、preparing、queued、starting、running、canceling、unknown、succeeded、failed、canceled。后三者为终态，整份 state.json 冻结。取消记录必要的信号阶段和截止时间；只有真实 wait 和组收口后才能完成已启动 Task。启动前失败或取消可无进程、无 wait；未知身份或旧 rund 丢失 wait 不伪造终态。

Service 状态另含 currentTask、nextStartAt、restartCount；nextStartAt 为 Unix 秒或 null。阶段为 stopped、starting、running、stopping、backoff、blocked。进程事实只存在关联 Task；currentTask 可在终态后保留。

| 状态字段 | 含义 |
| --- | --- |
| kind / id / node | 对应对象身份与执行节点 |
| declarationDigest | 已接受声明的规范 JSON 摘要；终态 Task 保留最后冻结值 |
| updatedAt / reason | 最近持久化时间与原因；时间为带时区 ISO 字符串 |
| Task.process | 未 spawn 为 null；启动后包含 pid、start、boot、uid、pgid、sid |
| Task.startedAt / finishedAt | 启动与终结时间，未发生时为 null |
| Task.exitCode / signal | 正常退出码或终止信号；未知时为 null |
| Task.waitObserved | 是否取得负责该 Task 的执行者持有的真实 wait 结果，初始 false |
| Task.queuedAt | 进入全局队列的时间，按需出现 |
| Task.observedAt | 持续运行观测时间，按需出现 |
| Task.cancelRequested | 已实际发送取消信号时置 true，按需出现 |
| Task.stopStep / stopDeadline | 取消阶段（1=TERM、2=KILL）与 Unix 秒截止时间，按需出现 |
| Service.currentTask | 当前或最近一次子 Task id，初始 null |
| Service.nextStartAt | 自动重试截止时间，初始 null |
| Service.restartCount | 自动重试计数，初始 0 |

Task 的常见路径为 `accepted → preparing → queued → starting → running → succeeded/failed`；文件齐备或无需全局放行时跳过相应阶段。accepted 也是初始内存状态，不保证单独持久化。取消可在 spawn 前直接进入 canceled，或经 canceling 在真实 wait 和组收口后终结。unknown 表示事实不充分，不等同 failed。Service.phase=running 只表示关联 Task 正在运行，CLI 的 online/ok 还会检查进程身份、版本及探针。

每次更新原子替换整份文件。执行者重启读取已接受声明及原状态，starting/unknown 不自动重放 spawn。rund 自身也是普通 Service，每次运行生成普通 Task，由外部 bootstrap 执行者持有其真实 wait；替换 rund 前收口其监督的 Task。

## 配置与凭据

普通配置与密文凭据可作为版本化共享文件；本机密钥不进入同步根。应用或普通启动包装器在内存解密并 exec 应用，核心不解释密文或加载 secret。声明和状态只保存引用；包装器不回显值或将其写入同步文件。bootstrap 通过显式私有引用提供工具与密钥。轮换发布新配置路径并更新 Service 模板，旧 Task 不被修改。

## Bootstrap

调用方先提供固定 runtime 源码目录和同步工具、节点配置、同步配置、私有凭据引用及普通同步 Service 声明。tools/bootstrap.py 接收 --prefix、--node-config、--runtime-source、--revision，可用 --upgrade 允许已有安装根。它写 prefix/local/node.json（runtimeSource 指向传入目录）、创建共享根及私有 artifacts/locks/tasks、生成 bin/runctl。revision 参数校验为固定 40 位小写 SHA；安装器不导出、复制或校验源码目录与该 SHA 的对应关系，固定源码由调用方保证。安装器也不生成同步配置和 Service 声明。

node.json 的 bootstrapService 引用一个普通 Service id。runctl start <该 Service> 在必要同步后启动外部 bootstrap；bootstrap 与节点 rund 使用同一套 Service/Task 执行状态机，分别只处理该 Service 及其子 Task、其余本机对象。名称不参与执行分派，现场显示名为 rund。同步 Service 直接执行预先提供的固定工具，无业务源码依赖。后续发布准备 Task 与业务 Service；准备工具获取固定源码并发布本地产物，业务 Task 仅等待 needs 文件。工具、端点、发布者映射及密钥位置由调用方提供。

## 统一入口

runctl status [name/id] 查询；start/stop <Service> 修改原发布者 desired 并等待实际状态；cancel <Task> [--mode kill] 发布取消。publish --input <file> 由声明原发布者执行。run <Service> 在前台执行节点配置所引用 Service 的外部 bootstrap；现场使用 runctl run rund。Makefile 不是协议约束。客户端不能直接执行或发信号给受管负载，不能回写 state。远端修改经 SSH 路由到声明原发布者；目标机仅执行自己的对象。

| 命令（全局支持 `--config <node.json>`） | 实际行为 |
| --- | --- |
| status | 本节点 Service 汇总（包含 rund）；返回 node、total、drift、services、diagnostics、blockedTargets |
| status <Service name/id> | 本节点单 Service 摘要：version、run_version、desired、actual、state、pid、task、note 等 |
| status <Task id> | 返回共享树中的 declaration 和 state，可读取其他节点副本 |
| start / stop <Service name/id> | 仅选择本机 Service；向原发布者修改 desired，触发同步，最多等待约 120 秒；stop 还等待子 Task 收口 |
| cancel <Task id> [--mode terminate\|kill] | 发布取消意图，返回声明，不等待终态 |
| publish --input <file\|-> | 本机作为原发布者校验并发布声明；`-` 读取 stdin；输出声明路径 |
| desired <id> <value> [--mode terminate\|kill] | 底层声明意图接口；可路由原发布者，不等待实际执行 |
| sync | 配置 syncConfig 时执行一次 push/pull；未配置时无操作 |
| run <Service name/id> | 前台运行 bootstrapService 引用的普通 Service 的外部监督；现场为 run rund |

start/stop 超时表示意图已发布而执行仍待完成，不能据此认定声明回滚。跨节点启停应在目标节点运行 CLI；原发布者路由使用 publishers 中的节点名作为 SSH 目标。status 不主动同步，其副本新鲜度由同步链路决定。

停止 bootstrapService 所引用的 rund 前，CLI 检查其余本机 Task 已终结。rund 观察自身停用、版本替换或子 Task 取消意图后暂停新执行；负载收口时在私有诊断写入与当前 Service 声明摘要绑定的 quiescentFor。外部 bootstrap 核对进程身份和该确认后才发送取消信号，取得真实 wait 后冻结 rund Task 终态并退出。前台 bootstrap 收到 TERM/INT 时同样经声明停止并等待收口。

start 是设置 online 的幂等操作，不承诺创建新一代；需要显式重启时顺序执行 stop、start。Service 子 Task 的 resources 在模板中指定，遵循与独立 Task 相同的放行语义。

node.json 是本机公共配置权威；普通同步配置通过 nodeConfig 引用它，只保存运输字段。rund 与外部 bootstrap 的诊断分别在 runtime.json、bootstrap.json，接受记录分别在 accepted.json、bootstrap-accepted.json。两执行者按 bootstrapService 及其子 Task 划分写权；新生成子 Task 的发布者分别为 rund-<node>、bootstrap-<node>。执行事实以共享 Service/Task state 为准。源码准备计划只描述固定 sources，不复用执行声明；Git 缓存与最终 artifacts 之间没有第二套代码或 manifest 目录。

## 全局执行放行

scheduler 是普通 Service，其 task.argv 执行 `tools/scheduler.py --config <配置附件>`，模板省略 resources，因此启动不依赖自身放行。独立配置字段为 nodeConfig、publisher、maxConcurrent，可选 interval（默认 0.2 秒）。publisher 必须映射到当前部署节点，本机 scheduler.lock 排除第二个写者。部署节点由 Service.node 指定，各节点 rund 职责相同。

节点配置 `admission={"publisher":"<调度发布者>"}` 只指定可信放行来源，经 publishers 映射定位原写者；节点不保存并发或排队策略。scheduler 单写 shared/scheduling/state.json，字段为 node、grants、maxConcurrent、updatedAt；grants 保存 Task id 与不可变执行定义摘要的绑定。rund 只读并检查来源节点和摘要，取得 grant 后负责本机执行。资源名为不透明的全局字符串，按原样比较；机器隔离由调用方命名。

Task 显式携带 resources 时参加全局名额分配，[] 表示只占名额；省略时独立执行，用于底座与常驻服务。目标 rund 在文件齐备后写 queued、queuedAt，获得匹配 grant 才 spawn。放行者按排队时间和 id 扫描，资源被占者不阻塞其它候补。只有 Task 终态释放 grant；unknown、未执行和离线节点均保留占位。存在非终态执行而放行记录丢失时拒绝新放行，须恢复记录后继续。

scheduler 停止时保留持久化 grant，已运行 Task 继续由各节点 rund 监督；已有匹配 grant 的 Task 仍可启动，新候补等待新 grant。scheduler 通过普通 Service 生命周期独立重启，重新读取并保留未终态占位，不要求重启节点 rund。

## 共享树运输

shared 同时容纳执行对象与 agents 业务树。agents、configs、gc、scheduling 为保留命名空间，rund 不扫描其中的业务 declaration.json。其余目录内仍按 kind 处理执行声明。

tools/sync.py 调用节点 sync.json 中显式指定的固定运输工具。运输采用 agents-sync 的原件/replica 属组协议和 neutral hub；同步器不解析执行或消息语义。一次性与常驻同步共享 cycleLock 串行完成 push/pull。配置包含 nodeConfig、tool、remote、probeRoot、remoteProbeRoot、cycleLock、sshConfig 及周期参数。底座启动前必须备好工具和 scratch。
