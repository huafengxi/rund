# rund — 节点执行底座

本仓提供声明校验、节点执行、Service 进程监督、一次性 Task、对象客户端及普通任务工具。主机、仓地址、端点、凭据引用和业务装配由配置仓提供；公开机制仓只保存通用机制。

机制仓 checkout 位于 ~/.rund/rund，配置仓位于 ~/.rund/config。入口为 `runctl`；默认读取 ~/.rund/config/rund.json，通过 host-id 将系统 hostname 映射到节点 ID。新机器完成两个 checkout 后执行 `~/.rund/rund/runctl start`；`runctl run` 在前台运行。启动只读取配置并创建运行目录、锁和状态。完整字段与命令见 [PROTOCOL.md](PROTOCOL.md)。

rund 是直接运行的节点程序。Service 是从配置仓读取的常驻程序声明，直接拥有进程状态；Task 是一次性执行对象，可来自配置仓或共享协议。两者共用进程启动、真实 wait、资源放行和取消逻辑。历史 Task 的终态文件冻结，未知结果不重放。

runtime/config.py 定义声明合同；services.py 解析共用配置与配置仓对象；engine.py 协调 Service 和 Task；process.py/native.py 处理原生进程身份、wait 与组收口；cli.py 提供节点与对象操作。Service 状态位于 shared/services/<id>/state.json，历次运行日志位于 local/services/<id>/runs/<runId>/。独立 Task 的声明和状态使用共享文件协议。

scheduler 是普通 Service，单写资源与并发放行记录；节点 rund 只检查 grant 和执行本机进程。tools/prepare.py、env_exec.py、sync.py、external_exec.py 分别负责固定业务源码准备、内存凭据注入、共享树运输和官方管理器适配。同步与业务准备按普通声明启动。

运行机制源码直接使用 checkout，升级前通过 runctl stop 收口负载，更新 checkout 后 start。配置仓保存期望配置，本机 start/stop Service 的意图保存在 local，绑定声明摘要；配置变化后使用新声明的默认 desired。配置仓不承载运行状态。

测试入口为 `python -B -m unittest discover -s tests -v`，测试使用独立目录与进程。实际部署和跨机验收由调用方完成。
