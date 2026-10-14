"""模型/裁判配置快照（W4，P0-C：配置版本化归因）

设计要点（整改开发计划 §2 W4）：
- create_task 创建任务时对被评测模型（task.model_provider）与裁判模型
  （task.judge_model）各做一次深拷贝快照，写入 task.model_config /
  task.judge_model_config；
- 快照只含口径字段（provider/model_type/endpoint/auth_type/request_config/
  response_parse_config）与掩码凭据（api_key_masked/api_key_fingerprint），
  **严禁落明文 api_key**；auth_config 含登录凭据（login_url 流程）同样不落快照；
- 执行时「快照优先，实时回退」：口径字段取创建时快照，调用凭据
  （api_key/auth_type/auth_config）从实时 ModelConfig 解析（密钥轮换后
  旧钥不可用，属唯一可行口径）；快照为空（存量任务）走实时查询；
- 每次执行开始把双快照写入 TaskExecutionRecord.config_snapshot，
  执行结束写入该次实际使用的配置摘要（归因锚点）。

对外入口（其余模块一律经由本模块，勿再内联拼配置字典）：
- build_model_config_snapshot：ModelConfig → 快照
- resolve_model_call_config：被评测模型调用配置（快照优先/实时回退）
- resolve_judge_call_config：裁判调用配置（快照 → CC 全局 → judge_model →
  consistency_config.judge_config）
- build_execution_config_snapshot / build_used_config_summary：执行记录写入
"""
import hashlib
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 快照掩码字段名（区别于调用配置里的明文 api_key 键，杜绝误用）
MASKED_KEY_FIELD = "api_key_masked"
FINGERPRINT_FIELD = "api_key_fingerprint"


def mask_api_key(value: Optional[str]) -> Optional[str]:
    """对 API Key 做脱敏处理，只保留前3位和后3位。

    与 model_config_api.ModelDetailResponse.mask_api_key 同一实现
    （W4 抽出为单一来源，API 层序列化器委托至此）。
    """
    if not value:
        return value
    if len(value) <= 8:
        return '*' * len(value)
    return value[:3] + '*' * (len(value) - 6) + value[-3:]


