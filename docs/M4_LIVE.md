# M4 固定市场执行与运维草稿

本批实现离线可验证的实盘入口，**未部署、未开实盘、未投放私钥**。
代码、服务模板和下列命令均不授予合入、部署、真实交易或 48–72 小时运行权限。
实际启动前仍需逐项满足 #5 的门禁并获得 Ray 对目标主机、钱包、市场、上限和窗口的明文批准。
既有 `run --capital-usdc`、`lp-v2.service` 和 `deploy/deploy-lp-v2` 继续是只读路径。

## 公开市场预检与人工切换

保持只读服务运行，先在候选代码环境单独执行：

```sh
lp-v2 preflight --market <完整condition_id> --capital-usdc 100 --code-sha <40位SHA>
```

该命令仅发公开 GET，不读取签名/L2 文件，不创建 SecureClient、进程锁、状态目录或 journal，
不调用 systemctl。JSON 输出含 `scope=public_market`、代码/市场/上限/UTC 时间和逐项
`status`、`reason_code`。所有必需公开检查 PASS 才退出 0；BLOCKED 或 UNKNOWN 退出 2。
身份不符时依赖该身份的检查保留 UNKNOWN，不使用返回的外来 token 查询订单簿。
已能独立核验的费用、接单状态、奖励、政治分类、in_play、tick/最小量和簿时效分别报告。
公开 CLOB 市场读取成功且返回 JSON 对象时，`in_play` 为布尔 `false` 或对象没有此键，
均通过该项；显式 `true`、`null`、数字和字符串仍拒绝。读取失败、坏 JSON、非对象
不能视为缺键。预检、SDK 固定市场与扫描读取使用同一规则，其余身份、费用、簿和账户门禁独立。
`PUBLIC_READ_UNAVAILABLE` 表示未能读回；`FEE_POLICY_UNPROVEN`、`MARKET_STATE_INVALID`、
`BOOK_IDENTITY_CONSTRAINTS_OR_FRESHNESS` 等是稳定的脱敏拒绝原因，不输出原始异常或载荷。

公开 PASS 不证明私有身份、Relayer 或 allowance 就绪；这些明确为 NOT_CHECKED，
也不授予实盘权限。不得因 PASS 自动停止只读或启动实盘。获批后由操作员人工切换；
回退仍由操作员按已批准的 stop/读回流程停止实盘，再明确恢复只读服务。
本命令不执行切换、回滚或服务启动。

`feesEnabled=true` 说明市场有费用，不等于 post-only maker 必须支付费用。
仅接受完整、可验证的 taker-only feeSchedule：有限非负 rate、exponent=1、
明确 takerOnly=true、rebateRate 在 [0,1]，并保留真实费用开关。
例如 rate=.05 映射为 taker_fee_bps=500，rebateRate=.25；maker 计划费用显式为 (0,0)。
明确关闭费用且没有费率表的市场仍支持；缺失、矛盾或不支持的费率数据不会默认为零。
潜在返佣仅用于排名，实际收益仍依赖独立实际证据。
当前成交适配器仍拒绝 maker.fee_rate_bps 非零或未落定成交，不能拿当前费率表推断历史成交成本。

启动时市场校验失败输出白名单诊断：`stage=market_check`、
`reason_code=MARKET_VALIDATION_UNAVAILABLE` 与完整公开 market。
其他启动/停止错误仍只输出固定安全信息；不打印原始异常、凭据或响应体。
签名身份核验仍先于可能产生副作用的 SDK 初始化。

## 启动输入与凭据

只支持 Linux、Python 3.12+ 和固定 `polymarket-client==0.2.0`，钱包必须为既有
`POLY_PROXY`。启动前从显式签名文件离线推导 signer 与 Proxy 地址，核对 L2 的
signer、wallet 和 `signature_type=1`；不匹配会在 SDK 钱包准备之前拒绝。
不自动发现密钥、派生 L2 凭据、创建钱包或提供 EOA/Deposit Wallet 模式。

以下文件均由 `lp-v2` 用户拥有，为无符号链接的普通文件，权限 **0600**；
其父目录也应限制访问。文件路径显式传入，不把秘密放进环境变量、进程参数、
Git、stdout 或 journal：

| 文件占位 | 内容 |
| --- | --- |
| `/etc/lp-v2/signing-key` | 仅该钱包的签名私钥文本，独立于 L2 文件 |
| `/etc/lp-v2/credentials.json` | 既有 L2 `key`、`secret`、`passphrase`、`signer_address`、`wallet_address`、`signature_type` |
| `/etc/lp-v2/relayer.json` | 既有 legacy `{key,address}` 或显式 Builder `{kind,key,secret,passphrase,address}`，供 SDK Proxy 免 gas merge 使用 |

