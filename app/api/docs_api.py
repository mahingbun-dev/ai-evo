"""
用户文档 API
提供用户使用文档的读取和更新接口
"""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

DOCS_DIR = settings.PROJECT_ROOT / "docs"
USER_GUIDE_PATH = DOCS_DIR / "user_guide.md"

router = APIRouter(prefix="/docs", tags=["用户文档"])


class UpdateDocRequest(BaseModel):
    content: str


@router.get("/user-guide", summary="获取用户使用文档")
def get_user_guide():
    """读取并返回 Markdown 格式的用户使用文档"""
    try:
        if not USER_GUIDE_PATH.exists():
            raise HTTPException(status_code=404, detail="文档文件不存在")
        content = USER_GUIDE_PATH.read_text(encoding="utf-8")
        return {"content": content}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"读取文档失败: {e}")
        raise HTTPException(status_code=500, detail="读取文档失败")


@router.put("/user-guide", summary="更新用户使用文档")
def update_user_guide(body: UpdateDocRequest):
    """更新 Markdown 格式的用户使用文档（管理员操作）"""
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        USER_GUIDE_PATH.write_text(body.content, encoding="utf-8")
        return {"message": "文档更新成功"}
    except Exception as e:
        logger.error(f"更新文档失败: {e}")
        raise HTTPException(status_code=500, detail="更新文档失败")
