# 本地跨链消息中继与资产守恒演示

用多个**本地链账本**模拟跨链消息中继：消息提交、最终性确认、投递执行，
以及 锁定(LOCK) → 铸造(MINT) → 销毁(BURN) → 解锁(UNLOCK) 的资产闭环。
不依赖外部链、真实凭据或外部服务，所有链路与故障场景均可本地复现。

## 结构

```
relayer/
  types.py    # Message / MessageRecord / MessageStatus / RejectReason / ChainEvent
  ledger.py   # LocalChainLedger：单链账本（余额、锁定、事件日志、最终性、回滚）
  store.py    # JsonStore：中继状态 JSON 持久化（临时文件 + 原子替换）
  relayer.py  # Relayer：提交、确认、投递、恢复、回滚处理
tests/test_relayer.py  # 重放/乱序/并发/恢复/回滚/守恒测试
main.py                # 端到端演示脚本
```

## 消息生命周期

```
PENDING --(来源链达到最终性)--> FINALIZED --(目标链执行成功)--> EXECUTED (终态)
   |                                |
   | (负载非法)                      | (目标链临时故障)
   v                                v
REJECTED (终态)            FAILED_RETRYABLE --(重试)--> EXECUTED
```

- **提交 `submit`**：校验负载与顺序，登记为 `PENDING` 并落盘。
- **确认 `confirm`**：来源链 `src_block <= finalized_height` 时迁移到
  `FINALIZED`，并记录最终性依据（`finality_proof`，如
  `src_block=1<=finalized_height=2 on A`）。
- **投递 `deliver`**：投递前**重新校验**最终性，然后在目标链执行
  `MINT`（目标是资产原生链时为 `UNLOCK`），成功迁移到 `EXECUTED`。
- 每次状态迁移都写入 `MessageRecord.history`，测试与演示会打印
  消息标识、最终性依据与迁移日志。

## 最终性规则

- 每条链以 `finalized_depth` 个确认定义最终性：
  `finalized_height = max(0, height - finalized_depth)`。
- 只有 `src_block` 已最终的消息才允许执行；未最终投递会被拒绝
  （`SOURCE_NOT_FINAL`）。
- 链回滚（`rollback`）**不得穿越** `finalized_height`——已最终的来源事件
  与已执行的结果不可回退。
- 未最终的托管事件被回滚时：账本撤销事件、资金返还来源账户，受影响
  消息终态拒绝（`SOURCE_REORGED`），因为资产背书已消失。

## 顺序、重放与幂等

- nonce 在 `(src_chain, dst_chain, sender)` 维度必须严格递增（从 1 开始），
  乱序提交被拒绝：`NONCE_OUT_OF_ORDER`。
- 同一 `message_id` 重复提交且负载一致：**幂等**返回原记录，历史中标注
  `DUPLICATE_MESSAGE`，不产生额外副作用。
- 同一 `message_id` 但负载不同：拒绝 `REPLAY_CONFLICT`。
- 目标链按 `(message_id, op)` 去重，重复执行/重放不会重复铸造。

## 资产守恒

- 方向由 `asset_home` 决定：离开原生链 = LOCK + MINT；回到原生链 =
  BURN + UNLOCK。
- 守恒不变量（测试中断言）：

  ```
  Σ 各链 circulating_supply(asset) + Σ 在途消息金额 == 创世发行总量
  ```

  在途 = 状态为 PENDING / FINALIZED / FAILED_RETRYABLE 的已托管消息。
- 任一环节失败或并发执行都不破坏该不变量；失败重试（`FAILED_RETRYABLE`）
  幂等，不产生额外副作用。

## 失败恢复

- 所有状态迁移即时落盘（`JsonStore`，写临时文件后 `os.replace` 原子替换，
  崩溃不会得到截断文件）。
- **中继重启**：新实例调用 `recover()`，加载记录、重建 nonce 状态，
  继续推进 PENDING / FINALIZED / FAILED_RETRYABLE 的挂起任务；
  EXECUTED / REJECTED 终态不受影响。
- **部分执行**（链上已执行但状态未落盘）：恢复投递时识别账本
  "already executed"，直接对齐为 `EXECUTED`，不重复铸造。
- **链回滚**：`on_source_rollback(chain, height)` 撤销未最终事件并
  终态拒绝失去背书的消息；已最终区块拒绝回滚（`LedgerError`）。

## 本地验证

```bash
# 运行测试（14 个用例：重放、乱序、并发、恢复、回滚、守恒、幂等）
python3 -m pytest tests/ -q

# 打印每条消息的标识、最终性依据与状态迁移日志
python3 -m pytest tests/ -s

# 端到端演示：闭环 + 重启恢复 + 守恒检查
python3 main.py
```
