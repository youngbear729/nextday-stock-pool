# -*- coding: utf-8 -*-
"""将选股、竞价确认和回测结果发送到飞书自定义机器人。

配置通过环境变量注入，避免把 webhook 写入仓库：
  FEISHU_WEBHOOK_URL       必填，GitHub Secret
  FEISHU_WEBHOOK_SECRET    可选，飞书机器人开启签名校验时填写

没有配置 URL 时安全跳过，方便本地运行和未配置 Secret 的工作流继续工作。
"""
from __future__ import annotations

import base64
import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def _fmt_pct(value) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)


def build_daily_message(latest: dict) -> str:
    """生成适合飞书文本机器人展示的短消息。"""
    signal_date = latest.get("signal_date", "-")
    plan = latest.get("plan") or {}
    regime = latest.get("regime") or {}
    decision = latest.get("t1_decision") or {}
    picks = latest.get("picks") or []
    stats = latest.get("stats") or {}

    lines = [
        "【A股次日选股清单】",
        f"信号日：{signal_date}",
        f"计划买入：{plan.get('buy_date') or '-'} | 卖出：{plan.get('sell_date') or '-'}",
        f"大盘风控：{'通过' if regime.get('ok') else '触发，建议空仓'}",
        f"执行建议：{decision.get('buy_note') or '-'}",
    ]

    if picks:
        lines.append("候选池：")
        for i, item in enumerate(picks, 1):
            code = item.get("code", "")
            name = item.get("name", "")
            pct = _fmt_pct(item.get("pct"))
            close = item.get("close")
            close_text = f"{float(close):.2f}" if close not in (None, "") else "-"
            lines.append(f"{i}. {code} {name} | 收盘 {close_text} | 涨跌 {pct}")
    else:
        lines.append("候选池：今日无合格候选")

    steps = decision.get("steps") or []
    if steps:
        lines.append(f"次日执行：{steps[0].get('rule') or '-'}")
    if stats:
        lines.append(
            "筛选统计："
            f"全市场 {stats.get('universe', '-')}，"
            f"流动性后 {stats.get('liquidity', '-')}，"
            f"趋势合格 {stats.get('trend_ok', '-')}"
        )

    lines.extend([
        "",
        "仅供研究参考，不构成投资建议；股市有风险，入市需谨慎。",
    ])
    return "\n".join(lines)


def build_auction_message(result: dict) -> str:
    """生成竞价确认通知。"""
    action = result.get("action") or ("buy" if result.get("allow_buy") else "skip")
    action_text = "买入" if action == "buy" else "空仓/顺延"
    lines = [
        "【A股竞价确认】",
        f"确认时间：{result.get('confirm_time', '-')}",
        f"信号日：{result.get('signal_date', '-')} | 交易日：{result.get('buy_date', '-')}",
        f"确认结果：{action_text}",
        f"数据状态：{'正常' if result.get('data_ok') else '异常'} | 大盘允许买入：{'是' if result.get('allow_buy') else '否'}",
    ]

    if action == "buy":
        open_px = result.get("open_px")
        open_text = f"{float(open_px):.3f}" if open_px not in (None, "") else "-"
        lines.extend([
            f"标的：{result.get('code', '-')} {result.get('name', '-')}",
            f"候选排名：{result.get('rank', '-')} | 竞价价：{open_text}",
            f"竞价涨跌：{_fmt_pct(result.get('gap_pct'))} | 建议仓位：{result.get('position_pct', '-')}%",
        ])
    else:
        lines.append(f"原因：{result.get('note') or '未形成买入信号'}")
        chain = result.get("chain") or []
        if chain:
            lines.append("候选处理：")
            lines.extend(f"- {item}" for item in chain[:5])

    lines.extend([
        "",
        "仅供研究参考，不构成投资建议；实际交易请自行核验行情和风险。",
    ])
    return "\n".join(lines)


def build_backtest_message(report: dict) -> str:
    """生成策略回测摘要通知。"""
    meta = report.get("meta") or {}
    metrics = report.get("metrics") or {}

    def value(key: str, suffix: str = "-") -> str:
        item = metrics.get(key)
        if item in (None, ""):
            return suffix
        try:
            return f"{float(item):+.2f}%" if key.endswith("%") or key in {
                "总收益率%", "年化收益率%", "最大回撤%", "胜率%", "平均单笔收益%",
                "同期市场等权基准%",
            } else f"{float(item):.2f}"
        except (TypeError, ValueError):
            return str(item)

    lines = [
        "【A股策略回测更新】",
        f"运行时间：{meta.get('run_time', '-')}",
        f"回测区间：{meta.get('start', '-')} 至 {meta.get('end', '-')}（{meta.get('days', '-')}个交易日）",
        f"总收益率：{value('总收益率%')} | 年化收益率：{value('年化收益率%')}",
        f"最大回撤：{value('最大回撤%')} | 夏普比率：{value('夏普比率')}",
        f"胜率：{value('胜率%')} | 交易次数：{metrics.get('交易次数', '-')} | 平均单笔：{value('平均单笔收益%')}",
        f"同期市场等权基准：{value('同期市场等权基准%')}",
        f"空仓天数：{metrics.get('空仓天数', '-')} | 总费用：{metrics.get('总费用(元)', '-')}",
    ]
    if meta.get("rules"):
        lines.append(f"口径：{meta['rules']}")
    lines.extend([
        "",
        "回测结果不代表未来收益，实盘前请确认数据完整性、滑点和成交约束。",
    ])
    return "\n".join(lines)


def build_message(data: dict, mode: str = "daily") -> str:
    if mode == "auction":
        return build_auction_message(data)
    if mode == "backtest":
        return build_backtest_message(data)
    return build_daily_message(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发送飞书策略通知")
    parser.add_argument(
        "--mode",
        choices=("daily", "auction", "backtest"),
        default="daily",
        help="通知类型，默认 daily",
    )
    parser.add_argument("--json", dest="json_path", help="结果 JSON 路径，默认按 mode 选择 output 文件")
    return parser.parse_args()


def _signature(timestamp: str, secret: str) -> str:
    # 飞书自定义机器人签名算法：HMAC-SHA256(timestamp + "\\n" + secret)。
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send(webhook_url: str, message: str, secret: str = "") -> dict:
    payload = {
        "msg_type": "text",
        "content": {"text": message},
    }
    if secret:
        timestamp = str(int(time.time()))
        payload.update({"timestamp": timestamp, "sign": _signature(timestamp, secret)})

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - URL来自GitHub Secret
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"飞书 webhook HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"飞书 webhook 网络错误: {exc.reason}") from exc

    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"飞书返回非 JSON: {raw[:500]}") from exc

    # 飞书机器人成功通常返回 code=0；若返回非零码，让工作流失败并触发已有 Issue 告警。
    code = result.get("code")
    if code not in (None, 0):
        raise RuntimeError(f"飞书 webhook 返回失败: {result}")
    return result


def main() -> None:
    args = parse_args()
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("未配置 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return

    default_files = {
        "daily": "latest.json",
        "auction": "auction.json",
        "backtest": "backtest.json",
    }
    latest_path = Path(
        args.json_path
        or os.environ.get("LATEST_JSON", str(ROOT / "output" / default_files[args.mode]))
    )
    if not latest_path.exists():
        raise FileNotFoundError(f"找不到选股结果: {latest_path}")
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    message = build_message(latest, args.mode)
    result = send(webhook_url, message, os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip())
    print(f"飞书推送成功: mode={args.mode}, response={result}")


if __name__ == "__main__":
    main()
