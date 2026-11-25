"""券务领域引擎：规则判定、离线分额、申领与对账。

所有金额单位为整数分，时间用整数分钟表示。规则要点：

* 券（coupon）按家庭发放，有总额度、有效期与成员名单；任一成员出示都占用
  同一家庭额度，因此跨终端、跨成员都不会重复花用同一部分额度。
* 终端的离线权利（grant）是组委会事先授予的核用容量，有商户归属与到期时点。
  商户被暂停后，其终端不再接收新券；暂停前已在有效权利内完成的使用仍可申领。
* 核用是否成立由「终端编号 + 实际发生时点 + 所属额度范围 + 商户签认」共同决定，
  以凭条号做幂等：终端重连后重复转送同一凭条不会产生第二笔核用或第二次补贴。
* 已报核用之外的离线容量在家庭视图中属于「暂被占用」；跨终端改配只腾出
  已确认未用的部分，未报不等于未用。
* 退订只退该项目对应行的券额；券仍在有效期内才恢复为家庭可复用额度。
  已到账的补贴不抹去，转为后续应结款中的扣回。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterable

# 核用被拒原因，同时作为对外 API 的错误码。
REJECT_REASONS = {
    "duplicate_receipt",
    "unknown_member",
    "terminal_without_grant",
    "grant_expired",
    "grant_closed",
    "merchant_paused",
    "coupon_expired",
    "activity_not_offered",
    "grant_capacity_exceeded",
    "household_capacity_exceeded",
}

PAY_OPERATION = "pay-merchant"
PAY_RECIPIENT = "county-funds"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coupons (
    id TEXT PRIMARY KEY,
    household TEXT NOT NULL,
    cap_cents INTEGER NOT NULL CHECK (cap_cents >= 0),
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_members (
    coupon_id TEXT NOT NULL REFERENCES coupons(id),
    member TEXT NOT NULL,
    PRIMARY KEY (coupon_id, member)
);
CREATE TABLE IF NOT EXISTS merchant_activities (
    merchant TEXT NOT NULL,
    activity TEXT NOT NULL,
    PRIMARY KEY (merchant, activity)
);
CREATE TABLE IF NOT EXISTS merchants (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused'))
);
CREATE TABLE IF NOT EXISTS merchant_pauses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant TEXT NOT NULL,
    status TEXT NOT NULL,
    minute INTEGER NOT NULL,
    event_cursor INTEGER
);
CREATE TABLE IF NOT EXISTS offline_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal TEXT NOT NULL UNIQUE,
    merchant TEXT NOT NULL,
    coupon_id TEXT REFERENCES coupons(id),
    cap_cents INTEGER NOT NULL CHECK (cap_cents >= 0),
    valid_until INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'closed')),
    opened_minute INTEGER NOT NULL DEFAULT 0,
    closed_minute INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    cursor INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    minute INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cursor INTEGER NOT NULL,
    source TEXT NOT NULL,
    minute INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uses (
    receipt TEXT PRIMARY KEY,
    coupon_id TEXT REFERENCES coupons(id),
    household TEXT,
    member TEXT NOT NULL,
    terminal TEXT NOT NULL,
    merchant TEXT,
    actual_minute INTEGER NOT NULL,
    received_minute INTEGER NOT NULL,
    benefit_cents INTEGER NOT NULL,
    self_pay_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'accepted', 'partial', 'rejected', 'partially_returned', 'returned')),
    reject_reason TEXT
);
CREATE TABLE IF NOT EXISTS use_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    use_receipt TEXT NOT NULL REFERENCES uses(receipt),
    line TEXT NOT NULL,
    activity TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    benefit_cents INTEGER NOT NULL,
    self_pay_cents INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    UNIQUE (use_receipt, line)
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    use_receipt TEXT NOT NULL UNIQUE REFERENCES uses(receipt),
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('payable', 'paid', 'cancelled')),
    created_minute INTEGER NOT NULL,
    paid_minute INTEGER
);
CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    use_receipt TEXT NOT NULL REFERENCES uses(receipt),
    line TEXT NOT NULL,
    cents INTEGER NOT NULL CHECK (cents > 0),
    reusable INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    event_cursor INTEGER,
    settled INTEGER NOT NULL DEFAULT 0,
    UNIQUE (use_receipt, line)
);
CREATE TABLE IF NOT EXISTS fund_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    use_receipt TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL,
    transfer TEXT,
    status TEXT NOT NULL DEFAULT 'promised'
        CHECK (status IN ('promised', 'fulfilled'))
);
CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    receipt TEXT NOT NULL,
    minute INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant TEXT NOT NULL,
    minute INTEGER NOT NULL,
    gross_cents INTEGER NOT NULL,
    clawback_cents INTEGER NOT NULL,
    net_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlements(id),
    claim_id TEXT,
    return_id INTEGER,
    cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reallocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_terminal TEXT NOT NULL,
    target_terminal TEXT,
    cap_cents INTEGER,
    confirmed_used_cents INTEGER,
    transferred_cents INTEGER,
    minute INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'rejected')),
    note TEXT,
    event_cursor INTEGER
);
CREATE TABLE IF NOT EXISTS receipt_outbox (
    reference TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    recipient TEXT NOT NULL,
    body TEXT NOT NULL,
    claim_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


class RuleError(Exception):
    """业务规则拒绝，``reason`` 为稳定错误码。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class Engine:
    """线程安全的券务引擎；数据落于单个 SQLite 文件。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ 基础

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _meta_get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def _meta_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ 台账装载

    def load_registers(self, registers: dict) -> dict:
        """一次性装载演练台账（券、商户范围、离线权利、县级资金承诺）。

        重复调用直接返回既有装载摘要，保证幂等。
        """
        with self._tx() as db:
            if self._meta_get("registers_loaded"):
                return self._register_summary(db)
            coupon = registers["coupon"]
            db.execute(
                "INSERT INTO coupons(id, household, cap_cents, expires) VALUES(?, ?, ?, ?)",
                (coupon["id"], coupon["household"], int(coupon["cap_cents"]),
                 int(coupon["expires"])),
            )
            for member in coupon["members"]:
                db.execute(
                    "INSERT INTO coupon_members(coupon_id, member) VALUES(?, ?)",
                    (coupon["id"], member),
                )
            for merchant, activities in registers["merchant_rules"].items():
                db.execute("INSERT INTO merchants(id, status) VALUES(?, 'active')",
                           (merchant,))
                for activity in activities:
                    db.execute(
                        "INSERT INTO merchant_activities(merchant, activity) VALUES(?, ?)",
                        (merchant, activity),
                    )
            for grant in registers["offline_grants"]:
                db.execute(
                    "INSERT INTO offline_grants(terminal, merchant, coupon_id, "
                    "cap_cents, valid_until, status, opened_minute) "
                    "VALUES(?, ?, ?, ?, ?, 'active', 0)",
                    (grant["terminal"], grant["merchant"], coupon["id"],
                     int(grant["cap_cents"]), int(grant["valid_until"])),
                )
            approved = registers.get("approved_payout")
            if approved:
                db.execute(
                    "INSERT INTO fund_commitments(use_receipt, amount_cents, "
                    "transfer, status) VALUES(?, ?, ?, 'promised')",
                    (approved["receipt"], int(approved["amount"]),
                     approved.get("transfer")),
                )
            db.execute(
                "INSERT INTO meta(key, value) VALUES('registers_loaded', '1')")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('purchase_catalog', ?)",
                (self._dumps(registers.get("purchase", [])),),
            )
            return self._register_summary(db)

    def _register_summary(self, db: sqlite3.Connection) -> dict:
        return {
            "coupons": db.execute("SELECT COUNT(*) c FROM coupons").fetchone()["c"],
            "merchants": db.execute("SELECT COUNT(*) c FROM merchants").fetchone()["c"],
            "grants": db.execute(
                "SELECT COUNT(*) c FROM offline_grants").fetchone()["c"],
            "commitments": db.execute(
                "SELECT COUNT(*) c FROM fund_commitments").fetchone()["c"],
        }

    def _purchase_catalog(self) -> list[dict]:
        raw = self._meta_get("purchase_catalog")
        return json.loads(raw) if raw else []

    # ------------------------------------------------------------ 事件摄入

    def ingest_event(self, event: dict) -> dict:
        """按送达游标幂等摄入一条消息，返回处理结果。"""
        cursor = int(event["cursor"])
        with self._tx() as db:
            if db.execute("SELECT 1 FROM events WHERE cursor=?", (cursor,)).fetchone():
                previous = db.execute(
                    "SELECT kind FROM events WHERE cursor=?", (cursor,)).fetchone()
                reason = ("duplicate_receipt" if previous["kind"] == "use"
                          else "duplicate_event")
                return {"cursor": cursor, "kind": previous["kind"],
                        "status": "duplicate", "reject_reason": reason}
            payload = event["payload"]
            kind = self._classify(payload)
            db.execute(
                "INSERT INTO events(cursor, source, minute, kind, payload) "
                "VALUES(?, ?, ?, ?, ?)",
                (cursor, event["source"], int(event["minute"]), kind,
                 self._dumps(payload)),
            )
            if kind == "pause":
                return self._handle_pause(db, event, payload)
            if kind == "use":
                return self._handle_use(db, event, payload)
            if kind == "return":
                return self._handle_return(db, event, payload)
            if kind == "reallocate":
                return self._handle_reallocate(db, event, payload)
            db.execute(
                "INSERT INTO dead_letters(cursor, source, minute, reason, payload) "
                "VALUES(?, ?, ?, 'unknown_event', ?)",
                (cursor, event["source"], int(event["minute"]),
                 self._dumps(payload)),
            )
            return {"cursor": cursor, "kind": "dead_letter",
                    "reason": "unknown_event"}

    @staticmethod
    def _classify(payload: dict) -> str:
        if "receipt" in payload and "actual_minute" in payload:
            return "use"
        if payload.get("request") == "return":
            return "return"
        if payload.get("request") == "reallocate":
            return "reallocate"
        if "status" in payload and "id" in payload:
            return "pause"
        return "unknown"

    # ------------------------------------------------------- 暂停 / 改配

    def _handle_pause(self, db, event: dict, payload: dict) -> dict:
        merchant = payload["id"]
        status = "paused" if payload["status"] == "paused" else "active"
        db.execute(
            "INSERT INTO merchants(id, status) VALUES(?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status",
            (merchant, status),
        )
        db.execute(
            "INSERT INTO merchant_pauses(merchant, status, minute, event_cursor) "
            "VALUES(?, ?, ?, ?)",
            (merchant, status, int(event["minute"]), int(event["cursor"])),
        )
        return {"cursor": event["cursor"], "kind": "pause",
                "merchant": merchant, "status": status}

    def _handle_reallocate(self, db, event: dict, payload: dict) -> dict:
        terminal = payload["terminal"]
        target = payload.get("to")
        grant = db.execute(
            "SELECT * FROM offline_grants WHERE terminal=?", (terminal,)).fetchone()
        if grant is None:
            db.execute(
                "INSERT INTO dead_letters(cursor, source, minute, reason, payload) "
                "VALUES(?, ?, ?, 'terminal_without_grant', ?)",
                (event["cursor"], event["source"], event["minute"],
                 self._dumps(payload)),
            )
            return {"cursor": event["cursor"], "kind": "dead_letter",
                    "reason": "terminal_without_grant"}
        db.execute(
            "INSERT INTO reallocations(source_terminal, target_terminal, cap_cents, "
            "minute, status, event_cursor) VALUES(?, ?, ?, ?, 'pending', ?)",
            (terminal, target, grant["cap_cents"], int(event["minute"]),
             int(event["cursor"])),
        )
        return {"cursor": event["cursor"], "kind": "reallocate",
                "source_terminal": terminal, "target_terminal": target,
                "status": "pending"}

    def close_grant(self, terminal: str, minute: int,
                    transfer_to: str | None = None) -> dict:
        """结清终端离线权利。

        未报不等于未用：只把「容量减去已确认核用净额」的部分腾出给新终端，
        前一台的未查清核用仍留在其额度范围内，事后凭条照样可查、可判定。
        """
        with self._tx() as db:
            grant = db.execute(
                "SELECT * FROM offline_grants WHERE terminal=?", (terminal,)).fetchone()
            if grant is None:
                raise RuleError("terminal_without_grant", terminal)
            if grant["status"] == "closed":
                raise RuleError("grant_closed", terminal)
            net_used = self._grant_net_used(db, terminal)
            transferable = max(0, grant["cap_cents"] - net_used)
            pending = db.execute(
                "SELECT * FROM reallocations WHERE source_terminal=? "
                "AND status='pending' ORDER BY id DESC LIMIT 1",
                (terminal,)).fetchone()
            target = transfer_to or (pending["target_terminal"] if pending else None)
            db.execute(
                "UPDATE offline_grants SET status='closed', closed_minute=? "
                "WHERE terminal=?", (minute, terminal))
            if target is not None and transferable > 0:
                existing = db.execute(
                    "SELECT * FROM offline_grants WHERE terminal=?",
                    (target,)).fetchone()
                if existing is None:
                    db.execute(
                        "INSERT INTO offline_grants(terminal, merchant, coupon_id, "
                        "cap_cents, valid_until, status, opened_minute) "
                        "VALUES(?, ?, ?, ?, ?, 'active', ?)",
                        (target, grant["merchant"], grant["coupon_id"],
                         transferable, grant["valid_until"], minute))
                else:
                    db.execute(
                        "UPDATE offline_grants SET cap_cents=cap_cents+? WHERE terminal=?",
                        (transferable, target))
            if pending is not None:
                db.execute(
                    "UPDATE reallocations SET confirmed_used_cents=?, "
                    "transferred_cents=?, status='done' WHERE id=?",
                    (net_used, transferable if target else 0, pending["id"]))
                row_id = pending["id"]
            else:
                cur = db.execute(
                    "INSERT INTO reallocations(source_terminal, target_terminal, "
                    "cap_cents, confirmed_used_cents, transferred_cents, minute, "
                    "status, note) VALUES(?, ?, ?, ?, ?, ?, 'done', 'close_grant')",
                    (terminal, target, grant["cap_cents"], net_used,
                     transferable if target else 0, minute))
                row_id = cur.lastrowid
            return self._reallocation_view(db, row_id)

    # ------------------------------------------------------------ 核用判定

    def _merchant_paused_at(self, db, merchant: str, minute: int) -> bool:
        """商户在指定（实际交易）时点是否处于暂停状态。

        以暂停/恢复台账中不晚于该时点的最后一条记录为准；该时点之前没有
        任何记录，说明当时尚未暂停。不能回退到商户当前状态，否则暂停前
        完成、暂停后才晚报的交易也会被误拒。
        """
        row = db.execute(
            "SELECT status FROM merchant_pauses WHERE merchant=? AND minute<=? "
            "ORDER BY minute DESC, id DESC LIMIT 1", (merchant, minute)).fetchone()
        return row is not None and row["status"] == "paused"

    def _grant_net_used(self, db, terminal: str) -> int:
        row = db.execute(
            "SELECT COALESCE(SUM(u.benefit_cents), 0) AS used "
            "FROM uses u WHERE u.terminal=? AND u.status != 'rejected'",
            (terminal,)).fetchone()
        ret = db.execute(
            "SELECT COALESCE(SUM(r.cents), 0) AS c FROM returns r "
            "JOIN uses u ON u.receipt=r.use_receipt WHERE u.terminal=?",
            (terminal,)).fetchone()
        return int(row["used"]) - int(ret["c"])

    def _coupon_net_used(self, db, coupon_id: str) -> int:
        """家庭券已用净额：仅可复用退订才恢复家庭可用额度。"""
        row = db.execute(
            "SELECT COALESCE(SUM(benefit_cents), 0) c FROM uses "
            "WHERE coupon_id=? AND status != 'rejected'", (coupon_id,)).fetchone()
        ret = db.execute(
            "SELECT COALESCE(SUM(cents), 0) c FROM returns "
            "WHERE reusable=1 AND use_receipt IN "
            "(SELECT receipt FROM uses WHERE coupon_id=?)", (coupon_id,)).fetchone()
        return int(row["c"]) - int(ret["c"])

    def _coupon_held(self, db, coupon_id: str) -> int:
        """活动离线权利中已预留、尚未被确认核用占用的容量。

        家庭视角下，每台有效终端的离线权利先整体预留；确认核用成立后，相应
        容量从「暂被占用」转为「已用」，因此任一时刻 已用 + 暂占用 不会重复
        计算同一部分额度，跨终端、跨成员的双花也被同一家庭池挡住。
        已结清（关闭）的权利不再预留容量，其未查清部分由改配记录留痕。
        """
        total = 0
        for grant in db.execute(
                "SELECT * FROM offline_grants WHERE coupon_id=? AND status='active'",
                (coupon_id,)):
            net_used = self._grant_net_used(db, grant["terminal"])
            total += max(0, grant["cap_cents"] - net_used)
        return total

    def _derive_lines(self, payload: dict, merchant: str,
                      db: sqlite3.Connection) -> list[dict]:
        eligible = {
            r["activity"] for r in db.execute(
                "SELECT activity FROM merchant_activities WHERE merchant=?",
                (merchant,))}
        catalog = self._purchase_catalog()
        if "lines" in payload:
            lines = []
            for item in payload["lines"]:
                price = int(item["price"])
                requested = int(item.get("benefit_cents", price))
                if requested > price:
                    raise RuleError("invalid_line",
                                    f"benefit exceeds price on {item.get('line')}")
                ok = item["activity"] in eligible
                lines.append({
                    "line": str(item["line"]),
                    "activity": item["activity"],
                    "price_cents": price,
                    "benefit_cents": requested if ok else 0,
                    "self_pay_cents": price - (requested if ok else 0),
                    "eligible": ok,
                })
            return lines
        activity = payload["activity"]
        benefit = int(payload["benefit_cents"])
        match = next((p for p in catalog if p["activity"] == activity), None)
        line_id = match["line"] if match else activity
        ok = activity in eligible
        return [{
            "line": line_id,
            "activity": activity,
            "price_cents": benefit,
            "benefit_cents": benefit if ok else 0,
            "self_pay_cents": 0,
            "eligible": ok,
        }]

    def _reject_use(self, db, event: dict, payload: dict, reason: str) -> dict:
        db.execute(
            "INSERT INTO uses(receipt, member, terminal, actual_minute, "
            "received_minute, benefit_cents, self_pay_cents, status, reject_reason) "
            "VALUES(?, ?, ?, ?, ?, 0, 0, 'rejected', ?)",
            (payload["receipt"], payload.get("member", ""), payload["terminal"],
             int(payload["actual_minute"]), int(event["minute"]), reason))
        return {"cursor": event["cursor"], "kind": "use",
                "receipt": payload["receipt"], "status": "rejected",
                "reject_reason": reason}

    def _handle_use(self, db, event: dict, payload: dict) -> dict:
        receipt = payload["receipt"]
        # 1. 凭条幂等：重连重送同一凭条不产生第二笔。
        if db.execute("SELECT 1 FROM uses WHERE receipt=?", (receipt,)).fetchone():
            return {"cursor": event["cursor"], "kind": "use",
                    "receipt": receipt, "status": "duplicate",
                    "reject_reason": "duplicate_receipt"}
        actual = int(payload["actual_minute"])
        # 2. 成员与券。
        member = payload["member"]
        coupon_row = db.execute(
            "SELECT c.* FROM coupons c JOIN coupon_members m ON m.coupon_id=c.id "
            "WHERE m.member=?", (member,)).fetchone()
        if coupon_row is None:
            return self._reject_use(db, event, payload, "unknown_member")
        # 3. 终端离线权利。
        grant = db.execute(
            "SELECT * FROM offline_grants WHERE terminal=?",
            (payload["terminal"],)).fetchone()
        if grant is None:
            return self._reject_use(db, event, payload, "terminal_without_grant")
        if grant["status"] == "closed":
            return self._reject_use(db, event, payload, "grant_closed")
        # 4. 权利自身到期时点（按实际交易时点）。
        if actual > grant["valid_until"]:
            return self._reject_use(db, event, payload, "grant_expired")
        # 5. 商户暂停：按实际交易时点判断。暂停生效前已在有效权利内完成的
        # 使用，晚报仍可申领；暂停生效后的交易不能借早日期凭条取得补贴。
        if self._merchant_paused_at(db, grant["merchant"], actual):
            return self._reject_use(db, event, payload, "merchant_paused")
        # 6. 券有效期（按实际交易时点）。
        if actual > coupon_row["expires"]:
            return self._reject_use(db, event, payload, "coupon_expired")
        # 7. 项目适用性与自付款部分。
        try:
            lines = self._derive_lines(payload, grant["merchant"], db)
        except RuleError as exc:
            return self._reject_use(db, event, payload, exc.reason)
        if not any(line["benefit_cents"] > 0 for line in lines):
            return self._reject_use(db, event, payload, "activity_not_offered")
        benefit = sum(line["benefit_cents"] for line in lines)
        self_pay = sum(line["self_pay_cents"] for line in lines)
        # 8. 终端离线额度范围。
        if self._grant_net_used(db, grant["terminal"]) + benefit > grant["cap_cents"]:
            return self._reject_use(db, event, payload,
                                    "grant_capacity_exceeded")
        # 9. 家庭额度（跨终端、跨成员共享同一池，防止双花）。
        # 已用净额按实际核用累计；离线权利的空闲容量在家庭视图中作为
        # 「暂被占用」单独展示，不在此重复扣减可用额度。
        used = self._coupon_net_used(db, coupon_row["id"])
        if used + benefit > coupon_row["cap_cents"]:
            return self._reject_use(db, event, payload,
                                    "household_capacity_exceeded")
        # 判定成立：落核用、明细行与商户申领（终端转送即商户签认事实）。
        status = "partial" if any(not line["eligible"] for line in lines) \
            else "accepted"
        db.execute(
            "INSERT INTO uses(receipt, coupon_id, household, member, terminal, "
            "merchant, actual_minute, received_minute, benefit_cents, "
            "self_pay_cents, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt, coupon_row["id"], coupon_row["household"], member,
             grant["terminal"], grant["merchant"], actual,
             int(event["minute"]), benefit, self_pay, status))
        for line in lines:
            db.execute(
                "INSERT INTO use_lines(use_receipt, line, activity, price_cents, "
                "benefit_cents, self_pay_cents, eligible) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (receipt, line["line"], line["activity"], line["price_cents"],
                 line["benefit_cents"], line["self_pay_cents"],
                 1 if line["eligible"] else 0))
        claim_id = f"claim-{receipt}"
        db.execute(
            "INSERT INTO claims(id, use_receipt, merchant, amount_cents, status, "
            "created_minute) VALUES(?, ?, ?, ?, 'payable', ?)",
            (claim_id, receipt, grant["merchant"], benefit, actual))
        reference = f"pay-{receipt}"
        db.execute(
            "INSERT INTO receipt_outbox(reference, operation, recipient, body, "
            "claim_id, status) VALUES(?, ?, ?, ?, ?, 'queued')",
            (reference, PAY_OPERATION, PAY_RECIPIENT,
             self._dumps({"reference": reference, "operation": PAY_OPERATION,
                          "recipient": PAY_RECIPIENT}),
             claim_id))
        return {"cursor": event["cursor"], "kind": "use", "receipt": receipt,
                "status": status, "benefit_cents": benefit,
                "self_pay_cents": self_pay, "claim_id": claim_id}

    # ------------------------------------------------------------ 退订处理

    def _handle_return(self, db, event: dict, payload: dict) -> dict:
        receipt = payload["receipt"]
        line_id = str(payload["line"])
        use = db.execute("SELECT * FROM uses WHERE receipt=?",
                         (receipt,)).fetchone()
        if use is None or use["status"] == "rejected":
            db.execute(
                "INSERT INTO dead_letters(cursor, source, minute, reason, payload) "
                "VALUES(?, ?, ?, 'return_without_use', ?)",
                (event["cursor"], event["source"], event["minute"],
                 self._dumps(payload)))
            return {"cursor": event["cursor"], "kind": "dead_letter",
                    "reason": "return_without_use"}
        line = db.execute(
            "SELECT * FROM use_lines WHERE use_receipt=? AND line=?",
            (receipt, line_id)).fetchone()
        if line is None:
            raise RuleError("line_not_found", f"{receipt}/{line_id}")
        if db.execute(
                "SELECT 1 FROM returns WHERE use_receipt=? AND line=?",
                (receipt, line_id)).fetchone():
            raise RuleError("already_returned", f"{receipt}/{line_id}")
        coupon = db.execute("SELECT * FROM coupons WHERE id=?",
                            (use["coupon_id"],)).fetchone()
        # 能否重新使用取决于券有效期（退订发生时点）与此前回执（核用确有回执）。
        reusable = int(event["minute"]) <= coupon["expires"]
        cents = int(line["benefit_cents"])
        cur = db.execute(
            "INSERT INTO returns(use_receipt, line, cents, reusable, minute, "
            "event_cursor) VALUES(?, ?, ?, ?, ?, ?)",
            (receipt, line_id, cents, 1 if reusable else 0,
             int(event["minute"]), int(event["cursor"])))
        db.execute(
            "UPDATE uses SET status=CASE WHEN "
            "(SELECT COALESCE(SUM(cents),0) FROM returns WHERE use_receipt=?) "
            ">= benefit_cents THEN 'returned' ELSE 'partially_returned' END "
            "WHERE receipt=?", (receipt, receipt))
        claim = db.execute("SELECT * FROM claims WHERE use_receipt=?",
                           (receipt,)).fetchone()
        if claim is not None:
            if claim["status"] == "paid":
                # 到账不抹去：保留 payout 记录，退订形成待扣回，进下次结算。
                db.execute(
                    "UPDATE claims SET amount_cents=MAX(0, amount_cents-?) WHERE id=?",
                    (cents, claim["id"]))
            else:
                new_amount = max(0, claim["amount_cents"] - cents)
                if new_amount == 0:
                    db.execute(
                        "UPDATE claims SET status='cancelled' WHERE id=?",
                        (claim["id"],))
                    # 申领归零：未产生兑付的付款任务直接撤销（含投递失败待重试）。
                    db.execute(
                        "DELETE FROM receipt_outbox WHERE claim_id=? "
                        "AND status IN ('queued', 'failed')", (claim["id"],))
                else:
                    db.execute(
                        "UPDATE claims SET amount_cents=? WHERE id=?",
                        (new_amount, claim["id"]))
        return {"cursor": event["cursor"], "kind": "return",
                "receipt": receipt, "line": line_id, "returned_cents": cents,
                "reusable": bool(reusable), "return_id": cur.lastrowid}

    # ------------------------------------------------------------ 补贴兑付

    def pay_due_claims(self, client, minute: int | None = None) -> dict:
        """把待兑付申领通过合作方回执通道送出，回执驱动、可安全重试。"""
        with self._tx() as db:
            if minute is None:
                minute = db.execute(
                    "SELECT COALESCE(MAX(minute), 0) m FROM events").fetchone()["m"]
            queued = db.execute(
                "SELECT o.* FROM receipt_outbox o JOIN claims c ON c.id=o.claim_id "
                "WHERE o.status IN ('queued', 'failed') AND c.status != 'cancelled' "
                "ORDER BY o.rowid").fetchall()
            sent, failed = [], []
            for row in queued:
                body = json.loads(row["body"])
                try:
                    receipt = client.send_receipt(
                        body["reference"], body["operation"], body["recipient"])
                except Exception as exc:  # 丢回复/网络异常：保留待重试
                    db.execute(
                        "UPDATE receipt_outbox SET status='failed', "
                        "attempts=attempts+1, last_error=? WHERE reference=?",
                        (str(exc), row["reference"]))
                    failed.append(row["reference"])
                    continue
                db.execute(
                    "UPDATE receipt_outbox SET status='sent', attempts=attempts+1, "
                    "last_error=NULL WHERE reference=?", (row["reference"],))
                claim = db.execute(
                    "SELECT * FROM claims WHERE id=?", (row["claim_id"],)).fetchone()
                db.execute(
                    "INSERT INTO payouts(claim_id, merchant, amount_cents, "
                    "reference, receipt, minute) VALUES(?, ?, ?, ?, ?, ?)",
                    (claim["id"], claim["merchant"], claim["amount_cents"],
                     row["reference"], self._dumps(receipt), minute))
                db.execute(
                    "UPDATE claims SET status='paid', paid_minute=? WHERE id=?",
                    (minute, claim["id"]))
                use_receipt = claim["use_receipt"]
                commitment = db.execute(
                    "SELECT * FROM fund_commitments WHERE use_receipt=?",
                    (use_receipt,)).fetchone()
                if commitment is not None:
                    db.execute(
                        "UPDATE fund_commitments SET status='fulfilled' WHERE id=?",
                        (commitment["id"],))
                sent.append({"claim_id": claim["id"],
                             "amount_cents": claim["amount_cents"],
                             "receipt": receipt})
            return {"minute": minute, "paid": sent, "failed": failed}

    def run_settlement(self, merchant: str, minute: int) -> dict:
        """按商户结算：已到账补贴减去退订扣回，负数结单冲减后续应结款。"""
        with self._tx() as db:
            claims = db.execute(
                "SELECT * FROM claims WHERE merchant=? AND status='paid' "
                "AND id NOT IN (SELECT claim_id FROM settlement_items "
                "WHERE claim_id IS NOT NULL)", (merchant,)).fetchall()
            clawbacks = db.execute(
                "SELECT r.* FROM returns r JOIN uses u ON u.receipt=r.use_receipt "
                "WHERE u.merchant=? AND r.settled=0 AND EXISTS ("
                "SELECT 1 FROM claims c WHERE c.use_receipt=u.receipt "
                "AND c.status='paid')", (merchant,)).fetchall()
            gross = sum(c["amount_cents"] for c in claims)
            clawback = sum(r["cents"] for r in clawbacks)
            net = gross - clawback
            cur = db.execute(
                "INSERT INTO settlements(merchant, minute, gross_cents, "
                "clawback_cents, net_cents) VALUES(?, ?, ?, ?, ?)",
                (merchant, minute, gross, clawback, net))
            for claim in claims:
                db.execute(
                    "INSERT INTO settlement_items(settlement_id, claim_id, cents) "
                    "VALUES(?, ?, ?)", (cur.lastrowid, claim["id"],
                                        claim["amount_cents"]))
            for item in clawbacks:
                db.execute(
                    "INSERT INTO settlement_items(settlement_id, return_id, cents) "
                    "VALUES(?, ?, ?)",
                    (cur.lastrowid, item["id"], -item["cents"]))
                db.execute("UPDATE returns SET settled=1 WHERE id=?",
                           (item["id"],))
            return self._settlement_view(db, cur.lastrowid)

    # ------------------------------------------------------------ 查询视图

    def _use_view(self, row: sqlite3.Row) -> dict:
        lines = self._db.execute(
            "SELECT line, activity, price_cents, benefit_cents, "
            "self_pay_cents, eligible FROM use_lines WHERE use_receipt=? ORDER BY id",
            (row["receipt"],)).fetchall()
        return {
            "receipt": row["receipt"],
            "coupon_id": row["coupon_id"],
            "household": row["household"],
            "member": row["member"],
            "terminal": row["terminal"],
            "merchant": row["merchant"],
            "actual_minute": row["actual_minute"],
            "received_minute": row["received_minute"],
            "benefit_cents": row["benefit_cents"],
            "self_pay_cents": row["self_pay_cents"],
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "lines": [dict(line) for line in lines],
        }

    def get_use(self, receipt: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM uses WHERE receipt=?",
                                   (receipt,)).fetchone()
            return None if row is None else self._use_view(row)

    def household_view(self, household: str) -> dict:
        """家庭查询：可用额度、暂被占用额度与逐笔核用、退订。"""
        with self._lock:
            db = self._db
            coupon = db.execute(
                "SELECT * FROM coupons WHERE household=?", (household,)).fetchone()
            if coupon is None:
                raise RuleError("household_not_found", household)
            used = self._coupon_net_used(db, coupon["id"])
            held = self._coupon_held(db, coupon["id"])
            # 可立即使用的额度同时受券余额和各终端权利空闲容量约束。
            coupon_remaining = max(0, coupon["cap_cents"] - used)
            available = min(coupon_remaining, held)
            uses_rows = db.execute(
                "SELECT * FROM uses WHERE coupon_id=? ORDER BY actual_minute, receipt",
                (coupon["id"],)).fetchall()
            returns = db.execute(
                "SELECT r.* FROM returns r JOIN uses u ON u.receipt=r.use_receipt "
                "WHERE u.coupon_id=? ORDER BY r.id", (coupon["id"],)).fetchall()
            return {
                "household": household,
                "coupon_id": coupon["id"],
                "cap_cents": coupon["cap_cents"],
                "expires": coupon["expires"],
                "used_cents": used,
                "held_cents": held,
                "available_cents": available,
                "coupon_remaining_cents": coupon_remaining,
                "uses": [self._use_view(row) for row in uses_rows],
                "returns": [dict(r) for r in returns],
            }

    def merchant_reconciliation(self, merchant: str) -> dict:
        """商户按交易对账：核用、自付款、申领状态、到账、退订扣回与结单。"""
        with self._lock:
            db = self._db
            uses_rows = db.execute(
                "SELECT * FROM uses WHERE merchant=? ORDER BY actual_minute, receipt",
                (merchant,)).fetchall()
            entries = []
            for row in uses_rows:
                view = self._use_view(row)
                claim = db.execute(
                    "SELECT * FROM claims WHERE use_receipt=?",
                    (row["receipt"],)).fetchone()
                view["claim"] = None if claim is None else {
                    "id": claim["id"],
                    "amount_cents": claim["amount_cents"],
                    "status": claim["status"],
                    "paid_minute": claim["paid_minute"],
                }
                entries.append(view)
            settlements = db.execute(
                "SELECT * FROM settlements WHERE merchant=? ORDER BY id",
                (merchant,)).fetchall()
            paid = db.execute(
                "SELECT COALESCE(SUM(amount_cents),0) c FROM payouts "
                "WHERE merchant=?", (merchant,)).fetchone()["c"]
            pending_clawback = db.execute(
                "SELECT COALESCE(SUM(r.cents),0) c FROM returns r "
                "JOIN uses u ON u.receipt=r.use_receipt "
                "WHERE u.merchant=? AND r.settled=0 AND EXISTS ("
                "SELECT 1 FROM claims c WHERE c.use_receipt=u.receipt "
                "AND c.status='paid')", (merchant,)).fetchone()["c"]
            return {
                "merchant": merchant,
                "uses": entries,
                "settlements": [dict(s) for s in settlements],
                "paid_total_cents": paid,
                "pending_clawback_cents": pending_clawback,
            }

    def committee_funds(self) -> dict:
        """组委会追查县级资金承诺与实际兑付，并总览离线权利占用。"""
        with self._lock:
            db = self._db
            commitments = db.execute(
                "SELECT * FROM fund_commitments ORDER BY id").fetchall()
            payouts = db.execute("SELECT * FROM payouts ORDER BY id").fetchall()
            claims = db.execute(
                "SELECT status, COUNT(*) n, COALESCE(SUM(amount_cents),0) c "
                "FROM claims GROUP BY status").fetchall()
            grants = db.execute(
                "SELECT * FROM offline_grants ORDER BY id").fetchall()
            grant_view = []
            for grant in grants:
                net = (self._grant_net_used(db, grant["terminal"])
                       if grant["status"] == "active" else None)
                grant_view.append({
                    "terminal": grant["terminal"],
                    "merchant": grant["merchant"],
                    "coupon_id": grant["coupon_id"],
                    "cap_cents": grant["cap_cents"],
                    "valid_until": grant["valid_until"],
                    "status": grant["status"],
                    "confirmed_used_cents": net,
                    "free_cents": (grant["cap_cents"] - net) if net is not None
                    else None,
                })
            return {
                "commitments": [dict(c) for c in commitments],
                "committed_total_cents": sum(c["amount_cents"] for c in commitments),
                "payouts": [dict(p) for p in payouts],
                "paid_total_cents": sum(p["amount_cents"] for p in payouts),
                "claims_by_status": {r["status"]: {"count": r["n"],
                                                    "cents": r["c"]}
                                     for r in claims},
                "grants": grant_view,
            }

    def _reallocation_view(self, db: sqlite3.Connection, row_id: int) -> dict:
        row = db.execute("SELECT * FROM reallocations WHERE id=?",
                         (row_id,)).fetchone()
        return dict(row)

    def list_reallocations(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM reallocations ORDER BY id").fetchall()]

    def _settlement_view(self, db: sqlite3.Connection, row_id: int) -> dict:
        row = db.execute("SELECT * FROM settlements WHERE id=?",
                         (row_id,)).fetchone()
        items = db.execute(
            "SELECT * FROM settlement_items WHERE settlement_id=?",
            (row_id,)).fetchall()
        view = dict(row)
        view["items"] = [dict(i) for i in items]
        return view

    def outbox_state(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT reference, operation, recipient, claim_id, status, "
                "attempts, last_error FROM receipt_outbox ORDER BY rowid").fetchall()]

    def dead_letters(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM dead_letters ORDER BY id").fetchall()]

    def pull_events(self, client, batch_size: int = 50) -> dict:
        """从联调入口分页拉取全部消息并逐条摄入（游标幂等）。"""
        after = int(self._meta_get("last_event_cursor") or 0)
        ingested, results = 0, []
        while True:
            page = client.fetch_events(after=after, limit=batch_size)
            events = page["events"]
            if not events:
                break
            for event in events:
                results.append(self.ingest_event(event))
                after = event["cursor"]
                ingested += 1
            with self._lock:
                self._meta_set("last_event_cursor", str(after))
            if len(events) < batch_size:
                break
        return {"ingested": ingested, "results": results}
