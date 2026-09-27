"""跨链消息中继测试：重放、乱序、并发、恢复；断言守恒与幂等并打印追踪日志。"""
from __future__ import annotations

import threading

import pytest

from relayer import (
    CrossChainMessage,
    LocalChainLedger,
    MessageStatus,
    RejectReason,
    Relayer,
    StateStore,
)


def make_chains():
    a = LocalChainLedger("A", confirmations=2,
                         genesis_balances={"alice": 100, "carol": 50})
    b = LocalChainLedger("B", confirmations=2)
    return a, b


def make_msg(mid, source, nonce, sender, recipient, amount, block):
    target = "B" if source == "A" else "A"
    return CrossChainMessage(mid, source, target, nonce,
                             sender, recipient, amount, block)


def finalize(chain, relayer):
    """推进足够区块使挂起消息最终化，并执行。"""
    for _ in range(chain.confirmations):
        chain.produce_block()
    ready = relayer.poll_finality()
    executed = relayer.execute_ready()
    return ready, executed


def print_transitions(relayer, title):
    print(f"\n=== {title} ===")
    for t in relayer.transitions:
        print(f"[迁移] msg={t.message_id} {t.from_status} -> {t.to_status} | {t.reason}")


def assert_conservation(*chains):
    for c in chains:
        assert c.check_conservation(), f"{c.name} 守恒被破坏"


# ---------- 重放 ----------

def test_duplicate_and_replay_are_distinguished_and_idempotent():
    a, b = make_chains()
    r = Relayer({"A": a, "B": b}, home_chain="A")

    m1 = make_msg("m1", "A", 1, "alice", "bob", 40, a.height + 1)
    rec = r.submit(m1)
    assert rec.status is MessageStatus.PENDING_FINALITY
    assert a.locked == 40

    # 未决消息的重复提交：DUPLICATE_MESSAGE，且无额外副作用
    dup = r.submit(m1)
    assert dup.status is MessageStatus.PENDING_FINALITY
    assert a.locked == 40 and a.balances["alice"] == 60
    assert r.transitions[-1].reason == RejectReason.DUPLICATE_MESSAGE.value

    ready, executed = finalize(a, r)
    assert ready == ["m1"] and executed == ["m1"]
    assert b.balances["bob"] == 40

    # 已执行消息的重放：REPLAY_AFTER_EXECUTION，且不重复铸造
    replay = r.submit(m1)
    assert replay.status is MessageStatus.EXECUTED
    assert r.transitions[-1].reason == RejectReason.REPLAY_AFTER_EXECUTION.value
    assert b.balances["bob"] == 40 and b.total_supply == 40

    # 重复执行同样幂等
    assert r.execute_ready() == []
    assert b.balances["bob"] == 40
    assert_conservation(a, b)
    print_transitions(r, "重放场景：消息 m1 的迁移与拒绝记录")


# ---------- 乱序 ----------

def test_out_of_order_rejected_then_correct_nonce_accepted():
    a, b = make_chains()
    r = Relayer({"A": a, "B": b}, home_chain="A")

    # nonce=2 在 nonce=1 之前提交：明确拒绝
    early = r.submit(make_msg("m2", "A", 2, "alice", "bob", 10, a.height + 1))
    assert early.status is MessageStatus.REJECTED
    assert early.reject_reason is RejectReason.OUT_OF_ORDER
    assert a.locked == 0  # 拒绝不产生资产副作用

    # 按序提交 nonce=1 被接受
    ok1 = r.submit(make_msg("m1", "A", 1, "alice", "bob", 10, a.height + 1))
    assert ok1.status is MessageStatus.PENDING_FINALITY
    # 之后 nonce=2 也可接受（新的 message_id，因 m2 已被拒占用了标识）
    ok2 = r.submit(make_msg("m2b", "A", 2, "alice", "bob", 10, a.height + 1))
    assert ok2.status is MessageStatus.PENDING_FINALITY

    ready, executed = finalize(a, r)
    # 确定的顺序语义：按 nonce 升序执行
    assert executed == ["m1", "m2b"]
    assert b.balances["bob"] == 20
    assert r.nonces["A"] == 2
    assert_conservation(a, b)
    print_transitions(r, "乱序场景：m2 被拒、m1/m2b 按序执行")


# ---------- 并发 ----------

