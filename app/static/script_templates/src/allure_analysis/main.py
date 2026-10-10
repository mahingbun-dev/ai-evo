"""Allure 结果分析模板：解析 inputs 执行包内 allure-results 的 *-result.json

演示要点：
- 遍历 inputs.hub_pkg（root/files 模式）筛出 *-result.json，逐个解析统计
- lib/ 同目录依赖 import（from lib import allure_stats）
- 无 allure 文件时输出 status=skip（契约允许，冒烟/真实运行都不崩）
"""
import json
import sys

from lib import allure_stats


def main() -> int:
    payload = json.load(sys.stdin)

    hub_pkg = (payload.get("inputs") or {}).get("hub_pkg") or {}
    files = hub_pkg.get("files") or []
    result_files = [item for item in files if str(item.get("rel_path") or "").endswith("-result.json")]

    if not result_files:
        print(json.dumps({
            "status": "skip",
            "outputs": {"result_file_count": 0, "status_counts": {}, "failed": []},
            "message": "inputs 执行包内未发现 *-result.json（allure-results），跳过统计",
            "summary": "no allure results",
            "finish_reason": "completed",
            "error": None,
        }, ensure_ascii=False))
        return 0

    rows = []
    for item in result_files:
        row = allure_stats.parse_result(item.get("path"))
        if row is not None:
            rows.append(row)

    status_counts = allure_stats.count_by_status(rows)
    failed = allure_stats.failed_brief(rows)
    outputs = {
        "result_file_count": len(result_files),
        "parsed_count": len(rows),
        "status_counts": status_counts,
        "passed_rate": allure_stats.passed_rate(rows),
        "failed": failed,
        "broken": allure_stats.broken_brief(rows),
    }
    message = (
        f"解析 {len(rows)} 条 allure 结果："
        + "、".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
        + f"；失败/损坏 {len(failed) + len(outputs['broken'])} 条"
    )
    print(json.dumps({
        "status": "ok",
        "outputs": outputs,
        "message": message,
        "output": outputs,  # 平台旧链路兼容字段
        "summary": f"allure results={len(rows)}, passed_rate={outputs['passed_rate']}",
        "files": {},
        "finish_reason": "completed",
        "error": None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
