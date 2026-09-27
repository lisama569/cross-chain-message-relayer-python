"""跨链消息中继测试：重放、乱序、并发、恢复、回滚与资产守恒。

运行 pytest -s 可看到每条消息的标识、最终性依据与状态迁移日志。
"""
from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from relayer import (
    JsonStore,
    LedgerError,
    LocalChainLedger,
    Message,
    MessageStatus,
    Relayer,
    RejectReason,
)
from relayer.relayer import SubmitRejected

GENESIS = 100
IN_FLIGHT = {MessageStatus.PENDING, MessageStatus.FINALIZED,
             MessageStatus.FAILED_RETRYABLE}


@pytest.fixture()
def env(tmp_path):
    A = LocalChainLedger("A", finalized_depth=2)
    B = LocalChainLedger("B", finalized_depth=2)
    store = JsonStore(tmp_path / "relayer-state.json")
    r = Relayer({"A": A, "B": B}, store, asset_home={"TOK": "A"})
    A.mine_block()
    A.mint("alice", "TOK", GENESIS, "genesis")
    A.mine_block()
    A.mine_block()  # 使 genesis 所在区块最终化，后续回滚不会触及
    return r, A, B, store


def make_msg(nonce=1, amount=30, sender="alice", recipient="bob",
             src="A", dst="B", mid=None):
    m = Message(src, dst, sender, recipient, "TOK", amount, nonce, src_block=0)
    if mid:
        m = replace(m, message_id=mid)
    return m


def finalize(chain, n=3):
    for _ in range(n):
        chain.mine_block()


def assert_conservation(r, *chains):
    in_flight = sum(rec.message.amount for rec in r.records().values()
                    if rec.status in IN_FLIGHT)
    total = sum(c.circulating_supply("TOK") for c in chains) + in_flight
    assert total == GENESIS, f"conservation violated: {total} != {GENESIS}"


def show(rec):
    print(f"\n[msg {rec.message.message_id[:8]}] "
          f"nonce={rec.message.nonce} amount={rec.message.amount} "
          f"status={rec.status.value}")
    if rec.finality_proof:
        print(f"  finality: {rec.finality_proof}")
    for h in rec.history:
        print(f"  transition: {h}")


# ---------- 闭环与守恒 ----------

def test_lock_mint_burn_unlock_closed_loop(env):
    r, A, B, _ = env
    # 正向 A->B：锁定 30，铸造 30
    m1 = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m1)
    assert_conservation(r, A, B)
    finalize(A)
    rec = r.process(m1)
    show(rec)
    assert rec.status is MessageStatus.EXECUTED
    assert A.locked_of("alice", "TOK") == 30
    assert B.balance_of("bob", "TOK") == 30
    assert_conservation(r, A, B)

    # 回程 B->A：销毁 10，解锁 10
    B.mine_block()
    m2 = r.prepare_source(make_msg(nonce=1, amount=10, sender="bob",
                                   recipient="alice", src="B", dst="A"))
    r.submit(m2)
    finalize(B)
    rec2 = r.process(m2)
    show(rec2)
    assert rec2.status is MessageStatus.EXECUTED
    assert A.balance_of("alice", "TOK") == 80
    assert A.locked_of("alice", "TOK") == 20
    assert B.balance_of("bob", "TOK") == 20
    assert_conservation(r, A, B)


# ---------- 重放与重复 ----------

def test_duplicate_submit_is_idempotent(env):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    finalize(A)
    rec1 = r.process(m)
    # 重复提交同一消息（负载一致）：幂等返回，不产生额外副作用
    rec2 = r.submit(m)
    rec3 = r.process(m)
    show(rec2)
    assert rec1 is rec2 is rec3
    assert rec2.status is MessageStatus.EXECUTED
    assert any("DUPLICATE_MESSAGE" in h for h in rec2.history)
    assert B.balance_of("bob", "TOK") == 30  # 未重复铸造
    assert_conservation(r, A, B)


