"""L2 运行时自检 + mockhub 注册/调用流程（离线，不需要真中台与真 ai-evo）。

对应接入指南的四道关：
- L2：conformance.runtime 对运行中的插件全绿（gRPC 起得来、五项应答正常）；
- L3 替身：mockhub 接受注册（manifest 自洽、实例不冲突）；
- L4 替身：经真实 gRPC 面调一次 Handle，双通道（hub.tool / payload.op）分发正常。

注意：health() 的 patch 必须覆盖「检查阶段」而不仅是「启动阶段」——runtime() 与
注册都会回调 health()。曾因 patch 作用域只包住 _start_plugin 导致假阳性
（本地恰好有 dev server 监听 8100 才没红），在 python:3.12 容器里暴露。
"""
import socket
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import hubkit
from hubkit import conformance, mockhub
from hubkit.config import Config
from hubkit.log import JsonLogger
from hubkit.proto.hubv1 import plugin_pb2
from hubkit.run import serve

from app.hub_plugin.plugin import AievoHubPlugin

import pytest
pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _wait_for_port(addr: str, timeout: float = 10.0):
    """等插件真的开始监听（serve 起来与测试线程拿到控制权之间有窗口）。"""
    host, port = addr.split(":")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((host, int(port))) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"插件未在 {timeout}s 内开始监听 {addr}")


@contextmanager
def _served_plugin(hub_addr: str, instance_id: str):
    """后台线程把插件 serve 起来，health patch 覆盖整个持有期。

    本地没有真 ai-evo，health() 被 patch 成自报健康；真实环境由 ai-evo api
    可达性决定（不可达时中台摘除实例，这是设计语义）。
    """
    with patch.object(
        AievoHubPlugin,
        "health",
        lambda self: plugin_pb2.HealthResponse(healthy=True),
    ):
        stop = threading.Event()
        addr = mockhub.free_addr()
        cfg = Config(
            hub_addr=hub_addr,
            advertise_addr=f"http://{addr}",
            listen_addr=addr,
            instance_id=instance_id,
            logger=JsonLogger(),
        )
        thread = threading.Thread(
            target=serve, args=(AievoHubPlugin(), cfg, stop), daemon=True
        )
        thread.start()
        _wait_for_port(addr)
        try:
            yield f"http://{addr}"
        finally:
            stop.set()
            time.sleep(0.2)


def test_runtime_conformance_green():
    """L2：真实 gRPC 服务上跑 runtime()，五项检查全绿。"""
    hub = mockhub.Hub()
    try:
        with _served_plugin(hub.addr, "aievo-l2") as addr:
            report = conformance.runtime(addr)
            assert report.passed(), str(report)
    finally:
        hub.close()


def test_registers_with_mockhub():
    """L3 替身：mockhub 收到注册，manifest 自洽（ai-evo@1.2.0）。"""
    hub = mockhub.Hub()
    try:
        with _served_plugin(hub.addr, "aievo-register"):
            hub.wait_for_registration(1, timeout=10)
            reg = hub.last_registration
            assert reg is not None
            assert reg.manifest.name == "ai-evo"
            assert reg.manifest.version == "1.3.0"
            assert reg.instance_id == "aievo-register"
    finally:
        hub.close()


def test_invoke_via_grpc_dispatches_tool():
    """L4 替身：真实 gRPC Handle 调用，双通道分发到插件体。"""
    hub = mockhub.Hub()
    try:
        with _served_plugin(hub.addr, "aievo-invoke") as addr:
            with mockhub.dial_plugin(addr) as client:
                # 聚合工具通道：meta['hub.tool'] 指定工具
                env = hubkit.with_payload_json(None, {"skip": 0, "limit": 10})
                env.meta["hub.tool"] = "dataset_list"
                out = client.handle(env)
                payload, is_json = hubkit.payload_json(out)
                assert is_json
                # 本地无 ai-evo 也无 mas_token：应得到 login_required 业务卡，
                # 而不是 gRPC 错误（链路本身必须通）
                assert payload["success"] is False
                assert payload["error_code"] == "login_required"

                # 通用入口通道：payload['op'] 指定工具
                env2 = hubkit.with_payload_json(None, {"op": "task_list"})
                out2 = client.handle(env2)
                payload2, _ = hubkit.payload_json(out2)
                assert payload2["error_code"] == "login_required"

                # 校验器拒绝未知工具（422 语义：链路通、业务拒绝）
                env3 = hubkit.with_payload_json(None, {"op": "no_such"})
                resp = client.validate(env3)
                assert not resp.valid
    finally:
        hub.close()
