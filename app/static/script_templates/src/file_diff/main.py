"""多文件对比模板：对 inputs 执行包内两份文本文件逐行差异对比

演示要点：
- params 指定对比对象（rel_path_a / rel_path_b，相对 inputs.root）
- lib/ 同目录依赖 import（from lib import diff_utils）
- 找不到任一对比文件时输出 status=skip（契约允许，冒烟/真实运行都不崩）
"""
import json
import sys

from lib import diff_utils


def main() -> int:
    payload = json.load(sys.stdin)

    params = payload.get("params") or {}
    rel_a = str(params.get("rel_path_a") or "a.txt")
    rel_b = str(params.get("rel_path_b") or "b.txt")

    hub_pkg = (payload.get("inputs") or {}).get("hub_pkg") or {}
    root = str(hub_pkg.get("root") or "")
    files = {str(item.get("rel_path") or ""): item for item in (hub_pkg.get("files") or [])}

    missing = [rel for rel in (rel_a, rel_b) if rel not in files]
    if missing or not root:
        print(json.dumps({
            "status": "skip",
            "outputs": {"rel_path_a": rel_a, "rel_path_b": rel_b, "missing": missing},
            "message": f"inputs 执行包内未找到对比文件: {', '.join(missing) or rel_a + ' / ' + rel_b}，跳过对比",
            "summary": "diff skipped",
            "finish_reason": "completed",
            "error": None,
        }, ensure_ascii=False))
        return 0

    text_a = diff_utils.read_text(files[rel_a].get("path"))
    text_b = diff_utils.read_text(files[rel_b].get("path"))
    report = diff_utils.line_diff_report(text_a, text_b)

    outputs = {
        "rel_path_a": rel_a,   # 路径三原则③：输出引用文件一律 rel_path
        "rel_path_b": rel_b,
        "identical": report["identical"],
        "diff_line_count": report["diff_line_count"],
        "diff_preview": report["diff_preview"],
        "line_counts": {"a": report["line_count_a"], "b": report["line_count_b"]},
    }
    if report["identical"]:
        message = f"{rel_a} 与 {rel_b} 内容完全一致（{report['line_count_a']} 行）"
    else:
        message = (f"{rel_a} 与 {rel_b} 存在差异：差异行 {report['diff_line_count']} 行"
                   f"（a {report['line_count_a']} 行 / b {report['line_count_b']} 行）")
    print(json.dumps({
        "status": "ok",
        "outputs": outputs,
        "message": message,
        "output": outputs,  # 平台旧链路兼容字段
        "summary": f"identical={report['identical']}, diff_lines={report['diff_line_count']}",
        "files": {},
        "finish_reason": "completed",
        "error": None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
