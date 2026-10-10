"""构建预判脚本脚手架模板 zip（阶段E）

把 app/static/script_templates/src/{name}/ 下的模板源码打包为
app/static/script_templates/{name}.zip（包根含 main.py / README.md / lib/），
并在构建时对产物跑一遍上传校验链（compile/路径扫描/风险声明/依赖校验/契约冒烟），
保证模板自身可通过平台校验。

用法:
    python scripts/build_script_templates.py          # 构建全部并自检
    python scripts/build_script_templates.py basic    # 只构建指定模板
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

TEMPLATES_SRC_DIR = PROJECT_ROOT / "app" / "static" / "script_templates" / "src"
TEMPLATES_OUT_DIR = PROJECT_ROOT / "app" / "static" / "script_templates"

# 固定 zip 时间戳，保证产物可重复构建
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def build_template_zip(name: str) -> Path:
    """把 src/{name}/ 打包为 {name}.zip（源码目录下文件按相对路径写入）"""
    src_dir = TEMPLATES_SRC_DIR / name
    if not src_dir.is_dir():
        raise SystemExit(f"模板源码目录不存在: {src_dir}")
    out_path = TEMPLATES_OUT_DIR / f"{name}.zip"
    TEMPLATES_OUT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            arcname = path.relative_to(src_dir).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    print(f"[build] {out_path.relative_to(PROJECT_ROOT)}（{out_path.stat().st_size} 字节）")
    return out_path


def verify_template_zip(zip_path: Path) -> None:
    """解包产物到临时目录并跑上传校验链（与平台上传通道同一条链）"""
    from app.services.hub_file_service import unpack_zip_safely
    from app.services.pre_judge_script_service import (
        _collect_package_sources,
        _locate_package_root,
        validate_script_chain,
    )

    with tempfile.TemporaryDirectory(prefix="tpl_verify_") as tmp:
        unpack_zip_safely(str(zip_path), tmp)
        package_root = _locate_package_root(Path(tmp))
        sources = _collect_package_sources(package_root)
        entry = package_root / "main.py"
        check = validate_script_chain(sources, entry)
        risk_hits = check["static_check"]
        if risk_hits:
            raise SystemExit(f"模板 {zip_path.name} 风险扫描意外命中（模板必须零风险）: {risk_hits}")
        smoke = check["smoke_output"]
        if smoke.get("status") not in ("ok", "skip"):
            raise SystemExit(f"模板 {zip_path.name} 冒烟输出 status 异常: {smoke}")
    print(f"[verify] {zip_path.name} 校验链通过（冒烟 status={smoke.get('status')}）")


def main(argv: list) -> int:
    names = argv[1:] or ["basic", "allure_analysis", "file_diff"]
    for name in names:
        zip_path = build_template_zip(name)
        verify_template_zip(zip_path)
    print("全部模板构建并自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
