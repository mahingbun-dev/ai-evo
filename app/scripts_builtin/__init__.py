"""内置前处理脚本包（builtin registry）

脚本均为纯进程内确定性计算，标准契约见 docs/design/pre-judge-scripts.md 第三节：
- 入参：标准输入 dict（fields/params/upstream/...）
- 返回：标准输出 dict {status, output, files, summary, finish_reason, error}
"""
