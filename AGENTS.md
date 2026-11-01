# lp-v2 协作约定

本仓是独立公开仓 `raymizzou/lp-v2`，与 `open_trader` 的旧双池 LP 解耦。
`open_trader` 仅作只读参考；其中的脚本、服务和 CI 门禁不是本仓能力。
本批仅建立文档与环境基线；路线图描述未来工作，不代表实现完成或运行授权。

## 会话开始

1. 核对仓根、当前 worktree、分支、HEAD 和 dirty 状态；命令见
   [工作站配置](docs/operations/workstation-setup.md)。身份不符时先停下核实。
2. 阅读 [README](README.md)；查背景、阶段目标或授权时，再读母票
   [open_trader#503](https://github.com/raymizzou/open_trader/issues/503)。
3. 保留已有未提交改动，确认本次目标与允许修改的路径后再动手。

## 主机与授权

- Air、Mini 在 Herdr tab 中使用 Codex；Claude 只在云电脑 Herdr 中做设计和评审。
- 东京部署默认由 Mini 执行，须先获得 Ray 对该部署的明文授权。
- 合入、部署、开 use／实盘模式、下单、启动或停止做市，均须 Ray 明文授权。
  环境配置、PR 和路线图都不能替代这些授权。

## 修改与交付

1. 仓库修改从新拉取的 `origin/main` 新建隔离分支和 worktree 开始。
   代码或行为修改先列目标路径、改动、影响、验证及排除项，等 Ray 批准再实现；
   明确请求的文档修改可在请求范围内直接进行。范围或风险明显扩大时先讨论。
2. Worker 只修改分配的 worktree 和交接文件，Git 只读。
   Main 负责 stage、commit、push 和 PR，并核对相应授权；PR 指向 `main`。
   不在本地 `main` 交付、不直接推送 `main`、不 force-push。
3. 只验证改动及直接受影响的范围，并取得独立只读评审。
   纯文档改动检查差异、链接和表述即可；按仓内实际文件选择检查，
   不编造测试、CI 门禁或部署脚本。没有 CI 就报告缺失，不算 PASS。
4. 交接写清改动、证据、待办和下一责任人。证据绑定准确 SHA；
   未提交文件另记内容哈希。结果区分 PASS、FAIL、UNKNOWN 和未运行，
   其他 SHA 的结果不能冒充当前结果。PR 发布与合入分别报告，合入另等授权。

## 公开仓边界

- 私钥、凭据留在仓外；仓库、日志和证据均不得包含秘密。
  公开文档不写本地私有路径、主机 IP 或本地任务证据。
- 本任务不修改全局或插件设置。将来若单独授权配置修改，仍须保留
  Superpowers 插件与技能的禁用规则。
- 配置工作站、检查 clone 或确认 Python 基线时，读
  [工作站配置](docs/operations/workstation-setup.md)。后续文档存在后再加入口；
  计划中的能力必须明确标为计划。
