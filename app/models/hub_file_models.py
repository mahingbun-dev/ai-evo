"""
anc-hub 数据集执行包登记模型

记录从 anc-hub file-store 下载并解包的 zip 执行包（数据集迭代功能）：
- file_id 唯一：同一个 hub 文件只登记一行，重复导入复用解包产物
- unpack_status 生命周期：pending → unpacked / failed
- ref_count 引用计数：执行/预览持有引用，归零时清理解包目录与登记记录

批量导入任务（hub_import_jobs）：
- 每次从 anc-hub 勾选多个执行包导入 = 一条 job，Celery 逐包处理
- succeeded_file_ids 记录已成功导入的 file_id，retry 只重跑失败项且幂等
- fail_detail 记录失败明细 {file_id: 中文错误}，供前端展示与重试定位
"""
from sqlalchemy import Column, Integer, String, DateTime, Text, JSON
from sqlalchemy.sql import func
from app.db.session import Base


class HubFileRecord(Base):
    """anc-hub 执行包登记表（hub_files）"""
    __tablename__ = "hub_files"

    id = Column(String, primary_key=True, index=True)
    # anc-hub file-store 文件 ID（全局唯一，重复导入按它幂等命中）
    file_id = Column(String, nullable=False, unique=True, index=True)
    # zip 原始文件名（服务端返回，经 sanitize 清理）
    filename = Column(String, nullable=True, comment="anc-hub 原始文件名")
    # zip 包大小与 sha256（下载时双校验的落盘值）
    size = Column(Integer, nullable=True, comment="zip 包大小（字节）")
    sha256 = Column(String, nullable=True, comment="zip 包 sha256")
    # hub 侧元数据（导入流程按 file_query 回填，缺省为空）
    uploader = Column(String, nullable=True, comment="hub 上传者")
    hub_created_at = Column(DateTime(timezone=True), nullable=True, comment="hub 侧上传时间")
    # 解包产物
    unpack_dir = Column(String, nullable=True, comment="解包目录（uploads/hub_datasets/{file_id}）")
    unpack_status = Column(
        String, nullable=False, server_default="pending",
        comment="解包状态：pending|unpacked|failed",
    )
    unpack_error = Column(Text, nullable=True, comment="最近一次解包失败的中文错误信息")
    file_count = Column(Integer, nullable=True, comment="解包文件数")
    total_size = Column(Integer, nullable=True, comment="解包后总字节数")
    # 引用计数：执行/预览 acquire +1、release -1，归零时清理登记与解包目录
    ref_count = Column(Integer, nullable=False, server_default="0", default=0, comment="引用计数")
    # 文件索引（build_file_index 产物：[{path,type,size,is_nested_zip,nested_index}]）
    file_index = Column(JSON, nullable=True, comment="解包文件索引（JSON 数组）")
    # 人工补传索引（人工上传补充进包的文件：[{path,type,size,is_nested_zip,
    # nested_index,uploaded_by,uploaded_at}]；物理落盘在 uploads/hub_supplements/
    # {file_id}/，读取面与 file_index 叠加展示，条目打 is_manual 标记）
    supplement_index = Column(JSON, nullable=True, comment="人工补传文件索引（JSON 数组，读取时叠加进树/匹配视图）")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class HubImportJob(Base):
    """anc-hub 执行包批量导入任务表（hub_import_jobs）"""
    __tablename__ = "hub_import_jobs"

    id = Column(String, primary_key=True, index=True)
    # 目标数据集（创建 job 时已同步解析：新建 or 追加到 real_dataset_id）
    dataset_id = Column(String, nullable=False, index=True, comment="目标数据集 ID")
    # 待导入的 anc-hub file_id 列表（创建时去重保序）
    file_ids = Column(JSON, nullable=False, comment="待导入的 anc-hub file_id 列表")
    status = Column(
        String, nullable=False, server_default="pending",
        comment="任务状态：pending|running|completed|failed",
    )
    total_count = Column(Integer, nullable=False, server_default="0", default=0, comment="总包数")
    success_count = Column(Integer, nullable=False, server_default="0", default=0, comment="成功包数")
    fail_count = Column(Integer, nullable=False, server_default="0", default=0, comment="失败包数")
    # 失败明细 {file_id: 中文错误}；retry 成功后对应条目移除
    fail_detail = Column(JSON, nullable=True, comment="失败明细（{file_id: 错误信息}）")
    # 已成功导入的 file_id 集合：retry 只重跑失败项、已成功的不重复导入（幂等键）
    succeeded_file_ids = Column(JSON, nullable=True, comment="已成功导入的 file_id 列表")
    created_by = Column(String, nullable=True, comment="创建人")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
