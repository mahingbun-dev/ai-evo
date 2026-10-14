"""W12-4 `_interpolate` 凭据替换顺序单元测试（整改计划 §3 W12 第 4 项）

漏洞链（修复前）：`_interpolate` 先注入 [[var]] 外部内容、后扫描 {{var}}
内部变量——数据集行内容中的 `{{api_key}}` 字样会被随后的凭据扫描替换成
真实密钥，随请求体带出。

修复后语义：
1. 先替换 {{var}}（内部变量，含 api_key/cookie 等凭据性 builtin），此刻
   模板中尚无外部内容；
2. 后注入 [[var]]（外部内容），且注入值中的 `{{` 转义为全角 `｛｛`，
   阻断任何后续模板处理的二次展开。

用例核心（按整改计划）：构造「数据集行含 {{api_key}}」场景，断言密钥
不被带出。
"""
import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services.request_chain import RequestChainExecutor
from app.services.variable_context import VariableContext


SECRET = "sk-test-SECRET-12345"


def _executor_with_vars() -> RequestChainExecutor:
    """最小执行器：api_key 为凭据性 builtin，row_content 模拟数据集行内容"""
    executor = RequestChainExecutor({"enabled": True, "requests": []})
    executor.variables = VariableContext()
    executor.variables.set_builtin_var("api_key", SECRET)
    executor.variables.set_builtin_var("cookie", "session=abc")
    # 数据集行内容：故意携带 {{api_key}} 注入字样
    executor.variables.set_runtime_var(
        "row_content", "问题正文，包含注入样例 {{api_key}} 请原样保留"
    )
    executor.variables.set_runtime_var("normal_var", "普通值")
    return executor


class TestInterpolateCredentialOrder:
    def test_dataset_row_with_api_key_placeholder_does_not_leak(self):
        """核心用例：数据集行（[[row_content]]）含 {{api_key}} → 密钥不被带出"""
        executor = _executor_with_vars()
        template = {"q": "[[row_content]]"}
        result = executor._interpolate(template["q"])

        assert SECRET not in result, "数据集内容中的 {{api_key}} 字样不得替换为真实密钥"
        # {{ 被转义为全角 ｛｛（阻断后续模板处理二次展开；闭合 }} 保持半角，无害）
        assert "{{" not in result
        assert "｛｛api_key}}" in result
        assert "问题正文" in result  # 外部内容本身正常注入

    def test_credential_replacement_still_works_for_template(self):
        """模板自身显式引用 {{api_key}} → 凭据替换正常（合法用途不受影响）"""
        executor = _executor_with_vars()
        result = executor._interpolate("Authorization: Bearer {{api_key}}")
        assert result == f"Authorization: Bearer {SECRET}"

    def test_normal_runtime_variable_replacement_unchanged(self):
        """普通运行时变量 {{normal_var}} 替换不受顺序调整影响"""
        executor = _executor_with_vars()
        result = executor._interpolate("输入：{{normal_var}}")
        assert result == "输入：普通值"

    def test_mixed_template_credential_then_external_content(self):
        """混合模板：凭据在模板占位处正常展开，外部内容中的注入字样被转义"""
        executor = _executor_with_vars()
        template = "Authorization: {{cookie}}\n数据：[[row_content]]"
        result = executor._interpolate(template)
        assert "session=abc" in result          # 模板占位凭据正常展开
        assert SECRET not in result             # 外部内容不触发凭据扫描
        assert "｛｛api_key}}" in result

    def test_external_missing_variable_still_empty(self):
        """[[var]] 未定义 → 置空（既有行为保留）"""
        executor = _executor_with_vars()
        assert executor._interpolate("前[[no_such_var]]后") == "前后"

    def test_no_placeholders_passthrough(self):
        """无占位符模板原样返回"""
        executor = _executor_with_vars()
        assert executor._interpolate("纯文本模板") == "纯文本模板"
