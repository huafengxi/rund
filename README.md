# rund — 单写者对象与本机 Task 执行

本仓提供声明校验、节点执行、Task 进程监督、Service 策略、对象客户端及普通任务工具。机器、仓地址、工具、目录、同步端点与凭据引用由调用方提供。rund 自身是普通 Service，由外部 bootstrap 启动并监督；命令为 runctl start/stop/status rund，前台入口为 runctl run rund。统一入口 runctl.py，正式合同见 [PROTOCOL.md](PROTOCOL.md)。

runtime/config.py 定义声明；store.py 提供原子持久化；protocol.py 是轻量客户端；engine.py 协调 Service 与 Task；process.py/native.py 处理真实进程身份、wait 与组收口；cli.py 暴露安装节点操作。

runtime/admission.py 供各节点只读检查 grant；runtime/scheduling.py 与 tools/scheduler.py 提供普通 scheduler Service，独立处理全局资源和并发策略。scheduler 配置与部署位置由调用方的 Service 声明及配置附件提供，不嵌入节点 rund。

共享对象各包含 declaration.json 和 state.json，声明与状态分别单写。Service 每次执行创建独立 Task；Task 通过 needs 等待本机文件。Task 终态状态冻结；未知事实不重放。节点锁、源码产物、日志、工作目录与密钥留在本机私有根。

tools/prepare.py、tools/env_exec.py、tools/sync.py 和 tools/external_exec.py 是普通 Task 的负载，分别完成固定源码准备、内存凭据注入、单写者运输和官方管理器适配。rund 不获取源码或解密凭据。源码准备只接受固定 sources 计划，直接校验 Git 对象并原子发布一份 artifact；源码缓存仅保存 Git 对象、Git 私有配置与锁。

机制测试使用 Python 标准库 unittest，入口 `python -B -m unittest discover -s tests -v`。测试创建自己的进程、仓与目录；真实五机安装及服务验证由调用方完成。历史变更由 Git 保存。
