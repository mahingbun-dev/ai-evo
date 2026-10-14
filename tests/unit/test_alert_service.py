"""告警服务单元测试（W13）

覆盖 app/services/alert_service.py 的行为契约：
  - ALERT_WEBHOOK_URL 配置后 → POST JSON（level/title/details/source/timestamp）
  - 缺省为空 = 禁用 webhook，不发任何网络请求，但 alerts.log 仍落盘
  - webhook 失败（超时/非 2xx）不抛异常，alerts.log 仍落盘
  - 告警是旁路能力：任何失败都不允许拖挂调用方
"""
import json
import urllib.request

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标

from app.services import alert_service


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def alert_log(tmp_path, monkeypatch):
    """把 alerts.log 重定向到临时目录，避免污染仓库 logs/"""
    p = tmp_path / "logs" / "alerts.log"
    monkeypatch.setattr(alert_service, "ALERT_LOG_PATH", p)
    return p


@pytest.fixture
def recorded_posts(monkeypatch):
    """假 webhook：记录每次 POST 的 url/body/method，不发生真实网络请求"""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "body": json.loads(req.data.decode("utf-8")),
                "content_type": req.headers.get("Content-type"),
                "timeout": timeout,
            }
        )
        return _FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


class TestWebhookEnabled:
    def test_posts_json_payload(self, alert_log, recorded_posts, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://fake-im.local/hook")

        delivered = alert_service.send_alert(
            level="error",
            title="评测任务失败",
            details={"evaluation_task_id": 42, "error": "boom"},
            source="celery",
        )

        assert delivered is True
        assert len(recorded_posts) == 1
        call = recorded_posts[0]
        assert call["url"] == "http://fake-im.local/hook"
        assert call["method"] == "POST"
        assert call["content_type"] == "application/json"
        body = call["body"]
        assert body["level"] == "error"
        assert body["title"] == "评测任务失败"
        assert body["details"] == {"evaluation_task_id": 42, "error": "boom"}
        assert body["source"] == "celery"
        assert body["timestamp"]  # 存在即可，格式不作为契约

    def test_alert_log_written_with_delivered_flag(self, alert_log, recorded_posts, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://fake-im.local/hook")
        alert_service.send_alert(level="error", title="t", details={"k": "v"})

        lines = alert_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["webhook_delivered"] is True
        assert record["title"] == "t"


class TestWebhookDisabled:
    def test_unset_env_no_network_but_log_written(self, alert_log, recorded_posts, monkeypatch):
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

        delivered = alert_service.send_alert(level="warning", title="离线告警", details={"a": 1})

        # 缺省 = 禁用：不发任何网络请求
        assert recorded_posts == []
        assert delivered is False
        # 兜底落盘仍生效
        record = json.loads(alert_log.read_text(encoding="utf-8").strip())
        assert record["title"] == "离线告警"
        assert record["webhook_delivered"] is False

    def test_blank_env_treated_as_disabled(self, alert_log, recorded_posts, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "   ")
        alert_service.send_alert(level="error", title="t")
        assert recorded_posts == []


class TestWebhookFailure:
    def test_post_exception_swallowed_log_still_written(self, alert_log, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://unreachable.local/hook")

        def boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)

        # 不抛异常；webhook 失败，落盘兜底仍在
        delivered = alert_service.send_alert(level="error", title="t", details={})
        assert delivered is False
        record = json.loads(alert_log.read_text(encoding="utf-8").strip())
        assert record["webhook_delivered"] is False

    def test_non_2xx_counts_as_not_delivered(self, alert_log, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://fake-im.local/hook")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(500))

        delivered = alert_service.send_alert(level="error", title="t")
        assert delivered is False


class TestNeverRaises:
    def test_log_write_failure_does_not_raise(self, monkeypatch, tmp_path):
        # ALERT_LOG_PATH 指向一个普通文件 → 其 parent.mkdir 抛 → send_alert 内部吞掉
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(alert_service, "ALERT_LOG_PATH", blocker / "alerts.log")
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

        # 不应抛出任何异常
        assert alert_service.send_alert(level="error", title="t") is False


def test_build_payload_contract():
    payload = alert_service.build_payload("error", "标题", {"k": 1}, "src")
    assert set(payload.keys()) == {"level", "title", "details", "source", "timestamp"}
    assert payload["level"] == "error"
    assert payload["title"] == "标题"
    assert payload["details"] == {"k": 1}
    assert payload["source"] == "src"


def test_details_none_defaults_to_empty_dict():
    payload = alert_service.build_payload("error", "标题")
    assert payload["details"] == {}