def test_concurrent_submit_and_execute_preserves_conservation():
    a, b = make_chains()
    r = Relayer({"A": a, "B": b}, home_chain="A")
    n = 20
    errors = []
    # block=0 表示由账本在线程安全地分配到下一个高度
    msgs = [make_msg(f"c{i}", "A", i + 1, "alice", "bob", 1, 0)
            for i in range(n)]

    def submitter(i):
        try:
            r.submit(msgs[i])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submitter, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 并发提交后，把被接受的按序补齐执行
    for _ in range(a.confirmations):
        a.produce_block()
    r.poll_finality()
    r.execute_ready()

    accepted = [rec for rec in r.records.values()
                if rec.status is MessageStatus.EXECUTED]
    total = sum(rec.message.amount for rec in accepted)
    assert b.total_supply == total == a.locked
    assert b.balances["bob"] == total
    # 已执行 nonce 必须是从 1 开始的连续前缀（确定顺序语义）
    nonces = sorted(rec.message.nonce for rec in accepted)
    assert nonces == list(range(1, len(nonces) + 1))
    assert_conservation(a, b)
    print(f"\n=== 并发场景：{n} 个并发提交，{len(accepted)} 个按序执行，"
          f"总量 {total} 守恒 ===")


# ---------- 恢复 ----------

def test_restart_recovery_resumes_pending_and_keeps_executed(tmp_path):
    store_path = tmp_path / "state.json"
    a, b = make_chains()
    r1 = Relayer({"A": a, "B": b}, home_chain="A", store=StateStore(store_path))

    m1 = make_msg("m1", "A", 1, "alice", "bob", 30, a.height + 1)
    r1.submit(m1)
    finalize(a, r1)  # m1 执行完毕
    assert b.balances["bob"] == 30

    # m2 已提交但未最终化时“重启”
    m2 = make_msg("m2", "A", 2, "alice", "bob", 20, a.height + 1)
    r1.submit(m2)

    r2 = Relayer({"A": a, "B": b}, home_chain="A", store=StateStore(store_path))
    for _ in range(a.confirmations):
        a.produce_block()
    resumed = r2.recover()
    assert "m2" in resumed
    # 已执行的 m1 不被回退也不重复执行
    assert r2.records["m1"].status is MessageStatus.EXECUTED
    assert b.balances["bob"] == 50 and b.total_supply == 50
    assert a.locked == 50
    assert_conservation(a, b)
    print_transitions(r2, "恢复场景：重启后续作 m2，m1 结果保留")


def test_source_reorg_invalidates_pending_message(tmp_path):
    a, b = make_chains()
    r = Relayer({"A": a, "B": b}, home_chain="A", store=StateStore(tmp_path / "s.json"))

    m1 = make_msg("m1", "A", 1, "alice", "bob", 30, a.height + 1)
    r.submit(m1)
    assert a.locked == 30

    # 来源链在最终化前回滚，m1 所在区块被移除
    removed = a.reorg(0)
    assert removed == ["m1"]

    r.poll_finality()
    rec = r.records["m1"]
    assert rec.status is MessageStatus.REJECTED
    assert rec.reject_reason is RejectReason.SOURCE_REORGED
    # 回滚后执行不得铸造
    assert r.execute_ready() == []
    assert b.total_supply == 0
    assert_conservation(a, b)
    print_transitions(r, "回滚场景：m1 因来源链回滚被拒绝")


def test_reorg_after_finality_does_not_revert_executed(tmp_path):
    a, b = make_chains()
    r = Relayer({"A": a, "B": b}, home_chain="A", store=StateStore(tmp_path / "s.json"))

    m1 = make_msg("m1", "A", 1, "alice", "bob", 30, a.height + 1)
    r.submit(m1)
    finalize(a, r)
    assert r.records["m1"].status is MessageStatus.EXECUTED

    # 模拟深层回滚 + 重启恢复：已执行结果不得被回退
    a.reorg(0)
    r2 = Relayer({"A": a, "B": b}, home_chain="A", store=StateStore(tmp_path / "s.json"))
    r2.recover()
    assert r2.records["m1"].status is MessageStatus.EXECUTED
    assert b.balances["bob"] == 30
    print_transitions(r2, "回滚场景：已执行的 m1 不被回退")


# ---------- 闭环与守恒 ----------

def test_full_loop_lock_mint_burn_unlock_conservation():
    a, b = make_chains()
    r = Relayer({"A": a, "B": b}, home_chain="A")

    # A->B：锁定 + 铸造
    r.submit(make_msg("out", "A", 1, "alice", "bob", 40, a.height + 1))
    finalize(a, r)
    assert a.locked == 40 and b.total_supply == 40

    # B->A：销毁 + 解锁
    r.submit(make_msg("back", "B", 1, "bob", "alice", 15, b.height + 1))
    finalize(b, r)
    assert b.total_supply == 25 and a.locked == 25
    assert a.balances["alice"] == 75

    # 全局守恒：A 链锁定托管 == B 链铸造流通
    assert a.locked == b.total_supply
    assert_conservation(a, b)

    snap = r.snapshot()
    assert all(c["conservation"] for c in snap["chains"].values())
    print(f"\n=== 闭环快照 ===\n{snap}")
    print_transitions(r, "闭环场景：lock/mint/burn/unlock 全生命周期")