Builder 文件的 `kind` 必须为 `builder`，`address` 必须是与签名文件一致的完整
20 字节 signer 地址（十六进制大小写可不同）。`secret` 为 SDK 支持的标准或 URL-safe
base64，可省略 padding；所有字符串必须非空且不能含 CR/LF/NUL，拒绝未知或额外字段。
legacy 文件仍严格只含 `key`、`address`。检查、实际 SDK 工厂和启动文件连续性证明
使用同一解析规则；不根据 401 猜测格式、回退认证或创建凭据。

L2 凭据不能替代 Relayer 凭据。模板只留路径，不创建、分发或申请任何凭据。
真实凭据的准备与投放另需授权。

持久状态目录 `/opt/lp-v2/state` 由 `lp-v2` 独占、权限 **0700**。
保留其中的 `journal.sqlite`、WAL 与锁/进程结果文件；不要清空或复制一套状态来并发运行。
进程锁避免同一状态目录启动两个执行者；运维仍须保证同一钱包没有其他执行者。

下面只展示未来获批后的参数形状；占位值不能直接运行：

```sh
lp-v2 run --live --capital-usdc <已批准上限> --market <已批准condition_id> \
  --code-sha <已批准40位SHA> --private-key-file /etc/lp-v2/signing-key \
  --credentials-file /etc/lp-v2/credentials.json \
  --relayer-credentials-file /etc/lp-v2/relayer.json --state-dir /opt/lp-v2/state
```

`--market` 只允许一个完整 condition id；启动时固定该市场的两个 token、资金上限和 UTC
启动时间，不在运行中重读配置。`run.env` 仅放 `LP_V2_MARKET`、`LP_V2_CAPITAL_USDC`、
`LP_V2_CODE_SHA` 等非秘密身份。改上限只能改该文件并在另行批准的停止/重启后生效；
降低上限前必须核对现有占用。HTTP 仍只有 GET，没有修改、stop 或 restart 接口。

## 循环、恢复与停止

每 10 秒先读回自己的订单历史、成交、实际持仓和余额，重新对账后再运行已有 Engine。
仅发固定 token 的 BUY、post-only、GTD 限价单，到期为意图时间 +1 小时；不支持 SELL、
吃单、市价单或 cancel-all。原始意图时间保留，交易所到期字段按整数秒核对。

地区必须为明确 `blocked=false`，或者明确 `blocked=true`、`country=JP` 且账户明确
`closed_only=false`。缺失、类型错误、其他受限地区均不挂；`/healthz` 保留实际
blocked 身份，不把 JP 例外显示为地区检查通过。地区拒绝、地区读回失败或任何对账失败时，
禁止新挂，并尝试撤销流水中已知自己的单；读接口不可用也不跳过这些撤单尝试。
撤单 ack 不释放预留，仍须终态读回。

SQLite 先持久写意图与签名身份，再发请求。拒绝、UNKNOWN、撤单尝试、成交、merge 回执及
每轮占用分项均追加记录，不能改写历史。重启后先用流水与交易所重新对账，恢复原成本与补腿
上限；不重发已存在身份的下单或 merge。签名准备/身份核验等在 `post_order` 前可捕获的失败
写为 FAILED「未提交」；进入 `post_order` 后的异常先保留 UNKNOWN。硬崩溃尚未持久写下确定失败时，
即使没有 signed 标记也仍保守保留 UNKNOWN，不能由缺标记推断未发出。
未落定意图的成交按持久签名哈希候选认领，仍核验钱包、市场、token、BUY、费用、数量及身份，
并跨重启去重。只有在原始到期时间 +120 秒后，两个签名哈希都明确查无、完整活动订单分页
身份有效且无此单、完整成交记录也无此 id，才记 FAILED「到期未见」并释放预留；
缺少签名证明、读回失败、分页不完整或身份不明时仍 UNKNOWN。后续引擎可创建新一代意图，原意图不重发。

每个逻辑轮次/stop 读回周期复用各订单号的查询结果，下周期清缓存；每次尝试撤单后，
无论成功、拒绝或超时，都清除该 id 的本周期缓存，随后读回实际状态，其他 id 继续复用。
已有权威成交/撤销终态不再反复 get_order，但成交历史仍读回以保留迟到成交。
`DELAYED`/`UNMATCHED` 缺乏终态证明时保留原意图、累计成交和可能在场的预留，记 UNKNOWN。
只有明确 404（或接口明确返回不存在）才使用完整活动订单分页和成交记录兜底：
在场的自有单核验后采用当前事实。已接受或从签名哈希成交中认领的单若列表暂缺，
只有已有撤单请求、原始到期时间 +120 秒已到，或确认成交已满，才可结合完整成交证明落定；
成交满原数量记 FILLED，其余有落定理由的单记 CANCELLED。未撤、未到期缓冲且未满成交时仍非权威 UNKNOWN，
保留累计成交及剩余预留，下一轮再查询，不能仅凭一次 404 与列表延迟释放。累计成交与持久成交身份不得倒退或矛盾。
传输错误、5xx、坏身份、未结算成交或其他不完整证据均不能证明不在场，仍保留非权威 UNKNOWN 与预留。
当前订单未核实时暂停决策并撤已知自有单；撤单 ack 自身不能证明终态。
已有明确 placement rejection 与这些不确定状态分别保存。
真实网关是否能查询已到期/已撤历史 id 的预检仍为 UNKNOWN，留待另行授权只读验证；本批未做账户预检。

