"""
RequestChainExecutor.extract_variables 单元测试

覆盖动态变量提取的几类场景，重点回归：
- 以 [[var]] 显式声明的外部变量，即使与请求链 output_vars 重名，也必须保留
"""
import pytest
from app.services.request_chain import RequestChainExecutor


def _chain(requests):
    """构造最小请求链配置"""
    return {"enabled": True, "requests": requests}


def _req(body=None, output_vars=None, output_var=None, alias="main", name="main", **extra):
    req = {"alias": alias, "name": name, "order": 1, "url": "https://api.example.com/x"}
    if body is not None:
        req["body"] = body
    if output_vars is not None:
        req["output_vars"] = [{"name": v} for v in output_vars]
    if output_var is not None:
        req["output_var"] = output_var
    req.update(extra)
    return req


class TestExtractVariables:
    def test_empty_chain_returns_empty(self):
        assert RequestChainExecutor.extract_variables(None) == []
        assert RequestChainExecutor.extract_variables({}) == []

    def test_basic_external_variables_included(self):
        """普通外部变量，不与输出变量冲突时全部保留"""
        chain = _chain([_req(body={"a": "[[img]]", "b": "[[label]]"})])
        result = RequestChainExecutor.extract_variables(chain)
        assert set(result) == {"img", "label"}

    def test_external_variable_colliding_with_output_var_is_kept(self):
        """核心回归：[[image_url]] 同时是输出变量名时，也必须保留（bug 修复）"""
        chain = _chain([
            _req(
                body={"image_url": "[[image_url]]", "image_type": "[[image_type]]", "barcode": "[[barcode]]"},
                output_vars=["code", "image_url", "is_valid", "reason", "barcode"],
            )
        ])
        result = RequestChainExecutor.extract_variables(chain)
        assert set(result) == {"image_url", "image_type", "barcode"}

    def test_output_vars_not_referenced_are_excluded(self):
        """未被引用的输出变量不应出现在结果里"""
        chain = _chain([_req(body={"a": "[[input]]"}, output_vars=["computed_a", "computed_b"])])
        result = RequestChainExecutor.extract_variables(chain)
        assert set(result) == {"input"}

    def test_single_output_var_excluded_when_unreferenced(self):
        chain = _chain([_req(body={"a": "[[seed]]"}, output_var="generated")])
        result = RequestChainExecutor.extract_variables(chain)
        assert set(result) == {"seed"}

    def test_sse_data_field_excluded_when_unreferenced(self):
        chain = _chain([_req(
            body={"a": "[[prompt]]"},
            response_parse={"data_field": "response_data"},
        )])
        result = RequestChainExecutor.extract_variables(chain)
        assert set(result) == {"prompt"}

    def test_chain_reference_undefined_alias_included(self):
        """{{alias.var}} 引用到未定义别名时，作为需输入变量保留"""
        chain = _chain([_req(body={"a": "{{other.var1}}"})])
        result = RequestChainExecutor.extract_variables(chain)
        assert "other.var1" in result

    def test_login_variable_excluded(self):
        """通过 auth_config 登录获取的 accessToken 不要求用户输入"""
        chain = _chain([_req(body={"a": "[[accessToken]]", "b": "[[real_input]]"})])
        result = RequestChainExecutor.extract_variables(chain, {"login_url": "https://api.example.com/login"})
        assert set(result) == {"real_input"}
