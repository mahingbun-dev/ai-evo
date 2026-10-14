"""告警服务（W13 整改计划 §3）

统一告警出口，当前两个调用方：
  - app/tasks/celery_tasks.py ``EvaluationBaseTask.on_failure``：评测任务失败
  - tools/build-service/build_service.py：部署 verify 失败（该服务是独立容器，
    不能 import 主应用包，在本地用标准库实现了同一契约，见其模块内注释）

行为契约：
  1. ``ALERT_WEBHOOK_URL`` 环境变量非空 → 向该地址 POST JSON
     （字段：level / title / details / source / timestamp）；
     缺省（空串）= 禁用 webhook，不发任何网络请求。
  2. 无论 webhook 是否配置/成功，都在 ``logs/alerts.log`` 落一条 JSON Line
     ——离线环境暂无 IM 出口时这是唯一告警记录，升级路径见
     docs/alerts-and-health.md（配置 webhook 即生效，无需改代码）。
  3. 告警是旁路能力，任何失败（webhook 超时/落盘失败）都不抛异常，
     不允许把业务主流程拖挂。
"""
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 与 app/main.py 的 LOG_DIR 同一目录（仓库根 logs/；容器内挂载 ./logs:/app/logs）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALERT_LOG_PATH = PROJECT_ROOT / "logs" / "alerts.log"

WEBHOOK_TIMEOUT_SECONDS = 5


def webhook_url() -> str:
    """每次调用时读取环境变量（不缓存在 import 时），便于运维改动与测试注入"""
    return os.environ.get("ALERT_WEBHOOK_URL", "").strip()


def build_payload(
    level: str,
    title: str,
    details: Optional[Dict[str, Any]] = None,
    source: str = "ai-evo",
) -> Dict[str, Any]:
    """构造告警报文（webhook 与 alerts.log 共用同一契约）"""
    return {
        "level": level,
        "title": title,
        "details": details or {},
        "source": source,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def post_webhook(url: str, payload: Dict[str, Any]) -> bool:
    """POST JSON 到 webhook；2xx 视为送达。任何异常都吞掉并返回 False"""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.warning("告警 webhook 返回非 2xx: %s %s", resp.status, url)
            return ok
    except Exception as e:  # noqa: BLE001 - 告警失败绝不抛出
        logger.warning("告警 webhook 发送失败 (%s): %s", url, e)
        return False


def append_alert_log(payload: Dict[str, Any], webhook_delivered: bool) -> None:
    """追加一条 JSON Line 到 logs/alerts.log（离线兜底 + 审计留痕）"""
    ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record["webhook_delivered"] = webhook_delivered
    with open(ALERT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def send_alert(
    level: str,
    title: str,
    details: Optional[Dict[str, Any]] = None,
    source: str = "ai-evo",
) -> bool:
    """发一条告警：webhook（若配置）+ logs/alerts.log 兜底。

    返回 webhook 是否送达；未配置 webhook 时返回 False（告警仍已落盘）。
    本函数不抛异常。
    """
    payload = build_payload(level, title, details, source)
    url = webhook_url()
    delivered = post_webhook(url, payload) if url else False
    try:
        append_alert_log(payload, delivered)
    except Exception as e:  # noqa: BLE001 - 落盘失败也不抛出
        logger.error("告警落盘 logs/alerts.log 失败: %s", e)
    return delivered