接受下单时将 response.order_id 与固定 SDK 算出的签名哈希候选比较。不同会写固定安全诊断，
同时保留交易所实际返回的订单号供归属和读回，不丢弃实际 id、不重发。真实网关哈希匹配尚未现场验证。
补腿按对侧持仓实际平均成本计算上限，旧单未确认终结前不替换。
固定市场跌出开仓流动性/中间价条件后，仍保留经验证的行情供已有持仓补腿/merge，
同时 eligible=false 禁止新开仓。费用、进行中状态、接单状态、身份、tick 和行情时效等执行硬检查仍生效；
普通只读扫描继续排除这些不符合开仓条件的市场。

merge 走 SDK Proxy 免 gas 路径。ack、现金增加或 relayer 的确认各自都不足以释放预留：
必须同一交易成功链上回执且哈希与 relayer 相符；已知 ERC1155 合约中，两个指定 token 各自
from=钱包的转出合计等于份数（可以先转给适配器，而非由钱包直接销毁）；已知 pUSD 合约中
到钱包的入账合计等于份数，包含 from=0x0 铸造。token/钱包/发出合约/份数必须吻合。

现金归属由该交易回执精确证明，实际现金仍独立读回。持仓按全部自有成交与已确认 merge
推算后对账，不要求整个钱包等于冻结的 merge 前快照加减固定量；期间自己的成交和独立奖励
不会阻断合法 merge。无相符回执的无关现金增长仍不能确认；实际持仓与完整历史不符则对账失败，
Ledger 不释放预留。缺证明保留 UNKNOWN，不盲目重发。
SDK 内部对已分类的限流/明确拒绝可能重试；适配器不会在超时或重启后再次提交该 merge 身份。

