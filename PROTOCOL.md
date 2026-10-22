# 声明对象与本机执行合同

Service 是持续监督对象；每次执行均为独立 Task。Task 是唯一持有进程身份和实际 wait 的执行对象。完整字段和机制由 runtime/config.py 校验，运行数据由 runtime/owner.py 生成。

## 文件与写权

共享根默认用 services/<id>/、tasks/<id>/ 组织对象，各含 declaration.json 和 state.json。处理只依据 kind、id、node，不从路径推断语义或节点。id 全局唯一；相同身份多路径拒绝。声明由 publisher 映射的节点单写，状态由 node 的 Owner 单写。配置附件由指定发布节点单写。同步副本只读，不能覆盖本机原件或取得执行权。

私有根包含节点配置、Owner 锁、接受记录、源码缓存、artifacts 及 tasks/<id> 中的日志和工作目录；不参加同步。共享根与私有根必须不相交。运行源码和机器配置由调用方注入，机制仓不保存部署值。

声明 revision 每次修改递增；状态由来源写者经串行链路覆盖副本，不按墙钟或 mtime 决定权威。Task 终态副本同样冻结。Service version 单独表示执行定义版本。

## 声明

公共必需字段：kind、id、publisher、node、revision。可选 name 用于显示，不决定身份。

Task 必需 desired、argv；可选 needs、cwd、env、resources、cancelMode、service。desired 为 run/cancel，仅允许 run 转 cancel；除 revision、desired、取消模式外的执行定义发布后不可变。取消发布后模式固定。needs 为本机文件路径，全部在场才启动；缺失时等待，取消不等待依赖。argv 无 shell。cwd 默认任务工作目录，环境从显式节点 HOME/PATH 与声明 env 构造，不继承调用者身份。service 引用生成它的 Service id、version、declarationDigest。

Service 必需 version、desired、restart、task；可选 probe。desired 为 online/offline；restart 为 manual/on-failure/always；task 是 argv、needs、cwd、env、resources 模板。修改模板、策略或探针须提高 version。每次启动复制模板生成新 Task；原 Task 状态未知或负载未收口时阻塞替代执行。offline 取消当前 Task 并停止后续创建。

路径占位符：${shared}、${private}、${artifacts}、${work}、${runtime}。needs 不引用其他 Task 身份。产物准备程序在独立 Task 中执行，完整产物原子发布后最后发布完成文件；成功发布的产物不可变，被引用期间不得清理。

## 状态

公共字段：kind、id、node、declarationDigest、updatedAt、phase、reason。摘要对应已接受声明，时间仅作观测说明。

Task 状态另含 process、startedAt、finishedAt、exitCode、signal、waitObserved；process 保留原生 boot、start、pid、uid、pgid、sid 身份。阶段为 accepted、preparing、queued、starting、running、canceling、unknown、succeeded、failed、canceled。后三者为终态，整份 state.json 冻结。取消记录必要的信号阶段和截止时间；只有真实 wait 和组收口后才能完成已启动 Task。启动前失败或取消可无进程、无 wait；未知身份或旧 Owner 丢失 wait 不伪造终态。

Service 状态另含 currentTask、nextStartAt、restartCount；nextStartAt 为 Unix 秒或 null。阶段为 stopped、starting、running、stopping、backoff、blocked。进程事实只存在关联 Task；currentTask 可在终态后保留。

每次更新原子替换整份文件。Owner 重启读取已接受声明及原状态，starting/unknown 不自动重放 spawn。Owner 自身在受管图外，替换前收口全部 Task。

## 配置与凭据

普通配置与密文凭据可作为版本化共享文件；本机密钥不进入同步根。应用或普通启动包装器在内存解密并 exec 应用，核心不解释密文或加载 secret。声明和状态只保存引用；包装器不回显值或将其写入同步文件。bootstrap 通过显式私有引用提供工具与密钥。轮换发布新配置路径并更新 Service 模板，旧 Task 不被修改。

## Bootstrap

安装固定 runtime、同步工具和准备工具，写入本机配置与普通同步 Service；通过 servicectl owner start 启动图外 Owner。同步 Service 直接执行已安装工具，无业务源码依赖。后续发布准备 Task 与业务 Service；准备工具获取固定源码并发布本地产物，业务 Task 仅等待 needs 文件。工具、端点、发布者映射及密钥位置由调用方提供。

## 统一入口

servicectl status [name/id] 查询；start/stop <Service> 修改原发布者 desired 并等待实际状态；cancel <Task> [--mode kill] 发布取消。publish --input <file> 由声明原发布者执行。owner start/stop 操作图外 Owner；owner run 是前台底座入口。Makefile 不是协议约束。客户端不能直接执行或发信号给受管负载，不能回写 state。远端修改经 SSH 路由到声明原发布者；目标机仅执行自己的对象。

node.json 是本机公共配置权威；普通同步配置通过 nodeConfig 引用它，只保存运输字段。Owner 身份和诊断同在 owner.json，已接受声明在 accepted.json。源码准备计划只描述固定 sources，不复用执行声明；Git 缓存与最终 artifacts 之间没有第二套代码或 manifest 目录。

## 全局执行放行

节点配置 scheduler 指定唯一放行节点和 maxConcurrent。该节点 Owner 单写 shared/scheduling/state.json；grants 保存 Task id 与不可变执行定义摘要的绑定。其他 Owner 只读。资源名为不透明的全局字符串，按原样比较；机器隔离由调用方命名。

Task 显式携带 resources 时参加全局名额分配，[] 表示只占名额；省略时独立执行，用于底座与常驻服务。目标 Owner 在文件齐备后写 queued、queuedAt，获得匹配 grant 才 spawn。放行者按排队时间和 id 扫描，资源被占者不阻塞其它候补。只有 Task 终态释放 grant；unknown、未执行和离线节点均保留占位。存在非终态执行而放行记录丢失时拒绝新放行，须恢复记录后继续。

## 共享树运输

shared 同时容纳执行对象与 agents 业务树。agents、configs、gc、scheduling 为保留命名空间，Owner 不扫描其中的业务 declaration.json。其余目录内仍按 kind 处理执行声明。

tools/sync.py 调用节点 sync.json 中显式指定的固定运输工具。运输采用 agents-sync 的原件/replica 属组协议和 neutral hub；同步器不解析执行或消息语义。一次性与常驻同步共享 cycleLock 串行完成 push/pull。配置包含 nodeConfig、tool、remote、probeRoot、remoteProbeRoot、cycleLock、sshConfig 及周期参数。底座启动前必须备好工具和 scratch。
