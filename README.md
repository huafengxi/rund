# rund — 节点执行底座

本仓提供声明校验、节点执行、Service 进程监督、一次性 Task、对象客户端及运行包装工具。主机身份和业务装配由调用方提供；公开机制仓保存通用机制。

机制仓位于 ~/.rund/rund，私有配置仓位于 ~/.rund/config。节点配置入口为 config/host-id：每行 hostname node，第一条有效记录指定 hub；node 同时是 SSH 别名。运行路径、Python、工具路径与轮询周期按统一约定推导。新机完成两个 checkout 并登记身份后执行 `~/.rund/rund/runctl start`；独立实例使用 `--root` 指定运行根。完整合同见 [PROTOCOL.md](PROTOCOL.md)。

Service 表达常驻程序并直接拥有进程状态；Task 表达一次性执行。每个对象保存一份 declaration.json。Service 原地更新，配置历史由 Git 保存；完整声明摘要变化决定重启，repos 指定依赖仓库的不可变 tag。Task 声明不可变，修改或重跑使用新 ID，取消通过独立的 cancel.json 表达，终态状态整份冻结。rund 不裁决跨节点写冲突，同步层负责运输和冲突处理。

runtime/config.py 定义声明合同；services.py 解析 host-id 与配置仓对象；engine.py 协调执行；process.py/native.py 处理原生进程身份、真实 wait 与组收口；cli.py 提供节点与对象操作。共享事实位于 shared，本机日志、锁、源码缓存与应用数据位于 local。

scheduler 是部署在 hub 的普通 Service，在各对象目录写 grant.json，汇总有效 grant 维护全局资源与并发；节点只检查 grant 并执行本机进程。tools/env_exec.py、sync.py、external_exec.py 分别处理内存凭据注入、共享树运输与官方管理器适配。源码由 git-sync 根据配置仓 repos.json 与 Service repos 维护，每仓库一个固定 checkout。共享树运输与 GC 实现位于 sync/，随机制 checkout 提供；shared-sync 直接运行其中的同步工具，hub 交换目录为该用户 ~/.rund/hub。

运行机制源码直接使用 checkout。源码 checkout 可由 git-sync 持续更新；需要使新机制完整生效时，先收口 Task 并通过 runctl stop 停止节点，再 start。Service 的本机启停意图位于 local，绑定声明摘要；声明变化后采用新声明的默认 desired。配置仓保存声明和附件，运行状态保存在运行目录。

测试入口为 `python -B -m unittest discover -s tests -v`。已有节点的声明迁移入口为停止节点后运行 `python -B tools/migrate.py`；历史文件与终态状态保留原样。部署与跨机验收由调用方完成。