事件回归使用[云端评审](https://github.com/raymizzou/lp-v2/pull/12#issuecomment-6572207114)
描述的两个公开交易形状：普通 `0xb6f78b8adb456fb0ad4aaa9377725d87372fc32ce5af300ec68d790851872317`、
neg-risk [纠正来源](https://github.com/raymizzou/lp-v2/pull/12#issuecomment-6573784304)
`0x9a891d1f8b65767027b039cb3778e3e5d53f1e1e9aab469bd4f2c12c0bfd7f35`。
旧 neg-risk 来源是 redeem，不作为 merge 来源。
仓内 JSON 是按评审事件描述归一化的**合成夹具**（替换钱包/token/份数），不是下载的原始回执；
本批没有重新取得原始 RPC receipt。两种形状均覆盖钱包先转给适配器、适配器后续销毁、pUSD 铸造给钱包。

未来获批停止命令为：

```sh
lp-v2 stop --state-dir /opt/lp-v2/state
```

stop 使用同一受保护目录里的进程锁、Linux 启动身份与 pidfd 发送 SIGTERM；陈旧、外来或
无法核验的进程不发信号。已有安全状态目录中的有效锁若已释放，stop 返回 0 作幂等进程清理，
不发信号，也不覆盖旧的非零排空结果/残余记录；该 0 只证明没有执行进程，不能证明崩溃后交易所无单。
锁仍被持有时，陈旧/外来/错误身份仍非零退出。执行进程停止新挂，只按已知自己的订单号撤单，持续读回。
stop 同时撤当前完整活动列表里具有持久自有归属的 id，即使流水此前记为终态；外来 id 不撤。
对仍在场或可能在场的自有单约每 5 秒重试撤单，确认终态且最新列表无此单后才停止重试。
成交、持仓或余额读取失败不阻止对已知 id 或当前列表中已证明归属的 id 尝试撤单，但不能据此干净退出。
只有自有活动单为零、无可能仍在途的未决下单才返回 0；读回失败或 90 秒排空超时返回非 0，
写明订单、UNKNOWN、库存份数/成本及有时间戳的估值（缺新鲜行情则 UNKNOWN）。
残余未配对库存原样持有，不做清库存交易。stop 命令最多等 110 秒取得同一启动身份的最终结果。

`deploy/lp-v2-live.service` 与 `deploy/run-live` 是独立草稿，含 `ExecStop` 和
`TimeoutStopSec=150`，不把既有只读部署脚本改成实盘启动器。
尚未在目标主机验证 systemd、进程停止、重启、SDK 网关或链上行为。
服务发生失败后重启时仍先对账；不要把自动重启当作解除 UNKNOWN 的办法。

## 收益、时效与证据

`/summary` 保持 schema_version=1 与原七个 m4 字段，新增 `net_profit` 净额；
现有第 14 列在净收益率旁显示净额，仍为 14 列。
计分仅对已权威读回、当前在场的自有 LIVE 订单逐个 GET，不把已成交/已撤/拒绝/不确定/外来订单混入当前计分；当天奖励读取全部分页，份额与返佣只读 GET，每 60 秒刷新一次，
按市场、钱包、资产及当天 **UTC** 过滤，缺数据/错误身份/不完整读回显示 UNKNOWN。
`freshness.income_as_of` 是收入源实际读回时间，`as_of` 只是状态快照发布时间。
失败轮次不把旧数值收益作为当前收益重新发布；账本最后已知占用附 `accounting` 状态与时间。

净收益计算为：

```text
net_profit = 当日奖励 + 当日返佣 + 当日配对毛利润
             + 库存毛浮动盈亏相对日初基准的变化 - 当日实际费用
net_yield_pct_per_day = net_profit / 当日已观察区间的时间加权平均占用 × 100
lp_yield_pct_per_day = 当日实际奖励 / 同一平均占用 × 100
```

配对毛利润与库存毛成本排除费用，费用仅扣一次；Ledger 原有资金和已实现利润仍使用
包含费用的成本。真实适配器目前只接受已验证零 maker fee 的已确认成交；费用不明、
未结算成交不参与乐观计算；当前市场有 taker 费用不等于 maker 成交费用非零。不是用配置的潜在奖励率代替实际奖励。

平均占用按已记录轮次间的占用作阶梯积分，属于观察频率下的时间加权估计，
不是每个链上事件的连续精确积分。分母缺失或为零时收益率 UNKNOWN，不能拿启动上限代替。
金额是当日累计，不把不足一天的数据外推成全年或保证回报。

连续轮次跨 UTC 日时（10 秒目标周期，实际间隔不超过 60 秒），保存新日首次观察到的持仓估值及真实行情时间作为开盘基准。
该时间可能稍晚于 00:00，明确记为 `observed_boundary`，不伪称精确午夜成交价。
重启沿用已有当天基准；停机跨日、持仓但缺开盘观察基准时，净收益 UNKNOWN，不能事后回填。
无隔夜库存时可由完整成交/回执历史证明基准为零。读回失败也会打断连续观察证明。

每轮 journal 的 `occupation` 保存资金分项、最后核验时间、未配对份数/成本/行情估值及
UNKNOWN 清单，失败轮次也记录。导出证据应脱敏，不把私有状态或凭据提交到公开仓。
本地离线测试、CI、评审、合入、部署、48–72 小时现场验收是不同结果，不能互相替代。


## 统一运维入口

新增 `lp-v2 live check / on / off` 运维编排层，兼容已保存的 v1 默认 profile。
[操作说明](operations/live-control.md)列出本地设置、严格 SSH 信任、确认与版本绑定、
两轮读回、同 run 排空、断线不重放及只读恢复。底层 run/stop、交易账本和部署模板的
原有合同继续适用。启动前分别要求新鲜的公开/账户事实、受支持的 Proxy 配置证明和既有操作员
sudo 路线；应用读写与进程身份仍使用 lp-v2 用户，只对两个固定 unit 的有限操作临时提权，
不安装 sudoers/polkit。鉴权与配置证明不能证明实际提交或完成 merge；后者属于 #5 运行验收，
CLI 单列 NOT_RUN/UNKNOWN。缺少任何必需启动证据仍阻止开启，离线测试不替代部署、
真实启停授权或现场验收。

统一入口还核对已安装 launcher 的固定字节、有效 unit/环境/文件引用，并从实际 PID 参数与
文件重新导出完整身份。交接只证明本机与所选市场：旧服务 inactive/disabled、无其他本机
实盘执行器、订单为零，且两个 token 的新鲜鉴权余额与完整已停止自有快照一致；新状态需
明确双零余额。未知或不匹配不自动接管、清空或导入持仓；控制锁内再次核验后才切换。
不声称跨主机互斥、其他市场全局库存或对抗同 UID 恶意替换的进程证明。

### Opening eligibility diagnostics

Public preflight exposes allowlisted `checks.opening_eligibility.details` with
finite decimal strings for proven liquidity and executable-book midpoint, fixed
thresholds `1000` and `0.10`–`0.90`, observation time and authored liquidity/midpoint
blockers. Unknown source numbers are omitted; existing status/reason and all
independent checks remain authoritative. `live check` preserves these details only
with the matching code, market, capital and fresh observation. These diagnostics
do not waive in-play, region, fee or authentication gates.
