"""券务引擎领域规则测试：只依赖内存 SQLite，不启动网络服务。"""

import unittest

from voucher.engine import Engine, RuleError

REGISTERS = {
    # 家庭券总额 10000，小于两台终端离线权利合计 11000，用于验证跨终端双花拦截。
    "coupon": {"id": "c-1", "household": "house-1", "cap_cents": 10000,
               "members": ["h-a", "h-b"], "expires": 100},
    "merchant_rules": {"m-a": ["rubbing"], "m-b": ["paper"],
                       "m-c": ["rubbing", "paper"]},
    "offline_grants": [
        # 两台终端的离线权利合计 11000，小于家庭券总额 12000。
        {"terminal": "t-1", "merchant": "m-a", "cap_cents": 6500,
         "valid_until": 70},
        {"terminal": "t-2", "merchant": "m-b", "cap_cents": 4500,
         "valid_until": 65},
    ],
    "purchase": [
        {"line": "L1", "activity": "rubbing", "price": 4200},
        {"line": "L2", "activity": "tea", "price": 2300},
    ],
}


def use_event(cursor, minute, receipt, *, member="h-a", terminal="t-1",
              activity="rubbing", benefit=4000, actual=None):
    return {"cursor": cursor, "source": "terminal", "minute": minute,
            "payload": {"receipt": receipt, "actual_minute": actual or minute,
                        "member": member, "terminal": terminal,
                        "activity": activity, "benefit_cents": benefit}}


