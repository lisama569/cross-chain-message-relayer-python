"""本地跨链消息中继演示：锁定-铸造-销毁-解锁闭环 + 重启恢复。

运行：python main.py
"""
import tempfile
from pathlib import Path

from relayer import JsonStore, LocalChainLedger, Message, MessageStatus, Relayer


def show(rec):
    print(f"[msg {rec.message.message_id[:8]}] "
          f"{rec.message.src_chain}->{rec.message.dst_chain} "
          f"nonce={rec.message.nonce} amount={rec.message.amount} "
          f"status={rec.status.value}")
    if rec.finality_proof:
        print(f"  finality: {rec.finality_proof}")
    for h in rec.history:
        print(f"  transition: {h}")


def main():
    workdir = Path(tempfile.mkdtemp(prefix="relayer-demo-"))
    store = JsonStore(workdir / "relayer-state.json")
    A = LocalChainLedger("A", finalized_depth=2)
    B = LocalChainLedger("B", finalized_depth=2)
    relayer = Relayer({"A": A, "B": B}, store, asset_home={"TOK": "A"})

    A.mine_block()
    A.mint("alice", "TOK", 100, "genesis")
    print("== genesis: alice@A 100 TOK ==")

    # 1) A -> B：锁定 30，等待最终性，铸造 30
    msg = Message("A", "B", "alice", "bob", "TOK", 30, nonce=1, src_block=0)
    msg = relayer.prepare_source(msg)
    relayer.submit(msg)
    print("\n== submitted, waiting for finality ==")
    for _ in range(3):
        A.mine_block()
    rec = relayer.process(msg)
    show(rec)

    # 2) 模拟中继重启：新实例从 store 恢复
    print("\n== relayer restart: recover from store ==")
    relayer2 = Relayer({"A": A, "B": B}, store, asset_home={"TOK": "A"})
    for rec in relayer2.recover():
        show(rec)

    # 3) B -> A：销毁 10，解锁 10
    B.mine_block()
    back = Message("B", "A", "bob", "alice", "TOK", 10, nonce=1, src_block=0)
    back = relayer2.prepare_source(back)
    relayer2.submit(back)
    for _ in range(3):
        B.mine_block()
    rec = relayer2.process(back)
    show(rec)

    print("\n== final snapshots ==")
    for chain in (A, B):
        snap = chain.snapshot()
        print(f"{snap['chain_id']}: height={snap['height']} "
              f"finalized={snap['finalized_height']} "
              f"balances={snap['balances']} locked={snap['locked']}")
    in_flight = sum(r.message.amount for r in relayer2.records().values()
                    if r.status in (MessageStatus.PENDING,
                                    MessageStatus.FINALIZED,
                                    MessageStatus.FAILED_RETRYABLE))
    total = (A.circulating_supply("TOK") + B.circulating_supply("TOK")
             + in_flight)
    print(f"conservation: circulating(A)+circulating(B)+in_flight = {total} "
          f"(expected 100)")


if __name__ == "__main__":
    main()
