"""本地跨链消息中继演示：两条本地链账本 + 中继器，打印全生命周期日志。

运行：uv run python main.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from relayer import CrossChainMessage, LocalChainLedger, Relayer, StateStore


def show(relayer: Relayer, title: str) -> None:
    snap = relayer.snapshot()
    print(f"\n=== {title} ===")
    for name, c in snap["chains"].items():
        print(f"链 {name}: 高度={c['height']} 锁定={c['locked']} "
              f"总量={c['total_supply']} 余额={c['balances']} "
              f"守恒={c['conservation']}")
    print(f"消息状态: {snap['messages']}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="relayer-demo-"))
    a = LocalChainLedger("A", confirmations=2,
                         genesis_balances={"alice": 100})
    b = LocalChainLedger("B", confirmations=2)
    chains = {"A": a, "B": b}
    store = StateStore(workdir / "relayer-state.json")
    relayer = Relayer(chains, home_chain="A", store=store)

    print("## 1. 提交 A->B 转账消息（来源链锁定）")
    m1 = CrossChainMessage("m1", "A", "B", 1, "alice", "bob", 40, 0)
    relayer.submit(m1)
    show(relayer, "提交后：等待最终性")

    print("\n## 2. 来源链出块，达到最终性后执行（目标链铸造）")
    a.produce_block()
    a.produce_block()
    relayer.poll_finality()
    relayer.execute_ready()
    show(relayer, "执行后：B 链铸造完成")

    print("\n## 3. 重放同一消息：被识别并拒绝，无额外副作用")
    relayer.submit(m1)
    show(relayer, "重放后：状态不变")

    print("\n## 4. 模拟中继重启：从持久化快照恢复")
    m2 = CrossChainMessage("m2", "A", "B", 2, "alice", "bob", 10, 0)
    relayer.submit(m2)
    relayer2 = Relayer(chains, home_chain="A", store=store)
    a.produce_block()
    a.produce_block()
    resumed = relayer2.recover()
    print(f"恢复后续作的消息: {resumed}")
    show(relayer2, "恢复后：m2 继续推进，m1 结果保留")

    print("\n## 5. 状态迁移日志（可追踪处理记录）")
    for t in relayer2.transitions:
        print(f"  msg={t.message_id}: {t.from_status} -> {t.to_status} | {t.reason}")

    print(f"\n状态文件: {workdir / 'relayer-state.json'}")


if __name__ == "__main__":
    main()
