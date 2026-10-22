# 声明对象与本机执行合同

Service 是持续监督对象；每次执行均为独立 Task。Task 是唯一持有进程身份和实际 wait 的执行对象。完整字段和机制由 runtime/config.py 校验，运行数据由 runtime/owner.py 生成。

## 文件与写权

共享根默认用 services/<id>/、tasks/<id>/ 组织对象，各含 declaration.json 和 state.json。处理只依据 kind、id、node，不从路径推断语义或节点。id 全局唯一；相同身份多路径拒绝。声明由 publisher 映射的节点单写，状态由 node 的 Owner 单写。配置附件由指定发布节点单写。同步副本只读，不能覆盖本机原件或取得执行权。

私有根包含节点配置、Owner 锁、接受记录、源码缓存、artifacts、日志和工作目录；不参加同步。共享根与私有根必须不相交。运行源码和机器配置由调用方注入，机制仓不保存部署值。

声明 revision 每次修改递增；状态由来源写者经串行链路覆盖副本，不按墙钟或 mtime 决定权威。Task 终态副本同样冻结。Service version 单独表示执行定义版本。

## 声明

公共必需字段：kind、id、publisher、node、revision。可选 name 用于显示，不决定身份。

Task 必需 desired、argv；可选 needs、cwd、env、cancelMode、service。desired 为 run/cancel，仅允许 run 转 cancel；除 revision、desired、取消模式外的执行定义发布后不可变。取消发布后模式固定。needs 为本机文件路径，全部在场才启动；缺失时等待，取消不等待依赖。argv 无 shell。cwd 默认任务工作目录，环境从显式节点 HOME/PATH 与声明 env 构造，不继承调用者身份。service 引用生成它的 Service id、version、declarationDigest。

Service 必需 version、desired、restart、task；可选 probe。desired 为 online/offline；restart 为 manual/on-failure/always；task 是 argv、needs、cwd、env 模板。修改模板、策略或探针须提高 version。每次启动复制模板生成新 Task；原 Task 状态未知或负载未收口时阻塞替代执行。offline 取消当前 Task 并停止后续创建。

路径占位符：${shared}、${private}、${artifacts}、${work}、${runtime}。needs 不引用其他 Task 身份。产物准备程序在独立 Task 中执行，完整产物原子发布后最后发布完成文件；成功发布的产物不可变，被引用期间不得清理。

## 状态

公共字段：kind、id、node、declarationDigest、updatedAt、phase、reason。摘要对应已接受声明，时间仅作观测说明。

Task 状态另含 process、startedAt、finishedAt、exitCode、signal、waitObserved；process 保留原生 boot/start/pid/uid/pgid/session 身份。阶段为 accepted、preparing、starting、running、canceling、unknown、succeeded、failed、canceled。后三者为终态，整份 state.json 冻结。取消记录必要的信号阶段和截止时间；只有真实 wait 和组收口后才能完成已启动 Task。启动前失败或取消可无进程、无 wait；未知身份或旧 Owner 丢失 wait 不伪造终态。

Service 状态另含 currentTask、nextStartAt、restartCount。阶段为 stopped、starting、running、stopping、backoff、blocked。进程事实只存在关联 Task；currentTask 可在终态后保留。

每次更新原子替换整份文件。Owner 重启读取已接受声明及原状态，starting/unknown 不自动重放 spawn。Owner 自身在受管图外，替换前收口全部 Task。

## 配置与凭据

普通配置与密文凭据可作为版本化共享文件；本机密钥不进入同步根。应用或普通启动包装器在内存解密并 exec 应用，核心不解释密文或加载 secret。声明和状态只保存引用；包装器不回显值或将其写入同步文件。bootstrap 通过显式私有引用提供工具与密钥。轮换发布新配置路径并更新 Service 模板，旧 Task 不被修改。

## Bootstrap

安装固定 runtime、同步工具和准备工具，写入本机配置与普通同步 Service；通过调用方 make 入口启动图外 Owner。同步 Service 直接执行已安装工具，无业务源码依赖。后续发布准备 Task 与业务 Service；准备工具获取固定源码并发布本地产物，业务 Task 仅等待 needs 文件。工具、端点、发布者映射及密钥位置由调用方提供。
