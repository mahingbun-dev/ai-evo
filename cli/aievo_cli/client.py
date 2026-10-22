"""aievo-cli HTTP 客户端：httpx 封装，Bearer API Key 认证。

仅覆盖门禁闭环所需的端点：任务运行、门禁查询、结果读取、数据集上传。
所有非 2xx 统一转成 AievoApiError，由命令层映射为退出码 2。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


class AievoApiError(Exception):
    """平台 API 错误（status_code 为 None 表示网络层失败）"""

    def __init__(self, status_code: Optional[int], message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AievoClient:
    """API Key 认证的 AI-Evo 平台客户端（API 前缀固定 /api/v1）"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, transport=None):
        # transport：测试注入 httpx.MockTransport，不发起真实网络
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ---------------- 底层请求 ----------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Any:
        try:
            resp = self._client.request(
                method, path, json=json_body, params=params, files=files
            )
        except httpx.HTTPError as e:
            raise AievoApiError(None, f"网络错误：无法连接平台（{e}）") from e

        if resp.status_code == 401:
            raise AievoApiError(401, "认证失败：API Key 无效、已吊销或已过期")
        if resp.status_code == 403:
            detail = self._detail(resp)
            raise AievoApiError(
                403, f"无权限（{detail or 'API Key 缺少所需 scope'}）"
            )
        if resp.status_code == 404:
            raise AievoApiError(404, f"资源不存在：{path}")
        if resp.status_code >= 400:
            raise AievoApiError(resp.status_code, self._detail(resp) or f"HTTP {resp.status_code}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            detail = body.get("detail")
            return detail if isinstance(detail, str) else str(detail)
        except (ValueError, AttributeError):
            return ""

    # ---------------- 任务运行与门禁 ----------------

    def run_task(self, task_id: str) -> Dict[str, Any]:
        """触发评测运行，返回任务对象（含 execution_number/execution_count）"""
        return self._request("POST", f"/api/v1/evaluation/tasks/{task_id}/run")

    def get_gate(
        self, task_id: str, execution_number: Optional[int] = None
    ) -> Dict[str, Any]:
        """查询门禁状态：{task_id, status, execution_number, gate_enabled, gate_mode, verdict, gate_result}"""
        params = {"execution_number": execution_number} if execution_number else None
        return self._request(
            "GET", f"/api/v1/evaluation/tasks/{task_id}/gate", params=params
        )

    # ---------------- 结果读取 ----------------

    def get_result(self, task_id: str) -> Dict[str, Any]:
        """最新一次执行的结果（EvaluationResultResponse）"""
        return self._request("GET", f"/api/v1/evaluation/results/{task_id}")

    def get_result_version(self, task_id: str, execution_number: int) -> Dict[str, Any]:
        """指定执行版本的结果（历史接口）"""
        return self._request(
            "GET",
            f"/api/v1/evaluation/results/{task_id}/history/{execution_number}",
        )

    # ---------------- 数据集 ----------------

    def create_dataset(
        self, name: str, description: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/evaluation/datasets",
            json_body={"name": name, "description": description},
        )

    def import_dataset_excel(self, dataset_id: str, file_path: Path) -> int:
        """上传 Excel 导入，返回导入条数"""
        with open(file_path, "rb") as f:
            body = self._request(
                "POST",
                f"/api/v1/evaluation/datasets/{dataset_id}/import/excel",
                files={"file": (file_path.name, f)},
            )
        return int(body.get("count", 0))

    def import_dataset_json(
        self, dataset_id: str, data: List[Dict[str, Any]]
    ) -> int:
        return int(
            self._request(
                "POST",
                f"/api/v1/evaluation/datasets/{dataset_id}/import/json",
                json_body={"data": data},
            ).get("count", 0)
        )
