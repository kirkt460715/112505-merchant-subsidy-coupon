"""券务后端的本地 HTTP 接口，只监听回环地址，仅依赖标准库。

接口一览（金额单位：分）::

    GET  /v1/households/<household>              家庭：可用/占用/核用/退订
    GET  /v1/merchants/<merchant>/reconciliation 商户：按交易对账与结单
    GET  /v1/merchants/<merchant>/settlements    触发一次结算（POST 同路径）
    GET  /v1/committee/funds                     组委会：承诺与兑付追查
    GET  /v1/uses/<receipt>                      单笔凭条核用明细
    GET  /v1/grants                              离线权利与确认占用
    GET  /v1/reallocations                       跨终端改配记录
    POST /v1/grants/<terminal>/close             结清权利并腾出确认空闲额度
    POST /v1/admin/ingest                        从联调入口拉取消息并办理
    POST /v1/admin/payouts                       送出待兑付申领
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .engine import Engine, RuleError
from .gateway_client import GatewayClient, GatewayError


class VoucherServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, engine: Engine,
                 client: GatewayClient | None = None):
        self.engine = engine
        self.client = client or GatewayClient(port=8765)
        super().__init__(("127.0.0.1", port), VoucherHandler)


class VoucherHandler(BaseHTTPRequestHandler):
    server: VoucherServer

    def reply(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 32_768:
            return {}
        try:
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            return body if isinstance(body, dict) else {}
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        parts = urlsplit(self.path)
        path, engine = parts.path, self.server.engine
        try:
            if method == "GET" and path == "/v1/grants":
                self.reply(200, engine.committee_funds()["grants"])
            elif method == "GET" and path == "/v1/reallocations":
                self.reply(200, engine.list_reallocations())
            elif method == "GET" and path == "/v1/committee/funds":
                self.reply(200, engine.committee_funds())
            elif path.startswith("/v1/households/"):
                household = path.removeprefix("/v1/households/")
                self.reply(200, engine.household_view(household))
            elif path.startswith("/v1/uses/"):
                receipt = path.removeprefix("/v1/uses/")
                view = engine.get_use(receipt)
                if view is None:
                    self.reply(404, {"code": "not_found"})
                else:
                    self.reply(200, view)
            elif path.endswith("/reconciliation"):
                merchant = path.removeprefix("/v1/merchants/").removesuffix(
                    "/reconciliation")
                self.reply(200, engine.merchant_reconciliation(merchant))
            elif path.endswith("/settlements"):
                merchant = path.removeprefix("/v1/merchants/").removesuffix(
                    "/settlements")
                if method == "GET":
                    self.reply(200,
                               engine.merchant_reconciliation(merchant)["settlements"])
                else:
                    body = self.read_body()
                    self.reply(200, engine.run_settlement(
                        merchant, int(body["minute"])))
            elif method == "POST" and path.startswith("/v1/grants/") \
                    and path.endswith("/close"):
                terminal = path.removeprefix("/v1/grants/").removesuffix("/close")
                body = self.read_body()
                self.reply(200, engine.close_grant(
                    terminal, int(body["minute"]),
                    body.get("transfer_to")))
            elif method == "POST" and path == "/v1/admin/ingest":
                self.reply(200, engine.pull_events(self.server.client))
            elif method == "POST" and path == "/v1/admin/payouts":
                body = self.read_body()
                minute = body.get("minute")
                self.reply(200, engine.pay_due_claims(
                    self.server.client, None if minute is None else int(minute)))
            else:
                self.reply(404, {"code": "not_found"})
        except RuleError as exc:
            self.reply(422, {"code": exc.reason, "detail": exc.detail})
        except (KeyError, TypeError, ValueError) as exc:
            self.reply(400, {"code": "invalid_request", "detail": str(exc)})
        except GatewayError as exc:
            self.reply(502, {"code": exc.code, "detail": str(exc)})

    def log_message(self, *args) -> None:
        return