def test_replay_conflict_rejected(env):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    evil = replace(m, amount=31)  # 同一 message_id，不同负载
    with pytest.raises(SubmitRejected) as ei:
        r.submit(evil)
    print(f"\nreplay conflict rejected: {ei.value.reason.value}")
    assert ei.value.reason is RejectReason.REPLAY_CONFLICT
    assert_conservation(r, A, B)


def test_invalid_payload_rejected_and_nonce_not_consumed(env):
    r, A, B, _ = env
    bad = make_msg(nonce=1, amount=0)
    rec = r.submit(bad)
    show(rec)
    assert rec.status is MessageStatus.REJECTED
    assert rec.reject_reason is RejectReason.INVALID_PAYLOAD
    # nonce 未被拒绝消息消耗，nonce=1 仍可使用
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    assert r.submit(m).status is MessageStatus.PENDING


# ---------- 顺序语义 ----------

def test_out_of_order_nonce_rejected(env):
    r, A, B, _ = env
    m2 = r.prepare_source(make_msg(nonce=2, amount=5))
    with pytest.raises(SubmitRejected) as ei:
        r.submit(m2)
    print(f"\nout-of-order rejected: {ei.value.reason}")
    assert ei.value.reason is RejectReason.NONCE_OUT_OF_ORDER
    # 按顺序提交后正常推进
    m1 = r.prepare_source(make_msg(nonce=1, amount=30))
    assert r.submit(m1).status is MessageStatus.PENDING
    assert r.submit(m2).status is MessageStatus.PENDING
    finalize(A)
    assert r.process(m1).status is MessageStatus.EXECUTED
    assert r.process(m2).status is MessageStatus.EXECUTED
    assert B.balance_of("bob", "TOK") == 35
    assert_conservation(r, A, B)


# ---------- 最终性 ----------

def test_delivery_refused_before_finality(env):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    A.mine_block()  # 只推进 1 个区块，finalized_depth=2，尚未最终
    with pytest.raises(SubmitRejected) as ei:
        r.deliver(m.message_id)
    print(f"\nearly delivery rejected: {ei.value.reason}")
    assert ei.value.reason is RejectReason.SOURCE_NOT_FINAL
    assert B.balance_of("bob", "TOK") == 0
    finalize(A)
    rec = r.deliver(m.message_id)
    show(rec)
    assert rec.status is MessageStatus.EXECUTED
    assert rec.finality_proof is not None
    assert_conservation(r, A, B)


def test_source_rollback_reverts_unfinalized_escrow(env):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    assert A.balance_of("alice", "TOK") == 70
    # 来源链回滚掉托管事件（尚未最终）：资金返还，消息终态拒绝
    affected = r.on_source_rollback("A", A.finalized_height)
    rec = r.get(m.message_id)
    show(rec)
    assert rec in affected
    assert rec.status is MessageStatus.REJECTED
    assert rec.reject_reason is RejectReason.SOURCE_REORGED
    assert A.balance_of("alice", "TOK") == 100  # 锁定被撤销
    assert B.balance_of("bob", "TOK") == 0
    assert_conservation(r, A, B)


def test_finalized_record_survives_rollback_attempt(env):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    finalize(A)
    r.confirm(m.message_id)
    # 已最终的区块不可回滚
    with pytest.raises(LedgerError):
        A.rollback(0)
    rec = r.deliver(m.message_id)
    assert rec.status is MessageStatus.EXECUTED


# ---------- 并发 ----------

def test_concurrent_deliver_is_idempotent(env):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    finalize(A)
    r.confirm(m.message_id)
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(r.deliver(m.message_id).status))
        for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(s is MessageStatus.EXECUTED for s in results)
    assert B.balance_of("bob", "TOK") == 30  # 恰好铸造一次
    assert_conservation(r, A, B)