class EngineRuleTest(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(":memory:")
        self.engine.load_registers(REGISTERS)

    def tearDown(self):
        self.engine.close()

    def test_load_registers_is_idempotent(self):
        first = self.engine.load_registers(REGISTERS)
        second = self.engine.load_registers(REGISTERS)
        self.assertEqual(first, second)
        self.assertEqual(second["grants"], 2)

    def test_accept_use_holds_grant_and_household_capacity(self):
        result = self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        self.assertEqual(result["status"], "accepted")
        view = self.engine.household_view("house-1")
        self.assertEqual(view["used_cents"], 4000)
        # 暂占用为各有效权利的空闲容量：11000 - 已确认核用 4000。
        self.assertEqual(view["held_cents"], 7000)
        # 可用额度取券余额（10000-4000）与权利空闲容量（7000）的较小值。
        self.assertEqual(view["available_cents"], 6000)
        self.assertEqual(view["coupon_remaining_cents"], 6000)

    def test_duplicate_receipt_does_not_double_spend_or_double_claim(self):
        event = use_event(1, 30, "r-1", benefit=4000)
        self.engine.ingest_event(event)
        again = self.engine.ingest_event(event)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["reject_reason"], "duplicate_receipt")
        funds = self.engine.committee_funds()
        self.assertEqual(funds["claims_by_status"]["payable"]["count"], 1)
        self.assertEqual(funds["claims_by_status"]["payable"]["cents"], 4000)

    def test_two_members_two_terminals_share_household_pool(self):
        # 第一名成员在 t-1 用掉家庭池的 6500（t-1 权利上限）。
        self.engine.ingest_event(use_event(1, 30, "r-1", member="h-a",
                                           terminal="t-1", benefit=6500))
        # 第二名成员在 t-2 再用 4000：终端容量够，但 6500+4000 > 家庭券 10000。
        result = self.engine.ingest_event(use_event(
            2, 31, "r-2", member="h-b", terminal="t-2", activity="paper",
            benefit=4000))
        self.assertEqual(result["reject_reason"],
                         "household_capacity_exceeded")
        # 家庭池剩余 3500 时，另一成员在另一终端可正常核用。
        ok = self.engine.ingest_event(use_event(
            3, 32, "r-3", member="h-b", terminal="t-2", activity="paper",
            benefit=3500))
        self.assertEqual(ok["status"], "accepted")
        self.assertEqual(
            self.engine.household_view("house-1")["used_cents"], 10000)

    def test_grant_capacity_caps_terminal_even_if_household_has_room(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        result = self.engine.ingest_event(use_event(2, 31, "r-2", benefit=3000))
        self.assertEqual(result["reject_reason"], "grant_capacity_exceeded")

    def test_paused_merchant_rejects_new_use_but_keeps_earlier_claim(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        self.engine.ingest_event({"cursor": 2, "source": "merchant",
                                  "minute": 35,
                                  "payload": {"id": "m-a",
                                              "status": "paused"}})
        result = self.engine.ingest_event(use_event(3, 36, "r-2", benefit=1000))
        self.assertEqual(result["reject_reason"], "merchant_paused")
        earlier = self.engine.get_use("r-1")
        self.assertEqual(earlier["status"], "accepted")
        reconciliation = self.engine.merchant_reconciliation("m-a")
        self.assertEqual(reconciliation["uses"][0]["claim"]["status"], "payable")

    def test_pause_uses_actual_minute_and_keeps_pre_pause_receipts(self):
        self.engine.ingest_event({"cursor": 1, "source": "merchant",
                                  "minute": 35,
                                  "payload": {"id": "m-a",
                                              "status": "paused"}})
        # 实际发生在暂停前、暂停后才晚报的凭条：交易当时权利有效，仍可申领。
        early = self.engine.ingest_event(use_event(2, 50, "r-early", actual=30,
                                                   benefit=1000))
        self.assertEqual(early["status"], "accepted")
        # 实际发生在暂停后的交易，即使凭条日期被写早也不能取得补贴：
        # actual_minute 是凭证反映的真实交易时点，晚于暂停即拒。
        late = self.engine.ingest_event(use_event(3, 51, "r-late", actual=36,
                                                  benefit=1000))
        self.assertEqual(late["reject_reason"], "merchant_paused")

    def test_resume_allows_new_uses_again(self):
        self.engine.ingest_event({"cursor": 1, "source": "merchant",
                                  "minute": 35,
                                  "payload": {"id": "m-a",
                                              "status": "paused"}})
        self.engine.ingest_event({"cursor": 2, "source": "merchant",
                                  "minute": 45,
                                  "payload": {"id": "m-a",
                                              "status": "active"}})
        result = self.engine.ingest_event(use_event(3, 50, "r-ok", actual=46,
                                                    benefit=1000))
        self.assertEqual(result["status"], "accepted")

    def test_grant_expiry_uses_actual_minute(self):
        result = self.engine.ingest_event(use_event(1, 75, "r-late", actual=71,
                                                    benefit=1000))
        self.assertEqual(result["reject_reason"], "grant_expired")
        within = self.engine.ingest_event(use_event(2, 75, "r-ok", actual=70,
                                                    benefit=1000))
        self.assertEqual(within["status"], "accepted")

    def test_coupon_expiry_uses_actual_minute(self):
        # 独立台账：券在 60 到期，但离线权利有效到 70，先撞券有效期。
        import copy

        registers = copy.deepcopy(REGISTERS)
        registers["coupon"]["expires"] = 60
        engine = Engine(":memory:")
        self.addCleanup(engine.close)
        engine.load_registers(registers)
        result = engine.ingest_event(use_event(1, 59, "r-late", actual=61,
                                               benefit=1000))
        self.assertEqual(result["reject_reason"], "coupon_expired")
        within = engine.ingest_event(use_event(2, 59, "r-ok", actual=60,
                                               benefit=1000))
        self.assertEqual(within["status"], "accepted")

    def test_unknown_member_and_terminal_are_rejected(self):
        result = self.engine.ingest_event(use_event(1, 20, "r-x", member="nobody"))
        self.assertEqual(result["reject_reason"], "unknown_member")
        result = self.engine.ingest_event(use_event(2, 20, "r-y",
                                                    terminal="t-9"))
        self.assertEqual(result["reject_reason"], "terminal_without_grant")

    def test_ineligible_activity_becomes_self_pay(self):
        result = self.engine.ingest_event(use_event(
            1, 30, "r-mix", activity="tea", benefit=2300))
        self.assertEqual(result["reject_reason"], "activity_not_offered")
        rejected = self.engine.get_use("r-mix")
        self.assertEqual(rejected["benefit_cents"], 0)

    def test_multiline_transaction_splits_benefit_and_self_pay(self):
        event = {"cursor": 1, "source": "terminal", "minute": 30, "payload": {
            "receipt": "r-multi", "actual_minute": 30, "member": "h-a",
            "terminal": "t-1",
            "lines": [
                {"line": "L1", "activity": "rubbing", "price": 4200},
                {"line": "L2", "activity": "tea", "price": 2300},
            ]}}
        result = self.engine.ingest_event(event)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["benefit_cents"], 4200)
        self.assertEqual(result["self_pay_cents"], 2300)
        view = self.engine.get_use("r-multi")
        self.assertFalse(view["lines"][1]["eligible"])
        self.assertEqual(view["lines"][1]["self_pay_cents"], 2300)

    def test_return_line_restores_capacity_only_while_coupon_valid(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        result = self.engine.ingest_event({
            "cursor": 2, "source": "household", "minute": 40,
            "payload": {"request": "return", "receipt": "r-1",
                        "line": "L1"}})
        self.assertTrue(result["reusable"])
        self.assertEqual(result["returned_cents"], 4000)
        view = self.engine.household_view("house-1")
        self.assertEqual(view["used_cents"], 0)
        # 离线权利容量同样腾回，可被同终端后续核用使用。
        follow = self.engine.ingest_event(use_event(3, 41, "r-2", benefit=5000))
        self.assertEqual(follow["status"], "accepted")

    def test_return_after_coupon_expiry_is_not_reusable(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        result = self.engine.ingest_event({
            "cursor": 2, "source": "household", "minute": 105,
            "payload": {"request": "return", "receipt": "r-1",
                        "line": "L1"}})
        self.assertFalse(result["reusable"])
        view = self.engine.household_view("house-1")
        self.assertEqual(view["used_cents"], 4000)

    def test_return_cancels_unpaid_claim_and_duplicate_return_fails(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        self.engine.ingest_event({"cursor": 2, "source": "household",
                                  "minute": 40,
                                  "payload": {"request": "return",
                                              "receipt": "r-1",
                                              "line": "L1"}})
        funds = self.engine.committee_funds()
        self.assertEqual(funds["claims_by_status"]["cancelled"]["count"], 1)
        with self.assertRaises(RuleError) as ctx:
            self.engine.ingest_event({"cursor": 3, "source": "household",
                                      "minute": 41,
                                      "payload": {"request": "return",
                                                  "receipt": "r-1",
                                                  "line": "L1"}})
        self.assertEqual(ctx.exception.reason, "already_returned")

    def test_reallocation_waits_for_close_and_only_moves_confirmed_free(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        self.engine.ingest_event({
            "cursor": 2, "source": "merchant", "minute": 40,
            "payload": {"request": "reallocate", "terminal": "t-1",
                        "to": "t-3"}})
        pending = self.engine.list_reallocations()[0]
        self.assertEqual(pending["status"], "pending")
        done = self.engine.close_grant("t-1", 50, transfer_to="t-3")
        self.assertEqual(done["confirmed_used_cents"], 4000)
        self.assertEqual(done["transferred_cents"], 2500)
        funds = self.engine.committee_funds()
        new_grant = next(g for g in funds["grants"] if g["terminal"] == "t-3")
        self.assertEqual(new_grant["cap_cents"], 2500)
        # 结清后再送达的迟到凭条不会占新终端的额度，留痕可追查。
        late = self.engine.ingest_event(use_event(3, 55, "r-late", benefit=1000))
        self.assertEqual(late["reject_reason"], "grant_closed")

    def test_unreported_is_not_treated_as_unused(self):
        # 未报核用无法凭空假定：请求改配时仅按已确认数字腾出容量。
        self.engine.ingest_event({
            "cursor": 1, "source": "merchant", "minute": 40,
            "payload": {"request": "reallocate", "terminal": "t-1",
                        "to": "t-3"}})
        done = self.engine.close_grant("t-1", 50, transfer_to="t-3")
        self.assertEqual(done["confirmed_used_cents"], 0)
        self.assertEqual(done["transferred_cents"], 6500)

    def test_concurrent_uses_never_overspend_household_pool(self):
        # 两台终端各自权利充足，但家庭券只有 10000；并发核用不得超花。
        from concurrent.futures import ThreadPoolExecutor

        def fire(index):
            terminal = "t-1" if index % 2 == 0 else "t-2"
            member = "h-a" if index % 2 == 0 else "h-b"
            activity = "rubbing" if index % 2 == 0 else "paper"
            return self.engine.ingest_event(use_event(
                index + 1, 30 + index, f"r-c{index}", member=member,
                terminal=terminal, activity=activity, benefit=3000))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fire, range(8)))
        accepted = [r for r in results if r.get("status") in
                    ("accepted", "partial")]
        # 每笔 3000，家庭券 10000，最多成立 3 笔。
        self.assertEqual(len(accepted), 3)
        self.assertLessEqual(
            self.engine.household_view("house-1")["used_cents"], 10000)


class FakeClient:
    """模拟合作方回执通道，支持首次丢回复。"""

    def __init__(self, lose_first=()):
        self.lose_first = set(lose_first)
        self.records = {}
        self.calls = []
        self.lookups = []

    def send_receipt(self, reference, operation, recipient):
        self.calls.append((reference, operation, recipient))
        if reference in self.lose_first:
            self.lose_first.discard(reference)
            self.records[reference] = {"state": "paid",
                                       "receipt": f"wire-{reference}"}
            # 模拟真实链路：首次投递对端已落账但连接断开，随后查到落账记录。
            return self.lookup_receipt(reference)
        self.records.setdefault(reference, {"state": "paid",
                                            "receipt": f"wire-{reference}"})
        return self.records[reference]

    def lookup_receipt(self, reference):
        self.lookups.append(reference)
        return self.records.get(reference)

    def fetch_events(self, after=0, limit=50):
        raise AssertionError("not used here")


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(":memory:")
        self.engine.load_registers(REGISTERS)

    def tearDown(self):
        self.engine.close()

    def test_paid_return_becomes_clawback_without_erasing_payout(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        client = FakeClient()
        payout = self.engine.pay_due_claims(client, minute=45)
        self.assertEqual(payout["paid"][0]["amount_cents"], 4000)
        funds = self.engine.committee_funds()
        self.assertEqual(funds["paid_total_cents"], 4000)

        self.engine.ingest_event({"cursor": 2, "source": "household",
                                  "minute": 50,
                                  "payload": {"request": "return",
                                              "receipt": "r-1",
                                              "line": "L1"}})
        # payout 历史保留；退订形成待扣回。
        reconciliation = self.engine.merchant_reconciliation("m-a")
        self.assertEqual(reconciliation["paid_total_cents"], 4000)
        self.assertEqual(reconciliation["pending_clawback_cents"], 4000)

        settlement = self.engine.run_settlement("m-a", 60)
        self.assertEqual(settlement["gross_cents"], 0)
        self.assertEqual(settlement["clawback_cents"], 4000)
        self.assertEqual(settlement["net_cents"], -4000)
        # 再次结算不重复扣回。
        again = self.engine.run_settlement("m-a", 61)
        self.assertEqual(again["net_cents"], 0)

    def test_lost_reply_retries_using_idempotent_reference(self):
        self.engine.ingest_event(use_event(1, 30, "r-1", benefit=4000))
        client = FakeClient(lose_first={"pay-r-1"})
        payout = self.engine.pay_due_claims(client, minute=45)
        self.assertEqual(payout["paid"][0]["receipt"]["receipt"], "wire-pay-r-1")
        # 断连后先查已落账记录：一次投递 + 一次查询，对端同一编号只落一笔。
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.lookups, ["pay-r-1"])
        self.assertEqual(
            self.engine.committee_funds()["paid_total_cents"], 4000)
        # 再跑兑付：队列已空，不会产生第二笔到账。
        again = self.engine.pay_due_claims(FakeClient(), minute=46)
        self.assertEqual(again["paid"], [])
        self.assertEqual(
            self.engine.committee_funds()["paid_total_cents"], 4000)


if __name__ == "__main__":
    unittest.main()
