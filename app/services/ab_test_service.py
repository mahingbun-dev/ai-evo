"""
A/B 测试会话 CRUD 服务
"""
import uuid
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from app.models.ab_test_models import ABTestSession


class ABTestService:
    """A/B 测试会话服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_session(
        self,
        model_a_id: str,
        model_b_id: str,
        model_a_name: Optional[str] = None,
        model_b_name: Optional[str] = None,
        variables: Optional[Dict] = None,
        result_a: Optional[Dict] = None,
        result_b: Optional[Dict] = None,
        status: str = "completed",
        duration_a: Optional[float] = None,
        duration_b: Optional[float] = None,
        name: Optional[str] = None,
    ) -> ABTestSession:
        """保存一次 A/B 测试记录"""
        session_id = f"abtest_{uuid.uuid4().hex[:12]}"
        session = ABTestSession(
            id=session_id,
            name=name,
            model_a_id=model_a_id,
            model_b_id=model_b_id,
            model_a_name=model_a_name,
            model_b_name=model_b_name,
            variables=variables or {},
            result_a=result_a or {},
            result_b=result_b or {},
            status=status,
            duration_a=duration_a,
            duration_b=duration_b,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_session(self, session_id: str) -> Optional[ABTestSession]:
        """获取单条记录"""
        return self.db.query(ABTestSession).filter(ABTestSession.id == session_id).first()

    def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        keyword: str = "",
    ) -> Dict[str, Any]:
        """获取历史记录列表（分页），支持按备注名模糊搜索"""
        query = self.db.query(ABTestSession)
        if keyword:
            query = query.filter(ABTestSession.name.ilike(f"%{keyword}%"))
        total = query.count()
        items = (
            query
            .order_by(ABTestSession.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {"total": total, "items": items, "page": page, "page_size": page_size}

    def delete_session(self, session_id: str) -> bool:
        """删除记录"""
        session = self.get_session(session_id)
        if not session:
            return False
        self.db.delete(session)
        self.db.commit()
        return True
