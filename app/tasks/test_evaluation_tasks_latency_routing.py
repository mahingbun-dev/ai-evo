"""
测试 latency 评测类型在 _execute_evaluation() 函数中的路由分支
"""
import pytest


class TestLatencyRouting:
    """验证 latency 评测类型的路由分支"""

    def test_latency_routing_in_execute_evaluation(self):
        """验证 _execute_evaluation() 函数中对 latency 类型的处理分支存在"""
        import app.tasks.evaluation_tasks_async as module

        # 读取源代码检查 latency 路由分支是否存在
        import inspect
        source = inspect.getsource(module._execute_evaluation)

        # 验证包含 latency 类型的判断分支
        assert 'if task.evaluation_type == "latency":' in source, \
            "_execute_evaluation() 函数中缺少 latency 类型的路由分支"

    def test_latency_routing_calls_executor(self):
        """验证 evaluation_type='latency' 时会调用 execute_batch_latency"""
        import app.tasks.evaluation_tasks_async as module

        # 读取源代码检查是否调用 execute_batch_latency
        import inspect
        source = inspect.getsource(module._execute_evaluation)

        # 验证在 latency 分支中调用了 execute_batch_latency
        assert "execute_batch_latency" in source, \
            "_execute_evaluation() 函数中 latency 分支缺少对 execute_batch_latency 的调用"

    def test_latency_routing_uses_evaluationexecutor(self):
        """验证 latency 路由使用 EvaluationExecutor"""
        import app.tasks.evaluation_tasks_async as module

        # 读取源代码检查
        import inspect
        source = inspect.getsource(module._execute_evaluation)

        # 在 latency 分支附近应该创建 EvaluationExecutor 实例
        # 提取 latency 分支附近的代码
        lines = source.split('\n')
        latency_lines = []
        in_latency_block = False
        indent_level = 0

        for line in lines:
            if 'if task.evaluation_type == "latency":' in line:
                in_latency_block = True
                indent_level = len(line) - len(line.lstrip())
            elif in_latency_block:
                current_indent = len(line) - len(line.lstrip())
                # 如果缩进回到同级或更少，退出 block
                if line.strip() and current_indent <= indent_level:
                    break
                latency_lines.append(line)

        latency_code = '\n'.join(latency_lines)

        # 验证 latency 分支创建了 EvaluationExecutor
        assert "EvaluationExecutor" in latency_code, \
            "latency 分支应该创建 EvaluationExecutor 实例"
        assert "execute_batch_latency" in latency_code, \
            "latency 分支应该调用 execute_batch_latency 方法"
