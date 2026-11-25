"""端到端测试：联调桩 → 网关客户端 → 引擎 → 本地券务 HTTP 服务。"""

import json
from http.client import HTTPConnection
from threading import Thread
import unittest

from fixture_gateway import FixtureGateway

from voucher.api import VoucherServer
from voucher.engine import Engine
from voucher.gateway_client import GatewayClient


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.gateway = FixtureGateway(0)
        self.addCleanup(self.gateway.server_close)
        gateway_thread = Thread(
            target=self.gateway.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        gateway_thread.start()
        self.addCleanup(gateway_thread.join, 2)
        self.addCleanup(self.gateway.shutdown)

        self.engine = Engine(":memory:")
        self.addCleanup(self.engine.close)
        client = GatewayClient(port=self.gateway.server_port)
        self.server = VoucherServer(0, self.engine, client)
        self.addCleanup(self.server.server_close)
        server_thread = Thread(target=self.server.serve_forever,
                               kwargs={"poll_interval": 0.02}, daemon=True)
        server_thread.start()
        self.addCleanup(server_thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.client = client

    def call(self, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port,
                                   timeout=3)
        try:
            body = None if payload is None else json.dumps(payload)
            connection.request(method, path, body,
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_full_fixture_scenario(self):
        # 1. 装载台账。
        summary = self.engine.load_registers(self.client.fetch_registers())
        self.assertEqual(summary["coupons"], 1)
        self.assertEqual(summary["grants"], 2)
        self.assertEqual(summary["commitments"], 1)

        # 2. 全量消息摄入（游标幂等，重跑不产生重复）。
        status, ingested = self.call("POST", "/v1/admin/ingest")
        self.assertEqual(status, 200)
        self.assertEqual(ingested["ingested"], 5)
        status, again = self.call("POST", "/v1/admin/ingest")
        self.assertEqual(status, 200)
        self.assertEqual(again["ingested"], 0)

        # use-19 发生在暂停前（actual=30 < pause=35），核用有效成立；
        # 随后被退订，因此当前状态为 returned，但补贴资格与 4200 分券额可追溯。
        use19 = self.engine.get_use("use-19")
        self.assertIn(use19["status"], ("accepted", "returned"))
        self.assertEqual(use19["benefit_cents"], 4200)
        self.assertEqual(use19["merchant"], "m-a")
        # use-20 晚报时 m-a 已暂停：不能借日期早的凭条取得补贴。
        use20 = self.engine.get_use("use-20")
        self.assertEqual(use20["status"], "rejected")
        self.assertEqual(use20["reject_reason"], "merchant_paused")

        # 退订 use-19 的 L1，券仍有效（minute 60 <= expires 90），额度可复用。
        household = self.engine.household_view("house-7")
        self.assertEqual(household["used_cents"], 0)
        self.assertTrue(household["returns"][0]["reusable"])
        self.assertEqual(household["returns"][0]["cents"], 4200)

        # 3. 兑付：退订在兑付前发生，申领已取消、付款任务已撤销，无兑付产生。
        status, first = self.call("POST", "/v1/admin/payouts", {"minute": 67})
        self.assertEqual(status, 200)
        self.assertEqual(first["failed"], [])
        self.assertEqual(first["paid"], [])
        self.assertEqual(self.engine.outbox_state(), [])
        status, second = self.call("POST", "/v1/admin/payouts", {"minute": 68})
        self.assertEqual(second["paid"], [])

        # 4. 改配：t-1 确认核用净额为 0（use-19 已退），腾出 6500 给 t-3。
        status, reallocation = self.call(
            "POST", "/v1/grants/t-1/close",
            {"minute": 71, "transfer_to": "t-3"})
        self.assertEqual(status, 200)
        self.assertEqual(reallocation["confirmed_used_cents"], 0)
        self.assertEqual(reallocation["transferred_cents"], 6500)
        status, grants = self.call("GET", "/v1/grants")
        t3 = next(g for g in grants if g["terminal"] == "t-3")
        self.assertEqual(t3["cap_cents"], 6500)

        # 5. 组委会视角：县级承诺与兑付可追查。
        status, funds = self.call("GET", "/v1/committee/funds")
        self.assertEqual(status, 200)
        self.assertEqual(funds["committed_total_cents"], 4200)
        self.assertEqual(funds["paid_total_cents"], 0)
        commitment = funds["commitments"][0]
        self.assertEqual(commitment["use_receipt"], "use-19")
        # 承诺未兑付（申领因退订取消），状态保持 promised 以便组委会追查。
        self.assertEqual(commitment["status"], "promised")

    def test_lost_reply_then_paid_and_clawback_flow(self):
        self.engine.load_registers(self.client.fetch_registers())
        # 只摄入暂停前的 use-19（直接喂事件，跳过退订）。
        events = self.client.fetch_all_events()
        self.engine.ingest_event(events[1])  # use-19

        # 首次兑付丢回复：outbox 标记 failed，但对端已落账。
        outcome = self.engine.pay_due_claims(self.client, minute=67)
        self.assertEqual(outcome["paid"][0]["receipt"]["receipt"], "wire-1")
        self.assertEqual(self.engine.committee_funds()["paid_total_cents"], 4200)
        # 重跑兑付不产生第二笔到账。
        again = self.engine.pay_due_claims(self.client, minute=68)
        self.assertEqual(again["paid"], [])
        self.assertEqual(self.engine.committee_funds()["paid_total_cents"], 4200)

        # 承诺已被实际兑付勾销。
        self.assertEqual(
            self.engine.committee_funds()["commitments"][0]["status"],
            "fulfilled")

        # 现在家庭退订：到账不抹去，形成后续结单扣回。
        self.engine.ingest_event(events[3])
        reconciliation = self.engine.merchant_reconciliation("m-a")
        self.assertEqual(reconciliation["paid_total_cents"], 4200)
        self.assertEqual(reconciliation["pending_clawback_cents"], 4200)
        settlement = self.engine.run_settlement("m-a", 72)
        self.assertEqual(settlement["net_cents"], -4200)
        self.assertEqual(
            self.engine.committee_funds()["payouts"][0]["amount_cents"], 4200)


if __name__ == "__main__":
    unittest.main()
