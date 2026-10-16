# 麦当劳中国 MCP 工具参考（清算场景精选）

来源：[麦当劳中国 MCP 官方接入指南](https://open.mcd.cn/mcp/doc) 与官方仓库 [M-China/mcd-mcp-server](https://github.com/M-China/mcd-mcp-server) README。**服务器实时返回的 input schema 优先于本文件**。

## 连接约束

- 端点：`https://mcp.mcd.cn`
- 传输：Streamable HTTP（MCP 配置 `type: "streamablehttp"`）
- 认证：`Authorization: Bearer <MCD_MCP_TOKEN>`
- 支持的最高 MCP 协议版本：`2025-06-18`
- 限流：每 Token 每分钟 600 次，超限返回 `429`
- Token 申请：https://open.mcd.cn/mcp （登录 → 控制台 → 激活 → 同意协议 → 复制）

## 清算用到的 7 个工具

| 工具 | 用途 | 入参 | 清算中扮演的角色 |
|---|---|---|---|
| `now-time-info` | 服务器当前时间 | 空 | 第 0 步对表，所有剩余天数的基准 |
| `query-my-coupons` | 卡包中的券 | 空 | 主资产扫描 ⚠️ 返回 Markdown |
| `available-coupons` | 可领取未领的券 | 空 | 未领损失扫描 |
| `auto-bind-coupons` | 一键领取所有可领券 | 空 | 写操作，须用户确认后执行 |
| `query-my-account` | 积分账户 | 空 | 积分余额与清零风险 |
| `mall-order-list` | 商城订单列表 | `lastId?`、`size?` | 未核销虚拟券扫描 |
| `mall-points-products` | 积分可兑商品 | `catRuleIds?` | 积分清算去向建议 |

## 关键契约陷阱（实测，官方文档未写）

1. **`query-my-coupons` 返回 Markdown 表格，不是 JSON**。解析表格行（`|` 分隔），失败时把原始文本展示给用户，不要伪造数据。
2. **价格单位按工具区分**：仅 `calculate-price` 的价格字段是"分"（无引号整数）；本 Skill 用到的券面金额、积分字段已是"元"字符串/整数，直接展示，不要除以 100。
3. **响应前导说明不统一**：部分工具的返回 JSON 前有 `Original Response` 等前导文字，解析前先定位第一个 `{`，不能硬编码前导标题。
4. **时间基准**：`now-time-info` 返回 `timestamp`、`formatted`、`date`、`dayOfWeek`、`timezone`。相对日期不明确时先调用它，不要用本地时钟。
5. **顶层检查**：所有工具先检查顶层 `success`、`code`、`message`、`data`；失败响应可能含 `traceId`，可向用户展示 traceId 便于排查，但**绝不展示 Token**。
6. **401 处理**：Token 缺失/过期统一提示用户去 https://open.mcd.cn/mcp 重新获取，不重试。

## 返回值注意点

- `query-my-coupons` 的券字段以实际返回为准（常见：券名、券面金额、有效期起止、使用限制、适用门店）。剩余天数 = 有效期止 - 服务器时间，**算天数时含当天**（当天到期的券是 P0 中的 P0）。
- `query-my-account` 返回积分余额与到期规则（如"年底清零"类规则）。若返回中无明确到期字段，把规则文案原样转述给用户，不要自行编造日期。
- `mall-order-list` 返回 `hasNext`、`lastId`、`list[]`；翻页时传 `lastId`。虚拟券订单重点看"未核销/待使用"状态。
- `mall-points-products` 返回 `spuName`、`spuId`、`point` 等；兑换前如需详情再调 `mall-product-detail`（必填 `spuId`，Long）。

## 限流与并发

- 清算标准流程 5-7 次调用，远低于 600/分钟。
- 无依赖的读取类调用可并发；`auto-bind-coupons` 必须单独、确认后执行。
