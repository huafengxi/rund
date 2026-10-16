# serviced — 声明式服务生命周期层

## 边界

本仓只住通用机制：读取调用方的两层 JSON 服务注册表、执行服务生命周期、把本机状态纠偏到声明，以及常驻纠偏壳和合成回归夹具。不住服务定义、部署主机/仓名单、端点、凭据路径或运维政策。修改部署不应产生本仓 diff。

- `serviced.py`：唯一服务生命周期执行层，提供 start/stop/status/sync/trim；语义权威是模块 docstring 的注册表、Pointers、Invariants 与函数注释。配置错误响亮退出；本机零声明仅告警，不把 `0 drift` 直接等同于已受管。
- `sync-loop.sh`：串行周期纠偏，每轮在独立进程组运行同仓 `serviced.py sync`。自指重启、在飞子进程不追杀、临时输出/静默判据等契约见壳的头注。本次恢复不引入 rund，不监督 Task，不改 drift、pid 复用、版本/meta、PATH 或启停语义。
- Python 3 标准库与 Bash；没有 pip/npm 依赖。但身份洗刷依赖 **独立 agentd 源码**，不是零仓间依赖。

## 独立源码与工作区

`platform_paths.py` 是本仓拥有的薄 helper，源自平台同名文件，合同版本 v1。无需统一仓、`.platform-root` 或 MODULES。Python import 与 shell 二次入口均定位本仓真实源码：软链身份取 realpath，与状态/注册表 workspace 独立。

- 非空 `PLATFORM_WORKSPACE` 优先，展开 `~` 并绝对化；相对值以调用 cwd 为基准。
- 未设置或空串默认取真实源码仓父目录，不根据 symlink 祖先猜根。
- 已有 CLI/模块专用覆盖应在调用 helper 前选取；serviced 当前没有 `--root` 选项，不新增该选项或改变现有 CLI。
- 注册表从 `<workspace>/services/{profiles,daemons}` 读取，日志、pid/meta 位于 `<workspace>/run/`；调用方提供注册表、启动器和权限。源码仓不含 Makefile 或真实服务定义。

示例（占位符均由调用方选择；不是部署或启动授权）：

```sh
AGENTD_DIR=/path/to/pinned-agentd PLATFORM_WORKSPACE=/path/to/workspace \
  python3 /path/to/serviced/serviced.py status
```

生产服务启动/停止入口与版本政策由调用方维护；不要把这里的源码验证当成服务激活。

## agentd 依赖与 envscrub 接口

本次 recovery 固定完整 **agentd revision `9625bdc15f853e49a6312d20dc429e23330e3d89`**；机器可读契约为 [`recovery/dependencies.json`](recovery/dependencies.json)。调用方必须从其 agentd origin 新建/选择该固定 revision 的 checkout，再配置路径。运行时不联网、不自动 clone/update 或改 pin，也不把仓间版本选择伪装成服务部署已完成。

定位顺序：

1. 设置了 `AGENTD_DIR`：只使用该目录下 `envscrub.py`（展开 `~`、绝对化；相对路径以 cwd 为准）。错误/缺失不再尝试 sibling；显式空串清楚拒绝。
2. 未设置：只尝试真实源码仓的开发 sibling `<serviced-repo-parent>/agentd/envscrub.py`。

缺文件/接口时给出包含 AGENTD_DIR 和依赖位置的清楚错误并拒绝运行。**禁止统一仓 fallback、Python 搜索路径 fallback、第二套洗刷名单；envscrub 不搬入本仓。** 本仓按所选文件精确加载，而不是碰巧命中别的 `envscrub` import。

接口：`envscrub.scrub_env(base=None, keep=frozenset(), strip_third_party=False) -> dict`。serviced 的 `clean_env()` 传空 keep、保持第三方 API key 的服务侧语义，再按既有 `canonical_path()` 规范 PATH。身份名单始终由 agentd 单点维护；测试从固定依赖读取名单而不是复制名单。

## 环境加载与生命周期

profile 的 `env_file` 默认为明文 env 语法（可选 `export `、等号两侧空格、成对引号）；提供 `env_loader` 时必须为非空 argv 字符串列表。loader 在 workspace cwd、洗刷后的环境中运行，最后一个参数是 env 文件的字面绝对路径，**不经 shell**，30 秒超时。只接收 stdout，不 import 调用方的解密模块；失败/超时贡献空输入，不输出转换器的敏感 stdout/stderr。

顺序不变：clean_env → env_file/loader → extra_env（值展开 `~`）→ require_env。环境文件缺失/失败时不抹掉原先导出的普通变量；最终 required 值缺失或为空一律拒启，在任何 spawn 前退出。`extra_env` 和文件内容是调用方有意注入的配置，不是继承身份的豁免名单。

## 验证（不启动真实服务）

```sh
AGENTD_DIR=/path/to/pinned-agentd BASH32=/path/to/existing/bash-3.2 \
  python3 recovery/run-tests.py
```

runner 枚举本仓所有适用 `test_*.py`，逐个独立进程执行并保存完整日志。没有下载/安装/Bash 构建，也不会调用 make、模型或真实服务启停。测试创建并保留独立临时 fixtures；runner 可选 `--output` 指定一个**尚不存在**的自建根。BASH32 必须实际为 Bash3.2，否则测试失败而非跳过。

- `test_env_loader.py`：明文/argv/超时/无 shell/覆盖顺序、require_env 在 spawn 前拒绝及单点身份洗刷。
- `test_dependency.py`：固定 agentd、非 sibling 显式路径、开发 sibling、优先级、缺依赖/接口负例与精确文件加载。
- `test_registry.py`、`test_lifecycle.py`、`test_serviced_path.py`：注册表/本机面、生命周期失败隔离、停止失败保留记录、PATH 语义。
- `test_platform_paths.py`：本地 import/helper CLI、空/非空/相对/`~` workspace、真实/软链与不同 cwd。
- `test_shell.py`：真实 Bash3.2 下完整壳、第二个 Python 入口、空格/相对/链式软链/override 和多字节输出；只运行合成 sync observer。Linux Bash3.2 不是 macOS 实测。

恢复来源/逐 source 投影/no-op/mode/删除、适配与逐路径净结果见 `recovery/`。统一平台根 README 的 serviced 职责、调用方配置、root 默认/覆盖及本地壳入口机制在此保留；其跨仓部署/portable/安装器/其它模块内容不属于本仓。不复活被撤销安装器，不包含生产集成或激活。
