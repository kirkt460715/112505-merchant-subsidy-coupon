"""联调入口（fixture_gateway）的 HTTP 客户端，仅用标准库。

合作方是模拟事实桩：业务办理全部由 :mod:`voucher.engine` 负责，本模块只做
消息分页拉取与回执投递。``lost_reply`` 场景下首次投递已在对端落账但连接断开，
因此重试前先用 GET 查询，避免凭主观重发制造重复入账。
"""

from __future__ import annotations

import json
from http.client import HTTPConnection, RemoteDisconnected
from urllib.parse import urlencode


class GatewayError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"gateway {status}: {code}")
        self.status = status
        self.code = code


class GatewayClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _connection(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        connection = self._connection()
        try:
            data = None
            headers = {}
            if body is not None:
                data = json.dumps(body, ensure_ascii=False)
                headers["Content-Type"] = "application/json"
            connection.request(method, path, data, headers)
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            connection.close()

    def fetch_registers(self) -> dict:
        status, payload = self._request("GET", "/v1/registers")
        if status != 200:
            raise GatewayError(status, payload.get("code", "error"))
        return payload

    def fetch_events(self, after: int = 0, limit: int = 50) -> dict:
        query = urlencode({"after": after, "limit": limit})
        status, payload = self._request("GET", f"/v1/events?{query}")
        if status != 200:
            raise GatewayError(status, payload.get("code", "error"))
        return payload

    def fetch_all_events(self, limit: int = 50) -> list[dict]:
        """按游标翻完整个消息台账，返回送达顺序的全量消息。"""
        after, collected = 0, []
        while True:
            page = self.fetch_events(after=after, limit=limit)
            events = page["events"]
            if not events:
                return collected
            collected.extend(events)
            after = page["next_cursor"]
            if len(events) < limit:
                return collected

    def lookup_receipt(self, reference: str) -> dict | None:
        status, payload = self._request("GET", f"/v1/receipts/{reference}")
        if status == 200:
            return payload["receipt"]
        if status == 404:
            return None
        raise GatewayError(status, payload.get("code", "error"))

    def send_receipt(self, reference: str, operation: str,
                     recipient: str, max_attempts: int = 3) -> dict:
        """投递回执并返回对端回执对象。

        * 编号被另一操作占用 → :class:`GatewayError` 409，调用方必须换编号排查；
        * 连接断开（lost_reply）→ 先查已落账记录，查不到再幂等重发；
        * 相同编号与内容的重试天然安全，对端返回同一回执。
        """
        body = {"reference": reference, "operation": operation,
                "recipient": recipient}
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                status, payload = self._request("POST", "/v1/receipts", body)
            except RemoteDisconnected as exc:
                last_error = exc
                known = self.lookup_receipt(reference)
                if known is not None:
                    return known
                continue
            if status == 200:
                return payload["receipt"]
            if status == 409:
                raise GatewayError(409, payload.get("code", "reference_reused"))
            last_error = GatewayError(status, payload.get("code", "error"))
        raise GatewayError(0, f"unreachable after {max_attempts} attempts: {last_error}")
