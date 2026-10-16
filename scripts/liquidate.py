#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦资产清算官 - 全功能清算引擎（可选组件）

纯 Python 标准库实现，零第三方依赖。

覆盖四大清算模块（与 SKILL.md 流程一一对应）：
  1. 券包清算     —— query-my-coupons 的 Markdown 表格解析 + 死亡时间计算
  2. 积分账户     —— query-my-account 的余额/清零规则解析
  3. 商城订单     —— mall-order-list 中未核销虚拟券扫描
  4. 可领未领券   —— available-coupons 的窗口损失扫描

设计约定：
  - 本脚本不直接访问麦当劳 MCP（网络调用由 Agent 宿主完成），
    而是接收宿主回传的文本（Markdown 表格 / 带前导说明的 JSON / 纯文本），
    本地完成解析与清算计算。不含 Token、不触网、可单测。
  - 契约陷阱在代码内注释标明（Markdown 返回、价格单位、前导说明等）。

用法：
    python liquidate.py coupons.md                     # 单文件
    python liquidate.py coupons.md account.txt orders.md available.md
    cat mixed.txt | python liquidate.py                # stdin 混合文本，自动分段识别
    python liquidate.py --demo                         # 内置示例数据演示
"""

import sys
import json
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- 常量与分级

P0, P1, P2, P3 = "🔴 P0", "🟠 P1", "🟡 P2", "🟢 P3"

LEVEL_ORDER = {P0: 0, P1: 1, P2: 2, P3: 3}

# ---------------------------------------------------------------- 基础解析


def strip_preamble(text: str) -> str:
    """剥掉 MCP 响应前导说明，返回纯 JSON（若存在）。

    契约陷阱：部分工具的返回 JSON 前垫有 'Original Response' 等说明文字，
    且不是每个 tool 都带，不能硬编码标题，直接定位第一个 '{' 或 '['。
    """
    for ch in ("{", "["):
        idx = text.find(ch)
        if idx >= 0:
            return text[idx:]
    return text


def parse_markdown_table(text: str):
    """解析 Markdown 表格（query-my-coupons 返回 Markdown 而非 JSON）。

    不假设首行即表头：逐行扫描 '|' 开头的行，跳过分隔行。
    """
    rows, header = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if header is None:
            header = cells
        else:
            rows.append(dict(zip(header, cells)))
    return rows


def parse_money(raw) -> Decimal:
    """金额/积分 → Decimal。兼容 '¥13.9'、'13.9元'、'1200 分' 等混排文本。"""
    m = re.search(r"(\d+(?:\.\d+)?)", str(raw))
    if not m:
        return Decimal("0")
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return Decimal("0")


DATE_PATTERNS = [
    r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})",
]


def parse_date(raw):
    """从文本提取日期，兼容 2026-10-17 / 2026/10/22 / 2026年11月05日 等。"""
    for p in DATE_PATTERNS:
        m = re.search(p, str(raw))
        if m:
            y, mo, d = (int(g) for g in m.groups())
            try:
                return date(y, mo, d)
            except ValueError:
                continue
    return None


def loss_level(days_left: int) -> str:
    """损失等级：≤3 天 P0，4-7 天 P1，8-30 天 P2，>30 天 P3。"""
    if days_left <= 3:
        return P0
    if days_left <= 7:
        return P1
    if days_left <= 30:
        return P2
    return P3


# ---------------------------------------------------------------- 模块 1：券包清算


def liquidate_coupons(text: str, today: date):
    """解析卡包券 Markdown 表格 → 资产条目列表。"""
    rows = parse_markdown_table(text)
    assets = []
    for row in rows:
        joined = " ".join(row.values())
        name = next((v for k, v in row.items()
                     if any(x in k for x in ("名称", "券名", "内容", "标题"))), None)
        value_raw = next((v for k, v in row.items()
                          if any(x in k for x in ("金额", "价值", "面额", "面值"))), None)
        value = parse_money(value_raw if value_raw is not None else joined)
        if name is None:
            name = next(iter(row.values()), "未知资产")
        expire = parse_date(joined)
        note = next((v for k, v in row.items()
                     if any(x in k for x in ("限制", "备注", "说明", "规则"))), "")
        if expire is None:
            assets.append(_asset(name, value, None, None, P3, note, source="卡包券"))
            continue
        days_left = max((expire - today).days, 0) + 1  # 含当天：当天到期 = 剩 1 天
        assets.append(_asset(name, value, expire, days_left,
                             loss_level(days_left), note, source="卡包券"))
    return assets


# ---------------------------------------------------------------- 模块 2：积分账户


def liquidate_account(text: str, today: date):
    """解析 query-my-account 返回（JSON 或文本）→ 积分资产条目。

    契约注意：若返回中无明确到期字段，把规则文案原样转述，
    不编造日期（无日期的积分按 P3 观察处理，仅提示规则原文）。
    """
    assets = []
    data = None
    try:
        data = json.loads(strip_preamble(text))
    except (json.JSONDecodeError, ValueError):
        data = None

    balance = None
    rule_text = ""
    expire = None

    if isinstance(data, dict):
        # 常见字段名兼容：totalPoints/points/balance
        for key in ("totalPoints", "points", "balance", "point"):
            if key in data:
                balance = parse_money(data[key])
                break
        # 嵌套 data 场景
        if balance is None and isinstance(data.get("data"), dict):
            inner = data["data"]
            for key in ("totalPoints", "points", "balance", "point"):
                if key in inner:
                    balance = parse_money(inner[key])
                    break
        # 到期/清零相关字段
        for key in ("expireTime", "expiryTime", "clearTime", "validEndTime"):
            if key in data:
                expire = parse_date(data[key])
                break
            if isinstance(data.get("data"), dict) and key in data["data"]:
                expire = parse_date(data["data"][key])
                break
        for key in ("rule", "expireRule", "clearRule", "description", "message"):
            if key in data and data[key]:
                rule_text = str(data[key])
                break
    if balance is None:
        m = re.search(r"(?:积分|余额)[^\d]{0,6}(\d+(?:\.\d+)?)", text)
        if m:
            balance = parse_money(m.group(1))
    if expire is None:
        expire = parse_date(text)
    if not rule_text:
        # 取含「清零/过期/失效」的完整短句；若是 JSON 源则提取引号内文案，避免混入结构符
        m = re.search(r"[^，。\n\"]*(?:清零|过期|失效)[^，。\n\"]*", text)
        if m:
            rule_text = m.group(0).strip().rstrip('",:}{')

    if balance is None or balance <= 0:
        return assets  # 无积分或解析失败时不产出资产

    if expire is not None:
        days_left = max((expire - today).days, 0) + 1
        level = loss_level(days_left)
    else:
        days_left, level = None, P3  # 无明确日期：不排序前压，转述规则原文

    assets.append(_asset(f"积分 {int(balance)} 分", balance, expire,
                         days_left, level, rule_text, source="积分账户"))
    return assets


# ---------------------------------------------------------------- 模块 3：商城未核销订单


DIE_STATUS_KEYWORDS = ("未核销", "待使用", "待核销", "未使用", "待取", "未消费")


def liquidate_mall_orders(text: str, today: date):
    """解析 mall-order-list（JSON 或 Markdown 表格）→ 未核销虚拟券资产。

    只挑「未核销/待使用」类状态的订单；已核销/已过期/退款的不算损失资产。
    """
    assets = []
    data = None
    try:
        data = json.loads(strip_preamble(text))
    except (json.JSONDecodeError, ValueError):
        data = None

    orders = []
    if isinstance(data, dict):
        for key in ("list", "orders", "records"):
            if isinstance(data.get(key), list):
                orders = data[key]
                break
            if isinstance(data.get("data"), dict) and isinstance(data["data"].get(key), list):
                orders = data["data"][key]
                break
    if not orders:
        # 回退：Markdown 表格逐行处理
        for row in parse_markdown_table(text):
            joined = " ".join(row.values())
            if not any(k in joined for k in DIE_STATUS_KEYWORDS):
                continue
            name = next((v for k, v in row.items()
                         if any(x in k for x in ("商品", "名称", "内容"))), "未核销商品")
            expire = parse_date(joined)
            # 契约陷阱：金额列优先按列名取，避免误抓订单号/日期数字
            value_raw = next((v for k, v in row.items()
                              if any(x in k for x in ("金额", "价值", "积分", "面额", "面值"))), None)
            value = parse_money(value_raw) if value_raw is not None else Decimal("0")
            assets.append(_mall_asset(name, value, expire, today))
        return assets

    for o in orders:
        status = str(o.get("status") or o.get("orderStatus") or "")
        detail = json.dumps(o, ensure_ascii=False)
        if not (any(k in status for k in DIE_STATUS_KEYWORDS)
                or any(k in detail for k in DIE_STATUS_KEYWORDS)):
            continue
        name = (o.get("spuName") or o.get("goodsName")
                or o.get("productName") or "未核销商品")
        # 陷阱：point 是积分不是钱；金额列优先级 price/amount，积分单独标注
        if o.get("point") is not None:
            value = parse_money(o["point"])
            name = f"{name}（{int(value)} 积分兑换）"
        else:
            value = parse_money(o.get("price") or o.get("amount") or "")
        expire = parse_date(o.get("validEndTime") or o.get("expireTime") or detail)
        assets.append(_mall_asset(name, value, expire, today))
    return assets


def _mall_asset(name, value, expire, today):
    if expire is None:
        return _asset(f"[商城未核销] {name}", value, None, None, P3,
                      "未核销虚拟券，尽快核销", source="商城订单")
    days_left = max((expire - today).days, 0) + 1
    return _asset(f"[商城未核销] {name}", value, expire, days_left,
                  loss_level(days_left), "未核销虚拟券，尽快核销", source="商城订单")


# ---------------------------------------------------------------- 模块 4：可领未领券


def liquidate_available(text: str, today: date):
    """解析 available-coupons 返回 → 未领券清单（不进资产池，单独提醒）。

    未领券的价值是「窗口关闭即永久失去」，但尚未拥有，故单独列出，
    并提示 auto-bind-coupons 需用户确认后执行。
    """
    coupons = []
    for row in parse_markdown_table(text):
        joined = " ".join(row.values())
        name = next((v for k, v in row.items()
                     if any(x in k for x in ("名称", "券名", "内容"))), None) \
            or next(iter(row.values()), "可领券")
        value = parse_money(joined)
        expire = parse_date(joined)
        window = expire.isoformat() if expire else "窗口待确认"
        coupons.append({"name": name, "value": str(value), "window": window})
    if not coupons:
        # 尝试 JSON
        try:
            data = json.loads(strip_preamble(text))
            pool = data if isinstance(data, list) else (
                data.get("list") or data.get("data", {}).get("list") or [])
            for c in pool:
                if isinstance(c, dict):
                    name = (c.get("couponName") or c.get("name") or "可领券")
                    value = parse_money(c.get("amount") or c.get("price") or c)
                    expire = parse_date(c.get("validEndTime") or c.get("endTime") or "")
                    coupons.append({"name": name, "value": str(value),
                                    "window": expire.isoformat() if expire else "窗口待确认"})
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass
    return coupons


# ---------------------------------------------------------------- 汇总清算


def _asset(name, value, expire, days_left, level, note="", source=""):
    speed = (value / days_left) if (days_left and days_left > 0) else Decimal("0")
    return {
        "name": name, "value": value, "expire": expire,
        "days_left": days_left, "level": level, "note": note,
        "source": source, "speed": speed,
    }


def merge_and_rank(assets):
    """排序：损失速度（价值÷剩余天数）降序 → 剩余天数升序 → 等级。"""
    return sorted(
        assets,
        key=lambda a: (-a["speed"], a["days_left"] if a["days_left"] is not None else 10**9,
                       LEVEL_ORDER[a["level"]]),
    )


def liquidate_all(texts, today: date):
    """输入多段文本，自动识别类型并合并清算。

    识别顺序：积分账户 → 商城订单 → 可领券 → 默认券包表格。
    """
    all_assets, coupons_pool = [], []
    for text in texts:
        t = text.strip()
        if not t:
            continue
        if ("积分" in t and re.search(r"(余额|清零|totalPoints|points)", t, re.I)) \
                or "query-my-account" in t:
            all_assets += liquidate_account(t, today)
        elif ("未核销" in t or "待使用" in t or "mall-order" in t
              or "订单" in t):
            all_assets += liquidate_mall_orders(t, today)
        elif "可领" in t or "available-coupons" in t:
            coupons_pool += liquidate_available(t, today)
        else:
            all_assets += liquidate_coupons(t, today)
    return merge_and_rank(all_assets), coupons_pool


# ---------------------------------------------------------------- 报告渲染


def _action_hint(asset) -> str:
    """照做就行的行动建议。"""
    if asset["days_left"] is None:
        return "规则原文见备注，无明确到期日，暂不用管"
    if asset["level"] == P0:
        return "今天就去用掉/核销，别拖"
    if asset["level"] == P1:
        return "本周内安排一餐使用"
    if asset["level"] == P2:
        return "排进未来两周计划"
    return "暂不用管"


def render(assets, coupons_pool, today: date) -> str:
    lines = [f"🍔 麦麦资产清算报告（基准日：{today.isoformat()}）", ""]

    dying = [a for a in assets if a["days_left"] is not None and a["days_left"] <= 7]
    if dying:
        total = sum((a["value"] for a in dying), Decimal("0"))
        lines.append(f"⚠️ 未来 7 天内有 ¥{total} 的麦麦资产正在死亡：")
    else:
        lines.append("✅ 未来 7 天内没有即将死亡的资产。")
    lines.append("")

    lines.append("| 优先级 | 资产 | 价值 | 死亡时间 | 剩余 | 来源 | 照做就行的行动 |")
    lines.append("|--------|------|------|----------|------|------|----------------|")
    for a in assets:
        expire = a["expire"].isoformat() if a["expire"] else "未明确"
        days = f"{a['days_left']} 天" if a["days_left"] is not None else "-"
        value = f"{a['value']}"
        note = f"（{a['note']}）" if a["note"] and a["days_left"] is None else ""
        lines.append(f"| {a['level']} | {a['name']} | {value} | {expire} "
                     f"| {days} | {a['source']} | {_action_hint(a)}{note} |")
    lines.append("")

    if coupons_pool:
        lines.append("📬 可领未领的券（窗口关闭即损失，需你确认后一键领取）：")
        for c in coupons_pool:
            lines.append(f"   · {c['name']}（¥{c['value']}，窗口至 {c['window']}）")
        lines.append("   回复“确认领券”后执行 auto-bind-coupons。")
        lines.append("")

    lines.append("💡 写操作（领券/积分兑换）均需你逐次确认，本报告默认只读。")
    return "\n".join(lines)


def to_json(assets, coupons_pool):
    return json.dumps({
        "assets": [{
            "name": a["name"], "value": str(a["value"]),
            "expire": a["expire"].isoformat() if a["expire"] else None,
            "days_left": a["days_left"], "level": a["level"],
            "source": a["source"], "note": a["note"],
        } for a in assets],
        "available_coupons": coupons_pool,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 入口


def run(texts, today: date):
    assets, coupons_pool = liquidate_all(texts, today)
    return render(assets, coupons_pool, today), to_json(assets, coupons_pool)


def demo_texts():
    """内置示例数据（虚构，用于 --demo 与单测）。"""
    coupons = """Original Response
