# 多展点惠民券的离线分额与商户申领

券额、商品价款及县级补贴以分计。终端晚报记录中的 actual_minute 是凭证反映的交易时点，消息台账的 minute 代表服务收到时点。

`fixtures/registers.json` 提供演练台账，`fixtures/events.ndjson` 提供各端送达的独立消息，`fixtures/receipts.json` 提供合作方的模拟回执。事件游标为送达顺序。业务发生时点出现在消息内容中时，以其来源所注明的时点为准。整套资料均为合成数据。

## 券务后端（voucher 包）

`voucher/` 是本次及后续活动可复用的券务后端，仅依赖 Python 3.11 标准库，数据落在单个 SQLite 文件中，线程安全、可在进程重启后继续办理。核心模块：

| 模块 | 职责 |
| --- | --- |
| `voucher/engine.py` | 领域引擎：核用判定、离线权利、暂停、退订、改配、申领、兑付、结算与三类查询视图 |
| `voucher/gateway_client.py` | 联调入口客户端：消息分页拉取、回执投递、丢回复查询与幂等重试 |
| `voucher/api.py` | 本地 HTTP 服务（只监听 127.0.0.1），对外提供家庭、商户、组委会接口 |
| `voucher/__main__.py` | 命令行入口：装载、端到端演练、启动服务与查询 |

### 规则如何落地

- **家庭共享池防双花**：券按家庭发放，成员名单内任一人出示都计入同一家庭额度。核用成立时校验 `已用净额 + 本笔 ≤ 券总额`，因此同一张券跨点使用、两个家庭成员分别在两台终端出示，都不会花掉同一部分额度。
- **离线权利与到期**：每台终端的离线权利（grant）有商户归属、容量与自身到期时点；核用以 `actual_minute`（凭证反映的实际交易时点）判断是否在权利有效期内，逾期即拒。
- **终端、时点、额度范围与商户签认**：核用记录同时保存终端编号、实际发生时点、所属离线权利与商户（由终端归属派生），凭条号 `receipt` 做幂等键。终端重连后重复转送同一凭条只返回 `duplicate_receipt`，不会产生第二笔核用或第二次补贴。
- **暂停的时点语义**：暂停/恢复按时间留痕。判定以实际交易时点对照暂停台账：暂停生效前已在有效权利内完成的使用，晚报仍可申领；暂停生效后的交易不能借一张日期写早的营业凭条取得补贴（`actual_minute` 晚于暂停即拒）。
- **项目适用与自付款**：券只对指定项目、商户、时间有效。一笔交易可含多行，不适用项目的行券额为 0、价款计入 `self_pay_cents`，整笔标记为 `partial`；全部不适用则拒绝。
- **改配只腾确认空闲**：跨终端改配先登记为 `pending`；结清前一台权利时，`可腾出 = 权利容量 − 已确认核用净额`。未报不等于未用，未查清的核用不被当作空闲调走，事后迟到凭条仍落在原终端范围内可查（权利已关闭则留痕拒绝）。
- **退订与复用**：退订只退该项目对应行的券额。券在退订时点仍有效，额度才恢复为家庭可复用；过期后退订不恢复。未兑付申领相应核减或撤销；已到账补贴不抹去，退订额转为下次结算的扣回（负结单冲减后续应结款）。
- **回执驱动的兑付**：申领生成后进入付款 outbox，经合作方回执通道投递。`lost_reply` 时对端已落账但连接断开，客户端先 `GET /v1/receipts/<reference>` 查落账记录再决定是否重发；同一编号内容重试安全，编号被他用返回 409。兑付成功才写 payout 并把县级资金承诺标记为 `fulfilled`。
- **三类查询**：家庭查可用/暂占用/已用与逐笔核用退订；商户按交易对账并出结算单；组委会追查县级资金承诺（promised/fulfilled）与实际兑付。

### 本地联调

使用 Python 3.11.2 标准库。联调入口只监听本地地址，不需要账号或外部连接。

```sh
python3 -m unittest -v
python3 fixture_gateway.py --port 8765
```

另起一个终端，用 SQLite 文件 `demo.db` 跑端到端演练（装载台账 → 摄入消息 → 兑付 → 改配 → 结算）：

```sh
python3 -m voucher --db demo.db --gateway-port 8765 demo
python3 -m voucher --db demo.db --gateway-port 8765 serve --port 8080
```

### HTTP 接口（券务服务，默认 8080）

```
GET  /v1/households/<household>               家庭：可用/暂占用/已用、核用与退订
GET  /v1/merchants/<merchant>/reconciliation  商户：按交易对账、申领状态、到账、待扣回
GET  /v1/merchants/<merchant>/settlements     结算单列表；POST 同路径（body 含 minute）触发结算
GET  /v1/uses/<receipt>                       单笔凭条核用明细（含拒绝原因与分行券额/自付）
GET  /v1/grants                               离线权利、确认占用与空闲容量
GET  /v1/reallocations                        跨终端改配记录
GET  /v1/committee/funds                      组委会：县级承诺与实际兑付追查
POST /v1/grants/<terminal>/close              结清权利，body 含 minute、可选 transfer_to
POST /v1/admin/ingest                         从联调入口分页拉取消息并办理（游标幂等）
POST /v1/admin/payouts                        送出待兑付申领，body 可选 minute
```

命令行也可直接打印视图：`household <household>`、`merchant <merchant>`、`funds`、`outbox`、`dead-letters`。

## 联调数据入口（fixture_gateway）

`GET /v1/registers` 返回台账。用 `GET /v1/events?after=0&limit=50` 读取一页消息，后续页将上一页的 `next_cursor` 作为 `after`。每条消息有 `cursor`、`source`、`minute` 及原业务记录 `payload`。

对端发送协议为 `POST /v1/receipts`，正文提供 `reference`、`operation`、`recipient`，三个字段均为字符串。需要配对的操作与接收方见回执示例文件。返回对象含 `reference` 和对端 `receipt`。若条目设置 `lost_reply`，首次调用被记录后连接断开。可用 `GET /v1/receipts/<reference>` 查看已记录的回执。相同编号和内容可重试，编号被另一个操作使用时返回 409。

联调进程运行期间保存回执记录。需要试读其他台账、消息与回执时，可通过 `--fixtures` 指向另一组同名资料文件。合作方只返回模拟事实，业务办理功能由各方应用负责。
