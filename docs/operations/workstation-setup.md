# 工作站配置

本页建立可重复核对的 clone 与协作基线。clone 可读不代表运行环境、部署或实盘就绪；
每台主机须单独核验，不能由一台的结果推断三端已配置完成。
授权与交付边界见 [AGENTS.md](../../AGENTS.md)。

## 三端分工

| 主机 | 工作方式 | 职责 |
| --- | --- | --- |
| Air | Herdr tab 中的 Codex | 开发与本机核验 |
| Mini | Herdr tab 中的 Codex | 开发与本机核验；东京部署的默认执行端，部署另需 Ray 明文授权 |
| 云电脑 | Herdr 中的 Claude | 设计与只读评审 |

使用各主机已获授权的 Git、Herdr 和代理环境。在目标 Herdr tab 内核对当前主机、
会话、角色和工作目录后再工作；clone 命令不负责安装或同步这些工具与全局配置。

## Clone 与身份核对

先选自己的工作目录。目标 `lp-v2` 不存在或确为空目录时，运行：

```sh
git clone https://github.com/raymizzou/lp-v2.git lp-v2
cd lp-v2
```

目标已有 clone 时，只进入该目录；不要重新 clone 覆盖，也不要 reset、clean 或自动 pull：

```sh
cd lp-v2
```

两种情况都运行以下只读核对。已有非空目录若不是本仓，停下另选空目标：

```sh
git rev-parse --show-toplevel
git worktree list
git branch --show-current
git rev-parse HEAD
git status --short --branch
git remote -v
```

确认仓根和 worktree 是本次目标，`origin` 指向 `raymizzou/lp-v2`
（HTTPS 或 SSH 均可），记录分支、完整 HEAD 和未提交改动。
发现身份不符先核实；有 dirty 工作先保留并交代归属，不在该工作树开始新任务。
完成这些只证明当前主机的 clone 身份已核对。

## Main 建立任务 worktree

以下由 Main 执行；worker 的 Git 保持只读。先在已核对的 clone 中刷新远端，
确认 fetch 成功，再记录基线：

```sh
git fetch origin
git rev-parse origin/main
```

选一个未使用的任务分支名和新的相邻目录；下列名称须替换为本次唯一名称：

```sh
git worktree add -b docs/task-name ../lp-v2-task-name origin/main
cd ../lp-v2-task-name
```

重新执行上面的身份核对，确认新分支、HEAD 等于记录的 `origin/main`、工作树干净，
再分配修改。目录或分支已存在时先核实归属，不删除或复用别人的工作。
stage、commit、push 与 PR 仍由 Main 按授权处理，PR 指向 `main`。

## Python 与运行基线

Python 基线：**待 M2 引入；当前仅文档/issue**。
当前仓库没有 `pyproject.toml`、依赖锁文件、测试、CI workflow 或部署脚本。
本阶段不指定 Python 版本、不安装依赖，也不复用 `open_trader` 的虚拟环境。
M2 引入实际声明后，再按本仓文件补充解释器、隔离环境和依赖核验步骤。

目前文档修改只做差异、Markdown 链接和范围检查；无 CI 不等于 CI 通过。
路线图里的命令、服务与验收是计划，不能当作可运行入口或授权。
仓库背景见 [README](../../README.md) 与
[母票 #503](https://github.com/raymizzou/open_trader/issues/503)。
