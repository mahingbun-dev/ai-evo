"""评测工作区统一入口（script_runs 路径构造/读写/归档）

ARCH-2-4（设计 plans/2026-10-15-企业级评测系统架构设计.md）：
- §2.3 决策 D5：uploads/script_runs/{task_id}/{data_id}/ 升格为每次执行的
  标准工作区，任务完成归档（压缩 + 清理原目录），作为产物对象存储的挂载点；
- §3 远期扩展点 2（K8s 预留）：script_runs 的读写收敛在 workspace 模块，
  届时切对象存储（MinIO）只换本模块 backend 实现，DB 存引用，调用方零改动。

收敛策略（最小改动）：路径构造/清理的唯一实现仍沉淀在 pre_judge_runner
（SCRIPT_RUN_BASE 口径、ensure_within 越界兜底、secure_mkdir 显式权限，
ARCH-0-3），本模块以 WorkspaceBackend 接口包装转发——既有调用方零改动、
现有 script_runs 路径行为不变；新增读写（任务级归档等）与后续迁移一律走
本模块入口。转发为调用时延迟导入：读 pre_judge_runner 模块常量
（SCRIPT_RUN_BASE）在调用时生效，测试 monkeypatch 该常量对经由本模块的
调用同样有效。
"""
import logging
import os
import shutil
import tarfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.utils.path_safety import ensure_within, secure_mkdir

logger = logging.getLogger(__name__)

# 任务级归档目录（相对项目根，与 uploads/script_runs 同级约定；测试可 monkeypatch）
ARCHIVE_BASE = Path("uploads/archives")

# 归档文件权限：属主读写、组只读、其他无（ARCH-0-3 收权口径——工作区产物
# 可能含输入数据/模型输出等敏感内容，归档不给其他用户读位）
ARCHIVE_FILE_MODE = 0o640


class WorkspaceBackend(ABC):
    """工作区后端接口雏形（ARCH-2-4，为 Phase 3 对象存储预留）

    Phase 3 工作区整体搬 MinIO 时新增对象存储实现替换默认后端实例
    （_default_backend），本模块对外函数签名不变；届时归档 = 上传压缩
    包 + 删本地目录，语义同 LocalWorkspaceBackend.archive_task_workspace。
    """

    @abstractmethod
    def build_run_dir(self, task_id: str, data_id: str) -> Path:
        """创建并返回单条数据工作区 uploads/script_runs/{task_id}/{data_id}/

        一律绝对路径；工作区直下 outputs/（脚本产物目录）保证存在。
        """

    @abstractmethod
    def build_script_run_dir(self, task_id: str, data_id: str, script_id: str) -> Path:
        """创建并返回 script 类型 cwd 隔离目录 .../{data_id}/{script_id}/
        （脚本相对路径写盘统一落该目录，随任务清理）
        """

    @abstractmethod
    def cleanup_item_run_files(self, task_id: str, data_id: str) -> None:
        """单条数据评完即清其工作区（目录不存在 no-op）"""

    @abstractmethod
    def cleanup_script_run_files(self, task_id: str) -> None:
        """任务结束清任务级工作区目录 uploads/script_runs/{task_id}/
        （目录不存在 no-op；异常中断残留的兜底清理）
        """

    @abstractmethod
    def archive_task_workspace(self, task_id: str) -> Optional[Path]:
        """任务级归档：工作区 tar 到 uploads/archives/{task_id}.tar.gz
        后清原目录（压缩 + 清理，设计 §2.3 D5）。

        返回归档文件绝对路径；工作区目录不存在返回 None（no-op）；
        归档失败抛出异常且保留原目录（由调用方告警，不影响任务状态）。
        """