以下是您的卡包优惠券列表：

| 券名称 | 面额 | 有效期至 | 使用限制 |
|--------|------|----------|----------|
| 麦麦脆汁鸡两件套券 | ¥13.9 | 2026-10-17 | 限指定门店 |
| 1+1限时惊喜券 | ¥3.0 | 2026/10/22 | 工作日可用 |
| 甜筒买一送一券 | ¥8.0 | 2026年11月05日 | 单次使用 |
"""
    account = """query-my-account
{"success": true, "data": {"totalPoints": 1200, "expireTime": "2026-12-31 23:59:59",
 "rule": "积分将于年底统一清零，请及时使用"}}
"""
    orders = """mall-order-list
| 订单号 | 商品名称 | 状态 | 有效期至 |
|--------|----------|------|----------|
| M10086 | 板烧鸡腿堡虚拟券 | 未核销 | 2026-10-20 |
| M10087 | 麦辣鸡翅虚拟券 | 已核销 | 2026-10-01 |
| M10088 | 巨无霸中套餐虚拟券 | 待使用 | 2026-11-30 |
"""
    available = """available-coupons
| 券名称 | 面额 | 领取截止 |
|--------|------|----------|
| 麦辣鸡翅5元券 | ¥5.0 | 2026-10-25 |
| 板烧买一送一券 | ¥12.5 | 2026-11-01 |
"""
    return [coupons, account, orders, available]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--demo" in sys.argv:
        texts = demo_texts()
        today = date(2026, 10, 16)  # 演示基准日固定，保证输出可复现
    elif args:
        texts = [open(p, "r", encoding="utf-8").read() for p in args]
        today = date.today()  # 生产环境由 Agent 用 now-time-info 对表后传入
    else:
        texts = [sys.stdin.read()]
        today = date.today()

    report, payload = run(texts, today)
    print(report)
    print()
    print(payload)


if __name__ == "__main__":
    main()
