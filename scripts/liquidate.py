#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦资产清算官 - 独立清算脚本（可选组件）

纯 Python 标准库实现，零第三方依赖。
作用：对 `query-my-coupons` 返回的 Markdown 表格做解析，计算每张券的
剩余天数与损失等级，按「损失速度 = 价值 ÷ 剩余天数」排序输出清算清单。

本脚本不直接访问麦当劳 MCP（网络调用由 Agent 宿主完成），
而是接收粘贴过来的券包 Markdown 文本或文件，本地完成清算计算。
这样设计的好处：不含 Token、不触网、可单测。

用法：
    python liquidate.py coupons.md
    cat coupons.md | python liquidate.py
"""

import sys
import json
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- 契约处理

P0, P1, P2, P3 = "🔴 P0", "🟠 P1", "🟡 P2", "🟢 P3"


def parse_markdown_table(text: str):
    """解析 query-my-coupons 返回的 Markdown 表格。

    契约陷阱：该工具返回 Markdown 而非 JSON；响应可能带前导说明文字，
    因此不假设首行就是表头，逐行扫描以 '|' 开头的行。
    """
    rows, header = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        # 分隔行（如 |---|---|）跳过
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if header is None:
            header = cells
        else:
            rows.append(dict(zip(header, cells)))
    return rows


def parse_money(raw: str) -> Decimal:
    """券面金额 → Decimal（元）。

    query-my-coupons 金额字段一般是「元字符串」（如 "13.9"），
    也可能带 ¥ 前缀或混入说明文字，失败时按 0 处理并保留原文。
    """
    m = re.search(r"(\d+(?:\.\d+)?)", str(raw))
    if not m:
        return Decimal("0")
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return Decimal("0")


def parse_expire(raw: str):
    """从文本中提取到期日期，兼容 yyyy-MM-dd / yyyy/MM/dd / yyyy年M月d日。"""
    patterns = [
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})",
    ]
    for p in patterns:
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


def liquidate(text: str, today: date = None):
    """清算主流程：解析 → 计算死亡时间 → 排序 → 生成清单。"""
    if today is None:
        today = date.today()  # 生产环境由 Agent 用 now-time-info 对表后传入

    rows = parse_markdown_table(text)
    if not rows:
        print("⚠️ 未能从输入中解析出券表格（query-my-coupons 返回 Markdown 表格，"
              "请确认粘贴内容完整）。以下为原始输入：\n")
        print(text[:2000])
        return None

    items = []
    for row in rows:
        joined = " ".join(row.values())
        expire = parse_expire(joined)
        # 列名模糊匹配：金额列、券名列、日期列
        name = next((v for k, v in row.items()
                     if any(x in k for x in ("名称", "券名", "内容", "标题"))), None)
        value_raw = next((v for k, v in row.items()
                          if any(x in k for x in ("金额", "价值", "面额", "面值"))), None)
        value = parse_money(value_raw if value_raw is not None else joined)
        if name is None:
            name = next(iter(row.values()), "未知资产")
        if expire is None:
            # 无到期日的资产（如永久权益）不参与清算排序
            items.append({
                "name": name, "value": value, "expire": None,
                "days_left": None, "level": P3, "speed": Decimal("0"),
            })
            continue
        # 含当天：当天到期 = 剩 1 天
        days_left = max((expire - today).days, 0) + 1
        speed = value / days_left  # 单位时间流失速度
        items.append({
            "name": name, "value": value, "expire": expire,
            "days_left": days_left, "level": loss_level(days_left),
            "speed": speed,
        })

    # 排序：先按损失速度降序，再按剩余天数升序
    items.sort(key=lambda x: (-x["speed"], x["days_left"] or 10**9))
    return items


def render(items, today: date) -> str:
    lines = [f"🍔 麦麦资产清算报告（基准日：{today.isoformat()}）", ""]
    dying = [i for i in items if i["expire"] and i["days_left"] <= 7]
    total = sum((i["value"] for i in dying), Decimal("0"))
    if dying:
        lines.append(f"⚠️ 未来 7 天内有 ¥{total} 的麦麦资产正在死亡：")
    else:
        lines.append("✅ 未来 7 天内没有即将死亡的资产。")
    lines.append("")
    lines.append("| 优先级 | 资产 | 价值(¥) | 死亡时间 | 剩余 |")
    lines.append("|--------|------|---------|----------|------|")
    for i in items:
        if i["expire"] is None:
            lines.append(f"| {i['level']} | {i['name']} | {i['value']} | 永久 | - |")
            continue
        lines.append(
            f"| {i['level']} | {i['name']} | {i['value']} "
            f"| {i['expire'].isoformat()} | {i['days_left']} 天 |"
        )
    return "\n".join(lines)


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    items = liquidate(text)
    if items is not None:
        print(render(items, date.today()))
        print()
        print(json.dumps(
            [{"name": i["name"], "value": str(i["value"]),
              "expire": i["expire"].isoformat() if i["expire"] else None,
              "days_left": i["days_left"], "level": i["level"]}
             for i in items], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