class LocalWorkspaceBackend(WorkspaceBackend):
    """本地盘后端：script_runs 路径构造/清理沿用 pre_judge_runner 既有口径

    现有 script_runs 路径行为不变（同一实现转发）；归档为本后端新增能力。
    """

    def build_run_dir(self, task_id: str, data_id: str) -> Path:
        from app.services import pre_judge_runner

        return pre_judge_runner.build_run_dir(task_id, data_id)

    def build_script_run_dir(self, task_id: str, data_id: str, script_id: str) -> Path:
        from app.services import pre_judge_runner

        return pre_judge_runner.build_script_run_dir(task_id, data_id, script_id)

    def cleanup_item_run_files(self, task_id: str, data_id: str) -> None:
        from app.services import pre_judge_runner

        pre_judge_runner.cleanup_item_run_files(task_id, data_id)

    def cleanup_script_run_files(self, task_id: str) -> None:
        from app.services import pre_judge_runner

        pre_judge_runner.cleanup_script_run_files(task_id)

    def archive_task_workspace(self, task_id: str) -> Optional[Path]:
        from app.services import pre_judge_runner

        # 空 task_id 拒绝归档：SCRIPT_RUN_BASE/"" 即 script_runs 根本身，
        # 放行会把全部任务工作区打进一个归档
        if not str(task_id or "").strip():
            return None
        task_dir = Path(pre_judge_runner.SCRIPT_RUN_BASE / str(task_id)).absolute()
        if not task_dir.is_dir():
            return None
        # 归档文件路径 ensure_within 兜底（ARCH-0-3 口径）：task_id 为内部
        # ID，仍拒绝 .. 穿越/symlink 越界写归档
        archive_path = Path(ensure_within(ARCHIVE_BASE, f"{task_id}.tar.gz")).absolute()
        secure_mkdir(archive_path.parent)
        # 先写临时文件再原子改名：归档中途失败不落半成品 .tar.gz；失败时
        # best-effort 清理临时文件后上抛，原目录保留由调用方告警（走既有
        # 兜底清理，核心要求「失败保留原目录」）
        tmp_path = archive_path.with_name(archive_path.name + ".tmp")
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                # arcname=task_id：归档内以任务目录名为根，解包即还原层级；
                # 默认不追踪 symlink（工作区内脚本可能建的软链原样入包，
                # 不读链向目标、无越界读取面）
                tar.add(task_dir, arcname=str(task_id))
            os.chmod(tmp_path, ARCHIVE_FILE_MODE)
            os.replace(tmp_path, archive_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        # 归档落盘成功后清原目录（与 cleanup_script_run_files 同款
        # ignore_errors 语义；清理失败不影响已落盘归档，残留走下次兜底）
        shutil.rmtree(task_dir, ignore_errors=True)
        return archive_path


# 默认后端（Phase 3 切对象存储时替换本实例即可，模块级入口签名不变）
_default_backend = LocalWorkspaceBackend()


def get_workspace_backend() -> WorkspaceBackend:
    """取当前工作区后端（测试可 monkeypatch _default_backend 注入替身）"""
    return _default_backend


# ---------------------------------------------------------------------------
# 统一入口：script_runs 路径构造/读写一律经本模块函数（后续读写迁移同此）
# ---------------------------------------------------------------------------

def build_run_dir(task_id: str, data_id: str) -> Path:
    """创建并返回单条数据工作区（语义见 WorkspaceBackend.build_run_dir）"""
    return get_workspace_backend().build_run_dir(task_id, data_id)


def build_script_run_dir(task_id: str, data_id: str, script_id: str) -> Path:
    """创建并返回 script 类型 cwd 隔离目录（语义见接口注释）"""
    return get_workspace_backend().build_script_run_dir(task_id, data_id, script_id)


def cleanup_item_run_files(task_id: str, data_id: str) -> None:
    """单条数据评完即清其工作区（语义见接口注释）"""
    get_workspace_backend().cleanup_item_run_files(task_id, data_id)


def cleanup_script_run_files(task_id: str) -> None:
    """任务结束清任务级工作区目录（语义见接口注释）"""
    get_workspace_backend().cleanup_script_run_files(task_id)


def archive_task_workspace(task_id: str) -> Optional[Path]:
    """任务级归档（tar + 清原目录，失败抛异常且保留原目录，见接口注释）"""
    return get_workspace_backend().archive_task_workspace(task_id)
