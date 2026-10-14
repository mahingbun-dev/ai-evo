"""评测前处理脚本 API

- GET    /pre-judge-scripts              列表（script 类型附 file_count/has_requirements）
- POST   /pre-judge-scripts              创建
- PUT    /pre-judge-scripts/{id}         更新
- DELETE /pre-judge-scripts/{id}         删除（物理删除；被评测任务引用时 400 提示先解除关联）
- POST   /pre-judge-scripts/{id}/upload  上传脚本文件（exec_type=script，multipart .py）
- POST   /pre-judge-scripts/{id}/upload-package  上传 zip 脚本包（multipart .zip ≤20MB，阶段E）
- POST   /pre-judge-scripts/{id}/debug   给输入样例试跑一次，落盘产物返回路径
- GET    /pre-judge-scripts/templates    脚手架模板 zip 列表（阶段E；须声明在 /{script_id} 之前）
- GET    /pre-judge-scripts/templates/{name}/download  脚手架模板 zip 下载（FileResponse）
- GET    /pre-judge-scripts/{id}/export  导出脚本契约 JSON
- POST   /pre-judge-scripts/import-package  导入脚本包（zip / 多文件列表，multipart；强制 main.py + model.py 契约校验）
- GET    /pre-judge-scripts/{id}/export-package  导出脚本包 zip（目录文件 + manifest.json）
- GET    /pre-judge-scripts/{id}/files   文件树（脚本目录内白名单文本文件清单）
- GET    /pre-judge-scripts/{id}/file    读取文本文件内容
- PUT    /pre-judge-scripts/{id}/file    在线编辑保存（乐观锁 409）
- POST   /pre-judge-scripts/{id}/file    新建文本文件（已存在 409）
- DELETE /pre-judge-scripts/{id}/file    删除文件（main.py 不可删）
- POST   /pre-judge-scripts/{id}/file/rename  重命名/移动文件（main.py 不可改名）
- POST   /pre-judge-scripts/from-template  从脚手架模板生成脚本（single/folder）

权限（W1-4 整改）：写操作校验请求用户的超管标志（user.super_admin_flag == 1，
与前端 Scripts.vue 的 isSuperAdmin 同口径），非超管一律 403（deny by default）；
查看、导出对所有登录用户开放，试跑（debug）按写操作口径纳入超管校验。
服务层另有内置脚本（pjs_builtin_*）不可变守卫：内置脚本记录禁编辑/删除/覆盖。

上传阻断校验链（阶段E）：单 .py（upload）与 zip 包（upload-package）上传
通道共用（语法编译 / 路径规范扫描 / 风险扫描命中必须 risk_acknowledged /
依赖声明逐包校验 / 契约冒烟），不通过返回 400 与中文错误；风险声明与依赖
声明以 multipart 表单字段随包提交。import-package 为契约交换通道，不走
冒烟阻断（保持提示级语义）。
"""
import logging
import os
import re
import time
import uuid
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import pre_judge_runner
from app.services.pre_judge_script_service import (
    PreJudgeFileError,
    PreJudgeScriptService,
    get_script_template_zip,
    list_script_templates,
    normalize_package,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pre-judge-scripts", tags=["评测前处理脚本"])


class PreJudgeScriptPayload(BaseModel):
    """脚本创建/更新请求"""
    name: str = Field(..., min_length=1, description="脚本名称（全局唯一）")
    description: Optional[str] = Field(default=None)
    exec_type: str = Field(default="script", description="script(自定义脚本)/claude_agent/deepseek_agent（builtin 已废除）")
    entrypoint: Optional[str] = Field(default=None, description="script: 脚本目录内相对路径（默认 main.py）；agent: 指令文本")
    allowed_tools: Optional[List[str]] = Field(default=None)
    harness_extra: Optional[Dict[str, Any]] = Field(default=None)
    input_fields: Optional[Dict[str, Any]] = Field(default=None)
    output_schema: Optional[Dict[str, Any]] = Field(default=None)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    is_active: bool = Field(default=True)


class PreJudgeScriptDebugPayload(BaseModel):
    """脚本试跑请求：输入样例（字段值可为本地路径或 inline 内容）+ 脚本参数"""
    fields: Dict[str, Any] = Field(default_factory=dict, description="输入样例：值为本地路径或 inline 内容")
    params: Dict[str, Any] = Field(default_factory=dict)


class PreJudgeScriptFileSavePayload(BaseModel):
    """在线编辑保存请求（乐观锁：updated_at 与记录不一致 → 409）"""
    path: str = Field(..., min_length=1, description="脚本目录内相对路径")
    content: str = Field(default="", description="文件全文内容")
    updated_at: Optional[str] = Field(default=None, description="编辑基准的记录 updated_at（isoformat）")


class PreJudgeScriptFileCreatePayload(BaseModel):
    """新建文件请求"""
    path: str = Field(..., min_length=1, description="脚本目录内相对路径（文本白名单内）")
    content: str = Field(default="", description="初始内容（默认空）")


class PreJudgeScriptFileRenamePayload(BaseModel):
    """重命名/移动文件请求"""
    path: str = Field(..., min_length=1, description="原相对路径")
    new_path: str = Field(..., min_length=1, description="新相对路径（文本白名单内）")


class PreJudgeScriptTemplatePayload(BaseModel):
    """脚手架生成请求"""
    template: str = Field(..., description="模板类型：single（单文件）/ folder（多文件工程）")
    name: str = Field(..., min_length=1, description="脚本名称（全局唯一）")
    description: Optional[str] = Field(default=None)


def _ensure_admin(request: Request) -> None:
    """写操作管理员校验（W1-4：真实校验超管标志）

    校验 user.super_admin_flag == 1（字段由 auth_middleware 注入，前端
    Scripts.vue 的 isSuperAdmin 同口径）；用户缺失（未登录/中间件未注入）
    或非超管一律 403，deny by default。
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可修改评测前处理脚本")


def _service(db: Session) -> PreJudgeScriptService:
    return PreJudgeScriptService(db)


@router.get("", summary="脚本列表（含内置）")
async def list_scripts(
    include_inactive: bool = Query(False, description="是否包含已停用脚本"),
    db: Session = Depends(get_db),
):
    # include_builtin 已废弃（内置脚本统一落库，列表仅 DB 记录），保留参数兼容旧客户端
    items = _service(db).list_scripts(include_inactive=include_inactive)
    return {"total": len(items), "items": items}


# ---- 脚手架模板 zip（阶段E）：路由须声明在 /{script_id} 之前，避免 "templates" 被当作 script_id ----

@router.get("/templates", summary="脚手架模板列表")
async def list_templates():
    """列出 app/static/script_templates/ 下已发布的脚手架模板 zip（含说明与下载路径）"""
    items = list_script_templates()
    return {"total": len(items), "items": items}


@router.get("/templates/{name}/download", summary="下载脚手架模板 zip")
async def download_template(name: str):
    """按模板名下载 zip（FileResponse 附件；模板名仅允许注册名，防路径穿越）"""
    try:
        zip_path = get_script_template_zip(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(
        path=str(zip_path),
        filename=zip_path.name,
        media_type="application/zip",
    )


@router.get("/{script_id}", summary="脚本详情")
async def get_script(script_id: str, db: Session = Depends(get_db)):
    script = _service(db).get_script(script_id)
    if script is None or not script.get("is_active", True):
        raise HTTPException(status_code=404, detail=f"脚本不存在: {script_id}")
    return script


@router.post("", summary="创建脚本")
async def create_script(payload: PreJudgeScriptPayload, request: Request, db: Session = Depends(get_db)):
    _ensure_admin(request)
    user = getattr(request.state, "user", None) or {}
    created_by = user.get("user_code") if isinstance(user, dict) else None
    try:
        script = _service(db).create_script(payload.model_dump(), created_by=created_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return script


@router.put("/{script_id}", summary="更新脚本")
async def update_script(script_id: str, payload: PreJudgeScriptPayload, request: Request, db: Session = Depends(get_db)):
    _ensure_admin(request)
    try:
        script = _service(db).update_script(script_id, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return script


@router.delete("/{script_id}", summary="删除脚本（物理删除）")
async def delete_script(script_id: str, request: Request, db: Session = Depends(get_db)):
    """物理删除 DB 记录与落盘目录；停用（可再启用）请用更新接口置 is_active=False。

    被评测任务引用（任务「待评测数据」编排了 script_output 目标行）时拒绝删除，
    返回 400 并列出引用任务名，引导先在任务中解除关联。
    """
    _ensure_admin(request)
    try:
        deleted = _service(db).delete_script(script_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"脚本不存在: {script_id}")
    return {"deleted": True, "script_id": script_id}


@router.post("/{script_id}/upload", summary="上传脚本文件（exec_type=script）")
async def upload_script_file(
    script_id: str,
    request: Request,
    file: UploadFile = File(..., description="Python 脚本文件（.py）"),
    risk_acknowledged: Optional[str] = Form(default=None, description="风险声明文本（风险扫描命中时必填）"),
    declared_requirements: Optional[str] = Form(
        default=None, description='依赖声明（JSON 数组字符串，如 ["requests>=2.31"]）'),
    db: Session = Depends(get_db),
):
    """multipart 上传 .py 脚本：服务端存为 uploads/pre_judge_scripts/{id}/main.py，
    entrypoint 回写为目录内相对路径（重复上传为覆盖语义）。

    阶段E起走上传阻断校验链（语法编译/路径规范扫描/风险声明/依赖校验/契约冒烟），
    不通过返回 400 与中文错误；已声明的风险扫描提示随响应返回并记录日志。
    """
    _ensure_admin(request)
    filename = file.filename or ""
    if not filename.lower().endswith(".py"):
        raise HTTPException(status_code=400, detail="仅支持 .py 脚本文件")
    content = await file.read()
    try:
        saved = _service(db).save_script_file(
            script_id, content,
            risk_acknowledged=risk_acknowledged,
            declared_requirements=declared_requirements,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return saved


@router.post("/{script_id}/upload-package", summary="上传 zip 脚本包（exec_type=script）")
async def upload_script_package(
    script_id: str,
    request: Request,
    file: UploadFile = File(..., description="zip 脚本包（包根必须含 main.py，≤20MB）"),
    risk_acknowledged: Optional[str] = Form(default=None, description="风险声明文本（风险扫描命中时必填）"),
    declared_requirements: Optional[str] = Form(
        default=None, description='依赖声明（JSON 数组字符串，如 ["requests>=2.31"]）'),
    db: Session = Depends(get_db),
):
    """multipart 上传 zip 脚本包：安全解包到 uploads/pre_judge_scripts/{id}/
    （目录式布局），entrypoint 回写为目录内 main.py 相对路径（重复上传为覆盖语义）。

    安全解包拒绝绝对路径/盘符/.. 穿越成员；包根必须有 main.py（zip 只含唯一
    一级子目录且其内有 main.py 时该子目录视为包根），否则中文报错拒绝。
    包内全部 .py 走上传阻断校验链（语法编译/路径规范扫描/风险声明/依赖校验/
    契约冒烟——冒烟经 wrapper 实跑包根 main.py，与生产执行同路径），不通过
    返回 400 与中文错误，正式目录不受影响。
    """
    _ensure_admin(request)
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="仅支持 .zip 脚本包")
    content = await file.read()
    try:
        saved = _service(db).save_script_package(
            script_id, content,
            risk_acknowledged=risk_acknowledged,
            declared_requirements=declared_requirements,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return saved


@router.post("/{script_id}/debug", summary="脚本试跑（落盘产物返回路径）")
async def debug_script(script_id: str, payload: PreJudgeScriptDebugPayload,
                       request: Request, db: Session = Depends(get_db)):
    """给输入样例试跑一次：字段值为本地路径直用，否则按 inline 内容写临时文件。

    产物写入 uploads/script_runs/debug/{script}_{ts}/，返回标准输出与产物绝对路径；
    script 类型附带运行前静态检查提示（static_check，提示级不拦截）。
    W1-4：试跑与写操作同口径，仅超管可用；W1-3：试跑与生产执行同一
    环境变量白名单（经 pre_judge_runner.execute_script）。
    """
    _ensure_admin(request)
    service = _service(db)
    script = service.get_script(script_id)
    if script is None or not script.get("is_active", True):
        raise HTTPException(status_code=404, detail=f"脚本不存在: {script_id}")

    stamp = time.strftime("%Y%m%d%H%M%S")
    run_dir = pre_judge_runner.build_run_dir("debug", f"{_safe(script['name'])}_{stamp}_{uuid.uuid4().hex[:6]}")

    fields: Dict[str, str] = {}
    try:
        for field_name, value in payload.fields.items():
            if isinstance(value, str) and os.path.exists(value):
                fields[field_name] = value  # 本地路径/目录直用
            else:
                fields[field_name] = pre_judge_runner.write_inline_value(field_name, value, run_dir)
    except (OSError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"输入样例落地失败: {e}")

    stdin: Dict[str, Any] = {
        "task_id": "debug",
        "data_id": "debug",
        "fields": fields,
        "model_output": None,
        "session": None,
        "params": payload.params,
        "upstream": {},
        "_run_dir": str(run_dir),
    }
    script_def = {
        "script_id": script["id"],
        "name": script["name"],
        "exec_type": script["exec_type"],
        "entrypoint": script.get("entrypoint"),
        "timeout_seconds": script.get("timeout_seconds") or 120,
    }
    result = pre_judge_runner.execute_script(script_def, stdin, timeout_seconds=script_def["timeout_seconds"])
    return {
        "script_id": script["id"],
        "script_name": script["name"],
        "run_dir": str(run_dir),
        "fields": fields,
        "result": result,
        # script 类型的运行前静态检查提示（提示级，不拦截）；其他类型为空列表
        "static_check": result.get("static_check") or [],
    }


@router.get("/{script_id}/export", summary="导出脚本契约 JSON")
async def export_script(script_id: str, db: Session = Depends(get_db)):
    """契约 JSON 打包（设计 §十二 二期）：脚本契约字段打包为可再导入的 JSON。

    - DB 记录：导出契约字段（id/created_by/created_at 等环境相关字段不导出）
    - builtin: 前缀：历史引用兼容，按名称解析到 DB 记录（统一契约，无内置专属标记）
    - 响应带 Content-Disposition 附件头，浏览器直接下载为 {name}.json
    """
    contract = _service(db).export_contract(script_id)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"脚本不存在: {script_id}")
    filename = f"{_safe((contract.get('script') or {}).get('name') or script_id)}.json"
    return JSONResponse(
        content=contract,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import-package", summary="导入脚本包（zip / 多文件列表）")
async def import_script_package(
    request: Request,
    files: List[UploadFile] = File(..., description="包文件（单 .zip / 多文件列表）"),
    relative_paths: List[str] = Form(default=[], description="与 files 一一对应的相对路径（多文件直选时使用）"),
    overwrite: bool = Form(default=False, description="同名活跃脚本已存在时是否覆盖"),
    # Annotated 形态保证直接调用（单测）时默认值为 ""，而 Form(default="") 的
    # Python 默认是 FieldInfo 对象，直调语义会失真
    source_name: Annotated[str, Form(description="显式包名（zip 文件名去扩展名 / 文件夹名）；"
                                      "前端本地解压展开为多文件提交时路径已平铺，服务端无法自行推断")] = "",
    db: Session = Depends(get_db),
):
    """导入脚本包（一次请求 = 一个包 = 一个脚本）：

    - 单 .zip：内存解包 + 路径穿越校验 + 唯一顶层目录剥离（GitHub 式）
    - 多文件列表：按 relative_paths 组装目录（文件夹直选 / 前端本地解压 zip 形态）
    - 强制契约校验：包内必须含根级 main.py 与 model.py；model.py 须语法可解析
      且 PreScriptInput/PreScriptOutput 声明齐全（字段可为空），不满足走逐项
      error 明细（引导补齐后重新导入）
    - 根级 manifest.json 存在时复用契约字段建脚本（exec_type 强制 script），
      否则自动命名（优先 source_name 表单字段，其次从路径推断）；
      所有 .py 过静态风险扫描；requirements.txt 非空时提示预装依赖
    - 响应 HTTP 200，逐项明细 results（status=ok|error）
    """
    _ensure_admin(request)
    user = getattr(request.state, "user", None) or {}
    created_by = user.get("user_code") if isinstance(user, dict) else None

    items: List[tuple] = []
    for index, upload in enumerate(files):
        rel = relative_paths[index] if index < len(relative_paths) else (upload.filename or f"file_{index}")
        items.append((rel, await upload.read()))

    files_map, inferred_name, error = normalize_package(items)
    if error:
        return {"results": [{"source": inferred_name or (files[0].filename if files else ""),
                             "status": "error", "script_id": None, "name": None,
                             "error": error, "warnings": []}]}
    source_name = str(source_name or "").strip() or inferred_name  # 显式包名优先于路径推断（平铺 zip 推断必失真）
    result = _service(db).import_package(files_map, source_name,
                                         overwrite=overwrite, created_by=created_by)
    logger.info(f"[PreJudgeScript] 导入脚本包 source={source_name} status={result['status']}")
    return {"results": [result]}


@router.get("/{script_id}/export-package", summary="导出脚本包 zip")
async def export_script_package(script_id: str, db: Session = Depends(get_db)):
    """导出脚本目录全部文件 + 根级 manifest.json（契约）为 zip 附件 {name}.zip"""
    import io
    from urllib.parse import quote

    try:
        name, zip_bytes = _service(db).export_package(script_id)
    except ValueError as e:
        message = str(e)
        status = 404 if "不存在" in message else 400
        raise HTTPException(status_code=status, detail=message)
    encoded = quote(_safe(name))
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{_safe(name)}.zip\"; filename*=UTF-8''{encoded}.zip"
            )
        },
    )


@router.post("/from-template", summary="从脚手架模板生成脚本")
async def create_script_from_template(
    payload: PreJudgeScriptTemplatePayload, request: Request, db: Session = Depends(get_db),
):
    """脚手架：single（main.py + README）/ folder（main.py + utils.py + README +
    requirements.txt），生成即建记录（exec_type=script、entrypoint=main.py）+ 落盘，
    可直接在线编辑/试跑/导出。同名冲突 400。
    """
    _ensure_admin(request)
    user = getattr(request.state, "user", None) or {}
    created_by = user.get("user_code") if isinstance(user, dict) else None
    try:
        script = _service(db).create_from_template(
            payload.template, payload.name,
            description=payload.description, created_by=created_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return script


# ==================== 文件树 / 在线编辑（script 类型专属） ====================

def _map_file_error(exc: PreJudgeFileError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/{script_id}/files", summary="脚本文件树（白名单文本文件清单）")
async def list_script_files(script_id: str, request: Request, db: Session = Depends(get_db)):
    """列出脚本目录内的文本白名单文件（目录结构由前端构树）"""
    _ensure_admin(request)
    try:
        return _service(db).list_script_files(script_id)
    except PreJudgeFileError as e:
        raise _map_file_error(e)


@router.get("/{script_id}/file", summary="读取脚本文本文件")
async def read_script_file(
    script_id: str, request: Request,
    path: str = Query(..., description="脚本目录内相对路径"),
    db: Session = Depends(get_db),
):
    """读取文本文件内容（不存在 404 / 非文本与穿越 400）"""
    _ensure_admin(request)
    try:
        return _service(db).read_script_file(script_id, path)
    except PreJudgeFileError as e:
        raise _map_file_error(e)


@router.put("/{script_id}/file", summary="在线编辑保存（乐观锁）")
async def update_script_file(
    script_id: str, payload: PreJudgeScriptFileSavePayload,
    request: Request, db: Session = Depends(get_db),
):
    """保存文本文件（仅已存在文件；updated_at 与记录不一致 → 409）；
    成功后 bump 记录 updated_at 并重跑风险扫描（warnings 随响应返回）"""
    _ensure_admin(request)
    try:
        return _service(db).update_script_file(
            script_id, payload.path, payload.content, payload.updated_at)
    except PreJudgeFileError as e:
        raise _map_file_error(e)


@router.post("/{script_id}/file", summary="新建脚本文本文件")
async def create_script_file(
    script_id: str, payload: PreJudgeScriptFileCreatePayload,
    request: Request, db: Session = Depends(get_db),
):
    """新建文本文件（默认空内容；已存在 409；路径必须合法且在白名单内）"""
    _ensure_admin(request)
    try:
        return _service(db).create_script_file(script_id, payload.path, payload.content)
    except PreJudgeFileError as e:
        raise _map_file_error(e)


@router.delete("/{script_id}/file", summary="删除脚本文件")
async def delete_script_file(
    script_id: str, request: Request,
    path: str = Query(..., description="脚本目录内相对路径"),
    db: Session = Depends(get_db),
):
    """删除脚本目录内文件（入口 main.py 不可删）"""
    _ensure_admin(request)
    try:
        return _service(db).delete_script_file(script_id, path)
    except PreJudgeFileError as e:
        raise _map_file_error(e)


@router.post("/{script_id}/file/rename", summary="重命名/移动脚本文件")
async def rename_script_file(
    script_id: str, payload: PreJudgeScriptFileRenamePayload,
    request: Request, db: Session = Depends(get_db),
):
    """重命名/移动文件（main.py 不可改名；目标存在 409）"""
    _ensure_admin(request)
    try:
        return _service(db).rename_script_file(script_id, payload.path, payload.new_path)
    except PreJudgeFileError as e:
        raise _map_file_error(e)


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(name)) or "script"
