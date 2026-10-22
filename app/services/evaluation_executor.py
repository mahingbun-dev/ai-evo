"""
并发评测执行器

使用ThreadPoolExecutor实现最大并发数为4的评测执行，
支持通过 threading.Event 中断任务执行。
"""
import json
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any, Optional

from celery.exceptions import SoftTimeLimitExceeded

from app.db.session import engine, SessionLocal
from app.services.metric_scoring import has_custom_dimension_max

logger = logging.getLogger(__name__)

# CC 裁判 targets 内容并入 prompt 的字符阈值：与超长会话落盘阈值一致
# （30000 字符 ≈ 90KB UTF-8，低于 harness_adapter 的 100KB argv 安全阈值
# 与内核 131072B 单参数上限）；超过即落盘由裁判 Read 工具阅读
CC_TARGETS_INLINE_LIMIT_CHARS = 30000


class EvaluationExecutor:
    """并发评测执行器"""

    def __init__(
        self,
        max_workers: int = 4,
        task_id: str = None,
        stop_event: threading.Event = None,
        langfuse_host: str = "",
        execution_number: Optional[int] = None,
        progress_offset: int = 0,
    ):
        """
        初始化执行器

        Args:
            max_workers: 最大并发数
            task_id: 任务ID，用于日志和进度更新
            stop_event: 停止信号，设置后执行器在下一个检查点退出
            langfuse_host: LangFuse 服务地址（用于 LangGraph 裁判追踪）
            execution_number: 本次执行序号（W3 行级落库键）。None 表示
                非任务执行链路（单测直调等），跳过行级 write-through
            progress_offset: 进度偏移（W3 断点续跑：本轮已跳过的已完成行数，
                使续跑进度显示为累计值而非从零回退）
        """
        self.max_workers = min(max_workers, 4)  # 最大并发数为4
        self.task_id = task_id
        self.stop_event = stop_event or threading.Event()
        self.langfuse_host = langfuse_host
        self.execution_number = execution_number
        self.progress_offset = progress_offset

    def execute_batch(
        self,
        payloads: List[Dict[str, Any]],
        task,
    ) -> List[Dict[str, Any]]:
        """
        批量执行评测

        Args:
            payloads: 请求Payload列表
            task: 任务对象

        Returns:
            评测结果列表
        """
        results = []
        total = len(payloads)

        # 创建线程池
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务（future -> (列表序号, payload)；payload 携带任务链路
            # 标注的 _orig_idx 数据项原始序号，作为行级落库键）
            future_to_payload = {
                executor.submit(
                    self._evaluate_single_with_trials,
                    payload,
                    task,
                ): (pos, payload)
                for pos, payload in enumerate(payloads)
            }

            # 收集结果
            stop_requested = False
            for future in as_completed(future_to_payload):
                # 如果已收到停止信号，跳过处理新完成的结果
                if stop_requested:
                    continue

                pos, payload = future_to_payload[future]
                try:
                    result = future.result()
                    results.append(result)

                    # 更新进度
                    self._update_progress(len(results), total)

                except SoftTimeLimitExceeded:
                    # 软超时不得按行失败吞掉（P0-E/W3-4）：立即上抛触发收尾，
                    # 已落库行保留，由 run_evaluation_async 标记任务失败
                    raise
                except Exception as e:
                    logger.exception(f"评测执行失败 (idx={pos})")
                    results.append({
                        "idx": pos,
                        "error": str(e),
                        "success": False,
                    })

                # 行级 write-through（W3-2）：每行终态（成功/失败）即时幂等落库，
                # 停止/强杀窗口内已完成行零丢失
                self._persist_row_result(pos, payload, results[-1])

                # 检查停止信号（每条完成后检查）
                if self.stop_event.is_set():
                    logger.info(
                        f"任务 {self.task_id} 收到停止信号，"
                        f"已完成 {len(results)}/{total} 条，取消剩余"
                    )
                    # 标记停止请求，不再处理新结果
                    stop_requested = True
                    # 注意：已开始的 future 无法取消，只能等待其完成或忽略其结果

        # 按原始顺序排序结果
        results.sort(key=lambda x: x.get("idx", 0))

        # ========== [evaluation/result 扩展点（本期预留，设计 §九.3）] ==========
        # 聚合完成后遍历任务配置的 result 观察脚本：只读 result，产出附加到
        # result.observations。任务未配置 result_observers 时零行为变化；
        # 观察管线自身异常不阻断评测主流程（与前处理脚本 continue 策略一致）
        try:
            from app.services.pre_judge_runner import run_result_observers

            if getattr(task, "result_observers", None):
                run_result_observers(task, results)
        except Exception as e:
            logger.warning(f"[ResultObservers] 结果观察脚本执行异常（不影响评测结果）: {e}")

        # 清理 Claude Code Agent 裁判的会话临时文件
        try:
            from app.services.claude_code_judge_agent import cleanup_session_files

            if self.task_id:
                cleanup_session_files(self.task_id)
        except Exception as e:
            logger.warning(f"清理裁判会话临时文件失败: {e}")

        # 清理前处理脚本产物目录 uploads/script_runs/{task_id}/（设计文档 §七.6，复用 judge_sessions 清理模式）
        try:
            from app.services.pre_judge_runner import cleanup_script_run_files

            if self.task_id:
                cleanup_script_run_files(self.task_id)
        except Exception as e:
            logger.warning(f"清理脚本产物临时目录失败: {e}")

        return results

    def _persist_row_result(self, pos: int, payload: Any, result: Dict[str, Any]) -> None:
        """行级结果 write-through 落库（W3-2/P0-E）

        - execution_number 未提供（单测直调/非任务链路）时跳过；
        - data_index 以 payload 的 _orig_idx（数据项原始序号，任务链路标注）为准，
          与 hub 拆分/断点续跑过滤后的列表序号解耦，保证 (task_id, execution_number,
          data_index) 键在多次执行间稳定；
        - 落库失败不阻断评测主流程（evaluation_result_service 内部已按进程去重告警）。
        """
        if not self.task_id or self.execution_number is None:
            return
        if not isinstance(payload, dict):
            payload = {}
        data_index = payload.get("_orig_idx", pos)
        try:
            from app.services.evaluation_result_service import persist_row_result

            persist_row_result(
                task_id=self.task_id,
                execution_number=self.execution_number,
                data_index=data_index,
                result=result,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception as e:
            logger.warning(f"[RowPersist] 行级结果落库失败（不影响评测主流程）idx={data_index}: {e}")

    def _evaluate_single_with_trials(
        self,
        payload: Dict[str, Any],
        task,
    ) -> Dict[str, Any]:
        """行级多 trial 评测入口（M2 QE2-3，pass^k）

        trials=1（未配置/非 judge 类型）直接走单 trial 评测，零额外开销；
        trials>1 时同一数据行重复「模型调用 → 前处理 → 校验器 → 裁判」
        k 次合并为一行：语义对齐 τ-bench pass^k（app/services/pass_k.py）。
        payload 逐 trial 拷贝（_evaluate_single 会写 payload["model_output"]
        等键，每次 trial 独立工作区语义）；收到停止信号跑完当前 trial 即止，
        pass^k 按实际跑到的 trial 数计算（不虚报）。
        """
        from app.services.pass_k import row_trials

        trials = row_trials(task)
        if trials <= 1:
            return self._evaluate_single(payload, task)

        trial_results = []
        for trial_no in range(1, trials + 1):
            trial = self._evaluate_single(dict(payload), task)
            trial["_trial"] = trial_no
            trial_results.append(trial)
            if self.stop_event.is_set():
                break
        return self._merge_trial_results(trial_results)

    @staticmethod
    def _trial_passed(trial: Dict[str, Any]) -> bool:
        """单 trial 通过判定（M2 QE2-3）

        优先确定性口径：trial 配置了终态校验器（verification_result 存在）
        时以校验 status==pass 为准（M2 验收的「校验器 + pass^k 全链路」）；
        未配置校验器时退化为「trial 产出有效裁判分」（模型调用成功且
        裁判未全失败）。trial 中途失败/停止均不计通过。
        """
        if not trial.get("success") or trial.get("average_score") is None:
            return False
        verification = trial.get("verification_result")
        if isinstance(verification, dict):
            return verification.get("status") == "pass"
        return True

    @staticmethod
    def _merge_trial_results(trial_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """k 次 trial 结果合并为单行（M2 QE2-3）

        - 证据主体（model_output/裁判明细/校验证据）取首个成功 trial
          （全失败取首个），完整 trial 明细单列 trials_detail；
        - 行分 average_score = k 次 trial 有效裁判分的均值（多次采样口径；
          维度分/反馈等其余证据字段保持主体 trial 的）；pass@1/pass^k/
          分数标准差/标准误由 pass_k 统一计算（与其他类型同口径）。
        """
        from app.services.pass_k import score_stats, trial_pass_stats

        primary = next(
            (t for t in trial_results if t.get("success")), trial_results[0]
        )
        merged = {k: v for k, v in primary.items() if k != "_trial"}

        passes = [EvaluationExecutor._trial_passed(t) for t in trial_results]
        scores = [
            float(t["average_score"])
            for t in trial_results
            if isinstance(t.get("average_score"), (int, float))
        ]
        merged["trials_count"] = len(trial_results)
        merged["trials_detail"] = [
            {
                "trial": t.get("_trial"),
                "success": bool(t.get("success")),
                "score": t.get("average_score"),
                "error": t.get("error"),
            }
            for t in trial_results
        ]
        merged["trial_scores"] = [t.get("average_score") for t in trial_results]
        merged["trial_passes"] = passes
        merged.update(trial_pass_stats(passes))
        merged.update(score_stats(scores))
        if scores:
            merged["average_score"] = round(sum(scores) / len(scores), 4)
        return merged

    @staticmethod
    def _aggregate_judge_scores(result: Dict[str, Any], judge_scores: List[Dict[str, Any]]) -> Dict[str, Any]:
        """多裁判评分聚合（W2-2 新口径）

        - 失败裁判（judge_failed=True，score 为 None）不再以 0 分计入均分；
        - 总分与维度分同口径：均只聚合 valid（未失败）裁判；
        - 全部裁判失败 → average_score=None、row_status="judge_failed"，
          该行不进任务均分（result_aggregator 对 None 行自动过滤）；
        - 新增统计字段 judge_count / failed_judge_count / row_status（增量，不删旧字段）；
        - feedback 拼接保留全部裁判（含失败裁判的错误信息，便于定位失败原因）。
        """
        if judge_scores:
            valid = [r for r in judge_scores if not r.get("judge_failed")]
            failed_count = len(judge_scores) - len(valid)

            result["judge_count"] = len(judge_scores)
            result["failed_judge_count"] = failed_count

            if valid:
                # valid 裁判均分（裁判分为 round2，行级均值精度收敛到 4 位小数）
                result["average_score"] = round(
                    sum(r.get("score") or 0 for r in valid) / len(valid), 4
                )
                result["row_status"] = "completed"
            else:
                # 行级：全部裁判失败 → 该行标记 judge_failed，不进任务均分
                result["average_score"] = None
                result["row_status"] = "judge_failed"

            # 收集所有有反馈的裁判反馈（含失败裁判的错误描述）
            feedbacks = [s.get("feedback", "") for s in judge_scores if s.get("feedback")]
            result["feedback"] = "\n---\n".join(feedbacks)

            # 聚合各裁判的维度评分：只吃 valid 裁判（与总分同口径），按维度取平均
            dim_accum: Dict[str, List[float]] = {}
            for s in valid:
                for dim_name, dim_score in (s.get("dimension_scores") or {}).items():
                    if isinstance(dim_score, (int, float)):
                        dim_accum.setdefault(dim_name, []).append(float(dim_score))
            if dim_accum:
                result["dimension_scores"] = {
                    dim: sum(vals) / len(vals)
                    for dim, vals in dim_accum.items()
                }
        return result

    @staticmethod
    def _renormalize_cc_judge_scores(judge_results, scoring_metrics, task_max_score):
        """维度最高分归一化重算 CC 裁判自报总分（原地修改并返回）

        CC 裁判自报总分不感知各维度量程（最高分 5 的维度打 5 会被当 5 分
        微平均拉低总分），任务配置了非任务满分的维度最高分时，服务端按
        「维度分/维度最高分×任务满分」加权重算每个裁判的总分（与图引擎聚合
        口径一致）；未配置时原样返回，存量任务行为不变。裁判自报分保留在
        judge_reported_score 供追溯；失败裁判与无维度分的裁判不重算。

        重算只按该裁判实际评分的维度做微平均（验收 P1-1）：本行不涉及的
        维度按 CC 协议输出 "F" 哨兵、normalize 阶段已从 dimension_scores
        剔除，若仍按全量维度表计权会把不涉及维度当 0 分压低总分，违背
        「F 维度不参与」的协议语义。
        """
        if not has_custom_dimension_max(scoring_metrics, task_max_score):
            return judge_results
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        for judge_result in judge_results or []:
            if not isinstance(judge_result, dict):
                continue
            dim_scores = judge_result.get("dimension_scores")
            if judge_result.get("judge_failed") or not dim_scores:
                continue
            # 只保留该裁判实际产出分数的维度配置（F 哨兵维度缺失 → 不计权）
            involved_metrics = [
                m for m in scoring_metrics
                if isinstance(m, dict) and m.get("name") in dim_scores
            ]
            if not involved_metrics:
                continue
            reported_score = judge_result.get("score")
            recomputed = JudgeEvaluationEngine.calculate_weighted_score(
                involved_metrics, dim_scores, task_max_score or 10
            )
            judge_result["judge_reported_score"] = reported_score
            judge_result["score"] = recomputed
        return judge_results

    @staticmethod
    def _judge_variable_keys(task) -> List[str]:
        """提取 judge_variable_config 的全部变量 key（dict / list 两种格式）

        用于 @ 引用标记解析的 vars 上下文：从 judge_payload 取这些 key 的已解析值
        """
        jvc = getattr(task, "judge_variable_config", None)
        if isinstance(jvc, dict):
            return list(jvc.keys())
        if isinstance(jvc, list):
            return [
                item.get("key") for item in jvc
                if isinstance(item, dict) and item.get("key")
            ]
        return []

    @staticmethod
    def _render_session_from_payload(payload: Dict[str, Any]) -> str:
        """从数据项中提取会话内容并渲染为 markdown

        支持两种存储形态：结构化 session_messages（SessionRecord）或已渲染的
        markdown 字符串（手工导入），返回空串表示无会话内容。
        """
        dataset_item = payload.get("_dataset_item", {}) or {}
        if not isinstance(dataset_item, dict):
            return ""
        data = dataset_item.get("data", {}) if isinstance(dataset_item.get("data"), dict) else {}
        session = data.get("session_messages") or dataset_item.get("session_messages")
        if not session:
            return ""
        if isinstance(session, str):
            return session
        try:
            from app.services.session_file_parser import render_session_markdown

            return render_session_markdown(session)
        except Exception as e:
            logger.warning(f"渲染会话 markdown 失败: {e}")
            return ""

    @staticmethod
    def _extract_dataset_field_output(payload: Dict[str, Any], field_name: Optional[str]) -> str:
        """从数据集字段提取模型输出（model_output_source=dataset_field）

        - 兼容数据项的两种形态：payload._dataset_item 为 dict（任务链路
          _load_dataset 构造）或 ORM 对象（get_data_list_v2 直出）；
        - 字段值可能是 dict/list 等非字符串，序列化为 JSON 文本（keep 中文）；
        - 字段缺失/为 None 返回空串，交由既有空值校验走 empty_input 口径。
        """
        field = (field_name or "").strip()
        if not field:
            raise RuntimeError(
                "模型输出来源为数据集字段但未配置字段名（model_output_field）"
            )
        dataset_item = payload.get("_dataset_item") or {}
        if isinstance(dataset_item, dict):
            data = dataset_item.get("data")
        else:
            data = getattr(dataset_item, "data", None)
        if not isinstance(data, dict):
            data = {}
        value = data.get(field)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _build_cc_targets_prompt(
        resolved_targets: Dict[str, Dict[str, Any]],
        task_id: str,
        payload: Dict[str, Any],
        session_file: Optional[str] = None,
        session_markdown: Optional[str] = None,
    ) -> str:
        """CC 裁判待评内容分流：小内容并入 prompt，大内容落盘由裁判 Read 阅读

        targets 内容原本经 PromptBuilder._build_system_prompt 注入 system_prompt，
        由 claude-agent-sdk 以 --system-prompt argv 传给 CLI；Linux 单个 argv
        字符串上限 131072B，超限 execve 直接报 [Errno 7] Argument list too long
        （所有裁判启动失败）。CC 引擎 system_prompt 不再携带数据，改为：
        ≤阈值的内容并入 user prompt（stdin 传输无上限），>阈值的写临时文件并把
        绝对路径附进 prompt，裁判 agent 用 Read 工具分段阅读（与超长会话
        session_file 同一机制）；与既有 session_file 内容相同时直接复用不重复落盘。
        """
        from app.services.claude_code_judge_agent import write_session_file
        from app.services.prompt_guard import wrap_eval_data

        if not resolved_targets:
            return ""
        dataset_item = payload.get("_dataset_item", {}) or {}
        data_id = dataset_item.get("id", payload.get("idx", 0))
        parts: List[str] = []
        for target_info in resolved_targets.values():
            name = target_info.get("name", "")
            content = target_info.get("content", "")
            if not content or target_info.get("is_empty", False):
                continue
            if session_file and content == session_markdown:
                # 与超长会话落盘内容一致：复用同一文件，避免重复写盘与重复阅读
                path = session_file
            elif len(content) > CC_TARGETS_INLINE_LIMIT_CHARS:
                path = write_session_file(task_id, data_id, content)
            else:
                path = None
            if path:
                parts.append(
                    f"### {name}\n完整内容已保存到本地文件：{path}\n"
                    f"请使用 Read 工具阅读该文件（文件较长时分段阅读，直到读完）。"
                )
            else:
                parts.append(f"### {name}\n{wrap_eval_data(content)}")
        if not parts:
            return ""
        return "\n---\n## 待评测目标内容\n\n" + "\n\n".join(parts)

    @staticmethod
    def _run_pre_scripts(task, payload: Dict[str, Any], steps=None) -> Dict[str, Any]:
        """执行任务编排的前处理脚本管线（设计文档 §二 步骤1.5 / §七.1）

        Args:
            task: 评测任务对象（读取 pre_scripts / id）
            payload: 评测请求 payload（model_output / _dataset_item 供脚本 stdin 组装）
            steps: 显式编排步骤（共享设计契约 v3 §三）：优先于 task.pre_scripts；
                由 evaluation_targets 的 script_output 行经 derive_script_steps 派生。
                None 保持旧行为读取 task.pre_scripts（未迁移任务兼容路径）

        Returns:
            {aborted, error, pre_script_results, script_outputs, script_files}
            aborted=True 表示某脚本 on_fail=abort 失败，调用方应将该条数据标记执行失败
        """
        from app.services.pre_judge_runner import run_pre_scripts

        try:
            return run_pre_scripts(
                task, payload=payload,
                dataset_item=payload.get("_dataset_item"),
                steps=steps,
            )
        except Exception as e:
            # 脚本管线自身异常不阻断评测主流程：按 fail 结果记录，裁判可见失败事实
            logger.exception("[PreScripts] 前处理脚本管线执行异常")
            return {
                "aborted": False,
                "error": None,
                "pre_script_results": [{
                    "script_id": "",
                    "script_name": "pipeline",
                    "status": "fail",
                    "output": None,
                    "files": {},
                    "summary": "",
                    "finish_reason": "error",
                    "error": f"前处理脚本管线异常: {e}",
                    "duration_ms": 0,
                }],
                "script_outputs": {},
                "script_files": {},
            }

    @staticmethod
    def _run_verifiers(task, payload: Dict[str, Any], model_output: Any) -> Optional[Dict[str, Any]]:
        """执行任务配置的终态校验器管线（M2 QE2-1，步骤1.6）

        Returns:
            {status: pass|fail|error, results, passed, failed, errored, total,
             aborted, error}；任务未配置校验器返回 None（result 不写
            verification_result 键，兼容旧结果结构）。管线自身异常收敛为
            error 状态整体条目（不阻断评测主流程，与 pre_scripts 同策略）。
        """
        from app.services.verifier_runner import STATUS_ERROR, run_verifiers

        try:
            return run_verifiers(task, payload=payload, model_output=model_output)
        except Exception as e:  # noqa: BLE001 - 校验管线异常不阻断评测主流程
            logger.exception("[Verifiers] 终态校验器管线执行异常")
            return {
                "status": STATUS_ERROR,
                "results": [],
                "passed": 0,
                "failed": 0,
                "errored": 0,
                "total": 0,
                "aborted": False,
                "error": f"终态校验器管线异常: {e}",
            }

    @staticmethod
    def _run_trajectory_match(task, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """执行任务配置的轨迹匹配（M2 QE2-2，步骤1.7，agent 类型）

        Returns:
            匹配结果 dict（含 status/timeline，行级落 trajectory_match 列）；
            任务未配置 trajectory_config 返回 None（result 不写该键，兼容旧
            结果结构）。管线自身异常收敛为 status=error 整体结果（确定性证据
            缺失可见，不阻断评测主流程）。
        """
        from app.services.trajectory_matcher import STATUS_ERROR, run_trajectory_match

        try:
            return run_trajectory_match(task, payload=payload)
        except Exception as e:  # noqa: BLE001 - 匹配管线异常不阻断评测主流程
            logger.exception("[Trajectory] 轨迹匹配管线执行异常")
            return {
                "mode": None,
                "status": STATUS_ERROR,
                "matched": [],
                "missing": [],
                "unexpected": [],
                "forbidden_hits": [],
                "matched_count": 0,
                "total_expected": 0,
                "total_calls": 0,
                "total_forbidden": 0,
                "timeline": [],
                "error": f"轨迹匹配管线异常: {e}",
            }

    def _evaluate_single(
        self,
        payload: Dict[str, Any],
        task,
    ) -> Dict[str, Any]:
        """
        执行单条评测

        步骤：
        1. 请求被评测模型，获取输出
        2. 组装裁判Prompt
        3. 请求裁判模型，获取评分
        4. 解析裁判响应

        Args:
            payload: 请求Payload
            task: 任务对象

        Returns:
            评测结果
        """
        from app.services.model_caller import ModelCallerSync
        from app.services.prompt_builder import PromptBuilder
        from app.services.evaluation_task_service import EvaluationTaskService

        # 在发起任何网络请求之前检查停止信号
        if self.stop_event.is_set():
            return {"idx": payload.get("idx", 0), "success": False, "error": "任务已停止"}

        result = {
            "idx": payload.get("idx", 0),
            "success": False,
        }

        # M2 QE2-4 裁判治理：按任务配置设置本线程的裁判输入消毒开关
        # （prompt 组装在 worker 线程内即时读取；显式关闭的任务可审计——
        # 开关状态随 judge_config_snapshot 落执行记录）
        from app.services.prompt_guard import set_prompt_guard_enabled

        set_prompt_guard_enabled(not bool(getattr(task, "judge_guard_disabled", False)))

        # M2 QE2-4 金丝雀探针行：合成「模型输出」直送裁判（见 judge_canary.py）。
        # 探针行跳过被测模型/前处理/校验器/轨迹与空值短路——探的是裁判本身
        is_canary = bool(payload.get("_judge_canary"))

        try:
            # 平铺 payload 中的输入变量到结果中
            input_variables = {k: v for k, v in payload.items()
                               if k not in ("_dataset_item", "idx") and not k.startswith("_")}
            result.update(input_variables)
            if is_canary:
                # 探针行标记（聚合整行剥离，不进均分；结果页单独展示）
                result["judge_canary"] = {
                    "kind": payload.get("_canary_kind", "empty"),
                    "content": payload.get("_canary_content", ""),
                }
            # ========== 步骤1: 请求被评测模型 ==========

            # 使用 ModelCallerSync（同步版本，与模型配置测试连通性使用相同的调用器）
            # 构建消息
            messages = [{"role": "user", "content": payload.get("user_input", "")}]

            # 获取 model_config：W4 快照优先（口径字段来自创建时快照，凭据
            # api_key/auth_config 从实时 ModelConfig 解析），快照为空（存量
            # 任务）回退实时查询
            from app.services.config_snapshot import resolve_model_call_config

            model_config, _config_source = resolve_model_call_config(task)

            # 提取 runtime_variables（从 payload 中提取非消息字段）
            runtime_variables = {}
            for key, value in payload.items():
                if key not in ("user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx"):
                    runtime_variables[key] = value

            # 处理 runtime_variables 中的 { value, type } 格式
            # 与模型配置测试连通性保持一致，确保类型正确转换
            for key, val in list(runtime_variables.items()):
                if isinstance(val, dict) and 'value' in val and 'type' in val:
                    var_type = val.get('type', 'string')
                    var_value = val.get('value', '')
                    if var_type == 'number':
                        try:
                            runtime_variables[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                        except (ValueError, TypeError):
                            runtime_variables[key] = 0
                    elif var_type == 'boolean':
                        runtime_variables[key] = str(var_value).lower() in ('true', '1', 'yes')
                    else:
                        runtime_variables[key] = str(var_value)

            # 模型输出来源：model_call-调用被评测模型（默认，含存量任务空值）/
            # dataset_field-直接取数据集字段 / none-不使用模型输出。
            # agent 的会话文件分支优先级最高（既有行为不变）。
            # 与创建/更新校验同口径归一化（strip/lower），避免带空白值校验通过
            # 却按字面量落到 model_call 分支。
            raw_output_source = getattr(task, "model_output_source", None)
            if isinstance(raw_output_source, str):
                model_output_source = raw_output_source.strip().lower() or "model_call"
            else:
                model_output_source = raw_output_source or "model_call"

            # 调用模型（使用同步方法，与模型配置测试连通性一致）
            session_markdown = None
            # 超长会话的临时文件路径（步骤1.5 前处理脚本与步骤3 CC 裁判共用，避免重复写盘）
            session_file = None
            if is_canary:
                # M2 QE2-4 金丝雀探针：空/乱码内容即「模型输出」，不调用被测
                # 模型（探针是给裁判的对抗样本，不消耗外部资源）
                call_result = {
                    "success": True,
                    "content": payload.get("_canary_content", ""),
                    "chain_results": [],
                }
            elif getattr(task, "evaluation_type", "") == "agent":
                # 长会话 Agent 评测：上传的会话文件即模型输出，不执行被测模型请求链
                session_markdown = self._render_session_from_payload(payload)
                if not session_markdown:
                    raise RuntimeError("数据项缺少会话内容（session_messages 字段），请使用会话文件导入数据集")
                call_result = {"success": True, "content": session_markdown, "chain_results": []}
                # 结果平铺会话摘要/来源（Results 明细列展示）
                dataset_item = payload.get("_dataset_item", {}) or {}
                item_data = dataset_item.get("data", {}) if isinstance(dataset_item.get("data"), dict) else {}
                result["session_summary"] = item_data.get("session_summary") or item_data.get("user_input", "")
                result["session_source"] = item_data.get("source", "")
            elif model_output_source == "dataset_field":
                # 模型输出已在数据集字段中：直接取值，不调用被评测模型
                # （知识库检索结果等场景无需也不应再次请求模型）
                model_output = self._extract_dataset_field_output(
                    payload, getattr(task, "model_output_field", None)
                )
                call_result = {"success": True, "content": model_output, "chain_results": []}
            elif model_output_source == "none":
                # 显式不使用模型输出：跳过模型调用，后续 targets/裁判变量照常解析
                call_result = {"success": True, "content": "", "chain_results": []}
            else:
                model_caller = ModelCallerSync()
                call_result = model_caller.call(messages, model_config, runtime_variables)

            if call_result.get("success"):
                model_output = call_result.get("content", "")
            else:
                error_msg = call_result.get("error", "未知错误")
                raise RuntimeError(f"模型调用失败: {error_msg}")

            result["model_output"] = model_output

            # 模型调用完成后再次检查停止信号（模型调用可能耗时较长）
            if self.stop_event.is_set():
                return {"idx": payload.get("idx", 0), "success": False, "error": "任务已停止"}

            # ========== 步骤1.5: 前处理脚本管线（扩展点 evaluation/pre-judge，设计文档 §二/§七/§九.3） ==========
            # 双路径（共享设计契约 v3 §三）：targets 含 script_output 行 → 用
            # derive_script_steps 派生的步骤执行（evaluation_targets 为唯一事实源）；
            # 否则未迁移任务回退 task.pre_scripts 旧配置路径。
            # 产出 script_outputs（输出变量）与 script_files（产物文件路径）注入裁判变量体系
            script_outputs: Dict[str, Any] = {}
            script_files: Dict[str, Any] = {}
            from app.services.pre_judge_runner import derive_script_steps

            derived_steps = derive_script_steps(getattr(task, "evaluation_targets", None) or [])
            if not is_canary and (derived_steps or getattr(task, "pre_scripts", None)):
                # model_output 文本写入 payload（payload 每条独立，见 _generate_payloads），
                # 由 pre_judge_runner 透传至脚本 stdin.model_output（设计文档 §三 标准输入契约）
                payload["model_output"] = model_output
                # agent 会话渲染的 model_output 超长时直接落盘到该条工作区
                # inputs/model_output.md（严格拷贝语义：脚本读写的都是本条工作区
                # 内文件，随工作区评完即清），并复用为步骤3 CC 裁判的 session_file，
                # 不重复写盘；data_id 派生与 pre_judge_runner._run_step 口径一致
                if session_markdown and len(session_markdown) > 30000:
                    from app.services.pre_judge_runner import build_run_dir

                    dataset_item_for_ws = payload.get("_dataset_item", {}) or {}
                    data_id = str(dataset_item_for_ws.get("id") or payload.get("idx") or "item")
                    ws_session_file = Path(build_run_dir(str(task.id), data_id)) / "inputs" / "model_output.md"
                    ws_session_file.parent.mkdir(parents=True, exist_ok=True)
                    ws_session_file.write_text(session_markdown, encoding="utf-8")
                    session_file = str(ws_session_file)
                    payload["model_output"] = session_file

                # targets 派生步骤优先（steps 显式传入优先）；legacy 路径传 None 保持读 task.pre_scripts
                pre_result = self._run_pre_scripts(task, payload, steps=derived_steps or None)
                result["pre_script_results"] = pre_result["pre_script_results"]
                if pre_result["aborted"]:
                    # on_fail=abort：该条数据标记执行失败，不再进入裁判评测
                    result["error"] = pre_result["error"] or "前处理脚本执行失败（on_fail=abort）"
                    return result
                script_outputs = pre_result["script_outputs"]
                script_files = pre_result["script_files"]

            # ========== 步骤1.6: 终态校验器（M2 QE2-1，确定性终态校验优先） ==========
            # 模型调用/被测 agent 执行之后、裁判之前运行（前处理脚本之后——校验器
            # 面向同一工作区，可断言脚本 stage 的执行包副本与产物）；输出
            # result.verification_result（行级落库+聚合 verification_pass_rate），
            # 独立于裁判计分（确定性证据，不算入裁判均分）。on_fail=abort 的校验器
            # 失败 → 该条数据标记执行失败，不再进入裁判（与 pre_scripts abort 同口径）。
            verification_result = None if is_canary else self._run_verifiers(task, payload, model_output)
            if verification_result is not None:
                result["verification_result"] = verification_result
                if verification_result.get("aborted"):
                    result["success"] = False
                    result["row_status"] = "verification_failed"
                    result["error"] = verification_result.get("error") or "终态校验器失败（on_fail=abort）"
                    return result

            # ========== 步骤1.7: 轨迹匹配（M2 QE2-2，agent 类型确定性轨迹断言） ==========
            # 对会话（session_messages）的工具调用序列做四模式匹配，输出
            # result.trajectory_match（行级落库+聚合 trajectory_match_rate）；
            # 独立于裁判维度计分（确定性证据，不算入裁判均分），不拦截评测流程
            # （截断 trace 虚高场景由匹配结果与门禁 metric 拦截，不是行失败）。
            if getattr(task, "evaluation_type", "") == "agent" and not is_canary:
                trajectory_match = self._run_trajectory_match(task, payload)
                if trajectory_match is not None:
                    result["trajectory_match"] = trajectory_match

            # ========== 步骤2: 组装裁判Prompt ==========
            # 从模型调用结果中提取 chain_results（需在空值校验前提取，因为评测目标可能来自 chain_result）
            # 当启用请求链时，call_result 会包含 chain_results 字段（从 request_details 构建）
            chain_results = call_result.get("chain_results", [])

            # 兼容旧格式：如果没有 chain_results 但有 all_results，转换格式
            if not chain_results:
                all_results = call_result.get("all_results", {})
                if all_results:
                    chain_results = [{"chain_index": 0, "all_results": all_results}]

            # ========== 空值校验：待评测内容为空时直接返回0分 ==========
            # 当配置了 evaluation_targets 时，从评测目标中解析实际待评测内容进行校验
            # 否则直接校验 model_output。
            # resolved_targets 全量结果同时作为 @ 引用标记解析的 targets 上下文（契约 v3 §三）
            evaluation_targets = getattr(task, 'evaluation_targets', None)
            content_to_validate = model_output
            resolved_targets: Dict[str, Dict[str, Any]] = {}
            # W12-3：evaluation_targets 中 script_output 行被置空的明细
            target_script_missing: List[Dict[str, Any]] = []
            if evaluation_targets:
                try:
                    from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget

                    resolver = JudgeVariableResolver(
                        task_config={},
                        dataset_item=payload.get("_dataset_item", {}),
                        model_result={"content": model_output},
                        chain_results=chain_results,
                        script_outputs=script_outputs,
                    )
                    targets = [EvaluationTarget(**t) for t in evaluation_targets]
                    resolved_targets = resolver.resolve_targets(targets)
                    target_script_missing = list(resolver.script_output_missing)

                    # 拼接所有 required 评测目标的内容作为校验对象
                    required_parts = []
                    for target_info in resolved_targets.values():
                        if target_info.get("required", True):
                            content = target_info.get("content", "")
                            if content and content.strip():
                                required_parts.append(content)
                    if required_parts:
                        content_to_validate = "\n".join(required_parts)
                except Exception as e:
                    logger.warning(f"[空值校验] 解析 evaluation_targets 失败，回退到 model_output 校验: {e}")
                    resolved_targets = {}

            # W12-3 脚本置空显式化：脚本输出被 evaluation_targets 引用但缺失
            # （脚本失败 on_fail=continue / 未编排 / 输出为 None）→ 该行标记
            # pre_script_failed、跳过裁判、不进均分。旧行为下置空内容会走
            # 空值校验以 0 分进均分（「脚本故障=模型 0 分」）；success=False
            # 使行级落库派生为 failed（可断点续跑重跑），任务均分自动排除。
            if target_script_missing:
                missing_desc = "; ".join(
                    f"script={m.get('script')!r} output_key={m.get('output_key')!r}"
                    for m in target_script_missing
                )
                logger.warning(
                    f"[PreScripts] idx={payload.get('idx', 0)} 脚本输出被评测目标引用但缺失: "
                    f"{missing_desc}，标记 pre_script_failed，跳过裁判"
                )
                result["success"] = False
                result["error"] = f"前处理脚本输出缺失（被评测目标引用）: {missing_desc}"
                result["row_status"] = "pre_script_failed"
                result["judge_scores"] = []
                result["judge_count"] = 0
                result["failed_judge_count"] = 0
                return result

            # M2 QE2-4：金丝雀探针行不做空值短路——空内容探针必须进裁判
            # （空值短路 0 分测不出「裁判被空内容欺骗」，对抗探针要让裁判
            # 看到空内容本身）
            if not is_canary and (not content_to_validate or not str(content_to_validate).strip()):
                logger.info(f"[空值校验] idx={payload.get('idx', 0)} 待评测内容为空，跳过裁判评测")
                result["success"] = True
                result["average_score"] = 0
                result["judge_scores"] = []
                result["feedback"] = "待评测内容为空，无法进行有效评估"
                result["dimension_scores"] = {}
                # W2-2: 空输入行 0 分口径保留，但显式标记 row_status，
                # 与真低分（completed）/ 裁判全失败（judge_failed）三种 0 可辨
                result["row_status"] = "empty_input"
                result["judge_count"] = 0
                result["failed_judge_count"] = 0
                return result

            # ========== 新增：添加 input_data 和 model_raw_response ==========
            # 1. model_raw_response：模型原始响应内容
            result["model_raw_response"] = self._format_model_raw_response(call_result, chain_results)

            # 2. input_data：输入数据（变量注入配置的字段内容）
            result["input_data"] = self._format_input_data(runtime_variables, payload)

            # 使用 VariableInjector 生成包含裁判变量的 payload
            from app.services.variable_injector import VariableInjector
            variable_injector = VariableInjector(
                task, chain_results=chain_results,
                script_outputs=script_outputs,
            )
            judge_payload = variable_injector.build_judge_payload(payload, model_output)
            # W12-3 脚本置空显式化：裁判变量（judge_variable_config）引用的
            # script_output 变量被置空 → 同样标记 pre_script_failed、跳过裁判、
            # 不进均分（口径与 targets 路径一致）
            if variable_injector.script_output_missing:
                missing_desc = "; ".join(
                    f"var={m.get('var')!r} script={m.get('script')!r} output_key={m.get('output_key')!r}"
                    for m in variable_injector.script_output_missing
                )
                logger.warning(
                    f"[PreScripts] idx={payload.get('idx', 0)} 裁判变量引用的脚本输出缺失: "
                    f"{missing_desc}，标记 pre_script_failed，跳过裁判"
                )
                result["success"] = False
                result["error"] = f"前处理脚本输出缺失（被裁判变量引用）: {missing_desc}"
                result["row_status"] = "pre_script_failed"
                result["judge_scores"] = []
                result["judge_count"] = 0
                result["failed_judge_count"] = 0
                return result
            # W12-6：不再向 judge_payload 塞 script_files 死键——裁判全链路
            # （graph 节点 / PromptBuilder / CC Agent）只消费显式声明的
            # script_output 变量，从不读取 script_files；旧赋值加注释
            # 「裁判 agent 可用 Read 工具阅读全量明细」从未真实接线，属误导。
            # script_files 保留自 _run_pre_scripts 的返回结构（产物文件路径），
            # 真实接线（CC 引擎 Read 产物）列入 M3。

            # ========== @ 引用标记统一解析（共享设计契约 v3 §三） ==========
            # 执行链路只解析一次：ctx.targets 用 resolve_targets 全量结果
            # {id: {name, content}}，ctx.vars 用裁判变量解析结果 {key: value}；
            # 得到的 overrides 传入 PromptBuilder 与 build_state_input，
            # 二者以 overrides 的 score_details / evaluation_metrics 替代 task 同名字段
            from app.services.reference_mark_resolver import resolve_task_ref_overrides

            ref_ctx = {
                "targets": resolved_targets,
                "vars": {
                    key: judge_payload.get(key)
                    for key in self._judge_variable_keys(task)
                },
            }
            overrides = resolve_task_ref_overrides(task, ref_ctx)

            # 计分用维度口径：与 build_state_input 一致，@ 引用覆盖件优先于 task
            # 同名字段（CC 引擎行级总分重算的守卫与公式都基于这份维度配置）
            scoring_metrics = overrides.get("evaluation_metrics")
            if scoring_metrics is None:
                scoring_metrics = getattr(task, "evaluation_metrics", None) or []

            prompt_builder = PromptBuilder(task, overrides=overrides)

            # 使用新的 build_user_prompt_from_judge_vars 方法构建 user_prompt
            user_prompt = prompt_builder.build_user_prompt_from_judge_vars(
                payload=judge_payload,
                model_output=model_output,
                chain_results=chain_results,
            )

            # 如果新方法返回空，则回退到原有方法
            if not user_prompt:
                system_prompt, user_prompt = prompt_builder.build_judge_prompts(
                    judge_payload,
                    model_output,
                    chain_results=chain_results,
                )
            else:
                # 构建 system_prompt
                system_prompt = prompt_builder._build_system_prompt(
                    user_input=judge_payload.get("user_input", ""),
                    ref_answer=judge_payload.get("ref_answer", ""),
                    model_output=model_output,
                    dataset_item=judge_payload.get("_dataset_item", {}),
                    payload=judge_payload,
                    chain_results=chain_results,
                )

            # ========== 步骤3: 裁判评分（双引擎：Claude Code Agent / LangGraph 裁判图） ==========
            # 历史任务无 judge_engine 值时按 graph 处理，保持原行为
            judge_engine = (getattr(task, "judge_engine", None) or "graph").lower()

            if judge_engine == "claude_agent":
                from app.services.claude_code_judge_agent import (
                    ClaudeCodeJudgeAgent,
                    write_session_file,
                )

                # 超长会话写入临时文件，裁判 agent 用 Read 工具分段阅读；短内容直接进 prompt
                # （步骤1.5 已为前处理脚本写过的 session_file 直接复用，不重复落盘）
                if session_file is None and session_markdown and len(session_markdown) > 30000:
                    data_id = (payload.get("_dataset_item", {}) or {}).get("id", payload.get("idx", 0))
                    session_file = write_session_file(str(task.id), data_id, session_markdown)

                # CC 引擎 system_prompt 只保留评分指令：claude-agent-sdk 以
                # --system-prompt argv 传 system_prompt，Linux 单参数上限
                # 131072B，携待评数据超限即 execve E2BIG（全裁判启动失败）。
                # targets 待评数据改由 _build_cc_targets_prompt 分流进
                # user prompt / 落盘文件
                system_prompt = prompt_builder._build_system_prompt(
                    user_input=judge_payload.get("user_input", ""),
                    ref_answer=judge_payload.get("ref_answer", ""),
                    model_output=model_output,
                    dataset_item=judge_payload.get("_dataset_item", {}),
                    payload=judge_payload,
                    chain_results=chain_results,
                    include_targets_content=False,
                )
                targets_prompt = self._build_cc_targets_prompt(
                    resolved_targets,
                    str(task.id),
                    payload,
                    session_file=session_file,
                    session_markdown=session_markdown,
                )
                if targets_prompt:
                    user_prompt = f"{user_prompt}\n\n{targets_prompt}" if user_prompt else targets_prompt

                cc_agent = ClaudeCodeJudgeAgent(
                    task=task,
                    langfuse_host=self.langfuse_host,
                )
                judge_results = cc_agent.run_parallel(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    session_file=session_file,
                    judge_count=task.judge_count or 1,
                )
                judge_results = self._renormalize_cc_judge_scores(
                    judge_results, scoring_metrics, task.max_score
                )
                judge_scores = judge_results
                result["judge_scores"] = judge_scores
                result = self._aggregate_judge_scores(result, judge_scores)
                result["success"] = True
                return result

            from app.services.judge_langgraph_agent import JudgeLangGraphAgent
            from app.services.judge_graph_state import build_state_input

            # 构建 user_variables 列表 (从 runtime_variables 和 judge_variable_config)
            user_variables = []
            jvc = getattr(task, 'judge_variable_config', None) or {}
            if isinstance(jvc, dict):
                for key, cfg in jvc.items():
                    if isinstance(cfg, dict):
                        name = cfg.get("name", key)
                        source = cfg.get("source", "")
                        if source == "model_output":
                            value = model_output
                        elif source == "dataset_field":
                            field = cfg.get("source_config", {}).get("field", "")
                            # 兼容 V2 数据集结构：字段可能嵌套在 data 子对象中
                            _dataset_item = payload.get("_dataset_item", {}) or {}
                            _dataset_data = _dataset_item.get("data", {}) if isinstance(_dataset_item, dict) else {}
                            raw_value = _dataset_item.get(field) or _dataset_data.get(field) or runtime_variables.get(key, "")
                            value = str(raw_value) if raw_value is not None else ""
                        elif source == "manual" or source == "manual_input":
                            value = str(cfg.get("source_config", {}).get("value", runtime_variables.get(key, "")))
                        elif source == "script_output":
                            # 前处理脚本输出（graph 裁判引擎变量体系，设计文档 §三）
                            from app.services.variable_injector import resolve_script_output_value

                            value = resolve_script_output_value(script_outputs, cfg, key)
                        else:
                            value = str(runtime_variables.get(key, ""))
                        user_variables.append({"name": name, "value": value})

            # 构建 state_input（overrides 携带 @ 标记解析后的 score_details /
            # evaluation_metrics，契约 v3 §三/§四：优先于 task 同名字段）
            resolved_content = content_to_validate
            state_input = build_state_input(
                task=task,
                resolved_targets_content=resolved_content,
                user_variables=user_variables,
                overrides=overrides,
            )

            # 执行 LangGraph Agent
            agent = JudgeLangGraphAgent(
                task=task,
                langfuse_host=self.langfuse_host,
            )
            judge_results = agent.run_parallel(
                state_input,
                judge_count=task.judge_count or 1,
            )

            # 兼容: 将结果赋值给原有变量
            judge_scores = judge_results

            result["judge_scores"] = judge_scores

            # ========== 步骤4: 聚合裁判评分 ==========
            result = self._aggregate_judge_scores(result, judge_scores)

            result["success"] = True

        except Exception as e:
            logger.exception("评测执行失败")
            result["error"] = str(e)
        finally:
            # 该条数据评测完成即清临时工作区（脚本工作区 + 裁判会话临时文件）；
            # execute_batch 结束的任务级清理保留为兜底（异常中断残留）
            self._cleanup_item_workspace(task, payload)

        return result

    @staticmethod
    def _cleanup_item_workspace(task, payload: Dict[str, Any]) -> None:
        """单条数据评测完成后清理其临时工作区（核心要求：每条建区、评完即清）

        清理范围：uploads/script_runs/{task_id}/{data_id}/（脚本 cwd/inline 输入/
        stdout/stderr 产物）与 uploads/judge_sessions/{task_id}_{data_id}_*.md
        （CC 裁判会话临时文件）。data_id 派生与 pre_judge_runner._run_step 一致
        （id 优先，idx 次之，兜底 "item"）。清理失败仅告警，不影响评测结果。
        """
        task_id = str(getattr(task, "id", "") or "unknown")
        dataset_item = payload.get("_dataset_item") or {}
        if not isinstance(dataset_item, dict):
            dataset_item = {}
        data_id = str(dataset_item.get("id") or payload.get("idx") or "item")
        try:
            from app.services.pre_judge_runner import cleanup_item_run_files

            cleanup_item_run_files(task_id, data_id)
        except Exception as e:
            logger.warning(f"[PreScripts] 清理本条脚本工作区失败: {e}")
        try:
            from app.services.claude_code_judge_agent import cleanup_session_files_for_item

            cleanup_session_files_for_item(task_id, data_id)
        except Exception as e:
            logger.warning(f"[CCJudge] 清理本条会话临时文件失败: {e}")

    def execute_batch_rag(
        self,
        payloads: List[Dict[str, Any]],
        task,
    ) -> List[Dict[str, Any]]:
        """
        批量执行 RAG 评测

        Args:
            payloads: 请求 Payload 列表
            task: 任务对象（需包含 rag_config）

        Returns:
            评测结果列表
        """
        results = []
        total = len(payloads)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {
                executor.submit(self._evaluate_single_rag, payload, task): (pos, payload)
                for pos, payload in enumerate(payloads)
            }

            for future in as_completed(future_to_idx):
                pos, payload = future_to_idx[future]
                try:
                    result = future.result()
                    results.append(result)
                    self._update_progress(len(results), total)
                except SoftTimeLimitExceeded:
                    # 软超时不得按行失败吞掉（P0-E/W3-4）：上抛触发收尾
                    raise
                except Exception as e:
                    logger.exception(f"RAG 评测执行失败 (idx={pos})")
                    results.append({
                        "idx": pos,
                        "error": str(e),
                        "success": False,
                    })

                # 行级 write-through（W3-2）：每行终态即时幂等落库
                self._persist_row_result(pos, payload, results[-1])

                if self.stop_event.is_set():
                    logger.info(f"RAG 任务 {self.task_id} 收到停止信号，已完成 {len(results)}/{total}")
                    for f in future_to_idx:
                        if not f.done():
                            f.cancel()
                    break

        results.sort(key=lambda x: x.get("idx", 0))
        return results

    def execute_batch_latency(
        self,
        payloads: List[Dict[str, Any]],
        task,
    ) -> List[Dict[str, Any]]:
        """
        批量执行耗时评测

        Args:
            payloads: 请求 Payload 列表
            task: 任务对象（需包含 latency_config）

        Returns:
            评测结果列表
        """
        # 获取重复次数配置
        latency_config = task.latency_config or {}
        repeat_count = latency_config.get("repeat_count", 1)

        # 如果 repeat_count > 1，展开 payloads：每个 payload 重复 repeat_count 次
        if repeat_count > 1:
            expanded_payloads = []
            for sample_idx, payload in enumerate(payloads):
                for rep_idx in range(repeat_count):
                    # 为每次重复创建独立的 payload 副本
                    expanded_payload = dict(payload)
                    expanded_payload["_sample_idx"] = sample_idx  # 追踪原始样本索引
                    expanded_payload["_latency_repeat_idx"] = rep_idx  # 追踪重复次数索引
                    expanded_payloads.append(expanded_payload)
            # W3 行级落库键改用展开后的全序序号：同一原始样本的多次重复行必须
            # 唯一（沿用 _orig_idx 样本序号会让同键 upsert 互相覆盖丢行）；
            # 展开规则由 repeat_count 唯一决定，同配置重跑序号稳定
            for expanded_pos, expanded_payload in enumerate(expanded_payloads):
                expanded_payload["_orig_idx"] = expanded_pos
            payloads = expanded_payloads
            logger.info(f"[execute_batch_latency] repeat_count={repeat_count}, 展开后 payloads 数量: {len(payloads)}")

        results = []
        total = len(payloads)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {
                executor.submit(self._evaluate_single_latency, payload, task): (pos, payload)
                for pos, payload in enumerate(payloads)
            }

            for future in as_completed(future_to_idx):
                pos, payload = future_to_idx[future]
                try:
                    result = future.result()
                    results.append(result)
                    self._update_progress(len(results), total)
                except SoftTimeLimitExceeded:
                    # 软超时不得按行失败吞掉（P0-E/W3-4）：上抛触发收尾
                    raise
                except Exception as e:
                    logger.exception(f"耗时评测执行失败 (idx={pos})")
                    results.append({
                        "idx": pos,
                        "error": str(e),
                        "success": False,
                    })

                # 行级 write-through（W3-2）：每行终态即时幂等落库
                self._persist_row_result(pos, payload, results[-1])

                if self.stop_event.is_set():
                    logger.info(f"耗时任务 {self.task_id} 收到停止信号，已完成 {len(results)}/{total}")
                    for f in future_to_idx:
                        if not f.done():
                            f.cancel()
                    break

        results.sort(key=lambda x: x.get("idx", 0))
        return results

    def _evaluate_single_latency(
        self,
        payload: Dict[str, Any],
        task,
    ) -> Dict[str, Any]:
        """
        执行单条耗时评测

        步骤：
        1. 获取模型配置
        2. 调用模型并计时
        3. 提取 token 使用量
        4. 构建耗时记录

        Args:
            payload: 请求 Payload
            task: 任务对象

        Returns:
            评测结果字典
        """
        import time
        from app.services.model_caller import ModelCallerSync

        if self.stop_event.is_set():
            sample_idx = payload.get("_sample_idx")
            return {"idx": sample_idx if sample_idx is not None else payload.get("idx", 0), "success": False, "error": "任务已停止"}

        sample_idx = payload.get("_sample_idx")
        result = {"idx": sample_idx if sample_idx is not None else payload.get("idx", 0), "success": False}

        try:
            # 获取模型配置：W4 快照优先（口径字段来自创建时快照，凭据实时解析），
            # 快照为空（存量任务）回退实时查询
            from app.services.config_snapshot import resolve_model_call_config

            model_config, _config_source = resolve_model_call_config(task)

            messages = [{"role": "user", "content": payload.get("user_input", "")}]

            # 提取运行时变量
            runtime_variables = {
                k: v for k, v in payload.items()
                if k not in ("user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx")
            }

            # 处理 runtime_variables 中的 { value, type } 格式
            for key, val in list(runtime_variables.items()):
                if isinstance(val, dict) and 'value' in val and 'type' in val:
                    var_type = val.get('type', 'string')
                    var_value = val.get('value', '')
                    if var_type == 'number':
                        try:
                            runtime_variables[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                        except (ValueError, TypeError):
                            runtime_variables[key] = 0
                    elif var_type == 'boolean':
                        runtime_variables[key] = str(var_value).lower() in ('true', '1', 'yes')
                    else:
                        runtime_variables[key] = str(var_value)

            # 调用模型并计时
            model_caller = ModelCallerSync()
            start_time = time.perf_counter()
            call_result = model_caller.call(messages, model_config, runtime_variables)
            end_time = time.perf_counter()

            duration = end_time - start_time

            if not call_result.get("success"):
                raise RuntimeError(f"模型调用失败: {call_result.get('error', '未知错误')}")

            # 获取token使用量（如果有）
            input_tokens = None
            output_tokens = None
            if call_result.get("raw", {}).get("usage"):
                usage = call_result["raw"]["usage"]
                input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
                output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

            # 耗时评测核心
            latency_config = task.latency_config or {}
            threshold = latency_config.get("threshold", 10.0)

            result.update({
                "success": True,
                "duration": duration,
                "passed": duration <= threshold,
                "threshold": threshold,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            })

        except Exception as e:
            logger.exception("耗时评测执行失败")
            result["error"] = str(e)

        return result

    def _evaluate_single_rag(
        self,
        payload: Dict[str, Any],
        task,
    ) -> Dict[str, Any]:
        """
        执行单条 RAG 评测

        步骤：
        1. 获取初始 question/ground_truth（从 payload 或 judge_variable_config）
        2. 调用被评测模型获取 model_output 和 chain_results
        3. 用 model_output 和 chain_results 解析 judge_variable_config 中的所有变量
        4. 获取 contexts（根据配置：model_output / rag_endpoint / 其他来源）
        5. 调用 RAGEvaluator 计算指标
        6. 返回标准化结果

        Args:
            payload: 请求 Payload（包含 question/ref_answer）
            task: 任务对象

        Returns:
            评测结果字典
        """
        from app.services.rag_evaluator import RAGEvaluator
        from app.services.model_caller import ModelCallerSync

        if self.stop_event.is_set():
            return {"idx": payload.get("idx", 0), "success": False, "error": "任务已停止"}

        result = {
            "idx": payload.get("idx", 0),
            "success": False,
        }

        try:
            # ========== 步骤1: 获取初始 question 和 ground_truth（用于调用模型） ==========
            # 先从 payload 获取默认值，后续用 judge_variable_config 覆盖
            question = payload.get("user_input") or payload.get("question", "")
            ground_truth = payload.get("ref_answer", "")

            # 解析 judge_variable_config 中不需要 model_output 的变量（dataset_field/manual）
            judge_var_config = task.judge_variable_config or {}
            contexts_cfg = None  # contexts 配置，可能依赖 model_output
            pb = None
            chain_results = []  # 请求链结果

            if judge_var_config:
                from app.services.prompt_builder import PromptBuilder
                pb = PromptBuilder(task)
                if isinstance(judge_var_config, list):
                    for item in judge_var_config:
                        if not isinstance(item, dict):
                            continue
                        item_key = item.get("key", "")
                        source = item.get("source", "")
                        # 只处理不依赖 model_output 的变量（dataset_field, manual, chain_result）
                        if item_key in ("question", "ground_truth") and source != "model_output":
                            val, _ = pb._resolve_judge_variable(item, payload, "", chain_results)
                            if val:
                                if item_key == "question":
                                    question = val
                                elif item_key == "ground_truth":
                                    ground_truth = val
                        elif item_key == "contexts":
                            contexts_cfg = item
                elif isinstance(judge_var_config, dict):
                    for key, cfg in judge_var_config.items():
                        if not isinstance(cfg, dict):
                            continue
                        source = cfg.get("source", "")
                        if key in ("question", "ground_truth") and source != "model_output":
                            val, _ = pb._resolve_judge_variable(cfg, payload, "", chain_results)
                            if val:
                                if key == "question":
                                    question = val
                                elif key == "ground_truth":
                                    ground_truth = val
                        elif key == "contexts":
                            contexts_cfg = cfg

            rag_config = task.rag_config or {}
            metrics_list = rag_config.get("metrics")
            rag_endpoint = rag_config.get("rag_endpoint", "").strip()
            model_output = ""
            model_config = {}

            # ========== 步骤2: 调用被评测模型获取 model_output 和 chain_results ==========
            # 始终调用被评测模型（因为 judge_variable 可能依赖 chain_results）
            messages = [{"role": "user", "content": question}]
            # W4 快照优先（口径字段来自创建时快照，凭据实时解析），快照为空
            # （存量任务）回退实时查询
            from app.services.config_snapshot import resolve_model_call_config

            model_config, _config_source = resolve_model_call_config(task)

            if not model_config:
                raise ValueError("RAG 评测未配置 rag_endpoint 且未配置被评测模型，请至少配置一项")

            runtime_variables = {
                k: v for k, v in payload.items()
                if k not in ("user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx")
            }

            # 处理 runtime_variables 中的 { value, type } 格式
            for key, val in list(runtime_variables.items()):
                if isinstance(val, dict) and 'value' in val and 'type' in val:
                    var_type = val.get('type', 'string')
                    var_value = val.get('value', '')
                    if var_type == 'number':
                        try:
                            runtime_variables[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                        except (ValueError, TypeError):
                            runtime_variables[key] = 0
                    elif var_type == 'boolean':
                        runtime_variables[key] = str(var_value).lower() in ('true', '1', 'yes')
                    else:
                        runtime_variables[key] = str(var_value)

            model_caller = ModelCallerSync()
            call_result = model_caller.call(messages, model_config, runtime_variables)
            if not call_result.get("success"):
                raise RuntimeError(f"模型调用失败: {call_result.get('error', '未知错误')}")
            model_output = call_result.get("content", "")

            # 从模型调用结果中提取 chain_results
            chain_results = call_result.get("chain_results", [])
            if chain_results:
                logger.info(f"从模型调用中提取 chain_results，请求数: {len(chain_results)}")
                for cr in chain_results:
                    logger.info(f"  - chain_index={cr.get('chain_index')}, name={cr.get('name')}, content长度={len(cr.get('content', ''))}")

            # 兼容旧格式：如果没有 chain_results 但有 all_results，转换格式
            if not chain_results:
                all_results = call_result.get("all_results", {})
                if all_results:
                    chain_results = [{"chain_index": 0, "all_results": all_results}]
                    logger.info(f"从模型调用中提取 all_results（兼容模式），变量数: {len(all_results)}")

            if self.stop_event.is_set():
                return {"idx": payload.get("idx", 0), "success": False, "error": "任务已停止"}

            # ========== 步骤3: 用 model_output 和 chain_results 解析 judge_variable_config 中的所有变量 ==========
            # 包括 question/ground_truth 可能配置为从 model_output 或 chain_result 获取的情况
            if pb and judge_var_config:
                if isinstance(judge_var_config, list):
                    for item in judge_var_config:
                        if not isinstance(item, dict):
                            continue
                        item_key = item.get("key", "")
                        if item_key == "question":
                            val, _ = pb._resolve_judge_variable(item, payload, model_output, chain_results)
                            if val:
                                question = val
                        elif item_key == "ground_truth":
                            val, _ = pb._resolve_judge_variable(item, payload, model_output, chain_results)
                            if val:
                                ground_truth = val
                        elif item_key == "contexts":
                            contexts_cfg = item
                elif isinstance(judge_var_config, dict):
                    for key, cfg in judge_var_config.items():
                        if not isinstance(cfg, dict):
                            continue
                        if key == "question":
                            val, _ = pb._resolve_judge_variable(cfg, payload, model_output, chain_results)
                            if val:
                                question = val
                        elif key == "ground_truth":
                            val, _ = pb._resolve_judge_variable(cfg, payload, model_output, chain_results)
                            if val:
                                ground_truth = val
                        elif key == "contexts":
                            contexts_cfg = cfg

            # ========== 步骤4: 获取 contexts ==========
            # 优先级：judge_variable_config（非 model_output 来源）> rag_endpoint > model_output
            contexts = []
            if contexts_cfg and pb and contexts_cfg.get("source") not in ("model_output", None):
                # 从指定来源获取 contexts
                import json as _json
                val, _ = pb._resolve_judge_variable(contexts_cfg, payload, model_output, chain_results)
                if val:
                    try:
                        parsed = _json.loads(val)
                        contexts = [str(c) for c in parsed] if isinstance(parsed, list) else [str(parsed)]
                    except (_json.JSONDecodeError, TypeError):
                        contexts = [val]
                    logger.info(f"从 judge_variable_config(contexts) 获取 contexts: {len(contexts)} 条")
            elif rag_endpoint:
                # 使用 RAG 接口获取 contexts
                logger.info(f"调用 RAG 接口: task={self.task_id}, question_len={len(question)}")
                contexts = _call_rag_api(
                    endpoint=rag_endpoint,
                    api_key=rag_config.get("rag_api_key", ""),
                    question=question,
                    top_k=rag_config.get("rag_top_k", 5),
                )
            else:
                # 使用模型输出作为 contexts
                contexts = [model_output] if model_output else []

            # ========== 步骤5: 调用 RAGEvaluator 计算指标 ==========
            logger.info(f"计算 RAG 指标: task={self.task_id}, contexts_count={len(contexts)}")

            # 优先用 judge_model（裁判模型）作为 LLM 裁判配置，fallback 到被评测模型
            judge_llm_config = {}
            if task.judge_model:
                try:
                    from app.models.model_config_models import ModelConfig as ModelConfigModel

                    db = SessionLocal()
                    try:
                        judge_model_obj = db.query(ModelConfigModel).filter(
                            ModelConfigModel.id == task.judge_model
                        ).first()
                        if judge_model_obj:
                            judge_llm_config = {
                                "provider": judge_model_obj.provider,
                                "model_type": judge_model_obj.model_type,
                                "endpoint": judge_model_obj.endpoint,
                                "api_key": judge_model_obj.api_key,
                                "auth_type": judge_model_obj.auth_type,
                                "auth_config": judge_model_obj.auth_config,
                                "request_config": judge_model_obj.request_config,
                                "response_parse_config": judge_model_obj.response_parse_config,
                            }
                            logger.info(f"RAG LLM裁判使用 judge_model: provider={judge_llm_config.get('provider')}, model={judge_llm_config.get('model_type')}")
                    finally:
                        db.close()
                except Exception as e:
                    logger.warning(f"加载 judge_model 配置失败，回退到被评测模型配置: {e}")

            llm_config = judge_llm_config if judge_llm_config else model_config
            evaluator = RAGEvaluator(llm_config=llm_config)
            eval_result = evaluator.evaluate(
                question=question,
                ground_truth=ground_truth,
                contexts=contexts,
                metrics_list=metrics_list,
            )

            # ========== 步骤6: 返回标准化结果 ==========
            # 平铺 payload 中的输入变量（不覆盖已解析的 question/ground_truth）
            input_variables = {k: v for k, v in payload.items()
                               if k not in ("_dataset_item", "idx", "user_input", "ref_answer", "question", "ground_truth") and not k.startswith("_")}
            result.update(input_variables)

            result.update({
                "success": True,
                "question": question,
                "ground_truth": ground_truth,
                "model_answer": model_output,
                "contexts": contexts,
                "scores": eval_result.get("scores", {}),
                "reasons": eval_result.get("reasons", {}),
            })

        except Exception as e:
            logger.exception("RAG 评测执行失败")
            result["error"] = str(e)

        return result

    def _format_model_raw_response(self, call_result: Dict[str, Any], chain_results: List[Dict]) -> str:
        """格式化模型原始响应

        Args:
            call_result: 模型调用结果
            chain_results: 请求链结果列表

        Returns:
            格式化的原始响应字符串
        """
        import json

        if chain_results:
            # 请求链模式：展示请求链的完整执行结果
            chain_info = []
            for cr in chain_results:
                chain_info.append({
                    "name": cr.get("name", ""),
                    "content": cr.get("content", ""),
                    "variables": cr.get("variables", {}),
                })
            return json.dumps(chain_info, ensure_ascii=False, indent=2)
        else:
            # 非请求链：直接返回 content
            return call_result.get("content", "")

    def _format_input_data(self, runtime_variables: Dict[str, Any], payload: Dict[str, Any]) -> str:
        """格式化输入数据（变量注入配置的字段内容）

        Args:
            runtime_variables: 运行时变量字典
            payload: 原始payload字典

        Returns:
            格式化的输入数据字符串
        """
        import json

        # 收集需要展示的输入数据
        input_data = {}

        # 添加 runtime_variables 中的所有变量
        for key, value in runtime_variables.items():
            if value is not None:
                input_data[key] = value

        # 如果 runtime_variables 为空，至少展示 user_input
        if not input_data:
            input_data["user_input"] = payload.get("user_input", "")

        return json.dumps(input_data, ensure_ascii=False, indent=2)

    def _update_progress(self, completed: int, total: int):
        """更新任务进度（W3 断点续跑：叠加本轮已跳过的已完成行偏移，进度为累计值）

        W10-4：进度推进即一批行完成，同步刷新执行记录心跳（heartbeat_at）——
        API 启动期收尸 reaper 以心跳区分「正在执行」与「worker 已死」的
        running 任务，部署重启不再误标。
        """
        if not self.task_id:
            return

        try:
            from app.services.evaluation_task_service import EvaluationTaskService

            db = SessionLocal()
            try:
                service = EvaluationTaskService(db)
                service.update_progress(
                    self.task_id,
                    self.progress_offset + completed,
                    self.progress_offset + total,
                )
                # W10-4 执行心跳：与进度同频刷新（本执行链路的 execution_number；
                # None 表示非任务链路，touch 内部跳过）
                service.touch_heartbeat(self.task_id, self.execution_number)
            finally:
                db.close()

        except Exception as e:
            logger.warning(f"更新进度失败: {e}")


def _call_rag_api(
    endpoint: str,
    api_key: str,
    question: str,
    top_k: int = 5,
) -> list:
    """
    调用 RAG 接口获取检索上下文

    Args:
        endpoint: RAG 接口地址
        api_key: RAG 接口密钥
        question: 用户问题
        top_k: 返回的上下文数量

    Returns:
        contexts 列表（字符串列表）
    """
    if not endpoint:
        raise ValueError("RAG endpoint 未配置")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"question": question, "top_k": top_k}

    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # 兼容多种响应格式
        # 格式1: {"contexts": ["ctx1", "ctx2", ...]}
        # 格式2: {"data": ["ctx1", "ctx2", ...]}
        # 格式3: ["ctx1", "ctx2", ...]
        if isinstance(data, list):
            contexts = data
        elif isinstance(data, dict):
            contexts = (
                data.get("contexts")
                or data.get("data")
                or data.get("results")
                or []
            )
        else:
            contexts = []

        # 确保每个 context 是字符串
        return [str(ctx) if not isinstance(ctx, str) else ctx for ctx in contexts]

    except requests.exceptions.Timeout:
        raise RuntimeError(f"RAG 接口请求超时: {endpoint}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"RAG 接口请求失败: {e}")
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"RAG 接口响应解析失败: {e}")
