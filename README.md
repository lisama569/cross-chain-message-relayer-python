# 本地跨链消息中继与资产守恒演示

用多个**本地链账本**模拟跨链消息中继：消息提交、最终性确认、按序投递执行，
以及锁定（lock）/ 铸造（mint）/ 销毁（burn）/ 解锁（unlock）的资产闭环。
不依赖外部链、真实凭据或外部服务，所有链路与故障场景均可本地复现。

## 结构

```
relayer/
  types.py    # 消息、状态机、拒绝原因、迁移日志等共享类型
  ledger.py   # LocalChainLedger：本地链账本（区块/最终性/回滚/资产操作）
  store.py    # StateStore：中继器状态的 JSON 原子快照持久化
  relayer.py  # Relayer：提交登记、最终性确认、按序执行、恢复续作
main.py       # 端到端本地演示（含重启恢复）
tests/        # 重放、乱序、并发、恢复、守恒场景测试
```

## 消息生命周期

```
NONE --submit--> PENDING_FINALITY --poll_finality(最终性达成)--> READY
READY --execute_ready--> EXECUTED（终态）
READY --执行失败--> EXECUTION_FAILED --重试(幂等)--> EXECUTED
PENDING_FINALITY --来源链回滚--> REJECTED（终态）
```

- **提交（submit）**：登记消息，在来源链提交区块并执行来源侧资产操作
  （离开原生链 `lock`，非原生链 `burn`）。重复提交与重放被识别并拒绝。
- **最终性确认（poll_finality）**：仅当来源区块获得约定确认数后进入 READY；
  若来源区块被回滚，消息以 `SOURCE_REORGED` 拒绝。
- **执行（execute_ready）**：按 `(来源链, nonce)` 确定顺序在目标链执行
  （到达原生链 `unlock`，非原生链 `mint`），逐条落盘，失败可安全重试。
- **恢复（recover）**：重启后加载快照，回滚失效的未决消息，
  续作挂起任务；已 EXECUTED 的结果绝不回退。

## 最终性规则

每条链以 `confirmations` 个确认数定义最终性：区块高度 `h` 在链高度达到
`h + confirmations` 后最终。只有最终化的消息才允许执行；最终性依据
（区块高度、确认数、当前链高）会写入状态迁移日志。

## 顺序与拒绝语义

- 每条来源链的消息 nonce 必须从 1 开始连续递增；乱序提交以
  `OUT_OF_ORDER` 明确拒绝（被拒绝消息不占 nonce 位）。
- 拒绝原因可区分：`DUPLICATE_MESSAGE`（未决消息重复提交）、
  `REPLAY_AFTER_EXECUTION`（已执行消息重放）、`OUT_OF_ORDER`（乱序）、
  `SOURCE_REORGED`（来源回滚）。拒绝不产生任何资产副作用。

## 资产守恒

- 单链不变量：`sum(balances) + locked == total_supply`，每次资产操作后断言。
- 跨链闭环：原生链 A 的 `locked` 始终等于非原生链 B 的铸造流通量
  （`A.locked == B.total_supply`）。
- 所有资产操作按 `message_id` 幂等：重试或重放不会产生额外副作用，
  不会丢失资产、重复铸造或破坏总量。

## 失败恢复

- 中继器每次状态迁移后把完整快照原子写入 JSON（临时文件 + rename）。
- 重启后 `recover()` 加载快照：未决消息若来源区块已被回滚则以
  `SOURCE_REORGED` 拒绝；其余挂起任务继续推进；已执行结果保留。
- 链回滚通过 `LocalChainLedger.reorg(height)` 本地复现。

## 本地验证

```bash
uv run python main.py          # 端到端演示：提交/最终性/执行/重放/重启恢复
uv run python -m pytest tests/ -q -s   # 场景测试，打印消息标识、最终性依据与迁移日志
```

测试覆盖：重放与重复提交的区分、乱序拒绝与按序执行、并发提交下的守恒与
连续 nonce 前缀、重启恢复续作、来源链回滚（最终前拒绝 / 最终后不回退）、
lock-mint-burn-unlock 全闭环守恒。