def api_key_fingerprint(value: Optional[str]) -> Optional[str]:
    """密钥指纹：sha256 前 8 位（归因锚点，可与快照/实时配置比对）"""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_model_config_dict(db, model_config_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """从数据库加载 ModelConfig 完整配置（含明文 api_key，仅在内存中流转）

    Args:
        db: 数据库会话；为 None 时自建 SessionLocal（用完即关）
        model_config_id: ModelConfig.id

    Returns:
        调用配置 dict（与原各处内联拼装的口径一致），查不到返回 None
    """
    if not model_config_id:
        return None

    close_db = False
    if db is None:
        from app.db.session import SessionLocal
        db = SessionLocal()
        close_db = True
    try:
        from app.models.model_config_models import ModelConfig

        obj = db.query(ModelConfig).filter(ModelConfig.id == model_config_id).first()
        if not obj:
            return None
        return {
            "provider": obj.provider,
            "model_type": obj.model_type,
            "endpoint": obj.endpoint,
            "api_key": obj.api_key,
            "auth_type": obj.auth_type,
            "auth_config": obj.auth_config,
            "request_config": obj.request_config,
            "response_parse_config": obj.response_parse_config,
            "model_id": obj.id,
        }
    except Exception as e:
        logger.warning(f"加载模型配置失败（model_id={model_config_id}）: {e}")
        return None
    finally:
        if close_db:
            db.close()


def build_model_config_snapshot(db, model_config_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """对 ModelConfig 做深拷贝快照（严禁明文凭据）

    Args:
        db: 数据库会话（None 时自建）
        model_config_id: ModelConfig.id；为空返回 None（无配置可快照）

    Returns:
        快照 dict（含掩码密钥与指纹，不含 api_key/auth_config），配置不存在返回 None
    """
    if not model_config_id:
        return None

    close_db = False
    if db is None:
        from app.db.session import SessionLocal
        db = SessionLocal()
        close_db = True
    try:
        from app.models.model_config_models import ModelConfig

        obj = db.query(ModelConfig).filter(ModelConfig.id == model_config_id).first()
        if not obj:
            logger.warning(f"配置快照来源不存在: model_id={model_config_id}")
            return None
        return {
            "snapshot": True,
            "snapshot_at": _utc_now_iso(),
            "model_id": obj.id,
            "name": obj.name,
            "provider": obj.provider,
            "model_type": obj.model_type,
            "endpoint": obj.endpoint,
            # auth_type 属调用方式说明（不含凭据），保留供归因展示；
            # 凭据三件套（api_key/auth_type 运行时值/auth_config）执行时实时解析
            "auth_type": obj.auth_type,
            "request_config": deepcopy(obj.request_config) or {},
            "response_parse_config": deepcopy(obj.response_parse_config) or {},
            MASKED_KEY_FIELD: mask_api_key(obj.api_key),
            FINGERPRINT_FIELD: api_key_fingerprint(obj.api_key),
        }
    except Exception as e:
        logger.warning(f"构建配置快照失败（model_id={model_config_id}）: {e}")
        return None
    finally:
        if close_db:
            db.close()


def _merge_live_credentials(config: Dict[str, Any], db, live_model_id: Optional[str]) -> None:
    """快照口径字段 + 实时凭据：调用凭据一律从实时 ModelConfig 解析覆盖

    密钥轮换/删除后快照中的旧钥不可用（且快照只存掩码），凭据必须取实时值。
    实时 ModelConfig 不存在时保留快照原值（掩码键不可用于调用，行为与现状
    「配置被删后调用失败」一致）。
    """
    if not live_model_id:
        return
    live = _load_model_config_dict(db, live_model_id)
    if live:
        config["api_key"] = live.get("api_key")
        config["auth_type"] = live.get("auth_type")
        config["auth_config"] = live.get("auth_config")


# ---------------------------------------------------------------------------
# 执行期解析：快照优先，实时回退
# ---------------------------------------------------------------------------

def resolve_model_call_config(task, db=None) -> Tuple[Dict[str, Any], str]:
    """解析被评测模型调用配置（W4 快照优先，实时回退）

    Args:
        task: 任务对象（duck-typing：读 model_config/model_provider）
        db: 数据库会话（None 时自建，仅在需要实时解析时）

    Returns:
        (调用配置 dict, 来源标签)
        来源标签："snapshot"（口径字段来自创建时快照）/"live"（存量任务实时
        查询）/"none"（无任何配置）
    """
    snapshot = getattr(task, "model_config", None) or {}
    if isinstance(snapshot, dict) and snapshot:
        config = dict(snapshot)
        _merge_live_credentials(
            config, db,
            snapshot.get("model_id") or getattr(task, "model_provider", None),
        )
        return config, "snapshot"

    # 快照为空（存量任务/无模型任务）：实时回退
    provider_id = getattr(task, "model_provider", None)
    if provider_id:
        live = _load_model_config_dict(db, provider_id)
        if live:
            return live, "live"
    return {}, "none"


def resolve_judge_call_config(task, db=None) -> Tuple[Dict[str, Any], str]:
    """解析裁判调用配置（W4 快照优先，实时回退）

    优先级（与原 judge_graph_state._load_judge_model_config / 一致性评测链一致，
    仅把 task.judge_model_config 从「恒空占位」升级为「创建时快照」）：
    1. task.judge_model_config 快照（口径字段来自创建时快照，凭据实时解析）
    2. 全局 Claude Code 配置（软退场：裁判模型统一由 CC 配置驱动）
    3. task.judge_model 对应的实时 ModelConfig（历史任务可复跑）
    4. consistency_config.judge_config（一致性评测历史兜底）

    Returns:
        (调用配置 dict, 来源标签)
        来源标签："snapshot"/"claude_code_global"/"live"/"consistency_config"/"none"
    """
    snapshot = getattr(task, "judge_model_config", None) or {}
    if isinstance(snapshot, dict) and snapshot:
        config = dict(snapshot)
        _merge_live_credentials(
            config, db,
            snapshot.get("model_id") or getattr(task, "judge_model", None),
        )
        return config, "snapshot"

    try:
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        cc_config = ClaudeCodeConfigService.get_judge_llm_config()
    except Exception as e:
        logger.warning(f"加载 Claude Code 全局配置失败: {e}")
        cc_config = None
    if cc_config:
        return cc_config, "claude_code_global"

    judge_model_id = getattr(task, "judge_model", None)
    if judge_model_id:
        live = _load_model_config_dict(db, judge_model_id)
        if live:
            return live, "live"

    consistency_config = getattr(task, "consistency_config", None) or {}
    if isinstance(consistency_config, dict) and consistency_config.get("judge_config"):
        return consistency_config["judge_config"], "consistency_config"

    return {}, "none"


def snapshot_used(task) -> bool:
    """本次执行是否使用创建时配置快照（result JSON 的 config_snapshot 标记）"""
    return bool(
        getattr(task, "model_config", None)
        or getattr(task, "judge_model_config", None)
    )


# ---------------------------------------------------------------------------
# 执行记录（TaskExecutionRecord.config_snapshot）写入
# ---------------------------------------------------------------------------

# 明文凭据键：执行记录拷贝双快照时一律剥离，保证 config_snapshot 列
# 安全无明文（即使存量任务/显式透传路径在 task.model_config 中带入了明文）
_PLAINTEXT_CREDENTIAL_KEYS = ("api_key", "auth_config")


def _strip_plaintext_credentials(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """深拷贝快照并剥离明文凭据键（掩码/指纹字段保留）"""
    if not isinstance(snapshot, dict):
        return None
    cleaned = deepcopy(snapshot)
    for key in _PLAINTEXT_CREDENTIAL_KEYS:
        cleaned.pop(key, None)
    return cleaned


def build_execution_config_snapshot(task, db=None) -> Dict[str, Any]:
    """执行开始写入：创建时双快照（归因锚点的「应然」面）"""
    return {
        "model": _strip_plaintext_credentials(getattr(task, "model_config", None)),
        "judge": _strip_plaintext_credentials(getattr(task, "judge_model_config", None)),
    }


def summarize_call_config(config: Optional[Dict[str, Any]], source: str) -> Optional[Dict[str, Any]]:
    """调用配置 → 掩码摘要（执行结束的归因锚点，严禁明文凭据出库）"""
    if not config:
        return None
    api_key = config.get("api_key")
    return {
        "source": source,
        "provider": config.get("provider"),
        "model_type": config.get("model_type"),
        "endpoint": config.get("endpoint"),
        "auth_type": config.get("auth_type"),
        "request_config": deepcopy(config.get("request_config") or {}),
        MASKED_KEY_FIELD: mask_api_key(api_key),
        FINGERPRINT_FIELD: api_key_fingerprint(api_key),
    }


def build_used_config_summary(task, db=None) -> Dict[str, Any]:
    """执行结束写入：该次实际使用的配置摘要（「实然」面）

    与执行链路共用同一解析器，摘要即可回答「这次 X 分当时用的什么模型/温度」。
    裁判侧按 judge_engine 归因：claude_agent 引擎走 CC 全局配置（不消费任务级
    裁判快照），graph 引擎走快照优先链。
    """
    summary: Dict[str, Any] = {"model": None, "judge": None}

    model_config, model_source = resolve_model_call_config(task, db)
    summary["model"] = summarize_call_config(model_config, model_source)

    evaluation_type = getattr(task, "evaluation_type", "")
    consistency_config = getattr(task, "consistency_config", None) or {}
    uses_judge = evaluation_type in ("judge", "agent") or (
        evaluation_type == "consistency"
        and isinstance(consistency_config, dict)
        and bool(consistency_config.get("use_judge"))
    )
    if uses_judge:
        judge_engine = (getattr(task, "judge_engine", None) or "graph").lower()
        if judge_engine == "claude_agent":
            try:
                from app.services.claude_code_config_service import ClaudeCodeConfigService

                cc_config = ClaudeCodeConfigService.get_judge_llm_config()
            except Exception as e:
                logger.warning(f"归因摘要加载 CC 全局配置失败: {e}")
                cc_config = None
            # 裁判链路在用而配置缺失时仍留 source 标签（可归因为何无配置），
            # 区别于「该任务本就不用裁判」的 None
            summary["judge"] = (
                summarize_call_config(cc_config, "claude_code_global")
                or {"source": "claude_code_global", "config_missing": True}
            )
        else:
            judge_config, judge_source = resolve_judge_call_config(task, db)
            summary["judge"] = (
                summarize_call_config(judge_config, judge_source)
                or {"source": judge_source, "config_missing": True}
            )

    return summary
