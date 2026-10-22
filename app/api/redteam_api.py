"""红队攻击样本库 API（M3 QE3-1）

- ``GET /evaluation/redteam/packs``：列出内置样本库（名称/版本/类目/样本数），
  任务表单「样本库选择」数据源；
- ``POST /evaluation/redteam/packs/{pack_name}/import-dataset``：把样本库导入为
  普通数据集（「可导入可编辑」通道——导入后与任何数据集一样可增删改，
  安全评测任务以 sample_source=dataset 引用编辑后的样本）。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import scopes
from app.core.scopes import require_scope
from app.db.session import get_db
from app.services.access_control import user_team_code
from app.services.dataset_service import DatasetService
from app.services.redteam import list_packs, load_pack, pack_samples_as_dataset_rows

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/redteam", tags=["红队样本库"])

# M1 QE1-5 同口径：新端点显式声明 scope 并进 API Key 路由注册表（default-deny）
scopes.register_api_key_route(
    "GET", "/api/v1/evaluation/redteam/packs", scopes.DATASETS_READ)
scopes.register_api_key_route(
    "POST", "/api/v1/evaluation/redteam/packs/{pack_name}/import-dataset",
    scopes.DATASETS_WRITE)


class ImportPackRequest(BaseModel):
    """样本库导入数据集请求"""
    name: Optional[str] = Field(None, description="新数据集名称（缺省按样本库命名）")
    description: Optional[str] = Field(None, description="数据集描述")
    categories: Optional[List[str]] = Field(
        None, description="导入的攻击面类目子集（缺省全部类目）"
    )


class ImportPackResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    pack: str
    pack_version: str
    categories: List[str]
    imported_count: int


@router.get("/packs", summary="列出内置红队攻击样本库",
            dependencies=[Depends(require_scope(scopes.DATASETS_READ))])
def get_packs():
    """列出内置样本库（含版本与各类目样本数）"""
    try:
        return list_packs()
    except Exception as e:  # noqa: BLE001
        logger.exception("样本库列表读取失败")
        raise HTTPException(status_code=500, detail=f"样本库列表读取失败: {e}")


@router.post(
    "/packs/{pack_name}/import-dataset",
    response_model=ImportPackResponse,
    summary="样本库导入为数据集（可导入可编辑）",
    dependencies=[Depends(require_scope(scopes.DATASETS_WRITE))],
)
def import_pack_as_dataset(
    pack_name: str,
    body: ImportPackRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """把内置样本库导入为普通数据集（user_input=攻击提示词 + 攻击面元数据字段）

    导入后样本可在数据集管理页增删改；安全评测任务以
    safety_config.sample_source="dataset" 引用编辑后的样本。
    """
    user = request.state.user
    user_code = user.get('user_code') if isinstance(user, dict) else (
        getattr(user, 'user_code', None) if hasattr(user, 'user_code') else None
    )
    org_code = user.get('org_code') if isinstance(user, dict) else (
        getattr(user, 'org_code', None) if hasattr(user, 'org_code') else None
    )

    try:
        pack = load_pack(pack_name, categories=body.categories)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    dataset_name = (body.name or "").strip() or f"红队样本库-{pack['name']}-v{pack['version']}"
    dataset_description = (body.description or "").strip() or (
        f"由内置红队样本库 {pack['name']}@{pack['version']} 导入"
        f"（类目: {', '.join(pack['categories'])}）；样本可编辑，安全评测任务以"
        f"数据集来源引用"
    )

    service = DatasetService(db)
    try:
        dataset = service.create_dataset(
            name=dataset_name,
            description=dataset_description,
            created_by=user_code,
            team_code=org_code if org_code else "DEFAULT",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    rows = pack_samples_as_dataset_rows(pack["samples"])
    # import_from_json 以 question/ref_answer 为主字段，其余键平铺进 data JSON
    import_items = [
        {
            "question": row["user_input"],
            "ref_answer": row.get("answer", ""),
            "attack_category": row["attack_category"],
            "attack_sample_id": row["attack_sample_id"],
            "expected_behavior": row["expected_behavior"],
            "risk_point": row["risk_point"],
            "severity": row["severity"],
        }
        for row in rows
    ]
    imported = service.import_from_json(dataset.id, import_items)

    return ImportPackResponse(
        dataset_id=dataset.id,
        dataset_name=dataset_name,
        pack=pack["name"],
        pack_version=pack["version"],
        categories=pack["categories"],
        imported_count=imported,
    )
