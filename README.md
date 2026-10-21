# serviced — 执行 Runtime 与服务注册表

本仓只住通用机制：固定 Git 源码准备、Task/Service 共用执行内核、原生进程身份与组收口、真实子进程 wait、文件请求与精确回执、调用方服务注册表转换和 CLI。部署主机、源码镜像、版本、目录、凭据路径与启动入口由调用方声明。

共享期望态模式下，Owner 只读消费主控发布的完整节点快照，私有状态保留最后有效输入与删除墓碑；最小本机配置只带身份、工具和同步种子。声明版本、固定源码、执行环境与凭据引用都在服务声明中；发布、单写者、offline/删除与故障语义见 [RUND.md](RUND.md)。

正式入口为 `rund.py`，CLI 实现为 `runtime/service_cli.py`。一个本机 Owner 同时承接 agent 工作负载和普通 Service。调用方注册表与配置合同见 [RUND.md](RUND.md)，执行声明/内核合同见 runtime/；文件接口见 experimental/file_owner/。Linux 与 Darwin 使用原生 boot/start/uid/session/pgid 事实；未知身份和未完成请求保持未知，不凭 PID、超时或快照伪造 wait、重放动作。

服务以前台进程运行，由单一 Runtime 监督。协调器本身通过固定 Git 导出引导；替换协调器前须停止受管工作负载。第三方官方 daemon 通过显式 external 声明保留官方管理 API。调用方提供唯一服务启动器以及声明版本递增规则。

`context` 显式声明 cwd/home/path/inheritEnv，缺省环境隔离。凭据通过本地 file/loader 引用在 spawn 前仅在内存读取；声明、prepare manifest、回执与结果只保存引用。机制不加载部署密钥，也不依赖 agentd 的身份洗刷名单。

源码来自独立仓的固定 commit，可由本地对象或声明的 SSH/HTTPS 地址获取，再由同一 Git object export 准备为只读树；源缓存、运行状态、协议目录和调用方工作数据分开。公开仓中不保存实际服务清单或部署值。历史入口、配套测试与恢复事实由 Git 历史及 recovery/ 保留；恢复材料的使用边界见 recovery/README.md。

注册表与凭据单测使用 `python -B -m unittest test_registry test_env_loader -v`，不依赖 agentd checkout。执行验证使用新建且保留的 RUND_NATIVE_OUTPUT 运行 `python -B -m runtime.test_native`，以及 RUND_CLI_TEST_OUTPUT 运行 `python -B test_service_cli.py`；生命周期、prepare 与文件协议测试各自说明其 fixture 合同。调用方另行完成真实服务启停、版本、进程时间和 drift 核验。