def test_concurrent_lanes_and_conservation(env):
    r, A, B, _ = env
    A.mint("carol", "TOK", 50, "genesis2")
    msgs = []
    for i, sender in enumerate(["alice", "carol"]):
        amt = 10 + i * 5
        m = r.prepare_source(make_msg(nonce=1, amount=amt, sender=sender))
        r.submit(m)
        msgs.append(m)
    finalize(A)
    threads = [threading.Thread(target=r.process, args=(m,)) for m in msgs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for m in msgs:
        rec = r.get(m.message_id)
        show(rec)
        assert rec.status is MessageStatus.EXECUTED
    assert B.balance_of("bob", "TOK") == 25
    in_flight = sum(rec.message.amount for rec in r.records().values()
                    if rec.status in IN_FLIGHT)
    assert (A.circulating_supply("TOK") + B.circulating_supply("TOK")
            + in_flight) == GENESIS + 50


# ---------- 失败重试 ----------

def test_failed_execution_is_retryable_without_side_effects(env, monkeypatch):
    r, A, B, _ = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    finalize(A)
    r.confirm(m.message_id)
    # 第一次投递时目标链故障
    def boom(*a, **k):
        raise LedgerError("simulated dst chain outage")
    monkeypatch.setattr(B, "mint", boom)
    rec = r.deliver(m.message_id)
    show(rec)
    assert rec.status is MessageStatus.FAILED_RETRYABLE
    assert B.balance_of("bob", "TOK") == 0
    monkeypatch.undo()
    # 重试成功，且只铸造一次
    rec = r.deliver(m.message_id)
    show(rec)
    assert rec.status is MessageStatus.EXECUTED
    assert B.balance_of("bob", "TOK") == 30
    assert_conservation(r, A, B)


# ---------- 重启与恢复 ----------

def test_restart_recovers_pending_and_finalized(env, tmp_path):
    r, A, B, store = env
    m1 = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m1)                      # PENDING
    m2 = r.prepare_source(make_msg(nonce=2, amount=5))
    r.submit(m2)
    finalize(A)
    r.confirm(m2.message_id)          # FINALIZED，未投递
    # 模拟中继重启：新实例，同一 store 与链
    r2 = Relayer({"A": A, "B": B}, store, asset_home={"TOK": "A"})
    advanced = r2.recover()
    for rec in advanced:
        show(rec)
    assert r2.get(m1.message_id).status is MessageStatus.EXECUTED
    assert r2.get(m2.message_id).status is MessageStatus.EXECUTED
    assert B.balance_of("bob", "TOK") == 35  # 各铸造一次
    # 恢复后 nonce 状态正确，可继续提交 nonce=3
    m3 = r2.prepare_source(make_msg(nonce=3, amount=1))
    assert r2.submit(m3).status is MessageStatus.PENDING
    assert_conservation(r2, A, B)


def test_recovery_reconciles_partial_execution(env, tmp_path):
    r, A, B, store = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    finalize(A)
    r.confirm(m.message_id)
    # 模拟崩溃：链上 mint 已发生，但状态未落盘为 EXECUTED
    B.mint("bob", "TOK", 30, m.message_id)
    r2 = Relayer({"A": A, "B": B}, store, asset_home={"TOK": "A"})
    advanced = r2.recover()
    rec = r2.get(m.message_id)
    show(rec)
    assert rec.status is MessageStatus.EXECUTED
    assert B.balance_of("bob", "TOK") == 30  # 未重复铸造
    assert_conservation(r2, A, B)


def test_executed_results_survive_restart_and_rollback(env, tmp_path):
    r, A, B, store = env
    m = r.prepare_source(make_msg(nonce=1, amount=30))
    r.submit(m)
    finalize(A)
    assert r.process(m).status is MessageStatus.EXECUTED
    # 重启后已执行记录保持 EXECUTED
    r2 = Relayer({"A": A, "B": B}, store, asset_home={"TOK": "A"})
    r2.recover()
    rec = r2.get(m.message_id)
    show(rec)
    assert rec.status is MessageStatus.EXECUTED
    # 已最终的来源区块不可回滚
    with pytest.raises(LedgerError):
        A.rollback(0)
    assert B.balance_of("bob", "TOK") == 30
    assert_conservation(r2, A, B)
