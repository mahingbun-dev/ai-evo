"""基础模板：读 stdin 契约 → 遍历 inputs 文件数组 → 契约输出

演示要点：
- 包根 main.py 即入口（平台以 python main.py 直接执行，包根即 sys.path[0]）
- lib/ 同目录依赖 import（from lib import helpers）
- inputs 两种模式：hub_pkg 为 root/files 文件对象数组模式，
  hub_pkg_all 为 root/index_path 索引模式
- 输出契约：stdout 最后一行 JSON（status/outputs/message，兼容回写 output/summary）
"""
import json
import sys

from lib import helpers


def main() -> int:
    payload = json.load(sys.stdin)

    inputs = payload.get("inputs") or {}
    hub_pkg = inputs.get("hub_pkg") or {}
    files = hub_pkg.get("files") or []
    listing = [helpers.file_brief(item) for item in files]

    # root/index_path 模式演示：读索引文件自行遍历（索引含包内全部文件清单）
    index_rows = helpers.read_index((inputs.get("hub_pkg_all") or {}).get("index_path"))

    # fields 通道：inline/local 字段值为平台落盘后的文件绝对路径
    note_path = (payload.get("fields") or {}).get("note")
    note_text = helpers.read_text_safe(note_path) if note_path else ""

    outputs = {
        "inputs_root": hub_pkg.get("root") or "",
        # 路径三原则③：输出引用文件一律 rel_path（相对 inputs.root）
        "file_count": len(files),
        "files": [{"rel_path": item["rel_path"], "size": item["size"], "type": item["type"]}
                  for item in listing],
        "index_file_count": len(index_rows),
        "note_preview": note_text[:120],
        "params": payload.get("params") or {},
        "upstream_keys": sorted((payload.get("upstream") or {}).keys()),
    }
    message = (
        f"遍历 inputs 完成：{len(files)} 个文件（root/files 模式），"
        f"索引 {len(index_rows)} 条（root/index_path 模式）"
    )
    print(json.dumps({
        "status": "ok",
        "outputs": outputs,
        "message": message,
        # 平台旧链路兼容字段：裁判与结果页读 output/summary
        "output": outputs,
        "summary": f"files={len(files)}, index_rows={len(index_rows)}",
        "files": {},
        "finish_reason": "completed",
        "error": None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
