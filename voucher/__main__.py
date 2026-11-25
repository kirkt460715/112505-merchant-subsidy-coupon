"""券务后端命令行入口。

用法示例::

    python -m voucher --db demo.db --port 8080 bootstrap --gateway-port 8765
    python -m voucher --db demo.db demo --gateway-port 8765
    python -m voucher --db demo.db serve --port 8080
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import Engine
from .gateway_client import GatewayClient, GatewayError


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _bootstrap(engine: Engine, client: GatewayClient,
               offline: bool) -> dict:
    registers = client.fetch_registers()
    summary = engine.load_registers(registers)
    if not offline:
        summary["ingest"] = engine.pull_events(client)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多展点惠民券券务后端")
    parser.add_argument("--db", default="voucher.db",
                        help="SQLite 数据文件（默认 voucher.db）")
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=8765)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="装载台账并摄入全部消息")
    p = sub.add_parser("demo", help="端到端演练：装载→摄入→兑付→改配→结算")
    p.add_argument("--close-minute", type=int, default=71)
    p.add_argument("--settle-minute", type=int, default=72)
    serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("household", help="打印家庭视图").add_argument(
        "household")
    sub.add_parser("merchant", help="打印商户对账").add_argument("merchant")
    sub.add_parser("funds", help="打印组委会资金视图")
    sub.add_parser("outbox", help="打印回执投递队列状态")
    sub.add_parser("dead-letters", help="打印无法办理的消息")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = Engine(args.db)
    client = GatewayClient(args.gateway_host, args.gateway_port)
    try:
        if args.command == "bootstrap":
            _print(_bootstrap(engine, client, offline=False))
        elif args.command == "demo":
            _print(_bootstrap(engine, client, offline=False))
            payouts = engine.pay_due_claims(client)
            close = engine.close_grant("t-1", args.close_minute,
                                       transfer_to="t-3")
            settlement = engine.run_settlement("m-a", args.settle_minute)
            _print({"payouts": payouts, "reallocation": close,
                    "settlement": settlement,
                    "household": engine.household_view("house-7"),
                    "funds": engine.committee_funds()})
        elif args.command == "serve":
            from .api import VoucherServer

            with VoucherServer(args.port, engine, client) as server:
                print(f"券务服务 http://127.0.0.1:{server.server_port}",
                      flush=True)
                server.serve_forever()
        elif args.command == "household":
            _print(engine.household_view(args.household))
        elif args.command == "merchant":
            _print(engine.merchant_reconciliation(args.merchant))
        elif args.command == "funds":
            _print(engine.committee_funds())
        elif args.command == "outbox":
            _print(engine.outbox_state())
        elif args.command == "dead-letters":
            _print(engine.dead_letters())
    except (GatewayError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
