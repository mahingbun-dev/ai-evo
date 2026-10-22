"""插件市场 API（插件评测「从市场选择评测对象」来源）"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.plugin_marketplace_service import (
    MarketplaceError,
    create_marketplace,
    delete_marketplace,
    list_marketplace_plugins,
    list_marketplaces,
    sync_marketplace,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plugin-marketplaces", tags=["plugin-marketplaces"])


class CreateMarketplaceRequest(BaseModel):
    name: str = Field(..., description="市场名称", max_length=100)
    git_url: str = Field(..., description="市场 git 仓库地址", max_length=500)
    branch: Optional[str] = Field("", description="分支（空=默认分支）", max_length=100)


def _biz(fn, *args, **kwargs):
    """业务错误统一转 400"""
    try:
        return fn(*args, **kwargs)
    except MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", summary="插件市场列表")
def get_marketplaces(db: Session = Depends(get_db)):
    return list_marketplaces(db)


@router.post("", summary="新增插件市场（立即克隆）")
def add_marketplace(payload: CreateMarketplaceRequest, db: Session = Depends(get_db)):
    return _biz(create_marketplace, db, payload.name, payload.git_url, payload.branch or "")


@router.post("/{market_id}/sync", summary="同步插件市场（拉取远端最新）")
def sync_marketplace_api(market_id: int, db: Session = Depends(get_db)):
    return _biz(sync_marketplace, db, market_id)


@router.delete("/{market_id}", summary="删除插件市场（清理本地克隆）")
def remove_marketplace(market_id: int, db: Session = Depends(get_db)):
    return _biz(delete_marketplace, db, market_id)


@router.get("/{market_id}/plugins", summary="列出市场中可评测的插件/skill")
def get_marketplace_plugins(market_id: int, db: Session = Depends(get_db)):
    return _biz(list_marketplace_plugins, db, market_id)
