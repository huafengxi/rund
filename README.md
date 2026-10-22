# serviced — 单写者对象与本机 Task 执行

本仓提供声明校验、单节点 Owner、Task 进程监督、Service 策略、对象客户端及普通任务工具。机器、仓地址、工具、目录、同步端点与凭据引用由调用方提供。运行入口 rund.py，正式合同见 [RUND.md](RUND.md)。

runtime/config.py 定义声明；store.py 提供原子持久化；protocol.py 是轻量客户端；owner.py 协调 Service 与 Task；process.py/native.py 处理真实进程身份、wait 与组收口；cli.py 暴露安装节点操作。

共享对象各包含 declaration.json 和 state.json，声明与状态分别单写。Service 每次执行创建独立 Task；Task 通过 needs 等待本机文件。Task 终态状态冻结；未知事实不重放。节点锁、源码产物、日志、工作目录与密钥留在本机私有根。

tools/prepare.py、tools/env_exec.py、tools/sync.py 和 tools/external_exec.py 是普通 Task 的负载，分别完成固定源码准备、内存凭据注入、单写者运输和官方管理器适配。Owner 不获取源码或解密凭据。源码准备复用独立的 Git object export 实现，其历史夹具合同不是 Owner 执行合同。

机制测试使用 Python 标准库 unittest，入口 `python -B -m unittest discover -s tests -v`。测试创建自己的进程、仓与目录；真实五机安装及服务验证由调用方完成。历史变更由 Git 保存。
