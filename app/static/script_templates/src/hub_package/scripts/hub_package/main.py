"""hub 执行包分析模板：按真实目录层级消费 anc-hub 执行包

【模板定位】本包是 ai-evo 脚手架的只读参考模板：新脚本请把本包结构复制到
scripts/{script_key}/（与本包平级）后再改，不要直接修改本包；工作流与硬性规则
见工作区根目录 AGENTS.md，完整契约见同目录 README.md。

演示要点：
- 泛化消费 stdin.inputs：遍历全部输入字段，不写死字段名（键名 = model.py 声明 +
  编排映射的脚本字段名），每个值都是 {root, files}/{root, index_path} 之一
- 执行包解包后 inputs.root 下第一层即 {execution_id}-attempt-1/（zip 原始最外层，
  平台原样保留），所有 rel_path 都带该前缀——按顶层目录分组统计，而不是硬编码
  attempt 目录名
- root/index_path 模式（未配 path_pattern）：从索引还原文件清单。索引条目
  {path: 相对 root 的 posix 路径, type, size, is_nested_zip}，path 与 files 数组的
  rel_path 同值——Path(root)/path 即文件位置
- 按 rel_path 后缀定位 execution-manifest.json 并提取执行摘要（不依赖层级写死路径）
- lib/ 同目录依赖 import（from lib import helpers）

注意：真实执行包体量大（数百文件、数十 MB，含 reports/segments 等目录），本模板
inputs/ 样例仅为形状示意；脚本遍历永远以 stdin 传入的文件清单/索引为准，不要对着
样例猜文件清单。目录层级详见 README.md「运行时工作区目录结构」。
"""
import json
import sys
from pathlib import Path

from lib import helpers


def collect_files(spec: dict) -> tuple:
    """把单个输入字段的规格统一还原为文件对象数组，返回 (mode, files)

    - root/files 模式（配了 path_pattern）：直接用 files 数组
      （{path: 绝对路径, rel_path: 相对 root, size, type}）
    - root/index_path 模式（未配）：从索引还原（索引条目 {path: 相对 root 的
      posix 路径, type, size, is_nested_zip}，path 与 files 的 rel_path 同值）
    """
    files = spec.get("files") or []
    if files:
        return "files", files
    pkg_root = spec.get("root") or ""
    files = [
        {"path": str(Path(pkg_root) / row.get("path", "")),
         "rel_path": row.get("path", ""),
         "size": row.get("size", 0), "type": row.get("type", "")}
        for row in helpers.read_index(spec.get("index_path"))
    ]
    return "index", files


def main() -> int:
    """入口：读 stdin JSON → 遍历 inputs 消费全部执行包字段 → 打印契约 JSON

    新脚本照此骨架改造：换掉消费逻辑与 outputs 内容即可，stdin 解析、
    泛化遍历、防御跳过、输出结构这些「与平台对接的部分」保持原样最稳。
    """
    payload = json.load(sys.stdin)

    inputs = payload.get("inputs") or {}
    fields_report = {}
    manifest_summary = {}
    seen_manifests = set()
    sample_rel_paths = []

    # 泛化消费：遍历全部输入字段，不依赖具体字段名（每个值都是执行包规格）
    for field in sorted(inputs):
        spec = inputs[field] or {}
        if not isinstance(spec, dict) or "root" not in spec:
            continue  # 防御：非执行包形态的条目跳过（未来新通道兼容）
        mode, files = collect_files(spec)
        grouped = helpers.group_by_attempt(files)
        fields_report[field] = {
            "mode": mode,
            "attempt_tops": sorted(grouped.keys()),
            "file_count": len(files),
        }
        if not sample_rel_paths and files:
            sample_rel_paths = [item.get("rel_path") for item in files[:5]]
        if not manifest_summary:
            for item in helpers.find_by_suffix(files, "execution-manifest.json"):
                rel = item.get("rel_path") or ""
                if rel in seen_manifests:
                    continue
                seen_manifests.add(rel)
                manifest_summary = helpers.read_manifest_summary(files)
                break

    outputs = {
        "input_fields": sorted(fields_report.keys()),
        "fields": fields_report,
        "manifest_summary": manifest_summary,
        # 路径三原则③：输出引用文件一律 rel_path（相对 inputs.root，含 attempt 前缀）
        "sample_rel_paths": sample_rel_paths,
    }
    total = sum(v["file_count"] for v in fields_report.values())
    message = f"消费 {len(fields_report)} 个输入字段共 {total} 个文件"
    if manifest_summary:
        message += f"；manifest 摘要 case_total={manifest_summary.get('case_total', 'N/A')}"

    # 文件产物统一落盘 {_run_dir}/outputs/（路径三原则②；平台保证目录存在，
    # ensure_outputs_dir 防御式兜底），落盘后在 files 键注册绝对路径——
    # 裁判与下游靠 files 发现产物；本模板把摘要存为 manifest_summary.json 示范
    summary_file = helpers.ensure_outputs_dir(payload.get("_run_dir") or ".") / "manifest_summary.json"
    summary_file.write_text(json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf-8")
    produced_files = {"manifest_summary.json": str(summary_file)}

    print(json.dumps({
        "status": "ok",
        "outputs": outputs,
        "message": message,
        # 平台旧链路兼容字段：裁判与结果页读 output/summary
        "output": outputs,
        "summary": f"fields={len(fields_report)}, files={total}",
        "files": produced_files,
        "finish_reason": "completed",
        "error": None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
