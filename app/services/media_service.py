"""
媒体资源管理服务
"""
import uuid
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from fastapi import UploadFile
from app.core.config import get_settings
from app.models.media_models import MediaResource
from app.utils.read_limited import read_limited
from app.utils.secure_filename import secure_filename


class MediaService:
    """媒体资源服务"""

    def __init__(self, db: Session, upload_dir: str = "./uploads/media"):
        self.db = db
        self.upload_dir = upload_dir
        # 确保上传目录存在
        os.makedirs(upload_dir, exist_ok=True)

    async def upload_file(
        self,
        file: UploadFile,
        description: Optional[str] = None,
        uploaded_by: Optional[str] = None,
        team_code: Optional[str] = None,
    ) -> MediaResource:
        """上传媒体文件（W1-1：文件名清洗 + 大小上限；W9：team_code 随用户团队落库）"""
        # 读取文件内容（超限即抛 413，不整读进内存后才发现）
        content = await read_limited(file, get_settings().UPLOAD_MAX_BYTES)
        file_size = len(content)

        # 生成文件ID
        media_id = f"media_{uuid.uuid4().hex[:12]}"

        # 文件名清洗：剥目录成分/控制字符，防路径穿越写（../../evil.py → evil.py）
        safe_name = secure_filename(file.filename)

        # 获取文件类型和MIME类型
        file_type = self._get_file_type(safe_name)
        mime_type = file.content_type or self._get_mime_type(file_type)

        # 保存文件到磁盘
        file_path = os.path.join(self.upload_dir, f"{media_id}_{safe_name}")
        with open(file_path, "wb") as f:
            f.write(content)

        # 创建数据库记录
        media = MediaResource(
            id=media_id,
            file_name=safe_name,
            file_type=file_type,
            mime_type=mime_type,
            file_size=file_size,
            file_path=file_path,
            description=description,
            uploaded_by=uploaded_by,
            team_code=team_code or "DEFAULT",
        )

        self.db.add(media)
        self.db.commit()
        self.db.refresh(media)

        return media

    def get_media(self, media_id: str) -> Optional[MediaResource]:
        """获取媒体资源"""
        return self.db.query(MediaResource).filter(MediaResource.id == media_id).first()

    def get_media_list(
        self,
        file_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        team_code: Optional[str] = None,
    ) -> List[MediaResource]:
        """获取媒体资源列表（W9：team_code 非空时只返回该团队，超管传 None 看全部）"""
        query = self.db.query(MediaResource).filter(MediaResource.is_active == True)

        if file_type:
            query = query.filter(MediaResource.file_type == file_type)
        if team_code:
            query = query.filter(MediaResource.team_code == team_code)

        return query.order_by(MediaResource.created_at.desc()).offset(skip).limit(limit).all()

    def delete_media(self, media_id: str) -> bool:
        """删除媒体资源"""
        media = self.get_media(media_id)
        if not media:
            return False

        # 删除物理文件
        if media.file_path and os.path.exists(media.file_path):
            try:
                os.remove(media.file_path)
            except Exception:
                pass

        # 软删除
        media.is_active = False
        self.db.commit()

        return True

    def get_file_content(self, media_id: str) -> Optional[bytes]:
        """获取文件内容"""
        media = self.get_media(media_id)
        if not media:
            return None

        # 从文件路径读取
        if media.file_path and os.path.exists(media.file_path):
            with open(media.file_path, "rb") as f:
                return f.read()

        # 从数据库读取
        if media.file_data:
            return bytes(media.file_data)

        return None

    @staticmethod
    def _get_file_type(filename: str) -> str:
        """根据文件名获取文件类型"""
        ext = os.path.splitext(filename)[1].lower()
        type_map = {
            ".jpg": "image",
            ".jpeg": "image",
            ".png": "image",
            ".gif": "image",
            ".bmp": "image",
            ".webp": "image",
            ".pdf": "pdf",
            ".doc": "document",
            ".docx": "document",
            ".xls": "spreadsheet",
            ".xlsx": "spreadsheet",
            ".ppt": "presentation",
            ".pptx": "presentation",
            ".txt": "text",
            ".json": "json",
            ".xml": "xml",
        }
        return type_map.get(ext, "unknown")

    @staticmethod
    def _get_mime_type(file_type: str) -> str:
        """根据文件类型获取MIME类型"""
        mime_map = {
            "image": "image/jpeg",
            "pdf": "application/pdf",
            "document": "application/octet-stream",
            "spreadsheet": "application/octet-stream",
            "presentation": "application/octet-stream",
            "text": "text/plain",
            "json": "application/json",
            "xml": "application/xml",
        }
        return mime_map.get(file_type, "application/octet-stream")

