"""
用户文档 API
提供用户使用文档的读取和更新接口
"""
import logging
from fastapi import APIRouter, HTTPException, Depends
from app.services.access_control import ensure_super_admin
from app.utils.path_safety import secure_mkdir
from pydantic import BaseModel
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

DOCS_DIR = settings.PROJECT_ROOT / "docs"
USER_GUIDE_PATH = DOCS_DIR / "user_guide.md"

# ARCH-0-1 后 api/worker 容器 read_only：/app/docs 属镜像 root 只读层，PUT 原地
# 写 user_guide.md 必 500。管理员更新改落到 uploads 卷（api/worker 共享的唯一
# 持久化写面，entrypoint 预检保证可写）下的覆盖副本；GET 优先读覆盖副本，
# 无覆盖时回退镜像内置默认文档（首次部署/未编辑过即走默认）。
UPLOADS_ROOT = settings.PROJECT_ROOT / "uploads"
USER_GUIDE_OVERRIDE_PATH = UPLOADS_ROOT / "user_guide.md"

router = APIRouter(prefix="/docs", tags=["用户文档"])


class UpdateDocRequest(BaseModel):
    content: str


@router.get("/user-guide", summary="获取用户使用文档")
def get_user_guide():
    """读取并返回 Markdown 格式的用户使用文档（管理员覆盖副本优先，否则镜像内置默认）"""
    path = USER_GUIDE_OVERRIDE_PATH if USER_GUIDE_OVERRIDE_PATH.exists() else USER_GUIDE_PATH
    try:
        if not path.exists():
            raise HTTPException(status_code=404, detail="文档文件不存在")
        content = path.read_text(encoding="utf-8")
        return {"content": content}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"读取文档失败: {e}")
        raise HTTPException(status_code=500, detail="读取文档失败")


@router.put("/user-guide", summary="更新用户使用文档", dependencies=[Depends(ensure_super_admin)])
def update_user_guide(body: UpdateDocRequest):
    """更新 Markdown 格式的用户使用文档（管理员操作，写 uploads 卷覆盖副本）"""
    try:
        secure_mkdir(str(UPLOADS_ROOT))
        USER_GUIDE_OVERRIDE_PATH.write_text(body.content, encoding="utf-8")
        return {"message": "文档更新成功"}
    except Exception as e:
        logger.error(f"更新文档失败: {e}")
        raise HTTPException(status_code=500, detail="更新文档失败")
